from __future__ import annotations

import unittest
import json
import tempfile
import copy
from pathlib import Path

from packs.search.reflect.review import (
    build_review_packet,
    finalize_human_labels,
    merge_human_labels,
    review_pool_evidence_hash,
    validate_ground_truth_semantics,
)
from packs.search.reflect.snapshots import snapshot_identity
from packs.search.primitives.validate_artifact.validate_artifact import validate_file


def candidate(person_id="synthetic-person-1", title="Synthetic Engineer"):
    return {"person_id": person_id, "evidence": {
        "role": [{"title": title, "current": True, "start_date": None, "end_date": None}],
        "company": [{"name": "Example Systems", "relationship": "current"}],
        "location": [{"value": "Example City", "source": "profile"}],
        "matched_positions": [{"position_id": "synthetic-position-1", "title": title, "company": "Example Systems", "location": "Example City"}],
        "retrieval_provenance": [{"lane": "synthetic_bm25", "probe": "synthetic-probe", "rank": 1, "score": 0.5}],
        "relevant_profile_evidence": ["Built synthetic distributed systems."],
    }}


def snapshot(packet=None):
    hashes = {row["person_id"]: row["evidence_hash"] for row in packet["rows"]} if packet else {}
    return {"schema_version": "reflect.corpus_snapshot.v1", "backend": "synthetic", "source": "synthetic_test_fixture",
            "verification_status": "verified_comparable", "set_id": "synthetic-set", "operator_scope_hash": "a" * 64,
            "membership_hash": "b" * 64, "namespace_schema_hashes": {"people": "c" * 64},
            "native_content_version": "synthetic-v1", "evidence_hashes": hashes}


