"""Adversarial tests for the historical Odds-API event identity architecture.

Every test here is offline evidence for
``NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE_INDEPENDENT_REVIEW.md``.
They are written to FALSIFY the architecture's claims, not to confirm them, so a
passing test named ``..._is_refused`` is a defence that held and one named
``..._is_admitted`` is a defect that must be repaired in code.

Attacks use DIRECT SQL wherever possible: a claim that survives only when the
caller cooperates is not a defence.

No provider is contacted, no credit is spent, and nothing under ``data/`` is
opened.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import utc_now_iso
from sports_quant.retrospective.attestations import (
    AttestationError,
    attestation_map_digest,
)
from sports_quant.retrospective.provenance import EntityType, ProviderNamespace
from sports_quant.retrospective.sources import AUDITED_SOURCE_TABLES, PROVIDER_LEAGUES
from sports_quant.retrospective.team_crosswalks import _require_corpus_provenance

ODDS = "the_odds_api:basketball_nba"
ODDS_GEN = "v4"
EVENT_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EVENT_B = "111a955795876d50988b15c219ce0796"


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "v19.db"
    Database(path).migrate()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    _seed(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    """Two canonical NBA games plus one MLB game, from the real migrations."""

    now = utc_now_iso()
    for league, code, name, sport in (("lg_nba", "NBA", "NBA", "basketball"),
                                      ("lg_mlb", "MLB", "MLB", "baseball")):
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)", (league, code, name, sport, now, now))
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, label, phase, "
            "start_date, end_date, created_at, updated_at) VALUES "
            "(?, ?, 2025, '2025-26', 'regular', '2025-10-01', '2026-06-30', ?, ?)",
            (f"sn_{league}", league, now, now))
    teams = [("tm_bos", "lg_nba", "Boston Celtics", "Boston", "Celtics", "BOS"),
             ("tm_mia", "lg_nba", "Miami Heat", "Miami", "Heat", "MIA"),
             ("tm_nyy", "lg_mlb", "New York Yankees", "New York", "Yankees", "NYY"),
             ("tm_bal", "lg_mlb", "Baltimore Orioles", "Baltimore", "Orioles", "BAL")]
    for tid, league, name, city, nick, abbr in teams:
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, "
            "nickname, abbreviation, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, league, name, city, nick, abbr, now, now))
    games = [("gm_nba_1", "lg_nba", "tm_bos", "tm_mia", "2026-03-06T00:10:00Z"),
             ("gm_nba_2", "lg_nba", "tm_mia", "tm_bos", "2026-03-09T23:40:00Z"),
             ("gm_mlb_1", "lg_mlb", "tm_nyy", "tm_bal", "2026-06-06T23:05:00Z")]
    for gid, league, home, away, start in games:
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
            "away_team_id, scheduled_start, original_start, game_date_local, "
            "status, official_provider, official_game_key, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?, 'final', ?, ?, ?, ?)",
            (gid, league, f"sn_{league}", home, away, start, start, start[:10],
             f"balldontlie:{league[3:]}:v1", gid.replace("gm_", ""), now, now))
    conn.commit()


def _corpus(conn: sqlite3.Connection, *, map_digest: str | None,
            source_digest: str = "SRC_DIGEST_A",
            corpus_id: str = "rcv_test") -> str:
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions (corpus_version_id, "
        "provenance_class, league_id, reconstruction_policy_version, "
        "cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
        "target_set_digest, static_identity_map_digest, g1_variant, code_version, "
        "semantic_digest, created_at) VALUES (?, 'reconstructed_research', "
        "'lg_nba', 'p-v1', 'cut', 'v1', ?, 'TGT', ?, 'g1_b_core', 'rev', ?, ?)",
        (corpus_id, source_digest, map_digest, f"SEM_{corpus_id}", now))
    conn.commit()
    return corpus_id


def _audit(conn: sqlite3.Connection, *, provider: str, generation: str,
           verified: int, verdict: str = "accepted",
           source_digest: str = "SRC_DIGEST_A",
           audit_id: str = "ida_forged", league: str = "lg_nba") -> tuple[str, str]:
    """Insert an identity audit by DIRECT SQL, bypassing every code-level check."""

    digest = f"SEMDIG_{audit_id}"
    conn.execute(
        "INSERT INTO identity_audit_records (identity_audit_id, league_id, "
        "provider, namespace_generation, namespace_verified, entity_type, "
        "source_corpus_digest, audit_policy_version, distinct_ids, "
        "total_observations, collision_count, flagged_count, verdict, "
        "semantic_digest, created_at) VALUES (?,?,?,?,?, 'game', ?, 'pol-v1', "
        "2, 8, 0, 0, ?, ?, ?)",
        (audit_id, league, provider, generation, verified, source_digest,
         verdict, digest, utc_now_iso()))
    conn.commit()
    return audit_id, digest


def _crosswalk(conn: sqlite3.Connection, *, corpus: str, provider: str,
               generation: str, provider_id: str, canonical: str,
               audit_id: str, audit_digest: str, league: str = "lg_nba",
               xid: str = "xwk_1") -> None:
    conn.execute(
        "INSERT INTO static_crosswalk_provenance (crosswalk_id, "
        "corpus_version_id, league_id, provider, namespace_generation, "
        "entity_type, provider_id, canonical_entity_id, identity_audit_id, "
        "identity_audit_digest, provenance_policy_version, semantic_digest, "
        "curated_at, created_at) VALUES (?,?,?,?,?, 'game', ?,?,?,?, 'link-v1', "
        "?,?,?)",
        (xid, corpus, league, provider, generation, provider_id, canonical,
         audit_id, audit_digest, f"SEM_{xid}",
         "2026-08-16T00:00:00.000000Z", utc_now_iso()))
    conn.commit()


def _raw(conn: sqlite3.Connection, raw_id: str) -> None:
    """One preserved exchange, with the ingestion run raw_responses requires."""

    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO ingestion_runs (run_id, command, provider, "
        "operation, args_json, status, requested_at, started_at, "
        "started_monotonic_ns, requests_made, records_received, "
        "records_normalized, records_inserted, records_deduplicated, "
        "records_rejected, records_updated, tool_version, created_at) VALUES "
        "('run_1', 'x', ?, 'op', '{}', 'started', ?, ?, 1, 0,0,0,0,0,0,0, 'v', ?)",
        (ODDS, now, now, now))
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, body, "
        "body_bytes, body_hash, content_hash, requested_at, received_at, "
        "elapsed_ns, created_at) VALUES (?, 'run_1', ?, '/x', '{}', 200, '{}', "
        "'[]', 2, 'h', 'c', ?, ?, 1, ?)", (raw_id, ODDS, now, now, now))
    conn.commit()


# --------------------------------------------------------------------------- #
# D1 -- one corpus field, two identity maps
# --------------------------------------------------------------------------- #
def test_attestation_map_digest_is_team_only_and_takes_no_arguments() -> None:
    """The corpus map digest is definitionally the TEAM map. It has no room."""

    assert inspect.signature(attestation_map_digest).parameters == {}
    assert attestation_map_digest() == attestation_map_digest()


def test_a_composed_two_map_digest_breaks_team_a(db: sqlite3.Connection) -> None:
    """DEFECT D1: composing a second map into the corpus field fails TEAM-A closed."""

    composed = hashlib.sha256(
        (attestation_map_digest() + "ODDS_EVENT_MAP_DIGEST").encode()).hexdigest()
    _corpus(db, map_digest=composed)
    repo = SqliteRetrospectiveProvenanceRepository(db)

    with pytest.raises(AttestationError, match="declares attestation map digest"):
        _require_corpus_provenance(repo, "rcv_test")


def test_team_only_digest_leaves_a_second_map_unbound(db: sqlite3.Connection) -> None:
    """The only value TEAM-A accepts says nothing about any second map."""

    _corpus(db, map_digest=attestation_map_digest())
    repo = SqliteRetrospectiveProvenanceRepository(db)
    _, declared = _require_corpus_provenance(repo, "rcv_test")

    assert declared == attestation_map_digest()


def test_the_corpus_row_cannot_be_amended_later(db: sqlite3.Connection) -> None:
    """Append-only, so a second digest cannot be added after the fact."""

    _corpus(db, map_digest=attestation_map_digest())
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE reconstruction_corpus_versions SET static_identity_map_digest "
            "= 'BOTH' WHERE corpus_version_id = 'rcv_test'")


def test_row_level_map_binding_does_exist_as_a_mitigation() -> None:
    """Repair D1 is possible: the ROW can carry a map digest the CORPUS cannot."""

    params = inspect.signature(
        SqliteRetrospectiveProvenanceRepository.record_static_crosswalk).parameters
    assert "attestation_map_digest" in params
    assert params["attestation_map_digest"].default is None


# --------------------------------------------------------------------------- #
# D2 -- the "hard stop" is code-only
# --------------------------------------------------------------------------- #
def test_an_odds_api_namespace_is_unverified_in_code() -> None:
    """The code-level fail-closed the architecture relies on."""

    ns = ProviderNamespace(league_id="lg_nba", provider=ODDS,
                           generation=ODDS_GEN, entity_type=EntityType.GAME)
    assert ns.verified is False
    assert ODDS not in PROVIDER_LEAGUES


def test_an_unverified_audit_still_cannot_be_accepted(db: sqlite3.Connection) -> None:
    """The honest path stays closed: verified = 0 refuses an accepted verdict."""

    with pytest.raises(sqlite3.IntegrityError):
        _audit(db, provider=ODDS, generation=ODDS_GEN, verified=0)


def test_direct_sql_can_forge_an_accepted_secondary_provider_audit(
    db: sqlite3.Connection,
) -> None:
    """DEFECT D2: the DB does not know which generations are attested.

    ``ida_accepted_is_clean`` checks only ``namespace_verified = 1`` -- a
    caller-asserted integer. ``ATTESTED_GENERATIONS`` lives in Python alone.
    """

    audit_id, _ = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    row = db.execute(
        "SELECT verdict, namespace_verified FROM identity_audit_records "
        "WHERE identity_audit_id = ?", (audit_id,)).fetchone()
    assert row["verdict"] == "accepted"
    assert row["namespace_verified"] == 1


def test_a_forged_audit_admits_a_secondary_provider_game_crosswalk(
    db: sqlite3.Connection,
) -> None:
    """With the forged audit, v19 admits the link. The table is willing."""

    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=EVENT_A, canonical="gm_nba_1",
               audit_id=audit_id, audit_digest=digest)

    row = db.execute(
        "SELECT canonical_entity_id FROM static_crosswalk_provenance").fetchone()
    assert row["canonical_entity_id"] == "gm_nba_1"


# --------------------------------------------------------------------------- #
# Secondary-provider authority: the defences that held
# --------------------------------------------------------------------------- #
def test_the_crosswalk_cannot_touch_canonical_game_state(
    db: sqlite3.Connection,
) -> None:
    """Writing a link changes no column of `games` -- authority is unreachable."""

    before = dict(db.execute(
        "SELECT * FROM games WHERE game_id = 'gm_nba_1'").fetchone())
    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=EVENT_A, canonical="gm_nba_1",
               audit_id=audit_id, audit_digest=digest)
    after = dict(db.execute(
        "SELECT * FROM games WHERE game_id = 'gm_nba_1'").fetchone())

    assert before == after
    assert after["official_provider"] == "balldontlie:nba:v1"


def test_one_event_id_cannot_bind_two_canonical_games(
    db: sqlite3.Connection,
) -> None:
    """Structurally impossible, as the architecture requires."""

    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=EVENT_A, canonical="gm_nba_1",
               audit_id=audit_id, audit_digest=digest, xid="xwk_1")

    with pytest.raises(sqlite3.IntegrityError):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=EVENT_A, canonical="gm_nba_2",
                   audit_id=audit_id, audit_digest=digest, xid="xwk_2")


def test_two_event_ids_may_bind_one_canonical_game(db: sqlite3.Connection) -> None:
    """DEFECT D4: permitted and correct, but the schema surfaces nothing."""

    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    for xid, event in (("xwk_1", EVENT_A), ("xwk_2", EVENT_B)):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=event, canonical="gm_nba_1",
                   audit_id=audit_id, audit_digest=digest, xid=xid)

    n = db.execute(
        "SELECT COUNT(*) c FROM static_crosswalk_provenance "
        "WHERE canonical_entity_id = 'gm_nba_1'").fetchone()["c"]
    assert n == 2   # nothing in the schema flags this; counters are the only control


def test_a_link_to_a_nonexistent_game_is_refused(db: sqlite3.Connection) -> None:
    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    with pytest.raises(sqlite3.IntegrityError, match="must bind an existing game"):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=EVENT_A, canonical="gm_does_not_exist",
                   audit_id=audit_id, audit_digest=digest)


def test_a_cross_league_link_is_refused(db: sqlite3.Connection) -> None:
    """An NBA-league crosswalk cannot bind an MLB game."""

    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    with pytest.raises(sqlite3.IntegrityError, match="same league"):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=EVENT_A, canonical="gm_mlb_1",
                   audit_id=audit_id, audit_digest=digest)


def test_an_audit_over_a_different_source_corpus_is_refused(
    db: sqlite3.Connection,
) -> None:
    """G5 point 8 enforced in SQL: a narrower audit never transfers."""

    corpus = _corpus(db, map_digest=attestation_map_digest(),
                     source_digest="SRC_DIGEST_A")
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1,
                              source_digest="SRC_DIGEST_B")
    with pytest.raises(sqlite3.IntegrityError, match="different source corpus"):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=EVENT_A, canonical="gm_nba_1",
                   audit_id=audit_id, audit_digest=digest)


def test_a_crosswalk_citing_a_mismatched_namespace_is_refused(
    db: sqlite3.Connection,
) -> None:
    """The audit must be for THIS provider/generation, not merely accepted."""

    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider="balldontlie", generation="v1",
                              verified=1, audit_id="ida_bdl")
    with pytest.raises(sqlite3.IntegrityError, match="ACCEPTED identity audit"):
        _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
                   provider_id=EVENT_A, canonical="gm_nba_1",
                   audit_id=audit_id, audit_digest=digest)


def test_crosswalk_rows_are_append_only(db: sqlite3.Connection) -> None:
    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=EVENT_A, canonical="gm_nba_1",
               audit_id=audit_id, audit_digest=digest)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE static_crosswalk_provenance SET canonical_entity_id "
                   "= 'gm_nba_2'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM static_crosswalk_provenance")


# --------------------------------------------------------------------------- #
# D3 -- provider_id has no format contract
# --------------------------------------------------------------------------- #
#: Built with ``chr()`` rather than pasted literals so the hostile codepoints
#: survive every editor and transport verbatim.
_LOOKALIKES = [
    EVENT_A.upper(),                    # case-flipped
    EVENT_A[:-1] + chr(0x0435),         # Cyrillic small letter IE
    chr(0x20) + EVENT_A,                # leading space
    EVENT_A + chr(0x200b),              # zero-width space
    EVENT_A[:-1] + chr(0xff45),         # fullwidth latin small e
]


@pytest.mark.parametrize("variant", _LOOKALIKES)
def test_a_lookalike_event_id_is_admitted_as_a_distinct_key(
    db: sqlite3.Connection, variant: str,
) -> None:
    """DEFECT D3: v19 puts no format contract on ``provider_id``.

    Only ``TRIM(provider_id) <> ''`` is checked, so a case-flipped, space-padded
    or confusable id is a DIFFERENT key: it neither collides with nor is
    rejected against the real one. It silently coexists, pointing at another
    game, and one of the two is wrong.
    """

    assert variant != EVENT_A
    corpus = _corpus(db, map_digest=attestation_map_digest())
    audit_id, digest = _audit(db, provider=ODDS, generation=ODDS_GEN, verified=1)
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=EVENT_A, canonical="gm_nba_1",
               audit_id=audit_id, audit_digest=digest, xid="xwk_real")
    _crosswalk(db, corpus=corpus, provider=ODDS, generation=ODDS_GEN,
               provider_id=variant, canonical="gm_nba_2",
               audit_id=audit_id, audit_digest=digest, xid="xwk_fake")

    assert db.execute(
        "SELECT COUNT(*) c FROM static_crosswalk_provenance").fetchone()["c"] == 2


# --------------------------------------------------------------------------- #
# source_corpus_digest blast radius
# --------------------------------------------------------------------------- #
def test_registering_a_new_audited_table_changes_every_corpus_digest() -> None:
    """Adding a table to the audited set is NOT digest-neutral.

    ``source_corpus_digest`` folds one entry per audited table, so registering a
    fourth changes the digest of corpora containing no market data at all --
    which invalidates the audit/crosswalk binding of every existing corpus.
    """

    def digest(tables: tuple[str, ...]) -> str:
        h = hashlib.sha256()
        for table in sorted(tables):
            h.update(table.encode())
            h.update(b"|")     # stand-in for that table's row hash
        return h.hexdigest()

    assert len(AUDITED_SOURCE_TABLES) == 3
    assert digest(AUDITED_SOURCE_TABLES) != digest(
        AUDITED_SOURCE_TABLES + ("historical_market_event_observations",))


def test_the_audited_source_tables_are_official_provider_evidence_only() -> None:
    """There is no table in the audited set that can hold a market event."""

    assert set(AUDITED_SOURCE_TABLES) == {
        "game_schedule_snapshots",
        "provider_player_identity_snapshots",
        "provider_team_identity_snapshots",
    }


# --------------------------------------------------------------------------- #
# Schema options A / B / C, falsified against the real tables
# --------------------------------------------------------------------------- #
def test_game_schedule_snapshots_cannot_hold_an_odds_event(
    db: sqlite3.Connection,
) -> None:
    """OPTION C falsified: the table demands an official game reference.

    ``game_ref_id`` is NOT NULL referencing ``provider_game_references``, so an
    Odds event cannot go here without first minting an official game reference
    for a sportsbook -- the exact authority a secondary provider must not have.
    """

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, "
            "provider, provider_game_id, mapped_status, observed_at, "
            "ingested_at, raw_response_id, raw_response_hash, content_hash, "
            "created_at) VALUES ('gss_x', NULL, ?, ?, 'scheduled', ?, ?, "
            "'raw_x', 'h', 'c', ?)",
            (ODDS, EVENT_A, utc_now_iso(), utc_now_iso(), utc_now_iso()))


def test_sportsbook_events_has_no_snapshot_instant_column(
    db: sqlite3.Connection,
) -> None:
    """OPTION B falsified: nowhere to record WHEN the provider's snapshot was."""

    cols = {r["name"] for r in db.execute("PRAGMA table_info(sportsbook_events)")}
    assert "provider_event_id" in cols
    # Every time column is OUR clock, not the provider's snapshot instant.
    assert {"first_observed_at", "last_observed_at", "updated_at"} <= cols
    assert not {c for c in cols if "snapshot" in c}


