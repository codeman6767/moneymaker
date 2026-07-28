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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from ..request_control import BudgetExhausted, RequestGate
from .checkpoint import Checkpoint, load_checkpoint, verify_resume, write_checkpoint
from .manifest import PilotManifest


@dataclass(frozen=True)
class UnitDone:
    """A semantic unit that reached the consistency boundary (persisted+committed)."""

    identity: str
    family: str
    database_mutated: bool = True


class PilotExecutor(Protocol):
    """Performs per-unit fetch+persist, yielding a unit only once it is durable.

    Implementations MUST skip any unit whose ``identity`` is already in
    ``completed`` without issuing a transport call, and MUST persist+commit a
    unit before yielding it.
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
    usage: dict[str, Any]
    checkpoint_state: str
    network_occurred: bool
    database_mutated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "truncated": self.truncated,
            "completed": self.completed,
            "skipped_on_resume": self.skipped,
            "budget_exhausted": self.exhaustion,
            "usage": self.usage,
            "checkpoint_state": self.checkpoint_state,
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
    if resume:
        ck = load_checkpoint(checkpoint_path)
        verify_resume(
            ck, manifest_hash=manifest.manifest_hash(), provider=manifest.provider,
            league=manifest.league, date_range=manifest.date_range, families=manifest.families,
            plan_version=manifest.plan_version, scratch_fingerprint=scratch_fingerprint,
        )
        completed = set(ck.completed_identities)
        ck.code_version = ck.code_version or code_version
    else:
        ck = _new_checkpoint(manifest, scratch_fingerprint=scratch_fingerprint,
                             code_version=code_version)

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

    try:
        for done in executor.iter_units(gate=gate, completed=completed):
            completed.add(done.identity)
            ck.completed_identities = sorted(completed)
            ck.last_boundary = done.identity
            families_done.add(done.family)
            newly_completed += 1
            if done.database_mutated:
                db_mutated = True
            ck.usage = gate.usage.as_dict()
            ck.state = "in_progress"
            ck.scratch_fingerprint = fp()  # current DB fingerprint at this boundary
            write_checkpoint(checkpoint_path, ck)  # atomic, after the boundary
        ck.state = "completed"
        gate.usage.checkpoint_state = "resumed_completed" if resume else "completed"
    except BudgetExhausted as exc:
        truncated = True
        exhaustion = exc.as_dict()
        ck.state = "truncated"
        ck.blocked_identities = sorted(set(ck.blocked_identities) | {exc.blocked_identity})
        gate.usage.checkpoint_state = "truncated"
        gate.usage.families_truncated = tuple(sorted(families_done | {exc.blocked_family}))

    gate.usage.families_completed = tuple(sorted(families_done))
    gate.usage.database_mutated = db_mutated
    ck.usage = gate.usage.as_dict()
    ck.scratch_fingerprint = fp()
    write_checkpoint(checkpoint_path, ck)

    return PilotResult(
        success=not truncated,
        truncated=truncated,
        completed=newly_completed,
        skipped=len(completed) - newly_completed if not truncated else gate.usage.skipped_on_resume,
        exhaustion=exhaustion,
        usage=gate.usage.as_dict(),
        checkpoint_state=ck.state,
        network_occurred=gate.usage.network_occurred,
        database_mutated=db_mutated,
    )
