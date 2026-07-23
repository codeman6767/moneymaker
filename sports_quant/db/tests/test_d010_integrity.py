"""Migration d010: provider-audit integrity guards enforced by the database.

Covers the three d010 fixes at the schema level (independent of application
code): evidence columns + the observed-flag CHECK on provider_capabilities, the
partial unique index that keeps a provider venue id mapped to one canonical
venue, and the data_quality_issues immutability/no-delete triggers. Applying the
migration twice is a no-op (verified by the shared migration idempotency tests);
here we assert the guards actually fire.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.capabilities import SqliteCapabilityRepository
from sports_quant.db.repositories.data_quality import SqliteDataQualityRepository
from sports_quant.db.repositories.venues import SqliteVenueRepository

_TS = "2026-07-22T18:00:00.000000Z"


def _seed_venue(conn: sqlite3.Connection, name: str) -> str:
    """Seed a canonical venue (creating its provenance run/raw once)."""

    conn.execute(
        "INSERT OR IGNORE INTO ingestion_runs (run_id, command, provider, operation, args_json, "
        "status, requested_at, started_at, started_monotonic_ns, tool_version, created_at) VALUES "
        "('run_d010', 'ingest-venues', 'mlb_statsapi', 'fetch_venues', '{}', 'started', ?, ?, 0, "
        "'t', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT OR IGNORE INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_d010', 'run_d010', 'mlb_statsapi', '/venues', '{}', 200, '{}', ?, ?, 1, '{}', 2, "
        "'bh', 'ch', ?)",
        (_TS, _TS, _TS),
    )
    repo = SqliteVenueRepository(conn)
    venue, _ = repo.upsert(
        name=name, raw_response_id="raw_d010", raw_response_hash="ch", observed_at=_TS
    )
    return venue.venue_id


# --------------------------------------------------------------------------- #
# 1. Evidence columns + observed-flag CHECK
# --------------------------------------------------------------------------- #
def test_provider_capabilities_has_evidence_columns(conn: sqlite3.Connection) -> None:
    cols = {c[1] for c in conn.execute("PRAGMA table_info(provider_capabilities)").fetchall()}
    for expected in (
        "declared_state",
        "observed_state",
        "is_observed",
        "probe_name",
        "endpoint",
        "http_status",
        "error_kind",
        "verified_at",
    ):
        assert expected in cols, f"{expected} missing from provider_capabilities"


def test_is_observed_flag_is_constrained_to_0_or_1(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO provider_capabilities "
            "(capability_id, provider, tier, capability, state, observed_at, content_hash, "
            " created_at, is_observed) "
            "VALUES ('cap_bad', 'p', NULL, 'teams', 'supported', ?, 'ch', ?, 2)",
            (_TS, _TS),
        )


def test_declared_only_row_stores_no_observation(conn: sqlite3.Connection) -> None:
    """A declared-only capability row keeps is_observed=0 and no evidence."""

    caps = SqliteCapabilityRepository(conn)
    rec, inserted = caps.record(
        provider="balldontlie",
        tier="goat",
        capability="plays",
        state="supported",
        observed_at=_TS,
        declared_state="supported",
        is_observed=False,
    )
    assert inserted and rec is not None
    assert rec.is_observed is False
    assert rec.observed_state is None
    assert rec.raw_response_id is None


def test_observed_row_requires_evidence(conn: sqlite3.Connection) -> None:
    """is_observed=True without a raw_response_id / observed_state is rejected."""

    caps = SqliteCapabilityRepository(conn)
    with pytest.raises(RepositoryError, match="raw_response_id"):
        caps.record(
            provider="balldontlie",
            tier="goat",
            capability="teams",
            state="supported",
            observed_at=_TS,
            declared_state="supported",
            observed_state="supported",
            is_observed=True,  # missing raw_response_id -> error
        )


def test_capability_history_is_append_only(conn: sqlite3.Connection) -> None:
    caps = SqliteCapabilityRepository(conn)
    caps.record(
        provider="balldontlie",
        tier="goat",
        capability="teams",
        state="supported",
        observed_at=_TS,
        declared_state="supported",
        is_observed=False,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE provider_capabilities SET state = 'unsupported'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM provider_capabilities")


# --------------------------------------------------------------------------- #
# 2. Provider venue identity: partial unique index
# --------------------------------------------------------------------------- #
def test_same_provider_venue_id_cannot_map_to_two_venues(conn: sqlite3.Connection) -> None:
    v1 = _seed_venue(conn, "Alpha Park")
    v2 = _seed_venue(conn, "Beta Park")
    conn.execute(
        "INSERT INTO venue_aliases (alias_id, venue_id, provider, provider_venue_id, alias, "
        "normalized, source, created_at) VALUES "
        "('val_1', ?, 'mlb_statsapi', '500', 'Alpha Park', 'alpha park', 'x', ?)",
        (v1, _TS),
    )
    # Same (provider, provider_venue_id) pointing at a different venue is blocked.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO venue_aliases (alias_id, venue_id, provider, provider_venue_id, alias, "
            "normalized, source, created_at) VALUES "
            "('val_2', ?, 'mlb_statsapi', '500', 'Beta Park', 'beta park', 'x', ?)",
            (v2, _TS),
        )


def test_partial_index_exempts_null_provider_venue_id(conn: sqlite3.Connection) -> None:
    """Same-named venues in different cities (no provider id) are allowed.

    The unique index is partial (provider_venue_id NOT NULL AND provider<>''),
    so many alias rows without a provider id never collide.
    """

    v1 = _seed_venue(conn, "Downtown Field Alpha")
    v2 = _seed_venue(conn, "Downtown Field Beta")
    for aid, vid, alias in (("val_a", v1, "Downtown Field"), ("val_b", v2, "Downtown Field")):
        conn.execute(
            "INSERT INTO venue_aliases (alias_id, venue_id, provider, provider_venue_id, alias, "
            "normalized, source, created_at) VALUES (?, ?, '', NULL, ?, 'downtown field', 'x', ?)",
            (aid, vid, alias, _TS),
        )
    count = conn.execute(
        "SELECT COUNT(*) FROM venue_aliases WHERE normalized = 'downtown field'"
    ).fetchone()[0]
    assert count == 2


# --------------------------------------------------------------------------- #
# 3. data_quality_issues immutability
# --------------------------------------------------------------------------- #
def test_data_quality_core_fields_are_immutable(conn: sqlite3.Connection) -> None:
    dq = SqliteDataQualityRepository(conn)
    issue = dq.record(
        severity="note",
        rule_code="DQ-CAP-001",
        entity_type="provider",
        description="original description",
        provider="balldontlie",
    )
    # Rewriting an evidence/identity field is refused by the trigger.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE data_quality_issues SET description = 'tampered' WHERE issue_id = ?",
            (issue.issue_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE data_quality_issues SET rule_code = 'DQ-OTHER' WHERE issue_id = ?",
            (issue.issue_id,),
        )


def test_data_quality_resolution_fields_are_updatable(conn: sqlite3.Connection) -> None:
    dq = SqliteDataQualityRepository(conn)
    issue = dq.record(
        severity="note",
        rule_code="DQ-CAP-001",
        entity_type="provider",
        description="d",
        provider="balldontlie",
    )
    resolved = dq.resolve(issue.issue_id, note="handled")
    assert resolved.resolved_at is not None
    assert resolved.resolution_note == "handled"


def test_data_quality_issues_cannot_be_deleted(conn: sqlite3.Connection) -> None:
    dq = SqliteDataQualityRepository(conn)
    issue = dq.record(
        severity="note",
        rule_code="DQ-CAP-001",
        entity_type="provider",
        description="d",
    )
    with pytest.raises(sqlite3.IntegrityError, match="not deletable"):
        conn.execute("DELETE FROM data_quality_issues WHERE issue_id = ?", (issue.issue_id,))
