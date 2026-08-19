"""Machine-verifiable Stage-A probe-reuse binding (retained blocker B1).

The threat this closes
----------------------
The v22 independent review reproduced the following, end to end:

    an arbitrary 2020 raw response
    + an all-zero commit SHA
    + a nonexistent report path
    -> stage_a_probe_registrations row
    -> REUSED_PROBE_RESPONSE
    -> Stage-A certification PASSED

`REUSED_PROBE_RESPONSE` is a deliberate exception to plan-before-network, so it
must be impossible to generalize. A registration row proved nothing: nothing
resolved the commit, loaded the report, or tied the report to a response.

Why a committed FINGERPRINT is required, not a committed description
-------------------------------------------------------------------
The obvious design is to extract the facts a probe report already states
(bucket, endpoint, HTTP status, snapshot instant, event count, body length) and
require a candidate response to match them all. That is **not sufficient**, and
this module refuses to pretend otherwise.

Measured against the real committed report at `d3984d0`: a synthetic body
containing **zero** real provider data was constructed that satisfies every fact
that report precommits -- exactly 2255 bytes, snapshot `16:55:37Z`, previous
`16:50:37Z`, next `17:00:38Z`, 11 events, all lowercase 32-hex ids, all
`basketball_nba` -- with a completely different SHA-256 and none of the real
provider event ids.

Every one of those facts is reproducible BY CONSTRUCTION. They form a
SPECIFICATION, not a preimage-resistant fingerprint. Binding on them would let a
curator fabricate a response that passes, which is the same class of failure the
review reported, merely harder to notice.

So the frozen policy requires the report to precommit at least one value a forger
cannot produce without the provider's actual answer:

* the exact SHA-256 of the preserved response body, and/or
* the exact set of provider-assigned event ids (opaque 32-hex values).

A report lacking both is REFUSED as non-bindable. That is a fail-closed outcome,
not a defect: the correct remedy is to request that bucket again as an ordinary
Stage-A acquisition for one credit. Saving a credit is not worth accepting
evidence whose identity was never committed.

Consequence for the existing probe: the `d3984d0` report deliberately recorded
"structure only -- no identity inferred", so it commits no body hash, no response
id and none of the 11 event ids. **It is therefore NOT reusable under this
policy.** See `STAGE_A_PROBE_REUSE_BINDING_IMPLEMENTATION.md`.

Trust boundary
--------------
Resolving a real commit object and loading the report from that commit is
tamper-EVIDENCE, not proof of chronology: git commit dates are attacker-settable
and a local repository can be rewritten. What this module guarantees is that the
report is a real committed artefact at a named commit, that it precommits a
fingerprint no forger can satisfy, and that exactly one preserved response
matches it.

This module performs no network I/O -- including no git fetch.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional, Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The frozen probe-reuse policy. Changing any rule below requires a NEW version
#: string; the mapping from version -> semantics is never mutated in place.
STAGE_A_PROBE_POLICY_V1: Final = "stage-a-probe-v1"

#: Providers whose probes may be reused at all.
ALLOWED_PROBE_PROVIDERS: Final[frozenset[str]] = frozenset({"the_odds_api"})

#: The only endpoint a reusable probe may come from.
ALLOWED_PROBE_ENDPOINTS: Final[frozenset[str]] = frozenset(
    {"/v4/historical/sports/basketball_nba/events"})

#: Request parameters a reusable probe may carry. Anything else is a
#: population-reducing filter and disqualifies the response.
ALLOWED_PROBE_REQUEST_PARAMS: Final[frozenset[str]] = frozenset(
    {"apiKey", "date", "dateFormat"})

#: Full 40-hex object ids only. A short id can be ambiguous, and an ambiguous
#: identity is not an identity.
_FULL_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"\A[0-9a-f]{32}\Z")

#: Report field markers. A narrow, frozen contract for a known report shape is
#: deliberately preferred over a permissive Markdown parser: a general parser
#: would have to guess which of several candidate values in prose is normative,
#: and guessing is exactly how "HTTP 200 here, HTTP 500 there" gets resolved
#: conveniently.
_FIELD_PREFIX: Final = "PROBE-BINDING:"


class ProbeBindingError(RuntimeError):
    """A probe cannot be bound to preserved evidence. Always fails closed."""


class GitObjectError(ProbeBindingError):
    """A named git object is missing, ambiguous, or the wrong object type."""


# --------------------------------------------------------------------------- #
# Git object resolution (probe reports only -- B2 remains open)
# --------------------------------------------------------------------------- #
def _git(*args: str, repo_root: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    """Run a read-only local git command. No network, ever.

    ``--no-optional-locks`` keeps this from touching the index, so verification
    can never mutate the working repository. ``repo_root`` is injectable so tests
    can point at a throwaway repository without mutating module state.
    """

    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo_root or REPO_ROOT), *args],
        capture_output=True, text=True, check=False)


def resolve_commit(commit_sha: str, *, repo_root: Optional[Path] = None) -> str:
    """Resolve ``commit_sha`` to a full commit object id, or refuse.

    Refuses: a non-hex or short id, a missing object, and any object that is not
    a commit -- a blob or tree id would otherwise "exist" and read as valid.
    An annotated tag is refused rather than silently peeled, because peeling is a
    policy decision and this policy does not permit it.
    """

    if not isinstance(commit_sha, str) or not _FULL_SHA1.match(commit_sha):
        raise GitObjectError(
            f"probe report commit id {commit_sha!r} is not a full 40-character "
            f"lowercase hex object id; a short or malformed id can be ambiguous")

    kind = _git("cat-file", "-t", commit_sha, repo_root=repo_root)
    if kind.returncode != 0:
        raise GitObjectError(
            f"probe report commit {commit_sha!r} does not exist in this "
            f"repository; refusing (no network fetch is permitted)")
    object_type = kind.stdout.strip()
    if object_type != "commit":
        raise GitObjectError(
            f"probe report commit {commit_sha!r} is a {object_type!r} object, not "
            f"a commit; a blob or tree id is not a source-control provenance point")
    return commit_sha


def load_committed_text(commit_sha: str, path: str, *,
                        repo_root: Optional[Path] = None) -> str:
    """Load ``path`` exactly as committed at ``commit_sha``.

    The working-tree copy is never consulted: a probe report that has since been
    edited locally must not be able to change a historical verification result.
    """

    resolved = resolve_commit(commit_sha, repo_root=repo_root)
    blob = _git("show", f"{resolved}:{path}", repo_root=repo_root)
    if blob.returncode != 0:
        raise GitObjectError(
            f"path {path!r} does not exist at commit {resolved!r}; "
            f"{blob.stderr.strip()[:120]}")
    return blob.stdout


# --------------------------------------------------------------------------- #
# Committed report parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProbeReportFacts:
    """The binding facts a probe report must precommit."""

    provider: str
    endpoint: str
    requested_bucket: str
    http_status: int
    #: At least one of these two must be present -- see the module docstring.
    body_sha256: Optional[str]
    event_ids: tuple[str, ...]

    def fingerprint_kind(self) -> str:
        if self.body_sha256 and self.event_ids:
            return "body_sha256+event_ids"
        return "body_sha256" if self.body_sha256 else "event_ids"


def _single_value(text: str, field: str) -> Optional[str]:
    """Extract exactly one declared value for ``field``, or refuse.

    Two declarations of the same field is a contradiction, not a choice: a report
    saying HTTP 200 in one place and HTTP 500 in another must never resolve to
    whichever is convenient.
    """

    pattern = re.compile(
        rf"^{re.escape(_FIELD_PREFIX)}\s*{re.escape(field)}\s*=\s*(\S+)\s*$",
        re.MULTILINE)
    found = pattern.findall(text)
    if not found:
        return None
    unique = set(found)
    if len(unique) > 1:
        raise ProbeBindingError(
            f"probe report declares conflicting values for {field!r}: "
            f"{sorted(unique)}; refusing rather than selecting one")
    return found[0]


def parse_probe_report(text: str) -> ProbeReportFacts:
    """Parse a committed probe report under the frozen v1 contract.

    The contract is an explicit machine-readable block, NOT prose scraping. A
    report that predates the contract simply has no such block and is refused as
    non-bindable -- which is the honest outcome, because prose facts alone are
    forgeable (module docstring).
    """

    if not isinstance(text, str) or not text.strip():
        raise ProbeBindingError("probe report is empty")

    provider = _single_value(text, "provider")
    endpoint = _single_value(text, "endpoint")
    bucket = _single_value(text, "requested_bucket")
    status = _single_value(text, "http_status")
    body_sha = _single_value(text, "body_sha256")
    ids_raw = _single_value(text, "event_ids")

    missing = [name for name, value in (
        ("provider", provider), ("endpoint", endpoint),
        ("requested_bucket", bucket), ("http_status", status)) if value is None]
    if missing:
        raise ProbeBindingError(
            f"probe report is not bindable: it does not declare a "
            f"{_FIELD_PREFIX} block with {missing}. A probe report written before "
            f"this contract cannot be bound, because the prose facts it does "
            f"state are reproducible by construction and therefore forgeable.")

    if body_sha is None and ids_raw is None:
        raise ProbeBindingError(
            "probe report declares no preimage-resistant response fingerprint "
            "(neither body_sha256 nor event_ids). Every other fact a probe report "
            "states can be satisfied by a fabricated body, so binding on them "
            "alone would admit forged evidence. Refusing.")

    if body_sha is not None and not _SHA256.match(body_sha):
        raise ProbeBindingError(
            f"probe report body_sha256 {body_sha!r} is not 64 lowercase hex chars")

    event_ids: tuple[str, ...] = ()
    if ids_raw is not None:
        parts = [p for p in ids_raw.split(",") if p]
        if not parts:
            raise ProbeBindingError("probe report event_ids is empty")
        if len(set(parts)) != len(parts):
            raise ProbeBindingError(
                "probe report event_ids contains a duplicate id")
        for part in parts:
            if not _EVENT_ID.match(part):
                raise ProbeBindingError(
                    f"probe report event id {part!r} is not exact lowercase 32-hex")
        event_ids = tuple(sorted(parts))

    try:
        http_status = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ProbeBindingError(
            f"probe report http_status {status!r} is not an integer") from None

    if http_status != 200:
        raise ProbeBindingError(
            f"probe report declares HTTP {http_status}; only a successful "
            f"exchange is reusable evidence")
    if provider not in ALLOWED_PROBE_PROVIDERS:
        raise ProbeBindingError(
            f"probe report provider {provider!r} is not a permitted probe provider")
    if endpoint not in ALLOWED_PROBE_ENDPOINTS:
        raise ProbeBindingError(
            f"probe report endpoint {endpoint!r} is not the permitted historical "
            f"events endpoint")

    return ProbeReportFacts(
        provider=str(provider), endpoint=str(endpoint),
        requested_bucket=str(bucket), http_status=http_status,
        body_sha256=body_sha, event_ids=event_ids)


# --------------------------------------------------------------------------- #
# Candidate binding
# --------------------------------------------------------------------------- #
def _body_bytes(body: object) -> bytes:
    if isinstance(body, bytes):
        return body
    return str(body).encode("utf-8")


def _response_matches(row: object, facts: ProbeReportFacts) -> bool:
    """Whether one preserved response satisfies the committed fingerprint."""

    record = row  # sqlite3.Row
    if str(record["provider"]) != facts.provider:  # type: ignore[index]
        return False
    if str(record["endpoint"]) != facts.endpoint:  # type: ignore[index]
        return False
    if int(record["http_status"]) != facts.http_status:  # type: ignore[index]
        return False

    try:
        params = json.loads(str(record["request_params_json"]))  # type: ignore[index]
    except (TypeError, ValueError):
        return False
    if not isinstance(params, dict):
        return False
    # No population-reducing filter, and the requested bucket must match.
    if set(params) - ALLOWED_PROBE_REQUEST_PARAMS:
        return False
    if str(params.get("date", "")) != facts.requested_bucket:
        return False

    body = _body_bytes(record["body"])  # type: ignore[index]
    if facts.body_sha256 is not None:
        if hashlib.sha256(body).hexdigest() != facts.body_sha256:
            return False
    if facts.event_ids:
        try:
            payload = json.loads(body.decode("utf-8"))
            ids = tuple(sorted(str(e["id"]) for e in payload["data"]))
        except (ValueError, KeyError, TypeError):
            return False
        if ids != facts.event_ids:
            return False
    return True


def bind_probe_response(
    conn: object,
    *,
    probe_report_commit_sha: str,
    probe_report_path: str,
    probe_policy_version: str,
    repo_root: Optional[Path] = None,
) -> str:
    """Resolve the committed report and return the UNIQUE matching response id.

    The caller does not get to nominate a response. The candidate set is every
    preserved response satisfying the committed fingerprint, and it must contain
    exactly one member:

    * zero  -> REFUSE (the report names evidence this database does not hold)
    * many  -> REFUSE AS AMBIGUOUS (choosing would be curator selection)

    This is what makes the exception ungeneralizable: it is not "is this response
    *a* match" but "is this response *the only* match".
    """

    if probe_policy_version != STAGE_A_PROBE_POLICY_V1:
        raise ProbeBindingError(
            f"unknown probe policy version {probe_policy_version!r}; the only "
            f"frozen version is {STAGE_A_PROBE_POLICY_V1!r}")

    text = load_committed_text(probe_report_commit_sha, probe_report_path,
                               repo_root=repo_root)
    facts = parse_probe_report(text)

    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT raw_response_id, provider, endpoint, http_status,"
        " request_params_json, body FROM raw_responses WHERE provider = ?",
        (facts.provider,)).fetchall()
    matches = [str(r["raw_response_id"]) for r in rows if _response_matches(r, facts)]

    if not matches:
        raise ProbeBindingError(
            f"no preserved response matches the committed probe report at "
            f"{probe_report_commit_sha}:{probe_report_path} "
            f"(fingerprint: {facts.fingerprint_kind()})")
    if len(matches) > 1:
        raise ProbeBindingError(
            f"{len(matches)} preserved responses match the committed probe report; "
            f"refusing as AMBIGUOUS rather than selecting one: {sorted(matches)[:3]}")
    return matches[0]


def probe_binding_failures(
    conn: object, *, raw_response_id: str, registration: object,
    repo_root: Optional[Path] = None,
) -> Sequence[str]:
    """Re-verify a registration from scratch, returning failure reasons.

    The registration row is only a POINTER. Certification re-resolves the commit,
    re-loads the report, re-parses the contract and re-derives the unique
    matching response, so a registration forged by direct SQL stays unusable.
    """

    row = registration  # sqlite3.Row
    try:
        bound = bind_probe_response(
            conn,
            probe_report_commit_sha=str(row["probe_report_commit_sha"]),  # type: ignore[index]
            probe_report_path=str(row["probe_report_path"]),  # type: ignore[index]
            probe_policy_version=str(row["probe_policy_version"]),  # type: ignore[index]
            repo_root=repo_root,
        )
    except ProbeBindingError as exc:
        return (f"probe registration for {raw_response_id!r} does not bind to "
                f"committed evidence: {exc}",)

    if bound != raw_response_id:
        return (f"probe registration cites {raw_response_id!r} but the committed "
                f"report uniquely identifies {bound!r}; a registration may not "
                f"nominate a different response than its own report proves",)
    return ()
