"""Adversarial tests for the v22 Stage-A provenance objects and certification gate.

Everything here is synthetic. No provider request is made, no real Stage-A plan
is declared, no linking provider is registered, and the real capability probe is
never consumed into production provenance.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import THE_ODDS_API_PROVIDER, utc_now_iso
from sports_quant.ingest.cost_policies import ODDS_COST_POLICY_VERSION
from sports_quant.ingest.planning import UnknownProviderError, _policy_for
from sports_quant.retrospective import sources
from sports_quant.retrospective.market_observations import (
    MarketEventObservation,
    observation_content_hash,
    observation_id,
)
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass
from sports_quant.retrospective.stage_a_manifest import (
    StageAManifest,
    StageAManifestError,
    StageATarget,
    dumps,
    loads,
    manifest_content_digest,
)
from sports_quant.retrospective.stage_a_policies import (
    MARKET_EVENTS_E0_DIGEST_POLICY_V1,
    OFFICIAL_SOURCE_DIGEST_POLICY_V1,
    PolicyRegistryError,
    require_projection_policy,
    resolve_digest_policy,
)
from sports_quant.retrospective.stage_a_provenance import (
    StageAProvenanceError,
    _record_plan_unverified,
    acquisition_set_digest,
    certify_stage_a,
    enrich_corpus_with_market_lane,
    record_attempt,
    record_committed_plan,
    register_acquisition,
    register_probe_response,
    verify_lane_binding,
)

NOW = utc_now_iso()
BUCKET_A = "2026-03-01T17:00:00.000000Z"
BUCKET_B = "2026-03-01T17:05:00.000000Z"
EVENT_ID = "be25eb82b82629d959c1e5ccb8dcc1e7"
ACQ_POLICY = "stage-a-acquisition-v1"
PROJ_POLICY = "hme-projection-v1"
PROBE_POLICY = "stage-a-probe-v1"
ENDPOINT = "/v4/historical/sports/basketball_nba/events"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# B2: plans are now declared FROM a committed artefact, so these suites commit
# their synthetic manifests into one scratch repository and declare from it. The
# repo is module-scoped and append-only, exactly like real plan history.
# --------------------------------------------------------------------------- #
_B2_REPO: Path | None = None
_B2_SEQ = itertools.count()


def _b2_repo() -> Path:
    global _B2_REPO
    if _B2_REPO is None:
        repo = Path(tempfile.mkdtemp()) / "plans"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo,
                       check=True)
        _B2_REPO = repo
    return _B2_REPO


def _commit_manifest(manifest) -> tuple[str, str]:
    """Commit a manifest and return (commit_sha, path)."""

    repo = _b2_repo()
    name = f"plan_{next(_B2_SEQ)}.json"
    (repo / name).write_text(dumps(manifest), encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha, name


def _declare_committed(conn, manifest) -> str:
    sha, name = _commit_manifest(manifest)
    return record_committed_plan(conn, manifest_commit_sha=sha,
                                 manifest_path=name, repo_root=_b2_repo())


def _certify(conn, acquisition_id):
    return certify_stage_a(conn, acquisition_id=acquisition_id,
                           repo_root=_b2_repo())


def _seed(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM leagues WHERE league_id='lg_nba'").fetchone() is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (NOW, NOW))
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date,"
        " end_date, created_at, updated_at) VALUES ('sn_v22','lg_nba',2025,"
        " '2025-26','regular','2025-10-01','2026-06-30',?,?)", (NOW, NOW))
    for tid, name in (("tm_v1", "AlphaV22"), ("tm_v2", "BetaV22")):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname,"
            " abbreviation, created_at, updated_at) VALUES (?, 'lg_nba', ?,?,?,?,?,?)",
            (tid, name, name, name, name[:3].upper(), NOW, NOW))
    # Distinct local dates: games carry a natural-key uniqueness constraint over
    # (league, date, home, away, game_number).
    for gid, day in (("gm_v1", "2026-03-01"), ("gm_v2", "2026-03-02")):
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id,"
            " away_team_id, scheduled_start, original_start, game_date_local, status,"
            " created_at, updated_at) VALUES (?, 'lg_nba','sn_v22','tm_v1','tm_v2',"
            " ?, ?, ?, 'final', ?, ?)",
            (gid, f"{day}T18:10:00Z", f"{day}T18:10:00Z", day, NOW, NOW))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json,"
        " status, requested_at, started_at, started_monotonic_ns, requests_made,"
        " records_received, records_normalized, records_inserted,"
        " records_deduplicated, records_rejected, records_updated, tool_version,"
        " created_at) VALUES ('run_v22','x',?,'op','{}','started',?,?,1,0,0,0,0,0,0,0,"
        " 'v',?)", (THE_ODDS_API_PROVIDER, NOW, NOW, NOW))


def _wrapper(events: list[dict[str, Any]]) -> str:
    """A valid historical-events snapshot body.

    Real bodies matter now: the certification gate composes the accepted
    projection verifier, so a placeholder payload would fail for the wrong
    reason (or, worse, let a test claim a guard it never exercised).
    """

    return json.dumps({
        "timestamp": "2026-03-01T16:55:37Z",
        "previous_timestamp": "2026-03-01T16:50:37Z",
        "next_timestamp": "2026-03-01T17:00:37Z",
        "data": events})


def _event(event_id: str = EVENT_ID) -> dict[str, Any]:
    return {"id": event_id, "sport_key": "basketball_nba",
            "commence_time": "2026-03-01T18:10:00Z",
            "home_team": "Boston Celtics", "away_team": "Miami Heat"}


def _raw_response(
    conn: sqlite3.Connection, raw_id: str, *,
    requested_at: str | None = None, received_at: str | None = None,
    endpoint: str = ENDPOINT, status: int = 200,
    body: str | None = None,
    params: str | None = None,
) -> None:
    if params is None:
        params = json.dumps({"apiKey": "x", "date": BUCKET_A, "dateFormat": "iso"})
    if body is None:
        body = _wrapper([_event()])
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body, body_bytes,"
        " body_hash, content_hash, requested_at, received_at, elapsed_ns, created_at)"
        " VALUES (?, 'run_v22', ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, 1, ?)",
        (raw_id, THE_ODDS_API_PROVIDER, endpoint, params, status, body,
         len(body), raw_id, raw_id,
         # Call-time clocks: an acquisition registers at utc_now_iso(), so a
         # module-level constant would look like a pre-registration response.
         requested_at or utc_now_iso(), received_at or utc_now_iso(),
         utc_now_iso()))


def _manifest(**overrides: Any) -> StageAManifest:
    base: dict[str, Any] = dict(
        league_id="lg_nba",
        provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4",
        sport_key="basketball_nba",
        official_source_corpus_digest="OFFICIAL_SRC",
        official_target_set_digest="OFFICIAL_TGT",
        targets=(StageATarget("gm_v1", BUCKET_A), StageATarget("gm_v2", BUCKET_A)),
        buckets=(BUCKET_A,),
        decision_horizon_minutes=60,
        bucket_floor_seconds=300,
        request_budget=10,
        credit_budget=10,
        acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY,
        cost_policy_version=ODDS_COST_POLICY_VERSION,
    )
    base.update(overrides)
    return StageAManifest(**base)


def _lane_args(acquisition_id: str, manifest: StageAManifest) -> dict[str, Any]:
    """Manifest inputs the corpus gate now requires for every lane member."""

    return {"manifests": {acquisition_id: manifest},
            "manifest_texts": {acquisition_id: dumps(manifest)}}


def _declared(conn: sqlite3.Connection) -> tuple[str, str, StageAManifest]:
    _seed(conn)
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    return plan_id, acquisition_id, manifest


# --------------------------------------------------------------------------- #
# Cost policy (§4)
# --------------------------------------------------------------------------- #
def test_odds_provider_resolves_to_its_own_explicit_cost_policy():
    policy = _policy_for(THE_ODDS_API_PROVIDER)
    assert policy.version == ODDS_COST_POLICY_VERSION
    assert policy.credit_applicable is True
    assert policy.cost_for("historical_events") == 1


def test_existing_provider_cost_policies_are_unchanged():
    assert _policy_for("mlb_statsapi").version == "mlb-cost-v1"
    assert _policy_for("balldontlie").version == "bdl-cost-v1"


@pytest.mark.parametrize(
    "provider",
    ["", " ", "the_odds_apo", "THE_ODDS_API", "The_Odds_Api", "the_odds_api ",
     " the_odds_api", "unknown", "mlb", "balldontlie2"],
)
def test_unknown_provider_cost_policy_fails_closed(provider):
    """The pre-v22 fallback silently costed every unknown provider as MLB."""

    with pytest.raises(UnknownProviderError):
        _policy_for(provider)


def test_non_events_odds_endpoint_is_not_classified_as_events():
    policy = _policy_for(THE_ODDS_API_PROVIDER)
    assert policy.classify("/v4/historical/sports/basketball_nba/odds") == "unknown"
    assert policy.is_known_family("unknown") is False


# --------------------------------------------------------------------------- #
# Manifest (§3, §35)
# --------------------------------------------------------------------------- #
def test_plan_digest_is_independent_of_local_paths():
    """The same logical plan under two checkouts has ONE identity.

    `f1a-manifest-v1` hashes `scratch_db` and `checkpoint_path`; this format has
    no path field at all, so this property holds by construction.
    """

    assert "scratch" not in dumps(_manifest())
    assert _manifest().plan_digest() == _manifest().plan_digest()


@pytest.mark.parametrize(
    "overrides",
    [
        {"targets": (StageATarget("gm_v1", BUCKET_A),)},
        {"buckets": (BUCKET_A, BUCKET_B),
         "targets": (StageATarget("gm_v1", BUCKET_A), StageATarget("gm_v2", BUCKET_B))},
        {"request_budget": 11},
        {"credit_budget": 11},
        {"provider": "balldontlie"},
        {"official_source_corpus_digest": "OTHER"},
        {"official_target_set_digest": "OTHER"},
        {"decision_horizon_minutes": 45},
    ],
)
def test_semantic_changes_change_the_plan_digest(overrides):
    assert _manifest(**overrides).plan_digest() != _manifest().plan_digest()


def test_target_remapped_to_another_bucket_changes_the_digest():
    """The pigeonhole attack: the bucket SET can stay identical while a target moves."""

    two_buckets = {"buckets": (BUCKET_A, BUCKET_B)}
    first = _manifest(
        **two_buckets,
        targets=(StageATarget("gm_v1", BUCKET_A), StageATarget("gm_v2", BUCKET_B)))
    second = _manifest(
        **two_buckets,
        targets=(StageATarget("gm_v1", BUCKET_B), StageATarget("gm_v2", BUCKET_A)))
    assert sorted(first.buckets) == sorted(second.buckets)
    assert first.plan_digest() != second.plan_digest()


def test_manifest_round_trips_and_verifies_its_own_digest():
    manifest = _manifest()
    assert loads(dumps(manifest)).plan_digest() == manifest.plan_digest()


def test_manifest_with_tampered_digest_is_refused():
    text = dumps(_manifest()).replace(_manifest().plan_digest(), "0" * 64)
    with pytest.raises(StageAManifestError, match="does not match its own content"):
        loads(text)


def test_duplicate_json_key_fails_closed():
    text = dumps(_manifest())
    tampered = "{" + '"provider":"evil",' + text[1:]
    with pytest.raises(StageAManifestError, match="duplicate JSON key"):
        loads(tampered)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"targets": (StageATarget("gm_v1", BUCKET_B),)}, "does not declare"),
        ({"targets": (StageATarget("gm_v1", BUCKET_A),
                      StageATarget("gm_v1", BUCKET_A))}, "more than once"),
        ({"buckets": (BUCKET_A, BUCKET_B)}, "serve no target"),
        ({"request_budget": 0}, "cannot cover"),
    ],
)
def test_structurally_invalid_manifests_are_refused(overrides, message):
    with pytest.raises(StageAManifestError, match=message):
        dumps(_manifest(**overrides))


# --------------------------------------------------------------------------- #
# Frozen digest policy (§19, §34)
# --------------------------------------------------------------------------- #
def test_frozen_official_policy_matches_the_live_registry():
    """CI invariant: editing sources.py may not redefine a frozen version.

    If this fails, the source contract changed -- mint a NEW digest policy
    version rather than silently rewriting what an accepted lane's digest meant.
    """

    live_tables = sources.audited_source_tables("balldontlie")
    assert set(live_tables) == set(OFFICIAL_SOURCE_DIGEST_POLICY_V1.table_names())
    for table in live_tables:
        assert (tuple(sources.digest_columns_for(table))
                == OFFICIAL_SOURCE_DIGEST_POLICY_V1.columns_for(table))


def test_frozen_market_lane_policy_matches_the_live_registry():
    """Same invariant for the E0 lane's source contract."""

    assert (set(sources.LINKING_SOURCE_TABLES)
            == set(MARKET_EVENTS_E0_DIGEST_POLICY_V1.table_names()))
    for table in sources.LINKING_SOURCE_TABLES:
        assert (tuple(sources.digest_columns_for(table))
                == MARKET_EVENTS_E0_DIGEST_POLICY_V1.columns_for(table))


