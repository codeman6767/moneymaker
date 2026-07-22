"""Deterministic name normalization for team and player aliases.

One function, applied identically at alias-write time and at lookup time. If
those two ever diverge, a name written into the corpus stops matching itself,
so there is exactly one implementation and a golden-file test pins its output.

The pipeline is deliberately conservative. It does not stem, apply phonetics,
or expand abbreviations, because all three map genuinely distinct names onto
each other -- and in a betting corpus a wrong team match inverts the sign of a
position's edge while leaving the row looking perfectly well-formed. Unknown
names are refused, not guessed at; ``NY`` -> ``New York Yankees`` is an alias
table fact, reviewable and testable per team, not a transformation hidden in
code.

Ambiguity is a first-class result. :func:`resolve_alias` returns
``AMBIGUOUS`` rather than picking the first row, because "there are two Jalen
Williamses" is information the caller must act on.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass, field
from typing import Final, Iterable, Optional, Sequence

# Characters removed outright. An apostrophe joins the letters around it:
# "D'Angelo" is one word, and turning it into "d angelo" would not match a
# provider that writes "DAngelo".
_REMOVED_CHARS: Final = frozenset("'’ʼ`´")

# Characters replaced with a space. "St. Louis" -> "st louis";
# "Gilgeous-Alexander" -> "gilgeous alexander".
_SPACE_CHARS: Final = frozenset(".,-/\\_()[]{}:;|–—‐‑+*\"")

# Generational suffixes, recognised only as a trailing token.
_SUFFIXES: Final[tuple[str, ...]] = ("jr", "sr", "ii", "iii", "iv", "v")

#: Suffix value meaning "no generational suffix present".
NO_SUFFIX: Final = ""


@dataclass(frozen=True)
class NormalizedName:
    """The result of normalizing one raw name string."""

    #: Normalization output: lowercase, unaccented, punctuation-folded,
    #: whitespace-collapsed, with any trailing generational suffix removed.
    normalized: str
    #: Normalized generational suffix ('jr', 'iii', ...) or ``NO_SUFFIX``.
    suffix: str = NO_SUFFIX
    #: The input exactly as supplied, for storage alongside the normal form.
    original: str = ""

    @property
    def has_suffix(self) -> bool:
        return self.suffix != NO_SUFFIX

    def with_suffix(self) -> str:
        """Normalized form with the suffix reattached, for display/debugging."""

        return f"{self.normalized} {self.suffix}" if self.has_suffix else self.normalized


def strip_accents(value: str) -> str:
    """NFKD-decompose and drop combining marks.

    ``Acuña`` -> ``Acuna``, ``Dončić`` -> ``Doncic``, ``Jokić`` -> ``Jokic``.
    """

    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fold_punctuation(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch in _REMOVED_CHARS:
            continue
        out.append(" " if ch in _SPACE_CHARS else ch)
    return "".join(out)


def _collapse_initials(tokens: Sequence[str]) -> list[str]:
    """Join a run of single-character tokens into one token.

    ``"N.Y."`` folds to ``["n", "y"]``, which should match ``"NY"`` -> ``["ny"]``.
    A name composed *entirely* of single letters is an abbreviation by
    construction, so joining is safe. A name with any multi-letter token is left
    alone -- "J R Smith" must not collapse into "jrsmith".
    """

    if len(tokens) > 1 and all(len(t) == 1 for t in tokens):
        return ["".join(tokens)]
    return list(tokens)


def normalize_name(value: str, *, extract_suffix: bool = True) -> NormalizedName:
    """Normalize ``value`` deterministically.

    Steps, in fixed order: strip accents, casefold, expand ``&`` to ``and``,
    fold punctuation, collapse whitespace, collapse all-initial abbreviations,
    then split off a trailing generational suffix.

    Pure and locale-independent: no clock, no randomness, no set iteration.
    """

    text = strip_accents(value)
    text = text.casefold()
    text = text.replace("&", " and ")
    text = _fold_punctuation(text)

    tokens = [t for t in text.split() if t]

    suffix = NO_SUFFIX
    if extract_suffix and len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        suffix = tokens[-1]
        tokens = tokens[:-1]

    tokens = _collapse_initials(tokens)
    return NormalizedName(normalized=" ".join(tokens), suffix=suffix, original=value)


def normalized_key(value: str) -> str:
    """Convenience: just the normalized string, suffix removed."""

    return normalize_name(value).normalized


# --------------------------------------------------------------------------- #
# Alias resolution
# --------------------------------------------------------------------------- #
class AliasMatchStatus(str, enum.Enum):
    """Outcome of resolving a raw name against alias rows.

    Mirrors ``intel.player_matching.MatchStatus``; Phase D unifies the two
    behind the shared matcher. The vocabulary is identical on purpose.
    """

    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class AliasCandidate:
    """One alias row considered during resolution."""

    entity_id: str
    alias: str
    normalized: str
    alias_type: str
    provider: str = ""
    suffix: str = NO_SUFFIX
    is_ambiguous: bool = False


@dataclass(frozen=True)
class AliasResolution:
    """The result of resolving a raw name.

    ``entity_id`` is populated only when ``status`` is ``MATCHED``; every other
    status carries a ``reason`` instead. Callers must branch on ``status`` and
    never read ``entity_id`` unconditionally.
    """

    status: AliasMatchStatus
    entity_id: Optional[str] = None
    candidates: tuple[AliasCandidate, ...] = field(default_factory=tuple)
    reason: Optional[str] = None
    query: str = ""
    query_suffix: str = NO_SUFFIX
    #: The season the caller asked about, or None if it did not supply one.
    season_year: Optional[int] = None
    #: Whether candidates were filtered by their season-validity window. False
    #: means historical validity was **not** checked -- see
    #: :attr:`season_validity_verified`.
    season_scoped: bool = False
    #: Whether every surviving candidate carries a curated validity window.
    #: False means at least one candidate is stored unbounded, so a match does
    #: not prove the alias was actually in use that season. Unbounded seed
    #: aliases await Phase D curation (ENTITY_MATCHING.md §3.2).
    season_validity_verified: bool = False

    @property
    def is_matched(self) -> bool:
        return self.status is AliasMatchStatus.MATCHED

    def matched_id(self) -> Optional[str]:
        """The entity id, or ``None`` unless this resolution actually matched.

        Pairs status and payload in one check so a caller cannot read an id off
        an ambiguous result.
        """

        return self.entity_id if self.is_matched else None


def resolve_alias(
    raw_name: str,
    candidates: Iterable[AliasCandidate],
    *,
    provider: Optional[str] = None,
) -> AliasResolution:
    """Resolve ``raw_name`` against ``candidates`` deterministically.

    ``candidates`` are alias rows already filtered to the relevant league; this
    function applies the normalization and the refusal rules.

    Resolution order:

    1. Provider-scoped normalized match, when ``provider`` is given. A provider's
       own spelling beats a generic alias.
    2. Unscoped normalized match.

    A tier producing two or more distinct entities yields ``AMBIGUOUS``. It does
    **not** fall through to a weaker tier: a lower tier cannot resolve an
    ambiguity a stronger one could not, and trying is how a wrong answer gets
    manufactured. Any candidate flagged ``is_ambiguous`` forces ``AMBIGUOUS``
    even when only one row matches, because the flag records that the alias is
    known to be shared.
    """

    query = normalize_name(raw_name)
    if not query.normalized:
        return AliasResolution(
            status=AliasMatchStatus.UNMATCHED,
            reason="empty name after normalization",
            query=query.normalized,
            query_suffix=query.suffix,
        )

    pool = [c for c in candidates if c.normalized == query.normalized]

    # A suffix present in the input is binding: "Vladimir Guerrero Jr." must
    # never resolve to the father. A suffix absent from the input is permissive.
    if query.suffix != NO_SUFFIX:
        pool = [c for c in pool if c.suffix == query.suffix]

    tiers: list[tuple[str, list[AliasCandidate]]] = []
    if provider:
        tiers.append((f"provider={provider}", [c for c in pool if c.provider == provider]))
    tiers.append(("unscoped", pool))

    for tier_name, tier in tiers:
        if not tier:
            continue
        entity_ids = sorted({c.entity_id for c in tier})
        if len(entity_ids) > 1:
            return AliasResolution(
                status=AliasMatchStatus.AMBIGUOUS,
                candidates=tuple(_ordered(tier)),
                reason=(
                    f"{len(entity_ids)} entities share the normalized alias "
                    f"{query.normalized!r} ({tier_name})"
                ),
                query=query.normalized,
                query_suffix=query.suffix,
            )
        if any(c.is_ambiguous for c in tier):
            return AliasResolution(
                status=AliasMatchStatus.AMBIGUOUS,
                candidates=tuple(_ordered(tier)),
                reason=(
                    f"alias {query.normalized!r} is flagged ambiguous; "
                    "it needs an additional discriminator"
                ),
                query=query.normalized,
                query_suffix=query.suffix,
            )
        return AliasResolution(
            status=AliasMatchStatus.MATCHED,
            entity_id=entity_ids[0],
            candidates=tuple(_ordered(tier)),
            query=query.normalized,
            query_suffix=query.suffix,
        )

    return AliasResolution(
        status=AliasMatchStatus.UNMATCHED,
        reason=f"no alias matches {query.normalized!r}",
        query=query.normalized,
        query_suffix=query.suffix,
    )


def _ordered(candidates: Iterable[AliasCandidate]) -> list[AliasCandidate]:
    """Stable candidate ordering, so a decision reads identically every run."""

    return sorted(candidates, key=lambda c: (c.entity_id, c.alias_type, c.provider, c.alias))
