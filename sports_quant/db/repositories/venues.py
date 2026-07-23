"""Venue + venue-alias repository.

Canonical venues are mutable current-state with c008 first/current provenance,
keyed by ``normalized_name`` (deterministic via ``db.normalize``). Coordinates,
roof type, and timezone are validated in application code in addition to the
database CHECKs. Aliases map provider venue strings/ids to a canonical venue,
mirroring ``team_aliases``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import new_venue_alias_id, new_venue_id
from ..models import Venue, VenueAlias
from ..normalize import normalized_key
from ..schema import VENUE_ROOF_TYPES, utc_now_iso
from .base import Repository, RepositoryError, to_db_bool
from .kalshi import UpsertOutcome

_ROOF_OUTDOOR = {"open", "retractable"}  # retractable venues can be outdoor


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
    ) -> VenueAlias:
        """Insert a venue alias idempotently (``INSERT OR IGNORE``)."""

        normalized = normalized_key(alias)
        now = utc_now_iso()
        alias_id = new_venue_alias_id()
        self._conn.execute(
            "INSERT OR IGNORE INTO venue_aliases "
            "(alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, now),
        )
        row = self._fetch_one(
            "SELECT alias_id, venue_id, provider, provider_venue_id, alias, normalized, source, "
            "created_at FROM venue_aliases WHERE venue_id = ? AND normalized = ? AND provider = ?",
            (venue_id, normalized, provider),
        )
        assert row is not None  # noqa: S101
        return VenueAlias(
            alias_id=str(row["alias_id"]),
            venue_id=str(row["venue_id"]),
            alias=str(row["alias"]),
            normalized=str(row["normalized"]),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
            provider=str(row["provider"]),
            provider_venue_id=self._opt_str(row, "provider_venue_id"),
        )

    def resolve_alias(self, alias: str, *, provider: str = "") -> Optional[str]:
        """Return the ``venue_id`` for an alias, or ``None``."""

        normalized = normalized_key(alias)
        row = self._fetch_one(
            "SELECT venue_id FROM venue_aliases WHERE normalized = ? AND provider = ?",
            (normalized, provider),
        )
        return None if row is None else str(row["venue_id"])

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
