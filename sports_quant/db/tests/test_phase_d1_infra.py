"""Phase D1 database infrastructure: d009 schema, triggers, repositories.

Uses the shared ``conn`` fixture (a migrated, seeded temporary database). Every
assertion is offline; no provider is contacted.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories.capabilities import SqliteCapabilityRepository
from sports_quant.db.repositories.data_quality import SqliteDataQualityRepository
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.kalshi import UpsertOutcome
from sports_quant.db.repositories.matching import CandidateInput, SqliteMatchingRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.db.repositories.venues import SqliteVenueRepository
from sports_quant.db.schema import PHASE_D1_TABLES

T0 = "2026-07-23T18:00:00.000000Z"
T1 = "2026-07-23T19:00:00.000000Z"
T2 = "2026-07-23T20:00:00.000000Z"

_ACCOUNT_TOKENS = ("balance", "portfolio", "fill", "payment", "position")


def _raw(conn: sqlite3.Connection, marker: str = "a") -> tuple[str, str]:
    run = SqliteIngestionRunRepository(conn).start(
        command="provider-audit", provider="mlb_statsapi", operation="audit", args_json="{}",
        started_monotonic_ns=0, tool_version="test",
    )
    body = '{"m":"%s"}' % marker
    ch = response_content_hash(provider="mlb_statsapi", endpoint="/venues", request_params={}, body=body)
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="mlb_statsapi", endpoint="/venues", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=T0, received_at=T0, elapsed_ns=1,
        body=body, content_hash=ch,
    )
    return raw.raw_response_id, ch


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_all_d1_tables_present_and_no_account_columns(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in PHASE_D1_TABLES:
        assert t in names
    offenders = []
    for t in PHASE_D1_TABLES:
        for row in conn.execute(f"PRAGMA table_info({t})").fetchall():
            col = row[1].lower()
            if any(tok in col for tok in _ACCOUNT_TOKENS):
                offenders.append(f"{t}.{col}")
    assert not offenders, f"account-scoped columns present: {offenders}"


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    from sports_quant.db.engine import foreign_keys_enabled

    assert foreign_keys_enabled(conn) is True
    # A venue with a dangling first_raw_response_id is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO venues (venue_id, name, normalized_name, first_raw_response_id, "
            "current_raw_response_id, current_raw_response_hash, first_observed_at, "
            "last_observed_at, created_at, updated_at) VALUES "
            "('ven_x','X','x','raw_missing','raw_missing','h',?,?,?,?)",
            (T0, T0, T0, T0),
        )


def test_invalid_enum_values_rejected(conn: sqlite3.Connection) -> None:
    raw_id, ch = _raw(conn)
    # Bad roof type.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO venues (venue_id, name, normalized_name, roof_type, first_raw_response_id, "
            "current_raw_response_id, current_raw_response_hash, first_observed_at, "
            "last_observed_at, created_at, updated_at) VALUES "
            "('ven_b','X','xb','glass',?,?,?,?,?,?,?)",
            (raw_id, raw_id, ch, T0, T0, T0, T0),
        )
    # Bad capability state.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO provider_capabilities (capability_id, provider, capability, state, "
            "observed_at, content_hash, created_at) VALUES "
            "('cap_b','balldontlie','plays','sorta',?,?,?)",
            (T0, "h", T0),
        )
    # Bad data-quality severity.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO data_quality_issues (issue_id, severity, rule_code, entity_type, "
            "description, detected_at, created_at) VALUES ('dqi_b','critical','R','x','d',?,?)",
            (T0, T0),
        )


def test_provider_capabilities_append_only(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO provider_capabilities (capability_id, provider, tier, capability, state, "
        "observed_at, content_hash, created_at) VALUES "
        "('cap_1','balldontlie','goat','plays','supported',?,'h1',?)",
        (T0, T0),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE provider_capabilities SET state='unavailable' WHERE capability_id='cap_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM provider_capabilities WHERE capability_id='cap_1'")


def test_match_candidates_append_only(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, source_ref, "
        "matched_entity_id, outcome, method, score, threshold, matcher_version, decided_at, "
        "created_at) VALUES ('mtc_ap','game','p','r','gm_x','accepted','official_key',1.0,0.85,"
        "'v1',?,?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO match_candidates (candidate_id, match_id, score, tier, rank, created_at) "
        "VALUES ('mcn_1','mtc_ap',1.0,'official_key',0,?)",
        (T0,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE match_candidates SET score=0.1 WHERE candidate_id='mcn_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM match_candidates WHERE candidate_id='mcn_1'")


def test_match_decision_review_only_update(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, source_ref, "
        "outcome, method, score, threshold, rejection_reason, matcher_version, decided_at, "
        "created_at) VALUES ('mtc_r','game','p','r','ambiguous','schedule_key',0.9,0.85,"
        "'two candidates','v1',?,?)",
        (T0, T0),
    )
    # Review-column update allowed.
    conn.execute(
        "UPDATE entity_match_decisions SET needs_manual_review=1, reviewed_by='me', reviewed_at=? "
        "WHERE match_id='mtc_r'",
        (T1,),
    )
    # Any other column update blocked.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE entity_match_decisions SET score=0.5 WHERE match_id='mtc_r'")


def test_reference_identity_immutable_once_linked(conn: sqlite3.Connection) -> None:
    raw_id, ch = _raw(conn)
    conn.execute(
        "INSERT INTO provider_team_references (reference_id, provider, provider_team_id, team_id, "
        "first_raw_response_id, current_raw_response_id, current_raw_response_hash, "
        "first_observed_at, last_observed_at, created_at, updated_at) VALUES "
        "('ptr_1','mlb_statsapi','111','tm_mlb_nyy',?,?,?,?,?,?,?)",
        (raw_id, raw_id, ch, T0, T0, T0, T0),
    )
    # A linked team_id cannot be silently re-pointed.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE provider_team_references SET team_id='tm_mlb_bos' WHERE reference_id='ptr_1'")


# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #
def test_provider_reference_upsert_outcomes(conn: sqlite3.Connection) -> None:
    repo = SqliteProviderReferenceRepository(conn)
    raw_id, ch = _raw(conn, "r1")
    ref, o1 = repo.upsert(kind="game", provider="mlb_statsapi", provider_entity_id="746001",
                          raw_response_id=raw_id, raw_response_hash=ch, observed_at=T0)
    assert o1 is UpsertOutcome.INSERTED
    assert ref.canonical_id is None  # never linked at ingest
    # Newer observation -> current provenance advances.
    raw2, ch2 = _raw(conn, "r2")
    ref2, o2 = repo.upsert(kind="game", provider="mlb_statsapi", provider_entity_id="746001",
                           raw_response_id=raw2, raw_response_hash=ch2, observed_at=T2)
    assert o2 is UpsertOutcome.UPDATED
    assert ref2.current_raw_response_id == raw2
    assert ref2.first_raw_response_id == raw_id
    # Older-or-equal -> unchanged.
    _r3, o3 = repo.upsert(kind="game", provider="mlb_statsapi", provider_entity_id="746001",
                          raw_response_id=raw_id, raw_response_hash=ch, observed_at=T0)
    assert o3 is UpsertOutcome.UNCHANGED
    assert repo.count("game") == 1


def test_venue_upsert_and_alias(conn: sqlite3.Connection) -> None:
    repo = SqliteVenueRepository(conn)
    raw_id, ch = _raw(conn)
    venue, outcome = repo.upsert(
        name="Fenway Park", raw_response_id=raw_id, raw_response_hash=ch, observed_at=T0,
        city="Boston", country="USA", latitude=42.34, longitude=-71.097,
        timezone="America/New_York", roof_type="open",
    )
    assert outcome is UpsertOutcome.INSERTED
    assert venue.is_outdoor is True  # derived from roof_type
    repo.add_alias(venue_id=venue.venue_id, alias="Fenway", provider="mlb_statsapi", provider_venue_id="15")
    assert repo.resolve_alias("Fenway", provider="mlb_statsapi") == venue.venue_id
    # Idempotent alias.
    repo.add_alias(venue_id=venue.venue_id, alias="Fenway", provider="mlb_statsapi", provider_venue_id="15")
    assert repo.count_aliases() == 1


def test_venue_invalid_coords_rejected(conn: sqlite3.Connection) -> None:
    from sports_quant.db.repositories.base import RepositoryError

    repo = SqliteVenueRepository(conn)
    raw_id, ch = _raw(conn)
    with pytest.raises(RepositoryError):
        repo.upsert(name="Bad", raw_response_id=raw_id, raw_response_hash=ch, observed_at=T0,
                    latitude=200.0)


def test_matching_records_decision_and_candidates(conn: sqlite3.Connection) -> None:
    repo = SqliteMatchingRepository(conn)
    decision = repo.record_decision(
        entity_type="game", source_provider="the_odds_api", source_ref="evt-1",
        outcome="accepted", method="schedule_key_exact", score=0.95, threshold=0.85,
        matcher_version="v1", matched_entity_id="gm_x",
        candidates=[
            CandidateInput(score=0.95, tier="schedule_key_exact", candidate_entity_id="gm_x"),
            CandidateInput(score=0.60, tier="schedule_key_window", candidate_entity_id="gm_y"),
        ],
    )
    assert decision.outcome == "accepted"
    cands = repo.candidates(decision.match_id)
    assert [c.rank for c in cands] == [0, 1]
    assert cands[0].candidate_entity_id == "gm_x"


def test_matching_rejects_accepted_without_entity(conn: sqlite3.Connection) -> None:
    from sports_quant.db.repositories.base import RepositoryError

    repo = SqliteMatchingRepository(conn)
    with pytest.raises(RepositoryError):
        repo.record_decision(
            entity_type="game", source_provider="p", source_ref="r", outcome="accepted",
            method="m", score=0.9, threshold=0.85, matcher_version="v1", candidates=[],
        )


def test_capability_repository_append_only_and_asof(conn: sqlite3.Connection) -> None:
    repo = SqliteCapabilityRepository(conn)
    _s, ins = repo.record(provider="balldontlie", tier="goat", capability="injuries",
                          state="supported", observed_at=T0)
    assert ins is True
    # Idempotent re-record at same observed_at + content.
    _s2, ins2 = repo.record(provider="balldontlie", tier="goat", capability="injuries",
                            state="supported", observed_at=T0)
    assert ins2 is False
    # A later, different state appends.
    repo.record(provider="balldontlie", tier="goat", capability="injuries",
                state="paid_tier_required", observed_at=T2)
    assert repo.count() == 2
    # as-of returns the state that held then.
    at_t1 = repo.state_as_of("balldontlie", "injuries", T1)
    assert at_t1 is not None and at_t1.state == "supported"
    at_t2 = repo.state_as_of("balldontlie", "injuries", T2)
    assert at_t2 is not None and at_t2.state == "paid_tier_required"


def test_data_quality_record_and_resolve(conn: sqlite3.Connection) -> None:
    repo = SqliteDataQualityRepository(conn)
    issue = repo.record(severity="note", rule_code="DQ-CAP-001", entity_type="provider",
                        description="capability unavailable", provider="balldontlie")
    assert issue.resolved_at is None
    assert len(repo.list_open()) == 1
    repo.resolve(issue.issue_id, note="fixed")
    assert len(repo.list_open()) == 0
