"""Adversarial ARCHITECTURE proofs behind the Stage-A lane-binding review.

These are not tests of a v22 implementation -- no such implementation exists, and
none is authorized. They pin the *current v21 facts* that the independent review
relied on, so that a future v22 migration cannot quietly invalidate the reasoning
without a test failing.

Each test names the review finding it supports.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import utc_now_iso
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass

NOW = utc_now_iso()


def _league(conn: sqlite3.Connection) -> None:
    """Seed the NBA league unless the shared fixture already provides it."""

    existing = conn.execute(
        "SELECT 1 FROM leagues WHERE league_id = 'lg_nba'").fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)",
            (NOW, NOW))


def _common() -> dict[str, object]:
    return {
        "provenance_class": ProvenanceClass.RECONSTRUCTED_RESEARCH,
        "league_id": "lg_nba",
        "reconstruction_policy_version": "p1",
        "cutoff_policy_id": "cut",
        "cutoff_policy_version": "v1",
        "source_corpus_digest": "OFFICIAL_DIGEST_X",
        "target_set_digest": "TARGETS_X",
        "g1_variant": G1Variant.G1_B_CORE,
    }


def test_d1a_corpus_collapses_when_market_evidence_is_not_committed(conn):
    """D1a: two reconstructions differing only in market evidence collide.

    This is why market evidence may NOT live only in a child lane row: the
    corpus is content-addressed, so leaving ``market_evidence_digest`` NULL
    makes distinct reconstructions share one ``corpus_version_id``.
    """

    _league(conn)
    repo = SqliteRetrospectiveProvenanceRepository(conn)

    first = repo.record_corpus_version(**_common())
    second = repo.record_corpus_version(**_common())

    assert first.corpus_version_id == second.corpus_version_id


def test_d1b_corpus_identity_does_commit_to_market_evidence(conn):
    """D1b: supplying the digest yields a DISTINCT corpus.

    Confirms ``market_evidence_digest`` is a real input to ``semantic_digest``,
    so the column is used -- not superseded.
    """

    _league(conn)
    repo = SqliteRetrospectiveProvenanceRepository(conn)

    without = repo.record_corpus_version(**_common())
    with_market = repo.record_corpus_version(
        **_common(), market_evidence_digest="MARKET_E0")

    assert with_market.corpus_version_id != without.corpus_version_id
    assert without.market_evidence_digest is None


def test_d2_audit_digest_is_not_recomputed_so_a_lane_may_be_bypassed(conn):
    """D2/D3: nothing recomputes an audit's digest, and no lane is required.

    A linking-provider audit that cites no lane and declares the OFFICIAL
    corpus digest is accepted, and its crosswalk inserts. A v22 nullable lane
    FK must therefore be fail-closed for non-official providers.
    """

    _league(conn)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(**_common())

    for team_id, name in (("tm_a", "Alpha"), ("tm_b", "Beta")):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city,"
            " nickname, abbreviation, created_at, updated_at)"
            " VALUES (?, 'lg_nba', ?, ?, ?, ?, ?, ?)",
            (team_id, name, name, name, name[:3].upper(), NOW, NOW))
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase,"
        " start_date, end_date, created_at, updated_at) VALUES"
        " ('sn_nba','lg_nba',2025,'2025-26','regular','2025-10-01',"
        " '2026-06-30',?,?)", (NOW, NOW))
    conn.execute(
        "INSERT INTO games (game_id, league_id, season_id, home_team_id,"
        " away_team_id, scheduled_start, original_start, game_date_local,"
        " status, created_at, updated_at) VALUES ('gm_1','lg_nba','sn_nba',"
        " 'tm_a','tm_b','2026-03-01T18:10:00Z','2026-03-01T18:10:00Z',"
        " '2026-03-01','final',?,?)", (NOW, NOW))

    # A LINKING-provider audit declaring the OFFICIAL corpus digest. No market
    # evidence exists at all; no trigger recomputes the value.
    conn.execute(
        "INSERT INTO identity_audit_records (identity_audit_id, league_id,"
        " provider, namespace_generation, namespace_verified, entity_type,"
        " source_corpus_digest, audit_policy_version, distinct_ids,"
        " total_observations, collision_count, flagged_count, verdict,"
        " semantic_digest, created_at) VALUES ('ida_forge','lg_nba',"
        " 'the_odds_api','v4', 1, 'game', 'OFFICIAL_DIGEST_X','pol',1,1,0,0,"
        " 'accepted','SEM_FORGE', ?)", (NOW,))

    conn.execute(
        "INSERT INTO static_crosswalk_provenance (crosswalk_id,"
        " corpus_version_id, league_id, provider, namespace_generation,"
        " entity_type, provider_id, canonical_entity_id, identity_audit_id,"
        " identity_audit_digest, provenance_policy_version, semantic_digest,"
        " curated_at, created_at) VALUES ('xwk_forge', ?, 'lg_nba',"
        " 'the_odds_api','v4','game','be25eb82b82629d959c1e5ccb8dcc1e7',"
        " 'gm_1','ida_forge','SEM_FORGE','pol','SEMX_FORGE',"
        " '2026-08-18T00:00:00.000000Z', ?)", (corpus.corpus_version_id, NOW))

    stored = conn.execute(
        "SELECT COUNT(*) FROM static_crosswalk_provenance"
        " WHERE crosswalk_id = 'xwk_forge'").fetchone()[0]
    assert stored == 1


def test_d10_received_at_is_only_shape_checked(conn):
    """D10: ``received_at`` has no ordering or calendar constraint.

    ``observed_at = received_at`` inherits exactly this trust level, so the
    architecture may claim tamper-EVIDENCE only, never unforgeability.
    """

    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation,"
        " args_json, status, requested_at, started_at, started_monotonic_ns,"
        " requests_made, records_received, records_normalized, records_inserted,"
        " records_deduplicated, records_rejected, records_updated, tool_version,"
        " created_at) VALUES ('run_1','x','the_odds_api','op','{}','started',"
        "?,?,1,0,0,0,0,0,0,0,'v',?)", (NOW, NOW, NOW))

    # received_at EARLIER than requested_at is accepted by the schema.
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body,"
        " body_bytes, body_hash, content_hash, requested_at, received_at,"
        " elapsed_ns, created_at) VALUES ('raw_backwards','run_1',"
        " 'the_odds_api','/e','{}',200,'{}','[]',2,'h','c',"
        " '2026-08-01T00:00:00.000000Z','2026-03-01T00:00:00.000000Z',1,?)",
        (NOW,))

    row = conn.execute(
        "SELECT requested_at, received_at FROM raw_responses"
        " WHERE raw_response_id = 'raw_backwards'").fetchone()
    assert row["received_at"] < row["requested_at"]


def test_d9_no_probe_classification_exists_on_raw_responses(conn):
    """D9: ``raw_responses`` cannot mark a response as a capability probe.

    Probe reuse therefore needs a dedicated registration object; without one
    the exception is a general plan-before-network bypass.
    """

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(raw_responses)")}
    assert not any("probe" in name for name in columns)


def test_d7_unknown_provider_cost_policy_fails_open_to_mlb():
    """D7: ``_policy_for`` silently costs an unknown provider as MLB.

    A Stage-A plan built through the existing planner would emit
    ``cost_policy_version = "mlb-cost-v1"``, which PASSES the manifest's
    supported-version check while mis-costing every Odds request. A Stage-A
    planner must add an explicit Odds policy and fail closed here.
    """

    from sports_quant.ingest.planning import _policy_for

    assert _policy_for("the_odds_api").version == "mlb-cost-v1"
    assert _policy_for("totally_unknown_provider").version == "mlb-cost-v1"


@pytest.mark.parametrize(
    "attempts, planned, balances",
    [
        ([("b1", "success"), ("b2", "empty")], 2, True),
        # One retry: three attempt rows against two planned buckets.
        ([("b1", "failure"), ("b1", "success"), ("b2", "empty")], 2, False),
    ],
)
def test_d4_bucket_equation_breaks_under_retries(attempts, planned, balances):
    """D4: the reviewed equation balances buckets against attempt outcomes.

    It cannot hold once any bucket is retried, which the proposed
    ``UNIQUE(acquisition, bucket, attempt_ordinal)`` explicitly permits.
    """

    assert (len(attempts) == planned) is balances
