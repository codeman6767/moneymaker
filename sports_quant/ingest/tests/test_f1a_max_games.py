"""B2 max_games enforcement tests (offline; mocked transports).

Covers the pure canonical-selection/validation logic for both leagues and an MLB
integration proving rich-data fetches occur ONLY for the selected bounded games.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.ingest.mlb_ingestor import (
    _select_games,
    ingest_mlb,
    validate_max_games,
)
from sports_quant.ingest.nba_ingestor import _select_nba_games
from sports_quant.ingest.tests.test_phase_d2_mlb import game, schedule
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient


# --- validation edge cases ------------------------------------------------- #
@pytest.mark.parametrize("bad", [-1, -100, True, False, 2.5, "3", 100_001])
def test_validate_max_games_rejects_bad(bad: object) -> None:
    with pytest.raises(ValueError):
        validate_max_games(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("ok", [None, 0, 1, 10, 100_000])
def test_validate_max_games_accepts_ok(ok: object) -> None:
    validate_max_games(ok)  # type: ignore[arg-type]


# --- MLB canonical selection (pure) ---------------------------------------- #
def _mlb_games(*pks_dates: tuple[int, str]) -> list[dict]:
    return [game(game_pk=pk, official_date=d) for pk, d in pks_dates]


def test_mlb_select_unbounded_preserves_order() -> None:
    games = _mlb_games((3, "2024-04-09"), (1, "2024-04-09"))
    selected, trunc = _select_games(games, None)
    assert [g["gamePk"] for g in selected] == [3, 1] and trunc == 0  # provider order


def test_mlb_select_canonical_and_bounded() -> None:
    games = _mlb_games((3, "2024-04-10"), (1, "2024-04-09"), (2, "2024-04-09"))
    selected, trunc = _select_games(games, 2)
    # canonical order = (date, pk): (04-09,1),(04-09,2),(04-10,3) -> first 2
    assert [g["gamePk"] for g in selected] == [1, 2] and trunc == 1


@pytest.mark.parametrize("limit,expected,trunc", [
    (0, [], 3), (1, [1], 2), (3, [1, 2, 3], 0), (5, [1, 2, 3], 0),
])
def test_mlb_select_boundaries(limit: int, expected: list[int], trunc: int) -> None:
    games = _mlb_games((2, "2024-04-09"), (1, "2024-04-09"), (3, "2024-04-09"))
    selected, t = _select_games(games, limit)
    assert [g["gamePk"] for g in selected] == expected and t == trunc


def test_mlb_select_dedupes_repeated_game() -> None:
    games = _mlb_games((1, "2024-04-09"), (1, "2024-04-09"), (2, "2024-04-09"))
    selected, trunc = _select_games(games, 5)
    assert [g["gamePk"] for g in selected] == [1, 2] and trunc == 0


def test_mlb_select_reordered_responses_same_selection() -> None:
    a = _select_games(_mlb_games((3, "2024-04-09"), (1, "2024-04-09"), (2, "2024-04-09")), 2)[0]
    b = _select_games(_mlb_games((1, "2024-04-09"), (2, "2024-04-09"), (3, "2024-04-09")), 2)[0]
    assert [g["gamePk"] for g in a] == [g["gamePk"] for g in b] == [1, 2]


# --- NBA canonical selection (pure) ---------------------------------------- #
class _Norm:
    def __init__(self, game_id: int, date_local: str) -> None:
        self.game_id = str(game_id)
        self.date_local = date_local


def _nba_entries(*ids_dates: tuple[int, str]):  # type: ignore[no-untyped-def]
    games = [({"id": i}, None) for i, _ in ids_dates]
    norms = [(_Norm(i, d), None) for i, d in ids_dates]
    return games, norms


def test_nba_select_canonical_bounded_and_dedup() -> None:
    games, norms = _nba_entries((30, "2024-01-06"), (10, "2024-01-05"),
                                (20, "2024-01-05"), (10, "2024-01-05"))
    sel_games, sel_norms, trunc = _select_nba_games(games, norms, 2)
    assert [n.game_id for n, _ in sel_norms] == ["10", "20"]  # (date,id) order, deduped
    assert [g["id"] for g, _ in sel_games] == [10, 20]
    assert trunc == 1  # 3 distinct games, kept 2


def test_nba_select_unbounded_preserves_order() -> None:
    games, norms = _nba_entries((30, "2024-01-06"), (10, "2024-01-05"))
    sel_games, _sn, trunc = _select_nba_games(games, norms, None)
    assert [g["id"] for g, _ in sel_games] == [30, 10] and trunc == 0


# --- MLB integration: rich fetches only for selected games ----------------- #
def test_mlb_ingest_max_games_bounds_rich_fetches(tmp_path: Path) -> None:
    box_pks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "boxscore" in path:
            m = re.search(r"/game/(\d+)/boxscore", path)
            if m:
                box_pks.append(m.group(1))
            return httpx.Response(200, json={"teams": {"home": {}, "away": {}}})
        if "/schedule" in path:
            return httpx.Response(200, json=schedule(
                game(game_pk=3, official_date="2024-04-10"),
                game(game_pk=1, official_date="2024-04-09"),
                game(game_pk=2, official_date="2024-04-09")))
        return httpx.Response(200, json={})

    db = tmp_path / "s.db"
    initialize_database(db)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mlb.invalid")
    client = MlbStatsApiClient(client=http, max_retries=0)

    async def go():  # type: ignore[no-untyped-def]
        try:
            return await ingest_mlb(database=Database(db), client=client,
                                    from_date="2024-04-09", to_date="2024-04-10",
                                    includes=("box",), max_games=2)
        finally:
            await client.aclose()

    result = asyncio.run(go())
    assert result.games_received == 3
    assert result.games_truncated == 1
    assert result.ordered_game_ids == ("1", "2")  # canonical first-2
    assert sorted(box_pks) == ["1", "2"]  # game 3 (beyond bound) never fetched
