"""Independent adversarial review of v22 Stage-A provenance.

Every test below FAILS against the reviewed implementation at `46f1725` and
passes after the repairs. They are written so they cannot be satisfied by the
production helpers agreeing with themselves: bodies are built here, and the
attack is always "make the ledger claim something the preserved exchange does
not support".

No provider is contacted, no credit is spent, and nothing under ``data/`` is
opened.
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
from sports_quant.retrospective.market_observations import (
    MarketEventObservation,
    observation_content_hash,
    observation_id,
)
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass
from sports_quant.retrospective.stage_a_manifest import (
    StageAManifest,
    StageATarget,
    dumps,
    manifest_content_digest,
)
from sports_quant.retrospective.stage_a_provenance import (
    StageAProvenanceError,
    _record_plan_unverified,
    certify_stage_a,
    enrich_corpus_with_market_lane,
    record_attempt,
    record_committed_plan,
    register_acquisition,
    register_probe_response,
)

BUCKET = "2026-03-01T17:00:00.000000Z"
SNAP = "2026-03-01T16:55:37.000000Z"
COMMENCE = "2026-03-01T18:10:00.000000Z"
EV_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EV_B = "111a955795876d50988b15c219ce0796"
ENDPOINT = "/v4/historical/sports/basketball_nba/events"
ACQ = "stage-a-acquisition-v1"
PROJ = "hme-projection-v1"



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


def _event(event_id: str, home: str, away: str) -> dict[str, Any]:
    return {"id": event_id, "sport_key": "basketball_nba",
            "commence_time": "2026-03-01T18:10:00Z",
            "home_team": home, "away_team": away}


def _wrapper(events: list[dict[str, Any]]) -> str:
    return json.dumps({"timestamp": "2026-03-01T16:55:37Z",
                       "previous_timestamp": "2026-03-01T16:50:37Z",
                       "next_timestamp": "2026-03-01T17:00:37Z",
                       "data": events})


def _seed(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    # Idempotent: several helpers call this, and the shared fixture may already
    # provide the league.
    if conn.execute(
            "SELECT 1 FROM seasons WHERE season_id='sn_rv'").fetchone() is not None:
        return
    if conn.execute("SELECT 1 FROM leagues WHERE league_id='lg_nba'").fetchone() is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date,"
        " end_date, created_at, updated_at) VALUES ('sn_rv','lg_nba',2025,'2025-26',"
        " 'regular','2025-10-01','2026-06-30',?,?)", (now, now))
    for tid, name, abbr in (("tm_r1", "Review Celtics", "RV1"),
                            ("tm_r2", "Review Heat", "RV2")):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname,"
            " abbreviation, created_at, updated_at) VALUES (?, 'lg_nba', ?,?,?,?,?,?)",
            (tid, name, name, name, abbr, now, now))
    for gid, day in (("gm_r1", "2026-03-01"), ("gm_r2", "2026-03-02")):
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id,"
            " away_team_id, scheduled_start, original_start, game_date_local, status,"
            " created_at, updated_at) VALUES (?, 'lg_nba','sn_rv','tm_r1','tm_r2',"
            " ?,?,?, 'final', ?, ?)",
            (gid, f"{day}T18:10:00Z", f"{day}T18:10:00Z", day, now, now))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json,"
        " status, requested_at, started_at, started_monotonic_ns, requests_made,"
        " records_received, records_normalized, records_inserted,"
        " records_deduplicated, records_rejected, records_updated, tool_version,"
        " created_at) VALUES ('run_rv','x',?,'op','{}','started',?,?,1,0,0,0,0,0,0,0,"
        " 'v',?)", (THE_ODDS_API_PROVIDER, now, now, now))
    conn.commit()


def _raw(conn: sqlite3.Connection, rid: str, body: str, *, status: int = 200,
         requested_at: str | None = None, params: str | None = None) -> None:
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body, body_bytes,"
        " body_hash, content_hash, requested_at, received_at, elapsed_ns, created_at)"
        " VALUES (?, 'run_rv', ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, 1, ?)",
        (rid, THE_ODDS_API_PROVIDER, ENDPOINT,
         params or json.dumps({"apiKey": "x", "date": BUCKET, "dateFormat": "iso"}),
         status, body, len(body), rid, rid, requested_at or now, now, now))
    conn.commit()


def _obs(conn: sqlite3.Connection, rid: str, event_id: str, home: str, away: str,
         *, content_hash: str | None = None) -> None:
    observation = MarketEventObservation(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        provider_event_id=event_id, requested_at_bucket=BUCKET,
        provider_snapshot_timestamp=SNAP, commence_time=COMMENCE,
        home_team_raw=home, away_team_raw=away)
    received = conn.execute(
        "SELECT received_at FROM raw_responses WHERE raw_response_id = ?",
        (rid,)).fetchone()[0]
    conn.execute(
        "INSERT INTO historical_market_event_observations (observation_id, league_id,"
        " provider, namespace_generation, sport_key, provider_event_id,"
        " requested_at_bucket, provider_snapshot_timestamp, commence_time,"
        " home_team_raw, away_team_raw, observation_content_hash, raw_response_id,"
        " observed_at, created_at) VALUES (?, 'lg_nba', ?, 'v4','basketball_nba',"
        " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (observation_id(observation), THE_ODDS_API_PROVIDER, event_id, BUCKET, SNAP,
         COMMENCE, home, away, content_hash or observation_content_hash(observation),
         rid, received, utc_now_iso()))
    conn.commit()


def _manifest(**over: Any) -> StageAManifest:
    base: dict[str, Any] = dict(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        official_source_corpus_digest="OFF_SRC",
        official_target_set_digest="OFF_TGT",
        targets=(StageATarget("gm_r1", BUCKET), StageATarget("gm_r2", BUCKET)),
        buckets=(BUCKET,), decision_horizon_minutes=60, bucket_floor_seconds=300,
        request_budget=10, credit_budget=10, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, cost_policy_version="odds-cost-v1")
    base.update(over)
    return StageAManifest(**base)


def _declared(conn: sqlite3.Connection, **over: Any) -> tuple[str, StageAManifest]:
    _seed(conn)
    manifest = _manifest(**over)
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    return acquisition_id, manifest


# --------------------------------------------------------------------------- #
# D1 -- the gate must COMPOSE the accepted projection / body verifier
# --------------------------------------------------------------------------- #
def test_selective_materialization_is_refused(conn):
    """The L1 defect: body lists two events, only the convenient one is stored.

    At 46f1725 this CERTIFIED. `_observation_failures` only checked observed_at,
    orphans, and "a full snapshot has >=1 observation" -- none of which opens the
    preserved body, so half the provider's evidence could vanish silently.
    """

    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat"),
                                  _event(EV_B, "Denver Nuggets", "Phoenix Suns")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("no stored observation" in f for f in report.failures)


def test_an_invented_observation_is_refused(conn):
    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")
    _obs(conn, "raw_1", EV_B, "Invented FC", "Fabricated United")

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("not derivable from its body" in f for f in report.failures)


def test_a_tampered_team_label_is_refused(conn):
    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "TAMPERED FC", "Miami Heat")

    report = _certify(conn, acquisition_id)
    assert not report.certified


def test_a_forged_content_hash_is_refused(conn):
    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat", content_hash="f" * 64)

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("content hash" in f for f in report.failures)


def test_a_malformed_body_cannot_be_recorded_as_a_success(conn):
    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", '{"garbage": true}')
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")

    report = _certify(conn, acquisition_id)
    assert not report.certified


# --------------------------------------------------------------------------- #
# D7 -- the outcome label is a CALLER CLAIM and must be derived from the body
# --------------------------------------------------------------------------- #
def test_a_real_event_cannot_be_erased_by_mislabelling_the_outcome(conn):
    """Body carries an event; the ledger claims the snapshot was empty."""

    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_empty_data", raw_response_id="raw_1")

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("success_empty_data" in f for f in report.failures)


def test_a_genuinely_empty_snapshot_still_certifies(conn):
    """The counterpart: valid zero-event evidence must remain acceptable."""

    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_empty_data", raw_response_id="raw_1")

    report = _certify(conn, acquisition_id)
    assert report.certified, report.failures


# --------------------------------------------------------------------------- #
# D6 -- stored budgets and policy versions must agree with the manifest
# --------------------------------------------------------------------------- #
def test_budgets_that_disagree_with_the_manifest_are_refused(conn):
    _seed(conn)
    manifest = _manifest(request_budget=10, credit_budget=10)
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=1, credit_budget=0)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("credit_budget" in f for f in report.failures)


# --------------------------------------------------------------------------- #
# D3 -- registered_at must not be a supported backdating parameter
# --------------------------------------------------------------------------- #
def test_register_acquisition_does_not_accept_a_caller_clock():
    """The architecture deleted `declared_at` for exactly this reason."""

    import inspect

    assert "registered_at" not in inspect.signature(register_acquisition).parameters


def test_fetch_then_declare_is_refused_through_the_public_api(conn):
    _seed(conn)
    _raw(conn, "raw_pre", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]),
         requested_at="2020-01-01T00:00:00.000000Z")
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)

    with pytest.raises(sqlite3.IntegrityError, match="before the acquisition"):
        record_attempt(conn, acquisition_id=acquisition_id,
                       requested_at_bucket=BUCKET,
                       outcome="success_full_snapshot", raw_response_id="raw_pre")


# --------------------------------------------------------------------------- #
# D2 -- enrichment must invoke the gate for EVERY member
# --------------------------------------------------------------------------- #
def _parent(conn: sqlite3.Connection, **over: Any):
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    base: dict[str, Any] = {
        "provenance_class": ProvenanceClass.RECONSTRUCTED_RESEARCH,
        "league_id": "lg_nba", "reconstruction_policy_version": "p1",
        "cutoff_policy_id": "cut", "cutoff_policy_version": "v1",
        "source_corpus_digest": "OFF_SRC", "target_set_digest": "OFF_TGT",
        "g1_variant": G1Variant.G1_B_CORE}
    base.update(over)
    return repo, repo.record_corpus_version(**base)


def test_an_uncertified_acquisition_cannot_be_enriched_into_a_corpus(conn):
    """At 46f1725 this SUCCEEDED, minting a C2 from incomplete evidence.

    A derived verdict that no consumer is required to invoke is not a trust
    gate, so the only call that creates load-bearing downstream provenance must
    call it.
    """

    other = "2026-03-01T17:05:00.000000Z"
    acquisition_id, manifest = _declared(
        conn, buckets=(BUCKET, other),
        targets=(StageATarget("gm_r1", BUCKET), StageATarget("gm_r2", other)))
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")

    assert not _certify(conn, acquisition_id).certified

    repo, parent = _parent(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]
    with pytest.raises(StageAProvenanceError, match="uncertified acquisition"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=parent.corpus_version_id,
            acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
            namespace_generation="v4", repo_root=_b2_repo())
    after = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]
    assert after == before
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_evidence_lane_bindings").fetchone()[0] == 0


def test_enrichment_cannot_proceed_without_a_resolvable_committed_manifest(conn):
    """B2 replaced the old `manifest_text` requirement with something stronger.

    Previously enrichment demanded the caller SUPPLY the committed text, which
    still placed the caller on the trust path. Now it derives the manifest from
    the plan row's commit and path, so a plan naming an unresolvable commit
    cannot build a lane at all -- there is no argument left to get right.
    """

    _seed(conn)
    manifest = _manifest()
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha="a" * 40,      # never committed
        manifest_content_digest=manifest_content_digest(dumps(manifest)),
        manifest_path="pilots/stage_a/ghost.json")
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")
    repo, parent = _parent(conn)

    with pytest.raises(StageAProvenanceError, match="uncertified acquisition"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=parent.corpus_version_id,
            acquisition_ids=[acquisition_id], provider=THE_ODDS_API_PROVIDER,
            namespace_generation="v4", repo_root=_b2_repo())
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_evidence_lane_bindings").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# D9 / O / P -- atomicity and argument handling
# --------------------------------------------------------------------------- #
def test_a_failed_enrichment_leaves_no_orphan_corpus(conn):
    """At 46f1725 a mid-function failure left C2 committing to evidence with no
    lane provenance to reconstruct it from."""

    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")
    repo, parent = _parent(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=parent.corpus_version_id,
            acquisition_ids=[acquisition_id], provider="fake_provider",
            namespace_generation="v4", repo_root=_b2_repo())

    after = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]
    assert after == before, "an orphan enriched corpus survived a failed enrichment"
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_evidence_lane_bindings").fetchone()[0] == 0


def test_duplicate_member_acquisitions_are_refused_not_collapsed(conn):
    acquisition_id, manifest = _declared(conn)
    _raw(conn, "raw_1", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]))
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="success_full_snapshot", raw_response_id="raw_1")
    _obs(conn, "raw_1", EV_A, "Boston Celtics", "Miami Heat")
    repo, parent = _parent(conn)

    with pytest.raises(StageAProvenanceError, match="duplicate"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=parent.corpus_version_id,
            acquisition_ids=[acquisition_id, acquisition_id],
            provider=THE_ODDS_API_PROVIDER, namespace_generation="v4", repo_root=_b2_repo())


def test_an_empty_member_set_gives_a_domain_refusal(conn):
    """Previously surfaced as a confusing 'mixed projection policies []'."""

    _seed(conn)
    repo, parent = _parent(conn)
    with pytest.raises(StageAProvenanceError, match="no member acquisitions"):
        enrich_corpus_with_market_lane(
            conn, repo, parent_corpus_id=parent.corpus_version_id,
            acquisition_ids=[], provider=THE_ODDS_API_PROVIDER,
            namespace_generation="v4", repo_root=_b2_repo())


# --------------------------------------------------------------------------- #
# R -- plan declaration is atomic
# --------------------------------------------------------------------------- #
def test_a_rejected_plan_leaves_no_partial_declaration(conn):
    """Targets and buckets are "closed together", which requires atomicity."""

    _seed(conn)
    # A target whose game is absent violates the FK on the LAST write, after the
    # plan row and bucket rows have already been inserted.
    manifest = _manifest(
        targets=(StageATarget("gm_r1", BUCKET), StageATarget("gm_missing", BUCKET)))
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        _declare_committed(conn, manifest)

    assert conn.execute("SELECT COUNT(*) FROM stage_a_plans").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_planned_buckets").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_plan_targets").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# RETAINED BLOCKERS -- documented, deliberately NOT repaired here
# --------------------------------------------------------------------------- #
def test_probe_registration_is_now_content_bound(conn):
    """B1 CLOSED. This test previously PINNED the blocker; it now proves the fix.

    At `8703239` the sequence below CERTIFIED: an arbitrary 2020 response, an
    all-zero commit SHA and a nonexistent report path were enough, because the
    registration row was the only thing consulted.

    Certification now re-resolves the commit, loads the committed report, parses
    the frozen contract and re-derives the UNIQUE matching response. The all-zero
    SHA fails at the first step, so the registration is a dead pointer rather than
    a grant of eligibility.
    """

    _seed(conn)
    _raw(conn, "raw_old", _wrapper([_event(EV_A, "Boston Celtics", "Miami Heat")]),
         requested_at="2020-01-01T00:00:00.000000Z")
    register_probe_response(
        conn, raw_response_id="raw_old", probe_report_commit_sha="0" * 40,
        probe_report_path="does/not/exist.md",
        probe_policy_version="stage-a-probe-v1")
    manifest = _manifest()
    plan_id = _declare_committed(conn, manifest)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    record_attempt(conn, acquisition_id=acquisition_id, requested_at_bucket=BUCKET,
                   outcome="reused_probe_response", raw_response_id="raw_old")
    _obs(conn, "raw_old", EV_A, "Boston Celtics", "Miami Heat")

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("does not bind to committed evidence" in f for f in report.failures),         report.failures


def test_manifest_commit_sha_is_now_resolved(conn):
    """B2 CLOSED. This test previously PINNED the blocker; it now proves the fix.

    At `e98363d` a fabricated 40-character commit id was stored and never
    checked, so a plan certified while its alleged source-control provenance was
    imaginary. Certification now resolves the commit named by the PLAN ROW and
    loads the manifest from it, so the fabrication fails at the first step.
    """

    _seed(conn)
    manifest = _manifest()
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha="a" * 40,      # fabricated
        manifest_content_digest=manifest_content_digest(dumps(manifest)),
        manifest_path="pilots/stage_a/ghost.json")
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)

    stored = conn.execute(
        "SELECT manifest_commit_sha FROM stage_a_plans WHERE plan_id=?",
        (plan_id,)).fetchone()[0]
    assert stored == "a" * 40, "the row still stores whatever it was given"

    report = _certify(conn, acquisition_id)
    assert not report.certified
    assert any("not bound to source control" in f for f in report.failures),         report.failures


def test_retained_receipt_ordering_is_second_granularity(conn):
    """RETAINED BLOCKER: the f022 ordering trigger compares 19 characters.

    A response can be recorded as received 800ms BEFORE it was requested, within
    the same second. Harmless today because nothing depends on sub-second
    ordering, but the documented claim is stronger than the check.
    """

    _seed(conn)
    _raw(conn, "raw_sub", _wrapper([]),
         requested_at="2026-03-01T12:00:00.900000Z")
    conn.execute(
        "UPDATE raw_responses SET received_at = '2026-03-01T12:00:00.100000Z'"
        " WHERE raw_response_id = 'raw_sub'") if False else None
    # Inserted directly to show the trigger admits it.
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body, body_bytes,"
        " body_hash, content_hash, requested_at, received_at, elapsed_ns, created_at)"
        " VALUES ('raw_sub2','run_rv', ?, ?, '{}', 200, '{}', '[]', 2, 'h','c',"
        " '2026-03-01T12:00:00.900000Z','2026-03-01T12:00:00.100000Z', 1, ?)",
        (THE_ODDS_API_PROVIDER, ENDPOINT, utc_now_iso()))
    row = conn.execute(
        "SELECT requested_at, received_at FROM raw_responses"
        " WHERE raw_response_id = 'raw_sub2'").fetchone()
    assert row["received_at"] < row["requested_at"]
