"""Stage-A projection / body verifier: adversarial tests for review finding L1.

Expected values are constructed independently from the spec wherever possible,
not by calling the production projector to produce both sides.

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
    PROJECTION_POLICY_VERSION,
    EvidenceVerdict,
    ProjectionRejected,
    RejectionCode,
    project_historical_events_response,
    verify_historical_event_projections,
    verify_historical_market_event_evidence,
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
PREV_RAW = "2026-03-01T16:50:37Z"
NEXT_RAW = "2026-03-01T17:00:38Z"
EV_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EV_B = "111a955795876d50988b15c219ce0796"

_OBS_COLS = ("observation_id", "league_id", "provider", "namespace_generation",
             "sport_key", "provider_event_id", "requested_at_bucket",
             "provider_snapshot_timestamp", "commence_time", "home_team_raw",
             "away_team_raw", "observation_content_hash", "raw_response_id",
             "observed_at", "created_at")


def event(eid: str = EV_A, *, home: str = "Boston Celtics",
          away: str = "Miami Heat",
          commence: Optional[str] = "2026-03-01T18:10:00Z",
          sport: str = "basketball_nba", drop_commence: bool = False) -> dict:
    e: dict[str, Any] = {"id": eid, "sport_key": sport, "sport_title": "NBA",
                         "home_team": home, "away_team": away}
    if not drop_commence:
        e["commence_time"] = commence
    return e


def body(events: Optional[list] = None, **over: Any) -> str:  # noqa: ANN401
    payload: dict[str, Any] = {
        "timestamp": SNAP_RAW, "previous_timestamp": PREV_RAW,
        "next_timestamp": NEXT_RAW,
        "data": [event()] if events is None else events}
    payload.update(over)
    for k in [k for k, v in payload.items() if v is ...]:
        del payload[k]
    return json.dumps(payload)


class Row(dict):
    """Minimal stand-in for a sqlite3.Row, so projection can be unit-tested."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


def raw_row(**over: Any) -> Row:
    base = dict(raw_response_id="raw_1", provider=THE_ODDS_API_PROVIDER,
                endpoint=ENDPOINT, http_status=200,
                request_params_json=json.dumps(
                    {"apiKey": "***REDACTED***", "date": BUCKET_RAW,
                     "dateFormat": "iso"}),
                body=body())
    base.update(over)
    return Row(base)


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "stagea.db"
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


def put_raw(conn: sqlite3.Connection, rid: str = "raw_1", **over: Any) -> str:
    r = raw_row(raw_response_id=rid, **over)
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, body, "
        "body_bytes, body_hash, content_hash, requested_at, received_at, "
        "elapsed_ns, created_at) VALUES (?, 'run_1', ?, ?, ?, ?, '{}', ?, ?, ?, "
        "?, ?, ?, 1, ?)",
        (rid, r["provider"], r["endpoint"], r["request_params_json"],
         r["http_status"], r["body"], len(r["body"]),
         hashlib.sha256(r["body"].encode()).hexdigest(),
         hashlib.sha256(r["body"].encode()).hexdigest(), now, now, now))
    conn.commit()
    return rid


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
        "raw_response_id": rid, "observed_at": now, "created_at": now}
    values.update(override)
    conn.execute(
        f"INSERT INTO historical_market_event_observations "  # noqa: S608
        f"({', '.join(_OBS_COLS)}) VALUES ({', '.join('?' * len(_OBS_COLS))})",
        tuple(values[c] for c in _OBS_COLS))
    conn.commit()
    return str(values["observation_id"])


def expected_observation(eid: str = EV_A, *, home: str = "Boston Celtics",
                         away: str = "Miami Heat",
                         commence: Optional[str] = "2026-03-01T18:10:00.000000Z",
                         ) -> MarketEventObservation:
    """Built from the spec, independently of the projector."""

    return MarketEventObservation(
        league_id="lg_nba", provider=THE_ODDS_API_PROVIDER,
        namespace_generation="v4", sport_key="basketball_nba",
        provider_event_id=eid, requested_at_bucket=BUCKET,
        provider_snapshot_timestamp=SNAP, commence_time=commence,
        home_team_raw=home, away_team_raw=away)


