"""`bin/deep-context seed`: legacy decisions and paid results land on cold parents by identifier.

Changelog:
- 2026-09-25: created with the seed stage.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.common.legacy import LEGACY_PARALLEL_HANDLE_RESULT
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.migration import seed
from packs.ingestion.primitives.deep_context.migration.seed import (
    SEEDED_AT_KEY,
    Seed,
    SeedRefused,
    carried_over_at,
)
from packs.ingestion.primitives.deep_context.shared.check_readiness import (
    ENSURE_PARENTS_COMMAND,
    OWNER_COMMAND,
    SEED_COMMAND,
    CheckReadiness,
)
from packs.shared.csv_io import CsvIO

REVIEW_COLUMNS = [
    "public_identifier", "worth_person_ids", "action", "approved", "new_linkedin_url",
    "new_public_identifier", "linkedin_url", "match_emails", "match_phones", "confidence",
    "reason", "person_id", "source", "updated_at", "llm_reject", "llm_reject_confidence",
    "llm_reject_reason", "llm_judge_fingerprint", "llm_worth", "llm_worth_reason",
    "network_worth", "user_worth_note",
]
JORDAN_URL = "https://www.linkedin.com/in/jordan-bravo"


def _facts_record(name: str, updated_at: str, *, worth: str = "yes") -> str:
    return json.dumps({
        "chunk_index": 0,
        "facts": {
            "canonical_name": name,
            "network_worth": {"decision": worth, "reason": "synthetic"},
            "confidence": 0.7,
        },
        "usage": {"input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0},
        "batches_used": 1, "batches_total": 1, "messages_used": 1, "messages_available": 1,
        "final_confidence": 0.7, "stop_reason": "exhausted", "updated_at": updated_at,
    }) + "\n"


class SeedFixture(unittest.TestCase):
    """A cold store from people.csv plus a synthetic legacy tree beside it."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        self.state = self.root / ".powerpacks"
        self.deep_context = self.state / "deep-context"
        self.db_path = self.deep_context / "deep-context.sqlite"
        self.people_csv = self.state / "network-import/merged/people.csv"
        CsvIO.write_dict_rows(
            self.people_csv,
            ["id", "full_name", "primary_email", "phone", "linkedin_url", "source_channels"],
            [
                {"id": "person-jordan", "full_name": "Jordan Bravo", "primary_email": "jordan@example.com",
                 "linkedin_url": JORDAN_URL, "source_channels": "linkedin"},
                {"id": "candidate:email:casey@example.com", "full_name": "Casey Alpha",
                 "primary_email": "casey@example.com", "source_channels": "gmail_msgvault"},
                {"id": "candidate:phone:+15550100", "full_name": "Riley Charlie", "phone": "+15550100",
                 "source_channels": "imessage"},
                {"id": "person-morgan", "full_name": "Morgan Delta", "primary_email": "morgan@example.com",
                 "source_channels": "gmail_msgvault"},
                {"id": "person-avery", "full_name": "Avery Echo", "primary_email": "avery@example.com",
                 "source_channels": "gmail_msgvault"},
            ],
        )
        self.legacy = self.root / "legacy"
        self.write_legacy_tree()

    def write_legacy_tree(self) -> None:
        legacy_dc = self.legacy / "deep-context"
        (legacy_dc / "facts").mkdir(parents=True)
        (legacy_dc / "index.json").write_text(json.dumps({
            "parents": {
                "jordan-bravo-parent": {
                    "parent_id": "parent-000000000001", "name": "Jordan Bravo",
                    "children": ["jordan-child", "casey-child"],
                },
                "morgan-delta-parent": {
                    "parent_id": "parent-000000000002", "name": "Morgan Delta",
                    "children": ["morgan-child"],
                },
            },
            "slugs": {
                "jordan-child": {"person_id": "person-jordan", "name": "Jordan Bravo",
                                 "emails": ["jordan@example.com"], "phones": []},
                "casey-child": {"person_id": "candidate:email:casey@example.com", "name": "Casey Alpha",
                                "emails": ["casey@example.com"], "phones": []},
                "morgan-child": {"person_id": "person-morgan", "name": "Morgan Delta",
                                 "emails": ["morgan@example.com"], "phones": []},
            },
        }), encoding="utf-8")
        (legacy_dc / "facts/person-jordan.jsonl").write_text(
            _facts_record("Jordan Bravo", "2026-09-02T00:00:00+00:00"), encoding="utf-8")
        (legacy_dc / "facts/candidate:email:casey@example.com.jsonl").write_text(
            _facts_record("Casey Alpha", "2026-09-01T00:00:00+00:00"), encoding="utf-8")
        (legacy_dc / "facts/candidate:phone:+15550100.jsonl").write_text(
            _facts_record("Riley Charlie", "2026-09-01T00:00:00+00:00", worth="maybe"), encoding="utf-8")
        (legacy_dc / "facts/person-ghost.jsonl").write_text(
            _facts_record("Nobody Known", "2026-09-01T00:00:00+00:00"), encoding="utf-8")
        (legacy_dc / "raw").mkdir()
        for subject, name, email, collected_at in (
            ("person-jordan", "Jordan Bravo", "jordan@example.com", "2026-09-02T00:00:00+00:00"),
            ("candidate:email:casey@example.com", "Casey Alpha", "casey@example.com", "2026-09-01T00:00:00+00:00"),
        ):
            (legacy_dc / f"raw/{subject}.json").write_text(json.dumps({
                "person_id": subject, "full_name": name, "emails": [email], "phones": [],
                "source_channels": ["gmail_msgvault"], "messages": [], "messages_available": 0,
                "capped": False, "collected_at": collected_at,
            }), encoding="utf-8")
        research = legacy_dc / "reconcile/deep-research/morgan-delta-parent"
        research.mkdir(parents=True)
        (research / "00_parallel_result.json").write_text(json.dumps({
            "type": "json",
            "content": {"real_name": "Morgan Delta", "work_experience": [], "education": [],
                        "linkedin_url": "https://www.linkedin.com/in/morgan-delta"},
            "basis": [],
        }), encoding="utf-8")
        overrides = self.legacy / "network-import/overrides"
        overrides.mkdir(parents=True)
        (self.legacy / "network-import/merged").mkdir()
        (self.legacy / "network-import/merged/people.csv").write_text(
            "id,full_name\nperson-jordan,Jordan Bravo\n", encoding="utf-8")
        with (overrides / "review.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
            writer.writeheader()
            writer.writerow({"public_identifier": "candidate:phone:+15550100", "network_worth": "no",
                             "user_worth_note": "spam line", "source": "deep-context-synthesis",
                             "updated_at": "2026-09-03T00:00:00Z"})
            writer.writerow({"public_identifier": "jordan-bravo", "action": "verify", "approved": "yes",
                             "linkedin_url": JORDAN_URL, "person_id": "person-jordan",
                             "source": "deep-context-review", "updated_at": "2026-09-04T00:00:00Z"})
            writer.writerow({"public_identifier": "person-morgan", "action": "verify", "approved": "auto",
                             "llm_worth": "yes", "source": "deep-context-reconcile",
                             "updated_at": "2026-09-01T00:00:00Z"})
            writer.writerow({"public_identifier": "candidate:email:nobody@example.com",
                             "network_worth": "yes", "updated_at": "2026-09-01T00:00:00Z"})
            writer.writerow({"public_identifier": "message-linkedin:abc123def456", "action": "detach",
                             "approved": "yes", "source": "deep-context-review",
                             "updated_at": "2026-09-05T00:00:00Z"})
        (overrides / "synthetic-people.csv").write_text(
            "id,full_name,approved\nsynthetic:one,Sam Foxtrot,yes\n", encoding="utf-8")

    def cold_store(self) -> Db:
        db = Db(self.db_path)
        EnsureParents(db=db, people_csv=self.people_csv).run()
        return db

    def seed(self, db: Db):
        return Seed(db=db, legacy_root=self.legacy).run()

    def readiness(self, db: Db | None = None):
        check = "packs.ingestion.primitives.deep_context.shared.check_readiness"
        with (
            mock.patch(f"{check}.load_env"),
            mock.patch(f"{check}.context_sources.probe_chat_db", return_value=ChatDbProbe(False, False, 0, 0, None)),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "TYPESAFE_API_KEY": "k"}, clear=False),
        ):
            return CheckReadiness(
                db=db, db_path=self.db_path, people_csv=self.people_csv,
                msgvault_db=self.root / "missing-msgvault.db", chat_db=self.root / "missing-chat.db",
                wacli_db=self.root / "missing-wacli.db",
            ).run()


