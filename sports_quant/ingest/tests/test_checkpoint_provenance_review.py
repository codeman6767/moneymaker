"""Independent-review regressions for checkpoint / resume provenance.

Each test below reproduced a defect the review of ``bd0903f`` found:

1. Usage values arriving from an untrusted checkpoint were accepted without any
   type or range validation, so a string / bool / dict / negative / NaN counter
   loaded silently and later surfaced as a bare ``TypeError`` from inside the
   combiner (or corrupted a total, since ``True`` counts as 1 in Python).
2. A LEGACY v1 checkpoint skipped accounting validation entirely, so impossible
   evidence -- more terminal outcomes than transports, pages beyond successful
   responses, usage above the manifest cap -- loaded silently.
3. Only ADDITIVE totals were checked against the recorded history, so a tampered
   non-additive total (``families_completed``, ``network_occurred``, selection
   counts) went undetected.
4. Unit sets were not checked for contradiction: one identity could be both
   completed and failed, and ``recovered_identities`` could name a unit that was
   never completed or that was still unresolved.
5. Process entries carried no per-invocation identifier, so nothing could
   correlate an entry with a run or detect a concurrent writer's clobber.
6. ``PilotResult`` exposed no recovered-identity information, so a consumer could
   not tell a first-time completion from a recovery after an earlier failure.

An absent counter must stay UNKNOWN rather than becoming a misleading zero -- the
first attempt at repair 2 broke exactly that, and the sparse-legacy tests pin it.

Everything here is offline and nothing sleeps for real.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
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
    LEGACY_PROCESS_ID,
    PROCESS_ID_KEY,
    USAGE_FIELD_COMBINE,
    Combine,
    UsageProvenanceError,
    combine_usage,
    new_process_id,
    sanitized_process_entries,
    sanitized_usage,
    usage_type_hints,
    validate_usage_accounting,
)

# A self-consistent single-process report: 4 attempts, 4 transports, 4 successes.
CONSISTENT = {
    "reserved_attempts": 4, "attempted_requests": 4, "transport_starts": 4,
    "successful_responses": 4, "failed_responses": 0, "retry_attempts": 0,
    "responses_received": 4, "parse_successes": 4, "pages_fetched": 1,
    "network_occurred": True, "checkpoint_state": "completed",
    "families_completed": ("schedule",),
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _manifest(tmp: Path, league: str = "mlb", *, request_cap: int = 200,
              credit_cap: Optional[int] = None) -> Any:
    plan = build_plan(league=league, from_date="2026-06-01", to_date="2026-06-02",
                      families=("schedule", "games"), stage="skeleton",
                      bounds=Bounds(max_games=20, max_retries=1, rate_per_min=30))
    return build_manifest(plan, scratch_db=str(tmp / "s.db"),
                          checkpoint_path=str(tmp / "s.ckpt"),
                          request_cap=request_cap, credit_cap=credit_cap)


def _gate(league: str = "mlb", *, request_cap: int = 200,
          credit_cap: Optional[int] = None) -> RequestGate:
    return RequestGate(
        request_budget=RequestBudget(max_requests=request_cap),
        credit_budget=CreditBudget(applicable=credit_cap is not None,
                                   max_credits=credit_cap),
        cost_policy=build_mlb_policy() if league == "mlb" else build_balldontlie_policy(),
        sleep=lambda _s: None)  # never a real sleep


class _Executor:
    """Yields units, recording internally consistent synthetic provider evidence."""

    def __init__(self, units: tuple[str, ...], *, family: str = "schedule",
                 succ: int = 1, failures: int = 0, retries: int = 0,
                 pages: int = 0, fail_after: Optional[int] = None,
                 throttles: int = 0, wait: float = 0.0, http_429s: int = 0,
                 blocked_family: Optional[str] = None) -> None:
        self.units = units
        self.family = family
        self.succ = succ
        self.failures = failures
        self.retries = retries
        self.pages = pages
        self.fail_after = fail_after
        self.throttles = throttles
        self.wait = wait
        self.http_429s = http_429s
        self.blocked_family = blocked_family
        self.executed: list[str] = []

    def remaining_identities(self, *, completed: set[str]) -> tuple[str, ...]:
        return tuple(u for u in self.units if u not in completed)

    def iter_units(self, *, gate: RequestGate,
                   completed: set[str]) -> Iterator[UnitDone]:
        yielded = 0
        for unit in self.units:
            if unit in completed:
                continue
            if self.fail_after is not None and yielded >= self.fail_after:
                extra = self.failures + self.retries
                gate.usage.transport_starts += extra
                gate.usage.reserved_attempts += extra
                gate.usage.attempted_requests = gate.usage.reserved_attempts
                gate.usage.retry_attempts += self.retries
                gate.usage.failed_responses += self.failures
                gate.usage.families_failed = tuple(sorted(
                    set(gate.usage.families_failed) | {self.family}))
                raise RuntimeError("unit failed after partial persistence")
            if self.blocked_family is not None:
                gate.reserve(RequestUnit(provider="mlb_statsapi", league="mlb",
                                         endpoint_family=self.blocked_family,
                                         date_key="2026-06-01", entity_key=unit))
            for i in range(self.succ):
                gate.reserve(RequestUnit(provider="mlb_statsapi", league="mlb",
                                         endpoint_family=self.family,
                                         date_key="2026-06-01",
                                         entity_key=f"{unit}-{i}"))
                gate.usage.transport_starts += 1
                gate.usage.responses_received += 1
                gate.usage.parse_successes += 1
                gate.usage.successful_responses += 1
                gate.usage.network_occurred = True
            gate.usage.pages_fetched += min(self.pages, self.succ)
            gate.usage.throttle_events += self.throttles
            gate.usage.throttle_wait_seconds += self.wait
            gate.usage.http_429s += self.http_429s
            self.executed.append(unit)
            yielded += 1
            yield UnitDone(identity=unit, family=self.family, database_mutated=True)


def _run(tmp: Path, ex: Any, *, resume: bool, league: str = "mlb",
         request_cap: int = 200, credit_cap: Optional[int] = None,
         gate: Optional[RequestGate] = None) -> PilotResult:
    g = gate or _gate(league, request_cap=request_cap, credit_cap=credit_cap)
    ck = tmp / "s.ckpt"
    if resume:
        prior = load_checkpoint(ck).usage
        g.seed_prior(prior_requests=int(prior.get("reserved_attempts") or 0),
                     prior_credits=int(prior.get("reserved_credits") or 0),
                     prior_transport_starts=int(prior.get("transport_starts") or 0),
                     prior_pages_fetched=int(prior.get("pages_fetched") or 0))
    return run_pilot(manifest=_manifest(tmp, league, request_cap=request_cap,
                                        credit_cap=credit_cap),
                     gate=g, executor=ex, checkpoint_path=ck,
                     scratch_fingerprint="FP", resume=resume, code_version="rev")


def _v1_body(usage: dict[str, Any], **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "checkpoint_format_version": LEGACY_CHECKPOINT_FORMAT_VERSION,
        "plan_version": "p", "manifest_hash": "h", "provider": "mlb_statsapi",
        "league": "mlb", "date_range": "r", "families": ["schedule"],
        "scratch_db": "s.db", "scratch_fingerprint": "FP", "schema_version": 17,
        "request_cap": 50, "credit_cap": None, "completed_identities": ["u1"],
        "failed_identities": [], "blocked_identities": [],
        "incomplete_identities": [], "stage_game_ids": [],
        "usage": usage, "last_boundary": "u1", "state": "completed",
    }
    body.update(over)
    return body


def _write_json(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _v2_file(tmp: Path, usage: dict[str, Any], processes: list[dict[str, Any]],
             **over: Any) -> Path:
    ck = Checkpoint(
        manifest_hash="h", plan_version="p", provider="mlb_statsapi", league="mlb",
        date_range="r", families=("schedule",), scratch_db="s.db",
        scratch_fingerprint="FP", schema_version=17, request_cap=50, credit_cap=None,
        completed_identities=["u1"], usage=usage, process_usage=processes,
        state="completed")
    path = tmp / "v2.ckpt"
    write_checkpoint(path, ck)
    body = json.loads(path.read_text(encoding="utf-8"))
    for key, value in over.items():
        if "." in key:
            outer, inner = key.split(".", 1)
            body[outer][inner] = value
        else:
            body[key] = value
    return _write_json(path, body)


# --------------------------------------------------------------------------- #
# 1. Untrusted value types and ranges
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field,value,needle", [
    ("successful_responses", "1999", "must be an integer"),
    ("successful_responses", True, "must be an integer"),
    ("successful_responses", 3.5, "must be an integer"),
    ("pages_fetched", {"a": 1}, "must be an integer"),
    ("pages_fetched", ["a"], "must be an integer"),
    ("failed_responses", -5, "may not be negative"),
    ("throttle_wait_seconds", float("nan"), "must be finite"),
    ("throttle_wait_seconds", float("inf"), "must be finite"),
    ("throttle_wait_seconds", -1.0, "may not be negative"),
    ("throttle_wait_seconds", "slow", "must be a number"),
    ("network_occurred", 1, "must be a boolean"),
    ("families_completed", "schedule", "must be a list of names"),
    ("families_completed", [1, 2], "only names"),
    ("checkpoint_state", 7, "must be a string"),
    ("budget_exhausted", "exhausted", "must be an object"),
])
def test_a_hostile_usage_value_is_refused(field: str, value: Any,
                                          needle: str) -> None:
    with pytest.raises(UsageProvenanceError) as exc:
        sanitized_usage({field: value})
    assert needle in str(exc.value)


def test_a_hostile_value_in_a_v1_checkpoint_fails_closed(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "bad.ckpt",
                       _v1_body(dict(CONSISTENT, successful_responses="1999")))
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "must be an integer" in str(exc.value)
    # The hostile file itself is never rewritten by a failed load.
    assert json.loads(path.read_text(encoding="utf-8"))["usage"][
        "successful_responses"] == "1999"


def test_a_hostile_value_in_a_v2_process_entry_fails_closed(tmp_path: Path) -> None:
    path = _v2_file(tmp_path, combine_usage([dict(CONSISTENT)]),
                    [dict(CONSISTENT, throttle_wait_seconds=float("nan"))])
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "finite" in str(exc.value)


def test_combining_a_hostile_value_raises_a_sanitized_error() -> None:
    """It used to escape as a bare TypeError from inside the combiner."""

    with pytest.raises(UsageProvenanceError) as exc:
        combine_usage([{"successful_responses": 1}, {"successful_responses": "2"}])
    assert "is not a number" in str(exc.value)


def test_validating_a_hostile_value_reports_rather_than_raising() -> None:
    problems = validate_usage_accounting({"successful_responses": "x"})
    assert any("not a number" in p for p in problems)
    problems = validate_usage_accounting({"throttle_wait_seconds": float("nan")})
    assert any("not finite" in p for p in problems)


def test_every_usage_field_has_a_resolvable_declared_type() -> None:
    hints = usage_type_hints()
    declared = {f.name for f in dataclasses.fields(UsageReport)}
    assert declared <= set(hints)
    assert declared == set(USAGE_FIELD_COMBINE)


def test_an_unknown_key_is_dropped_and_never_reaches_a_report() -> None:
    clean = sanitized_usage({"successful_responses": 1,
                             "api_key": "sk-live-SHOULD-NOT-SURVIVE"})
    assert clean == {"successful_responses": 1}


# --------------------------------------------------------------------------- #
# 2. Absent is unknown, never zero
# --------------------------------------------------------------------------- #
def test_a_sparse_legacy_checkpoint_loads_without_inventing_counters(
    tmp_path: Path,
) -> None:
    """Only ``successful_responses`` is known; nothing else may be assumed."""

    path = _write_json(tmp_path / "sparse.ckpt",
                       _v1_body({"successful_responses": 5}))
    ck = load_checkpoint(path)
    entry = ck.process_usage[0]
    assert entry["successful_responses"] == 5
    for absent in ("transport_starts", "failed_responses", "retry_attempts",
                   "http_429s", "pages_fetched", "throttle_wait_seconds"):
        assert absent not in entry, f"{absent} was invented"
    assert ck.legacy_migrated and not ck.process_count_known


def test_an_absent_counter_does_not_manufacture_a_contradiction() -> None:
    """5 successes with transport_starts ABSENT is unknown, not 5 > 0."""

    assert validate_usage_accounting({"successful_responses": 5}) == []
    # Present-but-impossible still fails.
    assert validate_usage_accounting(
        {"successful_responses": 5, "transport_starts": 1})


@pytest.mark.parametrize("usage,needle", [
    (dict(CONSISTENT, transport_starts=1, successful_responses=9),
     "transport_starts 1 < terminal outcomes 9"),
    (dict(CONSISTENT, pages_fetched=9), "pages_fetched 9 exceeds"),
    (dict(CONSISTENT, reserved_attempts=999, attempted_requests=999,
          transport_starts=999, responses_received=999, parse_successes=999,
          successful_responses=999), "exceeds the manifest request cap"),
])
def test_a_legacy_checkpoint_asserting_the_impossible_fails_closed(
    tmp_path: Path, usage: dict[str, Any], needle: str
) -> None:
    """Legacy files used to skip accounting validation altogether."""

    path = _write_json(tmp_path / "imp.ckpt", _v1_body(usage))
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert needle in str(exc.value)


def test_the_real_june_shaped_legacy_totals_still_load(tmp_path: Path) -> None:
    """The repair must not reject the genuine June evidence."""

    june = {"reserved_attempts": 2008, "attempted_requests": 2008,
            "transport_starts": 2008, "successful_responses": 1999,
            "failed_responses": 2, "retry_attempts": 7, "responses_received": 1999,
            "parse_successes": 1999, "pages_fetched": 401, "http_429s": 0,
            "blocked_requests": 0, "throttle_events": 1999,
            "throttle_wait_seconds": 3407.8885556863097,
            "families_completed": ["game", "skeleton"], "network_occurred": True,
            "database_mutated": True, "games_received": 402, "games_selected": 400,
            "checkpoint_state": "completed"}
    path = _write_json(tmp_path / "june.ckpt",
                       _v1_body(june, request_cap=6002))
    ck = load_checkpoint(path)
    total = ck.logical_usage()
    assert (total["successful_responses"], total["failed_responses"],
            total["retry_attempts"], total["pages_fetched"]) == (1999, 2, 7, 401)
    assert ck.process_usage[0][PROCESS_ID_KEY] == LEGACY_PROCESS_ID


# --------------------------------------------------------------------------- #
# 3. Derived totals must match the history for EVERY rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field,tampered", [
    ("successful_responses", 999),
    ("families_completed", ["schedule", "roster"]),
    ("network_occurred", False),
    ("games_received", 77),
    ("selection_truncated", True),
    ("authentication_status", "succeeded"),
    ("tier_verified", True),
])
def test_a_tampered_total_of_any_rule_kind_is_detected(
    tmp_path: Path, field: str, tampered: Any
) -> None:
    """Only ADDITIVE totals used to be compared against the history."""

    total = combine_usage([dict(CONSISTENT)])
    path = _v2_file(tmp_path, dict(total, **{field: tampered}), [dict(CONSISTENT)])
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "inconsistent" in str(exc.value)


def test_a_duplicated_family_list_is_normalized_not_propagated(
    tmp_path: Path,
) -> None:
    total = combine_usage([dict(CONSISTENT)])
    path = _v2_file(tmp_path, dict(total, families_completed=["schedule", "schedule"]),
                    [dict(CONSISTENT)])
    ck = load_checkpoint(path)
    assert tuple(ck.usage["families_completed"]) == ("schedule",)


# --------------------------------------------------------------------------- #
# 4. Unit-set contradictions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("over,needle", [
    ({"state": "failed", "failed_identities": ["u1"]}, "both completed and failed"),
    ({"state": "truncated", "blocked_identities": ["u1"]},
     "both completed and blocked"),
    ({"state": "failed", "incomplete_identities": ["u1"]},
     "both completed and incomplete"),
    ({"incomplete_identities": ["u2"]}, "unresolved unit"),
    ({"recovered_identities": ["ghost"]}, "recovered that are not completed"),
    ({"state": "failed", "failed_identities": ["u2"],
      "recovered_identities": ["u2"]}, "recovered that are not completed"),
])
def test_contradictory_unit_sets_fail_closed(tmp_path: Path, over: dict[str, Any],
                                             needle: str) -> None:
    path = _write_json(tmp_path / "sets.ckpt", _v1_body(dict(CONSISTENT), **over))
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert needle in str(exc.value)


def test_a_recovered_unit_that_is_still_unresolved_fails_closed(
    tmp_path: Path,
) -> None:
    path = _write_json(tmp_path / "both.ckpt", _v1_body(
        dict(CONSISTENT), state="failed", completed_identities=["u1", "u2"],
        failed_identities=["u2"], recovered_identities=["u2"]))
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "both completed and failed" in str(exc.value)


def test_an_identity_list_of_the_wrong_type_fails_closed(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "t.ckpt",
                       _v1_body(dict(CONSISTENT), completed_identities=[1, 2]))
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "list of identities" in str(exc.value)


# --------------------------------------------------------------------------- #
# 5. Process identity
# --------------------------------------------------------------------------- #
def test_the_pilot_stamps_a_per_invocation_process_id(tmp_path: Path) -> None:
    _run(tmp_path, _Executor(("a",)), resume=False)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    first = ck.process_usage[0][PROCESS_ID_KEY]
    assert first and first != LEGACY_PROCESS_ID
    _run(tmp_path, _Executor(("a", "b")), resume=True)
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    ids = [e[PROCESS_ID_KEY] for e in ck2.process_usage]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert ids[0] == first, "the earlier process's identifier changed"


def test_a_process_id_is_not_the_pid_so_reuse_cannot_collide() -> None:
    """A reused PID must not be able to impersonate an earlier invocation."""

    import os

    ids = {new_process_id() for _ in range(200)}
    assert len(ids) == 200
    assert str(os.getpid()) not in ids


def test_duplicate_process_identifiers_are_refused() -> None:
    with pytest.raises(UsageProvenanceError) as exc:
        sanitized_process_entries([{PROCESS_ID_KEY: "abc", "successful_responses": 1},
                                   {PROCESS_ID_KEY: "abc", "successful_responses": 1}])
    assert "same process_id" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "x" * 65, "has space", 7, {"a": 1}])
def test_a_malformed_process_identifier_is_refused(bad: Any) -> None:
    with pytest.raises(UsageProvenanceError):
        sanitized_process_entries([{PROCESS_ID_KEY: bad, "successful_responses": 1}])


def test_many_writes_in_one_process_replace_a_single_entry(tmp_path: Path) -> None:
    _run(tmp_path, _Executor(("a", "b", "c", "d", "e")), resume=False)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert len(ck.process_usage) == 1
    assert ck.usage["successful_responses"] == 5


def test_a_concurrent_second_writer_is_refused_rather_than_clobbering(
    tmp_path: Path,
) -> None:
    """A stale-history writer must not silently drop another process's entry."""

    from sports_quant.ingest import pilot as pilot_mod

    _run(tmp_path, _Executor(("a",)), resume=False)
    ckpt = tmp_path / "s.ckpt"
    stale = [dict(e) for e in load_checkpoint(ckpt).process_usage]
    # Another process appends while we hold `stale`.
    _run(tmp_path, _Executor(("a", "b")), resume=True)
    assert len(load_checkpoint(ckpt).process_usage) == 2
    with pytest.raises(CheckpointError) as exc:
        pilot_mod._assert_sole_writer(ckpt, stale, new_process_id())
    assert "another process" in str(exc.value)


