"""Legacy Parallel resolutions migrate into pending retarget proposals.

The regression these lock in: a legacy web-researched LinkedIn link (attached
with no judge) must enter the NEW review format — a `retarget` row in
overrides/review.csv — where the standard judge/queue machinery can finally
audit it, while people already admitted to merged/people.csv, user-decided
rows, and factless people are left alone. All data here is synthetic.
"""
import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from packs.ingestion.primitives.deep_context import migrate_legacy_resolutions as mig


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def gmail_person(pid: str, pub: str, name: str) -> dict[str, str]:
    return {
        "id": pid, "public_identifier": pub,
        "linkedin_url": f"https://www.linkedin.com/in/{pub}",
        "full_name": name, "enrichment_provider": "parallel_linkedin_resolution",
    }


class MigrateLegacyResolutionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.gmail = base / "gmail-people.csv"
        self.merged = base / "merged-people.csv"
        self.directory = base / "directory.csv"
        self.overrides = base / "review.csv"
        self.facts = base / "facts"
        self.raw = base / "raw"
        self.cache = base / "cache"
        for d in (self.facts, self.raw, self.cache):
            d.mkdir()

        write_csv(self.gmail, [
            gmail_person("uuid-eligible", "jordan-bravo", "Jordan Bravo"),
            gmail_person("uuid-in-merged", "casey-delta", "Casey Delta"),
            gmail_person("uuid-no-facts", "avery-stone", "Avery Stone"),
        ])
        write_csv(self.merged, [{"id": "uuid-in-merged"}])
        write_csv(self.directory, [{
            "public_identifier": "jordan-bravo", "status": "found",
            "confidence": "0.87", "reasoning": "legacy web research matched name+email",
            "email": "jordan@example.com",
        }])
        for pid in ("uuid-eligible", "uuid-in-merged"):
            (self.facts / f"{pid}.jsonl").write_text(json.dumps({
                "chunk_index": 0,
                "facts": {"canonical_name": "Jordan Bravo", "employers": [{"name": "Bravado Labs"}],
                          "relationship_to_owner": "professional contact", "topics": ["intros"],
                          "identifiers": ["jordan@example.com"]},
            }) + "\n", encoding="utf-8")
        (self.cache / "jordan-bravo.json").write_text(json.dumps({
            "normalized_profile": {"success": True, "full_name": "Jordan Bravo",
                                   "headline": "Partner at Bravado Labs",
                                   "location_str": "Austin, Texas",
                                   "experiences": [{"title": "Partner", "company": "Bravado Labs"}],
                                   "education": []},
            "simple_summary": "Jordan Bravo is a partner at Bravado Labs.",
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *extra: str) -> dict:
        buf = StringIO()
        with redirect_stdout(buf):
            code = mig.main([
                "--gmail-people", str(self.gmail), "--merged-people", str(self.merged),
                "--directory-csv", str(self.directory), "--overrides", str(self.overrides),
                "--facts-dir", str(self.facts), "--raw-dir", str(self.raw),
                "--cache-dir", str(self.cache), *extra,
            ])
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def read_overrides(self) -> dict[str, dict[str, str]]:
        if not self.overrides.exists():
            return {}
        with self.overrides.open(newline="", encoding="utf-8") as fh:
            return {r["public_identifier"]: r for r in csv.DictReader(fh)}

    def test_dry_run_counts_and_writes_nothing(self):
        out = self.invoke()
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(out["legacy_rows"], 3)
        self.assertEqual(out["eligible"], 1)
        self.assertEqual(out["skipped_in_merged"], 1)
        self.assertEqual(out["skipped_no_facts"], 1)
        self.assertFalse(self.overrides.exists())

    def test_apply_writes_pending_retarget_with_legacy_provenance(self):
        out = self.invoke("--apply")
        self.assertEqual(out["proposed"], 1)
        row = self.read_overrides()["jordan-bravo"]
        self.assertEqual(row["action"], "retarget")
        self.assertEqual(row["approved"], "")
        self.assertEqual(row["new_linkedin_url"], "https://www.linkedin.com/in/jordan-bravo")
        self.assertEqual(row["person_id"], "uuid-eligible")
        self.assertEqual(row["source"], "legacy-migration")
        self.assertIn("legacy conf 0.87", row["reason"])
        self.assertEqual(row["match_emails"], "jordan@example.com")
        # pending = unjudged: no verdict, no fingerprint, so a later judge pass owns it
        self.assertEqual(row["llm_reject"], "")
        self.assertEqual(row["llm_judge_fingerprint"], "")

    def test_apply_is_idempotent_and_preserves_user_decisions(self):
        self.invoke("--apply")
        rows = self.read_overrides()
        rows["jordan-bravo"]["approved"] = "yes"
        write_csv(self.overrides, list(rows.values()))
        out = self.invoke("--apply")
        self.assertEqual(out["skipped_user_decided"], 1)
        self.assertEqual(out["proposals"], 0)
        self.assertEqual(self.read_overrides()["jordan-bravo"]["approved"], "yes")

    def test_judge_no_llm_stamps_verdict_and_fingerprint(self):
        out = self.invoke("--apply", "--judge", "--no-llm")
        self.assertEqual(out["judged"], 1)
        row = self.read_overrides()["jordan-bravo"]
        self.assertNotEqual(row["llm_judge_fingerprint"], "")
        # deterministic judge never auto-confirms an unverified guess -> rejection recorded
        self.assertEqual(row["llm_reject"], "yes")
        self.assertNotEqual(row["llm_reject_reason"], "")

    def test_cache_profile_view_requires_success(self):
        self.assertEqual(mig.cache_profile_view({"normalized_profile": {"success": False}}), {})
        view = mig.cache_profile_view(json.loads((self.cache / "jordan-bravo.json").read_text()))
        self.assertEqual(view["name"], "Jordan Bravo")
        self.assertEqual(view["positions"], ["Partner — Bravado Labs"])


if __name__ == "__main__":
    unittest.main()
