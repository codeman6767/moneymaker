"""Independent adversarial review of B2 -- plan manifest commit binding.

Every test in the DEFECT sections fails against `40846d0` and passes after the
repairs. Git objects are real but built in self-contained temporary repositories,
so the suite runs at any checkout depth and performs no network I/O.
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
    GitObjectError,
    load_committed_bytes,
)
from sports_quant.retrospective.stage_a_provenance import (
    _record_plan_unverified,
    certify_stage_a,
    record_committed_plan,
    register_acquisition,
)

BUCKET = "2026-03-01T17:00:00.000000Z"
PATH = "plan.json"
ACQ = "stage-a-acquisition-v1"
PROJ = "hme-projection-v1"


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _repo(tmp_path: Path, name: str = "r") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def _commit(repo: Path, path: str, content: bytes | str, msg: str = "c") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _manifest(**over: Any) -> StageAManifest:
    base: dict[str, Any] = dict(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        official_source_corpus_digest="SRC", official_target_set_digest="TGT",
        targets=(StageATarget("gm_r1", BUCKET),), buckets=(BUCKET,),
        decision_horizon_minutes=60, bucket_floor_seconds=300,
        request_budget=5, credit_budget=5, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, cost_policy_version="odds-cost-v1")
    base.update(over)
    return StageAManifest(**base)


# --------------------------------------------------------------------------- #
# DEFECT 1 -- git replacement objects (refs/replace/*)
# --------------------------------------------------------------------------- #
def test_a_replacement_ref_cannot_substitute_a_different_manifest(tmp_path):
    """Reproduced at `40846d0`: `git replace -f C1 C2` made the verifier return
    C2's manifest while reporting it had resolved C1.

    git honours `refs/replace/*` by DEFAULT. A verifier that can be told "when
    you look up this object, use a different one" is not binding an object at
    all -- the whole B2 claim collapses in any repository carrying a replace ref.
    """

    repo = _repo(tmp_path)
    honest = _manifest(request_budget=5)
    swapped = _manifest(request_budget=999, credit_budget=999)
    c1 = _commit(repo, PATH, dumps(honest), "M1")
    c2 = _commit(repo, PATH, dumps(swapped), "M2")
    _git(repo, "replace", "-f", c1, c2)
    assert _git(repo, "replace", "-l").stdout.strip(), "replace ref not installed"

    loaded = load_committed_stage_a_manifest(c1, PATH, repo_root=repo)
    assert loaded.commit_sha == c1
    assert loaded.manifest.request_budget == 5, (
        "a replacement ref substituted a different committed manifest")
    assert loaded.content_digest == manifest_content_digest_bytes(
        dumps(honest).encode())


def test_replacement_refs_do_not_break_ordinary_verification(tmp_path):
    """Disabling replacement must not disturb a repository that uses none."""

    repo = _repo(tmp_path)
    manifest = _manifest()
    sha = _commit(repo, PATH, dumps(manifest))
    loaded = load_committed_stage_a_manifest(sha, PATH, repo_root=repo)
    assert loaded.plan_digest == manifest.plan_digest()


def test_verification_runs_with_lazy_object_fetch_disabled(tmp_path):
    """MISSING LOCALLY -> REFUSE, never 'silently download it'.

    In a partial/promisor clone an object command may fetch a missing object.
    The helper sets GIT_NO_LAZY_FETCH so the contract is enforced rather than
    assumed from the test environment happening to hold every object.
    """

    from sports_quant.retrospective.stage_a_probe_binding import _git_env

    env = _git_env()
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    # And the normal path still works under that environment.
    repo = _repo(tmp_path)
    sha = _commit(repo, PATH, dumps(_manifest()))
    assert load_committed_bytes(sha, PATH, repo_root=repo)


def test_a_missing_object_refuses_without_any_fetch(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "seed.txt", "x")
    with pytest.raises(PlanBindingError, match="not bound to source control"):
        load_committed_stage_a_manifest("d" * 40, PATH, repo_root=repo)


# --------------------------------------------------------------------------- #
# DEFECT 2 -- closed-schema type coercion
# --------------------------------------------------------------------------- #
def _payload(**over: Any) -> str:
    body = json.loads(dumps(_manifest()))
    body.pop("plan_digest", None)
    body.update(over)
    return json.dumps(body)


@pytest.mark.parametrize("field, value, was", [
    ("decision_horizon_minutes", 60.9, "int(60.9) -> 60, truncating the artefact"),
    ("decision_horizon_minutes", "60", "int('60') -> 60"),
    ("decision_horizon_minutes", True, "bool is an int subclass -> 1"),
    ("bucket_floor_seconds", 300.5, "float truncated"),
    ("request_budget", 5.7, "float truncated"),
    ("credit_budget", "5", "numeric string coerced"),
    ("league_id", 12345, "str(12345) -> '12345'"),
    ("provider", True, "str(True) -> 'True'"),
    ("sport_key", None, "str(None) -> the literal string 'None'"),
    ("namespace_generation", 4, "number coerced to text"),
])
def test_wrong_json_types_are_refused_not_coerced(field, value, was):
    """At `40846d0` every one of these was ACCEPTED and silently reinterpreted.

    That is the same defect class as the unknown-field bug B2 itself fixed: the
    committed artefact asserts semantics the frozen parser rewrote, so the
    content digest and the plan digest describe different documents.
    """

    with pytest.raises(StageAManifestError, match="must be a JSON"):
        loads(_payload(**{field: value}))


@pytest.mark.parametrize("value", [12345, True, None, 1.5, [], {}])
def test_wrong_target_field_types_are_refused(value):
    body = json.loads(dumps(_manifest()))
    body.pop("plan_digest", None)
    body["targets"][0]["canonical_game_id"] = value
    with pytest.raises(StageAManifestError, match="must be a JSON string"):
        loads(json.dumps(body))


@pytest.mark.parametrize("value", [12345, True, None])
def test_wrong_bucket_types_are_refused(value):
    with pytest.raises(StageAManifestError, match="must be a non-empty JSON string"):
        loads(_payload(buckets=[value]))


@pytest.mark.parametrize("field", ["league_id", "provider", "sport_key"])
def test_empty_or_padded_strings_are_refused(field):
    with pytest.raises(StageAManifestError, match="non-empty"):
        loads(_payload(**{field: ""}))
    with pytest.raises(StageAManifestError, match="outer whitespace"):
        loads(_payload(**{field: " lg_nba "}))


def test_negative_and_zero_policy_values_are_refused():
    with pytest.raises(StageAManifestError, match=">= 1"):
        loads(_payload(decision_horizon_minutes=0))
    with pytest.raises(StageAManifestError, match=">= 0"):
        loads(_payload(request_budget=-1))


def test_an_integer_beyond_exact_storage_is_refused():
    with pytest.raises(StageAManifestError, match="exactly storable"):
        loads(_payload(request_budget=2 ** 63))


def test_a_missing_required_field_is_refused():
    body = json.loads(dumps(_manifest()))
    body.pop("plan_digest", None)
    body.pop("cost_policy_version")
    with pytest.raises(StageAManifestError, match="missing required field"):
        loads(json.dumps(body))


# --------------------------------------------------------------------------- #
# Non-standard JSON (§21)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_json_constants_are_refused(constant):
    """Python's json module accepts these unless explicitly refused."""

    body = json.loads(dumps(_manifest()))
    body.pop("plan_digest", None)
    text = json.dumps(body)
    tampered = text[:-1] + f', "extra_probe": {constant}}}'
    with pytest.raises(StageAManifestError):
        loads(tampered)


