"""Independent adversarial review of the Stage-A projection / body verifier.

Every test in a DEFECT section fails against `2805665` and passes after the
repairs. Fixtures build raw JSON **text** rather than going through
`json.dumps` on a dict, because a dict fixture structurally cannot express the
states that mattered most here: a duplicate object key, and an absent versus
null member.

No provider is contacted, no credit is spent, and nothing under ``data/`` is
opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.schema import THE_ODDS_API_PROVIDER, utc_now_iso
from sports_quant.retrospective.historical_events_projection import (
    STAGE_A_ALLOWED_REQUEST_PARAMS,
    EvidenceGateResult,
    ProjectionRejected,
    RejectionCode,
    project_historical_events_response,
    verify_historical_market_event_evidence,
    verify_selected_responses_subset,
)
from sports_quant.retrospective.market_observations import (
    MarketEventObservation,
    observation_content_hash,
    observation_id,
)

ENDPOINT = "/v4/historical/sports/basketball_nba/events"
BUCKET_RAW = "2026-03-01T17:00:00Z"
BUCKET = "2026-03-01T17:00:00.000000Z"
SNAP_RAW = "2026-03-01T16:55:37Z"
SNAP = "2026-03-01T16:55:37.000000Z"
EV_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EV_B = "111a955795876d50988b15c219ce0796"

_OBS_COLS = ("observation_id", "league_id", "provider", "namespace_generation",
             "sport_key", "provider_event_id", "requested_at_bucket",
             "provider_snapshot_timestamp", "commence_time", "home_team_raw",
             "away_team_raw", "observation_content_hash", "raw_response_id",
             "observed_at", "created_at")

EVENT_A_TEXT = (
    '{"id": "%s", "sport_key": "basketball_nba", "sport_title": "NBA", '
    '"commence_time": "2026-03-01T18:10:00Z", "home_team": "Boston Celtics", '
    '"away_team": "Miami Heat"}' % EV_A)
EVENT_B_TEXT = (
    '{"id": "%s", "sport_key": "basketball_nba", "sport_title": "NBA", '
    '"commence_time": "2026-03-01T20:40:00Z", "home_team": "Chicago Bulls", '
    '"away_team": "Detroit Pistons"}' % EV_B)


def wrapper(data_text: Optional[str] = f"[{EVENT_A_TEXT}]",
            timestamp_text: str = f'"{SNAP_RAW}"') -> str:
    """Build the body as raw TEXT so absent/null/duplicate states are expressible."""

    parts = [f'"timestamp": {timestamp_text}']
    if data_text is not None:
        parts.append(f'"data": {data_text}')
    return "{" + ", ".join(parts) + "}"


def params_text(**over: Any) -> str:
    base = {"apiKey": "***REDACTED***", "date": BUCKET_RAW, "dateFormat": "iso"}
    base.update(over)
    return json.dumps(base)


def row(body: str = wrapper(), params: Optional[str] = None,
        **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "raw_response_id": "raw_1", "provider": THE_ODDS_API_PROVIDER,
        "endpoint": ENDPOINT, "http_status": 200,
        "request_params_json": params if params is not None else params_text(),
        "body": body}
    base.update(over)
    return base


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "rev.db"
    Database(path).migrate()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    conn.execute("INSERT INTO leagues (league_id, code, name, sport, created_at, "
                 "updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)",
                 (now, now))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, "
        "args_json, status, requested_at, started_at, started_monotonic_ns, "
        "requests_made, records_received, records_normalized, records_inserted, "
        "records_deduplicated, records_rejected, records_updated, tool_version, "
        "created_at) VALUES ('run_1','x',?, 'op','{}','started',?,?,1,"
        "0,0,0,0,0,0,0,'v',?)", (THE_ODDS_API_PROVIDER, now, now, now))
    conn.commit()
    return conn


def put_raw(conn: sqlite3.Connection, rid: str, *, body: str = wrapper(),
            params: Optional[str] = None, endpoint: str = ENDPOINT,
            status: int = 200, provider: str = THE_ODDS_API_PROVIDER) -> str:
    now = utc_now_iso()
    digest = hashlib.sha256(body.encode()).hexdigest()
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, body, "
        "body_bytes, body_hash, content_hash, requested_at, received_at, "
        "elapsed_ns, created_at) VALUES (?, 'run_1', ?, ?, ?, ?, '{}', ?, ?, ?, "
        "?, ?, ?, 1, ?)",
        (rid, provider, endpoint, params or params_text(), status, body,
         len(body), digest, digest, now, now, now))
    conn.commit()
    return rid



def _received_at(conn: sqlite3.Connection, raw_response_id: str) -> str:
    row = conn.execute(
        "SELECT received_at FROM raw_responses WHERE raw_response_id = ?",
        (raw_response_id,)).fetchone()
    return str(row[0]) if row is not None else utc_now_iso()


def store(conn: sqlite3.Connection, obs: MarketEventObservation, *,
          rid: str = "raw_1", **override: Any) -> str:
    now = utc_now_iso()
    values: dict[str, Any] = {
        "observation_id": observation_id(obs), "league_id": obs.league_id,
        "provider": obs.provider, "namespace_generation": obs.namespace_generation,
        "sport_key": obs.sport_key, "provider_event_id": obs.provider_event_id,
        "requested_at_bucket": obs.requested_at_bucket,
        "provider_snapshot_timestamp": obs.provider_snapshot_timestamp,
        "commence_time": obs.commence_time, "home_team_raw": obs.home_team_raw,
        "away_team_raw": obs.away_team_raw,
        "observation_content_hash": observation_content_hash(obs),
        # Since f022, `observed_at` MUST equal the cited response's `received_at`
        # (it records when WE possessed the evidence). Deriving it here keeps
        # every test below aimed at what it actually claims to test.
        "raw_response_id": rid, "observed_at": _received_at(conn, rid),
        "created_at": now}
    values.update(override)
    conn.execute(
        f"INSERT INTO historical_market_event_observations "  # noqa: S608
        f"({', '.join(_OBS_COLS)}) VALUES ({', '.join('?' * len(_OBS_COLS))})",
        tuple(values[c] for c in _OBS_COLS))
    conn.commit()
    return str(values["observation_id"])


def obs_a() -> MarketEventObservation:
    return MarketEventObservation(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        provider_event_id=EV_A, requested_at_bucket=BUCKET,
        provider_snapshot_timestamp=SNAP,
        commence_time="2026-03-01T18:10:00.000000Z",
        home_team_raw="Boston Celtics", away_team_raw="Miami Heat")


# --------------------------------------------------------------------------- #
# DEFECT 1 -- absent / null `data` was admitted as zero-event evidence
# --------------------------------------------------------------------------- #
def test_an_absent_data_member_is_not_evidence_of_zero_events() -> None:
    """Reproduced at 2805665: ACCEPTED with 0 observations.

    `data = body.get("data"); if data is None: data = []` collapsed a
    payload-shape deviation into the positive fact "no events existed at that
    snapshot". Malformed provider output must never become a market fact.
    """

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(row(body=wrapper(data_text=None)))
    assert caught.value.code is RejectionCode.DATA_MISSING


def test_an_explicit_null_data_is_not_evidence_of_zero_events() -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(row(body=wrapper(data_text="null")))
    assert caught.value.code is RejectionCode.DATA_MISSING


def test_only_an_empty_list_evidences_zero_events() -> None:
    proj = project_historical_events_response(row(body=wrapper(data_text="[]")))
    assert proj.observations == ()


# --------------------------------------------------------------------------- #
# DEFECT 2 -- a FILTERED request produced self-consistent partial evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("param,value", [
    ("eventIds", EV_A), ("eventIds", ""), ("eventIds", [EV_A]),
    ("commenceTimeFrom", "2026-03-01T18:00:00Z"),
    ("commenceTimeTo", "2026-03-01T19:00:00Z"),
    ("regions", "us"), ("markets", "h2h"), ("bookmakers", "draftkings"),
    ("somethingNew", "x"), ("sport", "basketball_nba"),
])
def test_a_population_reducing_request_is_refused(param: str, value: Any) -> None:
    """Reproduced at 2805665: every one of these projected successfully.

    This was the most serious defect. `eventIds=<one convenient event>` yields a
    perfectly self-consistent response whose complete projection is that single
    event, so two-way completeness PASSES while the real snapshot population was
    reduced by the request. The completeness guarantee was evadable from outside
    the body entirely.
    """

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            row(params=params_text(**{param: value})))
    assert caught.value.code is RejectionCode.FILTERED_REQUEST


def test_the_allowed_request_parameters_are_exactly_three() -> None:
    assert STAGE_A_ALLOWED_REQUEST_PARAMS == {"apiKey", "date", "dateFormat"}


def test_an_unfiltered_request_still_projects() -> None:
    assert len(project_historical_events_response(row()).observations) == 1


def test_a_filtered_request_cannot_be_laundered_by_two_way_completeness(
    db: sqlite3.Connection,
) -> None:
    """The end-to-end shape of the attack: consistent, complete, and wrong."""

    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"),
            params=params_text(eventIds=EV_A))
    store(db, obs_a())
    result = verify_historical_market_event_evidence(db)
    assert not result.verified
    assert result.reports[0].rejection_code is RejectionCode.FILTERED_REQUEST


# --------------------------------------------------------------------------- #
# DEFECT 3 -- duplicate JSON keys were silently resolved last-value-wins
# --------------------------------------------------------------------------- #
def test_duplicate_keys_in_the_request_are_refused() -> None:
    """Reproduced at 2805665: `dateFormat` twice (unix, then iso) was ACCEPTED."""

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(row(
            params='{"date": "2026-03-01T17:00:00Z", "dateFormat": "unix", '
                   '"dateFormat": "iso"}'))
    assert caught.value.code is RejectionCode.BAD_REQUEST_PARAMS


def test_a_duplicate_date_is_refused_for_the_right_reason() -> None:
    """At 2805665 this was refused only ACCIDENTALLY.

    The parser silently took the later `date`, and the snapshot-ordering check
    happened to catch the result. A guard that fires by coincidence is not a
    guard: with a different pair of dates it would have passed.
    """

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(row(
            params='{"date": "2026-03-01T17:00:00Z", '
                   '"date": "2026-03-01T18:00:00Z", "dateFormat": "iso"}'))
    assert caught.value.code is RejectionCode.BAD_REQUEST_PARAMS


@pytest.mark.parametrize("body_text", [
    '{"timestamp": "2026-03-01T10:00:00Z", "timestamp": "%s", "data": []}' % SNAP_RAW,
    '{"timestamp": "%s", "data": [{"id": "%s", "id": "%s", "sport_key": '
    '"basketball_nba", "commence_time": "2026-03-01T18:10:00Z", "home_team": '
    '"A", "away_team": "B"}]}' % (SNAP_RAW, EV_B, EV_A),
    '{"timestamp": "%s", "data": [{"id": "%s", "sport_key": "basketball_nba", '
    '"commence_time": "2026-03-01T18:10:00Z", "home_team": "Real", "home_team": '
    '"Fake", "away_team": "B"}]}' % (SNAP_RAW, EV_A),
    '{"timestamp": "%s", "data": [], "data": [%s]}' % (SNAP_RAW, EVENT_A_TEXT),
])
def test_duplicate_keys_in_the_body_are_refused(body_text: str) -> None:
    """One document must not have two readings."""

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(row(body=body_text))
    assert caught.value.code is RejectionCode.BODY_NOT_JSON


# --------------------------------------------------------------------------- #
# Subset verification must not be mistakeable for corpus proof
# --------------------------------------------------------------------------- #
def test_the_corpus_gate_takes_no_caller_selected_ids() -> None:
    """At 2805665 the composite accepted `raw_response_ids=[the easy ones]`."""

    import inspect

    assert "raw_response_ids" not in inspect.signature(
        verify_historical_market_event_evidence).parameters


def test_subset_verification_is_named_as_a_subset(db: sqlite3.Connection) -> None:
    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))
    put_raw(db, "raw_2", body=wrapper(f"[{EVENT_B_TEXT}]"))   # never materialized
    store(db, obs_a())

    subset = verify_selected_responses_subset(db, ["raw_1"])
    assert all(r.verified for r in subset)          # the easy one passes

    whole = verify_historical_market_event_evidence(db)
    assert not whole.verified                        # the corpus does not
    assert "subset" in verify_selected_responses_subset.__name__


# --------------------------------------------------------------------------- #
# Orphaned observations: rows no per-response report would ever examine
# --------------------------------------------------------------------------- #
def test_an_observation_citing_a_non_historical_response_is_caught(
    db: sqlite3.Connection,
) -> None:
    """Reproduced at 2805665: no report covered it, so nothing examined it."""

    put_raw(db, "raw_odds", endpoint="/v4/sports/basketball_nba/odds")
    store(db, obs_a(), rid="raw_odds")
    result = verify_historical_market_event_evidence(db)
    assert not result.verified
    assert result.orphaned_observation_ids == (observation_id(obs_a()),)


def test_a_clean_database_verifies(db: sqlite3.Connection) -> None:
    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))
    store(db, obs_a())
    result = verify_historical_market_event_evidence(db)
    assert isinstance(result, EvidenceGateResult)
    assert result.verified
    assert result.orphaned_observation_ids == ()


# --------------------------------------------------------------------------- #
# Multi-response completeness (K)
# --------------------------------------------------------------------------- #
def test_evidence_cannot_be_split_across_two_responses(
    db: sqlite3.Connection,
) -> None:
    """Both responses carry both events; materializing one each satisfies
    neither, because each response's own body demands both."""

    both = wrapper(f"[{EVENT_A_TEXT}, {EVENT_B_TEXT}]")
    put_raw(db, "raw_1", body=both)
    put_raw(db, "raw_2", body=both)
    store(db, obs_a(), rid="raw_1")
    result = verify_historical_market_event_evidence(db)
    assert not result.verified
    assert all(r.missing for r in result.reports)


