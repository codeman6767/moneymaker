"""Offline hoopR Parquet import boundary (Phase D3 supplement).

A typed, deterministic importer for historical NBA play-by-play exported by
`hoopR <https://hoopr.sportsdataverse.org/>`_ as Parquet. It is a **strictly
offline supplement**:

* no R runtime dependency and no network access -- it reads a local Parquet file;
* an explicit supported schema + version (``hoopr_pbp`` v1); an unsupported or
  malformed schema fails honestly rather than importing garbage;
* exact file-level provenance: the file's SHA-256 (plus row count and schema) is
  recorded as the body of a synthetic append-only ``raw_responses`` row, so every
  imported play traces to the exact file that supplied it;
* a distinct provider identity (``hoopr``) with its OWN provider references, so
  offline hoopR observations never silently mix with live BALLDONTLIE ones;
* idempotent re-import (identical file content -> the transition-aware
  ``play_snapshots`` append writes nothing new); a changed source row is preserved
  as a NEW observation;
* ``--dry-run`` reads + validates + normalizes but persists absolutely nothing;
* missing OPTIONAL columns stay missing (never fabricated).

The importer is deliberately limited to what the typed play boundary supports
reliably (historical plays / substitution events). It claims no coverage beyond
the supported schema and the file actually provided.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.nba import SqlitePlaySnapshotRepository
from ..db.repositories.observations import ObservationOutcome
from ..db.repositories.raw_responses import SqliteRawResponseRepository, response_content_hash
from ..db.repositories.references import SqliteProviderReferenceRepository
from ..db.schema import to_iso, utc_now
from .runner import sanitize_error

#: Provider identity for offline hoopR observations. Deliberately distinct from
#: ``balldontlie`` so the two never share a provider reference or observation.
PROVIDER_HOOPR = "hoopr"

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "import-hoopr"

#: The one Parquet schema this boundary supports, and its version. Anything else
#: is rejected rather than guessed.
SUPPORTED_SCHEMA = "hoopr_pbp"
SUPPORTED_SCHEMA_VERSION = 1

#: Columns a ``hoopr_pbp`` v1 file MUST supply. A file missing any is unsupported.
REQUIRED_COLUMNS: tuple[str, ...] = ("game_id", "sequence_number", "period_number")
#: Columns used when present; their absence stays missing (never fabricated).
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "id", "clock_display_value", "type_text", "text", "team_id", "athlete_id_1",
)


class HooprImportError(RuntimeError):
    """Raised when a hoopR Parquet file is unsupported or malformed."""


@dataclass
class HooprImportResult:
    """Sanitized, deterministic counters for one hoopR import."""

    dry_run: bool
    status: str
    command: str = _COMMAND
    run_id: Optional[str] = None
    file_name: Optional[str] = None
    file_sha256: Optional[str] = None
    schema: str = SUPPORTED_SCHEMA
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    rows_read: int = 0
    plays_normalized: int = 0
    games_observed: int = 0
    players_observed: int = 0
    provider_references_created: int = 0
    observations_normalized: int = 0
    rows_persisted: int = 0
    records_inserted: int = 0
    records_changed: int = 0
    records_unchanged: int = 0
    records_rejected: int = 0
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
# Pure parsing helpers
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


@dataclass(frozen=True)
class _HooprPlay:
    game_id: str
    play_identity: str
    provider_play_id: Optional[str]
    period: Optional[int]
    sequence: Optional[int]
    clock: Optional[str]
    event_type: Optional[str]
    description: Optional[str]
    team_id: Optional[str]
    athlete_id: Optional[str]
    is_substitution: bool
    extra_json: str


def _normalize_row(row: dict[str, Any]) -> Optional[_HooprPlay]:
    """Normalize one hoopR pbp row, or return ``None`` to reject it.

    A row missing its game id or sequence number cannot be identified and is
    rejected (never fabricated). Play identity is the file's play ``id`` when
    supplied, else a deterministic ``<game>:<sequence>`` identity.
    """

    game_id = _opt_str(row.get("game_id"))
    sequence = _opt_int(row.get("sequence_number"))
    if game_id is None or sequence is None:
        return None
    provider_play_id = _opt_str(row.get("id"))
    identity = provider_play_id if provider_play_id is not None else f"{game_id}:{sequence}"
    event_type = _opt_str(row.get("type_text"))
    description = _opt_str(row.get("text"))
    hay = f"{event_type or ''} {description or ''}".lower()
    is_sub = "substitution" in hay or "enters the game" in hay or "subs in" in hay
    extra = {
        k: v for k, v in row.items()
        if k not in ("game_id", "sequence_number", "id", "team_id", "athlete_id_1")
        and v is not None
    }
    return _HooprPlay(
        game_id=game_id,
        play_identity=identity,
        provider_play_id=provider_play_id,
        period=_opt_int(row.get("period_number")),
        sequence=sequence,
        clock=_opt_str(row.get("clock_display_value")),
        event_type=event_type,
        description=description,
        team_id=_opt_str(row.get("team_id")),
        athlete_id=_opt_str(row.get("athlete_id_1")),
        is_substitution=is_sub,
        extra_json=canonical_json(extra),
    )


def _read_parquet(path: Path, *, schema: str, schema_version: int) -> tuple[list[dict[str, Any]], str]:
    """Read + validate a hoopR Parquet file. Returns ``(rows, file_sha256)``.

    Raises :class:`HooprImportError` for an unsupported schema/version or a file
    missing a required column; propagates a read error for a malformed/non-Parquet
    file (the caller sanitizes and reports it as a failure).
    """

    if schema != SUPPORTED_SCHEMA:
        raise HooprImportError(
            f"unsupported hoopR schema {schema!r}; only {SUPPORTED_SCHEMA!r} is supported"
        )
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise HooprImportError(
            f"unsupported hoopR schema version {schema_version}; "
            f"only v{SUPPORTED_SCHEMA_VERSION} is supported"
        )
    if not path.is_file():
        raise HooprImportError(f"hoopR Parquet file not found: {path}")

    file_bytes = path.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    # Local import: no R runtime, and the large Arrow wheel is an OPTIONAL
    # dependency (pyproject `tracking` extra), pulled in only when Parquet import
    # is actually invoked. A missing package is an actionable error, not a crash.
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - exercised only without pyarrow
        raise HooprImportError(
            "hoopR Parquet import requires the optional 'pyarrow' package, which is "
            "not installed. Install it with: pip install \"sports-quant[tracking]\" "
            "(or pip install pyarrow)."
        ) from exc

    table = pq.read_table(path)
    columns = set(table.column_names)
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise HooprImportError(
            f"hoopR {schema!r} file is missing required column(s) {missing}; "
            f"present columns: {sorted(columns)}"
        )
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows, file_sha256


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def import_hoopr_parquet(
    *,
    database: Database,
    path: Path,
    schema: str = SUPPORTED_SCHEMA,
    schema_version: int = SUPPORTED_SCHEMA_VERSION,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> HooprImportResult:
    """Import a hoopR play-by-play Parquet file offline. ``--dry-run`` persists nothing."""

    result = HooprImportResult(dry_run=dry_run, status="succeeded", schema=schema,
                               schema_version=schema_version, file_name=path.name)
    try:
        rows, file_sha256 = _read_parquet(path, schema=schema, schema_version=schema_version)
    except HooprImportError as exc:
        result.status = "failed"
        result.error_type, result.error_message = "HooprImportError", str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - malformed/non-Parquet file
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    result.file_sha256 = file_sha256
    result.rows_read = len(rows)

    plays: list[_HooprPlay] = []
    for row in rows:
        play = _normalize_row(row)
        if play is None:
            result.records_rejected += 1
            result.note("row missing game_id/sequence_number")
            continue
        plays.append(play)
    result.plays_normalized = len(plays)

    if dry_run:
        refs: set[tuple[str, str]] = set()
        for play in plays:
            for kind, eid in (("game", play.game_id),
                              ("player", play.athlete_id) if play.athlete_id else ("", "")):
                if kind and (kind, eid) not in refs:
                    refs.add((kind, eid))
                    result.provider_references_created += 1
            result.observations_normalized += 1
            result.records_inserted += 1
        result.games_observed = len({p.game_id for p in plays})
        result.players_observed = len({p.athlete_id for p in plays if p.athlete_id})
        return result

    return _persist(database, path, plays, file_sha256, schema, schema_version, result, tool_version)


def _persist(
    database: Database,
    path: Path,
    plays: list[_HooprPlay],
    file_sha256: str,
    schema: str,
    schema_version: int,
    result: HooprImportResult,
    tool_version: str,
) -> HooprImportResult:
    started = time.monotonic_ns()
    now_iso = to_iso(utc_now())
    endpoint = f"/hoopr/{schema}/{path.name.replace('?', '_')}"
    request_params = {"schema": schema, "schema_version": schema_version, "file": path.name}
    manifest = canonical_json({
        "provider": PROVIDER_HOOPR, "schema": schema, "schema_version": schema_version,
        "file_name": path.name, "file_sha256": file_sha256, "rows": result.rows_read,
    })
    content_hash = response_content_hash(
        provider=PROVIDER_HOOPR, endpoint=endpoint, request_params=request_params, body=manifest,
    )

    games: set[str] = set()
    players: set[str] = set()
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=result.command, provider=PROVIDER_HOOPR, operation="import_hoopr",
                args_json=canonical_json(request_params), started_monotonic_ns=started,
                tool_version=tool_version, sport="nba",
            )
        result.run_id = run.run_id

        raw_repo = SqliteRawResponseRepository(conn)
        with transaction(conn):
            raw = raw_repo.store(
                run_id=run.run_id, provider=PROVIDER_HOOPR, endpoint=endpoint,
                request_params_json=canonical_json(request_params), http_status=200,
                response_headers_json="{}", requested_at=now_iso, received_at=now_iso,
                elapsed_ns=0, body=manifest, content_hash=content_hash,
                content_type="application/json",
            )
        raw_id, raw_hash, observed = raw.raw_response_id, content_hash, raw.received_at

        refs = SqliteProviderReferenceRepository(conn)
        play_repo = SqlitePlaySnapshotRepository(conn)

        def ref(kind: str, entity_id: str) -> str:
            reference, outcome = refs.upsert(
                kind=kind, provider=PROVIDER_HOOPR, provider_entity_id=entity_id,
                raw_response_id=raw_id, raw_response_hash=raw_hash, observed_at=observed,
            )
            if outcome.value == "inserted":
                result.provider_references_created += 1
            return reference.reference_id

        for play in plays:
            try:
                with transaction(conn):
                    games.add(play.game_id)
                    game_ref_id = ref("game", play.game_id)
                    if play.athlete_id:
                        players.add(play.athlete_id)
                        ref("player", play.athlete_id)
                    _pid, outcome = play_repo.append(
                        game_ref_id=game_ref_id, provider=PROVIDER_HOOPR,
                        provider_game_id=play.game_id, play_identity=play.play_identity,
                        observed_at=observed, ingested_at=now_iso, run_id=run.run_id,
                        raw_response_id=raw_id, raw_response_hash=raw_hash,
                        provider_play_id=play.provider_play_id, period=play.period,
                        play_sequence=play.sequence, clock=play.clock,
                        event_type=play.event_type, description=play.description,
                        provider_team_id=play.team_id, provider_player_id=play.athlete_id,
                        is_substitution=play.is_substitution, extra=play.extra_json,
                    )
                    if outcome is ObservationOutcome.UNCHANGED:
                        result.records_unchanged += 1
                    else:
                        result.observations_normalized += 1
                        result.rows_persisted += 1
                        count = conn.execute(
                            "SELECT COUNT(*) FROM play_snapshots "
                            "WHERE game_ref_id = ? AND play_identity = ?",
                            (game_ref_id, play.play_identity),
                        ).fetchone()[0]
                        if int(count) > 1:
                            result.records_changed += 1
                        else:
                            result.records_inserted += 1
            except Exception as exc:  # noqa: BLE001
                _t, msg = sanitize_error(exc)
                result.records_rejected += 1
                result.note(f"play {play.play_identity}: {msg}")

        result.games_observed = len(games)
        result.players_observed = len(players)
        with transaction(conn):
            runs.complete(
                run.run_id, status="succeeded", duration_ns=time.monotonic_ns() - started,
                requests_made=0, records_received=result.rows_read,
                records_normalized=result.observations_normalized,
                records_inserted=result.records_inserted + result.records_changed,
                records_deduplicated=result.records_unchanged,
                records_rejected=result.records_rejected,
            )
    return result
