"""Regression: build_research_review_csv must carry the deep-research LinkedIn URL.

Created: 2026-06-18

Context: the local ($setup) Messages path resolves contacts to LinkedIn via Parallel
deep research, then dumps reviewer-kept rows to import/messages/people.csv via
materialize_messages_review_people. That materializer only keeps a row if one of
linkedin_url / network_linkedin_url / retarget_linkedin_url is populated. Before the
fix, build_research_review_csv.flatten_row() never wrote the researched
social.linkedin_url into any of those columns, so every deep-research-resolved
contact silently produced zero people rows. These tests pin the column through the
real `build` command and through materialization.
"""
import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.import_contacts_pipeline.messages import (  # noqa: E402
    materialize_messages_review_people,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

BUILD = ROOT / "packs/ingestion/primitives/build_research_review_csv/build_research_review_csv.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_research_review_csv_lnk", BUILD)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FlattenRowLinkedInTests(unittest.TestCase):
    def test_csv_fields_includes_linkedin_url(self) -> None:
        build = _load_build_module()
        self.assertIn("linkedin_url", build.CSV_FIELDS)

    def test_flatten_row_carries_social_linkedin_url(self) -> None:
        build = _load_build_module()
        research_packet = {
            "person": {"full_name": "Jane Doe"},
            "social": {"linkedin_url": "https://www.linkedin.com/in/jane-doe"},
            "location": {"city": "San Francisco", "country": "United States"},
            "positions": [{"title": "Director", "company_name": "Roblox"}],
            "education": [],
        }
        row = build.flatten_row(
            "phone-1111111111",
            {"phone_e164": "+14155551111", "display_name": "Jane Doe", "total_messages": "100"},
            research_packet,
            None,
            {"bucket": "maybe", "short_reason": "", "identity_risk": "", "signals": []},
        )
        self.assertEqual(row["linkedin_url"], "https://www.linkedin.com/in/jane-doe")

    def test_flatten_row_empty_when_no_social(self) -> None:
        build = _load_build_module()
        row = build.flatten_row(
            "phone-2222222222",
            {"phone_e164": "+14155552222", "display_name": "Bob Smith"},
            {"person": {"full_name": "Bob Smith"}, "positions": []},
            None,
            {"bucket": "no"},
        )
        self.assertEqual(row["linkedin_url"], "")


class BuildToMaterializeTests(unittest.TestCase):
    """End-to-end: the real `build` writes linkedin_url, and a kept researched row
    then survives materialization while an excluded row is dropped."""

    def _write_artifact(self, research_dir: Path, handle: str, name: str, linkedin: str | None) -> None:
        d = research_dir / handle
        d.mkdir(parents=True, exist_ok=True)
        (d / "01_research_parallel.json").write_text(
            json.dumps(
                {
                    "person": {"full_name": name, "confidence": 0.95},
                    "social": {"linkedin_url": linkedin},
                    "location": {"city": "San Francisco", "country": "United States"},
                    "positions": [{"title": "Director", "company_name": "Roblox"}],
                    "education": [],
                }
            ),
            encoding="utf-8",
        )
        # Cached network review avoids any LLM/API-key requirement in build.
        (d / "03_network_review.json").write_text(
            json.dumps(
                {
                    "handle": handle,
                    "model": "openai/gpt-4.1",
                    "review": {"bucket": "maybe", "short_reason": "career signal", "identity_risk": "", "signals": ["career"]},
                }
            ),
            encoding="utf-8",
        )

    def test_build_writes_linkedin_url_and_row_materializes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            review = tmp / "research_review.csv"

            self._write_artifact(research_dir, "phone-1111111111", "Jane Doe", "https://www.linkedin.com/in/jane-doe")
            self._write_artifact(research_dir, "phone-3333333333", "Anita Kapadia", "https://www.linkedin.com/in/anita-kapadia")

            rq_headers = [
                "handle", "display_name", "first_name", "last_name", "phone_e164", "area_code",
                "total_messages", "imessage_message_count", "whatsapp_message_count", "message_source",
                "source_channel", "last_message", "imessage_last_message", "whatsapp_last_message",
                "group_names", "retarget_hint", "match_status", "match_confidence", "match_method", "match_reason",
            ]
            with queue.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=rq_headers)
                w.writeheader()
                for handle, name, phone in [
                    ("phone-1111111111", "Jane Doe", "+14155551111"),
                    ("phone-3333333333", "Anita Kapadia", "+14155553333"),
                ]:
                    w.writerow(
                        {k: "" for k in rq_headers}
                        | {
                            "handle": handle,
                            "display_name": name,
                            "phone_e164": phone,
                            "area_code": "415",
                            "source_channel": "phone",
                            "message_source": "imessage",
                            "imessage_message_count": "100",
                            "total_messages": "100",
                        }
                    )

            subprocess.run(
                ["python3", str(BUILD), "build", "--research-dir", str(research_dir),
                 "--queue-csv", str(queue), "--output-csv", str(review), "--allow-missing-queue"],
                cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
            )

            with review.open(newline="") as h:
                rows = list(CsvIO.dict_reader(h))
            self.assertIn("linkedin_url", rows[0])
            by_handle = {r["handle"]: r for r in rows}
            self.assertEqual(by_handle["phone-1111111111"]["linkedin_url"], "https://www.linkedin.com/in/jane-doe")

            # Reviewer excludes Anita; Jane is kept.
            for r in rows:
                if r["handle"] == "phone-3333333333":
                    r["exclude"] = "yes"
            with review.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

            people_csv = tmp / "people.csv"
            summary = materialize_messages_review_people(review, people_csv)

            self.assertEqual(summary["rows_written"], 1)
            self.assertGreaterEqual(summary["skipped"].get("rejected", 0), 1)
            with people_csv.open(newline="") as h:
                people = list(CsvIO.dict_reader(h))
            self.assertEqual(len(people), 1)
            self.assertEqual(people[0]["linkedin_url"], "https://www.linkedin.com/in/jane-doe")
            self.assertTrue(people[0]["public_identifier"])


if __name__ == "__main__":
    unittest.main()
