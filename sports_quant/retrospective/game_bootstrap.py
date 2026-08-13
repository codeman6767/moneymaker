"""Deterministic official-provider canonical-game bootstrap.

A canonical game may be created from retrospective evidence only when every one
of these holds (§21):

* the provider is the **designated official provider** for the league;
* an **ACCEPTED** G5 game audit exists for this corpus's **exact** source digest;
* the namespace generation is attested;
* both participating provider team ids have an exact **TEAM-A crosswalk** in this
  corpus version;
* the typed source evidence supplies the non-outcome metadata `games` requires;
* nothing conflicts with an existing canonical game.

Identity versus description
---------------------------
**Identity** is the namespace-qualified official provider plus the official game
key, and the two attested canonical teams. **Descriptive and mutable**: scheduled
start, original start, venue, status, reschedule metadata, and game number. A
reschedule updates description and can never mint a second canonical game, because
``UNIQUE (official_provider, official_game_key)`` already forbids it -- with the
qualified provider value (review repair RV3) that index now carries the league and
the API generation too.

**No score, winner or outcome participates in identity**, and the canonical
descriptive columns are *not* retrospective feature evidence: a future reader must
read historical typed observations, not these convenience fields.

Evidence strength
-----------------
The G5 game audit over the one-month corpora observed **no game id more than
once**, so a clean verdict there means "no contradiction detected", not "game ids
verified stable". This module inherits that and does not upgrade it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository
from ..db.schema import utc_now_iso
from .attestations import AttestationError
from .identity_audit import AuditPlan
from .namespaces import QualifiedProvider, qualified_provider_for
from .provenance import EntityType, ProviderNamespace
from .sources import GameObservation, iter_game_observations

__all__ = [
    "GAME_BOOTSTRAP_POLICY_VERSION",
    "GameBootstrapPlan",
    "GameBootstrapResult",
    "canonical_game_id",
    "plan_game_bootstrap",
    "write_game_bootstrap",
]

#: Distinct from the TEAM-A attestation policy and the player bootstrap policy:
#: the guarantees differ, so one generic string would let a material rule change
#: pass unversioned.
GAME_BOOTSTRAP_POLICY_VERSION: Final = "g5-game-bootstrap-v1"


def canonical_game_id(qualified: QualifiedProvider, official_game_key: str) -> str:
    """A canonical game id that is a pure function of the official namespace key.

    Deterministic and future-blind: the same key always yields the same id, so a
    rebuilt corpus reproduces every id and a corpus diff stays meaningful. The
    qualified provider already carries league, sport and API generation, so those
    participate without being spelled out separately.

    Nothing mutable is included -- no start time, venue, status or score.
    """

    key = "|".join(("canonical_game", qualified.league_id, qualified.value,
                    official_game_key))
    return "gm_r" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PlannedGame:
    """One canonical game the bootstrap would create or reuse."""

    provider_game_id: str
    canonical_game_id: str
    home_team_id: str
    away_team_id: str
    season: int
    game_date_local: str
    scheduled_start: str
    status: str
    game_number: int
    existing: bool = False


@dataclass(frozen=True)
class GameBootstrapPlan:
    """What the bootstrap would do, decided before anything is persisted."""

    namespace: ProviderNamespace
    qualified_provider: str
    ready: tuple[PlannedGame, ...] = ()
    unattested_team_ids: tuple[str, ...] = ()
    blocked_games: tuple[tuple[str, str], ...] = ()   # (provider_game_id, reason)
    #: The SOURCE evidence lacks a field the canonical row requires. Not fixable
    #: by preparing the output database -- it is a genuine evidence gap.
    missing_metadata: tuple[str, ...] = ()
    #: The OUTPUT database has no `seasons` row for the season the evidence
    #: names. Reported separately because it is an output-preparation gap, not an
    #: evidence gap: the bootstrap refuses to invent a canonical season rather
    #: than silently attaching games to the wrong one.
    missing_output_season: tuple[int, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_games)

    def as_json(self) -> dict[str, object]:
        return {
            "namespace": self.namespace.as_dict(),
            "qualified_provider": self.qualified_provider,
            "game_bootstrap_policy_version": GAME_BOOTSTRAP_POLICY_VERSION,
            "ready": len(self.ready),
            "already_present": sum(1 for g in self.ready if g.existing),
            "unattested_team_ids": list(self.unattested_team_ids),
            "missing_metadata": list(self.missing_metadata),
            "missing_output_season": list(self.missing_output_season),
            "blocked": [list(b) for b in self.blocked_games],
        }


@dataclass(frozen=True)
class GameBootstrapResult:
    plan: GameBootstrapPlan
    created: int = 0
    reused: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _team_crosswalk_index(
    output: sqlite3.Connection, *, corpus_version_id: str,
    namespace: ProviderNamespace,
) -> dict[str, str]:
    """Exact provider-team-id -> canonical team, from THIS corpus's crosswalks.

    Read from persisted TEAM-A provenance rather than from the map directly, so a
    game can only be built on team identity that was actually attested *and*
    written into this corpus version.
    """

    rows = output.execute(
        "SELECT provider_id, canonical_entity_id FROM static_crosswalk_provenance "
        "WHERE corpus_version_id = ? AND league_id = ? AND provider = ? "
        "AND namespace_generation = ? AND entity_type = 'team'",
        (corpus_version_id, namespace.league_id, namespace.provider,
         namespace.generation)).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _latest_observation(group: list[GameObservation]) -> GameObservation:
    """The descriptive snapshot to store, chosen deterministically.

    Latest ``observed_at``, tie-broken by the scheduled start then the local date,
    so the stored description is a function of the evidence and not of row order.
    This is a *descriptive* choice only -- identity does not depend on it.
    """

    return sorted(group, key=lambda o: (o.observed_at, o.scheduled_start or "",
                                        o.game_date_local or ""))[-1]


def plan_game_bootstrap(
    output: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    plan: AuditPlan,
    corpus_version_id: str,
) -> GameBootstrapPlan:
    """Decide every canonical game. Writes nothing."""

    namespace = plan.namespace
    if namespace.entity_type is not EntityType.GAME:
        raise AttestationError(
            f"game bootstrap plans game namespaces only, not "
            f"{namespace.entity_type.value!r}")
    if not plan.accepted:
        raise AttestationError(
            f"audit verdict is {plan.verdict.value!r}; only an accepted game audit "
            "may bootstrap canonical games")

    repo = SqliteRetrospectiveProvenanceRepository(output)
    corpus = repo.corpus_version(corpus_version_id)
    if corpus is None:
        raise AttestationError(f"unknown corpus version {corpus_version_id!r}")
    if corpus.source_corpus_digest != plan.source_corpus_digest:
        raise AttestationError(
            "the game audit examined a different source corpus than this corpus "
            "version is built over")

    qualified = qualified_provider_for(namespace)
    # Team identity comes from the TEAM namespace of the same provider generation.
    team_namespace = ProviderNamespace(namespace.league_id, namespace.provider,
                                       EntityType.TEAM, namespace.generation)
    teams = _team_crosswalk_index(output, corpus_version_id=corpus_version_id,
                                  namespace=team_namespace)

    by_game: dict[str, list[GameObservation]] = defaultdict(list)
    for observation in iter_game_observations(source, provider=namespace.provider):
        by_game[observation.provider_game_id].append(observation)

    season_id = _season_lookup(output, namespace.league_id)

    ready: list[PlannedGame] = []
    unattested: set[str] = set()
    blocked: list[tuple[str, str]] = []
    missing: list[str] = []
    missing_seasons: set[int] = set()

    for provider_game_id in sorted(by_game):
        group = by_game[provider_game_id]
        latest = _latest_observation(group)
        home_provider = latest.home_provider_team_id
        away_provider = latest.away_provider_team_id
        if home_provider is None or away_provider is None:
            missing.append(provider_game_id)
            continue
        home = teams.get(home_provider)
        away = teams.get(away_provider)
        if home is None or away is None:
            unattested.update(
                t for t in (home_provider, away_provider) if t not in teams)
            continue
        if (latest.season is None or latest.game_date_local is None
                or latest.scheduled_start is None or latest.mapped_status is None):
            missing.append(provider_game_id)
            continue
        if latest.season not in season_id:
            missing_seasons.add(int(latest.season))
            continue

        game_id = canonical_game_id(qualified, provider_game_id)
        existing = output.execute(
            "SELECT game_id, home_team_id, away_team_id FROM games "
            "WHERE official_provider = ? AND official_game_key = ?",
            (qualified.value, provider_game_id)).fetchone()
        if existing is not None:
            if (str(existing[1]), str(existing[2])) != (home, away):
                blocked.append((provider_game_id,
                                "existing canonical game has different teams"))
                continue
            if str(existing[0]) != game_id:
                blocked.append((provider_game_id,
                                "existing canonical game has a different id"))
                continue
        ready.append(PlannedGame(
            provider_game_id=provider_game_id, canonical_game_id=game_id,
            home_team_id=home, away_team_id=away, season=int(latest.season),
            game_date_local=latest.game_date_local,
            scheduled_start=latest.scheduled_start, status=latest.mapped_status,
            game_number=int(latest.game_number or 1),
            existing=existing is not None,
        ))

    return GameBootstrapPlan(
        namespace=namespace, qualified_provider=qualified.value,
        ready=tuple(ready), unattested_team_ids=tuple(sorted(unattested)),
        blocked_games=tuple(sorted(blocked)), missing_metadata=tuple(sorted(missing)),
        missing_output_season=tuple(sorted(missing_seasons)),
    )


def _season_lookup(output: sqlite3.Connection, league_id: str) -> dict[int, str]:
    rows = output.execute(
        "SELECT year, season_id FROM seasons WHERE league_id = ? AND phase='regular'",
        (league_id,)).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def write_game_bootstrap(
    output: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    plan: AuditPlan,
    corpus_version_id: str,
) -> GameBootstrapResult:
    """Create the canonical games one plan calls for. Caller owns the transaction."""

    game_plan = plan_game_bootstrap(output, source, plan=plan,
                                    corpus_version_id=corpus_version_id)
    if game_plan.blocked:
        raise AttestationError(
            f"{len(game_plan.blocked_games)} game(s) conflict with existing canonical "
            f"rows: {list(game_plan.blocked_games)}. Refusing rather than duplicating "
            "or overwriting a real event.")

    seasons = _season_lookup(output, plan.namespace.league_id)
    created = reused = 0
    now = utc_now_iso()
    for game in game_plan.ready:
        if game.existing:
            reused += 1
            continue
        output.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
            "away_team_id, scheduled_start, original_start, game_date_local, "
            "game_number, is_neutral_site, status, official_provider, "
            "official_game_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (game.canonical_game_id, plan.namespace.league_id,
             seasons[game.season], game.home_team_id, game.away_team_id,
             game.scheduled_start, game.scheduled_start, game.game_date_local,
             game.game_number, game.status, game_plan.qualified_provider,
             game.provider_game_id, now, now),
        )
        created += 1
    return GameBootstrapResult(plan=game_plan, created=created, reused=reused)
