"""Deterministic, versioned parsers for Kalshi game-series contracts (D5B2).

Pure string functions only -- no database, no clock, no network. Each supported
series has an explicit, versioned contract; anything outside it is rejected (a
typed error), never guessed. The matcher (:mod:`sports_quant.matching.kalshi`)
resolves the extracted team codes/names to canonical teams and cross-checks the
ticker, title, and rules evidence; this module never touches prices, results, or
order books.

Ticker contract (project-documented, provider-equivalent form)
--------------------------------------------------------------
* Event ticker:  ``{SERIES}-{YYMONDD}{AWAY}{HOME}``  e.g. ``KXMLBGAME-25JUL04ATLNYM``
  -- the date is ``YY`` + 3-letter month + ``DD``; the away and home provider team
  codes are concatenated with no delimiter, so the split is resolved against the
  curated ``kalshi_public`` alias code set (a non-unique split is *ambiguous* and
  rejected -- never guessed). Series contract: the first code is the AWAY team.
* Market ticker: ``{EVENT_TICKER}-{SUBJECT}`` -- the Yes-side team code. The market
  ticker must descend cleanly from its event ticker (exact prefix), and the
  subject must be one of the two event teams.

The series encode a DATE but no time; an exact scheduled time is only ever taken
from authoritative rules text, never invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..db.normalize import normalized_key

# --------------------------------------------------------------------------- #
# Series registry (task §4). Exact allowlist -- no prefix guessing, no generic
# "category = Sports" catch-all.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeriesDef:
    """One supported, versioned Kalshi game series."""

    series_ticker: str
    league_code: str
    parser_version: str
    semantic: str  # only 'game_winner' is supported in D5B2


SUPPORTED_SERIES: dict[str, SeriesDef] = {
    "KXMLBGAME": SeriesDef("KXMLBGAME", "MLB", "kmlb-1", "game_winner"),
    "KXNBAGAME": SeriesDef("KXNBAGAME", "NBA", "knba-1", "game_winner"),
}


def series_for(series_ticker: Optional[str]) -> Optional[SeriesDef]:
    """The supported series definition for a ticker, or ``None`` if unsupported."""

    if series_ticker is None:
        return None
    return SUPPORTED_SERIES.get(series_ticker.strip().upper())


# --------------------------------------------------------------------------- #
# Typed parse results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParseError:
    """A rejected parse. ``reason`` is a short, stable, reviewable explanation."""

    reason: str


@dataclass(frozen=True)
class ParsedEventTicker:
    series: SeriesDef
    event_ticker: str
    game_date_local: str  # provider calendar date, YYYY-MM-DD
    away_code: str
    home_code: str


@dataclass(frozen=True)
class ParsedMarketTicker:
    series: SeriesDef
    event_ticker: str
    market_ticker: str
    yes_code: str


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

_EVENT_RE = re.compile(r"^(?P<series>KX[A-Z]+GAME)-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<teams>[A-Z]{4,10})$")
_MARKET_SUFFIX_RE = re.compile(r"^(?P<subject>[A-Z]{2,5})$")


def _parse_date_code(code: str) -> Optional[str]:
    """``25JUL04`` -> ``2025-07-04``, or ``None`` if malformed/impossible."""

    if len(code) != 7:
        return None
    yy, mon, dd = code[:2], code[2:5], code[5:]
    if not (yy.isdigit() and dd.isdigit()):
        return None
    month = _MONTHS.get(mon)
    if month is None:
        return None
    day = int(dd)
    if not (1 <= day <= _DAYS_IN_MONTH[month]):
        return None
    return f"20{yy}-{month:02d}-{day:02d}"


def split_team_codes(blob: str, valid_codes: frozenset[str]) -> Optional[tuple[str, str]]:
    """The unique ``(away, home)`` split of a concatenated code blob.

    Every candidate split into two DISTINCT curated codes is enumerated; the
    result is returned only when EXACTLY ONE split is valid -- a non-unique split
    is ambiguous and yields ``None`` (the caller rejects it). Deterministic and
    independent of code ordering.
    """

    matches = [
        (blob[:i], blob[i:])
        for i in range(1, len(blob))
        if blob[:i] in valid_codes and blob[i:] in valid_codes and blob[:i] != blob[i:]
    ]
    return matches[0] if len(matches) == 1 else None


def parse_event_ticker(
    ticker: str, valid_codes: frozenset[str]
) -> ParsedEventTicker | ParseError:
    """Parse an event ticker against the supported-series contract.

    ``valid_codes`` is the curated ``kalshi_public`` team-code set for the series'
    league; the team split is resolved only against it, never by guessing lengths.
    """

    m = _EVENT_RE.match(ticker.strip())
    if m is None:
        return ParseError("event ticker does not match any supported game-series shape")
    series = series_for(m.group("series"))
    if series is None:
        return ParseError(f"unsupported series {m.group('series')!r}")
    date = _parse_date_code(m.group("date"))
    if date is None:
        return ParseError("event ticker has a malformed or impossible date")
    split = split_team_codes(m.group("teams"), valid_codes)
    if split is None:
        return ParseError("event ticker team codes are unknown or split ambiguously")
    away_code, home_code = split
    return ParsedEventTicker(
        series=series, event_ticker=ticker.strip(), game_date_local=date,
        away_code=away_code, home_code=home_code,
    )


def parse_market_ticker(
    ticker: str, event: ParsedEventTicker
) -> ParsedMarketTicker | ParseError:
    """Parse a market ticker and verify it descends cleanly from its event.

    The market ticker must be exactly ``{event_ticker}-{SUBJECT}`` and the
    subject must be one of the event's two team codes (the Yes side)."""

    ticker = ticker.strip()
    prefix = event.event_ticker + "-"
    if not ticker.startswith(prefix):
        return ParseError("market ticker does not descend from its event ticker")
    suffix = ticker[len(prefix):]
    m = _MARKET_SUFFIX_RE.match(suffix)
    if m is None:
        return ParseError("market ticker subject suffix is malformed")
    subject = m.group("subject")
    if subject not in (event.away_code, event.home_code):
        return ParseError("market ticker subject is not one of the event's two teams")
    return ParsedMarketTicker(
        series=event.series, event_ticker=event.event_ticker, market_ticker=ticker,
        yes_code=subject,
    )