# --------------------------------------------------------------------------- #
# DEFECT 3 -- tree entry type (§6)
# --------------------------------------------------------------------------- #
def test_a_symlink_tree_entry_is_refused(tmp_path):
    """Reproduced at `40846d0`: the helper happily returned the link TARGET
    string as if it were the committed artefact."""

    repo = _repo(tmp_path)
    _commit(repo, "seed.txt", "x")
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=b"../outside/secret.json",
                          capture_output=True, check=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    f"120000,{blob},{PATH}"], capture_output=True, check=True)
    _git(repo, "commit", "-q", "-m", "symlink")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "ls-tree", sha, PATH).stdout.split()[0] == "120000"

    with pytest.raises(GitObjectError, match="not a regular file"):
        load_committed_bytes(sha, PATH, repo_root=repo)


def test_a_gitlink_entry_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "seed.txt", "x")
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    f"160000,{'a' * 40},{PATH}"], capture_output=True, check=True)
    _git(repo, "commit", "-q", "-m", "gitlink")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(GitObjectError, match="not a regular file"):
        load_committed_bytes(sha, PATH, repo_root=repo)


def test_an_executable_regular_file_is_accepted(tmp_path):
    """100755 is still a regular file; only symlinks and gitlinks are refused."""

    repo = _repo(tmp_path)
    text = dumps(_manifest())
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=text.encode(), capture_output=True,
                          check=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    f"100755,{blob},{PATH}"], capture_output=True, check=True)
    _git(repo, "commit", "-q", "-m", "exec")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert load_committed_bytes(sha, PATH, repo_root=repo) == text.encode()


