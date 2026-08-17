"""SQLite repository for historical market EVENT observations.

Deliberately narrow. This repository stores and reads typed provider evidence so
a later identity audit can consume it. It performs **no** identity resolution:
there is no canonical-game lookup, no name or alias lookup, no fuzzy matching,
no sportsbook-matcher integration and no provider client. It also exposes no
update and no delete, because the table is append-only and a correction to an
observation is a contradiction in terms -- a later, different answer is a NEW
observation, which is why the uniqueness key carries the content hash.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ...retrospective.market_observations import (
    MarketEventObservation,
    observation_content_hash,
    observation_id,
    validate_provider_event_id,
)
from ..schema import utc_now_iso
from .base import RepositoryError

__all__ = [
    "MarketObservationRow",
    "RecordedObservation",
    "SqliteMarketObservationRepository",
]

_COLUMNS = (
    "observation_id", "league_id", "provider", "namespace_generation", "sport_key",
    "provider_event_id", "requested_at_bucket", "provider_snapshot_timestamp",
    "commence_time", "home_team_raw", "away_team_raw", "observation_content_hash",
    "raw_response_id", "observed_at", "created_at",
)


@dataclass(frozen=True)
class MarketObservationRow:
    """One persisted observation, exactly as stored."""

    observation_id: str
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
    observation_content_hash: str
    raw_response_id: str
    observed_at: str
    created_at: str


@dataclass(frozen=True)
class RecordedObservation:
    """The outcome of one write. ``created`` is False on an idempotent replay."""

    row: MarketObservationRow
    created: bool


def _row(record: sqlite3.Row) -> MarketObservationRow:
    return MarketObservationRow(**{c: record[c] for c in _COLUMNS})


class SqliteMarketObservationRepository:
    """Append-only reads and writes over ``historical_market_event_observations``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # -- writes ---------------------------------------------------------------
    def record(
        self,
        observation: MarketEventObservation,
        *,
        raw_response_id: str,
        observed_at: Optional[str] = None,
    ) -> RecordedObservation:
        """Persist one observation, or reuse the identical existing row.

        Idempotent on the deterministic id. A replay of byte-identical evidence
        writes nothing and returns ``created=False``; it does **not** refresh
        ``observed_at``, because doing so would rewrite when we first learned
        something. Two observations that differ in any semantic field get
        different ids and **both** persist -- that is the contradiction the
        audit exists to find, and deduplicating it would destroy the evidence.
        """

        content_hash = observation_content_hash(observation)
        oid = observation_id(observation)

        existing = self.get(oid)
        if existing is not None:
            if existing.observation_content_hash != content_hash:
                # Cannot happen while the id is a pure function of the hash;
                # refused rather than overwritten so a future change to the id
                # derivation can never silently clobber preserved evidence.
                raise RepositoryError(
                    f"observation id {oid!r} already exists with content hash "
                    f"{existing.observation_content_hash!r}, but this observation "
                    f"hashes to {content_hash!r}. Refusing to overwrite preserved "
                    "evidence."
                )
            return RecordedObservation(row=existing, created=False)

        now = utc_now_iso()
        values = (
            oid, observation.league_id, observation.provider,
            observation.namespace_generation, observation.sport_key,
            observation.provider_event_id, observation.requested_at_bucket,
            observation.provider_snapshot_timestamp, observation.commence_time,
            observation.home_team_raw, observation.away_team_raw, content_hash,
            raw_response_id, observed_at or now, now,
        )
        placeholders = ", ".join("?" * len(_COLUMNS))
        try:
            self._conn.execute(
                f"INSERT INTO historical_market_event_observations "  # noqa: S608
                f"({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(
                f"refused to record observation {oid!r}: {exc}") from exc

        stored = self.get(oid)
        if stored is None:      # pragma: no cover - defensive
            raise RepositoryError(f"observation {oid!r} vanished after insert")
        return RecordedObservation(row=stored, created=True)

    # -- reads ----------------------------------------------------------------
    def get(self, observation_id_: str) -> Optional[MarketObservationRow]:
        record = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM historical_market_event_observations "  # noqa: S608
            "WHERE observation_id = ?", (observation_id_,)
        ).fetchone()
        return _row(record) if record is not None else None

    def for_namespace(
        self, *, provider: str, namespace_generation: str, league_id: str,
    ) -> list[MarketObservationRow]:
        """Every observation in one provider namespace, deterministically ordered.

        This is the audit's input. The ordering is total and content-derived so
        two runs over the same evidence see the same sequence regardless of
        rowid assignment or insertion order.
        """

        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM historical_market_event_observations "  # noqa: S608
            "WHERE provider = ? AND namespace_generation = ? AND league_id = ? "
            "ORDER BY provider_event_id, provider_snapshot_timestamp, "
            "observation_content_hash",
            (provider, namespace_generation, league_id),
        ).fetchall()
        return [_row(r) for r in rows]

    def for_event(
        self, *, provider: str, namespace_generation: str, provider_event_id: str,
    ) -> list[MarketObservationRow]:
        """Every observation of one exact event id, for audit use only.

        Exact key equality. There is no name lookup and no near-match: an id that
        is not exactly lowercase 32-hex is refused here rather than searched for,
        so a confusable can never quietly return the real event's rows.
        """

        validate_provider_event_id(provider_event_id)
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM historical_market_event_observations "  # noqa: S608
            "WHERE provider = ? AND namespace_generation = ? AND provider_event_id = ? "
            "ORDER BY provider_snapshot_timestamp, observation_content_hash",
            (provider, namespace_generation, provider_event_id),
        ).fetchall()
        return [_row(r) for r in rows]

    def distinct_event_ids(
        self, *, provider: str, namespace_generation: str, league_id: str,
    ) -> list[str]:
        """Sorted distinct event ids -- the audit's ``distinct_ids`` population."""

        rows = self._conn.execute(
            "SELECT DISTINCT provider_event_id FROM "
            "historical_market_event_observations WHERE provider = ? "
            "AND namespace_generation = ? AND league_id = ? "
            "ORDER BY provider_event_id",
            (provider, namespace_generation, league_id),
        ).fetchall()
        return [r["provider_event_id"] for r in rows]