def test_an_unmaterialized_second_response_cannot_hide(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))
    put_raw(db, "raw_2", body=wrapper(f"[{EVENT_B_TEXT}]"))
    store(db, obs_a(), rid="raw_1")
    result = verify_historical_market_event_evidence(db)
    assert not result.verified
    assert {r.raw_response_id for r in result.reports} == {"raw_1", "raw_2"}


def test_a_malformed_second_response_fails_the_gate(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))
    put_raw(db, "raw_2", body=wrapper(data_text=None))    # no data member
    store(db, obs_a(), rid="raw_1")
    assert not verify_historical_market_event_evidence(db).verified


# --------------------------------------------------------------------------- #
# Team-label semantics: string, not JSON token bytes (J)
# --------------------------------------------------------------------------- #
def test_escaped_and_plain_json_decode_to_the_same_observation() -> None:
    """The typed layer preserves the decoded STRING, not the JSON token bytes.

    `"\\u0042oston Celtics"` and `"Boston Celtics"` are byte-distinct JSON that
    decode identically, so they project to the same observation and the same id.
    That is correct -- the provider said the same thing twice over -- but it
    means "byte-for-byte team label" overstates the typed layer's guarantee. The
    original bytes remain in `raw_responses.body`, which is where a byte-level
    claim belongs.
    """

    escaped = ('{"timestamp": "%s", "data": [{"id": "%s", "sport_key": '
               '"basketball_nba", "commence_time": "2026-03-01T18:10:00Z", '
               '"home_team": "\\u0042oston Celtics", "away_team": "Miami Heat"}]}'
               % (SNAP_RAW, EV_A))
    a = project_historical_events_response(row(body=escaped))
    b = project_historical_events_response(row())
    assert a.observations[0].home_team_raw == "Boston Celtics"
    assert a.observation_ids == b.observation_ids