def test_the_sole_writer_guard_allows_a_process_rewriting_its_own_entry(
    tmp_path: Path,
) -> None:
    from sports_quant.ingest import pilot as pilot_mod

    _run(tmp_path, _Executor(("a",)), resume=False)
    ckpt = tmp_path / "s.ckpt"
    disk = load_checkpoint(ckpt).process_usage
    mine = disk[-1][PROCESS_ID_KEY]
    pilot_mod._assert_sole_writer(ckpt, [], mine)          # our own entry: fine
    pilot_mod._assert_sole_writer(tmp_path / "absent.ckpt", [], mine)  # no file: fine


def test_a_crash_before_the_first_boundary_records_one_honest_entry(
    tmp_path: Path,
) -> None:
    class _Boom:
        def remaining_identities(self, *, completed: set[str]) -> tuple[str, ...]:
            return ("z",)

        def iter_units(self, *, gate: RequestGate,
                       completed: set[str]) -> Iterator[UnitDone]:
            raise RuntimeError("died before any unit")
            yield  # pragma: no cover

    result = _run(tmp_path, _Boom(), resume=False)
    assert result.failure is not None
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert len(ck.process_usage) == 1
    assert ck.usage["successful_responses"] == 0
    assert ck.usage["transport_starts"] == 0
    assert ck.completed_identities == []


