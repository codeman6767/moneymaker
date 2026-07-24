"""MLB StatsAPI official-data ingestion (Phase D2).

Reads the MLB StatsAPI schedule (optionally with probable pitchers and posted
lineups) and, per game, optionally the box score (team + player statistics) and
line score (inning lines + final result). Everything is GET-only through the
shared D1 provider client; each raw response is preserved before it is
normalized; every derived row is append-only and traces to the *exact* raw
response that supplied it.

Official game identity is anchored on ``provider_game_references`` (one row per
MLB ``gamePk``); canonical ``games``/team/player resolution is a Phase D5 concern,
so snapshots carry provider ids with NULLABLE canonical ids. Missing values stay
missing (never coerced to zero); contradictions are recorded as
``data_quality_issues`` rather than silently repaired. ``--dry-run`` fetches and
normalizes in memory and persists absolutely nothing. No historical backfill is
performed and no live call is made in tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.game_statistics import (
    SqlitePlayerGameStatRepository,
    SqliteTeamGameStatRepository,
)
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.lineups import LineupPlayerInput, SqliteLineupRepository
from ..db.repositories.observations import ObservationOutcome
from ..db.repositories.official_games import (
    SqliteInningLineRepository,
    SqliteResultRepository,
    SqliteScheduleRepository,
)
from ..db.repositories.probables import SqliteProbablePitcherRepository
from ..db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from ..db.repositories.references import SqliteProviderReferenceRepository
from ..db.repositories.rosters import SqliteRosterRepository
from ..db.schema import to_iso
from ..providers.base_provider import ProviderError, ProviderResponse
from ..providers.capabilities import (
    MLB_STATSAPI_DECLARATION,
    PROVIDER_MLB_STATSAPI,
    ProviderCapability,
)
from ..providers.mlb_statsapi import MlbStatsApiClient
from ..providers.mlb_status import map_mlb_status
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-mlb"
_LINEUPS_COMMAND = "ingest-lineups"

#: The optional include groups the CLI understands.
VALID_INCLUDES: tuple[str, ...] = (
    "results", "box", "inning", "probables", "lineups", "rosters",
)


@dataclass
class MlbIngestResult:
    """Sanitized, deterministic counters for one MLB ingest, safe to print/JSON."""

    dry_run: bool
    status: str
    command: str = _COMMAND
    run_id: Optional[str] = None
    requests_made: int = 0
    raw_responses_received: int = 0
    games_received: int = 0
    games_inserted: int = 0
    games_unchanged: int = 0
    schedule_snapshots_inserted: int = 0
    schedule_snapshots_unchanged: int = 0
    result_snapshots_inserted: int = 0
    team_statistics_inserted: int = 0
    player_statistics_inserted: int = 0
    inning_lines_inserted: int = 0
    roster_requests: int = 0
    roster_players_received: int = 0
    roster_observations_inserted: int = 0
    roster_observations_unchanged: int = 0
    roster_records_rejected: int = 0
    probable_pitchers_inserted: int = 0
    lineups_inserted: int = 0
    lineup_players_inserted: int = 0
    provider_references_created: int = 0
    corrections_appended: int = 0
    records_rejected: int = 0
    #: A genuine provider/normalization failure on a *requested* endpoint (network
    #: after retries, 5xx, oversized, malformed JSON, parser, unexpected). Drives
    #: the ``partially_failed`` status and a non-zero CLI exit. Distinct from
    #: ``records_rejected`` (a data-quality rejection in an otherwise-valid
    #: response, which is honest and exit-0).
    active_failures: int = 0
    data_quality_issues: int = 0
    capabilities_unavailable: int = 0
    rejections: list[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def has_active_failure(self) -> bool:
        return self.active_failures > 0

    @property
    def needs_failure_exit(self) -> bool:
        """A failed OR partially-failed run must exit non-zero."""

        return self.status in ("failed", "partially_failed")

    def note(self, reason: str) -> None:
        if len(self.rejections) < 50:
            self.rejections.append(reason)

    def record_active_failure(self, error_type: str, message: str) -> None:
        self.active_failures += 1
        if self.error_message is None:
            self.error_type, self.error_message = error_type, message


# --------------------------------------------------------------------------- #
# Parsing helpers (pure; operate on already-sanitized parsed JSON)
# --------------------------------------------------------------------------- #
def _opt_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _provider_id(obj: Any, key: str = "id") -> Optional[str]:
    if isinstance(obj, dict) and obj.get(key) is not None:
        return str(obj[key])
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a dict, else an empty dict (mypy-narrowing)."""

    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class _NormGame:
    """One normalized schedule game (provider identity + schedule fields)."""

    game_pk: str
    season: Optional[int]
    game_type: Optional[str]
    game_date_local: Optional[str]
    scheduled_start: Optional[str]
    home_provider_team_id: Optional[str]
    away_provider_team_id: Optional[str]
    venue_provider_id: Optional[str]
    status_code: Optional[str]
    detailed_status: Optional[str]
    mapped_status: str
    status_unknown: bool
    game_number: Optional[int]
    doubleheader_code: Optional[str]
    reschedule_info: Optional[str]
    home_probable_pitcher_id: Optional[str]
    away_probable_pitcher_id: Optional[str]
    raw_game: dict[str, Any]