# --------------------------------------------------------------------------- #
# 1-2. Happy path
# --------------------------------------------------------------------------- #
def test_a_valid_response_projects_to_the_independently_expected_set() -> None:
    proj = project_historical_events_response(
        raw_row(body=body([event(EV_A), event(EV_B, home="Chicago Bulls",
                                              away="Detroit Pistons")])))
    assert proj.policy_version == PROJECTION_POLICY_VERSION
    assert proj.requested_at_bucket == BUCKET
    assert proj.provider_snapshot_timestamp == SNAP
    assert proj.league_id == "lg_nba"
    assert proj.namespace_generation == "v4"
    assert set(proj.observation_ids) == {
        observation_id(expected_observation(EV_A)),
        observation_id(expected_observation(
            EV_B, home="Chicago Bulls", away="Detroit Pistons"))}


def test_a_complete_persisted_set_verifies(db: sqlite3.Connection) -> None:
    put_raw(db, body=body([event(EV_A), event(EV_B)]))
    for obs in project_historical_events_response(raw_row(
            body=body([event(EV_A), event(EV_B)]))).observations:
        store(db, obs)
    report = verify_historical_event_projections(db, "raw_1")
    assert report.verdict is EvidenceVerdict.VERIFIED
    assert (report.expected_count, report.stored_count) == (2, 2)
    assert report.failures == ()


# --------------------------------------------------------------------------- #
# 3-9. Raw-response admission
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint", [
    "/v4/sports/basketball_nba/odds",                       # current odds
    "/v4/historical/sports/basketball_nba/odds",            # historical PRICES
    "/v4/historical/sports/baseball_mlb/events",            # another sport
    "/v4/sports",
    "/v4/historical/sports/basketball_nba/events/",         # trailing slash
    "/v4/historical/sports/basketball_nba/events?date=x",   # query string
    "/V4/HISTORICAL/SPORTS/BASKETBALL_NBA/EVENTS",          # case variant
    "/v4/historical/sports/basketball_nba/events/extra",
    "v4/historical/sports/basketball_nba/events",           # no leading slash
])
def test_a_non_exact_endpoint_is_refused(endpoint: str) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(endpoint=endpoint))
    assert caught.value.code is RejectionCode.UNKNOWN_ENDPOINT


def test_a_different_provider_is_refused() -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(provider="balldontlie"))
    assert caught.value.code is RejectionCode.WRONG_PROVIDER


@pytest.mark.parametrize("status", [201, 204, 400, 401, 429, 500])
def test_a_non_200_response_is_refused(status: int) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(http_status=status))
    assert caught.value.code is RejectionCode.NOT_SUCCESSFUL


def test_malformed_json_is_refused() -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body="{not json"))
    assert caught.value.code is RejectionCode.BODY_NOT_JSON


@pytest.mark.parametrize("payload", ["[]", '"text"', "12", "null"])
def test_a_wrong_top_level_type_is_refused(payload: str) -> None:
    """A bare list is the CURRENT-odds shape and must never pass as historical."""

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=payload))
    assert caught.value.code is RejectionCode.BODY_NOT_OBJECT


@pytest.mark.parametrize("params,code", [
    ('{"date": "2026-03-01T17:00:00Z"}', RejectionCode.BAD_DATE_FORMAT),
    ('{"date": "2026-03-01T17:00:00Z", "dateFormat": "unix"}',
     RejectionCode.BAD_DATE_FORMAT),
    ('{"dateFormat": "iso"}', RejectionCode.BAD_REQUESTED_BUCKET),
    ('{"date": "not-a-time", "dateFormat": "iso"}',
     RejectionCode.BAD_REQUESTED_BUCKET),
    ('{"date": "2026-03-01T17:00:00+00:00", "dateFormat": "iso"}',
     RejectionCode.BAD_REQUESTED_BUCKET),
    ("[]", RejectionCode.BAD_REQUEST_PARAMS),
    ("{oops", RejectionCode.BAD_REQUEST_PARAMS),
])
def test_bad_request_params_are_refused(params: str, code: RejectionCode) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(request_params_json=params))
    assert caught.value.code is code


def test_the_redacted_api_key_does_not_participate_in_projection() -> None:
    """Only `date` and `dateFormat` are read; the secret placeholder is inert."""

    a = project_historical_events_response(raw_row())
    b = project_historical_events_response(raw_row(
        request_params_json=json.dumps(
            {"apiKey": "***DIFFERENT***", "date": BUCKET_RAW, "dateFormat": "iso"})))
    assert a.observation_ids == b.observation_ids


