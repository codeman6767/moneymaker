"""Targeted, bounded recovery of the NBA lineups the March 2026 month run cut short.

Why this exists
---------------
``/v1/lineups`` is fetched with exactly ONE request per game -- the reviewed month
plan reserves ``per_parent_max=1`` for it -- so when a response came back at the
``per_page=25`` ceiling with a live ``meta.next_cursor``, the remainder of that
game's lineup was simply never fetched. The independent execution review
(``F1_NBA_2026_03_EXECUTION_REVIEW.md`` §8/§9) found this on **40 of 239** games
and recorded it as a correctness blocker; ``DQ-NBA-LINEUP-002`` is the durable
historical marker that those stored lineups are partial.

Recovering the missing rows genuinely needs the provider, so this module prepares
that live step and nothing else. It is deliberately narrow:

* it fetches **continuation pages only** -- never a first page, which the corpus
  already holds, and never any other endpoint;
* the target set and each game's starting cursor are **re-derived from the
  protected March database at execution time**, so no cursor value has to be
  committed and the run cannot drift from the evidence it claims to extend;
* it writes to a **separate recovery database**, never into the executed March
  corpus, whose database, checkpoint, manifest and logs stay historical evidence;
* every stop condition fails closed, and a run cannot report success while any
  target game's cursor chain is unfinished.

Applying the recovered pages to the March corpus is a **later, separately
authorized offline merge**. This module does not merge anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: Endpoint whose first pages the month run preserved and whose continuations
#: this recovery fetches.
LINEUPS_ENDPOINT = "/v1/lineups"

#: The provider page size the month run used, and which the continuation must
#: keep so page boundaries line up with the preserved first page.
LINEUPS_PER_PAGE = 25

#: Hard per-target bound on CONTINUATION pages (the first page is already held).
MAX_CONTINUATION_PAGES = 8

#: Versioned identity of this recovery contract; part of the plan/manifest hash.
RECOVERY_PURPOSE = "lineup_continuation_recovery"
RECOVERY_CONTRACT_VERSION = "nba-lineup-continuation-v1"

SUPPORTED_PROVIDER = "balldontlie"
SUPPORTED_LEAGUE = "nba"


class LineupContinuationError(RuntimeError):
    """Target derivation or binding refused. Messages are sanitized."""


@dataclass(frozen=True)
class LineupTarget:
    """One game whose preserved first lineup page advertised more records."""

    provider_game_id: str
    #: The preserved first page that supplies the starting cursor -- the run's
    #: anchor into the historical evidence.
    first_raw_response_id: str
    first_raw_response_hash: str
    first_observed_at: str
    first_page_rows: int
    first_page_teams: int
    first_page_players: int
    first_page_starters: int
    #: ``meta.next_cursor`` from that first page. Never fabricated; a target
    #: without one is not a target.
    start_cursor: int

    def digest_tuple(self) -> list[Any]:
        """The fields that identify this target for the deterministic digest.

        The cursor participates: a recovery bound to a different starting point
        is a different recovery, and the manifest must not silently accept it.
        """

        return [self.provider_game_id, self.first_raw_response_hash,
                self.first_observed_at, self.first_page_rows, self.start_cursor]


@dataclass
class LineupSurvey:
    """What every selected game's first lineup page actually looks like."""

    selected_games: int = 0
    games_with_first_page: int = 0
    games_missing_first_page: list[str] = field(default_factory=list)
    games_with_duplicate_first_page: list[str] = field(default_factory=list)
    complete_games: int = 0
    targets: tuple[LineupTarget, ...] = ()

    @property
    def target_count(self) -> int:
        return len(self.targets)

    def target_digest(self) -> str:
        return target_set_digest(self.targets)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["targets"] = [asdict(t) for t in self.targets]
        out["target_count"] = self.target_count
        out["target_digest"] = self.target_digest()
        return out