def _normalize_schedule_game(game: dict[str, Any]) -> tuple[Optional[_NormGame], Optional[str]]:
    """Normalize one schedule ``games[]`` entry, or return a rejection reason."""

    game_pk = _provider_id(game, "gamePk")
    if game_pk is None:
        return None, "schedule game missing gamePk"
    status = _as_dict(game.get("status"))
    mapped = map_mlb_status(status)
    teams = _as_dict(game.get("teams"))
    home = _as_dict(teams.get("home"))
    away = _as_dict(teams.get("away"))
    reschedule = {
        k: game[k]
        for k in ("rescheduledFrom", "rescheduledTo", "resumeDate", "resumedFrom")
        if game.get(k)
    }
    return (
        _NormGame(
            game_pk=game_pk,
            season=_opt_int(game.get("season")),
            game_type=_opt_str(game.get("gameType")),
            game_date_local=_opt_str(game.get("officialDate")),
            scheduled_start=_opt_str(game.get("gameDate")),
            home_provider_team_id=_provider_id(home.get("team")),
            away_provider_team_id=_provider_id(away.get("team")),
            venue_provider_id=_provider_id(game.get("venue")),
            status_code=_opt_str(status.get("codedGameState")),
            detailed_status=mapped.detailed_state,
            mapped_status=mapped.canonical,
            status_unknown=mapped.is_unknown,
            game_number=_opt_int(game.get("gameNumber")),
            doubleheader_code=_opt_str(game.get("doubleHeader")),
            reschedule_info=canonical_json(reschedule) if reschedule else None,
            home_probable_pitcher_id=_provider_id(home.get("probablePitcher")),
            away_probable_pitcher_id=_provider_id(away.get("probablePitcher")),
            raw_game=game,
        ),
        None,
    )


def _schedule_games(data: Any) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return games
    for date_block in data.get("dates", []) or []:
        if isinstance(date_block, dict):
            for game in date_block.get("games", []) or []:
                if isinstance(game, dict):
                    games.append(game)
    return games


# --------------------------------------------------------------------------- #
# Pure parsers / validators (no I/O; shared by dry-run counting and persistence)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _DqIssue:
    severity: str
    rule_code: str
    description: str


@dataclass(frozen=True)
class _InningRow:
    inning: int
    side: str
    runs: Optional[int]
    hits: Optional[int]
    errors: Optional[int]


@dataclass(frozen=True)
class _InningsParse:
    #: Only sides the provider actually supplied become rows -- a missing half
    #: (e.g. the home team not batting in the bottom of the ninth) is neither a
    #: row nor a fabricated zero.
    rows: list[_InningRow]
    issues: list[_DqIssue]
    rejected: int
    #: A *trustworthy* per-team run sum (every inning 1..max supplied that side
    #: with a numeric run value), else ``None`` so reconciliation stays silent
    #: rather than false-positive on a legitimately-missing half.
    home_sum: Optional[int]
    away_sum: Optional[int]


def _parse_innings(data: dict[str, Any], game_pk: str) -> _InningsParse:
    rows: list[_InningRow] = []
    issues: list[_DqIssue] = []
    rejected = 0
    seen: set[tuple[int, str]] = set()
    home_runs_map: dict[int, Optional[int]] = {}
    away_runs_map: dict[int, Optional[int]] = {}
    max_inning = 0
    innings = data.get("innings")
    for inning in innings if isinstance(innings, list) else []:
        if not isinstance(inning, dict):
            continue
        num = _opt_int(inning.get("num"))
        if num is None or num < 1:
            rejected += 1
            issues.append(_DqIssue("issue", "DQ-MLB-INNING-001",
                                   f"malformed inning number {inning.get('num')!r} for {game_pk}"))
            continue
        max_inning = max(max_inning, num)
        for side in ("home", "away"):
            side_obj = inning.get(side)
            if not isinstance(side_obj, dict):
                continue  # half not supplied / not played: never a fabricated row
            if (num, side) in seen:
                issues.append(_DqIssue("issue", "DQ-MLB-INNING-002",
                                       f"duplicate inning identity {num}/{side} for {game_pk}"))
                continue
            seen.add((num, side))
            runs = _opt_int(side_obj.get("runs"))
            rows.append(_InningRow(num, side, runs, _opt_int(side_obj.get("hits")),
                                   _opt_int(side_obj.get("errors"))))
            (home_runs_map if side == "home" else away_runs_map)[num] = runs

    def _trustworthy_sum(runs_map: dict[int, Optional[int]]) -> Optional[int]:
        if max_inning == 0:
            return None
        total = 0
        for n in range(1, max_inning + 1):
            value = runs_map.get(n, "gap")
            if value == "gap" or value is None:
                return None  # a missing/None half -> the sum is not trustworthy
            total += int(value)  # type: ignore[arg-type]
        return total

    return _InningsParse(rows, issues, rejected, _trustworthy_sum(home_runs_map),
                         _trustworthy_sum(away_runs_map))


@dataclass(frozen=True)
class _ResultParse:
    home_runs: Optional[int]
    away_runs: Optional[int]
    home_hits: Optional[int]
    away_hits: Optional[int]
    home_errors: Optional[int]
    away_errors: Optional[int]
    innings_played: Optional[int]
    winning_side: Optional[str]


def _parse_result(data: dict[str, Any]) -> _ResultParse:
    teams = _as_dict(data.get("teams"))
    home = _as_dict(teams.get("home"))
    away = _as_dict(teams.get("away"))
    home_runs = _opt_int(home.get("runs"))
    away_runs = _opt_int(away.get("runs"))
    winning: Optional[str] = None
    if home_runs is not None and away_runs is not None:
        winning = "home" if home_runs > away_runs else "away" if away_runs > home_runs else "tie"
    return _ResultParse(
        home_runs=home_runs, away_runs=away_runs, home_hits=_opt_int(home.get("hits")),
        away_hits=_opt_int(away.get("hits")), home_errors=_opt_int(home.get("errors")),
        away_errors=_opt_int(away.get("errors")), innings_played=_opt_int(data.get("currentInning")),
        winning_side=winning,
    )


