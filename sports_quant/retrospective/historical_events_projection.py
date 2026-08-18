"""Stage-A proof: raw response -> historical wrapper -> event -> typed observation.

The threat this closes (review finding L1)
------------------------------------------
v21 lets an observation cite any same-provider HTTP-200 ``raw_responses`` row.
The database cannot check body contents without parsing the payload, so a row
whose cited response does not contain it is storable, readable and
digest-bound exactly as if genuine. Storage was never proof.

This module supplies the missing proof, deterministically and offline: it
re-derives the **complete** typed observation set that one preserved response
must yield, and compares it against what is actually stored.

Two-way completeness, on purpose
--------------------------------
A row-level check alone would leave a completeness hole: a caller could
materialize the easy events from a snapshot and quietly omit a contradictory
one, shrinking the very population a G5 event-id audit needs. So the verifier
asserts both directions -- every stored observation is derivable from the cited
body, **and** every event the body carries is stored.

What this module is not
-----------------------
It performs no acquisition, resolves no canonical game, registers no provider
and normalizes no team label. It reads preserved evidence and says whether the
typed rows honestly represent it.
"""

from __future__ import annotations

import enum
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Optional

from ..db.schema import THE_ODDS_API_PROVIDER
from .market_observations import (
    EVENT_ID_PATTERN,
    MarketEventObservation,
    ObservationHashMismatch,
    observation_content_hash,
    observation_id,
    verify_observation_content_hashes,
)

__all__ = [
    "HISTORICAL_EVENTS_ENDPOINTS",
    "PROJECTION_POLICY_VERSION",
    "SUPPORTED_SPORT_LEAGUES",
    "EvidenceVerdict",
    "HistoricalEventProjection",
    "ProjectionRejected",
    "RejectionCode",
    "VerificationReport",
    "canonical_instant",
    "project_historical_events_response",
    "verify_historical_event_projections",
    "verify_historical_market_event_evidence",
]

#: Bumped only if the projection rule changes. Recorded on every report so a
#: past verification can never be reinterpreted under later rules.
PROJECTION_POLICY_VERSION: Final = "hme-projection-v1"

#: The EXACT endpoints this projector accepts, mapped to their (sport_key,
#: league_id, namespace_generation). Exact membership, never substring matching:
#: `"/v4/historical/sports/basketball_nba/events"` and
#: `"/v4/sports/basketball_nba/odds"` share a great deal of text, and a
#: `startswith`/`in` test is exactly how a current-odds payload would be admitted
#: as historical evidence. A trailing slash, a query string, a case variant or a
#: percent-encoded form is a different string and is refused.
HISTORICAL_EVENTS_ENDPOINTS: Final[dict[str, tuple[str, str, str]]] = {
    "/v4/historical/sports/basketball_nba/events": ("basketball_nba", "lg_nba", "v4"),
}

#: Which league a provider sport key belongs to. A source-controlled constant,
#: not an inference: the provider body carries no league, and guessing one would
#: be exactly the kind of derived identity this lane refuses.
SUPPORTED_SPORT_LEAGUES: Final[dict[str, str]] = {"basketball_nba": "lg_nba"}

#: The provider's own instant spellings omit sub-second precision
#: (`2026-03-01T16:55:37Z`), while v21 requires one canonical spelling per
#: instant (`...:37.000000Z`). Both forms are accepted here and normalized.
_PROVIDER_INSTANT = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?Z")


class RejectionCode(str, enum.Enum):
    """Why a response cannot be projected. Every value is a refusal."""

    WRONG_PROVIDER = "wrong_provider"
    NOT_SUCCESSFUL = "not_successful"
    UNKNOWN_ENDPOINT = "unknown_endpoint"
    BAD_REQUEST_PARAMS = "bad_request_params"
    BAD_DATE_FORMAT = "bad_date_format"
    BAD_REQUESTED_BUCKET = "bad_requested_bucket"
    BODY_NOT_JSON = "body_not_json"
    BODY_NOT_OBJECT = "body_not_object"
    MISSING_SNAPSHOT_TIMESTAMP = "missing_snapshot_timestamp"
    BAD_SNAPSHOT_TIMESTAMP = "bad_snapshot_timestamp"
    SNAPSHOT_AFTER_REQUEST = "snapshot_after_request"
    BAD_ADJACENT_TIMESTAMP = "bad_adjacent_timestamp"
    ADJACENT_ORDERING = "adjacent_ordering"
    DATA_NOT_LIST = "data_not_list"
    EVENT_NOT_OBJECT = "event_not_object"
    EVENT_BAD_ID = "event_bad_id"
    EVENT_WRONG_SPORT = "event_wrong_sport"
    EVENT_BAD_TEAM = "event_bad_team"
    EVENT_MISSING_COMMENCE_KEY = "event_missing_commence_key"
    EVENT_BAD_COMMENCE = "event_bad_commence"
    DUPLICATE_EVENT_ID = "duplicate_event_id"


