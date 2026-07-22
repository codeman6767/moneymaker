"""Deterministic alias normalization and ambiguity refusal.

The parametrized cases below act as a golden file: changing normalization shows
up here as a reviewable diff rather than as a silent corpus-wide behaviour
change.
"""

from __future__ import annotations

import pytest

from sports_quant.db.normalize import (
    NO_SUFFIX,
    AliasCandidate,
    AliasMatchStatus,
    normalize_name,
    normalized_key,
    resolve_alias,
    strip_accents,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Case differences.
        ("Yankees", "yankees"),
        ("YANKEES", "yankees"),
        ("yAnKeEs", "yankees"),
        # Surrounding and repeated internal whitespace.
        ("  Red   Sox  ", "red sox"),
        ("\tBlue\n\nJays ", "blue jays"),
        # Punctuation: periods and hyphens become spaces.
        ("St. Louis Cardinals", "st louis cardinals"),
        ("Shai Gilgeous-Alexander", "shai gilgeous alexander"),
        ("Trail Blazers", "trail blazers"),
        # Apostrophes join, they do not split.
        ("D'Angelo Russell", "dangelo russell"),
        ("A's", "as"),
        # Accents.
        ("Ronald Acuña", "ronald acuna"),
        ("Luka Dončić", "luka doncic"),
        ("Nikola Jokić", "nikola jokic"),
        # Ampersand.
        ("Black & Gold", "black and gold"),
        # Common abbreviation formatting: an all-initials name folds together.
        ("N.Y.", "ny"),
        ("L.A.", "la"),
        ("N. Y.", "ny"),
        # ... but a name with any multi-letter token is left alone.
        ("J R Smith", "j r smith"),
    ],
)
def test_normalization_golden_cases(raw: str, expected: str) -> None:
    assert normalize_name(raw).normalized == expected


def test_normalization_is_idempotent() -> None:
    for raw in ("St. Louis Cardinals", "Ronald Acuña Jr.", "  Red   Sox  ", "N.Y."):
        once = normalized_key(raw)
        assert normalized_key(once) == once


def test_normalization_is_deterministic_across_calls() -> None:
    values = {normalize_name("Shai Gilgeous-Alexander").normalized for _ in range(200)}
    assert len(values) == 1


@pytest.mark.parametrize(
    "raw,expected_name,expected_suffix",
    [
        ("Ronald Acuna Jr.", "ronald acuna", "jr"),
        ("Ken Griffey Sr", "ken griffey", "sr"),
        ("Vladimir Guerrero Jr.", "vladimir guerrero", "jr"),
        ("Robert Griffin III", "robert griffin", "iii"),
        ("Gary Payton II", "gary payton", "ii"),
        ("Ronald Acuna", "ronald acuna", NO_SUFFIX),
        # A bare suffix is a name, not a suffix -- there is nothing to qualify.
        ("Jr", "jr", NO_SUFFIX),
    ],
)
def test_suffix_extraction(raw: str, expected_name: str, expected_suffix: str) -> None:
    parsed = normalize_name(raw)
    assert parsed.normalized == expected_name
    assert parsed.suffix == expected_suffix


def test_suffix_can_be_disabled() -> None:
    parsed = normalize_name("Ronald Acuna Jr.", extract_suffix=False)
    assert parsed.normalized == "ronald acuna jr"
    assert parsed.suffix == NO_SUFFIX


def test_with_suffix_round_trip() -> None:
    assert normalize_name("Ronald Acuna Jr.").with_suffix() == "ronald acuna jr"
    assert normalize_name("Ronald Acuna").with_suffix() == "ronald acuna"


def test_strip_accents_leaves_ascii_untouched() -> None:
    assert strip_accents("Plain Name") == "Plain Name"


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _candidate(entity_id: str, alias: str, **kwargs: object) -> AliasCandidate:
    parsed = normalize_name(alias)
    return AliasCandidate(
        entity_id=entity_id,
        alias=alias,
        normalized=parsed.normalized,
        alias_type=str(kwargs.get("alias_type", "full")),
        provider=str(kwargs.get("provider", "")),
        suffix=str(kwargs.get("suffix", parsed.suffix)),
        is_ambiguous=bool(kwargs.get("is_ambiguous", False)),
    )


def test_single_candidate_matches() -> None:
    result = resolve_alias("Yankees", [_candidate("tm_mlb_nyy", "Yankees")])
    assert result.status is AliasMatchStatus.MATCHED
    assert result.matched_id() == "tm_mlb_nyy"


