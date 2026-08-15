"""Historical-market target anchoring for Lane-R / F1-R. Zero network here.

What this module is
-------------------
The executable form of ``HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md`` **Repair 4**:
a target's ``T_cut`` is derived from the **contemporaneous** ``commence_time``
carried by a provider historical snapshot, never from the retrospectively known
final start. The original design anchored on ``scheduled_start - 60 min``, which
is circular, because ``scheduled_start`` is only knowable after the fact.

The decisive sentence, quoted from the reviewed architecture:

    *"``commence_time`` from the snapshot is the availability evidence. The
    retrospective final start is never the anchor."*

So ``S_final`` enters this module as a **search hint** and in exactly one place:
choosing which snapshot to look at first. It is never written into an anchor, and
a resolution that never reached a snapshot cannot produce one.

Evidence grade
--------------
The snapshot source this is built against is the historical **events** endpoint,
which reports the events that had odds available at an instant but carries no
prices. Under the reviewed economic-evidence grades that is **E0** -- "market
existed, no price" -- which the architecture admits for **target anchoring only**
and forbids for any EV claim. Economic backtesting still needs **E1** (a
timestamped price), and nothing here supplies it.

What this module is not
-----------------------
It makes no request. Every snapshot arrives through an injected
:class:`SnapshotSource`, so the resolver is a pure function of evidence and the
whole of it is testable offline. It builds no target population, certifies no
row, computes no feature, and -- see :class:`IdentityResolution` -- refuses by
default to link a provider event to a canonical game at all, because no reviewed
exact-identity path exists yet.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final, Optional, Protocol, Sequence

__all__ = [
    "ANCHOR_POLICY_VERSION",
    "ARCHIVE_START",
    "CUTOFF_LEAD",
    "GRID_CHANGE",
    "LEGACY_GRID_SECONDS",
    "MAX_ITERATIONS",
    "MODERN_GRID_SECONDS",
    "AnchorOutcome",
    "AnchorResolution",
    "BudgetExceeded",
    "IdentityResolution",
    "IdentityUnresolved",
    "RefuseNameMatching",
    "RequestBudget",
    "RequestPlan",
    "SnapshotEvent",
    "SnapshotSource",
    "SnapshotView",
    "plan_snapshot_requests",
    "resolve_target_anchor",
    "snapshot_grid_seconds",
    "floor_to_snapshot_grid",
]

#: Bumped whenever the anchoring rule changes. Written alongside any anchor so a
#: row can never be silently reinterpreted under a later rule.
ANCHOR_POLICY_VERSION: Final[str] = "historical-market-anchor-repair4-v1"

#: The provider archive does not exist before this instant.
ARCHIVE_START: Final[datetime] = datetime(2020, 6, 6, tzinfo=timezone.utc)

#: Snapshot cadence changed here: 10-minute before, 5-minute from.
GRID_CHANGE: Final[datetime] = datetime(2022, 9, 18, tzinfo=timezone.utc)

MODERN_GRID_SECONDS: Final[int] = 300
LEGACY_GRID_SECONDS: Final[int] = 600

#: Repair 4 step 4: ``T_cut := commence_time_snapshot - 60 min``.
CUTOFF_LEAD: Final[timedelta] = timedelta(minutes=60)

#: Repair 4 step 4: iteration is bounded, and non-convergence is a rejection --
#: not something to be resolved by looking one more time.
MAX_ITERATIONS: Final[int] = 3


# --------------------------------------------------------------------------- #
# Snapshot grid
# --------------------------------------------------------------------------- #
def _require_utc(value: datetime, label: str) -> datetime:
    """Reject naive datetimes outright rather than assuming they mean UTC.

    A naive instant in a point-in-time system is an unanswered question about
    which clock produced it. Guessing is how a local-time start leaks in as if
    it were UTC and shifts every cutoff by hours.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware; naive datetimes are refused")
    return value.astimezone(timezone.utc)


def snapshot_grid_seconds(instant: datetime) -> int:
    """The provider's snapshot cadence in effect at ``instant``."""

    return MODERN_GRID_SECONDS if _require_utc(instant, "instant") >= GRID_CHANGE else LEGACY_GRID_SECONDS