# --------------------------------------------------------------------------- #
# Exact blob bytes, independently cross-checked (§19, §5)
# --------------------------------------------------------------------------- #
def test_the_helper_returns_the_tree_entry_blob(tmp_path):
    """Resolve the blob id independently rather than comparing cat-file to
    itself."""

    repo = _repo(tmp_path)
    text = dumps(_manifest())
    sha = _commit(repo, PATH, text)
    blob_id = _git(repo, "ls-tree", sha, PATH).stdout.split()[2]
    direct = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob_id],
                            capture_output=True, check=True).stdout
    assert load_committed_bytes(sha, PATH, repo_root=repo) == direct


def test_the_helper_returns_the_stored_blob_not_the_checkout(tmp_path):
    """The scientific object is the blob git STORES, which `.gitattributes` can
    make differ from both the working tree and any future checkout.

    Committing CRLF bytes under `*.json text eol=lf` stores an LF blob: the
    normalization happens on the way IN. So the correct assertion is not "the
    blob equals the file I wrote" but "the helper returns exactly the blob the
    tree entry points at", resolved independently -- and that a later
    `core.autocrlf` change cannot move it.
    """

    repo = _repo(tmp_path)
    _commit(repo, ".gitattributes", "*.json text eol=lf\n")
    sha = _commit(repo, "m.json", b'{"x": 1}\r\n')

    blob_id = _git(repo, "ls-tree", sha, "m.json").stdout.split()[2]
    stored = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob_id],
                            capture_output=True, check=True).stdout
    assert stored == b'{"x": 1}\n', "git normalized on commit, as configured"
    assert load_committed_bytes(sha, "m.json", repo_root=repo) == stored

    # Flipping checkout behaviour afterwards must not move the stored object.
    _git(repo, "config", "core.autocrlf", "true")
    assert load_committed_bytes(sha, "m.json", repo_root=repo) == stored


def test_a_blob_committed_without_normalization_keeps_exact_bytes(tmp_path):
    """With no text attribute in play, CRLF survives into the blob and the
    digest fingerprints it exactly."""

    repo = _repo(tmp_path)
    crlf = b'{"x": 1}\r\n'
    sha = _commit(repo, "raw.json", crlf)
    got = load_committed_bytes(sha, "raw.json", repo_root=repo)
    assert got == crlf
    assert manifest_content_digest_bytes(got) == hashlib.sha256(crlf).hexdigest()


def test_the_working_tree_and_index_are_irrelevant(tmp_path):
    repo = _repo(tmp_path)
    m1 = _manifest()
    sha = _commit(repo, PATH, dumps(m1))
    (repo / PATH).write_text(dumps(_manifest(request_budget=999)), encoding="utf-8")
    _git(repo, "add", PATH)                       # staged, not committed
    loaded = load_committed_stage_a_manifest(sha, PATH, repo_root=repo)
    assert loaded.manifest.request_budget == 5