class TestReflectReview(unittest.TestCase):
    def packet(self):
        candidates = [candidate(), candidate("synthetic-person-2", "Synthetic Designer")]
        preliminary = build_review_packet(case_id="synthetic-case", case_hash="9" * 64, corpus_snapshot_hash="0" * 64,
                                          candidates=candidates)
        corpus = snapshot(preliminary)
        return build_review_packet(case_id="synthetic-case", case_hash="9" * 64, corpus_snapshot_hash=snapshot_identity(corpus),
                                   candidates=candidates)

    def label_all(self, labels, decision="eligible_bench"):
        for row in labels["rows"]:
            row["human"] = {"decision": decision, "reason_codes": ["synthetic_fit"], "notes": "",
                            "reviewer": "Synthetic Reviewer", "reviewed_at": "2026-07-31T00:00:00Z"}

    def test_resumption_preserves_matching_and_clears_stale_evidence(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        resumed = merge_human_labels(packet, labels)
        self.assertEqual(resumed["rows"][0]["human"]["decision"], "eligible_bench")
        changed = self.packet()
        changed["rows"][0] = build_review_packet(case_id="x", case_hash="9" * 64, corpus_snapshot_hash="f" * 64,
                                                 candidates=[candidate(title="Changed Synthetic Engineer")])["rows"][0]
        changed["review_pool_evidence_hash"] = review_pool_evidence_hash(changed["rows"])
        with self.assertRaises(ValueError):
            merge_human_labels(changed, labels)

    def test_resume_rejects_cross_case_and_changed_binding(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        for field, value in (("case_id", "other-case"), ("case_hash", "0" * 64),
                             ("corpus_snapshot_hash", "1" * 64), ("review_pool_evidence_hash", "2" * 64)):
            stale = copy.deepcopy(labels)
            stale[field] = value
            with self.assertRaises(ValueError, msg=field):
                merge_human_labels(packet, stale)

    def test_post_hash_evidence_mutation_and_duplicates_are_rejected(self) -> None:
        packet = self.packet()
        mutated = copy.deepcopy(packet)
        mutated["rows"][0]["evidence"]["role"][0]["title"] = "Mutated"
        with self.assertRaises(ValueError):
            merge_human_labels(mutated)
        duplicate = copy.deepcopy(packet)
        duplicate["rows"].append(copy.deepcopy(duplicate["rows"][0]))
        with self.assertRaises(ValueError):
            merge_human_labels(duplicate)

    def test_finalization_requires_explicit_human_fields(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        with self.assertRaises(ValueError):
            finalize_human_labels(packet, labels, snapshot(packet))

    def test_ground_truth_semantics_reject_pool_and_label_mismatch(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        gt = finalize_human_labels(packet, labels, snapshot(packet), finalized_at="2026-07-31T00:00:00Z")
        gt["labels"][0]["evidence_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_ground_truth_semantics(gt)

    def test_insufficient_evidence_is_unresolved_and_excluded(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        labels["rows"][1]["human"]["decision"] = "insufficient_evidence"
        gt = finalize_human_labels(packet, labels, snapshot(packet), finalized_at="2026-07-31T00:00:00Z")
        self.assertEqual([row["person_id"] for row in gt["labels"]], ["synthetic-person-1", "synthetic-person-2"])
        self.assertEqual(gt["labels"][1]["decision"], "insufficient_evidence")
        self.assertEqual(set(gt["review_pool_evidence_hashes"]), {"synthetic-person-1", "synthetic-person-2"})

    def test_finalization_rejects_changed_snapshot_evidence(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        stale_snapshot = snapshot(packet)
        stale_snapshot["evidence_hashes"]["synthetic-person-2"] = "0" * 64
        with self.assertRaises(ValueError):
            finalize_human_labels(packet, labels, stale_snapshot)

    def test_duplicate_labels_and_naive_timestamps_are_rejected(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        duplicate = copy.deepcopy(labels)
        duplicate["rows"].append(copy.deepcopy(duplicate["rows"][0]))
        with self.assertRaises(ValueError):
            finalize_human_labels(packet, duplicate, snapshot(packet))
        labels["rows"][0]["human"]["reviewed_at"] = "2026-07-31T00:00:00"
        with self.assertRaises(ValueError):
            finalize_human_labels(packet, labels, snapshot(packet))

    def test_only_insufficient_allows_empty_structured_evidence(self) -> None:
        empty_evidence = {field: [] for field in ("role", "company", "location", "matched_positions",
                                                   "retrieval_provenance", "relevant_profile_evidence")}
        empty_packet = build_review_packet(case_id="synthetic-case", case_hash="9" * 64,
                                           corpus_snapshot_hash="0" * 64,
                                           candidates=[{"person_id": "synthetic-empty", "evidence": empty_evidence}])
        corpus = snapshot(empty_packet)
        empty_packet["corpus_snapshot_hash"] = snapshot_identity(corpus)
        labels = merge_human_labels(empty_packet)
        self.label_all(labels, decision="eligible_bench")
        with self.assertRaises(ValueError):
            finalize_human_labels(empty_packet, labels, corpus)
        labels["rows"][0]["human"]["decision"] = "insufficient_evidence"
        gt = finalize_human_labels(empty_packet, labels, corpus, finalized_at="2026-07-31T00:00:00Z")
        self.assertEqual(gt["labels"][0]["decision"], "insufficient_evidence")

    def test_generated_artifacts_use_strict_repo_schemas(self) -> None:
        packet = self.packet()
        labels = merge_human_labels(packet)
        self.label_all(labels)
        corpus = snapshot(packet)
        gt = finalize_human_labels(packet, labels, corpus, finalized_at="2026-07-31T00:00:00Z")
        artifacts = {
            "reflect-review-packet": packet,
            "reflect-human-labels": labels,
            "reflect-corpus-snapshot": corpus,
            "reflect-ground-truth": gt,
        }
        root = Path(tempfile.mkdtemp())
        for schema, document in artifacts.items():
            path = root / f"{schema}.json"
            path.write_text(json.dumps(document) + "\n")
            self.assertEqual(validate_file(schema, path), document)
        packet["unexpected"] = True
        bad = root / "bad.json"
        bad.write_text(json.dumps(packet) + "\n")
        with self.assertRaises(ValueError):
            validate_file("reflect-review-packet", bad)


if __name__ == "__main__":
    unittest.main()
