"""Logical-run usage provenance: evidence must survive every kind of resume.

The F1 MLB June-2026 review proved that a *completed* ``--resume`` rewrote the
checkpoint and destroyed the earlier process's evidence: ``successful_responses``
1999 -> 0, ``failed_responses`` 2 -> 0, ``retry_attempts`` 7 -> 0,
``throttle_wait_seconds`` ~3407.9 -> 0, ``pages_fetched`` 401 -> 0 and
``families_completed`` -> empty. One harmless-looking resume was enough to erase
the only durable record that the run had contained terminal failures and retries.

These tests pin the repaired accounting: current-process evidence, prior-process
evidence and logical-run totals stay distinct and complete across any number of
processes, and repeated resumes never multiply prior usage.

Everything here is offline; no test sleeps for real.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from sports_quant.ingest.checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    LEGACY_CHECKPOINT_FORMAT_VERSION,
    Checkpoint,
    CheckpointError,
    load_checkpoint,
    write_checkpoint,
)
from sports_quant.ingest.cost_policies import build_balldontlie_policy, build_mlb_policy
from sports_quant.ingest.manifest import build_manifest
from sports_quant.ingest.pilot import PilotResult, UnitDone, run_pilot
from sports_quant.ingest.planning import Bounds, build_plan
from sports_quant.request_control import (
    CreditBudget,
    RequestBudget,
    RequestGate,
    RequestUnit,
    UsageReport,
)
from sports_quant.usage_provenance import (
    USAGE_ACCOUNTING_VERSION,
    USAGE_FIELD_COMBINE,
    Combine,
    UsageProvenanceError,
    combine_usage,
    current_process_entry,
    validate_usage_accounting,
)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _manifest(tmp_path: Path, league: str = "mlb", *, request_cap: int = 50,
              credit_cap: Optional[int] = None) -> Any:
    families = ("schedule", "games") if league == "mlb" else ("schedule", "games")
    plan = build_plan(league=league, from_date="2026-06-01", to_date="2026-06-02",
                      families=families, stage="skeleton",
                      bounds=Bounds(max_games=10, max_retries=1, rate_per_min=30))
    return build_manifest(plan, scratch_db=str(tmp_path / "s.db"),
                          checkpoint_path=str(tmp_path / "s.ckpt"),
                          request_cap=request_cap, credit_cap=credit_cap)


def _gate(league: str = "mlb", *, request_cap: int = 50,
          credit_cap: Optional[int] = None) -> RequestGate:
    return RequestGate(
        request_budget=RequestBudget(max_requests=request_cap),
        credit_budget=CreditBudget(applicable=credit_cap is not None,
                                   max_credits=credit_cap),
        cost_policy=build_mlb_policy() if league == "mlb" else build_balldontlie_policy(),
        sleep=lambda _s: None)  # never a real sleep


class _Executor:
    """Yields the units it is told to, recording synthetic provider evidence.

    ``fail_after`` raises once the given number of units have been yielded, which
    models a process that dies mid-run and leaves the unit incomplete.
    """

    def __init__(self, units: tuple[str, ...], *, family: str = "schedule",
                 successes_per_unit: int = 2, failures: int = 0, retries: int = 0,
                 pages_per_unit: int = 1, fail_after: Optional[int] = None,
                 throttle_wait: float = 0.0, throttles: int = 0,
                 http_429s: int = 0) -> None:
        self._units = units
        self._family = family
        self._succ = successes_per_unit
        self._failures = failures
        self._retries = retries
        self._pages = pages_per_unit
        self._fail_after = fail_after
        self._throttle_wait = throttle_wait
        self._throttles = throttles
        self._429s = http_429s
        self.executed: list[str] = []

    def remaining_identities(self, *, completed: set[str]) -> tuple[str, ...]:
        return tuple(u for u in self._units if u not in completed)

    def _spend(self, gate: RequestGate, unit_id: str) -> None:
        u = gate.usage
        for i in range(self._succ):
            gate.reserve(RequestUnit(provider=u.provider or "mlb_statsapi",
                                     league="mlb", endpoint_family=self._family,
                                     date_key="2026-06-01", entity_key=f"{unit_id}-{i}"))
            u.transport_starts += 1
            u.responses_received += 1
            u.parse_successes += 1
            u.successful_responses += 1
            u.network_occurred = True
        u.pages_fetched += self._pages
        u.throttle_events += self._throttles
        u.throttle_wait_seconds += self._throttle_wait
        u.http_429s += self._429s

    def iter_units(self, *, gate: RequestGate,
                   completed: set[str]) -> Iterator[UnitDone]:
        yielded = 0
        for unit_id in self._units:
            if unit_id in completed:
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                # Provider evidence generated before the crash must be preserved.
                # Every terminal failure and every retry consumed its own transport
                # attempt, so the synthetic evidence satisfies the same accounting
                # invariants a real gated client produces.
                extra = self._failures + self._retries
                gate.usage.transport_starts += extra
                gate.usage.reserved_attempts += extra
                gate.usage.attempted_requests = gate.usage.reserved_attempts
                gate.usage.retry_attempts += self._retries
                gate.usage.failed_responses += self._failures
                gate.usage.families_failed = tuple(sorted(
                    set(gate.usage.families_failed) | {self._family}))
                raise RuntimeError("provider unit failed after partial persistence")
            self._spend(gate, unit_id)
            self.executed.append(unit_id)
            yielded += 1
            yield UnitDone(identity=unit_id, family=self._family,
                           database_mutated=True)


def _run(tmp_path: Path, executor: Any, *, resume: bool, league: str = "mlb",
         request_cap: int = 50, credit_cap: Optional[int] = None,
         gate: Optional[RequestGate] = None) -> PilotResult:
    m = _manifest(tmp_path, league, request_cap=request_cap, credit_cap=credit_cap)
    g = gate or _gate(league, request_cap=request_cap, credit_cap=credit_cap)
    ck_path = tmp_path / "s.ckpt"
    if resume:
        prior = load_checkpoint(ck_path).usage
        g.seed_prior(prior_requests=int(prior.get("reserved_attempts") or 0),
                     prior_credits=int(prior.get("reserved_credits") or 0),
                     prior_transport_starts=int(prior.get("transport_starts") or 0),
                     prior_pages_fetched=int(prior.get("pages_fetched") or 0))
    return run_pilot(manifest=m, gate=g, executor=executor, checkpoint_path=ck_path,
                     scratch_fingerprint="FP", resume=resume, code_version="test")


# --------------------------------------------------------------------------- #
# 1. The reproduced defect
# --------------------------------------------------------------------------- #
def test_completed_resume_no_longer_erases_failure_and_retry_evidence(
    tmp_path: Path,
) -> None:
    """The exact June defect: a completed resume must not zero the evidence.

    Before the repair the second (zero-work) process rewrote ``usage`` with its
    own empty report, so successes, terminal failures, retries, pacing wait,
    pages and family outcomes all became 0/empty.
    """

    ex = _Executor(("u1", "u2"), successes_per_unit=3, pages_per_unit=2,
                   throttles=4, throttle_wait=12.5)
    first = _run(tmp_path, ex, resume=False)
    # Stamp terminal-failure and retry evidence, exactly as the June run held.
    ck = load_checkpoint(tmp_path / "s.ckpt")
    # 6 successes + 2 terminal failures = 8 terminal outcomes; 7 attempts were
    # retried, so 15 transports -- the same shape the June run had.
    ck.process_usage[-1].update({"failed_responses": 2, "retry_attempts": 7,
                                 "transport_starts": 15, "reserved_attempts": 15,
                                 "attempted_requests": 15})
    ck.usage = combine_usage(ck.process_usage)
    write_checkpoint(tmp_path / "s.ckpt", ck)
    before = load_checkpoint(tmp_path / "s.ckpt").usage
    before_bytes = (tmp_path / "s.ckpt").read_bytes()
    assert (before["successful_responses"], before["failed_responses"],
            before["retry_attempts"], before["pages_fetched"]) == (6, 2, 7, 4)

    second = _run(tmp_path, _Executor(("u1", "u2")), resume=True)

    after = load_checkpoint(tmp_path / "s.ckpt").usage
    assert after["successful_responses"] == 6
    assert after["failed_responses"] == 2
    assert after["retry_attempts"] == 7
    assert after["pages_fetched"] == 4
    assert after["throttle_events"] == 8
    assert float(after["throttle_wait_seconds"]) == 25.0
    assert tuple(after["families_completed"]) == ("schedule",)
    assert after["network_occurred"] is True
    # ... and the completed no-work resume did not touch the file at all.
    assert (tmp_path / "s.ckpt").read_bytes() == before_bytes
    assert second.performed_new_work is False
    assert second.checkpoint_mutated is False
    assert second.usage["failed_responses"] == 2
    assert first.usage["successful_responses"] == 6


# --------------------------------------------------------------------------- #
# 2/3/4. The accounting model
# --------------------------------------------------------------------------- #
def test_usage_field_combine_table_covers_every_usage_field() -> None:
    """A new usage field cannot be added without declaring how it composes."""

    declared = set(USAGE_FIELD_COMBINE)
    actual = {f.name for f in dataclasses.fields(UsageReport)}
    assert declared == actual, (
        f"undeclared: {sorted(actual - declared)}; stale: {sorted(declared - actual)}")


def test_additive_invariant_logical_equals_prior_plus_current() -> None:
    a = {"successful_responses": 5, "failed_responses": 1, "transport_starts": 6}
    b = {"successful_responses": 3, "failed_responses": 2, "transport_starts": 5}
    total = combine_usage([a, b])
    for field in ("successful_responses", "failed_responses", "transport_starts"):
        assert total[field] == a[field] + b[field]
    assert total["prior_transport_starts"] == 6
    assert total["transport_starts"] == 11


@pytest.mark.parametrize("rule,field,values,expected", [
    (Combine.ANY, "network_occurred", [True, False], True),
    (Combine.ANY, "database_mutated", [False, False], False),
    (Combine.UNION, "families_completed", [("game",), ("skeleton",)],
     ("game", "skeleton")),
    (Combine.MAX, "games_received", [402, 0], 402),
    (Combine.MAX, "games_selected", [0, 400], 400),
    (Combine.PRECEDENCE, "authentication_status", ["succeeded", "not_applicable"],
     "succeeded"),
    (Combine.PRECEDENCE, "credit_header_status", ["inconsistent", "present"],
     "inconsistent"),
    (Combine.PRECEDENCE, "tier_evidence_source",
     ["bounded_capability_audit", "none"], "bounded_capability_audit"),
    (Combine.ANY_EVIDENCE, "authentication_succeeded", [True, None], True),
    (Combine.ANY_EVIDENCE, "authentication_succeeded", [None, None], None),
    (Combine.LATEST, "skipped_on_resume", [3, 7], 7),
])
def test_non_additive_rules_are_applied(rule: Combine, field: str,
                                        values: list[Any], expected: Any) -> None:
    assert USAGE_FIELD_COMBINE[field] is rule
    assert combine_usage([{field: v} for v in values])[field] == expected


def test_a_budget_exhaustion_is_never_overwritten_by_a_clean_process() -> None:
    """Evidence that the budget ran out must survive a later successful process."""

    exhausted = {"budget_exhausted": {"limit_type": "request", "cap": 10}}
    total = combine_usage([exhausted, {"budget_exhausted": None}])
    assert total["budget_exhausted"] == exhausted["budget_exhausted"]


def test_tier_and_auth_evidence_is_never_upgraded_without_evidence() -> None:
    total = combine_usage([{"tier_status": "configured_not_verified:goat",
                            "tier_verified": False},
                           {"tier_status": "unknown", "tier_verified": False}])
    assert total["tier_status"] == "configured_not_verified:goat"
    assert total["tier_verified"] is False
    # A real audit upgrades it; a no-op process never does.
    assert combine_usage([{"tier_verified": False}, {"tier_verified": True}
                          ])["tier_verified"] is True


def test_processes_that_disagree_on_plan_identity_fail_closed() -> None:
    with pytest.raises(UsageProvenanceError) as exc:
        combine_usage([{"manifest_hash": "aaa"}, {"manifest_hash": "bbb"}])
    assert "manifest_hash" in str(exc.value)
    # A field the other process never populated asserts nothing.
    assert combine_usage([{"manifest_hash": "aaa"}, {"manifest_hash": ""}
                          ])["manifest_hash"] == "aaa"


def test_current_process_entry_removes_the_seeded_prior_precharge() -> None:
    """Without this, a third process would count the first one's attempts twice."""

    usage = {"reserved_attempts": 30, "attempted_requests": 30, "prior_requests": 20,
             "reserved_credits": 8, "prior_credits": 5, "transport_starts": 10}
    entry = current_process_entry(usage)
    assert entry["reserved_attempts"] == 10
    assert entry["attempted_requests"] == 10
    assert entry["reserved_credits"] == 3
    assert entry["transport_starts"] == 10        # never pre-charged
    assert "prior_requests" not in entry          # derived, never stored


