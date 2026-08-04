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

**Usage provenance (v2).** ``usage`` holds the LOGICAL-RUN totals across every
process that has executed this manifest, and ``usage_provenance.processes`` is
the append-only per-process history those totals are derived from. A process
replaces its own (last) entry on every write and never appends twice, so
repeated resumes cannot multiply prior usage. A v1 checkpoint is still readable:
its flat ``usage`` is adopted as a single legacy history entry, marked
``legacy_migrated``, and never fabricated into per-process detail it does not
contain. See :mod:`sports_quant.usage_provenance` for the combine rules.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..usage_provenance import (
    LEGACY_PROCESS_ID,
    PROCESS_ID_KEY,
    USAGE_ACCOUNTING_VERSION,
    UsageProvenanceError,
    combine_usage,
    prior_totals,
    sanitized_process_entries,
    sanitized_usage,
    validate_usage_accounting,
)

CHECKPOINT_FORMAT_VERSION = "f1a-checkpoint-v2"
#: Formats this build can read. v1 lacks per-process usage provenance.
LEGACY_CHECKPOINT_FORMAT_VERSION = "f1a-checkpoint-v1"
SUPPORTED_CHECKPOINT_FORMATS = (CHECKPOINT_FORMAT_VERSION,
                                LEGACY_CHECKPOINT_FORMAT_VERSION)

# Serialize checkpoint writes to the same path across threads/tasks: a temp+replace
# is atomic, but concurrent replaces onto one target race (notably on Windows). A
# per-path lock makes concurrent writers serialized rather than corrupting.
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


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
    #: Units that an earlier process left unresolved (blocked/failed/incomplete)
    #: and a later process completed. Keeps identity-level failure history after
    #: the unit legitimately leaves the unresolved sets.
    recovered_identities: list[str] = field(default_factory=list)
    #: The frozen canonical selected game set (from the skeleton unit); resume uses
    #: this exact set rather than re-deriving from a possibly-changed schedule.
    stage_game_ids: list[str] = field(default_factory=list)
    #: LOGICAL-RUN totals (all processes), derived from :attr:`process_usage`.
    usage: dict[str, Any] = field(default_factory=dict)
    #: Append-only per-process usage history, oldest first. Exactly one entry per
    #: process that has executed this manifest.
    process_usage: list[dict[str, Any]] = field(default_factory=list)
    #: True when this history was adopted from a v1 checkpoint, whose per-process
    #: split is unknowable. The evidence present is preserved; none is invented.
    legacy_migrated: bool = False
    last_boundary: str = ""
    state: str = "in_progress"  # in_progress|completed|truncated|failed

    @property
    def process_count_known(self) -> bool:
        """False for a migrated v1 history: it cannot say how many processes ran."""

        return not self.legacy_migrated

    def prior_usage(self) -> dict[str, Any]:
        """Combined evidence of every process except the newest."""

        return prior_totals(self.process_usage)

    def current_process_usage(self) -> dict[str, Any]:
        """The newest process's own evidence (empty when there is no history)."""

        return dict(self.process_usage[-1]) if self.process_usage else {}

    def logical_usage(self) -> dict[str, Any]:
        """Logical-run totals recomputed from the history (source of truth)."""

        return combine_usage(self.process_usage) if self.process_usage else dict(self.usage)

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
            "recovered_identities": sorted(set(self.recovered_identities)),
            "stage_game_ids": list(self.stage_game_ids),
            "usage": self.usage,
            "usage_provenance": {
                "accounting_version": USAGE_ACCOUNTING_VERSION,
                "legacy_migrated": self.legacy_migrated,
                "process_count_known": self.process_count_known,
                "process_count": len(self.process_usage),
                "processes": [dict(sorted(p.items())) for p in self.process_usage],
            },
            "last_boundary": self.last_boundary,
            "state": self.state,
        }

    def canonical(self) -> str:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":"),
                          default=_json_default)

    def is_complete_for(self, identity: str) -> bool:
        return identity in set(self.completed_identities)


