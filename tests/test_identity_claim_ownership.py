"""Extracted contact details do not establish source identity or repair ownership."""

import json
import unittest

from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_merged_parents
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from tests import test_deep_context_merge_repair as repair_tests


class ExtractedIdentityClaimsTests(unittest.TestCase):
    def test_colleague_linkedin_mention_remains_descriptive(self):
        url = "https://www.linkedin.com/in/casey-delta"
        facts = SynthesizedFacts.from_payload({"canonical_name": "Jordan Bravo", "identifiers": [url]})
        evidence = DossierEvidence.from_facts(facts)
        self.assertEqual(evidence.self_linkedin_url, "")
        self.assertIn(f"Mentioned identifiers: {url}", evidence.dossier)

    def test_extracted_other_person_email_is_an_unverified_claim(self):
        facts = SynthesizedFacts.from_payload({
            "canonical_name": "Jordan Bravo", "owned_identifiers": {"emails": ["casey@example.com"]},
        })
        evidence = DossierEvidence.from_facts(facts)
        self.assertEqual(evidence.emails, ())
        self.assertIn("Unverified extracted contact details (emails): casey@example.com", evidence.dossier)
        self.assertNotIn("Owned emails", evidence.dossier)


class RepairExtractedOwnershipTests(unittest.TestCase):
    setUp = repair_tests.MergeRepairTests.setUp

    def test_model_owned_other_person_email_does_not_reattach_child(self):
        parent = self.parents["person-a"]
        payload = json.dumps({"owned_identifiers": {"emails": ["casey@example.com"]}})
        with self.db.transaction() as conn:
            conn.execute("UPDATE facts SET facts_json=? WHERE subject_key=?", (payload, parent))
            conn.execute("INSERT INTO people(person_id,parent_id,display_name) VALUES ('casey-email',?,'Casey Delta')", (parent,))
            conn.execute("INSERT INTO person_identifiers VALUES ('casey-email','email','casey@example.com','casey@example.com')")
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query("SELECT parent_id FROM people WHERE person_id='casey-email'")[0][0],
                         mint_parent_id(("casey-email",)))

    def test_model_owned_phone_alone_does_not_reattach_child(self):
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        child = "candidate:phone:+15550100100"
        self.assertEqual(self.db.query("SELECT parent_id FROM people WHERE person_id=?", (child,))[0][0],
                         mint_parent_id((child,)))
        original = self.db.query("SELECT facts_json FROM facts WHERE subject_key=?", (self.parents["person-a"],))[0][0]
        self.assertEqual(json.loads(original)["owned_identifiers"]["phones"], ["+15550100100"])

    def test_premerge_membership_wins_over_wrong_model_owned_phone(self):
        parent = self.parents["person-a"]
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES ('research:casey',?,'casey','research','deep-research')", (parent,))
            for person in ("person-b", "candidate:phone:+15550100100"):
                conn.execute("INSERT INTO candidate_people VALUES ('research:casey',?,?)", (person, parent))
            conn.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,candidate_key,path,content_fingerprint,status,projected_at) VALUES ('research:casey','research',?,'research:casey','/paid/research.json','paid-hash','projected','2026-01-01T00:00:00Z')", (parent,))
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query("SELECT parent_id FROM people WHERE person_id='candidate:phone:+15550100100'")[0][0],
                         self.parents["person-b"])


if __name__ == "__main__":
    unittest.main()
