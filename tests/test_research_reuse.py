"""Paid deep-research is reused across parent-slug churn.

A research result directory is named for the caller's handle — the canonical
parent slug in the deep-context flow — and that slug changes whenever cluster
membership changes. These tests pin that already-paid work is still recognized
after such a change, because re-billing a completed person is the expensive
failure mode.

Created: 2026-07-27
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packs.ingestion.primitives.deep_context.deep_research_contacts import (  # noqa: E402
    RAW_FILENAME,
    RESULT_FILENAME,
    adopt_completed_research,
    completed_research_by_identity,
    filter_already_done,
    research_identity_key,
)


def write_result(out_dir: Path, handle: str, *, identifier: str) -> Path:
    person = out_dir / handle
    person.mkdir(parents=True, exist_ok=True)
    (person / RESULT_FILENAME).write_text(json.dumps({
        "person": {"full_name": "Jordan Bravo"},
        "social": {"linkedin_url": None, "primary_email": None, "primary_phone": None},
        "metadata": {"source_channel": "email", "source_identifier": identifier},
    }), encoding="utf-8")
    (person / RAW_FILENAME).write_text(json.dumps({"content": "raw"}), encoding="utf-8")
    return person


class ResearchIdentityKeyTests(unittest.TestCase):
    def test_email_and_phone_normalize_to_comparable_keys(self) -> None:
        self.assertEqual(research_identity_key(" Jordan@Example.COM "), "jordan@example.com")
        # A US number keys the same with or without its country code / formatting
        # — the same rule the dossier layer uses (common.phone_digits).
        self.assertEqual(research_identity_key("+1 (555) 010-0000"), "5550100000")
        self.assertEqual(research_identity_key("5550100000"), "5550100000")

    def test_unusable_identifiers_key_to_empty(self) -> None:
        # A slug fallback (source_identifier when a row had neither email nor
        # phone) must never become a reuse key — it is the mutable thing. Nor may
        # a too-short digit string, which could collide two different people.
        for value in ("", "   ", "jordan-bravo-parent15", "12345"):
            self.assertEqual(research_identity_key(value), "")


class AdoptCompletedResearchTests(unittest.TestCase):
    def test_a_renamed_parent_slug_does_not_re_bill(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_result(out, "jordan-bravo-parent15", identifier="jordan@example.com")
            # The cluster changed: same human, new parent slug, so the new handle
            # has no result directory of its own.
            queue = [{"handle": "jordan-bravo-15a813f2",
                      "primary_email": "jordan@example.com", "phone_e164": ""}]

            todo, skipped = filter_already_done(queue, out)

            self.assertEqual(todo, [], "a re-clustered person must not be re-submitted")
            self.assertEqual(skipped, 1)
            self.assertTrue((out / "jordan-bravo-15a813f2" / RESULT_FILENAME).is_file())
            # Copy, not move: the original stays put so older references resolve.
            self.assertTrue((out / "jordan-bravo-parent15" / RESULT_FILENAME).is_file())

    def test_phone_keyed_research_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_result(out, "casey-delta-parent01", identifier="+15550100")
            queue = [{"handle": "casey-delta-9f2b1c04",
                      "primary_email": "", "phone_e164": "+1 555 0100"}]

            todo, skipped = filter_already_done(queue, out)

            self.assertEqual((todo, skipped), ([], 1))

    def test_a_genuinely_new_person_is_still_queued(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_result(out, "jordan-bravo-parent15", identifier="jordan@example.com")
            queue = [{"handle": "robin-echo-77c1aa02",
                      "primary_email": "robin@example.com", "phone_e164": ""}]

            todo, skipped = filter_already_done(queue, out)

            self.assertEqual(skipped, 0)
            self.assertEqual([r["handle"] for r in todo], ["robin-echo-77c1aa02"])

    def test_adoption_is_idempotent_and_reports_only_real_work(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_result(out, "jordan-bravo-parent15", identifier="jordan@example.com")
            queue = [{"handle": "jordan-bravo-15a813f2",
                      "primary_email": "jordan@example.com", "phone_e164": ""}]

            self.assertEqual(adopt_completed_research(queue, out), 1)
            # Second run: already in place, nothing adopted, nothing rewritten.
            self.assertEqual(adopt_completed_research(queue, out), 0)

    def test_adopt_false_observes_raw_handle_state(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_result(out, "jordan-bravo-parent15", identifier="jordan@example.com")
            queue = [{"handle": "jordan-bravo-15a813f2",
                      "primary_email": "jordan@example.com", "phone_e164": ""}]

            todo, skipped = filter_already_done(queue, out, adopt=False)

            self.assertEqual(skipped, 0)
            self.assertEqual(len(todo), 1)
            self.assertFalse((out / "jordan-bravo-15a813f2").exists())

    def test_a_corrupt_result_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            broken = out / "broken-parent00"
            broken.mkdir(parents=True)
            (broken / RESULT_FILENAME).write_text("{not json", encoding="utf-8")
            write_result(out, "jordan-bravo-parent15", identifier="jordan@example.com")

            index = completed_research_by_identity(out)

            self.assertEqual(list(index), ["jordan@example.com"])


if __name__ == "__main__":
    unittest.main()
