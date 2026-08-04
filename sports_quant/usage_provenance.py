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

import math
import re
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence, Union, get_args, get_origin

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


def _number(field: str, value: Any) -> Any:
    """A numeric operand, or a sanitized error -- never a bare ``TypeError``."""

    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageProvenanceError(
            f"usage field {field!r} is not a number ({type(value).__name__})")
    if isinstance(value, float) and not math.isfinite(value):
        raise UsageProvenanceError(f"usage field {field!r} is not finite")
    return value


def _combine_pair(field: str, rule: Combine, acc: Any, nxt: Any) -> Any:
    if rule is Combine.ADDITIVE:
        return _number(field, acc) + _number(field, nxt)
    if rule is Combine.ADDITIVE_OPTIONAL:
        if acc is None:
            return nxt
        if nxt is None:
            return acc
        return _number(field, acc) + _number(field, nxt)
    if rule is Combine.MAX:
        return max(_number(field, acc), _number(field, nxt))
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
        """Read an integer counter defensively: junk becomes a problem, not a crash."""

        value = usage.get(field)
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{field} is not a number ({type(value).__name__})")
            return 0
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{field} is not finite")
            return 0
        return int(value)

    for field, rule in USAGE_FIELD_COMBINE.items():
        if rule not in (Combine.ADDITIVE, Combine.MAX, Combine.ADDITIVE_OPTIONAL):
            continue
        value = usage.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{field} is not a number ({type(value).__name__})")
        elif isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{field} is not finite ({value!r})")
        elif value < 0:
            problems.append(f"{field} is negative ({_short(value)})")

    # An ABSENT counter is unknown, not zero. A legacy checkpoint may simply not
    # carry a field, and inventing 0 for it would manufacture a contradiction that
    # the evidence never claimed -- so every relation below is only checked when
    # each field it compares is actually present.
    def _have(*fields: str) -> bool:
        return all(f in usage and usage[f] is not None for f in fields)

    reserved, transports = _i("reserved_attempts"), _i("transport_starts")
    successes, failures = _i("successful_responses"), _i("failed_responses")
    terminal = successes + failures
    if _have("reserved_attempts", "transport_starts") and reserved < transports:
        problems.append(
            f"reserved_attempts {reserved} < transport_starts {transports}")
    # An absent outcome counter is unknown, and unknown can only ever ADD terminal
    # outcomes -- so the outcomes that ARE recorded already exceeding the recorded
    # transports is a contradiction even when the other outcome field is missing.
    known_terminal = sum(_i(f) for f in ("successful_responses", "failed_responses")
                         if _have(f))
    if (_have("transport_starts")
            and any(_have(f) for f in ("successful_responses", "failed_responses"))
            and transports < known_terminal):
        problems.append(
            f"transport_starts {transports} < terminal outcomes {known_terminal} "
            f"({successes} successful + {failures} failed)")
    if (_have("attempted_requests", "reserved_attempts")
            and _i("attempted_requests") != reserved):
        problems.append(
            f"attempted_requests {_i('attempted_requests')} != "
            f"reserved_attempts {reserved} (they are aliases)")
    state = str(usage.get("checkpoint_state") or "")
    settled = state in _SETTLED_STATES if require_retry_identity is None \
        else bool(require_retry_identity)
    if (settled and _have("transport_starts", "successful_responses",
                          "failed_responses", "retry_attempts")
            and transports - terminal != _i("retry_attempts")):
        problems.append(
            f"retry identity broken: transport_starts {transports} - terminal "
            f"{terminal} != retry_attempts {_i('retry_attempts')}")
    if (_have("pages_fetched", "successful_responses")
            and _i("pages_fetched") > successes):
        problems.append(
            f"pages_fetched {_i('pages_fetched')} exceeds successful_responses "
            f"{successes}")
    if _have("http_429s", "transport_starts") and _i("http_429s") > transports:
        problems.append(
            f"http_429s {_i('http_429s')} exceeds transport_starts {transports}")
    if (_have("responses_received", "transport_starts")
            and _i("responses_received") > transports):
        problems.append(
            f"responses_received {_i('responses_received')} exceeds "
            f"transport_starts {transports}")
    if (_have("parse_successes", "responses_received")
            and _i("parse_successes") > _i("responses_received")):
        problems.append(
            f"parse_successes {_i('parse_successes')} exceeds responses_received "
            f"{_i('responses_received')}")
    if (request_cap is not None and _have("reserved_attempts")
            and reserved > request_cap):
        problems.append(
            f"logical reserved_attempts {reserved} exceeds the manifest request "
            f"cap {request_cap}")
    if (credit_cap is not None and _have("reserved_credits")
            and _i("reserved_credits") > credit_cap):
        problems.append(
            f"logical reserved_credits {_i('reserved_credits')} exceeds the "
            f"manifest credit cap {credit_cap}")
    if (_have("network_occurred", "transport_starts")
            and not usage.get("network_occurred") and transports > 0):
        problems.append(
            f"network_occurred is false while transport_starts is {transports}")

    if entries is not None and entries:
        try:
            recombined = combine_usage(list(entries))
        except UsageProvenanceError as exc:
            problems.append(str(exc))
        else:
            # The stored totals must be EXACTLY what the recorded history implies --
            # for every rule, not only the additive ones. Checking additive fields
            # alone left a hole: a tampered `families_completed`, `network_occurred`
            # or selection count in the derived totals went undetected.
            for field, rule in USAGE_FIELD_COMBINE.items():
                if rule is Combine.DERIVED or field not in usage:
                    continue
                want, got = recombined.get(field), usage.get(field)
                if rule is Combine.UNION:
                    want = tuple(want or ())
                    got = tuple(got or ())
                if want != got:
                    problems.append(
                        f"{field} {_short(got)} does not equal the value the "
                        f"{len(entries)} recorded processes imply ({_short(want)})")
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


