"""NBA Lane-R event-completion evidence: policy, derivation, materialization.

The policy, stated once and versioned
------------------------------------
    For NBA retrospective Lane-R evidence, the wallclock of the final recorded
    play in the preserved BALLDONTLIE ``/v1/plays`` payload is accepted as the
    source event completion evidence. It is a LOWER-BOUND COMPLETION PROXY, not
    an official-final timestamp. The existing six-hour
    ``prior_event_completion_conservative_v1`` rule is what makes downstream
    feature availability conservative.

That wording is deliberate and must not drift. ``wallclock`` is the provider's
own UTC instant for a play; the last play cannot have occurred after the game
ended, so it bounds completion **from below**. Whether official scorekeeping
declared the game final at that same instant is not evidenced by anything
preserved here, and this module never claims it is.

Why no new availability rule
----------------------------
The residual gap between "last recorded play" and "officially final" is minutes.
``prior_event_completion_conservative_v1`` already adds **six hours** before a
derived fact is treatable as knowable, which swamps that gap by two orders of
magnitude. Inventing a new rule or a bespoke safety margin would add an
unreviewed policy where an existing reviewed one already suffices.

What this module is not
-----------------------
It is not F1-R. It derives and materializes *completion evidence* for a single
preserved payload. It builds no target set, computes no feature, and enumerates
no prior-game relationships.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional

from ..db.models import RawResponse
from ..db.repositories.raw_responses import SqliteRawResponseRepository
from .provenance import RetrospectiveProvenanceError

__all__ = [
    "NBA_COMPLETION_CLASSIFICATION",
    "NBA_COMPLETION_ENDPOINT",
    "NBA_COMPLETION_POLICY",
    "NBA_COMPLETION_POLICY_VERSION",
    "NBA_COMPLETION_PROVIDER",
    "CompletionEvidenceError",
    "MaterializationOutcome",
    "MaterializedEvidence",
    "NbaCompletionEvidence",
    "derive_completion_evidence",
    "materialize_completion_evidence",
]


class CompletionEvidenceError(RetrospectiveProvenanceError):
    """Preserved evidence cannot defensibly yield a completion instant."""


#: Bumping this changes what a stored `availability_source` MEANS, so it is
#: versioned exactly like an availability rule id. A later policy (for example
#: one that accepted a different terminal marker) must be a NEW version.
NBA_COMPLETION_POLICY_VERSION: Final = "nba-final-play-wallclock-v1"

#: This policy is NBA-only and endpoint-specific by construction.
NBA_COMPLETION_PROVIDER: Final = "balldontlie"
NBA_COMPLETION_ENDPOINT: Final = "/v1/plays"
NBA_COMPLETION_LEAGUE: Final = "lg_nba"

#: Carried onto the certification so a reader/reviewer sees the strength claim
#: without having to find this file.
NBA_COMPLETION_CLASSIFICATION: Final = "defensible_derived_lower_bound"

#: The exact reviewed policy text, bound to its version. Stored as the
#: `availability_source` on a certification, which is the v19 field whose whole
#: purpose is naming the evidence documenting an availability claim -- so no
#: schema state is added merely to hold prose.
NBA_COMPLETION_POLICY: Final = (
    f"{NBA_COMPLETION_POLICY_VERSION}: the wallclock of the final recorded play "
    f"in the preserved BALLDONTLIE {NBA_COMPLETION_ENDPOINT} payload is accepted "
    "as source event completion evidence. It is a LOWER-BOUND completion proxy, "
    "NOT an official-final timestamp. Downstream availability remains gated by "
    "prior_event_completion_conservative_v1 (+6h)."
)

#: The provider's terminal marker. A payload whose highest-ordered play is not
#: this type is not evidence of a completed game, whatever else it contains.
_TERMINAL_PLAY_TYPE: Final = "End Game"


class MaterializationOutcome(str, enum.Enum):
    CREATED = "created"
    REUSED = "reused"


@dataclass(frozen=True)
class NbaCompletionEvidence:
    """One game's completion bound, with everything needed to defend it."""

    provider_game_id: str
    raw_response_id: str
    source_endpoint: str
    provider: str
    league_id: str
    #: The derived bound. UTC, microsecond ISO, project format.
    source_event_completed_at: str
    #: The play that produced it, so a reviewer can go straight to it.
    terminal_play_order: int
    terminal_play_period: int
    play_count: int
    policy_version: str
    classification: str
    #: The preserved payload's content hash, so the derivation can be re-checked
    #: against the exact bytes it came from.
    source_content_hash: str

    def as_json(self) -> dict[str, object]:
        return {
            "provider_game_id": self.provider_game_id,
            "raw_response_id": self.raw_response_id,
            "source_endpoint": self.source_endpoint,
            "provider": self.provider,
            "league_id": self.league_id,
            "source_event_completed_at": self.source_event_completed_at,
            "terminal_play_order": self.terminal_play_order,
            "terminal_play_period": self.terminal_play_period,
            "play_count": self.play_count,
            "policy_version": self.policy_version,
            "classification": self.classification,
            "source_content_hash": self.source_content_hash,
        }