def test_repeated_load_and_write_cycles_are_byte_stable(tmp_path: Path) -> None:
    _run(tmp_path, _Executor(("a", "b")), resume=False)
    path = tmp_path / "s.ckpt"
    first = path.read_bytes()
    for _ in range(5):
        write_checkpoint(path, load_checkpoint(path))
        assert path.read_bytes() == first


# --------------------------------------------------------------------------- #
# 6/7. Gate precharge, budgets and a randomized multi-process state machine
# --------------------------------------------------------------------------- #
def test_prior_reserved_attempts_reduce_the_remaining_request_budget() -> None:
    g = _gate(request_cap=10)
    g.seed_prior(prior_requests=8, prior_credits=0, prior_transport_starts=8,
                 prior_pages_fetched=0)
    from sports_quant.request_control import BudgetExhausted

    for i in range(2):
        g.reserve(RequestUnit(provider="mlb_statsapi", league="mlb",
                              endpoint_family="schedule", date_key="d",
                              entity_key=str(i)))
    with pytest.raises(BudgetExhausted):
        g.reserve(RequestUnit(provider="mlb_statsapi", league="mlb",
                              endpoint_family="schedule", date_key="d",
                              entity_key="over"))


def test_prior_credits_reduce_the_remaining_credit_budget() -> None:
    g = RequestGate(request_budget=RequestBudget(max_requests=100),
                    credit_budget=CreditBudget(applicable=True, max_credits=10),
                    cost_policy=build_balldontlie_policy(), sleep=lambda _s: None)
    g.seed_prior(prior_requests=0, prior_credits=10, prior_transport_starts=0,
                 prior_pages_fetched=0)
    assert g.usage.reserved_credits == 10
    from sports_quant.usage_provenance import current_process_entry

    assert current_process_entry(g.usage.as_dict())["reserved_credits"] == 0


