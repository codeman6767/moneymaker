"""Namespace-qualified official-provider constants (review repair RV3).

Why this module exists
----------------------
The canonical `games` table enforces
``UNIQUE (official_provider, official_game_key)``. That index is **global**: it
carries no league and no API generation, and the bare provider strings
(``mlb_statsapi``, ``balldontlie``) encode neither. The independent review proved
the consequence — the same ``(balldontlie, "12345")`` pair inserted for one league
refuses the identical pair for another, and a future API generation that re-issued
numeric ids would collide with v1 rows. BALLDONTLIE also serves more than one
sport under one brand, so this is a latent hazard rather than a hypothetical one.

The repair is **GAME-NAMESPACE-B**: the value stored in ``official_provider`` is
namespace-*qualified* — ``mlb_statsapi:mlb:v1`` — so the existing unique index
enforces exactly the intended (provider · sport · generation) key. Because
``games.official_provider`` is plain TEXT with no CHECK, this needs **no
migration**.

The qualified strings live here, once. They are never built by concatenating
pieces at a call site: a typo in one such expression would silently create a
second namespace that the unique index would then happily treat as distinct,
which is the exact failure this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .provenance import ATTESTED_GENERATIONS, EntityType, ProviderNamespace
from .sources import PROVIDER_LEAGUES, SourceCorpusError

__all__ = [
    "QUALIFIED_PROVIDERS",
    "QualifiedProvider",
    "qualified_provider",
    "qualified_provider_for",
]


@dataclass(frozen=True)
class QualifiedProvider:
    """One official provider namespace, qualified by sport and API generation."""

    provider: str
    sport: str
    generation: str
    league_id: str

    @property
    def value(self) -> str:
        """The exact string stored in ``games.official_provider``."""

        return f"{self.provider}:{self.sport}:{self.generation}"


#: Every official provider namespace this build can bootstrap canonical games in.
#:
#: Keyed by the qualified value itself so a lookup is total and a caller cannot
#: assemble a key that is absent here. Adding an entry is a reviewed change: it
#: asserts that this repository ingests that provider/sport/generation and that
#: its ids are audited under G5.
QUALIFIED_PROVIDERS: Final[dict[str, QualifiedProvider]] = {
    q.value: q for q in (
        QualifiedProvider("mlb_statsapi", "mlb", "v1", "lg_mlb"),
        QualifiedProvider("balldontlie", "nba", "v1", "lg_nba"),
    )
}

#: Reverse index: (league, provider, generation) -> qualified namespace.
_BY_TRIPLE: Final[dict[tuple[str, str, str], QualifiedProvider]] = {
    (q.league_id, q.provider, q.generation): q for q in QUALIFIED_PROVIDERS.values()
}


def qualified_provider(
    league_id: str, provider: str, generation: str
) -> QualifiedProvider:
    """Resolve the qualified namespace for one (league, provider, generation).

    Fails closed on anything unregistered. In particular a generation this build
    does not attest to is refused here as well as by ``ProviderNamespace``, so a
    caller cannot reach the canonical-game key with an unverified namespace.
    """

    expected_league = PROVIDER_LEAGUES.get(provider)
    if expected_league is None:
        raise SourceCorpusError(
            f"provider {provider!r} has no declared league; it cannot qualify a "
            "canonical-game namespace"
        )
    if expected_league != league_id:
        raise SourceCorpusError(
            f"provider {provider!r} serves {expected_league!r}, not {league_id!r}"
        )
    if generation not in ATTESTED_GENERATIONS.get(provider, frozenset()):
        raise SourceCorpusError(
            f"generation {generation!r} is not attested for provider {provider!r} "
            f"(attested: {sorted(ATTESTED_GENERATIONS.get(provider, ()))}). An "
            "unattested generation may never key a canonical game."
        )
    try:
        return _BY_TRIPLE[(league_id, provider, generation)]
    except KeyError:
        raise SourceCorpusError(
            f"no qualified provider namespace is registered for "
            f"({league_id!r}, {provider!r}, {generation!r}); register one in "
            "QUALIFIED_PROVIDERS rather than composing the string at a call site"
        ) from None


def qualified_provider_for(namespace: ProviderNamespace) -> QualifiedProvider:
    """The qualified namespace for a :class:`ProviderNamespace`.

    The entity type is deliberately ignored: game and team namespaces of one
    provider generation share one canonical-game provider key, because the key
    identifies the *source namespace*, not the entity class within it.
    """

    if namespace.entity_type not in (EntityType.GAME, EntityType.TEAM,
                                     EntityType.PLAYER):  # pragma: no cover
        raise SourceCorpusError(f"unknown entity type {namespace.entity_type!r}")
    return qualified_provider(namespace.league_id, namespace.provider,
                              namespace.generation)
