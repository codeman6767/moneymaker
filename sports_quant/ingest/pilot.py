"""F1A pilot runner: execute a plan unit-by-unit under the budget gate.

The runner is provider-agnostic. It consumes a :class:`PilotExecutor` that
performs the per-unit fetch + persist and *yields one :class:`UnitDone` only
after that unit has reached the consistency boundary* -- its raw response and
required normalized persistence committed in a single recoverable database
transaction. After each yielded unit the runner atomically rewrites the
checkpoint, so the checkpoint never claims completion the database cannot back.

Responsibilities kept here (not in the executor):

* resume: verify the checkpoint against the manifest + scratch fingerprint,
  seed the completed-set, and count skipped units;
* budget exhaustion: catch :class:`BudgetExhausted` from a gated transport,
  stop in a controlled truncated state, preserve completed work, write a final
  checkpoint, and never report success;
* deterministic usage/report assembly.

No network or database work happens here except through the injected executor;
the gate (wired into the transport chokepoint) is what actually blocks a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from ..request_control import BudgetExhausted, RequestGate
from ..usage_provenance import (
    PROCESS_ID_KEY,
    assert_no_new_transport,
    combine_usage,
    current_process_entry,
    new_process_id,
    validate_usage_accounting,
)
from .checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    Checkpoint,
    CheckpointError,
    load_checkpoint,
    verify_resume,
    write_checkpoint,
)
from .manifest import PilotManifest


@dataclass(frozen=True)
class UnitDone:
    """A semantic unit that reached the consistency boundary (persisted+committed)."""

    identity: str
    family: str
    database_mutated: bool = True
    #: Set by the skeleton unit to freeze the canonical selected game set into the
    #: checkpoint, so a resume uses the SAME games even if the provider schedule
    #: later changes.
    stage_game_ids: tuple[str, ...] = ()


class PilotExecutor(Protocol):
    """Performs per-unit fetch+persist, yielding a unit only once it is durable.

    Implementations MUST skip any unit whose ``identity`` is already in
    ``completed`` without issuing a transport call, and MUST persist+commit a
    unit before yielding it.

    An implementation MAY also provide ``remaining_identities(*, completed)``
    returning the units still outstanding **without any transport**. When it can
    prove nothing remains, the runner turns a completed resume into a true no-op
    (see :func:`run_pilot`). An executor that cannot answer offline must simply
    not define it.

    An implementation MAY also provide ``in_flight_identity()`` returning the
    identity of the unit it is working on right now (``None`` between units). The
    runner records it in ``incomplete_identities`` when a unit raises, so the
    checkpoint keeps identity-level evidence of which unit was left unfinished and
    a later completion of it is recognisable as a recovery. It is read, never
    inferred: an executor that does not track it simply omits the method.
    """

    def iter_units(self, *, gate: RequestGate, completed: set[str]) -> Iterator[UnitDone]:
        ...


@dataclass
class PilotResult:
    success: bool
    truncated: bool
    completed: int
    skipped: int
    exhaustion: Optional[dict[str, Any]]
    #: LOGICAL-RUN totals across every process of this run.
    usage: dict[str, Any]
    checkpoint_state: str
    #: Current-process facts: did THIS command reach the network / write rows.
    network_occurred: bool
    database_mutated: bool
    failure: Optional[str] = None
    #: This process's own usage, distinct from the logical totals above.
    current_process_usage: dict[str, Any] = field(default_factory=dict)
    #: Combined usage of every earlier process (empty for a first run).
    prior_process_usage: dict[str, Any] = field(default_factory=dict)
    #: Whether this command executed any unit at all.
    performed_new_work: bool = True
    #: Whether this command rewrote the checkpoint file.
    checkpoint_mutated: bool = True
    #: Number of processes whose evidence the logical totals combine.
    process_count: int = 1
    #: True when the history was adopted from a v1 checkpoint, so the per-process
    #: split of the prior evidence is unknowable (the evidence itself is intact).
    legacy_provenance: bool = False
    #: Units an earlier process left unresolved and a later one completed.
    recovered_identities: tuple[str, ...] = ()
    #: Units still failed / blocked / incomplete after this command.
    unresolved_identities: tuple[str, ...] = ()
    #: Units completed without ever having been unresolved.
    initially_completed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "truncated": self.truncated,
            "failed": self.failure is not None,
            "completed": self.completed,
            "skipped_on_resume": self.skipped,
            "budget_exhausted": self.exhaustion,
            "failure": self.failure,
            # `usage` remains the primary key for back-compatibility; its exact
            # semantics are the LOGICAL-RUN totals (all processes of this run).
            "usage": self.usage,
            "current_process_usage": self.current_process_usage,
            "prior_process_usage": self.prior_process_usage,
            "performed_new_work": self.performed_new_work,
            "checkpoint_mutated": self.checkpoint_mutated,
            "process_count": self.process_count,
            "legacy_provenance": self.legacy_provenance,
            # Unit-level provenance: a consumer must be able to tell a unit that
            # completed first time from one recovered after an earlier failure.
            "recovered_identities": list(self.recovered_identities),
            "unresolved_identities": list(self.unresolved_identities),
            "initially_completed": self.initially_completed,
            "recovered_count": len(self.recovered_identities),
            "unresolved_count": len(self.unresolved_identities),
            "checkpoint_state": self.checkpoint_state,
            # Current-process facts, NOT logical-run facts.
            "network_occurred": self.network_occurred,
            "database_mutated": self.database_mutated,
        }


def _new_checkpoint(
    manifest: PilotManifest, *, scratch_fingerprint: str, code_version: str
) -> Checkpoint:
    return Checkpoint(
        manifest_hash=manifest.manifest_hash(),
        plan_version=manifest.plan_version,
        provider=manifest.provider,
        league=manifest.league,
        date_range=manifest.date_range,
        families=manifest.families,
        scratch_db=manifest.scratch_db,
        scratch_fingerprint=scratch_fingerprint,
        schema_version=manifest.expected_schema_version,
        request_cap=manifest.request_cap,
        credit_cap=manifest.credit_cap,
        code_version=code_version,
    )


def run_pilot(
    *,
    manifest: PilotManifest,
    gate: RequestGate,
    executor: PilotExecutor,
    checkpoint_path: Path,
    scratch_fingerprint: str,
    resume: bool = False,
    code_version: str = "",
    fingerprint_fn: Optional[Callable[[], str]] = None,
) -> PilotResult:
    """Execute the plan through ``executor`` under ``gate``, checkpointing each unit.

    ``fingerprint_fn`` recomputes the *current* scratch-database row-count
    fingerprint; it is recorded at every checkpoint write so a resume can prove
    the database is exactly as the checkpoint left it. When omitted, the constant
    ``scratch_fingerprint`` is used (tests without a real database).
    """

    fp = fingerprint_fn or (lambda: scratch_fingerprint)
    checkpoint_path = Path(checkpoint_path)
    completed: set[str] = set()
    prior_history: list[dict[str, Any]] = []
    my_process_id = new_process_id()
    if resume:
        ck = load_checkpoint(checkpoint_path)
        verify_resume(
            ck, manifest_hash=manifest.manifest_hash(), provider=manifest.provider,
            league=manifest.league, date_range=manifest.date_range, families=manifest.families,
            plan_version=manifest.plan_version, scratch_fingerprint=scratch_fingerprint,
        )
        completed = set(ck.completed_identities)
        ck.code_version = ck.code_version or code_version
        # Every earlier process's evidence, carried forward untouched. A resumed
        # process appends exactly ONE entry of its own (below), so resuming
        # repeatedly can never multiply prior usage.
        prior_history = [dict(entry) for entry in ck.process_usage]
        # A completed checkpoint with nothing left to do must not rewrite itself:
        # rewriting is what destroyed the earlier process's failure/retry evidence.
        if ck.state == "completed":
            remaining = _remaining_identities(executor, completed)
            if remaining == ():
                return _no_work_result(ck, completed=completed, manifest=manifest,
                                       gate=gate)
        # A resumed process is upgraded to the current format on its next write;
        # a v1 file that is only read stays v1 on disk.
        ck.checkpoint_format_version = CHECKPOINT_FORMAT_VERSION
    else:
        ck = _new_checkpoint(manifest, scratch_fingerprint=scratch_fingerprint,
                             code_version=code_version)

    def _record_usage() -> None:
        """Refresh this process's history entry and the logical-run totals.

        The current process owns exactly one entry, identified by its own
        per-invocation ``process_id``, which is REPLACED on every checkpoint write
        rather than appended, so the many writes of one process contribute their
        evidence once. The identifier is a fresh random token, never the PID: PIDs
        are reused by the OS, so a PID could not distinguish two invocations.
        """

        entry = {PROCESS_ID_KEY: my_process_id,
                 **current_process_entry(gate.usage.as_dict())}
        ck.process_usage = [*prior_history, entry]
        ck.usage = combine_usage(ck.process_usage)

    def _write(path: Path) -> None:
        """Write the checkpoint after proving no other process has appended to it.

        The prior history was read once, at resume. If another process has written
        since, blindly writing ``prior + mine`` would drop that process's entry, so
        the divergence fails closed instead.
        """

        _assert_sole_writer(path, prior_history, my_process_id)
        write_checkpoint(path, ck)

    gate.usage.league = manifest.league
    gate.usage.manifest_hash = manifest.manifest_hash()
    gate.usage.skipped_on_resume = len(completed)
    gate.usage.estimated_requests_min = manifest.estimated_requests_min
    gate.usage.estimated_requests_max = manifest.estimated_requests_max or 0
    gate.usage.estimated_credits_min = manifest.estimated_credits_min
    gate.usage.estimated_credits_max = manifest.estimated_credits_max

    families_done: set[str] = set()
    newly_completed = 0
    db_mutated = False
    exhaustion: Optional[dict[str, Any]] = None
    truncated = False
    failed = False
    failure: Optional[str] = None

    try:
        for done in executor.iter_units(gate=gate, completed=completed):
            completed.add(done.identity)
            ck.completed_identities = sorted(completed)
            ck.last_boundary = done.identity
            # A unit that an earlier process left blocked/failed/incomplete is now
            # resolved, so it must leave the unresolved sets -- a `completed` state
            # holding an unresolved unit is a contradiction. The evidence is NOT
            # discarded: the identity is recorded as recovered, and the earlier
            # process's own truncation/failure report stays in the history.
            if (done.identity in set(ck.blocked_identities)
                    or done.identity in set(ck.failed_identities)
                    or done.identity in set(ck.incomplete_identities)):
                ck.recovered_identities = sorted(
                    set(ck.recovered_identities) | {done.identity})
                ck.blocked_identities = sorted(
                    set(ck.blocked_identities) - {done.identity})
                ck.failed_identities = sorted(
                    set(ck.failed_identities) - {done.identity})
                ck.incomplete_identities = sorted(
                    set(ck.incomplete_identities) - {done.identity})
            if done.stage_game_ids:  # freeze the selected game set into the checkpoint
                ck.stage_game_ids = list(done.stage_game_ids)
            families_done.add(done.family)
            newly_completed += 1
            if done.database_mutated:
                db_mutated = True
            gate.usage.database_mutated = db_mutated
            gate.usage.families_completed = tuple(sorted(families_done))
            _record_usage()
            ck.state = "in_progress"
            ck.scratch_fingerprint = fp()  # current DB fingerprint at this boundary
            _write(checkpoint_path)  # atomic, after the boundary
        ck.state = "completed"
        gate.usage.checkpoint_state = "resumed_completed" if resume else "completed"
    except BudgetExhausted as exc:
        # Controlled truncation: completed units are already durable + checkpointed.
        truncated = True
        exhaustion = exc.as_dict()
        ck.state = "truncated"
        ck.blocked_identities = sorted(set(ck.blocked_identities) | {exc.blocked_identity})
        gate.usage.checkpoint_state = "truncated"
        gate.usage.families_truncated = tuple(sorted(families_done | {exc.blocked_family}))
    except BaseException as exc:  # noqa: BLE001 - record, preserve completed work, re-classify
        # A non-budget failure (fetch/parse/persist, cancellation, KeyboardInterrupt):
        # the in-progress unit is NOT checkpointed (left incomplete -> resumable);
        # completed units stay durable; the original classification is preserved.
        failed = True
        failure = f"{type(exc).__name__}: {exc}"
        ck.state = "failed"
        gate.usage.checkpoint_state = "failed"
        # Record WHICH unit was in flight, when the executor can say so. Without
        # this the checkpoint kept no identity-level record of the failure at all:
        # `incomplete_identities` stayed empty, so nothing could report a unit as
        # still unresolved and no later completion could ever be recognised as a
        # recovery. The identity is read from the executor, never inferred.
        in_flight = _in_flight_identity(executor)
        if in_flight is not None and in_flight not in completed:
            ck.incomplete_identities = sorted(
                set(ck.incomplete_identities) | {in_flight})
        gate.usage.families_completed = tuple(sorted(families_done))
        gate.usage.database_mutated = db_mutated
        # A failed process's own evidence is recorded and merged with every
        # earlier process's, so a later successful resume cannot erase the fact
        # that this process failed after doing real work.
        _record_usage()
        ck.scratch_fingerprint = fp()
        _write(checkpoint_path)
        result = PilotResult(
            success=False, truncated=False, completed=newly_completed,
            skipped=gate.usage.skipped_on_resume, exhaustion=None,
            usage=dict(ck.usage), checkpoint_state="failed",
            network_occurred=gate.usage.network_occurred, database_mutated=db_mutated,
            failure=failure,
            current_process_usage=ck.current_process_usage(),
            prior_process_usage=ck.prior_usage(),
            performed_new_work=newly_completed > 0 or gate.usage.transport_starts > 0,
            checkpoint_mutated=True,
            process_count=len(ck.process_usage),
            legacy_provenance=ck.legacy_migrated,
            recovered_identities=tuple(sorted(set(ck.recovered_identities))),
            unresolved_identities=tuple(sorted(
                set(ck.failed_identities) | set(ck.blocked_identities)
                | set(ck.incomplete_identities))),
            initially_completed=len(set(ck.completed_identities)
                                    - set(ck.recovered_identities)))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise  # never swallow an interrupt after recording the resumable state
        return result

    gate.usage.families_completed = tuple(sorted(families_done))
    gate.usage.database_mutated = db_mutated
    _record_usage()
    ck.scratch_fingerprint = fp()
    write_checkpoint(checkpoint_path, ck)

    return PilotResult(
        success=not truncated and not failed,
        truncated=truncated,
        completed=newly_completed,
        skipped=len(completed) - newly_completed if not truncated else gate.usage.skipped_on_resume,
        exhaustion=exhaustion,
        usage=dict(ck.usage),
        checkpoint_state=ck.state,
        network_occurred=gate.usage.network_occurred,
        database_mutated=db_mutated,
        failure=failure,
        current_process_usage=ck.current_process_usage(),
        prior_process_usage=ck.prior_usage(),
        performed_new_work=newly_completed > 0 or gate.usage.transport_starts > 0,
        checkpoint_mutated=True,
        process_count=len(ck.process_usage),
        legacy_provenance=ck.legacy_migrated,
        recovered_identities=tuple(sorted(set(ck.recovered_identities))),
        unresolved_identities=tuple(sorted(
            set(ck.failed_identities) | set(ck.blocked_identities)
            | set(ck.incomplete_identities))),
        initially_completed=len(set(ck.completed_identities)
                                - set(ck.recovered_identities)),    )


def _in_flight_identity(executor: PilotExecutor) -> Optional[str]:
    """The unit the executor was working on, if it tracks that; never inferred."""

    probe = getattr(executor, "in_flight_identity", None)
    if probe is None:
        return None
    try:
        value = probe()
    except Exception:  # noqa: BLE001 - reporting must never mask the real failure
        return None
    return str(value) if value else None


def _assert_sole_writer(
    path: Path, prior_history: list[dict[str, Any]], my_process_id: str
) -> None:
    """Refuse to write when another process has appended to this checkpoint.

    The on-disk history must be exactly the prior history this process read, plus
    at most this process's own entry. Anything else means a second writer ran
    concurrently, and overwriting would silently discard its evidence.
    """

    if not path.is_file():
        return
    try:
        disk = load_checkpoint(path)
    except CheckpointError:
        return  # an unreadable/foreign file is handled by the caller's own checks
    disk_ids = [e.get(PROCESS_ID_KEY) for e in disk.process_usage]
    prior_ids = [e.get(PROCESS_ID_KEY) for e in prior_history]
    if disk_ids[:len(prior_ids)] != prior_ids:
        raise CheckpointError(
            "refusing to write: this checkpoint's earlier process history changed "
            "after it was read (another process wrote concurrently)")
    extra = disk_ids[len(prior_ids):]
    if extra and extra != [my_process_id]:
        raise CheckpointError(
            f"refusing to write: {len(extra)} process entr(y/ies) were appended by "
            "another process after this one started")


def _remaining_identities(
    executor: PilotExecutor, completed: set[str]
) -> Optional[tuple[str, ...]]:
    """Units still outstanding, if the executor can say so with zero transport.

    ``None`` means "cannot be determined offline", which conservatively keeps the
    normal execution path.
    """

    probe = getattr(executor, "remaining_identities", None)
    if probe is None:
        return None
    result = probe(completed=set(completed))
    return None if result is None else tuple(result)


def _no_work_result(
    ck: Checkpoint, *, completed: set[str], manifest: PilotManifest, gate: RequestGate
) -> PilotResult:
    """A completed resume with nothing to do: a true no-op.

    Nothing is fetched, no client is constructed by the caller (no unit runs), the
    database is not written, and -- critically -- the checkpoint file is NOT
    rewritten. The result is synthesized from the evidence already on disk, so the
    logical-run totals a caller sees are the preserved ones rather than a fresh
    report full of zeros.
    """

    logical = ck.logical_usage()
    problems = validate_usage_accounting(
        logical, request_cap=ck.request_cap, credit_cap=ck.credit_cap,
        entries=ck.process_usage if not ck.legacy_migrated else None)
    if problems:
        raise CheckpointError(
            "refusing a no-work resume against inconsistent checkpoint evidence: "
            + "; ".join(problems[:4]))
    # This process did no work at all; its own usage is empty by construction.
    current: dict[str, Any] = {}
    if assert_no_new_transport(current):  # pragma: no cover - defensive
        raise CheckpointError("a no-work resume recorded provider traffic")
    gate.usage.checkpoint_state = "no_work_resume"
    return PilotResult(
        success=True,
        truncated=False,
        completed=0,
        skipped=len(completed),
        exhaustion=logical.get("budget_exhausted"),
        usage=logical,
        checkpoint_state=ck.state,
        network_occurred=False,   # this process reached no network
        database_mutated=False,  # this process wrote no row
        failure=None,
        current_process_usage=current,
        prior_process_usage=logical,
        performed_new_work=False,
        checkpoint_mutated=False,
        process_count=len(ck.process_usage),
        legacy_provenance=ck.legacy_migrated,
        recovered_identities=tuple(sorted(set(ck.recovered_identities))),
        unresolved_identities=tuple(sorted(
            set(ck.failed_identities) | set(ck.blocked_identities)
            | set(ck.incomplete_identities))),
        initially_completed=len(set(ck.completed_identities)
                                - set(ck.recovered_identities)),    )