def test_a_blocked_request_is_not_counted_as_a_transport(tmp_path: Path) -> None:
    result = _run(tmp_path, _Executor(("a",), blocked_family="not_a_family"),
                  resume=False)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.usage["blocked_requests"] >= 1
    assert ck.usage["transport_starts"] == 0
    assert result.truncated is True


def test_five_process_randomized_accounting_state_machine() -> None:
    """Across five processes in random shapes, the logical totals must close.

    Deterministic per seed, and every seed must satisfy
    ``logical = prior + current`` exactly once for every additive counter.
    """

    for seed in range(30):
        rng = random.Random(seed)
        entries: list[dict[str, Any]] = []
        expected: dict[str, Any] = {}
        for _ in range(5):
            succ = rng.randint(0, 4)
            fails = rng.randint(0, 2)
            retries = rng.randint(0, 3)
            entry = {
                PROCESS_ID_KEY: new_process_id(),
                "successful_responses": succ,
                "failed_responses": fails,
                "retry_attempts": retries,
                "transport_starts": succ + fails + retries,
                "reserved_attempts": succ + fails + retries,
                "attempted_requests": succ + fails + retries,
                "responses_received": succ + fails,
                "parse_successes": succ,
                "pages_fetched": rng.randint(0, succ),
                "throttle_events": rng.randint(0, 5),
                "throttle_wait_seconds": round(rng.random() * 10, 3),
                "http_429s": rng.randint(0, 1),
                "blocked_requests": rng.randint(0, 1),
                "network_occurred": succ + fails + retries > 0,
            }
            entries.append(entry)
            for key, value in entry.items():
                if USAGE_FIELD_COMBINE.get(key) is Combine.ADDITIVE:
                    assert isinstance(value, (int, float))
                    expected[key] = expected.get(key, 0) + value

        total = combine_usage(entries)
        for key, want in expected.items():
            if isinstance(want, float):
                assert math.isclose(total[key], want), (seed, key)
            else:
                assert total[key] == want, (seed, key)
        # prior + current closes exactly, and prior never double counts.
        prior = combine_usage(entries[:-1])
        for key in expected:
            assert math.isclose(total[key], prior.get(key, 0) + entries[-1][key]), (seed, key)
        assert total["prior_transport_starts"] == sum(
            int(e["transport_starts"]) for e in entries[:-1])  # type: ignore[arg-type]
        # Order stability: combining is deterministic for a fixed process order.
        assert combine_usage(entries) == total
        assert validate_usage_accounting(
            total, entries=entries, require_retry_identity=True) == []


