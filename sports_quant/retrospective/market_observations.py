"""Historical market EVENT observations: content hash, deterministic id, validation.

What an observation asserts
---------------------------
Exactly one thing: *this provider reported this provider event id, with these
verbatim labels and this contemporaneous commence time, in the historical
snapshot it returned for this requested bucket.*

It does **not** assert which canonical game the event is. There is no
``canonical_game_id`` anywhere in this module or its table, because Stage-A
acquisition is identity-free by construction and the cleanest enforcement of
that is having nowhere to record an identity claim.

Why the content hash is the portable identity
---------------------------------------------
``raw_response_id`` is a database-local surrogate: it does not survive transport
between reconstruction databases, so it cannot be what a digest binds. The
content hash is computed from the semantic observation tuple alone, so the same
provider statement hashes identically in any database, on any machine, in any
insertion order.

``observed_at`` is deliberately **excluded** from the hash. It is *our*
materialization clock, not something the provider said. Including it would make
one provider statement hash differently depending on when we happened to write
it down, which would break replay idempotence and let identical evidence be
stored twice under two hashes. The reviewed field list in the architecture's
§11b agrees, and this module does not extend it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Optional

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3

from streaming.event_envelope import canonical_json

from ..db.ids import HISTORICAL_MARKET_EVENT_PREFIX

__all__ = [
    "EVENT_ID_PATTERN",
    "ObservationHashMismatch",
    "verify_observation_content_hashes",
    "OBSERVATION_CONTENT_POLICY_VERSION",
    "MarketEventObservation",
    "ObservationValidationError",
    "observation_content_hash",
    "observation_id",
    "validate_provider_event_id",
    "validate_canonical_instant",
]

#: Bumped only if the normalized tuple or its serialization ever changes. It
#: participates in the hash, so an old hash can never silently be reinterpreted
#: under new rules.
OBSERVATION_CONTENT_POLICY_VERSION: Final = "hme-observation-content-v1"

#: The Odds API historical event id, exactly. Anchored, lowercase, ASCII hex.
#: ``re.fullmatch`` is still used rather than trusting the anchors, because
#: ``$`` also matches before a trailing newline.
EVENT_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")

#: One canonical spelling per instant, matching ``utc_now_iso`` and the f019/f020
#: database triggers. Hour 24 is legal ISO-8601 but is never emitted here.
_INSTANT_PATTERN: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


class ObservationValidationError(ValueError):
    """An observation field violated its contract. Never repaired, only refused."""


def validate_provider_event_id(value: str) -> str:
    """Return ``value`` unchanged, or refuse it. **Never repairs.**

    Uppercase hex, padding whitespace, zero-width characters and Unicode
    confusables are all *rejected*, not normalized. Trimming or case-folding a
    bad id into a good-looking one silently invents a different identifier: the
    repository would then store a key the provider never issued, and it would
    coexist happily beside the real one because v19's only constraint on a
    provider id is that it is non-empty.
    """

    if not isinstance(value, str):
        raise ObservationValidationError(
            f"provider_event_id must be a str, got {type(value).__name__}")
    if EVENT_ID_PATTERN.fullmatch(value) is None:
        raise ObservationValidationError(
            f"provider_event_id {value!r} is not exact lowercase 32-hex "
            f"({EVENT_ID_PATTERN.pattern}). It is refused rather than trimmed, "
            "case-folded or Unicode-normalized into validity."
        )
    return value


def validate_canonical_instant(value: str, *, field: str) -> str:
    """Refuse anything that is not the one canonical UTC spelling.

    A naive local timestamp is **not** converted to UTC. The offset it should
    have had is unknowable here, and guessing it would shift a snapshot instant
    by hours while looking perfectly well-formed.
    """

    if not isinstance(value, str):
        raise ObservationValidationError(
            f"{field} must be a str, got {type(value).__name__}")
    if _INSTANT_PATTERN.fullmatch(value) is None:
        raise ObservationValidationError(
            f"{field} {value!r} is not a canonical UTC instant "
            "(YYYY-MM-DDTHH:MM:SS.ffffffZ). Naive and offset-bearing values are "
            "refused, never converted."
        )
    if value[11:13] == "24":
        raise ObservationValidationError(
            f"{field} {value!r} uses hour 24; one spelling per instant")
    # Reject impossible calendar instants (Feb 30, month 99) that match the shape.
    from datetime import datetime
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ObservationValidationError(
            f"{field} {value!r} is not a real instant: {exc}") from None
    return value


@dataclass(frozen=True)
class MarketEventObservation:
    """One provider statement about one event in one historical snapshot.

    Frozen: an observation is a record of what was said, and there is no such
    thing as correcting it. A later, different answer is a NEW observation.
    """

    league_id: str
    provider: str
    namespace_generation: str
    sport_key: str
    provider_event_id: str
    requested_at_bucket: str
    provider_snapshot_timestamp: str
    commence_time: Optional[str]
    home_team_raw: str
    away_team_raw: str

    def __post_init__(self) -> None:
        validate_provider_event_id(self.provider_event_id)
        validate_canonical_instant(
            self.requested_at_bucket, field="requested_at_bucket")
        validate_canonical_instant(
            self.provider_snapshot_timestamp, field="provider_snapshot_timestamp")
        if self.commence_time is not None:
            validate_canonical_instant(self.commence_time, field="commence_time")
        for field in ("league_id", "provider", "namespace_generation", "sport_key",
                      "home_team_raw", "away_team_raw"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ObservationValidationError(f"{field} must be a non-empty str")

    @property
    def content_hash(self) -> str:
        return observation_content_hash(self)

    @property
    def observation_id(self) -> str:
        return observation_id(self)


def observation_content_hash(observation: MarketEventObservation) -> str:
    """The portable semantic identity of one observation.

    Uses the repository's existing ``canonical_json`` convention (sorted keys,
    tight separators, ``ensure_ascii=False``), so this introduces no novel
    serialization. That gives determinism across processes, independence from
    dict insertion order, JSON ``null`` as an unambiguous encoding for an absent
    ``commence_time``, and UTF-8 bytes at the hash boundary rather than a
    locale-dependent encoding.

    Excluded on purpose: ``observation_id`` (derived from this hash, so including
    it would be circular), ``raw_response_id`` (database-local), ``created_at``
    and ``observed_at`` (our clocks, not the provider's statement).
    """

    payload = {
        "policy": OBSERVATION_CONTENT_POLICY_VERSION,
        "league_id": observation.league_id,
        "provider": observation.provider,
        "namespace_generation": observation.namespace_generation,
        "sport_key": observation.sport_key,
        "provider_event_id": observation.provider_event_id,
        "requested_at_bucket": observation.requested_at_bucket,
        "provider_snapshot_timestamp": observation.provider_snapshot_timestamp,
        # ``None`` encodes as JSON ``null``, which no string value can collide
        # with -- "the provider supplied no commence time" and "the provider
        # supplied the empty string" stay distinguishable.
        "commence_time": observation.commence_time,
        "home_team_raw": observation.home_team_raw,
        "away_team_raw": observation.away_team_raw,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def observation_id(observation: MarketEventObservation) -> str:
    """A deterministic id, derived from the content hash.

    Replays identically, differs for content-distinct observations that must
    coexist, and depends on no rowid, no wall clock and no database-local id --
    so a transported or rebuilt corpus reproduces every id exactly. Follows the
    existing ``prefix + sha256[:24]`` convention used by ``canonical_game_id``
    and ``canonical_player_id``.
    """

    key = "|".join(("historical_market_event_observation",
                    OBSERVATION_CONTENT_POLICY_VERSION,
                    observation_content_hash(observation)))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"{HISTORICAL_MARKET_EVENT_PREFIX}{digest}"


# --------------------------------------------------------------------------- #
# Verification (independent-review repair D3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ObservationHashMismatch:
    """One stored row whose content hash does not match its own columns."""

    observation_id: str
    stored_content_hash: str
    recomputed_content_hash: str
    recomputed_observation_id: str


def verify_observation_content_hashes(
    conn: "sqlite3.Connection",
) -> list[ObservationHashMismatch]:
    """Recompute every stored observation's hash and id from its own columns.

    Why this exists
    ---------------
    ``observation_content_hash`` and ``observation_id`` are written by the
    caller. Nothing in the database recomputes them, and
    ``source_corpus_digest`` folds the **stored** hash column rather than a
    derivation -- so a row inserted by direct SQL with a fabricated hash is
    digest-bound exactly as if it were genuine, and the repository hands it back
    unchallenged.

    That is a real gap between "a row exists" and "a row is audit-grade". This
    verifier closes it deterministically and offline: it re-derives both values
    from the row's own semantic columns and reports every disagreement. It must
    pass before an observation corpus is digested, audited or curated.

    Returns an empty list when every row verifies. Never mutates anything.
    """

    columns = ("observation_id", "league_id", "provider", "namespace_generation",
               "sport_key", "provider_event_id", "requested_at_bucket",
               "provider_snapshot_timestamp", "commence_time", "home_team_raw",
               "away_team_raw", "observation_content_hash")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM historical_market_event_observations "  # noqa: S608
        "ORDER BY observation_id"
    ).fetchall()

    mismatches: list[ObservationHashMismatch] = []
    for row in rows:
        record = (dict(row) if hasattr(row, "keys")
                  else dict(zip(columns, row, strict=True)))
        try:
            rebuilt = MarketEventObservation(
                league_id=record["league_id"],
                provider=record["provider"],
                namespace_generation=record["namespace_generation"],
                sport_key=record["sport_key"],
                provider_event_id=record["provider_event_id"],
                requested_at_bucket=record["requested_at_bucket"],
                provider_snapshot_timestamp=record["provider_snapshot_timestamp"],
                commence_time=record["commence_time"],
                home_team_raw=record["home_team_raw"],
                away_team_raw=record["away_team_raw"],
            )
        except ObservationValidationError:
            # A row the domain type refuses cannot have a valid hash either.
            mismatches.append(ObservationHashMismatch(
                observation_id=str(record["observation_id"]),
                stored_content_hash=str(record["observation_content_hash"]),
                recomputed_content_hash="<unrepresentable>",
                recomputed_observation_id="<unrepresentable>"))
            continue

        recomputed = observation_content_hash(rebuilt)
        rebuilt_id = observation_id(rebuilt)
        if (recomputed != record["observation_content_hash"]
                or rebuilt_id != record["observation_id"]):
            mismatches.append(ObservationHashMismatch(
                observation_id=str(record["observation_id"]),
                stored_content_hash=str(record["observation_content_hash"]),
                recomputed_content_hash=recomputed,
                recomputed_observation_id=rebuilt_id))
    return mismatches