def test_sportsbook_events_is_mutable(db: sqlite3.Connection) -> None:
    """OPTION B falsified again: the row can be overwritten in place."""

    now = utc_now_iso()
    _raw(db, "raw_1")
    db.execute(
        "INSERT INTO sportsbook_events (sb_event_id, provider, provider_event_id, "
        "league_id, sport_key, commence_time, home_team_raw, away_team_raw, "
        "raw_response_id, first_observed_at, last_observed_at, created_at, "
        "updated_at) VALUES ('sbe_1', ?, ?, 'lg_nba', 'basketball_nba', "
        "'2026-03-06T00:10:00Z', 'Boston Celtics', 'Miami Heat', 'raw_1', "
        "?, ?, ?, ?)", (ODDS, EVENT_A, now, now, now, now))
    db.commit()

    # No trigger stops this. The "observation" is not an observation.
    db.execute("UPDATE sportsbook_events SET commence_time = "
               "'2026-03-07T00:10:00Z', home_team_raw = 'Anything'")
    db.commit()
    row = db.execute(
        "SELECT commence_time, home_team_raw FROM sportsbook_events").fetchone()
    assert row["commence_time"] == "2026-03-07T00:10:00Z"
    assert row["home_team_raw"] == "Anything"


def test_raw_responses_is_append_only_but_untyped(db: sqlite3.Connection) -> None:
    """OPTION A: preservation is genuine; the problem is that it is a blob."""

    _raw(db, "raw_2")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE raw_responses SET body = '[1]' "
                   "WHERE raw_response_id = 'raw_2'")

    cols = {r["name"] for r in db.execute("PRAGMA table_info(raw_responses)")}
    # Nothing typed: no event id, no snapshot instant, no commence time.
    assert not {c for c in cols if "event" in c or "commence" in c}