# --------------------------------------------------------------------------- #
# 10-16. Wrapper validation
# --------------------------------------------------------------------------- #
def test_a_missing_wrapper_timestamp_is_refused() -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body(timestamp=...)))
    assert caught.value.code is RejectionCode.MISSING_SNAPSHOT_TIMESTAMP


@pytest.mark.parametrize("ts", [
    "2026-03-01T16:55:37", "2026-03-01T16:55:37+00:00", "2026-03-01T16:55:37z",
    "2026-02-30T16:55:37Z", "2026-99-01T16:55:37Z", "2026-03-01T24:00:00Z",
    "nonsense", "", 12345, None])
def test_a_malformed_wrapper_timestamp_is_refused(ts: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body(timestamp=ts)))
    assert caught.value.code in (RejectionCode.BAD_SNAPSHOT_TIMESTAMP,
                                 RejectionCode.MISSING_SNAPSHOT_TIMESTAMP)


def test_a_snapshot_after_the_requested_bucket_is_refused() -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=body(timestamp="2026-03-01T17:05:00Z")))
    assert caught.value.code is RejectionCode.SNAPSHOT_AFTER_REQUEST


def test_a_snapshot_exactly_at_the_requested_bucket_is_allowed() -> None:
    proj = project_historical_events_response(
        raw_row(body=body(timestamp=BUCKET_RAW, previous_timestamp=PREV_RAW,
                          next_timestamp="2026-03-01T17:05:00Z")))
    assert proj.provider_snapshot_timestamp == BUCKET


def test_an_off_grid_snapshot_is_accepted() -> None:
    """The real provider grid sits at ~:37s; grid alignment must NOT be required."""

    proj = project_historical_events_response(raw_row())
    assert proj.provider_snapshot_timestamp == "2026-03-01T16:55:37.000000Z"


@pytest.mark.parametrize("key", ["previous_timestamp", "next_timestamp"])
def test_a_malformed_adjacent_timestamp_is_refused(key: str) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body(None, **{key: "bad"})))
    assert caught.value.code is RejectionCode.BAD_ADJACENT_TIMESTAMP


@pytest.mark.parametrize("key,value", [
    ("previous_timestamp", "2026-03-01T16:56:00Z"),   # not before
    ("next_timestamp", "2026-03-01T16:50:00Z"),       # not after
    ("previous_timestamp", SNAP_RAW),                 # equal
    ("next_timestamp", SNAP_RAW),                     # equal
])
def test_adjacent_ordering_violations_are_refused(key: str, value: str) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body(None, **{key: value})))
    assert caught.value.code is RejectionCode.ADJACENT_ORDERING


@pytest.mark.parametrize("missing", ["previous_timestamp", "next_timestamp"])
def test_absent_or_null_adjacent_timestamps_are_permitted(missing: str) -> None:
    absent = project_historical_events_response(raw_row(body=body(None, **{missing: ...})))
    explicit_null = project_historical_events_response(
        raw_row(body=body(None, **{missing: None})))
    assert getattr(absent, missing) is None
    assert getattr(explicit_null, missing) is None


@pytest.mark.parametrize("data", ['{"a": 1}', '"text"', "5"])
def test_data_that_is_not_a_list_is_refused(data: str) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=json.dumps({"timestamp": SNAP_RAW,
                                     "data": json.loads(data)})))
    assert caught.value.code is RejectionCode.DATA_NOT_LIST


def test_an_empty_successful_snapshot_projects_to_zero_observations() -> None:
    """Valid evidence of zero events -- not the same as a failed request."""

    proj = project_historical_events_response(raw_row(body=body([])))
    assert proj.observations == ()
    assert proj.provider_snapshot_timestamp == SNAP


def test_an_empty_snapshot_verifies_only_when_nothing_cites_it(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, body=body([]))
    assert verify_historical_event_projections(db, "raw_1").verified
    store(db, expected_observation())      # fabricated: body has no events
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified
    assert len(report.unexpected) == 1


# --------------------------------------------------------------------------- #
# 17-24. Event-level validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["text", 5, None, [], True])
def test_an_event_that_is_not_an_object_is_refused(bad: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body([bad])))
    assert caught.value.code is RejectionCode.EVENT_NOT_OBJECT