class ProjectionRejected(Exception):
    """A preserved response cannot be projected. Carries the exact reason."""

    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def canonical_instant(value: str, *, field_name: str,
                      code: RejectionCode) -> str:
    """Normalize a provider instant to v21's one canonical spelling.

    The provider writes `2026-03-01T16:55:37Z`; v21's triggers require
    `2026-03-01T16:55:37.000000Z`. These denote the **same instant**, and the
    schema deliberately mandates a single spelling so TEXT comparison orders
    correctly. Normalizing here is therefore required by the storage contract,
    not a liberty taken with the evidence.

    Note the asymmetry with team labels, which are stored **verbatim**: a label
    is an opaque string whose bytes are the evidence, while an instant is a
    quantity with one canonical rendering. Anything that is not a real UTC
    instant in an accepted form is refused, never repaired.
    """

    if not isinstance(value, str):
        raise ProjectionRejected(
            code, f"{field_name} is {type(value).__name__}, expected a string")
    match = _PROVIDER_INSTANT.fullmatch(value)
    if match is None:
        raise ProjectionRejected(
            code, f"{field_name} {value!r} is not a UTC instant "
            "(YYYY-MM-DDTHH:MM:SS[.ffffff]Z); offsets and naive values are refused")
    if value[11:13] == "24":
        raise ProjectionRejected(
            code, f"{field_name} {value!r} uses hour 24; one spelling per instant")
    try:
        parsed = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ProjectionRejected(
            code, f"{field_name} {value!r} is not a real instant: {exc}") from None
    fraction = (match.group(7) or ".0")[1:]
    micro = int(fraction.ljust(6, "0")[:6])
    return parsed.replace(microsecond=micro).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)


@dataclass(frozen=True)
class HistoricalEventProjection:
    """The complete typed observation set one preserved response must yield."""

    raw_response_id: str
    provider: str
    namespace_generation: str
    league_id: str
    sport_key: str
    requested_at_bucket: str
    provider_snapshot_timestamp: str
    previous_timestamp: Optional[str]
    next_timestamp: Optional[str]
    #: Deterministically ordered by the observation id, which is a pure function
    #: of content -- so provider ordering inside `data` cannot change the result.
    observations: tuple[MarketEventObservation, ...]
    policy_version: str = PROJECTION_POLICY_VERSION

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(observation_id(o) for o in self.observations)


def _require(row: Any, column: str) -> Any:
    try:
        return row[column]
    except (KeyError, IndexError) as exc:  # pragma: no cover - defensive
        raise ProjectionRejected(
            RejectionCode.BAD_REQUEST_PARAMS,
            f"raw response row has no {column!r} column") from exc