@dataclass(frozen=True)
class MaterializedEvidence:
    """The destination row a later certification will cite."""

    raw_response_id: str
    outcome: MaterializationOutcome
    content_hash: str


def _fail(message: str) -> "CompletionEvidenceError":
    return CompletionEvidenceError(message)


def _parse_utc(value: Any, where: str) -> datetime:
    """Parse an explicitly-zoned UTC instant, refusing anything ambiguous."""

    if not isinstance(value, str) or not value:
        raise _fail(f"{where}: wallclock is missing or not a string ({value!r})")
    text = value.strip()
    # A naive timestamp has no defensible instant. Refuse rather than assume UTC.
    if not (text.endswith("Z") or text[-6:-3] in ("+0", "-0") or
            (len(text) > 6 and text[-6] in "+-")):
        raise _fail(f"{where}: wallclock {value!r} carries no timezone; a naive "
                    "timestamp cannot bound a historical instant")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(f"{where}: wallclock {value!r} is not a parseable "
                    f"timestamp ({exc})") from None
    if parsed.tzinfo is None:
        raise _fail(f"{where}: wallclock {value!r} parsed to a naive datetime")
    return parsed.astimezone(timezone.utc)


def derive_completion_evidence(raw: RawResponse) -> NbaCompletionEvidence:
    """Derive one NBA completion bound from one preserved payload, or fail.

    Every check below refuses rather than repairs. Malformed source evidence is
    not fixed up: a corpus that needs its evidence corrected to be usable is a
    corpus whose provenance nobody can defend afterwards.
    """

    from ..db.schema import to_iso

    where = f"raw_response {raw.raw_response_id!r}"

    # ---- 1. the evidence must be the thing this policy is about ------------ #
    if raw.provider != NBA_COMPLETION_PROVIDER:
        raise _fail(f"{where}: provider is {raw.provider!r}; this policy covers "
                    f"{NBA_COMPLETION_PROVIDER!r} (NBA) only")
    if raw.endpoint != NBA_COMPLETION_ENDPOINT:
        raise _fail(f"{where}: endpoint is {raw.endpoint!r}; this policy covers "
                    f"{NBA_COMPLETION_ENDPOINT!r} only")
    if raw.http_status != 200:
        raise _fail(f"{where}: http_status {raw.http_status} is not a successful "
                    "response; a non-200 body is not evidence")

    # ---- 2. which game was actually requested ------------------------------ #
    try:
        params = json.loads(raw.request_params_json or "{}")
    except (TypeError, ValueError) as exc:
        raise _fail(f"{where}: request params are not JSON ({exc})") from None
    requested = params.get("game_id")
    if requested is None or str(requested).strip() == "":
        raise _fail(f"{where}: request params name no game_id, so the payload "
                    "cannot be attributed to a game")
    requested = str(requested)

    # ---- 3. the payload itself --------------------------------------------- #
    try:
        payload = json.loads(raw.body)
    except (TypeError, ValueError) as exc:
        raise _fail(f"{where}: body is not valid JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise _fail(f"{where}: payload is {type(payload).__name__}, expected an object")
    plays = payload.get("data")
    if not isinstance(plays, list):
        raise _fail(f"{where}: payload has no 'data' list")
    if not plays:
        raise _fail(f"{where}: payload contains no plays")

    # ---- 4. every play must belong to the requested game -------------------- #
    ids = {str(p.get("game_id")) for p in plays if isinstance(p, dict)}
    if len(ids) != 1:
        raise _fail(f"{where}: payload mixes game ids {sorted(ids)}; a payload "
                    "covering more than one game cannot bound either")
    if ids != {requested}:
        raise _fail(f"{where}: payload covers game {ids.pop()!r} but the request "
                    f"asked for {requested!r}")

    # ---- 5. structural integrity of the play sequence ----------------------- #
    orders: list[int] = []
    for p in plays:
        if not isinstance(p, dict):
            raise _fail(f"{where}: a play is {type(p).__name__}, expected an object")
        order = p.get("order")
        if not isinstance(order, int):
            raise _fail(f"{where}: a play has non-integer order {order!r}")
        orders.append(order)
    if len(set(orders)) != len(orders):
        raise _fail(f"{where}: play orders are not unique; the sequence cannot be "
                    "trusted to identify a final play")

    ordered = sorted(plays, key=lambda p: p["order"])
    stamps = [_parse_utc(p.get("wallclock"), where) for p in ordered]

    # Wallclock must not go backwards along the sequence.
    for previous, current in zip(stamps, stamps[1:], strict=False):
        if current < previous:
            raise _fail(f"{where}: wallclock decreases along play order; the "
                        "chronology is self-contradictory")

    # Period must not go backwards either. This is the check that catches a
    # payload whose `order` field disagrees with the actual period progression --
    # observed in the real March corpus, where an 'End Game' play sat at order
    # 393 with 91 THIRD-QUARTER plays ordered after it, timestamped 23 minutes
    # later. Monotonic wallclock alone does not catch that.
    periods = [p.get("period") for p in ordered]
    for previous, current in zip(periods, periods[1:], strict=False):
        if not isinstance(current, int) or not isinstance(previous, int):
            raise _fail(f"{where}: a play has a non-integer period")
        if current < previous:
            raise _fail(f"{where}: period decreases along play order (saw "
                        f"{previous} then {current}); the sequence is truncated "
                        "or mis-ordered and its final play is not the game's")

    # ---- 6. terminality: the last play must actually end the game ----------- #
    terminal_positions = [i for i, p in enumerate(ordered)
                          if p.get("type") == _TERMINAL_PLAY_TYPE]
    if not terminal_positions:
        raise _fail(f"{where}: no {_TERMINAL_PLAY_TYPE!r} play; the payload is "
                    "truncated and its last recorded event is not the final play")
    if len(terminal_positions) > 1:
        raise _fail(f"{where}: {len(terminal_positions)} {_TERMINAL_PLAY_TYPE!r} "
                    "plays; the payload cannot identify a single completion")
    if terminal_positions[0] != len(ordered) - 1:
        raise _fail(f"{where}: {_TERMINAL_PLAY_TYPE!r} is not the last play by "
                    f"order ({len(ordered) - 1 - terminal_positions[0]} plays "
                    "follow it); the sequence is internally inconsistent")

    terminal = ordered[-1]
    completed_at = stamps[-1]
    if completed_at != max(stamps):
        raise _fail(f"{where}: the terminal play is not the latest instant")

    return NbaCompletionEvidence(
        provider_game_id=requested,
        raw_response_id=raw.raw_response_id,
        source_endpoint=raw.endpoint,
        provider=raw.provider,
        league_id=NBA_COMPLETION_LEAGUE,
        source_event_completed_at=to_iso(completed_at),
        terminal_play_order=int(terminal["order"]),
        terminal_play_period=int(terminal["period"]),
        play_count=len(ordered),
        policy_version=NBA_COMPLETION_POLICY_VERSION,
        classification=NBA_COMPLETION_CLASSIFICATION,
        source_content_hash=raw.content_hash,
    )


#: Every column of `raw_responses`, so a copy is provably total.
_RAW_COLUMNS: Final[tuple[str, ...]] = (
    "raw_response_id", "run_id", "provider", "endpoint", "request_params_json",
    "http_method", "http_status", "response_headers_json", "content_type",
    "requested_at", "received_at", "elapsed_ns", "body", "body_bytes",
    "body_hash", "content_hash", "created_at",
)


def materialize_completion_evidence(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    raw_response_id: str,
) -> MaterializedEvidence:
    """Copy one preserved raw response into a reconstruction database.

    The v19 evidence check resolves ``source_evidence_id`` against the SAME
    database that holds the certification, so a reconstruction corpus has to
    contain the evidence it cites. This is that copy, and nothing more.

    Every column is carried verbatim, **including ``raw_response_id``,
    ``requested_at``, ``received_at`` and ``created_at``**. The identifier is
    preserved rather than regenerated so the destination row is provably the
    same evidence rather than a look-alike; the timestamps are preserved because
    rewriting any of them -- especially substituting the derived March
    wallclock for an August ``received_at`` -- is exactly the backdating this
    lane exists to prevent.

    Idempotent: an identical existing row is reused. A row with the same id but
    different content is a genuine conflict and raises rather than overwriting.

    The caller owns the destination transaction. The source is never written to.
    """

    row = source.execute(
        f"SELECT {', '.join(_RAW_COLUMNS)} FROM raw_responses "
        "WHERE raw_response_id = ?", (raw_response_id,)).fetchone()
    if row is None:
        raise _fail(f"raw response {raw_response_id!r} does not exist in the "
                    "source corpus")
    values = tuple(row)
    by_name = dict(zip(_RAW_COLUMNS, values, strict=True))

    existing = destination.execute(
        f"SELECT {', '.join(_RAW_COLUMNS)} FROM raw_responses "
        "WHERE raw_response_id = ?", (raw_response_id,)).fetchone()
    if existing is not None:
        if tuple(existing) == values:
            return MaterializedEvidence(
                raw_response_id=raw_response_id,
                outcome=MaterializationOutcome.REUSED,
                content_hash=str(by_name["content_hash"]))
        differing = sorted(
            name for name, a, b in zip(_RAW_COLUMNS, existing, values, strict=True) if a != b)
        raise _fail(
            f"raw response {raw_response_id!r} already exists in the destination "
            f"with different content (differs in: {differing}). Refusing to "
            "overwrite preserved evidence.")

    destination.execute(
        f"INSERT INTO raw_responses ({', '.join(_RAW_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_RAW_COLUMNS))})", values)

    # Read it back through the ordinary repository so the copy is proven to be a
    # valid `raw_responses` row under the destination's own constraints, not just
    # bytes we pushed in.
    stored = SqliteRawResponseRepository(destination).get(raw_response_id)
    if stored is None:  # pragma: no cover - the insert just succeeded
        raise _fail(f"raw response {raw_response_id!r} vanished after insert")
    if stored.content_hash != by_name["content_hash"] or stored.body != by_name["body"]:
        raise _fail(f"raw response {raw_response_id!r} did not survive "
                    "materialization byte-identically")
    return MaterializedEvidence(
        raw_response_id=raw_response_id,
        outcome=MaterializationOutcome.CREATED,
        content_hash=str(by_name["content_hash"]))


def find_completion_payload(
    source: sqlite3.Connection, *, provider_game_id: str
) -> Optional[RawResponse]:
    """The single preserved ``/v1/plays`` response for one game, or fail closed.

    More than one candidate is refused rather than resolved by recency: two
    payloads for the same game could disagree about the final play, and picking
    one silently is how a corpus stops being reproducible.
    """

    rows = source.execute(
        "SELECT raw_response_id FROM raw_responses "
        "WHERE provider = ? AND endpoint = ? "
        "  AND json_extract(request_params_json, '$.game_id') = ? "
        "ORDER BY raw_response_id",
        (NBA_COMPLETION_PROVIDER, NBA_COMPLETION_ENDPOINT,
         str(provider_game_id))).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise _fail(
            f"game {provider_game_id!r} has {len(rows)} preserved "
            f"{NBA_COMPLETION_ENDPOINT} payloads "
            f"({[str(r[0]) for r in rows]}); refusing to choose between "
            "conflicting evidence")
    return SqliteRawResponseRepository(source).get(str(rows[0][0]))
