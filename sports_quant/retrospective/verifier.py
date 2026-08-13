"""The committed-map verifier (review repair RV1 #3).

The independent review proved that schema v19 will accept a team crosswalk that
contradicts the committed attestation map: the corpus records
``static_identity_map_digest``, but SQLite cannot read the map's contents, so it
cannot check membership. The database is not the wrong place for the other G5
bindings -- those are row-to-row and are DB-enforced -- it simply cannot enforce
agreement with an **external artifact**.

This verifier closes that loop as a **detective** control. It re-derives the map
from committed source and checks every stored TEAM-A crosswalk against it, so the
adversarial case the review constructed --

    committed map:      147 -> tm_mlb_hou
    stored crosswalk:   147 -> tm_mlb_nyy

-- is caught even though the row satisfies every v19 constraint.

**This is a code + CI invariant and is weaker than DB row-to-row enforcement.**
Stated plainly rather than glossed: direct SQL can still write a contradicting
row; what this guarantees is that such a row cannot survive a verification pass.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..db.engine import Database
from .attestations import (
    MAP_FORMAT_VERSION,
    TEAM_ATTESTATION_POLICY_VERSION,
    TEAM_ATTESTATIONS,
    attestation_map_digest,
    canonical_team_seed_digest,
)

__all__ = ["VerificationReport", "verify_corpus", "verify_database"]


@dataclass(frozen=True)
class VerificationReport:
    """Every discrepancy found in one reconstruction corpus."""

    corpus_version_id: str
    checked: int = 0
    map_digest: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, object]:
        return {
            "corpus_version_id": self.corpus_version_id,
            "crosswalks_checked": self.checked,
            "attestation_map_digest": self.map_digest,
            "map_format_version": MAP_FORMAT_VERSION,
            "attestation_policy_version": TEAM_ATTESTATION_POLICY_VERSION,
            "canonical_team_seed_digest": canonical_team_seed_digest(),
            "problems": list(self.problems),
            "ok": self.ok,
        }


def verify_corpus(
    conn: sqlite3.Connection, corpus_version_id: str, *, require_complete: bool = False
) -> VerificationReport:
    """Check one corpus's TEAM-A crosswalks against the committed map."""

    expected_digest = attestation_map_digest()
    problems: list[str] = []

    corpus = conn.execute(
        "SELECT static_identity_map_digest, code_version, league_id "
        "FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (corpus_version_id,)).fetchone()
    if corpus is None:
        return VerificationReport(
            corpus_version_id=corpus_version_id, map_digest=expected_digest,
            problems=(f"corpus version {corpus_version_id!r} does not exist",))

    declared = corpus[0]
    code_version = corpus[1]
    league_id = str(corpus[2])

    rows = conn.execute(
        "SELECT provider_id, canonical_entity_id, league_id, provider, "
        "       namespace_generation, provenance_policy_version "
        "FROM static_crosswalk_provenance "
        "WHERE corpus_version_id = ? AND entity_type = 'team' "
        "ORDER BY league_id, provider, namespace_generation, provider_id",
        (corpus_version_id,)).fetchall()

    if not rows:
        # A corpus with no TEAM-A crosswalks is not required to declare a map.
        return VerificationReport(corpus_version_id=corpus_version_id, checked=0,
                                  map_digest=expected_digest)

    if not declared:
        problems.append(
            "corpus holds TEAM-A team crosswalks but declares no "
            "static_identity_map_digest")
    elif declared != expected_digest:
        problems.append(
            f"corpus declares map digest {str(declared)[:16]}... but the committed "
            f"map digests to {expected_digest[:16]}...")
    if not code_version:
        problems.append("corpus declares no code_version (reproducibility contract)")

    committed = {
        (e.league_id, e.provider, e.namespace_generation, e.provider_team_id):
            e.canonical_team_id
        for e in TEAM_ATTESTATIONS
    }
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (str(row[2]), str(row[3]), str(row[4]), str(row[0]))
        seen.add(key)
        expected_team = committed.get(key)
        if expected_team is None:
            problems.append(
                f"stored crosswalk {key} is NOT a member of the committed map")
            continue
        if str(row[1]) != expected_team:
            problems.append(
                f"stored crosswalk {key} binds {row[1]!r} but the committed map "
                f"says {expected_team!r}")
        if str(row[5]) != TEAM_ATTESTATION_POLICY_VERSION:
            problems.append(
                f"stored crosswalk {key} carries policy {row[5]!r}, expected "
                f"{TEAM_ATTESTATION_POLICY_VERSION!r}")

    if require_complete:
        for key in sorted(committed):
            if key[0] == league_id and key not in seen:
                problems.append(f"committed map entry {key} has no stored crosswalk")

    return VerificationReport(
        corpus_version_id=corpus_version_id, checked=len(rows),
        map_digest=expected_digest, problems=tuple(problems))


def verify_database(
    database_path: Path, *, corpus_version_id: Optional[str] = None,
    require_complete: bool = False,
) -> list[VerificationReport]:
    """Verify one corpus, or every corpus holding TEAM-A crosswalks."""

    with Database(database_path).connection() as conn:
        if corpus_version_id is not None:
            targets = [corpus_version_id]
        else:
            targets = [
                str(r[0]) for r in conn.execute(
                    "SELECT DISTINCT corpus_version_id FROM "
                    "static_crosswalk_provenance WHERE entity_type = 'team' "
                    "ORDER BY corpus_version_id")
            ]
        return [verify_corpus(conn, target, require_complete=require_complete)
                for target in targets]
