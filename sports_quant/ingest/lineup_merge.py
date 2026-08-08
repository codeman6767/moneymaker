"""Offline merge of reviewed NBA lineup-continuation evidence into a copy.

This is a NARROW, offline-only merge for one reviewed artefact pair: the March
season-month corpus and the independently accepted lineup-continuation recovery.
It is deliberately not a general database-merge framework -- every bound is
specific to that reviewed pair and is re-checked from the evidence itself.

Why the merge APPENDS revision snapshots instead of extending existing ones
--------------------------------------------------------------------------
``lineup_snapshots`` and ``lineup_players`` are hard append-only: both carry
``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers that ``RAISE(ABORT)``. An
existing page-one snapshot therefore cannot gain members in place, and a merge
that tried would be rejected by the database.

The schema instead models observation-time REVISIONS. ``lineup_snapshots`` has
``UNIQUE (game_ref_id, provider_team_id, observed_at, content_hash)`` and an index
on ``(game_ref_id, provider_team_id, observed_at)``; the repository appends
through :func:`append_transition`, which collapses an unchanged re-observation and
appends a changed one. As-of selection then takes the latest observation at or
before the cutoff, so a later revision unambiguously supersedes page one.

So for every affected ``(game, team)`` this module appends ONE revision snapshot
whose membership is *page one plus the reviewed continuation rows*, observed at
the continuation's own receipt instant. Page one keeps its original row, its
original ``observed_at`` and its original raw-response provenance; the two-stage
acquisition stays visible rather than being flattened into a single observation.

A consequence worth stating plainly: the recovered observations number exactly
32, but they arrive inside revision snapshots that necessarily restate the
page-one members of those same teams, so the ``lineup_players`` row count grows
by more than 32. The exact identity is derived and asserted, never assumed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.lineups import LineupPlayerInput, SqliteLineupRepository
from ..db.schema import utc_now_iso

#: Contract version for the merge output and its provenance record.
MERGE_CONTRACT_VERSION = "nba-lineup-merge-v1"

#: The CLI command this module backs.
MERGE_COMMAND = "merge-nba-lineup-continuation"

MERGE_TOOL_VERSION = "sports_quant 0.1.0"

#: One note-severity provenance row per reviewed target game.
DQ_MERGE_PROVENANCE = "DQ-NBA-LINEUP-M001"

LINEUPS_ENDPOINT = "/v1/lineups"

PROVIDER = "balldontlie"


class LineupMergeError(RuntimeError):
    """The merge refused: a bound, path or evidence check failed."""


# --------------------------------------------------------------------------- #
# evidence loading
# --------------------------------------------------------------------------- #

def _game_id_of(params_json: Optional[str]) -> Optional[str]:
    """The single provider game id a lineup request carried.

    Tolerates both the repaired list encoding and the legacy ``str(list)`` form
    that an earlier defect wrote, so historical evidence stays readable.
    """

    if not params_json:
        return None
    try:
        params = json.loads(params_json)
    except (TypeError, ValueError):
        return None
    raw = params.get("game_ids[]", params.get("game_ids"))
    if raw is None:
        return None
    if isinstance(raw, list):
        return str(raw[0]) if len(raw) == 1 else None
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        inner = [x.strip().strip("'\"") for x in text[1:-1].split(",") if x.strip()]
        return inner[0] if len(inner) == 1 else None
    return text


def _cursor_of(params_json: Optional[str]) -> Optional[int]:
    if not params_json:
        return None
    try:
        value = json.loads(params_json).get("cursor")
    except (TypeError, ValueError):
        return None
    return None if value is None else int(value)


def _rows_of(body: Optional[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not body:
        return [], {}
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return [], {}
    if not isinstance(parsed, dict):
        return [], {}
    data = parsed.get("data")
    meta = parsed.get("meta")
    return ([r for r in data if isinstance(r, dict)] if isinstance(data, list) else [],
            meta if isinstance(meta, dict) else {})


def player_identity(row: dict[str, Any]) -> Optional[tuple[str, str]]:
    """``(provider team id, provider player id)`` -- the merge identity."""

    team, player = row.get("team"), row.get("player")
    if not isinstance(team, dict) or not isinstance(player, dict):
        return None
    if team.get("id") is None or player.get("id") is None:
        return None
    return str(team["id"]), str(player["id"])


def _observed_content(row: dict[str, Any]) -> tuple[Optional[str], Optional[bool]]:
    """Provider-observed ``(position, starter)``, preserved exactly."""

    position = row.get("position")
    starter = row.get("starter")
    return (str(position).strip() if position not in (None, "") else None,
            starter if isinstance(starter, bool) else None)


def _provenance_key(row: dict[str, Any]) -> tuple:
    """Deterministic order for continuation rows: provider row id, then identity."""

    raw_id = row.get("id")
    id_key = ((0, int(raw_id), "") if isinstance(raw_id, int) and not isinstance(raw_id, bool)
              else (1, 0, str(raw_id)))
    return (id_key, player_identity(row) or ("", ""))


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MergePlayer:
    """One member of a revision snapshot, with where it was observed."""

    provider_player_id: str
    batting_order: int
    position: Optional[str]
    is_starter: Optional[bool]
    origin: str                      # "page_one" | "continuation"


@dataclass
class TeamRevision:
    """One appended revision snapshot for a single ``(game, team)``."""

    provider_game_id: str
    provider_team_id: str
    game_ref_id: str                 # the DESTINATION's reference, never the recovery's
    players: list[MergePlayer]
    observed_at: str                 # the continuation receipt instant
    raw_response_id: str             # the recovery continuation response
    raw_response_hash: str
    run_id: Optional[str]
    page_one_count: int
    added_count: int

    def body(self) -> dict[str, Any]:
        return {
            "provider_game_id": self.provider_game_id,
            "provider_team_id": self.provider_team_id,
            "observed_at": self.observed_at,
            "raw_response_id": self.raw_response_id,
            "raw_response_hash": self.raw_response_hash,
            "page_one_count": self.page_one_count,
            "added_count": self.added_count,
            "players": [
                {"provider_player_id": p.provider_player_id,
                 "batting_order": p.batting_order,
                 "position": p.position,
                 "is_starter": p.is_starter,
                 "origin": p.origin}
                for p in sorted(self.players, key=lambda x: x.batting_order)
            ],
        }


@dataclass
class TargetOutcome:
    """What the merge found for one reviewed target game."""

    provider_game_id: str
    page_one_rows: int
    continuation_rows: int
    added_players: int
    revisions: int
    page_one_response_id: str
    page_one_response_hash: str
    continuation_response_id: str
    continuation_response_hash: str
    requested_cursor: Optional[int]
    empty_continuation_page: bool

    def body(self) -> dict[str, Any]:
        return {
            "provider_game_id": self.provider_game_id,
            "page_one_rows": self.page_one_rows,
            "continuation_rows": self.continuation_rows,
            "added_players": self.added_players,
            "revisions": self.revisions,
            "page_one_response_id": self.page_one_response_id,
            "page_one_response_hash": self.page_one_response_hash,
            "continuation_response_id": self.continuation_response_id,
            "continuation_response_hash": self.continuation_response_hash,
            "requested_cursor": self.requested_cursor,
            "empty_continuation_page": self.empty_continuation_page,
        }


@dataclass
class MergePlan:
    """The complete, deterministic merge derived from the two corpora."""

    contract_version: str = MERGE_CONTRACT_VERSION
    revisions: list[TeamRevision] = field(default_factory=list)
    outcomes: list[TargetOutcome] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    rejected_rows: int = 0

    @property
    def targets(self) -> int:
        return len(self.outcomes)

    @property
    def eligible(self) -> int:
        return sum(1 for _o in self.outcomes)

    @property
    def recovered_observations(self) -> int:
        """Distinct ``(game, team, player)`` observations the merge recovers."""

        return sum(o.added_players for o in self.outcomes)

    @property
    def new_snapshots(self) -> int:
        return len(self.revisions)

    @property
    def new_player_rows(self) -> int:
        """Rows appended: each revision restates page one plus its additions."""

        return sum(len(r.players) for r in self.revisions)

    def digest(self) -> str:
        """Order-independent semantic digest of the whole merge."""

        body = {
            "contract_version": self.contract_version,
            "revisions": sorted((r.body() for r in self.revisions),
                                key=lambda b: (b["provider_game_id"], b["provider_team_id"])),
            "outcomes": sorted((o.body() for o in self.outcomes),
                               key=lambda b: b["provider_game_id"]),
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def body(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "targets": self.targets,
            "eligible": self.eligible,
            "rejected_rows": self.rejected_rows,
            "conflicts": list(self.conflicts),
            "recovered_observations": self.recovered_observations,
            "new_snapshots": self.new_snapshots,
            "new_player_rows": self.new_player_rows,
            "digest": self.digest(),
        }


def _load_page_one(src: sqlite3.Connection) -> dict[str, tuple[str, str, list[dict[str, Any]],
                                                               dict[str, Any]]]:
    out: dict[str, tuple[str, str, list[dict[str, Any]], dict[str, Any]]] = {}
    for row in src.execute(
        "SELECT raw_response_id, body_hash, request_params_json, body FROM raw_responses "
        "WHERE endpoint = ?", (LINEUPS_ENDPOINT,)
    ):
        if _cursor_of(row["request_params_json"]) is not None:
            continue                                # a continuation page, not page one
        gid = _game_id_of(row["request_params_json"])
        if gid is None:
            continue
        rows, meta = _rows_of(row["body"])
        out[gid] = (str(row["raw_response_id"]), str(row["body_hash"]), rows, meta)
    return out


def _load_continuation(rec: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rec.execute(
        "SELECT raw_response_id, body_hash, request_params_json, body, http_status, run_id, "
        "received_at FROM raw_responses"
    ):
        gid = _game_id_of(row["request_params_json"])
        if gid is None:
            raise LineupMergeError(
                "recovery raw response does not carry exactly one provider game id")
        rows, meta = _rows_of(row["body"])
        out[gid] = {
            "raw_response_id": str(row["raw_response_id"]),
            "raw_response_hash": str(row["body_hash"]),
            "cursor": _cursor_of(row["request_params_json"]),
            "rows": rows,
            "meta": meta,
            "http_status": row["http_status"],
            "run_id": row["run_id"],
            "received_at": str(row["received_at"]),
        }
    return out


def plan_merge(
    *,
    source: sqlite3.Connection,
    recovery: sqlite3.Connection,
    destination: sqlite3.Connection,
    expected_targets: int,
    expected_digest_targets: Optional[str] = None,
) -> MergePlan:
    """Derive the merge from the evidence; refuse anything unreviewed.

    Nothing is written. Every target is re-validated against the same rule the
    accepted execution review applied, so a plan can never be produced for
    evidence that would not have been accepted.
    """

    page_one = _load_page_one(source)
    continuation = _load_continuation(recovery)
    if len(continuation) != expected_targets:
        raise LineupMergeError(
            f"recovery holds {len(continuation)} target(s); expected {expected_targets}")

    # destination reference ids, keyed by provider game id
    dest_refs = {
        str(r["provider_game_id"]): str(r["reference_id"])
        for r in destination.execute(
            "SELECT reference_id, provider_game_id FROM provider_game_references "
            "WHERE provider = ?", (PROVIDER,))
    }
    # Destination PAGE-ONE snapshot membership, keyed by (game, team).
    #
    # Deliberately the EARLIEST observation for each anchor, never the latest: a
    # revision this merge already appended must not become the base of a second
    # one. Anchoring on the original page-one observation makes the planned
    # membership a pure function of the two source corpora, so re-planning after a
    # merge yields byte-identical content, ``append_transition`` collapses it, and
    # the run is idempotent. Taking the latest instead would stack the continuation
    # rows on top of themselves on every replay.
    dest_snap: dict[tuple[str, str], tuple[str, list[sqlite3.Row]]] = {}
    for row in destination.execute(
        "SELECT lineup_id, provider_game_id, provider_team_id, observed_at "
        "FROM lineup_snapshots ORDER BY observed_at, lineup_id"
    ):
        key = (str(row["provider_game_id"]), str(row["provider_team_id"]))
        if key in dest_snap:
            continue                                # keep the earliest observation
        members = list(destination.execute(
            "SELECT batting_order, provider_player_id, position, is_starter "
            "FROM lineup_players WHERE lineup_id = ? ORDER BY batting_order",
            (row["lineup_id"],)))
        dest_snap[key] = (str(row["lineup_id"]), members)

    plan = MergePlan()
    for gid in sorted(continuation, key=lambda g: (len(g), g)):
        ev = continuation[gid]
        if gid not in page_one:
            raise LineupMergeError(f"game {gid}: no page-one evidence in the source corpus")
        p1_id, p1_hash, p1_rows, p1_meta = page_one[gid]
        if ev["http_status"] != 200:
            raise LineupMergeError(f"game {gid}: continuation HTTP {ev['http_status']}")
        if ev["cursor"] is None:
            raise LineupMergeError(f"game {gid}: continuation carries no cursor")
        if p1_meta.get("next_cursor") != ev["cursor"]:
            raise LineupMergeError(
                f"game {gid}: requested cursor {ev['cursor']} does not match the page-one "
                f"next_cursor {p1_meta.get('next_cursor')}")
        if ev["meta"].get("next_cursor") is not None:
            raise LineupMergeError(
                f"game {gid}: continuation still advertises a further cursor; the chain did "
                "not terminate and the game is not merge-eligible")
        if gid not in dest_refs:
            raise LineupMergeError(f"game {gid}: destination has no provider game reference")

        rows = ev["rows"]
        for row in rows:
            if str(row.get("game_id")) != gid:
                raise LineupMergeError(f"game {gid}: continuation carries a wrong-game row")

        # page-one identities and content, for overlap and contradiction checks
        p1_by_team: dict[str, list[dict[str, Any]]] = {}
        p1_content: dict[tuple[str, str], tuple[Optional[str], Optional[bool]]] = {}
        for row in p1_rows:
            ident = player_identity(row)
            if ident is None:
                continue
            p1_by_team.setdefault(ident[0], []).append(row)
            p1_content.setdefault(ident, _observed_content(row))

        added_by_team: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for row in sorted(rows, key=_provenance_key):
            ident = player_identity(row)
            if ident is None:
                plan.rejected_rows += 1
                raise LineupMergeError(
                    f"game {gid}: a continuation row lost team/player identity; the reviewed "
                    "evidence had none and the merge fails closed")
            if ident in seen:
                continue                              # identical duplicate collapses
            seen.add(ident)
            if ident in p1_content:
                if p1_content[ident] != _observed_content(row):
                    plan.conflicts.append(
                        f"{gid}: team {ident[0]} player {ident[1]} contradicts page one")
                continue                              # overlap never replaces page one
            added_by_team.setdefault(ident[0], []).append(row)

        # a player may not appear for both teams of the same game
        by_player: dict[str, set[str]] = {}
        for team_id, members in p1_by_team.items():
            for row in members:
                ident = player_identity(row)
                if ident:
                    by_player.setdefault(ident[1], set()).add(team_id)
        for team_id, members in added_by_team.items():
            for row in members:
                ident = player_identity(row)
                if ident:
                    by_player.setdefault(ident[1], set()).add(team_id)
        for pid, teams in by_player.items():
            if len(teams) > 1:
                plan.conflicts.append(
                    f"{gid}: player {pid} appears for opposing teams {sorted(teams)}")

        added_total = 0
        for team_id, new_rows in sorted(added_by_team.items()):
            key = (gid, team_id)
            if key not in dest_snap:
                raise LineupMergeError(
                    f"game {gid}: destination has no page-one snapshot for team {team_id}")
            _lineup_id, members = dest_snap[key]
            players = [
                MergePlayer(provider_player_id=str(m["provider_player_id"]),
                            batting_order=int(m["batting_order"]),
                            position=m["position"],
                            is_starter=(None if m["is_starter"] is None
                                        else bool(m["is_starter"])),
                            origin="page_one")
                for m in members
            ]
            next_order = (max((p.batting_order for p in players), default=0) + 1)
            for row in new_rows:
                ident = player_identity(row)
                assert ident is not None            # filtered above
                position, starter = _observed_content(row)
                players.append(MergePlayer(
                    provider_player_id=ident[1], batting_order=next_order,
                    position=position, is_starter=starter, origin="continuation"))
                next_order += 1
            plan.revisions.append(TeamRevision(
                provider_game_id=gid, provider_team_id=team_id,
                game_ref_id=dest_refs[gid], players=players,
                observed_at=ev["received_at"], raw_response_id=ev["raw_response_id"],
                raw_response_hash=ev["raw_response_hash"], run_id=ev["run_id"],
                page_one_count=len(members), added_count=len(new_rows)))
            added_total += len(new_rows)

        plan.outcomes.append(TargetOutcome(
            provider_game_id=gid, page_one_rows=len(p1_rows), continuation_rows=len(rows),
            added_players=added_total, revisions=len(added_by_team),
            page_one_response_id=p1_id, page_one_response_hash=p1_hash,
            continuation_response_id=ev["raw_response_id"],
            continuation_response_hash=ev["raw_response_hash"],
            requested_cursor=ev["cursor"], empty_continuation_page=not rows))

    if plan.conflicts:
        raise LineupMergeError(
            "merge refused: contradictory continuation evidence -- "
            + "; ".join(plan.conflicts[:5]))
    if expected_digest_targets is not None:
        got = target_digest(o.provider_game_id for o in plan.outcomes)
        if got != expected_digest_targets:
            raise LineupMergeError(
                f"target-set digest {got} does not match the reviewed {expected_digest_targets}")
    return plan


def target_digest(game_ids: Iterable[str]) -> str:
    """Digest of the merged target set, independent of traversal order."""

    ordered = sorted({str(g) for g in game_ids}, key=lambda g: (len(g), g))
    return hashlib.sha256(
        json.dumps(ordered, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

@dataclass
class MergeReport:
    """Deterministic counters for one merge application."""

    contract_version: str = MERGE_CONTRACT_VERSION
    dry_run: bool = True
    targets: int = 0
    eligible: int = 0
    conflicts: int = 0
    rejected_rows: int = 0
    recovered_observations: int = 0
    snapshots_appended: int = 0
    snapshots_unchanged: int = 0
    player_rows_appended: int = 0
    raw_responses_copied: int = 0
    ingestion_runs_copied: int = 0
    provenance_rows: int = 0
    digest: str = ""
    network_occurred: bool = False

    def body(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dry_run": self.dry_run,
            "targets": self.targets,
            "eligible": self.eligible,
            "conflicts": self.conflicts,
            "rejected_rows": self.rejected_rows,
            "recovered_observations": self.recovered_observations,
            "snapshots_appended": self.snapshots_appended,
            "snapshots_unchanged": self.snapshots_unchanged,
            "player_rows_appended": self.player_rows_appended,
            "raw_responses_copied": self.raw_responses_copied,
            "ingestion_runs_copied": self.ingestion_runs_copied,
            "provenance_rows": self.provenance_rows,
            "digest": self.digest,
            "network_occurred": self.network_occurred,
        }


def _copy_recovery_provenance(
    dest: sqlite3.Connection, rec: sqlite3.Connection, plan: MergePlan,
) -> tuple[int, int]:
    """Copy the continuation raw responses and their ingestion runs.

    Both are required: ``lineup_snapshots.raw_response_id`` is a NOT NULL foreign
    key, and the reviewed provenance chain must stay traversable inside the merged
    copy. Provider game REFERENCES are deliberately not copied -- the destination
    already holds one per game under ``UNIQUE (provider, provider_game_id)``, and
    the revision anchors on the destination's own reference.
    """

    # Every reviewed continuation response, not merely the ones that yielded a
    # revision: each target's provenance row references its own response, and
    # ``data_quality_issues.raw_response_id`` is a foreign key. The 19 terminal
    # empty pages are evidence too -- they are why those games are complete.
    wanted = {r.raw_response_id for r in plan.revisions}
    wanted |= {o.continuation_response_id for o in plan.outcomes}
    runs = 0
    responses = 0
    for rid in sorted(wanted):
        row = rec.execute("SELECT * FROM raw_responses WHERE raw_response_id = ?",
                          (rid,)).fetchone()
        if row is None:
            raise LineupMergeError(f"recovery raw response {rid} vanished")
        run_id = row["run_id"]
        if run_id is not None and dest.execute(
                "SELECT 1 FROM ingestion_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
            run = rec.execute("SELECT * FROM ingestion_runs WHERE run_id = ?",
                              (run_id,)).fetchone()
            if run is not None:
                cols = list(run.keys())
                dest.execute(
                    f"INSERT INTO ingestion_runs ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    tuple(run[c] for c in cols))
                runs += 1
        if dest.execute("SELECT 1 FROM raw_responses WHERE raw_response_id = ?",
                        (rid,)).fetchone() is None:
            cols = list(row.keys())
            dest.execute(
                f"INSERT INTO raw_responses ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols))
            responses += 1
    return responses, runs


def apply_merge(
    *,
    destination: sqlite3.Connection,
    recovery: sqlite3.Connection,
    plan: MergePlan,
    provenance: dict[str, Any],
    dry_run: bool = True,
) -> MergeReport:
    """Apply the plan to the destination inside the caller's transaction.

    On ``dry_run`` nothing is written at all: the report is produced from the plan
    alone, so the counts an operator inspects are the ones an apply would produce.
    """

    report = MergeReport(
        dry_run=dry_run, targets=plan.targets, eligible=plan.eligible,
        conflicts=len(plan.conflicts), rejected_rows=plan.rejected_rows,
        recovered_observations=plan.recovered_observations, digest=plan.digest(),
        network_occurred=False)
    if dry_run:
        report.snapshots_appended = plan.new_snapshots
        report.player_rows_appended = plan.new_player_rows
        report.raw_responses_copied = len(
            {r.raw_response_id for r in plan.revisions}
            | {o.continuation_response_id for o in plan.outcomes})
        report.provenance_rows = plan.targets
        return report

    responses, runs = _copy_recovery_provenance(destination, recovery, plan)
    report.raw_responses_copied = responses
    report.ingestion_runs_copied = runs

    lineups = SqliteLineupRepository(destination)
    for revision in sorted(plan.revisions,
                           key=lambda r: (r.provider_game_id, r.provider_team_id)):
        players = [
            LineupPlayerInput(batting_order=p.batting_order,
                              provider_player_id=p.provider_player_id,
                              position=p.position, is_starter=p.is_starter,
                              player_id=None)          # canonical ids are never invented here
            for p in sorted(revision.players, key=lambda x: x.batting_order)
        ]
        lineup_id, outcome, inserted = lineups.append(
            game_ref_id=revision.game_ref_id, provider=PROVIDER,
            provider_game_id=revision.provider_game_id,
            provider_team_id=revision.provider_team_id, players=players,
            observed_at=revision.observed_at, ingested_at=utc_now_iso(),
            run_id=revision.run_id, raw_response_id=revision.raw_response_id,
            raw_response_hash=revision.raw_response_hash,
            team_id=None, home_away=None, is_confirmed=False)
        if lineup_id is None:
            report.snapshots_unchanged += 1
        else:
            report.snapshots_appended += 1
            report.player_rows_appended += inserted

    dq = SqliteDataQualityRepository(destination)
    for target in sorted(plan.outcomes, key=lambda o: o.provider_game_id):
        existing = destination.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_id = ? "
            "AND provider = ?",
            (DQ_MERGE_PROVENANCE, target.provider_game_id, PROVIDER)).fetchone()
        if existing is not None:
            continue
        detail = dict(provenance)
        detail.update(target.body())
        detail["contract_version"] = MERGE_CONTRACT_VERSION
        detail["network_occurred"] = False
        dq.record(
            severity="note", rule_code=DQ_MERGE_PROVENANCE, entity_type="game",
            entity_id=target.provider_game_id, provider=PROVIDER,
            description=(
                f"offline lineup-continuation merge for game {target.provider_game_id}: "
                f"{target.added_players} recovered observation(s) appended as "
                f"{target.revisions} revision snapshot(s); page one retained its original "
                "observation and provenance"),
            detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
            run_id=None, raw_response_id=target.continuation_response_id)
        report.provenance_rows += 1
    return report


def render_report(report: MergeReport, plan: MergePlan, out: Any) -> None:
    """Human-readable merge report; reconciles with :meth:`MergeReport.body`."""

    mode = "DRY RUN" if report.dry_run else "APPLIED"
    out(f"nba lineup continuation merge  {mode}  contract={report.contract_version}")
    out(f"  targets:   total={report.targets} eligible={report.eligible} "
        f"conflicts={report.conflicts} rejected_rows={report.rejected_rows}")
    out(f"  recovered: observations={report.recovered_observations}")
    out(f"  appended:  snapshots={report.snapshots_appended} "
        f"unchanged={report.snapshots_unchanged} player_rows={report.player_rows_appended}")
    out(f"  copied:    raw_responses={report.raw_responses_copied} "
        f"ingestion_runs={report.ingestion_runs_copied}")
    out(f"  provenance: rows={report.provenance_rows}")
    out(f"  digest:    {report.digest}")
    out(f"  network_occurred={report.network_occurred}")
    for outcome in sorted(plan.outcomes, key=lambda o: o.provider_game_id):
        out(f"    game {outcome.provider_game_id:>10}  page_one={outcome.page_one_rows} "
            f"continuation={outcome.continuation_rows} added={outcome.added_players} "
            f"revisions={outcome.revisions} "
            f"{'(empty page)' if outcome.empty_continuation_page else ''}")
