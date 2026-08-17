"""Read-only source adapters and the audited-subset corpus fingerprint (G5).

The identity audit reads a historical corpus that is **evidence**, never a
workspace. Everything here opens the source ``immutable=1`` and issues only
SELECTs -- not even a ``-shm`` sidecar is touched; the audit's own output goes to
a separate schema-v19 database.

Why typed observations rather than raw JSON
-------------------------------------------
Every identity fact the audit needs was already normalized by ingestion into
append-only observation tables, so the audit reads those. Parsing
``raw_responses`` bodies at audit time would couple the audit to every provider's
payload shape and make its result unauditable -- the same reason migration e017
exists at all. ``raw_responses`` is therefore *not* a primary audit path here.

The audited subset, stated explicitly
-------------------------------------
Task §4 requires the fingerprint to make its scope explicit and deterministic.
The audit consumes exactly three tables:

* ``game_schedule_snapshots``      -- official game identity observations
* ``provider_team_identity_snapshots``   -- official team identity observations
* ``provider_player_identity_snapshots`` -- official person identity observations

and within them, only the columns listed in ``_DIGEST_COLUMNS``. Deliberately
excluded from the digest: surrogate ids, ``created_at``/``ingested_at``/``run_id``
(audit bookkeeping, not evidence), and every column the compatibility rules are
forbidden to read -- final scores, results and match decisions. A change to any
audited identity evidence changes the digest; a harmless traversal-order change
does not, because rows are sorted canonically before hashing.

The digest is per **(source corpus, league, provider)** and deliberately NOT per
entity type: all three entity-type audits of one corpus must share one
``source_corpus_digest``, or a reconstruction corpus version citing that digest
could consume crosswalks from only one of them.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from streaming.event_envelope import canonical_json

from .provenance import EntityType, RetrospectiveProvenanceError

__all__ = [
    "AUDITED_SOURCE_TABLES",
    "LINKING_SOURCE_TABLES",
    "audited_source_tables",
    "digest_columns_for",
    "PROVIDER_LEAGUES",
    "GameObservation",
    "PlayerObservation",
    "SOURCE_DIGEST_POLICY_VERSION",
    "SourceCorpusError",
    "TeamObservation",
    "iter_game_observations",
    "iter_player_observations",
    "iter_team_observations",
    "open_source_corpus",
    "require_provider_league",
    "source_corpus_digest",
]


class SourceCorpusError(RetrospectiveProvenanceError):
    """The source corpus cannot be read as the audit requires."""


#: Which league each official provider serves in this repository.
#:
#: ``game_schedule_snapshots`` has **no** ``league_id`` column, so the game audit
#: and the game half of the source digest are scoped by provider alone. That is
#: only sound while a provider is league-exclusive, which is an invariant rather
#: than a fact of the schema -- so it is stated here and enforced, instead of
#: being silently relied upon. A provider that ever served two leagues must get a
#: league-scoped game query before it can be audited.
PROVIDER_LEAGUES: Final[dict[str, str]] = {
    "mlb_statsapi": "lg_mlb",
    "balldontlie": "lg_nba",
}


def require_provider_league(provider: str, league_id: str) -> None:
    """Refuse a (provider, league) pair this build cannot scope safely."""

    expected = PROVIDER_LEAGUES.get(provider)
    if expected is None:
        raise SourceCorpusError(
            f"provider {provider!r} has no declared league in PROVIDER_LEAGUES. "
            "Game identity evidence carries no league column, so a provider whose "
            "league is undeclared cannot be scoped safely and is refused."
        )
    if expected != league_id:
        raise SourceCorpusError(
            f"provider {provider!r} serves {expected!r}, not {league_id!r}. Refusing "
            "rather than auditing one league's ids under another's namespace."
        )


#: Bumping this changes every source digest, which is the point: it means the
#: audited SUBSET itself changed, so old digests describe different evidence.
SOURCE_DIGEST_POLICY_VERSION: Final = "g5-source-subset-v1"

#: Exactly what the audit reads, and the columns that feed the digest.
_DIGEST_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "game_schedule_snapshots": (
        "provider", "provider_game_id", "season", "game_type", "game_date_local",
        "scheduled_start", "home_provider_team_id", "away_provider_team_id",
        "venue_provider_id", "mapped_status", "game_number", "doubleheader_code",
        "reschedule_info", "observed_at",
    ),
    "provider_team_identity_snapshots": (
        "provider", "provider_team_id", "league_id", "full_name", "normalized_name",
        "abbreviation", "city", "nickname", "observed_at",
    ),
    "provider_player_identity_snapshots": (
        "provider", "provider_player_id", "league_id", "full_name", "normalized_name",
        "suffix", "first_name", "last_name", "birth_date", "position",
        "provider_team_id", "observed_at",
    ),
}

#: The audited set for an OFFICIAL provider. This is the historical meaning of
#: `AUDITED_SOURCE_TABLES` and it is deliberately unchanged by f020.
AUDITED_SOURCE_TABLES: Final[tuple[str, ...]] = tuple(sorted(_DIGEST_COLUMNS))

#: Columns a LINKING (secondary, identity-linking) provider's evidence digests
#: over. Kept in a separate mapping from `_DIGEST_COLUMNS` for one measured
#: reason: `source_corpus_digest` folds ONE ENTRY PER AUDITED TABLE into its
#: payload, so adding a fourth table to the global set changes the recomputed
#: digest of a corpus holding ZERO market rows. That was reproduced, not
#: assumed, and it would invalidate every accepted audit and crosswalk binding
#: built under v19 -- rows that are append-only historical facts.
#:
#: Selecting the set by provider class is not a new concept bolted on; it makes
#: an existing partition explicit. `source_corpus_digest` already refuses any
#: provider absent from `PROVIDER_LEAGUES`, and every audited query already
#: filters `WHERE provider = ?`, so an official-provider digest could never have
#: contained a linking provider's rows anyway.
_LINKING_DIGEST_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "historical_market_event_observations": (
        "league_id", "provider", "namespace_generation", "sport_key",
        "provider_event_id", "requested_at_bucket", "provider_snapshot_timestamp",
        "commence_time", "home_team_raw", "away_team_raw",
        # The PORTABLE identity. `raw_response_id` is database-local and is
        # deliberately absent: digesting it would make a transported corpus hash
        # differently from the one it was copied from.
        "observation_content_hash",
    ),
}

#: The audited set for a linking provider.
LINKING_SOURCE_TABLES: Final[tuple[str, ...]] = tuple(sorted(_LINKING_DIGEST_COLUMNS))


def audited_source_tables(provider: str) -> tuple[str, ...]:
    """Which tables the source digest covers for one provider.

    An official provider keeps exactly the three-table set it was always
    digested under, so every existing corpus digest stays byte-identical under
    v20. A linking provider digests over its own evidence instead.

    No linking provider is registered at v20, so the linking branch is currently
    unreachable through `source_corpus_digest`, which still refuses any provider
    absent from `PROVIDER_LEAGUES`. The mechanism exists so that registering one
    later is a reviewed one-line change rather than a silent global digest
    change; it authorizes nothing by itself.
    """

    if provider in PROVIDER_LEAGUES:
        return AUDITED_SOURCE_TABLES
    return LINKING_SOURCE_TABLES


def digest_columns_for(table: str) -> tuple[str, ...]:
    """The digest columns for one audited table, official or linking."""

    if table in _DIGEST_COLUMNS:
        return _DIGEST_COLUMNS[table]
    return _LINKING_DIGEST_COLUMNS[table]


def open_source_corpus(path: Path | str) -> sqlite3.Connection:
    """Open a historical corpus with `immutable=1`, refusing a pending WAL.

    ``immutable=1`` is the only mode that touches **nothing**: ``mode=ro`` still
    builds the shared-memory index, which updates the ``-shm`` sidecar's mtime
    beside protected evidence. That is a real mutation of the evidence directory
    even though the database bytes are unchanged.

    The cost of `immutable` is that it ignores WAL content, so a corpus with a
    non-empty write-ahead log would be read as a stale view -- silently. Rather
    than trade one hazard for another, the WAL is checked first and a pending one
    is REFUSED: the caller must checkpoint into a protected copy, which is an
    explicit act on a copy rather than a quiet misread of the original.
    """

    source = Path(path)
    if not source.exists():
        raise SourceCorpusError(f"source corpus {source} does not exist")
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise SourceCorpusError(
            f"source corpus {source} has a non-empty write-ahead log "
            f"({wal.stat().st_size} bytes). It is opened immutable so that auditing "
            "leaves protected evidence untouched, and an immutable handle cannot see "
            "WAL content -- so this would audit a stale view. Checkpoint into a "
            "protected copy and audit that instead."
        )
    conn = sqlite3.connect(f"file:{source.as_posix()}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    missing = [
        table for table in AUDITED_SOURCE_TABLES
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone() is None
    ]
    if missing:
        conn.close()
        raise SourceCorpusError(
            f"source corpus {source} is missing audited evidence tables {missing}; "
            "the audit refuses to report a clean namespace from a corpus that "
            "cannot hold the evidence"
        )
    return conn


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GameObservation:
    """One observation of an official game id.

    ``season``/``home_provider_team_id``/``away_provider_team_id`` are the
    identity-defining triple. Everything else is legitimate mutation of the same
    event and is carried for reporting and for the reschedule explanation, never
    for identity. No score, winner or result field exists on this record at all.
    """

    provider_game_id: str
    season: Optional[int]
    home_provider_team_id: Optional[str]
    away_provider_team_id: Optional[str]
    game_date_local: Optional[str]
    scheduled_start: Optional[str]
    mapped_status: Optional[str]
    game_number: Optional[int]
    doubleheader_code: Optional[str]
    venue_provider_id: Optional[str]
    #: The provider's own statement that this event moved. It was already bound
    #: into the source digest but was NOT exposed to the compatibility rules, so
    #: the audit could not tell a postponement from an id reused for a second
    #: event on another date. That gap is what `reschedule_info` closes.
    reschedule_info: Optional[str]
    observed_at: str


@dataclass(frozen=True)
class TeamObservation:
    """One observation of an official team id.

    ``league_id`` is the only identity-defining field: a team id is FRANCHISE
    identity. Name, abbreviation, city and nickname are labels that change
    lawfully (rename, relocation, rebrand) and are used for detection only.
    """

    provider_team_id: str
    league_id: str
    full_name: str
    normalized_name: str
    abbreviation: Optional[str]
    city: Optional[str]
    nickname: Optional[str]
    observed_at: str


@dataclass(frozen=True)
class PlayerObservation:
    """One observation of an official person id.

    ``league_id`` is identity-defining; ``birth_date`` is decisive secondary
    evidence **when the provider genuinely supplied it on both observations**.
    ``provider_team_id`` and ``position`` are explicitly NOT identity: affiliation
    is time-varying, which is the whole reason it lives in Lane L.
    """

    provider_player_id: str
    league_id: str
    full_name: str
    normalized_name: str
    suffix: str
    birth_date: Optional[str]
    position: Optional[str]
    provider_team_id: Optional[str]
    observed_at: str


def _opt_str(row: sqlite3.Row, column: str) -> Optional[str]:
    value = row[column]
    return None if value is None else str(value)


def _opt_int(row: sqlite3.Row, column: str) -> Optional[int]:
    value = row[column]
    return None if value is None else int(value)


def iter_game_observations(
    conn: sqlite3.Connection, *, provider: str
) -> Iterator[GameObservation]:
    """Every official game identity observation, in a canonical order."""

    rows = conn.execute(
        "SELECT provider_game_id, season, game_date_local, scheduled_start, "
        "       home_provider_team_id, away_provider_team_id, mapped_status, "
        "       game_number, doubleheader_code, venue_provider_id, "
        "       reschedule_info, observed_at "
        "FROM game_schedule_snapshots WHERE provider = ? "
        "ORDER BY provider_game_id, observed_at, schedule_id",
        (provider,),
    )
    for row in rows:
        yield GameObservation(
            provider_game_id=str(row["provider_game_id"]),
            season=_opt_int(row, "season"),
            home_provider_team_id=_opt_str(row, "home_provider_team_id"),
            away_provider_team_id=_opt_str(row, "away_provider_team_id"),
            game_date_local=_opt_str(row, "game_date_local"),
            scheduled_start=_opt_str(row, "scheduled_start"),
            mapped_status=_opt_str(row, "mapped_status"),
            game_number=_opt_int(row, "game_number"),
            doubleheader_code=_opt_str(row, "doubleheader_code"),
            venue_provider_id=_opt_str(row, "venue_provider_id"),
            reschedule_info=_opt_str(row, "reschedule_info"),
            observed_at=str(row["observed_at"]),
        )


def iter_team_observations(
    conn: sqlite3.Connection, *, provider: str
) -> Iterator[TeamObservation]:
    rows = conn.execute(
        "SELECT provider_team_id, league_id, full_name, normalized_name, "
        "       abbreviation, city, nickname, observed_at "
        "FROM provider_team_identity_snapshots WHERE provider = ? "
        "ORDER BY provider_team_id, observed_at, identity_id",
        (provider,),
    )
    for row in rows:
        yield TeamObservation(
            provider_team_id=str(row["provider_team_id"]),
            league_id=str(row["league_id"]),
            full_name=str(row["full_name"]),
            normalized_name=str(row["normalized_name"]),
            abbreviation=_opt_str(row, "abbreviation"),
            city=_opt_str(row, "city"),
            nickname=_opt_str(row, "nickname"),
            observed_at=str(row["observed_at"]),
        )


def iter_player_observations(
    conn: sqlite3.Connection, *, provider: str
) -> Iterator[PlayerObservation]:
    rows = conn.execute(
        "SELECT provider_player_id, league_id, full_name, normalized_name, suffix, "
        "       birth_date, position, provider_team_id, observed_at "
        "FROM provider_player_identity_snapshots WHERE provider = ? "
        "ORDER BY provider_player_id, observed_at, identity_id",
        (provider,),
    )
    for row in rows:
        yield PlayerObservation(
            provider_player_id=str(row["provider_player_id"]),
            league_id=str(row["league_id"]),
            full_name=str(row["full_name"]),
            normalized_name=str(row["normalized_name"]),
            suffix=str(row["suffix"] or ""),
            birth_date=_opt_str(row, "birth_date"),
            position=_opt_str(row, "position"),
            provider_team_id=_opt_str(row, "provider_team_id"),
            observed_at=str(row["observed_at"]),
        )


def observations_for(
    conn: sqlite3.Connection, entity_type: EntityType, *, provider: str
) -> list[object]:
    """Dispatch to the adapter for one entity type."""

    if entity_type is EntityType.GAME:
        return list(iter_game_observations(conn, provider=provider))
    if entity_type is EntityType.TEAM:
        return list(iter_team_observations(conn, provider=provider))
    return list(iter_player_observations(conn, provider=provider))


# --------------------------------------------------------------------------- #
# Audited-subset fingerprint
# --------------------------------------------------------------------------- #
def source_corpus_digest(
    conn: sqlite3.Connection, *, league_id: str, provider: str
) -> str:
    """A deterministic fingerprint of exactly the evidence the audit scans.

    Per (corpus, league, provider) and NOT per entity type, so all three
    entity-type audits of one corpus agree on one digest -- which is what lets a
    reconstruction corpus version cite it once and consume crosswalks from any of
    them.

    Order-independent by construction: every row is reduced to a canonical tuple
    and the tuples are sorted before hashing, so SQLite traversal order, insertion
    order and rowid assignment cannot affect the result. Changing any audited
    identity value does change it.
    """

    require_provider_league(provider, league_id)
    payload: dict[str, object] = {
        "policy": SOURCE_DIGEST_POLICY_VERSION,
        "league_id": league_id,
        "provider": provider,
    }
    for table in audited_source_tables(provider):
        columns = digest_columns_for(table)
        # `league_id` is not a column of game_schedule_snapshots; that table is
        # already scoped by provider, and the league is bound in the payload.
        where = "WHERE provider = ?"
        params: tuple[object, ...] = (provider,)
        if "league_id" in columns:
            where += " AND league_id = ?"
            params = (provider, league_id)
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} {where}",  # noqa: S608
            params,
        ).fetchall()
        # Sorted canonical tuples: the digest is a function of the SET of audited
        # facts, never of the order they are returned in.
        reduced = sorted(
            canonical_json({c: _scalar(row[c]) for c in columns}) for row in rows
        )
        payload[table] = {
            "rows": len(reduced),
            "digest": hashlib.sha256("\n".join(reduced).encode("utf-8")).hexdigest(),
        }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _scalar(value: object) -> object:
    """Normalize a SQLite scalar for canonical JSON."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
