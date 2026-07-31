"""Append-only structured provider identity observations (e017, Phase F1).

The F1 matching pilot returned 0% because provider-written names existed only
inside ``raw_responses`` bodies: the normalized tables carried provider ids and
no names, so ``TeamResolver`` was handed the numeric id ``'141'`` and correctly
refused, and the canonical player registry was empty. This repository is where
ingestion lands the names it already receives, so matching can read a typed,
auditable row instead of parsing raw JSON.

``content_hash`` covers the semantic fields and excludes ``observed_at``; the
uniqueness key is ``(provider, entity id, observed_at, content_hash)`` -- one row
per (observation time, content). Keying on the content hash alone would
deduplicate *states* instead of *observations*, and the surviving row would keep
whichever ``observed_at`` was written first, making every later "latest identity
as of T" answer depend on raw-response processing order. Migration a003 already
fixed exactly this mistake in ``game_status_history``.

What holds as a result:

* **Idempotent replay.** Re-ingesting or replaying the same response inserts
  nothing: both the time and the content repeat.
* **Order independence.** The final row set is a pure function of the input
  observations, whatever order they arrive in.
* **Real history.** A changed provider-written name appends; an unchanged
  identity seen later by another endpoint family appends one honest "still
  called this, at this later time" row. Nothing is ever rewritten (the e017
  triggers refuse UPDATE and DELETE outright).

``latest_team`` / ``latest_player`` order by ``observed_at DESC, content_hash
DESC``. The hash is a *total* tie-break, so an equal-timestamp conflict resolves
by a stable property of the data rather than by insertion order or rowid --
which is what makes the replay-order determinism tests meaningful. Such a
conflict is also reported (:meth:`equal_time_conflicts`) rather than hidden, so
the caller can raise a DQ issue instead of silently preferring one name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..ids import new_provider_player_identity_id, new_provider_team_identity_id
from ..models import ProviderPlayerIdentity, ProviderTeamIdentity
from ..normalize import NO_SUFFIX, normalize_name
from ..schema import utc_now_iso
from .base import Repository, RepositoryError
from .observations import ObservationOutcome, observation_content_hash

_TEAM_TABLE = "provider_team_identity_snapshots"
_PLAYER_TABLE = "provider_player_identity_snapshots"

#: Ordering used everywhere "the latest identity" is selected. The content hash
#: is a total tie-break, so equal timestamps never fall back to rowid order.
_LATEST_ORDER = "ORDER BY observed_at DESC, content_hash DESC"


@dataclass(frozen=True)
class EqualTimeConflict:
    """Two different identity contents recorded at the same ``observed_at``."""

    provider: str
    provider_entity_id: str
    observed_at: str
    contents: int


@dataclass(frozen=True)
class PreparedTeamIdentity:
    """A validated, normalized, hashed team identity, ready to insert or count."""

    provider: str
    provider_team_id: str
    league_id: str
    full_name: str
    normalized_name: str
    abbreviation: Optional[str]
    city: Optional[str]
    nickname: Optional[str]
    content_hash: str


@dataclass(frozen=True)
class PreparedPlayerIdentity:
    """A validated, normalized, hashed player identity, ready to insert or count."""

    provider: str
    provider_player_id: str
    league_id: str
    full_name: str
    normalized_name: str
    suffix: str
    first_name: Optional[str]
    last_name: Optional[str]
    birth_date: Optional[str]
    position: Optional[str]
    provider_team_id: Optional[str]
    content_hash: str


def _clean(value: Optional[str]) -> Optional[str]:
    """Trim an optional provider string; empty becomes ``None``, never ``''``.

    A provider that sends ``""`` for a city has not supplied a city. Storing the
    empty string would make "not supplied" indistinguishable from "supplied as
    blank" and would trip the e017 nonempty CHECKs.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def prepare_team_identity(
    *,
    provider: str,
    provider_team_id: str,
    league_id: str,
    full_name: str,
    abbreviation: Optional[str] = None,
    city: Optional[str] = None,
    nickname: Optional[str] = None,
) -> PreparedTeamIdentity:
    """Validate, normalize and hash one team identity.

    Shared by :meth:`SqliteProviderIdentityRepository.record_team` and by the
    ingestors' dry-run counters, so a dry run cannot report a different number of
    would-be observations than a real run writes.

    Raises :class:`RepositoryError` on an empty or unnormalizable name rather
    than storing a nameless observation: a name is the entire point of the row,
    and an empty one would later look like evidence while proving nothing.
    """

    name = _clean(full_name)
    if name is None:
        raise RepositoryError(
            f"team identity for {provider}:{provider_team_id} has an empty full_name; "
            "a provider name is never inferred from the provider id"
        )
    normalized = normalize_name(name, extract_suffix=False).normalized
    if not normalized:
        raise RepositoryError(
            f"team identity {name!r} for {provider}:{provider_team_id} normalizes to "
            "an empty string; refusing to store an unmatched-by-construction alias"
        )
    abbrev, town, nick = _clean(abbreviation), _clean(city), _clean(nickname)
    return PreparedTeamIdentity(
        provider=provider, provider_team_id=provider_team_id, league_id=league_id,
        full_name=name, normalized_name=normalized, abbreviation=abbrev, city=town,
        nickname=nick,
        # observed_at is deliberately NOT hashed: that is what makes a repeat
        # observation idempotent and a genuine rename append.
        content_hash=observation_content_hash({
            "kind": "team", "provider": provider, "provider_team_id": provider_team_id,
            "league_id": league_id, "full_name": name, "normalized_name": normalized,
            "abbreviation": abbrev, "city": town, "nickname": nick,
        }),
    )


