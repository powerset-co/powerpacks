import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py"

spec = importlib.util.spec_from_file_location("import_contacts_pipeline", PRIMITIVE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


class ImportContactsPipelineTests(unittest.TestCase):
    def test_approval_id_is_stable_and_type_prefixed(self):
        payload = {"would_submit": 3, "estimated_usd": 0.15, "processor": "core2x"}
        a = mod.approval_id("parallel", payload)
        b = mod.approval_id("parallel", dict(reversed(list(payload.items()))))
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("parallel_"))

    def test_parse_multiple_json_objects(self):
        parsed = mod.parse_json_objects('{"command":"submit"}\nnoise\n{"command":"poll","status":"completed"}\n')
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[-1]["command"], "poll")

    def test_approve_current_block_records_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "import-run.json"
            approval_id = "parallel_abc123"
            mod.write_json(ledger, {
                "current_block": {
                    "approval_id": approval_id,
                    "approval_type": "parallel",
                    "payload": {"would_submit": 1, "estimated_usd": 0.05},
                },
                "steps": {},
                "approvals": {},
            })
            args = SimpleNamespace(ledger=ledger, kind="parallel", approval_id=None, confirm=True)
            rc = mod.cmd_approve(args)
            saved = mod.read_json(ledger)
        self.assertEqual(rc, 0)
        self.assertIsNone(saved["current_block"])
        self.assertTrue(saved["approvals"][approval_id]["confirmed"])

    def test_missing_contacts_blocks_user_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(contacts=Path(tmp) / "missing.csv")
            with self.assertRaises(mod.PipelineBlocked) as cm:
                mod.ensure_contacts(args, ledger_path, ledger)
            self.assertEqual(cm.exception.payload["status"], "blocked_user_action")


if __name__ == "__main__":
    unittest.main()