def test_resumes_never_multiply_prior_usage(tmp_path: Path) -> None:
    _run(tmp_path, _Executor(("a", "b", "c")), resume=False)
    baseline = load_checkpoint(tmp_path / "s.ckpt").usage
    for _ in range(4):
        _run(tmp_path, _Executor(("a", "b", "c")), resume=True)
        assert load_checkpoint(tmp_path / "s.ckpt").usage == baseline


def test_pacing_and_retry_evidence_is_attributed_to_the_process_that_saw_it(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _Executor(("a",), throttles=3, wait=1.5, http_429s=1),
         resume=False)
    _run(tmp_path, _Executor(("a", "b"), throttles=7, wait=2.5), resume=True)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.process_usage[0]["throttle_events"] == 3
    assert ck.process_usage[1]["throttle_events"] == 7
    assert ck.usage["throttle_events"] == 10
    assert math.isclose(float(ck.usage["throttle_wait_seconds"]), 4.0)
    assert ck.process_usage[0]["http_429s"] == 1
    assert ck.process_usage[1].get("http_429s", 0) == 0
    assert ck.usage["http_429s"] == 1


# --------------------------------------------------------------------------- #
# 8/11. Recovered identities end to end
# --------------------------------------------------------------------------- #
def test_recovery_moves_a_unit_out_of_every_unresolved_set_and_is_reported(
    tmp_path: Path,
) -> None:
    failed = _run(tmp_path, _Executor(("a", "b"), fail_after=1, failures=1,
                                      retries=1), resume=False)
    assert failed.failure is not None
    ck = load_checkpoint(tmp_path / "s.ckpt")
    ck.incomplete_identities = ["b"]
    write_checkpoint(tmp_path / "s.ckpt", ck)

    result = _run(tmp_path, _Executor(("a", "b")), resume=True)
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert ck2.state == "completed"
    assert ck2.recovered_identities == ["b"]
    assert ck2.incomplete_identities == []
    assert ck2.failed_identities == [] and ck2.blocked_identities == []
    # The earlier failure evidence is NOT erased by the recovery.
    assert ck2.usage["failed_responses"] == 1
    assert ck2.usage["retry_attempts"] == 1
    # Reported at unit level, distinguishing first-time completion from recovery.
    payload = result.as_dict()
    assert payload["recovered_identities"] == ["b"]
    assert payload["recovered_count"] == 1
    assert payload["initially_completed"] == 1
    assert payload["unresolved_identities"] == []
    assert payload["unresolved_count"] == 0


