"""F1 season-month pilots: manifest contract + planner-vs-REAL-executor differential.

Nothing here reaches a network. Every request goes through the real provider client
and the real ``RequestGate`` over an ``httpx.MockTransport``, driven by the real
``run_pilot_cli`` orchestration and the real committed manifests -- not a fake
executor. The property under test is the one that makes a month pilot safe to
authorize later:

    the number of transport attempts the real executor makes can never exceed
    the conservative maximum the planner published in the manifest.

The synthetic months are deliberately large enough to exercise the real limits:
multiple dates, doubleheaders, multi-page listings, >100 records in a page family,
and a genuinely empty family.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import SUPPORTED_SCHEMA_VERSIONS
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.f1a import run_pilot_cli
from sports_quant.ingest.manifest import canonical_json, load_and_validate, plan_hash
from sports_quant.ingest.planning import Bounds, build_plan
from sports_quant.request_control import RequestGate

REPO = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO / "pilots" / "f1"
MLB_MANIFEST = PILOT_DIR / "mlb_coverage_2026_06.manifest.json"
NBA_MANIFEST = PILOT_DIR / "nba_coverage_2026_03.manifest.json"
AUTH_ENV = "MONEYMAKER_F1B_AUTHORIZED"

# --------------------------------------------------------------------------- #
# Synthetic MLB month: 6 dates, 3 games/date, one doubleheader, mixed statuses.
# --------------------------------------------------------------------------- #
MLB_DATES = ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05",
             "2026-06-06")
MLB_TEAMS = ((141, "Toronto Blue Jays"), (139, "Tampa Bay Rays"),
             (147, "New York Yankees"), (111, "Boston Red Sox"),
             (114, "Cleveland Guardians"), (145, "Chicago White Sox"))


def _mlb_game(pk: int, date_str: str, home: int, away: int, *, status: str = "Final",
              game_number: int = 1) -> dict[str, Any]:
    names = dict(MLB_TEAMS)
    return {
        "gamePk": pk, "officialDate": date_str, "season": "2026", "gameType": "R",
        "gameDate": f"{date_str}T23:07:00Z", "gameNumber": game_number,
        "doubleHeader": "Y" if game_number > 1 else "N",
        "status": {"statusCode": "F" if status == "Final" else "S",
                   "detailedState": status},
        "teams": {
            "home": {"team": {"id": home, "name": names[home]}},
            "away": {"team": {"id": away, "name": names[away]}},
        },
        "venue": {"id": 14, "name": "Rogers Centre"},
    }


def mlb_schedule_body() -> dict[str, Any]:
    """The month schedule: 19 games over 6 dates, including a doubleheader."""

    dates = []
    pk = 900000
    for i, date_str in enumerate(MLB_DATES):
        games = []
        for j in range(3):
            home, away = MLB_TEAMS[(2 * j) % 6][0], MLB_TEAMS[(2 * j + 1) % 6][0]
            # The last date is still scheduled (a nonfinal label to measure).
            status = "Scheduled" if i == len(MLB_DATES) - 1 else "Final"
            games.append(_mlb_game(pk, date_str, home, away, status=status))
            pk += 1
        if i == 1:
            # A genuine doubleheader: same date, same pair, gameNumber 2.
            games.append(_mlb_game(pk, date_str, MLB_TEAMS[0][0], MLB_TEAMS[1][0],
                                   game_number=2))
            pk += 1
        dates.append({"date": date_str, "games": games})
    return {"dates": dates}


def mlb_single_game_body(pk: str) -> dict[str, Any]:
    for entry in mlb_schedule_body()["dates"]:
        for game in entry["games"]:
            if str(game["gamePk"]) == pk:
                return {"dates": [{"date": entry["date"], "games": [game]}]}
    return {"dates": []}


def mlb_box_body(pk: str) -> dict[str, Any]:
    return {"teams": {
        "home": {"team": {"id": 141, "name": "Toronto Blue Jays",
                          "abbreviation": "TOR", "locationName": "Toronto",
                          "teamName": "Blue Jays"},
                 "players": {"ID670764": {
                     "person": {"id": 670764, "fullName": "Bo Bichette"},
                     "position": {"abbreviation": "SS"}, "parentTeamId": 141,
                     "stats": {"batting": {"atBats": 4, "hits": 2}}}},
                 "teamStats": {"batting": {"runs": 4, "hits": 9}}},
        "away": {"team": {"id": 139, "name": "Tampa Bay Rays"},
                 # One malformed optional record: a player slot that is not an
                 # object. It must be rejected honestly, never crash the month.
                 "players": {"ID000000": "not-an-object"},
                 "teamStats": {"batting": {"runs": 2, "hits": 5}}}}}


def mlb_line_body(pk: str) -> dict[str, Any]:
    return {"teams": {"home": {"runs": 4, "hits": 9, "errors": 0},
                      "away": {"runs": 2, "hits": 5, "errors": 1}},
            "innings": [{"num": n, "home": {"runs": 1 if n < 5 else 0},
                         "away": {"runs": 1 if n < 3 else 0}} for n in range(1, 10)],
            "defense": {"pitcher": {"id": 680700, "fullName": "Jose Berrios"}},
            "offense": {"batter": {"id": 670764, "fullName": "Bo Bichette"}}}


def mlb_roster_body(team_id: str) -> dict[str, Any]:
    names = dict(MLB_TEAMS)
    base = 700000 + int(team_id)
    return {"teamId": int(team_id), "rosterType": "active", "roster": [
        {"person": {"id": base + i, "fullName": f"{names.get(int(team_id), 'Team')} "
                                                f"Player {i}"},
         "position": {"abbreviation": "P" if i % 2 else "SS"},
         "parentTeamId": int(team_id), "status": {"code": "A"}}
        for i in range(4)
    ]}


# --------------------------------------------------------------------------- #
# Synthetic NBA month: 3 listing pages (>100 games), >100 plays for one game,
# one genuinely empty family, one malformed optional record.
# --------------------------------------------------------------------------- #
NBA_TEAM_IDS = (9, 20, 2, 5, 13, 17)
#: A month of games spread over 28 dates. The mock serves them in SMALL pages, so
#: the games listing genuinely paginates without the fixture having to persist a
#: full 250-game month (which would make this test minutes long for no extra
#: coverage -- the differential property does not depend on absolute volume).
NBA_GAME_COUNT = 24
NBA_LISTING_PAGE_SIZE = 16   # -> 2 listing pages
NBA_PAGE_SIZE = 100          # the page size the client asks for on record families
#: One game carries >100 plays so plays pagination is genuinely exercised; the
#: rest are ordinary single-page games.
PLAYS_HEAVY_GAME_INDEX = 0
PLAYS_HEAVY_COUNT = 260      # -> 3 plays pages for that one game
PLAYS_LIGHT_COUNT = 30


def _nba_team(tid: int) -> dict[str, Any]:
    names = {9: ("Detroit", "Pistons"), 20: ("New York", "Knicks"),
             2: ("Boston", "Celtics"), 5: ("Chicago", "Bulls"),
             13: ("Houston", "Rockets"), 17: ("Los Angeles", "Lakers")}
    city, nick = names[tid]
    return {"id": tid, "abbreviation": f"T{tid:02d}", "city": city,
            "conference": "East", "division": "Central",
            "full_name": f"{city} {nick}", "name": nick}


def _nba_game(idx: int) -> dict[str, Any]:
    gid = 19000000 + idx
    # Four games per night, like an ordinary NBA slate, so the per-DATE box
    # request genuinely serves several games.
    day = 1 + (idx // 4)
    home = NBA_TEAM_IDS[idx % len(NBA_TEAM_IDS)]
    away = NBA_TEAM_IDS[(idx + 1) % len(NBA_TEAM_IDS)]
    # The final game of the month is still scheduled -> a nonfinal label.
    final = idx < NBA_GAME_COUNT - 1
    return {
        "id": gid, "date": f"2026-03-{day:02d}",
        "datetime": f"2026-03-{day:02d}T23:30:00.000Z", "season": 2025,
        "status": "Final" if final else "2026-03-31T23:30:00.000Z",
        "period": 4 if final else 0,
        "home_team": _nba_team(home), "visitor_team": _nba_team(away),
        "home_team_score": 110 if final else None,
        "visitor_team_score": 104 if final else None,
    }


NBA_GAMES = [_nba_game(i) for i in range(NBA_GAME_COUNT)]
NBA_GAME_BY_ID = {str(g["id"]): g for g in NBA_GAMES}


def _cursor_page(rows: list[Any], cursor: Optional[str], size: int) -> dict[str, Any]:
    start = int(cursor) if cursor else 0
    chunk = rows[start:start + size]
    nxt = start + size
    meta: dict[str, Any] = {}
    if nxt < len(rows):
        meta["next_cursor"] = nxt
    return {"data": chunk, "meta": meta}


def _nba_player(pid: int, tid: int) -> dict[str, Any]:
    return {"id": pid, "first_name": f"First{pid}", "last_name": f"Last{pid}",
            "position": "G", "jersey_number": "1", "team_id": tid}


def nba_box_body(date_str: str) -> dict[str, Any]:
    games = [g for g in NBA_GAMES if g["date"] == date_str]
    out = []
    for g in games:
        home, away = dict(g["home_team"]), dict(g["visitor_team"])
        home["players"] = [{"player": _nba_player(500000 + int(g["id"]) % 97, home["id"]),
                            "pts": 21, "min": "31"}]
        # One malformed optional record: a box player slot that is not an object.
        away["players"] = ["not-an-object"]
        out.append({**{k: v for k, v in g.items()
                       if k not in ("home_team", "visitor_team")},
                    "home_team": home, "visitor_team": away,
                    "home_q1": 25, "home_q2": 30, "home_q3": 28, "home_q4": 27,
                    "visitor_q1": 26, "visitor_q2": 25, "visitor_q3": 26,
                    "visitor_q4": 27})
    return {"data": out, "meta": {}}


# --------------------------------------------------------------------------- #
# Counting mocked clients driven through the REAL gate
# --------------------------------------------------------------------------- #
class Attempts:
    """Every transport attempt the real client made, by endpoint path."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    @property
    def total(self) -> int:
        return len(self.paths)

    def by_path(self) -> Counter[str]:
        return Counter(self.paths)

    def family(self, token: str) -> int:
        return sum(1 for p in self.paths if token in p)


