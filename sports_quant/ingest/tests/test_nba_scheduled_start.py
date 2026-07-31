"""Adversarial tests for NBA scheduled-start normalization.

The defect these pin down: `_normalize_game` derived `scheduled_start` from the
`status` field and only when the status happened to be `"scheduled"`, so a
**final** game silently lost the tipoff instant the provider had actually supplied
in `datetime`. Official game matching then correctly refused the game with
`no scheduled start to match a game on`.

Status and scheduled start are separate facts. Every test here holds that line,
and none of them opens a socket.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator, Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.observations import ObservationOutcome
from sports_quant.db.repositories.official_games import SqliteScheduleRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.ingest.nba_ingestor import (
    ScheduledStartKind,
    _normalize_game,
    _parse_provider_scheduled_start,
    _resolve_scheduled_start,
)

NBA = "balldontlie"
#: The provider's own value for the F1B pilot game, and its canonical UTC form.
PILOT_DATETIME = "2026-01-06T00:00:00.000Z"
PILOT_CANONICAL = "2026-01-06T00:00:00.000000Z"
T0 = "2026-01-06T04:00:00.000000Z"
T1 = "2026-01-06T05:00:00.000000Z"

_NYK = {"id": 20, "abbreviation": "NYK", "city": "New York", "conference": "East",
        "division": "Atlantic", "full_name": "New York Knicks", "name": "Knicks"}
_DET = {"id": 9, "abbreviation": "DET", "city": "Detroit", "conference": "East",
        "division": "Central", "full_name": "Detroit Pistons", "name": "Pistons"}


def game(
    *,
    status: Any = "Final",
    datetime_value: Any = PILOT_DATETIME,
    date: Any = "2026-01-05",
    include_datetime: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """One BALLDONTLIE game payload, shaped like the preserved pilot response."""

    payload: dict[str, Any] = {
        "id": 18447316, "date": date, "season": 2025, "status": status, "period": 4,
        "home_team": _DET, "visitor_team": _NYK,
        "home_team_score": 121, "visitor_team_score": 90,
    }
    if include_datetime:
        payload["datetime"] = datetime_value
    payload.update(extra)
    return payload


def norm(**kw: Any):
    normalized, reason = _normalize_game(game(**kw))
    assert normalized is not None, reason
    return normalized


# --------------------------------------------------------------------------- #
# 1-4: every status keeps the supplied scheduled start
# --------------------------------------------------------------------------- #
def test_final_game_retains_the_provider_scheduled_start() -> None:
    """The exact regression: a final game must not lose its tipoff."""

    n = norm(status="Final")
    assert n.mapped_status == "final"
    assert n.scheduled_start == PILOT_CANONICAL
    assert n.scheduled_start_source == "datetime"
    assert n.scheduled_start_issue is None


def test_in_progress_game_retains_the_provider_scheduled_start() -> None:
    n = norm(status="3rd Qtr")
    assert n.mapped_status == "in_progress"
    assert n.scheduled_start == PILOT_CANONICAL


def test_scheduled_game_uses_datetime_not_status_display_text() -> None:
    n = norm(status="7:00 pm ET")
    assert n.scheduled_start == PILOT_CANONICAL
    assert n.scheduled_start_source == "datetime"
    # The display string never becomes the timestamp.
    assert n.scheduled_start != "7:00 pm ET"


@pytest.mark.parametrize("status,expected", [
    ("Postponed", "postponed"), ("Suspended", "suspended"), ("Delayed", "delayed"),
    ("Cancelled", "cancelled"),
])
def test_disrupted_statuses_retain_the_provider_scheduled_start(
    status: str, expected: str,
) -> None:
    n = norm(status=status)
    assert n.mapped_status == expected
    assert n.scheduled_start == PILOT_CANONICAL


# --------------------------------------------------------------------------- #
# 5-8: parsing contract
# --------------------------------------------------------------------------- #
def test_z_suffix_normalizes_to_canonical_utc() -> None:
    parsed = _parse_provider_scheduled_start("2026-01-06T00:00:00.000Z")
    assert parsed.kind is ScheduledStartKind.VALID
    assert parsed.value == PILOT_CANONICAL


def test_explicit_numeric_offset_normalizes_to_utc() -> None:
    """A -05:00 tipoff is the same instant as the Z form; both land on UTC."""

    parsed = _parse_provider_scheduled_start("2026-01-05T19:00:00-05:00")
    assert parsed.kind is ScheduledStartKind.VALID
    assert parsed.value == PILOT_CANONICAL
    assert _parse_provider_scheduled_start("2026-01-06T09:00:00+09:00").value == (
        PILOT_CANONICAL)


def test_lowercase_z_is_accepted() -> None:
    assert _parse_provider_scheduled_start("2026-01-06T00:00:00.000z").value == (
        PILOT_CANONICAL)


def test_timezone_naive_datetime_is_refused_and_reported() -> None:
    """Never assumed to be UTC: a wrong guess shifts the venue-local date."""

    parsed = _parse_provider_scheduled_start("2026-01-06T00:00:00")
    assert parsed.kind is ScheduledStartKind.TIMEZONE_NAIVE
    assert parsed.value is None
    n = norm(datetime_value="2026-01-06T00:00:00")
    assert n.scheduled_start is None
    assert n.scheduled_start_issue == (
        "provider datetime carries no timezone offset; refusing to assume UTC")
    # The rest of normalization is untouched.
    assert n.mapped_status == "final" and n.date_local == "2026-01-05"


def test_date_only_value_never_becomes_a_tipoff() -> None:
    """A calendar date is not an instant, even though it parses."""

    parsed = _parse_provider_scheduled_start("2026-01-06")
    assert parsed.kind is ScheduledStartKind.TIMEZONE_NAIVE
    assert parsed.value is None


@pytest.mark.parametrize("bad", [
    "not-a-timestamp", "2026-13-45T99:99:99Z", "7:00 pm ET", "Final", "", "  ",
    12345, 1.5, True, [], {}, None,
])
def test_malformed_or_absent_values_never_produce_a_timestamp(bad: Any) -> None:
    parsed = _parse_provider_scheduled_start(bad)
    assert parsed.value is None
    assert parsed.kind is not ScheduledStartKind.VALID


def test_malformed_datetime_is_refused_and_reported() -> None:
    n = norm(datetime_value="not-a-timestamp")
    assert n.scheduled_start is None
    assert n.scheduled_start_issue == (
        "provider datetime is not a parseable ISO-8601 timestamp")
    assert n.mapped_status == "final"


def test_parsing_never_raises_on_hostile_input() -> None:
    for hostile in (object(), b"bytes", float("nan"), "9" * 200, "\x00"):
        assert _parse_provider_scheduled_start(hostile).value is None


# --------------------------------------------------------------------------- #
# 9-11: precedence
# --------------------------------------------------------------------------- #
def test_absent_datetime_falls_back_to_a_full_iso_legacy_status() -> None:
    n = norm(include_datetime=False, status="2026-01-06T00:00:00.000Z")
    assert n.scheduled_start == PILOT_CANONICAL
    assert n.scheduled_start_source == "legacy_status"
    assert n.scheduled_start_issue is None


def test_absent_datetime_with_display_only_status_invents_nothing() -> None:
    for status in ("Final", "7:00 pm ET", "3rd Qtr", "Postponed"):
        n = norm(include_datetime=False, status=status)
        assert n.scheduled_start is None, status
        assert n.scheduled_start_source is None
        # A genuinely absent timestamp is not a data-quality finding.
        assert n.scheduled_start_issue is None


def test_datetime_wins_over_a_conflicting_legacy_iso_status() -> None:
    n = norm(status="2020-01-01T00:00:00Z", datetime_value=PILOT_DATETIME)
    assert n.scheduled_start == PILOT_CANONICAL
    assert n.scheduled_start_source == "datetime"


def test_a_broken_datetime_does_not_fall_through_to_the_legacy_field() -> None:
    """A broken authoritative value must surface, not be papered over."""

    n = norm(datetime_value="garbage", status="2026-01-06T00:00:00Z")
    assert n.scheduled_start is None
    assert n.scheduled_start_issue is not None
    resolved = _resolve_scheduled_start(game(datetime_value="garbage",
                                             status="2026-01-06T00:00:00Z"))
    assert resolved.source == "datetime" and resolved.is_unusable


# --------------------------------------------------------------------------- #
# 12-14: status, date and conflict semantics
# --------------------------------------------------------------------------- #
def test_status_and_scheduled_start_are_independent_facts() -> None:
    n = norm(status="Final")
    assert n.mapped_status == "final"
    assert n.status_raw == "Final"
    assert n.status_unknown is False
    assert n.scheduled_start is not None


def test_provider_date_remains_the_game_local_date() -> None:
    """The UTC calendar date must not replace the provider's local date."""

    n = norm()
    assert n.date_local == "2026-01-05"
    assert n.scheduled_start is not None and n.scheduled_start.startswith("2026-01-06")
    assert n.date_conflict is False


