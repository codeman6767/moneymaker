"""Phase D5B1: deterministic sportsbook-event matching + outcome orientation.

Resolves already-ingested The Odds API ``sportsbook_events`` to the existing
canonical ``games`` using ONLY provider-scoped team aliases and venue-aware
schedule/time evidence -- never a price, implied probability, bookmaker count,
final score, or settled outcome. Every attempt records exactly one append-only
``entity_match_decisions`` row (``entity_type = 'sportsbook_event'``) plus its
winning-tier candidates; an accepted event is linked to its game with the EXACT
decision from that attempt and a typed ``orientation`` (direct / neutral-site
swapped). A neutral swapped match stays review-gated and is never treated as
orientation-approved pricing data. Existing h2h / spreads / totals outcomes are
validated against the accepted orientation; unknown/malformed ones are surfaced
via DQ, never dropped or rewritten.

This module deliberately does NOT import or read the sportsbook price-snapshot
table, and implements no Kalshi matching (D5B2) and no Phase E dataset/modeling
work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Optional

from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.games import SqliteGameRepository
from ..db.repositories.leagues import SqliteLeagueRepository
from ..db.repositories.matching import CandidateInput, SqliteMatchingRepository
from ..db.repositories.references import LinkOutcome
from ..db.repositories.sportsbook import SqliteSportsbookRepository
from ..db.schema import SPORT_KEY_TO_LEAGUE_CODE, THE_ODDS_API_PROVIDER
from .localdate import InvalidTimezoneError, resolve_local_date
from .model import (
    MATCHER_VERSION,
    SCORE_SCHEDULE_EXACT,
    SCORE_SCHEDULE_SWAPPED,
    SCORE_SCHEDULE_WINDOW,
    THRESHOLD,
    TIER_SCHEDULE_EXACT,
    TIER_SCHEDULE_SWAPPED,
    TIER_SCHEDULE_WINDOW,
)
from .service import MatchGamesService, _parse_utc
from .teams import TeamResolver

_EXACT_WINDOW = timedelta(minutes=90)
_WIDE_WINDOW = timedelta(hours=12)

_SPORT_ARG_KEY = {"mlb": "baseball_mlb", "nba": "basketball_nba"}


@dataclass
class SbCounters:
    """Every counter the sportsbook report/CLI exposes (task §14)."""

    events_considered: int = 0
    events_eligible: int = 0
    team_resolution_attempts: int = 0
    events_accepted: int = 0
    events_ambiguous: int = 0
    events_no_candidate: int = 0
    events_rejected: int = 0
    direct_orientation: int = 0
    swapped_review_gated: int = 0
    blocking_orientation_conflicts: int = 0
    candidates_recorded: int = 0
    event_links_applied: int = 0
    outcome_rows_checked: int = 0
    outcome_roles_approved: int = 0
    unknown_outcomes: int = 0
    dq_issues: int = 0
    blocking_issues: int = 0
    rows_persisted: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class MatchSportsbookResult:
    dry_run: bool
    status: str = "succeeded"
    counters: SbCounters = field(default_factory=SbCounters)
    run_id: Optional[str] = None

    @property
    def needs_failure_exit(self) -> bool:
        return self.counters.blocking_issues > 0


def sport_key_for_arg(sport: str) -> Optional[str]:
    return _SPORT_ARG_KEY.get(sport)


class MatchSportsbookService:
    """Resolves The Odds API sportsbook events to canonical games (network-free)."""

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
        self._sb = SqliteSportsbookRepository(conn)
        self._games = SqliteGameRepository(conn)
        self._leagues = SqliteLeagueRepository(conn)
        self._match = SqliteMatchingRepository(conn)
        self._dqrepo = SqliteDataQualityRepository(conn)
        self._teams = TeamResolver(conn)
        # Reused ONLY for the knowledge-time-bounded home-venue timezone (read-only).
        self._home = MatchGamesService(conn)
        # Source provenance of the event currently being matched.
        self._current_raw: Optional[str] = None

    # -- public entry -------------------------------------------------------- #
    def match_range(
        self,
        *,
        sport_key: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        unmatched_only: bool = False,
    ) -> MatchSportsbookResult:
        result = MatchSportsbookResult(dry_run=self._dry_run, run_id=self._run_id)
        league_id: Optional[str] = None
        if sport_key is not None:
            league_code = SPORT_KEY_TO_LEAGUE_CODE.get(sport_key)
            league = self._leagues.get_by_code(league_code) if league_code else None
            league_id = league.league_id if league is not None else "__none__"
        events = self._sb.list_events_for_matching(
            provider=THE_ODDS_API_PROVIDER, league_id=league_id, from_date=from_date,
            to_date=to_date, provider_event_id=provider_event_id, unmatched_only=unmatched_only,
        )
        for event in events:
            result.counters.events_considered += 1
            self._resolve_event(event, result)
        result.status = "partially_failed" if result.needs_failure_exit else "succeeded"
        return result

    # -- one event ----------------------------------------------------------- #
    def _resolve_event(self, event, result: MatchSportsbookResult) -> None:  # type: ignore[no-untyped-def]
        sb_id = event.sb_event_id
        self._current_raw = event.raw_response_id
        league_code = SPORT_KEY_TO_LEAGUE_CODE.get(event.sport_key)
        league = self._leagues.get_by_code(league_code) if league_code else None
        if league is None:
            self._dq(result, "blocking", "DQ-SB-LEAGUE-001", sb_id,
                     f"unknown/unsupported sport_key {event.sport_key!r}")
            self._record(result, sb_id, "rejected", "league_mismatch", 0.0, None, [],
                         reason=f"unsupported sport_key {event.sport_key!r}", review=True)
            return
        # A stored league that disagrees with the sport_key map is corruption.
        if event.league_id is not None and event.league_id != league.league_id:
            self._dq(result, "blocking", "DQ-SB-LEAGUE-001", sb_id,
                     f"stored league {event.league_id} != sport_key league {league.league_id}")
            self._record(result, sb_id, "rejected", "league_mismatch", 0.0, None, [],
                         reason="stored league disagrees with sport_key", review=True)
            return
        result.counters.events_eligible += 1

        season = int(event.commence_time[:4])
        home = self._teams.resolve(
            provider=THE_ODDS_API_PROVIDER, provider_team_id=event.home_team_raw,
            league_id=league.league_id, raw_name=event.home_team_raw, season_year=season)
        away = self._teams.resolve(
            provider=THE_ODDS_API_PROVIDER, provider_team_id=event.away_team_raw,
            league_id=league.league_id, raw_name=event.away_team_raw, season_year=season)
        result.counters.team_resolution_attempts += 2
        for res in (home, away):
            if res.via_ambiguous_alias:
                self._dq(result, "blocking", "DQ-MATCH-006", sb_id,
                         "sportsbook team resolved through an is_ambiguous alias")
        if not (home.is_matched and away.is_matched):
            self._record(result, sb_id, "no_candidate", "none", 0.0, None, [],
                         reason="home/away team did not resolve", review=True)
            return
        assert home.entity_id is not None and away.entity_id is not None  # noqa: S101
        if home.entity_id == away.entity_id:
            self._dq(result, "blocking", "DQ-SB-OUTCOME-001", sb_id,
                     "sportsbook event resolves home and away to the same team")
            self._record(result, sb_id, "rejected", "same_team", 0.0, None, [],
                         reason="home and away resolve to the same team", review=True)
            return

        commence = _parse_utc(event.commence_time)
        local_tier, confidence_cap, local_dq = self._local_tier(event, home.entity_id, commence)
        if local_dq is not None:
            self._dq(result, "note", local_dq, sb_id,
                     "sportsbook local date fell back to the UTC calendar date")

        self._match_event(event, home.entity_id, away.entity_id, commence, confidence_cap,
                          local_tier, result)

    # -- candidate tiers ----------------------------------------------------- #
    def _match_event(
        self, event, home_id, away_id, commence, cap, local_tier, result,  # type: ignore[no-untyped-def]
    ) -> None:
        sb_id = event.sb_event_id
        direct90 = self._candidates(home_id, away_id, commence, _EXACT_WINDOW)
        if len(direct90) == 1:
            self._accept(event, direct90[0], TIER_SCHEDULE_EXACT, min(SCORE_SCHEDULE_EXACT, cap),
                         "direct", direct90, home_id, away_id, commence, local_tier, result)
            return
        if len(direct90) > 1:
            self._ambiguous(result, sb_id, direct90, TIER_SCHEDULE_EXACT,
                            "multiple canonical games within 90 minutes (direct)")
            return
        direct12 = self._candidates(home_id, away_id, commence, _WIDE_WINDOW)
        if len(direct12) == 1:
            self._accept(event, direct12[0], TIER_SCHEDULE_WINDOW, min(SCORE_SCHEDULE_WINDOW, cap),
                         "direct", direct12, home_id, away_id, commence, local_tier, result)
            return
        if len(direct12) > 1:
            self._ambiguous(result, sb_id, direct12, TIER_SCHEDULE_WINDOW,
                            "multiple canonical games within 12 hours (direct)")
            return
        # Neutral-site swapped orientation (provider home/away reversed).
        swapped = self._candidates(away_id, home_id, commence, _WIDE_WINDOW)
        neutral = [g for g in swapped if g.is_neutral_site]
        nonneutral = [g for g in swapped if not g.is_neutral_site]
        if nonneutral:
            # A reversed orientation on a NON-neutral game inverts the sign; blocking.
            self._dq(result, "blocking", "DQ-MATCH-003", sb_id,
                     "sportsbook home/away reversed against a non-neutral canonical game")
            self._record(result, sb_id, "rejected", "conflict", 0.0, None,
                         [g.game_id for g in nonneutral],
                         reason="reversed orientation against a non-neutral game", review=True)
            return
        if len(neutral) == 1:
            self._accept(event, neutral[0], TIER_SCHEDULE_SWAPPED, SCORE_SCHEDULE_SWAPPED,
                         "swapped", neutral, home_id, away_id, commence, local_tier, result)
            return
        if len(neutral) > 1:
            self._ambiguous(result, sb_id, neutral, TIER_SCHEDULE_SWAPPED,
                            "multiple neutral-site swapped candidates")
            return
        self._record(result, sb_id, "no_candidate", "none", 0.0, None, [],
                     reason="no canonical game matches the teams and time window", review=True)

    def _candidates(self, home_id, away_id, commence, window):  # type: ignore[no-untyped-def]
        lo = (commence - window).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (commence + window).strftime("%Y-%m-%dT%H:%M:%SZ")
        games = self._games.find_in_window(
            league_id=self._league_of(home_id), home_team_id=home_id, away_team_id=away_id,
            start_low=lo, start_high=hi)
        return games

    def _league_of(self, team_id: str) -> str:
        row = self._conn.execute(
            "SELECT league_id FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        return str(row["league_id"]) if row is not None else "__none__"

    def _local_tier(self, event, home_id, commence):  # type: ignore[no-untyped-def]
        """Local-date evidence tier + confidence cap (never used to filter games).

        Recorded for provenance and used only to cap the achievable score when a
        UTC fallback was required; matching itself is by teams + UTC time window,
        so a 7 p.m. Pacific game whose UTC date is the next day is unaffected.
        """

        home_tz = self._home._home_venue_tz(
            home_id, before_start=event.commence_time, cutoff=event.last_observed_at)
        try:
            local = resolve_local_date(
                scheduled_start=event.commence_time, actual_venue_tz=None,
                provider_local_date=None, home_venue_tz=home_tz)
            return local.tier, local.confidence_cap, local.dq_code
        except InvalidTimezoneError:
            return "utc_fallback", 0.88, "DQ-TZ-001"

    # -- accept / record / link --------------------------------------------- #
    def _accept(
        self, event, game, tier, score, orientation, candidate_games, home_id, away_id,
        commence, local_tier, result,  # type: ignore[no-untyped-def]
    ) -> None:
        sb_id = event.sb_event_id
        minutes = round(abs((_parse_utc(game.scheduled_start) - commence).total_seconds()) / 60)
        evidence = json.dumps({
            "home_team_id": home_id, "away_team_id": away_id, "delta_minutes": minutes,
            "local_date_tier": local_tier, "orientation": orientation,
            "game_number": game.game_number,
        }, sort_keys=True)
        candidates = [
            CandidateInput(score=score, tier=tier, candidate_entity_id=g.game_id, method=tier,
                           evidence=evidence if g.game_id == game.game_id else None)
            for g in candidate_games
        ]
        review = orientation == "swapped"
        decision_id = self._record(
            result, sb_id, "accepted", tier, score, game.game_id, candidates, review=review)
        if orientation == "direct":
            result.counters.direct_orientation += 1
        else:
            result.counters.swapped_review_gated += 1
            self._dq(result, "issue", "DQ-MATCH-007", sb_id,
                     "neutral-site swapped sportsbook match accepted pending review")
        # A different sportsbook event already linked to this game with an
        # incompatible orientation inverts pricing sign -> blocking.
        for other_id, other_orient, _dec in self._sb.events_linked_to_game(game.game_id):
            if other_id != sb_id and other_orient is not None and other_orient != orientation:
                self._dq(result, "blocking", "DQ-MATCH-003", sb_id,
                         "two sportsbook events link one game with conflicting orientation")
                result.counters.blocking_orientation_conflicts += 1
        self._link(result, sb_id, game.game_id, decision_id, orientation)
        self._validate_outcomes(event, orientation, result)

    def _link(self, result, sb_id, game_id, decision_id, orientation):  # type: ignore[no-untyped-def]
        if self._dry_run or decision_id is None:
            return
        outcome = self._sb.link_game(
            sb_event_id=sb_id, game_id=game_id, match_decision_id=decision_id,
            orientation=orientation)
        if outcome == LinkOutcome.LINKED:
            result.counters.event_links_applied += 1
            result.counters.rows_persisted += 1
        elif outcome == LinkOutcome.CONFLICT:
            self._dq(result, "blocking", "DQ-MATCH-003", sb_id,
                     "sportsbook event already linked to a different canonical game")

    def _record(
        self, result, sb_id, outcome, method, score, matched, candidate_games,  # type: ignore[no-untyped-def]
        *, reason=None, review=False,
    ):
        result.counters.candidates_recorded += len(candidate_games)
        if outcome == "accepted":
            result.counters.events_accepted += 1
        elif outcome == "ambiguous":
            result.counters.events_ambiguous += 1
        elif outcome == "no_candidate":
            result.counters.events_no_candidate += 1
        elif outcome == "rejected":
            result.counters.events_rejected += 1
        if isinstance(candidate_games, list) and candidate_games and not isinstance(
            candidate_games[0], CandidateInput
        ):
            candidate_inputs = [
                CandidateInput(score=score if outcome == "accepted" else 0.0,
                               tier=method, candidate_entity_id=gid)
                for gid in candidate_games
            ]
        else:
            candidate_inputs = list(candidate_games)
        if self._dry_run:
            return None
        decision = self._match.record_decision(
            entity_type="sportsbook_event", source_provider=THE_ODDS_API_PROVIDER,
            source_ref=sb_id, outcome=outcome, method=method, score=score, threshold=THRESHOLD,
            matcher_version=self._version, candidates=candidate_inputs, matched_entity_id=matched,
            rejection_reason=None if outcome == "accepted" else (reason or "unresolved"),
            needs_manual_review=review, run_id=self._run_id,
            raw_response_id=self._current_raw,
        )
        result.counters.rows_persisted += 1
        return decision.match_id

    def _ambiguous(self, result, sb_id, games, tier, reason):  # type: ignore[no-untyped-def]
        self._record(result, sb_id, "ambiguous", tier, 0.0, None,
                     [g.game_id for g in games], reason=reason, review=True)

    # -- outcome semantics --------------------------------------------------- #
    def _validate_outcomes(self, event, orientation, result):  # type: ignore[no-untyped-def]
        sb_id = event.sb_event_id
        for market in self._sb.list_markets_for_event(sb_id):
            outcomes = self._sb.list_outcomes_for_market(market.sb_market_id)
            roles = [o.outcome_role for o in outcomes]
            result.counters.outcome_rows_checked += len(outcomes)
            for o in outcomes:
                if o.outcome_role == "unknown":
                    result.counters.unknown_outcomes += 1
                elif orientation == "direct" and o.outcome_role in (
                    "home", "away", "over", "under", "draw"
                ):
                    result.counters.outcome_roles_approved += 1
                # A swapped (review-gated) event approves no team orientation.
            self._validate_market_shape(market.market_key, roles, sb_id, result)

    def _validate_market_shape(self, market_key, roles, sb_id, result):  # type: ignore[no-untyped-def]
        counts = {r: roles.count(r) for r in set(roles)}
        problem: Optional[str] = None
        if counts.get("unknown", 0):
            # An unclassifiable outcome (e.g. a team name in a totals market, or a
            # moneyline name matching neither team) is retained, never dropped,
            # and surfaced -- a silently dropped outcome is missing data nobody sees.
            problem = f"{market_key} market has an unclassifiable/unknown outcome"
        elif market_key == "h2h":
            if counts.get("over", 0) or counts.get("under", 0):
                problem = "h2h market has over/under outcomes"
            elif counts.get("home", 0) > 1 or counts.get("away", 0) > 1:
                problem = "h2h market has duplicate home/away outcomes"
            elif counts.get("draw", 0):
                problem = "unexpected draw outcome on an MLB/NBA moneyline"
        elif market_key == "totals":
            if any(r in ("home", "away", "draw") for r in roles):
                problem = "totals market has a team-named outcome"
            elif counts.get("over", 0) > 1 or counts.get("under", 0) > 1:
                problem = "totals market has duplicate over/under outcomes"
        elif market_key == "spreads":
            if any(r in ("over", "under", "draw") for r in roles):
                problem = "spreads market has over/under outcomes"
            elif counts.get("home", 0) > 1 or counts.get("away", 0) > 1:
                problem = "spreads market has duplicate side outcomes"
        if problem is not None:
            self._dq(result, "issue", "DQ-SB-OUTCOME-001", sb_id, f"{market_key}: {problem}")

    # -- dq ------------------------------------------------------------------ #
    def _dq(self, result, severity, rule_code, entity_id, description):  # type: ignore[no-untyped-def]
        existing = self._conn.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_type = "
            "'sportsbook_event' AND entity_id IS ? AND provider IS ? AND resolved_at IS NULL "
            "LIMIT 1",
            (rule_code, entity_id, THE_ODDS_API_PROVIDER),
        ).fetchone()
        if existing is not None:
            return
        result.counters.dq_issues += 1
        if severity == "blocking":
            result.counters.blocking_issues += 1
        if self._dry_run:
            return
        self._dq_repo_record(severity, rule_code, entity_id, description)
        result.counters.rows_persisted += 1

    def _dq_repo_record(self, severity, rule_code, entity_id, description):  # type: ignore[no-untyped-def]
        self._dqrepo.record(
            severity=severity, rule_code=rule_code, entity_type="sportsbook_event",
            description=description, entity_id=entity_id, provider=THE_ODDS_API_PROVIDER,
            detail_json=json.dumps({"sb_event_id": entity_id}), run_id=self._run_id)