def test_distinct_unicode_forms_stay_distinct() -> None:
    """NFC vs NFD are different strings and must not be normalized together."""

    import unicodedata

    nfc = unicodedata.normalize("NFC", "Montréal")
    nfd = unicodedata.normalize("NFD", "Montréal")
    bodies = [
        '{"timestamp": "%s", "data": [{"id": "%s", "sport_key": "basketball_nba",'
        ' "commence_time": "2026-03-01T18:10:00Z", "home_team": %s, '
        '"away_team": "Miami Heat"}]}' % (SNAP_RAW, EV_A, json.dumps(label))
        for label in (nfc, nfd)]
    a, b = (project_historical_events_response(row(body=x)) for x in bodies)
    assert a.observation_ids != b.observation_ids


# --------------------------------------------------------------------------- #
# Timestamp precision policy (I / R)
# --------------------------------------------------------------------------- #
def test_more_than_six_fractional_digits_is_refused_not_truncated() -> None:
    """Widening to nanoseconds must be a NEW policy version, not silent loss."""

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            row(body=wrapper(data_text="[]",
                             timestamp_text='"2026-03-01T16:55:37.1234567Z"')))
    assert caught.value.code is RejectionCode.BAD_SNAPSHOT_TIMESTAMP


@pytest.mark.parametrize("ts", ["2026-03-01T23:59:60Z", " 2026-03-01T16:55:37Z",
                                "2026-03-01T16:55:37Z "])
