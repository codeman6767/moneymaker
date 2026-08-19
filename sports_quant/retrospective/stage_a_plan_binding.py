"""Stage-A plan manifest commit/content binding (retained blocker B2).

The threat this closes
----------------------
`stage_a_plans` already stored `manifest_commit_sha`, `manifest_content_digest`
and `manifest_path`, but nothing resolved them. The v22 independent review proved
the binding was not load-bearing: a fabricated 40-character commit id could be
persisted while an otherwise self-consistent plan certified, because certification
only ever proved

    DB plan  <->  caller-supplied StageAManifest  <->  caller-supplied text

Agreement among three values the caller controls is not source-control
provenance. B2 makes the statement real:

    COMMIT
      -> exact committed manifest blob
      -> parsed StageAManifest
      -> exact content digest
      -> semantic plan digest
      -> persisted stage_a_plans row
      -> persisted target / bucket membership

No caller may substitute a different manifest object or text at certification
time, and the working tree is never consulted.

Two digests, deliberately distinct
----------------------------------
``manifest_content_digest`` is the SHA-256 of the exact committed blob BYTES. It
proves *this exact artefact was committed*. It is byte-exact: an earlier
text-mode reader applied universal-newline translation, so a CRLF file hashed as
if it were LF and two different committed artefacts could share one digest.

``plan_digest`` is the SHA-256 of the canonical semantic body. It proves *this is
the scientific plan identity*, and is insensitive to formatting.

Both are recomputed from the committed blob. Neither is trusted because it is
stored.

What B2 does NOT prove
----------------------
That the committed manifest is scientifically CORRECT. A manifest mapping all 239
targets to wrong buckets binds perfectly and must still fail §AF, which
independently recomputes official-hint -> T-60 -> 5-minute-floor. B2 answers "did
we certify the exact artefact that was committed?", not "is that artefact right?".

Nor does it prove chronology: git commit timestamps are attacker-settable and a
local history can be rewritten. The plan-before-network boundary remains the
combination of committed-artefact binding, acquisition registration, the
``requested_at >= registered_at`` rule and the append-only ledger.

This module performs no network I/O, including no git fetch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from .stage_a_manifest import (
    StageAManifest,
    StageAManifestError,
    loads,
    manifest_content_digest_bytes,
)
from .stage_a_probe_binding import (
    GitObjectError,
    load_committed_bytes,
    resolve_commit,
    validate_repo_path,
)

#: Every persisted `stage_a_plans` column that is DERIVED from the manifest and
#: must therefore be recomputable from the committed artefact. `manifest_path`
#: and `manifest_commit_sha` are the pointer itself; `created_at` is a record
#: clock; neither derives from manifest content.
MANIFEST_DERIVED_PLAN_COLUMNS: Final[tuple[str, ...]] = (
    "manifest_content_digest", "plan_digest", "manifest_format_version",
    "plan_policy_version", "league_id", "provider", "namespace_generation",
    "sport_key", "official_source_corpus_digest", "official_target_set_digest",
    "decision_horizon_minutes", "bucket_floor_seconds",
    "acquisition_policy_version", "projection_policy_version",
    "cost_policy_version",
)


class PlanBindingError(RuntimeError):
    """A plan cannot be bound to its committed manifest. Always fails closed."""


@dataclass(frozen=True)
class CommittedStageAManifest:
    """A Stage-A manifest proven to be the artefact committed at a named commit."""

    commit_sha: str
    manifest_path: str
    #: Exact committed blob bytes -- not a decoded or normalized rendering.
    raw: bytes
    content_digest: str
    manifest: StageAManifest

    @property
    def plan_digest(self) -> str:
        return self.manifest.plan_digest()


def load_committed_stage_a_manifest(
    commit_sha: str,
    manifest_path: str,
    *,
    repo_root: Optional[Path] = None,
) -> CommittedStageAManifest:
    """Resolve a commit, load the manifest committed there, and derive its facts.

    The whole proof chain lives here so no caller can supply a step of it.
    """

    try:
        resolved = resolve_commit(commit_sha, repo_root=repo_root)
        safe_path = validate_repo_path(manifest_path)
        raw = load_committed_bytes(resolved, safe_path, repo_root=repo_root)
    except GitObjectError as exc:
        raise PlanBindingError(
            f"Stage-A plan manifest is not bound to source control: {exc}") from None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanBindingError(
            f"committed manifest at {resolved}:{safe_path} is not valid UTF-8: "
            f"{exc}") from None

    try:
        # `loads` rejects duplicate keys, refuses unknown fields (closed schema),
        # re-derives any embedded plan_digest and runs structural validation.
        manifest = loads(text)
    except StageAManifestError as exc:
        raise PlanBindingError(
            f"committed manifest at {resolved}:{safe_path} is not a valid Stage-A "
            f"manifest: {exc}") from None

    return CommittedStageAManifest(
        commit_sha=resolved,
        manifest_path=safe_path,
        raw=raw,
        content_digest=manifest_content_digest_bytes(raw),
        manifest=manifest,
    )


def committed_manifest_for_plan(
    conn: sqlite3.Connection,
    plan_row: sqlite3.Row,
    *,
    repo_root: Optional[Path] = None,
) -> CommittedStageAManifest:
    """Load the committed manifest named BY THE PERSISTED PLAN ROW.

    This is the function that removes the caller from the trust path: the commit
    and path come from the database, and the manifest comes from git.
    """

    return load_committed_stage_a_manifest(
        str(plan_row["manifest_commit_sha"]),
        str(plan_row["manifest_path"]),
        repo_root=repo_root,
    )


def plan_row_disagreements(
    plan_row: sqlite3.Row, committed: CommittedStageAManifest
) -> list[str]:
    """Every persisted plan field that disagrees with the committed artefact.

    The committed artefact is authoritative; a stored value never overrides it.
    """

    manifest = committed.manifest
    expected: dict[str, object] = {
        "manifest_content_digest": committed.content_digest,
        "plan_digest": committed.plan_digest,
        "manifest_format_version": manifest.manifest_format_version,
        "plan_policy_version": manifest.plan_policy_version,
        "league_id": manifest.league_id,
        "provider": manifest.provider,
        "namespace_generation": manifest.namespace_generation,
        "sport_key": manifest.sport_key,
        "official_source_corpus_digest": manifest.official_source_corpus_digest,
        "official_target_set_digest": manifest.official_target_set_digest,
        "decision_horizon_minutes": manifest.decision_horizon_minutes,
        "bucket_floor_seconds": manifest.bucket_floor_seconds,
        "acquisition_policy_version": manifest.acquisition_policy_version,
        "projection_policy_version": manifest.projection_policy_version,
        "cost_policy_version": manifest.cost_policy_version,
    }

    failures: list[str] = []
    for column in MANIFEST_DERIVED_PLAN_COLUMNS:
        want = expected[column]
        got = plan_row[column]
        if isinstance(want, int):
            matches = got is not None and int(got) == want
        else:
            matches = str(got) == str(want)
        if not matches:
            failures.append(
                f"persisted plan {column} is {got!r} but the committed manifest "
                f"at {committed.commit_sha[:12]}:{committed.manifest_path} "
                f"derives {want!r}")
    return failures


def plan_membership_disagreements(
    conn: sqlite3.Connection, plan_id: str, committed: CommittedStageAManifest
) -> list[str]:
    """Persisted target/bucket membership vs the committed artefact."""

    failures: list[str] = []
    manifest = committed.manifest

    db_targets = {
        str(r["canonical_game_id"]): str(r["requested_at_bucket"])
        for r in conn.execute(
            "SELECT canonical_game_id, requested_at_bucket FROM stage_a_plan_targets"
            " WHERE plan_id = ?", (plan_id,))
    }
    committed_targets = manifest.target_map()
    if set(db_targets) != set(committed_targets):
        missing = sorted(set(committed_targets) - set(db_targets))
        extra = sorted(set(db_targets) - set(committed_targets))
        failures.append(
            f"persisted target population differs from the committed manifest "
            f"(missing={missing[:3]}, extra={extra[:3]})")
    elif db_targets != committed_targets:
        moved = sorted(k for k in db_targets if db_targets[k] != committed_targets[k])
        failures.append(
            f"persisted target -> bucket mapping differs from the committed "
            f"manifest for {moved[:3]}")

    db_buckets = {
        str(r["requested_at_bucket"]) for r in conn.execute(
            "SELECT requested_at_bucket FROM stage_a_planned_buckets"
            " WHERE plan_id = ?", (plan_id,))
    }
    if db_buckets != set(manifest.buckets):
        failures.append(
            "persisted planned bucket set differs from the committed manifest")
    return failures