def test_repeated_resumes_never_multiply_prior_usage(tmp_path: Path) -> None:
    ex = _Executor(("u1", "u2", "u3"), successes_per_unit=1, pages_per_unit=1)
    _run(tmp_path, ex, resume=False)
    baseline = load_checkpoint(tmp_path / "s.ckpt").usage
    for _ in range(4):
        _run(tmp_path, _Executor(("u1", "u2", "u3")), resume=True)
        again = load_checkpoint(tmp_path / "s.ckpt").usage
        assert again == baseline, "a repeated resume changed the logical totals"


# --------------------------------------------------------------------------- #
# 5. Completed no-work resume is a true no-op
# --------------------------------------------------------------------------- #
def test_completed_no_work_resume_is_byte_identical_and_does_no_work(
    tmp_path: Path,
) -> None:
    ex = _Executor(("u1",), successes_per_unit=2)
    _run(tmp_path, ex, resume=False)
    before = (tmp_path / "s.ckpt").read_bytes()

    ex2 = _Executor(("u1",), successes_per_unit=2)
    result = _run(tmp_path, ex2, resume=True)

    assert (tmp_path / "s.ckpt").read_bytes() == before
    assert ex2.executed == []                       # no unit ran
    assert result.success and not result.truncated
    assert result.performed_new_work is False
    assert result.checkpoint_mutated is False
    assert result.completed == 0 and result.skipped == 1
    assert result.current_process_usage == {}       # this process did nothing
    assert result.network_occurred is False         # ... reached no network
    assert result.database_mutated is False         # ... wrote no row
    assert result.usage["successful_responses"] == 2  # preserved logical evidence
    assert result.usage["network_occurred"] is True   # the run DID fetch, earlier