@pytest.mark.parametrize("bad", [
    EV_A.upper(), EV_A[:-1], EV_A + "a", EV_A[:-1] + "g", " " + EV_A,
    EV_A + " ", EV_A + "\n", EV_A[:-1] + "е", EV_A[:-1] + "ｅ",
    "", None, 12345])
def test_a_malformed_event_id_is_refused(bad: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body([event(bad)])))
    assert caught.value.code is RejectionCode.EVENT_BAD_ID


@pytest.mark.parametrize("sport", ["baseball_mlb", "basketball_ncaab", "", None])
def test_an_event_with_the_wrong_sport_is_refused(sport: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=body([event(sport=sport)])))
    assert caught.value.code is RejectionCode.EVENT_WRONG_SPORT


@pytest.mark.parametrize("home,away", [
    (None, "Miami Heat"), ("Boston Celtics", None), ("", "Miami Heat"),
    ("Boston Celtics", ""), (5, "Miami Heat"), ("Boston Celtics", ["x"]),
])
def test_a_missing_or_mistyped_team_is_refused(home: Any, away: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=body([event(home=home, away=away)])))
    assert caught.value.code is RejectionCode.EVENT_BAD_TEAM


@pytest.mark.parametrize("label", [
    "Portland Trail Blazers", "  Leading Spaces", "trailing spaces  ",
    "MiXeD CaSe", "Montréal", "Montréal", "Team\twith\ttabs"])
def test_team_labels_are_preserved_byte_for_byte(label: str) -> None:
    proj = project_historical_events_response(
        raw_row(body=body([event(home=label)])))
    assert proj.observations[0].home_team_raw == label


# --------------------------------------------------------------------------- #
# 25-29. commence_time semantics
# --------------------------------------------------------------------------- #
def test_a_valid_commence_time_is_preserved_as_an_instant() -> None:
    proj = project_historical_events_response(raw_row())
    assert proj.observations[0].commence_time == "2026-03-01T18:10:00.000000Z"


def test_an_explicit_null_commence_time_is_real_evidence() -> None:
    proj = project_historical_events_response(
        raw_row(body=body([event(commence=None)])))
    assert proj.observations[0].commence_time is None


def test_a_missing_commence_key_is_refused_not_treated_as_null() -> None:
    """Adjudicated: absence of the key is a payload-shape deviation.

    An explicit null is the provider saying "no start time"; an absent key is
    the provider not speaking the shape this projector understands. Collapsing
    them would record a claim the provider never made.
    """

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=body([event(drop_commence=True)])))
    assert caught.value.code is RejectionCode.EVENT_MISSING_COMMENCE_KEY


@pytest.mark.parametrize("bad", [
    "2026-03-01T18:10:00", "2026-03-01T18:10:00+00:00", "2026-02-30T18:10:00Z",
    "later today", "", 5, []])
def test_a_malformed_commence_time_is_refused(bad: Any) -> None:
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body([event(commence=bad)])))
    assert caught.value.code is RejectionCode.EVENT_BAD_COMMENCE


def test_null_and_a_value_hash_differently() -> None:
    a = project_historical_events_response(raw_row(body=body([event(commence=None)])))
    b = project_historical_events_response(raw_row())
    assert a.observation_ids != b.observation_ids


# --------------------------------------------------------------------------- #
# 30-31. Duplicate event ids
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("second", [
    event(EV_A),                                        # byte-identical
    event(EV_A, home="Chicago Bulls"),                  # different home
    event(EV_A, away="Detroit Pistons"),                # different away
    event(EV_A, home="Miami Heat", away="Boston Celtics"),   # swapped
    event(EV_A, commence="2026-03-01T19:10:00Z"),       # different commence
    event(EV_A, commence=None),                         # null vs value
])
def test_any_duplicate_event_id_in_one_snapshot_is_refused(second: dict) -> None:
    """Fail closed: the provider contract does not authorize duplicates.

    Even an exact duplicate is refused rather than collapsed -- a snapshot that
    repeats an event is a provider anomaly, and quietly deduplicating it would
    hide exactly the kind of id irregularity a G5 audit exists to notice.
    """

    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(raw_row(body=body([event(EV_A), second])))
    assert caught.value.code is RejectionCode.DUPLICATE_EVENT_ID