# --------------------------------------------------------------------------- #
# Reachability (§7) -- adjudicated, documented, not silently changed
# --------------------------------------------------------------------------- #
def test_an_unreachable_commit_still_binds_and_that_is_the_contract(tmp_path):
    """ADJUDICATION: B2 requires a real commit OBJECT, not ref reachability.

    Refs are mutable -- a branch can be force-pushed or deleted -- so requiring
    reachability would make verification depend on something less stable than the
    object id it is meant to bind. The honest operational requirement is
    therefore: the commit object must be RETAINED and available locally for
    future certification, which a `gc` of an unreferenced object would break.
    """

    repo = _repo(tmp_path)
    _commit(repo, "seed.txt", "x")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / PATH).write_text(dumps(_manifest()), encoding="utf-8")
    _git(repo, "add", PATH)
    tree = _git(repo, "write-tree").stdout.strip()
    orphan = _git(repo, "commit-tree", tree, "-m", "orphan").stdout.strip()
    _git(repo, "reset", "-q", "--hard", base)

    reachable = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", orphan, "HEAD"],
        capture_output=True).returncode == 0
    assert not reachable
    loaded = load_committed_stage_a_manifest(orphan, PATH, repo_root=repo)
    assert loaded.plan_digest == _manifest().plan_digest()


# --------------------------------------------------------------------------- #
# repo_root boundary (§17) and SHA-1 vs SHA-256 (§8)
# --------------------------------------------------------------------------- #
def test_the_same_commit_in_another_repo_yields_identical_facts(tmp_path):
    """With replacement disabled, git object identity makes repo_root harmless:
    the same commit id can only hold the same tree and blobs."""

    text = dumps(_manifest())
    results = []
    for name in ("one", "two"):
        repo = _repo(tmp_path, name)
        sha = _commit(repo, PATH, text)
        loaded = load_committed_stage_a_manifest(sha, PATH, repo_root=repo)
        results.append((loaded.content_digest, loaded.plan_digest))
    assert results[0] == results[1]


def test_the_sha256_content_digest_is_independent_of_the_git_sha1(tmp_path):
    """A git SHA-1 collision alone would still have to match the SHA-256 content
    digest and the semantic plan digest recorded on the plan row."""

    repo = _repo(tmp_path)
    text = dumps(_manifest())
    sha = _commit(repo, PATH, text)
    loaded = load_committed_stage_a_manifest(sha, PATH, repo_root=repo)
    assert loaded.content_digest == hashlib.sha256(text.encode()).hexdigest()
    assert loaded.content_digest != sha


# --------------------------------------------------------------------------- #
# _record_plan_unverified reachability (§14) and direct SQL (§15)
# --------------------------------------------------------------------------- #
def _seed(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    if conn.execute("SELECT 1 FROM seasons WHERE season_id='sn_rb2'").fetchone():
        return
    if conn.execute("SELECT 1 FROM leagues WHERE league_id='lg_nba'").fetchone() is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date,"
        " end_date, created_at, updated_at) VALUES ('sn_rb2','lg_nba',2025,'2025-26',"
        " 'regular','2025-10-01','2026-06-30',?,?)", (now, now))
    for tid, name, abbr in (("tm_rb1", "RB2 A", "RA2"), ("tm_rb2", "RB2 B", "RB3")):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname,"
            " abbreviation, created_at, updated_at) VALUES (?, 'lg_nba', ?,?,?,?,?,?)",
            (tid, name, name, name, abbr, now, now))
    conn.execute(
        "INSERT INTO games (game_id, league_id, season_id, home_team_id,"
        " away_team_id, scheduled_start, original_start, game_date_local, status,"
        " created_at, updated_at) VALUES ('gm_r1','lg_nba','sn_rb2','tm_rb1',"
        " 'tm_rb2','2026-03-01T18:10:00Z','2026-03-01T18:10:00Z','2026-03-01',"
        " 'final', ?, ?)", (now, now))
    conn.commit()


def test_an_unverified_plan_row_cannot_certify(conn, tmp_path):
    """The safety claim is NOT 'nobody can call the private writer'.

    It is that even a row written directly is refused, because certification
    independently resolves the artefact the row names.
    """

    _seed(conn)
    manifest = _manifest()
    plan_id = _record_plan_unverified(
        conn, manifest, manifest_commit_sha="e" * 40,
        manifest_content_digest=manifest_content_digest_bytes(dumps(manifest).encode()),
        manifest_path=PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=5, credit_budget=5)
    report = certify_stage_a(conn, acquisition_id=acquisition_id,
                             repo_root=_repo(tmp_path))
    assert not report.certified
    assert any("not bound to source control" in f for f in report.failures)


