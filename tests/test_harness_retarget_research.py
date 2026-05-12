import csv
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs/messages/primitives/harness_retarget_research/harness_retarget_research.py"
spec = importlib.util.spec_from_file_location("harness_retarget_research", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class HarnessRetargetResearchTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = mod.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_queue(self, path: Path):
        fields = ["handle", "display_name", "phone_e164", "total_messages", "retarget_hint", "retarget_source_handle"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "handle": "phone-1__retarget_abc",
                "display_name": "Jane Doe",
                "phone_e164": "+14155550101",
                "total_messages": "5",
                "retarget_hint": "Jane Doe at Acme",
                "retarget_source_handle": "phone-1",
            })

    def write_queue_rows(self, path: Path, count: int):
        fields = ["handle", "display_name", "phone_e164", "total_messages", "retarget_hint", "retarget_source_handle"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for idx in range(count):
                writer.writerow({
                    "handle": f"phone-{idx}__retarget_abc",
                    "display_name": f"Person {idx}",
                    "phone_e164": f"+1415555010{idx}",
                    "total_messages": "5",
                    "retarget_hint": f"Person {idx} at Acme",
                    "retarget_source_handle": f"phone-{idx}",
                })

    def test_prepare_writes_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "retarget_queue.csv"
            out = Path(tmp) / "research_retarget"
            prompts = Path(tmp) / "prompts"
            self.write_queue(queue)
            code, payload = self.invoke(["prepare", "--input", str(queue), "--output-dir", str(out), "--prompt-dir", str(prompts)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["rows"], 1)
            prompt_path = Path(payload["prompts"][0]["prompt"])
            text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Jane Doe at Acme", text)
            self.assertIn("Return ONLY valid JSON", text)

    def test_run_custom_command_writes_profile_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "retarget_queue.csv"
            out = Path(tmp) / "research_retarget"
            prompts = Path(tmp) / "prompts"
            fake = Path(tmp) / "fake_harness.py"
            self.write_queue(queue)
            fake.write_text(
                "import json\n"
                "print(json.dumps({\n"
                " 'person': {'full_name': 'Jane Doe', 'confidence': 0.9},\n"
                " 'social': {'linkedin_url': 'https://linkedin.test/jane'},\n"
                " 'summary': {'text': 'Founder at Acme'},\n"
                " 'metadata': {'research_notes': 'matched feedback'}\n"
                "}))\n",
                encoding="utf-8",
            )
            cmd = f"{sys.executable} {fake} {{prompt_path}}"
            code, payload = self.invoke([
                "run", "--input", str(queue), "--output-dir", str(out), "--prompt-dir", str(prompts),
                "--command-template", cmd,
            ])
            self.assertEqual(code, 0)
            artifact = out / "phone-1__retarget_abc" / "01_research_parallel.json"
            self.assertTrue(artifact.exists())
            profile = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(profile["person"]["full_name"], "Jane Doe")
            self.assertEqual(profile["metadata"]["research_method"], "harness-websearch")

    def test_run_custom_command_honors_max_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "retarget_queue.csv"
            out = Path(tmp) / "research_retarget"
            prompts = Path(tmp) / "prompts"
            fake = Path(tmp) / "fake_harness.py"
            self.write_queue_rows(queue, 4)
            fake.write_text(
                "import json, time\n"
                "time.sleep(0.3)\n"
                "print(json.dumps({\n"
                " 'person': {'full_name': 'Jane Doe', 'confidence': 0.9},\n"
                " 'social': {'linkedin_url': 'https://linkedin.test/jane'},\n"
                " 'summary': {'text': 'Founder at Acme'},\n"
                " 'metadata': {'research_notes': 'matched feedback'}\n"
                "}))\n",
                encoding="utf-8",
            )
            cmd = f"{sys.executable} {fake} {{prompt_path}}"
            started = time.monotonic()
            code, payload = self.invoke([
                "run", "--input", str(queue), "--output-dir", str(out), "--prompt-dir", str(prompts),
                "--command-template", cmd, "--max-workers", "4",
            ])
            elapsed = time.monotonic() - started
            self.assertEqual(code, 0)
            self.assertEqual(payload["processed"], 4)
            self.assertEqual(payload["max_workers"], 4)
            self.assertLess(elapsed, 0.9)


if __name__ == "__main__":
    unittest.main()
