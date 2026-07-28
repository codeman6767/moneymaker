"""Phase E1 adversarial leakage fixtures DQ-PIT-001..011 + isolation (task §9).

Each test PLANTS a specific leak and asserts the feature-facing guard blocks it.
Phase E now covers DQ-PIT-001 through DQ-PIT-011 (not an obsolete 001-010 range).
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from sports_quant.pit import (
    AsOfReader,
    Cutoff,
    assert_column_readable,
    assert_joinable,
)
from sports_quant.pit.registry import (
    ForbiddenColumnError,
    ForbiddenJoinError,
    UnknownTableError,
)

from .conftest import (
    CUTOFF,
    T1,
    T2,
    Ctx,
    seed_injury,
    seed_lineup,
    seed_price,
    seed_result,
    seed_sb_outcome_ctx,
    seed_status,
    seed_team_stat,
    seed_weather,
)

_FAR_FUTURE = "2999-01-01T00:00:00.000000Z"
_PKG = Path(__file__).resolve().parent.parent


def _reader(conn: sqlite3.Connection, at: str = CUTOFF) -> AsOfReader:
    return AsOfReader(conn, Cutoff.parse(at))


# DQ-PIT-001 -- final result observed after the pregame cutoff --------------- #
def test_dq_pit_001_final_result_not_a_pregame_feature(conn: sqlite3.Connection,
                                                       ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    assert _reader(conn).game_result(ctx.game_ref_id) is None          # feature-facing: blind
    assert _reader(conn, T2).game_result(ctx.game_ref_id) is not None  # later: usable as label
    with pytest.raises(ForbiddenColumnError):  # no direct games.status join
        assert_column_readable("games", "status")


# DQ-PIT-002 -- postgame statistics ----------------------------------------- #
def test_dq_pit_002_postgame_stats_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=2)   # mid-season, known
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T2,
                   runs=11)  # season-final, future
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 2


# DQ-PIT-003 -- lineup before publication ----------------------------------- #
def test_dq_pit_003_confirmed_lineup_after_cutoff_absent(conn: sqlite3.Connection,
                                                         ctx: Ctx) -> None:
    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T2,
                is_confirmed=True)  # only a future confirmed lineup exists
    assert _reader(conn).lineup(ctx.game_ref_id, ctx.home_team_id) is None


# DQ-PIT-004 -- injury published before, observed after --------------------- #
def test_dq_pit_004_injury_observed_after_cutoff_invisible(conn: sqlite3.Connection,
                                                           ctx: Ctx) -> None:
    seed_injury(conn, player_ref_id=ctx.player_ref_id, team_id=ctx.home_team_id,
                player_id=ctx.player_id, game_ref_id=ctx.game_ref_id, observed_at=T2,
                published_at=T1)
    assert _reader(conn).injury(ctx.player_ref_id) is None


# DQ-PIT-005 -- closing odds isolated --------------------------------------- #
def test_dq_pit_005_closing_line_isolated_from_features(conn: sqlite3.Connection,
                                                        ctx: Ctx) -> None:
    outcome_id = seed_sb_outcome_ctx(conn)
    seed_price(conn, sb_outcome_id=outcome_id, price_american=-115, observed_at=T1)
    reader = _reader(conn, _FAR_FUTURE)
    # No feature-facing closing-line accessor exists.
    assert not hasattr(reader, "closing_line")
    assert not any("closing" in name for name in dir(reader))
    # It is reachable ONLY through the evaluation-only module.
    from sports_quant.pit.evaluation_only import closing_line_for_evaluation
    close = closing_line_for_evaluation(conn, sb_outcome_id=outcome_id,
                                        game_start=Cutoff.parse(_FAR_FUTURE))
    assert close is not None and close.price_american == -115


# DQ-PIT-006 -- future sportsbook snapshot (inner-aggregate) ----------------- #
def test_dq_pit_006_future_price_snapshot_trapped(conn: sqlite3.Connection, ctx: Ctx) -> None:
    outcome_id = seed_sb_outcome_ctx(conn)
    seed_price(conn, sb_outcome_id=outcome_id, price_american=-110, observed_at=T1)
    seed_price(conn, sb_outcome_id=outcome_id, price_american=+250, observed_at=T2)  # future
    price = _reader(conn).sportsbook_price(outcome_id)
    assert price is not None and price.price_american == -110  # not the future +250, not None


# DQ-PIT-007 -- unsafe mutable dimension join ------------------------------- #
def test_dq_pit_007_registry_blocks_unsafe_joins() -> None:
    assert_joinable({"games", "teams", "game_status_history", "weather_snapshots"})  # ok
    for bad in ("sportsbook_events", "kalshi_markets", "provider_game_references"):
        with pytest.raises(ForbiddenJoinError):
            assert_joinable({bad})
    with pytest.raises(ForbiddenJoinError):
        assert_joinable({"kalshi_orderbook_snapshots"})  # evaluation_only
    with pytest.raises(UnknownTableError):
        assert_joinable({"totally_made_up_table"})       # unknown fails closed


# DQ-PIT-008 -- overwritten historical snapshot ----------------------------- #
def test_dq_pit_008_append_only_update_delete_rejected(conn: sqlite3.Connection,
                                                       ctx: Ctx) -> None:
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=4)
    seed_status(conn, game_id=ctx.game_id, status="scheduled", observed_at=T1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE team_game_statistics SET runs = 99")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM team_game_statistics")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE game_status_history SET status = 'final'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM game_status_history")


# DQ-PIT-009 -- provider clock skew ----------------------------------------- #
def test_dq_pit_009_ordering_uses_observed_at_not_provider_time(conn: sqlite3.Connection,
                                                                ctx: Ctx) -> None:
    # Row A observed earlier but with a LATER provider_timestamp; row B observed
    # later with an EARLIER provider_timestamp. As-of ordering must follow
    # observed_at -> B wins.
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                   observed_at="2026-07-08T00:00:00.000000Z", runs=1,
                   provider_timestamp="2030-01-01T00:00:00.000000Z")
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                   observed_at="2026-07-11T00:00:00.000000Z", runs=2,
                   provider_timestamp="2020-01-01T00:00:00.000000Z")
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 2


# DQ-PIT-010 -- future match decision --------------------------------------- #
def test_dq_pit_010_future_match_decision_unresolved_earlier(conn: sqlite3.Connection,
                                                             ctx: Ctx) -> None:
    from .conftest import link_sb_event
    seed_sb_outcome_ctx(conn)
    ev = str(conn.execute("SELECT sb_event_id FROM sportsbook_events LIMIT 1").fetchone()[0])
    link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id)  # decided ~now
    early = _reader(conn, "2000-01-01T00:00:00.000000Z")
    assert early.sportsbook_event_game(ev) is None  # unresolved before the decision
    assert early.accepted_decision(source_provider="the_odds_api", source_ref=ev,
                                    entity_type="sportsbook_event") is None


# DQ-PIT-011 -- unsafe weather type ----------------------------------------- #
def test_dq_pit_011_unsafe_weather_rows_rejected(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="station_observation", observed_at=T1, pit_eligible=True)
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="reanalysis", observed_at=T1, pit_eligible=True,
                 forecast_mode="reanalysis")
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="historical_forecast", observed_at=T1, pit_eligible=None,
                 forecast_mode="hist")
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="current_forecast", observed_at=T2, pit_eligible=True)  # future
    assert _reader(conn).weather_pregame_forecast(ctx.game_ref_id, forecast_mode="point") is None
    # A valid current_forecast observed by the cutoff IS returned.
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="current_forecast", observed_at=T1, pit_eligible=True)
    good = _reader(conn).weather_pregame_forecast(ctx.game_ref_id, forecast_mode="point")
    assert good is not None and good.get("weather_kind") == "current_forecast"


# -- structural isolation --------------------------------------------------- #
def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_feature_modules_do_not_import_evaluation_only() -> None:
    for module in ("asof.py", "registry.py", "models.py", "__init__.py"):
        imports = _imports_of(_PKG / module)
        assert not any("evaluation_only" in m for m in imports), module


def test_pit_package_imports_no_provider_or_execution_code() -> None:
    banned = ("gateway", "execution", "httpx", "requests", "providers.kalshi",
              "providers.odds", "order", "streaming")
    for module in ("asof.py", "registry.py", "models.py", "evaluation_only.py", "__init__.py"):
        text = (_PKG / module).read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in _imports_joined(_PKG / module), (module, token)
        assert "import httpx" not in text and "import requests" not in text


def _imports_joined(path: Path) -> str:
    return " ".join(sorted(_imports_of(path)))


def test_games_status_column_is_forbidden() -> None:
    with pytest.raises(ForbiddenColumnError):
        assert_column_readable("games", "status")
    assert_column_readable("games", "home_team_id")  # identity column is fine
