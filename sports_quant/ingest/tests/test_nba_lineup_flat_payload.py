"""BALLDONTLIE NBA lineup payload-shape repair (offline; no network, no key).

The official ``/v1/lineups`` payload is FLAT -- one provider row per player, carrying
``game_id``, an individual ``player`` object, an individual ``team`` object,
``position`` and ``starter``. The previous parser only understood a nested per-team
``players`` array, so a real 25-row response normalized to zero lineups **silently**
(``lineup_snapshots=0`` / ``lineup_players=0``, no data-quality finding).

These tests pin the repair:

* the official flat shape parses, groups per team, and preserves ``starter``/``position``;
* the legacy nested shape still works, and a mixed payload cannot double-count;
* malformed rows are rejected honestly and counted;
* a nonempty matching payload that normalizes to nothing raises a deterministic
  data-quality signal instead of looking like a legitimately empty family, in BOTH
  dry-run counting and persisted execution;
* a genuinely empty ``data`` list stays an honest empty family;
* the parent snapshot keeps ``is_confirmed=False`` (retrospective data is never a
  confirmed pregame lineup) and children persist ``is_starter``.

``batting_order`` carries the repository's ordered-child ordinal only: basketball has
no batting order and the provider supplies none.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.nba_ingestor import _parse_lineups, ingest_nba
from sports_quant.ingest.tests.test_phase_d3_nba import game as nba_game
from sports_quant.ingest.tests.test_phase_d3_nba import page as nba_page
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.capabilities import BalldontlieTier

GAME = "18447316"
OTHER_GAME = "18447317"
TEAM_A, TEAM_B = "9", "20"
SENTINEL = "sk-nba-lineup-sentinel-do-not-store"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block DNS and every non-loopback connect (asyncio needs loopback on Windows)."""

    real_connect = socket.socket.connect

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted in an offline test")

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            raise AssertionError(f"external connect to {host!r} in an offline test")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", guarded)


# --------------------------------------------------------------------------- #
# Fixture builders mirroring the OFFICIAL flat provider shape
# --------------------------------------------------------------------------- #
def flat_row(*, row_id: Optional[int], player_id: int, team_id: str,
             position: str = "G", starter: bool = False,
             game_id: str = GAME) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "game_id": int(game_id),
        "starter": starter,
        "position": position,
        "player": {"id": player_id, "first_name": "A", "last_name": "B"},
        "team": {"id": int(team_id), "abbreviation": "XX"},
    }
    if row_id is not None:
        entry["id"] = row_id
    return entry


def official_payload(n_per_team: tuple[int, int] = (12, 13),
                     starters_per_team: int = 5) -> dict[str, Any]:
    """25 flat rows across two teams, 10 with ``starter=true`` -- the real shape."""

    rows: list[dict[str, Any]] = []
    rid = 11700
    pid = 100
    for team, n in ((TEAM_A, n_per_team[0]), (TEAM_B, n_per_team[1])):
        for i in range(n):
            rows.append(flat_row(row_id=rid, player_id=pid, team_id=team,
                                 position="FGC"[i % 3],
                                 starter=i < starters_per_team))
            rid += 1
            pid += 1
    return {"data": rows, "meta": {"next_cursor": 8280153, "per_page": 25}}


def nested_payload() -> dict[str, Any]:
    """The legacy nested shape: one entry per team with a `players` array."""

    return {"data": [
        {"game_id": int(GAME), "team": {"id": int(TEAM_A)}, "players": [
            {"player": {"id": 501}, "position": "G", "starter": True},
            {"player": {"id": 502}, "position": "F"},
        ]},
        {"game_id": int(GAME), "team": {"id": int(TEAM_B)}, "players": [
            {"player": {"id": 601}, "position": "C", "starter": True},
        ]},
    ]}


def _flat(groups) -> list[tuple[str, list[tuple[int, str, Optional[str], Optional[bool]]]]]:
    return [(t, [(p.batting_order, p.provider_player_id, p.position, p.is_starter)
                 for p in pl]) for t, pl in groups]


# ===================================================================== #
# 1-9. Official flat shape
# ===================================================================== #
def test_official_flat_shape_parses_into_two_team_groups() -> None:
    p = _parse_lineups(official_payload(), GAME)
    assert len(p.groups) == 2, "one lineup group per team"
    assert [t for t, _ in p.groups] == [TEAM_A, TEAM_B]      # deterministic team order
    assert p.matching_rows == 25
    assert p.rejected_rows == 0
    assert p.silent_loss is False


