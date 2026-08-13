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
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..db.engine import Database
from ..db.repositories.retrospective import semantic_digest
from .attestations import (
    MAP_FORMAT_VERSION,
    TEAM_ATTESTATION_POLICY_VERSION,
    TEAM_ATTESTATIONS,
    attestation_map_digest,
    canonical_team_seed_digest,
)
from .provenance import EntityType, ProviderNamespace
from .sources import open_source_corpus

__all__ = [
    "VerificationReport",
    "referenced_provider_team_ids",
    "verify_corpus",
    "verify_database",
]


@dataclass(frozen=True)
class VerificationReport:
    """Every discrepancy found in one reconstruction corpus."""

    corpus_version_id: str
    checked: int = 0
    map_digest: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)
    #: How many corpus-referenced provider team ids were checked for coverage.
    #: Zero means the reviewed completeness contract was NOT evaluated -- it needs
    #: the source corpus, which the map alone cannot supply.
    referenced_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, object]:
        return {
            "corpus_version_id": self.corpus_version_id,
            "crosswalks_checked": self.checked,
            "referenced_ids_checked": self.referenced_checked,
            "attestation_map_digest": self.map_digest,
            "map_format_version": MAP_FORMAT_VERSION,
            "attestation_policy_version": TEAM_ATTESTATION_POLICY_VERSION,
            "canonical_team_seed_digest": canonical_team_seed_digest(),
            "problems": list(self.problems),
            "ok": self.ok,
        }