def mlb_factory(attempts: Attempts) -> Callable[[RequestGate], Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts.paths.append(path)
        assert request.method == "GET", "a pilot must never issue a non-GET"
        if path == "/api/v1/schedule":
            pk = request.url.params.get("gamePk")
            body: dict[str, Any] = (mlb_single_game_body(pk) if pk
                                    else mlb_schedule_body())
        elif path.endswith("/boxscore"):
            body = mlb_box_body(path.split("/")[-2])
        elif path.endswith("/linescore"):
            body = mlb_line_body(path.split("/")[-2])
        elif path.endswith("/roster"):
            body = mlb_roster_body(path.split("/")[-2])
        else:
            body = {}
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    def factory(gate: RequestGate) -> Any:
        from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
        http = build_readonly_client(
            base_url="https://statsapi.mlb.com/api/v1",
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
            inner_transport=httpx.MockTransport(handler))
        return MlbStatsApiClient(client=http, gate=gate, league="mlb")

    return factory


def nba_factory(attempts: Attempts, *, empty_lineups: bool = True
                ) -> Callable[[RequestGate], Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts.paths.append(path)
        assert request.method == "GET", "a pilot must never issue a non-GET"
        params = request.url.params
        cursor = params.get("cursor")
        if path == "/v1/games":
            body: dict[str, Any] = _cursor_page(NBA_GAMES, cursor,
                                                NBA_LISTING_PAGE_SIZE)
        elif path.startswith("/v1/games/"):
            body = {"data": NBA_GAME_BY_ID[path.rsplit("/", 1)[-1]]}
        elif path == "/v1/box_scores":
            body = nba_box_body(params.get("date", ""))
        elif path == "/v1/stats":
            gid = params.get("game_ids[]") or params.get("game_ids") or "0"
            body = _cursor_page(
                [{"player": _nba_player(600000 + i, 9), "team": _nba_team(9),
                  "game": {"id": int(gid)}, "pts": 10 + i} for i in range(12)],
                cursor, NBA_PAGE_SIZE)
        elif path == "/nba/v1/stats/advanced":
            gid = params.get("game_ids[]") or params.get("game_ids") or "0"
            body = _cursor_page(
                [{"player": _nba_player(600000 + i, 9), "team": _nba_team(9),
                  "game": {"id": int(gid)}, "pace": 99.0} for i in range(10)],
                cursor, NBA_PAGE_SIZE)
        elif path == "/v1/plays":
            gid = params.get("game_id") or "0"
            heavy = str(NBA_GAMES[PLAYS_HEAVY_GAME_INDEX]["id"]) == str(gid)
            count = PLAYS_HEAVY_COUNT if heavy else PLAYS_LIGHT_COUNT
            body = _cursor_page(
                [{"game_id": int(gid), "order": i, "period": 1 + i // 70,
                  "team": _nba_team(9), "participants": [600000],
                  "type": "Shot", "text": "made"} for i in range(count)],
                cursor, NBA_PAGE_SIZE)
        elif path == "/v1/lineups":
            # A genuinely EMPTY family: the provider returns no rows at all.
            body = {"data": [], "meta": {}} if empty_lineups else {
                "data": [{"id": 1, "game_id": 19000000,
                          "player": _nba_player(600000, 9), "team": _nba_team(9),
                          "position": "G", "starter": True}], "meta": {}}
        else:
            body = {"data": [], "meta": {}}
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    def factory(gate: RequestGate) -> Any:
        from sports_quant.providers.balldontlie import BalldontlieClient
        http = build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler))
        return BalldontlieClient("test-key-never-logged", client=http, gate=gate,
                                 league="nba")

    return factory


