"""F1B RICH pilot manifest preparation + planner-vs-executor differential (offline).

The two committed rich manifests (`pilots/f1b/{mlb,nba}_rich.manifest.json`) are
validated here before any live execution is ever authorized. Nothing in this module
touches the network, a real key, or the preserved skeleton artifacts.

The decisive check is the **differential**: the real orchestration
(``run_pilot_cli``) and the real provider clients are driven through mocked
transports, every transport attempt is captured and classified, and the observed
fan-out is compared against the planner's conservative maximum. A manifest is only
safe if the executor can never exceed the plan.

Also pinned here:

* the derived caps (MLB semantic 6 -> cap 12; NBA semantic 7 -> cap 14 at
  ``max_retries=1``), taken from the planner rather than hand-written;
* shared requests are not double-counted (MLB results+inning share one linescore;
  NBA box+quarters share one box_scores);
* ``max_games=1`` and ``max_pages=1`` really prevent a second game / second page;
* the NBA family-vocabulary translation (``stats`` <-> ``player-stats``), whose
  absence silently dropped player statistics in both directions;
* manifest integrity: canonical, deterministic, duplicate-key safe, secret-free,
  hash-sensitive to every bound, and path-isolated from every other artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.checkpoint import load_checkpoint
from sports_quant.ingest.cost_policies import build_balldontlie_policy, build_mlb_policy
from sports_quant.ingest.f1a import (
    _F1B_AUTHORIZED_ENV,
    _families_and_stage,
    _ingestor_includes,
    _normalize_includes,
    _parse_date_range,
    emit_plan,
    run_pilot_cli,
)
from sports_quant.ingest.manifest import (
    ManifestError,
    canonical_json,
    load_and_validate,
    plan_hash,
)
from sports_quant.ingest.planning import Bounds, build_plan
from sports_quant.ingest.tests.test_phase_d2_mlb import boxscore, linescore
from sports_quant.ingest.tests.test_phase_d2_mlb import game as mlb_game
from sports_quant.ingest.tests.test_phase_d2_mlb import schedule as mlb_schedule
from sports_quant.ingest.tests.test_phase_d3_nba import (
    adv_row,
    box_object,
    lineups_body,
    play_row,
    stat_row,
)
from sports_quant.ingest.tests.test_phase_d3_nba import game as nba_game
from sports_quant.ingest.tests.test_phase_d3_nba import (
    page as nba_page,
)
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import RequestGate

REPO = Path(__file__).resolve().parents[3]
MLB_RICH = REPO / "pilots" / "f1b" / "mlb_rich.manifest.json"
NBA_RICH = REPO / "pilots" / "f1b" / "nba_rich.manifest.json"
MLB_SKEL = REPO / "pilots" / "f1b" / "mlb_skeleton.manifest.json"
NBA_SKEL = REPO / "pilots" / "f1b" / "nba_skeleton.manifest.json"

#: Deterministic fixture evidence only -- the games the skeleton pilots selected.
#: Never referenced by production selection logic.
MLB_PK, MLB_PK2 = 822788, 822874
NBA_GID, NBA_GID2 = 18447316, 18447317

SENTINEL = "sk-f1b-rich-sentinel-do-not-store"

#: Derived from the planner (asserted below), not hand-written.
MLB_SEMANTIC_MAX, MLB_CAP = 6, 12
NBA_SEMANTIC_MAX, NBA_CAP = 7, 14

MLB_INCLUDES = ("results", "box", "inning", "rosters")
NBA_INCLUDES = ("box", "player-stats", "advanced", "plays", "lineups", "quarters")


@pytest.fixture
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_F1B_AUTHORIZED_ENV, "1")


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately on DNS, direct HTTP, or any non-loopback connect."""

    real_connect = socket.socket.connect

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("external network access attempted in an offline test")

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            raise AssertionError(f"external connect to {host!r} in an offline test")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)


# ===================================================================== #
# 1. Cap derivation comes from the planner, not from a hand-written number
# ===================================================================== #
def _mlb_plan(max_games: int = 1, max_retries: int = 1):
    fams, stage = _families_and_stage("mlb", MLB_INCLUDES)
    return build_plan(league="mlb", from_date="2026-07-20", to_date="2026-07-21",
                      families=fams, stage=stage,
                      bounds=Bounds(max_games=max_games, max_retries=max_retries))