def test_repeated_successful_resumes_do_not_duplicate_a_recovered_identity(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _Executor(("a", "b"), fail_after=1), resume=False)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    ck.incomplete_identities = ["b"]
    write_checkpoint(tmp_path / "s.ckpt", ck)
    _run(tmp_path, _Executor(("a", "b")), resume=True)
    for _ in range(3):
        _run(tmp_path, _Executor(("a", "b")), resume=True)
    assert load_checkpoint(tmp_path / "s.ckpt").recovered_identities == ["b"]


def test_a_unit_completed_first_time_is_never_labelled_recovered(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _Executor(("a", "b")), resume=False)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    assert ck.recovered_identities == []
    assert result.as_dict()["initially_completed"] == 2
    assert result.as_dict()["recovered_count"] == 0


# --------------------------------------------------------------------------- #
# 9. Legacy upgrade honesty
# --------------------------------------------------------------------------- #
def test_a_legacy_file_upgrades_only_when_real_work_happens(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    body = _v1_body({"successful_responses": 2, "transport_starts": 2,
                     "reserved_attempts": 2, "attempted_requests": 2,
                     "responses_received": 2, "parse_successes": 2,
                     "network_occurred": True},
                    manifest_hash=m.manifest_hash(), plan_version=m.plan_version,
                    families=list(m.families), date_range=m.date_range,
                    state="failed", completed_identities=["a"], request_cap=200)
    path = _write_json(tmp_path / "s.ckpt", body)

    # Real work upgrades it, preserving the legacy aggregate as one entry.
    _run(tmp_path, _Executor(("a", "b")), resume=True)
    ck = load_checkpoint(path)
    assert ck.checkpoint_format_version == CHECKPOINT_FORMAT_VERSION
    assert len(ck.process_usage) == 2
    assert ck.process_usage[0][PROCESS_ID_KEY] == LEGACY_PROCESS_ID
    assert ck.usage["successful_responses"] == 3


def test_a_completed_legacy_no_work_resume_leaves_the_v1_file_untouched(
    tmp_path: Path,
) -> None:
    m = _manifest(tmp_path)
    body = _v1_body({"successful_responses": 2, "transport_starts": 2,
                     "reserved_attempts": 2, "attempted_requests": 2,
                     "responses_received": 2, "parse_successes": 2,
                     "network_occurred": True, "checkpoint_state": "completed"},
                    manifest_hash=m.manifest_hash(), plan_version=m.plan_version,
                    families=list(m.families), date_range=m.date_range,
                    state="completed", completed_identities=["a"], request_cap=200)
    path = _write_json(tmp_path / "s.ckpt", body)
    before = path.read_bytes()

    noop = _run(tmp_path, _Executor(("a",)), resume=True)

    assert noop.performed_new_work is False
    assert noop.checkpoint_mutated is False
    assert path.read_bytes() == before, "a no-work resume rewrote a v1 file"
    assert load_checkpoint(path).checkpoint_format_version == (
        LEGACY_CHECKPOINT_FORMAT_VERSION)
    assert noop.legacy_provenance is True
    assert noop.usage["successful_responses"] == 2


def test_a_failed_legacy_checkpoint_with_no_work_left_concludes_and_upgrades(
    tmp_path: Path,
) -> None:
    """A process can die after its last unit committed.

    Resuming then performs no provider work but DOES legitimately change state
    from failed to completed, so the checkpoint is rewritten -- the byte-identity
    guarantee covers a checkpoint that is already ``completed``.
    """

    m = _manifest(tmp_path)
    body = _v1_body({"successful_responses": 2, "transport_starts": 2,
                     "reserved_attempts": 2, "attempted_requests": 2,
                     "responses_received": 2, "parse_successes": 2,
                     "network_occurred": True},
                    manifest_hash=m.manifest_hash(), plan_version=m.plan_version,
                    families=list(m.families), date_range=m.date_range,
                    state="failed", completed_identities=["a"], request_cap=200)
    path = _write_json(tmp_path / "s.ckpt", body)

    result = _run(tmp_path, _Executor(("a",)), resume=True)

    assert result.performed_new_work is False   # no provider work
    assert result.checkpoint_mutated is True    # but a real state transition
    ck = load_checkpoint(path)
    assert ck.state == "completed"
    assert ck.checkpoint_format_version == CHECKPOINT_FORMAT_VERSION
    assert ck.usage["successful_responses"] == 2   # legacy evidence preserved
    assert ck.process_usage[0][PROCESS_ID_KEY] == LEGACY_PROCESS_ID


def test_a_later_resume_cannot_pretend_the_legacy_entry_was_several_processes(
    tmp_path: Path,
) -> None:
    """The migrated aggregate keeps its honest marker rather than a random id."""

    m = _manifest(tmp_path)
    body = _v1_body({"successful_responses": 1, "transport_starts": 1,
                     "reserved_attempts": 1, "attempted_requests": 1,
                     "responses_received": 1, "parse_successes": 1,
                     "network_occurred": True},
                    manifest_hash=m.manifest_hash(), plan_version=m.plan_version,
                    families=list(m.families), date_range=m.date_range,
                    state="failed", completed_identities=["a"], request_cap=200)
    _write_json(tmp_path / "s.ckpt", body)
    _run(tmp_path, _Executor(("a", "b")), resume=True)
    _run(tmp_path, _Executor(("a", "b", "c")), resume=True)
    ck = load_checkpoint(tmp_path / "s.ckpt")
    ids = [e[PROCESS_ID_KEY] for e in ck.process_usage]
    assert ids[0] == LEGACY_PROCESS_ID
    assert ids.count(LEGACY_PROCESS_ID) == 1
    assert len(set(ids)) == len(ids)


def test_a_v1_file_with_duplicate_json_keys_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "dup.ckpt"
    path.write_text('{"checkpoint_format_version": "f1a-checkpoint-v1", '
                    '"state": "completed", "state": "failed"}', encoding="utf-8")
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "duplicate" in str(exc.value)


def test_an_oversized_checkpoint_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.ckpt"
    path.write_text("{" + " " * (5 * 1024 * 1024), encoding="utf-8")
    with pytest.raises(CheckpointError) as exc:
        load_checkpoint(path)
    assert "too large" in str(exc.value)


# --------------------------------------------------------------------------- #
# 13. Authentication / tier / rate evidence precedence
# --------------------------------------------------------------------------- #
def test_a_no_work_process_cannot_upgrade_or_erase_auth_and_tier_evidence() -> None:
    earlier = {"authentication_status": "succeeded", "authentication_succeeded": True,
               "tier_status": "verified", "tier_verified": True,
               "tier_evidence_source": "bounded_capability_audit"}
    later_noop = {"authentication_status": "not_applicable",
                  "authentication_succeeded": None, "tier_status": "unknown",
                  "tier_verified": False, "tier_evidence_source": "none"}
    total = combine_usage([earlier, later_noop])
    assert total["authentication_status"] == "succeeded"
    assert total["authentication_succeeded"] is True
    assert total["tier_status"] == "verified"
    assert total["tier_verified"] is True
    assert total["tier_evidence_source"] == "bounded_capability_audit"


def test_an_unobserved_process_cannot_claim_authentication() -> None:
    total = combine_usage([{"authentication_status": "unknown",
                            "authentication_succeeded": None},
                           {"authentication_status": "unknown",
                            "authentication_succeeded": None}])
    assert total["authentication_status"] == "unknown"
    assert total["authentication_succeeded"] is None


def test_a_configured_tier_is_never_promoted_to_verified_without_evidence() -> None:
    total = combine_usage([{"tier_status": "configured_not_verified:goat",
                            "tier_verified": False,
                            "tier_evidence_source": "declared_capabilities"},
                           {"tier_status": "configured_not_verified:goat",
                            "tier_verified": False}])
    assert total["tier_status"] == "configured_not_verified:goat"
    assert total["tier_verified"] is False
    assert total["tier_evidence_source"] == "declared_capabilities"


def test_an_explicit_authentication_failure_stays_visible() -> None:
    total = combine_usage([{"authentication_status": "failed",
                            "authentication_succeeded": False},
                           {"authentication_status": "not_applicable"}])
    assert total["authentication_status"] == "failed"
    assert total["authentication_succeeded"] is False


def test_mlb_courtesy_pacing_never_becomes_a_claimed_provider_limit() -> None:
    mlb = {"rate_policy_basis": "project_courtesy_cap", "configured_rate_per_min": 30,
           "provider_rate_limit_per_min": None, "rate_policy_active": True}
    total = combine_usage([mlb, {"rate_policy_active": False}])
    assert total["rate_policy_basis"] == "project_courtesy_cap"
    assert total["provider_rate_limit_per_min"] is None
    assert total["configured_rate_per_min"] == 30
    assert total["rate_policy_active"] is True


def test_balldontlie_tier_maximum_and_configured_rate_stay_distinct() -> None:
    total = combine_usage([{"rate_policy_basis": "verified_tier_max",
                            "configured_rate_per_min": 300,
                            "provider_rate_limit_per_min": 600},
                           {"configured_rate_per_min": 300}])
    assert total["configured_rate_per_min"] == 300
    assert total["provider_rate_limit_per_min"] == 600
    assert total["rate_policy_basis"] == "verified_tier_max"


def test_provider_429_evidence_is_not_erased_by_a_later_clean_process() -> None:
    total = combine_usage([{"http_429s": 3, "rate_limited": True},
                           {"http_429s": 0, "rate_limited": False}])
    assert total["http_429s"] == 3
    assert total["rate_limited"] is True


def test_a_budget_exhaustion_survives_a_later_clean_process() -> None:
    exhausted = {"budget_exhausted": {"limit_type": "request", "cap": 10}}
    total = combine_usage([exhausted, {"budget_exhausted": None}])
    assert total["budget_exhausted"] == exhausted["budget_exhausted"]


# --------------------------------------------------------------------------- #
# 15. Atomic write behaviour
# --------------------------------------------------------------------------- #
def test_a_failed_write_leaves_the_previous_checkpoint_readable_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    good = path.read_bytes()
    ck = load_checkpoint(path)

    import os as os_mod

    def _boom(src: Any, dst: Any) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os_mod, "replace", _boom)
    with pytest.raises(OSError):
        write_checkpoint(path, ck)
    monkeypatch.undo()
    assert path.read_bytes() == good           # last valid checkpoint intact
    assert load_checkpoint(path).usage["successful_responses"] == 1
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_each_write_uses_a_unique_temp_name(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    ck = load_checkpoint(path)
    seen: list[str] = []
    real_replace = __import__("os").replace

    def _spy(src: Any, dst: Any) -> None:
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(__import__("os"), "replace", _spy)
    for _ in range(5):
        write_checkpoint(path, ck)
    monkeypatch.undo()
    assert len(seen) == 5 and len(set(seen)) == 5
    assert all(".tmp-" in name for name in seen)


def test_a_transient_replace_failure_is_retried_without_a_real_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows raises ERROR_ACCESS_DENIED when an unrelated handle holds the target.

    Observed under load during this review. Retrying is safe because the replace is
    atomic, and the write must still succeed rather than reporting a false failure.
    """

    import os as os_mod

    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    ck = load_checkpoint(path)
    real_replace = os_mod.replace
    calls = {"n": 0}

    def _flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    waits: list[float] = []
    monkeypatch.setattr(os_mod, "replace", _flaky)
    write_checkpoint(path, ck, sleep=waits.append)   # never a real sleep
    monkeypatch.undo()

    assert calls["n"] == 3, "the transient failure was not retried"
    assert waits and all(w > 0 for w in waits)
    assert load_checkpoint(path).usage["successful_responses"] == 1
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


def test_a_persistent_replace_failure_still_raises(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A real, non-transient failure must not be hidden by the retry."""

    import os as os_mod

    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    good = path.read_bytes()
    ck = load_checkpoint(path)

    def _always(src: Any, dst: Any) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os_mod, "replace", _always)
    with pytest.raises(PermissionError):
        write_checkpoint(path, ck, sleep=lambda _s: None)
    monkeypatch.undo()
    assert path.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


