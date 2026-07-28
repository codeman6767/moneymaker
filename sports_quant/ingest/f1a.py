"""F1A CLI orchestration: zero-network planning and guarded pilot execution.

Two entry points used by ``ingest-mlb`` / ``ingest-nba``:

* :func:`emit_plan` -- the ``--plan`` mode. Pure computation: it builds a request
  plan + manifest and emits it. It constructs no provider client, opens no
  socket, loads no settings/secret, and touches no database. A sentinel guards
  against any network-capable path being reached.
* :func:`run_pilot_cli` -- guarded live execution (F1B). Every invalid
  combination fails **before** a client is built or a database is opened:
  missing request cap, missing NBA credit cap, unbounded contingent fan-out, a
  cap below the plan's conservative maximum, a missing/unsafe scratch database,
  resume without a checkpoint, or a checkpoint/manifest/database mismatch. It
  then runs the plan through the budget :class:`RequestGate` and the pilot
  runner, returning :data:`EXIT_BUDGET_EXHAUSTED` on a controlled truncation.

This module is imported by the CLI only; it performs no import-time I/O.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ..request_control import (
    BudgetExhausted,
    CreditBudget,
    RequestBudget,
    RequestGate,
    RequestUnit,
)
from .checkpoint import CheckpointError, load_checkpoint
from .cost_policies import build_balldontlie_policy, build_mlb_policy
from .manifest import (
    ManifestError,
    PilotManifest,
    build_manifest,
    load_and_validate,
    plan_hash,
)
from .pilot import UnitDone, run_pilot
from .planning import Bounds, build_plan
from .scratch_db import (
    ScratchClass,
    ScratchDbError,
    classify_scratch_db,
)

#: Distinct, stable exit code for a controlled budget/credit exhaustion --
#: separate from ordinary provider failure (2) and database errors (3).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DATABASE_ERROR = 3
EXIT_BUDGET_EXHAUSTED = 4

_MLB_RICH = {"results", "box", "inning", "rosters"}
_NBA_RICH = {"box", "stats", "advanced", "plays", "lineups"}

#: F1B (live pilot) is DISABLED by default. A future controlled authorization is a
#: separate, reviewed step; this env var only lets tests exercise the mocked
#: guarded path. It never enables real network here (transports are mocked).
_F1B_AUTHORIZED_ENV = "MONEYMAKER_F1B_AUTHORIZED"


def _f1b_authorized() -> bool:
    return os.environ.get(_F1B_AUTHORIZED_ENV) == "1"


def _provider_for(league: str) -> str:
    return "mlb_statsapi" if league == "mlb" else "balldontlie"


def _parse_date_range(date_range: str) -> tuple[str, Optional[str]]:
    if ".." in date_range:
        a, b = date_range.split("..", 1)
        return a, b
    return date_range, None


def _families_and_stage(league: str, includes: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    rich = _MLB_RICH if league == "mlb" else _NBA_RICH
    skeleton = "schedule" if league == "mlb" else "games"
    rich_present = sorted(set(includes) & rich)
    families = (skeleton, *rich_present)
    return families, ("rich" if rich_present else "skeleton")


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (temp + replace), refusing a symlink."""

    path = Path(path)
    if path.is_symlink():
        raise ScratchDbError(f"refusing to write through a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _build_plan_and_manifest(
    *,
    league: str,
    from_date: str,
    to_date: Optional[str],
    includes: tuple[str, ...],
    bounds: Bounds,
    scratch_db: str = "",
    checkpoint: str = "",
    request_cap: Optional[int] = None,
    credit_cap: Optional[int] = None,
) -> tuple[Any, PilotManifest]:
    families, stage = _families_and_stage(league, includes)
    plan = build_plan(league=league, from_date=from_date, to_date=to_date,
                      families=families, stage=stage, bounds=bounds)
    manifest = build_manifest(plan, scratch_db=scratch_db, checkpoint_path=checkpoint,
                              request_cap=request_cap, credit_cap=credit_cap)
    return plan, manifest