def _json_default(value: Any) -> Any:
    """Serialize the tuple-valued usage fields (families_*) deterministically."""

    if isinstance(value, (tuple, set, frozenset)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    raise TypeError(f"checkpoint value of type {type(value).__name__} is not serializable")


#: Hard cap on a checkpoint file we will parse (defends against a hostile,
#: maliciously-huge JSON). A real checkpoint is a few KB.
_MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024


#: Bounded retries for a TRANSIENT replace failure. On Windows an unrelated
#: handle on the destination (a virus scanner or the search indexer opening the
#: file we just wrote) makes ``os.replace`` fail with ERROR_ACCESS_DENIED even
#: though no writer of ours is racing -- observed under load during the
#: independent provenance review. Retrying is safe precisely because the replace
#: is atomic: it either happened (no error) or it did not.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.02


def write_checkpoint(path: Path, checkpoint: Checkpoint,
                     *, sleep: Optional[Any] = None) -> None:
    """Atomically and durably write the checkpoint: unique temp, fsync, replace.

    The temp file name is unique per process AND per call (pid + random suffix)
    so concurrent writers/tasks never collide. The file is fsynced, replaced
    atomically, and the containing directory is fsynced so the rename is durable
    across a crash -- a reader always sees either the prior or the new valid
    checkpoint, never a torn file, and a completed write cannot silently roll back.
    A symlinked target is refused (isolation).

    A transient ``PermissionError`` from the replace is retried a bounded number of
    times; anything else, and a persistent failure, is raised so the caller records
    a genuine write failure. ``sleep`` is injectable so a test never really waits.
    """

    path = Path(path)
    if path.is_symlink():
        raise CheckpointError(f"refusing to write a checkpoint through a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = checkpoint.canonical()
    pause = sleep if sleep is not None else _default_sleep
    with _lock_for(path):  # serialize writers to this path (no racing replaces)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{os.urandom(6).hex()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(1, _REPLACE_ATTEMPTS + 1):
                try:
                    os.replace(tmp, path)  # atomic on POSIX and Windows
                    break
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS:
                        raise
                    pause(_REPLACE_BACKOFF_SECONDS * attempt)
            _fsync_dir(path.parent)
        finally:
            if tmp.exists():  # a failure before replace must leave no stray temp
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so an atomic rename is durable (POSIX)."""

    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return  # not supported on this platform (e.g. Windows dir fsync); skip
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise CheckpointError(f"duplicate JSON key in checkpoint: {k!r}")
        seen[k] = v
    return seen


def load_checkpoint(path: Path) -> Checkpoint:
    path = Path(path)
    if path.is_symlink():
        raise CheckpointError(f"refusing to read a checkpoint through a symlink: {path}")
    if not path.is_file():
        raise CheckpointError(f"no checkpoint at {path}")
    if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
        raise CheckpointError(f"checkpoint too large (> {_MAX_CHECKPOINT_BYTES} bytes)")
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CheckpointError(f"checkpoint is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise CheckpointError("checkpoint root must be a JSON object")
    fmt = data.get("checkpoint_format_version")
    if fmt not in SUPPORTED_CHECKPOINT_FORMATS:
        raise CheckpointError(f"unsupported checkpoint format {fmt!r}")
    for req in ("manifest_hash", "plan_version", "provider", "league", "date_range",
                "families", "scratch_db", "scratch_fingerprint", "schema_version"):
        if req not in data:
            raise CheckpointError(f"checkpoint missing required field: {req}")
    try:
        # Untrusted input: drop unknown keys and refuse a wrong type, a non-finite
        # float or a negative count before any value reaches arithmetic.
        flat_usage = sanitized_usage(data.get("usage", {}) or {})
        history, legacy = _load_process_usage(data, fmt, flat_usage)
    except UsageProvenanceError as exc:
        raise CheckpointError(f"checkpoint usage provenance is unusable: {exc}") from None
    # The stored logical totals must be exactly what the recorded history implies.
    # A checkpoint whose totals contradict its own history is not trustworthy
    # evidence, so it fails closed rather than being silently recomputed.
    #
    # A legacy v1 file cannot satisfy the history-closure or retry identities (its
    # per-process split is unknowable), but it must still not assert something
    # IMPOSSIBLE -- more terminal outcomes than transports, pages beyond successful
    # responses, or usage above the manifest cap. Those are checked for both.
    problems = validate_usage_accounting(
        flat_usage, request_cap=data.get("request_cap"),
        credit_cap=data.get("credit_cap"),
        entries=None if legacy else (history or None),
        require_retry_identity=False if legacy else None)
    if problems:
        raise CheckpointError(
            "checkpoint usage accounting is inconsistent: " + "; ".join(problems[:4]))
    state = data.get("state", "in_progress")
    _validate_identity_sets(data, state)
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
        recovered_identities=list(data.get("recovered_identities", [])),
        stage_game_ids=list(data.get("stage_game_ids", [])),
        usage=flat_usage,
        process_usage=history,
        legacy_migrated=legacy,
        last_boundary=data.get("last_boundary", ""),
        state=state,
        # Keep the on-disk format so a v1 file that is only READ (never resumed)
        # is not silently relabelled as v2 in memory.
        checkpoint_format_version=str(fmt),
    )


def _identity_list(data: dict[str, Any], key: str) -> list[str]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(i, str) for i in raw):
        raise CheckpointError(f"checkpoint field {key!r} must be a list of identities")
    return raw


def _validate_identity_sets(data: dict[str, Any], state: str) -> None:
    """Refuse a checkpoint whose unit sets contradict each other.

    Identity strings are canonical JSON and can be long, so messages report counts
    and a truncated example rather than dumping the sets.
    """

    completed = set(_identity_list(data, "completed_identities"))
    failed = set(_identity_list(data, "failed_identities"))
    blocked = set(_identity_list(data, "blocked_identities"))
    incomplete = set(_identity_list(data, "incomplete_identities"))
    recovered = set(_identity_list(data, "recovered_identities"))
    unresolved = failed | blocked | incomplete

    def _example(items: set[str]) -> str:
        one = sorted(items)[0]
        return one if len(one) <= 48 else one[:45] + "..."

    for label, other in (("failed", failed), ("blocked", blocked),
                         ("incomplete", incomplete)):
        overlap = completed & other
        if overlap:
            raise CheckpointError(
                f"checkpoint holds {len(overlap)} unit(s) that are both completed and "
                f"{label} (e.g. {_example(overlap)}); a unit cannot be in two states")
    if state == "completed" and unresolved:
        raise CheckpointError(
            f"checkpoint claims state 'completed' while holding {len(unresolved)} "
            "unresolved unit(s); refusing to treat it as complete")
    # `recovered` means "was unresolved earlier, and is complete now", so it must be
    # a subset of completed and disjoint from every unresolved set.
    ghosts = recovered - completed
    if ghosts:
        raise CheckpointError(
            f"checkpoint marks {len(ghosts)} unit(s) recovered that are not completed "
            f"(e.g. {_example(ghosts)})")
    still_unresolved = recovered & unresolved
    if still_unresolved:
        raise CheckpointError(
            f"checkpoint marks {len(still_unresolved)} unit(s) both recovered and "
            f"still unresolved (e.g. {_example(still_unresolved)})")


def _load_process_usage(
    data: dict[str, Any], fmt: Any, flat_usage: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(per_process_history, legacy_migrated)``.

    A v2 checkpoint carries its own history. A v1 checkpoint carries only the
    newest process's flat report; it is adopted verbatim as ONE history entry so
    every fact it does contain survives a resume, and it is flagged legacy so
    nothing later claims to know how many processes produced it. Missing counters
    are never invented -- an absent field stays absent rather than becoming 0.
    """

    prov = data.get("usage_provenance")
    if fmt == CHECKPOINT_FORMAT_VERSION:
        if prov is None:
            raise UsageProvenanceError("v2 checkpoint has no usage_provenance block")
        if not isinstance(prov, dict):
            raise UsageProvenanceError("usage_provenance must be a JSON object")
        version = prov.get("accounting_version")
        if version != USAGE_ACCOUNTING_VERSION:
            raise UsageProvenanceError(
                f"unsupported usage accounting version {version!r}")
        raw = prov.get("processes")
        if not isinstance(raw, list):
            raise UsageProvenanceError("usage_provenance.processes must be a list")
        history = sanitized_process_entries(raw)
        declared = prov.get("process_count")
        if isinstance(declared, int) and declared != len(history):
            raise UsageProvenanceError(
                f"usage_provenance.process_count {declared} does not match the "
                f"{len(history)} recorded process entries")
        return history, bool(prov.get("legacy_migrated", False))
    # -- v1 ---------------------------------------------------------------- #
    if prov is not None:
        raise UsageProvenanceError(
            "v1 checkpoint carries a usage_provenance block; refusing a "
            "contradictory mixture of formats")
    if not flat_usage:
        return [], True
    # The aggregate is tagged with a NON-random identifier saying exactly what it
    # is: one entry standing for an unknown number of earlier processes. A random
    # per-invocation token here would imply a single identified run.
    return sanitized_process_entries(
        [{**flat_usage, PROCESS_ID_KEY: LEGACY_PROCESS_ID}]), True


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