def target_set_digest(targets: Iterable[LineupTarget]) -> str:
    """Order-independent digest of a target set.

    Targets are sorted canonically first, so the digest depends on WHICH games
    are being recovered and from which cursor -- never on the order a cursor
    happened to return them.
    """

    ordered = sorted(targets, key=lambda t: _canonical_id_key(t.provider_game_id))
    payload = json.dumps([t.digest_tuple() for t in ordered], sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_id_key(value: str) -> tuple[int, str]:
    """Numeric-then-lexicographic ordering, so ``id 9`` precedes ``id 10``."""

    return (len(value), value)


# --------------------------------------------------------------------------- #
# Target derivation -- READ-ONLY against the protected March database
# --------------------------------------------------------------------------- #
def _next_cursor_of(body: Optional[str]) -> tuple[Optional[int], bool]:
    """``(cursor, parsed_ok)`` from a preserved response body.

    A malformed or hostile body yields ``(None, False)`` rather than raising:
    the caller must be able to report it, not crash on it.
    """

    if not body:
        return None, False
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None, False
    if not isinstance(payload, dict):
        return None, False
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None, True
    raw = meta.get("next_cursor")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return None, True
    return raw, True


def _page_shape(body: Optional[str]) -> tuple[int, int, int, int]:
    """``(rows, teams, players, starters)`` for one preserved lineup page.

    Counts only; no player name, no body text and nothing else identifying is
    returned, so a survey can be printed and stored safely.
    """

    try:
        rows = json.loads(body or "").get("data") or []
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError,
            RecursionError):
        return 0, 0, 0, 0
    if not isinstance(rows, list):
        return 0, 0, 0, 0
    teams: set[str] = set()
    players: set[str] = set()
    starters = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        team = row.get("team")
        if isinstance(team, dict) and team.get("id") is not None:
            teams.add(str(team["id"]))
        player = row.get("player")
        if isinstance(player, dict) and player.get("id") is not None:
            players.add(str(player["id"]))
        if row.get("starter") is True:
            starters += 1
    return len(rows), len(teams), len(players), starters


def survey_lineup_pages(conn: sqlite3.Connection) -> LineupSurvey:
    """Survey every selected game's preserved first lineup page.

    Deterministic and order-independent: responses are grouped by the game id in
    their own request parameters, and the result is emitted in canonical game-id
    order. Nothing is selected because of the order a cursor returned it.
    """

    conn.row_factory = sqlite3.Row
    selected = {r["provider_game_id"] for r in conn.execute(
        "SELECT provider_game_id FROM provider_game_references")}

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
            "SELECT raw_response_id, request_params_json, body, body_hash, "
            "received_at FROM raw_responses WHERE endpoint = ? "
            "ORDER BY raw_response_id", (LINEUPS_ENDPOINT,)):
        gid = _game_id_of_request(row["request_params_json"])
        if gid is None:
            continue
        grouped.setdefault(gid, []).append(row)

    survey = LineupSurvey(selected_games=len(selected))
    targets: list[LineupTarget] = []
    for gid in sorted(selected, key=_canonical_id_key):
        pages = grouped.get(gid, [])
        if not pages:
            survey.games_missing_first_page.append(gid)
            continue
        if len(pages) > 1:
            # The month run made exactly one lineups request per game, so more
            # than one preserved page for a game is an ambiguity this recovery
            # must not resolve by picking whichever row sorted first.
            distinct = {(p["body_hash"], p["received_at"]) for p in pages}
            if len(distinct) > 1:
                survey.games_with_duplicate_first_page.append(gid)
                continue
        page = pages[0]
        survey.games_with_first_page += 1
        cursor, parsed = _next_cursor_of(page["body"])
        if not parsed:
            survey.games_with_duplicate_first_page.append(gid)
            continue
        if cursor is None:
            survey.complete_games += 1
            continue
        rows_n, teams_n, players_n, starters_n = _page_shape(page["body"])
        targets.append(LineupTarget(
            provider_game_id=gid,
            first_raw_response_id=str(page["raw_response_id"]),
            first_raw_response_hash=str(page["body_hash"]),
            first_observed_at=str(page["received_at"]),
            first_page_rows=rows_n,
            first_page_teams=teams_n,
            first_page_players=players_n,
            first_page_starters=starters_n,
            start_cursor=cursor,
        ))
    survey.targets = tuple(targets)
    return survey


def _game_id_of_request(params_json: Optional[str]) -> Optional[str]:
    """The game id a preserved lineups request was for.

    Handles both encodings of a repeated query parameter: the pre-repair
    stringified container (``"[18447686]"``) that the March corpus holds, and the
    faithful list form written since. See the execution review's D5 finding.
    """

    try:
        params = json.loads(params_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    value = params.get("game_ids[]", params.get("game_id"))
    if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, list):
        return str(value[0]) if len(value) == 1 else None
    return None if value is None else str(value)


