"""The three canonical-matching defects that blocked F1 matching acceptance.

Each defect gets its reproducer first, written to require the corrected contract
rather than to describe the old behaviour:

1. **Official same-name determinism.** Two stable ids from a league's designated
   official provider are two people by construction. Whichever order they are
   processed in, both must end up with their own canonical identity. The old
   behaviour was "first one wins the name": whoever ran first bootstrapped and
   the other was refused as ambiguous, so the final state depended on traversal
   order.
2. **Accepted team replay.** Re-running `match-games` over an already-matched
   game re-resolved both teams by `exact_provider_id` and appended two *accepted*
   re-affirmation decisions every time, growing decision history without new
   information.
3. **Canonical resolution downstream.** An accepted canonical mapping must be
   reachable from normalized observations through the provider reference and its
   backing accepted decision -- and must stay invisible at a cutoff before that
   decision was known.

Offline only; nothing here opens a socket.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.identity import SqliteProviderIdentityRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.resolution import (
    CanonicalResolutionError,
    resolve_canonical,
    resolve_many,
)
from sports_quant.matching.service import MatchGamesService
from sports_quant.pit.dataset import build_historical_dataset

from .conftest import T0, raw_response, seed_player_ref, seed_schedule, seed_team

MLB = "mlb_statsapi"
NBA = "balldontlie"


def _identity_player(conn: sqlite3.Connection, *, provider: str, provider_player_id: str,
                     league_id: str, full_name: str, observed_at: str = T0,
                     **kw: Optional[str]) -> None:
    rid, rhash = raw_response(conn, marker=f"pi:{provider_player_id}:{full_name}:{observed_at}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_player(
            provider=provider, provider_player_id=provider_player_id, league_id=league_id,
            full_name=full_name, observed_at=observed_at, raw_response_id=rid,
            raw_response_hash=rhash, **kw)


def _identity_team(conn: sqlite3.Connection, *, provider: str, provider_team_id: str,
                   league_id: str, full_name: str, observed_at: str = T0) -> None:
    rid, rhash = raw_response(conn, marker=f"ti:{provider_team_id}:{full_name}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_team(
            provider=provider, provider_team_id=provider_team_id, league_id=league_id,
            full_name=full_name, observed_at=observed_at, raw_response_id=rid,
            raw_response_hash=rhash)


def _team_ref(conn: sqlite3.Connection, *, provider: str, provider_team_id: str) -> None:
    rid, rhash = raw_response(conn, marker=f"teamref:{provider}:{provider_team_id}")
    with transaction(conn):
        SqliteProviderReferenceRepository(conn).upsert(
            kind="team", provider=provider, provider_entity_id=provider_team_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at=T0)


def _match_players(conn: sqlite3.Connection, provider: str = MLB, **kw: object):
    return MatchPlayersService(conn).match_range(provider=provider, **kw)  # type: ignore[arg-type]


def _match_games(conn: sqlite3.Connection, provider: str = MLB, **kw: object):
    return MatchGamesService(conn).match_range(provider=provider, **kw)  # type: ignore[arg-type]


def _fresh(tmp_path: Path, tag: str) -> sqlite3.Connection:
    path = tmp_path / f"{tag}.db"
    initialize_database(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------- #
# Defect 1 -- official same-name identities must be order-independent
# --------------------------------------------------------------------------- #

def _seed_same_name_pair(conn: sqlite3.Connection, ids: list[str],
                         name: str = "Will Smith") -> None:
    """Two unlinked official references sharing one provider-written name."""

    for pid in ids:
        seed_player_ref(conn, provider=MLB, provider_player_id=pid)
        _identity_player(conn, provider=MLB, provider_player_id=pid, league_id="lg_mlb",
                         full_name=name)


def _semantic_player_state(conn: sqlite3.Connection) -> dict:
    """Order-independent view: which ids linked, and did they share a player."""

    rows = conn.execute(
        "SELECT provider_player_id, player_id FROM provider_player_references "
        "ORDER BY provider_player_id").fetchall()
    linked = {str(r["provider_player_id"]): r["player_id"] for r in rows}
    distinct = {v for v in linked.values() if v is not None}
    return {
        "ids": sorted(linked),
        "linked_ids": sorted(k for k, v in linked.items() if v is not None),
        "distinct_players": len(distinct),
        "shared": len([v for v in linked.values() if v is not None]) != len(distinct),
        "players": conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
    }


def test_two_official_ids_with_one_name_both_get_their_own_identity(
        tmp_path: Path) -> None:
    """The reproducer for the order-dependence defect.

    Both ids come from the league's designated official provider, so each stable
    id IS an identity. Refusing the second because the first already took the
    name makes the outcome depend on who ran first.
    """

    conn = _fresh(tmp_path, "same_name")
    _seed_same_name_pair(conn, ["1001", "1002"])
    _match_players(conn, MLB, season_year=2026)
    state = _semantic_player_state(conn)
    assert state["linked_ids"] == ["1001", "1002"], (
        "both official ids must resolve; the second was refused because the first "
        "already owned the name")
    assert state["distinct_players"] == 2
    assert state["shared"] is False, "two official ids collapsed into one identity"
    conn.close()


def test_same_name_result_is_identical_under_reversed_processing(
        tmp_path: Path) -> None:
    conn_a = _fresh(tmp_path, "order_a")
    _seed_same_name_pair(conn_a, ["1001", "1002"])
    _match_players(conn_a, MLB, season_year=2026)
    a = _semantic_player_state(conn_a)
    conn_a.close()

    conn_b = _fresh(tmp_path, "order_b")
    _seed_same_name_pair(conn_b, ["1002", "1001"])   # opposite insertion order
    _match_players(conn_b, MLB, season_year=2026)
    b = _semantic_player_state(conn_b)
    conn_b.close()

    assert a == b, f"insertion order changed the semantic outcome: {a} vs {b}"


@pytest.mark.parametrize("seed", list(range(25)))
def test_same_name_is_order_independent_under_randomized_permutations(
        tmp_path: Path, seed: int) -> None:
    """Reference, identity and processing order are all permuted."""

    rng = random.Random(seed)
    ids = ["1001", "1002", "1003", "1004"]
    order = list(ids)
    rng.shuffle(order)
    conn = _fresh(tmp_path, f"perm_{seed}")
    for pid in order:
        seed_player_ref(conn, provider=MLB, provider_player_id=pid)
    identity_order = list(ids)
    rng.shuffle(identity_order)
    for pid in identity_order:
        _identity_player(conn, provider=MLB, provider_player_id=pid, league_id="lg_mlb",
                         full_name="Will Smith")
    _match_players(conn, MLB, season_year=2026)
    state = _semantic_player_state(conn)
    conn.close()
    assert state["linked_ids"] == ids
    assert state["distinct_players"] == 4
    assert state["shared"] is False


def test_nonofficial_provider_same_name_stays_conservative(tmp_path: Path) -> None:
    """Name-only evidence from a nonofficial provider still creates nothing."""

    conn = _fresh(tmp_path, "nonofficial")
    for pid in ("9001", "9002"):
        seed_player_ref(conn, provider="odds_api", provider_player_id=pid)
        _identity_player(conn, provider="odds_api", provider_player_id=pid,
                         league_id="lg_mlb", full_name="Will Smith")
    _match_players(conn, "odds_api", season_year=2026)
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    linked = conn.execute(
        "SELECT COUNT(*) FROM provider_player_references WHERE player_id IS NOT NULL"
    ).fetchone()[0]
    assert linked == 0, "a nonofficial provider must never bootstrap a canonical player"
    conn.close()


def test_official_same_name_replay_is_idempotent(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "same_name_replay")
    _seed_same_name_pair(conn, ["1001", "1002"])
    _match_players(conn, MLB, season_year=2026)
    first = _semantic_player_state(conn)
    decisions = conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0]
    _match_players(conn, MLB, season_year=2026)
    assert _semantic_player_state(conn) == first
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == first["players"]
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == decisions
    conn.close()


def test_official_same_name_dry_run_persists_nothing(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "same_name_dry")
    _seed_same_name_pair(conn, ["1001", "1002"])
    MatchPlayersService(conn, dry_run=True).match_range(  # type: ignore[call-arg]
        provider=MLB, season_year=2026)
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM player_aliases").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_player_references WHERE player_id IS NOT NULL"
    ).fetchone()[0] == 0
    conn.close()


# --------------------------------------------------------------------------- #
# Defect 2 -- accepted team replays must not grow decision history
# --------------------------------------------------------------------------- #

def _seed_matchable_game(conn: sqlite3.Connection) -> None:
    seed_team(conn, league_code="MLB", abbreviation="TOR",
              canonical_name="Toronto Blue Jays", city="Toronto", nickname="Blue Jays")
    seed_team(conn, league_code="MLB", abbreviation="TB",
              canonical_name="Tampa Bay Rays", city="Tampa Bay", nickname="Rays")
    for tid, name in (("141", "Toronto Blue Jays"), ("139", "Tampa Bay Rays")):
        _team_ref(conn, provider=MLB, provider_team_id=tid)
        _identity_team(conn, provider=MLB, provider_team_id=tid, league_id="lg_mlb",
                       full_name=name)
    seed_schedule(conn, provider=MLB, provider_game_id="777001",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00.000000Z", season=2026,
                  game_date_local="2026-07-24")


def _accepted_counts(conn: sqlite3.Connection) -> dict:
    return {
        "decisions": conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0],
        "accepted": conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions WHERE outcome='accepted'"
        ).fetchone()[0],
        "team_accepted": conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions WHERE outcome='accepted' "
            "AND entity_type='team'").fetchone()[0],
        "candidates": conn.execute("SELECT COUNT(*) FROM match_candidates").fetchone()[0],
        "teams": conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
        "games": conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
        "team_links": conn.execute(
            "SELECT COUNT(*) FROM provider_team_references WHERE team_id IS NOT NULL"
        ).fetchone()[0],
    }


def test_accepted_team_replay_appends_no_new_decision(tmp_path: Path) -> None:
    """The reproducer for decision-history growth on replay."""

    conn = _fresh(tmp_path, "team_replay")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    after_first = _accepted_counts(conn)
    assert after_first["team_links"] == 2

    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    after_second = _accepted_counts(conn)
    assert after_second == after_first, (
        f"replay grew the audit log: {after_first} -> {after_second}")

    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    assert _accepted_counts(conn) == after_first
    conn.close()


def test_accepted_team_replay_is_reported_as_replay_not_a_new_match(
        tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "team_replay_counters")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    second = _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    assert second.counters.decisions_replayed >= 2, (
        "an already-linked semantic replay must be counted as a replay")
    conn.close()


def test_accepted_team_replay_preserves_history(tmp_path: Path) -> None:
    """Nothing historical may be deleted or rewritten by the replay rule."""

    conn = _fresh(tmp_path, "team_history")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    before = [tuple(r) for r in conn.execute(
        "SELECT match_id, entity_type, source_ref, outcome, matched_entity_id "
        "FROM entity_match_decisions ORDER BY match_id")]
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    after = [tuple(r) for r in conn.execute(
        "SELECT match_id, entity_type, source_ref, outcome, matched_entity_id "
        "FROM entity_match_decisions ORDER BY match_id")]
    assert after == before
    conn.close()


def test_link_without_a_valid_backing_decision_fails_closed(tmp_path: Path) -> None:
    """A corrupt link must be reported, never quietly treated as a replay.

    ``team_id`` is immutable once set, so the reachable corruption is the backing
    decision: a link whose ``match_decision_id`` no longer identifies an accepted
    team decision for that team has lost its provenance.
    """

    conn = _fresh(tmp_path, "team_conflict")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    conn.execute(
        "UPDATE provider_team_references SET match_decision_id = NULL "
        "WHERE provider_team_id = '141'")
    conn.commit()
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    issues = conn.execute(
        "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code = 'DQ-MATCH-017'"
    ).fetchone()[0]
    assert issues > 0, "a link with no valid backing decision must be reported"
    conn.close()


def test_broken_link_is_not_silently_repaired(tmp_path: Path) -> None:
    """Reporting the break must not itself rewrite the link."""

    conn = _fresh(tmp_path, "team_no_repair")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    conn.execute(
        "UPDATE provider_team_references SET match_decision_id = NULL "
        "WHERE provider_team_id = '141'")
    conn.commit()
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    still_null = conn.execute(
        "SELECT match_decision_id FROM provider_team_references "
        "WHERE provider_team_id = '141'").fetchone()[0]
    assert still_null is None, "the broken link was silently repaired without a decision"
    conn.close()


def test_team_replay_is_deterministic_under_repeated_runs(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "team_determinism")
    _seed_matchable_game(conn)
    _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
    baseline = _accepted_counts(conn)
    for _ in range(5):
        _match_games(conn, MLB, from_date="2026-07-24", to_date="2026-07-24")
        assert _accepted_counts(conn) == baseline
    conn.close()


def test_match_games_dry_run_persists_nothing(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "games_dry")
    _seed_matchable_game(conn)
    MatchGamesService(conn, dry_run=True).match_range(  # type: ignore[call-arg]
        provider=MLB, from_date="2026-07-24", to_date="2026-07-24")
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM match_candidates").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_team_references WHERE team_id IS NOT NULL"
    ).fetchone()[0] == 0
    conn.close()


# --------------------------------------------------------------------------- #
# Defect 3 -- accepted canonical mappings must be resolvable downstream
# --------------------------------------------------------------------------- #

def _accepted_player_link(conn: sqlite3.Connection) -> tuple[str, str]:
    """Match one official player; return (provider_player_id, canonical player)."""

    seed_player_ref(conn, provider=MLB, provider_player_id="660271")
    _identity_player(conn, provider=MLB, provider_player_id="660271",
                     league_id="lg_mlb", full_name="Shohei Ohtani")
    _match_players(conn, MLB, season_year=2026)
    row = conn.execute(
        "SELECT provider_player_id, player_id FROM provider_player_references "
        "WHERE provider_player_id = '660271'").fetchone()
    return str(row["provider_player_id"]), str(row["player_id"])


def test_observation_is_unresolved_before_any_accepted_mapping(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_before")
    seed_player_ref(conn, provider=MLB, provider_player_id="660271")
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id="660271") is None
    conn.close()


def test_accepted_mapping_resolves_consistently(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_after")
    pid, canonical = _accepted_player_link(conn)
    link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id=pid)
    assert link is not None
    assert link.canonical_id == canonical
    decision = conn.execute(
        "SELECT outcome, entity_type, matched_entity_id FROM entity_match_decisions "
        "WHERE match_id = ?", (link.match_decision_id,)).fetchone()
    assert decision["outcome"] == "accepted"
    assert decision["entity_type"] == "player"
    assert decision["matched_entity_id"] == canonical
    conn.close()


def test_unknown_reference_and_bad_kind(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_unknown")
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id="nope") is None
    with pytest.raises(CanonicalResolutionError):
        resolve_canonical(conn, kind="stadium", provider=MLB, provider_entity_id="1")
    conn.close()


def test_link_without_backing_decision_stays_unresolved(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_nodecision")
    pid, _canonical = _accepted_player_link(conn)
    conn.execute("UPDATE provider_player_references SET match_decision_id = NULL "
                 "WHERE provider_player_id = ?", (pid,))
    conn.commit()
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id=pid) is None
    conn.close()


def test_decision_pointing_at_another_entity_stays_unresolved(tmp_path: Path) -> None:
    """A link must be justified by ITS OWN decision, not any accepted decision."""

    conn = _fresh(tmp_path, "res_wrongdecision")
    pid, _canonical = _accepted_player_link(conn)
    # A second official player, with its own accepted decision for a DIFFERENT
    # canonical player. Repointing the first link at it must not resolve.
    seed_player_ref(conn, provider=MLB, provider_player_id="592450")
    _identity_player(conn, provider=MLB, provider_player_id="592450",
                     league_id="lg_mlb", full_name="Aaron Judge")
    _match_players(conn, MLB, season_year=2026)
    other_decision = conn.execute(
        "SELECT match_decision_id FROM provider_player_references "
        "WHERE provider_player_id = '592450'").fetchone()[0]
    conn.execute("UPDATE provider_player_references SET match_decision_id = ? "
                 "WHERE provider_player_id = ?", (other_decision, pid))
    conn.commit()
    assert resolve_canonical(conn, kind="player", provider=MLB,
                             provider_entity_id=pid) is None, (
        "a decision matching a different canonical player must not justify this link")
    conn.close()


def test_ambiguous_reference_never_resolves(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_ambiguous")
    for pid in ("9001", "9002"):
        seed_player_ref(conn, provider="odds_api", provider_player_id=pid)
        _identity_player(conn, provider="odds_api", provider_player_id=pid,
                         league_id="lg_mlb", full_name="Will Smith")
    _match_players(conn, "odds_api", season_year=2026)
    for pid in ("9001", "9002"):
        assert resolve_canonical(conn, kind="player", provider="odds_api",
                                 provider_entity_id=pid) is None
    conn.close()


def test_matching_today_is_invisible_at_an_earlier_cutoff(tmp_path: Path) -> None:
    """The knowledge-time gate: a later decision cannot resolve an earlier query."""

    conn = _fresh(tmp_path, "res_pit")
    pid, canonical = _accepted_player_link(conn)
    link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id=pid)
    assert link is not None
    decided = link.decided_at

    assert resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id=pid,
                             as_of="2020-01-01T00:00:00.000000Z") is None
    at_cutoff = resolve_canonical(conn, kind="player", provider=MLB,
                                  provider_entity_id=pid, as_of=decided)
    assert at_cutoff is not None and at_cutoff.canonical_id == canonical, (
        "cutoff == decided_at must include the decision")
    later = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id=pid,
                              as_of="2099-01-01T00:00:00.000000Z")
    assert later is not None and later.canonical_id == canonical
    conn.close()


def test_resolution_replay_does_not_multiply_state(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_replay")
    pid, canonical = _accepted_player_link(conn)
    before = (
        conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0],
    )
    for _ in range(5):
        link = resolve_canonical(conn, kind="player", provider=MLB, provider_entity_id=pid)
        assert link is not None and link.canonical_id == canonical
    after = (
        conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0],
    )
    assert after == before, "resolution must be read-only"
    conn.close()


def test_resolve_many_is_order_independent(tmp_path: Path) -> None:
    conn = _fresh(tmp_path, "res_batch")
    pid, canonical = _accepted_player_link(conn)
    a = resolve_many(conn, kind="player", provider=MLB,
                     provider_entity_ids=[pid, "unmatched"])
    b = resolve_many(conn, kind="player", provider=MLB,
                     provider_entity_ids=["unmatched", pid])
    assert a == b
    resolved = a[pid]
    assert resolved is not None
    assert resolved.canonical_id == canonical
    assert a["unmatched"] is None, "an unmatched id must stay explicitly unresolved"
    conn.close()


def test_provider_only_ids_do_not_admit_a_game_to_the_dataset(tmp_path: Path) -> None:
    """Provider identifiers alone never become dataset-admissible labels."""

    conn = _fresh(tmp_path, "res_dataset")
    _seed_matchable_game(conn)
    ds = build_historical_dataset(conn, league="nba")
    assert len(getattr(ds, "rows", []) or []) == 0
    conn.close()


def test_observation_tables_stay_append_only(tmp_path: Path) -> None:
    """The repair must not have weakened any observation guard to backfill."""

    conn = _fresh(tmp_path, "res_guards")
    guarded = {r[0] for r in conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='trigger' "
        "AND (name LIKE '%_no_update' OR name LIKE '%_no_delete')")}
    for table in ("lineup_players", "lineup_snapshots", "nba_player_statistics",
                  "team_game_statistics", "player_game_statistics",
                  "nba_team_statistics", "roster_snapshots", "play_snapshots"):
        assert table in guarded, (
            f"{table} lost its append-only guard; canonical identity must be "
            "RESOLVED through the reference, never backfilled into an observation")
    conn.close()