def prepare_player_identity(
    *,
    provider: str,
    provider_player_id: str,
    league_id: str,
    full_name: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    birth_date: Optional[str] = None,
    position: Optional[str] = None,
    provider_team_id: Optional[str] = None,
) -> PreparedPlayerIdentity:
    """Validate, normalize (including suffix split) and hash one player identity."""

    name = _clean(full_name)
    if name is None:
        raise RepositoryError(
            f"player identity for {provider}:{provider_player_id} has an empty "
            "full_name; a provider name is never inferred from the provider id"
        )
    parsed = normalize_name(name)
    if not parsed.normalized:
        raise RepositoryError(
            f"player identity {name!r} for {provider}:{provider_player_id} normalizes "
            "to an empty string; refusing to store an unmatchable alias"
        )
    first, last = _clean(first_name), _clean(last_name)
    birth, pos = _clean(birth_date), _clean(position)
    team = _clean(provider_team_id)
    return PreparedPlayerIdentity(
        provider=provider, provider_player_id=provider_player_id, league_id=league_id,
        full_name=name, normalized_name=parsed.normalized,
        suffix=parsed.suffix or NO_SUFFIX, first_name=first, last_name=last,
        birth_date=birth, position=pos, provider_team_id=team,
        content_hash=observation_content_hash({
            "kind": "player", "provider": provider,
            "provider_player_id": provider_player_id, "league_id": league_id,
            "full_name": name, "normalized_name": parsed.normalized,
            "suffix": parsed.suffix, "first_name": first, "last_name": last,
            "birth_date": birth, "position": pos, "provider_team_id": team,
        }),
    )


