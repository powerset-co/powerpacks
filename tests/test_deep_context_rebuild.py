"""Fresh source rebuild carries precisely scoped human decisions only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection, CandidatePersonRow, GuidanceRow, LinkRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    project_imported_people, read_imported_people,
)
from packs.ingestion.primitives.deep_context.migration.rebuild import Rebuild
from packs.shared.csv_io import CsvIO
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from scripts import audit_deep_context_sqlite


JORDAN = "candidate:email:jordan@example.com"
CASEY = "candidate:email:casey@example.com"
URL = "https://www.linkedin.com/in/jordan-bravo"
AT = "2026-09-01T00:00:00+00:00"


class RebuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original = self.root / "original"
        self.backup = self.root / "backup"
        self.state = self.root / "fresh"
        self.people = self.state / "network-import/merged/people.csv"
        self.source = self.state / "network-import/import/gmail/people.csv"
        self.rows = [
            {"id": JORDAN, "full_name": "Jordan Bravo", "primary_email": "jordan@example.com", "source_channels": "gmail_msgvault"},
            {"id": CASEY, "full_name": "Casey Delta", "primary_email": "casey@example.com", "source_channels": "gmail_msgvault"},
        ]
        self.write_sources()
        self.old = Db(self.original / "deep-context/deep-context.sqlite")
        project_imported_people(self.old, read_imported_people(self.source))
        self.parent = {row.person_id: row.parent_id for row in queries.people(self.old)}
        self.owner = self.root / "owner.json"
        self.owner.write_text(json.dumps({"name": "Riley Owner", "emails": ["riley@example.com"], "work": [], "education": []}))
        for name in ("facts/old.jsonl", "raw/old.json", "reconcile/deep-research/old.json", "index.json"):
            path = self.original / "deep-context" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old machine payload")
        directory = self.original / "network-import/directory.csv"
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.write_text("old aliases")

    def write_sources(self):
        for path in (self.source, self.people):
            CsvIO.write_dict_rows(path, tuple(self.rows[0]), self.rows)
        self.people.with_name("manifest.json").write_text(json.dumps({
            "stage": "merge_people", "input": {"people_csvs": [str(self.source)]},
            "stats": {"input_rows": {str(self.source): len(self.rows)}, "input_rows_total": len(self.rows),
                      "rows": len(self.rows), "linkedin_ids": 0, "candidate_ids": len(self.rows),
                      "dropped_unkeyable": 0, "groups_by_size": {"1": len(self.rows)}},
        }))

    def rebuild(self):
        return Rebuild(original_state_root=self.original, backup_root=self.backup,
                       state_root=self.state, people_csv=self.people, owner_profile=self.owner).run()

    def link(self, key="jordan-bravo", members=(JORDAN,), url=URL):
        parent = self.parent[members[0]]
        self.old.project_rows((
            LinkRow(key, parent, "jordan-bravo", "pub", url, source="deep-context-reconcile"),
            CandidatePeopleProjection(key, tuple(CandidatePersonRow(key, person, parent) for person in members)),
        ))
        return key

    def test_fresh_rebuild_keeps_human_fields_and_owner_without_machine_state(self):
        key = self.link()
        self.old.decide_worth(self.parent[JORDAN], "yes", source="user-guidance", note="Known colleague", decided_at=AT)
        self.old.decide_identity(key, "verify", source="deep-context-review", note="Reviewed contact", decided_at=AT)
        original_bytes = self.old.db_path.read_bytes()
        result = self.rebuild()
        self.assertEqual(result.applied, 2)
        self.assertEqual((result.held, result.unmatched), (0, 0))
        fresh = Db(self.state / "deep-context/deep-context.sqlite")
        self.assertEqual(len(queries.parents(fresh)), 2)
        worth = next(row for row in queries.parents(fresh) if row.human_worth)
        self.assertEqual((worth.human_worth_note, worth.human_worth_source, worth.human_worth_at), ("Known colleague", "user-guidance", AT))
        decision = fresh.query("SELECT * FROM links WHERE decision_action IS NOT NULL AND decision_source!='sibling-settle'")[0]
        self.assertEqual((decision["decision_note"], decision["decision_source"], decision["decided_at"], decision["linkedin_url"]), ("Reviewed contact", "deep-context-review", AT, URL))
        self.assertEqual(queries.owner_profile(fresh).name, "Riley Owner")
        for table in ("facts", "research", "artifacts", "merge_verdicts"):
            self.assertEqual(fresh.query(f"SELECT count(*) n FROM {table}")[0]["n"], 0)
        for path in ("facts", "raw", "reconcile/deep-research"):
            self.assertEqual(list((self.state / "deep-context" / path).iterdir()), [])
        self.assertFalse((self.state / "network-import/directory.csv").exists())
        self.assertTrue((self.backup / "deep-context/facts/old.jsonl").is_file())
        self.assertEqual(self.old.db_path.read_bytes(), original_bytes)

    def test_merged_parent_worth_and_multi_contact_profile_scope_are_held(self):
        self.old.merge_parents(self.parent[JORDAN], self.parent[CASEY])
        self.parent[CASEY] = self.parent[JORDAN]
        key = self.link(members=(JORDAN, CASEY))
        self.old.decide_worth(self.parent[JORDAN], "yes", decided_at=AT)
        self.old.decide_identity(key, "verify", decided_at=AT)
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 2))
        self.assertTrue(all(item.person_ids == (CASEY, JORDAN) for item in result.decisions))

    def test_generated_sibling_decision_is_never_carried(self):
        self.old.decide_identity(self.link(), "verify", decided_at=AT)
        result = self.rebuild()
        self.assertEqual(result.applied, 1)
        self.assertEqual(result.excluded_generated, 1)

    def test_seed_relabelled_unknown_source_is_held_even_with_exact_scope(self):
        self.old.decide_identity(self.link(), "verify", decided_at=AT)
        with self.old.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (AT,))
        review = self.original / "network-import/overrides/review.csv"
        CsvIO.write_dict_rows(review, ("public_identifier", "person_id", "action", "approved", "source", "updated_at", "linkedin_url"), [
            {"public_identifier": "jordan-bravo", "person_id": JORDAN, "action": "verify", "approved": "yes", "source": "legacy-sibling-settle", "updated_at": AT, "linkedin_url": URL},
        ])
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))
        self.assertIn("provenance", result.decisions[0].reason)
    def test_migration_reads_only_named_original_review_and_fresh_inputs(self):
        package = audit_deep_context_sqlite.PACKAGE
        fixtures = (
            ("migration/human_decisions.py", """from packs.shared.csv_io import CsvIO
