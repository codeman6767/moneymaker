"""Centralized structured identity extraction from official provider payloads.

One module, one entry point (:func:`extract_identities`), so the same provider
entity normalizes identically no matter which endpoint family observed it. That
is the whole point: before this existed the provider-written names sat only
inside ``raw_responses`` bodies, and the F1 matching pilot correctly refused
every reference because the normalized tables carried ids and no names.

Rules that hold for every extractor here:

* A name is **never** inferred from an id. No name -> no identity, and the
  omission is reported (:class:`IdentityRejection`) rather than filled in.
* Optional fields (first/last name, birth date, position, city, abbreviation,
  nickname) are emitted only when the provider genuinely supplied them. MLB
  StatsAPI sends ``fullName`` and no parts, so MLB player identities carry no
  first/last name -- splitting a full name is a guess, and a guess stored in a
  typed column is indistinguishable from a fact.
* Dispatch is fail-closed: an endpoint family this module does not recognise
  yields nothing at all rather than a best-effort recursive scrape, which is how
  a payload-shape change becomes a visible zero instead of silent garbage.
* Output ordering is total and content-derived, so replaying the same corpus in
  a different order produces the same rows in the same order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

PROVIDER_MLB = "mlb_statsapi"
PROVIDER_NBA = "balldontlie"

#: Which league a provider's observations belong to. The *designation* of which
#: provider is official for a league lives in ``matching.service`` (it is a
#: matching policy, not an extraction detail) and is deliberately not duplicated
#: here.
LEAGUE_BY_PROVIDER: dict[str, str] = {
    PROVIDER_MLB: "lg_mlb",
    PROVIDER_NBA: "lg_nba",
}


@dataclass(frozen=True)
class TeamIdentityInput:
    """A provider-written team identity, ready to record."""

    provider_team_id: str
    full_name: str
    abbreviation: Optional[str] = None
    city: Optional[str] = None
    nickname: Optional[str] = None

    def sort_key(self) -> tuple[int, int, str]:
        return _id_sort_key(self.provider_team_id)


@dataclass(frozen=True)
class PlayerIdentityInput:
    """A provider-written player identity, ready to record."""

    provider_player_id: str
    full_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[str] = None
    position: Optional[str] = None
    provider_team_id: Optional[str] = None

    def sort_key(self) -> tuple[int, int, str]:
        return _id_sort_key(self.provider_player_id)


@dataclass(frozen=True)
class IdentityRejection:
    """An identity object that carried an id but no usable provider name.

    Surfaced rather than dropped, so "the provider sent us a player with no
    name" is a reportable fact instead of a silent gap in coverage.
    """

    kind: str
    provider_entity_id: Optional[str]
    reason: str


@dataclass
class ExtractedIdentities:
    teams: list[TeamIdentityInput] = field(default_factory=list)
    players: list[PlayerIdentityInput] = field(default_factory=list)
    rejected: list[IdentityRejection] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.teams) + len(self.players)


def _id_sort_key(value: Optional[str]) -> tuple[int, int, str]:
    """A TOTAL order over provider ids: numeric where possible, else lexical.

    The exact string is the final component, so ``'1'`` and ``'01'`` -- equal as
    integers -- still order deterministically instead of colliding and letting
    input order decide.
    """

    if value is None:
        return (1, 0, "")
    try:
        return (0, int(value), str(value))
    except (TypeError, ValueError):
        return (0, 0, str(value))


def _text(value: Any) -> Optional[str]:
    """A nonempty trimmed string, or ``None``. Never coerces a number to a name."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    text = str(value).strip()
    return text or None


