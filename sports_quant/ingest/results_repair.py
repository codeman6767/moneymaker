"""Offline replay of the NBA ``results`` family from preserved raw responses.

Why this exists
---------------
``results`` is a valid NBA include the ingestor implements end to end, and
``nba_game_results`` is the ONLY table :meth:`AsOfReader.official_result` reads,
so it is the sole label source of the point-in-time dataset. The family was
nevertheless missing from the planner's NBA vocabulary
(:data:`~sports_quant.ingest.planning.NBA_RICH_FAMILIES`), so no NBA manifest
could declare it and the executed March 2026 month run produced **zero** result
rows -- see ``F1_NBA_2026_03_EXECUTION_REVIEW.md`` §5.

Unlike MLB -- whose results need their own per-game linescore call -- an NBA game
result is normalized entirely from the ``/v1/games/{id}`` payload the plan
*already* fetched and preserved. So the gap is repairable **offline**, with no
provider request at all.

What this module guarantees
---------------------------
* **Zero network.** No provider client is constructed, no settings or credential
  is loaded, no URL is accepted. The only inputs are an existing SQLite database,
  its own preserved ``raw_responses`` rows, and a committed manifest file.
* **Production normalization.** Rows are produced by the same
  :func:`~sports_quant.ingest.nba_ingestor._normalize_game` and written by the
  same :class:`~sports_quant.db.repositories.nba.SqliteNbaResultRepository` the
  live ingestor uses. There is no ad hoc INSERT anywhere in this module.
* **No fabricated provenance.** ``observed_at``/``ingested_at`` are the preserved
  response's own ``received_at`` -- never the replay wall clock -- and
  ``raw_response_id``/``raw_response_hash``/``run_id`` are the preserved
  response's own. A replayed row is therefore indistinguishable in provenance
  from one the original run would have written, because it derives from exactly
  the same bytes observed at exactly the same instant.
* **Fail closed.** Every ambiguity is refused, never resolved by insertion order.
* **Idempotent.** The repository's content-hash and UNIQUE backstops mean a
  second identical replay inserts nothing.

This is deliberately narrow: a results-only replay for one league from one
committed manifest. It is **not** a general raw-response execution framework.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..db.engine import Database, transaction
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.nba import SqliteNbaResultRepository
from ..db.repositories.observations import ObservationOutcome
from ..db.schema import utc_now_iso
from .manifest import ManifestError, load_and_validate
from .nba_ingestor import _normalize_game

#: Command name and a versioned contract identifier recorded in the provenance.
REPAIR_COMMAND = "repair-nba-results-from-raw"
REPAIR_CONTRACT_VERSION = "nba-results-offline-replay-v1"
REPAIR_TOOL_VERSION = "sports_quant 0.1.0"

#: The only (provider, league) pair this repair understands. Anything else is a
#: different normalizer and a different result table; refuse rather than guess.
SUPPORTED_PROVIDER = "balldontlie"
SUPPORTED_LEAGUE = "nba"

#: A single durable in-database marker that these result rows came from an
#: offline replay rather than a live results fetch. Severity ``note``: nothing is
#: wrong with the data, but its origin must never have to be inferred.
REPAIR_RULE_CODE = "DQ-NBA-RESULT-REPLAY-001"

#: ``/v1/games/{id}`` -- the single-game endpoint whose preserved bodies carry the
#: final score. The paginated ``/v1/games`` listing is deliberately NOT used: the
#: per-game response is the one the rich unit fetched for that specific game.
_SINGLE_GAME_PREFIX = "/v1/games/"


class ResultsRepairError(RuntimeError):
    """The repair refused to run. Message is sanitized and secret-free."""


@dataclass(frozen=True)
class _Candidate:
    """One preserved response normalized into a prospective result observation."""

    provider_game_id: str
    game_ref_id: str
    raw_response_id: str
    raw_response_hash: str
    observed_at: str
    run_id: Optional[str]
    home_points: int
    away_points: int
    period: Optional[int]
    winning_side: str
    mapped_status: str
    result_detail: Optional[str]
    home_provider_team_id: str
    away_provider_team_id: str
    game_date_local: Optional[str]

    def semantic_tuple(self) -> list[Any]:
        """The fields that define this result, for the deterministic run hash."""

        return [self.provider_game_id, self.home_provider_team_id,
                self.away_provider_team_id, self.home_points, self.away_points,
                self.winning_side, self.period, self.mapped_status,
                self.observed_at, self.raw_response_hash]


@dataclass
class RepairResult:
    """Sanitized, deterministic counters for one repair invocation."""

    command: str = REPAIR_COMMAND
    contract_version: str = REPAIR_CONTRACT_VERSION
    tool_version: str = REPAIR_TOOL_VERSION
    dry_run: bool = True
    database_path: str = ""
    manifest_path: str = ""
    manifest_hash: str = ""
    plan_hash: str = ""
    provider: str = SUPPORTED_PROVIDER
    league: str = SUPPORTED_LEAGUE
    date_range: str = ""
    schema_version: int = 0
    #: Preserved evidence examined.
    raw_responses_total: int = 0
    single_game_responses: int = 0
    selected_games: int = 0
    #: Normalization outcome.
    candidates: int = 0
    valid_results: int = 0
    rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    #: Persistence outcome (all zero in dry-run).
    results_inserted: int = 0
    results_unchanged: int = 0
    corrections_appended: int = 0
    raw_responses_inserted: int = 0
    provenance_notes_written: int = 0
    #: State.
    results_before: int = 0
    results_after: int = 0
    already_complete: bool = False
    database_mutated: bool = False
    checkpoint_mutated: bool = False
    network_occurred: bool = False
    provider_client_constructed: bool = False
    #: Deterministic digest of the normalized result set; stable across runs and
    #: independent of row insertion order.
    semantic_result_hash: str = ""
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def note(self, reason: str) -> None:
        self.rejected += 1
        if len(self.rejections) < 50:
            self.rejections.append(reason)


# --------------------------------------------------------------------------- #
# Guards -- every one of these runs BEFORE the database is opened for writing
# --------------------------------------------------------------------------- #
def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths are the same file, by identity not by spelling."""

    try:
        if a.resolve() == b.resolve():
            return True
    except OSError:
        pass
    try:
        sa, sb = a.lstat(), b.lstat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _check_paths(database_path: Path, forbidden_paths: Iterable[Path]) -> None:
    if database_path.is_symlink():
        raise ResultsRepairError(
            f"refusing to write through a symlink: {database_path}")
    if not database_path.exists():
        raise ResultsRepairError(f"database does not exist: {database_path}")
    if database_path.is_dir():
        raise ResultsRepairError(f"database path is a directory: {database_path}")
    if not os.access(database_path, os.W_OK):
        raise ResultsRepairError(f"database is not writable: {database_path}")
    for forbidden in forbidden_paths:
        forbidden = Path(forbidden)
        if forbidden.exists() and _same_file(database_path, forbidden):
            raise ResultsRepairError(
                "refusing to repair a protected/frozen artifact: the target "
                f"aliases {forbidden}")


