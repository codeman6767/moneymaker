"""Raw-response repository.

Every external response is written here **before** any normalized row derived
from it, so a parse failure loses nothing and a re-parse months later is
possible. The table is append-only, enforced by triggers in migration b004.

The content-hash discipline is shared with the rest of the corpus: the same
``canonical_json`` from ``streaming.event_envelope`` used by the game status
history, never a fourth canonicalizer (two that disagree produce two hashes for
one content and silently defeat deduplication).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Mapping, Optional, Protocol

from streaming.event_envelope import canonical_json

from ..ids import new_raw_response_id
from ..models import RawResponse
from ..schema import ALLOWED_HTTP_METHOD, utc_now_iso
from .base import Repository, RepositoryError


def body_hash(body: str) -> str:
    """SHA-256 of the response body bytes."""

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def response_content_hash(
    *, provider: str, endpoint: str, request_params: Mapping[str, object], body: str
) -> str:
    """Dedup identity of a response: provider + endpoint + params + body.

    Identical bytes from a *different* endpoint are not the same response, so
    the endpoint and params participate in the hash rather than the body alone.
    """

    payload = {
        "provider": provider,
        "endpoint": endpoint,
        "params": request_params,
        "body": body,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class RawResponseRepositoryProtocol(Protocol):
    """Operations the ingestion lane needs from a raw-response store."""

    def store(
        self,
        *,
        run_id: str,
        provider: str,
        endpoint: str,
        request_params_json: str,
        http_status: int,
        response_headers_json: str,
        requested_at: str,
        received_at: str,
        elapsed_ns: int,
        body: str,
        content_hash: str,
        content_type: Optional[str] = None,
        http_method: str = "GET",
    ) -> RawResponse: ...

    def get(self, raw_response_id: str) -> Optional[RawResponse]: ...

    def find_by_content_hash(self, content_hash: str) -> Optional[RawResponse]: ...

    def list_for_run(self, run_id: str) -> list[RawResponse]: ...

    def count(self) -> int: ...


class SqliteRawResponseRepository(Repository):
    """Append-only raw-response storage."""

    _COLUMNS = (
        "raw_response_id, run_id, provider, endpoint, request_params_json, http_method, "
        "http_status, response_headers_json, content_type, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at"
    )

    def store(
        self,
        *,
        run_id: str,
        provider: str,
        endpoint: str,
        request_params_json: str,
        http_status: int,
        response_headers_json: str,
        requested_at: str,
        received_at: str,
        elapsed_ns: int,
        body: str,
        content_hash: str,
        content_type: Optional[str] = None,
        http_method: str = "GET",
    ) -> RawResponse:
        """Persist one response verbatim.

        The caller supplies already-sanitized ``endpoint`` (a path, no query
        string), ``request_params_json`` (masked by name), and
        ``response_headers_json`` (allow-listed). This repository refuses a
        non-GET method rather than trusting the DB CHECK alone, so the failure
        is a clear Python error at the write site.
        """

        if http_method != ALLOWED_HTTP_METHOD:
            raise RepositoryError(
                f"refusing to store a {http_method!r} response; the corpus records GET only"
            )
        if "?" in endpoint:
            raise RepositoryError(
                "refusing to store an endpoint containing a query string; the Odds API key "
                "travels as a query parameter and must never be persisted"
            )

        raw_id = new_raw_response_id()
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO raw_responses "
            "(raw_response_id, run_id, provider, endpoint, request_params_json, http_method, "
            " http_status, response_headers_json, content_type, requested_at, received_at, "
            " elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                raw_id,
                run_id,
                provider,
                endpoint,
                request_params_json,
                http_method,
                http_status,
                response_headers_json,
                content_type,
                requested_at,
                received_at,
                elapsed_ns,
                body,
                len(body.encode("utf-8")),
                body_hash(body),
                content_hash,
                now,
            ),
        )
        stored = self.get(raw_id)
        if stored is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"raw response {raw_id!r} vanished immediately after insert")
        return stored

    def get(self, raw_response_id: str) -> Optional[RawResponse]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM raw_responses WHERE raw_response_id = ?",
            (raw_response_id,),
        )
        return None if row is None else self._to_model(row)

    def find_by_content_hash(self, content_hash: str) -> Optional[RawResponse]:
        """The earliest response with this content hash, if any.

        Traceability, not deduplication: the raw table keeps every observation,
        but a caller can still ask "have we ever seen these exact bytes?".
        """

        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM raw_responses WHERE content_hash = ? "
            "ORDER BY received_at, raw_response_id LIMIT 1",
            (content_hash,),
        )
        return None if row is None else self._to_model(row)

    def list_for_run(self, run_id: str) -> list[RawResponse]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM raw_responses WHERE run_id = ? "
                "ORDER BY received_at, raw_response_id",
                (run_id,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM raw_responses")

    def _to_model(self, row: sqlite3.Row) -> RawResponse:
        return RawResponse(
            raw_response_id=str(row["raw_response_id"]),
            run_id=str(row["run_id"]),
            provider=str(row["provider"]),
            endpoint=str(row["endpoint"]),
            request_params_json=str(row["request_params_json"]),
            http_status=int(row["http_status"]),
            response_headers_json=str(row["response_headers_json"]),
            requested_at=str(row["requested_at"]),
            received_at=str(row["received_at"]),
            elapsed_ns=int(row["elapsed_ns"]),
            body=str(row["body"]),
            body_bytes=int(row["body_bytes"]),
            body_hash=str(row["body_hash"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            http_method=str(row["http_method"]),
            content_type=self._opt_str(row, "content_type"),
        )