def _entity_id(value: Any) -> Optional[str]:
    """A provider id as a string. Accepts ints (both providers send numbers)."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text or None


def _join_name(first: Optional[str], last: Optional[str]) -> Optional[str]:
    """Compose a display name from genuinely-supplied parts only.

    This is composition, not inference: BALLDONTLIE supplies both parts and no
    combined string, so the full name is assembled from what it actually sent.
    """

    parts = [p for p in (first, last) if p]
    return " ".join(parts) if parts else None


# --------------------------------------------------------------------------- #
# MLB StatsAPI
# --------------------------------------------------------------------------- #
def mlb_team_identity(obj: Any) -> Optional[TeamIdentityInput]:
    """Extract a team identity from any MLB ``team`` object.

    Handles both the thin schedule shape ``{id, name}`` and the rich box-score
    shape, which additionally carries ``abbreviation``, ``locationName`` (city)
    and ``teamName`` (nickname).
    """

    if not isinstance(obj, dict):
        return None
    team_id = _entity_id(obj.get("id"))
    if team_id is None:
        return None
    name = _text(obj.get("name"))
    if name is None:
        return None
    return TeamIdentityInput(
        provider_team_id=team_id,
        full_name=name,
        abbreviation=_text(obj.get("abbreviation")),
        city=_text(obj.get("locationName")),
        nickname=_text(obj.get("teamName")),
    )


def mlb_player_identity(
    person: Any,
    *,
    position: Any = None,
    provider_team_id: Optional[str] = None,
) -> Optional[PlayerIdentityInput]:
    """Extract a player identity from an MLB ``person`` object.

    StatsAPI supplies ``fullName`` only, so ``first_name``/``last_name`` are left
    unset. ``position`` is a sibling of ``person`` in the box-score/roster
    shapes, and its ``abbreviation`` is used when present.
    """

    if not isinstance(person, dict):
        return None
    player_id = _entity_id(person.get("id"))
    if player_id is None:
        return None
    name = _text(person.get("fullName"))
    if name is None:
        return None
    pos = None
    if isinstance(position, dict):
        pos = _text(position.get("abbreviation")) or _text(position.get("name"))
    return PlayerIdentityInput(
        provider_player_id=player_id,
        full_name=name,
        birth_date=_text(person.get("birthDate")),
        position=pos,
        provider_team_id=provider_team_id,
    )


def _mlb_schedule(doc: Any, out: ExtractedIdentities) -> None:
    for date_entry in _as_list(_get(doc, "dates")):
        for game in _as_list(_get(date_entry, "games")):
            teams = _get(game, "teams")
            if not isinstance(teams, dict):
                continue
            for side in ("home", "away"):
                entry = teams.get(side)
                if not isinstance(entry, dict):
                    continue
                team = mlb_team_identity(entry.get("team"))
                if team is not None:
                    out.teams.append(team)
                pitcher = mlb_player_identity(
                    entry.get("probablePitcher"),
                    provider_team_id=team.provider_team_id if team else None,
                )
                if pitcher is not None:
                    out.players.append(pitcher)


def _mlb_boxscore(doc: Any, out: ExtractedIdentities) -> None:
    teams = _get(doc, "teams")
    if not isinstance(teams, dict):
        return
    for side in ("away", "home"):
        entry = teams.get(side)
        if not isinstance(entry, dict):
            continue
        team = mlb_team_identity(entry.get("team"))
        if team is not None:
            out.teams.append(team)
        players = entry.get("players")
        if not isinstance(players, dict):
            continue
        # Deterministic traversal: the provider's dict keys ("ID670764") are
        # sorted so a different JSON key order cannot reorder the output.
        for key in sorted(players):
            slot = players[key]
            if not isinstance(slot, dict):
                out.rejected.append(IdentityRejection(
                    "player", None, f"box-score slot {key!r} is not an object"))
                continue
            person = slot.get("person")
            team_pid = _entity_id(slot.get("parentTeamId")) or (
                team.provider_team_id if team else None)
            identity = mlb_player_identity(
                person, position=slot.get("position"), provider_team_id=team_pid)
            if identity is not None:
                out.players.append(identity)
            else:
                pid = _entity_id(_get(person, "id"))
                out.rejected.append(IdentityRejection(
                    "player", pid, "box-score person carries no usable fullName"))


def _mlb_linescore(doc: Any, out: ExtractedIdentities) -> None:
    # `defense` / `offense` carry current on-field people. There is no team
    # object here (linescore teams hold runs/hits/errors only), so no team
    # identity is claimed from this family.
    for section in ("defense", "offense"):
        block = _get(doc, section)
        if not isinstance(block, dict):
            continue
        for role in sorted(block):
            person = block[role]
            identity = mlb_player_identity(person)
            if identity is not None:
                out.players.append(identity)


def _mlb_roster(doc: Any, out: ExtractedIdentities) -> None:
    default_team = _entity_id(_get(doc, "teamId"))
    for entry in _as_list(_get(doc, "roster")):
        if not isinstance(entry, dict):
            continue
        team_pid = _entity_id(entry.get("parentTeamId")) or default_team
        identity = mlb_player_identity(
            entry.get("person"), position=entry.get("position"), provider_team_id=team_pid)
        if identity is not None:
            out.players.append(identity)
        else:
            pid = _entity_id(_get(entry.get("person"), "id"))
            out.rejected.append(IdentityRejection(
                "player", pid, "roster person carries no usable fullName"))


# --------------------------------------------------------------------------- #
# BALLDONTLIE
# --------------------------------------------------------------------------- #
def nba_team_identity(obj: Any) -> Optional[TeamIdentityInput]:
    """Extract a team identity from a BALLDONTLIE ``team`` object.

    BALLDONTLIE supplies ``full_name``, ``abbreviation``, ``city`` and ``name``
    (the nickname) on every team object it emits.
    """

    if not isinstance(obj, dict):
        return None
    team_id = _entity_id(obj.get("id"))
    if team_id is None:
        return None
    name = _text(obj.get("full_name"))
    if name is None:
        return None
    return TeamIdentityInput(
        provider_team_id=team_id,
        full_name=name,
        abbreviation=_text(obj.get("abbreviation")),
        city=_text(obj.get("city")),
        nickname=_text(obj.get("name")),
    )


def nba_player_identity(
    obj: Any, *, provider_team_id: Optional[str] = None
) -> Optional[PlayerIdentityInput]:
    """Extract a player identity from a BALLDONTLIE ``player`` object.

    Both name parts are genuinely supplied, so they are preserved AND composed
    into the display name. ``birth_date`` is never supplied by this provider and
    is therefore never set.
    """

    if not isinstance(obj, dict):
        return None
    player_id = _entity_id(obj.get("id"))
    if player_id is None:
        return None
    first, last = _text(obj.get("first_name")), _text(obj.get("last_name"))
    name = _join_name(first, last)
    if name is None:
        return None
    return PlayerIdentityInput(
        provider_player_id=player_id,
        full_name=name,
        first_name=first,
        last_name=last,
        position=_text(obj.get("position")),
        provider_team_id=_entity_id(obj.get("team_id")) or provider_team_id,
    )


def _nba_rows(doc: Any) -> list[Any]:
    """``data`` as a list. A single-entity endpoint returns one object."""

    data = _get(doc, "data")
    if isinstance(data, list):
        return list(data)
    return [data] if isinstance(data, dict) else []


def _nba_games(doc: Any, out: ExtractedIdentities) -> None:
    for row in _nba_rows(doc):
        for key in ("home_team", "visitor_team"):
            team = nba_team_identity(_get(row, key))
            if team is not None:
                out.teams.append(team)


def _nba_box_scores(doc: Any, out: ExtractedIdentities) -> None:
    for row in _nba_rows(doc):
        for key in ("home_team", "visitor_team"):
            block = _get(row, key)
            team = nba_team_identity(block)
            if team is not None:
                out.teams.append(team)
            for slot in _as_list(_get(block, "players")):
                identity = nba_player_identity(
                    _get(slot, "player"),
                    provider_team_id=team.provider_team_id if team else None)
                if identity is not None:
                    out.players.append(identity)
                else:
                    pid = _entity_id(_get(_get(slot, "player"), "id"))
                    out.rejected.append(IdentityRejection(
                        "player", pid, "box-score player carries no usable name parts"))


def _nba_player_rows(doc: Any, out: ExtractedIdentities) -> None:
    """``/v1/stats``, ``/nba/v1/stats/advanced`` and ``/v1/lineups`` share a shape.

    Each row carries a ``player`` object and a sibling ``team`` object, so both
    identities come from the same row and the player's team context is exact.
    """

    for row in _nba_rows(doc):
        team = nba_team_identity(_get(row, "team"))
        if team is not None:
            out.teams.append(team)
        player_obj = _get(row, "player")
        if player_obj is None:
            continue
        identity = nba_player_identity(
            player_obj, provider_team_id=team.provider_team_id if team else None)
        if identity is not None:
            out.players.append(identity)
        else:
            out.rejected.append(IdentityRejection(
                "player", _entity_id(_get(player_obj, "id")),
                "row player carries no usable name parts"))


def _nba_plays(doc: Any, out: ExtractedIdentities) -> None:
    # Plays carry a full team object but only bare integer `participants`, so
    # there is no player identity object to extract here. Recording an id
    # without a name would be exactly the fabrication this module forbids.
    for row in _nba_rows(doc):
        team = nba_team_identity(_get(row, "team"))
        if team is not None:
            out.teams.append(team)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_MLB_FAMILIES: tuple[tuple[re.Pattern[str], Any], ...] = (
    (re.compile(r"^/schedule\b"), _mlb_schedule),
    (re.compile(r"^/game/[^/]+/boxscore$"), _mlb_boxscore),
    (re.compile(r"^/game/[^/]+/linescore$"), _mlb_linescore),
    (re.compile(r"^/teams/[^/]+/roster$"), _mlb_roster),
)
_NBA_FAMILIES: tuple[tuple[re.Pattern[str], Any], ...] = (
    (re.compile(r"^/v1/games(/[^/]+)?$"), _nba_games),
    (re.compile(r"^/v1/box_scores\b"), _nba_box_scores),
    (re.compile(r"^/v1/stats$"), _nba_player_rows),
    (re.compile(r"^/nba/v1/stats/advanced$"), _nba_player_rows),
    (re.compile(r"^/v1/lineups$"), _nba_player_rows),
    (re.compile(r"^/v1/plays$"), _nba_plays),
)
_DISPATCH: dict[str, tuple[tuple[re.Pattern[str], Any], ...]] = {
    PROVIDER_MLB: _MLB_FAMILIES,
    PROVIDER_NBA: _NBA_FAMILIES,
}


def _get(node: Any, key: str) -> Any:
    return node.get(key) if isinstance(node, dict) else None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def endpoint_is_supported(provider: str, endpoint: str) -> bool:
    """Whether identity extraction understands this endpoint family."""

    return any(rx.match(endpoint) for rx, _fn in _DISPATCH.get(provider, ()))


def extract_identities(*, provider: str, endpoint: str, body: str) -> ExtractedIdentities:
    """Extract every structured identity a supported payload genuinely contains.

    Deduplicates within one response by ``(kind, provider id)``, keeping the
    richest observation (most non-null optional fields) so a thin schedule entry
    cannot mask the box score's abbreviation/city/nickname in the same document.
    Ties break on the sorted field values, so the choice never depends on
    traversal order. Output is sorted by the total provider-id order.

    A malformed body is reported, not raised: one unparseable response must not
    abort a whole ingestion run.
    """

    out = ExtractedIdentities()
    families = _DISPATCH.get(provider)
    if not families:
        out.rejected.append(IdentityRejection(
            "provider", None, f"identity extraction is not implemented for {provider!r}"))
        return out
    handler = next((fn for rx, fn in families if rx.match(endpoint)), None)
    if handler is None:
        out.rejected.append(IdentityRejection(
            "endpoint", None, f"no identity extractor for {provider} endpoint {endpoint!r}"))
        return out
    try:
        doc = json.loads(body)
    except (TypeError, ValueError) as exc:
        out.rejected.append(IdentityRejection(
            "body", None, f"response body is not valid JSON: {type(exc).__name__}"))
        return out
    handler(doc, out)
    out.teams = _dedupe(out.teams, lambda t: t.provider_team_id)
    out.players = _dedupe(out.players, lambda p: p.provider_player_id)
    return out


def _richness(item: Any) -> tuple[int, tuple[str, ...]]:
    """How much a candidate actually says, plus a deterministic tie-break."""

    values = tuple(
        "" if v is None else str(v)
        for k, v in sorted(vars(item).items())
    )
    filled = sum(1 for v in vars(item).values() if v is not None)
    return (filled, values)


def _dedupe(items: Iterable[Any], key: Any) -> list[Any]:
    best: dict[str, Any] = {}
    for item in items:
        k = key(item)
        current = best.get(k)
        if current is None or _richness(item) > _richness(current):
            best[k] = item
    return sorted(best.values(), key=lambda i: i.sort_key())
