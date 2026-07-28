"""Independent E1 correctness-audit regressions (red flags 1-7).

Each test fails on the pre-audit behavior: the leaky fragment blocklist, the
full-row `Observation` (provenance leak), the ULID status tie-break, and the
sportsbook cross-event/DQ timelines.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.ids import new_match_decision_id, new_team_game_stat_id
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.references import LinkOutcome
from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository
from sports_quant.pit import (
    AsOfAmbiguityError,
    AsOfReader,
    Cutoff,
    ForbiddenColumnError,
    deterministic_json,
    latest_as_of,
)

from .conftest import (
    CUTOFF,
    T1,
    T2,
    Ctx,
    seed_dq,
    seed_sb_outcome_ctx,
    seed_status,
    seed_team_stat,
)

_FAR = "2999-01-01T00:00:00.000000Z"


def _reader(conn: sqlite3.Connection, at: str = CUTOFF) -> AsOfReader:
    return AsOfReader(conn, Cutoff.parse(at))


# --------------------------------------------------------------------------- #
# RF1 -- positive-allowlist WHERE fragment
# --------------------------------------------------------------------------- #
def test_fragment_allowlist_rejects_bypasses(conn: sqlite3.Connection) -> None:
    cutoff = Cutoff.parse(CUTOFF)
    for bad in (
        "game_ref_id = ? OR 1=1",                       # plain OR (no comment)
        "game_ref_id = ? OR provider_team_id = 'x'",
        'game_ref_id = "team_id"',                       # quoted identifier
        "game_ref_id = ? , team_id = ?",                 # comma
        "game_ref_id LIKE ?",                            # LIKE
        "game_ref_id GLOB ?",                            # GLOB
        "game_ref_id = ? COLLATE NOCASE",                # COLLATE
        "1=1",                                           # no identifier
        "game_ref_id > ?",                               # non-equality operator
        "game_ref_id = randomblob(1)",                   # function
    ):
        with pytest.raises(ValueError):
            latest_as_of(conn, table="team_game_statistics", cutoff=cutoff, anchor_where=bad,
                         anchor_params=("x",))


def test_fragment_allowlist_accepts_legit_forms(conn: sqlite3.Connection, ctx: Ctx) -> None:
    # The forms the typed accessors actually use must pass (incl. the weather
    # literal/IS-NULL conjunction).
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=4)
    assert _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id) is not None
    from sports_quant.pit.asof import _validate_fragment
    _validate_fragment("weather_kind = 'current_forecast' AND pit_eligible = 1 "
                       "AND forecast_mode = ? AND valid_time IS NULL")
    _validate_fragment("game_id = ?")


# --------------------------------------------------------------------------- #
# RF2 -- provenance/audit columns never surface as features
# --------------------------------------------------------------------------- #
_PROVENANCE = {"stat_id", "content_hash", "created_at", "ingested_at", "run_id",
               "raw_response_id", "raw_response_hash", "provider", "provider_game_id",
               "provider_team_id", "provider_timestamp", "published_at", "team_id"}


def test_observation_excludes_provenance_columns(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=6)
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None
    exposed = set(obs.as_dict())
    assert exposed == {"home_away", "runs", "hits", "errors", "at_bats", "extra"}
    assert not (exposed & _PROVENANCE)  # no audit/provider/provenance field leaks
    for banned in _PROVENANCE:
        with pytest.raises(KeyError):
            obs.get(banned)


def test_observation_fails_closed_without_feature_policy(conn: sqlite3.Connection) -> None:
    # sportsbook_price_snapshots is asof_filtered but has NO feature-column policy
    # (prices are read only through the typed price accessor) -> observation() fails
    # closed rather than dumping every column.
    with pytest.raises(ForbiddenColumnError):
        _reader(conn).observation("sportsbook_price_snapshots",
                                  anchor_where="sb_outcome_id = ?", anchor_params=("x",))


# --------------------------------------------------------------------------- #
# RF3 -- status equal-time conflict fails closed (not a ULID winner)
# --------------------------------------------------------------------------- #
def test_status_equal_time_provider_conflict_fails_closed(conn: sqlite3.Connection,
                                                          ctx: Ctx) -> None:
    # Two providers report DIFFERENT status at the SAME observed_at (allowed by the
    # table's UNIQUE, which includes provider). The winner must NOT be the max
    # status_id (ULID) -> fail closed.
    seed_status(conn, game_id=ctx.game_id, status="scheduled", observed_at=T1,
                provider="mlb_statsapi")
    seed_status(conn, game_id=ctx.game_id, status="postponed", observed_at=T1,
                provider="balldontlie")
    reader = _reader(conn, T2)
    with pytest.raises(AsOfAmbiguityError):
        reader.game_status(ctx.game_id)
    with pytest.raises(AsOfAmbiguityError):
        reader.game_schedule_state(ctx.game_id)


# --------------------------------------------------------------------------- #
# RF6 -- feature object is rebuild-stable (content-hash determined)
# --------------------------------------------------------------------------- #
def _insert_one_stat(conn: sqlite3.Connection, *, content_hash: str, runs: int,
                     created_at: str) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO team_game_statistics (stat_id, game_ref_id, provider, provider_game_id, "
        "provider_team_id, team_id, home_away, runs, observed_at, ingested_at, raw_response_id, "
        "raw_response_hash, content_hash, created_at) "
        "VALUES (?, 'GR', 'p', 'G', 'PT', 'TID', 'home', ?, ?, ?, 'rr', 'rh', ?, ?)",
        (new_team_game_stat_id(), runs, T1, T1, content_hash, created_at))
    conn.commit()


def test_feature_object_rebuild_stable_across_fresh_dbs(tmp_path: Path) -> None:
    serialized = set()
    for i in range(4):
        p = tmp_path / f"c{i}.db"
        initialize_database(p)
        with Database(p).connection() as conn:
            # Same feature content; DIFFERENT fresh ULID id and created_at each DB.
            _insert_one_stat(conn, content_hash="HCONTENT", runs=5,
                             created_at=f"2026-07-0{i + 1}T00:00:00.000000Z")
        with Database(p).connection() as conn:
            obs = latest_as_of(conn, table="team_game_statistics", cutoff=Cutoff.parse(CUTOFF),
                               anchor_where="game_ref_id = ? AND provider_team_id = ?",
                               anchor_params=("GR", "PT"))
            reader = AsOfReader(conn, Cutoff.parse(CUTOFF))
            fobs = reader.team_game_statistics("GR", "TID")
            assert obs is not None and fobs is not None
        serialized.add(deterministic_json(fobs))
    assert len(serialized) == 1  # identical feature serialization despite differing provenance


# --------------------------------------------------------------------------- #
# RF5 -- sportsbook cross-event + DQ timelines valid at the cutoff
# --------------------------------------------------------------------------- #
def _insert_decision(conn: sqlite3.Connection, *, source_ref: str, game_id: str,
                     decided_at: str) -> str:
    mid = new_match_decision_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'sportsbook_event', 'the_odds_api', ?, ?, 'accepted', 'exact', 1.0, 0.85, 't', "
            "0, ?, ?)", (mid, source_ref, game_id, decided_at, decided_at))
    return mid


def _link(conn: sqlite3.Connection, *, sb_event_id: str, game_id: str, decision_id: str,
          orientation: str) -> None:
    with transaction(conn):
        assert SqliteSportsbookRepository(conn).link_game(
            sb_event_id=sb_event_id, game_id=game_id, match_decision_id=decision_id,
            orientation=orientation) is LinkOutcome.LINKED


def test_future_cross_event_conflict_does_not_unapprove_earlier(conn: sqlite3.Connection,
                                                                ctx: Ctx) -> None:
    from .conftest import seed_sb_event, seed_sb_market, seed_sb_outcome
    e1 = seed_sb_event(conn, provider_event_id="E1", sport_key="baseball_mlb",
                       commence_time="2026-07-15T02:10:00Z", home_team_raw="LAD", away_team_raw="SD")
    seed_sb_market(conn, sb_event_id=e1, market_key="h2h")
    d1 = _insert_decision(conn, source_ref=e1, game_id=ctx.game_id, decided_at=T1)
    _link(conn, sb_event_id=e1, game_id=ctx.game_id, decision_id=d1, orientation="direct")
    # A SECOND event, swapped, whose decision is decided in the future (T2).
    e2 = seed_sb_event(conn, provider_event_id="E2", sport_key="baseball_mlb",
                       commence_time="2026-07-15T02:10:00Z", home_team_raw="SD", away_team_raw="LAD")
    seed_sb_market(conn, sb_event_id=e2, market_key="h2h")
    d2 = _insert_decision(conn, source_ref=e2, game_id=ctx.game_id, decided_at=T2)
    _link(conn, sb_event_id=e2, game_id=ctx.game_id, decision_id=d2, orientation="swapped")
    mid_cut = "2026-07-13T00:00:00.000000Z"  # after d1(T1), before d2(T2)
    assert AsOfReader(conn, Cutoff.parse(mid_cut)).sportsbook_event_game(e1) is not None
    # Once the conflicting swapped decision is known, the game orientation is contested.
    assert AsOfReader(conn, Cutoff.parse(_FAR)).sportsbook_event_game(e1) is None
    _ = seed_sb_outcome


def test_sportsbook_dq_resolved_after_cutoff_still_blocks(conn: sqlite3.Connection,
                                                          ctx: Ctx) -> None:
    from .conftest import link_sb_event
    seed_sb_outcome_ctx(conn)
    ev = str(conn.execute("SELECT sb_event_id FROM sportsbook_events LIMIT 1").fetchone()[0])
    link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id, orientation="direct")
    # Blocking identity DQ active from T1, resolved only at 2040.
    seed_dq(conn, rule_code="DQ-MATCH-003", entity_type="sportsbook_event", entity_id=ev,
            severity="blocking", detected_at=T1, resolved_at="2040-01-01T00:00:00.000000Z")
    # A cutoff between the resolution... a cutoff before resolution sees it active.
    assert AsOfReader(conn, Cutoff.parse("2039-01-01T00:00:00.000000Z")
                      ).sportsbook_event_game(ev) is None
    # After the resolution, it no longer blocks (decision decided ~now < 2041).
    assert AsOfReader(conn, Cutoff.parse("2041-01-01T00:00:00.000000Z")
                      ).sportsbook_event_game(ev) is not None
