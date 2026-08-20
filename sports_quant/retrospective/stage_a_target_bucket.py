"""§AF — Stage-A target → T−60 → first-pass request bucket policy.

What this module proves, and what it deliberately cannot yet prove
------------------------------------------------------------------
B2 proves *which* committed manifest was certified. It does not prove the
manifest's target → bucket mapping follows the reviewed scientific algorithm, so
a perfectly bound manifest mapping every target to an absurd bucket still
certifies. §AF closes that by recomputing the mapping from evidence.

The **bucket algorithm** is fully specified and implemented here.

The **target population derivation** is NOT, and cannot be with the objects that
exist at v22. See `STAGE_A_TARGET_BUCKET_POLICY_IMPLEMENTATION.md`:
`reconstruction_corpus_versions` carries no target scope (no season, no date
range, no corpus→game membership), and `target_set_digest` is a free-text
caller-supplied label that no production code derives -- the audit runner
defaults it to the literal string ``identity-audit-no-targets``. So "enumerate
the parent corpus's official targets" has no authoritative answer to read.

Rather than invent a scoping rule and call it verified, the load-bearing entry
point REFUSES until a reviewed enumerator exists. The algorithm below is
independently testable now and is what that enumerator will feed.

The reviewed contract (Repair 4)
--------------------------------
From `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` and `NBA_F1R_TARGET_ANCHOR_PREFLIGHT.md`:

    1. Take the retrospective official start `S_final` as a SEARCH HINT ONLY.
    2. Query the historical snapshot at `S_final − 60 min`, floored to the grid.
    3. Read the contemporaneous `commence_time` FROM the snapshot.
    4. `T_cut := commence_time_snapshot − 60 min`  (bounded to 3 iterations)

Two distinct instants, and conflating them is the circularity Repair 4 exists to
prevent:

* the **first-pass REQUEST bucket** -- step 2 -- is computable before any
  provider contact, and is what a Stage-A plan declares. **That is what §AF
  verifies.**
* **`T_cut`** -- step 4 -- derives from the provider's contemporaneous
  `commence_time` and cannot be known at plan time at all.

Why using the retrospectively-known final start as the hint is not leakage:
it decides only WHEN to look, never WHAT is true. The scientific anchor remains
the snapshot's own `commence_time`. The preflight states this as
*"the retrospective final start is never the anchor"*.

The provider's snapshot grid was measured off the wall clock at roughly `:37`
seconds. That is an ANSWER, not a request: the request grid is exact 5-minute
UTC, and `provider_snapshot_timestamp` never participates in deriving a bucket.

This module performs no network and no provider I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Iterable, Mapping, Optional, Sequence

#: The frozen scientific policy. Its semantics are pinned by
#: `test_af_target_bucket_policy.py`; a semantic change requires a NEW version.
STAGE_A_TARGET_BUCKET_POLICY_V1: Final = "stage-a-plan-v1"

#: League this policy has been reviewed for. Anything else refuses.
SUPPORTED_LEAGUES: Final[frozenset[str]] = frozenset({"lg_nba"})

#: Repair 4 step 2. Minutes, not seconds, and SUBTRACTED.
DECISION_HORIZON_MINUTES: Final = 60

#: The request grid. Exact 5-minute UTC, floored DOWN.
BUCKET_FLOOR_SECONDS: Final = 300

#: The canonical instant spelling used throughout the corpus.
_CANONICAL = re.compile(
    r"\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?Z\Z")


class TargetBucketPolicyError(RuntimeError):
    """The scientific target→bucket policy failed. Always fails closed."""


class TargetPopulationUnavailable(TargetBucketPolicyError):
    """The parent corpus cannot enumerate its official target population.

    A RETAINED BLOCKER, not a transient error: at v22 nothing binds a corpus to
    its targets, so any enumeration would be an invented scoping rule rather
    than reviewed evidence.
    """


@dataclass(frozen=True)
class TargetHint:
    """One official target and the preserved official start that hints at it."""

    canonical_game_id: str
    #: `S_final` -- the retrospective official start. SEARCH HINT ONLY.
    official_start: str


@dataclass(frozen=True)
class DerivedTargetBucket:
    canonical_game_id: str
    official_start: str
    decision_instant: str
    requested_at_bucket: str


@dataclass(frozen=True)
class TargetBucketDerivation:
    """An independently recomputed target → bucket mapping."""

    policy_version: str
    league_id: str
    records: tuple[DerivedTargetBucket, ...]
    unresolved: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()

    def mapping(self) -> dict[str, str]:
        return {r.canonical_game_id: r.requested_at_bucket for r in self.records}

    def distinct_buckets(self) -> tuple[str, ...]:
        return tuple(sorted({r.requested_at_bucket for r in self.records}))

    @property
    def complete(self) -> bool:
        return not self.unresolved and not self.ambiguous


def parse_canonical_instant(value: str, *, what: str) -> datetime:
    """Strict UTC parse. No naive values, no offsets, no local timezone."""

    if not isinstance(value, str):
        raise TargetBucketPolicyError(
            f"{what} must be a string instant, got {type(value).__name__}")
    match = _CANONICAL.match(value)
    if not match:
        raise TargetBucketPolicyError(
            f"{what} {value!r} is not a canonical UTC instant "
            f"(YYYY-MM-DDTHH:MM:SS[.ffffff]Z); offsets, naive values and other "
            f"spellings are refused rather than normalized")
    year, month, day, hour, minute, second, micro = match.groups()
    if hour == "24":
        raise TargetBucketPolicyError(f"{what} {value!r} uses hour 24")
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute),
                        int(second), int(micro or 0), tzinfo=timezone.utc)
    except ValueError as exc:
        raise TargetBucketPolicyError(
            f"{what} {value!r} is not a real calendar instant: {exc}") from None


def _format(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def derive_requested_bucket(official_start: str) -> tuple[str, str]:
    """`S_final` -> (decision instant, first-pass request bucket).

    Order of operations is pinned deliberately, even though 60 minutes happens
    to be divisible by five so the two orderings agree today: SUBTRACT the exact
    horizon from the exact hint, THEN floor the result. The policy must say what
    it means, not merely produce today's output.
    """

    hint = parse_canonical_instant(official_start, what="official start hint")
    decision = hint - timedelta(minutes=DECISION_HORIZON_MINUTES)

    epoch_seconds = int(decision.replace(tzinfo=timezone.utc).timestamp())
    # FLOOR, never round or ceil: Python's // floors toward negative infinity,
    # which is the correct direction on both sides of the epoch.
    floored = (epoch_seconds // BUCKET_FLOOR_SECONDS) * BUCKET_FLOOR_SECONDS
    bucket = datetime.fromtimestamp(floored, tz=timezone.utc)
    return _format(decision), _format(bucket)


def derive_mapping(
    league_id: str,
    hints: Iterable[TargetHint],
    *,
    policy_version: str = STAGE_A_TARGET_BUCKET_POLICY_V1,
) -> TargetBucketDerivation:
    """Recompute the target → bucket mapping from official hints.

    Deterministic and order-independent: input order never reaches the output,
    which is sorted by canonical game id.
    """

    if policy_version != STAGE_A_TARGET_BUCKET_POLICY_V1:
        raise TargetBucketPolicyError(
            f"unknown target-bucket policy version {policy_version!r}; the only "
            f"frozen version is {STAGE_A_TARGET_BUCKET_POLICY_V1!r}")
    if league_id not in SUPPORTED_LEAGUES:
        raise TargetBucketPolicyError(
            f"league {league_id!r} has no reviewed target-bucket policy")

    seen: dict[str, str] = {}
    records: list[DerivedTargetBucket] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []

    for hint in hints:
        game_id = hint.canonical_game_id
        if not hint.official_start:
            unresolved.append(game_id)
            continue
        if game_id in seen:
            # Duplicate evidence resolves only when it is the SAME instant; two
            # different official starts for one target have no reviewed
            # deterministic resolution, so neither is chosen.
            if seen[game_id] != hint.official_start:
                ambiguous.append(game_id)
            continue
        seen[game_id] = hint.official_start
        decision, bucket = derive_requested_bucket(hint.official_start)
        records.append(DerivedTargetBucket(
            canonical_game_id=game_id, official_start=hint.official_start,
            decision_instant=decision, requested_at_bucket=bucket))

    resolved = {r.canonical_game_id for r in records}
    records = [r for r in records if r.canonical_game_id not in set(ambiguous)]
    return TargetBucketDerivation(
        policy_version=policy_version, league_id=league_id,
        records=tuple(sorted(records, key=lambda r: r.canonical_game_id)),
        unresolved=tuple(sorted(set(unresolved) - resolved)),
        ambiguous=tuple(sorted(set(ambiguous))))


def compare_to_declared(
    derivation: TargetBucketDerivation,
    declared_mapping: Mapping[str, str],
    declared_buckets: Sequence[str],
) -> tuple[str, ...]:
    """Exact keyed-set comparison. No tolerance; one wrong bucket fails.

    Both directions of the target set are compared, because the architecture's
    pigeonhole finding means a dropped target can leave the bucket SET
    byte-identical when another target shares its bucket.
    """

    failures: list[str] = []
    if not derivation.complete:
        if derivation.unresolved:
            failures.append(
                f"{len(derivation.unresolved)} target(s) have no admitted "
                f"official start: {list(derivation.unresolved)[:3]}")
        if derivation.ambiguous:
            failures.append(
                f"{len(derivation.ambiguous)} target(s) have conflicting official "
                f"starts with no reviewed resolution: "
                f"{list(derivation.ambiguous)[:3]}")

    expected = derivation.mapping()
    if set(expected) != set(declared_mapping):
        missing = sorted(set(expected) - set(declared_mapping))
        extra = sorted(set(declared_mapping) - set(expected))
        failures.append(
            f"declared target population differs from the independently derived "
            f"one (missing={missing[:3]}, extra={extra[:3]})")
    else:
        wrong = sorted(g for g in expected if expected[g] != declared_mapping[g])
        if wrong:
            failures.append(
                f"{len(wrong)} target(s) map to a bucket the policy does not "
                f"derive: " + ", ".join(
                    f"{g}: declared {declared_mapping[g]} != derived {expected[g]}"
                    for g in wrong[:3]))

    if set(declared_buckets) != set(derivation.distinct_buckets()):
        failures.append(
            "declared planned bucket set differs from the independently derived "
            "distinct bucket set")
    return tuple(failures)


def derive_target_population(
    conn: object, *, parent_corpus_id: str
) -> Sequence[TargetHint]:
    """RETAINED BLOCKER -- refuses.

    §AF requires the target population to come from the parent corpus, never
    from the manifest, so that an omitted target is visible. At v22 that is not
    derivable:

    * `reconstruction_corpus_versions` records `league_id` and policy versions
      but NO target scope -- no season, no date range, no corpus→game membership
      table. The only corpus-to-games link in the schema is
      `stage_a_plan_targets`, which is the plan's own claim and therefore
      exactly what must not be trusted.
    * `target_set_digest` is a free-text caller-supplied label. No production
      code derives it; the audit runner defaults it to the literal string
      ``identity-audit-no-targets``.
    * `reconstructed_input_provenance` is per-feature-input eligibility keyed by
      `provider_game_id`, not a target population, and is written only by an
      F1-R run, which is blocked.

    Enumerating "every NBA game in some month" from the `games` table would make
    the target set a property of whatever rows a database happens to hold rather
    than of the corpus, so two copies of the "same" corpus could derive
    different populations. That is an invented scoping rule, not evidence, so
    this refuses instead.
    """

    raise TargetPopulationUnavailable(
        f"corpus {parent_corpus_id!r} cannot enumerate its official target "
        f"population: reconstruction_corpus_versions carries no target scope and "
        f"target_set_digest is an underived caller-supplied label. §AF's bucket "
        f"algorithm is implemented and testable, but the corpus-bound target "
        f"population it must be applied to does not exist at v22. See "
        f"STAGE_A_TARGET_BUCKET_POLICY_IMPLEMENTATION.md.")


def verify_stage_a_target_bucket_policy(
    conn: object,
    *,
    plan_id: str,
    parent_corpus_id: str,
    repo_root: Optional[object] = None,
) -> tuple[str, ...]:
    """The load-bearing §AF verifier. Currently refuses -- see the blocker above.

    Kept as the single named entry point so that composing §AF into
    `certify_stage_a` later is a one-line change at a reviewed seam, rather than
    a new design decision taken under time pressure.
    """

    raise TargetPopulationUnavailable(
        f"§AF cannot verify plan {plan_id!r} against corpus {parent_corpus_id!r}: "
        f"the target population is not derivable from the parent corpus at v22")