def test_a_symlinked_checkpoint_target_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.ckpt"
    _run(tmp_path, _Executor(("a",)), resume=False)
    real.write_bytes((tmp_path / "s.ckpt").read_bytes())
    link = tmp_path / "link.ckpt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    ck = load_checkpoint(real)
    with pytest.raises(CheckpointError) as exc:
        write_checkpoint(link, ck)
    assert "symlink" in str(exc.value)
    with pytest.raises(CheckpointError):
        load_checkpoint(link)


def test_concurrent_same_path_writers_are_serialized(tmp_path: Path) -> None:
    """The per-path lock must serialize threads writing one checkpoint."""

    import threading

    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    ck = load_checkpoint(path)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                write_checkpoint(path, ck)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert load_checkpoint(path).usage["successful_responses"] == 1
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


# --------------------------------------------------------------------------- #
# NBA non-regression
# --------------------------------------------------------------------------- #
def test_nba_evidence_survives_and_reports_distinctly(tmp_path: Path) -> None:
    g = _gate("nba", request_cap=200)
    g.usage.credits_applicable = True
    g.usage.rate_policy_basis = "verified_tier_max"
    g.usage.configured_rate_per_min = 300
    g.usage.provider_rate_limit_per_min = 600
    g.usage.tier_status = "configured_not_verified:goat"
    g.usage.authentication_status = "succeeded"
    g.usage.authentication_succeeded = True
    g.usage.reported_credits_consumed = 5
    _run(tmp_path, _Executor(("a",), family="games", succ=2, pages=2), resume=False,
         league="nba", gate=g)
    before = (tmp_path / "s.ckpt").read_bytes()

    result = _run(tmp_path, _Executor(("a",), family="games"), resume=True,
                  league="nba")
    assert (tmp_path / "s.ckpt").read_bytes() == before
    u = result.usage
    assert u["pages_fetched"] == 2
    assert u["provider_rate_limit_per_min"] == 600
    assert u["configured_rate_per_min"] == 300
    assert u["rate_policy_basis"] == "verified_tier_max"
    assert u["tier_status"] == "configured_not_verified:goat"
    assert u["authentication_status"] == "succeeded"
    assert u["reported_credits_consumed"] == 5
    assert result.performed_new_work is False
    assert result.current_process_usage == {}