def test_no_work_resume_json_shows_totals_beside_its_own_zeros(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _Executor(("u1",), successes_per_unit=2), resume=False)
    payload = _run(tmp_path, _Executor(("u1",)), resume=True).as_dict()
    assert payload["performed_new_work"] is False
    assert payload["checkpoint_mutated"] is False
    assert payload["current_process_usage"] == {}
    assert payload["usage"]["successful_responses"] == 2
    assert payload["prior_process_usage"]["successful_responses"] == 2
    assert json.loads(json.dumps(payload, default=str))  # JSON serializable


def test_a_completed_resume_with_work_left_is_not_a_no_op(tmp_path: Path) -> None:
    """Only a checkpoint with genuinely nothing outstanding short-circuits."""

    ex = _Executor(("u1", "u2"), successes_per_unit=1, fail_after=1)
    failed = _run(tmp_path, ex, resume=False)
    assert failed.failure is not None
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.state == "failed"
    ex2 = _Executor(("u1", "u2"), successes_per_unit=1)
    result = _run(tmp_path, ex2, resume=True)
    assert ex2.executed == ["u2"]
    assert result.performed_new_work is True
    assert result.checkpoint_mutated is True


# --------------------------------------------------------------------------- #
# 6. Three processes: partial -> failed resume -> successful resume
# --------------------------------------------------------------------------- #
def test_three_process_logical_accounting(tmp_path: Path) -> None:
    """Initial partial run, a failed resume, then a successful resume.

    Each process's evidence is added exactly once, prior usage keeps counting
    against the manifest cap, and the final completed state still carries the
    first two processes' failures and retries.
    """

    # Process 1: completes u1, then dies on u2 (2 failures, 1 retry).
    p1 = _Executor(("u1", "u2", "u3"), successes_per_unit=2, pages_per_unit=1,
                   failures=2, retries=1, fail_after=1, throttles=1,
                   throttle_wait=2.0)
    failed = _run(tmp_path, p1, resume=False)
    assert failed.failure is not None and not failed.success
    ck1 = load_checkpoint(tmp_path / "s.ckpt")
    assert ck1.state == "failed"
    assert ck1.completed_identities == ["u1"]
    assert ck1.usage["successful_responses"] == 2
    assert ck1.usage["failed_responses"] == 2
    assert ck1.usage["retry_attempts"] == 1
    assert len(ck1.process_usage) == 1

    # Process 2: resumes, completes u2, then dies on u3 (1 more failure, 2 retries).
    p2 = _Executor(("u1", "u2", "u3"), successes_per_unit=2, pages_per_unit=1,
                   failures=1, retries=2, fail_after=1, throttles=1,
                   throttle_wait=3.0)
    failed2 = _run(tmp_path, p2, resume=True)
    assert failed2.failure is not None
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert p2.executed == ["u2"]
    assert ck2.state == "failed"
    assert sorted(ck2.completed_identities) == ["u1", "u2"]
    assert len(ck2.process_usage) == 2
    # Both processes' evidence, added exactly once each.
    assert ck2.usage["successful_responses"] == 4
    assert ck2.usage["failed_responses"] == 3
    assert ck2.usage["retry_attempts"] == 3
    assert ck2.usage["pages_fetched"] == 2
    assert float(ck2.usage["throttle_wait_seconds"]) == 5.0
    # 4 successes + 3 failures + 3 retried attempts, charged against ONE cap.
    assert ck2.usage["reserved_attempts"] == 10
    assert ck2.prior_usage()["successful_responses"] == 2      # process 1 only
    assert ck2.current_process_usage()["successful_responses"] == 2

    # Process 3: resumes and finishes u3 cleanly.
    p3 = _Executor(("u1", "u2", "u3"), successes_per_unit=2, pages_per_unit=1)
    result = _run(tmp_path, p3, resume=True)
    ck3 = load_checkpoint(tmp_path / "s.ckpt")
    assert p3.executed == ["u3"]
    assert result.success and ck3.state == "completed"
    assert len(ck3.process_usage) == 3
    # A successful resume does NOT erase that two earlier processes failed.
    assert ck3.usage["failed_responses"] == 3
    assert ck3.usage["retry_attempts"] == 3
    assert ck3.usage["successful_responses"] == 6
    assert ck3.usage["pages_fetched"] == 3
    assert float(ck3.usage["throttle_wait_seconds"]) == 5.0
    assert tuple(ck3.usage["families_failed"]) == ("schedule",)
    assert result.current_process_usage["failed_responses"] == 0
    assert result.prior_process_usage["failed_responses"] == 3
    # Prior attempts keep counting against the ONE manifest cap.
    assert ck3.usage["reserved_attempts"] == 12
    # Every invariant closes across all three processes, retry identity included.
    assert validate_usage_accounting(
        ck3.usage, request_cap=50, entries=ck3.process_usage,
        require_retry_identity=True) == []


