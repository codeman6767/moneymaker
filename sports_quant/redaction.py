"""Secret redaction helpers.

The Odds API key is a query-string secret: it travels as ``?apiKey=...`` on
every Odds API request. The rules (``CLAUDE.md``) forbid ever printing or
logging it. These helpers make redaction the default whenever a URL, string or
mapping might contain the key, so it cannot leak into logs, error messages or
the ``providers-check`` output.
"""

from __future__ import annotations

from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***REDACTED***"

# Query-parameter names that carry secrets and must always be masked.
SECRET_QUERY_PARAMS: frozenset[str] = frozenset({"apiKey", "api_key", "apikey", "key", "token"})

# Response headers that may be preserved in the corpus, as an **allow-list**.
#
# A deny-list ("store everything except authorization") fails open the day a
# provider introduces a new header carrying something sensitive. An allow-list
# fails closed: an unrecognized header is simply not stored. ``authorization``,
# ``set-cookie``, ``x-api-key`` and anything else outside this set can therefore
# never reach a stored column, whatever a provider decides to send.
STORABLE_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "date",
        "cache-control",
        "etag",
        # The Odds API credit accounting.
        "x-requests-remaining",
        "x-requests-used",
        "x-requests-last",
    }
)


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret value found in ``text`` with a marker."""

    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def sanitize_url(url: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``url`` with any secret query parameters masked.

    Masks by parameter *name* (so the key is redacted even if the caller does
    not know its value) and, as a belt-and-braces measure, also replaces any
    explicitly supplied secret values anywhere in the string.
    """

    parts = urlsplit(url)
    if parts.query:
        masked = [
            (name, REDACTED if name in SECRET_QUERY_PARAMS else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        parts = parts._replace(query=urlencode(masked))
    return redact_secrets(urlunsplit(parts), extra_secrets)


def sanitize_params(params: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of ``params`` with secret-named entries masked."""

    return {
        name: (REDACTED if name in SECRET_QUERY_PARAMS else value)
        for name, value in params.items()
    }


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return only the headers on :data:`STORABLE_RESPONSE_HEADERS`.

    Names are lower-cased so the result is comparable regardless of how the
    provider capitalized them. Anything not explicitly allowed is dropped, so
    an ``Authorization`` (or any future credential-bearing) header cannot be
    persisted even by accident.
    """

    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in STORABLE_RESPONSE_HEADERS
    }