def test_twenty_five_flat_entries_produce_twenty_five_children() -> None:
    p = _parse_lineups(official_payload(), GAME)
    assert sum(len(pl) for _t, pl in p.groups) == 25
    assert [len(pl) for _t, pl in p.groups] == [12, 13]


def test_exactly_ten_starters_are_flagged() -> None:
    p = _parse_lineups(official_payload(), GAME)
    starters = [x for _t, pl in p.groups for x in pl if x.is_starter is True]
    assert len(starters) == 10
    # Starters occupy the leading ordinals of each team group.
    for _t, pl in p.groups:
        flags = [x.is_starter is True for x in pl]
        assert flags[:5] == [True] * 5 and not any(flags[5:])


def test_filtering_is_strict_to_the_requested_game() -> None:
    p = _parse_lineups(official_payload(), GAME)
    assert p.matching_rows == 25
    # A different game id yields nothing and is NOT treated as silent loss.
    q = _parse_lineups(official_payload(), "99999999")
    assert q.groups == [] and q.matching_rows == 0 and q.rejected_rows == 0
    assert q.silent_loss is False


def test_entries_for_another_game_are_ignored_not_rejected() -> None:
    payload = official_payload()
    payload["data"].append(flat_row(row_id=999, player_id=777, team_id=TEAM_A,
                                    game_id=OTHER_GAME, starter=True))
    p = _parse_lineups(payload, GAME)
    assert p.matching_rows == 25                    # the foreign row does not match
    assert p.rejected_rows == 0                     # ... and is not a rejection
    assert sum(len(pl) for _t, pl in p.groups) == 25
    assert "777" not in {x.provider_player_id for _t, pl in p.groups for x in pl}


def test_team_and_player_ids_are_extracted_from_objects() -> None:
    p = _parse_lineups(official_payload(), GAME)
    ids = {t for t, _ in p.groups}
    assert ids == {TEAM_A, TEAM_B}
    pids = {x.provider_player_id for _t, pl in p.groups for x in pl}
    assert len(pids) == 25 and all(pid.isdigit() for pid in pids)


def test_scalar_team_and_player_fallbacks_are_supported() -> None:
    """Only the already-supported scalar fallbacks -- no new inference."""

    payload = {"data": [{"game_id": int(GAME), "team_id": 9, "player_id": 42,
                         "position": "G", "starter": True}]}
    p = _parse_lineups(payload, GAME)
    assert len(p.groups) == 1
    team, players = p.groups[0]
    assert team == "9" and players[0].provider_player_id == "42"
    assert players[0].is_starter is True and players[0].position == "G"


def test_position_is_preserved_without_fabrication() -> None:
    p = _parse_lineups(official_payload(), GAME)
    positions = {x.position for _t, pl in p.groups for x in pl}
    assert positions <= {"F", "G", "C"} and positions
    # A row without a position keeps None rather than inventing one.
    payload = {"data": [flat_row(row_id=1, player_id=7, team_id=TEAM_A)]}
    payload["data"][0].pop("position")
    q = _parse_lineups(payload, GAME)
    assert q.groups[0][1][0].position is None


def test_output_is_stable_when_rows_are_reordered() -> None:
    import random

    base = _parse_lineups(official_payload(), GAME)
    for seed in (1, 7, 99):
        payload = official_payload()
        random.Random(seed).shuffle(payload["data"])
        assert _flat(_parse_lineups(payload, GAME).groups) == _flat(base.groups)


def test_missing_provider_entry_ids_are_handled_stably() -> None:
    payload = official_payload()
    for entry in payload["data"]:
        entry.pop("id", None)                       # no provider row id at all
    p = _parse_lineups(payload, GAME)
    assert sum(len(pl) for _t, pl in p.groups) == 25
    assert p.rejected_rows == 0
    # Still deterministic: the player-id tiebreaker gives a total order.
    import random
    shuffled = official_payload()
    for entry in shuffled["data"]:
        entry.pop("id", None)
    random.Random(3).shuffle(shuffled["data"])
    assert _flat(_parse_lineups(shuffled, GAME).groups) == _flat(p.groups)


