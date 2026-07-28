"""F1A pilot-runner tests: consistency boundary, resume, truncation (offline).

A gate-aware fake executor stands in for a provider executor so the runner's
checkpoint/resume/truncation logic is proven without the network. The executor
reserves budget through the real :class:`RequestGate` for each non-completed
unit (so truncation is genuine) and yields a unit only after its "persist".
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from sports_quant.ingest.checkpoint import CheckpointError, load_checkpoint
from sports_quant.ingest.manifest import build_manifest
from sports_quant.ingest.pilot import UnitDone, run_pilot
from sports_quant.ingest.planning import Bounds, build_plan
from sports_quant.ingest.tests.f1a_support import known_cost_policy
from sports_quant.request_control import (
    CreditBudget,
    RequestBudget,
    RequestGate,
    RequestUnit,
)


def _manifest(max_pages: int = 4, scratch: str = "data/pilot_scratch.db"):
    plan = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=("games",), stage="skeleton", bounds=Bounds(max_pages=max_pages))
    return build_manifest(plan, scratch_db=scratch, checkpoint_path="data/pilot.ckpt")


def _gate(max_requests: int, max_credits: int = 100) -> RequestGate:
    # Runner-mechanism tests use a TEST metered policy with known costs (the real
    # BALLDONTLIE policy is intentionally unknown/fail-closed and tested elsewhere).
    return RequestGate(
        request_budget=RequestBudget(max_requests=max_requests),
        credit_budget=CreditBudget(applicable=True, max_credits=max_credits),
        cost_policy=known_cost_policy(),
    )


class _FakeExecutor:
    """Yields ``n`` page units; reserves budget for each non-completed unit.

    ``fail_at`` (identity index) raises a generic error AFTER reserving the unit
    but BEFORE yielding it (interruption after transport, before the persist
    boundary). ``fetched`` records which units actually issued a transport.
    """

    def __init__(self, n: int, *, fail_at: int | None = None) -> None:
        self.n = n
        self.fail_at = fail_at
        self.fetched: list[str] = []

    def _unit(self, i: int) -> RequestUnit:
        return RequestUnit(provider="balldontlie", league="nba", endpoint_family="games", page=i + 1)

    def iter_units(self, *, gate: RequestGate, completed: set[str]) -> Iterator[UnitDone]:
        for i in range(self.n):
            unit = self._unit(i)
            ident = unit.identity()
            if ident in completed:
                continue  # resume: proven complete -> no transport
            gate.reserve(unit)  # the real gate; raises BudgetExhausted when over budget
            self.fetched.append(ident)
            if self.fail_at is not None and i == self.fail_at:
                raise RuntimeError("interrupted after transport, before persist")
            yield UnitDone(identity=ident, family="games", database_mutated=True)


def test_full_run_completes(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    ex = _FakeExecutor(4)
    res = run_pilot(manifest=_manifest(), gate=_gate(100), executor=ex,
                    checkpoint_path=ck, scratch_fingerprint="FP")
    assert res.success and not res.truncated
    assert res.completed == 4
    assert load_checkpoint(ck).state == "completed"
    assert len(ex.fetched) == 4


def test_budget_truncation_preserves_completed(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    ex = _FakeExecutor(5)
    res = run_pilot(manifest=_manifest(), gate=_gate(max_requests=2), executor=ex,
                    checkpoint_path=ck, scratch_fingerprint="FP")
    assert res.truncated and not res.success
    assert res.exhaustion is not None and res.exhaustion["limit_type"] == "request"
    loaded = load_checkpoint(ck)
    assert loaded.state == "truncated"
    assert len(loaded.completed_identities) == 2  # first two durable
    assert len(loaded.blocked_identities) == 1
    assert len(ex.fetched) == 2  # third never issued a transport


def test_resume_skips_completed_and_finishes(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    # First run truncates after 2 of 4.
    run_pilot(manifest=_manifest(), gate=_gate(max_requests=2), executor=_FakeExecutor(4),
              checkpoint_path=ck, scratch_fingerprint="FP")
    # Resume with ample budget finishes the rest, skipping the first 2.
    ex2 = _FakeExecutor(4)
    res = run_pilot(manifest=_manifest(), gate=_gate(100), executor=ex2,
                    checkpoint_path=ck, scratch_fingerprint="FP", resume=True)
    assert res.success
    assert len(ex2.fetched) == 2  # only the two previously-incomplete pages
    assert load_checkpoint(ck).state == "completed"


def test_completed_resume_makes_zero_calls(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    run_pilot(manifest=_manifest(), gate=_gate(100), executor=_FakeExecutor(4),
              checkpoint_path=ck, scratch_fingerprint="FP")
    ex = _FakeExecutor(4)
    gate = _gate(100)
    res = run_pilot(manifest=_manifest(), gate=gate, executor=ex,
                    checkpoint_path=ck, scratch_fingerprint="FP", resume=True)
    assert res.success
    assert ex.fetched == []  # nothing re-fetched
    assert gate.usage.attempted_requests == 0
    assert gate.usage.network_occurred is False


def test_interrupted_before_persist_is_retried_on_resume(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    # Unit index 2 raises after transport, before persist -> not checkpointed.
    with pytest.raises(RuntimeError):
        run_pilot(manifest=_manifest(), gate=_gate(100), executor=_FakeExecutor(4, fail_at=2),
                  checkpoint_path=ck, scratch_fingerprint="FP")
    loaded = load_checkpoint(ck)
    assert len(loaded.completed_identities) == 2  # only 0,1 crossed the boundary
    # Resume retries unit 2 (and does 3), idempotently.
    ex2 = _FakeExecutor(4)
    res = run_pilot(manifest=_manifest(), gate=_gate(100), executor=ex2,
                    checkpoint_path=ck, scratch_fingerprint="FP", resume=True)
    assert res.success
    assert len(ex2.fetched) == 2  # units 2 and 3


def test_changed_manifest_cannot_reuse_checkpoint(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    run_pilot(manifest=_manifest(max_pages=4), gate=_gate(max_requests=2),
              executor=_FakeExecutor(4), checkpoint_path=ck, scratch_fingerprint="FP")
    with pytest.raises(CheckpointError):
        run_pilot(manifest=_manifest(max_pages=9), gate=_gate(100),  # different plan/hash
                  executor=_FakeExecutor(9), checkpoint_path=ck, scratch_fingerprint="FP",
                  resume=True)


def test_changed_database_cannot_reuse_checkpoint(tmp_path: Path) -> None:
    ck = tmp_path / "p.ckpt"
    run_pilot(manifest=_manifest(), gate=_gate(max_requests=2), executor=_FakeExecutor(4),
              checkpoint_path=ck, scratch_fingerprint="FP")
    with pytest.raises(CheckpointError):
        run_pilot(manifest=_manifest(), gate=_gate(100), executor=_FakeExecutor(4),
                  checkpoint_path=ck, scratch_fingerprint="DIFFERENT-DB", resume=True)


def test_reports_deterministic_despite_reserve_order(tmp_path: Path) -> None:
    # Same semantic work, run twice; usage totals identical.
    def usage(seed_pages: int) -> dict:
        ck = tmp_path / f"p{seed_pages}.ckpt"
        gate = _gate(100)
        run_pilot(manifest=_manifest(), gate=gate, executor=_FakeExecutor(4),
                  checkpoint_path=ck, scratch_fingerprint="FP")
        u = gate.usage.as_dict()
        u.pop("checkpoint_state", None)
        return u
    assert usage(1) == usage(2)
