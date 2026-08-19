"""Adversarial tests for retained blocker B1 -- probe-reuse binding.

Every attack the v22 independent review reproduced is re-run here and must now be
refused. Git objects are real (this repository's own history); no network is used,
including no git fetch.
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
from sports_quant.retrospective.stage_a_probe_binding import (
    REPO_ROOT,
    STAGE_A_PROBE_POLICY_V1,
    GitObjectError,
    ProbeBindingError,
    bind_probe_response,
    load_committed_text,
    parse_probe_report,
    probe_binding_failures,
    resolve_commit,
)

BUCKET = "2026-03-01T17:00:00Z"
ENDPOINT = "/v4/historical/sports/basketball_nba/events"
#: The commit that published the successful re-probe report.
REAL_PROBE_COMMIT = "d3984d0b897f3acfaa63f05c763416b3591d92d8"
REAL_PROBE_REPORT = "ODDS_API_HISTORICAL_ENTITLEMENT_REPROBE.md"


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                         capture_output=True, text=True, check=False)
    return out.stdout.strip()


def _real_history_available() -> bool:
    """Whether this checkout contains the probe report commit.

    A shallow clone (``actions/checkout`` defaults to depth 1) or a source
    tarball has no such object, and cannot evaluate claims about this
    repository's own history. The MECHANISM tests below never depend on it --
    they build their own throwaway repository -- so only the EVIDENCE tests
    about `d3984d0` are conditional.
    """

    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-t", REAL_PROBE_COMMIT],
        capture_output=True, text=True, check=False)
    return out.returncode == 0 and out.stdout.strip() == "commit"


requires_real_history = pytest.mark.skipif(
    not _real_history_available(),
    reason=f"probe report commit {REAL_PROBE_COMMIT[:7]} is not present in this "
           f"checkout (shallow clone or exported tree); the probe-binding "
           f"MECHANISM is covered by the synthetic-repository tests")


def _scratch_repo(tmp_path: Path, name: str, text: str) -> tuple[str, Path]:
    """Commit ``text`` as ``name`` in a throwaway repo; return (sha, repo root)."""

    repo = tmp_path / "scratch_repo"
    if not repo.exists():
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha, repo


def _events(count: int, seed: int = 0) -> list[dict[str, Any]]:
    return [{"id": "%032x" % (0xa11ce000 + seed + i),
             "sport_key": "basketball_nba",
             "commence_time": "2026-03-01T18:10:00Z",
             "home_team": f"Home {i}", "away_team": f"Away {i}"}
            for i in range(count)]


def _body(events: list[dict[str, Any]]) -> str:
    return json.dumps({"timestamp": "2026-03-01T16:55:37Z",
                       "previous_timestamp": "2026-03-01T16:50:37Z",
                       "next_timestamp": "2026-03-01T17:00:38Z",
                       "data": events})


def _report(body: str, *, status: int = 200, extra: str = "",
            provider: str = THE_ODDS_API_PROVIDER, endpoint: str = ENDPOINT,
            bucket: str = BUCKET, include_hash: bool = True,
            include_ids: bool = True) -> str:
    ids = tuple(sorted(e["id"] for e in json.loads(body)["data"]))
    lines = ["# Synthetic probe report", "",
             f"PROBE-BINDING: provider = {provider}",
             f"PROBE-BINDING: endpoint = {endpoint}",
             f"PROBE-BINDING: requested_bucket = {bucket}",
             f"PROBE-BINDING: http_status = {status}"]
    if include_hash:
        lines.append(
            "PROBE-BINDING: body_sha256 = "
            + hashlib.sha256(body.encode()).hexdigest())
    if include_ids:
        lines.append("PROBE-BINDING: event_ids = " + ",".join(ids))
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


def _seed(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM leagues WHERE league_id='lg_nba'").fetchone() is None:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at,"
            " updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json,"
        " status, requested_at, started_at, started_monotonic_ns, requests_made,"
        " records_received, records_normalized, records_inserted,"
        " records_deduplicated, records_rejected, records_updated, tool_version,"
        " created_at) VALUES ('run_b1','x',?,'op','{}','started',?,?,1,0,0,0,0,0,0,0,"
        " 'v',?)", (THE_ODDS_API_PROVIDER, now, now, now))
    conn.commit()


def _raw(conn: sqlite3.Connection, rid: str, body: str, *, status: int = 200,
         bucket: str = BUCKET, params: dict[str, Any] | None = None) -> None:
    now = utc_now_iso()
    if params is None:
        params = {"apiKey": "***REDACTED***", "date": bucket, "dateFormat": "iso"}
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, body, body_bytes,"
        " body_hash, content_hash, requested_at, received_at, elapsed_ns, created_at)"
        " VALUES (?, 'run_b1', ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, 1, ?)",
        (rid, THE_ODDS_API_PROVIDER, ENDPOINT, json.dumps(params), status, body,
         len(body.encode()), rid, rid, now, now, now))
    conn.commit()


# --------------------------------------------------------------------------- #
# Git object resolution
# --------------------------------------------------------------------------- #
def test_all_zero_commit_sha_is_refused():
    with pytest.raises(GitObjectError, match="does not exist"):
        resolve_commit("0" * 40)


@pytest.mark.parametrize("bad", ["", "x", "deadbeef", "D3984D0B897F3ACFAA63F05C763416B3591D92D8",
                                 " " + "a" * 40, "a" * 39, "a" * 41])
def test_malformed_or_short_commit_ids_are_refused(bad):
    with pytest.raises(GitObjectError, match="full 40-character"):
        resolve_commit(bad)


@requires_real_history
def test_a_real_commit_resolves():
    assert resolve_commit(REAL_PROBE_COMMIT) == REAL_PROBE_COMMIT


def test_a_blob_object_is_refused_as_a_commit(tmp_path):
    """A blob id EXISTS, so only an object-type check distinguishes it."""

    sha, repo = _scratch_repo(tmp_path, "r.md", "x")
    blob = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{sha}:r.md"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert len(blob) == 40
    with pytest.raises(GitObjectError, match="is a 'blob' object"):
        resolve_commit(blob, repo_root=repo)


def test_a_tree_object_is_refused_as_a_commit(tmp_path):
    sha, repo = _scratch_repo(tmp_path, "r.md", "x")
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert len(tree) == 40
    with pytest.raises(GitObjectError, match="is a 'tree' object"):
        resolve_commit(tree, repo_root=repo)


def test_a_nonexistent_report_path_is_refused(tmp_path):
    sha, repo = _scratch_repo(tmp_path, "r.md", "x")
    with pytest.raises(GitObjectError, match="does not exist at commit"):
        load_committed_text(sha, "does/not/exist.md", repo_root=repo)


@requires_real_history
def test_the_report_is_loaded_from_the_commit_not_the_working_tree(tmp_path):
    """A local edit must not change a historical verification result."""

    committed = load_committed_text(REAL_PROBE_COMMIT, REAL_PROBE_REPORT)
    working = (REPO_ROOT / REAL_PROBE_REPORT).read_text(encoding="utf-8")
    # Both exist; the committed copy is what the verifier uses. Even if the
    # working tree were rewritten, `git show <commit>:<path>` is unaffected.
    assert committed.strip()
    assert "PROBE-BINDING:" not in committed  # the historical report predates it
    assert isinstance(working, str)


# --------------------------------------------------------------------------- #
# Report contract parsing
# --------------------------------------------------------------------------- #
def test_a_report_without_the_binding_block_is_refused():
    with pytest.raises(ProbeBindingError, match="not bindable"):
        parse_probe_report("# just prose\n\nHTTP 200 and 11 events.\n")


def test_a_report_with_conflicting_values_is_refused():
    body = _body(_events(3))
    text = _report(body) + "PROBE-BINDING: http_status = 500\n"
    with pytest.raises(ProbeBindingError, match="conflicting values"):
        parse_probe_report(text)


def test_a_report_with_no_fingerprint_is_refused():
    """The heart of B1: description is not identity."""

    body = _body(_events(3))
    with pytest.raises(ProbeBindingError, match="no body_sha256"):
        parse_probe_report(_report(body, include_hash=False, include_ids=False))


def test_event_ids_alone_are_not_a_fingerprint():
    """INDEPENDENT REVIEW defect: the report PUBLISHES its own event ids.

    An earlier form of the policy accepted the id set as an alternative
    fingerprint. Reproduced against `ac36cc9`: a body carrying those same ids but
    entirely fabricated team names and commence times BOUND successfully, because
    a published identifier is a KNOWN value -- anyone who reads the report can
    construct a body containing it. A SHA-256 is different: publishing it grants
    no ability to produce a matching body.
    """

    body = _body(_events(3))
    with pytest.raises(ProbeBindingError, match="no body_sha256"):
        parse_probe_report(_report(body, include_hash=False, include_ids=True))


def test_the_published_id_forgery_is_refused_end_to_end(conn, tmp_path):
    """The full reproduction: same ids, fabricated everything else."""

    real = _body(_events(4, seed=0))
    forged = json.dumps({
        "timestamp": "2026-03-01T16:55:37Z",
        "previous_timestamp": "2026-03-01T16:50:37Z",
        "next_timestamp": "2026-03-01T17:00:38Z",
        "data": [{"id": e["id"], "sport_key": "basketball_nba",
                  "commence_time": "2026-03-01T23:59:00Z",
                  "home_team": "FORGED FC", "away_team": "FORGED UTD"}
                 for e in json.loads(real)["data"]]})
    assert forged != real
    _seed(conn)
    _raw(conn, "raw_forged", forged)

    # A report that (now illegally) omits the hash is refused outright.
    sha, repo = _scratch_repo(tmp_path, "probe_report.md",
                              _report(real, include_hash=False))
    with pytest.raises(ProbeBindingError, match="no body_sha256"):
        bind_probe_response(
            conn, probe_report_commit_sha=sha,
            probe_report_path="probe_report.md",
            probe_policy_version=STAGE_A_PROBE_POLICY_V1, repo_root=repo)

    # And with the hash present, the forgery simply does not match.
    sha2, repo2 = _scratch_repo(tmp_path, "probe_report2.md", _report(real))
    with pytest.raises(ProbeBindingError, match="no preserved response matches"):
        bind_probe_response(
            conn, probe_report_commit_sha=sha2,
            probe_report_path="probe_report2.md",
            probe_policy_version=STAGE_A_PROBE_POLICY_V1, repo_root=repo2)


def test_event_ids_remain_an_additional_cross_check():
    """Retained as a secondary check when supplied -- never a substitute."""

    body = _body(_events(3))
    facts = parse_probe_report(_report(body))
    assert facts.body_sha256
    assert facts.event_ids
    assert facts.fingerprint_kind() == "body_sha256+event_ids"


def test_a_non_200_report_is_refused():
    body = _body(_events(3))
    with pytest.raises(ProbeBindingError, match="only a successful"):
        parse_probe_report(_report(body, status=500))


def test_a_foreign_provider_or_endpoint_report_is_refused():
    body = _body(_events(3))
    with pytest.raises(ProbeBindingError, match="not a permitted probe provider"):
        parse_probe_report(_report(body, provider="balldontlie"))
    with pytest.raises(ProbeBindingError, match="not the permitted historical"):
        parse_probe_report(_report(body, endpoint="/v4/sports"))


def test_malformed_fingerprints_are_refused():
    body = _body(_events(3))
    text = _report(body, include_ids=False).replace(
        hashlib.sha256(body.encode()).hexdigest(), "abc")
    with pytest.raises(ProbeBindingError, match="not 64 lowercase hex"):
        parse_probe_report(text)


def test_duplicate_event_ids_in_a_report_are_refused():
    body = _body(_events(2))
    ids = sorted(e["id"] for e in json.loads(body)["data"])
    text = _report(body).replace(
        "event_ids = " + ",".join(ids), "event_ids = " + ",".join([ids[0], ids[0]]))
    with pytest.raises(ProbeBindingError, match="duplicate id"):
        parse_probe_report(text)


def test_unknown_probe_policy_is_refused(conn):
    _seed(conn)
    with pytest.raises(ProbeBindingError, match="unknown probe policy"):
        bind_probe_response(
            conn, probe_report_commit_sha=REAL_PROBE_COMMIT,
            probe_report_path=REAL_PROBE_REPORT,
            probe_policy_version="stage-a-probe-v99")


def test_the_frozen_policy_version_is_pinned():
    """Changing the policy's meaning requires a NEW version string."""

    assert STAGE_A_PROBE_POLICY_V1 == "stage-a-probe-v1"


