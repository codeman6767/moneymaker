"""Adversarial ARCHITECTURE proofs behind the Stage-A lane-binding review.

These pin the facts the v22 architecture review relied on, so the reasoning
cannot be invalidated silently.

Where f022 has since REPAIRED a defect the review reproduced, the test now
asserts the repair and records what the v21 behaviour was. That is the point of
having written them: a migration changing one of these behaviours cannot pass
unnoticed.

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
    """D2/D3, now REPAIRED by f022.

    At v21 a linking-provider audit that cited no lane and declared the
    OFFICIAL corpus digest was ACCEPTED, and its crosswalk inserted cleanly.
    That is the bypass the review reproduced.

    f022 makes the lane mandatory for any non-official provider, so the same
    sequence is refused at the database. Digest forgery is still not
    detectable at INSERT -- a trigger cannot compute SHA-256 over evidence
    rows -- and it is the deterministic gate that closes that half.
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

    assert corpus.corpus_version_id  # the parent the forged audit would cite

    # The v21 forge: a LINKING-provider audit declaring the OFFICIAL corpus
    # digest while citing no lane. Refused since f022.
    with pytest.raises(sqlite3.IntegrityError,
                       match="must cite an evidence lane binding"):
        conn.execute(
            "INSERT INTO identity_audit_records (identity_audit_id, league_id,"
            " provider, namespace_generation, namespace_verified, entity_type,"
            " source_corpus_digest, audit_policy_version, distinct_ids,"
            " total_observations, collision_count, flagged_count, verdict,"
            " semantic_digest, created_at) VALUES ('ida_forge','lg_nba',"
            " 'the_odds_api','v4', 1, 'game', 'OFFICIAL_DIGEST_X','pol',"
            "1,1,0,0,'accepted','SEM_FORGE', ?)", (NOW,))

    assert conn.execute(
        "SELECT COUNT(*) FROM identity_audit_records").fetchone()[0] == 0


def test_d10_received_at_is_only_shape_checked(conn):
    """D10, now PARTLY REPAIRED by f022.

    At v21 ``received_at`` was constrained by SHAPE only, so a response could
    be recorded as received five months BEFORE it was requested. v22 makes
    ``observed_at`` equal to it, so that clock became load-bearing and f022
    adds calendar validity plus an ordering check as a forward trigger.

    What is NOT repaired, and is the reviewed trust boundary: a caller writing
    the raw response still chooses both clocks. Ordering makes an INCOHERENT
    pair detectable; it cannot make an internally-consistent lie detectable.
    """

    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation,"
        " args_json, status, requested_at, started_at, started_monotonic_ns,"
        " requests_made, records_received, records_normalized, records_inserted,"
        " records_deduplicated, records_rejected, records_updated, tool_version,"
        " created_at) VALUES ('run_1','x','the_odds_api','op','{}','started',"
        "?,?,1,0,0,0,0,0,0,0,'v',?)", (NOW, NOW, NOW))

    # Accepted at v21; refused since f022.
    with pytest.raises(sqlite3.IntegrityError, match="cannot arrive before"):
        conn.execute(
            "INSERT INTO raw_responses (raw_response_id, run_id, provider,"
            " endpoint, request_params_json, http_status, response_headers_json,"
            " body, body_bytes, body_hash, content_hash, requested_at,"
            " received_at, elapsed_ns, created_at) VALUES ('raw_backwards',"
            " 'run_1','the_odds_api','/e','{}',200,'{}','[]',2,'h','c',"
            " '2026-08-01T00:00:00.000000Z','2026-03-01T00:00:00.000000Z',1,?)",
            (NOW,))

    # An internally-consistent pair remains entirely caller-chosen. This is the
    # tamper-evidence boundary, and it is why the architecture may not claim
    # that observed_at is unforgeable.
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body,"
        " body_bytes, body_hash, content_hash, requested_at, received_at,"
        " elapsed_ns, created_at) VALUES ('raw_invented','run_1',"
        " 'the_odds_api','/e','{}',200,'{}','[]',2,'h','c',"
        " '2001-01-01T00:00:00.000000Z','2001-01-01T00:00:01.000000Z',1,?)",
        (NOW,))
    assert conn.execute(
        "SELECT received_at FROM raw_responses WHERE raw_response_id ="
        " 'raw_invented'").fetchone()[0] == "2001-01-01T00:00:01.000000Z"


def test_d9_no_probe_classification_exists_on_raw_responses(conn):
    """D9: ``raw_responses`` cannot mark a response as a capability probe.

    Probe reuse therefore needs a dedicated registration object; without one
    the exception is a general plan-before-network bypass.
    """

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(raw_responses)")}
    assert not any("probe" in name for name in columns)


def test_d7_unknown_provider_cost_policy_now_fails_closed():
    """D7, now REPAIRED by v22.

    At `de7a48a` ``_policy_for`` was a binary fallback, so EVERY unknown provider
    silently inherited MLB's model: ``_policy_for("the_odds_api")`` returned
    ``mlb-cost-v1``. That is worse than an outright refusal, because
    ``mlb-cost-v1`` is a real registered version -- a Stage-A manifest carrying it
    would have PASSED the supported-version check while budgeting a
    credit-metered provider as if its requests were free.

    v22 gives the Odds API an explicit policy and refuses everything unregistered.
    """

    from sports_quant.ingest.planning import UnknownProviderError, _policy_for

    odds = _policy_for("the_odds_api")
    assert odds.version == "odds-cost-v1"
    assert odds.credit_applicable is True

    with pytest.raises(UnknownProviderError):
        _policy_for("totally_unknown_provider")


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
