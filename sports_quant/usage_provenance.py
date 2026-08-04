"""Logical-run usage provenance: how one run's evidence composes across processes.

A *logical run* is one manifest executed by one or more OS processes: an initial
process plus any number of ``--resume`` processes. Every process produces its own
:class:`~sports_quant.request_control.UsageReport`. Before this module existed the
checkpoint stored only the newest report, so a resume overwrote the earlier
process's evidence -- a completed no-work resume zeroed ``successful_responses``,
``failed_responses``, ``retry_attempts``, ``throttle_wait_seconds``,
``pages_fetched`` and ``families_completed``, destroying the only durable record
that a logical run had contained terminal failures and retries.

The model here is deliberately small and total:

**Current process**
    Evidence generated only by the presently running process.

**Prior processes**
    The combination of every earlier process in the same logical run.

**Logical-run totals**
    The exact combination across all processes, computed from an append-only
    per-process history so repeated resumes can never multiply prior usage.

For additive counters the invariant is exactly::

    logical_total = prior_total + current_process_value

Set-like and evidence-like fields are NOT summed. Every field of ``UsageReport``
carries exactly one declared :class:`Combine` rule in :data:`USAGE_FIELD_COMBINE`,
and a test asserts the table covers the dataclass exhaustively, so a new usage
field cannot be added without deciding how it composes.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Version of the accounting rules below. Stored in the checkpoint so a future
#: rule change is detectable rather than silently reinterpreting old evidence.
USAGE_ACCOUNTING_VERSION = "f1-usage-provenance-v1"


class UsageProvenanceError(RuntimeError):
    """Contradictory or uncombinable usage provenance (sanitized message)."""


class Combine(str, Enum):
    """How one usage field composes across the processes of a logical run."""

    #: Numeric counters: total = sum over processes.
    ADDITIVE = "additive"
    #: Optional counters: sum of the known values; unknown stays unknown (never 0).
    ADDITIVE_OPTIONAL = "additive_optional"
    #: Observations of a frozen quantity (e.g. the selected game set): high-water
    #: mark, so a process that never re-observed it (0) cannot erase it and a
    #: process that did re-observe it cannot double it.
    MAX = "max"
    #: Booleans: logical OR. Something that happened stays happened.
    ANY = "any"
    #: Optional[bool] evidence: True if any process observed True, else False if
    #: any observed False, else unknown.
    ANY_EVIDENCE = "any_evidence"
    #: Tuples of names: deterministic sorted set union.
    UNION = "union"
    #: Configuration / manifest identity: must agree wherever both are known,
    #: otherwise the processes did not execute the same plan -> fail closed.
    IDENTITY = "identity"
    #: Ranked enumerations: the strongest evidence any process observed wins, so
    #: evidence is never downgraded and never upgraded without an observation.
    PRECEDENCE = "precedence"
    #: Keep the first non-null evidence; a later ``None`` must not erase it.
    FIRST_EVIDENCE = "first_evidence"
    #: Point-in-time or per-process values where only the newest is meaningful.
    LATEST = "latest"
    #: Derived from the history itself; never stored in a per-process entry.
    DERIVED = "derived"


#: Ranked enumerations for :attr:`Combine.PRECEDENCE`, weakest first. Ranking is
#: on the part before ``:`` so suffixed values (``configured_not_verified:goat``)
#: rank correctly while keeping their detail.
USAGE_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "authentication_status": ("not_applicable", "unknown", "failed", "succeeded"),
    "tier_status": ("unknown", "not_applicable", "configured_not_verified", "verified"),
    "tier_evidence_source": ("none", "declared_capabilities", "bounded_capability_audit"),
    # `inconsistent` is the strongest signal here: a disagreement observed by any
    # process must survive a later process that saw a clean header.
    "credit_header_status": ("not_applicable", "absent", "present", "inconsistent"),
}

#: Exhaustive rule per ``UsageReport`` field. Enforced by
#: ``test_usage_field_combine_table_covers_every_usage_field``.
USAGE_FIELD_COMBINE: dict[str, Combine] = {
    # -- identity of the plan being executed --------------------------------- #
    "provider": Combine.IDENTITY,
    "league": Combine.IDENTITY,
    "manifest_hash": Combine.IDENTITY,
    "planned_requests": Combine.IDENTITY,
    "estimated_requests_min": Combine.IDENTITY,
    "estimated_requests_max": Combine.IDENTITY,
    "estimated_credits_min": Combine.IDENTITY,
    "estimated_credits_max": Combine.IDENTITY,
    "credits_applicable": Combine.IDENTITY,
    "provider_rate_limit_per_min": Combine.IDENTITY,
    "configured_rate_per_min": Combine.IDENTITY,
    "rate_policy_basis": Combine.IDENTITY,
    "rate_policy_version": Combine.IDENTITY,
    "rate_burst": Combine.IDENTITY,
    "rate_min_interval_seconds": Combine.IDENTITY,
    # -- staged request accounting ------------------------------------------- #
    "reserved_attempts": Combine.ADDITIVE,
    "attempted_requests": Combine.ADDITIVE,
    "transport_starts": Combine.ADDITIVE,
    "responses_received": Combine.ADDITIVE,
    "parse_successes": Combine.ADDITIVE,
    "successful_responses": Combine.ADDITIVE,
    "failed_responses": Combine.ADDITIVE,
    "retry_attempts": Combine.ADDITIVE,
    "pages_fetched": Combine.ADDITIVE,
    "blocked_requests": Combine.ADDITIVE,
    "http_429s": Combine.ADDITIVE,
    "throttle_events": Combine.ADDITIVE,
    "throttle_wait_seconds": Combine.ADDITIVE,
    "reserved_credits": Combine.ADDITIVE,
    "reported_credits_consumed": Combine.ADDITIVE_OPTIONAL,
    # A provider-reported balance is a point-in-time reading, never a sum.
    "provider_credits_remaining": Combine.LATEST,
    "credit_header_status": Combine.PRECEDENCE,
    # Each process skips the units already done when IT started; summing those
    # counts is meaningless, so the newest process's value is the logical answer.
    "skipped_on_resume": Combine.LATEST,
    # -- selection accounting (a frozen set observed by >= 1 process) --------- #
    "games_received": Combine.MAX,
    "games_selected": Combine.MAX,
    "games_excluded_by_max_games": Combine.MAX,
    "games_deduplicated": Combine.MAX,
    "selection_truncated": Combine.ANY,
    # -- things that stay true once observed --------------------------------- #
    "rate_policy_active": Combine.ANY,
    "rate_limited": Combine.ANY,
    "network_occurred": Combine.ANY,
    "database_mutated": Combine.ANY,
    "tier_verified": Combine.ANY,
    "authentication_succeeded": Combine.ANY_EVIDENCE,
    "authentication_status": Combine.PRECEDENCE,
    "tier_status": Combine.PRECEDENCE,
    "tier_evidence_source": Combine.PRECEDENCE,
    "families_completed": Combine.UNION,
    "families_failed": Combine.UNION,
    "families_truncated": Combine.UNION,
    # A budget exhaustion that happened must not be erased by a later clean run.
    "budget_exhausted": Combine.FIRST_EVIDENCE,
    "checkpoint_state": Combine.LATEST,
    # -- derived from the history (never stored per process) ------------------ #
    "prior_requests": Combine.DERIVED,
    "prior_credits": Combine.DERIVED,
    "prior_transport_starts": Combine.DERIVED,
    "prior_pages_fetched": Combine.DERIVED,
}

#: ``prior_*`` mirrors of per-process counters, filled in on the derived logical
#: report so ``logical = prior + current`` is visible in the JSON itself.
DERIVED_PRIOR_SOURCE: dict[str, str] = {
    "prior_requests": "reserved_attempts",
    "prior_credits": "reserved_credits",
    "prior_transport_starts": "transport_starts",
    "prior_pages_fetched": "pages_fetched",
}

#: Fields the gate pre-charges with prior usage in order to enforce the manifest
#: cap across the whole logical run. A per-process entry must have the pre-charge
#: removed, or every resume would re-count the prior process's attempts.
_PRECHARGED: dict[str, str] = {
    "reserved_attempts": "prior_requests",
    "attempted_requests": "prior_requests",
    "reserved_credits": "prior_credits",
}


def _rank(field: str, value: Any) -> int:
    order = USAGE_PRECEDENCE[field]
    head = str(value).split(":", 1)[0]
    return order.index(head) if head in order else -1


def current_process_entry(usage: Mapping[str, Any]) -> dict[str, Any]:
    """This process's OWN evidence, with the gate's prior pre-charge removed.

    The gate seeds ``reserved_attempts`` / ``attempted_requests`` /
    ``reserved_credits`` with the prior processes' totals so the manifest cap
    spans the logical run. Those seeded amounts belong to the earlier processes'
    history entries, so they are subtracted here; without this a third process
    would count the first process's attempts twice.
    """

    entry = {k: v for k, v in usage.items()
             if USAGE_FIELD_COMBINE.get(k) is not Combine.DERIVED}
    for field, prior_field in _PRECHARGED.items():
        if field in entry:
            prior = int(usage.get(prior_field) or 0)
            entry[field] = max(0, int(entry[field] or 0) - prior)
    return entry


def _combine_pair(field: str, rule: Combine, acc: Any, nxt: Any) -> Any:
    if rule is Combine.ADDITIVE:
        return (acc or 0) + (nxt or 0)
    if rule is Combine.ADDITIVE_OPTIONAL:
        if acc is None:
            return nxt
        if nxt is None:
            return acc
        return acc + nxt
    if rule is Combine.MAX:
        return max(acc or 0, nxt or 0)
    if rule is Combine.ANY:
        return bool(acc) or bool(nxt)
    if rule is Combine.ANY_EVIDENCE:
        if acc is True or nxt is True:
            return True
        if acc is False or nxt is False:
            return False
        return None
    if rule is Combine.UNION:
        return tuple(sorted(set(acc or ()) | set(nxt or ())))
    if rule is Combine.PRECEDENCE:
        return nxt if _rank(field, nxt) > _rank(field, acc) else acc
    if rule is Combine.FIRST_EVIDENCE:
        return acc if acc is not None else nxt
    if rule is Combine.LATEST:
        return nxt if nxt is not None else acc
    if rule is Combine.IDENTITY:
        if _is_default(acc):
            return nxt
        if _is_default(nxt) or acc == nxt:
            return acc
        raise UsageProvenanceError(
            f"processes of one logical run disagree on {field!r}: "
            f"{_short(acc)} vs {_short(nxt)}")
    raise UsageProvenanceError(f"no combine rule applied for {field!r}")


def _is_default(value: Any) -> bool:
    """A field that was never populated by this process (so it asserts nothing)."""

    return value is None or value == "" or value == 0 or value == ()


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def combine_usage(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Logical-run totals across an ordered per-process history.

    Deterministic and order-stable for every rule (``LATEST`` and ``PRECEDENCE``
    intentionally depend on order, which is the process order). Raises
    :class:`UsageProvenanceError` when two processes disagree on plan identity.
    """

    if not entries:
        return {}
    total: dict[str, Any] = {}
    for entry in entries:
        for field, value in entry.items():
            rule = USAGE_FIELD_COMBINE.get(field)
            if rule is None or rule is Combine.DERIVED:
                continue  # unknown/derived: never invented, never combined
            total[field] = (value if field not in total
                            else _combine_pair(field, rule, total[field], value))
    # Make `logical = prior + current` visible in the serialized report itself.
    prior_entries = entries[:-1]
    for prior_field, source in DERIVED_PRIOR_SOURCE.items():
        total[prior_field] = sum(int(e.get(source) or 0) for e in prior_entries)
    return total