def test_registering_a_linking_provider_cannot_redefine_a_frozen_policy(monkeypatch):
    monkeypatch.setattr(
        sources, "REGISTERED_LINKING_PROVIDERS", frozenset({THE_ODDS_API_PROVIDER}))
    frozen = resolve_digest_policy("official-source-v1")
    assert frozen.table_names() == OFFICIAL_SOURCE_DIGEST_POLICY_V1.table_names()


@pytest.mark.parametrize("version", ["", "official-source-v2", "OFFICIAL-SOURCE-V1",
                                     "unknown", None])
def test_unknown_digest_policy_fails_closed(version):
    with pytest.raises(PolicyRegistryError):
        resolve_digest_policy(version)


@pytest.mark.parametrize("version", ["", "hme-projection-v2", "unknown"])
def test_unknown_projection_policy_fails_closed(version):
    with pytest.raises(PolicyRegistryError):
        require_projection_policy(version)


# --------------------------------------------------------------------------- #
# Plan membership closure and target integrity (§5, §6, §33)
# --------------------------------------------------------------------------- #
def test_bucket_cannot_be_added_after_an_attempt_exists(conn):
    _, acquisition_id, _ = _declared(conn)
    _raw_response(conn, "raw_ok")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_ok")
    plan_id = conn.execute("SELECT plan_id FROM stage_a_plans").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="membership is closed"):
        conn.execute(
            "INSERT INTO stage_a_planned_buckets (plan_id, requested_at_bucket,"
            " created_at) VALUES (?,?,?)", (plan_id, BUCKET_B, NOW))