def _nba_plan(max_games: int = 1, max_pages: int = 1, max_retries: int = 1):
    fams, stage = _families_and_stage("nba", NBA_INCLUDES)
    return build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=fams, stage=stage,
                      bounds=Bounds(max_games=max_games, max_pages=max_pages,
                                    max_records=100, max_retries=max_retries,
                                    rate_per_min=60))


def test_mlb_cap_is_the_planner_derived_conservative_maximum() -> None:
    p = _mlb_plan()
    assert p.stage == "rich"
    # The skeleton family is added automatically by the planner contract.
    assert p.families == ("box", "inning", "results", "rosters", "schedule")
    assert set(c.family for c in p.contingents) == {
        "game_schedule", "game_linescore", "game_boxscore", "roster"}
    # 1 schedule + 1 game_schedule + 1 linescore (results+inning SHARED) + 1 box + 2 rosters
    assert p.semantic_requests_max() == MLB_SEMANTIC_MAX == 6
    assert p.bounds.retry_factor == 2                      # 1 + max_retries(1)
    assert p.requests_max_with_retries() == MLB_CAP == 12
    assert p.required_request_cap() == MLB_CAP
    assert p.executable() and p.unresolved_bounds() == ()


def test_nba_cap_is_the_planner_derived_conservative_maximum() -> None:
    p = _nba_plan()
    assert p.stage == "rich"
    assert p.families == ("advanced", "box", "games", "lineups", "plays",
                          "quarters", "stats")
    fams = [c.family for c in p.contingents]
    assert set(fams) == {"games", "game", "box_scores", "stats", "advanced_stats",
                         "plays", "lineups"}
    # quarters must NOT create a second box_scores contingent.
    assert fams.count("box_scores") == 1
    assert p.semantic_requests_max() == NBA_SEMANTIC_MAX == 7
    assert p.bounds.retry_factor == 2
    assert p.requests_max_with_retries() == NBA_CAP == 14
    assert p.required_request_cap() == NBA_CAP
    assert p.executable() and p.unresolved_bounds() == ()


def test_quarters_alone_still_shares_a_single_box_request() -> None:
    fams, stage = _families_and_stage("nba", ("quarters",))
    p = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                   families=fams, stage=stage,
                   bounds=Bounds(max_games=1, max_pages=1, max_retries=1))
    box = [c for c in p.contingents if c.family == "box_scores"]
    assert len(box) == 1, "quarters must reuse the single box_scores request"


def test_retries_scale_the_cap_but_not_the_semantic_maximum() -> None:
    for retries in (0, 1, 3):
        m, n = _mlb_plan(max_retries=retries), _nba_plan(max_retries=retries)
        assert m.semantic_requests_max() == MLB_SEMANTIC_MAX
        assert n.semantic_requests_max() == NBA_SEMANTIC_MAX
        assert m.required_request_cap() == MLB_SEMANTIC_MAX * (1 + retries)
        assert n.required_request_cap() == NBA_SEMANTIC_MAX * (1 + retries)


def test_committed_rich_manifest_caps_equal_the_planner_maximum() -> None:
    for path, league, provider, cap in ((MLB_RICH, "mlb", "mlb_statsapi", MLB_CAP),
                                        (NBA_RICH, "nba", "balldontlie", NBA_CAP)):
        m = load_and_validate(path, expected_league=league, expected_provider=provider)
        f, t = _parse_date_range(m.date_range)
        rebuilt = build_plan(
            league=league, from_date=f, to_date=t, families=m.families, stage=m.stage,
            bounds=Bounds(max_games=m.max_games, max_pages=m.max_pages,
                          max_records=m.max_records, max_retries=m.max_retries,
                          rate_per_min=m.plan_body.get("bounds", {}).get("rate_per_min")))
        assert m.request_cap == cap == rebuilt.required_request_cap()
        assert m.max_retries == 1
        assert m.max_games == 1


# ===================================================================== #
# 2. The NBA family-vocabulary translation (the repaired mismatch)
# ===================================================================== #
def test_cli_player_stats_include_is_not_silently_dropped() -> None:
    """`--include player-stats` must produce a RICH plan with the `stats` family."""

    fams, stage = _families_and_stage("nba", ("player-stats",))
    assert stage == "rich", "a documented CLI rich group must not collapse to skeleton"
    assert "stats" in fams


def test_manifest_stats_family_reaches_the_ingestor_as_player_stats() -> None:
    """A declared `stats` family must really execute, not vanish silently."""

    assert _ingestor_includes("nba", ("stats",)) == ("player-stats",)
    assert _normalize_includes("nba", ("player-stats",)) == ("stats",)
    # Round-trip is stable and MLB is untouched.
    assert _normalize_includes("nba", ("stats",)) == ("stats",)
    assert _ingestor_includes("mlb", ("results", "box")) == ("results", "box")