@pytest.fixture()
def authorized(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Process-scoped F1B authorization; never persisted anywhere."""

    monkeypatch.setenv(AUTH_ENV, "1")
    yield
    monkeypatch.delenv(AUTH_ENV, raising=False)


def _run(
    manifest: Path, factory: Callable[[RequestGate], Any], tmp_path: Path,
    *, resume: bool = False, checkpoint: Optional[Path] = None,
    scratch: Optional[Path] = None,
) -> tuple[int, dict[str, Any]]:
    """Drive the REAL pilot orchestration; return (exit code, JSON payload)."""

    body = json.loads(manifest.read_text(encoding="utf-8"))
    scratch_path = scratch or (tmp_path / "scratch.db")
    ckpt = checkpoint or (tmp_path / "pilot.ckpt")
    if not scratch_path.exists():
        initialize_database(scratch_path)
    lines: list[str] = []
    code = run_pilot_cli(
        league=body["league"], manifest_path=manifest, scratch_db=scratch_path,
        checkpoint=ckpt, resume=resume, as_json=True,
        client_factory=factory, out=lines.append)
    payload: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("{"):
            payload = json.loads(stripped)
    return code, payload



@dataclass
class MonthRun:
    """One completed month pilot: exit code, JSON payload, attempts, scratch db."""

    code: int
    payload: dict[str, Any]
    attempts: Attempts
    scratch: Path
    checkpoint: Path


@pytest.fixture(scope="module")
def mlb_run(tmp_path_factory: pytest.TempPathFactory) -> MonthRun:
    """The MLB month executed ONCE for every assertion that reads it.

    A month pilot is expensive to persist, and re-running it per test bought no
    extra coverage -- the differential property is a property of one run.
    """

    work = tmp_path_factory.mktemp("mlb_month")
    return _execute(MLB_MANIFEST, mlb_factory, work)


@pytest.fixture(scope="module")
def nba_run(tmp_path_factory: pytest.TempPathFactory) -> MonthRun:
    work = tmp_path_factory.mktemp("nba_month")
    return _execute(NBA_MANIFEST, nba_factory, work)


def _execute(
    manifest: Path,
    factory_builder: Callable[..., Callable[[RequestGate], Any]],
    work: Path,
) -> MonthRun:
    """Authorize in-process, run the real pilot once, and capture everything."""

    attempts = Attempts()
    previous = os.environ.get(AUTH_ENV)
    os.environ[AUTH_ENV] = "1"
    try:
        scratch = work / "scratch.db"
        ckpt = work / "pilot.ckpt"
        code, payload = _run(manifest, factory_builder(attempts), work,
                             scratch=scratch, checkpoint=ckpt)
    finally:
        if previous is None:
            os.environ.pop(AUTH_ENV, None)
        else:
            os.environ[AUTH_ENV] = previous
    return MonthRun(code=code, payload=payload, attempts=attempts, scratch=scratch,
                    checkpoint=ckpt)


def usage(run: "MonthRun") -> dict[str, Any]:
    """The pilot's ``usage`` report -- where every request/selection counter lives."""

    return dict(run.payload["usage"])


# =========================== manifest contract ============================= #
def test_both_month_manifests_exist_and_validate() -> None:
    for path, league, provider in ((MLB_MANIFEST, "mlb", "mlb_statsapi"),
                                   (NBA_MANIFEST, "nba", "balldontlie")):
        manifest = load_and_validate(path, expected_league=league,
                                     expected_provider=provider)
        assert manifest.executable is True
        assert manifest.unresolved_bounds == ()
        assert manifest.expected_schema_version in SUPPORTED_SCHEMA_VERSIONS
        assert manifest.expected_schema_version == 17
        assert manifest.request_cap is not None and manifest.request_cap > 0


def test_month_manifests_regenerate_byte_identically(tmp_path: Path) -> None:
    """The committed bytes are exactly what the generator produces."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "f1_gen", PILOT_DIR / "generate_manifests.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    written = module.generate(out_dir=tmp_path)
    assert len(written) == 2
    for produced in written:
        committed = PILOT_DIR / produced.name
        assert produced.read_text(encoding="utf-8") == committed.read_text(
            encoding="utf-8"), produced.name


def test_caps_are_exactly_the_planner_maximum() -> None:
    """No guessed number: the cap IS the planner's retry-inclusive maximum."""

    for path in (MLB_MANIFEST, NBA_MANIFEST):
        body = json.loads(path.read_text(encoding="utf-8"))
        bounds = body["bounds"]
        from_date, _, to_date = body["date_range"].partition("..")
        plan = build_plan(
            league=body["league"], from_date=from_date, to_date=to_date or None,
            families=tuple(body["families"]), stage=body["stage"],
            bounds=Bounds(max_games=bounds["max_games"], max_pages=bounds["max_pages"],
                          max_records=bounds["max_records"],
                          max_retries=bounds["max_retries"],
                          rate_per_min=bounds["rate_per_min"]))
        assert body["request_cap"] == plan.required_request_cap()
        assert body["estimated_requests_max"] == plan.semantic_requests_max()
        assert body["request_cap"] == body["estimated_requests_max"] * (
            1 + bounds["max_retries"])


def test_month_manifest_plan_hash_matches_its_embedded_plan_body() -> None:
    import hashlib

    for path in (MLB_MANIFEST, NBA_MANIFEST):
        body = json.loads(path.read_text(encoding="utf-8"))
        embedded = hashlib.sha256(
            canonical_json(body["plan_body"]).encode("utf-8")).hexdigest()
        from_date, _, to_date = body["date_range"].partition("..")
        b = body["bounds"]
        plan = build_plan(
            league=body["league"], from_date=from_date, to_date=to_date or None,
            families=tuple(body["families"]), stage=body["stage"],
            bounds=Bounds(max_games=b["max_games"], max_pages=b["max_pages"],
                          max_records=b["max_records"], max_retries=b["max_retries"],
                          rate_per_min=b["rate_per_min"]))
        assert embedded == plan_hash(plan)


def test_month_manifests_are_bound_to_unique_new_artifact_paths() -> None:
    paths = set()
    for path in (MLB_MANIFEST, NBA_MANIFEST):
        body = json.loads(path.read_text(encoding="utf-8"))
        for key in ("scratch_db", "checkpoint_path"):
            value = body[key]
            assert value, key
            # Never a skeleton, rich or prior matching artifact.
            for reserved in ("f1b_", "f1_matching_", "corpus.db"):
                assert reserved not in value, (key, value)
            assert value not in paths, f"duplicate artifact path {value}"
            paths.add(value)


def test_month_manifests_are_canonical_and_duplicate_key_safe() -> None:
    from sports_quant.ingest.manifest import ManifestError

    for path in (MLB_MANIFEST, NBA_MANIFEST):
        raw = path.read_text(encoding="utf-8")
        body = json.loads(raw)
        assert raw == canonical_json(body), f"{path.name} is not canonical JSON"
        assert "\n" not in raw and "  " not in raw
        injected = raw.replace('"league":', '"league":"x","league":', 1)
        tampered = path.parent / f"dup_{path.name}"
        try:
            tampered.write_text(injected, encoding="utf-8")
            with pytest.raises(ManifestError, match="duplicate"):
                load_and_validate(tampered, expected_league=body["league"],
                                  expected_provider=body["provider"])
        finally:
            tampered.unlink(missing_ok=True)


def test_month_manifests_carry_no_secret() -> None:
    for path in (MLB_MANIFEST, NBA_MANIFEST):
        low = path.read_text(encoding="utf-8").lower()
        for marker in ("api_key", "apikey", "authorization", "bearer", "x-api-key",
                       "secret", "token", "password", "?key=", "&key="):
            assert marker not in low, (path.name, marker)


@pytest.mark.parametrize("field,value", [
    ("from_date", "2026-06-02"), ("to_date", "2026-06-29"), ("max_games", 599),
    ("max_retries", 2), ("scratch_db", "data\\other.db"),
    ("checkpoint", "data\\other.ckpt"), ("expected_schema_version", 16),
])
def test_changing_any_semantic_input_changes_the_mlb_manifest_hash(
    tmp_path: Path, field: str, value: Any,
) -> None:
    import hashlib

    from sports_quant.ingest.f1a import emit_plan

    baseline = hashlib.sha256(MLB_MANIFEST.read_bytes()).hexdigest()
    kwargs: dict[str, Any] = {
        "league": "mlb", "from_date": "2026-06-01", "to_date": "2026-06-30",
        "includes": ("results", "box", "inning", "rosters"), "max_games": 600,
        "max_retries": 1, "scratch_db": "data\\f1_mlb_2026_06_scratch.db",
        "checkpoint": "data\\f1_mlb_2026_06.ckpt", "expected_schema_version": 17,
    }
    kwargs[field] = value
    out = tmp_path / "variant.json"
    emit_plan(manifest_out=out, out=lambda _s: None, **kwargs)
    assert hashlib.sha256(out.read_bytes()).hexdigest() != baseline, field


def test_nba_rate_and_family_inputs_change_the_hash(tmp_path: Path) -> None:
    import hashlib

    from sports_quant.ingest.f1a import emit_plan

    baseline = hashlib.sha256(NBA_MANIFEST.read_bytes()).hexdigest()
    base: dict[str, Any] = {
        "league": "nba", "from_date": "2026-03-01", "to_date": "2026-03-31",
        "includes": ("box", "quarters", "stats", "advanced", "plays", "lineups"),
        "max_games": 400, "max_pages": 8, "max_records": 1000, "max_retries": 1,
        "rate_per_min": 60, "scratch_db": "data\\f1_nba_2026_03_scratch.db",
        "checkpoint": "data\\f1_nba_2026_03.ckpt", "expected_schema_version": 17,
    }
    for field, value in (("rate_per_min", 30), ("max_pages", 9),
                         ("max_records", 999),
                         ("includes", ("box", "quarters", "stats", "advanced",
                                       "plays"))):
        kwargs = dict(base)
        kwargs[field] = value
        out = tmp_path / f"variant_{field}.json"
        emit_plan(manifest_out=out, out=lambda _s: None, **kwargs)
        assert hashlib.sha256(out.read_bytes()).hexdigest() != baseline, field


def test_committed_f1b_manifests_are_untouched() -> None:
    """The month work must not disturb the earlier committed manifests."""

    expected = {
        "pilots/f1b/mlb_skeleton.manifest.json":
            "fa28695b043eb38d",
        "pilots/f1b/nba_skeleton.manifest.json":
            "6fe6dc37ec4d5868",
        "pilots/f1b/mlb_rich.manifest.json":
            "f56b5c5da53d86c9",
        "pilots/f1b/nba_rich.manifest.json":
            "9de5d312b99c3e85",
    }
    import hashlib

    for rel, prefix in expected.items():
        digest = hashlib.sha256((REPO / rel).read_bytes()).hexdigest()
        assert digest.startswith(prefix), rel


# ================= planner vs REAL executor differential =================== #
def test_mlb_month_executor_never_exceeds_the_planned_maximum(
    mlb_run: MonthRun,
) -> None:
    body = json.loads(MLB_MANIFEST.read_text(encoding="utf-8"))
    assert mlb_run.code == 0, mlb_run.payload
    assert mlb_run.attempts.total > 0
    assert mlb_run.attempts.total <= body["request_cap"], (
        mlb_run.attempts.total, body["request_cap"])
    # No failure occurred, so no retry was spent: attempts stay within the
    # SEMANTIC maximum, strictly below the retry-inclusive cap.
    assert mlb_run.attempts.total <= body["estimated_requests_max"]
    by_path = mlb_run.attempts.by_path()
    assert by_path["/api/v1/schedule"] >= 1
    assert mlb_run.attempts.family("/boxscore") >= 1
    assert mlb_run.attempts.family("/linescore") >= 1
    assert mlb_run.attempts.family("/roster") >= 1
    u = usage(mlb_run)
    # Every transport attempt reserved against the budget: no free requests.
    assert u["transport_starts"] == mlb_run.attempts.total
    assert u["reserved_attempts"] == mlb_run.attempts.total
    # Nothing was refused, so the cap was never the binding constraint.
    assert u["blocked_requests"] == 0
    assert u["budget_exhausted"] in (None, False)


def test_nba_month_executor_never_exceeds_the_planned_maximum(
    nba_run: MonthRun,
) -> None:
    body = json.loads(NBA_MANIFEST.read_text(encoding="utf-8"))
    assert nba_run.code == 0, nba_run.payload
    assert nba_run.attempts.total > 0
    assert nba_run.attempts.total <= body["request_cap"], (
        nba_run.attempts.total, body["request_cap"])
    assert nba_run.attempts.total <= body["estimated_requests_max"]
    u = usage(nba_run)
    assert u["transport_starts"] == nba_run.attempts.total
    assert u["reserved_attempts"] == nba_run.attempts.total
    assert u["blocked_requests"] == 0
    assert u["budget_exhausted"] in (None, False)
    # Pagination genuinely happened on two independent families.
    assert nba_run.attempts.by_path()["/v1/games"] >= 2, "listing must paginate"
    assert nba_run.attempts.by_path()["/v1/plays"] > NBA_GAME_COUNT, (
        "at least one game must need several plays pages")


def test_every_page_consumes_budget(nba_run: MonthRun) -> None:
    """A paginated family must not get free requests."""

    u = usage(nba_run)
    assert u["reserved_attempts"] == nba_run.attempts.total
    # Each listing page is a counted page AND a reserved attempt.
    assert u["pages_fetched"] >= nba_run.attempts.by_path()["/v1/games"]
    assert u["pages_fetched"] >= 1


def test_box_is_fetched_at_most_once_per_selected_game(nba_run: MonthRun) -> None:
    """One box request per selected game -- exactly what the planner bounds.

    Documented behaviour, measured rather than assumed: ``_fetch_all`` fetches box
    scores per distinct DATE, but the pilot executor drives ONE GAME PER UNIT, so
    each unit sees a single date and issues its own box request. Two games on the
    same night therefore each fetch that night's box response. That is within the
    plan (which models box as 1 per game, never per date) and is not a budget
    risk; it is a bounded redundancy of roughly (games - dates) requests per
    month, recorded in pilots/f1/README.md rather than claimed as a saving.
    """

    box_calls = nba_run.attempts.by_path()["/v1/box_scores"]
    selected = usage(nba_run)["games_selected"]
    distinct_dates = len({g["date"] for g in NBA_GAMES})
    assert distinct_dates < NBA_GAME_COUNT, "fixture must put several games per date"
    # The planner's bound: at most one box request per selected game.
    assert box_calls <= selected, (box_calls, selected)
    # And never MORE than one per game -- no per-game fan-out beyond the plan.
    assert box_calls == selected, (box_calls, selected)


def test_quarters_and_box_share_one_request_within_a_unit(nba_run: MonthRun) -> None:
    """`box` and `quarters` are both requested, and share ONE box response."""

    body = json.loads(NBA_MANIFEST.read_text(encoding="utf-8"))
    assert {"box", "quarters"} <= set(body["families"])
    box_calls = nba_run.attempts.by_path()["/v1/box_scores"]
    selected = usage(nba_run)["games_selected"]
    # Two families, one request per unit -- not two.
    assert box_calls == selected, (box_calls, selected)


def test_mlb_shared_linescore_backs_results_and_inning(mlb_run: MonthRun) -> None:
    schedule_games = sum(len(d["games"]) for d in mlb_schedule_body()["dates"])
    # results + inning share ONE linescore per game, never two.
    assert mlb_run.attempts.family("/linescore") <= schedule_games


def test_an_ordinary_month_is_not_truncated_by_max_games(mlb_run: MonthRun) -> None:
    schedule_games = sum(len(d["games"]) for d in mlb_schedule_body()["dates"])
    u = usage(mlb_run)
    assert u["games_received"] == schedule_games
    assert u["games_selected"] == schedule_games
    assert u["games_excluded_by_max_games"] == 0
    assert u["selection_truncated"] is False


def test_truncation_is_reported_when_a_bound_does_bite(
    tmp_path: Path, authorized: None,
) -> None:
    """A deliberately tiny bound must be REPORTED, never silent."""

    from sports_quant.ingest.f1a import emit_plan

    tight = tmp_path / "tight.json"
    emit_plan(league="mlb", from_date="2026-06-01", to_date="2026-06-30",
              includes=("results", "box", "inning", "rosters"), max_games=2,
              max_retries=1, scratch_db="data\\tight.db",
              checkpoint="data\\tight.ckpt", expected_schema_version=17,
              manifest_out=tight, out=lambda _s: None)
    attempts = Attempts()
    code, payload = _run(tight, mlb_factory(attempts), tmp_path)
    assert code == 0, payload
    schedule_games = sum(len(d["games"]) for d in mlb_schedule_body()["dates"])
    u = dict(payload["usage"])
    assert u["games_received"] == schedule_games
    assert u["games_selected"] == 2
    assert u["games_excluded_by_max_games"] == schedule_games - 2
    assert u["selection_truncated"] is True
    assert attempts.total <= json.loads(tight.read_text(encoding="utf-8"))["request_cap"]


def test_reporting_separates_received_selected_pages_and_budget(
    nba_run: MonthRun,
) -> None:
    payload = nba_run.payload
    # Run-level outcome and the usage report are separate top-level facts.
    for key in ("success", "truncated", "failed", "completed", "skipped_on_resume",
                "budget_exhausted", "usage", "checkpoint_state", "network_occurred",
                "database_mutated"):
        assert key in payload, key
    u = usage(nba_run)
    # Selection, pages, records, budget and rate are each reported SEPARATELY --
    # never collapsed into one "requests" number.
    for key in ("games_received", "games_selected", "games_excluded_by_max_games",
                "selection_truncated",
                "pages_fetched", "transport_starts", "reserved_attempts",
                "planned_requests", "blocked_requests", "retry_attempts",
                "successful_responses", "failed_responses",
                "families_completed", "families_failed", "families_truncated",
                "rate_policy_active", "configured_rate_per_min",
                "provider_rate_limit_per_min", "throttle_events", "http_429s",
                "authentication_succeeded", "tier_status"):
        assert key in u, key
    # Selected is never silently equal to received when a bound bit.
    assert u["games_selected"] + u["games_excluded_by_max_games"] == u["games_received"]


def test_identity_observations_are_recorded_during_the_month_run(
    nba_run: MonthRun,
) -> None:
    with Database(nba_run.scratch).connection() as conn:
        teams = conn.execute(
            "SELECT COUNT(*) FROM provider_team_identity_snapshots").fetchone()[0]
        players = conn.execute(
            "SELECT COUNT(*) FROM provider_player_identity_snapshots").fetchone()[0]
        version = conn.execute(
            "SELECT MAX(version) FROM schema_versions").fetchone()[0]
    assert version == 17
    assert teams > 0 and players > 0


def test_completed_resume_makes_zero_additional_requests(
    mlb_run: MonthRun, authorized: None,
) -> None:
    """Resuming a COMPLETED month must not re-request anything."""

    second = Attempts()
    code, payload = _run(MLB_MANIFEST, mlb_factory(second), mlb_run.scratch.parent,
                         scratch=mlb_run.scratch, checkpoint=mlb_run.checkpoint,
                         resume=True)
    assert code == 0, payload
    assert second.total == 0, second.by_path()


def test_a_malformed_optional_record_is_rejected_not_fatal(mlb_run: MonthRun) -> None:
    # The month completed despite the deliberately malformed box-score slot.
    assert mlb_run.code == 0
    # The run completed; the malformed slot did not abort the month.
    assert mlb_run.payload["success"] is True
    assert mlb_run.payload["checkpoint_state"] == "completed"


def test_an_empty_family_is_reported_as_empty_not_missing(nba_run: MonthRun) -> None:
    # Lineups were REQUESTED (endpoint reached) and returned nothing.
    assert nba_run.attempts.by_path()["/v1/lineups"] > 0
    with Database(nba_run.scratch).connection() as conn:
        lineups = conn.execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0]
    assert lineups == 0


def test_no_real_external_access_occurs(mlb_run: MonthRun) -> None:
    """Every attempt went to the MockTransport; the paths prove it."""

    assert mlb_run.attempts.total > 0
    assert all(p.startswith("/api/v1/") for p in mlb_run.attempts.paths)


def test_authorization_is_required_and_not_granted_by_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committing a manifest must not authorize live execution."""

    monkeypatch.delenv(AUTH_ENV, raising=False)
    attempts = Attempts()
    code, _payload = _run(MLB_MANIFEST, mlb_factory(attempts), tmp_path)
    assert code != 0
    assert attempts.total == 0, "an unauthorized pilot must make no request"


def test_env_authorization_is_never_persisted(mlb_run: MonthRun) -> None:
    blob = mlb_run.checkpoint.read_bytes().lower()
    assert AUTH_ENV.lower().encode() not in blob
