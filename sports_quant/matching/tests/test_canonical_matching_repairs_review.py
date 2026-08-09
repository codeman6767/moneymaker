"""Independent review of the three canonical-matching repairs (`fae7650`).

Three further defects were found by this review and are pinned here, each with
its reproducer written before the fix:

* **Broken team links grew audit history without bound.** The repair reported
  `DQ-MATCH-017` and then fell through to `_record_decision`, so every rerun over
  the same corruption appended another *accepted* team decision and candidate.
  The DQ row deduplicated; the accepted decision did not.
* **`resolve_canonical` did not bind the decision to its source.** A reference
  pointed at *another* reference's accepted decision for the same canonical
  entity resolved as fully justified.
* **The point-in-time gate compared timestamps as strings.** Valid ISO cutoffs
  whose lexical order differs from chronological order were mis-gated, and a
  malformed cutoff resolved instead of failing closed.

Offline only; nothing here opens a socket.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.identity import SqliteProviderIdentityRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.resolution import resolve_canonical, resolve_many
from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE, MatchGamesService

from .conftest import T0, raw_response, seed_player_ref, seed_schedule, seed_team

MLB = "mlb_statsapi"
NBA = "balldontlie"


def _fresh(tmp_path: Path, tag: str) -> sqlite3.Connection:
    path = tmp_path / f"{tag}.db"
    initialize_database(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ident_player(conn, provider, pid, league_id, name):
    rid, rh = raw_response(conn, marker=f"pi:{provider}:{pid}:{name}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_player(
            provider=provider, provider_player_id=pid, league_id=league_id,
            full_name=name, observed_at=T0, raw_response_id=rid, raw_response_hash=rh)


def _ident_team(conn, provider, tid, league_id, name):
    rid, rh = raw_response(conn, marker=f"ti:{provider}:{tid}:{name}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_team(
            provider=provider, provider_team_id=tid, league_id=league_id,
            full_name=name, observed_at=T0, raw_response_id=rid, raw_response_hash=rh)


def _team_ref(conn, provider, tid):
    rid, rh = raw_response(conn, marker=f"tr:{provider}:{tid}")
    with transaction(conn):
        SqliteProviderReferenceRepository(conn).upsert(
            kind="team", provider=provider, provider_entity_id=tid,
            raw_response_id=rid, raw_response_hash=rh, observed_at=T0)


def _seed_mlb_game(conn) -> None:
    seed_team(conn, league_code="MLB", abbreviation="TOR",
              canonical_name="Toronto Blue Jays", city="Toronto", nickname="Blue Jays")
    seed_team(conn, league_code="MLB", abbreviation="TB",
              canonical_name="Tampa Bay Rays", city="Tampa Bay", nickname="Rays")
    for tid, nm in (("141", "Toronto Blue Jays"), ("139", "Tampa Bay Rays")):
        _team_ref(conn, MLB, tid)
        _ident_team(conn, MLB, tid, "lg_mlb", nm)
    seed_schedule(conn, provider=MLB, provider_game_id="777001",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00.000000Z", season=2026,
                  game_date_local="2026-07-24")


def _match_games(conn, provider=MLB):
    return MatchGamesService(conn).match_range(
        provider=provider, from_date="2026-07-24", to_date="2026-07-24")


def _counts(conn) -> dict:
    def q(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "team_accepted": q("SELECT COUNT(*) FROM entity_match_decisions "
                           "WHERE outcome='accepted' AND entity_type='team'"),
        "candidates": q("SELECT COUNT(*) FROM match_candidates"),
        "dq017": q("SELECT COUNT(*) FROM data_quality_issues "
                   "WHERE rule_code='DQ-MATCH-017'"),
    }


# --------------------------------------------------------------------------- #
# Review defect A -- a broken link must not grow audit history on every rerun
# --------------------------------------------------------------------------- #

def test_broken_team_link_does_not_grow_audit_history(tmp_path: Path) -> None:
    """The reproducer: reruns over one unchanged corruption must converge.

    Reporting the break is right; re-recording an *accepted* team match for a
    link that is known to be broken is not, and it grew by one decision and one
    candidate on every rerun.
    """

    conn = _fresh(tmp_path, "broken_growth")
    _seed_mlb_game(conn)
    _match_games(conn)
    conn.execute("UPDATE provider_team_references SET match_decision_id = NULL "
                 "WHERE provider_team_id = '141'")
    conn.commit()

    _match_games(conn)
    after_first = _counts(conn)
    assert after_first["dq017"] == 1, "the break must be reported"

    for _ in range(4):
        _match_games(conn)
    assert _counts(conn) == after_first, (
        f"repeated identical corruption grew the audit log: "
        f"{after_first} -> {_counts(conn)}")
    conn.close()


def test_broken_link_is_reported_not_repaired_and_not_replayed(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "broken_semantics")
    _seed_mlb_game(conn)
    _match_games(conn)
    conn.execute("UPDATE provider_team_references SET match_decision_id = NULL "
                 "WHERE provider_team_id = '141'")
    conn.commit()
    result = _match_games(conn)
    assert conn.execute(
        "SELECT match_decision_id FROM provider_team_references "
        "WHERE provider_team_id = '141'").fetchone()[0] is None, "silently repaired"
    assert result.counters.blocking_issues > 0, "a broken link must be blocking"
    conn.close()


def test_a_healthy_link_alongside_a_broken_one_still_replays(tmp_path: Path) -> None:
    """The break must be scoped to the corrupt reference, not the whole run."""

    conn = _fresh(tmp_path, "broken_scoped")
    _seed_mlb_game(conn)
    _match_games(conn)
    conn.execute("UPDATE provider_team_references SET match_decision_id = NULL "
                 "WHERE provider_team_id = '141'")
    conn.commit()
    before = _counts(conn)
    result = _match_games(conn)
    # team 139 is untouched and must still be a clean replay
    assert result.counters.decisions_replayed >= 1
    assert _counts(conn)["team_accepted"] == before["team_accepted"]
    conn.close()


# --------------------------------------------------------------------------- #
# Review defect B -- the decision must be bound to ITS OWN source reference
# --------------------------------------------------------------------------- #

def _one_matched_player(conn, pid: str = "2001", name: str = "Gamma Player"):
    seed_player_ref(conn, provider=MLB, provider_player_id=pid)
    _ident_player(conn, MLB, pid, "lg_mlb", name)
    MatchPlayersService(conn).match_range(provider=MLB, season_year=2026)
    row = conn.execute(
        "SELECT player_id, match_decision_id FROM provider_player_references "
        "WHERE provider_player_id = ?", (pid,)).fetchone()
    return str(row["player_id"]), str(row["match_decision_id"])


def test_decision_recorded_for_another_reference_does_not_justify_this_link(
        tmp_path: Path) -> None:
    """The reproducer: same canonical target is NOT enough.

    A second reference pointed at the first reference's accepted decision has no
    decision of its own. Matching canonical ids only means both happen to name
    the same player -- the decision never adjudicated this provider id.
    """

    conn = _fresh(tmp_path, "srcbind")
    canonical, decision_id = _one_matched_player(conn)
    seed_player_ref(conn, provider=MLB, provider_player_id="2002")
    conn.execute("UPDATE provider_player_references SET player_id = ?, "
                 "match_decision_id = ? WHERE provider_player_id = '2002'",
                 (canonical, decision_id))
    conn.commit()
    src = conn.execute("SELECT source_ref FROM entity_match_decisions WHERE match_id = ?",
                       (decision_id,)).fetchone()[0]
    assert src == "2001", "fixture: the decision belongs to 2001"

    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id="2002") is None, (
        "a decision recorded for another source reference must not justify this link")
    # the legitimate one still resolves
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id="2001") is not None
    conn.close()


def test_decision_from_another_provider_does_not_justify_the_link(
        tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "srcbind_provider")
    canonical, decision_id = _one_matched_player(conn)
    rid, rh = raw_response(conn, marker="otherprov")
    with transaction(conn):
        SqliteProviderReferenceRepository(conn).upsert(
            kind="player", provider="odds_api", provider_entity_id="2001",
            raw_response_id=rid, raw_response_hash=rh, observed_at=T0)
    conn.execute("UPDATE provider_player_references SET player_id = ?, "
                 "match_decision_id = ? WHERE provider = 'odds_api'",
                 (canonical, decision_id))
    conn.commit()
    assert resolve_canonical(conn, kind="player", provider="odds_api",
                             provider_entity_id="2001") is None, (
        "an mlb_statsapi decision must not justify an odds_api link")
    conn.close()


def test_team_link_state_also_binds_the_source(tmp_path: Path) -> None:
    """`_existing_team_link_state` must apply the same binding."""

    conn = _fresh(tmp_path, "srcbind_team")
    _seed_mlb_game(conn)
    _match_games(conn)
    other = conn.execute(
        "SELECT match_decision_id FROM provider_team_references "
        "WHERE provider_team_id = '139'").fetchone()[0]
    conn.execute("UPDATE provider_team_references SET match_decision_id = ? "
                 "WHERE provider_team_id = '141'", (other,))
    conn.commit()
    _match_games(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code='DQ-MATCH-017'"
    ).fetchone()[0] > 0, "a decision belonging to another team must be reported"
    conn.close()


# --------------------------------------------------------------------------- #
# Review defect C -- the PIT gate must compare instants, not strings
# --------------------------------------------------------------------------- #

def test_equivalent_instant_with_offset_notation_resolves(tmp_path: Path) -> None:
    """The reproducer: ``+00:00`` is the same instant as ``Z``."""

    conn = _fresh(tmp_path, "ts_offset")
    _canonical, _decision = _one_matched_player(conn, "3001", "Delta Player")
    link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3001")
    assert link is not None
    same_instant = link.decided_at.replace("Z", "+00:00")
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3001",
                             as_of=same_instant) is not None, (
        "the same instant written with an explicit offset must be inclusive")
    conn.close()


def test_lower_precision_cutoff_before_the_decision_is_excluded(tmp_path: Path) -> None:
    """A truncated-second cutoff is EARLIER than a sub-second decision."""

    conn = _fresh(tmp_path, "ts_precision")
    _canonical, _decision = _one_matched_player(conn, "3002", "Epsilon Player")
    link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3002")
    assert link is not None
    if link.decided_at.split(".")[1].rstrip("Z") == "000000":
        pytest.skip("decision landed exactly on a whole second")
    truncated = link.decided_at.split(".")[0] + "Z"
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3002",
                             as_of=truncated) is None, (
        "a whole-second cutoff precedes a sub-second decision")
    conn.close()


def test_offset_cutoff_is_compared_chronologically(tmp_path: Path) -> None:
    """A cutoff in a non-UTC offset must be converted, not string-compared."""

    conn = _fresh(tmp_path, "ts_zone")
    _canonical, _decision = _one_matched_player(conn, "3003", "Zeta Player")
    # 1900 in +01:00 is unambiguously before any 2026 decision.
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3003",
                             as_of="1900-01-01T00:00:00+01:00") is None
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3003",
                             as_of="2099-01-01T00:00:00+01:00") is not None
    conn.close()


@pytest.mark.parametrize("bad", ["not-a-timestamp", "", "2026-13-45T99:99:99Z",
                                 "2026-08-08T23:55:49.342103"])
def test_malformed_or_naive_cutoff_fails_closed(tmp_path: Path, bad: str) -> None:
    """A cutoff that cannot be ordered must never silently grant access.

    Lexically ``'not-a-timestamp' > '2026-...'``, so string comparison RESOLVED
    the mapping for a malformed cutoff.
    """

    conn = _fresh(tmp_path, f"ts_bad_{abs(hash(bad))}")
    _one_matched_player(conn, "3004", "Eta Player")
    with pytest.raises(ValueError):
        resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3004",
                          as_of=bad)
    conn.close()


def test_pit_cutoffs_remain_documented_at_the_boundary(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "ts_boundary")
    _one_matched_player(conn, "3005", "Theta Player")
    link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3005")
    assert link is not None
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3005",
                             as_of="2020-01-01T00:00:00.000000Z") is None
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3005",
                             as_of=link.decided_at) is not None
    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id="3005",
                             as_of="2099-01-01T00:00:00.000000Z") is not None
    conn.close()


# --------------------------------------------------------------------------- #
# Independent re-verification of the three original repairs
# --------------------------------------------------------------------------- #

def test_official_provider_contract_covers_both_leagues() -> None:
    """The "one official id is one person" rule rests on this mapping."""

    assert OFFICIAL_PROVIDER_BY_LEAGUE == {"lg_mlb": MLB, "lg_nba": NBA}


@pytest.mark.parametrize("league_id,provider,count", [
    ("lg_mlb", MLB, 2), ("lg_mlb", MLB, 3), ("lg_mlb", MLB, 4),
    ("lg_nba", NBA, 2), ("lg_nba", NBA, 3), ("lg_nba", NBA, 4),
])
def test_same_name_official_ids_stay_distinct_in_both_leagues(
        tmp_path: Path, league_id: str, provider: str, count: int) -> None:
    conn = _fresh(tmp_path, f"sn_{league_id}_{count}")
    ids = [str(7000 + i) for i in range(count)]
    for pid in ids:
        seed_player_ref(conn, provider=provider, provider_player_id=pid)
        _ident_player(conn, provider, pid, league_id, "Chris Johnson")
    MatchPlayersService(conn).match_range(provider=provider, season_year=2026)
    rows = conn.execute(
        "SELECT provider_player_id, player_id FROM provider_player_references "
        "ORDER BY provider_player_id").fetchall()
    linked = {str(r["provider_player_id"]): r["player_id"] for r in rows}
    assert all(v is not None for v in linked.values()), linked
    assert len({v for v in linked.values()}) == count, "ids collapsed into one identity"
    conn.close()


@pytest.mark.parametrize("seed", list(range(30)))
def test_same_name_determinism_across_randomized_orders(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    ids = [str(8000 + i) for i in range(4)]
    conn = _fresh(tmp_path, f"det_{seed}")
    order = list(ids)
    rng.shuffle(order)
    for pid in order:
        seed_player_ref(conn, provider=NBA, provider_player_id=pid)
    ident_order = list(ids)
    rng.shuffle(ident_order)
    for pid in ident_order:
        _ident_player(conn, NBA, pid, "lg_nba", "Chris Johnson")
    MatchPlayersService(conn).match_range(provider=NBA, season_year=2026)
    linked = {str(r["provider_player_id"]): r["player_id"] for r in conn.execute(
        "SELECT provider_player_id, player_id FROM provider_player_references")}
    assert sorted(linked) == sorted(ids)
    assert all(v is not None for v in linked.values())
    assert len({v for v in linked.values()}) == 4
    conn.close()


def test_an_unclaimed_candidate_still_keeps_the_id_ambiguous(tmp_path: Path) -> None:
    """The `all`, not `any`, rule: one free candidate means real ambiguity."""

    from .conftest import seed_player

    conn = _fresh(tmp_path, "unclaimed")
    # A pre-existing canonical player nobody owns, matching by name.
    seed_player(conn, league_code="MLB", full_name="Chris Johnson")
    seed_player_ref(conn, provider=MLB, provider_player_id="9100")
    _ident_player(conn, MLB, "9100", "lg_mlb", "Chris Johnson")
    MatchPlayersService(conn).match_range(provider=MLB, season_year=2026)
    linked = conn.execute(
        "SELECT player_id FROM provider_player_references "
        "WHERE provider_player_id='9100'").fetchone()[0]
    created = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    # It resolved onto the existing player rather than bootstrapping a duplicate.
    assert created == 1, "an unclaimed canonical candidate must not be duplicated"
    assert linked is not None
    conn.close()


def test_valid_team_replay_still_writes_nothing(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "valid_replay")
    _seed_mlb_game(conn)
    _match_games(conn)
    baseline = _counts(conn)
    for _ in range(6):
        _match_games(conn)
        assert _counts(conn) == baseline
    conn.close()


def test_resolver_covers_all_three_kinds(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "kinds")
    _seed_mlb_game(conn)
    _match_games(conn)
    team = resolve_canonical(conn, kind="team", provider=MLB, provider_entity_id="141")
    game = resolve_canonical(conn, kind="game", provider=MLB, provider_entity_id="777001")
    assert team is not None and team.kind == "team"
    assert game is not None and game.kind == "game"
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id="141") is None
    conn.close()


def test_resolve_many_keeps_unresolved_entries_explicit(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "many")
    _one_matched_player(conn, "5001", "Iota Player")
    out = resolve_many(conn, kind="player", provider=MLB,
                       provider_entity_ids=["5001", "nope", "5001"])
    assert set(out) == {"5001", "nope"}
    assert out["5001"] is not None
    assert out["nope"] is None
    conn.close()


# --------------------------------------------------------------------------- #
# Architecture -- the light resolver must not leak into PIT / feature paths
# --------------------------------------------------------------------------- #

def test_pit_and_feature_paths_never_import_the_light_resolver() -> None:
    """`resolution.py` is matching/reporting-side only.

    It gates knowledge time but NOT the manual-review gate, so a feature or label
    builder using it would bypass an acceptance gate that
    `AsOfReader.matched_entity` enforces. PIT paths must keep going through the
    reader.
    """

    import sports_quant

    root = Path(sports_quant.__file__).resolve().parent
    offenders: list[str] = []
    for package in ("pit", "features", "modeling", "backtest"):
        directory = root / package
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "matching.resolution" in text or "from ..matching import resolution" in text:
                offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"point-in-time / feature code imported the light resolver: {offenders}")


def test_provider_reference_tables_remain_forbidden_pit_inputs() -> None:
    from sports_quant.pit.registry import ForbiddenJoinError, require_asof

    for table in ("provider_team_references", "provider_player_references",
                  "provider_game_references"):
        with pytest.raises(ForbiddenJoinError):
            require_asof(table)
