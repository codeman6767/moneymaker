"""Venue + venue-alias repository.

Canonical venues are mutable current-state with c008 first/current provenance,
keyed by ``normalized_name`` (deterministic via ``db.normalize``). Coordinates,
roof type, and timezone are validated in application code in addition to the
database CHECKs. Aliases map provider venue strings/ids to a canonical venue,
mirroring ``team_aliases``.
"""

from __future__ import annotations

import enum
import sqlite3
from typing import Optional, Protocol

from ..ids import new_venue_alias_id, new_venue_id
from ..models import Venue, VenueAlias
from ..normalize import normalized_key
from ..schema import VENUE_ROOF_TYPES, utc_now_iso
from .base import Repository, RepositoryError, to_db_bool
from .kalshi import UpsertOutcome

_ROOF_OUTDOOR = {"open", "retractable"}  # retractable venues can be outdoor


class AliasOutcome(str, enum.Enum):
    """Result of adding a venue alias, so the ingestor counts accurately.

    * ``INSERTED``  -- a new alias row was written.
    * ``UNCHANGED`` -- the identical alias already existed (idempotent no-op).
    * ``CONFLICT``  -- a provider venue id already maps to a *different* canonical
      venue: an ambiguity to surface, never a silent arbitrary choice.
    """

    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


def validate_venue_fields(
    *,
    latitude: Optional[float],
    longitude: Optional[float],
    timezone: Optional[str],
    roof_type: Optional[str],
) -> None:
    """Reject clearly-invalid venue attributes (belt-and-braces over the CHECKs)."""

    if latitude is not None and not (-90.0 <= latitude <= 90.0):
        raise RepositoryError(f"venue latitude out of range: {latitude!r}")
    if longitude is not None and not (-180.0 <= longitude <= 180.0):
        raise RepositoryError(f"venue longitude out of range: {longitude!r}")
    if roof_type is not None and roof_type not in VENUE_ROOF_TYPES:
        raise RepositoryError(
            f"invalid venue roof_type {roof_type!r}; expected one of {list(VENUE_ROOF_TYPES)}"
        )
    if timezone is not None and (not timezone.strip() or "/" not in timezone):
        # A pragmatic sanity check: IANA zones look like 'Area/Location'. UTC is
        # allowed as an explicit exception.
        if timezone.strip() != "UTC":
            raise RepositoryError(f"venue timezone does not look like an IANA zone: {timezone!r}")


class VenueRepositoryProtocol(Protocol):
    def upsert(
        self,
        *,
        name: str,
        raw_response_id: str,
        raw_response_hash: str,
        observed_at: str,
        **fields: object,
    ) -> tuple[Venue, UpsertOutcome]: ...

    def get_by_normalized(self, normalized_name: str) -> Optional[Venue]: ...