def _check_request(*, provider: str, league: str, date_range: str, offline: bool) -> None:
    if not offline:
        raise ResultsRepairError(
            "this repair is offline-only; pass --offline to acknowledge that it "
            "makes no provider request")
    if provider != SUPPORTED_PROVIDER:
        raise ResultsRepairError(
            f"unsupported provider {provider!r}; this repair only understands "
            f"{SUPPORTED_PROVIDER!r}")
    if league != SUPPORTED_LEAGUE:
        raise ResultsRepairError(
            f"unsupported league {league!r}; this repair only understands "
            f"{SUPPORTED_LEAGUE!r}")
    if not date_range.strip():
        raise ResultsRepairError("an explicit --date-range is required")


def _load_manifest(manifest_path: Path, *, provider: str, league: str,
                   date_range: str) -> Any:
    try:
        manifest = load_and_validate(manifest_path, expected_league=league,
                                     expected_provider=provider)
    except ManifestError as exc:
        raise ResultsRepairError(f"manifest rejected: {exc}") from None
    if manifest.date_range != date_range:
        raise ResultsRepairError(
            f"manifest date_range {manifest.date_range!r} does not match the "
            f"requested {date_range!r}; refusing to repair a different slice")
    return manifest


def _range_bounds(date_range: str) -> tuple[str, str]:
    start, _sep, end = date_range.partition("..")
    return start, (end or start)