def test_target_cannot_be_added_after_an_attempt_exists(conn):
    _, acquisition_id, _ = _declared(conn)
    _raw_response(conn, "raw_ok2")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_ok2")
    plan_id = conn.execute("SELECT plan_id FROM stage_a_plans").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="membership is closed"):
        conn.execute(
            "INSERT INTO stage_a_plan_targets (plan_id, canonical_game_id,"
            " requested_at_bucket, created_at) VALUES (?,?,?,?)",
            (plan_id, "gm_v2", BUCKET_A, NOW))


def test_target_citing_an_undeclared_bucket_is_refused(conn):
    plan_id, _, _ = _declared(conn)
    with pytest.raises(sqlite3.IntegrityError, match="did not declare"):
        conn.execute(
            "INSERT INTO stage_a_plan_targets (plan_id, canonical_game_id,"
            " requested_at_bucket, created_at) VALUES (?,?,?,?)",
            (plan_id, "gm_v2", BUCKET_B, NOW))


def test_target_cannot_map_to_two_buckets(conn):
    plan_id, _, _ = _declared(conn)
    conn.execute(
        "INSERT INTO stage_a_planned_buckets (plan_id, requested_at_bucket,"
        " created_at) VALUES (?,?,?)", (plan_id, BUCKET_B, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO stage_a_plan_targets (plan_id, canonical_game_id,"
            " requested_at_bucket, created_at) VALUES (?,?,?,?)",
            (plan_id, "gm_v1", BUCKET_B, NOW))


def test_attempt_on_an_undeclared_bucket_is_refused(conn):
    _, acquisition_id, _ = _declared(conn)
    _raw_response(conn, "raw_bad")
    with pytest.raises(sqlite3.IntegrityError, match="outside the declared plan"):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_B,
                       outcome="success_full_snapshot", raw_response_id="raw_bad")


def test_first_pass_forbids_a_retry(conn):
    _, acquisition_id, _ = _declared(conn)
    _raw_response(conn, "raw_r1")
    _raw_response(conn, "raw_r2")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="http_or_provider_failure", raw_response_id="raw_r1")
    with pytest.raises(sqlite3.IntegrityError, match="forbids retries"):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_A, attempt_ordinal=2,
                       outcome="success_full_snapshot", raw_response_id="raw_r2")