# --------------------------------------------------------------------------- #
# --plan : genuine zero-network planning
# --------------------------------------------------------------------------- #
def emit_plan(
    *,
    league: str,
    from_date: str,
    to_date: Optional[str],
    includes: tuple[str, ...],
    max_games: Optional[int] = None,
    max_pages: Optional[int] = None,
    max_records: Optional[int] = None,
    max_retries: int = 3,
    request_cap: Optional[int] = None,
    credit_cap: Optional[int] = None,
    scratch_db: str = "",
    checkpoint: str = "",
    as_json: bool = False,
    manifest_out: Optional[Path] = None,
    out: Callable[[str], None] = print,
) -> int:
    """``--plan``: build + emit a plan/manifest with ZERO network or database work."""

    bounds = Bounds(max_games=max_games, max_pages=max_pages, max_records=max_records,
                    max_retries=max_retries)
    plan, manifest = _build_plan_and_manifest(
        league=league, from_date=from_date, to_date=to_date, includes=includes, bounds=bounds,
        scratch_db=scratch_db, checkpoint=checkpoint, request_cap=request_cap,
        credit_cap=credit_cap)
    payload = manifest.as_dict()
    payload["network_occurred"] = False
    payload["database_touched"] = False
    if manifest_out is not None:
        # The only file write permitted in zero-network plan mode: atomic + no symlink.
        _atomic_write_text(Path(manifest_out), manifest.canonical())
        payload["manifest_written_to"] = str(manifest_out)
    if as_json:
        out(json.dumps(payload, sort_keys=True))
        return EXIT_OK
    ex_max = manifest.estimated_requests_max
    cr_max = manifest.estimated_credits_max
    out(f"plan  {league.upper()}  stage={plan.stage}  range={manifest.date_range}  "
        f"[{'EXECUTABLE' if manifest.executable else 'NON-EXECUTABLE'}]  (no network)")
    out(f"  families: {', '.join(manifest.families)}")
    out(f"  requests: min={manifest.estimated_requests_min} "
        f"max={'unbounded' if ex_max is None else ex_max}  "
        f"required_request_cap={manifest.request_cap}")
    if manifest.credits_applicable:
        out(f"  credits:  min={manifest.estimated_credits_min} "
            f"max={'unbounded' if cr_max is None else cr_max}  "
            f"required_credit_cap={manifest.credit_cap}")
    else:
        out("  credits:  not applicable (keyless MLB StatsAPI)")
    if not manifest.executable:
        out(f"  unresolved bounds: {', '.join(manifest.unresolved_bounds) or 'none'}")
    out(f"  manifest_hash: {manifest.manifest_hash()}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Guarded live pilot execution (F1B)
# --------------------------------------------------------------------------- #
class _IngestorExecutor:
    """Stage-level executor that runs the real ingestor under the gate.

    The whole (schedule/games [+ rich]) ingestion for the range is one semantic
    checkpoint unit whose consistency boundary is the ingestor's committed run;
    the gate (wired into the transport) enforces the budget on every underlying
    call, so a mid-run exhaustion truncates safely, and a completed unit is
    skipped on resume with zero transport. NBA pagination bounds (max_pages /
    max_records) from the reviewed manifest are passed through to ``ingest_nba``.

    KNOWN LIMITATION (F1A review, tracked): resume granularity is the STAGE, not
    the individual request/page/game -- resuming an interrupted rich stage re-runs
    the ingestor (idempotent on rows via content hash, but it re-issues completed
    requests). Finer request-level resumability requires making the D2/D3 bulk
    ingestors request-addressable and is deferred to a dedicated, separately
    reviewed change; it does NOT weaken the hard budget stop (the gate still caps
    the aggregate spend). ``max_games`` is likewise not yet honored by the bulk
    ingestors.
    """

    def __init__(
        self,
        *,
        league: str,
        database: Any,
        client_factory: Callable[[RequestGate], Any],
        from_date: str,
        to_date: Optional[str],
        includes: tuple[str, ...],
        stage: str,
        max_pages: Optional[int] = None,
        max_records: Optional[int] = None,
    ) -> None:
        self._league = league
        self._database = database
        self._client_factory = client_factory
        self._from = from_date
        self._to = to_date
        self._includes = includes
        self._stage = stage
        self._max_pages = max_pages
        self._max_records = max_records

    def _unit(self) -> RequestUnit:
        rng = self._from if not self._to or self._to == self._from else f"{self._from}..{self._to}"
        fam = "schedule" if self._league == "mlb" else "games"
        return RequestUnit(provider=("mlb_statsapi" if self._league == "mlb" else "balldontlie"),
                           league=self._league, endpoint_family=fam, date_key=rng,
                           entity_key=f"stage:{self._stage}")

    def iter_units(self, *, gate: RequestGate, completed: set[str]) -> Iterator[UnitDone]:
        unit = self._unit()
        ident = unit.identity()
        if ident in completed:
            return  # resume: this stage already durable -> zero transport
        client = self._client_factory(gate)

        async def _run() -> Any:
            from .mlb_ingestor import ingest_mlb
            from .nba_ingestor import ingest_nba
            try:
                if self._league == "mlb":
                    return await ingest_mlb(
                        database=self._database, client=client, from_date=self._from,
                        to_date=self._to, includes=self._includes, dry_run=False)
                from ..providers.capabilities import BalldontlieTier
                nba_kwargs: dict[str, Any] = {}
                if self._max_pages is not None:
                    nba_kwargs["max_pages"] = self._max_pages
                if self._max_records is not None:
                    nba_kwargs["max_records"] = self._max_records
                return await ingest_nba(
                    database=self._database, client=client, from_date=self._from,
                    to_date=self._to, includes=self._includes,
                    tier=BalldontlieTier.GOAT, dry_run=False, **nba_kwargs)
            finally:
                await client.aclose()

        result = asyncio.run(_run())
        # If a gated call was blocked and the ingestor swallowed it, surface it.
        if gate.usage.budget_exhausted is not None:
            raise BudgetExhausted.from_dict(gate.usage.budget_exhausted)
        mutated = bool(getattr(result, "raw_responses_received", 0))
        yield UnitDone(identity=ident, family=unit.endpoint_family, database_mutated=mutated)


def _make_gate(*, league: str, request_cap: int, credit_cap: Optional[int]) -> RequestGate:
    if league == "mlb":
        return RequestGate(
            request_budget=RequestBudget(max_requests=request_cap),
            credit_budget=CreditBudget(applicable=False),
            cost_policy=build_mlb_policy())
    return RequestGate(
        request_budget=RequestBudget(max_requests=request_cap),
        credit_budget=CreditBudget(applicable=True, max_credits=credit_cap),
        cost_policy=build_balldontlie_policy())


def run_pilot_cli(
    *,
    league: str,
    manifest_path: Optional[Path],
    scratch_db: Optional[Path],
    checkpoint: Optional[Path] = None,
    resume: bool = False,
    as_json: bool = False,
    code_version: str = "",
    out: Callable[[str], None] = print,
    client_factory: Optional[Callable[[RequestGate], Any]] = None,
    forbidden_paths: tuple[Path, ...] = (),
) -> int:
    """Guarded F1B live pilot, GOVERNED BY A REVIEWED MANIFEST.

    Execution never generates or overrides its own plan: it loads and validates a
    manifest produced earlier by ``--plan --manifest-out`` and drives exactly that.
    Every guard fails before any client/DB work: F1B authorization (off by
    default), manifest presence/validity/policy-consistency/executability, request
    cap, explicit scratch DB, and resume/checkpoint/db-identity matching.
    """

    # 1. F1B authorization boundary (disabled by default; separate reviewed step).
    if not _f1b_authorized():
        out("[FAILED ] F1B live pilot is not authorized. Completing F1A does not "
            "authorize F1B; a separate, reviewed authorization is required "
            f"(dev/test only: set {_F1B_AUTHORIZED_ENV}=1).")
        return EXIT_USAGE

    # 2. A reviewed manifest is REQUIRED; execution cannot self-generate one.
    if manifest_path is None:
        out("[FAILED ] --pilot requires --manifest PATH (generate it with "
            "'--plan --manifest-out PATH' and review it first)")
        return EXIT_USAGE
    if scratch_db is None:
        out("[FAILED ] pilot execution requires an explicit --scratch-db")
        return EXIT_USAGE
    if resume and checkpoint is None:
        out("[FAILED ] --resume requires --checkpoint")
        return EXIT_USAGE

    try:
        manifest = load_and_validate(
            Path(manifest_path), expected_league=league,
            expected_provider=_provider_for(league))
    except ManifestError as exc:
        out(f"[FAILED ] manifest rejected: {exc}")
        return EXIT_USAGE

    # 3. Policy-consistency: rebuild the plan from the manifest's own fields and
    #    require its hash to match; a manifest from a different planner/policy
    #    version must be explicitly regenerated and re-reviewed.
    from_date, to_date = _parse_date_range(manifest.date_range)
    includes = tuple(f for f in manifest.families if f not in ("schedule", "games"))
    bounds = Bounds(max_games=manifest.max_games, max_pages=manifest.max_pages,
                    max_records=manifest.max_records, max_retries=manifest.max_retries)
    rebuilt = build_plan(league=league, from_date=from_date, to_date=to_date,
                         families=manifest.families, stage=manifest.stage, bounds=bounds)
    if plan_hash(rebuilt) != manifest.computed_plan_hash():
        out("[FAILED ] manifest was generated under a different planner/policy "
            "version; regenerate and re-review it ('--plan --manifest-out')")
        return EXIT_USAGE
    if not manifest.executable:
        out("[FAILED ] manifest is non-executable: "
            f"{', '.join(manifest.unresolved_bounds) or 'unbounded/unknown cost'}")
        return EXIT_USAGE
    if manifest.request_cap is None:
        out("[FAILED ] manifest has no request cap")
        return EXIT_USAGE

    # 4. Resume must match the exact manifest (before any DB read).
    expected_fp: Optional[str] = None
    if resume:
        assert checkpoint is not None
        try:
            ck = load_checkpoint(Path(checkpoint))
        except CheckpointError as exc:
            out(f"[FAILED ] {exc}")
            return EXIT_USAGE
        if ck.manifest_hash != manifest.manifest_hash():
            out("[FAILED ] checkpoint does not match this manifest")
            return EXIT_USAGE
        expected_fp = ck.scratch_fingerprint

    # 5. Scratch-database isolation (read-only classification; never mutates a DB).
    try:
        classification = classify_scratch_db(
            scratch_db, resume=resume, expected_fingerprint=expected_fp,
            forbidden_paths=forbidden_paths)
    except ScratchDbError as exc:
        out(f"[FAILED ] {exc}")
        return EXIT_DATABASE_ERROR
    if classification.kind is ScratchClass.NEW:
        out(f"[FAILED ] scratch db does not exist; run 'db-init --db {scratch_db}' "
            "(schema v16) first — ingestion never migrates")
        return EXIT_USAGE
    if classification.kind is ScratchClass.UNSAFE:
        out(f"[FAILED ] unsafe scratch database: {classification.reason}")
        return EXIT_DATABASE_ERROR
    if resume and classification.kind is not ScratchClass.AUTHORIZED_RESUMABLE:
        out(f"[FAILED ] resume rejected: {classification.reason}")
        return EXIT_USAGE

    from ..db.engine import Database
    database = Database(Path(scratch_db))

    def _fingerprint() -> str:
        # Recompute the strong content digest immediately (used at each checkpoint
        # boundary so resume can prove the DB is exactly as the checkpoint left it).
        c = classify_scratch_db(scratch_db, resume=True, expected_fingerprint=None,
                                forbidden_paths=forbidden_paths)
        return c.fingerprint or ""

    if client_factory is None:
        client_factory = _default_client_factory(league)

    executor = _IngestorExecutor(
        league=league, database=database, client_factory=client_factory,
        from_date=from_date, to_date=to_date, includes=includes, stage=manifest.stage,
        max_pages=manifest.max_pages, max_records=manifest.max_records)

    result = run_pilot(
        manifest=manifest,
        gate=_make_gate(league=league, request_cap=manifest.request_cap,
                        credit_cap=manifest.credit_cap),
        executor=executor,
        checkpoint_path=Path(checkpoint) if checkpoint else Path(f"{scratch_db}.ckpt"),
        scratch_fingerprint=classification.fingerprint or "",
        resume=resume, code_version=code_version, fingerprint_fn=_fingerprint)

    if as_json:
        out(json.dumps(result.as_dict(), sort_keys=True))
    else:
        state = "TRUNCATED" if result.truncated else "COMPLETE"
        out(f"pilot {league.upper()} {state}  requests={result.usage['attempted_requests']} "
            f"completed_units={result.completed} skipped={result.skipped}")
        if result.truncated and result.exhaustion is not None:
            out(f"  budget exhausted: {result.exhaustion['limit_type']} "
                f"cap={result.exhaustion['cap']}")
    return EXIT_BUDGET_EXHAUSTED if result.truncated else EXIT_OK


def _default_client_factory(league: str) -> Callable[[RequestGate], Any]:
    """Build the real gated provider client from settings (secrets never echoed)."""

    def factory(gate: RequestGate) -> Any:
        from ..config import load_settings
        settings = load_settings()
        if league == "mlb":
            from ..providers.mlb_statsapi import MlbStatsApiClient
            return MlbStatsApiClient(gate=gate, league="mlb")
        from ..providers.balldontlie import BalldontlieClient
        return BalldontlieClient(settings.nba_data_api_key, gate=gate, league="nba")

    return factory