def floor_to_snapshot_grid(instant: datetime) -> datetime:
    """Floor ``instant`` DOWN to the provider snapshot grid, in UTC.

    Always downward, including for instants before the epoch: rounding up would
    ask for a snapshot later than the intended cutoff, which is the one direction
    that can leak post-cutoff information into a target. An instant already on
    the grid is returned unchanged, and sub-second precision is discarded because
    the grid has none.
    """

    at = _require_utc(instant, "instant")
    grid = snapshot_grid_seconds(at)
    # Integer seconds throughout: float epochs lose precision at the boundary,
    # and an instant exactly on the grid must stay exactly on it. Dropping
    # microseconds always moves an instant earlier, which is the safe direction.
    delta = at.replace(microsecond=0) - ARCHIVE_START
    total = delta.days * 86_400 + delta.seconds
    return ARCHIVE_START + timedelta(seconds=(total // grid) * grid)


# --------------------------------------------------------------------------- #
# Evidence types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SnapshotEvent:
    """One event as it stood at a snapshot instant.

    ``commence_time`` is the CONTEMPORANEOUS value read out of the snapshot. It
    is the only start time the anchoring rule may consume.
    """

    event_id: str
    sport_key: str
    commence_time: Optional[datetime]
    home_team: Optional[str] = None
    away_team: Optional[str] = None


@dataclass(frozen=True)
class SnapshotView:
    """A historical snapshot: which instant actually answered, and what it held.

    ``timestamp`` is the provider's snapshot instant. ``requested_at`` is merely
    what was asked for. The provider answers with the nearest snapshot at or
    before the request, so the two are routinely different, and every downstream
    comparison in this module uses ``timestamp``. Keeping both, separately named,
    is what stops the request clock from being mistaken for the evidence clock.
    """

    timestamp: datetime
    requested_at: datetime
    events: tuple[SnapshotEvent, ...] = ()

    def find(self, event_id: str) -> Optional[SnapshotEvent]:
        """Exact id lookup. There is deliberately no name-based fallback."""

        for event in self.events:
            if event.event_id == event_id:
                return event
        return None


class SnapshotSource(Protocol):
    """Supplies snapshots. Injected, so the resolver never opens a socket.

    A live implementation would wrap ``OddsApiClient.get_historical_events``;
    tests supply a dict. The resolver cannot tell the difference, which is the
    point: its logic is exercised in full with zero credits spent.
    """

    def fetch(self, *, sport_key: str, at: datetime) -> Optional[SnapshotView]:
        """Return the snapshot answering ``at``, or ``None`` if there is none."""


# --------------------------------------------------------------------------- #
# Identity -- the retained blocker
# --------------------------------------------------------------------------- #
class IdentityUnresolved(RuntimeError):
    """No exact link exists between a provider event and a canonical game."""


class IdentityResolution(Protocol):
    """Maps a canonical game id to a provider event id, EXACTLY or not at all."""

    def provider_event_id(self, *, canonical_game_id: str, sport_key: str) -> str:
        """Return the provider event id, or raise :class:`IdentityUnresolved`."""


class RefuseNameMatching:
    """The default resolution: refuse, and say why.

    RETAINED BLOCKER. A historical event carries ``home_team``/``away_team`` as
    provider display NAMES, not ids that join to the audited TEAM-A crosswalk.
    The repository's only existing bridge is the production sportsbook matcher,
    which resolves by provider-scoped alias and normalized key -- i.e. by name.
    Name matching is not admissible Lane-R identity evidence and no reviewed
    exact path has been authorized, so this class refuses rather than quietly
    introducing fuzzy matching into a lane whose whole value is exactness.

    Everything else in this module is complete and tested against it. What is
    missing is one architectural decision, not code.
    """

    reason: Final[str] = (
        "No reviewed exact identity path links an Odds API historical event to a "
        "canonical game. Historical events expose team NAMES only; the existing "
        "sportsbook matcher resolves by alias/normalized key, which is name "
        "matching and inadmissible as Lane-R identity evidence."
    )

    def provider_event_id(self, *, canonical_game_id: str, sport_key: str) -> str:
        raise IdentityUnresolved(
            f"{self.reason} (canonical_game_id={canonical_game_id!r}, "
            f"sport_key={sport_key!r})"
        )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
class AnchorOutcome(str, enum.Enum):
    """Why a resolution ended. Every non-``RESOLVED`` value is a rejection."""

    RESOLVED = "resolved"
    #: Step 6: no snapshot exists at the cutoff.
    NO_MARKET_AT_CUTOFF = "no_market_at_cutoff"
    #: Step 6: the event is absent from the snapshot that answered.
    EVENT_ABSENT = "event_absent"
    #: Step 6: the snapshot carries no contemporaneous commence_time.
    MISSING_COMMENCE_TIME = "missing_commence_time"
    #: Step 5: the event had already commenced at the snapshot instant.
    ALREADY_COMMENCED = "already_commenced"
    #: Step 4: the bounded iteration did not settle.
    NO_CONVERGENCE = "no_convergence"
    #: The hint precedes the provider archive, so no evidence can exist.
    BEFORE_ARCHIVE_START = "before_archive_start"
    #: No exact provider/canonical identity -- see :class:`RefuseNameMatching`.
    IDENTITY_UNRESOLVED = "identity_unresolved"


@dataclass(frozen=True)
class AnchorResolution:
    """The result, including the full audit trail of what was consulted."""

    outcome: AnchorOutcome
    canonical_game_id: str
    sport_key: str
    policy_version: str = ANCHOR_POLICY_VERSION
    #: Populated only when ``outcome is RESOLVED``.
    cutoff: Optional[datetime] = None
    commence_time_snapshot: Optional[datetime] = None
    snapshot_timestamp: Optional[datetime] = None
    provider_event_id: Optional[str] = None
    #: Every instant requested, in order. One entry per iteration performed.
    requested_instants: tuple[datetime, ...] = ()
    iterations: int = 0
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome is AnchorOutcome.RESOLVED


def _reject(
    outcome: AnchorOutcome,
    *,
    canonical_game_id: str,
    sport_key: str,
    requested: Sequence[datetime],
    iterations: int,
    detail: str,
) -> AnchorResolution:
    return AnchorResolution(
        outcome=outcome,
        canonical_game_id=canonical_game_id,
        sport_key=sport_key,
        requested_instants=tuple(requested),
        iterations=iterations,
        detail=detail,
    )


def resolve_target_anchor(
    *,
    canonical_game_id: str,
    sport_key: str,
    search_hint: datetime,
    source: SnapshotSource,
    identity: Optional[IdentityResolution] = None,
    budget: Optional["RequestBudget"] = None,
) -> AnchorResolution:
    """Resolve one target's ``T_cut`` per Repair 4. Fails closed throughout.

    ``search_hint`` is the retrospectively known start. It selects the first
    snapshot to look at and has no other effect: it is never compared against,
    never returned, and cannot become the anchor even if every iteration fails.

    The loop is Repair 4 steps 2-4. Each pass reads the contemporaneous
    ``commence_time``, recomputes ``T_cut = commence_time - 60 min`` floored to
    the grid, and stops once the instant it would request next is the one it just
    requested. That is convergence; running out of iterations is a rejection.
    """

    identity = identity if identity is not None else RefuseNameMatching()
    hint = _require_utc(search_hint, "search_hint")
    requested: list[datetime] = []

    if hint < ARCHIVE_START:
        return _reject(
            AnchorOutcome.BEFORE_ARCHIVE_START,
            canonical_game_id=canonical_game_id,
            sport_key=sport_key,
            requested=requested,
            iterations=0,
            detail=f"search hint {hint.isoformat()} precedes archive start "
            f"{ARCHIVE_START.isoformat()}",
        )

    try:
        event_id = identity.provider_event_id(
            canonical_game_id=canonical_game_id, sport_key=sport_key
        )
    except IdentityUnresolved as exc:
        return _reject(
            AnchorOutcome.IDENTITY_UNRESOLVED,
            canonical_game_id=canonical_game_id,
            sport_key=sport_key,
            requested=requested,
            iterations=0,
            detail=str(exc),
        )

    target = floor_to_snapshot_grid(hint - CUTOFF_LEAD)

    for iteration in range(1, MAX_ITERATIONS + 1):
        if budget is not None:
            budget.charge(requests=1, credits=1)
        requested.append(target)
        snapshot = source.fetch(sport_key=sport_key, at=target)

        if snapshot is None:
            return _reject(
                AnchorOutcome.NO_MARKET_AT_CUTOFF,
                canonical_game_id=canonical_game_id,
                sport_key=sport_key,
                requested=requested,
                iterations=iteration,
                detail=f"no snapshot at {target.isoformat()}",
            )

        snapshot_ts = _require_utc(snapshot.timestamp, "snapshot.timestamp")
        event = snapshot.find(event_id)
        if event is None:
            return _reject(
                AnchorOutcome.EVENT_ABSENT,
                canonical_game_id=canonical_game_id,
                sport_key=sport_key,
                requested=requested,
                iterations=iteration,
                detail=f"event {event_id!r} absent from snapshot "
                f"{snapshot_ts.isoformat()}",
            )
        if event.commence_time is None:
            return _reject(
                AnchorOutcome.MISSING_COMMENCE_TIME,
                canonical_game_id=canonical_game_id,
                sport_key=sport_key,
                requested=requested,
                iterations=iteration,
                detail=f"snapshot {snapshot_ts.isoformat()} carries no "
                f"commence_time for {event_id!r}",
            )

        commence = _require_utc(event.commence_time, "commence_time")
        # Step 5: the snapshot's own clock must precede the start it reports.
        # Compared against the SNAPSHOT timestamp, not the requested instant --
        # the provider may have answered with an earlier or later snapshot.
        if commence <= snapshot_ts:
            return _reject(
                AnchorOutcome.ALREADY_COMMENCED,
                canonical_game_id=canonical_game_id,
                sport_key=sport_key,
                requested=requested,
                iterations=iteration,
                detail=f"event had commenced: commence_time "
                f"{commence.isoformat()} <= snapshot {snapshot_ts.isoformat()}",
            )

        cutoff = floor_to_snapshot_grid(commence - CUTOFF_LEAD)
        if cutoff == target:
            return AnchorResolution(
                outcome=AnchorOutcome.RESOLVED,
                canonical_game_id=canonical_game_id,
                sport_key=sport_key,
                cutoff=cutoff,
                commence_time_snapshot=commence,
                snapshot_timestamp=snapshot_ts,
                provider_event_id=event_id,
                requested_instants=tuple(requested),
                iterations=iteration,
                detail="contemporaneous commence_time is stable at the cutoff",
            )
        target = cutoff

    return _reject(
        AnchorOutcome.NO_CONVERGENCE,
        canonical_game_id=canonical_game_id,
        sport_key=sport_key,
        requested=requested,
        iterations=MAX_ITERATIONS,
        detail=f"cutoff still moving after {MAX_ITERATIONS} iterations: "
        + " -> ".join(instant.isoformat() for instant in requested),
    )


# --------------------------------------------------------------------------- #
# Budget and dry-run planning
# --------------------------------------------------------------------------- #
class BudgetExceeded(RuntimeError):
    """A charge would breach a cap. Raised BEFORE the request is made."""


@dataclass
class RequestBudget:
    """A hard cap on both requests and credits, enforced ahead of each call.

    Two caps, not one, because they are not the same risk. A request count
    bounds provider load; a credit count bounds spend, and a single request to a
    priced endpoint can cost ten or more credits. Enforcing only requests would
    let a small number of calls run up a large bill.

    ``charge`` refuses before anything is spent, so a breach costs nothing.
    """

    max_requests: int = 10
    max_credits: int = 100
    requests_used: int = 0
    credits_used: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.max_credits < 0:
            raise ValueError("budget caps must be non-negative")

    @property
    def requests_remaining(self) -> int:
        return max(0, self.max_requests - self.requests_used)

    @property
    def credits_remaining(self) -> int:
        return max(0, self.max_credits - self.credits_used)

    def charge(self, *, requests: int = 1, credits: int = 1) -> None:
        """Reserve budget, or raise. Nothing is charged on a refusal."""

        if requests < 0 or credits < 0:
            raise ValueError("a charge must be non-negative")
        if self.requests_used + requests > self.max_requests:
            raise BudgetExceeded(
                f"request cap reached: {self.requests_used}+{requests} > "
                f"{self.max_requests}"
            )
        if self.credits_used + credits > self.max_credits:
            raise BudgetExceeded(
                f"credit cap reached: {self.credits_used}+{credits} > "
                f"{self.max_credits}"
            )
        self.requests_used += requests
        self.credits_used += credits


@dataclass(frozen=True)
class RequestPlan:
    """A dry-run plan: exactly which instants a run would request, and the cost.

    Deduplicated, because one snapshot answers for every game sharing a bucket --
    the saving is a property of the grid, not an optimization to be trusted
    blindly, so the mapping from bucket to games is kept for inspection.
    """

    sport_key: str
    instants: tuple[datetime, ...]
    games_by_instant: dict[datetime, tuple[str, ...]] = field(default_factory=dict)
    credits_per_request: int = 1

    @property
    def request_count(self) -> int:
        return len(self.instants)

    @property
    def credit_cost(self) -> int:
        return self.request_count * self.credits_per_request

    def within(self, budget: RequestBudget) -> bool:
        return (
            self.request_count <= budget.requests_remaining
            and self.credit_cost <= budget.credits_remaining
        )


def plan_snapshot_requests(
    *,
    sport_key: str,
    hints: dict[str, datetime],
    credits_per_request: int = 1,
) -> RequestPlan:
    """Compute the FIRST-PASS request plan without contacting anything.

    First pass only: iteration in :func:`resolve_target_anchor` may request
    further instants once contemporaneous commence times are known, and those
    cannot be predicted from retrospective hints. Treating this count as the
    total would understate the cost.
    """

    buckets: dict[datetime, list[str]] = {}
    for game_id, hint in hints.items():
        instant = floor_to_snapshot_grid(_require_utc(hint, "hint") - CUTOFF_LEAD)
        buckets.setdefault(instant, []).append(game_id)

    instants = tuple(sorted(buckets))
    return RequestPlan(
        sport_key=sport_key,
        instants=instants,
        games_by_instant={k: tuple(sorted(buckets[k])) for k in instants},
        credits_per_request=credits_per_request,
    )