# ===================================================================== #
# 11-14. Malformed, duplicate, nested, mixed
# ===================================================================== #
@pytest.mark.parametrize("bad", [
    {"game_id": int(GAME), "player": {"id": 1}},                  # no team
    {"game_id": int(GAME), "team": {"id": 9}},                    # no player, no players
    {"game_id": int(GAME), "team": {}, "player": {}},             # empty objects
    "not-a-dict",                                                 # malformed entry
])
def test_malformed_entries_are_rejected_honestly(bad: Any) -> None:
    p = _parse_lineups({"data": [bad]}, GAME)
    assert p.groups == []
    assert p.rejected_rows >= 1, "a malformed row must be counted, never dropped silently"
    assert p.silent_loss is True, "nonempty payload -> zero lineups must signal"


def test_duplicate_team_player_entries_do_not_duplicate_children() -> None:
    payload = official_payload()
    payload["data"].append(flat_row(row_id=11700, player_id=100, team_id=TEAM_A,
                                    position="G", starter=True))
    p = _parse_lineups(payload, GAME)
    assert sum(len(pl) for _t, pl in p.groups) == 25, "duplicate collapses"
    team_a = dict(p.groups)[TEAM_A]
    assert len(team_a) == len({x.provider_player_id for x in team_a})


def test_legacy_nested_shape_remains_supported() -> None:
    p = _parse_lineups(nested_payload(), GAME)
    assert len(p.groups) == 2
    assert sum(len(pl) for _t, pl in p.groups) == 3
    assert p.rejected_rows == 0 and p.silent_loss is False
    starters = [x for _t, pl in p.groups for x in pl if x.is_starter is True]
    assert len(starters) == 2


def test_mixed_flat_and_nested_cannot_double_count() -> None:
    """The same team/player in both shapes resolves to ONE child, deterministically."""

    payload = {"data": [
        # Nested entry for team A listing player 501 ...
        {"game_id": int(GAME), "team": {"id": int(TEAM_A)}, "players": [
            {"player": {"id": 501}, "position": "G", "starter": True}]},
        # ... and a flat row for the SAME team/player.
        flat_row(row_id=5, player_id=501, team_id=TEAM_A, position="G", starter=True),
    ]}
    p = _parse_lineups(payload, GAME)
    assert len(p.groups) == 1
    team, players = p.groups[0]
    assert team == TEAM_A
    assert len(players) == 1, "one child per (team, player) regardless of shape"
    assert players[0].provider_player_id == "501"
    # And the policy is stable when the two representations are swapped.
    swapped = {"data": list(reversed(payload["data"]))}
    assert _flat(_parse_lineups(swapped, GAME).groups) == _flat(p.groups)


def test_entry_with_both_player_and_players_prefers_the_flat_player() -> None:
    payload = {"data": [{
        "game_id": int(GAME), "team": {"id": int(TEAM_A)}, "id": 3,
        "player": {"id": 900}, "position": "C", "starter": True,
        "players": [{"player": {"id": 901}}],
    }]}
    p = _parse_lineups(payload, GAME)
    assert len(p.groups) == 1
    players = p.groups[0][1]
    assert [x.provider_player_id for x in players] == ["900"], "flat player wins"


# ===================================================================== #
# 15-16. Silent-loss signal vs honest empty
# ===================================================================== #
def test_nonempty_matching_payload_that_normalizes_to_zero_signals_loss() -> None:
    payload = {"data": [
        {"game_id": int(GAME), "player": {"id": 1}},   # team missing
        {"game_id": int(GAME), "player": {"id": 2}},
    ]}
    p = _parse_lineups(payload, GAME)
    assert p.groups == []
    assert p.matching_rows == 2 and p.rejected_rows == 2
    assert p.silent_loss is True


def test_genuinely_empty_data_is_an_honest_empty_family() -> None:
    empties: tuple[dict[str, Any], ...] = (
        {"data": []}, {"data": None}, {}, {"meta": {}})
    for payload in empties:
        p = _parse_lineups(payload, GAME)
        assert p.groups == []
        assert p.matching_rows == 0 and p.rejected_rows == 0
        assert p.silent_loss is False, "an empty provider family is not a parser failure"


