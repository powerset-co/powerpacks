from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

from packs.search.pipeline.models import (
    Backend,
    LocalCorpus,
    PowersetCorpus,
    Profile,
    SearchSpec,
)
from packs.search.reflect.snapshots import (
    RUN_VERIFICATION_STATUSES,
    STRICT_VERIFICATION_STATUS,
    TAGGED_METADATA_VERIFICATION_STATUS,
    canonical_hash,
    compare_snapshots,
    snapshot_identity,
    validate_complete_evidence,
    validate_snapshot,
    watermark_membership_hash,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "packs" / "search" / "schemas"


def snapshot(**changes):
    value = {
        "schema_version": "reflect.corpus_snapshot.v2", "set_id": "synthetic-set",
        "backend": "synthetic", "source": "synthetic_test_fixture",
        "verification_status": "verified_comparable",
        "operator_scope_hash": "a" * 64, "membership_hash": "b" * 64,
        "namespace_schema_hashes": {"people": "c" * 64}, "native_content_version": "synthetic-v1",
        "evidence_hashes": {"synthetic-person-1": "d" * 64, "synthetic-person-2": "e" * 64},
        "observed_at": "2026-07-30T00:00:00Z",
    }
    value.update(changes)
    return value


def tagged_snapshot(**changes):
    metadata = {
        "people": {
            "approx_row_count": 688998,
            "last_write_at": "2026-06-11T16:23:22.000000000Z",
            "index_status": "up-to-date",
        }
    }
    return snapshot(**{
        "verification_status": TAGGED_METADATA_VERIFICATION_STATUS,
        "membership_hash": watermark_membership_hash(metadata),
        "namespace_metadata": metadata,
        "native_content_version": "synthetic-index-tag",
        **changes,
    })


class TestReflectSnapshots(unittest.TestCase):
    def test_intermediate_v1_snapshot_is_not_comparable(self) -> None:
        legacy = snapshot(schema_version="reflect.corpus_snapshot.v1")
        result = compare_snapshots(legacy, snapshot())
        self.assertEqual(result["status"], "non_comparable")
        self.assertIn("unsupported corpus snapshot schema", result["reasons"][0])

    def test_observed_at_does_not_change_identity(self) -> None:
        self.assertEqual(snapshot_identity(snapshot()), snapshot_identity(snapshot(observed_at="2026-07-31T00:00:00Z")))

    def test_nested_observed_at_is_part_of_canonical_record_identity(self) -> None:
        self.assertNotEqual(
            canonical_hash({"record": {"observed_at": "2026-07-30T00:00:00Z"}}),
            canonical_hash({"record": {"observed_at": "2026-07-31T00:00:00Z"}}),
        )

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


class TestTaggedMetadataSnapshots(unittest.TestCase):
    def test_strict_validation_refuses_tagged_but_run_validation_accepts_it(self) -> None:
        tagged = tagged_snapshot()
        errors = validate_snapshot(tagged)
        self.assertTrue(any("verification_status" in error for error in errors), errors)
        self.assertTrue(any(TAGGED_METADATA_VERIFICATION_STATUS in error for error in errors), errors)
        self.assertEqual(validate_snapshot(tagged, accepted_statuses=RUN_VERIFICATION_STATUSES), [])

    def test_tagged_snapshots_are_never_comparable_to_anything(self) -> None:
        tagged = tagged_snapshot()
        self.assertEqual(compare_snapshots(tagged, tagged)["status"], "non_comparable")
        self.assertEqual(compare_snapshots(snapshot(), tagged)["status"], "non_comparable")

    def test_tagged_snapshot_requires_its_cheap_identity_proof(self) -> None:
        for changes, expected in (
            ({"namespace_metadata": {}}, "namespace_metadata watermark document"),
            (
                {"namespace_metadata": {"people": {"last_write_at": None, "index_status": None}}},
                "missing an integer approx_row_count",
            ),
            ({"native_content_version": None}, "caller-supplied native_content_version"),
        ):
            errors = validate_snapshot(
                tagged_snapshot(**changes), accepted_statuses=RUN_VERIFICATION_STATUSES
            )
            self.assertTrue(any(expected in error for error in errors), (changes, errors))

    def test_mutual_exclusion_still_holds_for_both_statuses(self) -> None:
        message = "exactly one of native_content_version or scoped_records_hash is required"
        for status in RUN_VERIFICATION_STATUSES:
            both = tagged_snapshot(verification_status=status, scoped_records_hash="f" * 64)
            neither = tagged_snapshot(
                verification_status=status, native_content_version=None, scoped_records_hash=None
            )
            for value in (both, neither):
                errors = validate_snapshot(value, accepted_statuses=RUN_VERIFICATION_STATUSES)
                self.assertIn(message, errors)
        with self.assertRaisesRegex(ValueError, "at most one Powerset content identity"):
            PowersetCorpus("s", ("o",), native_content_version="tag", scoped_records_hash="f" * 64)
        with self.assertRaisesRegex(ValueError, "at most one local content identity"):
            LocalCorpus("/var/tmp/synthetic.duckdb", "f" * 64, native_content_version="tag")

    def test_tagged_snapshot_round_trips_through_its_json_schema(self) -> None:
        schema = json.loads((SCHEMAS / "reflect-corpus-snapshot.schema.json").read_text())
        powerset_metadata = {
            name: {
                "approx_row_count": 1,
                "last_write_at": "2026-06-11T16:23:22.000000000Z",
                "index_status": "up-to-date",
            }
            for name in ("people", "summaries", "companies", "company_signals", "education", "schools")
        }
        tagged = tagged_snapshot(
            backend="powerset", source="pr_b_runner_snapshot",
            namespace_metadata=powerset_metadata,
            membership_hash=watermark_membership_hash(powerset_metadata),
        )
        jsonschema.validate(tagged, schema)
        self.assertEqual(json.loads(json.dumps(tagged, sort_keys=True)), tagged)
        # A tagged snapshot may not smuggle in a strict content identity or drop its watermark.
        for invalid in (
            {**tagged, "scoped_records_hash": "f" * 64},
            {key: value for key, value in tagged.items() if key != "namespace_metadata"},
        ):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)

    def test_corpus_tag_round_trips_through_the_search_spec_contract(self) -> None:
        schema = json.loads((SCHEMAS / "search-spec.schema.json").read_text())
        for backend, corpus in (
            (Backend.LOCAL, LocalCorpus("/var/tmp/synthetic.duckdb", native_content_version="local-index-v1")),
            (
                Backend.POWERSET,
                PowersetCorpus("synthetic-set", ("synthetic-operator",), native_content_version="set-index-v1"),
            ),
        ):
            spec = SearchSpec("search.spec.v1", "synthetic query", Profile.GTM, backend, corpus)
            value = spec.to_dict()
            jsonschema.validate(value, schema)
            self.assertEqual(value["corpus"]["native_content_version"], corpus.native_content_version)
            self.assertEqual(SearchSpec.from_dict(value).corpus, corpus)

    def test_verification_status_constants_match_the_persisted_schema_file(self) -> None:
        """Read the schema FILE: a hardcoded copy would pass while the contract drifted."""
        schema = json.loads((SCHEMAS / "reflect-corpus-snapshot.schema.json").read_text())
        self.assertEqual(
            sorted(schema["properties"]["verification_status"]["enum"]),
            sorted([STRICT_VERIFICATION_STATUS, TAGGED_METADATA_VERIFICATION_STATUS,
                    "unverified_non_comparable"]),
        )
        self.assertEqual(
            RUN_VERIFICATION_STATUSES,
            (STRICT_VERIFICATION_STATUS, TAGGED_METADATA_VERIFICATION_STATUS),
        )
        # The schema's two conditional branches must key off the same two constants.
        branches = {
            rule["if"]["properties"]["verification_status"]["const"]: rule
            for rule in schema["allOf"]
            if "verification_status" in rule["if"]["properties"]
        }
        self.assertEqual(set(branches), {STRICT_VERIFICATION_STATUS, TAGGED_METADATA_VERIFICATION_STATUS})
        self.assertEqual(
            sorted(branches[TAGGED_METADATA_VERIFICATION_STATUS]["then"]["required"]),
            ["namespace_metadata", "native_content_version"],
        )

    def test_corpus_oneof_accepts_documents_that_omit_the_identity_keys(self) -> None:
        """Each oneOf branch must be exclusive by `required`, not by vacuous properties."""
        schema = json.loads((SCHEMAS / "search-spec.schema.json").read_text())
        base = SearchSpec(
            "search.spec.v1", "synthetic query", Profile.GTM, Backend.LOCAL,
            LocalCorpus("/var/tmp/synthetic.duckdb"),
        ).to_dict()
        remote = SearchSpec(
            "search.spec.v1", "synthetic query", Profile.GTM, Backend.POWERSET,
            PowersetCorpus("synthetic-set", ("synthetic-operator",)),
        ).to_dict()
        for value, tag_key, hash_key in (
            (base, "native_content_version", "content_hash"),
            (remote, "native_content_version", "scoped_records_hash"),
        ):
            corpus = value["corpus"]
            variants = [
                dict(corpus),  # both keys present and null
                {k: v for k, v in corpus.items() if k != tag_key},  # pre-tag document shape
                {k: v for k, v in corpus.items() if k != hash_key},
                {k: v for k, v in corpus.items() if k not in {tag_key, hash_key}},
            ]
            for variant in variants:
                jsonschema.validate({**value, "corpus": variant}, schema)
            # Supplying both identities at once is still the one rejected shape.
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    {**value, "corpus": {**corpus, tag_key: "tag", hash_key: "f" * 64}}, schema
                )

    def test_watermark_membership_hash_tracks_the_write_watermark(self) -> None:
        base = {"people": {"approx_row_count": 1, "last_write_at": "2026-06-11T16:23:22.000000000Z",
                           "index_status": "up-to-date"}}
        moved = {"people": {**base["people"], "last_write_at": "2026-08-06T00:00:00.000000000Z"}}
        self.assertEqual(watermark_membership_hash(base), watermark_membership_hash(dict(base)))
        self.assertNotEqual(watermark_membership_hash(base), watermark_membership_hash(moved))
        self.assertNotEqual(STRICT_VERIFICATION_STATUS, TAGGED_METADATA_VERIFICATION_STATUS)


if __name__ == "__main__":
    unittest.main()