def project_historical_events_response(row: Any) -> HistoricalEventProjection:
    """Derive the complete typed observation set from one preserved response.

    A pure function of the stored row. It consults no clock, no network and no
    canonical entity, so two runs over the same evidence -- in any database, in
    any order -- produce the identical projection.

    Raises :class:`ProjectionRejected` rather than returning a partial result.
    See the module docs and the implementation report for why a snapshot is
    rejected whole rather than materialized in part.
    """

    provider = _require(row, "provider")
    if provider != THE_ODDS_API_PROVIDER:
        raise ProjectionRejected(
            RejectionCode.WRONG_PROVIDER,
            f"provider {provider!r} is not {THE_ODDS_API_PROVIDER!r}")

    status = _require(row, "http_status")
    if status != 200:
        raise ProjectionRejected(
            RejectionCode.NOT_SUCCESSFUL,
            f"HTTP {status} is not a successful snapshot; a failed request is "
            "not evidence that a market did or did not exist")

    endpoint = _require(row, "endpoint")
    if endpoint not in HISTORICAL_EVENTS_ENDPOINTS:
        raise ProjectionRejected(
            RejectionCode.UNKNOWN_ENDPOINT,
            f"endpoint {endpoint!r} is not an exact historical-events endpoint; "
            f"accepted: {sorted(HISTORICAL_EVENTS_ENDPOINTS)}")
    sport_key, league_id, generation = HISTORICAL_EVENTS_ENDPOINTS[endpoint]

    try:
        params = json.loads(_require(row, "request_params_json"))
    except (TypeError, ValueError) as exc:
        raise ProjectionRejected(
            RejectionCode.BAD_REQUEST_PARAMS,
            f"request params are not JSON: {exc}") from None
    if not isinstance(params, dict):
        raise ProjectionRejected(
            RejectionCode.BAD_REQUEST_PARAMS,
            f"request params are {type(params).__name__}, expected an object")

    date_format = params.get("dateFormat")
    if date_format != "iso":
        raise ProjectionRejected(
            RejectionCode.BAD_DATE_FORMAT,
            f"dateFormat {date_format!r} is not 'iso'; under any other format "
            "the provider's timestamps mean something different and projecting "
            "them as ISO instants would silently misread the evidence")

    requested_raw = params.get("date")
    if not isinstance(requested_raw, str) or not requested_raw:
        raise ProjectionRejected(
            RejectionCode.BAD_REQUESTED_BUCKET,
            "request carries no historical 'date' parameter")
    requested_at_bucket = canonical_instant(
        requested_raw, field_name="requested date",
        code=RejectionCode.BAD_REQUESTED_BUCKET)

    try:
        body = json.loads(_require(row, "body"))
    except (TypeError, ValueError) as exc:
        raise ProjectionRejected(
            RejectionCode.BODY_NOT_JSON, f"body is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise ProjectionRejected(
            RejectionCode.BODY_NOT_OBJECT,
            f"body is {type(body).__name__}, expected the historical wrapper "
            "object; a bare list is the CURRENT-odds shape")

    if "timestamp" not in body:
        raise ProjectionRejected(
            RejectionCode.MISSING_SNAPSHOT_TIMESTAMP,
            "wrapper carries no snapshot timestamp; the requested date is NOT a "
            "substitute for the instant the provider answered")
    snapshot = canonical_instant(
        body["timestamp"], field_name="wrapper timestamp",
        code=RejectionCode.BAD_SNAPSHOT_TIMESTAMP)

    if _instant(snapshot) > _instant(requested_at_bucket):
        raise ProjectionRejected(
            RejectionCode.SNAPSHOT_AFTER_REQUEST,
            f"snapshot {snapshot} is AFTER the requested bucket "
            f"{requested_at_bucket}; the provider answers at or before")

    # The provider's snapshot grid is genuinely off the wall-clock five-minute
    # boundary (measured at ~:37s), so no grid alignment is required of it here.
    adjacent: dict[str, Optional[str]] = {}
    for key, code in (("previous_timestamp", RejectionCode.BAD_ADJACENT_TIMESTAMP),
                      ("next_timestamp", RejectionCode.BAD_ADJACENT_TIMESTAMP)):
        value = body.get(key)
        if value is None:          # absent or explicitly null: both permitted
            adjacent[key] = None
            continue
        adjacent[key] = canonical_instant(value, field_name=key, code=code)
    if adjacent["previous_timestamp"] is not None and (
            _instant(adjacent["previous_timestamp"]) >= _instant(snapshot)):
        raise ProjectionRejected(
            RejectionCode.ADJACENT_ORDERING,
            f"previous_timestamp {adjacent['previous_timestamp']} is not before "
            f"the snapshot {snapshot}")
    if adjacent["next_timestamp"] is not None and (
            _instant(adjacent["next_timestamp"]) <= _instant(snapshot)):
        raise ProjectionRejected(
            RejectionCode.ADJACENT_ORDERING,
            f"next_timestamp {adjacent['next_timestamp']} is not after the "
            f"snapshot {snapshot}")

    data = body.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ProjectionRejected(
            RejectionCode.DATA_NOT_LIST,
            f"data is {type(data).__name__}, expected a list")

    observations: list[MarketEventObservation] = []
    seen: set[str] = set()
    for index, event in enumerate(data):
        if not isinstance(event, dict):
            raise ProjectionRejected(
                RejectionCode.EVENT_NOT_OBJECT,
                f"data[{index}] is {type(event).__name__}, expected an object")

        event_id = event.get("id")
        if not isinstance(event_id, str) or EVENT_ID_PATTERN.fullmatch(
                event_id) is None:
            raise ProjectionRejected(
                RejectionCode.EVENT_BAD_ID,
                f"data[{index}] id {event_id!r} is not exact lowercase 32-hex; "
                "it is refused, never normalized into validity")
        if event_id in seen:
            # The provider contract does not authorize duplicates. Even an
            # exact duplicate is refused rather than collapsed: a snapshot that
            # repeats an event is a provider anomaly, and silently deduplicating
            # would hide it from the very audit that looks for id irregularities.
            raise ProjectionRejected(
                RejectionCode.DUPLICATE_EVENT_ID,
                f"provider_event_id {event_id!r} appears more than once in one "
                "snapshot; the provider contract does not authorize duplicates")
        seen.add(event_id)

        event_sport = event.get("sport_key")
        if event_sport != sport_key:
            raise ProjectionRejected(
                RejectionCode.EVENT_WRONG_SPORT,
                f"data[{index}] sport_key {event_sport!r} is not {sport_key!r}")

        teams: dict[str, str] = {}
        for label in ("home_team", "away_team"):
            value = event.get(label)
            if not isinstance(value, str) or not value:
                raise ProjectionRejected(
                    RejectionCode.EVENT_BAD_TEAM,
                    f"data[{index}] {label} is {value!r}; a non-empty provider "
                    "string is required and is stored verbatim")
            teams[label] = value

        # Missing key and explicit null are NOT collapsed. The provider contract
        # includes `commence_time`; an absent key means the payload shape is not
        # the one this projector understands, which is a different fact from the
        # provider saying "no start time is known".
        if "commence_time" not in event:
            raise ProjectionRejected(
                RejectionCode.EVENT_MISSING_COMMENCE_KEY,
                f"data[{index}] has no commence_time key; an ABSENT key is a "
                "payload-shape deviation, not the same evidence as an explicit "
                "null, and is not collapsed into one")
        commence_raw = event["commence_time"]
        commence = (None if commence_raw is None else canonical_instant(
            commence_raw, field_name=f"data[{index}] commence_time",
            code=RejectionCode.EVENT_BAD_COMMENCE))

        observations.append(MarketEventObservation(
            league_id=league_id,
            provider=THE_ODDS_API_PROVIDER,
            namespace_generation=generation,
            sport_key=sport_key,
            provider_event_id=event_id,
            requested_at_bucket=requested_at_bucket,
            provider_snapshot_timestamp=snapshot,
            commence_time=commence,
            home_team_raw=teams["home_team"],
            away_team_raw=teams["away_team"],
        ))

    # Ordered by content-derived id, so the provider's ordering inside `data`
    # cannot change the projection.
    observations.sort(key=observation_id)
    return HistoricalEventProjection(
        raw_response_id=str(_require(row, "raw_response_id")),
        provider=THE_ODDS_API_PROVIDER,
        namespace_generation=generation,
        league_id=league_id,
        sport_key=sport_key,
        requested_at_bucket=requested_at_bucket,
        provider_snapshot_timestamp=snapshot,
        previous_timestamp=adjacent["previous_timestamp"],
        next_timestamp=adjacent["next_timestamp"],
        observations=tuple(observations),
    )