# --------------------------------------------------------------------------- #
# Plan-before-network and probe reuse (§10, §11, §22)
# --------------------------------------------------------------------------- #
def test_fetch_then_declare_is_refused_for_an_ordinary_success(conn):
    """Acquire first, declare afterwards, then cite it as normal success."""

    _seed(conn)
    _raw_response(conn, "raw_pre", requested_at="2026-01-01T00:00:00.000000Z",
                  received_at="2026-01-01T00:00:01.000000Z")
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    with pytest.raises(sqlite3.IntegrityError, match="before the acquisition was registered"):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_A,
                       outcome="success_full_snapshot", raw_response_id="raw_pre")


def test_unregistered_response_cannot_be_reused_as_a_probe(conn):
    _seed(conn)
    _raw_response(conn, "raw_pre2", requested_at="2026-01-01T00:00:00.000000Z",
                  received_at="2026-01-01T00:00:01.000000Z")
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    with pytest.raises(sqlite3.IntegrityError, match="no probe registration"):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_A,
                       outcome="reused_probe_response", raw_response_id="raw_pre2")


def test_registered_probe_may_be_reused(conn):
    """The narrow exception: same chronology, but the response is registered."""

    _seed(conn)
    _raw_response(conn, "raw_probe", requested_at="2026-01-01T00:00:00.000000Z",
                  received_at="2026-01-01T00:00:01.000000Z")
    register_probe_response(
        conn, raw_response_id="raw_probe", probe_report_commit_sha="abc",
        probe_report_path="ODDS_API_HISTORICAL_ENTITLEMENT_PROBE.md",
        probe_policy_version=PROBE_POLICY)
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    attempt_id = record_attempt(
        conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
        outcome="reused_probe_response", raw_response_id="raw_probe")
    assert attempt_id.startswith("sat_")