def _result_issues(
    norm: "_NormGame", result: _ResultParse, innings: Optional[_InningsParse]
) -> list[_DqIssue]:
    """Validate a result: negative runs, final-with-no-data, and (when inning
    data is available) score-vs-inning-sum reconciliation. Distinct rule codes
    keep contradiction / incomplete / malformed / not-played separable."""

    issues: list[_DqIssue] = []
    for label, runs in (("home", result.home_runs), ("away", result.away_runs)):
        if runs is not None and runs < 0:
            issues.append(_DqIssue("issue", "DQ-MLB-RESULT-001",
                                   f"negative {label} runs ({runs}) for gamePk {norm.game_pk}"))
    if norm.mapped_status == "final" and result.home_runs is None and result.away_runs is None:
        issues.append(_DqIssue("issue", "DQ-MLB-RESULT-002",
                               f"final gamePk {norm.game_pk} has no usable result data"))
    if innings is not None:
        # Contradiction: a trustworthy inning sum disagrees with the team total.
        for label, total, isum in (("home", result.home_runs, innings.home_sum),
                                    ("away", result.away_runs, innings.away_sum)):
            if total is not None and isum is not None and total != isum:
                issues.append(_DqIssue("issue", "DQ-MLB-RECON-001",
                    f"{label} total {total} conflicts with inning-run sum {isum} "
                    f"for gamePk {norm.game_pk}"))
        # Incomplete: a final game supplies a total but no usable inning lines to
        # reconcile against (distinct from a plain not-played half).
        if (norm.mapped_status == "final" and not innings.rows
                and (result.home_runs is not None or result.away_runs is not None)):
            issues.append(_DqIssue("note", "DQ-MLB-RECON-002",
                f"final gamePk {norm.game_pk} supplies a total but no usable inning lines"))
    return issues


# --------------------------------------------------------------------------- #
# Ingestor
# --------------------------------------------------------------------------- #
def _requested_capabilities_available(includes: set[str], result: MlbIngestResult) -> set[str]:
    """Drop include groups the capability declaration marks unavailable.

    Consults the static MLB declaration; an unavailable/unsupported group is
    skipped and counted, never requested.
    """

    gate = {
        "results": ProviderCapability.GAME_RESULTS,
        "box": ProviderCapability.TEAM_STATISTICS,
        "inning": ProviderCapability.INNING_LINES,
        "probables": ProviderCapability.PROBABLE_PITCHERS,
        "lineups": ProviderCapability.LINEUPS,
        "rosters": ProviderCapability.PLAYERS,
    }
    available: set[str] = set()
    for name in includes:
        cap = gate.get(name)
        if cap is not None and not MLB_STATSAPI_DECLARATION.is_available(cap):
            result.capabilities_unavailable += 1
            result.note(f"capability for {name!r} is not available at this provider tier")
            continue
        available.add(name)
    return available


