"""Realize writes the final roster into canonical SQLite, then exports it."""

import importlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection, CandidatePersonRow, LinkRow, WriterSource,
)
from packs.ingestion.primitives.deep_context.db.queries import imported_people, parents
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import project_profile_results
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.deep_context.realize import export_people
from packs.ingestion.primitives.enrich import profile_transforms
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, generate_person_id
from packs.shared.csv_io import CsvIO

WRONG_WORK = json.dumps([{"title": "Wrong", "company": "Wrong Co"}])
JORDAN_WORK = json.dumps([{"title": "Engineer", "company": "Example Labs"}])
JORDAN_EDUCATION = json.dumps([{"school": "Example University"}])


def imported(**fields: str) -> dict[str, str]:
    return {column: fields.get(column, "") for column in PEOPLE_SCHEMA_COLUMNS}


def linkedin(slug: str) -> str:
    return f"https://www.linkedin.com/in/{slug}"


def project_profile(db: Db, row_key: str, slug: str, name: str, title: str) -> None:
    parent_id = db.query("SELECT parent_id FROM links WHERE row_key=?", (row_key,))[0]["parent_id"]
    raw = {"full_name": name, "headline": f"{title} at Example Labs", "public_identifier": slug,
           "experiences": [{"title": title, "company": "Example Labs", "ends_at": None}],
           "education": [{"school": "Example University"}]}
    result = ProfileResult.from_payload(slug, linkedin(slug), {
        "state": "content", "data": raw, "from_cache": True, "fetched": False,
        "normalized_profile": {"success": True, "public_identifier": slug, "full_name": name,
                               "experiences": raw["experiences"], "education": raw["education"]},
    })
    project_profile_results(db, ((ProfileTarget(slug, linkedin(slug), row_key, parent_id), result),), db.db_path.parent)


