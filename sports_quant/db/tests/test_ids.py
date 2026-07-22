"""Canonical identifier construction: determinism, prefixes, ULID ordering."""

from __future__ import annotations

import pytest

from sports_quant.db import ids


def test_league_id_is_deterministic() -> None:
    assert ids.league_id("MLB") == "lg_mlb"
    assert ids.league_id("NBA") == "lg_nba"
    assert ids.league_id("MLB") == ids.league_id("mlb")


def test_team_id_is_deterministic_and_league_scoped() -> None:
    assert ids.team_id("MLB", "NYY") == "tm_mlb_nyy"
    assert ids.team_id("NBA", "BOS") == "tm_nba_bos"
    # The same abbreviation in two leagues is two distinct teams.
    assert ids.team_id("MLB", "BOS") != ids.team_id("NBA", "BOS")


def test_season_id_includes_the_phase() -> None:
    """A league runs three phases in one year; the id must separate them."""

    assert ids.season_id("MLB", 2026, "regular") == "sn_mlb_2026_regular"
    assert ids.season_id("MLB", 2026, "postseason") == "sn_mlb_2026_postseason"
    assert ids.season_id("MLB", 2026, "regular") != ids.season_id("MLB", 2026, "preseason")


def test_deterministic_ids_survive_punctuation_and_case() -> None:
    assert ids.team_id("MLB", "St. Louis") == ids.team_id("mlb", "st louis")


def test_empty_slug_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot build an identifier slug"):
        ids.team_id("MLB", "!!!")


def test_ulid_shape() -> None:
    value = ids.new_ulid()
    assert len(value) == ids.ULID_LENGTH == 26
    assert set(value) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulids_are_unique() -> None:
    values = {ids.new_ulid() for _ in range(5_000)}
    assert len(values) == 5_000


def test_ulids_are_monotonic_within_a_millisecond() -> None:
    """Two ids minted in the same millisecond must still sort deterministically.

    Without this, a rebuilt corpus can order rows differently from the
    original, breaking deterministic tie-breaks in as-of queries.
    """

    values = [ids.new_ulid() for _ in range(1_000)]
    assert values == sorted(values)


def test_surrogate_ids_carry_their_prefix() -> None:
    assert ids.new_player_id().startswith(ids.PLAYER_PREFIX)
    assert ids.new_game_id().startswith(ids.GAME_PREFIX)
    assert ids.new_team_alias_id().startswith(ids.TEAM_ALIAS_PREFIX)
    assert ids.new_player_alias_id().startswith(ids.PLAYER_ALIAS_PREFIX)
    assert ids.new_game_status_id().startswith(ids.GAME_STATUS_PREFIX)


def test_surrogate_prefixes_are_distinct() -> None:
    prefixes = {
        ids.LEAGUE_PREFIX,
        ids.SEASON_PREFIX,
        ids.TEAM_PREFIX,
        ids.PLAYER_PREFIX,
        ids.GAME_PREFIX,
        ids.TEAM_ALIAS_PREFIX,
        ids.PLAYER_ALIAS_PREFIX,
        ids.GAME_STATUS_PREFIX,
    }
    assert len(prefixes) == 8