def test_filtered_probe_request_is_refused_by_the_gate(conn):
    _seed(conn)
    _raw_response(conn, "raw_filtered", requested_at="2026-01-01T00:00:00.000000Z",
                  received_at="2026-01-01T00:00:01.000000Z",
                  params='{"apiKey":"x","date":"d","eventIds":"one"}')
    register_probe_response(
        conn, raw_response_id="raw_filtered", probe_report_commit_sha="abc",
        probe_report_path="p.md", probe_policy_version=PROBE_POLICY)
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="reused_probe_response", raw_response_id="raw_filtered")
    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("filtered request parameters" in f for f in report.failures)


# --------------------------------------------------------------------------- #
# Raw-response and observation integrity (§12, §13)
# --------------------------------------------------------------------------- #
def test_backwards_receipt_clock_is_refused(conn):
    _seed(conn)
    with pytest.raises(sqlite3.IntegrityError, match="cannot arrive before"):
        _raw_response(conn, "raw_back", requested_at="2026-08-01T00:00:00.000000Z",
                      received_at="2026-03-01T00:00:00.000000Z")


def test_impossible_calendar_receipt_instant_is_refused(conn):
    _seed(conn)
    with pytest.raises(sqlite3.IntegrityError, match="not a real calendar instant"):
        _raw_response(conn, "raw_feb30", requested_at="2026-02-30T00:00:00.000000Z",
                      received_at="2026-02-30T00:00:01.000000Z")


def test_observed_at_must_equal_the_cited_received_at(conn):
    _seed(conn)
    _raw_response(conn, "raw_obs")
    with pytest.raises(sqlite3.IntegrityError, match="must equal the cited raw response"):
        conn.execute(
            "INSERT INTO historical_market_event_observations (observation_id,"
            " league_id, provider, namespace_generation, sport_key, provider_event_id,"
            " requested_at_bucket, provider_snapshot_timestamp, commence_time,"
            " home_team_raw, away_team_raw, observation_content_hash, raw_response_id,"
            " observed_at, created_at) VALUES ('hme_bad','lg_nba',?,'v4',"
            " 'basketball_nba', ?, ?, ?, NULL,'H','A','hash','raw_obs',"
            " '2026-03-01T00:00:00.000000Z', ?)",
            (THE_ODDS_API_PROVIDER, EVENT_ID, BUCKET_A, BUCKET_A, NOW))


