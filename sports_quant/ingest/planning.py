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
from typing import Optional

from ..request_control import EndpointCostPolicy, RequestUnit
from .cost_policies import build_balldontlie_policy, build_mlb_policy

PLAN_VERSION = "f1a-plan-v1"

MLB_SKELETON_FAMILIES: frozenset[str] = frozenset({"schedule"})
MLB_RICH_FAMILIES: frozenset[str] = frozenset({"results", "box", "inning", "rosters"})
NBA_SKELETON_FAMILIES: frozenset[str] = frozenset({"games"})
NBA_RICH_FAMILIES: frozenset[str] = frozenset(
    {"box", "stats", "advanced", "plays", "lineups", "quarters"}
)


@dataclass(frozen=True)
class Bounds:
    """Conservative caps that bound contingent fan-out (from CLI flags)."""

    max_games: Optional[int] = None
    max_pages: Optional[int] = None
    max_records: Optional[int] = None
    max_retries: int = 3

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
        credits_applicable=True,
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