def test_ingestor_recognises_every_translated_nba_include() -> None:
    from sports_quant.ingest.nba_ingestor import VALID_INCLUDES

    fams, _stage = _families_and_stage("nba", NBA_INCLUDES)
    rich = tuple(f for f in fams if f != "games")
    for inc in _ingestor_includes("nba", rich):
        assert inc in VALID_INCLUDES, f"{inc!r} is not an ingestor include group"


# ===================================================================== #
# 3. Differential: planner maximum vs REAL mocked executor fan-out
# ===================================================================== #
def _mlb_factory(seen: list[str], *, fail_first: Optional[str] = None):
    """Real MlbStatsApiClient over a mocked transport returning two games."""

    state = {"failed": False}
    sched_all = mlb_schedule(
        mlb_game(game_pk=MLB_PK, official_date="2026-07-20", home_team=133, away_team=147),
        mlb_game(game_pk=MLB_PK2, official_date="2026-07-20", home_team=111, away_team=121),
        date="2026-07-20")

    def handler(request: httpx.Request) -> httpx.Response:
        p = re.sub(r"^/api/v1", "", request.url.path)
        seen.append(p)
        if fail_first is not None and fail_first in p and not state["failed"]:
            state["failed"] = True
            return httpx.Response(503, json={})          # retryable once
        if p.endswith("/schedule"):
            gpk = dict(request.url.params).get("gamePk")
            body = (mlb_schedule(mlb_game(game_pk=int(gpk), official_date="2026-07-20",
                                          home_team=133, away_team=147), date="2026-07-20")
                    if gpk else sched_all)
            return httpx.Response(200, json=body,
                                  headers={"content-type": "application/json"})
        if p.endswith("/boxscore"):
            return httpx.Response(200, json=boxscore(),
                                  headers={"content-type": "application/json"})
        if p.endswith("/linescore"):
            return httpx.Response(200, json=linescore(),
                                  headers={"content-type": "application/json"})
        if "/roster" in p:
            return httpx.Response(200, json={"roster": [
                {"person": {"id": 111, "fullName": "A B"},
                 "position": {"abbreviation": "SS"}, "status": {"code": "A"}}]},
                headers={"content-type": "application/json"})
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        http = build_readonly_client(
            base_url="https://statsapi.mlb.com/api/v1",
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
            inner_transport=httpx.MockTransport(handler))
        return MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=1)

    return factory


def _nba_factory(seen: list[str], *, fail_first: Optional[str] = None):
    state = {"failed": False}
    games = [nba_game(gid=NBA_GID, date="2026-01-05"),
             nba_game(gid=NBA_GID2, date="2026-01-05")]

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        seen.append(p)
        if fail_first is not None and p == fail_first and not state["failed"]:
            state["failed"] = True
            return httpx.Response(503, json={})
        ct = {"content-type": "application/json"}
        if p == "/v1/games":
            return httpx.Response(200, json=nba_page(games), headers=ct)
        if p.startswith("/v1/games/"):
            gid = int(p.rsplit("/", 1)[-1])
            return httpx.Response(
                200, json={"data": nba_game(gid=gid, date="2026-01-05")}, headers=ct)
        if p == "/v1/box_scores":
            return httpx.Response(200, json={"data": [box_object(date="2026-01-05")]},
                                  headers=ct)
        if p == "/v1/stats":
            return httpx.Response(200, json=nba_page([stat_row(gid=NBA_GID)]), headers=ct)
        if p == "/nba/v1/stats/advanced":
            return httpx.Response(200, json=nba_page([adv_row(gid=NBA_GID)]), headers=ct)
        if p == "/v1/plays":
            return httpx.Response(200, json=nba_page([play_row(111)]), headers=ct)
        if p == "/v1/lineups":
            return httpx.Response(200, json=lineups_body(gid=NBA_GID), headers=ct)
        return httpx.Response(200, json={"data": []}, headers=ct)

    def factory(gate: RequestGate) -> BalldontlieClient:
        http = build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler))
        return BalldontlieClient(SENTINEL, client=http, gate=gate, league="nba")

    return factory