class HumanSnapshot:
    def read(self, review_csv):
        return CsvIO.read_dict_rows(review_csv)
"""),
            ("migration/rebuild.py", """from packs.shared.csv_io import CsvIO
class Rebuild:
    def _validate(self):
        self.owner_profile.read_text()
        manifest_path.read_text()
        return CsvIO.read_dict_rows(path)
"""),
        )
        for relative, source in fixtures:
            with self.subTest(relative=relative):
                self.assertEqual(audit_deep_context_sqlite.audit_source(package / relative, source), [])
                changed = source.replace("review_csv)", "facts_path)").replace("self.owner_profile.read_text()", "self.facts.read_text()")
                self.assertTrue(audit_deep_context_sqlite.audit_source(package / relative, changed))

    def test_seeded_decision_requires_matching_original_target_and_scope(self):
        self.old.decide_identity(self.link(), "verify", decided_at=AT)
        with self.old.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (AT,))
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_changed_source_name_holds_old_human_instruction(self):
        self.old.decide_worth(self.parent[JORDAN], "no", decided_at=AT)
        self.rows[0]["full_name"] = "Taylor Foxtrot"
        self.write_sources()
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))
        self.assertIn("source", result.decisions[0].reason)

    def test_missing_original_contact_is_unmatched(self):
        self.old.decide_worth(self.parent[JORDAN], "yes", decided_at=AT)
        self.rows = self.rows[1:]
        self.write_sources()
        result = self.rebuild()
        self.assertEqual((result.applied, result.unmatched), (0, 1))

    def test_existing_derived_state_refuses_before_backup_or_writes(self):
        old_facts = self.state / "deep-context/facts/old.jsonl"
        old_facts.parent.mkdir(parents=True)
        old_facts.write_text("machine")
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())
        self.assertEqual(old_facts.read_text(), "machine")

    def test_missing_source_manifest_refuses_instead_of_aggregate_fallback(self):
        self.people.with_name("manifest.json").unlink()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())

    def test_original_and_destination_overlap_refuses(self):
        with self.assertRaises(StoreError):
            Rebuild(original_state_root=self.original, backup_root=self.original / "backup",
                    state_root=self.state, people_csv=self.people, owner_profile=self.owner).run()

    def test_completed_prepare_refuses_rerun_without_mutation(self):
        self.rebuild()
        manifest = self.state / "deep-context/rebuild/manifest.json"
        before = manifest.read_bytes()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertEqual(manifest.read_bytes(), before)

    def test_cli_runs_with_explicit_copy_paths_without_provider(self):
        result = subprocess.run([sys.executable, "-m", "packs.ingestion.primitives.deep_context.migration.rebuild",
                                 "--original-state-root", str(self.original), "--backup-root", str(self.backup),
                                 "--state-root", str(self.state), "--people-csv", str(self.people),
                                 "--owner-profile", str(self.owner)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")

    def test_backup_preserves_self_symlink_without_following_it(self):
        (self.original / ".powerpacks").symlink_to(self.original, target_is_directory=True)
        historical = self.original / "deep-context/deep-context.sqlite.bkup-human-reset"
        historical.write_text("historical decisions")
        self.rebuild()
        self.assertTrue((self.backup / ".powerpacks").is_symlink())
        self.assertEqual((self.backup / "deep-context" / historical.name).read_text(), "historical decisions")

    def test_invalid_owner_and_no_eligible_sources_refuse_without_writes(self):
        self.owner.write_text(json.dumps({"name": "Riley Owner", "emails": 42}))
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.state / "deep-context").exists())
        self.owner.write_text(json.dumps({"name": "Riley Owner"}))
        self.rows = [{"id": "candidate:email:billing@example.com", "full_name": "Billing", "primary_email": "billing@example.com", "source_channels": "gmail_msgvault"}]
        self.write_sources()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.state / "deep-context").exists())

    def test_mixed_channel_row_cannot_hide_old_profile_association(self):
        self.rows[0].update(source_channels="gmail_msgvault,linkedin_csv", public_identifier="taylor-foxtrot", linkedin_url="https://www.linkedin.com/in/taylor-foxtrot")
        self.write_sources()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())

    def test_missing_fan_in_csv_refuses_before_writes(self):
        self.people.unlink()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())

    def test_changed_source_endpoint_cannot_reuse_old_worth(self):
        self.old.decide_worth(self.parent[JORDAN], "yes", decided_at=AT)
        self.rows[0]["primary_email"] = "taylor@example.com"
        self.write_sources()
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_verified_decision_without_original_target_is_held(self):
        self.old.decide_identity(JORDAN, "verify", decided_at=AT)
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_retarget_preserves_original_and_replacement_targets(self):
        self.old.decide_identity(self.link(), "retarget", replacement_url="https://www.linkedin.com/in/jordan-new",
                                 replacement_public_identifier="jordan-new", note="Corrected profile", decided_at=AT)
        with patch("packs.ingestion.primitives.deep_context.shared.build_owner.hydrate_profiles", side_effect=AssertionError("provider forbidden")):
            result = self.rebuild()
        self.assertEqual(result.applied, 1)
        row = Db(self.state / "deep-context/deep-context.sqlite").query("SELECT * FROM links WHERE row_key='jordan-bravo'")[0]
        self.assertEqual((row["linkedin_url"], row["replacement_url"], row["decision_note"], row["decided_at"]),
                         (URL, "https://www.linkedin.com/in/jordan-new", "Corrected profile", AT))

    def test_matching_original_legacy_human_row_is_carried(self):
        self.old.decide_identity(self.link(), "verify", decided_at=AT)
        with self.old.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (AT,))
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "person_id", "action", "approved", "source", "updated_at", "linkedin_url"),
                             [{"public_identifier": "jordan-bravo", "person_id": JORDAN, "action": "verify", "approved": "yes",
                               "source": "deep-context-review", "updated_at": AT, "linkedin_url": URL}])
        result = self.rebuild()
        self.assertEqual(result.applied, 1)
        row = Db(self.state / "deep-context/deep-context.sqlite").query("SELECT decision_source FROM links WHERE row_key='jordan-bravo'")[0]
        self.assertEqual(row["decision_source"], "deep-context-review")

    def test_normalization_and_ensure_cannot_reload_archived_machine_facts(self):
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        original_history = self.original / "deep-context/facts" / f"{JORDAN}.jsonl"
        payload = json.dumps({"facts": {"canonical_name": "Taylor Foxtrot", "confidence": 0.7},
                              "chunk_index": 0, "updated_at": AT}) + "\n"
        original_history.write_text(payload)
        self.rebuild()
        fresh = Db(self.state / "deep-context/deep-context.sqlite")
        EnsureParents(db=fresh, people_csv=self.people).execute()
        normalize_parent_cache(fresh, raw_dir=self.state / "deep-context/raw", facts_dir=self.state / "deep-context/facts")
        for table in ("facts", "research", "merge_verdicts"):
            self.assertEqual(fresh.query(f"SELECT count(*) n FROM {table}")[0]["n"], 0)
        self.assertEqual(fresh.query("PRAGMA foreign_key_check"), [])
        self.assertEqual((self.original / "deep-context/facts/old.jsonl").read_text(), "old machine payload")
        self.assertEqual(original_history.read_text(), payload)

    def test_singleton_legacy_worth_view_scope_does_not_prove_original_click(self):
        self.old.decide_worth(self.parent[JORDAN], "yes", decided_at=AT)
        with self.old.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (AT,))
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "worth_person_ids", "network_worth", "source", "updated_at"),
                             [{"public_identifier": "parent-worth:old-parent", "worth_person_ids": JORDAN,
                               "network_worth": "yes", "source": "deep-context-parent-worth", "updated_at": AT}])
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))
        self.assertIn("provenance", result.decisions[0].reason)

    def test_source_missing_and_row_count_change_refuse_before_backup(self):
        self.rows.append({"id": "candidate:email:taylor@example.com", "full_name": "Taylor Foxtrot", "primary_email": "taylor@example.com", "source_channels": "gmail_msgvault"})
        CsvIO.write_dict_rows(self.source, tuple(self.rows[0]), self.rows)
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())
        self.source.unlink()
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())

    def test_csv_only_current_human_instruction_survives_without_sqlite_human_row(self):
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "person_id", "action", "approved", "source", "updated_at", "linkedin_url"),
                             [{"public_identifier": "jordan-bravo", "person_id": JORDAN, "action": "verify", "approved": "yes",
                               "source": "deep-context-review", "updated_at": AT, "linkedin_url": URL}])
        result = self.rebuild()
        self.assertEqual((result.applied, result.held, result.unmatched), (1, 0, 0))

    def test_csv_only_unknown_and_scope_rewritten_marks_remain_held(self):
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "person_id", "action", "approved", "source", "updated_at", "linkedin_url", "worth_person_ids", "network_worth"),
                             [{"public_identifier": "jordan-bravo", "person_id": JORDAN, "action": "verify", "approved": "yes",
                               "source": "legacy-migration", "updated_at": AT, "linkedin_url": URL},
                              {"public_identifier": "parent-worth:historical", "worth_person_ids": CASEY,
                               "source": "deep-context-parent-worth", "network_worth": "yes", "updated_at": AT}])
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 2))

    def test_csv_only_direct_worth_has_exact_contact_scope(self):
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "worth_person_ids", "network_worth", "user_worth_note", "source", "updated_at"),
                             [{"public_identifier": "parent-worth:historical", "worth_person_ids": JORDAN, "network_worth": "no",
                               "user_worth_note": "Reviewed exact contact", "source": "user-guidance", "updated_at": AT}])
        result = self.rebuild()
        self.assertEqual(result.applied, 1)

    def test_conflicting_csv_scope_does_not_overwrite_current_sqlite_decisions(self):
        self.old.decide_worth(self.parent[JORDAN], "no", decided_at=AT)
        self.old.decide_identity(self.link(), "detach", decided_at=AT)
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "worth_person_ids", "person_id", "network_worth", "action", "approved", "source", "updated_at", "linkedin_url"),
                             [{"public_identifier": "parent-worth:another-key", "worth_person_ids": JORDAN, "network_worth": "yes",
                               "source": "user-guidance", "updated_at": "2026-08-01T00:00:00+00:00"},
                              {"public_identifier": "another-profile-key", "person_id": JORDAN, "action": "verify", "approved": "yes",
                               "source": "deep-context-review", "updated_at": "2026-08-01T00:00:00+00:00", "linkedin_url": URL}])
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 4))
        self.assertTrue(all("conflicting" in item.reason for item in result.decisions))

    def test_csv_target_cannot_be_replaced_by_a_raw_machine_candidate(self):
        self.link()
        wrong_url = "https://www.linkedin.com/in/taylor-foxtrot"
        CsvIO.write_dict_rows(self.original / "network-import/overrides/review.csv",
                             ("public_identifier", "person_id", "action", "approved", "source", "updated_at", "linkedin_url"),
                             [{"public_identifier": "jordan-bravo", "person_id": JORDAN, "action": "verify", "approved": "yes",
                               "source": "deep-context-review", "updated_at": AT, "linkedin_url": wrong_url}])
        result = self.rebuild()
        self.assertEqual(result.decisions[0].target_url, wrong_url)
        self.assertEqual((result.applied, result.held), (0, 1))

    def gmail_fan_in(self, account_root=None):
        accounts = []
        for number in range(2):
            account = (account_root or self.state) / f"accounts/{number}/people.csv"
            CsvIO.write_dict_rows(account, ("primary_email", "full_name", "interaction_counts"),
                                 [{"primary_email": "jordan@example.com", "full_name": "Jordan Bravo",
                                   "interaction_counts": '{"email": 3}'}])
            accounts.append({"account_email": f"owner{number}@example.com", "people_csv": str(account)})
        discovery = self.state / "gmail-discovery.json"
        discovery.write_text(json.dumps({"children": accounts}))
        gmail = GmailImport(manifest_json=discovery, import_dir=self.state / "network-import/import")
        gmail.run()
        self.assertEqual(len(CsvIO.read_dict_rows(gmail.people_csv)), 1)
        merge = PeopleMerge(inputs=[gmail.people_csv], output_dir=self.people.parent)
        result = merge.run()
        self.assertEqual(result.stats.input_rows[str(gmail.people_csv)], 2)
        self.assertEqual(result.stats.rows, 1)

    def test_gmail_account_observations_are_counted_identically_by_fan_in_and_rebuild(self):
        self.gmail_fan_in()
        result = self.rebuild()
        self.assertEqual(result.contacts, 1)
        fresh = Db(self.state / "deep-context/deep-context.sqlite")
        self.assertEqual(queries.imported_people(fresh)[0].full_name, "Jordan Bravo")
        self.assertEqual(json.loads(queries.imported_people(fresh)[0].interaction_counts), {"email": 3})

    def test_gmail_nested_original_account_input_refuses_before_backup(self):
        self.gmail_fan_in(self.original)
        with self.assertRaises(StoreError):
            self.rebuild()
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.state / "deep-context").exists())

    def saved_guidance(self, text, target="https://www.linkedin.com/in/jordan-new"):
        key = self.link()
        self.old.decide_identity(key, "retarget", replacement_url=target,
                                 replacement_public_identifier="jordan-new", source="user-guidance", note=text, decided_at=AT)
        request = {"slug": "jordan-bravo", "row_key": key, "name": "Jordan Bravo", "guidance": text,
                   "person_ids": [JORDAN], "linkedin_url": URL, "submitted_at": AT}
        self.old.project_rows((GuidanceRow(self.parent[JORDAN], self.parent[JORDAN], text, "applied", key,
                                          AT, target, json.dumps({"request": request, "new_url": target, "updated_at": AT})),))

    def test_free_text_guidance_does_not_prove_human_chose_provider_retarget(self):
        self.saved_guidance("Find their engineering profile")
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))
        self.assertIn("provenance", result.decisions[0].reason)

    def test_explicit_submitted_profile_url_is_human_retarget_proof(self):
        self.saved_guidance("https://www.linkedin.com/in/jordan-new")
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (1, 0))

    def test_url_mentioned_in_arbitrary_guidance_is_not_exact_human_choice(self):
        self.saved_guidance("Find someone different from https://www.linkedin.com/in/jordan-new")
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_user_guidance_retarget_without_saved_explicit_input_is_held(self):
        self.old.decide_identity(self.link(), "retarget", replacement_url="https://www.linkedin.com/in/jordan-new",
                                 replacement_public_identifier="jordan-new", source="user-guidance", decided_at=AT)
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_exact_profile_url_note_preserves_explicit_human_retarget(self):
        target = "https://www.linkedin.com/in/jordan-new"
        self.old.decide_identity(self.link(), "retarget", replacement_url=target,
                                 replacement_public_identifier="jordan-new", source="user-guidance", note=target, decided_at=AT)
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (1, 0))

    def test_guided_provider_failure_does_not_prove_human_detach(self):
        key = self.link()
        self.old.decide_identity(key, "detach", source="user-guidance", note="Find their engineering profile", decided_at=AT)
        self.old.project_rows((GuidanceRow(self.parent[JORDAN], self.parent[JORDAN], "Find their engineering profile",
                                          "failed", key, AT),))
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))

    def test_explicit_guidance_target_must_match_applied_target(self):
        self.saved_guidance("https://www.linkedin.com/in/someone-else")
        result = self.rebuild()
        self.assertEqual((result.applied, result.held), (0, 1))


if __name__ == "__main__":
    unittest.main()
