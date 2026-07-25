"""Official-game canonicalization + the ``match-games`` orchestration (task §9-14).

There is exactly one canonical game system: official provider identities stay in
``provider_game_references``; canonical identity is a ``games`` row. This service
reads the current schedule observation for each in-scope provider game, resolves
its teams and venue deterministically, computes the venue-local date, then links
the provider reference to an existing canonical game or creates one -- recording
a complete ``entity_match_decisions`` row (with normalized candidates) before any
link, and refusing to guess an ambiguous or unknown entity.

Nothing here touches the network, sportsbook events, or Kalshi markets. In
``dry_run`` mode it computes the identical decisions and counters and persists
nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from ..db.models import Game, Venue
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.games import SqliteGameRepository
from ..db.repositories.leagues import SqliteLeagueRepository, SqliteSeasonRepository
from ..db.repositories.matching import CandidateInput, SqliteMatchingRepository
from ..db.repositories.official_games import SqliteScheduleRepository
from ..db.repositories.references import LinkOutcome, SqliteProviderReferenceRepository
from ..db.repositories.venues import SqliteVenueRepository
from .localdate import InvalidTimezoneError, resolve_local_date
from .model import (
    AMBIGUOUS,
    MATCHER_VERSION,
    SCORE_OFFICIAL_KEY,
    SCORE_SCHEDULE_EXACT,
    SCORE_SCHEDULE_SWAPPED,
    SCORE_SCHEDULE_WINDOW,
    THRESHOLD,
    TIER_OFFICIAL_KEY,
    TIER_SCHEDULE_EXACT,
    TIER_SCHEDULE_SWAPPED,
    TIER_SCHEDULE_WINDOW,
    Resolution,
)
from .teams import TeamResolver
from .venues import VenueResolver

#: Providers whose game key we treat as an official anchor, and their league.
_PROVIDER_LEAGUE = {"mlb_statsapi": "MLB", "balldontlie": "NBA"}
_OFFICIAL_PROVIDERS = frozenset(_PROVIDER_LEAGUE)
_SPORT_PROVIDERS = {"mlb": "mlb_statsapi", "nba": "balldontlie"}

_SCHEDULE_EXACT_WINDOW = timedelta(minutes=90)
_SCHEDULE_WIDE_WINDOW = timedelta(hours=12)
_DOUBLEHEADER_GAP = timedelta(minutes=90)

# schedule.mapped_status -> games.status (a002 vocabulary).
_STATUS_MAP = {
    "scheduled": "scheduled", "pregame": "pregame", "warmup": "pregame",
    "in_progress": "in_progress", "delayed": "delayed", "postponed": "postponed",
    "suspended": "suspended", "final": "final", "cancelled": "cancelled",
    "rescheduled": "rescheduled", "unknown": "scheduled",
}
# MLB StatsAPI game_type / NBA season_type -> canonical season phase.
_POSTSEASON_TYPES = frozenset({"P", "D", "L", "W", "F", "C", "postseason", "playoff", "playoffs"})
_PRESEASON_TYPES = frozenset({"S", "E", "preseason", "exhibition"})


@dataclass
class MatchCounters:
    """Every counter the report and CLI JSON expose (task §12)."""

    decisions_evaluated: int = 0
    accepted: int = 0
    ambiguous: int = 0
    no_candidate: int = 0
    rejected: int = 0
    manual_review_required: int = 0
    candidates_recorded: int = 0
    provider_references_linked: int = 0
    canonical_games_created: int = 0
    canonical_entities_unchanged: int = 0
    dq_issues: int = 0
    blocking_issues: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class MatchGamesResult:
    """Outcome of a ``match-games`` run."""

    dry_run: bool
    status: str = "succeeded"
    games_considered: int = 0
    counters: MatchCounters = field(default_factory=MatchCounters)
    notes: list[str] = field(default_factory=list)
    run_id: Optional[str] = None

    @property
    def needs_failure_exit(self) -> bool:
        # Blocking identity/orientation contradictions make the corpus unfit.
        return self.counters.blocking_issues > 0

    def note(self, message: str) -> None:
        self.notes.append(message)


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _season_phase(game_type: Optional[str]) -> str:
    if game_type is None:
        return "regular"
    token = game_type.strip()
    if token in _POSTSEASON_TYPES or token.lower() in _POSTSEASON_TYPES:
        return "postseason"
    if token in _PRESEASON_TYPES or token.lower() in _PRESEASON_TYPES:
        return "preseason"
    return "regular"


class MatchGamesService:
    """Resolves teams, venues and official games for a bounded set of games."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        matcher_version: str = MATCHER_VERSION,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._run_id = run_id
        self._version = matcher_version
        self._schedule = SqliteScheduleRepository(conn)
        self._games = SqliteGameRepository(conn)
        self._refs = SqliteProviderReferenceRepository(conn)
        self._venues = SqliteVenueRepository(conn)
        self._leagues = SqliteLeagueRepository(conn)
        self._seasons = SqliteSeasonRepository(conn)
        self._match = SqliteMatchingRepository(conn)
        self._dq = SqliteDataQualityRepository(conn)
        self._team_resolver = TeamResolver(conn)
        self._venue_resolver = VenueResolver(conn)
        self._current_provider: Optional[str] = None
        # Source provenance of the schedule observation currently being matched,
        # attached to every decision derived from it (team / venue / game).
        self._current_raw_response_id: Optional[str] = None

    # -- public entry points ------------------------------------------------- #
    def match_range(
        self,
        *,
        provider: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider_game_id: Optional[str] = None,
    ) -> MatchGamesResult:
        result = MatchGamesResult(dry_run=self._dry_run, run_id=self._run_id)
        self._current_provider = provider
        rows: list[sqlite3.Row] = []
        if provider_game_id is not None:
            row = self._schedule.latest_for_provider_game(provider, provider_game_id)
            if row is not None:
                rows = [row]
        elif from_date is not None and to_date is not None:
            rows = self._schedule.latest_games_in_date_range(provider, from_date, to_date)
        result.games_considered = len(rows)
        # Deterministic order: by provider_game_id, so a rebuild processes identically.
        for row in sorted(rows, key=lambda r: str(r["provider_game_id"])):
            self._canonicalize_game(provider, row, result)
        result.status = "partially_failed" if result.needs_failure_exit else "succeeded"
        return result

    # -- one game ------------------------------------------------------------ #
    def _canonicalize_game(
        self, provider: str, sched: sqlite3.Row, result: MatchGamesResult
    ) -> None:
        provider_game_id = str(sched["provider_game_id"])
        self._current_raw_response_id = _opt(sched, "raw_response_id")
        league_code = _PROVIDER_LEAGUE.get(provider)
        league = self._leagues.get_by_code(league_code) if league_code else None
        if league is None:
            self._record_game_no_candidate(
                provider, provider_game_id, result, f"unknown league for provider {provider!r}"
            )
            return

        season_year = sched["season"]
        home_pid = _opt(sched, "home_provider_team_id")
        away_pid = _opt(sched, "away_provider_team_id")
        if home_pid is None or away_pid is None:
            self._record_game_no_candidate(
                provider, provider_game_id, result, "schedule is missing a provider team id"
            )
            return

        home = self._resolve_team(provider, home_pid, league.league_id, season_year, result)
        away = self._resolve_team(provider, away_pid, league.league_id, season_year, result)
        if not (home.is_matched and away.is_matched):
            self._dq_issue(
                result, severity="issue", rule_code="DQ-MATCH-010", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="official game has an unresolved or ambiguous team",
            )
            self._record_game_no_candidate(
                provider, provider_game_id, result, "home/away team did not resolve"
            )
            return
        assert home.entity_id is not None and away.entity_id is not None  # noqa: S101
        if home.entity_id == away.entity_id:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-012", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="home and away resolved to the same canonical team",
            )
            self._record_game_reject(
                provider, provider_game_id, result, "home and away are the same team"
            )
            return

        venue = self._resolve_venue(provider, sched, result)
        actual_tz = venue.timezone if venue is not None else None
        provider_local = _opt(sched, "game_date_local")
        # Tier 3 (home venue tz) is only consulted -- and only computed -- when
        # neither the actual event venue tz nor a provider local date is
        # available, so an actual/neutral venue is never replaced by the home park.
        home_tz = (
            self._home_venue_tz(
                home.entity_id, before_start=_opt(sched, "scheduled_start"),
                cutoff=_opt(sched, "observed_at"),
            )
            if actual_tz is None and not provider_local
            else None
        )
        try:
            local = resolve_local_date(
                scheduled_start=_opt(sched, "scheduled_start"),
                actual_venue_tz=actual_tz,
                provider_local_date=provider_local,
                home_venue_tz=home_tz,
            )
        except InvalidTimezoneError as exc:
            self._dq_issue(
                result, severity="issue", rule_code="DQ-TZ-001", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description=f"local date unresolved: {exc}",
            )
            self._record_game_no_candidate(
                provider, provider_game_id, result, f"local date unresolved: {exc}"
            )
            return
        if local.dq_code is not None:
            self._dq_issue(
                result, severity="note", rule_code=local.dq_code, entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="venue-local date fell back to the UTC calendar date",
            )

        self._match_or_create(
            provider=provider,
            provider_game_id=provider_game_id,
            league_id=league.league_id,
            league_code=league.code,
            season_year=int(season_year) if season_year is not None else None,
            game_type=_opt(sched, "game_type"),
            home_team_id=home.entity_id,
            away_team_id=away.entity_id,
            scheduled_start=_opt(sched, "scheduled_start"),
            local_date=local.game_date_local,
            confidence_cap=local.confidence_cap,
            schedule_game_number=sched["game_number"],
            mapped_status=str(sched["mapped_status"]),
            venue_id=venue.venue_id if venue is not None else None,
            provider_home_pid=home_pid,
            provider_away_pid=away_pid,
            result=result,
        )

    # -- team / venue resolution + decision recording ------------------------ #
    def _resolve_team(
        self,
        provider: str,
        provider_team_id: str,
        league_id: str,
        season_year: Any,
        result: MatchGamesResult,
    ) -> Resolution:
        res = self._team_resolver.resolve(
            provider=provider,
            provider_team_id=provider_team_id,
            league_id=league_id,
            season_year=int(season_year) if season_year is not None else None,
        )
        decision_id = self._record_decision(
            entity_type="team", source_provider=provider, source_ref=provider_team_id,
            resolution=res, result=result,
        )
        if res.is_matched and res.tier != "exact_provider_id":
            self._link_reference("team", provider, provider_team_id, res, decision_id, result)
        if res.status == AMBIGUOUS and res.via_ambiguous_alias:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-006", entity_type="team",
                entity_id=provider_team_id, provider=provider,
                description="team resolved through an is_ambiguous alias row",
            )
        if res.scope_conflict:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-014", entity_type="team",
                entity_id=provider_team_id, provider=provider,
                description="exact provider team link resolves into the wrong league",
            )
        return res

    def _resolve_venue(
        self, provider: str, sched: sqlite3.Row, result: MatchGamesResult
    ) -> Optional[Venue]:
        venue_pid = _opt(sched, "venue_provider_id")
        if venue_pid is None:
            return None
        res = self._venue_resolver.resolve(provider=provider, provider_venue_id=venue_pid)
        self._record_decision(
            entity_type="venue", source_provider=provider, source_ref=venue_pid,
            resolution=res, result=result,
        )
        if not res.is_matched:
            self._dq_issue(
                result, severity="issue", rule_code="DQ-MATCH-011", entity_type="venue",
                entity_id=venue_pid, provider=provider,
                description="official game venue did not resolve to a canonical venue",
            )
            return None
        assert res.entity_id is not None  # noqa: S101
        return self._venues.get(res.entity_id)

    # -- game match / create ------------------------------------------------- #
    def _match_or_create(
        self,
        *,
        provider: str,
        provider_game_id: str,
        league_id: str,
        league_code: str,
        season_year: Optional[int],
        game_type: Optional[str],
        home_team_id: str,
        away_team_id: str,
        scheduled_start: Optional[str],
        local_date: str,
        confidence_cap: float,
        schedule_game_number: Any,
        mapped_status: str,
        venue_id: Optional[str],
        provider_home_pid: Optional[str],
        provider_away_pid: Optional[str],
        result: MatchGamesResult,
    ) -> None:
        # Tier 1 -- exact official key anchors identity across reschedules / DHs.
        existing_key = self._games.find_by_official_key(
            official_provider=provider, official_game_key=provider_game_id
        )
        if existing_key is not None:
            orient = self._orientation(existing_key, home_team_id, away_team_id)
            if orient == "same":
                self._accept_game(
                    provider, provider_game_id, existing_key.game_id, TIER_OFFICIAL_KEY,
                    SCORE_OFFICIAL_KEY, [existing_key.game_id], result, created=False,
                )
                return
            if orient == "swapped" and existing_key.is_neutral_site:
                self._accept_swapped(provider, provider_game_id, existing_key.game_id, result)
                return
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-003", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="official key matches a game with a conflicting team orientation",
            )
            self._record_game_reject(
                provider, provider_game_id, result,
                "official key matches a game with conflicting orientation",
                candidate=existing_key.game_id,
            )
            return

        if scheduled_start is None:
            self._record_game_no_candidate(
                provider, provider_game_id, result, "no scheduled start to match a game on"
            )
            return
        start = _parse_utc(scheduled_start)

        # Tier 2/3 -- schedule-key match on resolved teams + local date + window.
        exact = self._window_candidates(
            league_id, home_team_id, away_team_id, start, _SCHEDULE_EXACT_WINDOW, local_date
        )
        if len(exact) == 1:
            self._accept_game(
                provider, provider_game_id, exact[0], TIER_SCHEDULE_EXACT,
                min(SCORE_SCHEDULE_EXACT, confidence_cap), exact, result, created=False,
            )
            return
        if len(exact) > 1:
            self._record_game_ambiguous(
                provider, provider_game_id, result, exact,
                "multiple canonical games share the schedule key within 90 minutes",
            )
            return

        window = self._window_candidates(
            league_id, home_team_id, away_team_id, start, _SCHEDULE_WIDE_WINDOW, local_date
        )
        if len(window) == 1:
            self._accept_game(
                provider, provider_game_id, window[0], TIER_SCHEDULE_WINDOW,
                min(SCORE_SCHEDULE_WINDOW, confidence_cap), window, result, created=False,
            )
            return
        if len(window) > 1:
            self._record_game_ambiguous(
                provider, provider_game_id, result, window,
                "multiple canonical games share the schedule key within 12 hours",
            )
            return

        # No existing canonical game -> create one (all guards already satisfied:
        # official provider, both teams accepted, league known, unique official key).
        self._create_game(
            provider=provider, provider_game_id=provider_game_id, league_id=league_id,
            league_code=league_code, season_year=season_year, game_type=game_type,
            home_team_id=home_team_id, away_team_id=away_team_id, scheduled_start=scheduled_start,
            local_date=local_date, schedule_game_number=schedule_game_number,
            mapped_status=mapped_status, venue_id=venue_id, start=start,
            provider_home_pid=provider_home_pid, provider_away_pid=provider_away_pid,
            result=result,
        )

    def _create_game(
        self,
        *,
        provider: str,
        provider_game_id: str,
        league_id: str,
        league_code: str,
        season_year: Optional[int],
        game_type: Optional[str],
        home_team_id: str,
        away_team_id: str,
        scheduled_start: str,
        local_date: str,
        schedule_game_number: Any,
        mapped_status: str,
        venue_id: Optional[str],
        start: datetime,
        provider_home_pid: Optional[str],
        provider_away_pid: Optional[str],
        result: MatchGamesResult,
    ) -> None:
        if provider not in _OFFICIAL_PROVIDERS:
            self._record_game_no_candidate(
                provider, provider_game_id, result, "provider is not an approved official source"
            )
            return
        if season_year is None:
            self._record_game_no_candidate(
                provider, provider_game_id, result, "no season on the schedule observation"
            )
            return

        game_number = self._slate_game_number(
            provider=provider, provider_home_pid=provider_home_pid,
            provider_away_pid=provider_away_pid, provider_game_id=provider_game_id,
            target_local_date=local_date, home_team_id=home_team_id,
            schedule_game_number=schedule_game_number,
        )
        if game_number is None:
            self._record_game_ambiguous(
                provider, provider_game_id, result, [],
                "indistinguishable doubleheader (no game number, starts within 90 minutes)",
            )
            return

        # A natural-key collision with a DIFFERENT official key is a duplication
        # attempt, not a match -- refuse it as blocking rather than corrupt identity.
        collision = self._games.find_natural(
            league_id=league_id, game_date_local=local_date, home_team_id=home_team_id,
            away_team_id=away_team_id, game_number=game_number,
        )
        if collision is not None:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-013", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="creating this game would duplicate an existing natural key",
            )
            self._record_game_reject(
                provider, provider_game_id, result,
                "natural key already held by a different canonical game",
                candidate=collision.game_id,
            )
            return

        phase = _season_phase(game_type)
        if not self._dry_run:
            self._seasons.upsert(
                league_code=league_code, league_id=league_id, year=season_year, phase=phase,
                label=f"{league_code} {season_year} {phase}", start_date=f"{season_year}-01-01",
            )
            game = self._games.create(
                league_id=league_id,
                season_id=_season_id(league_code, season_year, phase),
                home_team_id=home_team_id, away_team_id=away_team_id,
                scheduled_start=scheduled_start, game_date_local=local_date,
                status=_STATUS_MAP.get(mapped_status, "scheduled"), game_number=game_number,
                venue=venue_id, official_provider=provider, official_game_key=provider_game_id,
            )
            game_id: Optional[str] = game.game_id
        else:
            game_id = None
        result.counters.canonical_games_created += 1
        self._accept_game(
            provider, provider_game_id, game_id, TIER_OFFICIAL_KEY, SCORE_OFFICIAL_KEY,
            [game_id] if game_id else [], result, created=True,
        )

    def _slate_game_number(
        self,
        *,
        provider: str,
        provider_home_pid: Optional[str],
        provider_away_pid: Optional[str],
        provider_game_id: str,
        target_local_date: str,
        home_team_id: str,
        schedule_game_number: Any,
    ) -> Optional[int]:
        """Deterministic doubleheader game number, independent of processing order.

        A provider-supplied game number always wins. Otherwise the number is the
        game's **chronological rank** within its slate. The slate is grouped by the
        **resolved venue-local date** -- each sibling's local date is derived
        through the same venue-aware hierarchy (actual venue tz -> provider local
        date -> knowledge-bounded home-venue tz -> UTC), so a missing provider
        local date does NOT collapse the game to a single-game slate. Siblings are
        the latest observation of every provider game sharing the same provider and
        provider home/away team ids whose resolved local date equals this game's,
        ranked by ``(scheduled_start, provider_game_id)`` over the *whole schedule
        corpus* -- never by created-game order or provider-id sort. Two starts
        within 90 minutes are indistinguishable -> ``None`` (ambiguous). A partial
        bounded run still sees an unresolved earlier sibling in the corpus.
        """

        if schedule_game_number is not None:
            return int(schedule_game_number)
        if provider_home_pid is None or provider_away_pid is None:
            return 1

        siblings: list[tuple[str, str]] = []  # (scheduled_start, provider_game_id)
        for row in self._latest_slate_rows(provider, provider_home_pid, provider_away_pid):
            ss = _opt(row, "scheduled_start")
            if ss is None:
                continue
            if self._row_local_date(row, home_team_id) == target_local_date:
                siblings.append((ss, str(row["provider_game_id"])))
        target = next((s for s in siblings if s[1] == provider_game_id), None)
        if target is None:
            return 1
        target_start = target[0]
        others = [s for s in siblings if s[1] != provider_game_id]
        target_dt = _parse_utc(target_start)
        for ss, _pgid in others:
            if abs(_parse_utc(ss) - target_dt) < _DOUBLEHEADER_GAP:
                return None  # a same-slate sibling within 90 min -> indistinguishable
        # Rank = 1 + siblings that sort strictly before this game chronologically.
        return 1 + sum(1 for ss, pgid in others if (ss, pgid) < (target_start, provider_game_id))

    def _latest_slate_rows(
        self, provider: str, provider_home_pid: str, provider_away_pid: str
    ) -> list[sqlite3.Row]:
        """The latest schedule observation of every provider game for this matchup.

        Ordered so the first row seen per provider game is its deterministic
        current observation: newest ``observed_at`` first, then ``schedule_id``
        (a creation-ordered ULID) as a stable tie-break -- so two rows sharing an
        ``observed_at`` are never resolved by SQLite's physical row order.
        """

        rows = self._conn.execute(
            "SELECT schedule_id, provider_game_id, scheduled_start, game_date_local, "
            "venue_provider_id, observed_at, provider FROM game_schedule_snapshots "
            "WHERE provider = ? AND home_provider_team_id = ? AND away_provider_team_id = ? "
            "AND scheduled_start IS NOT NULL "
            "ORDER BY provider_game_id, observed_at DESC, schedule_id DESC",
            (provider, provider_home_pid, provider_away_pid),
        ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for r in rows:
            pgid = str(r["provider_game_id"])
            if pgid not in latest:  # rows are pre-ordered; first per game is latest
                latest[pgid] = r
        return list(latest.values())

    def _row_local_date(self, row: sqlite3.Row, home_team_id: str) -> Optional[str]:
        """A schedule row's resolved venue-local date (read-only; records nothing)."""

        venue: Optional[Venue] = None
        vpid = _opt(row, "venue_provider_id")
        if vpid:
            vres = self._venue_resolver.resolve(
                provider=str(row["provider"]), provider_venue_id=vpid)
            if vres.is_matched and vres.entity_id is not None:
                venue = self._venues.get(vres.entity_id)
        actual_tz = venue.timezone if venue is not None else None
        provider_local = _opt(row, "game_date_local")
        home_tz = (
            self._home_venue_tz(
                home_team_id, before_start=_opt(row, "scheduled_start"),
                cutoff=_opt(row, "observed_at"))
            if actual_tz is None and not provider_local
            else None
        )
        try:
            return resolve_local_date(
                scheduled_start=_opt(row, "scheduled_start"), actual_venue_tz=actual_tz,
                provider_local_date=provider_local, home_venue_tz=home_tz,
            ).game_date_local
        except InvalidTimezoneError:
            return None

    def _window_candidates(
        self,
        league_id: str,
        home_team_id: str,
        away_team_id: str,
        start: datetime,
        window: timedelta,
        local_date: str,
    ) -> list[str]:
        lo = (start - window).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (start + window).strftime("%Y-%m-%dT%H:%M:%SZ")
        games = self._games.find_in_window(
            league_id=league_id, home_team_id=home_team_id, away_team_id=away_team_id,
            start_low=lo, start_high=hi,
        )
        # The venue-local date is the doubleheader key: only same-slate games
        # count. A game already anchored on a DIFFERENT official key from this
        # same provider is a distinct official game (a doubleheader sibling), not
        # a schedule-key match -- excluding it prevents merging two official games.
        return sorted(
            g.game_id
            for g in games
            if g.game_date_local == local_date
            and g.official_provider != self._current_provider
        )

    def _home_venue_tz(
        self, home_team_id: str, *, before_start: Optional[str], cutoff: Optional[str]
    ) -> Optional[str]:
        """The canonical home team's ordinary venue timezone, or ``None``.

        Bounded by BOTH event time and knowledge time. A prior canonical game may
        contribute its venue timezone only when:

        * its scheduled start precedes the target start (``scheduled_start <
          before_start``) -- so a future game never influences an earlier match;
        * it is non-neutral with a venue set;
        * its canonical identity is backed by an **accepted, non-swapped** game
          match decision whose ``decided_at <= cutoff`` -- so a game discovered or
          matched only in a *later* backfill (or a later manual approval of a
          review-gated swapped match) is invisible at the target's schedule
          observation cutoff, and an unreviewed neutral-swapped match never
          supplies ordinary home-venue evidence.

        A timezone is returned only when the qualifying prior games share exactly
        one venue timezone; an ambiguous home history (a relocation spanning
        zones) yields ``None`` rather than a guess. Never used to replace an
        actual/neutral event venue (the caller consults it only when no actual
        venue tz and no provider local date exist). With no cutoff or no
        before-start, no evidence qualifies -> ``None`` (conservative).
        """

        if before_start is None or cutoff is None:
            return None
        rows = self._conn.execute(
            "SELECT DISTINCT g.venue AS venue FROM games g "
            "JOIN entity_match_decisions d ON d.matched_entity_id = g.game_id "
            "WHERE g.home_team_id = ? AND g.is_neutral_site = 0 AND g.venue IS NOT NULL "
            "AND g.scheduled_start < ? "
            "AND d.entity_type = 'game' AND d.outcome = 'accepted' "
            "AND d.method <> ? AND d.decided_at <= ?",
            (home_team_id, before_start, TIER_SCHEDULE_SWAPPED, cutoff),
        ).fetchall()
        zones: set[str] = set()
        for r in rows:
            venue = self._venues.get(str(r["venue"]))
            if venue is not None and venue.timezone:
                zones.add(venue.timezone)
        return next(iter(zones)) if len(zones) == 1 else None

    @staticmethod
    def _orientation(game: Game, home_team_id: str, away_team_id: str) -> str:
        if game.home_team_id == home_team_id and game.away_team_id == away_team_id:
            return "same"
        if game.home_team_id == away_team_id and game.away_team_id == home_team_id:
            return "swapped"
        return "conflict"

    # -- decision + link helpers -------------------------------------------- #
    def _accept_game(
        self, provider: str, provider_game_id: str, game_id: Optional[str], tier: str,
        score: float, candidate_ids: Sequence[Optional[str]], result: MatchGamesResult, *,
        created: bool,
    ) -> None:
        candidates = [
            CandidateInput(score=score, tier=tier, candidate_entity_id=cid, method=tier)
            for cid in candidate_ids
        ]
        decision_id = self._persist_decision(
            entity_type="game", source_provider=provider, source_ref=provider_game_id,
            outcome="accepted", method=tier, score=score, matched_entity_id=game_id,
            candidates=candidates, needs_review=False, result=result,
        )
        if not created:
            result.counters.canonical_entities_unchanged += 1
        if game_id is not None:
            self._link_game_reference(provider, provider_game_id, game_id, decision_id, result)

    def _accept_swapped(
        self, provider: str, provider_game_id: str, game_id: str, result: MatchGamesResult
    ) -> None:
        decision_id = self._persist_decision(
            entity_type="game", source_provider=provider, source_ref=provider_game_id,
            outcome="accepted", method=TIER_SCHEDULE_SWAPPED, score=SCORE_SCHEDULE_SWAPPED,
            matched_entity_id=game_id,
            candidates=[CandidateInput(
                score=SCORE_SCHEDULE_SWAPPED, tier=TIER_SCHEDULE_SWAPPED,
                candidate_entity_id=game_id, method=TIER_SCHEDULE_SWAPPED,
                evidence="neutral-site team-swapped orientation",
            )],
            needs_review=True, result=result,
        )
        result.counters.canonical_entities_unchanged += 1
        self._dq_issue(
            result, severity="issue", rule_code="DQ-MATCH-007", entity_type="game",
            entity_id=provider_game_id, provider=provider,
            description="neutral-site team-swapped match accepted pending review",
        )
        self._link_game_reference(provider, provider_game_id, game_id, decision_id, result)

    def _record_game_ambiguous(
        self, provider: str, provider_game_id: str, result: MatchGamesResult,
        candidate_ids: list[str], reason: str,
    ) -> None:
        self._persist_decision(
            entity_type="game", source_provider=provider, source_ref=provider_game_id,
            outcome="ambiguous", method=TIER_SCHEDULE_EXACT, score=0.0, matched_entity_id=None,
            candidates=[
                CandidateInput(score=0.0, tier=TIER_SCHEDULE_EXACT, candidate_entity_id=c)
                for c in sorted(candidate_ids)
            ],
            rejection_reason=reason, needs_review=True, result=result,
        )

    def _record_game_no_candidate(
        self, provider: str, provider_game_id: str, result: MatchGamesResult, reason: str
    ) -> None:
        self._persist_decision(
            entity_type="game", source_provider=provider, source_ref=provider_game_id,
            outcome="no_candidate", method="none", score=0.0, matched_entity_id=None,
            candidates=[], rejection_reason=reason, needs_review=True, result=result,
        )

    def _record_game_reject(
        self, provider: str, provider_game_id: str, result: MatchGamesResult, reason: str,
        *, candidate: Optional[str] = None,
    ) -> None:
        candidates = (
            [CandidateInput(score=0.0, tier="conflict", candidate_entity_id=candidate)]
            if candidate else []
        )
        self._persist_decision(
            entity_type="game", source_provider=provider, source_ref=provider_game_id,
            outcome="rejected", method="conflict", score=0.0, matched_entity_id=None,
            candidates=candidates, rejection_reason=reason, needs_review=True, result=result,
        )

    def _record_decision(
        self, *, entity_type: str, source_provider: str, source_ref: str,
        resolution: Resolution, result: MatchGamesResult,
    ) -> Optional[str]:
        # A scope conflict names a candidate but refuses it: a rejection, not a
        # bare no-candidate.
        outcome = "rejected" if resolution.scope_conflict else resolution.outcome()
        return self._persist_decision(
            entity_type=entity_type, source_provider=source_provider, source_ref=source_ref,
            outcome=outcome, method=resolution.method, score=resolution.score,
            matched_entity_id=resolution.entity_id,
            candidates=[
                CandidateInput(
                    score=c.score, tier=c.tier, candidate_entity_id=c.entity_id,
                    method=c.method, evidence=c.evidence,
                )
                for c in resolution.candidates
            ],
            rejection_reason=resolution.reason,
            needs_review=resolution.needs_review, result=result,
        )

    def _persist_decision(
        self, *, entity_type: str, source_provider: str, source_ref: str, outcome: str,
        method: str, score: float, matched_entity_id: Optional[str],
        candidates: list[CandidateInput], needs_review: bool, result: MatchGamesResult,
        rejection_reason: Optional[str] = None,
    ) -> Optional[str]:
        # Count identically whether or not we persist (dry-run parity).
        result.counters.decisions_evaluated += 1
        result.counters.candidates_recorded += len(candidates)
        if outcome == "accepted":
            result.counters.accepted += 1
        elif outcome == "ambiguous":
            result.counters.ambiguous += 1
        elif outcome == "no_candidate":
            result.counters.no_candidate += 1
        elif outcome == "rejected":
            result.counters.rejected += 1
        if needs_review:
            result.counters.manual_review_required += 1
        if outcome == "accepted":
            reason = None
        else:
            reason = rejection_reason or "unresolved"
        if self._dry_run:
            return None
        decision = self._match.record_decision(
            entity_type=entity_type, source_provider=source_provider, source_ref=source_ref,
            outcome=outcome, method=method, score=score, threshold=THRESHOLD,
            matcher_version=self._version, candidates=candidates,
            matched_entity_id=matched_entity_id, rejection_reason=reason,
            needs_manual_review=needs_review, run_id=self._run_id,
            raw_response_id=self._current_raw_response_id,
        )
        return decision.match_id

    def _link_reference(
        self, kind: str, provider: str, provider_entity_id: str, res: Resolution,
        decision_id: Optional[str], result: MatchGamesResult,
    ) -> None:
        # Links the reference to the exact decision created by THIS attempt --
        # never an unconstrained "latest decision" lookup that a same-timestamp
        # sibling could win. Errors are NOT blanket-swallowed: a genuinely absent
        # crosswalk row is an explicit, checked skip (the canonical id is still
        # used directly for the game), while a conflicting link is blocking and any
        # real repository/constraint failure propagates to command failure.
        if self._dry_run or res.entity_id is None or decision_id is None:
            return
        if self._refs.get(kind, provider, provider_entity_id) is None:
            # No provider crosswalk row for this team/venue (optional for game
            # canonicalization) -- nothing to link, and never a silent catch.
            return
        _ref, outcome = self._refs.link_canonical(
            kind=kind, provider=provider, provider_entity_id=provider_entity_id,
            canonical_id=res.entity_id, match_decision_id=decision_id,
        )
        if outcome == LinkOutcome.LINKED:
            result.counters.provider_references_linked += 1
        elif outcome == LinkOutcome.CONFLICT:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-016", entity_type=kind,
                entity_id=provider_entity_id, provider=provider,
                description=f"provider {kind} already linked to a different canonical entity",
            )

    def _link_game_reference(
        self, provider: str, provider_game_id: str, game_id: str,
        decision_id: Optional[str], result: MatchGamesResult,
    ) -> None:
        if self._dry_run or decision_id is None:
            return
        _ref, outcome = self._refs.link_canonical(
            kind="game", provider=provider, provider_entity_id=provider_game_id,
            canonical_id=game_id, match_decision_id=decision_id,
        )
        if outcome == LinkOutcome.LINKED:
            result.counters.provider_references_linked += 1
        elif outcome == LinkOutcome.CONFLICT:
            self._dq_issue(
                result, severity="blocking", rule_code="DQ-MATCH-003", entity_type="game",
                entity_id=provider_game_id, provider=provider,
                description="provider game already linked to a different canonical game",
            )

    def _dq_issue(
        self, result: MatchGamesResult, *, severity: str, rule_code: str, entity_type: str,
        entity_id: str, provider: str, description: str,
    ) -> None:
        # Idempotent: an identical unresolved issue is counted (and recorded)
        # once. The existence check is read-only, so dry-run and persisted runs
        # report the same DQ counters against the same corpus.
        existing = self._conn.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_type = ? "
            "AND entity_id IS ? AND provider IS ? AND resolved_at IS NULL LIMIT 1",
            (rule_code, entity_type, entity_id, provider),
        ).fetchone()
        if existing is not None:
            return
        result.counters.dq_issues += 1
        if severity == "blocking":
            result.counters.blocking_issues += 1
        if self._dry_run:
            return
        self._dq.record(
            severity=severity, rule_code=rule_code, entity_type=entity_type,
            description=description, entity_id=entity_id, provider=provider,
            detail_json=json.dumps({"provider": provider, "ref": entity_id}), run_id=self._run_id,
        )


def _opt(row: sqlite3.Row, key: str) -> Optional[str]:
    value = row[key]
    return None if value is None else str(value)


def _season_id(league_code: str, year: int, phase: str) -> str:
    from ..db.ids import season_id

    return season_id(league_code, year, phase)


def resolve_provider_for_sport(sport: str) -> Optional[str]:
    """Map a ``--sport`` value to its official provider."""

    return _SPORT_PROVIDERS.get(sport)