def _run(tmp_path: Path, league: str, manifest: Path, factory, resume: bool = False):
    db, ckpt = tmp_path / f"{league}_rich.db", tmp_path / f"{league}_rich.ckpt"
    if not db.exists():
        initialize_database(db)
    lines: list[str] = []
    rc = run_pilot_cli(league=league, manifest_path=manifest, scratch_db=db,
                       checkpoint=ckpt, resume=resume, out=lines.append,
                       client_factory=factory)
    return rc, lines, db, ckpt


def _plan_families(plan) -> set[str]:
    return {u.endpoint_family for u in plan.fixed_units} | {
        c.family for c in plan.contingents}


def test_mlb_differential_executor_never_exceeds_the_plan(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    seen: list[str] = []
    rc, lines, db, ckpt = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)

    pol = build_mlb_policy()
    fams = [pol.classify(p) for p in seen]
    plan = _mlb_plan()

    # (5) every attempted family is modelled by the plan, or is the trusted
    #     classification of a modelled request (rosters classify as `teams`).
    modelled = _plan_families(plan) | {"teams"}
    assert set(fams) <= modelled, f"unmodelled families: {set(fams) - modelled}"
    # Every attempted family must be gate-known, else the gate would fail closed.
    assert all(pol.is_known_family(f) for f in fams), fams

    # (6) actual attempts never exceed the conservative maximum, nor the cap.
    assert len(seen) <= MLB_SEMANTIC_MAX, seen
    ck = load_checkpoint(ckpt)
    assert ck.usage["attempted_requests"] <= MLB_CAP
    assert ck.usage["attempted_requests"] == len(seen)

    # (10) results + inning share ONE linescore request (never two).
    assert fams.count("game_linescore") == 1, seen
    assert fams.count("game_boxscore") == 1
    # TWO schedule documents: the range discovery listing plus the single-game
    # re-fetch, which the planner models as its separate `game_schedule` slot. The
    # classifier maps both to the `schedule` family because the path is the same.
    assert fams.count("schedule") == 2, seen
    assert fams.count("teams") == 2, seen
    # The observed total is EXACTLY the planner's conservative semantic maximum.
    assert len(seen) == MLB_SEMANTIC_MAX == 6, seen

    # (7) max_games=1 -> no rich request for the second game.
    assert not [p for p in seen if str(MLB_PK2) in p], seen
    assert [p for p in seen if str(MLB_PK) in p], "the selected game must be fetched"
    # Selection describes the DISCOVERY pass only; per-game units must not inflate it.
    assert ck.usage["games_received"] == 2
    assert ck.usage["games_selected"] == 1
    assert ck.usage["games_excluded_by_max_games"] == 1
    assert ck.usage["selection_truncated"] is True