def test_a_duplicate_differing_only_in_an_ignored_field_is_still_refused() -> None:
    duplicate = event(EV_A)
    duplicate["sport_title"] = "National Basketball Association"
    with pytest.raises(ProjectionRejected) as caught:
        project_historical_events_response(
            raw_row(body=body([event(EV_A), duplicate])))
    assert caught.value.code is RejectionCode.DUPLICATE_EVENT_ID


# --------------------------------------------------------------------------- #
# 32-41. Complete-set verification
# --------------------------------------------------------------------------- #
def test_a_missing_expected_observation_fails(db: sqlite3.Connection) -> None:
    """Selective materialization is the completeness hole this closes."""

    put_raw(db, body=body([event(EV_A), event(EV_B)]))
    store(db, expected_observation(EV_A))          # EV_B deliberately omitted
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified
    assert len(report.missing) == 1 and report.unexpected == ()
    assert any("not stored" in f for f in report.failures)


def test_an_unexpected_stored_observation_fails(db: sqlite3.Connection) -> None:
    """The L1 threat itself: a row citing a body that does not contain it."""

    put_raw(db, body=body([event(EV_A)]))
    store(db, expected_observation(EV_A))
    store(db, expected_observation(EV_B))          # not in the body
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified
    assert len(report.unexpected) == 1
    assert any("NOT derivable" in f for f in report.failures)


@pytest.mark.parametrize("column,value", [
    ("home_team_raw", "Tampered FC"),
    ("away_team_raw", "Tampered United"),
    ("commence_time", "2026-03-01T19:10:00.000000Z"),
    ("provider_snapshot_timestamp", "2026-03-01T16:50:37.000000Z"),
    ("requested_at_bucket", "2026-03-01T16:00:00.000000Z"),
    ("sport_key", "baseball_mlb"),
    ("namespace_generation", "v3"),
    ("league_id", "lg_nba"),
])
def test_a_tampered_stored_column_fails(
    db: sqlite3.Connection, column: str, value: str,
) -> None:
    """The stored id stays genuine; only a body-derived column is altered."""

    put_raw(db, body=body([event(EV_A)]))
    obs = expected_observation(EV_A)
    if column == "league_id":
        pytest.skip("league_id is FK-constrained; covered by the projection test")
    store(db, obs, **{column: value})
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified


def test_an_observation_citing_the_wrong_raw_response_is_unseen_there(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, "raw_1", body=body([event(EV_A)]))
    put_raw(db, "raw_2", body=body([event(EV_B)]))
    store(db, expected_observation(EV_A), rid="raw_2")   # cites the wrong one
    assert not verify_historical_event_projections(db, "raw_1").verified   # missing
    assert not verify_historical_event_projections(db, "raw_2").verified   # both


def test_a_forged_content_hash_fails(db: sqlite3.Connection) -> None:
    put_raw(db, body=body([event(EV_A)]))
    store(db, expected_observation(EV_A), observation_content_hash="forged")
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified
    assert report.hash_mismatches or any("content hash" in f for f in report.failures)


def test_a_forged_observation_id_fails(db: sqlite3.Connection) -> None:
    put_raw(db, body=body([event(EV_A)]))
    store(db, expected_observation(EV_A), observation_id="hme_forged")
    report = verify_historical_event_projections(db, "raw_1")
    assert not report.verified
    assert report.missing and report.unexpected


