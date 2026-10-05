"""Unresolved source identities retain evidence without publishing human dossiers."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    LinkRow,
    OwnerContextRow,
    ParentRow,
    PersonRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_lookup
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.shared.lookup_person import PersonLookup, main as lookup_main
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.pipeline.contract import PeopleRow


REASON = ("Source identity needs review; original contact names are missing or disagree "
          "with each other or retained facts.")


class DossierQuarantineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.dossiers = self.root / "dossiers"
        self.dossiers.mkdir()
        self.compose = ComposeDossier(
            db=self.db, dossier_dir=self.dossiers, index_md=self.root / "index.md",
        )
        facts = {"canonical_name": "Jordan Bravo", "title": "Engineer", "confidence": 0.9,
                 "notable_events": [{"date": "2025-02-12", "summary": "Answered the support request."}]}
        bundle = {"person_id": "parent-jordan", "full_name": "Jordan Bravo",
                  "emails": ["support@example.com"], "phones": [],
                  "source_channels": ["gmail_msgvault"], "messages": []}
        self.fact_path = self.root / "facts.jsonl"
        self.raw_path = self.root / "raw.json"
        self.fact_path.write_text(json.dumps({"facts": facts}) + "\n")
        self.raw_path.write_text(json.dumps(bundle))
        self.db.project_rows((
            ParentRow("parent-jordan", "parent-worth:parent-jordan", "Jordan Bravo", "jordan"),
            PersonRow("contact-jordan", "parent-jordan", "jordan-child", "Jordan Bravo"),
            PersonIdentifiersProjection("contact-jordan", (
                PersonIdentifierRow("contact-jordan", "email", "support@example.com"),
            )),
            OwnerContextRow("owner", json.dumps({"name": "Casey Owner"}), str(self.root / "owner.json"), "owner"),
            ArtifactRow("facts:parent-jordan", "facts", "parent-jordan", str(self.fact_path), "facts", "projected",
                        payload_json=json.dumps({"facts": facts})),
            FactRow("parent-jordan", "parent-jordan", "facts:parent-jordan", facts_json=json.dumps(facts)),
            ArtifactRow("source-bundle:parent-jordan", "source_bundle", "parent-jordan", str(self.raw_path), "raw", "projected",
                        payload_json=json.dumps(bundle)),
        ))
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Jordan Bravo"),))
        self.assertEqual(self.compose.execute().dossiers_written, 1)
        self.parent_path = self.dossiers / "jordan-parent.md"
        parent_body = "# Jordan Bravo\n\nPreviously rendered parent facts.\n"
        self.parent_path.write_text(parent_body)
        self.db.project_rows((ArtifactRow(
            "dossier-parent:parent-jordan", "dossier", "parent-jordan", str(self.parent_path),
            hashlib.sha256(parent_body.encode()).hexdigest(), "projected",
            payload_json=json.dumps({"name": "Jordan Bravo", "body": parent_body}),
        ),))

    def _assert_quarantined(self) -> None:
        evidence_rows = [dict(row) for row in self.db.query("SELECT * FROM artifacts WHERE kind!='dossier'")]
        fact_rows = [dict(row) for row in self.db.query("SELECT * FROM facts")]
        dossier_rows = {row.artifact_key: row for row in artifacts(self.db, kind="dossier")}
        files = {path: path.read_bytes() for path in (self.fact_path, self.raw_path, self.parent_path, self.dossiers / "jordan.md")}

        result = self.compose.execute()

        self.assertEqual((result.dossiers_written, result.skipped, result.orphans_removed), (0, 1, 0))
        self.assertEqual(result.skip_reasons[0].reason, REASON)
        failed = artifacts(self.db, kind="dossier")
        self.assertEqual(len(failed), 2)
        for row in failed:
            self.assertEqual((row.status, row.error), ("failed", REASON))
            prior = dossier_rows[row.artifact_key]
            self.assertEqual((row.path, row.payload_json, row.content_fingerprint),
                             (prior.path, prior.payload_json, prior.content_fingerprint))
        self.assertEqual(evidence_rows, [dict(row) for row in self.db.query("SELECT * FROM artifacts WHERE kind!='dossier'")])
        self.assertEqual(fact_rows, [dict(row) for row in self.db.query("SELECT * FROM facts")])
        for path, body in files.items():
            self.assertEqual(path.read_bytes(), body)
        for query in ({"name": "Jordan Bravo"}, {"email": "support@example.com"}):
            match, = person_lookup(self.db.db_path, **query)
            self.assertEqual((match.parent_id, match.dossier_body, match.dossier_path),
                             ("parent-jordan", "", ""))
            lookup = PersonLookup(db=self.db.db_path, **query).run()
            self.assertEqual(lookup.status, "found")
            self.assertEqual(lookup.matches, (match,))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = lookup_main(["--db", str(self.db.db_path), "--email", "support@example.com"])
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()),
                         (0, "Jordan Bravo [parent-jordan]: No saved dossier is available.\n", ""))
        self.assertNotIn("Jordan Bravo", (self.root / "index.md").read_text())

    def test_missing_original_roster_quarantines_both_cached_dossiers(self) -> None:
        self.db.replace_imported_people(())
        self._assert_quarantined()

    def test_blank_original_name_quarantines_and_corrected_source_renders_again(self) -> None:
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name=""),))
        self._assert_quarantined()

        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Jordan Bravo"),))
        result = self.compose.execute()

        self.assertEqual((result.dossiers_written, result.skipped), (1, 0))
        lookup = PersonLookup(db=self.db.db_path, name="Jordan Bravo").run()
        self.assertEqual(lookup.status, "found")
        self.assertTrue(lookup.matches[0].dossier_body)
        rows = {row.artifact_key: row for row in artifacts(self.db, kind="dossier")}
        self.assertEqual(rows["dossier:parent-jordan"].status, "projected")
        self.assertEqual(rows["dossier-parent:parent-jordan"].status, "failed")

    def test_incompatible_original_names_quarantine_even_with_human_worth_yes(self) -> None:
        self.db.project_rows((PersonRow("contact-casey", "parent-jordan", "casey-child", "Casey Delta"),))
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Jordan Bravo"),
                                         PeopleRow(id="contact-casey", full_name="Casey Delta")))
        self.db.decide_worth("parent-jordan", "yes")
        self._assert_quarantined()

    def test_profile_verification_does_not_assign_missing_source_identity(self) -> None:
        self.db.project_rows((LinkRow(
            "support-profile", "parent-jordan", "jordan-bravo", "candidate_email",
            linkedin_url="https://www.linkedin.com/in/jordan-bravo/",
            source="deep-context-review",
        ),))
        self.db.decide_identity("support-profile", "verify")
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name=""),))
        before = dict(self.db.query("SELECT * FROM links WHERE row_key='support-profile'")[0])
        self._assert_quarantined()
        self.assertEqual(dict(self.db.query("SELECT * FROM links WHERE row_key='support-profile'")[0]), before)

    def test_source_and_fact_policy_scopes_identity_resolution_to_unresolved_parents(self) -> None:
        from packs.ingestion.primitives.deep_context.shared.dossier_policy import unresolved_source_parents

        self.db.project_rows((ParentRow("parent-casey", "parent-worth:parent-casey"),
                              PersonRow("contact-casey", "parent-casey", display_name="Casey Delta"),
                              PersonRow("owner-contact", "parent-jordan", is_owner=True),
                              PersonRow("ghost-contact", "parent-jordan", is_ghost=True)))
        with mock.patch.object(self.db, "query", wraps=self.db.query) as query:
            result = unresolved_source_parents(self.db)
        self.assertEqual(result, {"parent-casey"})
        self.assertEqual(query.call_count, 6)
        for call in query.call_args_list[3:]:
            self.assertEqual(json.loads(call.args[1][0]), ["parent-casey"])

    def test_readonly_audit_uses_the_same_source_and_fact_policy(self) -> None:
        from packs.ingestion.primitives.deep_context.shared.dossier_policy import unresolved_source_parent_ids

        sources = {"compatible": ("Jordan Bravo",), "conflicting": ("Jordan Bravo",),
                   "missing": ("",), "empty": ("Casey Delta",)}
        retained = tuple(FactRow(parent_id, parent_id, f"facts:{parent_id}",
                                 facts_json=json.dumps({"canonical_name": name}))
                         for parent_id, name in (("compatible", "Jordan A. Bravo"),
                                                 ("conflicting", "Casey Delta"),
                                                 ("missing", "Jordan Bravo"), ("empty", "")))
        self.assertEqual(unresolved_source_parent_ids(sources, retained), {"conflicting", "missing"})
        self.assertEqual(sources["conflicting"], ("Jordan Bravo",))

    def _assert_build_quarantined(self, build: BuildParents) -> None:
        files = {Path(row.path): Path(row.path).read_bytes()
                 for row in artifacts(self.db, kind="dossier")}
        files.update({path: path.read_bytes() for path in (self.fact_path, self.raw_path)})
        evidence = [dict(row) for row in self.db.query("SELECT * FROM artifacts WHERE kind!='dossier'")]
        facts = [dict(row) for row in self.db.query("SELECT * FROM facts")]
        original_dossiers = {row.artifact_key: row for row in artifacts(self.db, kind="dossier")}

        result = build.execute()

        self.assertEqual((result.parents_changed, result.orphans_removed), (0, 0))
        rows = artifacts(self.db, kind="dossier")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row.status, row.error), ("failed", REASON))
            self.assertEqual(row.payload_json, original_dossiers[row.artifact_key].payload_json)
        for path, body in files.items():
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual(evidence, [dict(row) for row in self.db.query("SELECT * FROM artifacts WHERE kind!='dossier'")])
        self.assertEqual(facts, [dict(row) for row in self.db.query("SELECT * FROM facts")])
        match, = person_lookup(self.db.db_path, email="support@example.com")
        self.assertEqual((match.parent_id, match.dossier_body, match.dossier_path),
                         ("parent-jordan", "", ""))

    def test_build_missing_source_name_preserves_both_dossiers_and_recovers(self) -> None:
        build = BuildParents(db=self.db, parents_dir=self.root / "parents")
        build.execute()
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name=""),))
        self._assert_build_quarantined(build)

        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Jordan Bravo"),))
        result = build.execute()
        self.assertEqual(result.parents_changed, 1)
        self.compose.execute()
        self.assertEqual({row.status for row in artifacts(self.db, kind="dossier")}, {"projected"})
        lookup = PersonLookup(db=self.db.db_path, email="support@example.com").run()
        self.assertEqual(lookup.status, "found")
        self.assertTrue(lookup.matches[0].dossier_body)
        self.assertEqual(build.execute().parents_changed, 0)

    def test_build_conflicting_original_names_quarantines_both_dossiers(self) -> None:
        build = BuildParents(db=self.db, parents_dir=self.root / "parents")
        build.execute()
        self.db.project_rows((PersonRow("contact-casey", "parent-jordan", "casey-child", "Casey Delta"),))
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Jordan Bravo"),
                                         PeopleRow(id="contact-casey", full_name="Casey Delta")))
        self._assert_build_quarantined(build)

    def test_corrected_source_cannot_reopen_an_incompatible_retained_fact_identity(self) -> None:
        parents = self.root / "parents"
        parents.mkdir()
        self.parent_path = self.parent_path.rename(parents / "jordan-bravo-jordan.md")
        self.db.project_rows(tuple(replace(row, path=str(self.parent_path))
                                   for row in artifacts(self.db, kind="dossier")
                                   if row.artifact_key.startswith("dossier-parent:")))
        build = BuildParents(db=self.db, parents_dir=self.root / "parents")
        build.execute()
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name=""),))
        self._assert_build_quarantined(build)
        self.db.replace_imported_people((PeopleRow(id="contact-jordan", full_name="Casey Delta"),))
        self.db.project_rows((PersonRow("contact-jordan", "parent-jordan", "jordan-child",
                                       display_name="Casey Delta"),))

        self._assert_build_quarantined(build)
        self._assert_quarantined()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = lookup_main(["--db", str(self.db.db_path), "--name", "Casey Delta"])
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()),
                         (0, "Jordan Bravo [parent-jordan]: No saved dossier is available.\n", ""))

    def test_compatible_and_empty_canonical_names_keep_composition_available(self) -> None:
        for name in ("Jordan A. Bravo", ""):
            with self.subTest(canonical_name=name):
                self.db.project_rows((FactRow(
                    "parent-jordan", "parent-jordan", "facts:parent-jordan",
                    facts_json=json.dumps({"canonical_name": name, "title": "Engineer"}),
                ),))
                built = BuildParents(db=self.db, parents_dir=self.root / "parents").execute()
                composed = self.compose.execute()
                self.assertEqual((built.parents_changed, composed.dossiers_written, composed.skipped), (1, 1, 0))
                lookup = PersonLookup(db=self.db.db_path, email="support@example.com").run()
                self.assertEqual(lookup.status, "found")
                self.assertTrue(lookup.matches[0].dossier_body)


if __name__ == "__main__":
    unittest.main()
