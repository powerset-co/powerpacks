from __future__ import annotations

import unittest

from packs.search.reflect.snapshots import compare_snapshots, snapshot_identity, validate_complete_evidence


def snapshot(**changes):
    value = {
        "schema_version": "reflect.corpus_snapshot.v1", "set_id": "synthetic-set",
        "backend": "synthetic", "source": "synthetic_test_fixture",
        "verification_status": "verified_comparable",
        "operator_scope_hash": "a" * 64, "membership_hash": "b" * 64,
        "namespace_schema_hashes": {"people": "c" * 64}, "native_content_version": "synthetic-v1",
        "evidence_hashes": {"synthetic-person-1": "d" * 64, "synthetic-person-2": "e" * 64},
        "observed_at": "2026-07-30T00:00:00Z",
    }
    value.update(changes)
    return value


class TestReflectSnapshots(unittest.TestCase):
    def test_observed_at_does_not_change_identity(self) -> None:
        self.assertEqual(snapshot_identity(snapshot()), snapshot_identity(snapshot(observed_at="2026-07-31T00:00:00Z")))

    def test_naive_observed_at_is_not_comparable(self) -> None:
        naive = snapshot(observed_at="2026-07-31T00:00:00")
        self.assertEqual(compare_snapshots(naive, naive)["status"], "non_comparable")

    def test_changed_corpus_or_evidence_is_non_comparable(self) -> None:
        changed = snapshot(membership_hash="f" * 64)
        self.assertEqual(compare_snapshots(snapshot(), changed, ["synthetic-person-1"])["status"], "non_comparable")

    def test_powerset_is_always_non_comparable_before_pr_b_producer(self) -> None:
        remote = snapshot(backend="powerset", source="pr_b_runner_snapshot")
        self.assertEqual(compare_snapshots(remote, remote, ["synthetic-person-1"])["status"], "non_comparable")

    def test_synthetic_cannot_masquerade_as_remote(self) -> None:
        disguised = snapshot(backend="synthetic", source="pr_b_runner_snapshot")
        self.assertEqual(compare_snapshots(disguised, disguised)["status"], "non_comparable")
        changed = snapshot(evidence_hashes={"synthetic-person-1": "0" * 64, "synthetic-person-2": "e" * 64})
        self.assertEqual(compare_snapshots(snapshot(), changed, ["synthetic-person-1"])["status"], "non_comparable")

    def test_missing_evidence_for_labeled_person_absent_from_candidates_fails(self) -> None:
        candidate_snapshot = snapshot(evidence_hashes={"synthetic-person-1": "d" * 64})
        result = compare_snapshots(snapshot(), candidate_snapshot, ["synthetic-person-1", "synthetic-person-2"])
        self.assertEqual(result["status"], "non_comparable")

    def test_complete_review_evidence_validation(self) -> None:
        rows = [{"person_id": "synthetic-person-2", "evidence_hash": "e" * 64}]
        self.assertEqual(validate_complete_evidence(rows, snapshot()), [])


if __name__ == "__main__":
    unittest.main()