# --------------------------------------------------------------------------- #
# 42-45. Mutation of the cited evidence after materialization
# --------------------------------------------------------------------------- #
def _bypass_append_only(conn: sqlite3.Connection) -> None:
    """Drop the guards so mutation can be tested, not assumed impossible."""

    for name in ("trg_raw_responses_no_update", "trg_raw_responses_no_delete",
                 "trg_raw_responses_no_replace"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.commit()


@pytest.mark.parametrize("column,value", [
    ("body", "REPLACED"),
    ("endpoint", "/v4/sports/basketball_nba/odds"),
    ("http_status", 500),
    ("provider", "balldontlie"),
    ("request_params_json",
     '{"date": "2026-03-01T16:00:00Z", "dateFormat": "iso"}'),
])
def test_mutating_the_cited_evidence_is_detected(
    db: sqlite3.Connection, column: str, value: Any,
) -> None:
    """Do not rely on "that row is normally immutable" as the only test."""

    put_raw(db, body=body([event(EV_A)]))
    store(db, expected_observation(EV_A))
    assert verify_historical_event_projections(db, "raw_1").verified

    _bypass_append_only(db)
    if column == "body":
        value = body([event(EV_B)])          # valid, but a different event
    db.execute(f"UPDATE raw_responses SET {column} = ? "  # noqa: S608
               "WHERE raw_response_id = 'raw_1'", (value,))
    db.commit()
    assert not verify_historical_event_projections(db, "raw_1").verified


def test_a_deleted_cited_response_fails(db: sqlite3.Connection) -> None:
    put_raw(db, body=body([event(EV_A)]))
    report = verify_historical_event_projections(db, "raw_missing")
    assert not report.verified
    assert "does not exist" in report.failures[0]


# --------------------------------------------------------------------------- #
# 46-47. Determinism
# --------------------------------------------------------------------------- #
def test_provider_ordering_does_not_change_the_projection() -> None:
    forward = project_historical_events_response(
        raw_row(body=body([event(EV_A), event(EV_B)])))
    reverse = project_historical_events_response(
        raw_row(body=body([event(EV_B), event(EV_A)])))
    assert forward.observation_ids == reverse.observation_ids


def test_insertion_order_does_not_change_verification(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    put_raw(db, body=body([event(EV_A), event(EV_B)]))
    for obs in reversed(project_historical_events_response(
            raw_row(body=body([event(EV_A), event(EV_B)]))).observations):
        store(db, obs)
    assert verify_historical_event_projections(db, "raw_1").verified


def test_the_projection_is_stable_across_repeated_calls() -> None:
    a = project_historical_events_response(raw_row())
    b = project_historical_events_response(raw_row())
    assert a == b


# --------------------------------------------------------------------------- #
# Composite gate
# --------------------------------------------------------------------------- #
def test_the_composite_gate_scans_every_historical_response(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, "raw_1", body=body([event(EV_A)]))
    put_raw(db, "raw_2", body=body([event(EV_B)]))       # nothing materialized
    store(db, expected_observation(EV_A), rid="raw_1")

    result = verify_historical_market_event_evidence(db)
    assert len(result.reports) == 2
    assert not result.verified
    by_id = {r.raw_response_id: r for r in result.reports}
    assert by_id["raw_1"].verified
    # raw_2's event was never materialized: the gate must NOT let it hide.
    assert not by_id["raw_2"].verified
    assert by_id["raw_2"].missing


def test_the_composite_gate_ignores_non_historical_responses(
    db: sqlite3.Connection,
) -> None:
    put_raw(db, "raw_odds", endpoint="/v4/sports/basketball_nba/odds")
    result = verify_historical_market_event_evidence(db)
    assert result.reports == ()
    assert result.verified          # nothing to check, and nothing orphaned


# --------------------------------------------------------------------------- #
# Non-regression
# --------------------------------------------------------------------------- #
def test_no_identity_authority_was_granted() -> None:
    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective import sources
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()
    assert THE_ODDS_API_PROVIDER not in sources.PROVIDER_LEAGUES
    assert THE_ODDS_API_PROVIDER not in ATTESTED_GENERATIONS
    assert THE_ODDS_API_PROVIDER not in OFFICIAL_PROVIDER_BY_LEAGUE.values()
    assert not hasattr(sources, "LINKING_NAMESPACES")


def test_verification_creates_no_rows(db: sqlite3.Connection) -> None:
    put_raw(db, body=body([event(EV_A)]))
    store(db, expected_observation(EV_A))
    verify_historical_market_event_evidence(db)
    for table in ("identity_audit_records", "static_crosswalk_provenance",
                  "games", "reconstruction_corpus_versions",
                  "provider_game_references"):
        assert db.execute(
            f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0  # noqa: S608
    assert db.execute(
        "SELECT COUNT(*) c FROM historical_market_event_observations"
    ).fetchone()["c"] == 1


def test_the_projector_is_not_an_asof_reader_source() -> None:
    from sports_quant.pit import asof

    source = Path(asof.__file__).read_text(encoding="utf-8")
    assert "historical_events_projection" not in source
    assert "historical_market_event_observations" not in source