# ===================================================================== #
# 17-20. Persistence, is_confirmed, is_starter, redaction, no network
# ===================================================================== #
def _client(lineups: dict[str, Any], seen: list[str]) -> BalldontlieClient:
    games = [nba_game(gid=int(GAME), date="2026-01-05")]

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        seen.append(p)
        ct = {"content-type": "application/json"}
        if p == "/v1/games":
            return httpx.Response(200, json=nba_page(games), headers=ct)
        if p.startswith("/v1/games/"):
            return httpx.Response(
                200, json={"data": nba_game(gid=int(GAME), date="2026-01-05")}, headers=ct)
        if p == "/v1/lineups":
            return httpx.Response(200, json=lineups, headers=ct)
        return httpx.Response(200, json={"data": []}, headers=ct)

    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler))
    return BalldontlieClient(SENTINEL, client=http)


def _ingest(tmp_path: Path, lineups: dict[str, Any], *, dry_run: bool = False):
    db = tmp_path / "nba.db"
    if not db.exists():
        initialize_database(db)
    seen: list[str] = []
    client = _client(lineups, seen)

    async def go():
        try:
            return await ingest_nba(
                database=Database(db), client=client,
                from_date="2026-01-05", to_date="2026-01-05",
                includes=("lineups",), tier=BalldontlieTier.GOAT,
                max_games=1, max_pages=1, max_records=100, dry_run=dry_run)
        finally:
            await client.aclose()

    return asyncio.run(go()), db, seen


def test_persisted_flat_lineups_write_two_snapshots_and_children(
    tmp_path: Path, no_network: None
) -> None:
    res, db, _seen = _ingest(tmp_path, official_payload())
    assert res.status == "succeeded"
    assert res.lineup_observations == 2
    assert res.lineup_players_observed == 25

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        snaps = con.execute("select lineup_id, provider_team_id, provider_game_id, "
                            "is_confirmed, player_count from lineup_snapshots "
                            "order by provider_team_id").fetchall()
        assert len(snaps) == 2
        assert {s[1] for s in snaps} == {TEAM_A, TEAM_B}
        assert {s[2] for s in snaps} == {GAME}
        # 17. Retrospective data is NEVER a confirmed pregame lineup.
        assert all(s[3] == 0 for s in snaps), "is_confirmed must stay False"
        assert sorted(s[4] for s in snaps) == [12, 13]

        kids = con.execute("select lineup_id, batting_order, provider_player_id, "
                           "position, is_starter from lineup_players").fetchall()
        assert len(kids) == 25
        # 18. is_starter is persisted, exactly ten of them.
        assert sum(1 for k in kids if k[4] == 1) == 10
        assert all(k[3] in ("F", "G", "C") for k in kids)
        # Ordinals are 1..n within each parent, with no gaps or duplicates.
        by_parent: dict[str, list[int]] = {}
        for lid, order, *_ in kids:
            by_parent.setdefault(lid, []).append(order)
        for orders in by_parent.values():
            assert sorted(orders) == list(range(1, len(orders) + 1))
        # Player references were created for every child.
        refs = con.execute("select count(distinct provider_player_id) "
                           "from provider_player_references").fetchone()[0]
        assert refs == 25
        # No spurious data-quality finding on a healthy payload.
        assert con.execute("select count(*) from data_quality_issues where "
                           "rule_code='DQ-NBA-LINEUP-001'").fetchone()[0] == 0
    finally:
        con.close()


def test_persisted_replay_is_idempotent(tmp_path: Path, no_network: None) -> None:
    res1, db, _s = _ingest(tmp_path, official_payload())
    assert res1.lineup_observations == 2
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    before = (con.execute("select count(*) from lineup_snapshots").fetchone()[0],
              con.execute("select count(*) from lineup_players").fetchone()[0])
    con.close()

    res2, _db, _s2 = _ingest(tmp_path, official_payload())
    assert res2.status == "succeeded"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        after = (con.execute("select count(*) from lineup_snapshots").fetchone()[0],
                 con.execute("select count(*) from lineup_players").fetchone()[0])
        assert after == before == (2, 25), "no duplicate snapshots or children"
        assert res2.lineup_observations == 0, "an unchanged observation is not re-counted"
    finally:
        con.close()