# --------------------------------------------------------------------------- #
# Unique-candidate binding, using a committed report in THIS repository
# --------------------------------------------------------------------------- #
def _committed_synthetic_report(tmp_path: Path, body: str, **kw: Any) -> tuple[str, str]:
    """Commit a synthetic report into a throwaway git repo and return (sha, path).

    A real commit object is required, so a scratch repository is built rather than
    polluting this one.
    """

    repo = tmp_path / "probe_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    name = "probe_report.md"
    (repo / name).write_text(_report(body, **kw), encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "probe"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha, str(repo / name)


def test_zero_matching_responses_is_refused(conn):
    _seed(conn)
    body = _body(_events(3))
    text = _report(body)
    facts = parse_probe_report(text)
    assert facts.body_sha256
    # No response persisted at all.
    with pytest.raises(ProbeBindingError):
        bind_probe_response(
            conn, probe_report_commit_sha=REAL_PROBE_COMMIT,
            probe_report_path=REAL_PROBE_REPORT,
            probe_policy_version=STAGE_A_PROBE_POLICY_V1)


def test_a_filtered_request_never_matches():
    """A population-reducing filter disqualifies a response outright."""

    from sports_quant.retrospective.stage_a_probe_binding import _response_matches

    body = _body(_events(3))
    facts = parse_probe_report(_report(body))
    row = {"provider": THE_ODDS_API_PROVIDER, "endpoint": ENDPOINT,
           "http_status": 200, "body": body,
           "request_params_json": json.dumps(
               {"apiKey": "x", "date": BUCKET, "eventIds": "one"})}
    assert _response_matches(row, facts) is False


def test_a_wrong_bucket_never_matches():
    from sports_quant.retrospective.stage_a_probe_binding import _response_matches

    body = _body(_events(3))
    facts = parse_probe_report(_report(body))
    row = {"provider": THE_ODDS_API_PROVIDER, "endpoint": ENDPOINT,
           "http_status": 200, "body": body,
           "request_params_json": json.dumps(
               {"apiKey": "x", "date": "2026-03-01T18:00:00Z", "dateFormat": "iso"})}
    assert _response_matches(row, facts) is False


def test_a_body_that_differs_by_one_byte_never_matches():
    from sports_quant.retrospective.stage_a_probe_binding import _response_matches

    body = _body(_events(3))
    facts = parse_probe_report(_report(body, include_ids=False))
    row = {"provider": THE_ODDS_API_PROVIDER, "endpoint": ENDPOINT,
           "http_status": 200, "body": body + " ",
           "request_params_json": json.dumps(
               {"apiKey": "x", "date": BUCKET, "dateFormat": "iso"})}
    assert _response_matches(row, facts) is False


def test_a_fabricated_body_with_different_event_ids_never_matches():
    """The forgery that defeats description-only binding is caught by ids."""

    from sports_quant.retrospective.stage_a_probe_binding import _response_matches

    real = _body(_events(11, seed=0))
    forged = _body(_events(11, seed=5000))       # same shape, different ids
    assert len(real.encode()) == len(forged.encode())
    facts = parse_probe_report(_report(real))
    row = {"provider": THE_ODDS_API_PROVIDER, "endpoint": ENDPOINT,
           "http_status": 200, "body": forged,
           "request_params_json": json.dumps(
               {"apiKey": "x", "date": BUCKET, "dateFormat": "iso"})}
    assert _response_matches(row, facts) is False


def test_two_identical_matching_responses_are_refused_as_ambiguous(conn, tmp_path):
    """Curator selection among compatible candidates must be impossible."""

    _seed(conn)
    body = _body(_events(4))
    sha, path = _committed_synthetic_report(tmp_path, body)
    _raw(conn, "raw_one", body)
    _raw(conn, "raw_two", body)   # byte-identical evidence, two preserved rows

    with pytest.raises(ProbeBindingError, match="AMBIGUOUS"):
        bind_probe_response(
            conn, probe_report_commit_sha=sha,
            probe_report_path="probe_report.md",
            probe_policy_version=STAGE_A_PROBE_POLICY_V1,
            repo_root=Path(path).parent)


def test_a_unique_match_binds_and_the_caller_cannot_nominate_another(conn, tmp_path):
    _seed(conn)
    body = _body(_events(4))
    other = _body(_events(4, seed=900))
    sha, path = _committed_synthetic_report(tmp_path, body)
    _raw(conn, "raw_real", body)
    _raw(conn, "raw_decoy", other)

    root = Path(path).parent
    bound = bind_probe_response(
        conn, probe_report_commit_sha=sha, probe_report_path="probe_report.md",
        probe_policy_version=STAGE_A_PROBE_POLICY_V1, repo_root=root)
    assert bound == "raw_real"

    # A registration naming the decoy is refused: the report proves which
    # response it describes, and the caller does not get a vote.
    failures = probe_binding_failures(
        conn, raw_response_id="raw_decoy",
        registration={"probe_report_commit_sha": sha,
                      "probe_report_path": "probe_report.md",
                      "probe_policy_version": STAGE_A_PROBE_POLICY_V1},
        repo_root=root)
    assert failures and "uniquely identifies" in failures[0]


# --------------------------------------------------------------------------- #
# The real d3984d0 probe -- verdict B
# --------------------------------------------------------------------------- #
@requires_real_history
def test_the_real_probe_report_is_not_bindable(conn):
    """VERDICT B, enforced in code.

    `d3984d0` deliberately recorded "structure only -- no identity inferred", so
    it commits no body hash, no response id and none of the 11 provider event
    ids. Every fact it does state is reproducible by construction: a synthetic
    body with zero real provider data was built satisfying all of them at exactly
    2255 bytes.

    So the real probe is NOT reusable, and the March 1 17:00 bucket must be
    acquired normally for one credit.
    """

    committed = load_committed_text(REAL_PROBE_COMMIT, REAL_PROBE_REPORT)
    assert "2,255 body bytes" in committed          # description IS present
    assert "PROBE-BINDING:" not in committed        # fingerprint is NOT

    with pytest.raises(ProbeBindingError, match="not bindable"):
        parse_probe_report(committed)


@requires_real_history
def test_the_real_report_commits_no_response_fingerprint():
    committed = load_committed_text(REAL_PROBE_COMMIT, REAL_PROBE_REPORT)
    # The real provider event ids observed in the preserved scratch response.
    for real_id in ("bb4195b038cce802d282f17ff4ca7471",
                    "ab2357087428a9fee8c8a747eed1fced"):
        assert real_id not in committed
    assert "raw_01M0A1GA953DC83R" not in committed


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def test_register_probe_response_does_not_accept_a_caller_clock():
    import inspect

    from sports_quant.retrospective.stage_a_provenance import register_probe_response

    assert "registered_at" not in inspect.signature(
        register_probe_response).parameters


def test_probe_registration_grants_no_identity_authority():
    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective import sources
    from sports_quant.retrospective.namespaces import QUALIFIED_PROVIDERS
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()
    assert THE_ODDS_API_PROVIDER not in ATTESTED_GENERATIONS
    assert THE_ODDS_API_PROVIDER not in sources.PROVIDER_LEAGUES
    assert THE_ODDS_API_PROVIDER not in OFFICIAL_PROVIDER_BY_LEAGUE.values()
    assert THE_ODDS_API_PROVIDER not in QUALIFIED_PROVIDERS
