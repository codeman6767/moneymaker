"""Phase D5B2: deterministic Kalshi event + game-winner market matching.

Resolves already-ingested PUBLIC Kalshi MLB/NBA events and supported binary
game-winner markets to the existing canonical ``games`` using ONLY exact series
filtering, versioned ticker parsing, provider-scoped (``kalshi_public``) team
aliases, explicit title/rules agreement, and venue-aware canonical schedule
evidence -- never a price, order-book side, trade, volume, market result, or
settlement value. Every processed event and supported market records exactly one
append-only ``entity_match_decisions`` row plus its candidates; an accepted link
is applied atomically with the exact decision (and, for a market, the canonical
Yes team, the supported semantic, and the matched ``rules_hash``).

Only binary game-winner markets are linked automatically. Spreads, totals,
player props, team totals, period markets, and multivariate/combo markets remain
stored and are reported as unsupported semantics, never mislabeled. A later rules
change invalidates a market's orientation through a blocking ``DQ-MATCH-004`` and
never silently retains approval. This module never imports or reads the Kalshi
order-book, level, or trade tables, and implements no Phase E work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db.engine import transaction
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.games import SqliteGameRepository
from ..db.repositories.kalshi import SqliteKalshiRepository
from ..db.repositories.leagues import SqliteLeagueRepository
from ..db.repositories.matching import CandidateInput, SqliteMatchingRepository
from ..db.repositories.references import LinkOutcome
from ..db.repositories.venues import SqliteVenueRepository
from ..db.schema import KALSHI_PUBLIC_PROVIDER
from . import kalshi_parse as kp
from .linkatomic import MatchLinkError
from .localdate import _parse_utc
from .model import MATCHER_VERSION, TIER_SCHEDULE_SWAPPED
from .season import season_year_for
from .service import MatchGamesService
from .teams import TeamResolver

_THRESHOLD = 0.85
_EXACT_WINDOW = timedelta(minutes=90)
_SCORE_TICKER_TIME = 0.97   # Tier 1: rules exact time + venue-local slate
_SCORE_DATE = 0.92          # Tier 2: provider date + local slate
_UTC_CAP = 0.88             # UTC-fallback confidence cap (mirrors D5A/D5B1)

_SPORT_TO_SERIES = {"mlb": "KXMLBGAME", "nba": "KXNBAGAME"}


def series_ticker_for_sport(sport: Optional[str]) -> Optional[str]:
    return _SPORT_TO_SERIES.get(sport) if sport else None


def _local_clock_to_utc(
    date_local: str, local_clock: str, venue_tz: str, tz_abbrev: Optional[str]
) -> tuple[Optional[datetime], Optional[str]]:
    """Convert a venue-LOCAL wall clock to a UTC instant via the venue timezone.

    ``date_local`` (``YYYY-MM-DD``) + ``local_clock`` (``HH:MM``) are a naive wall
    time in ``venue_tz`` (an IANA name). Returns ``(utc_datetime, None)`` on
    success or ``(None, reason)`` when the timezone is unknown, the local time is
    DST-ambiguous/invalid, or a supplied ``tz_abbrev`` (e.g. ``EDT``) does not
    match the venue timezone's abbreviation at that instant. The local clock is
    NEVER treated as UTC or as a fixed offset (task §4/§6)."""

    try:
        zone = ZoneInfo(venue_tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None, f"unknown venue timezone {venue_tz!r}"
    y, mo, d = (int(x) for x in date_local.split("-"))
    hh, mm = (int(x) for x in local_clock.split(":"))
    naive = datetime(y, mo, d, hh, mm)
    dt0 = naive.replace(tzinfo=zone, fold=0)
    dt1 = naive.replace(tzinfo=zone, fold=1)
    if dt0.utcoffset() != dt1.utcoffset():
        # DST fold (ambiguous) or gap (invalid) local time -> refuse, never guess.
        return None, f"DST-ambiguous/invalid local time {date_local} {local_clock} in {venue_tz}"
    if tz_abbrev is not None and dt0.tzname() != tz_abbrev:
        return None, (f"rules timezone {tz_abbrev!r} does not match venue {venue_tz!r} "
                      f"({dt0.tzname()}) at {date_local} {local_clock}")
    return dt0.astimezone(timezone.utc), None


@dataclass
class KalCounters:
    """Every counter the match-markets report/CLI exposes (task §20)."""

    events_considered: int = 0
    events_supported: int = 0
    events_accepted: int = 0
    events_ambiguous: int = 0
    events_no_candidate: int = 0
    events_rejected: int = 0
    events_linked: int = 0
    events_already_linked: int = 0
    markets_considered: int = 0
    markets_supported: int = 0
    markets_accepted: int = 0
    markets_ambiguous: int = 0
    markets_no_candidate: int = 0
    markets_rejected: int = 0
    markets_linked: int = 0
    markets_already_linked: int = 0
    yes_teams_resolved: int = 0
    rules_hash_conflicts: int = 0
    unsupported_series: int = 0
    unsupported_semantics: int = 0
    candidates_recorded: int = 0
    dq_issues: int = 0
    blocking_issues: int = 0
    rows_persisted: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class MatchKalshiResult:
    dry_run: bool
    status: str = "succeeded"
    counters: KalCounters = field(default_factory=KalCounters)
    run_id: Optional[str] = None

    @property
    def needs_failure_exit(self) -> bool:
        return self.counters.blocking_issues > 0


@dataclass(frozen=True)
class _TeamsResolved:
    away_id: str
    home_id: str
    by_code: dict[str, str]  # provider code -> team_id


class MatchKalshiService:
    """Resolves public Kalshi events + game-winner markets to canonical games."""

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
        self._kal = SqliteKalshiRepository(conn)
        self._games = SqliteGameRepository(conn)
        self._leagues = SqliteLeagueRepository(conn)
        self._match = SqliteMatchingRepository(conn)
        self._dqrepo = SqliteDataQualityRepository(conn)
        self._venues = SqliteVenueRepository(conn)
        self._teams = TeamResolver(conn)
        self._home = MatchGamesService(conn)  # reused ONLY for _home_venue_tz
        self._current_raw: Optional[str] = None

    # -- public entry -------------------------------------------------------- #
    def match_range(
        self,
        *,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        market_ticker: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        unmatched_only: bool = False,
    ) -> MatchKalshiResult:
        result = MatchKalshiResult(dry_run=self._dry_run, run_id=self._run_id)
        # Events first, then their supported markets (task §20).
        for event in self._kal.list_events_for_matching(
            series_ticker=series_ticker, event_ticker=event_ticker,
            unmatched_only=unmatched_only,
        ):
            result.counters.events_considered += 1
            self._resolve_event(event, result)
        for market in self._kal.list_markets_for_matching(
            series_ticker=series_ticker, event_ticker=event_ticker,
            market_ticker=market_ticker, from_date=from_date, to_date=to_date,
            unmatched_only=unmatched_only,
        ):
            result.counters.markets_considered += 1
            self._resolve_market(market, result)
        result.status = "partially_failed" if result.needs_failure_exit else "succeeded"
        return result

    # -- shared team-code helpers ------------------------------------------- #
    def _valid_codes(self, league_id: str) -> frozenset[str]:
        """Curated ``kalshi_public`` team codes for a league (for ticker splits).

        Ambiguous aliases are INCLUDED here so a ticker still splits cleanly; the
        ambiguity is then surfaced by :class:`TeamResolver` as a blocking
        ``DQ-MATCH-006`` during resolution, rather than being hidden as an
        unparseable ticker."""

        rows = self._conn.execute(
            "SELECT DISTINCT alias FROM team_aliases WHERE league_id = ? AND provider = ?",
            (league_id, KALSHI_PUBLIC_PROVIDER),
        ).fetchall()
        return frozenset(str(r["alias"]) for r in rows)

    def _resolve_code(self, code: str, league_id: str, season: int) -> Optional[str]:
        """Resolve one provider team code to a canonical team, or ``None``.

        Returns the team id on a clean match; ``None`` when unresolved. Sets a
        flag on the result via the caller for the ambiguous case is handled by
        inspecting ``via_ambiguous_alias`` here."""

        res = self._teams.resolve(
            provider=KALSHI_PUBLIC_PROVIDER, provider_team_id=code, raw_name=code,
            league_id=league_id, season_year=season)
        if res.via_ambiguous_alias:
            return None
        return res.entity_id if res.is_matched else None

    def _resolve_name(self, name: str, league_id: str, season: int) -> Optional[str]:
        res = self._teams.resolve(
            provider=KALSHI_PUBLIC_PROVIDER, provider_team_id=name, raw_name=name,
            league_id=league_id, season_year=season)
        if res.via_ambiguous_alias:
            return None
        return res.entity_id if res.is_matched else None

    # -- event matching ------------------------------------------------------ #
    def _resolve_event(self, event, result: MatchKalshiResult) -> None:  # type: ignore[no-untyped-def]
        kev = event.kalshi_event_id
        self._current_raw = event.current_raw_response_id
        series = kp.series_for(event.series_ticker)
        if series is None:
            # Unsupported (non-sports or unsupported sports) series: honest
            # no-candidate, NOT review-gated, so the review queue is not flooded.
            result.counters.unsupported_series += 1
            self._record(result, "kalshi_event", kev, "no_candidate", "unsupported_series", 0.0,
                         None, [], reason="unsupported Kalshi series", review=False)
            return
        result.counters.events_supported += 1
        league = self._leagues.get_by_code(series.league_code)
        if league is None:
            self._record(result, "kalshi_event", kev, "no_candidate", "none", 0.0, None, [],
                         reason="league not configured", review=True)
            return
        league_id = league.league_id

        parsed = kp.parse_event_ticker(event.event_ticker, self._valid_codes(league_id))
        if isinstance(parsed, kp.ParseError):
            self._dq(result, "issue", "DQ-KAL-SERIES-001", "kalshi_event", kev, event,
                     f"event ticker unparseable: {parsed.reason}")
            self._record(result, "kalshi_event", kev, "no_candidate", series.parser_version, 0.0,
                         None, [], reason=parsed.reason, review=True)
            return

        season = season_year_for(series.league_code, parsed.game_date_local)
        teams = self._resolve_pair(parsed.away_code, parsed.home_code, league_id, season,
                                   kev, event, result)
        if teams is None:
            return

        # Title cross-check: the ticker and title teams (and 'at' orientation) agree.
        title_kind = self._title_agrees(event.title, event.sub_title, teams, league_id, season,
                                        "kalshi_event", kev, event, result)
        if title_kind is None:
            return

        # MLB event tickers carry a venue-local clock; NBA are date-only. Events
        # have no rules, so there is no timezone abbreviation to verify here.
        game = self._match_game(
            league_id, teams, parsed.game_date_local, local_clock=parsed.local_clock,
            tz_abbrev=None, cutoff=event.last_observed_at, entity_type="kalshi_event",
            entity_id=kev, event=event, result=result,
        )
        if game is None:
            return
        cand, tier, score = game
        self._accept_event(event, cand, tier, score, [cand], title_kind, result)

    def _accept_event(self, event, game, tier, score, candidate_games, title_kind, result):  # type: ignore[no-untyped-def]
        kev = event.kalshi_event_id
        current_game, current_dec = self._kal.event_link(kev)
        if current_game is not None and not self._dry_run:
            if current_game == game.game_id and self._event_decision_valid(kev, current_dec,
                                                                           game.game_id):
                result.counters.events_already_linked += 1
                return
            # Different game, or same game with a corrupt/review-gated decision:
            # blocking, never an idempotent replay (task §4).
            self._dq(result, "blocking", "DQ-MATCH-003", "kalshi_event", kev, event,
                     f"event already linked to game {current_game} via an invalid/mismatched "
                     f"decision; attempt proposes {game.game_id}")
            self._record(result, "kalshi_event", kev, "rejected", tier, 0.0, None,
                         [game.game_id],
                         reason="event already linked to a different game or corrupt decision",
                         review=True)
            return
        evidence = json.dumps({"away": game.away_team_id, "home": game.home_team_id,
                               "tier": tier, "game_date_local": game.game_date_local,
                               "title": title_kind}, sort_keys=True)
        candidates = [CandidateInput(score=score, tier=tier, candidate_entity_id=g.game_id,
                                     method=tier, evidence=evidence if g.game_id == game.game_id
                                     else None) for g in candidate_games]
        if self._dry_run:
            result.counters.events_accepted += 1
            result.counters.candidates_recorded += len(candidates)
            return
        # Record the accepted decision and apply + verify the link as ONE atomic
        # unit, safe even for a direct persisted service call with no outer
        # transaction (task §2). A non-LINKED result raises, rolling the decision
        # and candidates back; committed counters increment only afterwards.
        with transaction(self._conn):
            decision = self._match.record_decision(
                entity_type="kalshi_event", source_provider=KALSHI_PUBLIC_PROVIDER,
                source_ref=kev, outcome="accepted", method=tier, score=score,
                threshold=_THRESHOLD, matcher_version=self._version, candidates=candidates,
                matched_entity_id=game.game_id, needs_manual_review=False, run_id=self._run_id,
                raw_response_id=self._current_raw)
            outcome = self._kal.link_event_game(kalshi_event_id=kev, game_id=game.game_id,
                                                match_decision_id=decision.match_id)
            if outcome != LinkOutcome.LINKED:
                raise MatchLinkError(
                    f"kalshi event link {kev} -> {game.game_id} returned {outcome.value}")
        result.counters.events_accepted += 1
        result.counters.candidates_recorded += len(candidates)
        result.counters.events_linked += 1
        result.counters.rows_persisted += 2

    # -- market matching ----------------------------------------------------- #
    def _resolve_market(self, market, result: MatchKalshiResult) -> None:  # type: ignore[no-untyped-def]
        kmk = market.kalshi_market_id
        self._current_raw = market.current_raw_response_id
        series = kp.series_for(market.series_ticker)
        if series is None:
            result.counters.unsupported_series += 1
            self._record(result, "kalshi_market", kmk, "no_candidate", "unsupported_series", 0.0,
                         None, [], reason="unsupported Kalshi series", review=False)
            return
        result.counters.markets_supported += 1
        league = self._leagues.get_by_code(series.league_code)
        if league is None:
            self._record(result, "kalshi_market", kmk, "no_candidate", "none", 0.0, None, [],
                         reason="league not configured", review=True)
            return
        league_id = league.league_id
        codes = self._valid_codes(league_id)

        if market.event_ticker is None:
            self._record(result, "kalshi_market", kmk, "no_candidate", series.parser_version, 0.0,
                         None, [], reason="market carries no event ticker", review=True)
            return
        pev = kp.parse_event_ticker(market.event_ticker, codes)
        if isinstance(pev, kp.ParseError):
            self._dq(result, "issue", "DQ-KAL-SERIES-001", "kalshi_market", kmk, market,
                     f"market event ticker unparseable: {pev.reason}")
            self._record(result, "kalshi_market", kmk, "no_candidate", series.parser_version, 0.0,
                         None, [], reason=pev.reason, review=True)
            return
        # Classify the market semantic. A binary game-winner market's ticker
        # suffix is exactly one of the event's two team codes (the Yes subject).
        # Any other supported-series market (spread/total/prop/period/combo) is
        # retained and reported as an UNSUPPORTED SEMANTIC -- never linked, never
        # flagged malformed (task §10). A suffix that does not descend cleanly
        # from the event ticker is a genuine malformation (reviewable).
        mt = market.market_ticker.strip()
        prefix = pev.event_ticker + "-"
        if not mt.startswith(prefix):
            self._dq(result, "issue", "DQ-KAL-SERIES-001", "kalshi_market", kmk, market,
                     "market ticker does not descend from its event ticker")
            self._record(result, "kalshi_market", kmk, "no_candidate", series.parser_version, 0.0,
                         None, [], reason="market ticker ancestry mismatch", review=True)
            return
        if mt[len(prefix):] not in (pev.away_code, pev.home_code):
            result.counters.unsupported_semantics += 1
            self._record(result, "kalshi_market", kmk, "no_candidate", "unsupported_semantic",
                         0.0, None, [], reason="unsupported market semantic (not game-winner)",
                         review=False)
            return
        pmk = kp.parse_market_ticker(market.market_ticker, pev)
        if isinstance(pmk, kp.ParseError):  # defensive: ruled out by the checks above
            self._record(result, "kalshi_market", kmk, "no_candidate", series.parser_version, 0.0,
                         None, [], reason=pmk.reason, review=True)
            return

        season = season_year_for(series.league_code, pev.game_date_local)
        teams = self._resolve_pair(pev.away_code, pev.home_code, league_id, season, kmk, market,
                                   result)
        if teams is None:
            return
        title_kind = self._title_agrees(market.title, market.subtitle, teams, league_id, season,
                                        "kalshi_market", kmk, market, result)
        if title_kind is None:
            return

        # Rules are authoritative for the settlement subject; they also cross-check
        # the ticker teams/time and validate no_sub_title.
        yes_team, local_clock, tz_abbrev = self._resolve_yes_and_rules(
            market, pmk, pev, teams, league_id, season, result)
        if yes_team is None:
            return
        result.counters.yes_teams_resolved += 1

        game = self._match_game(
            league_id, teams, pev.game_date_local, local_clock=local_clock, tz_abbrev=tz_abbrev,
            cutoff=market.last_observed_at, entity_type="kalshi_market", entity_id=kmk,
            event=market, result=result,
        )
        if game is None:
            return
        cand, tier, score = game
        self._accept_market(market, cand, tier, score, [cand], yes_team, series, title_kind,
                            result)

    def _resolve_yes_and_rules(self, market, pmk, pev, teams, league_id, season, result):  # type: ignore[no-untyped-def]
        """Resolve the canonical Yes team and the scheduled local clock/timezone.

        Returns ``(yes_team_id, local_clock, tz_abbrev)`` or ``(None, None, None)``
        on rejection. Ticker, ``yes_sub_title``, and authoritative rules must all
        agree on the Yes team and the participants; rules also cross-check the
        ticker date and (MLB) local clock. Never defaults Yes to home/first-team;
        never reads a price, result, or order book."""

        kmk = market.kalshi_market_id
        reject = lambda code, msg, dq_sev="issue": (  # noqa: E731
            self._dq(result, dq_sev, code, "kalshi_market", kmk, market, msg),
            self._record(result, "kalshi_market", kmk, "rejected", "conflict", 0.0, None, [],
                         reason=msg, review=True))

        yes_from_ticker = teams.by_code.get(pmk.yes_code)
        if yes_from_ticker is None or yes_from_ticker not in (teams.away_id, teams.home_id):
            reject("DQ-KAL-YES-001", "Yes subject team does not participate in the game",
                   "blocking")
            return None, None, None
        # yes_sub_title must agree with the ticker Yes subject.
        if market.yes_sub_title:
            if self._resolve_name(market.yes_sub_title, league_id, season) != yes_from_ticker:
                reject("DQ-KAL-RULES-001", "yes_sub_title disagrees with the ticker Yes subject")
                return None, None, None
        # Rules are authoritative.
        rules = kp.parse_rules_yes_subject(market.rules_primary)
        if rules is None or isinstance(rules, kp.ParseError):
            reason = "rules absent" if rules is None else rules.reason
            reject("DQ-KAL-RULES-001", f"rules Yes subject unresolved: {reason}")
            return None, None, None
        rules_pair = {self._resolve_name(n, league_id, season) for n in rules.names}
        if None in rules_pair or rules_pair != {teams.away_id, teams.home_id}:
            reject("DQ-KAL-RULES-001", "rules team set disagrees with the ticker/title teams")
            return None, None, None
        if self._resolve_name(rules.yes_name, league_id, season) != yes_from_ticker:
            reject("DQ-KAL-RULES-001", "rules Yes subject disagrees with the ticker Yes subject")
            return None, None, None
        # Ordered `at` rules must agree with the ticker away/home orientation.
        if rules.away_name is not None and rules.home_name is not None:
            if (self._resolve_name(rules.away_name, league_id, season) != teams.away_id
                    or self._resolve_name(rules.home_name, league_id, season) != teams.home_id):
                reject("DQ-KAL-RULES-001", "rules 'at' orientation disagrees with the ticker")
                return None, None, None
        # Rules scheduled date must agree with the ticker date.
        if rules.scheduled_date != pev.game_date_local:
            reject("DQ-KAL-RULES-001", "rules scheduled date disagrees with the ticker date")
            return None, None, None
        # A ticker local clock (MLB) and a rules local clock must not disagree.
        if pev.local_clock is not None and rules.local_clock is not None \
                and pev.local_clock != rules.local_clock:
            reject("DQ-KAL-RULES-001", "rules scheduled clock disagrees with the ticker clock")
            return None, None, None
        # `no_sub_title`, when present, must resolve to a game participant (current
        # public Kalshi sets it to the Yes-subject team, not the opponent); an
        # unresolved or unrelated team is rejected. Absent is acceptable because
        # the authoritative ticker + rules already prove both binary participants.
        if market.no_sub_title:
            no_id = self._resolve_name(market.no_sub_title, league_id, season)
            if no_id is None or no_id not in (teams.away_id, teams.home_id):
                reject("DQ-KAL-RULES-001", "no_sub_title does not name a game participant")
                return None, None, None
        local_clock = pev.local_clock if pev.local_clock is not None else rules.local_clock
        return yes_from_ticker, local_clock, rules.tz_abbrev

    def _accept_market(self, market, game, tier, score, candidate_games, yes_team, series,
                       title_kind, result):  # type: ignore[no-untyped-def]
        kmk = market.kalshi_market_id
        rules_hash = market.rules_hash
        if rules_hash is None:
            self._record(result, "kalshi_market", kmk, "no_candidate", tier, 0.0, None, [],
                         reason="market has no rules hash to bind the decision to", review=True)
            return
        # Owning-event consistency: a linked event must name the same game.
        if market.kalshi_event_id is not None:
            ev_game, _ = self._kal.event_link(market.kalshi_event_id)
            if ev_game is not None and ev_game != game.game_id:
                self._dq(result, "blocking", "DQ-MATCH-003", "kalshi_market", kmk, market,
                         "market game disagrees with its linked event's game")
                self._record(result, "kalshi_market", kmk, "rejected", "conflict", 0.0, None,
                             [game.game_id], reason="event/market game disagreement", review=True)
                return
        # Pre-check the market's own current link (replay / rules-change / conflict).
        cur = self._kal.market_link(kmk)
        cur_game, cur_dec, cur_yes, cur_hash, cur_sem = cur
        if cur_game is not None and not self._dry_run:
            same_link = (cur_game == game.game_id and cur_yes == yes_team
                         and cur_sem == series.semantic)
            if (same_link and cur_hash == rules_hash
                    and self._market_decision_valid(kmk, cur_dec, game.game_id)):
                result.counters.markets_already_linked += 1
                return
            if same_link and cur_hash != rules_hash:
                # Rules changed since the accepted decision was bound (task §15):
                # invalidate readiness with a blocking DQ-MATCH-004 and FLAG the
                # existing decision for review WITHOUT recording a human reviewer
                # (task §8). Never rewrite the matched hash or record a fresh
                # accepted decision.
                result.counters.rules_hash_conflicts += 1
                self._dq(result, "blocking", "DQ-MATCH-004", "kalshi_market", kmk, market,
                         "market rules_hash changed since the accepted decision; "
                         "orientation invalidated")
                if cur_dec is not None:
                    self._match.flag_for_review(cur_dec)
                return
            self._dq(result, "blocking", "DQ-MATCH-003", "kalshi_market", kmk, market,
                     f"market already linked (game {cur_game}, yes {cur_yes}) incompatibly")
            self._record(result, "kalshi_market", kmk, "rejected", tier, 0.0, None,
                         [game.game_id], reason="market already linked incompatibly", review=True)
            return
        evidence = json.dumps({"yes_team_id": yes_team, "semantic": series.semantic,
                               "tier": tier, "game_date_local": game.game_date_local,
                               "title": title_kind}, sort_keys=True)
        candidates = [CandidateInput(score=score, tier=tier, candidate_entity_id=g.game_id,
                                     method=tier, evidence=evidence if g.game_id == game.game_id
                                     else None) for g in candidate_games]
        if self._dry_run:
            result.counters.markets_accepted += 1
            result.counters.candidates_recorded += len(candidates)
            return
        # Record the accepted decision and apply + verify the full semantic link
        # (game, decision, Yes team, matched hash, semantic) as ONE atomic unit,
        # safe for a direct persisted call (task §3). `link_market_game` also
        # verifies the Yes team participates in the game; any non-LINKED result
        # raises and rolls the decision, candidates and semantic fields back.
        with transaction(self._conn):
            decision = self._match.record_decision(
                entity_type="kalshi_market", source_provider=KALSHI_PUBLIC_PROVIDER,
                source_ref=kmk, outcome="accepted", method=tier, score=score,
                threshold=_THRESHOLD, matcher_version=self._version, candidates=candidates,
                matched_entity_id=game.game_id, needs_manual_review=False, run_id=self._run_id,
                raw_response_id=self._current_raw)
            outcome = self._kal.link_market_game(
                kalshi_market_id=kmk, game_id=game.game_id, match_decision_id=decision.match_id,
                yes_team_id=yes_team, matched_rules_hash=rules_hash,
                market_semantic=series.semantic)
            if outcome != LinkOutcome.LINKED:
                raise MatchLinkError(
                    f"kalshi market link {kmk} -> {game.game_id} returned {outcome.value}")
        result.counters.markets_accepted += 1
        result.counters.candidates_recorded += len(candidates)
        result.counters.markets_linked += 1
        result.counters.rows_persisted += 2

    # -- team pair + title helpers ------------------------------------------ #
    def _resolve_pair(self, away_code, home_code, league_id, season, entity_id, event, result):  # type: ignore[no-untyped-def]
        etype = "kalshi_event" if event.__class__.__name__ == "KalshiEvent" else "kalshi_market"
        # Ambiguous alias -> blocking, stop.
        for code in (away_code, home_code):
            res = self._teams.resolve(
                provider=KALSHI_PUBLIC_PROVIDER, provider_team_id=code, raw_name=code,
                league_id=league_id, season_year=season)
            if res.via_ambiguous_alias:
                self._dq(result, "blocking", "DQ-MATCH-006", etype, entity_id, event,
                         f"team code {code!r} resolved through an is_ambiguous alias")
                self._record(result, etype, entity_id, "rejected", "ambiguous_team", 0.0, None,
                             [], reason="ambiguous team alias", review=True)
                return None
        away_id = self._resolve_code(away_code, league_id, season)
        home_id = self._resolve_code(home_code, league_id, season)
        if away_id is None or home_id is None:
            self._record(result, etype, entity_id, "no_candidate", "none", 0.0, None, [],
                         reason="ticker team code did not resolve", review=True)
            return None
        if away_id == home_id:
            self._record(result, etype, entity_id, "rejected", "same_team", 0.0, None, [],
                         reason="away and home resolve to the same team", review=True)
            return None
        return _TeamsResolved(away_id=away_id, home_id=home_id,
                              by_code={away_code: away_id, home_code: home_id})

    def _title_agrees(self, title, subtitle, teams, league_id, season, etype, entity_id, event,
                      result):  # type: ignore[no-untyped-def]
        """Cross-check the ticker teams against the title, honouring orientation.

        Returns ``'ordered'`` (an ``A at B`` title whose away/home agree with the
        ticker), ``'unordered'`` (a valid ``A vs B`` set match), or ``None`` when
        the title is absent/one-sided/reversed/mismatched (rejected + review). An
        ``at`` title with reversed away/home is a real orientation error and is
        rejected, never silently reduced to an unordered set."""

        parsed = kp.parse_title_teams(title)
        if not isinstance(parsed, kp.TitleTeams):
            sub = kp.parse_title_teams(subtitle)
            if isinstance(sub, kp.TitleTeams):
                parsed = sub
        if not isinstance(parsed, kp.TitleTeams):
            reason = "title absent" if parsed is None else parsed.reason
            self._dq(result, "issue", "DQ-KAL-TITLE-001", etype, entity_id, event,
                     f"title does not name a clear game pair: {reason}")
            self._record(result, etype, entity_id, "rejected", "title_unresolved", 0.0, None, [],
                         reason=f"title unresolved: {reason}", review=True)
            return None
        if parsed.away_name is not None and parsed.home_name is not None:
            # Ordered `A at B`: away/home must match the ticker's away/home.
            away = self._resolve_name(parsed.away_name, league_id, season)
            home = self._resolve_name(parsed.home_name, league_id, season)
            if away != teams.away_id or home != teams.home_id:
                self._dq(result, "issue", "DQ-KAL-TITLE-001", etype, entity_id, event,
                         "ordered 'at' title orientation disagrees with the ticker away/home")
                self._record(result, etype, entity_id, "rejected", "conflict", 0.0, None, [],
                             reason="ticker/title orientation disagreement", review=True)
                return None
            return "ordered"
        # Unordered `A vs B`: only the team SET must match; no orientation claimed.
        title_ids = {self._resolve_name(n, league_id, season) for n in parsed.names}
        if None in title_ids or title_ids != {teams.away_id, teams.home_id}:
            self._dq(result, "issue", "DQ-KAL-TITLE-001", etype, entity_id, event,
                     "ticker and title team sets disagree")
            self._record(result, etype, entity_id, "rejected", "conflict", 0.0, None, [],
                         reason="ticker/title team disagreement", review=True)
            return None
        return "unordered"

    # -- canonical game matching (tiers §9/§11) ------------------------------ #
    def _match_game(self, league_id, teams, date_local, *, local_clock, tz_abbrev, cutoff,
                    entity_type, entity_id, event, result):  # type: ignore[no-untyped-def]
        """Return ``(game, tier, score)`` for exactly one canonical candidate, else
        record ambiguous/no_candidate and return ``None``.

        ``local_clock`` is a venue-LOCAL wall clock (``HH:MM``) from the MLB ticker
        and/or the rules -- never a UTC instant. Tier 1 converts it through each
        candidate's actual event-venue ``zoneinfo`` timezone (knowledge-time
        gated) to UTC and requires it within +/-90 min of the canonical start on
        the same slate. When no candidate has usable venue evidence, or the clock
        is absent (NBA date-only), it falls back to Tier 2 (date-only slate)."""

        candidates = self._games.find_on_local_date(
            league_id=league_id, home_team_id=teams.home_id, away_team_id=teams.away_id,
            game_date_local=date_local)
        if local_clock is not None:
            timed = []
            for g in candidates:
                vtz = self._candidate_venue_tz(g, cutoff=cutoff)
                if vtz is None:
                    continue  # no knowledge-time venue evidence -> no exact-time here
                utc, err = _local_clock_to_utc(date_local, local_clock, vtz, tz_abbrev)
                if err is not None:
                    self._dq(result, "issue", "DQ-TZ-001", entity_type, entity_id, event,
                             f"candidate {g.game_id}: {err}")
                    continue
                if abs((utc - _parse_utc(g.scheduled_start)).total_seconds()) \
                        <= _EXACT_WINDOW.total_seconds():
                    timed.append(g)
            if len(timed) == 1:
                return timed[0], "kalshi_ticker_time", _SCORE_TICKER_TIME
            if len(timed) > 1:
                self._ambiguous(result, entity_type, entity_id, [g.game_id for g in timed],
                                "kalshi_ticker_time")
                return None
            # else: no venue evidence / no time match -> conservative date-only.

        # Tier 2: provider date + same venue-local slate (game_date_local).
        if len(candidates) == 1:
            return candidates[0], "kalshi_date", _SCORE_DATE
        if len(candidates) > 1:
            self._ambiguous(result, entity_type, entity_id, [g.game_id for g in candidates],
                            "kalshi_date")
            return None
        self._record(result, entity_type, entity_id, "no_candidate", "none", 0.0, None, [],
                     reason="no canonical game matches the teams and date/time", review=True)
        return None

    def _candidate_venue_tz(self, game, *, cutoff):  # type: ignore[no-untyped-def]
        if game.venue is None:
            return None
        venue = self._venues.get(game.venue)
        if venue is None or not venue.timezone:
            return None
        if cutoff is not None and venue.first_observed_at > cutoff:
            return None
        row = self._conn.execute(
            "SELECT 1 FROM entity_match_decisions WHERE entity_type = 'game' "
            "AND matched_entity_id = ? AND outcome = 'accepted' AND method <> ? "
            "AND decided_at <= ? LIMIT 1",
            (game.game_id, TIER_SCHEDULE_SWAPPED, cutoff)).fetchone()
        return venue.timezone if row is not None else None

    # -- decision / dq helpers ----------------------------------------------- #
    def _event_decision_valid(self, kev, decision_id, game_id) -> bool:  # type: ignore[no-untyped-def]
        """Whether an event's current link decision genuinely, cleanly backs it.

        A matching game id alone is NOT enough (task §4): the decision must exist,
        be a ``kalshi_event`` decision for THIS event, accepted, name this game,
        and NOT be review-gated (a flagged decision is not a clean replay)."""

        if decision_id is None:
            return False
        d = self._match.get(decision_id)
        return (d is not None and d.entity_type == "kalshi_event"
                and d.source_provider == KALSHI_PUBLIC_PROVIDER and d.source_ref == kev
                and d.outcome == "accepted" and d.matched_entity_id == game_id
                and not d.needs_manual_review)

    def _market_decision_valid(self, kmk, decision_id, game_id) -> bool:  # type: ignore[no-untyped-def]
        if decision_id is None:
            return False
        d = self._match.get(decision_id)
        return (d is not None and d.entity_type == "kalshi_market"
                and d.source_provider == KALSHI_PUBLIC_PROVIDER and d.source_ref == kmk
                and d.outcome == "accepted" and d.matched_entity_id == game_id
                and not d.needs_manual_review)

    def _ambiguous(self, result, entity_type, entity_id, game_ids, tier):  # type: ignore[no-untyped-def]
        self._record(result, entity_type, entity_id, "ambiguous", tier, 0.0, None, game_ids,
                     reason="multiple canonical candidates", review=True)

    def _record(self, result, entity_type, source_ref, outcome, method, score, matched,  # type: ignore[no-untyped-def]
                candidate_games, *, reason=None, review=False):
        is_event = entity_type == "kalshi_event"
        c = result.counters
        result.counters.candidates_recorded += len(candidate_games)
        if outcome == "accepted":
            (setattr(c, "events_accepted", c.events_accepted + 1) if is_event
             else setattr(c, "markets_accepted", c.markets_accepted + 1))
        elif outcome == "ambiguous":
            (setattr(c, "events_ambiguous", c.events_ambiguous + 1) if is_event
             else setattr(c, "markets_ambiguous", c.markets_ambiguous + 1))
        elif outcome == "no_candidate":
            (setattr(c, "events_no_candidate", c.events_no_candidate + 1) if is_event
             else setattr(c, "markets_no_candidate", c.markets_no_candidate + 1))
        elif outcome == "rejected":
            (setattr(c, "events_rejected", c.events_rejected + 1) if is_event
             else setattr(c, "markets_rejected", c.markets_rejected + 1))
        if method == "unsupported_series":
            pass  # already counted in unsupported_series
        inputs = ([CandidateInput(score=score if outcome == "accepted" else 0.0, tier=method,
                                  candidate_entity_id=gid) for gid in candidate_games]
                  if candidate_games and not isinstance(candidate_games[0], CandidateInput)
                  else list(candidate_games))
        if self._dry_run:
            return None
        decision = self._match.record_decision(
            entity_type=entity_type, source_provider=KALSHI_PUBLIC_PROVIDER, source_ref=source_ref,
            outcome=outcome, method=method, score=score, threshold=_THRESHOLD,
            matcher_version=self._version, candidates=inputs, matched_entity_id=matched,
            rejection_reason=None if outcome == "accepted" else (reason or "unresolved"),
            needs_manual_review=review, run_id=self._run_id, raw_response_id=self._current_raw)
        result.counters.rows_persisted += 1
        return decision.match_id

    def _dq(self, result, severity, rule_code, entity_type, entity_id, event, description):  # type: ignore[no-untyped-def]
        existing = self._conn.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_type = ? "
            "AND entity_id IS ? AND provider IS ? AND description = ? AND resolved_at IS NULL "
            "LIMIT 1",
            (rule_code, entity_type, entity_id, KALSHI_PUBLIC_PROVIDER, description),
        ).fetchone()
        if existing is not None:
            return
        result.counters.dq_issues += 1
        if severity == "blocking":
            result.counters.blocking_issues += 1
        if self._dry_run:
            return
        raw = getattr(event, "current_raw_response_id", None) if event is not None else None
        self._dqrepo.record(
            severity=severity, rule_code=rule_code, entity_type=entity_type,
            description=description, entity_id=entity_id, provider=KALSHI_PUBLIC_PROVIDER,
            detail_json=json.dumps({"entity_type": entity_type, "entity_id": entity_id},
                                   sort_keys=True),
            run_id=self._run_id, raw_response_id=raw)
        result.counters.rows_persisted += 1
