"""F1A external checkpoint format + atomic persistence + resume verification.

A checkpoint records, outside the database and without any schema change, which
semantic requests are provably complete so an interrupted pilot can resume
without repeating completed work or spending credits twice. It contains **no**
secret, header, or raw body.

**Consistency boundary.** A semantic request is recorded *complete* only after
its raw response **and** the required normalized persistence are committed in a
single recoverable database transaction. A request interrupted before that
commit is treated as *incomplete* and is retried on resume (idempotent, because
observation writes are content-hash append-only). The checkpoint is written
atomically (temp file + ``os.replace``) after each unit reaches the boundary, so
the checkpoint never claims completion the database cannot back.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CHECKPOINT_FORMAT_VERSION = "f1a-checkpoint-v1"


class CheckpointError(RuntimeError):
    """A checkpoint/manifest/database mismatch (no network/DB mutation performed)."""


@dataclass
class Checkpoint:
    """Durable, secret-free record of pilot progress against a fixed manifest."""

    manifest_hash: str
    plan_version: str
    provider: str
    league: str
    date_range: str
    families: tuple[str, ...]
    scratch_db: str
    scratch_fingerprint: str
    schema_version: int
    request_cap: Optional[int]
    credit_cap: Optional[int]
    code_version: str = ""
    checkpoint_format_version: str = CHECKPOINT_FORMAT_VERSION
    completed_identities: list[str] = field(default_factory=list)
    failed_identities: list[str] = field(default_factory=list)
    blocked_identities: list[str] = field(default_factory=list)
    incomplete_identities: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    last_boundary: str = ""
    state: str = "in_progress"  # in_progress|completed|truncated

    def body(self) -> dict[str, Any]:
        return {
            "checkpoint_format_version": self.checkpoint_format_version,
            "plan_version": self.plan_version,
            "manifest_hash": self.manifest_hash,
            "code_version": self.code_version,
            "provider": self.provider,
            "league": self.league,
            "date_range": self.date_range,
            "families": list(self.families),
            "scratch_db": self.scratch_db,
            "scratch_fingerprint": self.scratch_fingerprint,
            "schema_version": self.schema_version,
            "request_cap": self.request_cap,
            "credit_cap": self.credit_cap,
            "completed_identities": sorted(set(self.completed_identities)),
            "failed_identities": sorted(set(self.failed_identities)),
            "blocked_identities": sorted(set(self.blocked_identities)),
            "incomplete_identities": sorted(set(self.incomplete_identities)),
            "usage": self.usage,
            "last_boundary": self.last_boundary,
            "state": self.state,
        }

    def canonical(self) -> str:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":"))

    def is_complete_for(self, identity: str) -> bool:
        return identity in set(self.completed_identities)


def write_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    """Atomically write the checkpoint: temp file in the same dir, then replace.

    The temp+replace makes a torn write impossible -- a reader sees either the
    prior checkpoint or the new one, never a partial file.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    text = checkpoint.canonical()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on POSIX and Windows


def load_checkpoint(path: Path) -> Checkpoint:
    path = Path(path)
    if not path.exists():
        raise CheckpointError(f"no checkpoint at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("checkpoint_format_version") != CHECKPOINT_FORMAT_VERSION:
        raise CheckpointError(
            f"unsupported checkpoint format {data.get('checkpoint_format_version')!r}")
    return Checkpoint(
        manifest_hash=data["manifest_hash"],
        plan_version=data["plan_version"],
        provider=data["provider"],
        league=data["league"],
        date_range=data["date_range"],
        families=tuple(data["families"]),
        scratch_db=data["scratch_db"],
        scratch_fingerprint=data["scratch_fingerprint"],
        schema_version=int(data["schema_version"]),
        request_cap=data.get("request_cap"),
        credit_cap=data.get("credit_cap"),
        code_version=data.get("code_version", ""),
        completed_identities=list(data.get("completed_identities", [])),
        failed_identities=list(data.get("failed_identities", [])),
        blocked_identities=list(data.get("blocked_identities", [])),
        incomplete_identities=list(data.get("incomplete_identities", [])),
        usage=dict(data.get("usage", {})),
        last_boundary=data.get("last_boundary", ""),
        state=data.get("state", "in_progress"),
    )


def verify_resume(
    checkpoint: Checkpoint,
    *,
    manifest_hash: str,
    provider: str,
    league: str,
    date_range: str,
    families: tuple[str, ...],
    plan_version: str,
    scratch_fingerprint: str,
) -> None:
    """Reject a resume whose manifest/provider/league/dates/families/plan/db differ.

    Raises :class:`CheckpointError` *before* any network or database mutation.
    """

    mismatches: list[str] = []
    if checkpoint.manifest_hash != manifest_hash:
        mismatches.append("manifest_hash")
    if checkpoint.plan_version != plan_version:
        mismatches.append("plan_version")
    if checkpoint.provider != provider:
        mismatches.append("provider")
    if checkpoint.league != league:
        mismatches.append("league")
    if checkpoint.date_range != date_range:
        mismatches.append("date_range")
    if tuple(checkpoint.families) != tuple(families):
        mismatches.append("families")
    if checkpoint.scratch_fingerprint != scratch_fingerprint:
        mismatches.append("scratch_db_fingerprint")
    if mismatches:
        raise CheckpointError(
            "resume rejected: checkpoint does not match the current manifest/database: "
            + ", ".join(sorted(mismatches)))