def test_ordinary_utc_rollover_is_not_a_conflict() -> None:
    """A one-day gap is normal for an evening tipoff and is never flagged."""

    for dt, local in (("2026-01-06T00:00:00Z", "2026-01-05"),
                      ("2026-01-06T03:30:00Z", "2026-01-05"),
                      ("2026-01-05T18:00:00Z", "2026-01-05"),
                      ("2026-01-05T00:30:00Z", "2026-01-05")):
        assert norm(datetime_value=dt, date=local).date_conflict is False, dt


def test_materially_inconsistent_date_and_datetime_is_reported() -> None:
    """Two or more days apart is a provider contradiction, not a rollover."""

    n = norm(datetime_value="2026-01-09T00:00:00Z", date="2026-01-05")
    assert n.date_conflict is True
    # Both provider values are preserved; neither is guessed at or corrected.
    assert n.date_local == "2026-01-05"
    assert n.scheduled_start == "2026-01-09T00:00:00.000000Z"


def test_conflict_check_is_silent_when_either_value_is_missing() -> None:
    assert norm(include_datetime=False, status="Final").date_conflict is False
    assert norm(date=None).date_conflict is False


# --------------------------------------------------------------------------- #
# 18-20: determinism, sanitization, no network
# --------------------------------------------------------------------------- #
def test_normalization_is_order_and_key_order_independent() -> None:
    """A different JSON key order must not change any normalized field."""

    payload = game()
    shuffled = {k: payload[k] for k in reversed(list(payload))}
    a, b = _normalize_game(payload)[0], _normalize_game(shuffled)[0]
    assert a is not None and b is not None
    assert vars(a) == vars(b)