# --------------------------------------------------------------------------- #
# Certification gate (§9, §24)
# --------------------------------------------------------------------------- #
def _observation(conn: sqlite3.Connection, obs_id: str, raw_id: str,
                 event_id: str = EVENT_ID) -> None:
    """Persist exactly the observation the fixture body projects to.

    ``obs_id`` is now only a label for readability: the stored id and content
    hash are computed with the PRODUCTION helpers. Since the certification gate
    composes the accepted projection and content-hash verifiers, an invented id
    or placeholder hash would be rejected as a forgery -- correctly -- and every
    test would fail for a reason it was not written to exercise.
    """

    assert obs_id  # keeps call sites self-documenting
    observation = MarketEventObservation(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        provider_event_id=event_id, requested_at_bucket=BUCKET_A,
        provider_snapshot_timestamp="2026-03-01T16:55:37.000000Z",
        commence_time="2026-03-01T18:10:00.000000Z",
        home_team_raw="Boston Celtics", away_team_raw="Miami Heat")
    received = conn.execute(
        "SELECT received_at FROM raw_responses WHERE raw_response_id = ?",
        (raw_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO historical_market_event_observations (observation_id, league_id,"
        " provider, namespace_generation, sport_key, provider_event_id,"
        " requested_at_bucket, provider_snapshot_timestamp, commence_time,"
        " home_team_raw, away_team_raw, observation_content_hash, raw_response_id,"
        " observed_at, created_at) VALUES (?, 'lg_nba', ?, 'v4','basketball_nba',"
        " ?, ?, ?, ?, 'Boston Celtics','Miami Heat', ?, ?, ?, ?)",
        (observation_id(observation), THE_ODDS_API_PROVIDER, event_id, BUCKET_A,
         "2026-03-01T16:55:37.000000Z", "2026-03-01T18:10:00.000000Z",
         observation_content_hash(observation), raw_id, received, utc_now_iso()))


def _complete_acquisition(conn: sqlite3.Connection) -> tuple[str, StageAManifest]:
    _, acquisition_id, manifest = _declared(conn)
    _raw_response(conn, "raw_c1")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_c1")
    _observation(conn, "hme_c1", "raw_c1")
    return acquisition_id, manifest


def test_a_complete_acquisition_certifies(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    report = _certify(conn, acquisition_id)
    assert report.certified, report.failures
    assert report.counts["planned_buckets"] == 1


def test_missing_bucket_keeps_the_acquisition_incomplete(conn):
    _seed(conn)
    manifest = _manifest(
        buckets=(BUCKET_A, BUCKET_B),
        targets=(StageATarget("gm_v1", BUCKET_A), StageATarget("gm_v2", BUCKET_B)))
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    _raw_response(conn, "raw_only")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_only")
    _observation(conn, "hme_only", "raw_only")
    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("never requested" in f for f in report.failures)


def test_manifest_target_omission_is_detected(conn):
    """A plan whose DB membership is richer than its committed artefact.

    Since B2 the caller cannot hand certification a convenient manifest, so the
    omission is expressed the only way that is now possible: the persisted plan
    carries two targets while the artefact it names carries one.
    """

    _seed(conn)
    full = _manifest()
    short = _manifest(targets=(StageATarget("gm_v1", BUCKET_A),))
    sha, name = _commit_manifest(short)
    plan_id = _record_plan_unverified(
        conn, full,                       # two targets persisted
        manifest_commit_sha=sha,
        manifest_content_digest=manifest_content_digest(dumps(short)),
        manifest_path=name)               # artefact declares one
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10,
        credit_budget=10)

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("target population differs" in f for f in report.failures), \
        report.failures


def test_a_full_snapshot_with_no_observations_fails(conn):
    _, acquisition_id, manifest = _declared(conn)
    _raw_response(conn, "raw_empty")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_empty")
    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("projected no observations" in f for f in report.failures)


def test_empty_data_success_is_valid_zero_event_evidence(conn):
    """HTTP 200 + valid wrapper + data=[] is evidence, not a failure."""

    _, acquisition_id, manifest = _declared(conn)
    _raw_response(conn, "raw_zero", body=_wrapper([]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_empty_data", raw_response_id="raw_zero")
    report = _certify(conn, acquisition_id)
    assert report.certified, report.failures


def test_failed_request_never_becomes_no_market(conn):
    _, acquisition_id, manifest = _declared(conn)
    _raw_response(conn, "raw_500", status=500)
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="http_or_provider_failure", raw_response_id="raw_500")
    report = _certify(conn, acquisition_id)
    # The bucket was requested, so it is not "not_requested" -- but a failure is
    # not projecting evidence either, so it cannot masquerade as zero events.
    assert report.counts["attempts"] == 1
    assert not any("never requested" in f for f in report.failures)


@pytest.mark.parametrize(
    "outcome", ["quota_blocked", "budget_blocked", "transport_failure"])
def test_pre_transport_outcomes_must_not_cite_a_response(conn, outcome):
    _, acquisition_id, _ = _declared(conn)
    _raw_response(conn, f"raw_{outcome}", requested_at=NOW, received_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_A, outcome=outcome,
                       raw_response_id=f"raw_{outcome}")


@pytest.mark.parametrize(
    "outcome",
    ["success_full_snapshot", "success_empty_data", "malformed_wrapper",
     "projection_rejected_snapshot", "http_or_provider_failure",
     "entitlement_or_auth_failure"])
def test_outcomes_with_a_preserved_exchange_require_a_response(conn, outcome):
    _, acquisition_id, _ = _declared(conn)
    with pytest.raises(sqlite3.IntegrityError):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_A, outcome=outcome,
                       raw_response_id=None)


# --------------------------------------------------------------------------- #
# Content-addressed corpus enrichment (§16, §32)
# --------------------------------------------------------------------------- #
def _corpus(conn: sqlite3.Connection, **overrides: Any):
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    base: dict[str, Any] = {
        "provenance_class": ProvenanceClass.RECONSTRUCTED_RESEARCH,
        "league_id": "lg_nba",
        "reconstruction_policy_version": "p1",
        "cutoff_policy_id": "cut",
        "cutoff_policy_version": "v1",
        "source_corpus_digest": "OFFICIAL_SRC",
        "target_set_digest": "OFFICIAL_TGT",
        "g1_variant": G1Variant.G1_B_CORE,
    }
    base.update(overrides)
    return repo, repo.record_corpus_version(**base)


def test_enriched_corpus_is_a_distinct_superseding_row(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, parent = _corpus(conn)
    parent_digest = parent.semantic_digest

    new_id, lane_id = enrich_corpus_with_market_lane(
        conn, repo, parent_corpus_id=parent.corpus_version_id,
        acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", repo_root=_b2_repo())

    assert new_id != parent.corpus_version_id
    child = conn.execute(
        "SELECT * FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (new_id,)).fetchone()
    assert child["supersedes_corpus_version_id"] == parent.corpus_version_id
    assert child["market_evidence_digest"] is not None
    assert child["semantic_digest"] != parent_digest

    # C1 is untouched.
    unchanged = conn.execute(
        "SELECT semantic_digest, market_evidence_digest FROM"
        " reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (parent.corpus_version_id,)).fetchone()
    assert unchanged["semantic_digest"] == parent_digest
    assert unchanged["market_evidence_digest"] is None
    assert verify_lane_binding(conn, lane_binding_id=lane_id) == ()


def test_lane_cannot_attach_to_a_corpus_with_different_official_provenance(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, wrong = _corpus(conn, source_corpus_digest="SOMETHING_ELSE")
    with pytest.raises(StageAProvenanceError, match="different official source corpus"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=wrong.corpus_version_id,
            acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
            namespace_generation="v4", repo_root=_b2_repo())


def test_lane_cannot_attach_to_a_corpus_with_a_different_target_set(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, wrong = _corpus(conn, target_set_digest="OTHER_TARGETS")
    with pytest.raises(StageAProvenanceError, match="different official target set"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=wrong.corpus_version_id,
            acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
            namespace_generation="v4", repo_root=_b2_repo())


def test_market_lane_cannot_be_bound_to_a_corpus_that_does_not_commit_to_it(conn):
    """Attaching a lane while leaving market_evidence_digest NULL is refused."""

    _complete_acquisition(conn)
    _, parent = _corpus(conn)
    with pytest.raises(sqlite3.IntegrityError, match="does not commit to this E0 lane"):
        conn.execute(
            "INSERT INTO corpus_evidence_lane_bindings (lane_binding_id,"
            " corpus_version_id, evidence_lane, provider, namespace_generation,"
            " league_id, digest_policy_version, lane_evidence_digest,"
            " acquisition_set_digest, projection_policy_version, created_at)"
            " VALUES ('eln_bad', ?, 'market_events_e0', ?, 'v4', 'lg_nba',"
            " 'market-events-e0-v1','FORGED','SET',?,?)",
            (parent.corpus_version_id, THE_ODDS_API_PROVIDER, PROJ_POLICY, NOW))


def test_forged_acquisition_set_digest_is_detected(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, parent = _corpus(conn)
    _, lane_id = enrich_corpus_with_market_lane(
        conn, repo, parent_corpus_id=parent.corpus_version_id,
        acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", repo_root=_b2_repo())
    # Add a second acquisition to the lane WITHOUT updating the set digest.
    plan_id = conn.execute("SELECT plan_id FROM stage_a_plans").fetchone()[0]
    second = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    conn.execute(
        "INSERT INTO corpus_evidence_lane_acquisitions (lane_binding_id,"
        " acquisition_id, created_at) VALUES (?,?,?)", (lane_id, second, NOW))
    failures = verify_lane_binding(conn, lane_binding_id=lane_id)
    assert any("acquisition_set_digest" in f for f in failures)


def test_acquisition_set_digest_is_order_independent():
    assert acquisition_set_digest(["a", "b"]) == acquisition_set_digest(["b", "a"])
    assert acquisition_set_digest(["a"]) != acquisition_set_digest(["a", "b"])


# --------------------------------------------------------------------------- #
# Audit lane binding (§26)
# --------------------------------------------------------------------------- #
def _audit(conn: sqlite3.Connection, audit_id: str, provider: str,
           lane_binding_id: object = None, digest: str = "OFFICIAL_SRC") -> None:
    conn.execute(
        "INSERT INTO identity_audit_records (identity_audit_id, league_id, provider,"
        " namespace_generation, namespace_verified, entity_type, source_corpus_digest,"
        " audit_policy_version, distinct_ids, total_observations, collision_count,"
        " flagged_count, verdict, semantic_digest, created_at, lane_binding_id)"
        " VALUES (?, 'lg_nba', ?, 'v4', 1, 'game', ?, 'pol', 1, 1, 0, 0, 'accepted',"
        " ?, ?, ?)", (audit_id, provider, digest, f"SEM_{audit_id}", NOW,
                      lane_binding_id))


def test_non_official_audit_with_a_null_lane_fails_closed(conn):
    """The exact bypass the v22 review reproduced at v21."""

    _seed(conn)
    with pytest.raises(sqlite3.IntegrityError, match="must cite an evidence lane"):
        _audit(conn, "ida_bypass", THE_ODDS_API_PROVIDER, lane_binding_id=None)


def test_legacy_official_audit_with_a_null_lane_remains_accepted(conn):
    _seed(conn)
    _audit(conn, "ida_legacy", "balldontlie", lane_binding_id=None)
    stored = conn.execute(
        "SELECT lane_binding_id FROM identity_audit_records"
        " WHERE identity_audit_id = 'ida_legacy'").fetchone()[0]
    assert stored is None


def test_audit_citing_a_lane_for_another_provider_is_refused(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, parent = _corpus(conn)
    _, lane_id = enrich_corpus_with_market_lane(
        conn, repo, parent_corpus_id=parent.corpus_version_id,
        acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", repo_root=_b2_repo())
    with pytest.raises(sqlite3.IntegrityError, match="lane for a different provider"):
        _audit(conn, "ida_wrong", "balldontlie", lane_binding_id=lane_id)


def test_audit_digest_must_equal_its_cited_lane_digest(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, parent = _corpus(conn)
    _, lane_id = enrich_corpus_with_market_lane(
        conn, repo, parent_corpus_id=parent.corpus_version_id,
        acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", repo_root=_b2_repo())
    with pytest.raises(sqlite3.IntegrityError, match="does not equal its cited lane"):
        _audit(conn, "ida_forged", THE_ODDS_API_PROVIDER, lane_binding_id=lane_id,
               digest="FORGED")


# --------------------------------------------------------------------------- #
# Append-only hardening (§33)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "table, key_column",
    [("stage_a_plans", "plan_id"),
     ("stage_a_acquisitions", "acquisition_id"),
     ("stage_a_request_attempts", "attempt_id")],
)
def test_new_tables_reject_update_and_delete(conn, table, key_column):
    _complete_acquisition(conn)
    row_id = conn.execute(f"SELECT {key_column} FROM {table}").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET {key_column} = 'x' WHERE {key_column} = ?",
                     (row_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table} WHERE {key_column} = ?", (row_id,))


def test_replace_cannot_mutate_a_recorded_attempt(conn):
    """SQLite REPLACE performs an implicit DELETE that skips DELETE triggers."""

    acquisition_id, manifest = _complete_acquisition(conn)
    attempt = conn.execute("SELECT * FROM stage_a_request_attempts").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO stage_a_request_attempts (attempt_id,"
            " acquisition_id, requested_at_bucket, attempt_ordinal, outcome,"
            " raw_response_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (attempt["attempt_id"], acquisition_id, BUCKET_A, 1,
             "success_empty_data", attempt["raw_response_id"], NOW))
    # The recorded outcome is unchanged: REPLACE did not mutate it.
    assert conn.execute(
        "SELECT outcome FROM stage_a_request_attempts WHERE attempt_id = ?",
        (attempt["attempt_id"],)).fetchone()[0] == "success_full_snapshot"


def test_replace_cannot_mutate_a_lane_digest(conn):
    acquisition_id, manifest = _complete_acquisition(conn)
    repo, parent = _corpus(conn)
    _, lane_id = enrich_corpus_with_market_lane(
        conn, repo, parent_corpus_id=parent.corpus_version_id,
        acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", repo_root=_b2_repo())
    lane = conn.execute(
        "SELECT * FROM corpus_evidence_lane_bindings WHERE lane_binding_id = ?",
        (lane_id,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "REPLACE INTO corpus_evidence_lane_bindings (lane_binding_id,"
            " corpus_version_id, evidence_lane, provider, namespace_generation,"
            " league_id, digest_policy_version, lane_evidence_digest,"
            " acquisition_set_digest, projection_policy_version, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lane_id, lane["corpus_version_id"], lane["evidence_lane"],
             lane["provider"], lane["namespace_generation"], lane["league_id"],
             lane["digest_policy_version"], "FORGED", lane["acquisition_set_digest"],
             lane["projection_policy_version"], NOW))
    # The stored lane digest is unchanged.
    assert conn.execute(
        "SELECT lane_evidence_digest FROM corpus_evidence_lane_bindings"
        " WHERE lane_binding_id = ?", (lane_id,)).fetchone()[0] != "FORGED"


def test_one_response_cannot_serve_two_buckets_in_one_acquisition(conn):
    _seed(conn)
    manifest = _manifest(
        buckets=(BUCKET_A, BUCKET_B),
        targets=(StageATarget("gm_v1", BUCKET_A), StageATarget("gm_v2", BUCKET_B)))
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ_POLICY,
        projection_policy_version=PROJ_POLICY, request_budget=10, credit_budget=10)
    _raw_response(conn, "raw_shared")
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET_A,
                   outcome="success_full_snapshot", raw_response_id="raw_shared")
    with pytest.raises(sqlite3.IntegrityError):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET_B,
                       outcome="success_full_snapshot", raw_response_id="raw_shared")


# --------------------------------------------------------------------------- #
# Non-regression (§29)
# --------------------------------------------------------------------------- #
def test_linking_provider_registry_is_still_empty():
    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()


def test_odds_provider_has_no_official_authority():
    assert THE_ODDS_API_PROVIDER not in sources.PROVIDER_LEAGUES
    with pytest.raises(sources.SourceCorpusError):
        sources.audited_source_tables(THE_ODDS_API_PROVIDER)
