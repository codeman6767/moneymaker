"""Frozen digest policies for corpus target-population binding (v23).

Three digests, three separate claims, each under its own frozen policy name:

``target-set-v1``
    WHICH canonical games the corpus asserts are its targets. Membership only.

``target-derivation-v1``
    WHICH precommitted acquisition, and which runs resolving it, the membership
    was derived from.

``target-binding-v1``
    The composite actually stored in
    ``reconstruction_corpus_versions.target_set_digest``.

Why a composite rather than a new corpus column
-----------------------------------------------
Run provenance must participate in corpus identity, because the independent
review proved that two derivations from different run sets reaching the same
member set otherwise collapse to ONE corpus row: ``target_set_digest`` and
therefore ``semantic_digest`` are equal, ``record_corpus_version`` returns the
existing row, and ``UNIQUE (semantic_digest)`` enforces it. A child run-binding
table would then hang two derivation explanations off one content-addressed
identity.

The obvious repair -- adding a provenance field to the ``semantic_digest``
payload -- is blocked: that payload is effectively frozen, because adding any
key, *even one whose value is None*, changes the canonical form and therefore the
digest of EVERY corpus including legacy rows. So derivation provenance enters
identity through the one semantic field that already exists, while membership
keeps its own separately named and separately verifiable digest.

A semantic change to any of these requires a NEW version, never an edit here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any, Final

__all__ = [
    "TARGET_SET_POLICY_V1",
    "TARGET_DERIVATION_POLICY_V1",
    "TARGET_BINDING_POLICY_V1",
    "SUPPORTED_TARGET_SET_POLICIES",
    "SUPPORTED_TARGET_DERIVATION_POLICIES",
    "SUPPORTED_TARGET_BINDING_POLICIES",
    "TargetBindingError",
    "members_digest",
    "derivation_digest",
    "target_binding_digest",
]

#: Membership only: policy, league, sorted unique canonical game ids.
TARGET_SET_POLICY_V1: Final = "target-set-v1"
#: Derivation provenance: policy, manifest hash, plan version, sorted unique runs.
TARGET_DERIVATION_POLICY_V1: Final = "target-derivation-v1"
#: The composite stored in `reconstruction_corpus_versions.target_set_digest`.
TARGET_BINDING_POLICY_V1: Final = "target-binding-v1"

SUPPORTED_TARGET_SET_POLICIES: Final = frozenset({TARGET_SET_POLICY_V1})
SUPPORTED_TARGET_DERIVATION_POLICIES: Final = frozenset({TARGET_DERIVATION_POLICY_V1})
SUPPORTED_TARGET_BINDING_POLICIES: Final = frozenset({TARGET_BINDING_POLICY_V1})

#: A sha256 hex digest, lowercase.
_HEX64_CHARS: Final = frozenset("0123456789abcdef")


class TargetBindingError(RuntimeError):
    """A target-binding digest input violates its frozen policy."""


def _canonical(payload: dict[str, Any]) -> str:
    """Stable canonical JSON: sorted keys, no insignificant whitespace, UTF-8.

    Matches `stage_a_manifest.canonical_json` exactly so one project has one
    canonical form, not two that agree by coincidence.
    """

    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _exact_identifier(value: object, *, field: str) -> str:
    """A non-empty `str` that is not a bool and carries no surrounding space.

    Type coercion is refused outright. B2's independent review caught a closed
    schema that silently accepted an int where a string was declared; the same
    class of defect here would let two different inputs share a digest.
    """

    if isinstance(value, bool) or not isinstance(value, str):
        raise TargetBindingError(
            f"{field} must be a string, got {type(value).__name__}")
    if value != value.strip() or not value:
        raise TargetBindingError(
            f"{field} must be a non-empty string without surrounding whitespace, "
            f"got {value!r}")
    return value


def _exact_unique_sorted(values: Iterable[object], *, field: str) -> list[str]:
    """Sorted identifiers, refusing duplicates rather than absorbing them.

    De-duplicating silently would let one membership set have two valid digests
    (`sorted()` keeps a duplicate, so the canonical form differs), and would let
    a caller hide a double-count. Both are refused.
    """

    items: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = _exact_identifier(raw, field=f"{field} entry")
        if item in seen:
            raise TargetBindingError(
                f"{field} contains the duplicate entry {item!r}; duplicates are "
                f"refused, never de-duplicated")
        seen.add(item)
        items.append(item)
    return sorted(items)


def _exact_hex64(value: object, *, field: str) -> str:
    text = _exact_identifier(value, field=field)
    if len(text) != 64 or not set(text) <= _HEX64_CHARS:
        raise TargetBindingError(
            f"{field} must be a lowercase 64-character sha256 hex digest")
    return text


def members_digest(
    *,
    league_id: str,
    members: Sequence[object],
    policy_version: str = TARGET_SET_POLICY_V1,
) -> str:
    """`target-set-v1`: the membership claim, and nothing else.

    `S_final` is deliberately excluded -- membership and hint evidence are
    different claims, and `source_corpus_digest` already commits the source
    evidence. Ordering never changes identity; membership always does.

    An empty membership hashes to a perfectly well-formed digest, which is
    mathematically correct and scientifically useless. Non-emptiness is therefore
    enforced at the SEAL (`member_count > 0`), so this function stays honest for
    generic use while no real corpus can be certified vacuously.
    """

    if policy_version not in SUPPORTED_TARGET_SET_POLICIES:
        raise TargetBindingError(
            f"unknown target-set policy {policy_version!r}; supported: "
            f"{sorted(SUPPORTED_TARGET_SET_POLICIES)}")
    return _sha256(_canonical({
        "policy": policy_version,
        "league_id": _exact_identifier(league_id, field="league_id"),
        "members": _exact_unique_sorted(members, field="members"),
    }))


def derivation_digest(
    *,
    acquisition_manifest_hash: str,
    plan_version: str,
    run_ids: Sequence[object],
    policy_version: str = TARGET_DERIVATION_POLICY_V1,
) -> str:
    """`target-derivation-v1`: which precommitted acquisition proves membership.

    The manifest hash is the load-bearing field. Binding run ids alone proves
    only "these targets came from these runs" -- never "these are ALL the runs
    the acquisition required" -- so a caller could omit a required run and
    produce membership that is internally perfect.
    """

    if policy_version not in SUPPORTED_TARGET_DERIVATION_POLICIES:
        raise TargetBindingError(
            f"unknown target-derivation policy {policy_version!r}; supported: "
            f"{sorted(SUPPORTED_TARGET_DERIVATION_POLICIES)}")
    runs = _exact_unique_sorted(run_ids, field="run_ids")
    if not runs:
        raise TargetBindingError(
            "target derivation requires at least one bound acquisition run")
    return _sha256(_canonical({
        "policy": policy_version,
        "acquisition_manifest_hash": _exact_hex64(
            acquisition_manifest_hash, field="acquisition_manifest_hash"),
        "plan_version": _exact_identifier(plan_version, field="plan_version"),
        "run_ids": runs,
    }))


def target_binding_digest(
    *,
    league_id: str,
    members_digest_value: str,
    derivation_digest_value: str,
    policy_version: str = TARGET_BINDING_POLICY_V1,
) -> str:
    """`target-binding-v1`: the composite stored as `target_set_digest`.

    Different members OR different derivation evidence yield a different corpus
    identity, which is correct: those are different scientific claims.
    """

    if policy_version not in SUPPORTED_TARGET_BINDING_POLICIES:
        raise TargetBindingError(
            f"unknown target-binding policy {policy_version!r}; supported: "
            f"{sorted(SUPPORTED_TARGET_BINDING_POLICIES)}")
    return _sha256(_canonical({
        "policy": policy_version,
        "league_id": _exact_identifier(league_id, field="league_id"),
        "members_digest": _exact_hex64(
            members_digest_value, field="members_digest"),
        "derivation_digest": _exact_hex64(
            derivation_digest_value, field="derivation_digest"),
    }))