def test_prior_reserved_usage_keeps_counting_against_the_manifest_cap(
    tmp_path: Path,
) -> None:
    """A resume must not get a fresh budget: the cap spans the logical run."""

    ex = _Executor(("u1", "u2"), successes_per_unit=2)
    _run(tmp_path, ex, resume=False, request_cap=6)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.usage["reserved_attempts"] == 4

    g = _gate(request_cap=6)
    prior = ck.usage
    g.seed_prior(prior_requests=int(prior["reserved_attempts"]), prior_credits=0,
                 prior_transport_starts=int(prior["transport_starts"]),
                 prior_pages_fetched=int(prior["pages_fetched"]))
    assert g.usage.reserved_attempts == 4      # pre-charged, not reset
    # Only 2 of the cap's 6 slots are left, so a 3-request unit must truncate
    # rather than being handed a fresh budget.
    ex2 = _Executor(("u1", "u2", "u3"), successes_per_unit=3)
    m = _manifest(tmp_path, request_cap=6)
    result = run_pilot(manifest=m, gate=g, executor=ex2,
                       checkpoint_path=tmp_path / "s.ckpt", scratch_fingerprint="FP",
                       resume=True, code_version="test")
    assert result.truncated is True
    assert result.exhaustion is not None
    assert result.exhaustion["limit_type"] == "request"
    after = load_checkpoint(tmp_path / "s.ckpt").usage
    # The first process's evidence survived the truncated second process, and the
    # truncation itself is now part of the logical-run evidence.
    assert after["successful_responses"] >= 4
    assert after["budget_exhausted"] is not None
    assert after["reserved_attempts"] <= 6