async def ingest_lineups(
    *,
    database: Database,
    client: MlbStatsApiClient,
    date: Optional[str] = None,
    game_pk: Optional[int] = None,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> MlbIngestResult:
    """Ingest posted MLB lineups for a date or a game. ``--dry-run`` persists nothing.

    Writes the schedule observation that anchors each game plus its posted
    lineups; nothing here confirms pregame starters (MLB StatsAPI supplies no such
    confirmation), so lineups are recorded ``is_confirmed = 0``.
    """

    return await ingest_mlb(
        database=database, client=client, from_date=date, game_pk=game_pk,
        includes=("lineups",), dry_run=dry_run, tool_version=tool_version,
        command=_LINEUPS_COMMAND,
    )


async def ingest_mlb(
    *,
    database: Database,
    client: MlbStatsApiClient,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    game_pk: Optional[int] = None,
    includes: tuple[str, ...] = (),
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
    command: str = _COMMAND,
) -> MlbIngestResult:
    """Ingest MLB schedule (+ optional per-game data). ``--dry-run`` persists nothing."""

    result = MlbIngestResult(dry_run=dry_run, status="succeeded", command=command)
    include_set = _requested_capabilities_available(set(includes), result)

    hydrate_groups = ["probablePitcher"]
    if "lineups" in include_set:
        hydrate_groups.append("lineups")
    hydrate = ",".join(hydrate_groups)

    try:
        schedule = await _fetch_schedule(
            client, from_date=from_date, to_date=to_date, game_pk=game_pk, hydrate=hydrate
        )
    except ProviderError as exc:
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result
    result.requests_made += 1

    games = _schedule_games(schedule.data)
    result.games_received = len(games)

    # Per-game sub-fetches happen up front (GET-only, sequential -- no fan-out),
    # so a dry run performs the same reads but persists nothing.
    per_game: dict[str, dict[str, Optional[ProviderResponse]]] = {}
    for game in games:
        game_pk_str = _provider_id(game, "gamePk")
        if game_pk_str is None:
            continue
        fetched: dict[str, Optional[ProviderResponse]] = {}
        if include_set & {"box"}:
            fetched["box"] = await _try_fetch(client.fetch_boxscore, (game_pk_str,), {},
                                              f"box gamePk {game_pk_str}", result)
        if include_set & {"results", "inning"}:
            fetched["line"] = await _try_fetch(client.fetch_linescore, (game_pk_str,), {},
                                               f"linescore gamePk {game_pk_str}", result)
        per_game[game_pk_str] = fetched

    # Rosters are team-scoped: fetch each unique provider team ONCE per run
    # (deduplicated across a doubleheader that reuses a team).
    rosters_by_team: dict[str, Optional[ProviderResponse]] = {}
    if "rosters" in include_set:
        for team_id in _unique_team_ids(games):
            result.roster_requests += 1
            rosters_by_team[team_id] = await _try_fetch(
                client.fetch_roster, (team_id,), {"date": from_date},
                f"roster team {team_id}", result,
            )

    if dry_run:
        # Normalize in memory only: count what WOULD be written, persist nothing.
        _dry_run_count(games, per_game, rosters_by_team, include_set, result)
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        return result

    return await _persist(
        database, schedule, games, per_game, rosters_by_team, include_set, result, tool_version,
        roster_date=from_date,
    )


def _unique_team_ids(games: list[dict[str, Any]]) -> list[str]:
    """Distinct provider team ids across the games, in first-seen order."""

    seen: list[str] = []
    for game in games:
        teams = _as_dict(_as_dict(game).get("teams"))
        for side in ("home", "away"):
            tid = _provider_id(_as_dict(teams.get(side)).get("team"))
            if tid and tid not in seen:
                seen.append(tid)
    return seen


async def _fetch_schedule(
    client: MlbStatsApiClient,
    *,
    from_date: Optional[str],
    to_date: Optional[str],
    game_pk: Optional[int],
    hydrate: str,
) -> ProviderResponse:
    if game_pk is not None:
        return await client.fetch_schedule(game_pk=game_pk, hydrate=hydrate)
    if from_date is not None and to_date is not None:
        return await client.fetch_schedule(start_date=from_date, end_date=to_date, hydrate=hydrate)
    if from_date is not None:
        return await client.fetch_schedule(date=from_date, hydrate=hydrate)
    return await client.fetch_schedule(hydrate=hydrate)


async def _try_fetch(fetch, args, kwargs, label: str, result: MlbIngestResult):  # noqa: ANN001
    """Fetch one requested sub-resource; on failure record an **active failure**.

    A requested endpoint that fails (network after retries, 5xx, oversized,
    malformed content type, parser) is a genuine provider failure -- it is
    counted as an active failure (driving ``partially_failed`` + a non-zero CLI
    exit), never a harmless rejection. The rest of the ingest continues.
    """

    try:
        response = await fetch(*args, **{k: v for k, v in kwargs.items() if v is not None})
    except ProviderError as exc:
        error_type, msg = sanitize_error(exc)
        result.record_active_failure(error_type, f"{label}: {msg}")
        result.note(f"{label}: sub-fetch failed ({msg})")
        return None
    except Exception as exc:  # noqa: BLE001
        error_type, msg = sanitize_error(exc)
        result.record_active_failure(error_type, f"{label}: {msg}")
        result.note(f"{label}: sub-fetch error ({msg})")
        return None
    result.requests_made += 1
    return response


@dataclass(frozen=True)
class _RosterPlayer:
    provider_player_id: str
    roster_status: Optional[str]
    jersey_number: Optional[str]
    position: Optional[str]


def _parse_roster(data: Any) -> tuple[list[_RosterPlayer], int]:
    """Parse a ``/roster`` response into players; ``rejected`` counts rows whose
    player id is missing (never fabricated into an identity)."""

    players: list[_RosterPlayer] = []
    rejected = 0
    roster = data.get("roster") if isinstance(data, dict) else None
    for row in roster if isinstance(roster, list) else []:
        if not isinstance(row, dict):
            continue
        pid = _provider_id(row.get("person"))
        if pid is None:
            rejected += 1
            continue
        status = _as_dict(row.get("status"))
        pos = _as_dict(row.get("position"))
        players.append(_RosterPlayer(
            provider_player_id=pid,
            roster_status=_opt_str(status.get("description") or status.get("code")),
            jersey_number=_opt_str(row.get("jerseyNumber")),
            position=_opt_str(pos.get("abbreviation")),
        ))
    return players, rejected


def _dry_run_count(
    games: list[dict[str, Any]],
    per_game: dict[str, dict[str, Optional[ProviderResponse]]],
    rosters_by_team: dict[str, Optional[ProviderResponse]],
    include_set: set[str],
    result: MlbIngestResult,
) -> None:
    """Run the same parse + validation logic in memory and report accurate
    would-be counts. Persists nothing. With no prior DB state every parsed
    observation is a would-be insert (a fresh ingest), so counters are truthful
    and never left at zero for successfully-parsed responses."""

    refs: set[tuple[str, str]] = set()  # (kind, id) distinct would-be references

    def ref(kind: str, entity_id: str) -> None:
        if (kind, entity_id) not in refs:
            refs.add((kind, entity_id))
            result.provider_references_created += 1

    for game in games:
        norm, reason = _normalize_schedule_game(game)
        if norm is None:
            result.records_rejected += 1
            result.note(reason or "invalid game")
            continue
        result.games_inserted += 1
        ref("game", norm.game_pk)
        for tid in (norm.home_provider_team_id, norm.away_provider_team_id):
            if tid:
                ref("team", tid)
        result.schedule_snapshots_inserted += 1
        if norm.status_unknown:
            result.data_quality_issues += 1
        if "probables" in include_set:
            for pid in (norm.home_probable_pitcher_id, norm.away_probable_pitcher_id):
                if pid:
                    result.probable_pitchers_inserted += 1
                    ref("player", pid)
        if "lineups" in include_set:
            for _side, players in _parse_schedule_lineups(norm.raw_game):
                if players:
                    result.lineups_inserted += 1
                    result.lineup_players_inserted += len(players)
                    for p in players:
                        ref("player", p.provider_player_id)

        game_responses = per_game.get(norm.game_pk, {})
        line_resp = game_responses.get("line")
        if line_resp is not None and isinstance(line_resp.data, dict) and (
                include_set & {"results", "inning"}):
            innings = _parse_innings(line_resp.data, norm.game_pk) if "inning" in include_set else None
            if innings is not None:
                result.inning_lines_inserted += len(innings.rows)
                result.records_rejected += innings.rejected
                result.data_quality_issues += len(innings.issues)
            if "results" in include_set:
                parsed = _parse_result(line_resp.data)
                result.result_snapshots_inserted += 1
                result.data_quality_issues += len(_result_issues(norm, parsed, innings))
        box_resp = game_responses.get("box")
        if box_resp is not None and isinstance(box_resp.data, dict) and "box" in include_set:
            for side in ("home", "away"):
                block = _as_dict(_as_dict(box_resp.data.get("teams")).get(side))
                if not _provider_id(block.get("team")):
                    continue
                result.team_statistics_inserted += 1
                for person in _as_dict(block.get("players")).values():
                    if not isinstance(person, dict):
                        continue
                    pid = _provider_id(person.get("person"))
                    if pid is None:
                        continue
                    ref("player", pid)
                    stats = _as_dict(person.get("stats"))
                    for key in ("batting", "pitching"):
                        if isinstance(stats.get(key), dict) and stats.get(key):
                            result.player_statistics_inserted += 1

    if "rosters" in include_set:
        for team_id, roster_resp in rosters_by_team.items():
            if roster_resp is None:
                continue  # a failed roster fetch is already an active failure
            roster_players, rejected = _parse_roster(roster_resp.data)
            ref("team", team_id)
            result.roster_players_received += len(roster_players)
            result.roster_records_rejected += rejected
            for rp in roster_players:
                result.roster_observations_inserted += 1
                ref("player", rp.provider_player_id)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
async def _persist(
    database: Database,
    schedule: ProviderResponse,
    games: list[dict[str, Any]],
    per_game: dict[str, dict[str, Optional[ProviderResponse]]],
    rosters_by_team: dict[str, Optional[ProviderResponse]],
    include_set: set[str],
    result: MlbIngestResult,
    tool_version: str,
    roster_date: Optional[str] = None,
) -> MlbIngestResult:
    started = time.monotonic_ns()
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=result.command, provider=PROVIDER_MLB_STATSAPI, operation="ingest_mlb",
                args_json=canonical_json({"includes": sorted(include_set)}),
                started_monotonic_ns=started, tool_version=tool_version,
            )
        result.run_id = run.run_id
        raw_repo = SqliteRawResponseRepository(conn)

        sched_raw = _store_raw(conn, raw_repo, run.run_id, schedule)
        result.raw_responses_received += 1

        # Store each per-game raw ONCE and remember (raw_id, hash) per endpoint.
        game_raws: dict[str, dict[str, tuple[str, str, str]]] = {}
        for pk, fetched in per_game.items():
            game_raws[pk] = {}
            for kind, resp in fetched.items():
                if resp is None:
                    continue
                game_raws[pk][kind] = _store_raw(conn, raw_repo, run.run_id, resp)
                result.raw_responses_received += 1

        # Roster raws (team-scoped), stored once each.
        roster_raws: dict[str, tuple[str, str, str]] = {}
        for team_id, roster_resp in rosters_by_team.items():
            if roster_resp is not None:
                roster_raws[team_id] = _store_raw(conn, raw_repo, run.run_id, roster_resp)
                result.raw_responses_received += 1

        refs = SqliteProviderReferenceRepository(conn)
        dq = SqliteDataQualityRepository(conn)
        ctx = _PersistCtx(conn, run.run_id, refs, dq, result, roster_date=roster_date)

        for game in games:
            norm, reason = _normalize_schedule_game(game)
            if norm is None:
                result.records_rejected += 1
                result.note(reason or "invalid game")
                continue
            try:
                with transaction(conn):
                    _persist_one_game(
                        ctx, norm, sched_raw, game_raws.get(norm.game_pk, {}),
                        per_game.get(norm.game_pk, {}), include_set,
                    )
            except Exception as exc:  # noqa: BLE001 - one bad game must not corrupt the rest
                _t, msg = sanitize_error(exc)
                # An unexpected normalization failure on a valid response is an
                # active failure, not a harmless rejection.
                result.record_active_failure(_t, f"game {norm.game_pk}: {msg}")
                result.note(f"game {norm.game_pk}: normalization failed ({msg})")

        # Roster phase (team-scoped; each unique team's roster persisted once).
        for team_id, roster_resp in rosters_by_team.items():
            raw = roster_raws.get(team_id)
            if roster_resp is None or raw is None:
                continue  # a failed roster fetch never fabricates an empty roster
            try:
                with transaction(conn):
                    _persist_roster(ctx, team_id, roster_resp.data, raw)
            except Exception as exc:  # noqa: BLE001
                _t, msg = sanitize_error(exc)
                result.record_active_failure(_t, f"roster {team_id}: {msg}")
                result.note(f"roster {team_id}: normalization failed ({msg})")

        # Truthful status: an active failure -> partially_failed (a genuine
        # provider/normalization failure occurred even if data persisted).
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        run_status = "partially_succeeded" if result.status == "partially_failed" else "succeeded"
        with transaction(conn):
            runs.complete(
                run.run_id, status=run_status, duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made,
                records_received=result.games_received,
                records_normalized=result.schedule_snapshots_inserted,
                records_inserted=result.schedule_snapshots_inserted,
                records_deduplicated=result.schedule_snapshots_unchanged,
                records_rejected=result.records_rejected,
            )
    return result


