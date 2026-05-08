import importlib.util
import tempfile
import unittest
from unittest import mock
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

    def test_llm_auto_approve_default_is_ten_dollars(self):
        self.assertEqual(mod.DEFAULT_LLM_AUTO_APPROVE_USD, 10.0)

    def test_approval_command_uses_uv_run(self):
        args = SimpleNamespace(ledger=Path(".powerpacks/messages/import-run.json"))
        command = mod.approval_command(args, "parallel", "parallel_abc123")
        self.assertIn("uv run --project . python", command)
        self.assertNotIn("&& python ", command)

    def test_under_ten_dollar_llm_estimate_runs_without_approval_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            contacts = Path(tmp) / "contacts.csv"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                contacts=contacts,
                model="anthropic/claude-sonnet-4-6",
                timeout=30,
                env_file=".env",
                llm_auto_approve_usd=10.0,
                llm_batch_size=20,
                llm_max_workers=4,
                rerun_llm=False,
            )
            estimate = {
                "primitive": "llm_review_contacts",
                "command": "estimate",
                "candidates": 1,
                "estimate": {"estimated_usd": 0.25},
            }
            review = {
                "primitive": "llm_review_contacts",
                "command": "review",
                "status": "completed",
                "counts": {"verdicts": 1},
            }
            with mock.patch.object(mod, "run_command", side_effect=[
                {"returncode": 0, "json": estimate},
                {"returncode": 0, "json": review},
            ]) as run_command:
                mod.llm_review(args, ledger_path, ledger)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["llm_review"]["status"], "completed")
            self.assertIn("auto_approved_reason", saved["steps"]["llm_review"]["summary"])
            self.assertEqual(run_command.call_count, 2)
            review_cmd = run_command.call_args_list[1].args[0]
            self.assertIn("--batch-size", review_cmd)
            self.assertIn("20", review_cmd)
            self.assertIn("--max-workers", review_cmd)
            self.assertIn("4", review_cmd)

    def test_upload_block_message_only_shows_upload_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=Path(tmp) / "review.csv",
                timeout=30,
                rerun_upload=False,
            )
            with mock.patch.object(mod, "summarize_upload", return_value={
                "row_count": 99,
                "yes_count": 10,
                "maybe_count": 11,
                "no_count": 1,
                "explicit_include_count": 10,
                "explicit_exclude_count": 1,
            }):
                with self.assertRaises(mod.PipelineBlocked) as cm:
                    mod.upload_review(args, ledger_path, ledger)
            block = cm.exception.payload
            self.assertIn("uploading 10", block["message"])
            self.assertNotIn("maybe", block["message"])
            self.assertNotIn("no=", block["message"])
            self.assertEqual(set(block["payload"]), {"upload_count"})

    def test_missing_contacts_bootstraps_empty_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "missing.csv"
            args = SimpleNamespace(contacts=contacts)
            with mock.patch.object(mod, "DEFAULT_IMESSAGE_CONTACTS", Path(tmp) / "missing-imessage.csv"):
                with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", Path(tmp) / "missing-whatsapp.csv"):
                    mod.ensure_contacts(args, ledger_path, ledger)
            saved = mod.read_json(ledger_path)
            self.assertTrue(contacts.exists())
            self.assertEqual(contacts.read_text(encoding="utf-8").splitlines()[0].split(","), mod.CONTACT_CSV_HEADERS)
            self.assertEqual(saved["steps"]["ensure_contacts"]["status"], "completed")
            self.assertEqual(saved["steps"]["ensure_contacts"]["summary"]["source"], "empty_bootstrap")

    def test_missing_contacts_merges_existing_channel_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "contacts.csv"
            imessage = Path(tmp) / "imessage.contacts.csv"
            whatsapp = Path(tmp) / "whatsapp.contacts.csv"
            imessage.write_text("phone,name\n+15550000001,Ada\n", encoding="utf-8")
            whatsapp.write_text("phone,name\n+15550000002,Grace\n", encoding="utf-8")
            args = SimpleNamespace(contacts=contacts, timeout=30, env_file=".env")
            merge_payload = {"primitive": "merge_message_contacts", "counts": {"rows_written": 2}}
            with mock.patch.object(mod, "DEFAULT_IMESSAGE_CONTACTS", imessage):
                with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", whatsapp):
                    with mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": merge_payload}) as run_command:
                        mod.ensure_contacts(args, ledger_path, ledger)
            saved = mod.read_json(ledger_path)
            cmd = run_command.call_args.args[0]
            self.assertEqual(cmd.count("--input"), 2)
            self.assertIn(str(imessage), cmd)
            self.assertIn(str(whatsapp), cmd)
            self.assertEqual(saved["steps"]["ensure_contacts"]["status"], "completed")
            self.assertEqual(saved["steps"]["ensure_contacts"]["summary"]["source"], "merged_channel_exports")
            self.assertEqual(saved["artifacts"]["contacts_csv"], str(contacts))

    def test_missing_contacts_merge_failure_fails_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "contacts.csv"
            imessage = Path(tmp) / "imessage.contacts.csv"
            imessage.write_text("phone,name\n+15550000001,Ada\n", encoding="utf-8")
            args = SimpleNamespace(contacts=contacts, timeout=30, env_file=".env")
            with mock.patch.object(mod, "DEFAULT_IMESSAGE_CONTACTS", imessage):
                with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", Path(tmp) / "missing-whatsapp.csv"):
                    with mock.patch.object(mod, "run_command", return_value={"returncode": 1, "stderr": "merge failed", "stdout": ""}):
                        with self.assertRaises(mod.PipelineFailed):
                            mod.ensure_contacts(args, ledger_path, ledger)

    def test_existing_contacts_completes_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "contacts.csv"
            contacts.write_text(",".join(mod.CONTACT_CSV_HEADERS) + "\n", encoding="utf-8")
            args = SimpleNamespace(contacts=contacts)
            with mock.patch.object(mod, "run_command") as run_command:
                mod.ensure_contacts(args, ledger_path, ledger)
            run_command.assert_not_called()
            self.assertEqual(mod.read_json(ledger_path)["steps"]["ensure_contacts"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
