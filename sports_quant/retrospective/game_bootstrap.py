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
from typing import Final, Optional

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
    #: Game static-crosswalk rows written (one per ready game, new or reused).
    #: This is the per-corpus, per-audit binding a future reader needs; the
    #: canonical `games` row alone is global and says nothing about either.
    provenance_written: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def legacy_equivalent_providers(qualified: QualifiedProvider) -> tuple[str, ...]:
    """Official-provider strings that denote the SAME authority as ``qualified``.

    Review repair (§7/§8). The conventional matcher writes the bare provider
    string (``mlb_statsapi``); TEAM-A writes the namespace-qualified value
    (``mlb_statsapi:mlb:v1``). ``UNIQUE (official_provider, official_game_key)``
    treats those as unrelated, so without an explicit equivalence rule the same
    real game can be canonicalized twice.

    The rule is deliberately narrow and total:

    * ONLY the bare provider of this exact qualified value is equivalent, and
      only for the league that provider is the designated official provider for;
    * it is a *read* equivalence for convergence. Historical rows are never
      rewritten -- mass-updating canonical games is out of scope and would need
      its own review;
    * new Lane-R games are always written under the qualified representation.

    Arbitrary provider strings are never equated: anything not derived from a
    registered ``QualifiedProvider`` is simply absent from this tuple.
    """

    return (qualified.value, qualified.provider)


def _find_existing_game(
    output: sqlite3.Connection, qualified: QualifiedProvider, official_game_key: str
) -> Optional[sqlite3.Row]:
    """The canonical game for this official key under any equivalent provider.

    Fails closed on ambiguity: if both the qualified and the legacy bare row
    exist and they are not the same game, this is a real split identity that a
    human must resolve, not something to pick a winner for.
    """

    rows = output.execute(
        "SELECT game_id, home_team_id, away_team_id, league_id, season_id, "
        "       official_provider "
        "FROM games WHERE official_game_key = ? AND official_provider IN "
        "(" + ",".join("?" * len(legacy_equivalent_providers(qualified))) + ") "
        "ORDER BY official_provider",
        (official_game_key, *legacy_equivalent_providers(qualified))).fetchall()
    if len(rows) > 1:
        distinct = {str(r["game_id"]) for r in rows}
        if len(distinct) > 1:
            raise AttestationError(
                f"official game key {official_game_key!r} already denotes "
                f"{sorted(distinct)} under equivalent providers "
                f"{[str(r['official_provider']) for r in rows]}; refusing to "
                "choose between two canonical games for one real event")
    return rows[0] if rows else None