@dataclass
class _PersistCtx:
    conn: Any
    run_id: str
    refs: SqliteProviderReferenceRepository
    dq: SqliteDataQualityRepository
    result: MlbIngestResult
    roster_date: Optional[str] = None


def _game_ref(ctx: _PersistCtx, norm: _NormGame, raw: tuple[str, str, str]) -> str:
    raw_id, raw_hash, received_at = raw
    ref, outcome = ctx.refs.upsert(
        kind="game", provider=PROVIDER_MLB_STATSAPI, provider_entity_id=norm.game_pk,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=received_at,
    )
    if outcome.value == "inserted":
        ctx.result.games_inserted += 1
        ctx.result.provider_references_created += 1
    else:
        ctx.result.games_unchanged += 1
    return ref.reference_id


def _team_ref(ctx: _PersistCtx, provider_team_id: str, raw: tuple[str, str, str]) -> str:
    raw_id, raw_hash, received_at = raw
    ref, outcome = ctx.refs.upsert(
        kind="team", provider=PROVIDER_MLB_STATSAPI, provider_entity_id=provider_team_id,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=received_at,
    )
    if outcome.value == "inserted":
        ctx.result.provider_references_created += 1
    return ref.reference_id


def _player_ref(ctx: _PersistCtx, provider_player_id: str, raw: tuple[str, str, str]) -> str:
    """Create/reuse a provider_player_references row from the EXACT raw response
    that supplied this player id (box / probable / lineup / roster). Never a
    canonical player; never a name match. One provider id cannot silently move
    between canonical players (enforced by the reference's identity guard)."""

    raw_id, raw_hash, received_at = raw
    ref, outcome = ctx.refs.upsert(
        kind="player", provider=PROVIDER_MLB_STATSAPI, provider_entity_id=provider_player_id,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=received_at,
    )
    if outcome.value == "inserted":
        ctx.result.provider_references_created += 1
    return ref.reference_id