def test_prior_credits_do_not_receive_a_fresh_budget(tmp_path: Path) -> None:
    g = _gate("nba", request_cap=50, credit_cap=100)
    g.seed_prior(prior_requests=3, prior_credits=60, prior_transport_starts=3,
                 prior_pages_fetched=1)
    assert g.usage.reserved_credits == 60
    assert g.usage.prior_credits == 60
    entry = current_process_entry(g.usage.as_dict())
    assert entry["reserved_credits"] == 0   # none of it belongs to this process


# --------------------------------------------------------------------------- #
# 7. Partial-unit semantics stay repaired
# --------------------------------------------------------------------------- #
def test_partial_unit_stays_resumable_and_history_keeps_both_outcomes(
    tmp_path: Path,
) -> None:
    """A unit that persists some families and terminally fails another.

    The newly repaired ``partially_failed`` behaviour must not be weakened: the
    checkpoint stays failed/resumable, earlier units stay complete, the incomplete
    unit is retried, and the logical run remembers the first failure alongside the
    later success.
    """

    p1 = _Executor(("u1", "u2"), successes_per_unit=2, failures=2, retries=2,
                   fail_after=1)
    failed = _run(tmp_path, p1, resume=False)
    assert failed.failure is not None and not failed.success
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.state == "failed"                      # not completed
    assert ck.completed_identities == ["u1"]         # the durable unit stays done
    assert ck.usage["failed_responses"] == 2

    p2 = _Executor(("u1", "u2"), successes_per_unit=2)
    result = _run(tmp_path, p2, resume=True)
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert p2.executed == ["u2"]                     # only the incomplete unit
    assert ck2.state == "completed"
    assert ck2.usage["failed_responses"] == 2        # first failure remembered
    assert ck2.usage["successful_responses"] == 4
    # Completion does not claim every process individually succeeded.
    assert tuple(ck2.usage["families_failed"]) == ("schedule",)
    assert result.process_count == 2


def test_completed_state_cannot_coexist_with_an_unresolved_unit(
    tmp_path: Path,
) -> None:
    ck = Checkpoint(
        manifest_hash="h", plan_version="p", provider="mlb_statsapi", league="mlb",
        date_range="2026-06-01..2026-06-02", families=("schedule",),
        scratch_db="s.db", scratch_fingerprint="FP", schema_version=17,
        request_cap=10, credit_cap=None, state="completed",
        completed_identities=["u1"], incomplete_identities=["u2"])
    path = tmp_path / "bad.ckpt"
    write_checkpoint(path, ck)
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "unresolved" in str(exc.value)