def test_leap_seconds_and_padded_timestamps_are_refused(ts: str) -> None:
    with pytest.raises(ProjectionRejected):
        project_historical_events_response(
            row(body=wrapper(data_text="[]", timestamp_text=json.dumps(ts))))


# --------------------------------------------------------------------------- #
# Additive provider drift stays valid under v1 (R)
# --------------------------------------------------------------------------- #
def test_an_unknown_additive_event_field_does_not_break_v1() -> None:
    """A harmless new field must not be fatal; it changes no represented value."""

    text = ('{"id": "%s", "sport_key": "basketball_nba", "sport_title": "NBA", '
            '"commence_time": "2026-03-01T18:10:00Z", "home_team": "Boston '
            'Celtics", "away_team": "Miami Heat", "brandNewField": {"a": 1}}'
            % EV_A)
    proj = project_historical_events_response(row(body=wrapper(f"[{text}]")))
    assert proj.observation_ids == (observation_id(obs_a()),)


def test_a_removed_sport_title_does_not_break_v1() -> None:
    text = ('{"id": "%s", "sport_key": "basketball_nba", "commence_time": '
            '"2026-03-01T18:10:00Z", "home_team": "Boston Celtics", '
            '"away_team": "Miami Heat"}' % EV_A)
    proj = project_historical_events_response(row(body=wrapper(f"[{text}]")))
    assert proj.observation_ids == (observation_id(obs_a()),)