def _persist_one_game(
    ctx: _PersistCtx,
    norm: _NormGame,
    sched_raw: tuple[str, str, str],
    game_raws: dict[str, tuple[str, str, str]],
    game_responses: dict[str, Optional[ProviderResponse]],
    include_set: set[str],
) -> None:
    conn = ctx.conn
    res = ctx.result
    sched_raw_id, sched_raw_hash, sched_observed = sched_raw
    ingested = to_iso(_now())

    game_ref_id = _game_ref(ctx, norm, sched_raw)
    for provider_team_id in (norm.home_provider_team_id, norm.away_provider_team_id):
        if provider_team_id:
            _team_ref(ctx, provider_team_id, sched_raw)

    if norm.status_unknown:
        ctx.dq.record(
            severity="issue", rule_code="DQ-MLB-STATUS-001", entity_type="game",
            description=(
                f"unknown MLB status for gamePk {norm.game_pk}: "
                f"detailed={norm.detailed_status!r} code={norm.status_code!r}"
            ),
            provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id, raw_response_id=sched_raw_id,
            entity_id=norm.game_pk,
        )
        res.data_quality_issues += 1

    # -- Schedule snapshot (from the schedule response) ----------------------
    schedule_repo = SqliteScheduleRepository(conn)
    _sid, sched_outcome = schedule_repo.append(
        game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI, provider_game_id=norm.game_pk,
        observed_at=sched_observed, ingested_at=ingested, run_id=ctx.run_id,
        raw_response_id=sched_raw_id, raw_response_hash=sched_raw_hash,
        mapped_status=norm.mapped_status, season=norm.season, game_type=norm.game_type,
        game_date_local=norm.game_date_local, scheduled_start=norm.scheduled_start,
        home_provider_team_id=norm.home_provider_team_id,
        away_provider_team_id=norm.away_provider_team_id,
        venue_provider_id=norm.venue_provider_id, status_code=norm.status_code,
        detailed_status=norm.detailed_status, game_number=norm.game_number,
        doubleheader_code=norm.doubleheader_code, reschedule_info=norm.reschedule_info,
        home_probable_pitcher_id=norm.home_probable_pitcher_id,
        away_probable_pitcher_id=norm.away_probable_pitcher_id,
    )
    if sched_outcome is ObservationOutcome.INSERTED:
        res.schedule_snapshots_inserted += 1
    else:
        res.schedule_snapshots_unchanged += 1

    # -- Probable pitchers (from the schedule response) ----------------------
    if "probables" in include_set:
        probable_repo = SqliteProbablePitcherRepository(conn)
        for side, pid in (("home", norm.home_probable_pitcher_id),
                          ("away", norm.away_probable_pitcher_id)):
            if not pid:
                continue  # missing probable stays unknown; never fabricated
            _player_ref(ctx, pid, sched_raw)  # provenance = the schedule response
            _pp, outcome = probable_repo.append(
                game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
                provider_game_id=norm.game_pk, side=side, provider_player_id=pid,
                observed_at=sched_observed, ingested_at=ingested, run_id=ctx.run_id,
                raw_response_id=sched_raw_id, raw_response_hash=sched_raw_hash,
                status="probable",
            )
            if outcome is ObservationOutcome.INSERTED:
                res.probable_pitchers_inserted += 1

    # -- Lineups (from the schedule 'lineups' hydrate) -----------------------
    if "lineups" in include_set:
        _persist_lineups(ctx, norm, game_ref_id, sched_raw, ingested)

    # -- Line score: result + inning lines -----------------------------------
    line = game_raws.get("line")
    line_response = game_responses.get("line")
    if line is not None and line_response is not None and (include_set & {"results", "inning"}):
        _persist_linescore(ctx, norm, game_ref_id, line, ingested, include_set,
                           per_game_line_response=line_response.data)

    # -- Box score: team + player statistics ---------------------------------
    box = game_raws.get("box")
    box_response = game_responses.get("box")
    if box is not None and box_response is not None and "box" in include_set:
        _persist_boxscore(ctx, norm, game_ref_id, box, ingested,
                         per_game_box_response=box_response.data)