def test_no_candidate_is_unmatched() -> None:
    result = resolve_alias("Nobody", [_candidate("tm_mlb_nyy", "Yankees")])
    assert result.status is AliasMatchStatus.UNMATCHED
    assert result.matched_id() is None
    assert result.reason is not None


def test_two_entities_sharing_a_name_are_ambiguous_not_first_row() -> None:
    """The two-Jalen-Williamses case: refuse, never pick one."""

    candidates = [
        _candidate("pl_a", "Jalen Williams"),
        _candidate("pl_b", "Jalen Williams"),
    ]
    result = resolve_alias("Jalen Williams", candidates)
    assert result.status is AliasMatchStatus.AMBIGUOUS
    assert result.matched_id() is None
    assert "2 entities" in (result.reason or "")
    assert len(result.candidates) == 2


def test_ambiguity_is_order_independent() -> None:
    a = _candidate("pl_a", "Jalen Williams")
    b = _candidate("pl_b", "Jalen Williams")
    forward = resolve_alias("Jalen Williams", [a, b])
    reverse = resolve_alias("Jalen Williams", [b, a])
    assert forward.status is reverse.status is AliasMatchStatus.AMBIGUOUS
    assert forward.candidates == reverse.candidates


def test_flagged_alias_is_ambiguous_even_with_one_row() -> None:
    """A row flagged ambiguous refuses on its own: the flag records shared use."""

    result = resolve_alias("NY", [_candidate("tm_mlb_nyy", "NY", is_ambiguous=True)])
    assert result.status is AliasMatchStatus.AMBIGUOUS
    assert "flagged ambiguous" in (result.reason or "")


def test_provider_scope_wins_over_unscoped() -> None:
    candidates = [
        _candidate("tm_a", "Big Apple", provider="the_odds_api"),
        _candidate("tm_b", "Big Apple"),
    ]
    scoped = resolve_alias("Big Apple", candidates, provider="the_odds_api")
    assert scoped.status is AliasMatchStatus.MATCHED
    assert scoped.matched_id() == "tm_a"
    # Without the provider scope the same inputs are genuinely ambiguous.
    unscoped = resolve_alias("Big Apple", candidates)
    assert unscoped.status is AliasMatchStatus.AMBIGUOUS


def test_ambiguous_tier_does_not_fall_through_to_a_weaker_tier() -> None:
    """A weaker tier cannot resolve what a stronger one could not."""

    candidates = [
        _candidate("tm_a", "Shared", provider="p1"),
        _candidate("tm_b", "Shared", provider="p1"),
        _candidate("tm_c", "Shared"),
    ]
    result = resolve_alias("Shared", candidates, provider="p1")
    assert result.status is AliasMatchStatus.AMBIGUOUS
    assert "provider=p1" in (result.reason or "")


def test_present_suffix_is_binding() -> None:
    """'Guerrero Jr.' must never resolve to the father."""

    candidates = [
        _candidate("pl_father", "Vladimir Guerrero"),
        _candidate("pl_son", "Vladimir Guerrero Jr."),
    ]
    result = resolve_alias("Vladimir Guerrero Jr.", candidates)
    assert result.status is AliasMatchStatus.MATCHED
    assert result.matched_id() == "pl_son"


def test_absent_suffix_is_permissive_when_unambiguous() -> None:
    result = resolve_alias("Ronald Acuna", [_candidate("pl_x", "Ronald Acuna Jr.")])
    assert result.status is AliasMatchStatus.MATCHED
    assert result.matched_id() == "pl_x"


def test_absent_suffix_is_ambiguous_when_both_generations_exist() -> None:
    """The 1995 Ken Griffey case: both were active, so refuse."""

    candidates = [
        _candidate("pl_sr", "Ken Griffey Sr."),
        _candidate("pl_jr", "Ken Griffey Jr."),
    ]
    result = resolve_alias("Ken Griffey", candidates)
    assert result.status is AliasMatchStatus.AMBIGUOUS


def test_empty_name_is_unmatched_not_crashing() -> None:
    result = resolve_alias("   ", [_candidate("tm_a", "Yankees")])
    assert result.status is AliasMatchStatus.UNMATCHED
    assert "empty name" in (result.reason or "")


def test_matched_id_pairs_status_and_payload() -> None:
    ambiguous = resolve_alias(
        "Shared", [_candidate("a", "Shared"), _candidate("b", "Shared")]
    )
    # entity_id is never populated on a non-match, and matched_id() enforces it.
    assert ambiguous.entity_id is None
    assert ambiguous.matched_id() is None
    assert ambiguous.is_matched is False