def test_a_fully_forged_direct_sql_plan_cannot_certify(conn, tmp_path):
    """No git hash can be computed by SQLite, so the gate is the only control."""

    _seed(conn)
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO stage_a_plans (plan_id, plan_digest, manifest_commit_sha,"
        " manifest_content_digest, manifest_path, manifest_format_version,"
        " plan_policy_version, league_id, provider, namespace_generation,"
        " sport_key, official_source_corpus_digest, official_target_set_digest,"
        " decision_horizon_minutes, bucket_floor_seconds,"
        " acquisition_policy_version, projection_policy_version,"
        " cost_policy_version, created_at) VALUES ('sap_forged','FORGED',?,"
        " 'FORGEDDIGEST', ?, 'stage-a-manifest-v1','stage-a-plan-v1','lg_nba',"
        " ?, 'v4','basketball_nba','SRC','TGT',60,300,?,?,'odds-cost-v1',?)",
        ("f" * 40, PATH, THE_ODDS_API_PROVIDER, ACQ, PROJ, now))
    conn.execute(
        "INSERT INTO stage_a_planned_buckets (plan_id, requested_at_bucket,"
        " created_at) VALUES ('sap_forged', ?, ?)", (BUCKET, now))
    conn.execute(
        "INSERT INTO stage_a_plan_targets (plan_id, canonical_game_id,"
        " requested_at_bucket, created_at) VALUES ('sap_forged','gm_r1',?,?)",
        (BUCKET, now))
    conn.commit()
    acquisition_id = register_acquisition(
        conn, plan_id="sap_forged", acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=5, credit_budget=5)
    report = certify_stage_a(conn, acquisition_id=acquisition_id,
                             repo_root=_repo(tmp_path))
    assert not report.certified


def test_a_real_commit_with_forged_semantic_fields_is_detected(conn, tmp_path):
    _seed(conn)
    repo = _repo(tmp_path)
    manifest = _manifest()
    sha = _commit(repo, PATH, dumps(manifest))
    plan_id = _record_plan_unverified(
        conn, _manifest(request_budget=999),          # persisted semantics differ
        manifest_commit_sha=sha,
        manifest_content_digest=manifest_content_digest_bytes(dumps(manifest).encode()),
        manifest_path=PATH)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=5, credit_budget=5)
    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert not report.certified
    assert any("plan_digest" in f for f in report.failures), report.failures


# --------------------------------------------------------------------------- #
# §AF separation must survive (§26)
# --------------------------------------------------------------------------- #
def test_b2_still_accepts_a_scientifically_wrong_manifest(conn, tmp_path):
    """B2 = WHICH committed plan. §AF = is that plan algorithmically correct.

    This must keep passing: if a later change makes B2 reject an absurd mapping,
    §AF's contract has been silently absorbed and the separation is lost.
    """

    _seed(conn)
    repo = _repo(tmp_path)
    absurd = "2029-12-31T23:55:00.000000Z"
    wrong = _manifest(buckets=(absurd,),
                      targets=(StageATarget("gm_r1", absurd),))
    sha = _commit(repo, PATH, dumps(wrong))
    plan_id = record_committed_plan(
        conn, manifest_commit_sha=sha, manifest_path=PATH, repo_root=repo)
    acquisition_id = register_acquisition(
        conn, plan_id=plan_id, acquisition_policy_version=ACQ,
        projection_policy_version=PROJ, request_budget=5, credit_budget=5)
    report = certify_stage_a(conn, acquisition_id=acquisition_id, repo_root=repo)
    assert not any("source control" in f for f in report.failures)
    assert not any("differs from the committed manifest" in f
                   for f in report.failures), report.failures


# --------------------------------------------------------------------------- #
# v1 freezing precondition (§22)
# --------------------------------------------------------------------------- #
def test_no_real_stage_a_plan_has_been_declared(conn):
    """The precondition that made tightening v1 safe. Once a real v1 plan is
    declared, semantic changes to v1 are forbidden and require v2."""

    assert conn.execute("SELECT COUNT(*) FROM stage_a_plans").fetchone()[0] == 0
    assert not (Path(__file__).resolve().parents[3] / "pilots" / "stage_a").exists()


def test_no_provider_authority_is_granted():
    from sports_quant.retrospective import sources
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()
    assert THE_ODDS_API_PROVIDER not in ATTESTED_GENERATIONS