def _parse_schedule_lineups(game: dict[str, Any]) -> list[tuple[str, list[LineupPlayerInput]]]:
    """Extract posted lineups (home/away) from a schedule game's ``lineups`` hydrate."""

    lineups = _as_dict(game.get("lineups"))
    out: list[tuple[str, list[LineupPlayerInput]]] = []
    for side, key in (("home", "homePlayers"), ("away", "awayPlayers")):
        players_raw = lineups.get(key)
        if not isinstance(players_raw, list) or not players_raw:
            continue
        players: list[LineupPlayerInput] = []
        for order, person in enumerate(players_raw, start=1):
            if not isinstance(person, dict):
                continue
            pid = _provider_id(person)
            if pid is None:
                continue
            position = None
            pos = person.get("primaryPosition")
            if isinstance(pos, dict):
                position = _opt_str(pos.get("abbreviation"))
            players.append(
                LineupPlayerInput(batting_order=order, provider_player_id=pid, position=position)
            )
        if players:
            out.append((side, players))
    return out


def _persist_lineups(
    ctx: _PersistCtx, norm: _NormGame, game_ref_id: str,
    sched_raw: tuple[str, str, str], ingested: str,
) -> None:
    raw_id, raw_hash, observed = sched_raw
    lineup_repo = SqliteLineupRepository(ctx.conn)
    teams = {"home": norm.home_provider_team_id, "away": norm.away_provider_team_id}
    for side, players in _parse_schedule_lineups(norm.raw_game):
        provider_team_id = teams.get(side)
        if not provider_team_id:
            continue
        for p in players:  # provenance = the schedule (lineups hydrate) response
            _player_ref(ctx, p.provider_player_id, sched_raw)
        # A posted lineup is NOT a confirmed pregame starter set unless the
        # provider says so; MLB StatsAPI supplies no such confirmation here.
        _lid, outcome, n_players = lineup_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
            provider_game_id=norm.game_pk, provider_team_id=provider_team_id, players=players,
            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
            raw_response_id=raw_id, raw_response_hash=raw_hash, home_away=side,
            is_confirmed=False,
        )
        if outcome is ObservationOutcome.INSERTED:
            ctx.result.lineups_inserted += 1
            ctx.result.lineup_players_inserted += n_players


def _record_dq(ctx: _PersistCtx, norm: _NormGame, raw_id: str, issues: list[_DqIssue]) -> None:
    for issue in issues:
        ctx.dq.record(
            severity=issue.severity, rule_code=issue.rule_code, entity_type="game",
            description=issue.description, provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id,
            raw_response_id=raw_id, entity_id=norm.game_pk,
        )
        ctx.result.data_quality_issues += 1


def _persist_linescore(
    ctx: _PersistCtx, norm: _NormGame, game_ref_id: str,
    line_raw: tuple[str, str, str], ingested: str, include_set: set[str],
    per_game_line_response: Any,
) -> None:
    data = per_game_line_response
    if not isinstance(data, dict):
        return
    raw_id, raw_hash, observed = line_raw
    res = ctx.result

    # Parse innings once (only supplied halves become rows; missing halves are
    # neither rows nor fabricated zeros).
    innings = _parse_innings(data, norm.game_pk) if "inning" in include_set else None

    if "inning" in include_set and innings is not None:
        res.records_rejected += innings.rejected
        _record_dq(ctx, norm, raw_id, innings.issues)
        inning_repo = SqliteInningLineRepository(ctx.conn)
        for row in innings.rows:
            _lid, outcome = inning_repo.append(
                game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
                provider_game_id=norm.game_pk, inning=row.inning, side=row.side,
                observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
                raw_response_id=raw_id, raw_response_hash=raw_hash, runs=row.runs,
                hits=row.hits, errors=row.errors,
            )
            if outcome is ObservationOutcome.INSERTED:
                res.inning_lines_inserted += 1

    if "results" in include_set:
        parsed = _parse_result(data)
        _record_dq(ctx, norm, raw_id, _result_issues(norm, parsed, innings))
        result_repo = SqliteResultRepository(ctx.conn)
        _rid, outcome, is_correction = result_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
            provider_game_id=norm.game_pk, observed_at=observed, ingested_at=ingested,
            run_id=ctx.run_id, raw_response_id=raw_id, raw_response_hash=raw_hash,
            mapped_status=norm.mapped_status, home_runs=parsed.home_runs,
            away_runs=parsed.away_runs, home_hits=parsed.home_hits, away_hits=parsed.away_hits,
            home_errors=parsed.home_errors, away_errors=parsed.away_errors,
            innings_played=parsed.innings_played, winning_side=parsed.winning_side,
        )
        if outcome is ObservationOutcome.INSERTED:
            res.result_snapshots_inserted += 1
            if is_correction:
                res.corrections_appended += 1


