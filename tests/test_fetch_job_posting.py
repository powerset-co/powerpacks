import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import fetch_jd


FIXTURE = Path(__file__).parent / "fixtures" / "listenlabs-product-ashby.json"


class TestAshbyJobPosting(unittest.TestCase):
    def _fetch(self, job: dict) -> tuple[str, dict]:
        response = io.BytesIO(json.dumps({"jobs": [job]}).encode())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jd.txt"
            with (
                mock.patch("sys.argv", ["fetch_jd", "--url", job["jobUrl"], "--out", str(out)]),
                mock.patch.object(fetch_jd.urllib.request, "urlopen", return_value=response),
                mock.patch.object(fetch_jd, "fetch", return_value=("", job["jobUrl"])),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                fetch_jd.main()
            return out.read_text(), json.loads((Path(tmp) / "source.json").read_text())

    def test_preserves_primary_secondary_locations_and_workplace(self):
        job = json.loads(FIXTURE.read_text())
        text, source = self._fetch(job)

        header = text.split("\n\n", 1)[0]
        self.assertIn("Locations: San Francisco, CA; New York, NY", header)
        self.assertIn("Workplace: OnSite", header)
        self.assertIn("Department: Engineering, Product & Design", header)
        self.assertIn("Employment type: FullTime", header)
        for key in ("location", "secondaryLocations", "address", "workplaceType",
                    "isRemote", "department", "team", "employmentType"):
            self.assertEqual(source[key], job[key], key)
        self.assertIs(source["isRemote"], False)
        self.assertEqual(source["address"]["postalAddress"]["addressLocality"], "San Francisco")
        self.assertEqual(source["secondaryLocations"][0]["address"]["postalAddress"]["addressLocality"], "New York")
        self.assertEqual(source["via"], "ashby_posting_api")
        self.assertEqual(source["source_title"], job["title"])

    def test_is_remote_false_does_not_imply_onsite(self):
        job = json.loads(FIXTURE.read_text())
        del job["workplaceType"]
        text, source = self._fetch(job)

        self.assertIs(source["isRemote"], False)
        self.assertNotIn("workplaceType", source)
        self.assertNotIn("Workplace:", text.split("\n\n", 1)[0])


class TestGenericJobPosting(unittest.TestCase):
    def _fetch(self, fixture: str, url: str) -> tuple[str, dict, dict]:
        html = (FIXTURE.parent / fixture).read_text()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jd.txt"
            output = io.StringIO()
            with (
                mock.patch("sys.argv", ["fetch_jd", "--url", url, "--out", str(out)]),
                mock.patch.object(fetch_jd, "fetch", return_value=(html, url)),
                mock.patch.object(fetch_jd.urllib.request, "urlopen", side_effect=AssertionError("unexpected network call")),
                contextlib.redirect_stdout(output),
            ):
                fetch_jd.main()
            return out.read_text(), json.loads((Path(tmp) / "source.json").read_text()), json.loads(output.getvalue())

    def test_script_only_job_posting_preserves_description_and_both_locations(self):
        text, source, summary = self._fetch(
            "greenhouse-job-metadata.html", "https://boards.greenhouse.io/example/jobs/123")

        self.assertIn("Product Engineer", text)
        self.assertIn("Own customer-facing software", text)
        self.assertIn("San Francisco, CA, US", text)
        self.assertIn("New York, NY, US", text)
        self.assertNotIn("renderJob", text)
        self.assertEqual(source["via"], "html")
        self.assertEqual(source["company_name"], "Example Robotics")
        self.assertEqual(summary["status"], "ok")

    def test_graph_job_posting_keeps_visible_text_and_single_location(self):
        text, _, summary = self._fetch(
            "generic-job-metadata.html", "https://example.test/careers/data-engineer")

        self.assertIn("Applications close next month.", text)
        self.assertIn("Build data pipelines", text)
        self.assertIn("Toronto, Ontario, Canada", text)
        self.assertEqual(summary["status"], "thin")

    def test_visible_lever_style_locations_and_workplace_are_preserved(self):
        text, source, summary = self._fetch(
            "lever-job-page.html", "https://jobs.lever.co/example/software-engineer")

        self.assertIn("London, England / Dublin, Ireland", text)
        self.assertIn("Hybrid", text)
        self.assertIn("two office days each week", text)
        self.assertNotIn("Privacy policy", text)
        self.assertEqual(source["via"], "html")
        self.assertEqual(summary["status"], "ok")


if __name__ == "__main__":
    unittest.main()
