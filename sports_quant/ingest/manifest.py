"""F1A pilot manifest: a versioned, deterministic, secret-free plan document.

A manifest freezes exactly what a pilot stage would do so it can be reviewed,
diffed, hashed, and later matched by a checkpoint on resume. Equivalent logical
inputs produce **byte-identical** canonical JSON and the same SHA-256 hash. No
API key, header, secret-bearing URL, random id, or wall-clock creation time ever
enters the manifest or its hash.

This module performs no network or database I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from .planning import RequestPlan

MANIFEST_FORMAT_VERSION = "f1a-manifest-v1"
EXPECTED_SCHEMA_VERSION = 16


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable canonical JSON: sorted keys, no insignificant whitespace, UTF-8."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_body(plan: RequestPlan) -> dict[str, Any]:
    """The hashable, secret-free body of a plan (identities only, no wall-clock)."""

    return {
        "plan_version": plan.plan_version,
        "provider": plan.provider,
        "league": plan.league,
        "stage": plan.stage,
        "date_range": plan.date_range,
        "families": list(plan.families),
        "fixed_units": [json.loads(u.identity()) for u in plan.fixed_units],
        "contingents": [
            {
                "kind": c.kind, "family": c.family,
                "per_parent_min": c.per_parent_min, "per_parent_max": c.per_parent_max,
                "parent_min": c.parent_min, "parent_max": c.parent_max,
            }
            for c in plan.contingents
        ],
        "bounds": {
            "max_games": plan.bounds.max_games,
            "max_pages": plan.bounds.max_pages,
            "max_records": plan.bounds.max_records,
            "max_retries": plan.bounds.max_retries,
        },
        "cost_policy_version": plan.cost_policy_version,
        "credits_applicable": plan.credits_applicable,
    }


def plan_hash(plan: RequestPlan) -> str:
    return _hash(canonical_json(plan_body(plan)))


@dataclass(frozen=True)
class PilotManifest:
    """The full, canonical, hashable pilot manifest for one provider/league/stage."""

    provider: str
    league: str
    stage: str
    date_range: str
    families: tuple[str, ...]
    plan_version: str
    manifest_format_version: str
    request_cap: Optional[int]
    credit_cap: Optional[int]
    cost_policy_version: str
    credits_applicable: bool
    estimated_requests_min: int
    estimated_requests_max: Optional[int]
    estimated_credits_min: Optional[int]
    estimated_credits_max: Optional[int]
    max_games: Optional[int]
    max_pages: Optional[int]
    max_records: Optional[int]
    max_retries: int
    scratch_db: str
    checkpoint_path: str
    expected_schema_version: int
    executable: bool
    unresolved_bounds: tuple[str, ...]
    plan_body: dict[str, Any]

    def body(self) -> dict[str, Any]:
        """The canonical, secret-free manifest body used for serialization/hash."""

        return {
            "manifest_format_version": self.manifest_format_version,
            "plan_version": self.plan_version,
            "provider": self.provider,
            "league": self.league,
            "stage": self.stage,
            "date_range": self.date_range,
            "families": list(self.families),
            "request_cap": self.request_cap,
            "credit_cap": self.credit_cap,
            "cost_policy_version": self.cost_policy_version,
            "credits_applicable": self.credits_applicable,
            "estimated_requests_min": self.estimated_requests_min,
            "estimated_requests_max": self.estimated_requests_max,
            "estimated_credits_min": self.estimated_credits_min,
            "estimated_credits_max": self.estimated_credits_max,
            "bounds": {
                "max_games": self.max_games, "max_pages": self.max_pages,
                "max_records": self.max_records, "max_retries": self.max_retries,
            },
            "scratch_db": self.scratch_db,
            "checkpoint_path": self.checkpoint_path,
            "expected_schema_version": self.expected_schema_version,
            "executable": self.executable,
            "unresolved_bounds": list(self.unresolved_bounds),
            "plan_body": self.plan_body,
        }

    def canonical(self) -> str:
        return canonical_json(self.body())

    def manifest_hash(self) -> str:
        return _hash(self.canonical())

    def as_dict(self) -> dict[str, Any]:
        d = self.body()
        d["manifest_hash"] = self.manifest_hash()
        d["plan_hash"] = self.computed_plan_hash()
        return d

    def computed_plan_hash(self) -> str:
        return _hash(canonical_json(self.plan_body))


def build_manifest(
    plan: RequestPlan,
    *,
    scratch_db: str = "",
    checkpoint_path: str = "",
    request_cap: Optional[int] = None,
    credit_cap: Optional[int] = None,
) -> PilotManifest:
    """Build a canonical manifest from a plan. Caps default to the plan's
    conservative required caps when not explicitly supplied."""

    req_cap = request_cap if request_cap is not None else plan.required_request_cap()
    cr_cap = credit_cap if credit_cap is not None else plan.required_credit_cap()
    return PilotManifest(
        provider=plan.provider,
        league=plan.league,
        stage=plan.stage,
        date_range=plan.date_range,
        families=plan.families,
        plan_version=plan.plan_version,
        manifest_format_version=MANIFEST_FORMAT_VERSION,
        request_cap=req_cap,
        credit_cap=cr_cap,
        cost_policy_version=plan.cost_policy_version,
        credits_applicable=plan.credits_applicable,
        estimated_requests_min=plan.semantic_requests_min(),
        estimated_requests_max=plan.semantic_requests_max(),
        estimated_credits_min=plan.credits_min(),
        estimated_credits_max=plan.credits_max(),
        max_games=plan.bounds.max_games,
        max_pages=plan.bounds.max_pages,
        max_records=plan.bounds.max_records,
        max_retries=plan.bounds.max_retries,
        scratch_db=scratch_db,
        checkpoint_path=checkpoint_path,
        expected_schema_version=EXPECTED_SCHEMA_VERSION,
        executable=plan.executable(),
        unresolved_bounds=plan.unresolved_bounds(),
        plan_body=plan_body(plan),
    )