class SeedTests(SeedFixture):
    def test_seed_preserves_original_contact_fact_ownership_after_family_merge(self) -> None:
        paths = [self.legacy / f"deep-context/facts/{subject}.jsonl" for subject in (
            "person-jordan", "candidate:email:casey@example.com",
        )]
        originals = {path: path.read_bytes() for path in paths}
        db = self.cold_store()
        self.seed(db)
        family = next(row.parent_id for row in queries.people(db) if row.person_id == "person-jordan")
        facts = {row.person_id: row for row in queries.facts(db) if row.person_id}
        for path in paths:
            subject = path.stem
            self.assertIn(subject, facts)
            fact = facts[subject]
            self.assertEqual((fact.subject_key, fact.parent_id), (subject, family))
            artifact = db.query("SELECT * FROM artifacts WHERE artifact_key=?", (fact.artifact_key,))[0]
            self.assertEqual((artifact["person_id"], artifact["parent_id"]), (subject, family))
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertEqual(path.read_bytes(), originals[path])
            self.assertEqual(json.loads(artifact["payload_json"])["facts"], json.loads(originals[path])["facts"])

    def test_seeded_candidate_keeps_only_original_contact_membership(self) -> None:
        review = self.legacy / "network-import/overrides/review.csv"
        with review.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS).writerow({
                "public_identifier": "candidate:email:jordan@example.com", "person_id": "person-jordan",
                "action": "detach", "approved": "yes", "source": "deep-context-review",
                "updated_at": "2026-09-06T00:00:00Z",
            })
        db = self.cold_store()
        self.seed(db)
        people = db.query("SELECT person_id FROM candidate_people WHERE row_key=?",
                          ("candidate:email:jordan@example.com",))
        self.assertEqual([row["person_id"] for row in people], ["person-jordan"])

    def test_seed_keeps_all_extractions_for_one_contact_from_legacy_aliases(self) -> None:
        path = self.legacy / "deep-context/facts/jordan-bravo.jsonl"
        older = json.loads(_facts_record("Jordan Bravo", "2026-08-01T00:00:00Z"))
        older["facts"]["employers"] = [{"name": "Synthetic Company", "role": "Engineer", "status": "past"}]
        path.write_text(json.dumps(older) + "\n", encoding="utf-8")
        db = self.cold_store()
        manifest = self.seed(db)
        artifact = db.query("SELECT payload_json FROM artifacts WHERE artifact_key='facts:person-jordan'")[0]
        history = json.loads(artifact["payload_json"])
        self.assertEqual(len(history["records"]), 2)
        self.assertIn("Synthetic Company", {employer["name"] for employer in history["facts"]["employers"]})
        self.assertEqual(manifest.facts_duplicate_dropped, 0)

    def test_seed_candidate_identifier_retains_contact_without_legacy_person_id(self) -> None:
        review = self.legacy / "network-import/overrides/review.csv"
        with review.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS).writerow({
                "public_identifier": "candidate:email:jordan@example.com",
                "action": "detach", "approved": "yes", "source": "deep-context-review",
            })
        db = self.cold_store()
        self.seed(db)
        people = db.query("SELECT person_id FROM candidate_people WHERE row_key=?",
                          ("candidate:email:jordan@example.com",))
        self.assertEqual([row["person_id"] for row in people], ["person-jordan"])

    def test_seed_keeps_mixed_parent_history_separate_from_contact_facts(self) -> None:
        path = self.legacy / "deep-context/facts/parent-000000000001.jsonl"
        path.write_text(_facts_record("Mixed legacy family", "2026-10-01T00:00:00Z"), encoding="utf-8")
        db = self.cold_store()
        self.seed(db)
        facts = queries.facts(db)
        self.assertEqual({row.person_id for row in facts if row.person_id}, {
            "person-jordan", "candidate:email:casey@example.com", "candidate:phone:+15550100",
        })
        mixed = next(row for row in facts if row.person_id is None)
        self.assertEqual(json.loads(mixed.facts_json)["canonical_name"], "Mixed legacy family")

    def test_in_place_seed_leaves_original_contact_files_unchanged(self) -> None:
        paths = list((self.legacy / "deep-context/facts").glob("*.jsonl"))
        paths += list((self.legacy / "deep-context/raw").glob("*.json"))
        originals = {path: path.read_bytes() for path in paths}
        db = self.cold_store()
        Seed(db=db, legacy_root=self.legacy,
             facts_dir=self.legacy / "deep-context/facts",
             raw_dir=self.legacy / "deep-context/raw").run()
        self.assertEqual({path: path.read_bytes() for path in paths}, originals)
        artifacts = [row for row in queries.artifacts(db) if row.kind in ("facts", "source_bundle")]
        self.assertTrue(all(Path(row.path).parent.name == "seed" for row in artifacts))

    def test_saved_verify_merges_source_with_imported_linkedin_before_copying_decision(self) -> None:
        review = self.legacy / "network-import/overrides/review.csv"
        with review.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS).writerow({
                "public_identifier": "jordan-bravo", "person_id": "candidate:phone:+15550100",
                "linkedin_url": JORDAN_URL, "action": "verify", "approved": "yes",
                "source": "deep-context-review", "updated_at": "2026-09-06T00:00:00Z",
            })
        db = self.cold_store()
        self.seed(db)
        parent_of = {row.person_id: row.parent_id for row in queries.people(db)}
        self.assertEqual(parent_of["person-jordan"], parent_of["candidate:phone:+15550100"])
        self.assertEqual(db.query("SELECT decision_action FROM links WHERE row_key='jordan-bravo'")[0][0], "verify")
        parent = next(row for row in queries.parents(db) if row.parent_id == parent_of["person-jordan"])
        self.assertEqual(parent.human_worth, "no")

    def test_saved_detach_does_not_merge_source_or_detach_other_persons_linkedin(self) -> None:
        review = self.legacy / "network-import/overrides/review.csv"
        rows = list(CsvIO.read_dict_rows(review))
        rows = [row for row in rows if row["public_identifier"] != "jordan-bravo"]
        rows.append({
            "public_identifier": "jordan-bravo", "person_id": "candidate:phone:+15550100",
            "linkedin_url": JORDAN_URL, "action": "detach", "approved": "yes",
            "source": "deep-context-review", "updated_at": "2026-09-06T00:00:00Z",
        })
        CsvIO.write_dict_rows(review, REVIEW_COLUMNS, rows)
        db = self.cold_store()
        manifest = self.seed(db)
        parent_of = {row.person_id: row.parent_id for row in queries.people(db)}
        self.assertNotEqual(parent_of["person-jordan"], parent_of["candidate:phone:+15550100"])
        self.assertIsNone(db.query("SELECT decision_action FROM links WHERE row_key='jordan-bravo'")[0][0])
        self.assertEqual(manifest.identity_two_plus, 1)

    def test_disagreeing_research_is_left_for_normal_enrichment(self) -> None:
        from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue

        base = self.legacy / "deep-context/reconcile/deep-research"
        (base / "morgan-alternative").mkdir()
        (base / "morgan-alternative/00_parallel_result.json").write_text(json.dumps({
            "type": "json", "content": {"linkedin_url": ""},
            "metadata": {"source_identifier": "morgan@example.com"},
        }))
        (self.legacy / "deep-context/facts/person-morgan.jsonl").write_text(
            _facts_record("Morgan Delta", "2026-09-02T00:00:00Z"))
        db = self.cold_store()
        manifest = self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "person-morgan")
        self.assertFalse(db.query("SELECT 1 FROM research WHERE parent_id=?", (parent_id,)))
        self.assertFalse(db.query("SELECT 1 FROM artifacts WHERE parent_id=? AND kind='research'", (parent_id,)))
        self.assertFalse(db.query("SELECT 1 FROM links WHERE parent_id=? AND kind='research'", (parent_id,)))
        self.assertIn(parent_id, {row.parent_id for row in enrichment_queue(db)})
        self.assertEqual(manifest.research_duplicate_dropped, 2)

    def test_equivalent_research_urls_are_still_reused(self) -> None:
        base = self.legacy / "deep-context/reconcile/deep-research"
        (base / "morgan-alternative").mkdir()
        (base / "morgan-alternative/00_parallel_result.json").write_text(json.dumps({
            "type": "json", "content": {"linkedin_url": "https://linkedin.com/in/morgan-delta/"},
            "metadata": {"source_identifier": "morgan@example.com"},
        }))
        db = self.cold_store()
        manifest = self.seed(db)
        self.assertEqual(len(db.query("SELECT * FROM research")), 1)
        self.assertEqual(manifest.research_carried, 1)
        self.assertEqual(manifest.research_duplicate_dropped, 1)

    def test_different_nonempty_research_urls_are_not_chosen_by_filename(self) -> None:
        base = self.legacy / "deep-context/reconcile/deep-research"
        (base / "morgan-alternative").mkdir()
        (base / "morgan-alternative/00_parallel_result.json").write_text(json.dumps({
            "type": "json", "content": {"linkedin_url": "https://linkedin.com/in/another-morgan"},
            "metadata": {"source_identifier": "morgan@example.com"},
        }))
        db = self.cold_store()
        manifest = self.seed(db)
        self.assertFalse(db.query("SELECT 1 FROM research"))
        self.assertEqual(manifest.research_duplicate_dropped, 2)

    def test_agreeing_no_match_research_is_reused(self) -> None:
        base = self.legacy / "deep-context/reconcile/deep-research"
        (base / "morgan-alternative").mkdir()
        for name in ("morgan-alternative", "morgan-delta-parent"):
            path = base / name / "00_parallel_result.json"
            path.write_text(json.dumps({
                "type": "json", "content": {"linkedin_url": ""},
                "metadata": {"source_identifier": "morgan@example.com"},
            }))
        db = self.cold_store()
        manifest = self.seed(db)
        self.assertEqual([row["status"] for row in db.query("SELECT status FROM research")], ["no_match"])
        self.assertEqual(manifest.research_carried, 1)
        self.assertEqual(manifest.research_duplicate_dropped, 1)

    def test_bare_slugs_follow_the_roster_normalization(self) -> None:
        self.assertEqual(seed._slug("jordan%2Dbravo"), "jordan-bravo")
        self.assertEqual(seed._slug("Jordan-Bravo/"), "jordan-bravo")
        self.assertEqual(seed._slug("https://www.linkedin.com/in/jordan%2Dbravo/"), "jordan-bravo")
        self.assertEqual(seed._slug("candidate:email:casey@example.com"), "")

    def test_merges_first_then_facts_decisions_and_research_land_by_identifier(self) -> None:
        db = self.cold_store()
        self.assertEqual(len(queries.parents(db)), 5)

        manifest = self.seed(db)

        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.merges_applied, 1)
        parents = queries.parents(db)
        self.assertEqual(len(parents), 4)
        parent_of = {row.person_id: row.parent_id for row in queries.people(db)}
        family = parent_of["person-jordan"]
        self.assertEqual(parent_of["candidate:email:casey@example.com"], family)

        facts = {row.person_id: row for row in queries.facts(db)}
        self.assertEqual(set(facts), {"person-jordan", "candidate:email:casey@example.com", "candidate:phone:+15550100"})
        self.assertEqual(json.loads(facts["person-jordan"].facts_json)["canonical_name"], "Jordan Bravo")
        self.assertEqual(json.loads(facts["candidate:email:casey@example.com"].facts_json)["canonical_name"], "Casey Alpha")
        self.assertEqual(facts["candidate:phone:+15550100"].machine_worth, "maybe")
        self.assertEqual(
            (manifest.facts_carried, manifest.facts_duplicate_dropped, manifest.facts_unmatched),
            (3, 0, 1),
        )
        self.assertTrue((self.deep_context / "facts/person-jordan.jsonl").is_file())

        riley = next(row for row in parents if row.parent_id == parent_of["candidate:phone:+15550100"])
        self.assertEqual((riley.human_worth, riley.human_worth_note), ("no", "spam line"))
        self.assertEqual(riley.human_worth_source, "deep-context-review")
        self.assertEqual((manifest.worth_carried, manifest.worth_unmatched), (1, 1))

        link = db.query("SELECT * FROM links WHERE row_key='jordan-bravo'")[0]
        self.assertEqual((link["parent_id"], link["kind"], link["linkedin_url"]), (family, "pub", JORDAN_URL))
        self.assertEqual(
            (link["decision_action"], link["decision_approved"], link["decision_source"]),
            ("verify", "yes", "deep-context-review"),
        )
        # The retired message-linkedin key carries a click the seed cannot place: counted, not lost.
        self.assertEqual((manifest.identity_carried, manifest.identity_unmatched), (1, 1))
        self.assertEqual(manifest.machine_review_rows_not_carried, 1)

        # Each contact keeps its own bundle after the family merges.
        self.assertEqual(
            (manifest.bundles_carried, manifest.bundles_duplicate_dropped, manifest.bundles_unmatched),
            (2, 0, 0),
        )
        bundle = db.query(
            "SELECT person_id, payload_json, path FROM artifacts WHERE kind='source_bundle' AND parent_id=?",
            (family,),
        )
        self.assertEqual({row["person_id"] for row in bundle}, {"person-jordan", "candidate:email:casey@example.com"})
        for row in bundle:
            self.assertEqual(json.loads(row["payload_json"])["person_id"], row["person_id"])
            self.assertEqual(Path(row["path"]), (self.deep_context / f'raw/{row["person_id"]}.json').resolve())
        self.assertEqual(manifest.synthetic_rows_not_carried, 1)

        morgan = parent_of["person-morgan"]
        slug = next(row.display_slug for row in parents if row.parent_id == morgan)
        research = db.query("SELECT * FROM research WHERE parent_id=?", (morgan,))
        self.assertEqual([(row["handle"], row["status"]) for row in research], [(slug, "complete")])
        artifact = db.query("SELECT * FROM artifacts WHERE artifact_key=?", (f"research:{slug}",))[0]
        self.assertEqual(artifact["input_fingerprint"], LEGACY_PARALLEL_HANDLE_RESULT)
        self.assertTrue(Path(artifact["path"]).is_file())
        self.assertEqual(
            Path(artifact["path"]),
            (self.deep_context / f"reconcile/deep-research/{slug}/00_parallel_result.json").resolve(),
        )
        self.assertEqual((manifest.research_carried, manifest.research_unmatched), (1, 0))
        self.assertIsNotNone(carried_over_at(db))
        self.assertEqual(db.query("SELECT value FROM meta WHERE key=?", (SEEDED_AT_KEY,))[0]["value"], manifest.seeded_at)

    def test_seed_associates_research_and_profile_with_current_candidate(self) -> None:
        cache = self.legacy / "network-import/profile_cache_v2"
        cache.mkdir(parents=True)
        (cache / "morgan-delta.json").write_text(json.dumps({
            "public_identifier": "morgan-delta",
            "linkedin_url": "https://www.linkedin.com/in/morgan-delta",
            "raw_response": {"name": "Morgan Delta"},
            "normalized_profile": {
                "success": True, "public_identifier": "morgan-delta",
                "experiences": [{"title": "Engineer", "company_name": "Example"}],
            },
        }))
        db = self.cold_store()
        manifest = self.seed(db)
        research = db.query("SELECT candidate_key FROM research WHERE status='complete'")[0]
        self.assertTrue(research["candidate_key"])
        profile = db.query("SELECT candidate_key FROM artifacts WHERE kind='profile'")[0]
        self.assertEqual(profile["candidate_key"], research["candidate_key"])
        self.assertEqual(manifest.profiles_carried, 1)

    def test_seed_does_not_restore_harmonic_profiles_after_migration(self) -> None:
        cache = self.legacy / "network-import/profile_cache_v2"
        cache.mkdir(parents=True)
        path = cache / "morgan-delta.json"
        path.write_text(json.dumps({
            "source": {"provider": "existing_export_bootstrap",
                       "source_file": "/old/harmonic_enriched_example.csv"},
            "public_identifier": "morgan-delta",
            "raw_response": {"name": "Morgan Delta"},
            "normalized_profile": {"success": True, "public_identifier": "morgan-delta",
                                   "experiences": [{"title": "Engineer", "company_name": "Example"}]},
        }))
        db = self.cold_store()
        self.assertEqual(db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')
        manifest = self.seed(db)
        self.assertEqual(manifest.profiles_carried, 0)
        self.assertEqual(db.query("SELECT * FROM artifacts WHERE kind='profile'"), [])
        self.assertEqual(manifest.research_carried, 1)
        self.assertTrue(path.is_file())

    def test_human_retarget_settles_research_candidate_before_judging(self) -> None:
        research = self.legacy / "deep-context/reconcile/deep-research/morgan-delta-parent/00_parallel_result.json"
        payload = json.loads(research.read_text())
        payload["content"]["linkedin_url"] = "https://www.linkedin.com/in/morgan-new"
        research.write_text(json.dumps(payload))
        review = self.legacy / "network-import/overrides/review.csv"
        with review.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS).writerow({
                "public_identifier": "candidate:email:morgan@example.com",
                "person_id": "person-morgan", "action": "retarget", "approved": "yes",
                "new_linkedin_url": "https://www.linkedin.com/in/morgan-new",
                "source": "deep-context-review", "updated_at": "2026-09-06T00:00:00Z",
            })
        db = self.cold_store()
        self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "person-morgan")
        links = db.query("SELECT row_key, decision_approved FROM links WHERE parent_id=? AND kind!='synthetic'", (parent_id,))
        self.assertTrue(all(row["decision_approved"] in ("yes", "no") for row in links))
        from packs.ingestion.primitives.deep_context.db.identity_views import judge_candidates
        self.assertFalse(any(row.parent_id == parent_id for row in judge_candidates(db)))

    def test_seeded_no_match_still_needs_synthetic_assembly(self) -> None:
        from packs.ingestion.primitives.deep_context.db.identity_views import unassembled_research
        from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
        from packs.ingestion.primitives.deep_context.enrich.synthetic.assemble import AssembleSyntheticProfile

        research = self.legacy / "deep-context/reconcile/deep-research/morgan-delta-parent/00_parallel_result.json"
        payload = json.loads(research.read_text())
        payload["content"]["linkedin_url"] = ""
        payload["content"]["location_city"] = "Oakland"
        research.write_text(json.dumps(payload))
        (self.legacy / "deep-context/facts/person-morgan.jsonl").write_text(
            _facts_record("Morgan Delta", "2026-09-02T00:00:00+00:00")
        )
        db = self.cold_store()
        self.seed(db)
        self.assertTrue(unassembled_research(db))
        self.assertEqual(workflow_state(db).next_action, "enrich")
        AssembleSyntheticProfile(db=db).run()
        self.assertFalse(unassembled_research(db))

    def test_seed_reuses_link_for_equivalent_research_url(self) -> None:
        research = self.legacy / "deep-context/reconcile/deep-research/morgan-delta-parent/00_parallel_result.json"
        payload = json.loads(research.read_text())
        payload["content"]["linkedin_url"] = "https://linkedin.com/in/morgan-delta/"
        research.write_text(json.dumps(payload))
        people = list(CsvIO.read_dict_rows(self.people_csv))
        next(row for row in people if row["id"] == "person-morgan")["linkedin_url"] = \
            "https://www.linkedin.com/in/morgan-delta"
        CsvIO.write_dict_rows(self.people_csv, list(people[0]), people)
        db = self.cold_store()
        self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "person-morgan")
        links = db.query("SELECT row_key FROM links WHERE parent_id=? AND kind!='synthetic'", (parent_id,))
        self.assertEqual(len(links), 1)

    def test_retired_alias_uses_observed_url_with_trailing_slash(self) -> None:
        review = self.legacy / "network-import/overrides/review.csv"
        with review.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS).writerow({
                "public_identifier": "message-linkedin:jordan-bravo", "person_id": "person-jordan",
                "linkedin_url": "https://linkedin.com/in/jordan-bravo/", "action": "detach",
                "approved": "yes", "source": "deep-context-review",
                "updated_at": "2026-09-06T00:00:00Z",
            })
        db = self.cold_store()
        manifest = self.seed(db)
        self.assertEqual(db.query("SELECT decision_action FROM links WHERE row_key='jordan-bravo'")[0][0], "detach")
        self.assertGreaterEqual(manifest.identity_carried, 2)

    def test_a_second_seed_is_refused_and_an_empty_store_is_refused(self) -> None:
        empty = Db(self.db_path)
        with self.assertRaises(SeedRefused):
            self.seed(empty)
        self.db_path.unlink()

        db = self.cold_store()
        self.seed(db)
        with self.assertRaises(SeedRefused):
            self.seed(db)

    def test_check_routes_legacy_installs_to_ensure_parents_then_seed_then_owner(self) -> None:
        # The legacy tree is the install's own .powerpacks here: copy it beside the store.
        (self.state / "network-import/overrides").mkdir(parents=True)
        (self.state / "network-import/overrides/review.csv").write_bytes(
            (self.legacy / "network-import/overrides/review.csv").read_bytes()
        )
        missing = self.readiness()
        self.assertEqual(missing.checks.canonical_sqlite.status, "missing")
        self.assertEqual(missing.next_command, ENSURE_PARENTS_COMMAND)

        db = self.cold_store()
        unseeded = self.readiness(db)
        self.assertEqual(unseeded.checks.canonical_sqlite.status, "seed_required")
        self.assertEqual(unseeded.next_command, SEED_COMMAND)
        self.assertFalse(unseeded.ready)

        self.seed(db)
        seeded = self.readiness(db)
        self.assertEqual(seeded.checks.canonical_sqlite.status, "ok")
        self.assertEqual(seeded.next_command, OWNER_COMMAND)

    def test_check_treats_a_legacy_migrated_store_as_carried_over(self) -> None:
        (self.state / "network-import/overrides").mkdir(parents=True)
        (self.state / "network-import/overrides/review.csv").write_bytes(
            (self.legacy / "network-import/overrides/review.csv").read_bytes()
        )
        db = self.cold_store()
        with db.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_imported_at', '2026-09-25T00:00:00Z')")

        self.assertEqual(self.readiness(db).next_command, OWNER_COMMAND)


if __name__ == "__main__":
    unittest.main()
