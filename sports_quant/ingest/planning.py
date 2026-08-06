"""F1A deterministic, zero-network request planner (offline).

Given only ``(provider, league, date range, families, stage, bounds)`` this
module enumerates the *semantic* requests a pilot would make and estimates a
conservative request/credit range -- **without any HTTP, DNS, auth, database, or
provider-audit call**. It distinguishes:

* **fixed** requests (known now: the MLB schedule range call);
* **schedule/list expansion** (paginated list, bounded by ``max_pages``);
* **per-game expansion** (bounded by ``max_games``);
* **per-team/date expansion** (bounded by ``max_games`` and the date span);
* **pagination** (bounded by ``max_pages`` / ``max_records``);
* **retries**, which are usage attempts, not separate semantic work -- folded
  into the conservative maximum via the retry factor, never as new units.

An unbounded contingent expansion (e.g. per-game families with no ``max_games``)
makes the plan **non-executable**: ``executable=False`` with the unresolved
bounds named. A skeleton stage (schedule/games only) is always boundable.

Nothing here imports a provider client or opens a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from ..request_control import EndpointCostPolicy, RequestUnit
from .cost_policies import build_balldontlie_policy, build_mlb_policy

PLAN_VERSION = "f1a-plan-v1"

MLB_SKELETON_FAMILIES: frozenset[str] = frozenset({"schedule"})
MLB_RICH_FAMILIES: frozenset[str] = frozenset({"results", "box", "inning", "rosters"})
NBA_SKELETON_FAMILIES: frozenset[str] = frozenset({"games"})
#: ``results`` belongs here even though it adds NO contingent and NO request: an
#: NBA game result is normalized from the ``/v1/games`` payload the per-game
#: ``game`` contingent already fetches (unlike MLB, whose results need their own
#: linescore call). Omitting the NAME made the family unreachable from any
#: manifest, so every NBA month run necessarily produced zero ``nba_game_results``
#: rows -- the one table the point-in-time dataset reads labels from.
NBA_RICH_FAMILIES: frozenset[str] = frozenset(
    {"results", "box", "stats", "advanced", "plays", "lineups", "quarters"}
)


@dataclass(frozen=True)
class Bounds:
    """Conservative caps that bound contingent fan-out (from CLI flags)."""

    max_games: Optional[int] = None
    max_pages: Optional[int] = None
    max_records: Optional[int] = None
    max_retries: int = 3
    #: Configured request-rate (requests/min) for a rate-limited provider (NBA).
    #: None = provider default; validated against the tier max when the gate builds.
    rate_per_min: Optional[int] = None

    @property
    def retry_factor(self) -> int:
        return 1 + max(0, self.max_retries)


@dataclass(frozen=True)
class Contingent:
    """One contingent expansion and its resolved/unresolved conservative bound."""

    kind: str
    family: str
    per_parent_min: int
    per_parent_max: Optional[int]  # None => unbounded
    parent_min: int
    parent_max: Optional[int]  # None => unbounded
    note: str = ""

    def request_min(self) -> int:
        return self.per_parent_min * self.parent_min

    def request_max(self) -> Optional[int]:
        if self.per_parent_max is None or self.parent_max is None:
            return None
        return self.per_parent_max * self.parent_max


#: Stage name for a targeted recovery that extends an already-executed run.
RECOVERY_STAGE = "lineup_continuation_recovery"

#: Contingent kind for CONTINUATION pages. Deliberately distinct from the month
#: plan's ``per_game`` lineups contingent, which reserves exactly one request per
#: game and is therefore the wrong shape for a paginated recovery.
CONTINUATION_KIND = "continuation"

#: Hard ceiling on continuation pages per target game, for any recovery plan.
MAX_CONTINUATION_PAGES = 8


@dataclass(frozen=True)
class RecoveryBinding:
    """What a targeted recovery is bound to in already-executed evidence.

    Every field participates in the plan (and therefore manifest) hash, so the
    identity of a recovery changes if the source corpus, the target set, or the
    page bound changes. That is the point: a manifest reviewed against one body
    of evidence must not silently execute against another.

    No cursor value appears here. Cursors are re-derived from the protected
    source database at execution time and checked against ``target_digest``, so
    the committed artifact stays a description of WHICH games are being recovered
    rather than a snapshot of provider pagination state.
    """

    purpose: str
    contract_version: str
    source_manifest_hash: str
    source_plan_hash: str
    source_database_fingerprint: str
    source_date_range: str
    source_selected_games: int
    target_count: int
    target_digest: str
    max_continuation_pages: int

    def body(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "contract_version": self.contract_version,
            "source_manifest_hash": self.source_manifest_hash,
            "source_plan_hash": self.source_plan_hash,
            "source_database_fingerprint": self.source_database_fingerprint,
            "source_date_range": self.source_date_range,
            "source_selected_games": self.source_selected_games,
            "target_count": self.target_count,
            "target_digest": self.target_digest,
            "max_continuation_pages": self.max_continuation_pages,
        }


@dataclass(frozen=True)
class RequestPlan:
    """A deterministic, secret-free request plan for one provider/league/stage."""

    provider: str
    league: str
    stage: str
    date_range: str
    families: tuple[str, ...]
    fixed_units: tuple[RequestUnit, ...]
    contingents: tuple[Contingent, ...]
    bounds: Bounds
    cost_policy_version: str
    credits_applicable: bool
    plan_version: str = PLAN_VERSION
    #: Present only for a targeted recovery plan. Absent (and omitted from the
    #: hashed body) for every ordinary plan, so existing plan identities are
    #: unchanged by this addition.
    recovery: Optional[RecoveryBinding] = None

    # -- estimation ----------------------------------------------------------
    def _family_credit(self, family: str) -> Optional[int]:
        policy = _policy_for(self.provider)
        return policy.cost_for(family)

    def semantic_requests_min(self) -> int:
        return len(self.fixed_units) + sum(c.request_min() for c in self.contingents)

    def semantic_requests_max(self) -> Optional[int]:
        total = len(self.fixed_units)
        for c in self.contingents:
            rmax = c.request_max()
            if rmax is None:
                return None
            total += rmax
        return total

    def requests_max_with_retries(self) -> Optional[int]:
        base = self.semantic_requests_max()
        return None if base is None else base * self.bounds.retry_factor

    def credits_min(self) -> Optional[int]:
        if not self.credits_applicable:
            return None
        total = 0
        for u in self.fixed_units:
            c = self._family_credit(u.endpoint_family)
            if c is None:
                return None
            total += c
        for cont in self.contingents:
            c = self._family_credit(cont.family)
            if c is None:
                return None
            total += c * cont.request_min()
        return total

    def credits_max(self) -> Optional[int]:
        if not self.credits_applicable:
            return None
        total = 0
        for u in self.fixed_units:
            c = self._family_credit(u.endpoint_family)
            if c is None:
                return None
            total += c
        for cont in self.contingents:
            c = self._family_credit(cont.family)
            rmax = cont.request_max()
            if c is None or rmax is None:
                return None
            total += c * rmax
        base = total
        return base * self.bounds.retry_factor

    def unresolved_bounds(self) -> tuple[str, ...]:
        out: list[str] = []
        for c in self.contingents:
            if c.request_max() is None:
                out.append(f"{c.kind}:{c.family} ({c.note or 'needs a bound'})")
        if self.credits_applicable:
            for fam in {u.endpoint_family for u in self.fixed_units} | {
                c.family for c in self.contingents
            }:
                if self._family_credit(fam) is None:
                    out.append(f"unknown_credit_cost:{fam}")
        return tuple(sorted(set(out)))

    def executable(self) -> bool:
        return self.semantic_requests_max() is not None and not self.unresolved_bounds()

    def required_request_cap(self) -> Optional[int]:
        return self.requests_max_with_retries()

    def required_credit_cap(self) -> Optional[int]:
        return self.credits_max()


def _policy_for(provider: str) -> EndpointCostPolicy:
    return build_balldontlie_policy() if provider == "balldontlie" else build_mlb_policy()


def _days_in_range(from_date: str, to_date: Optional[str]) -> int:
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date) if to_date else start
    if end < start:
        raise ValueError("to_date precedes from_date")
    return (end - start).days + 1


def _range_key(from_date: str, to_date: Optional[str]) -> str:
    return from_date if not to_date or to_date == from_date else f"{from_date}..{to_date}"


# --------------------------------------------------------------------------- #
# MLB planner
# --------------------------------------------------------------------------- #
def plan_mlb(
    *,
    from_date: str,
    to_date: Optional[str],
    families: tuple[str, ...],
    stage: str,
    bounds: Bounds,
) -> RequestPlan:
    rng = _range_key(from_date, to_date)
    fixed = [
        RequestUnit(provider="mlb_statsapi", league="mlb", endpoint_family="schedule",
                    date_key=rng)
    ]
    contingents: list[Contingent] = []
    fam = set(families)
    if stage == "rich" and fam & MLB_RICH_FAMILIES:
        gmax = bounds.max_games  # None => unbounded => non-executable
        # Each rich game is fetched via single-game mode, which re-fetches that
        # game's schedule (hydrated) before its per-game data -- modeled here so the
        # plan maximum bounds the executor's ACTUAL fan-out.
        contingents.append(Contingent(
            kind="per_game", family="game_schedule", per_parent_min=1, per_parent_max=1,
            parent_min=0, parent_max=gmax,
            note="single-game schedule re-fetch per selected game; needs --max-games"))
        if fam & {"results", "inning"}:
            contingents.append(Contingent(
                kind="per_game", family="game_linescore", per_parent_min=1, per_parent_max=1,
                parent_min=0, parent_max=gmax,
                note="linescore per game (shared by results+inning); needs --max-games"))
        if "box" in fam:
            contingents.append(Contingent(
                kind="per_game", family="game_boxscore", per_parent_min=1, per_parent_max=1,
                parent_min=0, parent_max=gmax, note="boxscore per game; needs --max-games"))
        if "rosters" in fam:
            contingents.append(Contingent(
                kind="per_team_date", family="roster", per_parent_min=1, per_parent_max=2,
                parent_min=0, parent_max=gmax,
                note="~2 team-date rosters per game; needs --max-games"))
    return RequestPlan(
        provider="mlb_statsapi", league="mlb", stage=stage, date_range=rng,
        families=tuple(sorted(fam)), fixed_units=tuple(fixed), contingents=tuple(contingents),
        bounds=bounds, cost_policy_version=build_mlb_policy().version, credits_applicable=False,
    )


# --------------------------------------------------------------------------- #
# NBA planner
# --------------------------------------------------------------------------- #
def plan_nba(
    *,
    from_date: str,
    to_date: Optional[str],
    families: tuple[str, ...],
    stage: str,
    bounds: Bounds,
) -> RequestPlan:
    rng = _range_key(from_date, to_date)
    _days_in_range(from_date, to_date)  # validate range ordering (raises on reversed)
    fam = set(families)
    contingents: list[Contingent] = []
    # Games list is paginated (bounded by --max-pages); it is the skeleton.
    contingents.append(Contingent(
        kind="pagination", family="games", per_parent_min=1, per_parent_max=bounds.max_pages,
        parent_min=1, parent_max=1, note="games list pages; needs --max-pages"))
    if stage == "rich" and fam & NBA_RICH_FAMILIES:
        gmax = bounds.max_games
        # Each rich game is fetched via single-game mode: fetch_game(id) per game.
        contingents.append(Contingent(
            kind="per_game", family="game", per_parent_min=1, per_parent_max=1,
            parent_min=0, parent_max=gmax,
            note="single-game fetch per selected game; needs --max-games"))
        if "box" in fam or "quarters" in fam:
            # In single-game mode box is fetched once per selected game (that game's
            # date); box also backs derived quarter lines (quarters -> box_scores).
            contingents.append(Contingent(
                kind="per_game", family="box_scores", per_parent_min=1, per_parent_max=1,
                parent_min=0, parent_max=gmax,
                note="box per selected game (also backs quarters); needs --max-games"))
        for family in ("stats", "advanced", "plays", "lineups"):
            if family in fam:
                efam = {"stats": "stats", "advanced": "advanced_stats",
                        "plays": "plays", "lineups": "lineups"}[family]
                per_max = 1 if family == "lineups" else bounds.max_pages
                contingents.append(Contingent(
                    kind="per_game", family=efam, per_parent_min=1, per_parent_max=per_max,
                    parent_min=0, parent_max=gmax,
                    note=f"{family} per game{'' if family == 'lineups' else ' (paginated)'}; "
                         "needs --max-games" + ("" if family == "lineups" else " and --max-pages")))
    return RequestPlan(
        provider="balldontlie", league="nba", stage=stage, date_range=rng,
        families=tuple(sorted(fam)), fixed_units=(), contingents=tuple(contingents),
        bounds=bounds, cost_policy_version=build_balldontlie_policy().version,
        # BALLDONTLIE is request-RATE limited, not credit metered -> credits N/A;
        # executability now depends only on bounded request fan-out.
        credits_applicable=False,
    )


def build_plan(
    *,
    league: str,
    from_date: str,
    to_date: Optional[str],
    families: tuple[str, ...],
    stage: str,
    bounds: Bounds,
) -> RequestPlan:
    """Deterministic entry point used by the CLI ``--plan`` mode and the runner."""

    if league == "mlb":
        return plan_mlb(from_date=from_date, to_date=to_date, families=families,
                        stage=stage, bounds=bounds)
    if league == "nba":
        return plan_nba(from_date=from_date, to_date=to_date, families=families,
                        stage=stage, bounds=bounds)
    raise ValueError(f"unsupported league {league!r}")


class RecoveryPlanError(ValueError):
    """A targeted-recovery plan was refused. Message is sanitized."""


def plan_lineup_continuation(
    *,
    date_range: str,
    binding: RecoveryBinding,
    bounds: Bounds,
) -> RequestPlan:
    """Plan the bounded NBA lineup-continuation recovery.

    Shape: one parent per TARGET GAME, and 1..``max_continuation_pages``
    continuation requests per parent. This is deliberately NOT the month plan's
    ``per_game`` lineups contingent -- that reserves exactly one request per game,
    which is precisely the bound that left 40 games truncated. The first page is
    already preserved in the source corpus and is never re-requested, so every
    request this plan reserves is a continuation.

    For the March 2026 recovery the arithmetic is
    ``40 targets x 8 pages = 320`` semantic requests and, at ``max_retries=1``,
    ``320 x 2 = 640`` attempts.

    Fails closed rather than producing an unbounded or mis-scoped plan.
    """

    if binding.purpose != "lineup_continuation_recovery":
        raise RecoveryPlanError(
            f"unsupported recovery purpose {binding.purpose!r}")
    if binding.target_count <= 0:
        raise RecoveryPlanError(
            "a recovery plan needs at least one target game; nothing to recover")
    if not binding.target_digest.strip():
        raise RecoveryPlanError("a recovery plan requires a target-set digest")
    if not binding.source_database_fingerprint.strip():
        raise RecoveryPlanError(
            "a recovery plan requires the source database fingerprint it is bound to")
    if not (binding.source_manifest_hash.strip() and binding.source_plan_hash.strip()):
        raise RecoveryPlanError(
            "a recovery plan requires the source manifest and plan hashes")
    if binding.source_date_range != date_range:
        raise RecoveryPlanError(
            "recovery date_range must equal the source range it extends")
    if bounds.max_pages is None:
        raise RecoveryPlanError(
            "a recovery plan requires an explicit max_pages continuation bound")
    if bounds.max_pages > MAX_CONTINUATION_PAGES:
        raise RecoveryPlanError(
            f"max_pages {bounds.max_pages} exceeds the authorized "
            f"{MAX_CONTINUATION_PAGES} continuation pages per target game")
    if bounds.max_pages != binding.max_continuation_pages:
        raise RecoveryPlanError(
            "bounds.max_pages and the recovery binding disagree on the "
            "continuation page limit")
    if bounds.max_games is not None and bounds.max_games != binding.target_count:
        raise RecoveryPlanError(
            "bounds.max_games must equal the recovery target count")

    contingent = Contingent(
        kind=CONTINUATION_KIND, family="lineups",
        per_parent_min=1, per_parent_max=bounds.max_pages,
        parent_min=binding.target_count, parent_max=binding.target_count,
        note=("continuation pages per target game; the preserved first page is "
              "never re-requested"),
    )
    return RequestPlan(
        provider="balldontlie", league="nba", stage=RECOVERY_STAGE,
        date_range=date_range,
        # Exactly one family. A recovery that could touch another endpoint would
        # not be a recovery.
        families=("lineups",),
        fixed_units=(), contingents=(contingent,), bounds=bounds,
        cost_policy_version=build_balldontlie_policy().version,
        credits_applicable=False,
        recovery=binding,
    )