class ExportPeopleTests(unittest.TestCase):
    def test_one_profile_normalization_failure_still_exports_both_people(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            people_csv = base / "merged" / "people.csv"
            CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [
                imported(id=generate_person_id(slug), public_identifier=slug,
                         linkedin_url=linkedin(slug), full_name=name, source_channels="linkedin_csv")
                for slug, name in (("casey-delta", "Casey Delta"), ("jordan-bravo", "Jordan Bravo"))
            ])
            db = Db(base / "deep-context.sqlite")
            EnsureParents(db=db, people_csv=people_csv).run()
            for slug, name in (("casey-delta", "Casey Delta"), ("jordan-bravo", "Jordan Bravo")):
                db.decide_identity(slug, "verify")
                project_profile(db, slug, slug, name, "Engineer")
            normalize = profile_transforms.normalize_rapidapi

            def fail_casey(raw, public_identifier, linkedin_url):
                if public_identifier == "casey-delta":
                    raise ValueError("fixture profile cannot normalize")
                return normalize(raw, public_identifier, linkedin_url)

            try:
                with (
                    mock.patch.object(profile_transforms, "normalize_rapidapi", side_effect=fail_casey) as normalizer,
                    redirect_stderr(io.StringIO()) as log,
                ):
                    # Reload the direct import so the patch stays at the definition.
                    importlib.reload(export_people)
                    payload = export_people.ExportPeople(db=db, out_dir=base / "merged").run()
            finally:
                importlib.reload(export_people)

            rows = {row["public_identifier"]: row for row in CsvIO.read_dict_rows(people_csv)}
            self.assertEqual(set(rows), {"casey-delta", "jordan-bravo"})
            self.assertEqual((payload["status"], payload["rows"]), ("completed", 2))
            self.assertEqual((payload["profiles_filled"], payload["profiles_missing"]), (1, 1))
            self.assertEqual(normalizer.call_count, 2)
            self.assertEqual(rows["casey-delta"]["work_experiences"], "")
            jordan = rows["jordan-bravo"]
            self.assertEqual((jordan["current_title"], jordan["current_company"]), ("Engineer", "Example Labs"))
            self.assertEqual(json.loads(jordan["work_experiences"])[0]["title"], "Engineer")
            self.assertTrue(json.loads(jordan["education"]))
            self.assertEqual({row.id for row in imported_people(db)}, {row["id"] for row in rows.values()})
            self.assertEqual({row.id: row.full_name for row in imported_people(db)},
                             {generate_person_id("casey-delta"): "Casey Delta", generate_person_id("jordan-bravo"): "Jordan Bravo"})
            self.assertIn(f"[realize] {generate_person_id('casey-delta')}: profile not used: "
                          "ValueError: fixture profile cannot normalize", log.getvalue())

    def test_reviewed_roster_exports_from_sqlite_without_csv_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            people_csv = base / "merged" / "people.csv"
            CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [
                imported(id=generate_person_id("jordan-bravo"), public_identifier="jordan-bravo",
                         linkedin_url=linkedin("jordan-bravo"), full_name="Jordan Bravo", headline="Engineer",
                         work_experiences=JORDAN_WORK, education=JORDAN_EDUCATION,
                         rapidapi_response='{"full_name": "Jordan Bravo"}', source_channels="linkedin_csv"),
                imported(id=generate_person_id("casey-wrong"), public_identifier="casey-wrong",
                         linkedin_url=linkedin("casey-wrong"), full_name="Casey Delta", headline="Wrong person",
                         current_company="Wrong Co", work_experiences=WRONG_WORK,
                         rapidapi_response='{"full_name": "Wrong Person"}', entity_urn="urn:wrong",
                         primary_email="casey@example.com", all_emails='["casey@example.com"]',
                         primary_phone="+15550101", all_phones='["+15550101"]',
                         interaction_counts='{"gmail": 12}', last_interaction="2026-09-01T00:00:00Z",
                         source_channels="gmail"),
                imported(id="candidate:phone:+15550102", full_name="Robin Echo", primary_phone="+15550102",
                         all_phones='["+15550102"]', interaction_counts='{"imessage": 40}',
                         source_channels="imessage"),
                imported(id=generate_person_id("sam-fox"), public_identifier="sam-fox", linkedin_url=linkedin("sam-fox"),
                         full_name="Sam Fox", work_experiences=WRONG_WORK, primary_email="sam@example.com",
                         all_emails='["sam@example.com"]', source_channels="gmail"),
                imported(id=generate_person_id("pat-gray"), public_identifier="pat-gray", linkedin_url=linkedin("pat-gray"),
                         full_name="Pat Gray", work_experiences=WRONG_WORK, source_channels="linkedin_csv"),
                imported(id="candidate:phone:+15550103", full_name="Riley Stone", primary_phone="+15550103",
                         all_phones='["+15550103"]', source_channels="whatsapp"),
            ])
            db = Db(base / "deep-context.sqlite")
            EnsureParents(db=db, people_csv=people_csv).run()
            people_csv.unlink()
            db.decide_identity("jordan-bravo", "verify")

            robin_parent = db.query("SELECT parent_id FROM people WHERE person_id='candidate:phone:+15550102'")[0][0]
            db.project_rows((LinkRow("robin-echo", robin_parent, "robin-echo", "pub", linkedin("robin-echo"),
                                     "Robin Echo", machine_action="verify", machine_approved="auto",
                                     candidate_origin=True, source=WriterSource.RECONCILE.value),))
            db.project_rows((CandidatePeopleProjection("robin-echo", (CandidatePersonRow("robin-echo", "candidate:phone:+15550102", robin_parent),)),))
            project_profile(db, "robin-echo", "robin-echo", "Robin Echo", "Designer")
            db.decide_identity("casey-wrong", "retarget", replacement_url=linkedin("casey-delta"),
                               replacement_public_identifier="casey-delta")
            project_profile(db, "casey-wrong", "casey-delta", "Casey Delta", "Founder")
            db.decide_identity("sam-fox", "detach")
            db.decide_identity("pat-gray", "detach")
            riley_parent = db.query("SELECT parent_id FROM people WHERE person_id='candidate:phone:+15550103'")[0][0]
            db.project_rows((LinkRow("synthetic:riley", riley_parent, "synthetic:riley", "synthetic",
                                     source=WriterSource.RECONCILE.value),))
            db.project_rows((CandidatePeopleProjection("synthetic:riley", (CandidatePersonRow("synthetic:riley", "candidate:phone:+15550103", riley_parent),)),))
            db.decide_identity("synthetic:riley", "retarget", replacement_url=linkedin("riley-stone"),
                               replacement_public_identifier="riley-stone")
            db.decide_worth(robin_parent, "yes")
            before_parents = parents(db)
            before_people = db.query("SELECT * FROM people ORDER BY person_id")
            before_identifiers = db.query("SELECT * FROM person_identifiers ORDER BY 1, 2, 3")

            payload = ExportPeople(db=db, out_dir=base / "merged").run()
            rows = {row["id"]: row for row in CsvIO.read_dict_rows(people_csv)}
            self.assertEqual(sorted(path.name for path in base.rglob("*.csv")), ["people.csv"])
            self.assertEqual(parents(db), before_parents)
            self.assertEqual(db.query("SELECT * FROM person_identifiers ORDER BY 1, 2, 3"), before_identifiers)
            after_people = db.query("SELECT * FROM people ORDER BY person_id")
            self.assertTrue(set(map(tuple, before_people)) <= set(map(tuple, after_people)))
            parent_of = {row["person_id"]: row["parent_id"] for row in after_people}
            self.assertEqual(parent_of["candidate:phone:+15550102"], robin_parent)
            self.assertEqual({row.id for row in imported_people(db)}, set(rows))
            self.assertEqual(next(row.primary_phone for row in imported_people(db) if row.id == "candidate:phone:+15550102"),
                             "+15550102")

            ShareList(db=db, out_dir=base / "share").run()
            shared = {row["person_id"] for row in db.query("SELECT person_id FROM share")}
            self.assertEqual(shared, set(rows))
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])

            again = ExportPeople(db=db, out_dir=base / "merged").run()
            self.assertEqual((again["rows"], again["people_added"]), (payload["rows"], 0))
            self.assertEqual({row["id"]: row for row in CsvIO.read_dict_rows(people_csv)}, rows)

        self.assertEqual(payload["status"], "completed")
        jordan = rows[generate_person_id("jordan-bravo")]
        self.assertEqual((jordan["work_experiences"], jordan["education"]), (JORDAN_WORK, JORDAN_EDUCATION))
        self.assertEqual(jordan["rapidapi_response"], '{"full_name": "Jordan Bravo"}')

        casey = rows[generate_person_id("casey-wrong")]
        self.assertEqual(casey["public_identifier"], "casey-delta")
        self.assertEqual([item["title"] for item in json.loads(casey["work_experiences"])], ["Founder"])
        self.assertEqual((casey["primary_email"], casey["primary_phone"]), ("casey@example.com", "+15550101"))
        self.assertEqual(casey["interaction_counts"], '{"gmail": 12}')
        self.assertEqual(casey["superseded_person_ids"], "")
        self.assertEqual((casey["rapidapi_response"], casey["entity_urn"]), ("", ""))

        robin = rows["candidate:phone:+15550102"]
        self.assertEqual(robin["primary_phone"], "+15550102")
        self.assertEqual([item["title"] for item in json.loads(robin["work_experiences"])], ["Designer"])
        self.assertEqual(robin["superseded_person_ids"], "")

        sam = rows[generate_person_id("sam-fox")]
        self.assertEqual((sam["public_identifier"], sam["work_experiences"]), ("", ""))
        exported = json.dumps(list(rows.values()))
        for gone in ("casey-wrong", "Wrong Co", "sam-fox", "pat-gray"):
            self.assertNotIn(gone, exported)
        riley = rows["candidate:phone:+15550103"]
        self.assertEqual((riley["public_identifier"], riley["primary_phone"]), ("riley-stone", "+15550103"))
        self.assertEqual(len(rows), 6)

    def test_retarget_onto_an_existing_profile_keeps_families_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            people_csv = base / "merged" / "people.csv"
            CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [
                imported(id=generate_person_id("jordan-old"), public_identifier="jordan-old",
                         linkedin_url=linkedin("jordan-old"), full_name="Jordan Bravo",
                         primary_email="jordan@example.test", all_emails='["jordan@example.test"]',
                         source_channels="gmail"),
                imported(id=generate_person_id("jordan-new"), public_identifier="jordan-new",
                         linkedin_url=linkedin("jordan-new"), full_name="Jordan Bravo", work_experiences=JORDAN_WORK,
                         primary_phone="+15550102", all_phones='["+15550102"]', source_channels="imessage"),
            ])
            db = Db(base / "deep-context.sqlite")
            EnsureParents(db=db, people_csv=people_csv).run()
            people_csv.unlink()
            parent_of = {row["person_id"]: row["parent_id"] for row in db.query("SELECT * FROM people")}
            old_parent, new_parent = parent_of[generate_person_id("jordan-old")], parent_of[generate_person_id("jordan-new")]
            self.assertNotEqual(old_parent, new_parent)
            db.decide_worth(old_parent, "yes")
            db.decide_identity("jordan-old", "retarget", replacement_url=linkedin("jordan-new"),
                               replacement_public_identifier="jordan-new")

            payload = ExportPeople(db=db, out_dir=base / "merged").run()
            rows = {row["id"]: row for row in CsvIO.read_dict_rows(people_csv)}
            families = db.query("SELECT DISTINCT parent_id FROM people")
            old = next(row for row in parents(db) if row.parent_id == old_parent)
            again = ExportPeople(db=db, out_dir=base / "merged").run()
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(set(rows), {generate_person_id("jordan-old"), generate_person_id("jordan-new")})
        self.assertEqual(rows[generate_person_id("jordan-old")]["primary_email"], "jordan@example.test")
        self.assertEqual(rows[generate_person_id("jordan-new")]["primary_phone"], "+15550102")
        self.assertEqual(len(families), 2)
        self.assertEqual(old.human_worth, "yes")
        self.assertEqual((again["rows"], again["accepted_identities"]), (2, 1))

    def test_an_empty_roster_refuses_to_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            payload = ExportPeople(db=Db(base / "deep-context.sqlite"), out_dir=base / "merged").run()
            self.assertFalse((base / "merged").exists())
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("ensure-parents", payload["reason"])


if __name__ == "__main__":
    unittest.main()