class EvidenceVerdict(str, enum.Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class VerificationReport:
    """Outcome of verifying one preserved response against its stored rows."""

    raw_response_id: str
    verdict: EvidenceVerdict
    policy_version: str = PROJECTION_POLICY_VERSION
    expected_count: int = 0
    stored_count: int = 0
    #: Derivable from the body but absent from the database.
    missing: tuple[str, ...] = ()
    #: Stored against this response but NOT derivable from its body -- the L1
    #: threat itself.
    unexpected: tuple[str, ...] = ()
    #: Stored rows whose content hash or id disagrees with their own columns.
    hash_mismatches: tuple[ObservationHashMismatch, ...] = ()
    rejection_code: Optional[RejectionCode] = None
    detail: str = ""
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verified(self) -> bool:
        return self.verdict is EvidenceVerdict.VERIFIED


_STORED_COLUMNS = (
    "observation_id", "league_id", "provider", "namespace_generation",
    "sport_key", "provider_event_id", "requested_at_bucket",
    "provider_snapshot_timestamp", "commence_time", "home_team_raw",
    "away_team_raw", "observation_content_hash", "raw_response_id")


def verify_historical_event_projections(
    conn: sqlite3.Connection, raw_response_id: str,
) -> VerificationReport:
    """Prove the stored observations for one response are exactly its body.

    Both directions are checked. A missing row is as much a failure as an
    unexpected one: selective materialization would let a curator drop a
    contradictory event and shrink the audit population without trace.
    """

    conn.row_factory = sqlite3.Row
    raw = conn.execute(
        "SELECT raw_response_id, provider, endpoint, http_status, "
        "request_params_json, body FROM raw_responses WHERE raw_response_id = ?",
        (raw_response_id,)).fetchone()
    if raw is None:
        return VerificationReport(
            raw_response_id=raw_response_id, verdict=EvidenceVerdict.REJECTED,
            detail=f"no raw response {raw_response_id!r}",
            failures=("cited raw response does not exist",))

    try:
        projection = project_historical_events_response(raw)
    except ProjectionRejected as exc:
        return VerificationReport(
            raw_response_id=raw_response_id, verdict=EvidenceVerdict.REJECTED,
            rejection_code=exc.code, detail=exc.detail,
            failures=(f"projection refused: {exc.code.value}",))

    expected = {observation_id(o): o for o in projection.observations}
    stored_rows = conn.execute(
        f"SELECT {', '.join(_STORED_COLUMNS)} FROM "  # noqa: S608
        "historical_market_event_observations WHERE raw_response_id = ? "
        "ORDER BY observation_id", (raw_response_id,)).fetchall()
    stored = {str(r["observation_id"]): r for r in stored_rows}

    failures: list[str] = []
    missing = tuple(sorted(set(expected) - set(stored)))
    unexpected = tuple(sorted(set(stored) - set(expected)))
    if missing:
        failures.append(
            f"{len(missing)} observation(s) derivable from the body are not "
            "stored; partial materialization is refused")
    if unexpected:
        failures.append(
            f"{len(unexpected)} stored observation(s) are NOT derivable from "
            "the cited body (L1)")

    for oid in sorted(set(expected) & set(stored)):
        want, got = expected[oid], stored[oid]
        for column, value in (
                ("league_id", want.league_id),
                ("provider", want.provider),
                ("namespace_generation", want.namespace_generation),
                ("sport_key", want.sport_key),
                ("provider_event_id", want.provider_event_id),
                ("requested_at_bucket", want.requested_at_bucket),
                ("provider_snapshot_timestamp", want.provider_snapshot_timestamp),
                ("commence_time", want.commence_time),
                ("home_team_raw", want.home_team_raw),
                ("away_team_raw", want.away_team_raw),
                ("observation_content_hash", observation_content_hash(want)),
        ):
            if got[column] != value:
                failures.append(
                    f"{oid}: {column} is {got[column]!r}, body says {value!r}")

    # Composition, not an optional extra: a row can agree with the body and
    # still carry a forged self-hash, and a row can hash correctly and cite the
    # wrong payload. Neither half alone is "verified".
    mismatches = tuple(
        m for m in verify_observation_content_hashes(conn)
        if m.observation_id in stored)
    if mismatches:
        failures.append(
            f"{len(mismatches)} stored observation(s) disagree with their own "
            "recomputed content hash or id")

    return VerificationReport(
        raw_response_id=raw_response_id,
        verdict=EvidenceVerdict.REJECTED if failures else EvidenceVerdict.VERIFIED,
        expected_count=len(expected), stored_count=len(stored),
        missing=missing, unexpected=unexpected, hash_mismatches=mismatches,
        failures=tuple(failures))


def verify_historical_market_event_evidence(
    conn: sqlite3.Connection, *, raw_response_ids: Optional[list[str]] = None,
) -> list[VerificationReport]:
    """The composite trust gate. Run this before evidence is called verified.

    Equals content-hash integrity AND raw-response projection integrity AND
    projection completeness. Naming it once, here, is what makes the
    composition impossible to forget at a call site.

    With no explicit ids, every historical-events response in the database is
    checked -- including any that carries **no** observations, so a response
    whose events were never materialized cannot hide by having nothing to
    compare.
    """

    conn.row_factory = sqlite3.Row
    if raw_response_ids is None:
        placeholders = ", ".join("?" * len(HISTORICAL_EVENTS_ENDPOINTS))
        rows = conn.execute(
            f"SELECT raw_response_id FROM raw_responses WHERE provider = ? "  # noqa: S608
            f"AND endpoint IN ({placeholders}) AND http_status = 200 "
            "ORDER BY raw_response_id",
            (THE_ODDS_API_PROVIDER, *sorted(HISTORICAL_EVENTS_ENDPOINTS))
        ).fetchall()
        raw_response_ids = [str(r["raw_response_id"]) for r in rows]
    return [verify_historical_event_projections(conn, rid)
            for rid in raw_response_ids]
