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

The season passed to the team resolver follows the league-specific convention
(:func:`sports_quant.matching.season.season_year_for`), so an NBA game played in
January-June resolves under the *previous* calendar year's season. A candidate
canonical game is kept only when the sportsbook event and the game fall on the
same resolved venue-local slate; local-date consistency is a real candidate
requirement, not merely a confidence hint.

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

from ..db.normalize import normalized_key
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.games import SqliteGameRepository
from ..db.repositories.leagues import SqliteLeagueRepository
from ..db.repositories.matching import CandidateInput, SqliteMatchingRepository
from ..db.repositories.references import LinkOutcome
from ..db.repositories.sportsbook import SqliteSportsbookRepository
from ..db.repositories.venues import SqliteVenueRepository
from ..db.schema import SPORT_KEY_TO_LEAGUE_CODE, THE_ODDS_API_PROVIDER
from .localdate import InvalidTimezoneError, _parse_utc, resolve_local_date
from .model import (
    LOCALDATE_UTC_FALLBACK,
    MATCHER_VERSION,
    SCORE_SCHEDULE_EXACT,
    SCORE_SCHEDULE_SWAPPED,
    SCORE_SCHEDULE_WINDOW,
    THRESHOLD,
    TIER_SCHEDULE_EXACT,
    TIER_SCHEDULE_SWAPPED,
    TIER_SCHEDULE_WINDOW,
)
from .season import season_year_for
from .service import MatchGamesService
from .teams import TeamResolver

_EXACT_WINDOW = timedelta(minutes=90)
_WIDE_WINDOW = timedelta(hours=12)

_SPORT_ARG_KEY = {"mlb": "baseball_mlb", "nba": "basketball_nba"}


class SportsbookLinkError(RuntimeError):
    """A link application failed after its accepted decision was recorded.

    Raised so the enclosing run transaction rolls back rather than committing an
    accepted decision without its exact, verified link (task §3). The runner
    surfaces it as an active failure (exit 1)."""

# Provider-side outcome roles the accepted orientation can approve.
_APPROVABLE_ROLES = ("home", "away", "over", "under", "draw")

# Canonically supported D5B1 market types. Any other key (player props, alternate
# exotic markets) is retained but its outcome roles are never approved.
_SUPPORTED_MARKETS = ("h2h", "spreads", "totals")


@dataclass
class SbCounters:
    """Every counter the sportsbook report/CLI exposes (task §14)."""

    events_considered: int = 0
    events_eligible: int = 0
    team_resolution_attempts: int = 0
    events_accepted: int = 0
    events_already_linked: int = 0
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