def _require_persisted_accepted_audit(
    output: sqlite3.Connection, plan: AuditPlan, identity_audit_id: str
) -> str:
    """The audit row that actually cleared this namespace. Returns its digest.

    Review repair (§4). Before this, the bootstrap trusted ``plan.accepted`` on
    an in-memory dataclass, so anything that could construct an ``AuditPlan``
    could mint canonical games no G5 audit had ever cleared. Team crosswalks
    were already held to the persisted standard by schema triggers; games were
    not, because they never cited an audit at all.
    """

    row = output.execute(
        "SELECT semantic_digest, source_corpus_digest FROM identity_audit_records "
        "WHERE identity_audit_id = ? AND verdict = 'accepted' AND league_id = ? "
        "  AND provider = ? AND namespace_generation = ? AND entity_type = 'game'",
        (identity_audit_id, plan.namespace.league_id, plan.namespace.provider,
         plan.namespace.generation)).fetchone()
    if row is None:
        raise AttestationError(
            f"no persisted ACCEPTED game audit {identity_audit_id!r} exists for "
            f"({plan.namespace.league_id}, {plan.namespace.provider}, "
            f"{plan.namespace.generation}, game). A canonical game may not be "
            "created because a caller supplied an object claiming ACCEPTED.")
    if str(row["source_corpus_digest"]) != plan.source_corpus_digest:
        raise AttestationError(
            "the persisted game audit was taken over a different source corpus "
            "than this plan; a narrower audit never transfers to a wider "
            "reconstruction")
    return str(row["semantic_digest"])


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
        # Convergence (§7): look under the qualified value AND the legacy bare
        # provider the conventional matcher writes, so one real game is never
        # canonicalized twice.
        existing = _find_existing_game(output, qualified, provider_game_id)
        legacy = False
        if existing is not None:
            legacy = str(existing["official_provider"]) != qualified.value
            if (str(existing["home_team_id"]),
                    str(existing["away_team_id"])) != (home, away):
                blocked.append((provider_game_id,
                                "existing canonical game has different teams"))
                continue
            # Identity contradictions (§9). Teams agreeing is not enough: a
            # canonical row in the wrong league or the wrong season is corrupt,
            # and counting it as a valid replay would launder that corruption.
            if str(existing["league_id"]) != namespace.league_id:
                blocked.append((provider_game_id,
                                "existing canonical game is in league "
                                f"{existing['league_id']!r}, not "
                                f"{namespace.league_id!r}"))
                continue
            if str(existing["season_id"]) != season_id[int(latest.season)]:
                blocked.append((provider_game_id,
                                "existing canonical game is in season "
                                f"{existing['season_id']!r}, but the evidence "
                                f"says season {latest.season}"))
                continue
            # A legacy bare-provider row keeps its own id: it is the same real
            # game, and rewriting canonical ids is never a convergence action.
            if not legacy and str(existing["game_id"]) != game_id:
                blocked.append((provider_game_id,
                                "existing canonical game has a different id"))
                continue
            game_id = str(existing["game_id"])
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
    identity_audit_id: str,
) -> GameBootstrapResult:
    """Create the canonical games one plan calls for. Caller owns the transaction.

    ``identity_audit_id`` is required (review repair §4): it must name a real
    persisted ACCEPTED game audit for this exact namespace and source corpus.
    Every canonical game also gets a ``static_crosswalk_provenance`` row (review
    decision GAME-PROV-C, §5) so a future reader can prove *provider game G in
    corpus version C resolves to canonical game X under audit A* -- which the
    global ``games.official_provider``/``official_game_key`` pair cannot say,
    because it carries no corpus and no audit.

    The canonical game and its provenance row are written inside the caller's
    transaction, so a failure at any point leaves neither behind.
    """

    audit_digest = _require_persisted_accepted_audit(output, plan, identity_audit_id)
    game_plan = plan_game_bootstrap(output, source, plan=plan,
                                    corpus_version_id=corpus_version_id)
    if game_plan.blocked:
        raise AttestationError(
            f"{len(game_plan.blocked_games)} game(s) conflict with existing canonical "
            f"rows: {list(game_plan.blocked_games)}. Refusing rather than duplicating "
            "or overwriting a real event.")

    repo = SqliteRetrospectiveProvenanceRepository(output)
    seasons = _season_lookup(output, plan.namespace.league_id)
    created = reused = provenance = 0
    now = utc_now_iso()
    for game in game_plan.ready:
        if not game.existing:
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
        else:
            reused += 1

        prior = repo.static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=plan.namespace,
            provider_id=game.provider_game_id)
        if prior is not None and prior.canonical_entity_id != game.canonical_game_id:
            raise AttestationError(
                f"game key {game.provider_game_id!r} is already bound to "
                f"{prior.canonical_entity_id!r} in corpus {corpus_version_id!r}")
        repo.record_static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=plan.namespace,
            provider_id=game.provider_game_id,
            canonical_entity_id=game.canonical_game_id,
            identity_audit_id=identity_audit_id,
            provenance_policy_version=GAME_BOOTSTRAP_POLICY_VERSION,
        )
        provenance += 1

    del audit_digest   # validated above; the repository re-reads it from the row
    return GameBootstrapResult(plan=game_plan, created=created, reused=reused,
                               provenance_written=provenance)
