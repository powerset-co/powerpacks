from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.build_owner import BuildOwner, owner_from_profile
from packs.ingestion.primitives.deep_context.check_readiness import sqlite_counts
from packs.ingestion.primitives.deep_context.db.legacy import import_legacy
from packs.ingestion.primitives.deep_context.db.models import (
    OwnerContextRow,
    OwnerEducation,
    OwnerProfile,
    OwnerWork,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.collection.normalization import normalize_cached_bundles
from packs.ingestion.primitives.deep_context.synthesis.selection import build_plan
from packs.ingestion.primitives.deep_context.profile_projection import (
    canonical_profile_result,
)
from packs.ingestion.primitives.deep_context.profile_models import ProfileResult


OWNER = {
    "name": "Mailbox Owner",
    "emails": ["owner@example.com"],
    "phones": [],
    "education": [{"school": "Example University", "start": 2010, "end": 2014}],
    "work": [{"company": "Example Labs", "title": "Engineer", "start": 2015}],
    "locations": ["San Francisco"],
    "notes": "Builder",
}
OWNER_PROFILE = OwnerProfile(
    name="Mailbox Owner",
    emails=("owner@example.com",),
    education=(OwnerEducation("Example University", 2010, 2014),),
    work=(OwnerWork("Example Labs", "Engineer", 2015),),
    locations=("San Francisco",),
    notes="Builder",
)


class OwnerProjectionTests(unittest.TestCase):
    def test_profile_boundary_stamps_canonical_company_school_and_identifier(self) -> None:
        result = canonical_profile_result(
            "jordan-bravo",
            "https://www.linkedin.com/in/jordan-bravo",
            {
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "experiences": [{"title": "Founder", "company": "Example Labs"}],
                    "education": [{"school": "Example University"}],
                }
            },
        )
        profile = result["normalized_profile"]

        self.assertEqual(profile["public_identifier"], "jordan-bravo")
        self.assertEqual(profile["experiences"], [
            {"title": "Founder", "company_name": "Example Labs"},
        ])
        self.assertEqual(profile["education"], [
            {"school_name": "Example University"},
        ])
        owner = owner_from_profile(
            ProfileResult.from_payload(
                "jordan-bravo",
                "https://www.linkedin.com/in/jordan-bravo",
                result,
            ).normalized_profile
        )
        self.assertEqual(owner.work[0].company, "Example Labs")
        self.assertEqual(owner.education[0].school, "Example University")

    def test_corrupt_existing_owner_is_an_error_and_is_not_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner_path = root / "owner.json"
            owner_path.write_text("not json", encoding="utf-8")
            db = Db(root / "deep-context.sqlite")

            result = BuildOwner(out=owner_path, db=db).run()

            self.assertEqual(result.status, "error")
            self.assertIn("owner.json is invalid", result.error or "")
            self.assertIsNone(canonical_snapshot(db).owner)

    def test_build_owner_reuse_projects_complete_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner_path = root / "owner.json"
            owner_path.write_text(json.dumps(OWNER, indent=2) + "\n", encoding="utf-8")
            db = Db(root / "deep-context.sqlite")

            result = BuildOwner(out=owner_path, db=db).run()

            self.assertEqual(result.status, "exists")
            snapshot = canonical_snapshot(db)
            self.assertEqual(snapshot.owner, OWNER_PROFILE)
            self.assertEqual(snapshot.owner_path, str(owner_path))

    def test_synthesis_and_readiness_use_sqlite_after_file_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner_path = root / "owner.json"
            db = Db(root / "deep-context.sqlite")
            db.project_rows((OwnerContextRow("owner", json.dumps(OWNER), str(owner_path), "0" * 64),))

            plan = build_plan(
                db, chunk_chars=9000, max_batches=20,
                no_owner=False, force=False, rejudge=False,
            )
            counts = sqlite_counts(db)

            self.assertEqual(plan.owner, OWNER_PROFILE)
            self.assertIn("MAILBOX OWNER BACKGROUND (me): Mailbox Owner", plan.system_prompt)
            self.assertEqual(
                (
                    counts.message_people,
                    counts.candidates.total,
                    counts.messages.total,
                ),
                (0, 0, 0),
            )
            self.assertTrue(counts.has_owner)
            self.assertEqual(counts.owner_path, str(owner_path))

    def test_legacy_import_absorbs_owner_json_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner_path = root / "owner.json"
            owner_path.write_text(json.dumps(OWNER), encoding="utf-8")
            db = Db(root / "deep-context.sqlite")

            counts = import_legacy(db, review_csv=root / "missing-review.csv", owner_json=owner_path)

            self.assertEqual(counts["owner_context"], 1)
            self.assertEqual(canonical_snapshot(db).owner, OWNER_PROFILE)

    def test_owner_only_database_does_not_block_legacy_graph_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((OwnerContextRow("owner", json.dumps(OWNER), str(root / "owner.json"), "0" * 64),))

            counts = import_legacy(db, review_csv=root / "missing-review.csv")

            self.assertEqual(counts["owner_context"], 0)
            self.assertEqual(canonical_snapshot(db).owner, OWNER_PROFILE)

    def test_legacy_dossiers_and_profile_cache_survive_source_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossiers = root / "dossiers"
            parents = root / "parents"
            profiles = root / "profiles"
            for path in (dossiers, parents, profiles):
                path.mkdir()
            child_path = dossiers / "jordan.md"
            parent_path = parents / "jordan-parent.md"
            profile_path = profiles / "jordan-bravo.json"
            child_path.write_text("# Jordan child dossier\n", encoding="utf-8")
            parent_path.write_text("# Jordan parent dossier\n", encoding="utf-8")
            profile = {
                "public_identifier": "jordan-bravo",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "normalized_profile": {"success": True, "full_name": "Jordan Bravo"},
            }
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            (root / "index.json").write_text(
                json.dumps(
                    {
                        "slugs": {"jordan": {"person_id": "person-1", "name": "Jordan Bravo", "path": "dossiers/jordan.md"}},
                        "parents": {
                            "jordan-parent": {
                                "parent_id": "parent-1",
                                "name": "Jordan Bravo",
                                "path": "parents/jordan-parent.md",
                                "children": ["jordan"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            review_path = root / "review.csv"
            with review_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("public_identifier", "person_id", "linkedin_url"))
                writer.writeheader()
                writer.writerow(
                    {
                        "public_identifier": "jordan-bravo",
                        "person_id": "person-1",
                        "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    }
                )
            db = Db(root / "deep-context.sqlite")

            counts = import_legacy(db, review_csv=review_path, index_json=root / "index.json", profile_cache_dir=profiles)
            child_path.unlink()
            parent_path.unlink()
            profile_path.unlink()

            snapshot = canonical_snapshot(db)
            by_person = {row.person_id: row for row in snapshot.dossiers}
            self.assertEqual(by_person["person-1"].body, "# Jordan child dossier\n")
            parent = next(row for row in snapshot.dossiers if row.person_id is None)
            self.assertEqual(parent.body, "# Jordan parent dossier\n")
            projected_profile = next(row for row in snapshot.artifacts if row.kind == "profile")
            self.assertEqual(json.loads(projected_profile.payload_json or "{}"), profile)
            self.assertEqual(counts["profiles"], 1)

    def test_legacy_source_bundle_hydrates_synthesis_after_file_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            person_id = "person-1"
            bundle_path = raw / f"{person_id}.json"
            bundle = {"person_id": person_id, "messages": [{"channel": "imessage", "text": "Synthetic hello"}]}
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({"status": "completed", "total": 1}), encoding="utf-8")
            (root / "index.json").write_text(
                json.dumps(
                    {
                        "slugs": {"jordan": {"person_id": person_id}},
                        "parents": {"jordan": {"parent_id": "parent-1", "children": ["jordan"]}},
                    }
                ),
                encoding="utf-8",
            )
            db = Db(root / "deep-context.sqlite")

            counts = import_legacy(db, review_csv=root / "missing-review.csv", index_json=root / "index.json", raw_dir=raw)
            bundle_path.unlink()
            normalize_cached_bundles(db, raw)
            plan = build_plan(
                db, chunk_chars=9000, max_batches=20,
                no_owner=True, force=False, rejudge=False,
            )

            self.assertEqual(counts["artifacts"], 1)
            self.assertEqual(plan.bundles[0].person_id, "parent-1")
            self.assertEqual(
                [row.to_payload() for row in plan.bundles[0].messages],
                bundle["messages"],
            )


if __name__ == "__main__":
    unittest.main()