# --------------------------------------------------------------------------- #
# Title / sub-title team extraction (task §8). Explicit phrase templates only --
# no NLP, no LLM.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TitleTeams:
    """Teams named by a title. ``away``/``home`` are set only for an ``A at B``
    ordered form; ``vs`` forms carry an unordered pair with no orientation."""

    names: tuple[str, str]
    away_name: Optional[str] = None
    home_name: Optional[str] = None


# ``A at B`` (away at home) and ``A vs B`` / ``A vs. B`` (unordered). The series
# contract documents ``at`` as away-then-home; ``vs`` carries no orientation.
_TITLE_AT_RE = re.compile(r"^\s*(?P<a>.+?)\s+at\s+(?P<b>.+?)\s*$", re.IGNORECASE)
_TITLE_VS_RE = re.compile(r"^\s*(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s*$", re.IGNORECASE)


def parse_title_teams(text: Optional[str]) -> TitleTeams | ParseError | None:
    """Extract the team pair from a title/sub-title.

    Returns ``None`` when there is no text to parse; a :class:`ParseError` when a
    single team (or no recognizable pair) is present; a :class:`TitleTeams`
    otherwise. ``at`` forms carry away/home orientation; ``vs`` forms do not."""

    if text is None or not text.strip():
        return None
    at = _TITLE_AT_RE.match(text)
    if at is not None:
        a, b = at.group("a").strip(), at.group("b").strip()
        if a and b and normalized_key(a) != normalized_key(b):
            return TitleTeams(names=(a, b), away_name=a, home_name=b)
    vs = _TITLE_VS_RE.match(text)
    if vs is not None:
        a, b = vs.group("a").strip(), vs.group("b").strip()
        if a and b and normalized_key(a) != normalized_key(b):
            return TitleTeams(names=(a, b))
    return ParseError("title names one team or no recognizable game pair")


# --------------------------------------------------------------------------- #
# Rules Yes-subject extraction (task §9). Explicit settlement templates only.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedRules:
    """Yes-subject and team evidence extracted from authoritative rules text.

    ``yes_name`` and ``other_name`` are the provider-side team phrases the rules
    settle on; ``scheduled_time`` is an offset-bearing ISO instant only when the
    rules explicitly supply one."""

    yes_name: str
    other_name: str
    scheduled_time: Optional[str] = None


# "... resolves Yes if the <YES> win/beat/defeat ... <OTHER> ..." -- the Yes
# subject is the team named as winning. Conservative and fixture-backed.
_RULES_YES_RE = re.compile(
    r"\bif the (?P<yes>[A-Za-z0-9 .'&-]+?) (?:win|wins|beat|beats|defeat|defeats)\b",
    re.IGNORECASE,
)
_RULES_OTHER_RE = re.compile(
    r"\b(?:against|versus|vs\.?|over|face|facing|play|plays|host|hosts|visit|visits) "
    r"the (?P<other>[A-Za-z0-9 .'&-]+?)(?:[.,]| on | at | in |$)",
    re.IGNORECASE,
)
# "... originally scheduled for <ISO instant> ..." -- an offset-bearing instant.
_RULES_TIME_RE = re.compile(
    r"originally scheduled (?:for|at) (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))",
    re.IGNORECASE,
)


def parse_rules_yes_subject(rules_primary: Optional[str]) -> ParsedRules | ParseError | None:
    """Extract the Yes subject + opposing team from game-winner rules text.

    Returns ``None`` when there is no rules text; a :class:`ParseError` when the
    settlement subject cannot be identified with a supported template (reviewable,
    never guessed). The full rules text is never returned or stored."""

    if rules_primary is None or not rules_primary.strip():
        return None
    yes_m = _RULES_YES_RE.search(rules_primary)
    other_m = _RULES_OTHER_RE.search(rules_primary)
    if yes_m is None or other_m is None:
        return ParseError("rules do not match a supported game-winner settlement template")
    yes_name = yes_m.group("yes").strip()
    other_name = other_m.group("other").strip()
    if not yes_name or not other_name or normalized_key(yes_name) == normalized_key(other_name):
        return ParseError("rules Yes subject and opponent could not be distinguished")
    time_m = _RULES_TIME_RE.search(rules_primary)
    return ParsedRules(
        yes_name=yes_name, other_name=other_name,
        scheduled_time=time_m.group("ts") if time_m is not None else None,
    )