def test_a_recovered_unit_leaves_the_unresolved_sets_but_keeps_its_history(
    tmp_path: Path,
) -> None:
    p1 = _Executor(("u1", "u2"), successes_per_unit=1, fail_after=1)
    failed = _run(tmp_path, p1, resume=False)
    assert failed.failure is not None and not failed.success
    ck = load_checkpoint(tmp_path / "s.ckpt")
    ck.incomplete_identities = ["u2"]
    ck.state = "failed"
    write_checkpoint(tmp_path / "s.ckpt", ck)

    _run(tmp_path, _Executor(("u1", "u2"), successes_per_unit=1), resume=True)
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert ck2.state == "completed"
    assert ck2.incomplete_identities == []          # genuinely resolved now
    assert ck2.recovered_identities == ["u2"]       # ... and remembered as recovered


# --------------------------------------------------------------------------- #
# 8. Backward compatibility with v1 checkpoints
# --------------------------------------------------------------------------- #
def _v1_body(usage: dict[str, Any], *, state: str = "completed") -> dict[str, Any]:
    return {
        "checkpoint_format_version": LEGACY_CHECKPOINT_FORMAT_VERSION,
        "plan_version": "p", "manifest_hash": "h", "code_version": "old",
        "provider": "mlb_statsapi", "league": "mlb",
        "date_range": "2026-06-01..2026-06-02", "families": ["schedule"],
        "scratch_db": "s.db", "scratch_fingerprint": "FP", "schema_version": 17,
        "request_cap": 6000, "credit_cap": None,
        "completed_identities": ["u1"], "failed_identities": [],
        "blocked_identities": [], "incomplete_identities": [],
        "stage_game_ids": [], "usage": usage, "last_boundary": "u1", "state": state,
    }


def test_a_legacy_v1_checkpoint_loads_and_keeps_all_evidence_present(
    tmp_path: Path,
) -> None:
    usage = {"successful_responses": 1999, "failed_responses": 2, "retry_attempts": 7,
             "transport_starts": 2008, "reserved_attempts": 2008,
             "attempted_requests": 2008, "pages_fetched": 401,
             "throttle_events": 1999, "throttle_wait_seconds": 3407.88,
             "families_completed": ["game", "skeleton"], "network_occurred": True,
             "responses_received": 2001, "parse_successes": 1999}
    path = tmp_path / "legacy.ckpt"
    path.write_text(json.dumps(_v1_body(usage)), encoding="utf-8")

    ck = load_checkpoint(path)
    assert ck.checkpoint_format_version == LEGACY_CHECKPOINT_FORMAT_VERSION
    assert ck.legacy_migrated is True
    assert ck.process_count_known is False       # the split is unknowable
    assert len(ck.process_usage) == 1
    assert ck.logical_usage()["successful_responses"] == 1999
    assert ck.logical_usage()["failed_responses"] == 2
    assert ck.logical_usage()["retry_attempts"] == 7


def test_a_legacy_checkpoint_never_invents_a_missing_counter(tmp_path: Path) -> None:
    """An absent field stays absent; it does not become a misleading zero."""

    path = tmp_path / "sparse.ckpt"
    path.write_text(json.dumps(_v1_body({"successful_responses": 5})),
                    encoding="utf-8")
    ck = load_checkpoint(path)
    entry = ck.process_usage[0]
    assert entry["successful_responses"] == 5
    assert "failed_responses" not in entry
    assert "retry_attempts" not in entry
    assert "http_429s" not in entry


def test_a_legacy_completed_resume_preserves_the_evidence_it_has(
    tmp_path: Path,
) -> None:
    usage = {"successful_responses": 9, "failed_responses": 1, "retry_attempts": 3,
             "transport_starts": 13, "reserved_attempts": 13,
             "attempted_requests": 13, "pages_fetched": 2, "network_occurred": True,
             "responses_received": 10, "parse_successes": 9}
    m = _manifest(tmp_path)
    body = _v1_body(usage)
    body["manifest_hash"] = m.manifest_hash()
    body["plan_version"] = m.plan_version
    body["families"] = list(m.families)
    body["date_range"] = m.date_range
    body["completed_identities"] = ["u1"]
    (tmp_path / "s.ckpt").write_text(json.dumps(body), encoding="utf-8")
    before = (tmp_path / "s.ckpt").read_bytes()

    result = _run(tmp_path, _Executor(("u1",)), resume=True)

    assert result.performed_new_work is False
    assert result.checkpoint_mutated is False
    assert (tmp_path / "s.ckpt").read_bytes() == before   # v1 file left untouched
    assert result.usage["successful_responses"] == 9
    assert result.usage["failed_responses"] == 1
    assert result.usage["retry_attempts"] == 3
    assert result.legacy_provenance is True