class SqliteVenueRepository(Repository):
    """Venue + alias storage."""

    _COLUMNS = (
        "venue_id, name, normalized_name, city, country, latitude, longitude, timezone, "
        "roof_type, is_outdoor, first_raw_response_id, current_raw_response_id, "
        "current_raw_response_hash, first_observed_at, last_observed_at, created_at, updated_at"
    )

    def upsert(
        self,
        *,
        name: str,
        raw_response_id: str,
        raw_response_hash: str,
        observed_at: str,
        city: Optional[str] = None,
        country: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        timezone: Optional[str] = None,
        roof_type: Optional[str] = None,
        is_outdoor: Optional[bool] = None,
    ) -> tuple[Venue, UpsertOutcome]:
        """Insert a venue, or refresh its mutable metadata + provenance if newer.

        Identity is the normalized name. Newer observations advance current
        metadata and provenance; older-or-equal backfills do not regress it.
        """

        if not name.strip():
            raise RepositoryError("venue name must be non-blank")
        validate_venue_fields(
            latitude=latitude, longitude=longitude, timezone=timezone, roof_type=roof_type
        )
        # Derive is_outdoor from roof_type when not explicitly given.
        if is_outdoor is None and roof_type is not None:
            is_outdoor = roof_type in _ROOF_OUTDOOR
        normalized = normalized_key(name)
        existing = self.get_by_normalized(normalized)
        now = utc_now_iso()
        outdoor_db = None if is_outdoor is None else to_db_bool(is_outdoor)

        if existing is None:
            venue_id = new_venue_id()
            self._conn.execute(
                "INSERT INTO venues "
                "(venue_id, name, normalized_name, city, country, latitude, longitude, timezone, "
                " roof_type, is_outdoor, first_raw_response_id, current_raw_response_id, "
                " current_raw_response_hash, first_observed_at, last_observed_at, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    venue_id, name, normalized, city, country, latitude, longitude, timezone,
                    roof_type, outdoor_db, raw_response_id, raw_response_id, raw_response_hash,
                    observed_at, observed_at, now, now,
                ),
            )
            fetched = self.get(venue_id)
            assert fetched is not None  # noqa: S101
            return fetched, UpsertOutcome.INSERTED

        if observed_at > existing.last_observed_at:
            self._conn.execute(
                "UPDATE venues SET name = ?, city = ?, country = ?, latitude = ?, longitude = ?, "
                "timezone = ?, roof_type = ?, is_outdoor = ?, current_raw_response_id = ?, "
                "current_raw_response_hash = ?, last_observed_at = ?, updated_at = ? "
                "WHERE venue_id = ?",
                (
                    name, city, country, latitude, longitude, timezone, roof_type, outdoor_db,
                    raw_response_id, raw_response_hash, observed_at, now, existing.venue_id,
                ),
            )
            refreshed = self.get(existing.venue_id)
            assert refreshed is not None  # noqa: S101
            return refreshed, UpsertOutcome.UPDATED

        return existing, UpsertOutcome.UNCHANGED

    def get(self, venue_id: str) -> Optional[Venue]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM venues WHERE venue_id = ?", (venue_id,)
        )
        return None if row is None else self._to_venue(row)

    def get_by_normalized(self, normalized_name: str) -> Optional[Venue]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM venues WHERE normalized_name = ?", (normalized_name,)
        )
        return None if row is None else self._to_venue(row)

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM venues")

    # -- Aliases -------------------------------------------------------------
    def add_alias(
        self,
        *,
        venue_id: str,
        alias: str,
        provider: str = "",
        provider_venue_id: Optional[str] = None,
        source: str = "provider_observed",
    ) -> tuple[Optional[VenueAlias], AliasOutcome]:
        """Add a venue alias, distinguishing insert / unchanged / conflict.

        Returns ``(alias, outcome)``. A provider venue id already bound to a
        *different* canonical venue is a **conflict** (``(None, CONFLICT)``) --
        surfaced, never resolved by an arbitrary choice, so ambiguous mappings are
        detected. An identical existing alias is ``UNCHANGED`` (no ``INSERT OR
        IGNORE`` miscounting). A genuinely new alias is ``INSERTED``.
        """

        normalized = normalized_key(alias)
        now = utc_now_iso()

        # A provider id already mapped to another venue is an ambiguity, not a dup.
        if provider_venue_id and provider:
            bound = self._fetch_one(
                "SELECT venue_id FROM venue_aliases "
                "WHERE provider = ? AND provider_venue_id = ?",
                (provider, provider_venue_id),
            )
            if bound is not None and str(bound["venue_id"]) != venue_id:
                return None, AliasOutcome.CONFLICT

        existing = self._fetch_one(
            "SELECT alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, "
            "created_at FROM venue_aliases WHERE venue_id = ? AND normalized = ? AND provider = ?",
            (venue_id, normalized, provider),
        )
        if existing is not None:
            return self._to_alias(existing), AliasOutcome.UNCHANGED

        alias_id = new_venue_alias_id()
        self._conn.execute(
            "INSERT INTO venue_aliases "
            "(alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, now),
        )
        row = self._fetch_one(
            "SELECT alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, "
            "created_at FROM venue_aliases WHERE alias_id = ?",
            (alias_id,),
        )
        assert row is not None  # noqa: S101
        return self._to_alias(row), AliasOutcome.INSERTED

    @staticmethod
    def _to_alias(row: sqlite3.Row) -> VenueAlias:
        return VenueAlias(
            alias_id=str(row["alias_id"]),
            venue_id=str(row["venue_id"]),
            alias=str(row["alias"]),
            normalized=str(row["normalized"]),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
            provider=str(row["provider"]),
            provider_venue_id=(None if row["provider_venue_id"] is None else str(row["provider_venue_id"])),
        )

    def resolve_alias(self, alias: str, *, provider: str = "") -> Optional[str]:
        """Return the venue an alias uniquely resolves to, else ``None``.

        Returns ``None`` when the alias is unknown **or ambiguous** (a normalized
        name that maps to more than one canonical venue -- e.g. same-named venues
        in different cities). Ambiguity is detected, never resolved to an
        arbitrary venue; a caller needing the distinction uses
        :meth:`resolve_alias_detail`.
        """

        venue_ids, _ambiguous = self.resolve_alias_detail(alias, provider=provider)
        return venue_ids[0] if len(venue_ids) == 1 else None

    def resolve_alias_detail(
        self, alias: str, *, provider: str = ""
    ) -> tuple[list[str], bool]:
        """Return ``(venue_ids, ambiguous)`` for an alias.

        ``venue_ids`` is every distinct canonical venue the normalized alias maps
        to; ``ambiguous`` is ``True`` when there is more than one.
        """

        normalized = normalized_key(alias)
        rows = self._fetch_all(
            "SELECT DISTINCT venue_id FROM venue_aliases WHERE normalized = ? AND provider = ?",
            (normalized, provider),
        )
        venue_ids = [str(r["venue_id"]) for r in rows]
        return venue_ids, len(venue_ids) > 1

    def count_aliases(self) -> int:
        return self._count("SELECT COUNT(*) FROM venue_aliases")

    def _to_venue(self, row: sqlite3.Row) -> Venue:
        outdoor = row["is_outdoor"]
        return Venue(
            venue_id=str(row["venue_id"]),
            name=str(row["name"]),
            normalized_name=str(row["normalized_name"]),
            first_raw_response_id=str(row["first_raw_response_id"]),
            current_raw_response_id=str(row["current_raw_response_id"]),
            current_raw_response_hash=str(row["current_raw_response_hash"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            city=self._opt_str(row, "city"),
            country=self._opt_str(row, "country"),
            latitude=self._opt_float(row, "latitude"),
            longitude=self._opt_float(row, "longitude"),
            timezone=self._opt_str(row, "timezone"),
            roof_type=self._opt_str(row, "roof_type"),
            is_outdoor=None if outdoor is None else bool(outdoor),
        )

    @staticmethod
    def _opt_float(row: sqlite3.Row, column: str) -> Optional[float]:
        value = row[column]
        return None if value is None else float(value)