def test_nba_partial_unit_recovers_and_keeps_both_outcomes(tmp_path: Path) -> None:
    failed = _run(tmp_path, _Executor(("a", "b"), family="games", fail_after=1,
                                      failures=1, retries=1), resume=False,
                  league="nba")
    assert failed.failure is not None
    ck = load_checkpoint(tmp_path / "s.ckpt")
    ck.incomplete_identities = ["b"]
    write_checkpoint(tmp_path / "s.ckpt", ck)
    result = _run(tmp_path, _Executor(("a", "b"), family="games"), resume=True,
                  league="nba")
    ck2 = load_checkpoint(tmp_path / "s.ckpt")
    assert ck2.state == "completed"
    assert ck2.recovered_identities == ["b"]
    assert ck2.usage["failed_responses"] == 1
    assert result.as_dict()["recovered_count"] == 1


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_a_secret_shaped_unknown_field_never_reaches_a_report_or_v2_output(
    tmp_path: Path,
) -> None:
    SENTINEL = "sk-live-do-not-log"
    _run(tmp_path, _Executor(("a",)), resume=False)
    path = tmp_path / "s.ckpt"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["usage"]["api_key"] = SENTINEL
    body["usage_provenance"]["processes"][0]["authorization"] = SENTINEL
    _write_json(path, body)

    ck = load_checkpoint(path)
    assert SENTINEL not in json.dumps(ck.body(), default=str)
    assert SENTINEL not in json.dumps(ck.logical_usage(), default=str)
    assert SENTINEL not in json.dumps(ck.process_usage, default=str)
    write_checkpoint(path, ck)
    assert SENTINEL not in path.read_text(encoding="utf-8")


def test_error_messages_do_not_echo_a_whole_hostile_value() -> None:
    huge = "x" * 5000
    with pytest.raises(UsageProvenanceError) as exc:
        combine_usage([{"manifest_hash": "aaa"}, {"manifest_hash": huge}])
    assert len(str(exc.value)) < 200
    assert huge not in str(exc.value)