def prior_totals(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combined evidence of every process EXCEPT the newest one."""

    return combine_usage(list(entries)[:-1]) if len(entries) > 1 else {}


def logical_from_prior_and_current(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Convenience: combine one prior-total mapping with one current entry."""

    return combine_usage([prior, current] if prior else [current])


# --------------------------------------------------------------------------- #
# Accounting invariants
# --------------------------------------------------------------------------- #
#: States in which every transport attempt has necessarily reached a terminal
#: outcome, so the retry identity below is meaningful. A truncated/failed process
#: can hold an attempt that never resolved, which is an exception, not a bug.
_SETTLED_STATES = frozenset({"completed", "resumed_completed", "no_work_resume"})


def validate_usage_accounting(
    usage: Mapping[str, Any],
    *,
    request_cap: Optional[int] = None,
    credit_cap: Optional[int] = None,
    entries: Optional[Sequence[Mapping[str, Any]]] = None,
    require_retry_identity: Optional[bool] = None,
) -> list[str]:
    """Return sanitized accounting problems (empty list == valid).

    Checks only relationships that hold by construction of the request gate and
    the provider-client contract. ``require_retry_identity`` defaults to true only
    for a settled checkpoint state, because an unsettled process may hold a
    transport attempt that never reached a terminal outcome.
    """

    problems: list[str] = []

    def _i(field: str) -> int:
        return int(usage.get(field) or 0)

    for field, rule in USAGE_FIELD_COMBINE.items():
        if rule in (Combine.ADDITIVE, Combine.MAX) and field in usage:
            value = usage.get(field)
            if value is not None and value < 0:
                problems.append(f"{field} is negative ({_short(value)})")
    if usage.get("throttle_wait_seconds") is not None and _f(usage, "throttle_wait_seconds") < 0:
        problems.append("throttle_wait_seconds is negative")

    reserved, transports = _i("reserved_attempts"), _i("transport_starts")
    successes, failures = _i("successful_responses"), _i("failed_responses")
    terminal = successes + failures
    if reserved < transports:
        problems.append(
            f"reserved_attempts {reserved} < transport_starts {transports}")
    if transports < terminal:
        problems.append(
            f"transport_starts {transports} < terminal outcomes {terminal} "
            f"({successes} successful + {failures} failed)")
    if _i("attempted_requests") != reserved:
        problems.append(
            f"attempted_requests {_i('attempted_requests')} != "
            f"reserved_attempts {reserved} (they are aliases)")
    state = str(usage.get("checkpoint_state") or "")
    settled = state in _SETTLED_STATES if require_retry_identity is None \
        else bool(require_retry_identity)
    if settled and transports - terminal != _i("retry_attempts"):
        problems.append(
            f"retry identity broken: transport_starts {transports} - terminal "
            f"{terminal} != retry_attempts {_i('retry_attempts')}")
    if _i("pages_fetched") > successes:
        problems.append(
            f"pages_fetched {_i('pages_fetched')} exceeds successful_responses "
            f"{successes}")
    if _i("http_429s") > transports:
        problems.append(
            f"http_429s {_i('http_429s')} exceeds transport_starts {transports}")
    if _i("responses_received") > transports:
        problems.append(
            f"responses_received {_i('responses_received')} exceeds "
            f"transport_starts {transports}")
    if _i("parse_successes") > _i("responses_received"):
        problems.append(
            f"parse_successes {_i('parse_successes')} exceeds responses_received "
            f"{_i('responses_received')}")
    if request_cap is not None and reserved > request_cap:
        problems.append(
            f"logical reserved_attempts {reserved} exceeds the manifest request "
            f"cap {request_cap}")
    if credit_cap is not None and _i("reserved_credits") > credit_cap:
        problems.append(
            f"logical reserved_credits {_i('reserved_credits')} exceeds the "
            f"manifest credit cap {credit_cap}")
    if not usage.get("network_occurred") and transports > 0:
        problems.append(
            f"network_occurred is false while transport_starts is {transports}")

    if entries is not None and entries:
        try:
            recombined = combine_usage(list(entries))
        except UsageProvenanceError as exc:
            problems.append(str(exc))
        else:
            for field, rule in USAGE_FIELD_COMBINE.items():
                if rule is not Combine.ADDITIVE or field not in usage:
                    continue
                if int(recombined.get(field) or 0) != _i(field):
                    problems.append(
                        f"{field} {_i(field)} does not equal the sum over the "
                        f"{len(entries)} recorded processes "
                        f"({int(recombined.get(field) or 0)})")
            for prior_field, source in DERIVED_PRIOR_SOURCE.items():
                expected = sum(int(e.get(source) or 0) for e in list(entries)[:-1])
                if int(usage.get(prior_field) or 0) != expected:
                    problems.append(
                        f"{prior_field} {int(usage.get(prior_field) or 0)} does not "
                        f"equal the prior processes' {source} total ({expected})")
                current = int(list(entries)[-1].get(source) or 0)
                if int(recombined.get(source) or 0) != expected + current:
                    problems.append(
                        f"prior+current does not close for {source}: "
                        f"{expected} + {current} != {int(recombined.get(source) or 0)}")
    return problems


def _f(usage: Mapping[str, Any], field: str) -> float:
    try:
        return float(usage.get(field) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def assert_no_new_transport(current: Mapping[str, Any]) -> list[str]:
    """Problems if a supposedly zero-work process recorded provider traffic."""

    problems: list[str] = []
    for field in ("transport_starts", "responses_received", "successful_responses",
                  "failed_responses", "retry_attempts", "pages_fetched",
                  "throttle_events", "http_429s"):
        if int(current.get(field) or 0) != 0:
            problems.append(
                f"a zero-work resume recorded {field}={current.get(field)}")
    if current.get("network_occurred"):
        problems.append("a zero-work resume recorded network_occurred=true")
    if current.get("database_mutated"):
        problems.append("a zero-work resume recorded database_mutated=true")
    return problems


def sanitized_process_entries(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-process entries reduced to declared usage fields only.

    ``UsageReport`` holds no secret, but an entry loaded from an on-disk
    checkpoint is untrusted input: unknown keys are dropped rather than carried
    into a combined report.
    """

    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise UsageProvenanceError("usage provenance process entry is not an object")
        clean: dict[str, Any] = {}
        for field, value in entry.items():
            rule = USAGE_FIELD_COMBINE.get(str(field))
            if rule is None or rule is Combine.DERIVED:
                continue
            if rule is Combine.UNION:
                if value is None:
                    continue
                if not isinstance(value, (list, tuple)):
                    raise UsageProvenanceError(
                        f"usage provenance field {field!r} must be a list of names")
                clean[str(field)] = tuple(sorted({str(v) for v in value}))
            else:
                clean[str(field)] = value
        out.append(clean)
    return out
