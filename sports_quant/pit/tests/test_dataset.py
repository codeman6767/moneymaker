"""Phase E2 historical dataset builder tests (offline, deterministic)."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from sports_quant.pit.dataset import HistoricalDataset, build_historical_dataset

from .conftest import CUTOFF, T1, T2, Ctx, seed_result, seed_status

# The pit `ctx` game is scheduled at SCHED_START (2026-07-15T02:10:00Z); a result
# observed at T2 (2026-07-16) is strictly after that pregame cutoff.


def _build(conn: sqlite3.Connection, league: str = "mlb") -> HistoricalDataset:
    return build_historical_dataset(conn, league=league)


def test_mlb_happy_path_one_pregame_row(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home",
                mapped_status="final")
    ds = _build(conn, "mlb")
    assert len(ds) == 1
    row = ds.rows[0]
    assert row.sport == "mlb" and row.label == 1 and row.winning_side == "home"
    assert row.score_diff == 0 and row.phase == 0            # pregame state only
    assert row.cutoff == "2026-07-15T02:10:00.000000Z"        # scheduled start
    assert row.label_observed_at == T2                        # strictly after cutoff
    assert "game_id" not in row.feature_state()               # ULID excluded (rebuild-stable)


def test_away_win_label_zero(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="away")
    ds = _build(conn, "mlb")
    assert len(ds) == 1 and ds.rows[0].label == 0


def test_empty_corpus(conn: sqlite3.Connection) -> None:
    ds = _build(conn, "mlb")
    assert len(ds) == 0
    gs = ds.to_game_state_dataset()
    assert gs.X.shape == (0, 0) and len(gs) == 0
    tr, te = gs.chronological_split()  # deterministic on empty
    assert len(tr) == 0 and len(te) == 0


def test_unfinished_game_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side=None,
                mapped_status="in_progress")
    assert len(_build(conn, "mlb")) == 0  # no fabricated label


def test_result_before_cutoff_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    # A final result observed BEFORE the scheduled start would be a leak; excluded.
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-10T00:00:00.000000Z",
                winning_side="home", mapped_status="final")
    assert len(_build(conn, "mlb")) == 0


def test_corrected_result_uses_latest(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-18T00:00:00.000000Z",
                winning_side="away", mapped_status="final")  # correction: away actually won
    ds = _build(conn, "mlb")
    assert len(ds) == 1 and ds.rows[0].label == 0  # latest (corrected) result wins


def test_gamestate_conversion_no_fabrication(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    gs = _build(conn, "mlb").to_game_state_dataset()
    assert gs.X.shape == (1, 0) and gs.X.dtype == np.float32       # zero-column, no features
    assert np.isnan(gs.true_prob).all()                            # explicitly unavailable
    assert gs.y.tolist() == [1] and gs.y.dtype == np.int8
    assert gs.score_diff.tolist() == [0] and gs.phase.tolist() == [0]


def test_status_history_not_required_for_row(conn: sqlite3.Connection, ctx: Ctx) -> None:
    # A stray status observation must not change the pregame cutoff/label.
    seed_status(conn, game_id=ctx.game_id, status="final", observed_at=T2)
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    ds = _build(conn, "mlb")
    assert len(ds) == 1 and ds.rows[0].cutoff == "2026-07-15T02:10:00.000000Z"
    _ = (CUTOFF, T1)


# --------------------------------------------------------------------------- #
# NBA
# --------------------------------------------------------------------------- #
def test_nba_happy_path(conn: sqlite3.Connection) -> None:
    from .conftest import seed_nba_ctx, seed_nba_result
    nba = seed_nba_ctx(conn)
    seed_nba_result(conn, game_ref_id=nba.game_ref_id, observed_at=T2, winning_side="home")
    ds = _build(conn, "nba")
    assert len(ds) == 1 and ds.rows[0].sport == "nba" and ds.rows[0].label == 1
    # An MLB build sees nothing (league isolation).
    assert len(_build(conn, "mlb")) == 0


# --------------------------------------------------------------------------- #
# Adversarial: future decision, equal-time conflict, leakage isolation
# --------------------------------------------------------------------------- #
def test_future_game_decision_excluded(conn: sqlite3.Connection) -> None:
    # The game<->reference 'game' decision is decided AFTER the scheduled-start
    # cutoff -> the correspondence was not known pregame -> row excluded.
    ref = _seed_mlb_game(conn, key="FUT", date="2026-08-01", sched="2026-08-01T18:00:00Z",
                         result_at="2026-08-02T00:00:00.000000Z", winning="home",
                         decided_at="2030-01-01T00:00:00.000000Z")
    assert ref
    assert len(_build(conn, "mlb")) == 0


def test_equal_time_conflicting_result_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    # Two DIFFERENT final results at the SAME observed_at -> fail closed (excluded);
    # never a generated-id winner.
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="away")
    assert len(_build(conn, "mlb")) == 0


def test_label_correction_changes_label_surface_not_feature_state(conn: sqlite3.Connection,
                                                                 ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    before_state = _build(conn, "mlb").serialize()
    before_labels = _build(conn, "mlb").serialize_labels()
    # A LATER correction flips the label (append-only correction semantics). The
    # FEATURE-STATE surface (identity + cutoff + pregame state) is unchanged; only
    # the SEPARATE label surface changes.
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-20T00:00:00.000000Z",
                winning_side="away", mapped_status="final")
    after = _build(conn, "mlb")
    assert after.rows[0].label == 0
    assert after.serialize() == before_state        # feature-state UNCHANGED by a label change
    assert after.serialize_labels() != before_labels  # label surface reflects the correction


# --------------------------------------------------------------------------- #
# Determinism: random insertion order + fresh-database rebuild byte-identical
# --------------------------------------------------------------------------- #
def test_deterministic_across_insertion_order_and_fresh_dbs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import random

    from sports_quant.db.engine import Database
    from sports_quant.db.init import initialize_database

    games = [
        dict(key="G1", date="2026-07-14", sched="2026-07-15T02:10:00Z",
             result_at="2026-07-16T00:00:00.000000Z", winning="home"),
        dict(key="G2", date="2026-07-20", sched="2026-07-21T02:10:00Z",
             result_at="2026-07-22T00:00:00.000000Z", winning="away"),
        dict(key="G3", date="2026-07-25", sched="2026-07-26T02:10:00Z",
             result_at="2026-07-27T00:00:00.000000Z", winning="home"),
    ]
    serials = set()
    for i in range(6):
        p = tmp_path / f"c{i}.db"
        initialize_database(p)
        order = games[:]
        random.Random(i).shuffle(order)
        with Database(p).connection() as conn:
            for gspec in order:
                _seed_mlb_game(conn, key=gspec["key"], date=gspec["date"], sched=gspec["sched"],
                               result_at=gspec["result_at"], winning=gspec["winning"])
        with Database(p).connection() as conn:
            ds = build_historical_dataset(conn, league="mlb")
            serials.add(ds.serialize())
            assert [r.official_game_key for r in ds.rows] == ["G1", "G2", "G3"]  # chronological
            assert ds.labels().tolist() == [1, 0, 1]
    assert len(serials) == 1  # byte-identical across all fresh rebuilds / orders


def test_chronological_split_compatibility(conn: sqlite3.Connection) -> None:
    for gspec in (dict(key="A", date="2026-07-14", sched="2026-07-15T02:10:00Z",
                       result_at="2026-07-16T00:00:00.000000Z", winning="home"),
                  dict(key="B", date="2026-07-20", sched="2026-07-21T02:10:00Z",
                       result_at="2026-07-22T00:00:00.000000Z", winning="away")):
        _seed_mlb_game(conn, **gspec)  # type: ignore[arg-type]
    gs = build_historical_dataset(conn, league="mlb").to_game_state_dataset()
    tr, te = gs.chronological_split(train_frac=0.5)
    assert len(tr) == 1 and len(te) == 1
    assert tr.timestamps[0] < te.timestamps[0]  # never shuffles across time


# --------------------------------------------------------------------------- #
# §3 joined-table safety / isolation
# --------------------------------------------------------------------------- #
def test_dataset_imports_are_isolated() -> None:
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "dataset.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    joined = " ".join(sorted(modules))
    for banned in ("evaluation_only", "gateway", "execution", "httpx", "requests",
                   "providers", "ingest", "order"):
        assert banned not in joined, banned


def test_feature_state_excludes_label_and_provenance(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    row = _build(conn, "mlb").rows[0]
    state = row.feature_state()
    assert set(state) == {"sport", "league_code", "home_team_id", "away_team_id",
                          "official_provider", "official_game_key", "cutoff", "timestamp",
                          "score_diff", "phase"}
    # No label, winner, result timestamp, score, or provenance leaks into state.
    for banned in ("label", "winning_side", "label_observed_at", "game_id", "ingested_at",
                   "created_at", "run_id", "raw_response_id", "content_hash",
                   "provider_timestamp", "home_runs", "away_runs"):
        assert banned not in state
    # A generic iteration over the state dict cannot reveal the outcome.
    assert 1 not in {k: v for k, v in state.items() if k in ("label",)}
    # The label + provenance live ONLY on the separate label surface.
    label = row.label_record()
    assert label["label"] == 1 and label["winning_side"] == "home"
    assert label["label_observed_at"] == T2
    # Dataset feature-state serialization carries no label; label surface does.
    ds = _build(conn, "mlb")
    assert "winning_side" not in ds.serialize() and "label" not in ds.serialize()
    assert "winning_side" in ds.serialize_labels()


# --------------------------------------------------------------------------- #
# RF1: schedule/cutoff historical-visibility policy
# --------------------------------------------------------------------------- #
def test_schedule_first_observed_after_start_excluded(conn: sqlite3.Connection) -> None:
    # The ONLY schedule for this game was first observed (2026-07-24) AFTER its
    # scheduled start (2026-07-15). No historically-visible pregame cutoff exists.
    _seed_mlb_game(conn, key="AFT", date="2026-07-14", sched="2026-07-15T02:10:00Z",
                   result_at="2026-08-01T00:00:00.000000Z", winning="home",
                   sched_observed="2026-07-24T00:00:00.000000Z")
    assert len(_build(conn, "mlb")) == 0


def test_pregame_schedule_then_postgame_correction_stable(conn: sqlite3.Connection) -> None:
    from .conftest import seed_schedule_snapshot
    ref = _seed_mlb_game(conn, key="COR", date="2026-07-14", sched="2026-07-15T02:10:00Z",
                         result_at="2026-07-16T00:00:00.000000Z", winning="home",
                         sched_observed="2026-07-10T00:00:00.000000Z")  # known pregame
    before = _build(conn, "mlb")
    assert len(before) == 1 and before.rows[0].cutoff == "2026-07-15T02:10:00.000000Z"
    # A POSTGAME schedule correction (different start, observed after the game) must
    # NOT move the row's cutoff/ordering.
    seed_schedule_snapshot(conn, game_ref_id=ref, provider_game_id="COR",
                           scheduled_start="2026-09-01T02:10:00Z",
                           observed_at="2026-07-20T00:00:00.000000Z")
    after = _build(conn, "mlb")
    assert after.rows[0].cutoff == "2026-07-15T02:10:00.000000Z"  # unchanged
    assert after.serialize() == before.serialize()


def test_pregame_postponement_earliest_known_schedule_sets_cutoff(
        conn: sqlite3.Connection) -> None:
    from .conftest import seed_schedule_snapshot
    # First-known schedule (observed 2026-07-05, start 2026-07-10) sets the cutoff;
    # a later postponement to 2026-07-20 (observed 2026-07-08) does not move it,
    # because only the earliest historically-visible schedule sets the cutoff.
    ref = _seed_mlb_game(conn, key="PPD", date="2026-07-09", sched="2026-07-10T02:10:00Z",
                         result_at="2026-07-21T00:00:00.000000Z", winning="home",
                         sched_observed="2026-07-05T00:00:00.000000Z")
    seed_schedule_snapshot(conn, game_ref_id=ref, provider_game_id="PPD",
                           scheduled_start="2026-07-20T02:10:00Z",
                           observed_at="2026-07-08T00:00:00.000000Z")
    ds = _build(conn, "mlb")
    assert len(ds) == 1 and ds.rows[0].cutoff == "2026-07-10T02:10:00.000000Z"


def test_equal_time_conflicting_schedule_fails_closed(conn: sqlite3.Connection) -> None:
    from .conftest import seed_schedule_snapshot
    ref = _seed_mlb_game(conn, key="CFL", date="2026-07-14", sched="2026-07-15T02:10:00Z",
                         result_at="2026-07-20T00:00:00.000000Z", winning="home",
                         sched_observed="2026-07-05T00:00:00.000000Z")
    # A SECOND schedule with a different start at the SAME earliest observed_at ->
    # the earliest schedule is ambiguous -> cutoff fails closed -> excluded.
    seed_schedule_snapshot(conn, game_ref_id=ref, provider_game_id="CFL",
                           scheduled_start="2026-07-16T02:10:00Z",
                           observed_at="2026-07-05T00:00:00.000000Z")
    assert len(_build(conn, "mlb")) == 0


def test_timestamp_is_microseconds_distinct_within_second(conn: sqlite3.Connection) -> None:
    # Two games scheduled 100us apart must get distinct, correctly-ordered timestamps.
    _seed_mlb_game(conn, key="EARLY", date="2026-07-14", sched="2026-07-15T02:10:00.000000Z",
                   result_at="2026-07-16T00:00:00.000000Z", winning="home",
                   sched_observed="2026-07-10T00:00:00.000000Z")
    _seed_mlb_game(conn, key="LATE", date="2026-07-15", sched="2026-07-15T02:10:00.000100Z",
                   result_at="2026-07-16T00:00:00.000000Z", winning="away",
                   sched_observed="2026-07-10T00:00:00.000000Z")
    ds = _build(conn, "mlb")
    ts = [r.timestamp for r in ds.rows]
    assert len(ds) == 2 and ts[0] < ts[1] and ts[1] - ts[0] == 100  # microseconds preserved


def test_future_nonlabel_observation_does_not_change_row(conn: sqlite3.Connection,
                                                        ctx: Ctx) -> None:
    from .conftest import seed_team_stat
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    before = _build(conn, "mlb").serialize()
    # A team-stat observed AFTER the label horizon is irrelevant to the pregame row
    # (state stays 0/0; identity/label unchanged).
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                   observed_at="2027-01-01T00:00:00.000000Z", runs=99)
    assert _build(conn, "mlb").serialize() == before


def test_games_identity_columns_are_feature_safe() -> None:
    from sports_quant.pit.dataset import _GAMES_IDENTITY_COLUMNS
    from sports_quant.pit.registry import ForbiddenColumnError, assert_selectable
    assert_selectable("games", list(_GAMES_IDENTITY_COLUMNS))  # all identity, safe
    with pytest.raises(ForbiddenColumnError):
        assert_selectable("games", ["status"])  # mutable current-state rejected
    with pytest.raises(ForbiddenColumnError):
        assert_selectable("games", ["scheduled_start"])


def _seed_mlb_game(conn: sqlite3.Connection, *, key: str, date: str, sched: str, result_at: str,
                   winning: str, decided_at: str = "2020-01-01T00:00:00.000000Z",
                   sched_observed: str = "2026-07-01T00:00:00.000000Z") -> str:
    from sports_quant.matching.tests.conftest import seed_schedule, seed_team
    from sports_quant.matching.tests.test_phase_d5a_matching import _create_canonical
    home = seed_team(conn, league_code="MLB", abbreviation="LAD",
                     canonical_name="Los Angeles Dodgers", city="Los Angeles", nickname="Dodgers")
    away = seed_team(conn, league_code="MLB", abbreviation="SD",
                     canonical_name="San Diego Padres", city="San Diego", nickname="Padres")
    ref = seed_schedule(conn, provider="mlb_statsapi", provider_game_id=key,
                        home_provider_team_id="101", away_provider_team_id="102",
                        scheduled_start=sched, season=2026, game_date_local=date,
                        observed_at=sched_observed)
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start=sched, game_date_local=date, official_provider="mlb_statsapi",
                      official_game_key=key, decided_at=decided_at)
    seed_result(conn, game_ref_id=ref, observed_at=result_at, winning_side=winning)
    return ref
