"""BALLDONTLIE (GOAT) NBA official-data ingestion (Phase D3).

Reads the BALLDONTLIE games listing (by date range or a single game id) and,
per include group, the box score (team statistics), per-player statistics,
advanced statistics, quarter lines, plays, and lineups. Everything is GET-only
through the shared D1 provider client; each raw response is preserved before it
is normalized; every derived row is append-only and traces to the *exact* raw
response that supplied it.

Design (mirrors the D2 MLB ingestor and honours the permanent CLAUDE.md rules):

* Official game identity is anchored on ``provider_game_references`` (one row per
  BALLDONTLIE game id); canonical resolution is a Phase D5 concern, so snapshots
  carry provider ids with NULLABLE canonical ids. **No second canonical game,
  team, or player system is created.**
* NBA observations use SPORT-CORRECT typed tables from the d013 repair -- NBA data
  is never stored as (or exposed through) baseball runs/innings/batting/pitching:
    - game results -> ``nba_game_results`` (home/away **points**, current
      **period**), so the corrected D2 correction semantics apply unchanged on
      points/winner (a previously-final score/winner changing, or a cumulative
      score decreasing, is a correction; a normal scheduled->in_progress->final
      progression, a rising score, and a period advancing are not);
    - team box lines -> ``nba_team_statistics`` (team **points** + a sport-neutral
      JSON ``stats`` line);
    - player box + advanced lines -> ``nba_player_statistics`` with an
      NBA-appropriate ``stat_group`` discriminator (``'traditional'`` = the box
      line, ``'advanced'`` = the advanced-stats line -- kept as distinct
      transition anchors so re-polls are idempotent).
  The baseball-named d011 result/stat tables (``game_result_snapshots`` /
  ``team_game_statistics`` / ``player_game_statistics``) remain MLB-only.
* NBA-specific append-only observations use the d012 tables directly:
  ``nba_quarter_lines`` (derived from the detailed box-score response, since
  ``/v1/games`` carries no per-quarter field), ``play_snapshots``, and
  ``injury_snapshots`` (with an exact ``return_estimate`` text preserved and a
  parsed ``return_date`` only for an unambiguous full ISO date -- no fabricated
  year). Cross-sport schedule (``game_schedule_snapshots``) and lineup
  (``lineup_snapshots`` / ``lineup_players``) infrastructure is still shared.
* Box scores are associated with a schedule game by a genuine provider game id
  when supplied, else the deterministic ``(official date, provider home-team id,
  provider visitor-team id)`` key; a no-match or ambiguous match is rejected
  honestly (a ``DQ-NBA-BOX-001`` note), never guessed or cross-attached.
* Missing values stay missing (never coerced to zero); a missing conditional
  group is recorded as a capability state, never fabricated and never an active
  failure. Confirmed pregame starters stay unavailable; lineups are best-effort
  and never reinterpreted as confirmed starters.
* ``--dry-run`` runs the identical normalization/validation and persists
  absolutely nothing (no database, run, raw response, capability, reference,
  observation, or data-quality issue).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.capabilities import SqliteCapabilityRepository
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.lineups import LineupPlayerInput, SqliteLineupRepository
from ..db.repositories.nba import (
    SqliteInjurySnapshotRepository,
    SqliteNbaPlayerStatRepository,
    SqliteNbaResultRepository,
    SqliteNbaTeamStatRepository,
    SqlitePlaySnapshotRepository,
    SqliteQuarterLineRepository,
)
from ..db.repositories.observations import ObservationOutcome
from ..db.repositories.official_games import SqliteScheduleRepository
from ..db.repositories.raw_responses import SqliteRawResponseRepository, response_content_hash
from ..db.repositories.references import SqliteProviderReferenceRepository
from ..db.schema import to_iso
from ..providers.balldontlie import BalldontlieClient, next_cursor
from ..providers.base_provider import ProviderError, ProviderResponse
from ..providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    BalldontlieTier,
    CapabilityDeclaration,
    CapabilityState,
    ProviderCapability,
    ProviderErrorKind,
    balldontlie_declaration,
)
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-nba"
_INJURIES_COMMAND = "ingest-injuries"

#: The optional include groups the ``ingest-nba`` CLI understands.
VALID_INCLUDES: tuple[str, ...] = (
    "results", "box", "player-stats", "advanced", "quarters", "plays", "lineups",
)

#: Safe finite bounds on cursor pagination so a run can never loop unbounded.
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_RECORDS = 10_000

_INCLUDE_CAPABILITY = {
    "results": ProviderCapability.GAME_RESULTS,
    "box": ProviderCapability.TEAM_STATISTICS,
    "player-stats": ProviderCapability.PLAYER_STATISTICS,
    "advanced": ProviderCapability.ADVANCED_STATISTICS,
    "quarters": ProviderCapability.QUARTER_LINES,
    "plays": ProviderCapability.PLAYS,
    "lineups": ProviderCapability.LINEUPS,
}

#: Conditional capabilities recorded (honestly, with their declared state) on
#: every persisted run, so the corpus records what NBA data was believed
#: available (and at which tier) when each row was ingested.
_CONDITIONAL_CAPABILITIES = (
    ProviderCapability.ADVANCED_STATISTICS,
    ProviderCapability.QUARTER_LINES,
    ProviderCapability.PLAYS,
    ProviderCapability.LINEUPS,
    ProviderCapability.INJURIES,
    ProviderCapability.CONFIRMED_PREGAME_STARTERS,
    ProviderCapability.SUBSTITUTIONS,
    ProviderCapability.CORRECTION_TIMESTAMPS,
    ProviderCapability.HISTORICAL_DEPTH,
)


@dataclass
class NbaIngestResult:
    """Sanitized, deterministic counters for one NBA ingest, safe to print/JSON."""

    dry_run: bool
    status: str
    command: str = _COMMAND
    run_id: Optional[str] = None
    requests_made: int = 0
    pages_fetched: int = 0
    raw_responses_received: int = 0
    games_received: int = 0
    teams_observed: int = 0
    players_observed: int = 0
    schedule_observations: int = 0
    result_observations: int = 0
    corrections_appended: int = 0
    team_stat_observations: int = 0
    player_stat_observations: int = 0
    advanced_stat_observations: int = 0
    quarter_observations: int = 0
    play_observations: int = 0
    lineup_observations: int = 0
    lineup_players_observed: int = 0
    injury_observations: int = 0
    provider_references_created: int = 0
    #: Total normalized observations (nonzero in dry-run and persist alike).
    observations_normalized: int = 0
    #: Rows actually written to the database (always 0 in dry-run).
    rows_persisted: int = 0
    records_inserted: int = 0
    records_changed: int = 0
    records_unchanged: int = 0
    records_rejected: int = 0
    records_truncated: int = 0
    data_quality_issues: int = 0
    capabilities_unavailable: int = 0
    capabilities_recorded: int = 0
    truncations: list[str] = field(default_factory=list)
    #: A genuine provider/normalization failure on a *requested* endpoint (network
    #: after retries, 5xx, auth, parser, unexpected). Drives ``partially_failed``
    #: and a non-zero CLI exit. A tier restriction is NOT an active failure.
    active_failures: int = 0
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
        return self.status in ("failed", "partially_failed")

    def note(self, reason: str) -> None:
        if len(self.rejections) < 50:
            self.rejections.append(reason)

    def truncation(self, reason: str) -> None:
        self.records_truncated += 1
        if reason not in self.truncations and len(self.truncations) < 50:
            self.truncations.append(reason)

    def record_active_failure(self, error_type: str, message: str) -> None:
        self.active_failures += 1
        if self.error_message is None:
            self.error_type, self.error_message = error_type, message


# --------------------------------------------------------------------------- #
# Pure parsing helpers (operate on already-sanitized parsed JSON)
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
    return value if isinstance(value, dict) else {}


def _map_nba_status(status_raw: Optional[str]) -> tuple[str, bool]:
    """Map a BALLDONTLIE game ``status`` string to a canonical status.

    Returns ``(mapped_status, is_unknown)``. A scheduled game's status is an ISO
    datetime; a finished game is ``"Final"``; a live game names a quarter/half/OT.
    An unrecognised value maps to ``'unknown'`` (never guessed final/scheduled)
    with ``is_unknown=True`` so the ingestor records a data-quality issue.
    """

    if status_raw is None:
        return "unknown", True
    text = status_raw.strip().lower()
    if not text:
        return "unknown", True
    if "final" in text:
        return "final", False
    if any(tok in text for tok in ("qtr", "quarter", "half", "ot", "overtime", "in progress")):
        return "in_progress", False
    if "postpon" in text:
        return "postponed", False
    if "suspend" in text:
        return "suspended", False
    if "cancel" in text:
        return "cancelled", False
    if "delay" in text:
        return "delayed", False
    # An ISO datetime (a scheduled tip-off time) begins with a 4-digit year.
    if len(text) >= 10 and text[:4].isdigit() and text[4] == "-":
        return "scheduled", False
    if text in ("scheduled", "pregame", "warmup"):
        return text, False
    return "unknown", True


@dataclass(frozen=True)
class _QuarterRow:
    period: int
    side: str
    points: Optional[int]


@dataclass(frozen=True)
class _NormGame:
    game_id: str
    date_local: Optional[str]
    scheduled_start: Optional[str]
    season: Optional[int]
    status_raw: Optional[str]
    mapped_status: str
    status_unknown: bool
    period: Optional[int]
    home_provider_team_id: Optional[str]
    away_provider_team_id: Optional[str]
    home_score: Optional[int]
    away_score: Optional[int]
    winning_side: Optional[str]
    raw_game: dict[str, Any]


def _normalize_game(game: dict[str, Any]) -> tuple[Optional[_NormGame], Optional[str]]:
    """Normalize one BALLDONTLIE game dict, or return a rejection reason.

    Uses only documented ``/v1/games`` fields (id, date, season, status, period,
    home/visitor team + score). Per-quarter scores are NOT a documented games
    field, so they are never read here -- quarter lines are derived from the
    detailed box-score response instead (see ``_parse_box_quarters``).
    """

    game_id = _provider_id(game, "id")
    if game_id is None:
        return None, "game missing id"
    home = _as_dict(game.get("home_team"))
    away = _as_dict(game.get("visitor_team"))
    status_raw = _opt_str(game.get("status"))
    mapped, unknown = _map_nba_status(status_raw)
    home_score = _opt_int(game.get("home_team_score"))
    away_score = _opt_int(game.get("visitor_team_score"))
    winning: Optional[str] = None
    if home_score is not None and away_score is not None:
        winning = "home" if home_score > away_score else "away" if away_score > home_score else "tie"
    date_local = None
    raw_date = _opt_str(game.get("date"))
    if raw_date is not None:
        date_local = raw_date.split("T", 1)[0]
    return (
        _NormGame(
            game_id=game_id,
            date_local=date_local,
            scheduled_start=status_raw if (mapped == "scheduled") else None,
            season=_opt_int(game.get("season")),
            status_raw=status_raw,
            mapped_status=mapped,
            status_unknown=unknown,
            period=_opt_int(game.get("period")),
            home_provider_team_id=_provider_id(home),
            away_provider_team_id=_provider_id(away),
            home_score=home_score,
            away_score=away_score,
            winning_side=winning,
            raw_game=game,
        ),
        None,
    )


def _parse_box_quarters(box_game: dict[str, Any]) -> list[_QuarterRow]:
    """Derive per-period lines from a matched box-score game (best-effort).

    Quarter/period scores are not exposed by the ``/v1/games`` listing, so they
    are derived from the richer box-score object when it supplies a ``periods``
    array (the exact GOAT field is pending live verification). ONLY periods the
    provider actually supplied become rows; an absent period is never a row, and a
    missing per-side score stays ``None`` (distinct from an explicit 0). Regulation
    quarters and overtime periods are both supported.
    """

    rows: list[_QuarterRow] = []
    periods = box_game.get("periods")
    seen: set[tuple[int, str]] = set()
    for entry in periods if isinstance(periods, list) else []:
        if not isinstance(entry, dict):
            continue
        period = _opt_int(entry.get("period"))
        if period is None or period < 1:
            continue
        for side, key in (("home", "home"), ("away", "away")):
            if key not in entry:
                continue  # a period side not supplied is never a fabricated row/zero
            if (period, side) in seen:
                continue
            seen.add((period, side))
            rows.append(_QuarterRow(period=period, side=side, points=_opt_int(entry.get(key))))
    return rows


@dataclass(frozen=True)
class _PlayerStatRow:
    provider_player_id: str
    provider_team_id: Optional[str]
    stat_group: str  # 'traditional' (box line) | 'advanced' (advanced-stats line)
    position: Optional[str]
    is_starter: Optional[bool]
    points: Optional[int]
    stats_json: str


@dataclass(frozen=True)
class _TeamStatRow:
    provider_team_id: str
    home_away: str
    points: Optional[int]
    stats_json: str


def _stat_player(row: dict[str, Any]) -> Optional[str]:
    return _provider_id(_as_dict(row.get("player"))) or _provider_id(row, "player_id")


def _normalize_stat_row(row: dict[str, Any], *, stat_group: str) -> Optional[_PlayerStatRow]:
    """Normalize one ``/v1/stats`` or advanced-stats row into a player line.

    ``stat_group`` is an NBA-appropriate discriminator ('traditional' | 'advanced'),
    never a baseball role. ``points`` (``pts``) is surfaced as a typed column; the
    full sport-neutral stat line is preserved as canonical JSON.
    """

    if not isinstance(row, dict):
        return None
    pid = _stat_player(row)
    if pid is None:
        return None
    stats = {k: v for k, v in row.items() if k not in ("player", "team", "game")}
    starter = row.get("starter")
    is_starter = starter if isinstance(starter, bool) else None
    return _PlayerStatRow(
        provider_player_id=pid,
        provider_team_id=_provider_id(_as_dict(row.get("team"))) or _provider_id(row, "team_id"),
        stat_group=stat_group,
        position=_opt_str(row.get("position")),
        is_starter=is_starter,
        points=_opt_int(row.get("pts")),
        stats_json=canonical_json(stats),
    )


def _normalize_box_team_lines(box_game: dict[str, Any]) -> list[_TeamStatRow]:
    """Normalize a single box-score game into per-team lines (team-level only).

    Player statistics come from ``/v1/stats`` (a distinct include), so the box
    path deliberately produces only team lines here: the team ``points`` (from the
    game's home/visitor score) plus the team block (without its ``players`` array)
    as the sport-neutral JSON stat line. Missing values stay missing.
    """

    team_points = {
        "home": _opt_int(box_game.get("home_team_score")),
        "away": _opt_int(box_game.get("visitor_team_score")),
    }
    team_rows: list[_TeamStatRow] = []
    for side, key in (("home", "home_team"), ("away", "visitor_team")):
        block = _as_dict(box_game.get(key))
        provider_team_id = _provider_id(block)
        if provider_team_id is None:
            continue
        team_meta = {k: v for k, v in block.items() if k != "players"}
        team_rows.append(
            _TeamStatRow(provider_team_id=provider_team_id, home_away=side,
                         points=team_points[side], stats_json=canonical_json(team_meta))
        )
    return team_rows


@dataclass(frozen=True)
class _PlayRow:
    play_identity: str
    provider_play_id: Optional[str]
    period: Optional[int]
    play_sequence: Optional[int]
    clock: Optional[str]
    event_type: Optional[str]
    description: Optional[str]
    provider_team_id: Optional[str]
    provider_player_id: Optional[str]
    is_substitution: bool
    extra_json: str


def _is_substitution(row: dict[str, Any]) -> bool:
    event_type = str(row.get("type") or row.get("event_type") or "").lower()
    text = str(row.get("text") or row.get("description") or "").lower()
    if "substitution" in event_type or event_type in ("sub", "substitution"):
        return True
    return "substitution" in text or "enters the game" in text


def _normalize_play(
    row: dict[str, Any], *, provider_game_id: str, ordinal: int
) -> Optional[_PlayRow]:
    """Normalize one play. Uses the provider play id when it genuinely exists;
    otherwise derives a deterministic provider-game-scoped identity from stable
    supplied sequence fields (falling back to the response ordinal)."""

    if not isinstance(row, dict):
        return None
    provider_play_id = _provider_id(row, "id")
    period = _opt_int(row.get("period"))
    sequence = _opt_int(row.get("event_num") if row.get("event_num") is not None
                        else row.get("sequence"))
    clock = _opt_str(row.get("clock") or row.get("time"))
    event_type = _opt_str(row.get("type") or row.get("event_type"))
    description = _opt_str(row.get("text") or row.get("description"))
    if provider_play_id is not None:
        identity = provider_play_id
    else:
        basis = canonical_json({
            "game": provider_game_id, "period": period, "sequence": sequence,
            "clock": clock, "type": event_type, "description": description,
            "ordinal": None if sequence is not None else ordinal,
        })
        identity = "d:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return _PlayRow(
        play_identity=identity,
        provider_play_id=provider_play_id,
        period=period,
        play_sequence=sequence if sequence is not None else ordinal,
        clock=clock,
        event_type=event_type,
        description=description,
        provider_team_id=_provider_id(_as_dict(row.get("team"))) or _provider_id(row, "team_id"),
        provider_player_id=_provider_id(_as_dict(row.get("player")))
        or _provider_id(row, "player_id"),
        is_substitution=_is_substitution(row),
        extra_json=canonical_json({k: v for k, v in row.items() if k not in ("team", "player")}),
    )


@dataclass(frozen=True)
class _InjuryRow:
    provider_player_id: str
    provider_team_id: Optional[str]
    status: str
    description: Optional[str]
    reason: Optional[str]
    #: A parsed ISO date, ONLY when the provider supplied an unambiguous full
    #: ``YYYY-MM-DD`` value; ``None`` for an ambiguous estimate like ``"Nov 17"``.
    return_date: Optional[str]
    #: The provider's EXACT return-estimate text, preserved verbatim (never
    #: discarded, never given a fabricated year).
    return_estimate: Optional[str]


def _parse_full_iso_date(text: Optional[str]) -> Optional[str]:
    """Return ``text`` as ``YYYY-MM-DD`` only when it is an unambiguous full ISO
    calendar date; otherwise ``None`` (no year is ever fabricated)."""

    if text is None:
        return None
    from datetime import date as _date

    candidate = text.split("T", 1)[0].strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return None
    try:
        _date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _normalize_injury(row: dict[str, Any]) -> Optional[_InjuryRow]:
    """Normalize one injury record.

    A supplied-but-missing status is recorded as the literal ``'unknown'`` -- never
    interpreted as active/available/probable/questionable/doubtful/out/healthy.
    Absence of a record entirely is handled by the caller (it is never "healthy").
    The provider's return estimate is preserved EXACTLY as ``return_estimate``; a
    parsed ISO ``return_date`` is populated only when the value is an unambiguous
    full calendar date (so ``"Nov 17"`` is kept verbatim with no fabricated year).
    """

    if not isinstance(row, dict):
        return None
    player = _as_dict(row.get("player"))
    pid = _provider_id(player) or _provider_id(row, "player_id")
    if pid is None:
        return None
    return_estimate = _opt_str(row.get("return_date"))
    return _InjuryRow(
        provider_player_id=pid,
        provider_team_id=_provider_id(player, "team_id") or _provider_id(_as_dict(row.get("team"))),
        status=_opt_str(row.get("status")) or "unknown",
        description=_opt_str(row.get("description")),
        reason=_opt_str(row.get("reason")),
        return_date=_parse_full_iso_date(return_estimate),
        return_estimate=return_estimate,
    )


# --------------------------------------------------------------------------- #
# Capability gating
# --------------------------------------------------------------------------- #
def _requested_capabilities_available(
    includes: set[str], declaration: CapabilityDeclaration, result: NbaIngestResult
) -> set[str]:
    """Drop include groups the capability declaration marks unavailable.

    A group whose capability is not requestable at this tier (e.g.
    ``paid_tier_required`` on a lower tier) is skipped and counted, never
    requested -- and it is not an active failure.
    """

    available: set[str] = set()
    for name in includes:
        cap = _INCLUDE_CAPABILITY.get(name)
        if cap is not None and not declaration.is_available(cap):
            result.capabilities_unavailable += 1
            result.note(f"capability for {name!r} is not available at this provider tier")
            continue
        available.add(name)
    return available


def _rows(response: ProviderResponse) -> list[dict[str, Any]]:
    data = response.data
    rows = data.get("data") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _box_key(box_game: dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """The deterministic ``(date, home_team_id, visitor_team_id)`` key of a box
    object, or ``None`` when any component is missing."""

    date = _opt_str(box_game.get("date"))
    if date is not None:
        date = date.split("T", 1)[0]
    home = _provider_id(_as_dict(box_game.get("home_team")))
    away = _provider_id(_as_dict(box_game.get("visitor_team")))
    if date and home and away:
        return (date, home, away)
    return None


def _match_box_game(data: Any, norm: "_NormGame") -> tuple[Optional[dict[str, Any]], str]:
    """Associate a box-score object with a normalized schedule game.

    The documented ``/v1/box_scores`` response may not carry a top-level game id,
    so matching is by the deterministic ``(official date, provider home-team id,
    provider visitor-team id)`` key. A genuine provider game id is still honoured
    when present. Returns ``(box_game, reason)`` where reason is ``'matched'``
    (exactly one match -> process it), ``'no_match'`` (reject honestly), or
    ``'ambiguous'`` (several possible matches -> reject rather than guess). One
    game's box score is never attached to another game.
    """

    rows = data.get("data") if isinstance(data, dict) else None
    games = [g for g in (rows if isinstance(rows, list) else []) if isinstance(g, dict)]

    # 1. A genuine provider game id, if the provider actually supplies one.
    id_matches = [g for g in games if _provider_id(g, "id") == norm.game_id]
    if len(id_matches) == 1:
        return id_matches[0], "matched"
    if len(id_matches) > 1:
        return None, "ambiguous"

    # 2. Deterministic (date, home-team, visitor-team) key.
    if not (norm.date_local and norm.home_provider_team_id and norm.away_provider_team_id):
        return None, "no_match"
    key = (norm.date_local, norm.home_provider_team_id, norm.away_provider_team_id)
    keyed = [g for g in games if _box_key(g) == key]
    if len(keyed) == 1:
        return keyed[0], "matched"
    if len(keyed) > 1:
        return None, "ambiguous"
    return None, "no_match"


def _parse_lineups(data: Any, game_id: str) -> list[tuple[str, list[LineupPlayerInput]]]:
    """Parse a ``/v1/lineups`` payload into ``(provider_team_id, players)`` pairs.

    Best-effort only: a posted lineup is NEVER a confirmed pregame starter set --
    the caller writes these with ``is_confirmed=False`` unconditionally.
    """

    out: list[tuple[str, list[LineupPlayerInput]]] = []
    rows = data.get("data") if isinstance(data, dict) else None
    for entry in rows if isinstance(rows, list) else []:
        if not isinstance(entry, dict):
            continue
        gid = _provider_id(_as_dict(entry.get("game"))) or _provider_id(entry, "game_id")
        if gid is not None and gid != game_id:
            continue
        team_id = _provider_id(_as_dict(entry.get("team"))) or _provider_id(entry, "team_id")
        if team_id is None:
            continue
        players: list[LineupPlayerInput] = []
        raw_players = entry.get("players")
        for order, p in enumerate(raw_players if isinstance(raw_players, list) else [], start=1):
            if not isinstance(p, dict):
                continue
            pid = _provider_id(_as_dict(p.get("player"))) or _provider_id(p, "player_id")
            if pid is None:
                continue
            players.append(
                LineupPlayerInput(
                    batting_order=order, provider_player_id=pid, position=_opt_str(p.get("position"))
                )
            )
        if players:
            out.append((team_id, players))
    return out


# --------------------------------------------------------------------------- #
# Fetch phase (GET-only, sequential; identical for dry-run and persisted mode)
# --------------------------------------------------------------------------- #
async def _try(
    coro: Awaitable[ProviderResponse], *, label: str, result: NbaIngestResult
) -> Optional[ProviderResponse]:
    """Await one requested sub-resource fetch; classify failures honestly.

    A tier restriction (a plan-gated endpoint on this subscription tier) is a
    **capability-unavailable**, not an active failure -- unrelated groups continue.
    Any other provider/parser/network failure is a genuine active failure (drives
    ``partially_failed`` + a non-zero exit). Never raises.
    """

    try:
        response = await coro
    except ProviderError as exc:
        error_type, msg = sanitize_error(exc)
        if exc.kind is ProviderErrorKind.TIER_RESTRICTED:
            result.capabilities_unavailable += 1
            result.note(f"{label}: capability unavailable for current subscription tier")
            return None
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


async def _paginate(
    make_call: Callable[[Optional[int]], Awaitable[ProviderResponse]],
    *,
    label: str,
    result: NbaIngestResult,
    max_pages: int,
    max_records: int,
) -> list[ProviderResponse]:
    """Cursor-paginate a sub-resource with hard bounds and loop protection.

    Detects a repeated cursor (stops without claiming completeness), honours an
    explicit page/record bound (reporting truncation honestly), and keeps requests
    strictly sequential.
    """

    pages: list[ProviderResponse] = []
    seen: set[int] = set()
    cursor: Optional[int] = None
    total = 0
    while True:
        response = await _try(make_call(cursor), label=label, result=result)
        if response is None:
            break
        pages.append(response)
        result.pages_fetched += 1
        total += len(_rows(response))
        nxt = next_cursor(response.data)
        if nxt is None:
            break
        if nxt in seen:
            result.note(f"{label}: repeated cursor {nxt} -- pagination stopped (loop guard)")
            break
        if len(pages) >= max_pages:
            result.truncation(f"{label} truncated at max_pages={max_pages} -- coverage is partial")
            break
        if total >= max_records:
            result.truncation(f"{label} truncated at max_records={max_records} -- coverage partial")
            break
        seen.add(nxt)
        cursor = nxt
    return pages


async def _fetch_games(
    client: BalldontlieClient,
    *,
    from_date: Optional[str],
    to_date: Optional[str],
    game_id: Optional[int],
    result: NbaIngestResult,
    max_pages: int,
    max_records: int,
) -> list[ProviderResponse]:
    """Fetch the games listing. A failure here is fatal (raised to the caller)."""

    if game_id is not None:
        response = await client.fetch_game(game_id)
        result.requests_made += 1
        result.pages_fetched += 1
        return [response]

    pages: list[ProviderResponse] = []
    seen: set[int] = set()
    cursor: Optional[int] = None
    total = 0
    while True:
        response = await client.fetch_games(
            start_date=from_date, end_date=to_date, per_page=100, cursor=cursor
        )
        result.requests_made += 1
        result.pages_fetched += 1
        pages.append(response)
        total += len(_rows(response))
        nxt = next_cursor(response.data)
        if nxt is None:
            break
        if nxt in seen:
            result.note("games: repeated cursor -- pagination stopped (loop guard)")
            break
        if len(pages) >= max_pages:
            result.truncation(f"games listing truncated at max_pages={max_pages} -- coverage partial")
            break
        if total >= max_records:
            result.truncation("games listing truncated at max_records -- coverage partial")
            break
        seen.add(nxt)
        cursor = nxt
    return pages


def _stats_call(
    client: BalldontlieClient, gid: str
) -> Callable[[Optional[int]], Awaitable[ProviderResponse]]:
    def call(cursor: Optional[int]) -> Awaitable[ProviderResponse]:
        return client.fetch_stats(game_ids=[gid], per_page=100, cursor=cursor)

    return call


def _advanced_call(
    client: BalldontlieClient, gid: str
) -> Callable[[Optional[int]], Awaitable[ProviderResponse]]:
    def call(cursor: Optional[int]) -> Awaitable[ProviderResponse]:
        return client.fetch_advanced_stats(game_ids=[gid], per_page=100, cursor=cursor)

    return call


def _plays_call(
    client: BalldontlieClient, gid: str
) -> Callable[[Optional[int]], Awaitable[ProviderResponse]]:
    def call(cursor: Optional[int]) -> Awaitable[ProviderResponse]:
        return client.fetch_plays(game_id=gid, per_page=100, cursor=cursor)

    return call


def _injuries_call(
    client: BalldontlieClient,
) -> Callable[[Optional[int]], Awaitable[ProviderResponse]]:
    def call(cursor: Optional[int]) -> Awaitable[ProviderResponse]:
        return client.fetch_player_injuries(per_page=100, cursor=cursor)

    return call


@dataclass
class _Fetched:
    games: list[tuple[dict[str, Any], ProviderResponse]]
    responses: list[ProviderResponse]
    box_by_date: dict[str, ProviderResponse]
    stats_by_game: dict[str, list[ProviderResponse]]
    adv_by_game: dict[str, list[ProviderResponse]]
    plays_by_game: dict[str, list[ProviderResponse]]
    lineup_by_game: dict[str, ProviderResponse]


async def _fetch_all(
    client: BalldontlieClient,
    *,
    from_date: Optional[str],
    to_date: Optional[str],
    game_id: Optional[int],
    include_set: set[str],
    result: NbaIngestResult,
    max_pages: int,
    max_records: int,
) -> _Fetched:
    game_pages = await _fetch_games(
        client, from_date=from_date, to_date=to_date, game_id=game_id, result=result,
        max_pages=max_pages, max_records=max_records,
    )
    games: list[tuple[dict[str, Any], ProviderResponse]] = []
    for page in game_pages:
        data = page.data
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(rows, dict):  # /v1/games/{id} returns a single object
            rows = [rows]
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                games.append((row, page))
    result.games_received = len(games)

    norm_games = [(_normalize_game(g)[0], page) for g, page in games]
    game_ids = [n.game_id for n, _ in norm_games if n is not None]

    responses: list[ProviderResponse] = list(game_pages)
    box_by_date: dict[str, ProviderResponse] = {}
    stats_by_game: dict[str, list[ProviderResponse]] = {}
    adv_by_game: dict[str, list[ProviderResponse]] = {}
    plays_by_game: dict[str, list[ProviderResponse]] = {}
    lineup_by_game: dict[str, ProviderResponse] = {}

    # Box scores back both team statistics (box) and derived quarter lines
    # (quarters), so fetch them when either group is requested.
    if include_set & {"box", "quarters"}:
        dates = sorted({n.date_local for n, _ in norm_games if n is not None and n.date_local})
        for d in dates:
            resp = await _try(client.fetch_box_scores(date=d), label=f"box {d}", result=result)
            if resp is not None:
                box_by_date[d] = resp
                responses.append(resp)
                if next_cursor(resp.data) is not None:
                    result.truncation(f"box_scores for {d} truncated -- coverage is partial")

    for gid in game_ids:
        if "player-stats" in include_set:
            pages = await _paginate(
                _stats_call(client, gid), label=f"stats game {gid}", result=result,
                max_pages=max_pages, max_records=max_records,
            )
            if pages:
                stats_by_game[gid] = pages
                responses.extend(pages)
        if "advanced" in include_set:
            pages = await _paginate(
                _advanced_call(client, gid), label=f"advanced game {gid}", result=result,
                max_pages=max_pages, max_records=max_records,
            )
            if pages:
                adv_by_game[gid] = pages
                responses.extend(pages)
        if "plays" in include_set:
            pages = await _paginate(
                _plays_call(client, gid), label=f"plays game {gid}", result=result,
                max_pages=max_pages, max_records=max_records,
            )
            if pages:
                plays_by_game[gid] = pages
                responses.extend(pages)
        if "lineups" in include_set:
            resp = await _try(client.fetch_lineups(game_ids=[gid]),
                              label=f"lineups game {gid}", result=result)
            if resp is not None:
                lineup_by_game[gid] = resp
                responses.append(resp)

    return _Fetched(games, responses, box_by_date, stats_by_game, adv_by_game,
                    plays_by_game, lineup_by_game)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
async def ingest_nba(
    *,
    database: Database,
    client: BalldontlieClient,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    game_id: Optional[int] = None,
    includes: tuple[str, ...] = (),
    tier: BalldontlieTier = BalldontlieTier.GOAT,
    dry_run: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
    tool_version: str = _TOOL_VERSION,
    command: str = _COMMAND,
) -> NbaIngestResult:
    """Ingest NBA official data from BALLDONTLIE. ``--dry-run`` persists nothing."""

    result = NbaIngestResult(dry_run=dry_run, status="succeeded", command=command)
    declaration = balldontlie_declaration(tier)
    include_set = _requested_capabilities_available(set(includes), declaration, result)

    try:
        fetched = await _fetch_all(
            client, from_date=from_date, to_date=to_date, game_id=game_id,
            include_set=include_set, result=result, max_pages=max_pages, max_records=max_records,
        )
    except ProviderError as exc:
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    if dry_run:
        _count_plan(fetched, include_set, result)
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        return result

    return await _persist(
        database, fetched, include_set, declaration, tier, result, tool_version,
    )


async def ingest_injuries(
    *,
    database: Database,
    client: BalldontlieClient,
    date: Optional[str] = None,
    tier: BalldontlieTier = BalldontlieTier.GOAT,
    dry_run: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
    tool_version: str = _TOOL_VERSION,
) -> NbaIngestResult:
    """Ingest current NBA player injuries from BALLDONTLIE. ``--dry-run`` persists nothing.

    ``date`` is the requested point-in-time label (recorded in the run args). The
    endpoint reports the CURRENT injury picture, so ``observed_at`` (=
    ``received_at``) is the true cutoff; no date is fabricated onto a row.
    """

    result = NbaIngestResult(dry_run=dry_run, status="succeeded", command=_INJURIES_COMMAND)
    declaration = balldontlie_declaration(tier)
    if not declaration.is_available(ProviderCapability.INJURIES):
        # Tier does not grant injuries: capability-unavailable, not a failure.
        result.capabilities_unavailable += 1
        result.note("injuries capability is not available at this provider tier")
        if not dry_run:
            return await _persist_injuries(database, [], date, tier, result, tool_version)
        result.status = "succeeded"
        return result

    try:
        pages = await _paginate(
            _injuries_call(client), label="injuries", result=result, max_pages=max_pages,
            max_records=max_records,
        )
    except Exception as exc:  # noqa: BLE001 - defensive; _paginate already guards
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    if dry_run:
        for page in pages:
            for row in _rows(page):
                inj = _normalize_injury(row)
                if inj is None:
                    result.records_rejected += 1
                    continue
                result.injury_observations += 1
                result.observations_normalized += 1
                result.records_inserted += 1
                result.provider_references_created += 1
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        return result

    return await _persist_injuries(database, pages, date, tier, result, tool_version)


# --------------------------------------------------------------------------- #
# Dry-run counting (same normalization/validation; nothing persisted)
# --------------------------------------------------------------------------- #
def _count_plan(fetched: _Fetched, include_set: set[str], result: NbaIngestResult) -> None:
    refs: set[tuple[str, str]] = set()
    teams: set[str] = set()
    players: set[str] = set()

    def ref(kind: str, entity_id: str) -> None:
        if (kind, entity_id) not in refs:
            refs.add((kind, entity_id))
            result.provider_references_created += 1
        if kind == "team":
            teams.add(entity_id)
        elif kind == "player":
            players.add(entity_id)

    def observe(counter: str) -> None:
        setattr(result, counter, getattr(result, counter) + 1)
        result.observations_normalized += 1
        result.records_inserted += 1  # with no prior DB state, every one is a would-be insert

    for game, _page in fetched.games:
        norm, reason = _normalize_game(game)
        if norm is None:
            result.records_rejected += 1
            result.note(reason or "invalid game")
            continue
        ref("game", norm.game_id)
        for tid in (norm.home_provider_team_id, norm.away_provider_team_id):
            if tid:
                ref("team", tid)
        observe("schedule_observations")
        if norm.status_unknown:
            result.data_quality_issues += 1
        if "results" in include_set:
            observe("result_observations")
        box_game: Optional[dict[str, Any]] = None
        if (include_set & {"box", "quarters"}) and norm.date_local in fetched.box_by_date:
            box_game, reason = _match_box_game(fetched.box_by_date[norm.date_local].data, norm)
            if box_game is None and reason in ("no_match", "ambiguous"):
                result.data_quality_issues += 1
                result.records_rejected += 1
        if box_game is not None:
            if "box" in include_set:
                for _tr in _normalize_box_team_lines(box_game):
                    observe("team_stat_observations")
            if "quarters" in include_set:
                for _q in _parse_box_quarters(box_game):
                    observe("quarter_observations")
        if "player-stats" in include_set:
            for page in fetched.stats_by_game.get(norm.game_id, []):
                for row in _rows(page):
                    ps = _normalize_stat_row(row, stat_group="traditional")
                    if ps is None:
                        result.records_rejected += 1
                        continue
                    ref("player", ps.provider_player_id)
                    observe("player_stat_observations")
        if "advanced" in include_set:
            for page in fetched.adv_by_game.get(norm.game_id, []):
                for row in _rows(page):
                    ps = _normalize_stat_row(row, stat_group="advanced")
                    if ps is None:
                        result.records_rejected += 1
                        continue
                    ref("player", ps.provider_player_id)
                    observe("advanced_stat_observations")
        if "plays" in include_set:
            ordinal = 0
            for page in fetched.plays_by_game.get(norm.game_id, []):
                for row in _rows(page):
                    ordinal += 1
                    pl = _normalize_play(row, provider_game_id=norm.game_id, ordinal=ordinal)
                    if pl is None:
                        result.records_rejected += 1
                        continue
                    if pl.provider_player_id:
                        ref("player", pl.provider_player_id)
                    observe("play_observations")
        if "lineups" in include_set and norm.game_id in fetched.lineup_by_game:
            for _team_id, plist in _parse_lineups(
                fetched.lineup_by_game[norm.game_id].data, norm.game_id
            ):
                observe("lineup_observations")
                result.lineup_players_observed += len(plist)
                for p in plist:
                    ref("player", p.provider_player_id)

    result.teams_observed = len(teams)
    result.players_observed = len(players)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
@dataclass
class _Ctx:
    conn: Any
    run_id: str
    refs: SqliteProviderReferenceRepository
    dq: SqliteDataQualityRepository
    result: NbaIngestResult
    raws: dict[int, tuple[str, str, str]]
    teams: set[str] = field(default_factory=set)
    players: set[str] = field(default_factory=set)


def _store_raw(
    conn: Any, raw_repo: SqliteRawResponseRepository, run_id: str, response: ProviderResponse
) -> tuple[str, str, str]:
    exchange = response.exchange
    content_hash = response_content_hash(
        provider=PROVIDER_BALLDONTLIE, endpoint=exchange.endpoint,
        request_params=exchange.request_params, body=exchange.body,
    )
    with transaction(conn):
        raw = raw_repo.store(
            run_id=run_id, provider=PROVIDER_BALLDONTLIE, endpoint=exchange.endpoint,
            request_params_json=canonical_json(exchange.request_params),
            http_status=exchange.http_status,
            response_headers_json=canonical_json(exchange.response_headers),
            requested_at=to_iso(exchange.requested_at), received_at=to_iso(exchange.received_at),
            elapsed_ns=exchange.elapsed_ns, body=exchange.body, content_hash=content_hash,
            content_type=exchange.content_type,
        )
    return raw.raw_response_id, content_hash, raw.received_at


def _store_all_raws(
    conn: Any, raw_repo: SqliteRawResponseRepository, run_id: str,
    responses: list[ProviderResponse], result: NbaIngestResult,
) -> dict[int, tuple[str, str, str]]:
    raws: dict[int, tuple[str, str, str]] = {}
    for response in responses:
        if id(response) in raws:
            continue
        raws[id(response)] = _store_raw(conn, raw_repo, run_id, response)
        result.raw_responses_received += 1
    return raws


def _ref(ctx: _Ctx, kind: str, provider_entity_id: str, source: ProviderResponse) -> str:
    raw_id, raw_hash, observed = ctx.raws[id(source)]
    reference, outcome = ctx.refs.upsert(
        kind=kind, provider=PROVIDER_BALLDONTLIE, provider_entity_id=provider_entity_id,
        raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=observed,
    )
    if outcome.value == "inserted":
        ctx.result.provider_references_created += 1
    if kind == "team":
        ctx.teams.add(provider_entity_id)
    elif kind == "player":
        ctx.players.add(provider_entity_id)
    return reference.reference_id


def _bucket(
    ctx: _Ctx, table: str, where: str, params: tuple[Any, ...], outcome: ObservationOutcome
) -> None:
    """Classify one append as unchanged / inserted (first) / changed (superseding).

    Called AFTER the append: a post-insert anchor count > 1 means a predecessor
    existed, so the write is a genuine *change*; == 1 is a first observation.
    """

    if outcome is ObservationOutcome.UNCHANGED:
        ctx.result.records_unchanged += 1
        return
    ctx.result.rows_persisted += 1
    count = ctx.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    if int(count) > 1:
        ctx.result.records_changed += 1
    else:
        ctx.result.records_inserted += 1


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def _persist(
    database: Database,
    fetched: _Fetched,
    include_set: set[str],
    declaration: CapabilityDeclaration,
    tier: BalldontlieTier,
    result: NbaIngestResult,
    tool_version: str,
) -> NbaIngestResult:
    started = time.monotonic_ns()
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=result.command, provider=PROVIDER_BALLDONTLIE, operation="ingest_nba",
                args_json=canonical_json({"includes": sorted(include_set), "tier": tier.value}),
                started_monotonic_ns=started, tool_version=tool_version, sport="nba",
            )
        result.run_id = run.run_id
        raw_repo = SqliteRawResponseRepository(conn)
        raws = _store_all_raws(conn, raw_repo, run.run_id, fetched.responses, result)

        ctx = _Ctx(
            conn=conn, run_id=run.run_id, refs=SqliteProviderReferenceRepository(conn),
            dq=SqliteDataQualityRepository(conn), result=result, raws=raws,
        )
        for game, page in fetched.games:
            norm, reason = _normalize_game(game)
            if norm is None:
                result.records_rejected += 1
                result.note(reason or "invalid game")
                continue
            try:
                with transaction(conn):
                    _persist_one_game(ctx, norm, page, fetched, include_set)
            except Exception as exc:  # noqa: BLE001 - one bad game must not poison the rest
                _t, msg = sanitize_error(exc)
                result.record_active_failure(_t, f"game {norm.game_id}: {msg}")
                result.note(f"game {norm.game_id}: normalization failed ({msg})")

        # Record the conditional capability states (honest declared beliefs) so the
        # corpus captures what NBA data was available (and at which tier) now.
        _record_capabilities(ctx, declaration, tier)

        result.teams_observed = len(ctx.teams)
        result.players_observed = len(ctx.players)
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        run_status = "partially_succeeded" if result.status == "partially_failed" else "succeeded"
        with transaction(conn):
            runs.complete(
                run.run_id, status=run_status, duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made, records_received=result.games_received,
                records_normalized=result.observations_normalized,
                records_inserted=result.records_inserted + result.records_changed,
                records_deduplicated=result.records_unchanged,
                records_rejected=result.records_rejected,
            )
    return result


def _persist_one_game(
    ctx: _Ctx, norm: _NormGame, page: ProviderResponse, fetched: _Fetched, include_set: set[str]
) -> None:
    conn = ctx.conn
    res = ctx.result
    raw_id, raw_hash, observed = ctx.raws[id(page)]
    ingested = to_iso(_now())

    game_ref_id = _ref(ctx, "game", norm.game_id, page)
    for tid in (norm.home_provider_team_id, norm.away_provider_team_id):
        if tid:
            _ref(ctx, "team", tid, page)

    if norm.status_unknown:
        ctx.dq.record(
            severity="issue", rule_code="DQ-NBA-STATUS-001", entity_type="game",
            description=f"unknown NBA status for game {norm.game_id}: {norm.status_raw!r}",
            provider=PROVIDER_BALLDONTLIE, run_id=ctx.run_id, raw_response_id=raw_id,
            entity_id=norm.game_id,
        )
        res.data_quality_issues += 1

    schedule_repo = SqliteScheduleRepository(conn)
    _sid, sched_outcome = schedule_repo.append(
        game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE, provider_game_id=norm.game_id,
        observed_at=observed, ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
        raw_response_hash=raw_hash, mapped_status=norm.mapped_status, season=norm.season,
        game_date_local=norm.date_local, scheduled_start=norm.scheduled_start,
        home_provider_team_id=norm.home_provider_team_id,
        away_provider_team_id=norm.away_provider_team_id, status_code=norm.status_raw,
        detailed_status=norm.status_raw,
    )
    if sched_outcome is ObservationOutcome.INSERTED:
        res.schedule_observations += 1
        res.observations_normalized += 1
    _bucket(ctx, "game_schedule_snapshots", "game_ref_id = ?", (game_ref_id,), sched_outcome)

    if "results" in include_set:
        result_repo = SqliteNbaResultRepository(conn)
        _rid, r_outcome, is_correction = result_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE, provider_game_id=norm.game_id,
            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
            raw_response_hash=raw_hash, mapped_status=norm.mapped_status,
            home_points=norm.home_score, away_points=norm.away_score, period=norm.period,
            winning_side=norm.winning_side, result_detail=norm.status_raw,
        )
        if r_outcome is ObservationOutcome.INSERTED:
            res.result_observations += 1
            res.observations_normalized += 1
            if is_correction:
                res.corrections_appended += 1
        _bucket(ctx, "nba_game_results", "game_ref_id = ?", (game_ref_id,), r_outcome)

    # Box team statistics and derived quarter lines both come from the matched
    # box-score object (quarter/period scores are NOT a documented /v1/games field).
    box_game = _matched_box(ctx, norm, fetched, include_set)
    if box_game is not None:
        box_resp = fetched.box_by_date[norm.date_local]  # type: ignore[index]
        if "box" in include_set:
            _persist_box_team(ctx, norm, game_ref_id, box_resp, box_game, ingested)
        if "quarters" in include_set:
            _persist_box_quarters(ctx, norm, game_ref_id, box_resp, box_game, ingested)

    if "player-stats" in include_set:
        _persist_player_stats(ctx, norm, game_ref_id,
                              fetched.stats_by_game.get(norm.game_id, []), "traditional", ingested)
    if "advanced" in include_set:
        _persist_player_stats(ctx, norm, game_ref_id,
                              fetched.adv_by_game.get(norm.game_id, []), "advanced", ingested)
    if "plays" in include_set:
        _persist_plays(ctx, norm, game_ref_id, fetched.plays_by_game.get(norm.game_id, []), ingested)
    if "lineups" in include_set and norm.game_id in fetched.lineup_by_game:
        _persist_lineups(ctx, norm, game_ref_id, fetched.lineup_by_game[norm.game_id], ingested)


def _matched_box(
    ctx: _Ctx, norm: _NormGame, fetched: "_Fetched", include_set: set[str]
) -> Optional[dict[str, Any]]:
    """Return the box-score object matched to this game, or ``None``.

    Only attempted when box or quarters are requested and a box response exists
    for the game's date. A no-match or ambiguous match records a data-quality note
    (and is never guessed); one game's box is never attached to another game.
    """

    if not (include_set & {"box", "quarters"}):
        return None
    if norm.date_local is None or norm.date_local not in fetched.box_by_date:
        return None
    box_resp = fetched.box_by_date[norm.date_local]
    box_game, reason = _match_box_game(box_resp.data, norm)
    if box_game is not None:
        return box_game
    if reason in ("no_match", "ambiguous"):
        raw_id = ctx.raws[id(box_resp)][0]
        ctx.dq.record(
            severity="note", rule_code="DQ-NBA-BOX-001", entity_type="game",
            description=(
                f"box score {reason} for game {norm.game_id} "
                f"(date={norm.date_local}, home={norm.home_provider_team_id}, "
                f"away={norm.away_provider_team_id}); no box rows attached"
            ),
            provider=PROVIDER_BALLDONTLIE, run_id=ctx.run_id, raw_response_id=raw_id,
            entity_id=norm.game_id,
        )
        ctx.result.data_quality_issues += 1
        ctx.result.records_rejected += 1
    return None


def _persist_box_team(
    ctx: _Ctx, norm: _NormGame, game_ref_id: str, box_resp: ProviderResponse,
    box_game: dict[str, Any], ingested: str,
) -> None:
    raw_id, raw_hash, observed = ctx.raws[id(box_resp)]
    team_repo = SqliteNbaTeamStatRepository(ctx.conn)
    for tr in _normalize_box_team_lines(box_game):
        _ref(ctx, "team", tr.provider_team_id, box_resp)
        _tid, outcome = team_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE, provider_game_id=norm.game_id,
            provider_team_id=tr.provider_team_id, home_away=tr.home_away, points=tr.points,
            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
            raw_response_hash=raw_hash, stats=tr.stats_json,
        )
        if outcome is ObservationOutcome.INSERTED:
            ctx.result.team_stat_observations += 1
            ctx.result.observations_normalized += 1
        _bucket(ctx, "nba_team_statistics", "game_ref_id = ? AND provider_team_id = ?",
                (game_ref_id, tr.provider_team_id), outcome)


def _persist_box_quarters(
    ctx: _Ctx, norm: _NormGame, game_ref_id: str, box_resp: ProviderResponse,
    box_game: dict[str, Any], ingested: str,
) -> None:
    raw_id, raw_hash, observed = ctx.raws[id(box_resp)]
    quarter_repo = SqliteQuarterLineRepository(ctx.conn)
    for q in _parse_box_quarters(box_game):
        _qid, outcome = quarter_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE, provider_game_id=norm.game_id,
            period=q.period, side=q.side, points=q.points, observed_at=observed,
            ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
            raw_response_hash=raw_hash,
        )
        if outcome is ObservationOutcome.INSERTED:
            ctx.result.quarter_observations += 1
            ctx.result.observations_normalized += 1
        _bucket(ctx, "nba_quarter_lines", "game_ref_id = ? AND period = ? AND side = ?",
                (game_ref_id, q.period, q.side), outcome)


def _persist_player_stats(
    ctx: _Ctx, norm: _NormGame, game_ref_id: str, pages: list[ProviderResponse],
    stat_group: str, ingested: str,
) -> None:
    player_repo = SqliteNbaPlayerStatRepository(ctx.conn)
    for page in pages:
        raw_id, raw_hash, observed = ctx.raws[id(page)]
        for row in _rows(page):
            ps = _normalize_stat_row(row, stat_group=stat_group)
            if ps is None:
                ctx.result.records_rejected += 1
                continue
            _ref(ctx, "player", ps.provider_player_id, page)
            _pid, outcome = player_repo.append(
                game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE,
                provider_game_id=norm.game_id, provider_player_id=ps.provider_player_id,
                stat_group=stat_group, observed_at=observed, ingested_at=ingested,
                run_id=ctx.run_id, raw_response_id=raw_id, raw_response_hash=raw_hash,
                provider_team_id=ps.provider_team_id, position=ps.position,
                is_starter=ps.is_starter, points=ps.points, stats=ps.stats_json,
            )
            if outcome is ObservationOutcome.INSERTED:
                if stat_group == "advanced":
                    ctx.result.advanced_stat_observations += 1
                else:
                    ctx.result.player_stat_observations += 1
                ctx.result.observations_normalized += 1
            _bucket(
                ctx, "nba_player_statistics",
                "game_ref_id = ? AND provider_player_id = ? AND stat_group = ?",
                (game_ref_id, ps.provider_player_id, stat_group), outcome,
            )


def _persist_plays(
    ctx: _Ctx, norm: _NormGame, game_ref_id: str, pages: list[ProviderResponse], ingested: str
) -> None:
    play_repo = SqlitePlaySnapshotRepository(ctx.conn)
    ordinal = 0
    for page in pages:
        raw_id, raw_hash, observed = ctx.raws[id(page)]
        for row in _rows(page):
            ordinal += 1
            pl = _normalize_play(row, provider_game_id=norm.game_id, ordinal=ordinal)
            if pl is None:
                ctx.result.records_rejected += 1
                continue
            if pl.provider_player_id:
                _ref(ctx, "player", pl.provider_player_id, page)
            _pid, outcome = play_repo.append(
                game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE,
                provider_game_id=norm.game_id, play_identity=pl.play_identity,
                observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
                raw_response_id=raw_id, raw_response_hash=raw_hash,
                provider_play_id=pl.provider_play_id, period=pl.period,
                play_sequence=pl.play_sequence, clock=pl.clock, event_type=pl.event_type,
                description=pl.description, provider_team_id=pl.provider_team_id,
                provider_player_id=pl.provider_player_id, is_substitution=pl.is_substitution,
                extra=pl.extra_json,
            )
            if outcome is ObservationOutcome.INSERTED:
                ctx.result.play_observations += 1
                ctx.result.observations_normalized += 1
            _bucket(ctx, "play_snapshots", "game_ref_id = ? AND play_identity = ?",
                    (game_ref_id, pl.play_identity), outcome)


def _persist_lineups(
    ctx: _Ctx, norm: _NormGame, game_ref_id: str, lineup_resp: ProviderResponse, ingested: str
) -> None:
    raw_id, raw_hash, observed = ctx.raws[id(lineup_resp)]
    lineup_repo = SqliteLineupRepository(ctx.conn)
    for provider_team_id, players in _parse_lineups(lineup_resp.data, norm.game_id):
        for p in players:
            _ref(ctx, "player", p.provider_player_id, lineup_resp)
        # A posted lineup is NEVER a confirmed pregame starter set: is_confirmed=False.
        _lid, outcome, n_players = lineup_repo.append(
            game_ref_id=game_ref_id, provider=PROVIDER_BALLDONTLIE, provider_game_id=norm.game_id,
            provider_team_id=provider_team_id, players=players, observed_at=observed,
            ingested_at=ingested, run_id=ctx.run_id, raw_response_id=raw_id,
            raw_response_hash=raw_hash, is_confirmed=False,
        )
        if outcome is ObservationOutcome.INSERTED:
            ctx.result.lineup_observations += 1
            ctx.result.lineup_players_observed += n_players
            ctx.result.observations_normalized += 1
        _bucket(ctx, "lineup_snapshots", "game_ref_id = ? AND provider_team_id = ?",
                (game_ref_id, provider_team_id), outcome)


def _record_capabilities(
    ctx: _Ctx, declaration: CapabilityDeclaration, tier: BalldontlieTier
) -> None:
    """Persist the conditional capability states honestly (declared beliefs).

    Each conditional capability is recorded with its declared state and, when it
    is not available at this tier, an accompanying ``DQ-CAP-*`` data-quality note.
    Confirmed pregame starters stay ``unavailable``; correction timestamps stay
    ``unsupported``; NBA history is ``provider_history_limited``.
    """

    cap_repo = SqliteCapabilityRepository(ctx.conn)
    observed_at = to_iso(_now())
    for capability in _CONDITIONAL_CAPABILITIES:
        state = declaration.state(capability)
        with transaction(ctx.conn):
            _record, inserted = cap_repo.record(
                provider=PROVIDER_BALLDONTLIE, tier=tier.value, capability=capability.value,
                state=state.value, observed_at=observed_at, run_id=ctx.run_id,
                declared_state=state.value,
            )
        if inserted:
            ctx.result.capabilities_recorded += 1
        if state in (CapabilityState.PAID_TIER_REQUIRED, CapabilityState.UNAVAILABLE,
                     CapabilityState.UNSUPPORTED):
            with transaction(ctx.conn):
                ctx.dq.record(
                    severity="note", rule_code="DQ-CAP-NBA-001", entity_type="capability",
                    description=(
                        f"NBA capability {capability.value!r} is {state.value} at tier "
                        f"{tier.value!r}; no {capability.value} rows were fabricated"
                    ),
                    provider=PROVIDER_BALLDONTLIE, run_id=ctx.run_id, entity_id=capability.value,
                )
                ctx.result.data_quality_issues += 1


async def _persist_injuries(
    database: Database,
    pages: list[ProviderResponse],
    date: Optional[str],
    tier: BalldontlieTier,
    result: NbaIngestResult,
    tool_version: str,
) -> NbaIngestResult:
    started = time.monotonic_ns()
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=result.command, provider=PROVIDER_BALLDONTLIE, operation="ingest_injuries",
                args_json=canonical_json({"sport": "nba", "date": date, "tier": tier.value}),
                started_monotonic_ns=started, tool_version=tool_version, sport="nba",
            )
        result.run_id = run.run_id
        raw_repo = SqliteRawResponseRepository(conn)
        raws = _store_all_raws(conn, raw_repo, run.run_id, pages, result)
        ctx = _Ctx(
            conn=conn, run_id=run.run_id, refs=SqliteProviderReferenceRepository(conn),
            dq=SqliteDataQualityRepository(conn), result=result, raws=raws,
        )
        injury_repo = SqliteInjurySnapshotRepository(conn)
        ingested = to_iso(_now())
        for page in pages:
            raw_id, raw_hash, observed = ctx.raws[id(page)]
            for row in _rows(page):
                inj = _normalize_injury(row)
                if inj is None:
                    result.records_rejected += 1
                    continue
                try:
                    with transaction(conn):
                        player_ref_id = _ref(ctx, "player", inj.provider_player_id, page)
                        _iid, outcome = injury_repo.append(
                            player_ref_id=player_ref_id, provider=PROVIDER_BALLDONTLIE,
                            provider_player_id=inj.provider_player_id, status=inj.status,
                            observed_at=observed, ingested_at=ingested, run_id=ctx.run_id,
                            raw_response_id=raw_id, raw_response_hash=raw_hash,
                            provider_team_id=inj.provider_team_id, description=inj.description,
                            reason=inj.reason, return_date=inj.return_date,
                            return_estimate=inj.return_estimate,
                        )
                        if outcome is ObservationOutcome.INSERTED:
                            result.injury_observations += 1
                            result.observations_normalized += 1
                        _bucket(ctx, "injury_snapshots", "player_ref_id = ?",
                                (player_ref_id,), outcome)
                except Exception as exc:  # noqa: BLE001
                    _t, msg = sanitize_error(exc)
                    result.record_active_failure(_t, f"injury {inj.provider_player_id}: {msg}")

        result.teams_observed = len(ctx.teams)
        result.players_observed = len(ctx.players)
        result.status = "partially_failed" if result.has_active_failure else "succeeded"
        run_status = "partially_succeeded" if result.status == "partially_failed" else "succeeded"
        with transaction(conn):
            runs.complete(
                run.run_id, status=run_status, duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made, records_received=result.injury_observations,
                records_normalized=result.observations_normalized,
                records_inserted=result.records_inserted + result.records_changed,
                records_deduplicated=result.records_unchanged,
                records_rejected=result.records_rejected,
            )
    return result