def derive_targets(
    database_path: Path, *, expected_targets: Optional[int] = None,
    expected_digest: Optional[str] = None,
    expected_selected_games: Optional[int] = None,
) -> LineupSurvey:
    """Derive the recovery target set from the protected source, read-only.

    The database is opened ``mode=ro``; this never writes to the March corpus.
    When the caller supplies expectations (from a committed manifest) they are
    enforced here, BEFORE any client or network work could occur, so a run whose
    evidence has moved refuses instead of recovering the wrong games.
    """

    database_path = Path(database_path)
    if not database_path.exists():
        raise LineupContinuationError(f"source database does not exist: {database_path}")
    conn = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        survey = survey_lineup_pages(conn)
    finally:
        conn.close()

    if survey.games_missing_first_page:
        raise LineupContinuationError(
            f"{len(survey.games_missing_first_page)} selected game(s) have no "
            "preserved first lineup page; the continuation cannot be anchored")
    if survey.games_with_duplicate_first_page:
        raise LineupContinuationError(
            f"{len(survey.games_with_duplicate_first_page)} game(s) have an "
            "ambiguous or unreadable first lineup page; refusing to choose one")
    if expected_selected_games is not None and survey.selected_games != expected_selected_games:
        raise LineupContinuationError(
            f"source holds {survey.selected_games} selected games, expected "
            f"{expected_selected_games}; this is not the bound corpus")
    if expected_targets is not None and survey.target_count != expected_targets:
        raise LineupContinuationError(
            f"derived {survey.target_count} continuation target(s), expected "
            f"{expected_targets}; refusing to run against changed evidence")
    if expected_digest is not None and survey.target_digest() != expected_digest:
        raise LineupContinuationError(
            "derived target-set digest does not match the manifest; the source "
            "evidence has changed since the manifest was reviewed")
    return survey


# --------------------------------------------------------------------------- #
# Continuation execution
# --------------------------------------------------------------------------- #
#: Durable findings for the RECOVERY run. `DQ-NBA-LINEUP-002` stays the historical
#: signal on the March corpus that a first page was partial; these describe what
#: the recovery itself encountered and are never written over that note.
DQ_REPEATED_CURSOR = "DQ-NBA-LINEUP-R001"
DQ_PAGE_LIMIT_REACHED = "DQ-NBA-LINEUP-R002"
DQ_WRONG_GAME = "DQ-NBA-LINEUP-R003"
DQ_MALFORMED_PAGE = "DQ-NBA-LINEUP-R004"
DQ_EMPTY_PAGE_WITH_CURSOR = "DQ-NBA-LINEUP-R005"
DQ_TERMINAL_FAILURE = "DQ-NBA-LINEUP-R006"
DQ_SILENT_LOSS = "DQ-NBA-LINEUP-R007"
DQ_CONFLICTING_PLAYER = "DQ-NBA-LINEUP-R008"

#: Why a game's cursor chain stopped. Only ``exhausted`` is completion.
STOP_EXHAUSTED = "exhausted"          # provider returned a null next_cursor
STOP_PAGE_LIMIT = "page_limit"        # 8 pages fetched, cursor still live
STOP_REPEATED_CURSOR = "repeated_cursor"
STOP_WRONG_GAME = "wrong_game"
STOP_MALFORMED = "malformed"
STOP_FAILED = "request_failed"


@dataclass
class ContinuationPage:
    """One fetched continuation page and the cursor chain position it occupies."""

    provider_game_id: str
    page_ordinal: int          # 1 = first CONTINUATION page (page two overall)
    requested_cursor: object
    returned_cursor: Optional[int]
    rows: int
    raw_response_id: str = ""
    raw_response_hash: str = ""
    observed_at: str = ""


@dataclass
class ContinuationOutcome:
    """What the recovery achieved for one target game."""

    provider_game_id: str
    start_cursor: object
    pages: list[ContinuationPage] = field(default_factory=list)
    stop_reason: str = ""
    findings: list[str] = field(default_factory=list)
    lineup_rows: int = 0
    players_added: int = 0

    @property
    def complete(self) -> bool:
        """A game is complete ONLY when the provider ended the chain itself."""

        return self.stop_reason == STOP_EXHAUSTED

    @property
    def cursor_chain(self) -> list[object]:
        return [self.start_cursor] + [p.returned_cursor for p in self.pages]