def test_nba_differential_executor_never_exceeds_the_plan(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    seen: list[str] = []
    rc, lines, db, ckpt = _run(tmp_path, "nba", NBA_RICH, _nba_factory(seen))
    assert rc == 0, "\n".join(lines)

    pol = build_balldontlie_policy()
    fams = [pol.classify(p) for p in seen]
    plan = _nba_plan()

    modelled = _plan_families(plan)
    assert set(fams) <= modelled, f"unmodelled families: {set(fams) - modelled}"
    assert all(pol.is_known_family(f) for f in fams), fams

    assert len(seen) <= NBA_SEMANTIC_MAX, seen
    ck = load_checkpoint(ckpt)
    assert ck.usage["attempted_requests"] <= NBA_CAP
    assert ck.usage["attempted_requests"] == len(seen)

    # (10) box + quarters share ONE box_scores request.
    assert fams.count("box_scores") == 1, seen
    # The repaired vocabulary means player statistics really are requested.
    assert fams.count("stats") == 1, seen
    assert fams.count("advanced_stats") == 1
    assert fams.count("plays") == 1
    assert fams.count("lineups") == 1

    # (8) one-page bounds -> exactly one games listing page, no second page.
    assert fams.count("games") == 1, seen
    assert ck.usage["pages_fetched"] == 1, "max_pages=1 -> a single listing page"
    # The per-game re-fetch is its own family (`game`), never a listing page.
    assert fams.count("game") == 1, seen
    # The observed total is EXACTLY the planner's conservative semantic maximum.
    assert len(seen) == NBA_SEMANTIC_MAX == 7, seen

    # (7) max_games=1 -> the second game is never fetched.
    assert not [p for p in seen if str(NBA_GID2) in p], seen
    assert ck.usage["games_received"] == 2
    assert ck.usage["games_selected"] == 1
    assert ck.usage["games_excluded_by_max_games"] == 1
    assert ck.usage["selection_truncated"] is True


@pytest.mark.parametrize("league,manifest,factory_for,fail_at,cap", [
    ("mlb", MLB_RICH, _mlb_factory, "/boxscore", MLB_CAP),
    ("nba", NBA_RICH, _nba_factory, "/v1/plays", NBA_CAP),
])
def test_retries_consume_attempts_without_duplicating_semantic_work(
    tmp_path: Path, f1b_authorized: None, no_external_network: None,
    league: str, manifest: Path, factory_for, fail_at: str, cap: int
) -> None:
    """(9) A retried request adds an ATTEMPT but not a second semantic unit."""

    seen: list[str] = []
    rc, lines, _db, ckpt = _run(tmp_path, league, manifest,
                                factory_for(seen, fail_first=fail_at))
    assert rc == 0, "\n".join(lines)
    ck = load_checkpoint(ckpt)
    # The failing endpoint was hit twice (initial + one retry) ...
    retried = [p for p in seen if fail_at in p]
    assert len(retried) == 2, seen
    # ... attempts grew, and stayed inside the retry-inclusive cap ...
    assert ck.usage["retry_attempts"] == 1
    assert ck.usage["attempted_requests"] == len(seen)
    assert ck.usage["attempted_requests"] <= cap
    # ... while the semantic unit count did not double.
    assert len(set(seen)) <= (MLB_SEMANTIC_MAX if league == "mlb" else NBA_SEMANTIC_MAX)


def test_rich_manifest_paths_are_isolated_from_every_other_artifact() -> None:
    """(11) rich scratch/checkpoint paths differ from corpus, skeletons, other league."""

    mlb = json.loads(MLB_RICH.read_text(encoding="utf-8"))
    nba = json.loads(NBA_RICH.read_text(encoding="utf-8"))
    assert mlb["scratch_db"] == "data\\f1b_mlb_rich_scratch.db"
    assert mlb["checkpoint_path"] == "data\\f1b_mlb_rich.ckpt"
    assert nba["scratch_db"] == "data\\f1b_nba_rich_scratch.db"
    assert nba["checkpoint_path"] == "data\\f1b_nba_rich.ckpt"

    forbidden = {
        "data\\corpus.db",
        "data\\f1b_mlb_scratch.db", "data\\f1b_mlb_skeleton.ckpt",
        "data\\f1b_nba_scratch.db", "data\\f1b_nba_skeleton.ckpt",
    }
    rich_paths = {mlb["scratch_db"], mlb["checkpoint_path"],
                  nba["scratch_db"], nba["checkpoint_path"]}
    assert not (rich_paths & forbidden)
    assert len(rich_paths) == 4                     # all four are distinct
    # Cross-league separation.
    assert mlb["scratch_db"] != nba["scratch_db"]
    assert mlb["checkpoint_path"] != nba["checkpoint_path"]
    # Everything stays under the git-ignored data/ directory, relative.
    for p in rich_paths:
        assert not Path(p).is_absolute()
        assert p.replace("\\", "/").startswith("data/")


# ===================================================================== #
# 4. Manifest integrity
# ===================================================================== #
@pytest.mark.parametrize("path,league,provider,cost_v", [
    (MLB_RICH, "mlb", "mlb_statsapi", "mlb-cost-v1"),
    (NBA_RICH, "nba", "balldontlie", "bdl-cost-v1"),
])
def test_rich_manifest_integrity(path: Path, league: str, provider: str,
                                 cost_v: str, tmp_path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    body = json.loads(raw)
    m = load_and_validate(path, expected_league=league, expected_provider=provider)

    assert canonical_json(body) == raw            # canonical bytes ARE the integrity check
    assert m.canonical() == raw
    assert not path.is_symlink()
    assert body["manifest_format_version"] == "f1a-manifest-v1"
    assert body["plan_version"] == "f1a-plan-v1"
    assert body["cost_policy_version"] == cost_v
    assert body["expected_schema_version"] == 16
    assert body["stage"] == "rich"
    assert body["executable"] is True
    assert body["unresolved_bounds"] == []
    assert body["credits_applicable"] is False
    assert body["credit_cap"] is None
    assert m.manifest_hash() == hashlib.sha256(path.read_bytes()).hexdigest()

    # Duplicate-key safety, on a scratch copy only.
    needle = f'"league":"{league}"'
    assert needle in raw
    dup = tmp_path / "dup.json"
    dup.write_text(raw.replace(needle, f"{needle},{needle}", 1), encoding="utf-8")
    with pytest.raises(ManifestError, match="duplicate"):
        load_and_validate(dup, expected_league=league, expected_provider=provider)

    # Secret-free, and no wall-clock in the semantic identity.
    low = raw.lower()
    for banned in ("api_key", "apikey", "authorization", "bearer", "password",
                   "secret", "token", "x-api-key", "http://", "https://"):
        assert banned not in low, banned
    assert not re.search(r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}", low), "no timestamp in identity"


@pytest.mark.parametrize("league,includes,kw", [
    ("mlb", MLB_INCLUDES, {"max_games": 1, "max_retries": 1, "request_cap": MLB_CAP,
                           "from_date": "2026-07-20", "to_date": "2026-07-21",
                           "scratch_db": "data\\f1b_mlb_rich_scratch.db",
                           "checkpoint": "data\\f1b_mlb_rich.ckpt"}),
    ("nba", NBA_INCLUDES, {"max_games": 1, "max_pages": 1, "max_records": 100,
                           "max_retries": 1, "rate_per_min": 60, "request_cap": NBA_CAP,
                           "from_date": "2026-01-05", "to_date": "2026-01-05",
                           "scratch_db": "data\\f1b_nba_rich_scratch.db",
                           "checkpoint": "data\\f1b_nba_rich.ckpt"}),
])
def test_rich_manifest_regenerates_byte_identically(
    tmp_path: Path, league: str, includes: tuple[str, ...], kw: dict
) -> None:
    committed = (MLB_RICH if league == "mlb" else NBA_RICH).read_text(encoding="utf-8")
    out = tmp_path / "regen.json"
    emit_plan(league=league, includes=includes, manifest_out=out,
              out=lambda _s: None, **kw)
    assert out.read_text(encoding="utf-8") == committed
    # Reordered input families produce the SAME canonical manifest.
    out2 = tmp_path / "regen2.json"
    emit_plan(league=league, includes=tuple(reversed(includes)), manifest_out=out2,
              out=lambda _s: None, **kw)
    assert out2.read_text(encoding="utf-8") == committed


@pytest.mark.parametrize("league,includes,base", [
    ("mlb", MLB_INCLUDES, {"max_games": 1, "max_retries": 1, "request_cap": MLB_CAP,
                           "from_date": "2026-07-20", "to_date": "2026-07-21",
                           "scratch_db": "data\\f1b_mlb_rich_scratch.db",
                           "checkpoint": "data\\f1b_mlb_rich.ckpt"}),
    ("nba", NBA_INCLUDES, {"max_games": 1, "max_pages": 1, "max_records": 100,
                           "max_retries": 1, "rate_per_min": 60, "request_cap": NBA_CAP,
                           "from_date": "2026-01-05", "to_date": "2026-01-05",
                           "scratch_db": "data\\f1b_nba_rich_scratch.db",
                           "checkpoint": "data\\f1b_nba_rich.ckpt"}),
])
def test_changing_any_bound_or_path_changes_the_manifest_hash(
    tmp_path: Path, league: str, includes: tuple[str, ...], base: dict
) -> None:
    def gen(name: str, **over: Any) -> str:
        out = tmp_path / f"{name}.json"
        emit_plan(league=league, includes=includes, manifest_out=out,
                  out=lambda _s: None, **{**base, **over})
        return hashlib.sha256(out.read_bytes()).hexdigest()

    ref = gen("ref")
    variants: dict[str, dict[str, Any]] = {
        "max_games": {"max_games": 2, "request_cap": base["request_cap"] * 2},
        "max_retries": {"max_retries": 2, "request_cap": base["request_cap"] * 3},
        "scratch": {"scratch_db": "data\\other_scratch.db"},
        "checkpoint": {"checkpoint": "data\\other.ckpt"},
        "cap": {"request_cap": base["request_cap"] + 1},
        "family": {},  # handled below
    }
    for name, over in variants.items():
        if name == "family":
            out = tmp_path / "fam.json"
            emit_plan(league=league, includes=includes[:-1], manifest_out=out,
                      out=lambda _s: None, **base)
            assert hashlib.sha256(out.read_bytes()).hexdigest() != ref, "family set"
            continue
        assert gen(name, **over) != ref, f"changing {name} must change the hash"
    if league == "nba":
        assert gen("rate", rate_per_min=120) != ref, "configured rate"
        assert gen("pages", max_pages=2,
                   request_cap=base["request_cap"] * 2) != ref, "max_pages"


def test_committed_skeleton_manifests_remain_unchanged() -> None:
    """Preparing the rich manifests must not perturb the reviewed skeleton ones."""

    assert hashlib.sha256(MLB_SKEL.read_bytes()).hexdigest() == (
        "fa28695b043eb38da3de13c1a49dd24adef022d83f40d870e495968351c4cf3b")
    assert hashlib.sha256(NBA_SKEL.read_bytes()).hexdigest() == (
        "6fe6dc37ec4d5868c7f456ba231d4b8c0f6edbda940fdba6f3f41acbb4b1f446")


@pytest.mark.parametrize("league,manifest", [("mlb", MLB_RICH), ("nba", NBA_RICH)])
def test_rich_manifest_is_not_overridable_by_conflicting_cli_args(
    tmp_path: Path, f1b_authorized: None, league: str, manifest: Path
) -> None:
    """A plan-shaping flag alongside --pilot must fail closed before any work."""

    from sports_quant.cli import main

    db = tmp_path / "s.db"
    initialize_database(db)
    for flag, value in (("--max-games", "9"), ("--max-retries", "9"),
                        ("--request-cap", "999"), ("--from", "2020-01-01")):
        rc = main([f"ingest-{league}", "--pilot", "--manifest", str(manifest),
                   "--scratch-db", str(db), flag, value])
        assert rc == 2, f"{flag} must be refused alongside --pilot"


def test_plan_hash_rebuilds_from_each_committed_rich_manifest() -> None:
    for path, league, provider in ((MLB_RICH, "mlb", "mlb_statsapi"),
                                   (NBA_RICH, "nba", "balldontlie")):
        m = load_and_validate(path, expected_league=league, expected_provider=provider)
        f, t = _parse_date_range(m.date_range)
        rebuilt = build_plan(
            league=league, from_date=f, to_date=t, families=m.families, stage=m.stage,
            bounds=Bounds(max_games=m.max_games, max_pages=m.max_pages,
                          max_records=m.max_records, max_retries=m.max_retries,
                          rate_per_min=m.plan_body.get("bounds", {}).get("rate_per_min")))
        assert plan_hash(rebuilt) == m.computed_plan_hash()
        assert rebuilt.date_range == m.date_range


# ===================================================================== #
# 5/6. Persisted-family contract + reporting contract for RICH runs
# ===================================================================== #
def test_mlb_rich_persists_only_its_own_families(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    import sqlite3

    seen: list[str] = []
    rc, lines, db, _ck = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        def n(t: str) -> int:
            return con.execute(f'select count(*) from "{t}"').fetchone()[0]

        # Expected MLB rich families.
        assert n("game_schedule_snapshots") >= 1
        assert n("provider_game_references") >= 1
        assert n("provider_team_references") >= 1
        assert n("raw_responses") == len(seen)
        # A rich pilot is two atomic units (discovery + one per-game), so it records
        # two ingestion runs -- one per committed unit.
        assert n("ingestion_runs") == 2
        assert n("game_result_snapshots") >= 1            # results
        assert n("mlb_inning_lines") >= 1                 # inning/linescore
        assert n("team_game_statistics") >= 1             # box-derived
        assert n("roster_snapshots") >= 1                 # rosters
        assert n("provider_player_references") >= 1
        # Nothing from another league or an unrelated provider.
        for t in ("nba_game_results", "nba_player_statistics", "nba_team_statistics",
                  "nba_quarter_lines", "sportsbook_price_snapshots", "kalshi_markets",
                  "kalshi_orderbook_snapshots", "weather_snapshots"):
            assert n(t) == 0, t
    finally:
        con.close()


def test_nba_rich_persists_only_its_own_families(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    import sqlite3

    seen: list[str] = []
    rc, lines, db, _ck = _run(tmp_path, "nba", NBA_RICH, _nba_factory(seen))
    assert rc == 0, "\n".join(lines)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        def n(t: str) -> int:
            return con.execute(f'select count(*) from "{t}"').fetchone()[0]

        assert n("game_schedule_snapshots") >= 1
        assert n("provider_game_references") >= 1
        assert n("provider_team_references") >= 1
        assert n("raw_responses") == len(seen)
        assert n("ingestion_runs") == 2
        assert n("nba_player_statistics") >= 1            # stats (repaired vocabulary)
        assert n("play_snapshots") >= 1                    # plays
        assert n("nba_quarter_lines") >= 1                 # quarters, from the one box
        # Nothing from another league or unrelated provider.
        for t in ("mlb_inning_lines", "probable_pitcher_snapshots",
                  "sportsbook_price_snapshots", "kalshi_markets",
                  "kalshi_orderbook_snapshots", "weather_snapshots"):
            assert n(t) == 0, t
    finally:
        con.close()


@pytest.mark.parametrize("league,manifest,factory_for,rate_active", [
    ("mlb", MLB_RICH, _mlb_factory, False),
    ("nba", NBA_RICH, _nba_factory, True),
])
def test_rich_reports_are_deterministic_and_secret_free(
    tmp_path: Path, f1b_authorized: None, no_external_network: None,
    league: str, manifest: Path, factory_for, rate_active: bool
) -> None:
    seen: list[str] = []
    rc, lines, _db, ckpt = _run(tmp_path, league, manifest, factory_for(seen))
    assert rc == 0, "\n".join(lines)
    u = load_checkpoint(ckpt).usage

    # Every repaired reporting field is present and coherent for a RICH run.
    for field in ("games_received", "games_selected", "games_excluded_by_max_games",
                  "selection_truncated", "pages_fetched", "rate_policy_active",
                  "rate_limited", "throttle_events", "throttle_wait_seconds",
                  "http_429s", "authentication_status", "tier_status", "tier_verified",
                  "tier_evidence_source", "prior_transport_starts",
                  "prior_pages_fetched", "budget_exhausted", "blocked_requests"):
        assert field in u, field

    assert u["rate_policy_active"] is rate_active
    assert u["rate_limited"] is False                 # nothing was throttled
    assert u["throttle_events"] == 0
    assert float(u["throttle_wait_seconds"]) == 0.0
    assert u["http_429s"] == 0
    # Selection truncation is reported, and stays separate from budget truncation.
    assert u["selection_truncated"] is True
    assert u["games_excluded_by_max_games"] == 1
    assert u["budget_exhausted"] is None
    assert u["blocked_requests"] == 0
    # Tier is NEVER claimed as verified from a rich run alone.
    assert u["tier_verified"] is False
    assert u["tier_evidence_source"] == "none"
    if league == "nba":
        assert u["authentication_succeeded"] is True
        assert u["tier_status"] == "configured_not_verified:goat"
    else:
        assert u["authentication_status"] == "not_applicable"
        assert u["tier_status"] == "not_applicable"

    # Deterministic + secret-free human and JSON reports.
    text = "\n".join(lines)
    assert SENTINEL not in text
    assert SENTINEL not in ckpt.read_text(encoding="utf-8")
    blob = json.dumps(u, sort_keys=True, default=str)
    assert SENTINEL not in blob
    for banned in ("api_key", "apikey", "authorization", "bearer"):
        assert banned not in blob.lower()
    assert "selection_truncated=True" in text
    assert "rate_limited=False" in text


def test_rich_totals_expose_logical_run_transport_provenance() -> None:
    """`total_*` helpers combine prior and current-process counters."""

    from sports_quant.request_control import UsageReport

    u = UsageReport(provider="balldontlie", transport_starts=6, pages_fetched=1,
                    prior_transport_starts=6, prior_pages_fetched=1)
    assert u.total_transport_starts == 12
    assert u.total_pages_fetched == 2


def test_rich_plan_mode_makes_no_network_and_writes_no_database(
    tmp_path: Path, no_external_network: None
) -> None:
    """Regenerating either rich plan is pure: zero network, zero DB."""

    cases: tuple[tuple[str, tuple[str, ...], dict[str, Any]], ...] = (
        ("mlb", MLB_INCLUDES, {"max_games": 1, "max_retries": 1, "request_cap": MLB_CAP,
                               "from_date": "2026-07-20", "to_date": "2026-07-21"}),
        ("nba", NBA_INCLUDES, {"max_games": 1, "max_pages": 1, "max_records": 100,
                               "max_retries": 1, "rate_per_min": 60,
                               "request_cap": NBA_CAP,
                               "from_date": "2026-01-05", "to_date": "2026-01-05"}),
    )
    for league, includes, kw in cases:
        out = tmp_path / f"{league}_plan.json"
        rc = emit_plan(league=league, includes=includes, manifest_out=out,
                       out=lambda _s: None, **kw)
        assert rc == 0
        body = json.loads(out.read_text(encoding="utf-8"))
        assert body["stage"] == "rich" and body["executable"] is True
    assert not list(tmp_path.glob("*.db"))