@dataclass(frozen=True)
class _CandidateEval:
    """A canonical game that passed local-slate validation for one event.

    ``cap`` is the achievable-confidence cap for THIS candidate (1.0 for a real
    venue/home-venue local date, the UTC cap when only a UTC fallback was
    available); ``local_tier`` is the venue-local-date tier that produced it.
    """

    game: object  # sports_quant.db.models.Game
    cap: float
    local_tier: str


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
        self._venues = SqliteVenueRepository(conn)
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
            self._dq(result, "blocking", "DQ-SB-LEAGUE-001", "sportsbook_event", sb_id, event,
                     f"unknown/unsupported sport_key {event.sport_key!r}")
            self._record(result, sb_id, "rejected", "league_mismatch", 0.0, None, [],
                         reason=f"unsupported sport_key {event.sport_key!r}", review=True)
            return
        # A stored league that disagrees with the sport_key map is corruption.
        if event.league_id is not None and event.league_id != league.league_id:
            self._dq(result, "blocking", "DQ-SB-LEAGUE-001", "sportsbook_event", sb_id, event,
                     f"stored league {event.league_id} != sport_key league {league.league_id}")
            self._record(result, sb_id, "rejected", "league_mismatch", 0.0, None, [],
                         reason="stored league disagrees with sport_key", review=True)
            return
        result.counters.events_eligible += 1

        # A naive/malformed commence time is unusable and must never produce a
        # guessed season or a matched game.
        try:
            commence = _parse_utc(event.commence_time)
        except InvalidTimezoneError as exc:
            self._dq(result, "issue", "DQ-TZ-001", "sportsbook_event", sb_id, event,
                     f"unusable commence_time {event.commence_time!r}: {exc}")
            self._record(result, sb_id, "no_candidate", "none", 0.0, None, [],
                         reason=f"unusable commence_time: {exc}", review=True)
            return

        # League-specific season (NBA Jan-June belongs to the previous start year).
        season = season_year_for(league.code, commence.strftime("%Y-%m-%d"))
        home = self._teams.resolve(
            provider=THE_ODDS_API_PROVIDER, provider_team_id=event.home_team_raw,
            league_id=league.league_id, raw_name=event.home_team_raw, season_year=season)
        away = self._teams.resolve(
            provider=THE_ODDS_API_PROVIDER, provider_team_id=event.away_team_raw,
            league_id=league.league_id, raw_name=event.away_team_raw, season_year=season)
        result.counters.team_resolution_attempts += 2
        for res in (home, away):
            if res.via_ambiguous_alias:
                self._dq(result, "blocking", "DQ-MATCH-006", "sportsbook_event", sb_id, event,
                         "sportsbook team resolved through an is_ambiguous alias")
        if not (home.is_matched and away.is_matched):
            self._record(result, sb_id, "no_candidate", "none", 0.0, None, [],
                         reason="home/away team did not resolve", review=True)
            return
        assert home.entity_id is not None and away.entity_id is not None  # noqa: S101
        if home.entity_id == away.entity_id:
            self._dq(result, "blocking", "DQ-SB-OUTCOME-001", "sportsbook_event", sb_id, event,
                     "sportsbook event resolves home and away to the same team")
            self._record(result, sb_id, "rejected", "same_team", 0.0, None, [],
                         reason="home and away resolve to the same team", review=True)
            return

        self._match_event(event, home.entity_id, away.entity_id, commence, result)

    # -- candidate tiers ----------------------------------------------------- #
    def _match_event(
        self, event, home_id, away_id, commence, result,  # type: ignore[no-untyped-def]
    ) -> None:
        sb_id = event.sb_event_id
        # Exact direct tier: same slate, orientation, and start within +/-90 min.
        ev90 = self._slate_filter(
            event, self._candidates(home_id, away_id, commence, _EXACT_WINDOW), commence, result)
        if len(ev90) == 1:
            c = ev90[0]
            self._accept(event, c, TIER_SCHEDULE_EXACT, min(SCORE_SCHEDULE_EXACT, c.cap),
                         "direct", ev90, home_id, away_id, commence, result)
            return
        if len(ev90) > 1:
            self._ambiguous(result, sb_id, ev90, TIER_SCHEDULE_EXACT,
                            "multiple same-slate canonical games within 90 minutes (direct)")
            return
        # Wider direct tier: consulted ONLY when the exact tier has zero candidates.
        ev12 = self._slate_filter(
            event, self._candidates(home_id, away_id, commence, _WIDE_WINDOW), commence, result)
        if len(ev12) == 1:
            c = ev12[0]
            self._accept(event, c, TIER_SCHEDULE_WINDOW, min(SCORE_SCHEDULE_WINDOW, c.cap),
                         "direct", ev12, home_id, away_id, commence, result)
            return
        if len(ev12) > 1:
            self._ambiguous(result, sb_id, ev12, TIER_SCHEDULE_WINDOW,
                            "multiple same-slate canonical games within 12 hours (direct)")
            return
        # Neutral-site swapped orientation (provider home/away reversed).
        swp = self._slate_filter(
            event, self._candidates(away_id, home_id, commence, _WIDE_WINDOW), commence, result)
        neutral = [c for c in swp if c.game.is_neutral_site]  # type: ignore[attr-defined]
        nonneutral = [c for c in swp if not c.game.is_neutral_site]  # type: ignore[attr-defined]
        if nonneutral:
            # A reversed orientation on a NON-neutral game inverts the sign; blocking.
            self._dq(result, "blocking", "DQ-MATCH-003", "sportsbook_event", sb_id, event,
                     "sportsbook home/away reversed against a non-neutral canonical game")
            self._record(result, sb_id, "rejected", "conflict", 0.0, None,
                         [c.game.game_id for c in nonneutral],  # type: ignore[attr-defined]
                         reason="reversed orientation against a non-neutral game", review=True)
            return
        if len(neutral) == 1:
            c = neutral[0]
            self._accept(event, c, TIER_SCHEDULE_SWAPPED, min(SCORE_SCHEDULE_SWAPPED, c.cap),
                         "swapped", neutral, home_id, away_id, commence, result)
            return
        if len(neutral) > 1:
            self._ambiguous(result, sb_id, neutral, TIER_SCHEDULE_SWAPPED,
                            "multiple same-slate neutral-site swapped candidates")
            return
        self._record(result, sb_id, "no_candidate", "none", 0.0, None, [],
                     reason="no same-slate canonical game matches the teams and time window",
                     review=True)

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

    # -- local-slate validation (task §3) ------------------------------------ #
    def _slate_filter(self, event, games, commence, result):  # type: ignore[no-untyped-def]
        """Keep only candidates that fall on the same resolved venue-local slate."""

        kept: list[_CandidateEval] = []
        for game in games:
            ev = self._slate_eval(event, game, commence, result)
            if ev is not None:
                kept.append(ev)
        return kept

    def _slate_eval(self, event, game, commence, result):  # type: ignore[no-untyped-def]
        """Local-date consistency for one candidate game, or ``None`` to exclude.

        The sportsbook event's candidate-relative local date is derived by the
        fixed hierarchy: the candidate's actual event-venue timezone (only when
        that venue association was known by ``event.last_observed_at``), else the
        knowledge-time-valid canonical home-venue timezone, else the UTC calendar
        date as a last resort. **The derived date must always equal the
        candidate's ``game_date_local``** -- a contradiction excludes the
        candidate (task §5, Policy A). The UTC fallback is not a licence to skip
        the check: it is kept only when the UTC-derived date still equals the
        canonical local date, and then only at the reduced ``DQ-TZ-001`` cap; a
        cross-midnight game without timezone evidence therefore does not
        false-match. An unresolvable timezone is surfaced honestly and the
        candidate excluded, never silently forced to UTC.
        """

        actual_tz = self._candidate_venue_tz(game, cutoff=event.last_observed_at)
        home_tz = None
        if actual_tz is None:
            home_tz = self._home._home_venue_tz(
                game.home_team_id, before_start=event.commence_time,
                cutoff=event.last_observed_at)
        try:
            local = resolve_local_date(
                scheduled_start=event.commence_time, actual_venue_tz=actual_tz,
                provider_local_date=None, home_venue_tz=home_tz)
        except InvalidTimezoneError as exc:
            self._dq(result, "issue", "DQ-TZ-001", "sportsbook_event", event.sb_event_id, event,
                     f"candidate game {game.game_id} local date unresolved: {exc}")
            return None
        if local.game_date_local != game.game_date_local:
            # Derived local date contradicts the canonical slate (including a UTC
            # fallback whose calendar date differs): exclude.
            return None
        return _CandidateEval(game=game, cap=local.confidence_cap, local_tier=local.tier)

    def _candidate_venue_tz(self, game, *, cutoff):  # type: ignore[no-untyped-def]
        """The candidate game's actual event-venue timezone, knowledge-time bounded.

        A neutral / international / relocated game carries its true event venue on
        the game row; that venue's timezone is the strongest local-date evidence.
        Two knowledge-time conditions must BOTH hold as of ``cutoff``
        (``event.last_observed_at``):

        * the venue **entity** was known (``venues.first_observed_at <= cutoff``);
        * the venue **association** to this game was known -- proven by an
          accepted, non-swapped game match decision for this game whose
          ``decided_at <= cutoff``. A venue that existed in the corpus for years
          but was only attached to a relocated/new game later must not leak
          backward.

        ``None`` when the game has no venue, the venue has no timezone, the venue
        entity post-dates the cutoff, or no accepted game-association decision was
        known by the cutoff -- the caller then falls back to home-venue/UTC policy.
        """

        if game.venue is None:
            return None
        venue = self._venues.get(game.venue)
        if venue is None or not venue.timezone:
            return None
        if cutoff is not None and venue.first_observed_at > cutoff:
            return None
        if not self._game_association_known(game.game_id, cutoff=cutoff):
            return None
        return venue.timezone

    def _game_association_known(self, game_id: str, *, cutoff) -> bool:  # type: ignore[no-untyped-def]
        """Whether the game's venue/identity association was known by ``cutoff``.

        Proven by an accepted, non-swapped ``entity_type='game'`` match decision
        for the game with ``decided_at <= cutoff`` -- the same knowledge-time
        anchor the home-venue tier uses. With no cutoff, no association qualifies
        (conservative)."""

        if cutoff is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM entity_match_decisions WHERE entity_type = 'game' "
            "AND matched_entity_id = ? AND outcome = 'accepted' AND method <> ? "
            "AND decided_at <= ? LIMIT 1",
            (game_id, TIER_SCHEDULE_SWAPPED, cutoff),
        ).fetchone()
        return row is not None

    # -- accept / record / link --------------------------------------------- #
    def _accept(
        self, event, cand, tier, score, orientation, candidate_evals, home_id, away_id,
        commence, result,  # type: ignore[no-untyped-def]
    ) -> None:
        sb_id = event.sb_event_id
        game = cand.game

        # (1) THIS event is already linked. Either an exact idempotent replay
        # (recognized, no new accepted decision, no recount), or a blocking
        # conflict; in neither case do we record a fresh accepted decision. The
        # persisted-link state is authoritative here (dry-run never has one).
        current_game, current_dec, current_orient = self._sb.event_link(sb_id)
        if current_game is not None and not self._dry_run:
            if (current_game == game.game_id and current_orient == orientation
                    and self._link_decision_valid(sb_id, current_dec, game.game_id)):
                # Exact idempotent replay: recognize and leave everything unchanged.
                result.counters.events_already_linked += 1
                return
            # Linked to a different game/orientation, or the current link's
            # decision is corrupt (wrong event, wrong game, not accepted).
            self._dq(result, "blocking", "DQ-MATCH-003", "sportsbook_event", sb_id, event,
                     f"event already linked to game {current_game} (orientation "
                     f"{current_orient!r}); attempt proposes game {game.game_id} "
                     f"({orientation!r})")
            result.counters.blocking_orientation_conflicts += 1
            self._record(result, sb_id, "rejected", tier, 0.0, None, [game.game_id],
                         reason="event already linked to a different game or orientation",
                         review=True)
            return

        # (2) A DIFFERENT event already linked to this game with an incompatible
        # orientation inverts the pricing sign. Detect it BEFORE recording any
        # accepted decision, so we never write an accepted row we cannot link.
        conflict = self._orientation_conflict(sb_id, game.game_id, orientation)
        if conflict is not None:
            other_id, other_orient = conflict
            self._dq(result, "blocking", "DQ-MATCH-003", "sportsbook_event", sb_id, event,
                     f"orientation {orientation!r} conflicts with event {other_id} "
                     f"({other_orient!r}) already linked to game {game.game_id}")
            result.counters.blocking_orientation_conflicts += 1
            self._record(result, sb_id, "rejected", tier, 0.0, None, [game.game_id],
                         reason=f"orientation conflict with linked event {other_id}", review=True)
            return

        # (3) Clean new link: record the accepted decision, apply and VERIFY the
        # link in the same transaction, then validate outcomes. A link failure
        # raises (rolls the whole attempt back) rather than leaving an accepted
        # decision unlinked (task §3).
        minutes = round(abs((_parse_utc(game.scheduled_start) - commence).total_seconds()) / 60)
        evidence = json.dumps({
            "home_team_id": home_id, "away_team_id": away_id, "delta_minutes": minutes,
            "local_date_tier": cand.local_tier, "orientation": orientation,
            "game_number": game.game_number,
        }, sort_keys=True)
        candidates = [
            CandidateInput(score=score, tier=tier, candidate_entity_id=c.game.game_id, method=tier,
                           evidence=evidence if c.game.game_id == game.game_id else None)
            for c in candidate_evals
        ]
        review = orientation == "swapped"
        decision_id = self._record(
            result, sb_id, "accepted", tier, score, game.game_id, candidates, review=review)
        if cand.local_tier == LOCALDATE_UTC_FALLBACK:
            self._dq(result, "note", "DQ-TZ-001", "sportsbook_event", sb_id, event,
                     "sportsbook local date fell back to the UTC calendar date")
        if orientation == "direct":
            result.counters.direct_orientation += 1
        else:
            result.counters.swapped_review_gated += 1
            self._dq(result, "issue", "DQ-MATCH-007", "sportsbook_event", sb_id, event,
                     "neutral-site swapped sportsbook match accepted pending review")
        self._link(result, sb_id, game.game_id, decision_id, orientation)
        # Outcomes are validated only after the link is applied and verified.
        self._validate_outcomes(event, orientation, result)

    def _orientation_conflict(self, sb_id, game_id, orientation):  # type: ignore[no-untyped-def]
        """The first ``(other_event, orientation)`` linked to ``game_id`` that
        disagrees with ``orientation``, ignoring this event's own current link."""

        for other_id, other_orient, _dec in self._sb.events_linked_to_game(game_id):
            if other_id != sb_id and other_orient is not None and other_orient != orientation:
                return (other_id, other_orient)
        return None

    def _link_decision_valid(self, sb_id, decision_id, game_id) -> bool:  # type: ignore[no-untyped-def]
        """Whether the current link's decision genuinely supports this link.

        True only when the decision exists, is a ``sportsbook_event`` decision
        for THIS event (`the_odds_api`/`sb_id`), is accepted, and names this
        game. Guards idempotent replay against a corrupt current link (a decision
        for another event, a rejected one, or one naming another game)."""

        if decision_id is None:
            return False
        d = self._match.get(decision_id)
        return (
            d is not None
            and d.entity_type == "sportsbook_event"
            and d.source_provider == THE_ODDS_API_PROVIDER
            and d.source_ref == sb_id
            and d.outcome == "accepted"
            and d.matched_entity_id == game_id
        )

    def _link(self, result, sb_id, game_id, decision_id, orientation):  # type: ignore[no-untyped-def]
        if self._dry_run or decision_id is None:
            return
        outcome = self._sb.link_game(
            sb_event_id=sb_id, game_id=game_id, match_decision_id=decision_id,
            orientation=orientation)
        if outcome == LinkOutcome.LINKED:
            result.counters.event_links_applied += 1
            result.counters.rows_persisted += 1
            return
        # Step 3 only runs after verifying this event had no existing link, so a
        # non-LINKED result is a concurrent/corrupt state. Raise to roll the whole
        # attempt back rather than commit an accepted decision without its link.
        raise SportsbookLinkError(
            f"link_game returned {outcome.value} for event {sb_id} -> game {game_id}; "
            "expected a clean LINKED")

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

    def _ambiguous(self, result, sb_id, cands, tier, reason):  # type: ignore[no-untyped-def]
        self._record(result, sb_id, "ambiguous", tier, 0.0, None,
                     [c.game.game_id for c in cands], reason=reason, review=True)

    # -- outcome semantics (task §7) ----------------------------------------- #
    def _validate_outcomes(self, event, orientation, result):  # type: ignore[no-untyped-def]
        """Independently recompute each outcome's provider-side role and validate
        market shape. Stored ``outcome_role`` is never trusted blindly, never
        rewritten, and a disagreement is surfaced (scoped to the outcome), never
        silently corrected.

        Approval is gated on real link readiness -- the same fail-closed
        `is_orientation_approved` check exposed to downstream callers -- not on
        the proposed orientation argument (task §11). A swapped/review-gated event
        approves nothing. An unsupported market key is retained and flagged, and
        none of its outcome roles are approved (task §12)."""

        sb_id = event.sb_event_id
        ready = (orientation == "direct") if self._dry_run else self._sb.is_orientation_approved(
            sb_id)
        for market in self._sb.list_markets_for_event(sb_id):
            outcomes = self._sb.list_outcomes_for_market(market.sb_market_id)
            result.counters.outcome_rows_checked += len(outcomes)
            if market.market_key not in _SUPPORTED_MARKETS:
                # Retain the market and its outcomes; approve nothing; surface once.
                for o in outcomes:
                    if self._provider_role(
                        o.provider_outcome_name, event, market.market_key
                    ) == "unknown":
                        result.counters.unknown_outcomes += 1
                self._dq(result, "issue", "DQ-SB-OUTCOME-001", "sportsbook_market",
                         market.sb_market_id, event,
                         f"{market.market_key}: unsupported market key; outcome roles are "
                         "not canonically approved")
                continue
            recomputed: list[tuple[str, Optional[float]]] = []
            for o in outcomes:
                role = self._provider_role(o.provider_outcome_name, event, market.market_key)
                recomputed.append((role, o.point))
                if o.outcome_role != role:
                    self._dq(
                        result, "issue", "DQ-SB-OUTCOME-001", "sportsbook_outcome",
                        o.sb_outcome_id, event,
                        f"{market.market_key}: stored role {o.outcome_role!r} disagrees with "
                        f"provider-side role {role!r} for {o.provider_outcome_name!r}")
                if role == "unknown":
                    result.counters.unknown_outcomes += 1
                elif ready and o.outcome_role == role and role in _APPROVABLE_ROLES:
                    result.counters.outcome_roles_approved += 1
            self._validate_market_shape(
                market.market_key, recomputed, market.sb_market_id, event, result)

    def _provider_role(self, provider_outcome_name, event, market_key):  # type: ignore[no-untyped-def]
        """Recompute the provider-side semantic role from immutable provider text.

        Uses the shared canonical normalizer. Market-aware: a team name inside a
        totals market is not a team side (it is unclassifiable there), and an
        over/under label inside a team market is likewise unclassifiable."""

        n = normalized_key(provider_outcome_name)
        if market_key == "totals":
            if n == "over":
                return "over"
            if n == "under":
                return "under"
            return "unknown"
        home = normalized_key(event.home_team_raw)
        away = normalized_key(event.away_team_raw)
        if n and n == home:
            return "home"
        if n and n == away:
            return "away"
        if n == "draw":
            return "draw"
        return "unknown"

    @staticmethod
    def _contract_key(market_key, point):  # type: ignore[no-untyped-def]
        """The grouping that makes one betting contract within a market row.

        Alternate lines share a market row, so completeness must be judged per
        contract, not across the whole row (task §14):

        * ``totals`` -- group by ``point`` (Over/Under of one line share it);
        * ``spreads`` -- group by ``abs(point)`` (home/away carry opposite signs
          of the same line);
        * ``h2h`` -- a single contract (no line)."""

        if market_key == "spreads":
            return None if point is None else abs(point)
        if market_key == "totals":
            return point
        return None

    def _validate_market_shape(self, market_key, roles_points, sb_market_id, event, result):  # type: ignore[no-untyped-def]
        if not roles_points:
            # An empty market row carries no outcome to interpret yet.
            return
        problem: Optional[str] = None
        if any(role == "unknown" for role, _pt in roles_points):
            # An unclassifiable outcome (a team name in a totals market, an
            # over/under in a moneyline, a name matching neither team) is retained
            # and surfaced -- a silently dropped outcome is missing data nobody sees.
            problem = f"{market_key} market has an unclassifiable/unknown outcome"
        else:
            groups: dict[object, list[str]] = {}
            for role, point in roles_points:
                groups.setdefault(self._contract_key(market_key, point), []).append(role)
            for _key, roles in sorted(
                groups.items(), key=lambda kv: (kv[0] is None, kv[0] if kv[0] is not None else 0.0)
            ):
                counts = {r: roles.count(r) for r in set(roles)}
                if market_key == "totals":
                    if counts.get("over", 0) > 1 or counts.get("under", 0) > 1:
                        problem = "totals contract has duplicate over/under outcomes"
                    elif counts.get("over", 0) < 1 or counts.get("under", 0) < 1:
                        problem = "totals contract is missing an over or under outcome"
                else:  # h2h / spreads
                    if counts.get("draw", 0):
                        problem = f"unexpected draw outcome on an MLB/NBA {market_key} market"
                    elif counts.get("home", 0) > 1 or counts.get("away", 0) > 1:
                        problem = f"{market_key} contract has duplicate home/away outcomes"
                    elif counts.get("home", 0) < 1 or counts.get("away", 0) < 1:
                        problem = f"{market_key} contract is missing a home or away outcome"
                if problem is not None:
                    break
        if problem is not None:
            self._dq(result, "issue", "DQ-SB-OUTCOME-001", "sportsbook_market", sb_market_id,
                     event, f"{market_key}: {problem}")

    # -- dq (task §8) -------------------------------------------------------- #
    def _dq(self, result, severity, rule_code, entity_type, entity_id, event, description):  # type: ignore[no-untyped-def]
        """Record one DQ issue, scoped to the narrowest relevant entity.

        Idempotency key is ``(rule_code, entity_type, entity_id, provider,
        description)`` among unresolved rows: an identical rerun is a no-op, two
        distinct defects (different entity or materially different description)
        each stay visible. Carries the event's raw-response provenance."""

        existing = self._conn.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_type = ? "
            "AND entity_id IS ? AND provider IS ? AND description = ? AND resolved_at IS NULL "
            "LIMIT 1",
            (rule_code, entity_type, entity_id, THE_ODDS_API_PROVIDER, description),
        ).fetchone()
        if existing is not None:
            return
        result.counters.dq_issues += 1
        if severity == "blocking":
            result.counters.blocking_issues += 1
        if self._dry_run:
            return
        raw = event.raw_response_id if event is not None else self._current_raw
        detail = json.dumps(
            {"sb_event_id": event.sb_event_id if event is not None else None,
             "entity_type": entity_type, "entity_id": entity_id},
            sort_keys=True)
        self._dqrepo.record(
            severity=severity, rule_code=rule_code, entity_type=entity_type,
            description=description, entity_id=entity_id, provider=THE_ODDS_API_PROVIDER,
            detail_json=detail, run_id=self._run_id, raw_response_id=raw)
        result.counters.rows_persisted += 1
