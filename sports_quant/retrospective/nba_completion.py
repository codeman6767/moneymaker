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
    "CompletionVerificationReport",
    "verify_completion_certifications",
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
        # `isinstance(True, int)` is True in Python, and True would sort as 1 --
        # silently reordering the sequence. Booleans are refused explicitly.
        if isinstance(order, bool) or not isinstance(order, int):
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

    # NOTE (independent review, defect R1): a global period-monotonicity gate
    # used to live here. It was removed because the preserved evidence does not
    # support it. Real game 18447743 visits periods out of order in the MIDDLE of
    # its `order` sequence while its terminal play is corroborated three
    # independent ways, and the gate rejected it -- shrinking the corpus for an
    # assumption about provider `order` semantics that nothing evidences.
    #
    # What actually distinguishes a truncated feed from a merely disordered one
    # is whether the sequence ENDS where it claims to, which is checked below.

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

    # ---- 7. the sequence must END where it claims to ----------------------- #
    # The discriminator the review identified. A truncated or stitched feed can
    # still carry an `End Game` marker at maximum order and maximum wallclock --
    # real games 18447741 and 18447742 both do -- but its terminal play reports a
    # score LOWER than plays it supposedly follows. A game's score never
    # decreases, so a terminal play below the payload's own maximum is proof the
    # recorded sequence is not the whole game.
    #
    # Deliberately compared WITHIN the payload rather than against `/v1/games`:
    # real game 18447470's play feed disagrees with the game object by 3 points,
    # which is a scoring-feed discrepancy and says nothing about when the game
    # ended. Gating on it would reject sound completion evidence.
    scores = [(p.get("home_score"), p.get("away_score")) for p in ordered]
    if any(h is None or a is None for h, a in scores):
        raise _fail(f"{where}: a play carries no score, so the terminal play "
                    "cannot be corroborated")
    highest = (max(h for h, _ in scores), max(a for _, a in scores))
    if (terminal.get("home_score"), terminal.get("away_score")) != highest:
        raise _fail(
            f"{where}: the terminal play reports score "
            f"{(terminal.get('home_score'), terminal.get('away_score'))} but the "
            f"payload reaches {highest}; the recorded sequence does not end "
            "where it claims to, so its last play is not the game's final play")

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


@dataclass(frozen=True)
class CompletionVerificationReport:
    """Every certification whose completion instant does not re-derive."""

    checked: int = 0
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, object]:
        return {
            "policy_version": NBA_COMPLETION_POLICY_VERSION,
            "certifications_checked": self.checked,
            "problems": list(self.problems),
            "ok": self.ok,
        }


def verify_completion_certifications(
    conn: sqlite3.Connection, *, corpus_version_id: Optional[str] = None
) -> CompletionVerificationReport:
    """Re-derive every stored NBA completion instant from its own evidence.

    Independent review repair (R4). ``availability_source`` is a free-text
    LOCATOR by design -- the architecture asks for "a pointer to the documenting
    evidence" and f018 calls the column "a stable citation key" -- so unlike
    ``availability_rule_digest`` it is deliberately not digest-bound, and
    nothing should claim it is.

    The consequence is that a stored ``source_event_completed_at`` was checked by
    nothing at all: a certification could name this policy, cite real preserved
    evidence, and still carry an instant that evidence does not produce. The
    reader admits it, correctly, because the reader decides availability rather
    than re-deriving evidence.

    This closes that loop the same way the TEAM-A verifier does -- as a
    **detective** control. It is weaker than a DB constraint and is stated as
    such: direct SQL can still write a wrong instant; what it cannot do is
    survive verification.
    """

    where = "WHERE rip.availability_basis = 'event_derived' "             "AND rip.source_evidence_table = 'raw_responses' "             "AND rip.source_event_completed_at IS NOT NULL"
    params: tuple[str, ...] = ()
    if corpus_version_id is not None:
        where += " AND rip.corpus_version_id = ?"
        params = (corpus_version_id,)

    rows = conn.execute(
        "SELECT rip.input_provenance_id, rip.corpus_version_id, "
        "       rip.provider_game_id, rip.source_evidence_id, "
        "       rip.source_event_completed_at, rip.availability_source "
        f"FROM reconstructed_input_provenance AS rip {where} "
        "ORDER BY rip.input_provenance_id", params).fetchall()

    repo = SqliteRawResponseRepository(conn)
    problems: list[str] = []
    checked = 0
    for row in rows:
        (input_id, _corpus, game_id, evidence_id, stored_at,
         source_text) = (str(row[0]), str(row[1]), str(row[2]),
                         row[3], str(row[4]), row[5] or "")
        # Only certifications written under THIS policy are re-derivable here.
        if NBA_COMPLETION_POLICY_VERSION not in source_text:
            continue
        checked += 1
        if evidence_id is None:
            problems.append(
                f"certification {input_id} cites this policy but names no "
                "source evidence row")
            continue
        raw = repo.get(str(evidence_id))
        if raw is None:
            problems.append(
                f"certification {input_id} cites evidence "
                f"{str(evidence_id)!r} which no longer exists in this database")
            continue
        try:
            evidence = derive_completion_evidence(raw)
        except CompletionEvidenceError as exc:
            problems.append(
                f"certification {input_id} cites evidence {str(evidence_id)!r} "
                f"that no longer yields a completion instant: {exc}")
            continue
        if evidence.source_event_completed_at != stored_at:
            problems.append(
                f"certification {input_id} stores completion {stored_at}, but "
                f"its cited evidence re-derives to "
                f"{evidence.source_event_completed_at}; the stored instant does "
                "not match the evidence it claims to come from")
        # NOT an equality check. An EVENT_DERIVED certification is for a TARGET
        # game and its evidence comes from a PRIOR one, so these differing is
        # the normal case; them MATCHING is the leak the reader already refuses
        # as `target_game_self_reference`. Verified here too, because this
        # verifier is also runnable standalone.
        if evidence.provider_game_id == game_id:
            problems.append(
                f"certification {input_id} for target game {game_id!r} cites "
                "completion evidence from that same game; a game cannot be a "
                "prior event for itself")
    return CompletionVerificationReport(checked=checked,
                                        problems=tuple(problems))
