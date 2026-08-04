"""Regressions for the defects the F1 MLB June-2026 month review confirmed.

Each test is the minimal reproducer that failed before its repair:

1. A ``partially_failed`` ingest unit must NOT be checkpointed as complete
   (the June run recorded game 824011 complete while both of its roster
   requests had failed terminally).
2. Duplicate schedule entries for one ``gamePk`` must collapse deterministically
   and CONTENT-AWARE -- never by provider payload order -- and every removal
   must be counted, so ``received = selected + excluded_by_max_games +
   deduplicated`` always closes. In the June payload gamePk 823613 arrived twice,
   once ``Postponed`` and once ``Final``, and the stale record won.
3. A result carrying a complete score while its schedule status is not final is
   a data-quality finding, not silence (June games 823613 and 823042).
4. A dated roster observation's transition anchor must include ``roster_date``:
   re-observing an earlier date after a later one is not a state change.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from sports_quant.ingest.mlb_ingestor import (
    _NormGame,
    _result_issues,
    _ResultParse,
    _select_games,
)


# --------------------------------------------------------------------------- #
# 1. partially_failed must not be checkpointed as complete
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, status: str) -> None:
        self.status = status
        self.error_type: Optional[str] = None
        self.error_message = ""
        self.games_received = 0
        self.games_truncated = 0
        self.games_deduplicated = 0
        self.ordered_game_ids: tuple[str, ...] = ()
        self.raw_responses_received = 3


def _executor(monkeypatch: pytest.MonkeyPatch, status: str) -> Any:
    from sports_quant.ingest import f1a

    async def _fake_ingest_mlb(**_kwargs: Any) -> Any:
        return _Result(status)

    monkeypatch.setattr("sports_quant.ingest.mlb_ingestor.ingest_mlb", _fake_ingest_mlb)

    class _Client:
        async def aclose(self) -> None:
            return None

    return f1a._IngestorExecutor(
        league="mlb", database=object(), client_factory=lambda _gate: _Client(),
        from_date="2026-06-01", to_date="2026-06-30", includes=("rosters",),
        stage="rich")


class _Gate:
    def __init__(self) -> None:
        class _U:
            budget_exhausted = None

        self.usage = _U()
        self.selections: list[tuple[int, int, int]] = []

    def record_selection(self, **kw: Any) -> None:
        self.selections.append(
            (kw["games_received"], kw["games_selected"], kw["excluded"]))


@pytest.mark.parametrize("status", ["failed", "partially_failed"])
def test_incomplete_unit_status_is_surfaced_not_checkpointed(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A unit that did not fully succeed must raise, leaving it resumable.

    Before the repair only ``failed`` raised, so a ``partially_failed`` unit --
    exactly what game 824011 produced when both of its roster requests failed --
    was yielded and therefore checkpointed as complete, permanently hiding the
    missing rosters behind a ``completed`` checkpoint.
    """

    from sports_quant.ingest.f1a import _UnitFailed

    ex = _executor(monkeypatch, status)
    with pytest.raises(_UnitFailed) as exc:
        ex._run_ingest(_Gate(), game_id="824011", includes=("rosters",))
    assert status in str(exc.value)


def test_fully_successful_unit_still_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _executor(monkeypatch, "succeeded")
    result = ex._run_ingest(_Gate(), game_id="824011", includes=("rosters",))
    assert result.status == "succeeded"


# --------------------------------------------------------------------------- #
# 2. deterministic, content-aware, fully-accounted deduplication
# --------------------------------------------------------------------------- #
def _entry(pk: int, *, date: str, status: str, code: str, game_date: str,
           dh: str = "N") -> dict[str, Any]:
    return {"gamePk": pk, "officialDate": date, "gameDate": game_date,
            "gameType": "R", "doubleHeader": dh,
            "status": {"detailedState": status, "statusCode": code}}


#: The June 2026 payload, reduced to the one gamePk that arrived twice.
_POSTPONED = _entry(823613, date="2026-06-24", status="Postponed", code="DR",
                    game_date="2026-06-22T23:10:00Z")
_FINAL = _entry(823613, date="2026-06-24", status="Final", code="F",
                game_date="2026-06-24T17:10:00Z", dh="S")


@pytest.mark.parametrize("payload_order", [[_POSTPONED, _FINAL], [_FINAL, _POSTPONED]])
def test_duplicate_game_pk_keeps_the_completed_record_in_any_payload_order(
    payload_order: list[dict[str, Any]],
) -> None:
    """Whichever order the provider sends, the completed record must win.

    Before the repair the survivor was simply the first entry in payload order,
    so the June corpus recorded a game that had actually finished 10-3 as
    ``postponed`` -- an outcome that flips if the provider reorders its payload.
    """

    selected, truncated, deduped = _select_games(list(payload_order), 600)
    assert [g["gamePk"] for g in selected] == [823613]
    assert selected[0]["status"]["detailedState"] == "Final"
    assert selected[0]["gameDate"] == "2026-06-24T17:10:00Z"
    assert truncated == 0
    assert deduped == 1