# --------------------------------------------------------------------------- #
# Type / range validation of untrusted checkpoint values
# --------------------------------------------------------------------------- #
#: Metadata key identifying the process that produced an entry. Not a usage field,
#: so it is never combined into totals; carried through so an entry can be
#: correlated with one invocation and so a clobbered history is detectable.
PROCESS_ID_KEY = "process_id"

#: The identifier a migrated v1 aggregate is given. It is deliberately NOT a
#: random token: a legacy entry represents an unknown number of earlier processes,
#: and this value says so instead of implying one identified run.
LEGACY_PROCESS_ID = "legacy-v1-unsplit"

_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Deepest nesting accepted inside a mapping-valued usage field (budget
#: exhaustion). Defends against a hostile deeply-nested checkpoint.
_MAX_MAPPING_DEPTH = 6
_MAX_MAPPING_KEYS = 64


def _usage_type_hints() -> dict[str, Any]:
    """Declared type of every ``UsageReport`` field, resolved lazily.

    The dataclass annotations are the single source of truth for value types, so
    the validator below cannot drift from the report it validates.
    """

    from typing import get_type_hints

    from .request_control import UsageReport

    return get_type_hints(UsageReport)


_HINTS: Optional[dict[str, Any]] = None


def usage_type_hints() -> dict[str, Any]:
    global _HINTS
    if _HINTS is None:
        _HINTS = _usage_type_hints()
    return _HINTS


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    if get_origin(hint) is Union:
        args = [a for a in get_args(hint) if a is not type(None)]
        return (args[0] if len(args) == 1 else Any), len(args) != len(get_args(hint))
    return hint, False


def _coerce_usage_value(field: str, value: Any, hint: Any) -> Any:
    """Validate one untrusted usage value against its declared type.

    Rejects rather than coerces: a checkpoint is evidence, so a value of the wrong
    type is a corrupt record, not something to guess at. ``bool`` is refused where
    a number is declared (in Python ``True`` would silently count as 1), every
    number must be finite and non-negative (no usage field is meaningfully
    negative), and a name collection is deduplicated into a deterministic tuple.
    """

    base, optional = _unwrap_optional(hint)
    if value is None:
        if optional or base is Any:
            return None
        raise UsageProvenanceError(f"usage field {field!r} may not be null")
    origin = get_origin(base)
    if base is bool:
        if not isinstance(value, bool):
            raise UsageProvenanceError(
                f"usage field {field!r} must be a boolean, got {type(value).__name__}")
        return value
    if base is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise UsageProvenanceError(
                f"usage field {field!r} must be an integer, got {type(value).__name__}")
        if value < 0:
            raise UsageProvenanceError(
                f"usage field {field!r} may not be negative ({value})")
        return value
    if base is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UsageProvenanceError(
                f"usage field {field!r} must be a number, got {type(value).__name__}")
        number = float(value)
        if not math.isfinite(number):
            raise UsageProvenanceError(
                f"usage field {field!r} must be finite, got {number!r}")
        if number < 0:
            raise UsageProvenanceError(
                f"usage field {field!r} may not be negative ({number})")
        return number
    if base is str:
        if not isinstance(value, str):
            raise UsageProvenanceError(
                f"usage field {field!r} must be a string, got {type(value).__name__}")
        return value
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise UsageProvenanceError(
                f"usage field {field!r} must be a list of names, got "
                f"{type(value).__name__}")
        names = []
        for item in value:
            if not isinstance(item, str):
                raise UsageProvenanceError(
                    f"usage field {field!r} must contain only names")
            names.append(item)
        return tuple(sorted(set(names)))
    if origin is dict or base is dict:
        return _validated_mapping(field, value, depth=0)
    return value


def _validated_mapping(field: str, value: Any, *, depth: int) -> dict[str, Any]:
    if depth > _MAX_MAPPING_DEPTH:
        raise UsageProvenanceError(
            f"usage field {field!r} nests deeper than {_MAX_MAPPING_DEPTH} levels")
    if not isinstance(value, Mapping):
        raise UsageProvenanceError(
            f"usage field {field!r} must be an object, got {type(value).__name__}")
    if len(value) > _MAX_MAPPING_KEYS:
        raise UsageProvenanceError(f"usage field {field!r} has too many keys")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise UsageProvenanceError(f"usage field {field!r} has a non-string key")
        if isinstance(item, Mapping):
            out[key] = _validated_mapping(field, item, depth=depth + 1)
        elif isinstance(item, (list, tuple)):
            out[key] = [i for i in item]
        elif isinstance(item, float) and not math.isfinite(item):
            raise UsageProvenanceError(f"usage field {field!r} holds a non-finite value")
        else:
            out[key] = item
    return out


def sanitized_usage(usage: Mapping[str, Any], *, what: str = "usage") -> dict[str, Any]:
    """A usage mapping reduced to declared fields with every value type-checked.

    ``UsageReport`` holds no secret, but a mapping loaded from an on-disk
    checkpoint is untrusted input: unknown keys are dropped rather than carried
    into a combined report, and a value of the wrong type, a non-finite float or a
    negative count is refused instead of being propagated into arithmetic (which
    previously surfaced as a bare ``TypeError`` from deep inside the combiner).
    """

    if not isinstance(usage, Mapping):
        raise UsageProvenanceError(f"{what} is not an object")
    hints = usage_type_hints()
    clean: dict[str, Any] = {}
    for field, value in usage.items():
        name = str(field)
        rule = USAGE_FIELD_COMBINE.get(name)
        if rule is None:
            continue  # unknown key: dropped, never reported, never combined
        hint = hints.get(name, Any)
        clean[name] = _coerce_usage_value(name, value, hint)
    return clean


def sanitized_process_entries(
    entries: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Per-process entries reduced to declared usage fields, fully type-checked.

    Preserves the :data:`PROCESS_ID_KEY` metadata key (validated as a short opaque
    token) and drops every other unknown key. Derived fields are dropped because
    they are recomputed from the history rather than trusted.
    """

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise UsageProvenanceError("usage provenance process entry is not an object")
        clean = {k: v for k, v in sanitized_usage(entry, what="process entry").items()
                 if USAGE_FIELD_COMBINE.get(k) is not Combine.DERIVED}
        raw_id = entry.get(PROCESS_ID_KEY)
        if raw_id is not None:
            if not isinstance(raw_id, str) or not _PROCESS_ID_RE.match(raw_id):
                raise UsageProvenanceError(
                    f"usage provenance {PROCESS_ID_KEY} must be a short opaque token")
            if raw_id in seen_ids and raw_id != LEGACY_PROCESS_ID:
                raise UsageProvenanceError(
                    f"two process entries share the same {PROCESS_ID_KEY}")
            seen_ids.add(raw_id)
            clean[PROCESS_ID_KEY] = raw_id
        out.append(clean)
    return out


def new_process_id() -> str:
    """A fresh per-invocation process identifier (not a PID: PIDs get reused)."""

    import secrets

    return secrets.token_hex(12)
