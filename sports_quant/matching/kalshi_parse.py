"""Deterministic, versioned parsers for Kalshi game-series contracts (D5B2).

Pure string functions only -- no database, no clock, no network. Each supported
series has an explicit, VERSIONED contract that matches the **current public
Kalshi format** verified by a bounded GET-only audit (see
`ENTITY_MATCHING.md` §6); anything outside it is rejected (a typed error), never
guessed. The matcher (:mod:`sports_quant.matching.kalshi`) resolves the extracted
team codes/names to canonical teams and cross-checks ticker, title, and rules
evidence, and converts a venue-local clock to UTC using the candidate venue's
timezone; this module never touches prices, results, or order books.

Verified current public contracts (audited 2026-07)
----------------------------------------------------
* **MLB** ``KXMLBGAME`` (parser ``kmlb-2``): event ticker
  ``KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}`` -- e.g. ``KXMLBGAME-26JUL271840AZPIT``
  (2026-07-27, 18:40 **venue-local** wall clock, away ``AZ``, home ``PIT``). The
  ``HHMM`` is a local scheduled clock, NOT UTC.
* **NBA** ``KXNBAGAME`` (parser ``knba-1``): event ticker
  ``KXNBAGAME-{YYMONDD}{AWAY}{HOME}`` -- date-only, no clock (e.g.
  ``KXNBAGAME-26JUN13NYKSAS``).
* Market ticker: ``{EVENT_TICKER}-{SUBJECT}`` (the Yes-side team code).
* Titles: MLB ``A vs B`` (unordered); NBA ``[Game N: ]A at B`` (ordered).
* Rules: ``If {Yes} wins the [Game N: ]{A} (vs|at) {B} professional {sport} game
  originally scheduled for {Mon D, YYYY}[ at {H:MM AM/PM TZ}], then the market
  resolves to Yes.``

The away/home code split is resolved against the curated ``kalshi_public`` alias
code set (a non-unique split is *ambiguous* and rejected, never guessed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..db.normalize import normalized_key

# --------------------------------------------------------------------------- #
# Series registry (task §3). Exact allowlist -- no prefix guessing, no generic
# "category = Sports" catch-all, no single cross-league regex.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeriesDef:
    """One supported, versioned Kalshi game series."""

    series_ticker: str
    league_code: str
    parser_version: str
    semantic: str          # only 'game_winner' is supported in D5B2
    has_ticker_clock: bool  # MLB encodes a local HHMM; NBA is date-only


SUPPORTED_SERIES: dict[str, SeriesDef] = {
    "KXMLBGAME": SeriesDef("KXMLBGAME", "MLB", "kmlb-2", "game_winner", has_ticker_clock=True),
    "KXNBAGAME": SeriesDef("KXNBAGAME", "NBA", "knba-1", "game_winner", has_ticker_clock=False),
}


def series_for(series_ticker: Optional[str]) -> Optional[SeriesDef]:
    if series_ticker is None:
        return None
    return SUPPORTED_SERIES.get(series_ticker.strip().upper())


# --------------------------------------------------------------------------- #
# Typed parse results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParseError:
    reason: str


@dataclass(frozen=True)
class ParsedEventTicker:
    series: SeriesDef
    event_ticker: str
    game_date_local: str          # provider calendar date, YYYY-MM-DD
    local_clock: Optional[str]    # venue-local wall clock 'HH:MM' (MLB) or None (NBA)
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
_FULL_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}
_DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

_MLB_EVENT_RE = re.compile(
    r"^KXMLBGAME-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<clock>\d{4})(?P<teams>[A-Z]{4,10})$")
_NBA_EVENT_RE = re.compile(
    r"^KXNBAGAME-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<teams>[A-Z]{4,10})$")
_MARKET_SUFFIX_RE = re.compile(r"^(?P<subject>[A-Z]{2,5})$")


def _parse_date_code(code: str) -> Optional[str]:
    """``26JUL27`` -> ``2026-07-27``, or ``None`` if malformed/impossible."""

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


def _parse_ticker_clock(code: str) -> Optional[str]:
    """``1840`` -> ``18:40`` (venue-local), validating hour 00-23 / minute 00-59."""

    if len(code) != 4 or not code.isdigit():
        return None
    hh, mm = int(code[:2]), int(code[2:])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def split_team_codes(blob: str, valid_codes: frozenset[str]) -> Optional[tuple[str, str]]:
    """The unique ``(away, home)`` split of a concatenated code blob.

    Every split into two DISTINCT curated codes is enumerated; returned only when
    EXACTLY ONE is valid (a non-unique split is ambiguous -> ``None``)."""

    matches = [
        (blob[:i], blob[i:])
        for i in range(1, len(blob))
        if blob[:i] in valid_codes and blob[i:] in valid_codes and blob[:i] != blob[i:]
    ]
    return matches[0] if len(matches) == 1 else None


def parse_event_ticker(
    ticker: str, valid_codes: frozenset[str]
) -> ParsedEventTicker | ParseError:
    """Parse an event ticker against the current supported-series contract.

    Dispatches by exact series ticker: MLB (``kmlb-2``) requires an ``HHMM``
    venue-local clock segment; NBA (``knba-1``) is date-only and REJECTS any time
    segment. The team split is resolved only against ``valid_codes`` (the curated
    ``kalshi_public`` codes for the league)."""

    t = ticker.strip()
    if t.startswith("KXMLBGAME-"):
        m = _MLB_EVENT_RE.match(t)
        if m is None:
            return ParseError("MLB event ticker does not match KXMLBGAME-{YYMONDD}{HHMM}{teams}")
        series = SUPPORTED_SERIES["KXMLBGAME"]
        clock = _parse_ticker_clock(m.group("clock"))
        if clock is None:
            return ParseError("MLB event ticker has a malformed/impossible local clock")
    elif t.startswith("KXNBAGAME-"):
        m = _NBA_EVENT_RE.match(t)
        if m is None:
            return ParseError("NBA event ticker does not match KXNBAGAME-{YYMONDD}{teams} "
                              "(date-only; no time segment)")
        series = SUPPORTED_SERIES["KXNBAGAME"]
        clock = None
    else:
        return ParseError("unsupported series")
    date = _parse_date_code(m.group("date"))
    if date is None:
        return ParseError("event ticker has a malformed or impossible date")
    split = split_team_codes(m.group("teams"), valid_codes)
    if split is None:
        return ParseError("event ticker team codes are unknown or split ambiguously")
    away_code, home_code = split
    return ParsedEventTicker(series=series, event_ticker=t, game_date_local=date,
                             local_clock=clock, away_code=away_code, home_code=home_code)


def parse_market_ticker(
    ticker: str, event: ParsedEventTicker
) -> ParsedMarketTicker | ParseError:
    """Parse ``{event_ticker}-{SUBJECT}`` and verify it descends from its event."""

    ticker = ticker.strip()
    prefix = event.event_ticker + "-"
    if not ticker.startswith(prefix):
        return ParseError("market ticker does not descend from its event ticker")
    m = _MARKET_SUFFIX_RE.match(ticker[len(prefix):])
    if m is None:
        return ParseError("market ticker subject suffix is malformed")
    subject = m.group("subject")
    if subject not in (event.away_code, event.home_code):
        return ParseError("market ticker subject is not one of the event's two teams")
    return ParsedMarketTicker(series=event.series, event_ticker=event.event_ticker,
                              market_ticker=ticker, yes_code=subject)


# --------------------------------------------------------------------------- #
# Title / sub-title team extraction (task §5). Explicit templates only.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TitleTeams:
    """Teams named by a title. ``away``/``home`` set only for an ordered ``at``
    form; ``vs`` forms carry an unordered pair with no orientation."""

    names: tuple[str, str]
    away_name: Optional[str] = None
    home_name: Optional[str] = None


# An optional ``Game N:`` (or ``Game N -``) prefix, then ``A at B`` (ordered) or
# ``A vs B`` / ``A vs. B`` (unordered). A trailing ``Winner?`` (market titles) is
# stripped. The series contract documents ``at`` as away-then-home.
_GAME_PREFIX_RE = re.compile(r"^\s*game\s+\d+\s*[:\-]\s*", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(r"\s+winner\??\s*$", re.IGNORECASE)
_TITLE_AT_RE = re.compile(r"^\s*(?P<a>.+?)\s+at\s+(?P<b>.+?)\s*$", re.IGNORECASE)
_TITLE_VS_RE = re.compile(r"^\s*(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s*$", re.IGNORECASE)


def _strip_title(text: str) -> str:
    return _TITLE_SUFFIX_RE.sub("", _GAME_PREFIX_RE.sub("", text.strip()))


def parse_title_teams(text: Optional[str]) -> TitleTeams | ParseError | None:
    """Extract the team pair from a title/sub-title.

    ``None`` when there is no text; :class:`ParseError` when only one team (or no
    recognizable pair) is present; :class:`TitleTeams` otherwise. An optional
    ``Game N:`` prefix and a trailing ``Winner?`` are stripped first. ``at`` forms
    carry away/home orientation; ``vs`` forms do not."""

    if text is None or not text.strip():
        return None
    body = _strip_title(text)
    at = _TITLE_AT_RE.match(body)
    if at is not None:
        a, b = at.group("a").strip(), at.group("b").strip()
        if a and b and normalized_key(a) != normalized_key(b):
            return TitleTeams(names=(a, b), away_name=a, home_name=b)
    vs = _TITLE_VS_RE.match(body)
    if vs is not None:
        a, b = vs.group("a").strip(), vs.group("b").strip()
        if a and b and normalized_key(a) != normalized_key(b):
            return TitleTeams(names=(a, b))
    return ParseError("title names one team or no recognizable game pair")


# --------------------------------------------------------------------------- #
# Rules Yes-subject extraction (task §5/§6). Explicit settlement templates only.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedRules:
    """Yes-subject and team evidence extracted from authoritative rules text.

    ``yes_name`` is the team the rules settle Yes on; ``away_name``/``home_name``
    are set for an ordered ``at`` phrase (else the unordered pair is in
    ``names``). ``scheduled_date`` is ``YYYY-MM-DD``; ``local_clock`` (``HH:MM``,
    24h) and ``tz_abbrev`` (e.g. ``EDT``) are present only when the rules supply a
    time. The full rules text is never stored."""

    yes_name: str
    names: tuple[str, str]
    away_name: Optional[str]
    home_name: Optional[str]
    scheduled_date: str
    local_clock: Optional[str] = None
    tz_abbrev: Optional[str] = None


# "If {Yes} wins the [Game N: ]{A} (vs|at) {B} professional {sport} game
#  originally scheduled for {Mon D, YYYY}[ at {H:MM AM/PM TZ}], then ..."
_RULES_RE = re.compile(
    r"\bIf (?P<yes>.+?) wins the (?:game\s+\d+\s*[:\-]\s*)?(?P<phrase>.+?) "
    r"professional (?:baseball|basketball) game "
    r"originally scheduled for (?P<date>[A-Za-z]+ \d{1,2}, \d{4})"
    r"(?: at (?P<clock>\d{1,2}:\d{2} [AP]M) (?P<tz>[A-Z]{2,4}))?",
    re.IGNORECASE,
)
_PHRASE_AT_RE = re.compile(r"^(?P<a>.+?)\s+at\s+(?P<b>.+?)$", re.IGNORECASE)
_PHRASE_VS_RE = re.compile(r"^(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)$", re.IGNORECASE)


def _parse_nl_date(text: str) -> Optional[str]:
    """``Jul 27, 2026`` / ``July 27, 2026`` -> ``2026-07-27``."""

    m = re.match(r"^([A-Za-z]+) (\d{1,2}), (\d{4})$", text.strip())
    if m is None:
        return None
    mon = m.group(1).upper()
    month = _MONTHS.get(mon[:3]) if mon[:3] in _MONTHS else _FULL_MONTHS.get(mon)
    if month is None:
        return None
    day, year = int(m.group(2)), int(m.group(3))
    if not (1 <= day <= _DAYS_IN_MONTH[month]):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_nl_clock(text: str) -> Optional[str]:
    """``6:40 PM`` -> ``18:40``; ``12:00 AM`` -> ``00:00``; ``12:00 PM`` -> ``12:00``."""

    m = re.match(r"^(\d{1,2}):(\d{2}) ([AP])M$", text.strip(), re.IGNORECASE)
    if m is None:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if not (1 <= hh <= 12 and 0 <= mm <= 59):
        return None
    if ap == "A":
        hh = 0 if hh == 12 else hh
    else:
        hh = 12 if hh == 12 else hh + 12
    return f"{hh:02d}:{mm:02d}"


def parse_rules_yes_subject(rules_primary: Optional[str]) -> ParsedRules | ParseError | None:
    """Extract the Yes subject + participants (+ optional local time) from
    game-winner rules text. ``None`` when absent; :class:`ParseError` when the
    supported template does not match (reviewable, never guessed)."""

    if rules_primary is None or not rules_primary.strip():
        return None
    m = _RULES_RE.search(rules_primary)
    if m is None:
        return ParseError("rules do not match a supported game-winner settlement template")
    yes_name = m.group("yes").strip()
    phrase = m.group("phrase").strip()
    at = _PHRASE_AT_RE.match(phrase)
    vs = _PHRASE_VS_RE.match(phrase)
    away_name = home_name = None
    if at is not None:
        a, b = at.group("a").strip(), at.group("b").strip()
        away_name, home_name, names = a, b, (a, b)
    elif vs is not None:
        a, b = vs.group("a").strip(), vs.group("b").strip()
        names = (a, b)
    else:
        return ParseError("rules game phrase is not an 'A vs B' or 'A at B' pair")
    if normalized_key(names[0]) == normalized_key(names[1]):
        return ParseError("rules name the same team twice")
    date = _parse_nl_date(m.group("date"))
    if date is None:
        return ParseError("rules scheduled date is malformed")
    clock = _parse_nl_clock(m.group("clock")) if m.group("clock") else None
    if m.group("clock") is not None and clock is None:
        return ParseError("rules scheduled clock is malformed")
    return ParsedRules(
        yes_name=yes_name, names=names, away_name=away_name, home_name=home_name,
        scheduled_date=date, local_clock=clock,
        tz_abbrev=m.group("tz").upper() if m.group("tz") else None,
    )
