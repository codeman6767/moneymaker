"""`official-listing-projection-v1`: preserved listing evidence -> target members.

The architecture originally described this as "projecting raw listing responses
through existing official normalization." The independent review proved that is
not a pure projection: ``games.game_id`` is a random ULID (``db/ids.py`` uses a
surrogate precisely because a game's natural key can change), so no provider
payload yields one. Resolution must go through ``provider_game_references``,
whose ``game_id`` is NULLABLE and whose row is MUTABLE.

This module therefore does three separable things, each of which can fail closed:

1. **Admission** -- exactly which preserved responses count as official listing
   evidence for a bound acquisition.
2. **Cursor-chain closure** -- whether that evidence is a COMPLETE listing
   acquisition, derived from the evidence itself rather than trusted from a
   runtime report.
3. **Projection** -- provider game object -> canonical member, refusing (never
   dropping) anything unresolved.

Nothing here reads results, markets, eligibility or audits: membership must not
depend on downstream success, or the denominator becomes the numerator.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from ..db.repositories.raw_responses import response_content_hash

__all__ = [
    "LISTING_PROJECTION_POLICY_V1",
    "SUPPORTED_LISTING_PROJECTION_POLICIES",
    "BALLDONTLIE_PROVIDER",
    "NBA_GAMES_LISTING_ENDPOINT",
    "ListingProjectionError",
    "ListingChain",
    "ProjectionResult",
    "admitted_listing_responses",
    "verify_response_integrity",
    "verify_cursor_chain",
    "project_targets",
]

LISTING_PROJECTION_POLICY_V1: Final = "official-listing-projection-v1"
SUPPORTED_LISTING_PROJECTION_POLICIES: Final = frozenset(
    {LISTING_PROJECTION_POLICY_V1})

BALLDONTLIE_PROVIDER: Final = "balldontlie"
#: EXACT equality, never a prefix or substring. `/v1/games/18447469` is a
#: single-game fetch, not listing evidence, and `raw_responses` stores the
#: endpoint separately from the query string (a CHECK forbids `?` in it), so
#: exact comparison is available and no pattern matching is needed.
NBA_GAMES_LISTING_ENDPOINT: Final = "/v1/games"


class ListingProjectionError(RuntimeError):
    """Listing evidence is inadmissible, incomplete, or unprojectable."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse duplicate JSON keys in a preserved provider body (RV-4).

    A body carrying `"meta"` twice would otherwise parse last-value-wins, so a
    truncated page could be made to look terminal (or vice versa) by an
    appended key that a human reading the payload never sees.
    """

    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ListingProjectionError(
                f"preserved listing body contains the duplicate key {key!r}")
        seen[key] = value
    return seen


def _reject_non_standard(value: str) -> float:
    raise ListingProjectionError(
        f"preserved listing body contains the non-standard JSON constant {value!r}")


@dataclass(frozen=True)
class ListingChain:
    """One verified cursor chain over the admitted listing responses."""

    response_ids: tuple[str, ...]
    provider_game_ids: tuple[str, ...]
    pages: int
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class ProjectionResult:
    """The canonical member set derived from a verified chain."""

    members: tuple[str, ...]
    provider_game_ids: tuple[str, ...]
    pages: int
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems


def _require_policy(policy_version: str) -> None:
    if policy_version not in SUPPORTED_LISTING_PROJECTION_POLICIES:
        raise ListingProjectionError(
            f"unknown listing-projection policy {policy_version!r}; supported: "
            f"{sorted(SUPPORTED_LISTING_PROJECTION_POLICIES)}")


def admitted_listing_responses(
    conn: sqlite3.Connection,
    *,
    run_ids: Collection[str],
    provider: str = BALLDONTLIE_PROVIDER,
    endpoint: str = NBA_GAMES_LISTING_ENDPOINT,
) -> list[sqlite3.Row]:
    """Exactly the official listing responses belonging to the bound runs.

    Admission is by EXACT equality on provider, endpoint and HTTP status, scoped
    to the bound run set. Stats, box scores, advanced stats, plays, lineups and
    single-game `/v1/games/{id}` fetches are excluded because their endpoint is
    a different string -- not because of a substring rule that a new endpoint
    could slip past.

    A non-200 response is NOT treated as an empty page: it is excluded from the
    chain entirely, so a failed request can never masquerade as "the provider
    returned no games".
    """

    if not run_ids:
        raise ListingProjectionError(
            "no acquisition runs are bound; listing evidence cannot be scoped")
    ordered = sorted(set(run_ids))
    placeholders = ",".join("?" for _ in ordered)
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT raw_response_id, run_id, provider, endpoint, http_status, "
            "       request_params_json, body, body_hash, content_hash "
            "FROM raw_responses "
            f"WHERE provider = ? AND endpoint = ? AND run_id IN ({placeholders}) "
            "ORDER BY raw_response_id",
            (provider, endpoint, *ordered)).fetchall()
    finally:
        conn.row_factory = prior_factory
    return [r for r in rows if int(r["http_status"]) == 200]


def verify_response_integrity(rows: Iterable[sqlite3.Row]) -> tuple[str, ...]:
    """Recompute `body_hash` and `content_hash` from the stored bytes.

    Independent review defect RV-1. `scoped_source_digest` fingerprints the
    STORED `content_hash`, so a forged body left with its original hashes
    produced an unchanged source digest -- the tamper was caught only
    indirectly, when derived membership happened to differ from stored
    membership. A forgery that changes nothing about the member set would have
    passed silently. Provenance must be recomputed, never trusted.

    `content_hash` is sha256 over (provider, endpoint, params, body), so this
    also detects a rewritten endpoint, provider or request parameter.
    """

    problems: list[str] = []
    for row in rows:
        rid = str(row["raw_response_id"])
        body = str(row["body"])
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != str(row["body_hash"]):
            problems.append(
                f"listing response {rid} body does not match its stored body_hash")
        try:
            params = json.loads(str(row["request_params_json"]))
        except json.JSONDecodeError:
            problems.append(f"listing response {rid} has unreadable request params")
            continue
        recomputed = response_content_hash(
            provider=str(row["provider"]), endpoint=str(row["endpoint"]),
            request_params=params, body=body)
        if recomputed != str(row["content_hash"]):
            problems.append(
                f"listing response {rid} provider/endpoint/params/body do not match "
                f"its stored content_hash")
    return tuple(problems)


def _parse_body(row: sqlite3.Row) -> dict[str, Any]:
    try:
        body = json.loads(row["body"], object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=_reject_non_standard)
    except json.JSONDecodeError as exc:
        raise ListingProjectionError(
            f"listing response {row['raw_response_id']} body is not valid JSON: "
            f"{exc}") from None
    if not isinstance(body, dict):
        raise ListingProjectionError(
            f"listing response {row['raw_response_id']} body is not a JSON object")
    return body


def _request_cursor(row: sqlite3.Row) -> Optional[str]:
    """The cursor this request was issued with, as preserved text, or None.

    `cursor` is not a sensitive parameter name, so it survives sanitization into
    `request_params_json`. Values are compared as TEXT because that is how the
    preserved params store them; a numeric comparison would let `25` and `"25"`
    disagree about whether the chain closes.
    """

    try:
        params = json.loads(row["request_params_json"])
    except json.JSONDecodeError as exc:
        raise ListingProjectionError(
            f"listing response {row['raw_response_id']} has unreadable request "
            f"params: {exc}") from None
    if not isinstance(params, dict):
        raise ListingProjectionError(
            f"listing response {row['raw_response_id']} request params are not an "
            f"object")
    raw = params.get("cursor")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ListingProjectionError(
            f"listing response {row['raw_response_id']} has a malformed cursor "
            f"{raw!r}")
    return str(raw)


def _next_cursor(body: dict[str, Any], response_id: str) -> Optional[str]:
    """`meta.next_cursor`, requiring `meta` to be PRESENT on every page.

    Independent review defect RV-2. Treating a missing `meta` as a terminus made
    a truncated body indistinguishable from a real last page, and the documented
    mitigation -- the manifest cap proof -- closes nothing when the caps are far
    from binding: a single 100-game page with no `meta` certified as a complete
    population under caps of 8 pages / 1000 records / 400 games.

    Requiring `meta` is safe against the preserved March evidence, where all
    three pages carry it including the terminal page whose `next_cursor` is
    `null`. An explicit `"next_cursor": null` still terminates the chain; only a
    body that omits the pagination envelope entirely is refused.
    """

    if "meta" not in body:
        raise ListingProjectionError(
            f"listing response {response_id} has no `meta` pagination envelope, so "
            f"a truncated body cannot be distinguished from a real last page")
    meta = body.get("meta")
    if meta is None:
        raise ListingProjectionError(
            f"listing response {response_id} has a null `meta`")
    if not isinstance(meta, dict):
        raise ListingProjectionError(
            f"listing response {response_id} has a non-object meta")
    raw = meta.get("next_cursor")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ListingProjectionError(
            f"listing response {response_id} has a malformed next_cursor {raw!r}")
    return str(raw)


def _provider_game_ids(body: dict[str, Any], response_id: str) -> list[str]:
    data = body.get("data")
    if not isinstance(data, list):
        raise ListingProjectionError(
            f"listing response {response_id} has no `data` array")
    ids: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ListingProjectionError(
                f"listing response {response_id} contains a non-object game")
        raw = entry.get("id")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ListingProjectionError(
                f"listing response {response_id} contains a game with a malformed "
                f"id {raw!r}")
        ids.append(str(raw))
    return ids


def verify_cursor_chain(
    rows: list[sqlite3.Row],
    *,
    policy_version: str = LISTING_PROJECTION_POLICY_V1,
) -> ListingChain:
    """Re-derive the pagination chain from preserved evidence alone.

    Detects a truncated tail, a missing middle page, a duplicate page, an
    unreachable orphan page and a cursor cycle.

    **Retained limitation, stated plainly.** A response body that omits `meta`
    terminates the chain and is indistinguishable from a genuine last page. This
    is not cryptographically detectable: the acquisition itself would have
    stopped at the same point, and `body_hash`/`content_hash` cover tampering but
    not provider-side truncation. The mitigation lives one layer up, in the
    manifest's precommitted coverage assertion and its cap proof.
    """

    _require_policy(policy_version)
    if not rows:
        return ListingChain((), (), 0, ("no admitted listing responses",))

    problems: list[str] = []
    by_cursor: dict[Optional[str], sqlite3.Row] = {}
    for row in rows:
        requested = _request_cursor(row)
        if requested in by_cursor:
            problems.append(
                f"duplicate listing page for cursor {requested!r} "
                f"({by_cursor[requested]['raw_response_id']} and "
                f"{row['raw_response_id']})")
            continue
        by_cursor[requested] = row

    if None not in by_cursor:
        problems.append("no first listing page (no cursor-less request preserved)")
        return ListingChain((), (), 0, tuple(problems))

    ordered: list[sqlite3.Row] = []
    provider_games: list[str] = []
    seen: set[Optional[str]] = set()
    cursor: Optional[str] = None
    while True:
        page = by_cursor.get(cursor)
        if page is None:
            problems.append(
                f"listing chain breaks: no preserved page for cursor {cursor!r} "
                f"(the acquisition was truncated or a page is missing)")
            break
        seen.add(cursor)
        ordered.append(page)
        body = _parse_body(page)
        provider_games.extend(_provider_game_ids(body, str(page["raw_response_id"])))
        nxt = _next_cursor(body, str(page["raw_response_id"]))
        if nxt is None:
            break  # the provider says this is the last page
        if nxt in seen:
            problems.append(f"listing chain cycles at cursor {nxt!r}")
            break
        cursor = nxt

    unreached = set(by_cursor) - seen
    if unreached:
        problems.append(
            "listing pages are not reachable from the chain: "
            + ", ".join(sorted(repr(c) for c in unreached)))

    return ListingChain(
        response_ids=tuple(str(r["raw_response_id"]) for r in ordered),
        provider_game_ids=tuple(provider_games),
        pages=len(ordered),
        problems=tuple(problems))


def project_targets(
    conn: sqlite3.Connection,
    *,
    chain: ListingChain,
    league_id: str,
    provider: str = BALLDONTLIE_PROVIDER,
    policy_version: str = LISTING_PROJECTION_POLICY_V1,
) -> ProjectionResult:
    """Provider game objects -> exact canonical member set.

    Repeated evidence for one provider game (the same id on several pages, or in
    several bound runs) is legitimate and collapses HERE, at the projection
    layer, to a single canonical member -- so two semantically identical
    acquisitions cannot produce two digests. That is different from the digest
    APIs, which refuse duplicate MEMBER input outright.

    Every failure mode refuses. Nothing is ever silently dropped: a dropped
    unresolved game is precisely how a target population quietly shrinks.
    """

    _require_policy(policy_version)
    if not chain.ok:
        return ProjectionResult((), (), chain.pages, chain.problems)

    problems: list[str] = []
    members: list[str] = []
    seen_provider: set[str] = set()
    ordered_provider: list[str] = []

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for provider_game_id in chain.provider_game_ids:
            if provider_game_id in seen_provider:
                continue  # repeated evidence for one game: one target
            seen_provider.add(provider_game_id)
            ordered_provider.append(provider_game_id)

            refs = conn.execute(
                "SELECT reference_id, game_id FROM provider_game_references "
                "WHERE provider = ? AND provider_game_id = ?",
                (provider, provider_game_id)).fetchall()
            if not refs:
                problems.append(
                    f"provider game {provider_game_id!r} has no "
                    f"provider_game_references row")
                continue
            if len(refs) > 1:  # pragma: no cover - UNIQUE(provider, id) forbids it
                problems.append(
                    f"provider game {provider_game_id!r} resolves to "
                    f"{len(refs)} references")
                continue
            game_id = refs[0]["game_id"]
            if game_id is None:
                problems.append(
                    f"provider game {provider_game_id!r} has an UNRESOLVED "
                    f"canonical identity (reference game_id is NULL)")
                continue
            game = conn.execute(
                "SELECT game_id, league_id FROM games WHERE game_id = ?",
                (game_id,)).fetchone()
            if game is None:
                problems.append(
                    f"provider game {provider_game_id!r} resolves to {game_id!r}, "
                    f"which has no games row")
                continue
            if game["league_id"] != league_id:
                problems.append(
                    f"provider game {provider_game_id!r} resolves to a game in "
                    f"league {game['league_id']!r}, not {league_id!r}")
                continue
            members.append(str(game_id))
    finally:
        conn.row_factory = prior_factory

    if len(set(members)) != len(members):
        problems.append(
            "two distinct provider games resolve to the same canonical game")

    return ProjectionResult(
        members=tuple(sorted(set(members))),
        provider_game_ids=tuple(ordered_provider),
        pages=chain.pages,
        problems=tuple(problems))