def _persist_boxscore(
    ctx: _PersistCtx, norm: _NormGame, game_ref_id: str, box_raw: tuple[str, str, str],
    ingested: str, per_game_box_response: Any,
) -> None:
    data = per_game_box_response
    if not isinstance(data, dict):
        return
    raw_id, raw_hash, observed = box_raw
    res = ctx.result
    teams = _as_dict(data.get("teams"))
    team_repo = SqliteTeamGameStatRepository(ctx.conn)
    player_repo = SqlitePlayerGameStatRepository(ctx.conn)
    for side in ("home", "away"):
        block = _as_dict(teams.get(side))
        provider_team_id = _provider_id(block.get("team"))
        if not provider_team_id:
            continue
        team_stats = _as_dict(block.get("teamStats"))
        batting = _as_dict(team_stats.get("batting"))
        fielding = _as_dict(team_stats.get("fielding"))
        _tid, outcome = team_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
            provider_game_id=norm.game_pk, provider_team_id=provider_team_id, home_away=side,
            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
            raw_response_id=raw_id, raw_response_hash=raw_hash,
            runs=_opt_int(batting.get("runs")), hits=_opt_int(batting.get("hits")),
            errors=_opt_int(fielding.get("errors")), at_bats=_opt_int(batting.get("atBats")),
            extra=canonical_json({"batting": batting, "fielding": fielding}),
        )
        if outcome is ObservationOutcome.INSERTED:
            res.team_statistics_inserted += 1

        players = _as_dict(block.get("players"))
        for person in players.values():
            if not isinstance(person, dict):
                continue
            pid = _provider_id(person.get("person"))
            if pid is None:
                continue
            _player_ref(ctx, pid, box_raw)  # provenance = the box-score response
            stats = _as_dict(person.get("stats"))
            bstats = stats.get("batting") if isinstance(stats.get("batting"), dict) else None
            pstats = stats.get("pitching") if isinstance(stats.get("pitching"), dict) else None
            pos = _as_dict(person.get("position"))
            position = _opt_str(pos.get("abbreviation"))
            order = _opt_int(person.get("battingOrder"))
            batting_order = (order // 100) if (order is not None and order >= 100) else None
            for role, block_stats in (("batting", bstats), ("pitching", pstats)):
                if not block_stats:
                    continue
                _pid, p_out = player_repo.append(
                    game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
                    provider_game_id=norm.game_pk, provider_player_id=pid, role=role,
                    observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
                    raw_response_id=raw_id, raw_response_hash=raw_hash,
                    provider_team_id=provider_team_id, position=position,
                    batting_order=batting_order if role == "batting" else None,
                    batting_stats=canonical_json(bstats) if role == "batting" and bstats else None,
                    pitching_stats=canonical_json(pstats) if role == "pitching" and pstats else None,
                )
                if p_out is ObservationOutcome.INSERTED:
                    res.player_statistics_inserted += 1


def _persist_roster(
    ctx: _PersistCtx, provider_team_id: str, roster_data: Any, roster_raw: tuple[str, str, str],
) -> None:
    """Persist one team's roster observations from its own raw response.

    Creates the team reference and a provider player reference per valid roster
    player (all with the roster response's own provenance), then appends
    transition-aware roster observations. A missing player id is rejected (never
    fabricated); canonical player ids stay NULL for D5.
    """

    raw_id, raw_hash, observed = roster_raw
    players, rejected = _parse_roster(roster_data)
    ctx.result.roster_players_received += len(players)
    ctx.result.roster_records_rejected += rejected

    team_ref_id = _team_ref(ctx, provider_team_id, roster_raw)
    roster_repo = SqliteRosterRepository(ctx.conn)
    ingested = to_iso(_now())
    for p in players:
        _player_ref(ctx, p.provider_player_id, roster_raw)  # provenance = roster response
        _rid, outcome = roster_repo.append(
            team_ref_id=team_ref_id, provider=PROVIDER_MLB_STATSAPI,
            provider_team_id=provider_team_id, provider_player_id=p.provider_player_id,
            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
            raw_response_id=raw_id, raw_response_hash=raw_hash,
            roster_date=ctx.roster_date, roster_status=p.roster_status,
            jersey_number=p.jersey_number, position=p.position,
        )
        if outcome is ObservationOutcome.INSERTED:
            ctx.result.roster_observations_inserted += 1
        else:
            ctx.result.roster_observations_unchanged += 1


def _store_raw(
    conn: Any, raw_repo: SqliteRawResponseRepository, run_id: str,
    response: ProviderResponse,
) -> tuple[str, str, str]:
    """Store one raw response ONCE; return (raw_id, content_hash, received_at)."""

    exchange = response.exchange
    content_hash = response_content_hash(
        provider=PROVIDER_MLB_STATSAPI, endpoint=exchange.endpoint,
        request_params=exchange.request_params, body=exchange.body,
    )
    with transaction(conn):
        raw = raw_repo.store(
            run_id=run_id, provider=PROVIDER_MLB_STATSAPI, endpoint=exchange.endpoint,
            request_params_json=canonical_json(exchange.request_params),
            http_status=exchange.http_status,
            response_headers_json=canonical_json(exchange.response_headers),
            requested_at=to_iso(exchange.requested_at), received_at=to_iso(exchange.received_at),
            elapsed_ns=exchange.elapsed_ns, body=exchange.body, content_hash=content_hash,
            content_type=exchange.content_type,
        )
    return raw.raw_response_id, content_hash, raw.received_at


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