def test_a_legacy_checkpoint_upgrades_to_v2_only_when_a_resume_does_work(
    tmp_path: Path,
) -> None:
    m = _manifest(tmp_path)
    body = _v1_body({"successful_responses": 2, "transport_starts": 2,
                     "reserved_attempts": 2, "attempted_requests": 2,
                     "responses_received": 2, "parse_successes": 2,
                     "network_occurred": True}, state="failed")
    body.update({"manifest_hash": m.manifest_hash(), "plan_version": m.plan_version,
                 "families": list(m.families), "date_range": m.date_range,
                 "completed_identities": ["u1"]})
    (tmp_path / "s.ckpt").write_text(json.dumps(body), encoding="utf-8")

    _run(tmp_path, _Executor(("u1", "u2"), successes_per_unit=2), resume=True)

    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.checkpoint_format_version == CHECKPOINT_FORMAT_VERSION
    assert ck.usage["successful_responses"] == 4      # 2 legacy + 2 new
    assert len(ck.process_usage) == 2
    # The legacy flag does not survive an upgrade that added a real process entry,
    # but the legacy evidence itself does.
    assert ck.process_usage[0]["successful_responses"] == 2


def test_a_v1_file_carrying_a_provenance_block_is_refused(tmp_path: Path) -> None:
    body = _v1_body({"successful_responses": 1})
    body["usage_provenance"] = {"accounting_version": USAGE_ACCOUNTING_VERSION,
                                "processes": []}
    path = tmp_path / "mixed.ckpt"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "contradictory" in str(exc.value)


def test_an_unknown_accounting_version_fails_closed(tmp_path: Path) -> None:
    ck = Checkpoint(
        manifest_hash="h", plan_version="p", provider="mlb_statsapi", league="mlb",
        date_range="r", families=("schedule",), scratch_db="s.db",
        scratch_fingerprint="FP", schema_version=17, request_cap=10, credit_cap=None)
    path = tmp_path / "future.ckpt"
    write_checkpoint(path, ck)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage_provenance"]["accounting_version"] = "f1-usage-provenance-v99"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "accounting version" in str(exc.value)


def test_a_checkpoint_whose_totals_contradict_its_history_is_refused(
    tmp_path: Path,
) -> None:
    ex = _Executor(("u1",), successes_per_unit=2)
    _run(tmp_path, ex, resume=False)
    path = tmp_path / "s.ckpt"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage"]["successful_responses"] = 999      # totals now lie
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "inconsistent" in str(exc.value)


def test_a_declared_process_count_must_match_the_recorded_history(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _Executor(("u1",)), resume=False)
    path = tmp_path / "s.ckpt"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage_provenance"]["process_count"] = 7
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(path)


# --------------------------------------------------------------------------- #
# 9. Accounting invariants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("usage,needle", [
    ({"reserved_attempts": 1, "attempted_requests": 1, "transport_starts": 5,
      "network_occurred": True}, "reserved_attempts 1 < transport_starts 5"),
    ({"reserved_attempts": 9, "attempted_requests": 9, "transport_starts": 2,
      "successful_responses": 3, "network_occurred": True},
     "transport_starts 2 < terminal outcomes 3"),
    ({"reserved_attempts": 9, "attempted_requests": 4}, "aliases"),
    ({"successful_responses": -1}, "negative"),
    ({"reserved_attempts": 5, "attempted_requests": 5, "transport_starts": 5,
      "successful_responses": 1, "pages_fetched": 4, "network_occurred": True},
     "pages_fetched 4 exceeds successful_responses 1"),
    ({"reserved_attempts": 2, "attempted_requests": 2, "transport_starts": 2,
      "http_429s": 5, "successful_responses": 2, "network_occurred": True},
     "http_429s 5 exceeds transport_starts 2"),
    ({"reserved_attempts": 2, "attempted_requests": 2, "transport_starts": 2,
      "successful_responses": 2, "network_occurred": False},
     "network_occurred is false while transport_starts is 2"),
])
def test_accounting_invariants_are_enforced(usage: dict[str, Any],
                                            needle: str) -> None:
    problems = validate_usage_accounting(usage, require_retry_identity=False)
    assert any(needle in p for p in problems), problems


def test_the_retry_identity_holds_for_the_june_totals() -> None:
    june = {"reserved_attempts": 2008, "attempted_requests": 2008,
            "transport_starts": 2008, "successful_responses": 1999,
            "failed_responses": 2, "retry_attempts": 7, "pages_fetched": 401,
            "responses_received": 2001, "parse_successes": 1999,
            "http_429s": 0, "blocked_requests": 0, "network_occurred": True,
            "checkpoint_state": "completed"}
    assert validate_usage_accounting(june, request_cap=6002) == []


def test_the_retry_identity_is_not_applied_to_an_unsettled_process() -> None:
    """A truncated process may hold an attempt that never reached an outcome."""

    unsettled = {"reserved_attempts": 5, "attempted_requests": 5,
                 "transport_starts": 5, "successful_responses": 3,
                 "failed_responses": 0, "retry_attempts": 0,
                 "responses_received": 3, "parse_successes": 3,
                 "network_occurred": True, "checkpoint_state": "truncated"}
    assert validate_usage_accounting(unsettled) == []
    assert validate_usage_accounting(unsettled, require_retry_identity=True)