def test_normalization_is_pure_and_repeatable() -> None:
    payload = game()
    first = _normalize_game(payload)[0]
    second = _normalize_game(payload)[0]
    assert first is not None and second is not None
    assert first.scheduled_start == second.scheduled_start == PILOT_CANONICAL


def test_the_reported_issue_carries_no_secret_or_raw_payload() -> None:
    """Only the provider game id and a generic reason may appear."""

    for bad in ("not-a-timestamp", "2026-01-06T00:00:00",
                "Bearer sk-live-abcdefghijklmnop"):
        n = norm(datetime_value=bad)
        message = n.scheduled_start_issue or ""
        low = message.lower()
        for marker in ("api_key", "apikey", "authorization", "bearer", "x-api-key",
                       "secret", "token", "{", "}", "http"):
            assert marker not in low, (bad, message)
        # The raw value itself is never echoed.
        assert str(bad) not in message


def test_no_socket_is_used_by_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def blocked(*_a: object, **_kw: object) -> None:
        raise AssertionError("normalization attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    assert norm().scheduled_start == PILOT_CANONICAL


# --------------------------------------------------------------------------- #
# 15-17: persistence, append-only history, idempotency (temporary schema v17)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "corpus.db"
    result = initialize_database(db_path)
    assert result.schema_version == 17
    with Database(db_path).connection() as connection:
        yield connection


def _persist_schedule(
    conn: sqlite3.Connection, n: Any, observed_at: str, marker: str
) -> ObservationOutcome:
    """Append one schedule observation exactly as the ingestor would."""

    runs = SqliteIngestionRunRepository(conn)
    with transaction(conn):
        run = runs.start(command="seed", provider=NBA, operation="seed", args_json="{}",
                         started_monotonic_ns=0, tool_version="t")
        content_hash = response_content_hash(
            provider=NBA, endpoint="/v1/games", request_params={}, body=marker)
        raw = SqliteRawResponseRepository(conn).store(
            run_id=run.run_id, provider=NBA, endpoint="/v1/games",
            request_params_json="{}", http_status=200, response_headers_json="{}",
            requested_at=observed_at, received_at=observed_at, elapsed_ns=1,
            body="{}", content_hash=content_hash)
        ref, _ = SqliteProviderReferenceRepository(conn).upsert(
            kind="game", provider=NBA, provider_entity_id=n.game_id,
            raw_response_id=raw.raw_response_id, raw_response_hash=content_hash,
            observed_at=observed_at)
        _sid, outcome = SqliteScheduleRepository(conn).append(
            game_ref_id=ref.reference_id, provider=NBA, provider_game_id=n.game_id,
            observed_at=observed_at, ingested_at=observed_at, run_id=run.run_id,
            raw_response_id=raw.raw_response_id, raw_response_hash=content_hash,
            mapped_status=n.mapped_status, season=n.season,
            game_date_local=n.date_local, scheduled_start=n.scheduled_start,
            home_provider_team_id=n.home_provider_team_id,
            away_provider_team_id=n.away_provider_team_id,
            status_code=n.status_raw, detailed_status=n.status_raw)
    return outcome


def _starts(conn: sqlite3.Connection) -> list[tuple[Optional[str], str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT scheduled_start, mapped_status FROM game_schedule_snapshots "
        "ORDER BY observed_at, schedule_id")]


def test_persisted_final_game_carries_a_canonical_scheduled_start(
    conn: sqlite3.Connection,
) -> None:
    outcome = _persist_schedule(conn, norm(), T0, "one")
    assert outcome is ObservationOutcome.INSERTED
    row = conn.execute(
        "SELECT scheduled_start, mapped_status, game_date_local "
        "FROM game_schedule_snapshots").fetchone()
    assert row["scheduled_start"] == PILOT_CANONICAL
    assert row["mapped_status"] == "final"
    assert row["game_date_local"] == "2026-01-05"


def test_identical_replay_is_idempotent(conn: sqlite3.Connection) -> None:
    _persist_schedule(conn, norm(), T0, "one")
    before = _starts(conn)
    second = _persist_schedule(conn, norm(), T0, "one")
    assert second is ObservationOutcome.UNCHANGED
    assert _starts(conn) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM game_schedule_snapshots").fetchone()[0] == 1


def test_a_changed_datetime_appends_and_preserves_history(
    conn: sqlite3.Connection,
) -> None:
    """A reschedule is a new observation; the earlier one is never rewritten."""

    _persist_schedule(conn, norm(), T0, "one")
    rescheduled = norm(datetime_value="2026-01-07T00:00:00Z", date="2026-01-06")
    assert _persist_schedule(conn, rescheduled, T1, "two") is ObservationOutcome.INSERTED
    history = _starts(conn)
    assert history == [(PILOT_CANONICAL, "final"),
                       ("2026-01-07T00:00:00.000000Z", "final")]
    # Provenance is attached to each observation.
    provenance = conn.execute(
        "SELECT COUNT(*) FROM game_schedule_snapshots s "
        "JOIN raw_responses r ON r.raw_response_id = s.raw_response_id").fetchone()[0]
    assert provenance == 2


def test_status_progression_never_loses_the_scheduled_start(
    conn: sqlite3.Connection,
) -> None:
    """scheduled -> in_progress -> final, with the datetime constant throughout.

    The pre-game status is the provider's ISO form (which is what maps to
    ``scheduled``); the point is that the SAME tipoff instant survives all three
    observations rather than appearing only while the game had not started.
    """

    observed = ["2026-01-06T00:00:00.000000Z", "2026-01-06T01:00:00.000000Z",
                "2026-01-06T04:00:00.000000Z"]
    statuses = ("2026-01-06T00:00:00.000Z", "3rd Qtr", "Final")
    for at, status in zip(observed, statuses, strict=True):
        _persist_schedule(conn, norm(status=status), at, status)
    history = _starts(conn)
    assert [h[1] for h in history] == ["scheduled", "in_progress", "final"]
    assert {h[0] for h in history} == {PILOT_CANONICAL}


def test_display_only_status_stays_unknown_and_still_keeps_the_start(
    conn: sqlite3.Connection,
) -> None:
    """Display text is not in the status vocabulary -- unchanged, pre-existing.

    What matters for this repair is that an unrecognised status does not cost the
    game its scheduled start.
    """

    n = norm(status="7:00 pm ET")
    assert n.mapped_status == "unknown" and n.status_unknown is True
    assert n.scheduled_start == PILOT_CANONICAL
    _persist_schedule(conn, n, T0, "display")
    assert _starts(conn) == [(PILOT_CANONICAL, "unknown")]


def test_an_invalid_later_datetime_does_not_overwrite_a_valid_observation(
    conn: sqlite3.Connection,
) -> None:
    _persist_schedule(conn, norm(), T0, "one")
    _persist_schedule(conn, norm(datetime_value="garbage"), T1, "two")
    history = _starts(conn)
    # The earlier valid start is still on record, unmodified.
    assert history[0][0] == PILOT_CANONICAL
    assert history[-1][0] is None
    # And latest-as-of remains deterministic.
    latest = conn.execute(
        "SELECT scheduled_start FROM game_schedule_snapshots "
        "ORDER BY observed_at DESC, schedule_id DESC LIMIT 1").fetchone()[0]
    assert latest is None
    as_of_earlier = conn.execute(
        "SELECT scheduled_start FROM game_schedule_snapshots WHERE observed_at <= ? "
        "ORDER BY observed_at DESC, schedule_id DESC LIMIT 1", (T0,)).fetchone()[0]
    assert as_of_earlier == PILOT_CANONICAL


def test_schedule_history_is_append_only(conn: sqlite3.Connection) -> None:
    _persist_schedule(conn, norm(), T0, "one")
    schedule_id = conn.execute(
        "SELECT schedule_id FROM game_schedule_snapshots").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError):
        with transaction(conn):
            conn.execute("DELETE FROM game_schedule_snapshots WHERE schedule_id = ?",
                         (schedule_id,))
