"""The retired message-linkedin facts scrub drives its own removal condition."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from packs.ingestion.primitives.common.legacy import (
    MESSAGE_LINKEDIN_PREFIX,
    scrub_retired_message_linkedin_facts,
)


class ScrubRetiredMessageLinkedinFacts(unittest.TestCase):
    def test_removes_only_the_retired_prefix(self) -> None:
        with TemporaryDirectory() as tmp:
            facts = Path(tmp)
            durable = facts / "3929f3a9-a135-5066-b9d1-67756fa95bce.jsonl"
            parent = facts / "parent-b0acdab7512f.jsonl"
            retired = facts / f"{MESSAGE_LINKEDIN_PREFIX}95dc53092e056110.jsonl"
            for path in (durable, parent, retired):
                path.write_text('{"facts": {}}\n', encoding="utf-8")

            removed = scrub_retired_message_linkedin_facts(facts)

            self.assertEqual(removed, 1)
            self.assertFalse(retired.exists())
            self.assertTrue(durable.exists())
            self.assertTrue(parent.exists())

    def test_current_install_is_a_no_op(self) -> None:
        with TemporaryDirectory() as tmp:
            facts = Path(tmp)
            (facts / "parent-b0acdab7512f.jsonl").write_text("{}\n", encoding="utf-8")

            self.assertEqual(scrub_retired_message_linkedin_facts(facts), 0)
            self.assertEqual(scrub_retired_message_linkedin_facts(facts), 0)

    def test_missing_directory_is_tolerated(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(scrub_retired_message_linkedin_facts(Path(tmp) / "nope"), 0)
        self.assertEqual(scrub_retired_message_linkedin_facts(None), 0)


if __name__ == "__main__":
    unittest.main()
