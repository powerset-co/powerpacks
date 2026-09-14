"""Directory lists stay narrow and hydrate only the selected person."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db import _view_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.review.server import make_handler
from deep_context_sqlite_test_helpers import seed_identity
from http_handler_test_helpers import InProcessHttpClient


class DirectoryQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / "context.sqlite")
        for slug, name, worth, paid in (
            ("jordan-bravo", "Jordan Bravo", "yes", 1),
            ("casey-delta", "Casey Delta", "no", 1),
            ("unreviewable", "Unreviewable Person", "yes", 0),
        ):
            seed_identity(
                self.db, parent_id=slug, person_id=f"person-{slug}",
                row_key=slug, name=name, machine_worth=worth,
                linkedin_url=f"https://www.linkedin.com/in/{slug}",
                link_updates={"paid_profile": paid}, candidate_people=True,
                artifact_root=Path(self.temp.name),
                dossier_body=f"# {name}\n\nSelected person's dossier.",
            )
        self.http = InProcessHttpClient(make_handler(db=self.db, run_jobs=False))

    def test_directory_list_does_not_hydrate_every_person(self):
        with mock.patch.object(
            _view_rows, "_hydrate_parents",
            side_effect=AssertionError("directory list must not hydrate people"),
        ):
            status, _, body, _ = self.http.request("GET", "/directory")
        self.assertEqual(status, 200)
        payload = re.search(rb"data-directory-people>(.*?)</script>", body).group(1)
        self.assertEqual(json.loads(payload), [
            {"slug": "casey-delta", "name": "Casey Delta", "worth": "no"},
            {"slug": "jordan-bravo", "name": "Jordan Bravo", "worth": "yes"},
        ])
        self.assertIn(b"2 people", body)

    def test_directory_hydrates_only_selected_person(self):
        with mock.patch.object(
            _view_rows, "_candidate_row", wraps=_view_rows._candidate_row,
        ) as hydrate:
            status, _, body, _ = self.http.request("GET", "/directory?person=jordan-bravo")
        self.assertEqual(status, 200)
        self.assertEqual(hydrate.call_count, 1)
        self.assertEqual(hydrate.call_args.args[0]["parent_id"], "jordan-bravo")
        self.assertIn(b"Selected person's dossier.", body)


if __name__ == "__main__":
    unittest.main()