def test_the_logical_cap_is_enforced_across_processes() -> None:
    problems = validate_usage_accounting(
        {"reserved_attempts": 12, "attempted_requests": 12, "transport_starts": 10,
         "successful_responses": 10, "responses_received": 10,
         "parse_successes": 10, "network_occurred": True}, request_cap=10)
    assert any("exceeds the manifest request cap" in p for p in problems)


def test_a_zero_work_resume_adds_no_transport_evidence() -> None:
    from sports_quant.usage_provenance import assert_no_new_transport

    assert assert_no_new_transport({}) == []
    assert assert_no_new_transport({"transport_starts": 1})
    assert assert_no_new_transport({"network_occurred": True})


# --------------------------------------------------------------------------- #
# 11. NBA non-regression
# --------------------------------------------------------------------------- #
def test_nba_credit_and_rate_evidence_survives_a_completed_resume(
    tmp_path: Path,
) -> None:
    g = _gate("nba", request_cap=50)
    g.usage.credits_applicable = True
    g.usage.rate_policy_active = True
    g.usage.configured_rate_per_min = 600
    g.usage.provider_rate_limit_per_min = 600
    g.usage.tier_status = "configured_not_verified:goat"
    g.usage.authentication_status = "succeeded"
    g.usage.authentication_succeeded = True
    g.usage.reported_credits_consumed = 4
    ex = _Executor(("u1",), successes_per_unit=3, pages_per_unit=3, family="games")
    _run(tmp_path, ex, resume=False, league="nba", request_cap=50, gate=g)
    before = (tmp_path / "s.ckpt").read_bytes()

    result = _run(tmp_path, _Executor(("u1",), family="games"), resume=True,
                  league="nba", request_cap=50)

    assert (tmp_path / "s.ckpt").read_bytes() == before
    u = result.usage
    assert u["pages_fetched"] == 3                    # pagination total survives
    assert u["credits_applicable"] is True
    assert u["configured_rate_per_min"] == 600
    assert u["provider_rate_limit_per_min"] == 600
    assert u["tier_status"] == "configured_not_verified:goat"
    assert u["authentication_status"] == "succeeded"
    assert u["reported_credits_consumed"] == 4
    assert result.performed_new_work is False


def test_nba_partial_unit_remains_resumable(tmp_path: Path) -> None:
    p1 = _Executor(("u1", "u2"), successes_per_unit=1, failures=1, retries=1,
                   fail_after=1, family="games")
    failed = _run(tmp_path, p1, resume=False, league="nba")
    assert failed.failure is not None and not failed.success
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.state == "failed" and ck.completed_identities == ["u1"]

    p2 = _Executor(("u1", "u2"), successes_per_unit=1, family="games")
    _run(tmp_path, p2, resume=True, league="nba")
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert p2.executed == ["u2"]
    assert ck2.state == "completed"
    assert ck2.usage["failed_responses"] == 1     # the NBA failure is remembered


def test_reported_credits_compose_without_inventing_a_zero() -> None:
    assert combine_usage([{"reported_credits_consumed": None},
                          {"reported_credits_consumed": None}
                          ])["reported_credits_consumed"] is None
    assert combine_usage([{"reported_credits_consumed": 3},
                          {"reported_credits_consumed": None}
                          ])["reported_credits_consumed"] == 3
    assert combine_usage([{"reported_credits_consumed": 3},
                          {"reported_credits_consumed": 4}
                          ])["reported_credits_consumed"] == 7
    # A point-in-time balance is never summed.
    assert combine_usage([{"provider_credits_remaining": 90},
                          {"provider_credits_remaining": 80}
                          ])["provider_credits_remaining"] == 80


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_the_provenance_block_carries_no_unknown_or_secret_key(
    tmp_path: Path,
) -> None:
    SENTINEL = "sk-live-do-not-log"
    _run(tmp_path, _Executor(("u1",)), resume=False)
    path = tmp_path / "s.ckpt"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage_provenance"]["processes"][0]["api_key"] = SENTINEL
    path.write_text(json.dumps(body), encoding="utf-8")

    ck = load_checkpoint(path)
    assert "api_key" not in ck.process_usage[0]     # unknown keys dropped
    assert SENTINEL not in json.dumps(ck.body(), default=str)
    assert SENTINEL not in json.dumps(ck.logical_usage(), default=str)


def test_a_non_object_process_entry_is_refused(tmp_path: Path) -> None:
    _run(tmp_path, _Executor(("u1",)), resume=False)
    path = tmp_path / "s.ckpt"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage_provenance"]["processes"] = ["not-an-object"]
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(path)





