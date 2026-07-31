"""Regression tests pinning the reviewed MLB F1B RICH pilot behaviour (offline).

These lock in what the independent review of the completed MLB rich pilot verified
from the preserved artifacts, so a future change cannot silently drift:

* the probable-pitcher policy at RICH stage (inline schedule fields only, never a
  standalone family and never an extra request) -- previously only pinned for the
  skeleton stage;
* the reviewed request/page/selection accounting (6 attempts of cap 12, 2 listing
  pages, 30 received / 1 selected / 29 excluded);
* results and innings deriving from ONE shared linescore response, with the inning
  rows summing to the persisted result;
* roster fan-out bounded to two team/date requests;
* keyless-MLB authentication and tier reported as not applicable, rate policy
  inactive;
* completed-resume provenance carrying the first run's transports and pages.

Everything runs against mocked transports; no key, no network, no live pilot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from sports_quant.ingest.checkpoint import load_checkpoint
from sports_quant.ingest.cost_policies import build_mlb_policy
from sports_quant.ingest.tests.test_f1b_rich_manifests import (
    MLB_CAP,
    MLB_PK,
    MLB_PK2,
    MLB_RICH,
    MLB_SEMANTIC_MAX,
    _run,
    f1b_authorized,  # noqa: F401  (fixture)
    no_external_network,  # noqa: F401  (fixture)
)
from sports_quant.ingest.tests.test_phase_d2_mlb import game as mlb_game
from sports_quant.ingest.tests.test_phase_d2_mlb import schedule as mlb_schedule
from sports_quant.request_control import RequestGate


def _mlb_factory(seen: list[str]):
    """Real MlbStatsApiClient over a mocked transport returning THIRTY games.

    Mirrors the reviewed pilot response: 15 games on each date, with the reviewed
    selected game (822788) carrying the lowest ``(officialDate, gamePk)`` key so the
    canonical selection is deterministic and matches the real run.
    """

    import re

    games = [mlb_game(game_pk=MLB_PK, official_date="2026-07-20",
                      home_team=141, away_team=139,
                      home_probable=656302, away_probable=607259)]
    games += [mlb_game(game_pk=822800 + i, official_date="2026-07-20",
                       home_probable=656302, away_probable=607259)
              for i in range(14)]
    games += [mlb_game(game_pk=823000 + i, official_date="2026-07-21",
                       home_probable=656302, away_probable=607259)
              for i in range(15)]
    assert len(games) == 30

    def handler(request: httpx.Request) -> httpx.Response:
        p = re.sub(r"^/api/v1", "", request.url.path)
        seen.append(p)
        ct = {"content-type": "application/json"}
        if p.endswith("/schedule"):
            gpk = dict(request.url.params).get("gamePk")
            if gpk:
                one = mlb_game(game_pk=int(gpk), official_date="2026-07-20",
                               home_team=141, away_team=139,
                               home_probable=656302, away_probable=607259)
                return httpx.Response(200, json=mlb_schedule(one, date="2026-07-20"),
                                      headers=ct)
            body = {"dates": [
                {"date": "2026-07-20",
                 "games": [g for g in games if g["officialDate"] == "2026-07-20"]},
                {"date": "2026-07-21",
                 "games": [g for g in games if g["officialDate"] == "2026-07-21"]}]}
            return httpx.Response(200, json=body, headers=ct)
        if p.endswith("/boxscore"):
            from sports_quant.ingest.tests.test_phase_d2_mlb import boxscore
            return httpx.Response(200, json=boxscore(home_team=141, away_team=139),
                                  headers=ct)
        if p.endswith("/linescore"):
            from sports_quant.ingest.tests.test_phase_d2_mlb import linescore
            return httpx.Response(200, json=linescore(), headers=ct)
        if "/roster" in p:
            return httpx.Response(200, json={"roster": [
                {"person": {"id": 900 + i, "fullName": f"P {i}"},
                 "position": {"abbreviation": "SS"},
                 "status": {"code": "A"}} for i in range(26)]}, headers=ct)
        return httpx.Response(200, json={}, headers=ct)

    def factory(gate: RequestGate) -> Any:
        from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
        from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
        http = build_readonly_client(
            base_url="https://statsapi.mlb.com/api/v1",
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
            inner_transport=httpx.MockTransport(handler))
        client = MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=1)
        # MLB now paces at 30/min. The delay itself is verified with a mocked
        # clock in test_mlb_pacing.py; here the returned wait is swallowed so the
        # fixture still traverses the real pacing chokepoint without sleeping.
        async def _no_wait(_seconds: float) -> None:
            return None

        client._sleep = _no_wait  # noqa: SLF001 - deterministic test pacing
        return client

    return factory


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {t: con.execute(f'select count(*) from "{t}"').fetchone()[0]
                for (t,) in con.execute(
                    "select name from sqlite_master where type='table' "
                    "and name not like 'sqlite_%'")}
    finally:
        con.close()


# ===================================================================== #
# Probable-pitcher policy at RICH stage
# ===================================================================== #
def test_rich_probable_pitcher_stays_inline_and_costs_no_request(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    """RICH stage keeps the reviewed inline-hydration policy.

    `hydrate=probablePitcher` is a response-shaping parameter on `/schedule`. It must
    stay inside the `schedule` family: no standalone probable-pitcher endpoint, no
    extra request, and no rich `probable_pitcher_snapshots` row -- the ids live only
    as inline columns on the schedule snapshot.
    """

    seen: list[str] = []
    rc, lines, db, _ck = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)

    # No standalone probable-pitcher endpoint was called.
    assert not [p for p in seen if "probable" in p.lower()], seen
    # Both schedule requests carried the hydrate parameter; nothing else did.
    assert len([p for p in seen if p.endswith("/schedule")]) == 2, seen

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # Declared-but-unpopulated rich family: expected ZERO, never fabricated.
        assert con.execute(
            "select count(*) from probable_pitcher_snapshots").fetchone()[0] == 0
        # The ids arrived INLINE on the schedule snapshot instead.
        rows = con.execute("select home_probable_pitcher_id, away_probable_pitcher_id "
                           "from game_schedule_snapshots").fetchall()
        assert rows and all(r[0] is not None and r[1] is not None for r in rows), rows
    finally:
        con.close()


def test_planner_declares_no_probable_pitcher_unit() -> None:
    """The plan must not hide a probable-pitcher request behind the schedule family."""

    from sports_quant.ingest.tests.test_f1b_rich_manifests import _mlb_plan

    p = _mlb_plan()
    families = {u.endpoint_family for u in p.fixed_units} | {
        c.family for c in p.contingents}
    assert not any("probable" in f for f in families), families
    # And the cost policy has no probable-pitcher endpoint family at all.
    assert not build_mlb_policy().is_known_family("probable_pitcher")


# ===================================================================== #
# Reviewed request / page / selection accounting
# ===================================================================== #
def test_rich_reviewed_request_and_selection_accounting(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    seen: list[str] = []
    rc, lines, _db, ck = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)
    u = load_checkpoint(ck).usage

    # 6 semantic requests == the planner maximum, inside the cap of 12.
    assert len(seen) == MLB_SEMANTIC_MAX == 6, seen
    assert u["attempted_requests"] == 6
    assert u["attempted_requests"] <= MLB_CAP == 12
    assert u["transport_starts"] == 6
    assert u["successful_responses"] == 6
    assert u["failed_responses"] == 0
    assert u["retry_attempts"] == 0
    assert u["blocked_requests"] == 0
    # Two schedule documents are the two listing pages.
    assert u["pages_fetched"] == 2
    # Selection: 30 received, 1 kept, 29 excluded -- and NOT budget truncation.
    assert (u["games_received"], u["games_selected"],
            u["games_excluded_by_max_games"]) == (30, 1, 29)
    assert u["selection_truncated"] is True
    assert u["budget_exhausted"] is None
    # Keyless MLB: auth and tier are not applicable and never claimed verified.
    assert u["authentication_status"] == "not_applicable"
    assert u["authentication_succeeded"] is None
    assert u["tier_status"] == "not_applicable"
    assert u["tier_verified"] is False
    assert u["tier_evidence_source"] == "none"
    # No rate policy for a keyless provider, and nothing was throttled.
    # MLB now carries a project courtesy pacing policy (30/min, burst 1). This
    # fixture issues SIX requests, so requests 2..6 were each genuinely delayed:
    # `rate_limited` is true because a real wait happened, and the wait is
    # courtesy pacing -- not a provider 429, which stays at zero.
    assert u["rate_policy_active"] is True
    assert u["rate_policy_basis"] == "project_courtesy_cap"
    assert u["configured_rate_per_min"] == 30
    assert u["provider_rate_limit_per_min"] is None
    assert u["rate_limited"] is True
    assert u["throttle_events"] == 5
    assert u["throttle_wait_seconds"] > 0.0
    assert u["http_429s"] == 0


def test_rich_endpoint_families_match_the_reviewed_set(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    """2x schedule, 1 boxscore, 1 linescore, 2 rosters -- all gate-known."""

    seen: list[str] = []
    rc, lines, _db, _ck = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)
    pol = build_mlb_policy()
    fams = [pol.classify(p) for p in seen]
    assert {f: fams.count(f) for f in set(fams)} == {
        "schedule": 2, "game_boxscore": 1, "game_linescore": 1, "teams": 2}, seen
    assert all(pol.is_known_family(f) for f in fams), fams
    # Roster fan-out is bounded to two team/date requests.
    assert fams.count("teams") == 2
    # max_games=1: the second game is never fetched.
    assert not [p for p in seen if str(MLB_PK2) in p], seen
    assert [p for p in seen if str(MLB_PK) in p]


def test_results_and_innings_share_one_linescore_and_sum_consistently(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    """One linescore response backs BOTH families, and innings sum to the result."""

    # A linescore whose innings sum exactly on runs, hits and errors.
    innings = [
        {"num": 1, "home": {"runs": 1, "hits": 2, "errors": 0},
         "away": {"runs": 0, "hits": 1, "errors": 1}},
        {"num": 2, "home": {"runs": 2, "hits": 3, "errors": 1},
         "away": {"runs": 4, "hits": 5, "errors": 0}},
    ]
    home = {"runs": 3, "hits": 5, "errors": 1}
    away = {"runs": 4, "hits": 6, "errors": 1}
    line = {"currentInning": 2, "teams": {"home": home, "away": away}, "innings": innings}

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import re
        p = re.sub(r"^/api/v1", "", request.url.path)
        seen.append(p)
        ct = {"content-type": "application/json"}
        if p.endswith("/schedule"):
            gpk = dict(request.url.params).get("gamePk")
            g = mlb_game(game_pk=int(gpk) if gpk else MLB_PK,
                         official_date="2026-07-20", home_team=133, away_team=147)
            return httpx.Response(200, json=mlb_schedule(g, date="2026-07-20"), headers=ct)
        if p.endswith("/linescore"):
            return httpx.Response(200, json=line, headers=ct)
        if p.endswith("/boxscore"):
            from sports_quant.ingest.tests.test_phase_d2_mlb import boxscore
            return httpx.Response(200, json=boxscore(), headers=ct)
        if "/roster" in p:
            return httpx.Response(200, json={"roster": []}, headers=ct)
        return httpx.Response(200, json={}, headers=ct)

    def factory(gate: RequestGate) -> Any:
        from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
        from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
        http = build_readonly_client(
            base_url="https://statsapi.mlb.com/api/v1",
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
            inner_transport=httpx.MockTransport(handler))
        client = MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=1)
        # MLB now paces at 30/min. The delay itself is verified with a mocked
        # clock in test_mlb_pacing.py; here the returned wait is swallowed so the
        # fixture still traverses the real pacing chokepoint without sleeping.
        async def _no_wait(_seconds: float) -> None:
            return None

        client._sleep = _no_wait  # noqa: SLF001 - deterministic test pacing
        return client

    rc, lines, db, _ck = _run(tmp_path, "mlb", MLB_RICH, factory)
    assert rc == 0, "\n".join(lines)

    # Exactly ONE linescore request served both families.
    assert len([p for p in seen if p.endswith("/linescore")]) == 1, seen

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        res = con.execute("select home_runs, away_runs, home_hits, away_hits, "
                          "home_errors, away_errors, innings_played, winning_side, "
                          "raw_response_hash from game_result_snapshots").fetchall()
        assert len(res) == 1
        r = res[0]
        assert (r[0], r[1]) == (home["runs"], away["runs"])

        ilines = con.execute("select inning, side, runs, hits, errors, raw_response_hash "
                             "from mlb_inning_lines").fetchall()
        assert len(ilines) == len(innings) * 2                # innings x 2 sides
        agg: dict[str, dict[str, int]] = {}
        for _inning, side, runs, hits, errors, _h in ilines:
            a = agg.setdefault(side, {"runs": 0, "hits": 0, "errors": 0})
            a["runs"] += runs or 0
            a["hits"] += hits or 0
            a["errors"] += errors or 0
        # Innings SUM to the persisted result on every dimension.
        assert agg["home"]["runs"] == r[0] and agg["away"]["runs"] == r[1]
        assert agg["home"]["hits"] == r[2] and agg["away"]["hits"] == r[3]
        assert agg["home"]["errors"] == r[4] and agg["away"]["errors"] == r[5]
        assert r[6] == len({i for i, *_ in ilines})           # innings_played
        assert r[7] == ("away" if r[1] > r[0] else "home")    # winning_side

        # BOTH families trace to the SAME single linescore raw response.
        assert {r[8]} == {row[5] for row in ilines}, "shared linescore provenance"
    finally:
        con.close()


def test_rich_completed_resume_preserves_transport_provenance(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    """A completed rich resume adds nothing and keeps the first run's provenance."""

    seen: list[str] = []
    factory = _mlb_factory(seen)
    rc, lines, db, ck = _run(tmp_path, "mlb", MLB_RICH, factory)
    assert rc == 0, "\n".join(lines)
    assert len(seen) == 6
    before = _counts(db)

    rc2, lines2, _db, _ck = _run(tmp_path, "mlb", MLB_RICH, factory, resume=True)
    assert rc2 == 0, "\n".join(lines2)
    assert len(seen) == 6, "a completed resume must issue ZERO further requests"

    u = load_checkpoint(ck).usage
    assert u["transport_starts"] == 0 and u["pages_fetched"] == 0
    assert u["database_mutated"] is False and u["network_occurred"] is False
    assert u["skipped_on_resume"] == 2                       # both durable units
    assert u["attempted_requests"] == 6 and u["prior_requests"] == 6
    # Defect-E provenance: the first run's transports and pages survive the resume.
    assert u["prior_transport_starts"] == 6
    assert u["prior_pages_fetched"] == 2
    assert load_checkpoint(ck).state == "completed"
    # Selection accounting survives, and nothing was persisted twice.
    assert (u["games_received"], u["games_selected"],
            u["games_excluded_by_max_games"]) == (30, 1, 29)
    assert _counts(db) == before


def test_rich_report_is_deterministic_and_secret_free(
    tmp_path: Path, f1b_authorized: None, no_external_network: None  # noqa: F811
) -> None:
    seen: list[str] = []
    rc, lines, _db, ck = _run(tmp_path, "mlb", MLB_RICH, _mlb_factory(seen))
    assert rc == 0, "\n".join(lines)
    text = "\n".join(lines)
    blob = json.dumps(load_checkpoint(ck).usage, sort_keys=True, default=str)
    for banned in ("api_key", "apikey", "authorization", "bearer", "password", "secret"):
        assert banned not in blob.lower()
        assert banned not in text.lower()
    # The human output separates selection from budget, and states auth honestly.
    assert "selection_truncated=True" in text
    assert "excluded_by_max_games=29" in text
    assert "budget:" in text and "truncated=False" in text
    assert "status=not_applicable" in text
    assert ck.read_text(encoding="utf-8").count("822788") >= 1
