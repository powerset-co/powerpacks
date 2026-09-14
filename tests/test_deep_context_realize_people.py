from __future__ import annotations

import json
import io
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, LinkRow, ParentRow, PersonIdentifierRow, PersonRow,
    PersonSourceRow, PersonIdentifiersProjection, PersonSourcesProjection, WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement, settle_machine_identities,
)
from packs.ingestion.primitives.deep_context.realize.people import RealizePeople, main
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, generate_person_id
from packs.shared.csv_io import CsvIO


class RealizePeopleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.csv = self.root / "merged" / "people.csv"

    def tearDown(self):
        self.temp.cleanup()

    def parent(self, name="jordan", *, worth="yes", person_id=None, email=None):
        person_id = person_id or f"candidate:email:{name}@example.com"
        self.db.project_rows((
            ParentRow(name, f"parent-worth:{name}", name.title()),
            PersonRow(person_id, name, display_name=name.title()),
            PersonIdentifiersProjection(person_id, (PersonIdentifierRow(
                person_id, "email", email or f"{name}@example.com"),)),
            PersonSourcesProjection(person_id, (PersonSourceRow(person_id, "gmail_msgvault"),)),
            ArtifactRow(f"facts:{name}", "facts", name, f"/facts/{name}.jsonl", name, "projected"),
            FactRow(name, name, f"facts:{name}", machine_worth=worth,
                    facts_json=json.dumps({"canonical_name": name.title(),
                        "relationship_to_owner": "Friend from astronomy club",
                        "topics": ["astronomy", "weekend hikes"]})),
        ))
        return person_id

    def identity(self, parent="jordan", *, slug="casey-bravo", action="detach", human=None):
        key = f"{parent}:{slug}"
        self.db.project_rows((LinkRow(key, parent, slug, "pub",
            linkedin_url=f"https://www.linkedin.com/in/{slug}/", candidate_origin=True,
            source=WriterSource.RECONCILE.value),))
        settle_machine_identities(self.db, (MachineIdentitySettlement(
            key=key, judgment_fingerprint="fixture", judgment_payload_json=None,
            machine_action=action, machine_approved="auto", machine_confidence=.9,
            machine_reason="Saved evidence", machine_judgment="needs_review",
            source=WriterSource.RECONCILE.value,
        ),))
        if human:
            self.db.decide_identity(key, human)
        return key

    def row(self, person_id, **values):
        return {"id": person_id, "full_name": "Jordan Bravo",
                "primary_email": "jordan@example.com", "source_channels": '["gmail_msgvault"]', **values}

    def run_projection(self, rows=None):
        if rows is not None:
            CsvIO.write_dict_rows(self.csv, PEOPLE_SCHEMA_COLUMNS, rows)
        result = RealizePeople(db=self.db, people_csv=self.csv).run()
        return CsvIO.read_dict_rows(self.csv), result

    def test_worth_no_removes_only_derived_row(self):
        person_id = self.parent(worth="no")
        source = self.root / "import" / "gmail_msgvault" / "people.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [self.row(person_id)])
        original = source.read_bytes()
        rows, result = self.run_projection(CsvIO.read_dict_rows(source))
        self.assertEqual(rows, [])
        self.assertEqual(result["excluded_parents"], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(self.db.query("SELECT * FROM people")), 1)
        self.assertEqual(next(self.csv.parent.glob("people.csv.*.bkup")).read_bytes(), original)

    def test_settled_unlinked_uses_facts_only_and_reruns_identically(self):
        person_id = self.parent()
        self.identity()
        rows, result = self.run_projection([self.row(person_id,
            public_identifier="casey-bravo", linkedin_url="https://www.linkedin.com/in/casey-bravo/",
            full_name="Wrong Research Person", summary="Unverified research biography",
            current_title="CEO", current_company="Invented Co", headline="Invented CEO",
            work_experiences='[{"title":"CEO"}]', rapidapi_response='{"wrong":true}')])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["full_name"], "Jordan")
        self.assertIn("astronomy", row["summary"])
        for field in ("linkedin_url", "public_identifier", "current_title", "current_company",
                      "headline", "work_experiences", "rapidapi_response"):
            self.assertEqual(row[field], "", field)
        self.assertNotIn("Unverified", row["summary"])
        self.assertEqual(result["dossier_parents"], 1)
        original = self.csv.read_bytes()
        self.run_projection()
        self.assertEqual(self.csv.read_bytes(), original)

    def test_dossier_preserves_owned_contact_metadata_and_reruns_identically(self):
        person_id = self.parent()
        self.identity()
        rows, _ = self.run_projection([
            self.row(person_id, interaction_counts='{"gmail": 12}',
                last_interaction="2023-08-21 11:30:43+00:00", source_artifacts='["gmail.csv"]'),
            self.row("merged-alias", superseded_person_ids=json.dumps([person_id]),
                interaction_counts='{"gmail": 10, "imessage": 3}',
                last_interaction="2024-01-01T10:00:00+00:00", source_artifacts='["gmail.csv", "messages.csv"]'),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["interaction_counts"]), {"gmail": 12, "imessage": 3})
        self.assertEqual(rows[0]["last_interaction"], "2024-01-01T10:00:00+00:00")
        self.assertEqual(json.loads(rows[0]["source_artifacts"]), ["gmail.csv", "messages.csv"])
        original = self.csv.read_bytes()
        self.run_projection()
        self.assertEqual(self.csv.read_bytes(), original)

    def test_shared_parent_row_does_not_duplicate_contact_metadata(self):
        first, second = self.parent(), self.parent("casey")
        self.identity()
        self.identity("casey")
        rows, _ = self.run_projection([self.row("shared-row",
            superseded_person_ids=json.dumps([first, second]),
            interaction_counts='{"gmail": 12}', last_interaction="2024-01-01T10:00:00+00:00",
            source_artifacts='["shared.csv"]')])
        self.assertEqual(len(rows), 2)
        for row in rows:
            for field in ("interaction_counts", "last_interaction", "source_artifacts"):
                self.assertEqual(row[field], "", field)

    def test_realization_receipt_fingerprints_final_csv_and_preserves_fan_in_receipt(self):
        person_id = self.parent()
        self.identity()
        self.csv.parent.mkdir(parents=True)
        fan_in = self.csv.parent / "manifest.json"
        fan_in.write_text('{"stage":"merge_people","stats":{"rows":2}}')
        original = fan_in.read_bytes()
        rows, _ = self.run_projection([self.row(person_id)])
        manifest = json.loads((self.db.db_path.parent / "realize/manifest.json").read_text())
        self.assertEqual(manifest["people"], len(rows))
        self.assertEqual(manifest["fingerprints"]["output_artifacts"][str(self.csv)]["sha256"],
                         hashlib.sha256(self.csv.read_bytes()).hexdigest())
        self.assertEqual(fan_in.read_bytes(), original)

    def test_changed_fan_in_id_maps_by_contact(self):
        self.parent()
        self.identity()
        rows, _ = self.run_projection([self.row(generate_person_id("casey-bravo"),
            public_identifier="casey-bravo", linkedin_url="https://www.linkedin.com/in/casey-bravo/")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linkedin_url"], "")

    def test_merged_row_is_removed_when_all_direct_parents_are_excluded(self):
        first, second = self.parent(), self.parent("casey")
        self.db.decide_worth("jordan", "no")
        self.db.decide_worth("casey", "no")
        rows, result = self.run_projection([self.row("merged-parent-id",
            all_emails='["jordan@example.com", "casey@example.com"]',
            superseded_person_ids=json.dumps([first, second]))])
        self.assertEqual(rows, [])
        self.assertEqual(result["removed_rows"], 1)

    def test_superseded_candidate_maps_to_canonical_parent(self):
        person_id = self.parent(worth="no")
        rows, _ = self.run_projection([self.row("new-import-id", primary_email="",
            superseded_person_ids=json.dumps([person_id]))])
        self.assertEqual(rows, [])

    def test_human_worth_yes_restores_machine_excluded_contact_unlinked(self):
        person_id = self.parent()
        self.identity(action="exclude")
        self.db.decide_worth("jordan", "yes")
        rows, result = self.run_projection([self.row(person_id)])
        self.assertEqual(result["dossier_parents"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linkedin_url"], "")
        self.assertIn("astronomy", rows[0]["summary"])

    def test_human_worth_overrides_machine(self):
        kept = self.parent(worth="no")
        dropped = self.parent("casey", worth="yes")
        self.db.decide_worth("jordan", "yes")
        self.db.decide_worth("casey", "no")
        self.identity()
        rows, _ = self.run_projection([self.row(kept), self.row(dropped, primary_email="casey@example.com")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["primary_email"], "jordan@example.com")

    def test_human_verify_preserves_profile_and_human_detach_strips_it(self):
        person_id = self.parent()
        self.identity(human="verify")
        original = self.row(person_id, public_identifier="casey-bravo",
            linkedin_url="https://www.linkedin.com/in/casey-bravo/", summary="Verified biography",
            source_channels='["linkedin_csv"]')
        rows, _ = self.run_projection([original])
        self.assertEqual(rows[0]["summary"], "Verified biography")
        self.db.decide_identity("jordan:casey-bravo", "detach")
        rows, _ = self.run_projection([original])
        self.assertEqual(rows[0]["linkedin_url"], "")

    def test_human_worth_no_removes_owned_imported_profile(self):
        person_id = self.parent()
        self.identity(action="verify")
        self.db.decide_worth("jordan", "no")
        rows, _ = self.run_projection([self.row(person_id, source_channels='["linkedin_csv"]',
            linkedin_url="https://www.linkedin.com/in/casey-bravo/")])
        self.assertEqual(rows, [])

    def test_shared_proposed_slug_does_not_drop_other_parents_profile(self):
        self.parent()
        self.identity()
        real_id = self.parent("casey", person_id=generate_person_id("casey-bravo"))
        self.identity("casey", action="verify")
        rows, _ = self.run_projection([self.row(real_id, full_name="Casey Bravo",
            public_identifier="casey-bravo", linkedin_url="https://www.linkedin.com/in/casey-bravo/",
            primary_email="casey@example.com", all_emails='["casey@example.com","jordan@example.com"]',
            source_channels='["linkedin_csv","gmail_msgvault"]', summary="Casey's verified biography")])
        self.assertEqual(len(rows), 2)
        profile = next(row for row in rows if row["linkedin_url"])
        self.assertEqual(profile["id"], real_id)
        self.assertEqual(profile["summary"], "Casey's verified biography")
        self.assertNotIn("jordan@example.com", profile["all_emails"])
        dossier = next(row for row in rows if not row["linkedin_url"])
        self.assertEqual(dossier["primary_email"], "jordan@example.com")

    def test_shared_identifier_keeps_unassigned_profile_and_reports_ambiguity(self):
        self.parent(email="shared@example.com")
        self.parent("casey", email="shared@example.com")
        self.identity()
        self.identity("casey")
        profile_id = generate_person_id("other-person")
        rows, result = self.run_projection([self.row(profile_id, primary_email="shared@example.com",
            linkedin_url="https://www.linkedin.com/in/other-person/", source_channels='["linkedin_csv"]')])
        self.assertEqual(len(rows), 3)
        self.assertEqual(result["ambiguous_rows"], 1)
        self.assertEqual(next(row for row in rows if row["id"] == profile_id)["primary_email"], "shared@example.com")

    def test_verified_profile_owner_wins_over_another_contacts_proposed_slug(self):
        self.parent()
        self.identity()
        self.parent("casey")
        self.identity("casey", action="verify")
        profile_id = generate_person_id("casey-bravo")
        rows, _ = self.run_projection([self.row(profile_id,
            linkedin_url="https://www.linkedin.com/in/casey-bravo/", summary="Verified Casey")])
        self.assertEqual(len(rows), 2)
        profile = next(row for row in rows if row["id"] == profile_id)
        self.assertEqual(profile["summary"], "Verified Casey")
        self.assertEqual(profile["primary_email"], "")

    def test_unassigned_imported_profile_reports_ownership_gap(self):
        self.parent(worth="no")
        rows, result = self.run_projection([self.row(generate_person_id("other-person"),
            linkedin_url="https://www.linkedin.com/in/other-person/", source_channels='["linkedin_csv"]')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["primary_email"], "")
        self.assertEqual(result["unassigned_profile_rows"], 1)

    def test_cli_dry_run_reports_projection_without_writing(self):
        person_id = self.parent(worth="no")
        CsvIO.write_dict_rows(self.csv, PEOPLE_SCHEMA_COLUMNS, [self.row(person_id)])
        before = self.csv.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--db", str(self.db.db_path), "--people-csv", str(self.csv), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["people"], 0)
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(list(self.csv.parent.glob("*.bkup")), [])
        self.assertFalse((self.db.db_path.parent / "realize/manifest.json").exists())

    def test_human_identity_yes_survives_machine_worth_no_until_human_worth_no(self):
        for action in ("verify", "retarget"):
            with self.subTest(action=action):
                person_id = self.parent(action, worth="no")
                slug = f"{action}-bravo"
                key = self.identity(action, slug=slug)
                url = f"https://www.linkedin.com/in/{slug}/"
                self.db.decide_identity(key, action, **(
                    {"replacement_url": url} if action == "retarget" else {}
                ))
                original = self.row(person_id, linkedin_url=url, summary="Human approved profile")
                rows, result = self.run_projection([original])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["summary"], "Human approved profile")
                self.db.decide_worth(action, "no")
                rows, _ = self.run_projection([original])
                self.assertEqual(rows, [])

    def test_owned_imported_linkedin_survives_machine_worth_no_until_human_worth_no(self):
        person_id = self.parent(worth="no")
        self.db.project_rows((PersonSourcesProjection(person_id, (
            PersonSourceRow(person_id, "linkedin_csv"),
        )),))
        original = self.row(person_id, source_channels='["linkedin_csv"]',
            linkedin_url="https://www.linkedin.com/in/jordan-bravo/")
        rows, result = self.run_projection([original])
        self.assertEqual(len(rows), 1)
        self.assertEqual(result["excluded_parents"], 0)
        self.db.decide_worth("jordan", "no")
        rows, _ = self.run_projection([original])
        self.assertEqual(rows, [])

    def test_human_detach_strips_imported_profile_despite_machine_worth_no(self):
        person_id = self.parent(worth="no")
        self.db.project_rows((PersonSourcesProjection(person_id, (
            PersonSourceRow(person_id, "linkedin_csv"),
        )),))
        self.identity(human="detach")
        rows, result = self.run_projection([self.row(person_id,
            source_channels='["linkedin_csv"]',
            linkedin_url="https://www.linkedin.com/in/casey-bravo/",
            summary="Wrong person biography")])
        self.assertEqual(result["dossier_parents"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linkedin_url"], "")
        self.assertIn("astronomy", rows[0]["summary"])
        self.assertNotIn("Wrong person", rows[0]["summary"])

    def test_dossier_summary_preserves_explicit_title_and_event_dates(self):
        person_id = self.parent()
        self.identity()
        self.db.project_rows((FactRow("jordan", "jordan", "facts:jordan", machine_worth="yes",
            facts_json=json.dumps({
                "title": "Architect", "relationship_to_owner": "Astronomy club friend",
                "employers": [{"name": "Example Studio", "role": "Designer", "status": "past"}],
                "notable_events": [{"date": "2022-03", "summary": "Left Example Studio"}],
            })),))
        rows, _ = self.run_projection([self.row(person_id)])
        self.assertIn("Architect", rows[0]["summary"])
        self.assertIn("past", rows[0]["summary"])
        self.assertIn("2022-03", rows[0]["summary"])
        self.assertIn("Left Example Studio", rows[0]["summary"])
        self.assertEqual(rows[0]["current_title"], "")
        self.assertEqual(rows[0]["work_experiences"], "")


if __name__ == "__main__":
    unittest.main()
