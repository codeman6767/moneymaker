"""Shared identity-observation recording for the official ingestors (Phase F1).

Both the MLB and the NBA ingestor drive identity recording through this one
class, so "what counts as an identity observation" has a single answer. In
persist mode it writes through :class:`SqliteProviderIdentityRepository`; in
dry-run mode it holds no connection and counts the same would-be rows using the
*same* ``prepare_*`` normalization and content hashing, which is what keeps the
dry-run counters honest instead of approximate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..db.repositories.base import RepositoryError
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.identity import (
    SqliteProviderIdentityRepository,
    prepare_player_identity,
    prepare_team_identity,
)
from ..db.repositories.observations import ObservationOutcome
from .identity_extract import LEAGUE_BY_PROVIDER, extract_identities

#: Raised as a DQ note, not an error: a provider sending an identity object with
#: an id and no name is a provider fact worth recording, not a crash.
DQ_IDENTITY_MISSING_NAME = "DQ-IDENTITY-001"
#: Two different names for one entity at the same instant.
DQ_IDENTITY_EQUAL_TIME = "DQ-IDENTITY-002"


@dataclass
class IdentityCounts:
    """Per-run identity-observation counters (reported by both ingestors)."""

    team_identities_inserted: int = 0
    team_identities_unchanged: int = 0
    player_identities_inserted: int = 0
    player_identities_unchanged: int = 0
    identities_rejected: int = 0
    identity_endpoints_unsupported: int = 0

    @property
    def inserted(self) -> int:
        return self.team_identities_inserted + self.player_identities_inserted


@dataclass
class IdentityRecorder:
    """Records (or, in dry-run, counts) identity observations from raw payloads."""

    conn: Optional[sqlite3.Connection] = None
    dry_run: bool = False
    counts: IdentityCounts = field(default_factory=IdentityCounts)
    #: Dry-run only: distinct (kind, provider id, observed_at, content hash) already
    #: counted, mirroring the e017 UNIQUE key exactly so a dry run reports the same
    #: number of would-be rows that persistence writes.
    _seen: set[tuple[str, str, str, str]] = field(default_factory=set)

    def observe_response(
        self,
        *,
        provider: str,
        endpoint: str,
        body: str,
        raw_response_id: str,
        raw_response_hash: str,
        observed_at: str,
    ) -> IdentityCounts:
        """Extract and record every identity one stored raw response contains.

        Never raises on payload content: a malformed body or a nameless entity is
        counted as rejected and, when persisting, recorded as a DQ note.
        """

        league_id = LEAGUE_BY_PROVIDER.get(provider)
        if league_id is None:
            self.counts.identity_endpoints_unsupported += 1
            return self.counts

        extracted = extract_identities(provider=provider, endpoint=endpoint, body=body)
        for rejection in extracted.rejected:
            if rejection.kind in ("endpoint", "provider"):
                self.counts.identity_endpoints_unsupported += 1
            else:
                self.counts.identities_rejected += 1
                self._dq_note(
                    entity_type=rejection.kind,
                    entity_id=rejection.provider_entity_id or endpoint,
                    provider=provider,
                    description=f"identity observation skipped: {rejection.reason}",
                )

        for team in extracted.teams:
            try:
                prepared = prepare_team_identity(
                    provider=provider, provider_team_id=team.provider_team_id,
                    league_id=league_id, full_name=team.full_name,
                    abbreviation=team.abbreviation, city=team.city,
                    nickname=team.nickname,
                )
            except RepositoryError as exc:
                self._reject("team", team.provider_team_id, provider, exc)
                continue
            if self.dry_run:
                self._count_dry("team", team.provider_team_id, observed_at,
                                prepared.content_hash)
                continue
            assert self.conn is not None  # noqa: S101 - persist mode holds a connection
            _identity, outcome = SqliteProviderIdentityRepository(self.conn).record_team(
                provider=provider, provider_team_id=team.provider_team_id,
                league_id=league_id, full_name=team.full_name,
                abbreviation=team.abbreviation, city=team.city, nickname=team.nickname,
                observed_at=observed_at, raw_response_id=raw_response_id,
                raw_response_hash=raw_response_hash,
            )
            if outcome is ObservationOutcome.INSERTED:
                self.counts.team_identities_inserted += 1
            else:
                self.counts.team_identities_unchanged += 1

        for player in extracted.players:
            try:
                prepared_p = prepare_player_identity(
                    provider=provider, provider_player_id=player.provider_player_id,
                    league_id=league_id, full_name=player.full_name,
                    first_name=player.first_name, last_name=player.last_name,
                    birth_date=player.birth_date, position=player.position,
                    provider_team_id=player.provider_team_id,
                )
            except RepositoryError as exc:
                self._reject("player", player.provider_player_id, provider, exc)
                continue
            if self.dry_run:
                self._count_dry("player", player.provider_player_id, observed_at,
                                prepared_p.content_hash)
                continue
            assert self.conn is not None  # noqa: S101 - persist mode holds a connection
            _player, outcome = SqliteProviderIdentityRepository(self.conn).record_player(
                provider=provider, provider_player_id=player.provider_player_id,
                league_id=league_id, full_name=player.full_name,
                first_name=player.first_name, last_name=player.last_name,
                birth_date=player.birth_date, position=player.position,
                provider_team_id=player.provider_team_id, observed_at=observed_at,
                raw_response_id=raw_response_id, raw_response_hash=raw_response_hash,
            )
            if outcome is ObservationOutcome.INSERTED:
                self.counts.player_identities_inserted += 1
            else:
                self.counts.player_identities_unchanged += 1
        return self.counts

    def report_equal_time_conflicts(self, provider: str) -> int:
        """Raise a DQ issue per equal-timestamp identity contradiction.

        ``latest_*`` still answers deterministically, but a provider that wrote
        two different names for one entity at one instant must be visible rather
        than resolved silently by a hash comparison.
        """

        if self.dry_run or self.conn is None:
            return 0
        repo = SqliteProviderIdentityRepository(self.conn)
        found = 0
        for kind in ("team", "player"):
            for conflict in repo.equal_time_conflicts(kind):
                if conflict.provider != provider:
                    continue
                found += 1
                self._dq_issue(
                    entity_type=kind, entity_id=conflict.provider_entity_id,
                    provider=provider,
                    description=(
                        f"{conflict.contents} conflicting provider identity names at "
                        f"{conflict.observed_at}; latest-as-of resolved by content hash"
                    ),
                )
        return found

    # -- internals ----------------------------------------------------------- #
    def _count_dry(
        self, kind: str, entity_id: str, observed_at: str, content_hash: str
    ) -> None:
        key = (kind, entity_id, observed_at, content_hash)
        if key in self._seen:
            if kind == "team":
                self.counts.team_identities_unchanged += 1
            else:
                self.counts.player_identities_unchanged += 1
            return
        self._seen.add(key)
        if kind == "team":
            self.counts.team_identities_inserted += 1
        else:
            self.counts.player_identities_inserted += 1

    def _reject(
        self, kind: str, entity_id: str, provider: str, exc: RepositoryError
    ) -> None:
        self.counts.identities_rejected += 1
        self._dq_note(entity_type=kind, entity_id=entity_id, provider=provider,
                      description=f"identity observation rejected: {exc}")

    def _dq_note(
        self, *, entity_type: str, entity_id: str, provider: str, description: str
    ) -> None:
        if self.dry_run or self.conn is None:
            return
        SqliteDataQualityRepository(self.conn).record(
            severity="note", rule_code=DQ_IDENTITY_MISSING_NAME, entity_type=entity_type,
            entity_id=entity_id, provider=provider, description=description,
        )

    def _dq_issue(
        self, *, entity_type: str, entity_id: str, provider: str, description: str
    ) -> None:
        if self.dry_run or self.conn is None:
            return
        SqliteDataQualityRepository(self.conn).record(
            severity="issue", rule_code=DQ_IDENTITY_EQUAL_TIME, entity_type=entity_type,
            entity_id=entity_id, provider=provider, description=description,
        )

#: Deterministic orders a caller may replay a corpus in. The point of offering
#: more than one is that the RESULT must not depend on the choice -- the
#: determinism tests and the offline pilot replay both rely on that.
REPLAY_ORDERS: tuple[str, ...] = ("received", "received_desc", "endpoint", "endpoint_desc")


def replay_identities_from_corpus(
    conn: sqlite3.Connection,
    *,
    provider: str,
    order: str = "received",
    recorder: Optional[IdentityRecorder] = None,
) -> IdentityCounts:
    """Record identity observations from raw responses already in the corpus.

    This is the backfill path for a corpus ingested before e017 existed: the
    provider-written names were preserved in ``raw_responses`` all along, so no
    provider request is needed to recover them. It runs the same extraction and
    the same recorder as live ingestion, which is what makes an offline replay
    evidence about production behaviour rather than about a test double.

    ``order`` varies only the traversal; because recording is keyed on content
    hash and every extractor emits in a total order, all orders converge on the
    same rows. Only successful (2xx) responses are replayed -- an error body is
    not an identity observation.
    """

    if order not in REPLAY_ORDERS:
        raise ValueError(f"unknown replay order {order!r}; expected one of {REPLAY_ORDERS}")
    sort = {
        "received": "received_at ASC, raw_response_id ASC",
        "received_desc": "received_at DESC, raw_response_id DESC",
        "endpoint": "endpoint ASC, received_at ASC, raw_response_id ASC",
        "endpoint_desc": "endpoint DESC, received_at DESC, raw_response_id DESC",
    }[order]
    rows = conn.execute(
        "SELECT raw_response_id, endpoint, body, content_hash, received_at "
        "FROM raw_responses WHERE provider = ? AND http_status >= 200 AND http_status < 300 "
        f"ORDER BY {sort}",
        (provider,),
    ).fetchall()
    active = recorder if recorder is not None else IdentityRecorder(conn=conn)
    for row in rows:
        active.observe_response(
            provider=provider, endpoint=str(row["endpoint"]), body=str(row["body"]),
            raw_response_id=str(row["raw_response_id"]),
            raw_response_hash=str(row["content_hash"]),
            observed_at=str(row["received_at"]),
        )
    active.report_equal_time_conflicts(provider)
    return active.counts