@dataclass
class ContinuationReport:
    """Sanitized, deterministic counters for one recovery run."""

    contract_version: str = RECOVERY_CONTRACT_VERSION
    targets: int = 0
    targets_completed: int = 0
    targets_incomplete: int = 0
    continuation_requests: int = 0
    pages_persisted: int = 0
    lineup_rows: int = 0
    findings: int = 0
    first_page_requests: int = 0        # MUST stay zero
    outcomes: list[ContinuationOutcome] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """A run cannot succeed while any target's chain is unfinished."""

        return self.targets > 0 and self.targets_incomplete == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "targets": self.targets,
            "targets_completed": self.targets_completed,
            "targets_incomplete": self.targets_incomplete,
            "continuation_requests": self.continuation_requests,
            "pages_persisted": self.pages_persisted,
            "lineup_rows": self.lineup_rows,
            "findings": self.findings,
            "first_page_requests": self.first_page_requests,
            "success": self.success,
            "outcomes": [
                {
                    "provider_game_id": o.provider_game_id,
                    "start_cursor": o.start_cursor,
                    "pages": len(o.pages),
                    "stop_reason": o.stop_reason,
                    "complete": o.complete,
                    "lineup_rows": o.lineup_rows,
                    "findings": list(o.findings),
                    "cursor_chain": list(o.cursor_chain),
                }
                for o in sorted(self.outcomes,
                                key=lambda x: _canonical_id_key(x.provider_game_id))
            ],
        }


def semantic_lineup_key(row: dict[str, Any]) -> Optional[tuple[str, str]]:
    """The identity a lineup row is deduplicated on: ``(team id, player id)``.

    Page boundaries can repeat a row, and two pages of one game must converge on
    one lineup regardless of the order they arrive in.
    """

    team = row.get("team")
    player = row.get("player")
    if not isinstance(team, dict) or not isinstance(player, dict):
        return None
    if team.get("id") is None or player.get("id") is None:
        return None
    return str(team["id"]), str(player["id"])


def lineup_row_content(row: dict[str, Any]) -> dict[str, Any]:
    """The provider-observed fields whose disagreement is a real conflict.

    Position and starter are preserved exactly as observed. A later page may not
    overwrite a contradictory earlier value -- the caller raises a finding and
    keeps the first observation rather than letting arrival order decide.
    """

    starter = row.get("starter")
    return {
        "position": (str(row["position"]).strip()
                     if row.get("position") not in (None, "") else None),
        "starter": starter if isinstance(starter, bool) else None,
    }


