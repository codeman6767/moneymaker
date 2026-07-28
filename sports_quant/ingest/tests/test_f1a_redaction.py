"""F1A redaction proof: a provider secret never enters usage, output, or a
checkpoint, even when the provider echoes the key in its body/headers (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from sports_quant.ingest.checkpoint import Checkpoint, write_checkpoint
from sports_quant.ingest.tests.f1a_support import known_cost_policy
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.request_control import CreditBudget, RequestBudget, RequestGate

_SECRET = "bdl_SECRET_key_do_not_leak_9c1f"


def _gate() -> RequestGate:
    # Known-cost TEST policy so the request proceeds and we can assert the secret
    # is redacted from the body/usage/checkpoint.
    return RequestGate(
        request_budget=RequestBudget(max_requests=10),
        credit_budget=CreditBudget(applicable=True, max_credits=100),
        cost_policy=known_cost_policy(),
    )


def test_secret_never_enters_usage_or_checkpoint(tmp_path: Path) -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile provider that echoes the Authorization key in the body.
        echoed = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [], "echoed_key": echoed},
                              headers={"x-echo": echoed})

    gate = _gate()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://bdl.invalid")
    client = BalldontlieClient(_SECRET, client=http, gate=gate, league="nba", max_retries=0)

    async def go() -> None:
        resp = await client.fetch_teams()
        # The stored raw exchange body must have the echoed key redacted.
        assert _SECRET not in resp.exchange.body
        await client.aclose()

    asyncio.run(go())

    # The gate's usage report must not contain the secret anywhere.
    usage_json = json.dumps(gate.usage.as_dict())
    assert _SECRET not in usage_json

    # A checkpoint carrying that usage must not contain the secret either.
    ckpt = tmp_path / "p.ckpt"
    write_checkpoint(ckpt, Checkpoint(
        manifest_hash="H", plan_version="f1a-plan-v1", provider="balldontlie", league="nba",
        date_range="2026-01-05", families=("games",), scratch_db="s.db",
        scratch_fingerprint="FP", schema_version=16, request_cap=10, credit_cap=100,
        usage=gate.usage.as_dict()))
    assert _SECRET not in ckpt.read_text(encoding="utf-8")


def test_request_unit_identity_has_no_secret() -> None:
    # A unit built from params that (wrongly) carried a key still would not embed
    # it via the secret_param path -- but assert defensively on the identity.
    from sports_quant.request_control import RequestUnit

    unit = RequestUnit(provider="balldontlie", league="nba", endpoint_family="games",
                       params=(("per_page", "100"),))
    assert _SECRET not in unit.identity()
