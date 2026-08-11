"""Availability rules: code-defined, versioned, and digest-bound.

Why code-defined rather than a registry table
---------------------------------------------
An availability rule is a deterministic function of one timestamp. Three designs
were available (task §9), and the narrowest one that still guarantees
reproducibility wins:

* **A registry table** would let a row be inserted that no code implements, or
  let two deployments disagree about what ``lag_hours = 6`` means. It stores a
  parameter and calls it a policy.
* **A bare identifier** ("the code knows what ``rule_x`` means") reproduces
  nothing: a later edit silently reinterprets every accepted corpus.
* **Code-defined + a bound implementation digest** -- what this module does.
  The rule lives in one frozen table here; each rule's digest covers its id, its
  version, the *named evaluation form*, and every parameter. A provenance row
  stores ``availability_rule_id`` together with that digest, so if the rule is
  ever edited, the digest stops matching and verification fails closed instead
  of quietly producing a different answer for an old corpus.

Changing a rule therefore means adding a NEW rule id or version, exactly as
migrations are immutable once applied. That is the whole point: an accepted
corpus can never be silently reinterpreted.

The evaluation form is part of the digest
-----------------------------------------
Hashing the parameters alone would not notice someone changing
``completed + lag`` into ``completed - lag``. ``evaluation_form`` names the
arithmetic, so swapping the function without changing the declared form is a
mismatch the tests catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping

from ..db.schema import from_iso, to_iso
from .provenance import RetrospectiveProvenanceError, semantic_digest

__all__ = [
    "AVAILABILITY_RULES",
    "AvailabilityRule",
    "UnknownAvailabilityRuleError",
    "derive_availability_instant",
    "lookup_rule",
    "verify_rule_digest",
]


class UnknownAvailabilityRuleError(RetrospectiveProvenanceError):
    """A provenance record cited a rule id this build does not implement."""


#: The only evaluation form implemented. Named in the digest so a change to the
#: arithmetic cannot hide behind unchanged parameters.
_COMPLETION_PLUS_LAG: Final = "completion_plus_lag"


@dataclass(frozen=True)
class AvailabilityRule:
    """One deterministic EVENT_DERIVED availability policy.

    ``lag_seconds`` is the conservative delay between a source event completing
    and the derived fact being treatable as knowable. It is a policy choice, not
    a measurement, and it is stated rather than tuned: the architecture requires
    correction-sensitive inputs to carry a conservative lag and to be reported
    with a sensitivity analysis against the zero-lag counterpart.
    """

    rule_id: str
    version: str
    evaluation_form: str
    lag_seconds: int
    description: str

    def __post_init__(self) -> None:
        if self.lag_seconds < 0:
            raise RetrospectiveProvenanceError(
                f"availability rule {self.rule_id!r} has a negative lag "
                f"({self.lag_seconds}s); a fact cannot become knowable before the "
                "event it derives from completed"
            )
        if self.evaluation_form != _COMPLETION_PLUS_LAG:
            raise RetrospectiveProvenanceError(
                f"availability rule {self.rule_id!r} declares evaluation form "
                f"{self.evaluation_form!r}, which this build does not implement"
            )

    @property
    def digest(self) -> str:
        """Implementation digest: id, version, evaluation form, parameters.

        The description is excluded on purpose -- fixing a typo in prose must not
        invalidate an accepted corpus, whereas changing the lag must.
        """

        return semantic_digest({
            "rule_id": self.rule_id,
            "version": self.version,
            "evaluation_form": self.evaluation_form,
            "lag_seconds": self.lag_seconds,
        })

    def availability_instant(self, source_event_completed_at: str) -> datetime:
        """Derive -- never store -- the instant this fact became knowable."""

        return from_iso(source_event_completed_at) + timedelta(seconds=self.lag_seconds)


#: Every implemented rule, keyed by id. Frozen: adding a rule is a code change
#: with a new id or version, never an edit to an existing entry.
AVAILABILITY_RULES: Final[Mapping[str, AvailabilityRule]] = {
    rule.rule_id: rule
    for rule in (
        AvailabilityRule(
            rule_id="prior_event_completion_conservative_v1",
            version="1",
            evaluation_form=_COMPLETION_PLUS_LAG,
            lag_seconds=6 * 3600,
            description=(
                "A fact derived from a completed prior event is treated as knowable "
                "six hours after that event completed. Conservative by design: the "
                "publication delay of official box-score detail is not documented, so "
                "the lag is stated as an assumption and carried into the sensitivity "
                "analysis rather than presented as a measurement."
            ),
        ),
        AvailabilityRule(
            rule_id="prior_event_completion_immediate_v1",
            version="1",
            evaluation_form=_COMPLETION_PLUS_LAG,
            lag_seconds=0,
            description=(
                "Zero-lag counterpart of the conservative rule, existing only as the "
                "optimistic bound in the required sensitivity analysis. A corpus built "
                "on this rule alone is not a defensible headline result."
            ),
        ),
    )
}


def lookup_rule(rule_id: str) -> AvailabilityRule:
    """Fetch a rule by id, failing closed on an unknown one."""

    try:
        return AVAILABILITY_RULES[rule_id]
    except KeyError:
        raise UnknownAvailabilityRuleError(
            f"availability rule {rule_id!r} is not implemented by this build "
            f"(known: {sorted(AVAILABILITY_RULES)}). A corpus citing it cannot be "
            "reproduced here, so it is refused rather than approximated."
        ) from None


def verify_rule_digest(rule_id: str, expected_digest: str) -> AvailabilityRule:
    """Fetch a rule and assert the stored digest still matches this build.

    This is the check that stops an accepted corpus from being silently
    reinterpreted: if the rule was edited, the digest differs and the caller
    fails closed instead of computing a different answer under the same name.
    """

    rule = lookup_rule(rule_id)
    if rule.digest != expected_digest:
        raise RetrospectiveProvenanceError(
            f"availability rule {rule_id!r} has changed since this provenance record "
            f"was written (stored {expected_digest[:16]}..., this build "
            f"{rule.digest[:16]}...). Rules are immutable once cited -- add a new rule "
            "id or version instead of editing this one."
        )
    return rule


def derive_availability_instant(
    *, rule_id: str, rule_digest: str, source_event_completed_at: str
) -> str:
    """Derive the availability instant for an EVENT_DERIVED input.

    Derived on read, never stored: a materialized ``effective_at`` is a second
    source of truth that goes stale the moment the rule changes, and it is
    exactly the column a future bug would quietly backdate.
    """

    rule = verify_rule_digest(rule_id, rule_digest)
    return to_iso(rule.availability_instant(source_event_completed_at))