def merge_lineup_rows(
    pages: Iterable[Iterable[dict[str, Any]]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[tuple[str, str]], int]:
    """Deterministically fold continuation pages into one lineup set.

    Returns ``(by_identity, conflicts, rejected)``. Identical repeats collapse;
    a contradictory repeat keeps the FIRST observation and is reported as a
    conflict rather than silently overwritten. The result is independent of page
    and row order, so a recovery cannot depend on traversal.
    """

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[tuple[str, str]] = []
    rejected = 0
    for page in pages:
        for row in page:
            if not isinstance(row, dict):
                rejected += 1
                continue
            key = semantic_lineup_key(row)
            if key is None:
                rejected += 1
                continue
            content = lineup_row_content(row)
            existing = merged.get(key)
            if existing is None:
                merged[key] = content
            elif existing != content and key not in conflicts:
                conflicts.append(key)
    return merged, sorted(conflicts), rejected


class ContinuationUnitFailed(RuntimeError):
    """A target's continuation did not finish; the unit stays incomplete."""


class LineupContinuationExecutor:
    """One checkpoint unit per TARGET GAME, driven by the shared pilot runner.

    Reusing ``run_pilot`` is deliberate: it already supplies per-unit
    checkpointing, resume that skips a completed unit with zero transport, the
    budget gate wired into the transport, and v2 usage provenance. This class
    supplies only the continuation-specific work.
    """

    def __init__(
        self,
        *,
        database: Any,
        client_factory: Any,
        targets: tuple[LineupTarget, ...],
        date_range: str,
        max_pages: int = MAX_CONTINUATION_PAGES,
        per_page: int = LINEUPS_PER_PAGE,
    ) -> None:
        if max_pages < 1 or max_pages > MAX_CONTINUATION_PAGES:
            raise LineupContinuationError(
                f"max_pages must be 1..{MAX_CONTINUATION_PAGES}, got {max_pages}")
        missing = [t.provider_game_id for t in targets if t.start_cursor is None]
        if missing:
            # Before any client is built: a target without a starting cursor has
            # nothing to continue from and must never be guessed at.
            raise LineupContinuationError(
                f"{len(missing)} target(s) have no starting cursor; refusing to run")
        self._database = database
        self._client_factory = client_factory
        self._targets = tuple(
            sorted(targets, key=lambda t: _canonical_id_key(t.provider_game_id)))
        self._range = date_range
        self._max_pages = max_pages
        self._per_page = per_page
        self._in_flight: Optional[str] = None
        self.report = ContinuationReport(targets=len(self._targets))

    # -- unit identity ------------------------------------------------------ #
    def _identity(self, provider_game_id: str) -> str:
        from ..request_control import RequestUnit

        return RequestUnit(
            provider=SUPPORTED_PROVIDER, league=SUPPORTED_LEAGUE,
            endpoint_family="lineups_continuation", date_key=self._range,
            entity_key=provider_game_id,
        ).identity()

    def remaining_identities(self, *, completed: set[str]) -> Optional[tuple[str, ...]]:
        """Outstanding targets, known offline: the set is fixed by the manifest."""

        return tuple(self._identity(t.provider_game_id) for t in self._targets
                     if self._identity(t.provider_game_id) not in completed)

    def in_flight_identity(self) -> Optional[str]:
        return self._in_flight

    # -- execution ---------------------------------------------------------- #
    def iter_units(self, *, gate: Any, completed: set[str]) -> Any:
        import asyncio

        for target in self._targets:
            identity = self._identity(target.provider_game_id)
            if identity in completed:
                continue                      # already durable -> zero transport
            self._in_flight = identity
            outcome = asyncio.run(self._run_target(gate, target))
            self.report.outcomes.append(outcome)
            self.report.continuation_requests += len(outcome.pages)
            self.report.pages_persisted += len(outcome.pages)
            self.report.lineup_rows += outcome.lineup_rows
            self.report.findings += len(outcome.findings)
            if not outcome.complete:
                self.report.targets_incomplete += 1
                self._in_flight = None
                # A short chain must stay resumable, exactly like a short ingest
                # unit: yielding it would checkpoint a partial recovery as done.
                raise ContinuationUnitFailed(
                    f"game {target.provider_game_id}: {outcome.stop_reason}")
            self.report.targets_completed += 1
            self._in_flight = None
            yield _unit_done(identity, bool(outcome.pages))

    async def _run_target(self, gate: Any, target: LineupTarget) -> ContinuationOutcome:
        """Walk one game's cursor chain, bounded and fail-closed."""

        from ..providers.balldontlie import next_cursor

        outcome = ContinuationOutcome(provider_game_id=target.provider_game_id,
                                      start_cursor=target.start_cursor)
        seen: set[object] = {target.start_cursor}
        cursor: object = target.start_cursor
        client = self._client_factory(gate)
        payloads: list[list[dict[str, Any]]] = []
        try:
            for ordinal in range(1, self._max_pages + 1):
                try:
                    response = await client.fetch_lineups(
                        game_ids=[target.provider_game_id], per_page=self._per_page,
                        cursor=cursor)
                except Exception as exc:                        # noqa: BLE001
                    outcome.stop_reason = STOP_FAILED
                    outcome.findings.append(
                        f"{DQ_TERMINAL_FAILURE}: continuation request failed "
                        f"({type(exc).__name__})")
                    return outcome

                rows, ok = _rows_of(response.data)
                if not ok:
                    outcome.stop_reason = STOP_MALFORMED
                    outcome.findings.append(
                        f"{DQ_MALFORMED_PAGE}: page {ordinal} is not a usable payload")
                    return outcome

                wrong = {str(r.get("game_id")) for r in rows
                         if isinstance(r, dict) and r.get("game_id") is not None}
                if wrong - {str(target.provider_game_id)}:
                    # A page describing a different game cannot be attributed to
                    # this one; attaching it would corrupt the lineup silently.
                    outcome.stop_reason = STOP_WRONG_GAME
                    outcome.findings.append(
                        f"{DQ_WRONG_GAME}: page {ordinal} carries a different game id")
                    return outcome

                nxt = next_cursor(response.data)
                exchange = response.exchange
                outcome.pages.append(ContinuationPage(
                    provider_game_id=target.provider_game_id, page_ordinal=ordinal,
                    requested_cursor=cursor, returned_cursor=nxt, rows=len(rows),
                    raw_response_id="", raw_response_hash="",
                    observed_at=str(getattr(exchange, "received_at", "")),
                ))
                payloads.append([r for r in rows if isinstance(r, dict)])

                if not rows and nxt is not None:
                    outcome.findings.append(
                        f"{DQ_EMPTY_PAGE_WITH_CURSOR}: page {ordinal} was empty but "
                        "advertised a further cursor")

                if nxt is None:
                    outcome.stop_reason = STOP_EXHAUSTED
                    break
                if nxt in seen:
                    # A cursor we have already requested means the provider is
                    # cycling; following it would loop without end.
                    outcome.stop_reason = STOP_REPEATED_CURSOR
                    outcome.findings.append(
                        f"{DQ_REPEATED_CURSOR}: page {ordinal} returned an "
                        "already-requested cursor")
                    return outcome
                seen.add(nxt)
                cursor = nxt
            else:
                outcome.stop_reason = STOP_PAGE_LIMIT
                outcome.findings.append(
                    f"{DQ_PAGE_LIMIT_REACHED}: {self._max_pages} continuation pages "
                    "fetched and the provider still advertises more")
                return outcome
        finally:
            await client.aclose()

        merged, conflicts, rejected = merge_lineup_rows(payloads)
        outcome.lineup_rows = sum(len(p) for p in payloads)
        outcome.players_added = len(merged)
        for team_id, player_id in conflicts:
            outcome.findings.append(
                f"{DQ_CONFLICTING_PLAYER}: team {team_id} player {player_id} was "
                "observed with contradictory position/starter across pages; the "
                "first observation was kept")
        if rejected:
            outcome.findings.append(
                f"{DQ_SILENT_LOSS}: {rejected} row(s) could not be normalized")
        return outcome


def _rows_of(data: Any) -> tuple[list[Any], bool]:
    """``(rows, usable)`` from a lineups payload; never raises on provider input."""

    if not isinstance(data, dict):
        return [], False
    rows = data.get("data")
    if not isinstance(rows, list):
        return [], False
    return rows, True


def _unit_done(identity: str, mutated: bool) -> Any:
    from .pilot import UnitDone

    return UnitDone(identity=identity, family="lineups_continuation",
                    database_mutated=mutated)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_survey(survey: LineupSurvey, out: Any) -> None:
    """Human-readable target survey. Counts only -- no player names, no bodies."""

    out(f"nba lineup continuation  targets={survey.target_count} "
        f"(offline derivation; no provider request)")
    out(f"  source:    selected_games={survey.selected_games} "
        f"with_first_page={survey.games_with_first_page} "
        f"already_complete={survey.complete_games}")
    out(f"  anchors:   missing_first_page={len(survey.games_missing_first_page)} "
        f"ambiguous_first_page={len(survey.games_with_duplicate_first_page)}")
    out(f"  digest:    target_digest={survey.target_digest()[:32]}…")
    if survey.targets:
        rows = [t.first_page_rows for t in survey.targets]
        out(f"  page one:  rows min={min(rows)} max={max(rows)} "
            f"(the {LINEUPS_PER_PAGE}-row ceiling is what truncated them)")


def render_report(report: ContinuationReport, out: Any) -> None:
    """Human-readable recovery report; reconciles with :meth:`as_dict`."""

    state = "COMPLETE" if report.success else "INCOMPLETE"
    out(f"nba lineup continuation  {state}  contract={report.contract_version}")
    out(f"  targets:   total={report.targets} completed={report.targets_completed} "
        f"incomplete={report.targets_incomplete}")
    out(f"  requests:  continuation_pages={report.continuation_requests} "
        f"first_page_requests={report.first_page_requests}")
    out(f"  rows:      lineup_rows={report.lineup_rows} "
        f"pages_persisted={report.pages_persisted}")
    out(f"  findings:  {report.findings}")
    for outcome in sorted(report.outcomes,
                          key=lambda o: _canonical_id_key(o.provider_game_id)):
        chain = " -> ".join("null" if c is None else str(c)
                            for c in outcome.cursor_chain)
        out(f"    game {outcome.provider_game_id:>10}  pages={len(outcome.pages)} "
            f"stop={outcome.stop_reason} complete={outcome.complete}  cursors: {chain}")