def verify_corpus(
    conn: sqlite3.Connection,
    corpus_version_id: str,
    *,
    require_full_league_map: bool = False,
    referenced_provider_team_ids: Optional[Collection[str]] = None,
) -> VerificationReport:
    """Check one corpus's TEAM-A crosswalks against the committed map.

    ``require_full_league_map`` asserts the whole committed league map was
    materialized. ``referenced_provider_team_ids`` asserts the reviewed
    completeness contract: every official team id the corpus actually references
    is covered. See the note at the completeness block for why these differ.
    """

    expected_map_digest = attestation_map_digest()
    problems: list[str] = []

    corpus = conn.execute(
        "SELECT static_identity_map_digest, code_version, league_id "
        "FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (corpus_version_id,)).fetchone()
    if corpus is None:
        return VerificationReport(
            corpus_version_id=corpus_version_id, map_digest=expected_map_digest,
            problems=(f"corpus version {corpus_version_id!r} does not exist",))

    declared = corpus[0]
    code_version = corpus[1]
    league_id = str(corpus[2])

    rows = conn.execute(
        "SELECT provider_id, canonical_entity_id, league_id, provider, "
        "       namespace_generation, provenance_policy_version, "
        "       semantic_digest, identity_audit_digest "
        "FROM static_crosswalk_provenance "
        "WHERE corpus_version_id = ? AND entity_type = 'team' "
        "ORDER BY league_id, provider, namespace_generation, provider_id",
        (corpus_version_id,)).fetchall()

    if not rows:
        # A corpus with no TEAM-A crosswalks is not required to declare a map.
        return VerificationReport(corpus_version_id=corpus_version_id, checked=0,
                                  map_digest=expected_map_digest)

    if not declared:
        problems.append(
            "corpus holds TEAM-A team crosswalks but declares no "
            "static_identity_map_digest")
    elif declared != expected_map_digest:
        problems.append(
            f"corpus declares map digest {str(declared)[:16]}... but the committed "
            f"map digests to {expected_map_digest[:16]}...")
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

        # Review repair (§14/§15). Folding the map digest into semantic_digest
        # binds nothing unless something recomputes it. Without this the row's
        # digest was never checked at all, so "the crosswalk is
        # cryptographically bound to the map" was decorative: a tampered digest,
        # or a TEAM row carrying the older non-map-backed digest, both passed.
        expected_digest = semantic_digest({
            "kind": "static_crosswalk",
            "corpus_version_id": corpus_version_id,
            **ProviderNamespace(str(row[2]), str(row[3]), EntityType.TEAM,
                                str(row[4])).as_dict(),
            "provider_id": str(row[0]),
            "canonical_entity_id": str(row[1]),
            "identity_audit_digest": str(row[7]),
            "provenance_policy_version": str(row[5]),
            "attestation_map_digest": expected_map_digest,
        })
        if str(row[6]) != expected_digest:
            legacy = semantic_digest({
                "kind": "static_crosswalk",
                "corpus_version_id": corpus_version_id,
                **ProviderNamespace(str(row[2]), str(row[3]), EntityType.TEAM,
                                    str(row[4])).as_dict(),
                "provider_id": str(row[0]),
                "canonical_entity_id": str(row[1]),
                "identity_audit_digest": str(row[7]),
                "provenance_policy_version": str(row[5]),
            })
            if str(row[6]) == legacy:
                # Distinguished from tampering on purpose: this row is internally
                # consistent, it simply predates (or bypassed) the map binding.
                # Player crosswalks legitimately have no map digest; a TEAM row
                # must have one.
                problems.append(
                    f"stored crosswalk {key} carries the pre-TEAM-A digest with no "
                    "attestation map bound; it is not map-backed provenance")
            else:
                problems.append(
                    f"stored crosswalk {key} has semantic_digest "
                    f"{str(row[6])[:16]}... but its own contents digest to "
                    f"{expected_digest[:16]}...")

    # Review repair (§17). These are two DIFFERENT properties and were conflated.
    #
    #   full league map  -- every committed entry for the league is materialized.
    #                       Useful, but NOT the reviewed completeness contract:
    #                       a one-month corpus that legitimately references two
    #                       teams would be reported incomplete.
    #   referenced       -- every official team id the corpus actually references
    #                       has a crosswalk. THIS is the reviewed contract, and it
    #                       is the only one that can surface a referenced id which
    #                       is missing from the committed map altogether.
    if require_full_league_map:
        for key in sorted(committed):
            if key[0] == league_id and key not in seen:
                problems.append(f"committed map entry {key} has no stored crosswalk")

    if referenced_provider_team_ids is not None:
        namespaces = {(str(r[2]), str(r[3]), str(r[4])) for r in rows}
        for provider_id in sorted(referenced_provider_team_ids):
            covered = any((lg, pv, gen, provider_id) in seen
                          for lg, pv, gen in namespaces)
            if covered:
                continue
            in_map = any(k[3] == provider_id for k in committed)
            problems.append(
                f"corpus references provider team id {provider_id!r} with no stored "
                + ("crosswalk (attested but not written)" if in_map
                   else "crosswalk, and it is NOT in the committed map at all "
                        "-- this is a selection-bias exclusion, not a clean corpus"))

    return VerificationReport(
        corpus_version_id=corpus_version_id, checked=len(rows),
        map_digest=expected_map_digest, problems=tuple(problems),
        referenced_checked=(0 if referenced_provider_team_ids is None
                            else len(referenced_provider_team_ids)))


def verify_database(
    database_path: Path,
    *,
    corpus_version_id: Optional[str] = None,
    require_full_league_map: bool = False,
    source_db: Optional[Path] = None,
) -> list[VerificationReport]:
    """Verify one corpus, or every corpus holding TEAM-A crosswalks.

    When ``source_db`` is given the reviewed completeness contract is evaluated
    too: the source corpus is opened READ-ONLY through the immutable path and
    its referenced official team ids are checked for coverage.
    """

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
        referenced: Optional[Collection[str]] = None
        if source_db is not None:
            referenced = referenced_provider_team_ids(source_db)
        return [verify_corpus(conn, target,
                              require_full_league_map=require_full_league_map,
                              referenced_provider_team_ids=referenced)
                for target in targets]


def referenced_provider_team_ids(source_db: Path) -> frozenset[str]:
    """Every official team id the corpus references, from READ-ONLY evidence.

    Both the identity snapshots and the home/away sides of scheduled games count:
    a team can be referenced by a game without ever appearing in a team-identity
    response, and that is exactly the referenced-but-unattested case the reviewed
    completeness contract exists to surface.
    """

    conn = open_source_corpus(source_db)
    try:
        found: set[str] = set()
        for sql in (
            "SELECT DISTINCT provider_team_id FROM provider_team_identity_snapshots",
            "SELECT DISTINCT home_provider_team_id FROM game_schedule_snapshots",
            "SELECT DISTINCT away_provider_team_id FROM game_schedule_snapshots",
        ):
            for row in conn.execute(sql):
                if row[0] is not None:
                    found.add(str(row[0]))
        return frozenset(found)
    finally:
        conn.close()