def _check_database_binding(conn: sqlite3.Connection, manifest: Any,
                            result: RepairResult) -> None:
    """Refuse a database that is not the one this manifest's plan produced.

    There is no manifest hash stored in the corpus, so the binding is structural:
    the schema the manifest declares, the provider and league every run recorded,
    and every selected game falling inside the manifest's own date range. A
    database from a different slice, league, provider or schema fails here --
    before a single row is written.
    """

    version = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    result.schema_version = int(version or 0)
    if result.schema_version != manifest.expected_schema_version:
        raise ResultsRepairError(
            f"database schema v{result.schema_version} does not match the "
            f"manifest's declared v{manifest.expected_schema_version}")

    providers = {r[0] for r in conn.execute("SELECT DISTINCT provider FROM raw_responses")}
    if providers - {manifest.provider}:
        raise ResultsRepairError(
            "database holds responses from a provider this manifest does not "
            f"declare: {sorted(providers - {manifest.provider})}")

    runs = conn.execute(
        "SELECT DISTINCT sport, command FROM ingestion_runs").fetchall()
    unexpected = [(s, c) for s, c in runs if s != manifest.league or c != "ingest-nba"]
    if unexpected:
        raise ResultsRepairError(
            f"database holds ingestion runs outside this manifest's league/command: "
            f"{sorted(unexpected)}")

    start, end = _range_bounds(manifest.date_range)
    outside = conn.execute(
        "SELECT COUNT(*) FROM game_schedule_snapshots "
        "WHERE game_date_local IS NULL OR game_date_local < ? OR game_date_local > ?",
        (start, end)).fetchone()[0]
    if outside:
        raise ResultsRepairError(
            f"{outside} scheduled game(s) fall outside the manifest range "
            f"{manifest.date_range}; this database is not bound to that plan")


def _check_no_conflicting_results(conn: sqlite3.Connection) -> int:
    """Existing results are fine (idempotent replay); *contradictory* ones are not.

    Two different result contents recorded for one game at the same observation
    time cannot both be true and there is no deterministic rule to pick one, so
    the repair refuses rather than appending a third interpretation.
    """

    conflicts = conn.execute(
        "SELECT game_ref_id, observed_at, COUNT(DISTINCT content_hash) n "
        "FROM nba_game_results GROUP BY game_ref_id, observed_at HAVING n > 1"
    ).fetchall()
    if conflicts:
        raise ResultsRepairError(
            f"{len(conflicts)} game(s) already hold conflicting result observations "
            "at one observation time; resolve them before replaying")
    return conn.execute("SELECT COUNT(*) FROM nba_game_results").fetchone()[0]


# --------------------------------------------------------------------------- #
# Candidate construction (pure; no I/O beyond the read-only queries above)
# --------------------------------------------------------------------------- #
def _collect_candidates(conn: sqlite3.Connection, result: RepairResult) -> list[_Candidate]:
    """Normalize every preserved selected-game response into a result candidate.

    Deterministic: responses are grouped by provider game id and the output is
    ordered by that id, so the outcome never depends on row or payload order.
    Anything ambiguous or incomplete is refused and counted, never guessed.
    """

    refs = {r["provider_game_id"]: r["reference_id"] for r in conn.execute(
        "SELECT provider_game_id, reference_id FROM provider_game_references")}
    result.selected_games = len(refs)
    result.raw_responses_total = conn.execute(
        "SELECT COUNT(*) FROM raw_responses").fetchone()[0]

    rows = conn.execute(
        "SELECT raw_response_id, run_id, endpoint, body, body_hash, received_at "
        "FROM raw_responses WHERE endpoint LIKE ? ORDER BY raw_response_id",
        (f"{_SINGLE_GAME_PREFIX}%",)).fetchall()
    result.single_game_responses = len(rows)

    # Group first, so a duplicated game is judged as a whole rather than by
    # whichever row the cursor happened to return first.
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        payload = _payload_of(row["body"])
        gid = None if payload is None else _provider_game_id(payload)
        if gid is None:
            result.note(f"response {row['raw_response_id']}: no usable provider game id")
            continue
        grouped.setdefault(gid, []).append(row)

    candidates: list[_Candidate] = []
    for gid in sorted(grouped, key=lambda g: (len(g), g)):
        group = grouped[gid]
        ref = refs.get(gid)
        if ref is None:
            result.note(f"game {gid}: response present but the game is not selected")
            continue
        built = [_build(row, gid, ref, result) for row in group]
        usable = [c for c in built if c is not None]
        if not usable:
            continue
        if len(usable) > 1:
            by_time: dict[str, set[str]] = {}
            for cand in usable:
                by_time.setdefault(cand.observed_at, set()).add(
                    _canonical_hash(cand.semantic_tuple()))
            ambiguous = [t for t, hashes in by_time.items() if len(hashes) > 1]
            if ambiguous:
                # Equal observation time, different content, no correction rule
                # that could order them: refuse the game outright.
                result.note(
                    f"game {gid}: {len(ambiguous)} observation time(s) carry "
                    "conflicting response bodies; refusing to choose one")
                continue
            distinct = {_canonical_hash(c.semantic_tuple()) for c in usable}
            if len(distinct) > 1:
                result.note(
                    f"game {gid}: {len(usable)} preserved responses disagree; "
                    "refusing to choose one")
                continue
        candidates.append(usable[0])

    result.candidates = len(grouped)
    result.valid_results = len(candidates)
    return candidates