def test_deduplication_applies_without_a_max_games_bound() -> None:
    """An unbounded run must not ingest one gamePk twice."""

    selected, truncated, deduped = _select_games([_POSTPONED, _FINAL], None)
    assert [g["gamePk"] for g in selected] == [823613]
    assert (truncated, deduped) == (0, 1)


def test_selection_accounting_identity_closes() -> None:
    """received = selected + excluded_by_max_games + deduplicated."""

    games = [_POSTPONED, _FINAL,
             _entry(824000, date="2026-06-25", status="Final", code="F",
                    game_date="2026-06-25T18:00:00Z"),
             _entry(824001, date="2026-06-26", status="Final", code="F",
                    game_date="2026-06-26T18:00:00Z")]
    selected, truncated, deduped = _select_games(games, 2)
    assert len(games) == len(selected) + truncated + deduped


def test_an_entry_without_a_game_pk_reaches_the_normalizer_for_rejection() -> None:
    """Selection must not silently swallow a malformed entry.

    The normalizer already rejects a game with no ``gamePk`` and records the
    data-quality rejection, so dropping it during selection would replace one
    unreported removal with another.
    """

    malformed = {"officialDate": "2026-06-25", "status": {"detailedState": "Final"}}
    games = [_entry(824000, date="2026-06-25", status="Final", code="F",
                    game_date="2026-06-25T18:00:00Z"), malformed]
    selected, truncated, deduped = _select_games(games, 600)
    assert malformed in selected
    assert len(games) == len(selected) + truncated + deduped


def test_selection_order_is_canonical_not_payload_order() -> None:
    later = _entry(824001, date="2026-06-26", status="Final", code="F",
                   game_date="2026-06-26T18:00:00Z")
    earlier = _entry(824000, date="2026-06-25", status="Final", code="F",
                     game_date="2026-06-25T18:00:00Z")
    selected, _, _ = _select_games([later, earlier], 600)
    assert [g["gamePk"] for g in selected] == [824000, 824001]


def test_usage_report_carries_the_deduplicated_count() -> None:
    from sports_quant.ingest.cost_policies import build_mlb_policy
    from sports_quant.request_control import CreditBudget, RequestBudget, RequestGate

    gate = RequestGate(request_budget=RequestBudget(max_requests=10),
                       credit_budget=CreditBudget(applicable=False),
                       cost_policy=build_mlb_policy())
    gate.record_selection(games_received=402, games_selected=400, excluded=0,
                          deduplicated=2)
    u = gate.usage.as_dict()
    assert u["games_received"] == 402
    assert u["games_selected"] == 400
    assert u["games_excluded_by_max_games"] == 0
    assert u["games_deduplicated"] == 2
    assert (u["games_received"] == u["games_selected"]
            + u["games_excluded_by_max_games"] + u["games_deduplicated"])
    assert u["selection_truncated"] is False


# --------------------------------------------------------------------------- #
# 3. a complete score under a non-final status is a finding, not silence
# --------------------------------------------------------------------------- #
def _norm(status: str) -> _NormGame:
    return _NormGame(
        game_pk="823042", season=2026, game_type="R", game_date_local="2026-07-23",
        scheduled_start="2026-06-25T23:45:00Z", home_provider_team_id="147",
        away_provider_team_id="111", venue_provider_id="3313", status_code="D",
        detailed_status="Postponed", mapped_status=status, status_unknown=False,
        game_number=1, doubleheader_code="N", reschedule_info=None,
        home_probable_pitcher_id=None, away_probable_pitcher_id=None, raw_game={})


def _parse(home: Optional[int], away: Optional[int],
           innings: Optional[int]) -> _ResultParse:
    return _ResultParse(
        home_runs=home, away_runs=away, home_hits=None, away_hits=None,
        home_errors=None, away_errors=None, innings_played=innings,
        winning_side="away" if home is not None else None)


def test_complete_score_under_a_non_final_status_is_reported() -> None:
    """June games 823613 and 823042 carried 9-inning finals while labelled
    postponed, and the corpus recorded zero data-quality findings."""

    issues = _result_issues(_norm("postponed"), _parse(6, 10, 9), None)
    assert [i.rule_code for i in issues] == ["DQ-MLB-RESULT-003"]
    assert "postponed" in issues[0].description
    assert issues[0].severity == "issue"


def test_a_postponed_game_with_no_score_is_not_reported() -> None:
    assert _result_issues(_norm("postponed"), _parse(None, None, None), None) == []


def test_a_final_game_with_a_score_is_not_reported() -> None:
    assert _result_issues(_norm("final"), _parse(6, 10, 9), None) == []


