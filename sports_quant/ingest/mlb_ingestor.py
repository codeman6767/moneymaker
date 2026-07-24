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

#: The optional per-game include groups the CLI understands.
VALID_INCLUDES: tuple[str, ...] = ("results", "box", "inning", "probables", "lineups")


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
    roster_observations_inserted: int = 0
    probable_pitchers_inserted: int = 0
    lineups_inserted: int = 0
    lineup_players_inserted: int = 0
    corrections_appended: int = 0
    records_rejected: int = 0
    data_quality_issues: int = 0
    capabilities_unavailable: int = 0
    rejections: list[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def note(self, reason: str) -> None:
        if len(self.rejections) < 50:
            self.rejections.append(reason)


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
            fetched["box"] = await _try_fetch(client.fetch_boxscore, game_pk_str, result)
        if include_set & {"results", "inning"}:
            fetched["line"] = await _try_fetch(client.fetch_linescore, game_pk_str, result)
        per_game[game_pk_str] = fetched

    if dry_run:
        # Normalize in memory only: count what WOULD be written, persist nothing.
        _dry_run_count(games, per_game, include_set, result)
        result.status = "partially_succeeded" if result.records_rejected else "succeeded"
        return result

    return await _persist(
        database, schedule, games, per_game, include_set, result, tool_version
    )


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


async def _try_fetch(fetch, game_pk_str: str, result: MlbIngestResult):  # noqa: ANN001
    """Fetch one per-game sub-resource, recording a failure without aborting."""

    try:
        response = await fetch(game_pk_str)
    except ProviderError as exc:
        _t, msg = sanitize_error(exc)
        result.note(f"game {game_pk_str}: sub-fetch failed ({msg})")
        return None
    except Exception as exc:  # noqa: BLE001
        _t, msg = sanitize_error(exc)
        result.note(f"game {game_pk_str}: sub-fetch error ({msg})")
        return None
    result.requests_made += 1
    return response


def _dry_run_count(
    games: list[dict[str, Any]],
    per_game: dict[str, dict[str, Optional[ProviderResponse]]],
    include_set: set[str],
    result: MlbIngestResult,
) -> None:
    for game in games:
        norm, reason = _normalize_schedule_game(game)
        if norm is None:
            result.records_rejected += 1
            result.note(reason or "invalid game")
            continue
        result.schedule_snapshots_inserted += 1
        if norm.status_unknown:
            result.data_quality_issues += 1
        if "probables" in include_set:
            result.probable_pitchers_inserted += sum(
                1 for pid in (norm.home_probable_pitcher_id, norm.away_probable_pitcher_id) if pid
            )
        if "lineups" in include_set:
            for _side, players in _parse_schedule_lineups(norm.raw_game):
                if players:
                    result.lineups_inserted += 1
                    result.lineup_players_inserted += len(players)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
async def _persist(
    database: Database,
    schedule: ProviderResponse,
    games: list[dict[str, Any]],
    per_game: dict[str, dict[str, Optional[ProviderResponse]]],
    include_set: set[str],
    result: MlbIngestResult,
    tool_version: str,
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

        refs = SqliteProviderReferenceRepository(conn)
        dq = SqliteDataQualityRepository(conn)
        ctx = _PersistCtx(conn, run.run_id, refs, dq, result)

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
                result.records_rejected += 1
                result.note(f"game {norm.game_pk}: normalization failed ({msg})")

        status = "partially_succeeded" if (result.records_rejected or result.rejections) else "succeeded"
        with transaction(conn):
            runs.complete(
                run.run_id, status=status, duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made,
                records_received=result.games_received,
                records_normalized=result.schedule_snapshots_inserted,
                records_inserted=result.schedule_snapshots_inserted,
                records_deduplicated=result.schedule_snapshots_unchanged,
                records_rejected=result.records_rejected,
            )
        result.status = status
    return result


@dataclass
class _PersistCtx:
    conn: Any
    run_id: str
    refs: SqliteProviderReferenceRepository
    dq: SqliteDataQualityRepository
    result: MlbIngestResult


def _game_ref(ctx: _PersistCtx, norm: _NormGame, raw: tuple[str, str, str]) -> str:
    raw_id, raw_hash, received_at = raw
    ref, outcome = ctx.refs.upsert(
        kind="game", provider=PROVIDER_MLB_STATSAPI, provider_entity_id=norm.game_pk,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=received_at,
    )
    if outcome.value == "inserted":
        ctx.result.games_inserted += 1
    else:
        ctx.result.games_unchanged += 1
    return ref.reference_id


def _team_ref(ctx: _PersistCtx, provider_team_id: str, raw: tuple[str, str, str]) -> str:
    raw_id, raw_hash, received_at = raw
    ref, _ = ctx.refs.upsert(
        kind="team", provider=PROVIDER_MLB_STATSAPI, provider_entity_id=provider_team_id,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=received_at,
    )
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

    if "results" in include_set:
        teams = _as_dict(data.get("teams"))
        home = _as_dict(teams.get("home"))
        away = _as_dict(teams.get("away"))
        home_runs = _opt_int(home.get("runs"))
        away_runs = _opt_int(away.get("runs"))
        winning = None
        if home_runs is not None and away_runs is not None:
            winning = "home" if home_runs > away_runs else "away" if away_runs > home_runs else "tie"
        innings_played = _opt_int(data.get("currentInning"))
        _validate_result(ctx, norm, home_runs, away_runs, raw_id)
        result_repo = SqliteResultRepository(ctx.conn)
        _rid, outcome = result_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
            provider_game_id=norm.game_pk, observed_at=observed, ingested_at=ingested,
            run_id=ctx.run_id, raw_response_id=raw_id, raw_response_hash=raw_hash,
            mapped_status=norm.mapped_status, home_runs=home_runs, away_runs=away_runs,
            home_hits=_opt_int(home.get("hits")), away_hits=_opt_int(away.get("hits")),
            home_errors=_opt_int(home.get("errors")), away_errors=_opt_int(away.get("errors")),
            innings_played=innings_played, winning_side=winning,
        )
        if outcome is ObservationOutcome.INSERTED:
            res.result_snapshots_inserted += 1

    if "inning" in include_set:
        inning_repo = SqliteInningLineRepository(ctx.conn)
        seen: set[tuple[int, str]] = set()
        for inning in data.get("innings", []) or []:
            if not isinstance(inning, dict):
                continue
            num = _opt_int(inning.get("num"))
            if num is None or num < 1:
                res.records_rejected += 1
                ctx.dq.record(
                    severity="issue", rule_code="DQ-MLB-INNING-001", entity_type="game",
                    description=f"malformed inning number {inning.get('num')!r} for {norm.game_pk}",
                    provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id, raw_response_id=raw_id,
                    entity_id=norm.game_pk,
                )
                res.data_quality_issues += 1
                continue
            for side in ("home", "away"):
                if (num, side) in seen:
                    ctx.dq.record(
                        severity="issue", rule_code="DQ-MLB-INNING-002", entity_type="game",
                        description=f"duplicate inning identity {num}/{side} for {norm.game_pk}",
                        provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id, raw_response_id=raw_id,
                        entity_id=norm.game_pk,
                    )
                    res.data_quality_issues += 1
                    continue
                seen.add((num, side))
                half = _as_dict(inning.get(side))
                _lid, outcome = inning_repo.append(
                    game_ref_id=game_ref_id, provider=PROVIDER_MLB_STATSAPI,
                    provider_game_id=norm.game_pk, inning=num, side=side, observed_at=observed,
                    ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
                    raw_response_hash=raw_hash, runs=_opt_int(half.get("runs")),
                    hits=_opt_int(half.get("hits")), errors=_opt_int(half.get("errors")),
                )
                if outcome is ObservationOutcome.INSERTED:
                    res.inning_lines_inserted += 1


def _validate_result(
    ctx: _PersistCtx, norm: _NormGame, home_runs: Optional[int], away_runs: Optional[int],
    raw_id: str,
) -> None:
    for label, runs in (("home", home_runs), ("away", away_runs)):
        if runs is not None and runs < 0:
            ctx.dq.record(
                severity="issue", rule_code="DQ-MLB-RESULT-001", entity_type="game",
                description=f"negative {label} runs ({runs}) for gamePk {norm.game_pk}",
                provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id, raw_response_id=raw_id,
                entity_id=norm.game_pk,
            )
            ctx.result.data_quality_issues += 1
    if norm.mapped_status == "final" and home_runs is None and away_runs is None:
        ctx.dq.record(
            severity="issue", rule_code="DQ-MLB-RESULT-002", entity_type="game",
            description=f"final gamePk {norm.game_pk} has no usable result data",
            provider=PROVIDER_MLB_STATSAPI, run_id=ctx.run_id, raw_response_id=raw_id,
            entity_id=norm.game_pk,
        )
        ctx.result.data_quality_issues += 1


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