# --------------------------------------------------------------------------- #
# observed_at ownership (P)
# --------------------------------------------------------------------------- #
def test_observed_at_is_now_owned_by_the_database_not_by_this_verifier(
    db: sqlite3.Connection,
) -> None:
    """The handed-forward requirement, now DISCHARGED by f022.

    This layer still does not constrain `observed_at`: it is not derivable from
    the response body, and it remains outside the semantic content hash (the v20
    decision, unchanged). The review handed the clock forward to whichever layer
    owns acquisition provenance -- and f022 is that layer.

    So the boundary is unchanged but the hole is closed: an arbitrary value no
    longer passes, because the DATABASE now requires `observed_at` to equal the
    cited response's `received_at`. Both halves are asserted here.
    """

    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))

    # 1. The database owns the clock: an arbitrary value is refused outright.
    with pytest.raises(sqlite3.IntegrityError,
                       match="must equal the cited raw response"):
        store(db, obs_a(), observed_at="2030-01-01T00:00:00.000000Z")

    # 2. This verifier's remit is unchanged -- a correctly-clocked row projects
    #    and verifies exactly as before, and the verifier examines the BODY.
    store(db, obs_a())
    assert verify_historical_market_event_evidence(db).verified


# --------------------------------------------------------------------------- #
# Non-regression
# --------------------------------------------------------------------------- #
def test_no_authority_was_granted() -> None:
    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective import sources
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()
    assert THE_ODDS_API_PROVIDER not in sources.PROVIDER_LEAGUES
    assert THE_ODDS_API_PROVIDER not in ATTESTED_GENERATIONS
    assert THE_ODDS_API_PROVIDER not in OFFICIAL_PROVIDER_BY_LEAGUE.values()


def test_verification_creates_nothing(db: sqlite3.Connection) -> None:
    put_raw(db, "raw_1", body=wrapper(f"[{EVENT_A_TEXT}]"))
    store(db, obs_a())
    verify_historical_market_event_evidence(db)
    for table in ("identity_audit_records", "static_crosswalk_provenance",
                  "games", "reconstruction_corpus_versions"):
        assert db.execute(
            f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0  # noqa: S608
