"""Game-status history: stale-backfill protection and transition deduplication.

Two behaviours are pinned here, both corrected by the a003 integrity patch:

1. **A late-arriving observation of an earlier moment never regresses current
   state.** It is preserved in history, but ``games.status`` continues to
   reflect the newest observation.
2. **Deduplication is transition-aware, not global.** An unchanged re-poll is
   suppressed; a genuine return to an earlier state is recorded.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories import SqliteGameRepository, SqliteSeasonRepository
from sports_quant.db.schema import utc_now_iso

START = "2026-07-04T23:05:00.000000Z"
MOVED = "2026-07-05T23:05:00.000000Z"

T0 = "2026-07-04T20:00:00.000000Z"
T1 = "2026-07-04T21:00:00.000000Z"
T2 = "2026-07-04T22:00:00.000000Z"
T3 = "2026-07-04T23:00:00.000000Z"


@pytest.fixture
def game_id(conn: sqlite3.Connection, mlb_league_id: str) -> str:
    season = SqliteSeasonRepository(conn).upsert(
        league_code="MLB", league_id=mlb_league_id, year=2026, phase="regular",
        label="2026", start_date="2026-03-26",
    )
    return SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=season.season_id,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    ).game_id


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteGameRepository:
    return SqliteGameRepository(conn)


# --------------------------------------------------------------------------- #
# Stale backfill
# --------------------------------------------------------------------------- #
def test_newer_observation_becomes_current(
    repo: SqliteGameRepository, game_id: str
) -> None:
    repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                       provider="mlb", observed_at=T2)

    current = repo.get(game_id)
    assert current is not None
    assert current.status == "in_progress"


def test_older_backfill_is_preserved_in_history(
    repo: SqliteGameRepository, game_id: str
) -> None:
    repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                       provider="mlb", observed_at=T2)
    # A late-arriving observation describing an earlier moment.
    assert repo.record_status(game_id=game_id, status="pregame", scheduled_start=START,
                              provider="mlb", observed_at=T0) is True

    history = repo.status_history(game_id)
    assert [h.status for h in history] == ["pregame", "in_progress"]


def test_older_backfill_does_not_replace_current_status(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """The regression this patch fixes: a stale row used to overwrite the present."""

    repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                       provider="mlb", observed_at=T2)
    repo.record_status(game_id=game_id, status="pregame", scheduled_start=START,
                       provider="mlb", observed_at=T0)

    current = repo.get(game_id)
    assert current is not None
    assert current.status == "in_progress"


def test_backfill_does_not_regress_scheduled_start(
    repo: SqliteGameRepository, game_id: str
) -> None:
    repo.record_status(game_id=game_id, status="rescheduled", scheduled_start=MOVED,
                       provider="mlb", observed_at=T2)
    repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                       provider="mlb", observed_at=T0)

    current = repo.get(game_id)
    assert current is not None
    assert current.scheduled_start == MOVED
    # original_start is still the very first value, never rewritten.
    assert current.original_start == START


def test_out_of_order_arrival_converges_to_the_newest(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """Whatever the arrival order, current state is the newest observation."""

    for status, observed in [("final", T3), ("pregame", T0), ("in_progress", T2),
                             ("scheduled", T1)]:
        repo.record_status(game_id=game_id, status=status, scheduled_start=START,
                           provider="mlb", observed_at=observed)

    current = repo.get(game_id)
    assert current is not None
    assert current.status == "final"
    assert len(repo.status_history(game_id)) == 4


def test_equal_observed_at_resolves_deterministically(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """Ties on observed_at break by status_id, which is a monotonic ULID.

    The most recently recorded observation therefore wins, and a rebuild
    produces the same answer.
    """

    repo.record_status(game_id=game_id, status="delayed", scheduled_start=START,
                       provider="mlb", observed_at=T1)
    repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                       provider="mlb", observed_at=T1)

    history = repo.status_history(game_id)
    assert len(history) == 2
    assert history[0].status_id < history[1].status_id

    current = repo.get(game_id)
    assert current is not None
    assert current.status == "in_progress"
    # The as-of accessor uses the same ordering, so the two never disagree.
    as_of = repo.status_as_of(game_id, T1)
    assert as_of is not None
    assert as_of.status == "in_progress"


def test_original_start_survives_a_full_reschedule_sequence(
    repo: SqliteGameRepository, game_id: str
) -> None:
    repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    repo.record_status(game_id=game_id, status="postponed", scheduled_start=START,
                       provider="mlb", observed_at=T1, detail="rain")
    repo.record_status(game_id=game_id, status="rescheduled", scheduled_start=MOVED,
                       provider="mlb", observed_at=T2)

    current = repo.get(game_id)
    assert current is not None
    assert current.original_start == START
    assert current.scheduled_start == MOVED


# --------------------------------------------------------------------------- #
# Transition-aware deduplication
# --------------------------------------------------------------------------- #
def test_unchanged_repoll_is_suppressed(
    repo: SqliteGameRepository, game_id: str
) -> None:
    assert repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                              provider="mlb", observed_at=T0) is True
    # Polling again five minutes later with nothing changed is not news.
    assert repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                              provider="mlb", observed_at=T1) is False
    assert len(repo.status_history(game_id)) == 1


def test_repeated_transition_is_recorded(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """delayed -> in_progress -> delayed: an ordinary rain delay that re-delays.

    Before the a003 patch the global ``(game_id, provider, content_hash)``
    uniqueness silently dropped the third observation -- the corpus lost the
    transition, and current state was left reading ``in_progress`` while the
    game was actually delayed. This test pins the corrected behaviour.
    """

    assert repo.record_status(game_id=game_id, status="delayed", scheduled_start=START,
                              provider="mlb", observed_at=T0) is True
    assert repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                              provider="mlb", observed_at=T1) is True
    assert repo.record_status(game_id=game_id, status="delayed", scheduled_start=START,
                              provider="mlb", observed_at=T2) is True

    assert [h.status for h in repo.status_history(game_id)] == [
        "delayed", "in_progress", "delayed"
    ]
    current = repo.get(game_id)
    assert current is not None
    assert current.status == "delayed"


def test_repeated_transition_works_without_a_provider_timestamp(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """The failure mode was worst when provider_timestamp was absent."""

    for status, observed in [("delayed", T0), ("in_progress", T1), ("delayed", T2),
                             ("in_progress", T3)]:
        assert repo.record_status(game_id=game_id, status=status, scheduled_start=START,
                                  provider="mlb", observed_at=observed,
                                  provider_timestamp=None) is True
    assert len(repo.status_history(game_id)) == 4


def test_exact_duplicate_observation_is_idempotent(
    repo: SqliteGameRepository, game_id: str
) -> None:
    for _ in range(3):
        repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                           provider="mlb", observed_at=T0)
    assert len(repo.status_history(game_id)) == 1


def test_backfilling_an_already_recorded_observation_is_idempotent(
    repo: SqliteGameRepository, game_id: str
) -> None:
    repo.record_status(game_id=game_id, status="pregame", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    repo.record_status(game_id=game_id, status="in_progress", scheduled_start=START,
                       provider="mlb", observed_at=T2)
    # Replaying the older row must not duplicate it, even though the newest
    # state now differs from it.
    assert repo.record_status(game_id=game_id, status="pregame", scheduled_start=START,
                              provider="mlb", observed_at=T0) is False
    assert len(repo.status_history(game_id)) == 2


def test_deduplication_is_scoped_per_provider(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """Two providers reporting the same state are two independent observations."""

    assert repo.record_status(game_id=game_id, status="final", scheduled_start=START,
                              provider="mlb", observed_at=T1) is True
    assert repo.record_status(game_id=game_id, status="final", scheduled_start=START,
                              provider="odds", observed_at=T2) is True
    assert len(repo.status_history(game_id)) == 2


def test_database_rejects_an_exact_duplicate_row(
    conn: sqlite3.Connection, repo: SqliteGameRepository, game_id: str
) -> None:
    """The repository suppresses no-change polls; the UNIQUE is the backstop."""

    repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    row = conn.execute(
        "SELECT content_hash FROM game_status_history WHERE game_id = ?", (game_id,)
    ).fetchone()
    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO game_status_history (status_id, game_id, status, scheduled_start, "
            "provider, observed_at, ingested_at, content_hash, created_at) VALUES "
            "('gst_manual_dupe', ?, 'scheduled', ?, 'mlb', ?, ?, ?, ?)",
            (game_id, START, T0, now, str(row["content_hash"]), now),
        )


def test_same_state_at_a_later_time_is_allowed_by_the_constraint(
    conn: sqlite3.Connection, repo: SqliteGameRepository, game_id: str
) -> None:
    """Uniqueness includes observed_at, so a re-entry into a state is storable."""

    repo.record_status(game_id=game_id, status="delayed", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    row = conn.execute(
        "SELECT content_hash FROM game_status_history WHERE game_id = ?", (game_id,)
    ).fetchone()
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO game_status_history (status_id, game_id, status, scheduled_start, "
        "provider, observed_at, ingested_at, content_hash, created_at) VALUES "
        "('gst_later_same_state', ?, 'delayed', ?, 'mlb', ?, ?, ?, ?)",
        (game_id, START, T2, now, str(row["content_hash"]), now),
    )
    assert len(repo.status_history(game_id)) == 2


def test_history_remains_append_only_after_the_rebuild(
    conn: sqlite3.Connection, repo: SqliteGameRepository, game_id: str
) -> None:
    """a003 rebuilds the table; the append-only triggers must survive it."""

    repo.record_status(game_id=game_id, status="scheduled", scheduled_start=START,
                       provider="mlb", observed_at=T0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE game_status_history SET status = 'final'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM game_status_history")


def test_status_as_of_still_ignores_backdated_provider_timestamps(
    repo: SqliteGameRepository, game_id: str
) -> None:
    """DQ-PIT-004 must still hold after the rebuild."""

    repo.record_status(game_id=game_id, status="postponed", scheduled_start=START,
                       provider="mlb", observed_at=T2, provider_timestamp=T0)
    assert repo.status_as_of(game_id, T1) is None
    assert repo.status_as_of(game_id, T2) is not None