class SqliteProviderIdentityRepository(Repository):
    """Storage for the two provider identity-observation tables."""

    # -- teams --------------------------------------------------------------- #
    def record_team(
        self,
        *,
        provider: str,
        provider_team_id: str,
        league_id: str,
        full_name: str,
        observed_at: str,
        raw_response_id: str,
        raw_response_hash: str,
        abbreviation: Optional[str] = None,
        city: Optional[str] = None,
        nickname: Optional[str] = None,
    ) -> tuple[ProviderTeamIdentity, ObservationOutcome]:
        """Append one team identity observation, or no-op on identical content."""

        prepared = prepare_team_identity(
            provider=provider, provider_team_id=provider_team_id, league_id=league_id,
            full_name=full_name, abbreviation=abbreviation, city=city, nickname=nickname,
        )
        name, normalized = prepared.full_name, prepared.normalized_name
        abbrev, town, nick = prepared.abbreviation, prepared.city, prepared.nickname
        content_hash = prepared.content_hash
        existing = self._fetch_one(
            f"SELECT * FROM {_TEAM_TABLE} WHERE provider = ? AND provider_team_id = ? "
            "AND observed_at = ? AND content_hash = ?",
            (provider, provider_team_id, observed_at, content_hash),
        )
        if existing is not None:
            return self._to_team(existing), ObservationOutcome.UNCHANGED

        identity_id = new_provider_team_identity_id()
        self._conn.execute(
            f"INSERT INTO {_TEAM_TABLE} "
            "(identity_id, provider, provider_team_id, league_id, full_name, "
            " normalized_name, abbreviation, city, nickname, observed_at, "
            " raw_response_id, raw_response_hash, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identity_id, provider, provider_team_id, league_id, name, normalized,
             abbrev, town, nick, observed_at, raw_response_id, raw_response_hash,
             content_hash, utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_TEAM_TABLE} WHERE identity_id = ?", (identity_id,)
        )
        assert row is not None  # noqa: S101 - just inserted
        return self._to_team(row), ObservationOutcome.INSERTED

    def latest_team(
        self, provider: str, provider_team_id: str, *, as_of: Optional[str] = None
    ) -> Optional[ProviderTeamIdentity]:
        """The newest identity at or before ``as_of`` (unbounded when ``None``).

        ``as_of`` exists so a point-in-time matching decision is never resolved
        with a name the provider had not yet written at decision time.
        """

        sql = f"SELECT * FROM {_TEAM_TABLE} WHERE provider = ? AND provider_team_id = ?"
        params: tuple[object, ...] = (provider, provider_team_id)
        if as_of is not None:
            sql += " AND observed_at <= ?"
            params = (*params, as_of)
        row = self._fetch_one(f"{sql} {_LATEST_ORDER} LIMIT 1", params)
        return None if row is None else self._to_team(row)

    def count_teams(self) -> int:
        return self._count(f"SELECT COUNT(*) FROM {_TEAM_TABLE}")

    # -- players ------------------------------------------------------------- #
    def record_player(
        self,
        *,
        provider: str,
        provider_player_id: str,
        league_id: str,
        full_name: str,
        observed_at: str,
        raw_response_id: str,
        raw_response_hash: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        birth_date: Optional[str] = None,
        position: Optional[str] = None,
        provider_team_id: Optional[str] = None,
    ) -> tuple[ProviderPlayerIdentity, ObservationOutcome]:
        """Append one player identity observation, or no-op on identical content."""

        prepared = prepare_player_identity(
            provider=provider, provider_player_id=provider_player_id,
            league_id=league_id, full_name=full_name, first_name=first_name,
            last_name=last_name, birth_date=birth_date, position=position,
            provider_team_id=provider_team_id,
        )
        name, first, last = prepared.full_name, prepared.first_name, prepared.last_name
        birth, pos, team = prepared.birth_date, prepared.position, prepared.provider_team_id
        content_hash = prepared.content_hash
        existing = self._fetch_one(
            f"SELECT * FROM {_PLAYER_TABLE} WHERE provider = ? AND provider_player_id = ? "
            "AND observed_at = ? AND content_hash = ?",
            (provider, provider_player_id, observed_at, content_hash),
        )
        if existing is not None:
            return self._to_player(existing), ObservationOutcome.UNCHANGED

        identity_id = new_provider_player_identity_id()
        self._conn.execute(
            f"INSERT INTO {_PLAYER_TABLE} "
            "(identity_id, provider, provider_player_id, league_id, full_name, "
            " normalized_name, suffix, first_name, last_name, birth_date, position, "
            " provider_team_id, observed_at, raw_response_id, raw_response_hash, "
            " content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identity_id, provider, provider_player_id, league_id, name,
             prepared.normalized_name, prepared.suffix, first, last, birth, pos,
             team, observed_at, raw_response_id, raw_response_hash, content_hash,
             utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_PLAYER_TABLE} WHERE identity_id = ?", (identity_id,)
        )
        assert row is not None  # noqa: S101 - just inserted
        return self._to_player(row), ObservationOutcome.INSERTED

    def latest_player(
        self, provider: str, provider_player_id: str, *, as_of: Optional[str] = None
    ) -> Optional[ProviderPlayerIdentity]:
        sql = f"SELECT * FROM {_PLAYER_TABLE} WHERE provider = ? AND provider_player_id = ?"
        params: tuple[object, ...] = (provider, provider_player_id)
        if as_of is not None:
            sql += " AND observed_at <= ?"
            params = (*params, as_of)
        row = self._fetch_one(f"{sql} {_LATEST_ORDER} LIMIT 1", params)
        return None if row is None else self._to_player(row)

    def count_players(self) -> int:
        return self._count(f"SELECT COUNT(*) FROM {_PLAYER_TABLE}")

    # -- conflict reporting -------------------------------------------------- #
    def equal_time_conflicts(self, kind: str) -> list[EqualTimeConflict]:
        """Provider entities with two different contents at the same instant.

        ``latest_*`` still answers deterministically (the content-hash tie-break),
        but the caller must be able to *say* the provider contradicted itself
        rather than quietly taking one of the two names.
        """

        table, column = self._kind(kind)
        rows = self._fetch_all(
            f"SELECT provider, {column} AS entity_id, observed_at, "
            "COUNT(DISTINCT content_hash) AS contents "
            f"FROM {table} GROUP BY provider, {column}, observed_at "
            "HAVING contents > 1 "
            f"ORDER BY provider, {column}, observed_at"
        )
        return [
            EqualTimeConflict(
                provider=str(r["provider"]), provider_entity_id=str(r["entity_id"]),
                observed_at=str(r["observed_at"]), contents=int(r["contents"]),
            )
            for r in rows
        ]

    @staticmethod
    def _kind(kind: str) -> tuple[str, str]:
        if kind == "team":
            return _TEAM_TABLE, "provider_team_id"
        if kind == "player":
            return _PLAYER_TABLE, "provider_player_id"
        raise RepositoryError(f"unknown identity kind {kind!r}; expected 'team' or 'player'")

    # -- row mapping --------------------------------------------------------- #
    def _to_team(self, row: sqlite3.Row) -> ProviderTeamIdentity:
        return ProviderTeamIdentity(
            identity_id=str(row["identity_id"]),
            provider=str(row["provider"]),
            provider_team_id=str(row["provider_team_id"]),
            league_id=str(row["league_id"]),
            full_name=str(row["full_name"]),
            normalized_name=str(row["normalized_name"]),
            observed_at=str(row["observed_at"]),
            raw_response_id=str(row["raw_response_id"]),
            raw_response_hash=str(row["raw_response_hash"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            abbreviation=self._opt_str(row, "abbreviation"),
            city=self._opt_str(row, "city"),
            nickname=self._opt_str(row, "nickname"),
        )

    def _to_player(self, row: sqlite3.Row) -> ProviderPlayerIdentity:
        return ProviderPlayerIdentity(
            identity_id=str(row["identity_id"]),
            provider=str(row["provider"]),
            provider_player_id=str(row["provider_player_id"]),
            league_id=str(row["league_id"]),
            full_name=str(row["full_name"]),
            normalized_name=str(row["normalized_name"]),
            suffix=str(row["suffix"]),
            observed_at=str(row["observed_at"]),
            raw_response_id=str(row["raw_response_id"]),
            raw_response_hash=str(row["raw_response_hash"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            first_name=self._opt_str(row, "first_name"),
            last_name=self._opt_str(row, "last_name"),
            birth_date=self._opt_str(row, "birth_date"),
            position=self._opt_str(row, "position"),
            provider_team_id=self._opt_str(row, "provider_team_id"),
        )