def _payload_of(body: Optional[str]) -> Optional[dict[str, Any]]:
    if not body:
        return None
    try:
        data = json.loads(body).get("data")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _provider_game_id(payload: dict[str, Any]) -> Optional[str]:
    value = payload.get("id")
    return None if value is None else str(value)


def _build(row: sqlite3.Row, gid: str, game_ref_id: str,
           result: RepairResult) -> Optional[_Candidate]:
    """Normalize one preserved response, or refuse it with a sanitized reason."""

    payload = _payload_of(row["body"])
    if payload is None:
        result.note(f"game {gid}: response body is not a usable game object")
        return None
    norm, reason = _normalize_game(payload)          # the PRODUCTION normalizer
    if norm is None:
        result.note(f"game {gid}: normalization refused ({reason})")
        return None
    if not row["raw_response_id"] or not row["body_hash"] or not row["received_at"]:
        result.note(f"game {gid}: preserved response is missing provenance")
        return None
    if norm.home_provider_team_id is None or norm.away_provider_team_id is None:
        result.note(f"game {gid}: response is missing a provider team identity")
        return None
    if norm.home_score is None or norm.away_score is None:
        # Never coerce a missing side to zero and never persist a half score.
        result.note(f"game {gid}: final score is missing or asymmetric")
        return None
    if norm.mapped_status != "final":
        result.note(f"game {gid}: status is {norm.mapped_status!r}, not final")
        return None
    if norm.winning_side not in ("home", "away"):
        # NBA games cannot end tied; a tie means the payload is not a real final.
        result.note(f"game {gid}: final score is tied or has no winner")
        return None
    return _Candidate(
        provider_game_id=gid,
        game_ref_id=game_ref_id,
        raw_response_id=str(row["raw_response_id"]),
        raw_response_hash=str(row["body_hash"]),
        # PRESERVED observation time -- the instant the provider's own response
        # arrived, never the replay wall clock.
        observed_at=str(row["received_at"]),
        run_id=row["run_id"],
        home_points=norm.home_score,
        away_points=norm.away_score,
        period=norm.period,
        winning_side=norm.winning_side,
        mapped_status=norm.mapped_status,
        result_detail=norm.status_raw,
        home_provider_team_id=norm.home_provider_team_id,
        away_provider_team_id=norm.away_provider_team_id,
        game_date_local=norm.date_local,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def repair_nba_results_from_raw(
    *,
    database_path: Path,
    manifest_path: Path,
    provider: str,
    league: str,
    date_range: str,
    offline: bool = False,
    dry_run: bool = True,
    forbidden_paths: tuple[Path, ...] = (),
) -> RepairResult:
    """Replay the NBA ``results`` family from preserved responses. No network.

    ``dry_run`` performs the identical read, normalization, binding and ambiguity
    checks and writes absolutely nothing.
    """

    started = utc_now_iso()
    database_path, manifest_path = Path(database_path), Path(manifest_path)
    _check_request(provider=provider, league=league, date_range=date_range,
                   offline=offline)
    manifest = _load_manifest(manifest_path, provider=provider, league=league,
                              date_range=date_range)
    _check_paths(database_path, forbidden_paths)

    result = RepairResult(
        dry_run=dry_run,
        database_path=str(database_path),
        manifest_path=str(manifest_path),
        manifest_hash=manifest.manifest_hash(),
        plan_hash=manifest.computed_plan_hash(),
        provider=provider,
        league=league,
        date_range=date_range,
        started_at=started,
    )

    # ---- read-only inspection ------------------------------------------- #
    ro = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        _check_database_binding(ro, manifest, result)
        result.results_before = _check_no_conflicting_results(ro)
        candidates = _collect_candidates(ro, result)
    finally:
        ro.close()

    result.semantic_result_hash = _canonical_hash(
        [c.semantic_tuple() for c in candidates])

    if result.rejected:
        raise ResultsRepairError(
            f"{result.rejected} preserved response(s) could not be replayed "
            f"unambiguously: {'; '.join(result.rejections[:5])}")

    if dry_run:
        result.results_after = result.results_before
        result.already_complete = result.results_before >= result.valid_results > 0
        result.finished_at = utc_now_iso()
        return result

    # ---- persistence: production repository only, one transaction -------- #
    database = Database(database_path)
    raw_before = _scalar(database, "SELECT COUNT(*) FROM raw_responses")
    with database.connection() as conn:
        with transaction(conn):
            repo = SqliteNbaResultRepository(conn)
            for cand in candidates:
                _rid, outcome, is_correction = repo.append(
                    game_ref_id=cand.game_ref_id,
                    provider=provider,
                    provider_game_id=cand.provider_game_id,
                    observed_at=cand.observed_at,
                    # The corpus's ingestion time for a replayed observation is the
                    # same preserved instant: inventing a later one would imply the
                    # row was learned later than it actually was.
                    ingested_at=cand.observed_at,
                    run_id=cand.run_id,
                    raw_response_id=cand.raw_response_id,
                    raw_response_hash=cand.raw_response_hash,
                    mapped_status=cand.mapped_status,
                    home_points=cand.home_points,
                    away_points=cand.away_points,
                    period=cand.period,
                    winning_side=cand.winning_side,
                    result_detail=cand.result_detail,
                )
                if outcome is ObservationOutcome.INSERTED:
                    result.results_inserted += 1
                    result.corrections_appended += int(is_correction)
                else:
                    result.results_unchanged += 1
            if result.results_inserted:
                SqliteDataQualityRepository(conn).record(
                    severity="note", rule_code=REPAIR_RULE_CODE, entity_type="repair",
                    provider=provider, entity_id=date_range,
                    description=(
                        f"{result.results_inserted} NBA result observation(s) for "
                        f"{date_range} were populated by an OFFLINE replay of "
                        f"preserved {_SINGLE_GAME_PREFIX}{{id}} responses "
                        f"({REPAIR_CONTRACT_VERSION}); no provider request was made "
                        "and each row carries its source response's own observation "
                        "time and provenance"),
                    detail_json=json.dumps({
                        "command": REPAIR_COMMAND,
                        "contract_version": REPAIR_CONTRACT_VERSION,
                        "tool_version": REPAIR_TOOL_VERSION,
                        "manifest_hash": result.manifest_hash,
                        "plan_hash": result.plan_hash,
                        "source_single_game_responses": result.single_game_responses,
                        "results_inserted": result.results_inserted,
                        "semantic_result_hash": result.semantic_result_hash,
                        "network_occurred": False,
                    }, sort_keys=True),
                )
                result.provenance_notes_written = 1

    result.raw_responses_inserted = (
        _scalar(database, "SELECT COUNT(*) FROM raw_responses") - raw_before)
    result.results_after = _scalar(database, "SELECT COUNT(*) FROM nba_game_results")
    result.database_mutated = bool(result.results_inserted)
    result.already_complete = result.results_inserted == 0
    result.finished_at = utc_now_iso()
    return result


def _scalar(database: Database, sql: str) -> int:
    with database.connection() as conn:
        return int(conn.execute(sql).fetchone()[0])


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_repair(result: RepairResult, out: Any) -> None:
    """Human-readable summary. Prints no path secret and no response body."""

    mode = "DRY-RUN" if result.dry_run else "APPLIED"
    state = "already complete" if result.already_complete else "repaired"
    out(f"repair-nba-results-from-raw  {mode}  {result.league.upper()} "
        f"{result.date_range}  [{state}]  (offline; no provider request)")
    out(f"  binding:   manifest={result.manifest_hash[:16]}… "
        f"plan={result.plan_hash[:16]}… schema=v{result.schema_version}")
    out(f"  evidence:  raw_responses={result.raw_responses_total} "
        f"single_game={result.single_game_responses} "
        f"selected_games={result.selected_games}")
    out(f"  normalize: candidates={result.candidates} valid={result.valid_results} "
        f"rejected={result.rejected}")
    out(f"  results:   before={result.results_before} inserted={result.results_inserted} "
        f"unchanged={result.results_unchanged} after={result.results_after} "
        f"corrections={result.corrections_appended}")
    out(f"  isolation: database_mutated={result.database_mutated} "
        f"checkpoint_mutated={result.checkpoint_mutated} "
        f"raw_responses_inserted={result.raw_responses_inserted} "
        f"provenance_notes={result.provenance_notes_written}")
    out(f"  offline:   network_occurred={result.network_occurred} "
        f"provider_client_constructed={result.provider_client_constructed}")
    out(f"  digest:    semantic_result_hash={result.semantic_result_hash[:32]}…")
