"""Adversarial tests for retained blocker B2 -- plan manifest commit binding.

Every test here fails against `e98363d`. Git objects are real but built in
self-contained temporary repositories, so the suite runs at any checkout depth.
No network is used, including no git fetch.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sports_quant.db.schema import THE_ODDS_API_PROVIDER, utc_now_iso
from sports_quant.retrospective.stage_a_manifest import (
    StageAManifest,
    StageAManifestError,
    StageATarget,
    dumps,
    loads,
    manifest_content_digest_bytes,
)
from sports_quant.retrospective.stage_a_plan_binding import (
    PlanBindingError,
    load_committed_stage_a_manifest,
)
from sports_quant.retrospective.stage_a_probe_binding import (
    ProbeBindingError,
    load_committed_bytes,
    validate_repo_path,
)
from sports_quant.retrospective.stage_a_provenance import (
    _record_plan_unverified,
    certify_stage_a,
    record_committed_plan,
    register_acquisition,
)

BUCKET = "2026-03-01T17:00:00.000000Z"
MANIFEST_PATH = "pilots/stage_a/plan.json"
ACQ = "stage-a-acquisition-v1"
PROJ = "hme-projection-v1"


def _manifest(**over: Any) -> StageAManifest:
    base: dict[str, Any] = dict(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        official_source_corpus_digest="OFF_SRC",
        official_target_set_digest="OFF_TGT",
        targets=(StageATarget("gm_b1", BUCKET), StageATarget("gm_b2", BUCKET)),
        buckets=(BUCKET,), decision_horizon_minutes=60, bucket_floor_seconds=300,
        request_budget=10, credit_budget=10, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, cost_policy_version="odds-cost-v1")
    base.update(over)
    return StageAManifest(**base)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "planrepo"
    if not (repo / ".git").exists():
        (repo / "pilots" / "stage_a").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    return repo


def _commit(repo: Path, path: str, content: bytes | str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", path], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _seed(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    if conn.execute("SELECT 1 FROM seasons WHERE season_id='sn_b2'").fetchone():
        return
    if conn.execute("SELECT 1 FROM leagues WHERE league_id='lg_nba'").fetchone() is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date,"
        " end_date, created_at, updated_at) VALUES ('sn_b2','lg_nba',2025,'2025-26',"
        " 'regular','2025-10-01','2026-06-30',?,?)", (now, now))
    for tid, name, abbr in (("tm_b1", "B2 Alpha", "BA1"), ("tm_b2", "B2 Beta", "BB2")):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname,"
            " abbreviation, created_at, updated_at) VALUES (?, 'lg_nba', ?,?,?,?,?,?)",
            (tid, name, name, name, abbr, now, now))
    for gid, day in (("gm_b1", "2026-03-01"), ("gm_b2", "2026-03-02")):
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id,"
            " away_team_id, scheduled_start, original_start, game_date_local, status,"
            " created_at, updated_at) VALUES (?, 'lg_nba','sn_b2','tm_b1','tm_b2',"
            " ?,?,?, 'final', ?, ?)",
            (gid, f"{day}T18:10:00Z", f"{day}T18:10:00Z", day, now, now))
    conn.commit()


# --------------------------------------------------------------------------- #
# THE B2 DEFECT, reproduced
# --------------------------------------------------------------------------- #
def test_a_fabricated_commit_id_cannot_certify(conn, tmp_path):
    """At `e98363d` this CERTIFIED: DB and caller text agreed, and nothing ever
    asked git whether the named commit existed."""

    _seed(conn)
    manifest = _manifest()
    text = dumps(manifest)
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha="a" * 40,          # fabricated
        manifest_content_digest=manifest_content_digest_bytes(text.encode()),
        manifest_path=MANIFEST_PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)

    report = certify_stage_a(conn, acquisition_id=acquisition_id,
                             repo_root=_repo(tmp_path))
    assert not report.certified
    assert any("not bound to source control" in f for f in report.failures), \
        report.failures


def test_db_and_caller_agreement_is_not_source_control_provenance(conn, tmp_path):
    """The invariant in one sentence: three caller-controlled values agreeing
    proves nothing about what was committed."""

    _seed(conn)
    manifest = _manifest()
    text = dumps(manifest)
    # Internally perfectly consistent: digest matches the text, plan digest
    # matches the manifest, targets match. Only the commit is imaginary.
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha="b" * 40,
        manifest_content_digest=manifest_content_digest_bytes(text.encode()),
        manifest_path=MANIFEST_PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    assert not certify_stage_a(conn, acquisition_id=acquisition_id,
                               repo_root=_repo(tmp_path)).certified


# --------------------------------------------------------------------------- #
# The trusted declaration path
# --------------------------------------------------------------------------- #
def test_record_committed_plan_derives_everything_from_the_commit(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    manifest = _manifest()
    text = dumps(manifest)
    sha = _commit(repo, MANIFEST_PATH, text)

    plan_id = record_committed_plan(
        conn, manifest_commit_sha=sha, manifest_path=MANIFEST_PATH, repo_root=repo)
    row = conn.execute("SELECT * FROM stage_a_plans WHERE plan_id = ?",
                       (plan_id,)).fetchone()
    assert row["manifest_commit_sha"] == sha
    assert row["manifest_content_digest"] == manifest_content_digest_bytes(
        text.encode())
    assert row["plan_digest"] == manifest.plan_digest()
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_plan_targets WHERE plan_id=?",
        (plan_id,)).fetchone()[0] == 2

    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    # Only the acquisition side remains incomplete; the plan binding itself is
    # proven, which is what B2 is about.
    assert not any("source control" in f for f in report.failures), report.failures


def test_declaration_is_atomic_when_the_artefact_is_unprovable(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM stage_a_plans").fetchone()[0]
    with pytest.raises(PlanBindingError):
        record_committed_plan(conn, manifest_commit_sha="c" * 40,
                              manifest_path=MANIFEST_PATH, repo_root=repo)
    assert conn.execute("SELECT COUNT(*) FROM stage_a_plans").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_planned_buckets").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_plan_targets").fetchone()[0] == 0


def test_a_target_that_violates_a_constraint_rolls_the_plan_back(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    manifest = _manifest(
        targets=(StageATarget("gm_b1", BUCKET), StageATarget("gm_absent", BUCKET)))
    sha = _commit(repo, MANIFEST_PATH, dumps(manifest))
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        record_committed_plan(conn, manifest_commit_sha=sha,
                              manifest_path=MANIFEST_PATH, repo_root=repo)
    assert conn.execute("SELECT COUNT(*) FROM stage_a_plans").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM stage_a_planned_buckets").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# The committed artefact is authoritative
# --------------------------------------------------------------------------- #
def _declared(conn: sqlite3.Connection, repo: Path, manifest: StageAManifest):
    sha = _commit(repo, MANIFEST_PATH, dumps(manifest))
    plan_id = record_committed_plan(
        conn, manifest_commit_sha=sha, manifest_path=MANIFEST_PATH, repo_root=repo)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    return sha, plan_id, acquisition_id


def test_a_working_tree_edit_cannot_change_verification(conn, tmp_path):
    """Commit holds M1; the working tree is rewritten to M2. M1 governs."""

    _seed(conn)
    repo = _repo(tmp_path)
    m1 = _manifest()
    sha, plan_id, acquisition_id = _declared(conn, repo, m1)

    m2 = _manifest(request_budget=999)
    (repo / MANIFEST_PATH).write_text(dumps(m2), encoding="utf-8")   # uncommitted

    committed = load_committed_stage_a_manifest(sha, MANIFEST_PATH, repo_root=repo)
    assert committed.manifest.request_budget == 10
    assert not any("source control" in f for f in certify_stage_a(
        conn, acquisition_id=acquisition_id, repo_root=repo).failures)


def test_a_later_commit_changing_the_manifest_invalidates_the_plan(conn, tmp_path):
    """Inverse direction: the plan row is stale relative to what it names."""

    _seed(conn)
    repo = _repo(tmp_path)
    m1 = _manifest()
    _commit(repo, MANIFEST_PATH, dumps(m1))

    # A second commit changes the artefact; a forged plan row points at it while
    # still carrying M1's derived values.
    m2 = _manifest(request_budget=42)
    sha2 = _commit(repo, MANIFEST_PATH, dumps(m2))
    forged = _record_plan_unverified(
        conn, m1, manifest_commit_sha=sha2,
        manifest_content_digest=manifest_content_digest_bytes(dumps(m1).encode()),
        manifest_path=MANIFEST_PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=forged, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)

    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert not report.certified
    assert any("manifest_content_digest" in f for f in report.failures), report.failures


def test_a_commit_path_swap_is_detected(conn, tmp_path):
    """Right commit, but the path names a different committed manifest."""

    _seed(conn)
    repo = _repo(tmp_path)
    m1 = _manifest()
    m2 = _manifest(request_budget=77)
    (repo / MANIFEST_PATH).parent.mkdir(parents=True, exist_ok=True)
    (repo / MANIFEST_PATH).write_text(dumps(m1), encoding="utf-8")
    (repo / "pilots/stage_a/other.json").write_text(dumps(m2), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "two"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()

    plan_id = _record_plan_unverified(
        conn, m1, manifest_commit_sha=sha,
        manifest_content_digest=manifest_content_digest_bytes(dumps(m1).encode()),
        manifest_path="pilots/stage_a/other.json")     # points at M2
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert not report.certified


def test_forged_stored_digests_are_detected(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    manifest = _manifest()
    sha = _commit(repo, MANIFEST_PATH, dumps(manifest))
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha=sha,
        manifest_content_digest="f" * 64,              # forged
        manifest_path=MANIFEST_PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert any("manifest_content_digest" in f for f in report.failures)


def test_db_membership_disagreeing_with_the_artefact_is_detected(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    manifest = _manifest()
    # A plan whose DB membership (two targets, from `manifest`) is deliberately
    # richer than the artefact it names (one target).
    other = _manifest(targets=(StageATarget("gm_b1", BUCKET),))
    sha2 = _commit(repo, "pilots/stage_a/short.json", dumps(other))
    forged = _record_plan_unverified(
        conn, manifest,                                   # two targets persisted
        manifest_commit_sha=sha2,
        manifest_content_digest=manifest_content_digest_bytes(dumps(other).encode()),
        manifest_path="pilots/stage_a/short.json")        # artefact has one
    aq = register_acquisition(
        conn, plan_id=forged, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)
    report = certify_stage_a(conn, acquisition_id=aq, repo_root=repo)
    assert not report.certified
    assert any("target population differs" in f for f in report.failures), \
        report.failures


# --------------------------------------------------------------------------- #
# Byte-exact content digest (§18)
# --------------------------------------------------------------------------- #
def test_the_content_digest_is_over_exact_committed_bytes(tmp_path):
    """At `e98363d` the loader used text mode, so CRLF was normalized to LF and
    the digest did NOT fingerprint the committed blob."""

    repo = _repo(tmp_path)
    crlf = b'{"a": 1,\r\n "b": 2}'
    sha = _commit(repo, "m.json", crlf)
    raw = load_committed_bytes(sha, "m.json", repo_root=repo)
    assert raw == crlf
    assert manifest_content_digest_bytes(raw) == hashlib.sha256(crlf).hexdigest()


def test_two_artefacts_differing_only_in_line_endings_have_distinct_digests(tmp_path):
    repo = _repo(tmp_path)
    sha_lf = _commit(repo, "a.json", b'{"x": 1}\n')
    sha_crlf = _commit(repo, "b.json", b'{"x": 1}\r\n')
    a = manifest_content_digest_bytes(load_committed_bytes(sha_lf, "a.json",
                                                           repo_root=repo))
    b = manifest_content_digest_bytes(load_committed_bytes(sha_crlf, "b.json",
                                                           repo_root=repo))
    assert a != b


def test_invalid_utf8_committed_manifest_is_refused(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "bad.json", b"\xff\xfe not utf-8")
    with pytest.raises(PlanBindingError, match="not valid UTF-8"):
        load_committed_stage_a_manifest(sha, "bad.json", repo_root=repo)


# --------------------------------------------------------------------------- #
# Parser strictness (§19) -- stage-a-manifest-v1 is a CLOSED schema
# --------------------------------------------------------------------------- #
def test_an_unknown_top_level_field_is_refused():
    """Otherwise the artefact says something v1 silently drops: the content
    digest changes while the plan digest does not."""

    payload = json.loads(dumps(_manifest()))
    payload["future_magic"] = "believed load-bearing by a future producer"
    with pytest.raises(StageAManifestError, match="unknown top-level field"):
        loads(json.dumps(payload))


def test_an_unknown_target_field_is_refused():
    payload = json.loads(dumps(_manifest()))
    payload["targets"][0]["priority"] = "high"
    with pytest.raises(StageAManifestError, match="unknown field"):
        loads(json.dumps(payload))


def test_a_duplicate_json_key_is_still_refused():
    text = dumps(_manifest())
    with pytest.raises(StageAManifestError, match="duplicate JSON key"):
        loads("{" + '"provider":"evil",' + text[1:])


def test_noncanonical_but_semantically_identical_text_still_binds(tmp_path):
    """Formatting is bound by the content digest; MEANING is bound by the plan
    digest. A pretty-printed artefact is a different file, not a different plan."""

    repo = _repo(tmp_path)
    manifest = _manifest()
    pretty = json.dumps(json.loads(dumps(manifest)), indent=2, sort_keys=True)
    sha = _commit(repo, MANIFEST_PATH, pretty)
    committed = load_committed_stage_a_manifest(sha, MANIFEST_PATH, repo_root=repo)
    assert committed.plan_digest == manifest.plan_digest()
    assert committed.content_digest != manifest_content_digest_bytes(
        dumps(manifest).encode())


# --------------------------------------------------------------------------- #
# Path contract (§10)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "", "   ", "/etc/passwd", "C:/abs/path.json", "../escape.json",
    "pilots/../../escape.json", "pilots//plan.json", "./plan.json",
    "pilots\\stage_a\\plan.json", "plan.json:evil", " plan.json",
])
def test_unsafe_manifest_paths_are_refused(bad):
    with pytest.raises(ProbeBindingError):
        validate_repo_path(bad)


def test_a_safe_nested_path_is_accepted():
    assert validate_repo_path(MANIFEST_PATH) == MANIFEST_PATH


# --------------------------------------------------------------------------- #
# Portability (§17)
# --------------------------------------------------------------------------- #
def test_the_same_artefact_verifies_identically_under_a_different_repo_root(tmp_path):
    manifest = _manifest()
    text = dumps(manifest)
    digests = []
    for name in ("rootA", "rootB"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
        sha = _commit(repo, MANIFEST_PATH, text)
        c = load_committed_stage_a_manifest(sha, MANIFEST_PATH, repo_root=repo)
        digests.append((c.content_digest, c.plan_digest))
    assert digests[0] == digests[1], "local filesystem root leaked into a digest"


# --------------------------------------------------------------------------- #
# The §AF boundary -- deliberately NOT closed by B2
# --------------------------------------------------------------------------- #
def test_a_scientifically_wrong_manifest_still_binds(conn, tmp_path):
    """B2 answers 'did we certify the exact committed artefact?'.

    It does NOT answer 'does that artefact follow the reviewed
    official-hint -> T-60 -> 5-minute-floor algorithm?'. A manifest mapping every
    target to an obviously wrong bucket binds perfectly here and must be caught
    later by §AF, which remains OPEN.
    """

    _seed(conn)
    repo = _repo(tmp_path)
    wrong_bucket = "2029-12-31T23:55:00.000000Z"      # nowhere near T-60
    wrong = _manifest(
        buckets=(wrong_bucket,),
        targets=(StageATarget("gm_b1", wrong_bucket),
                 StageATarget("gm_b2", wrong_bucket)))
    sha = _commit(repo, MANIFEST_PATH, dumps(wrong))
    plan_id = record_committed_plan(
        conn, manifest_commit_sha=sha, manifest_path=MANIFEST_PATH, repo_root=repo)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=10, credit_budget=10)

    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert not any("source control" in f for f in report.failures)
    assert not any("differs from the committed manifest" in f
                   for f in report.failures), report.failures


# --------------------------------------------------------------------------- #
# API shape
# --------------------------------------------------------------------------- #
def test_certification_takes_no_caller_manifest():
    """The B2 trust shape is gone: there is nothing convenient to pass."""

    import inspect

    params = inspect.signature(certify_stage_a).parameters
    assert "manifest" not in params
    assert "manifest_text" not in params


def test_enrichment_takes_no_caller_manifest():
    import inspect

    from sports_quant.retrospective.stage_a_provenance import (
        enrich_corpus_with_market_lane,
    )

    params = inspect.signature(enrich_corpus_with_market_lane).parameters
    assert "manifests" not in params
    assert "manifest_texts" not in params


def test_the_public_module_exposes_no_unverified_plan_writer():
    import sports_quant.retrospective.stage_a_provenance as module

    assert not hasattr(module, "record_plan")
    assert hasattr(module, "_record_plan_unverified")
    assert hasattr(module, "record_committed_plan")


def test_no_provider_authority_is_granted():
    from sports_quant.retrospective import sources
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()
    assert THE_ODDS_API_PROVIDER not in ATTESTED_GENERATIONS