def test_malformed_nonempty_payload_records_a_data_quality_issue(
    tmp_path: Path, no_network: None
) -> None:
    """Persisted mode must not report a clean empty family for discarded rows."""

    payload = {"data": [{"game_id": int(GAME), "player": {"id": i}} for i in range(1, 6)]}
    res, db, _seen = _ingest(tmp_path, payload)
    assert res.lineup_observations == 0
    assert res.records_rejected >= 5
    assert res.data_quality_issues >= 1

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("select severity, rule_code, entity_id, description "
                           "from data_quality_issues "
                           "where rule_code='DQ-NBA-LINEUP-001'").fetchall()
        assert len(rows) == 1
        sev, rule, entity, desc = rows[0]
        assert sev == "issue" and rule == "DQ-NBA-LINEUP-001" and entity == GAME
        # 19. Sanitized: counts only -- no player names, no raw body, no credential.
        assert SENTINEL not in desc
        for banned in ("first_name", "last_name", "api_key", "authorization", "bearer"):
            assert banned not in desc.lower()
        assert "5 provider row(s) matched" in desc
        assert con.execute("select count(*) from lineup_snapshots").fetchone()[0] == 0
    finally:
        con.close()


def test_genuinely_empty_payload_records_no_data_quality_issue(
    tmp_path: Path, no_network: None
) -> None:
    res, db, _seen = _ingest(tmp_path, {"data": []})
    assert res.lineup_observations == 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("select count(*) from data_quality_issues where "
                           "rule_code='DQ-NBA-LINEUP-001'").fetchone()[0] == 0
        assert con.execute("select count(*) from lineup_snapshots").fetchone()[0] == 0
    finally:
        con.close()


def test_dry_run_counting_matches_persisted_accounting(
    tmp_path: Path, no_network: None
) -> None:
    dry, db, _s = _ingest(tmp_path, official_payload(), dry_run=True)
    assert dry.lineup_observations == 2
    assert dry.lineup_players_observed == 25
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("select count(*) from lineup_snapshots").fetchone()[0] == 0
    finally:
        con.close()

    # And a malformed payload signals in dry-run too, not just in persisted mode.
    dry2, _db2, _s2 = _ingest(
        tmp_path / "second", {"data": [{"game_id": int(GAME), "player": {"id": 1}}]},
        dry_run=True)
    assert dry2.lineup_observations == 0
    assert dry2.records_rejected >= 1
    assert dry2.data_quality_issues >= 1


def test_no_secret_appears_in_the_persisted_lineup_evidence(
    tmp_path: Path, no_network: None
) -> None:
    _res, db, seen = _ingest(tmp_path, official_payload())
    blob = Path(db).read_bytes().decode("latin-1")
    assert SENTINEL not in blob
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for endpoint, params, headers in con.execute(
                "select endpoint, request_params_json, response_headers_json "
                "from raw_responses"):
            assert "?" not in endpoint and "://" not in endpoint
            assert SENTINEL not in (params or "")
            hdr = json.loads(headers or "{}")
            assert not ({h.lower() for h in hdr}
                        & {"authorization", "x-api-key", "cookie"})
    finally:
        con.close()
    assert "/v1/lineups" in seen


# ===================================================================== #
# Step 6: reporting contracts stay consistent after the repair
# ===================================================================== #
def test_human_and_json_reporting_surface_the_repaired_lineup_counts(
    tmp_path: Path, no_network: None
) -> None:
    from sports_quant.cli import _nba_json, _report_nba

    res, _db, _seen = _ingest(tmp_path, official_payload())
    lines: list[str] = []
    _report_nba(res, lines.append, as_json=False)
    text = "\n".join(lines)
    assert "lineups 2 (25 players)" in text, text
    assert "rejected 0" in text
    payload = _nba_json(res)
    assert payload["lineup_observations"] == 2
    assert payload["lineup_players_observed"] == 25
    assert payload["records_rejected"] == 0
    assert SENTINEL not in json.dumps(payload)


def test_reporting_never_shows_a_clean_zero_for_discarded_lineup_rows(
    tmp_path: Path, no_network: None
) -> None:
    """A malformed nonempty payload must read as rejected + DQ, not a clean empty run."""

    from sports_quant.cli import _nba_json, _report_nba

    payload = {"data": [{"game_id": int(GAME), "player": {"id": i}} for i in range(1, 6)]}
    res, _db, _seen = _ingest(tmp_path, payload)
    lines: list[str] = []
    _report_nba(res, lines.append, as_json=False)
    text = "\n".join(lines)
    assert "lineups 0 (0 players)" in text, text
    # The same line set must ALSO show the rejections and the data-quality finding,
    # so zero lineups can never be mistaken for a legitimately empty family.
    assert "rejected 0" not in text, text
    assert "data-quality 0," not in text, text
    j = _nba_json(res)
    assert j["lineup_observations"] == 0
    assert j["records_rejected"] >= 5
    assert j["data_quality_issues"] >= 1