# --------------------------------------------------------------------------- #
# 4. the roster transition anchor must include roster_date
# --------------------------------------------------------------------------- #
def _roster_db(path: Any) -> Any:
    """A migrated temp corpus with the one team reference and raw response the
    roster repository's foreign keys require."""

    from sports_quant.db.engine import Database
    from sports_quant.db.init import initialize_database

    initialize_database(path)
    db = Database(path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, command, provider, operation, "
            "args_json, status, requested_at, started_at, completed_at, "
            "started_monotonic_ns, duration_ns, tool_version, created_at) VALUES "
            "('run_x', 'ingest-mlb', 'mlb_statsapi', 'ingest_mlb', '{}', "
            "'succeeded', '2026-06-24T00:00:00Z', '2026-06-24T00:00:00Z', "
            "'2026-06-24T00:00:01Z', 1, 1000, 'test', '2026-06-24T00:00:00Z')")
        conn.execute(
            "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
            "request_params_json, http_method, http_status, response_headers_json, "
            "content_type, requested_at, received_at, elapsed_ns, body, body_bytes, "
            "body_hash, content_hash, created_at) VALUES ('raw_x', 'run_x', "
            "'mlb_statsapi', '/teams/112/roster', '{}', 'GET', 200, '{}', "
            "'application/json', '2026-06-24T00:00:00Z', '2026-06-24T00:00:00Z', "
            "1000, '{}', 2, 'bh', 'ch', '2026-06-24T00:00:00Z')")
        conn.execute(
            "INSERT INTO provider_team_references (reference_id, provider, "
            "provider_team_id, first_raw_response_id, current_raw_response_id, "
            "current_raw_response_hash, first_observed_at, last_observed_at, "
            "created_at, updated_at) VALUES ('ptr_x', 'mlb_statsapi', '112', "
            "'raw_x', 'raw_x', 'h', '2026-06-24T00:00:00Z', '2026-06-24T00:00:00Z', "
            "'2026-06-24T00:00:00Z', '2026-06-24T00:00:00Z')")
    return db



def test_re_observing_an_earlier_roster_date_is_not_a_transition(
    tmp_path: Any,
) -> None:
    """The doubleheader case: two units fetch the same (team, date) roster.

    Before the repair the transition anchor was (team, player) only while
    ``roster_date`` was inside the content hash, so ingesting 06-25 in between
    made a re-observation of 06-24 look like a state change and appended a
    duplicate row -- a row count that varied with ingestion order.
    """

    from sports_quant.db.repositories.observations import ObservationOutcome
    from sports_quant.db.repositories.rosters import SqliteRosterRepository

    db = _roster_db(tmp_path / "roster.db")

    def observe(date: str, observed_at: str) -> ObservationOutcome:
        with db.transaction() as conn:
            repo = SqliteRosterRepository(conn)
            _id, outcome = repo.append(
                team_ref_id="ptr_x", provider="mlb_statsapi", provider_team_id="112",
                provider_player_id="571948", observed_at=observed_at,
                ingested_at=observed_at, run_id=None, raw_response_id="raw_x",
                raw_response_hash="h", roster_date=date, roster_status="Active",
                jersey_number="41", position="P")
        return outcome

    assert observe("2026-06-24", "2026-08-01T00:00:01Z") is ObservationOutcome.INSERTED
    assert observe("2026-06-25", "2026-08-01T00:00:02Z") is ObservationOutcome.INSERTED
    # The doubleheader's second unit re-observes 06-24 after 06-25 was ingested.
    assert observe("2026-06-24", "2026-08-01T00:00:03Z") is ObservationOutcome.UNCHANGED

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT roster_date, COUNT(*) n FROM roster_snapshots GROUP BY 1 "
            "ORDER BY 1").fetchall()
    assert [(r["roster_date"], r["n"]) for r in rows] == [
        ("2026-06-24", 1), ("2026-06-25", 1)]


def test_a_real_same_date_roster_change_is_still_recorded(tmp_path: Any) -> None:
    """Narrowing the anchor must not hide a genuine same-date state change."""

    from sports_quant.db.repositories.observations import ObservationOutcome
    from sports_quant.db.repositories.rosters import SqliteRosterRepository

    db = _roster_db(tmp_path / "roster2.db")

    def observe(status: str, observed_at: str) -> ObservationOutcome:
        with db.transaction() as conn:
            repo = SqliteRosterRepository(conn)
            _id, outcome = repo.append(
                team_ref_id="ptr_x", provider="mlb_statsapi", provider_team_id="112",
                provider_player_id="571948", observed_at=observed_at,
                ingested_at=observed_at, run_id=None, raw_response_id="raw_x",
                raw_response_hash="h", roster_date="2026-06-24",
                roster_status=status, jersey_number="41", position="P")
        return outcome

    assert observe("Active", "2026-08-01T00:00:01Z") is ObservationOutcome.INSERTED
    assert observe("Injured", "2026-08-01T00:00:02Z") is ObservationOutcome.INSERTED
    assert observe("Injured", "2026-08-01T00:00:03Z") is ObservationOutcome.UNCHANGED
    with db.transaction() as conn:
        n = conn.execute("SELECT COUNT(*) FROM roster_snapshots").fetchone()[0]
    assert n == 2


