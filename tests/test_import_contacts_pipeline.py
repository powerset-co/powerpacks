import csv
import hashlib
import io
import importlib.util
import json
import os
from contextlib import ExitStack, redirect_stderr
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
    def setUp(self):
        self._status_env = mock.patch.dict(os.environ, {"POWERPACKS_IMPORT_CONTACTS_STATUS": "0"})
        self._status_env.start()

    def tearDown(self):
        self._status_env.stop()

    def patch_pipeline_steps(self, calls: list[str], **overrides):
        stack = ExitStack()

        def record(name):
            def inner(*_args, **_kwargs):
                calls.append(name)
            return inner

        default_steps = {
            "extract_imessage": record("extract_imessage"),
            "extract_whatsapp": record("extract_whatsapp"),
            "ensure_contacts": record("ensure_contacts"),
            "sync_candidates": record("sync_candidates"),
            "match_contacts": record("match_contacts"),
            "llm_review": record("llm_review"),
            "prepare_queue": record("prepare_queue"),
            "parallel_research": record("parallel_research"),
            "build_review_csv": record("build_review_csv"),
            "migrate_review_schema": record("migrate_review_schema"),
            "retarget_research_after_review": record("retarget_research_after_review"),
            "normalize_channel": lambda *_args, **kwargs: calls.append(kwargs["step_id"]),
        }
        default_steps.update(overrides)
        for name, replacement in default_steps.items():
            stack.enter_context(mock.patch.object(mod, name, side_effect=replacement))
        return stack

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

    def test_whatsapp_step_status_uses_user_facing_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            mod.mark_step(ledger_path, ledger, "extract_whatsapp", "running")
            saved = mod.read_json(ledger_path)
            step = saved["steps"]["extract_whatsapp"]
            self.assertEqual(step["user_message"], "Syncing WhatsApp Messages and Contacts.")
            self.assertNotIn("WAHA", step["user_message"])

    def test_mark_step_emits_user_facing_status_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"POWERPACKS_IMPORT_CONTACTS_STATUS": "1"}), redirect_stderr(stderr):
                mod.mark_step(ledger_path, ledger, "llm_review", "running")
                mod.mark_step(ledger_path, ledger, "llm_review", "running")
            lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
            self.assertEqual(lines, ["[import-contacts] Reviewing contacts for enrichment."])
            saved = mod.read_json(ledger_path)
            self.assertEqual(
                saved["steps"]["llm_review"]["status_message"],
                "Reviewing contacts for enrichment.",
            )
            self.assertEqual(
                saved["status_events"],
                [{
                    "event_id": 1,
                    "message": "Reviewing contacts for enrichment.",
                    "recorded_at": saved["status_events"][0]["recorded_at"],
                    "status": "running",
                    "step_id": "llm_review",
                }],
            )

    def test_imessage_import_status_is_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"POWERPACKS_IMPORT_CONTACTS_STATUS": "1"}), redirect_stderr(stderr):
                mod.mark_step(ledger_path, ledger, "check_imessage", "running")
                mod.mark_step(ledger_path, ledger, "check_imessage", "completed")
                mod.mark_step(ledger_path, ledger, "extract_imessage", "running")
                mod.mark_step(ledger_path, ledger, "extract_imessage", "completed")
                mod.mark_step(ledger_path, ledger, "normalize_imessage", "running")
                mod.mark_step(ledger_path, ledger, "normalize_imessage", "completed")
            self.assertEqual(
                [line.strip() for line in stderr.getvalue().splitlines()],
                ["[import-contacts] Imported iMessage contacts."],
            )

    def test_run_command_emits_heartbeat_status_broadcast(self):
        cmd = [
            mod.sys.executable,
            "-c",
            "import json, time; time.sleep(0.16); print(json.dumps({'status': 'ok'}))",
        ]
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"POWERPACKS_IMPORT_CONTACTS_STATUS": "1"}), redirect_stderr(stderr):
            result = mod.run_command(
                cmd,
                timeout=2,
                env=os.environ.copy(),
                heartbeat_message="Still working.",
                heartbeat_interval=0.05,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["json"]["status"], "ok")
        self.assertIn("[import-contacts] Still working.", stderr.getvalue())

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
            args = SimpleNamespace(ledger=ledger, kind=None, approval_id=None, confirm=False)
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
        self.assertNotIn("--approval-id", command)
        self.assertNotIn("--confirm", command)
        self.assertNotIn("--ledger", command)
        self.assertIn(" approve && ", command)

    def test_parallel_approval_message_includes_cost_and_rough_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                ledger=ledger_path,
                research_queue=Path(tmp) / "research_queue.csv",
                research_dir=Path(tmp) / "research",
                processor="core2x",
                timeout=30,
                env_file=".env",
                rerun_parallel=False,
            )
            estimate = {
                "primitive": "deep_research_contacts",
                "command": "estimate",
                "would_submit": 3,
                "processor": "core2x",
                "estimated_usd": 0.15,
                "estimated_latency": {
                    "processor": "core2x",
                    "per_task": "60s-10min",
                    "rough_wall_clock": "about 10-15 min once submitted",
                },
            }
            with mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": estimate}):
                with self.assertRaises(mod.PipelineBlocked) as ctx:
                    mod.parallel_research(args, ledger_path, ledger)
            self.assertEqual(
                ctx.exception.payload["message"],
                "Estimated deep research cost: $0.1500, completion time is about 10-15 min once submitted. Approve?",
            )
            self.assertEqual(ctx.exception.payload["payload"]["estimated_latency"]["per_task"], "60s-10min")

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
                env_file=".env",
                rerun_upload=False,
            )
            with mock.patch.object(mod, "summarize_upload", return_value={
                "approved_count": 10,
                "skipped_unapproved_count": 12,
            }):
                with self.assertRaises(mod.PipelineBlocked) as cm:
                    mod.upload_review(args, ledger_path, ledger)
            block = cm.exception.payload
            self.assertEqual(block["message"], "Please approve upload of 10 approved contacts to Powerset.")
            self.assertNotIn("artifact", block["message"].lower())
            self.assertNotIn("maybe", block["message"])
            self.assertNotIn("no=", block["message"])
            self.assertEqual(set(block["payload"]), {"approved_count"})

    def test_upload_completion_user_message_hides_artifact_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=Path(tmp) / "review.csv",
                timeout=30,
                env_file=".env",
                rerun_upload=False,
            )
            summary = {"approved_count": 10}
            ledger["approvals"][mod.approval_id("upload", {"approved_count": 10})] = {"confirmed": True}
            with mock.patch.object(mod, "summarize_upload", return_value=summary):
                with mock.patch.object(mod, "run_command", return_value={
                    "returncode": 0,
                    "json": {
                        "status": "ok",
                        "approved_count": 10,
                        "response": {"artifact_id": "artifact-1", "approved_count": 10},
                    },
                }):
                    mod.upload_review(args, ledger_path, ledger)
            saved = mod.read_json(ledger_path)
            upload_summary = saved["steps"]["upload_research_review"]["summary"]
            self.assertEqual(upload_summary["user_message"], "Uploaded 10 contacts")
            self.assertNotIn("artifact-1", upload_summary["user_message"])
            self.assertNotIn("artifact", upload_summary["user_message"].lower())

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

    def test_empty_bootstrap_contacts_are_remerged_after_channel_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "contacts.csv"
            contacts.write_text(",".join(mod.CONTACT_CSV_HEADERS) + "\n", encoding="utf-8")
            mod.write_json(contacts.with_suffix(contacts.suffix + ".manifest.json"), {
                "primitive": "import_contacts_pipeline",
                "reason": "no_channel_contact_exports_found",
            })
            imessage = Path(tmp) / "imessage.contacts.csv"
            imessage.write_text("phone,name\n+15550000001,Ada\n", encoding="utf-8")
            args = SimpleNamespace(contacts=contacts, timeout=30, env_file=".env")
            merge_payload = {"primitive": "merge_message_contacts", "counts": {"rows_written": 1}}
            with mock.patch.object(mod, "DEFAULT_IMESSAGE_CONTACTS", imessage):
                with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", Path(tmp) / "missing-whatsapp.csv"):
                    with mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": merge_payload}) as run_command:
                        mod.ensure_contacts(args, ledger_path, ledger)
            self.assertEqual(run_command.call_count, 1)
            self.assertEqual(mod.read_json(ledger_path)["steps"]["ensure_contacts"]["summary"]["source"], "merged_channel_exports")

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

    def test_new_run_archives_previous_contact_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            contacts = Path(tmp) / "contacts.csv"
            candidates = Path(tmp) / "powerset_contacts.csv"
            research_queue = Path(tmp) / "research_queue.csv"
            review_csv = Path(tmp) / "research_review.csv"
            retarget_queue = Path(tmp) / "retarget_queue.csv"
            retarget_ledger = Path(tmp) / "retarget_attempts.json"
            for path in (ledger_path, contacts, candidates, research_queue, review_csv, retarget_queue, retarget_ledger):
                path.write_text("old\n", encoding="utf-8")

            imessage = Path(tmp) / "imessage.contacts.csv"
            whatsapp = Path(tmp) / "whatsapp.contacts.csv"
            whatsapp_count_cache = Path(tmp) / "whatsapp.message-count-cache.json"
            imessage.write_text("old imessage\n", encoding="utf-8")
            whatsapp.write_text("old whatsapp\n", encoding="utf-8")
            whatsapp_count_cache.write_text("{}\n", encoding="utf-8")
            default_paths = {
                "DEFAULT_IMESSAGE_CONTACTS": imessage,
                "DEFAULT_IMESSAGE_JSONL": Path(tmp) / "imessage.contacts.raw.jsonl",
                "DEFAULT_IMESSAGE_MANIFEST": Path(tmp) / "imessage.manifest.json",
                "DEFAULT_IMESSAGE_NORMALIZED": Path(tmp) / "imessage.contacts.normalized.jsonl",
                "DEFAULT_IMESSAGE_NORMALIZED_MANIFEST": Path(tmp) / "imessage.contacts.normalized.jsonl.manifest.json",
                "DEFAULT_WHATSAPP_CONTACTS": whatsapp,
                "DEFAULT_WHATSAPP_JSONL": Path(tmp) / "whatsapp.contacts.raw.jsonl",
                "DEFAULT_WHATSAPP_MANIFEST": Path(tmp) / "whatsapp.contacts.csv.manifest.json",
                "DEFAULT_WHATSAPP_NORMALIZED": Path(tmp) / "whatsapp.contacts.normalized.jsonl",
                "DEFAULT_WHATSAPP_NORMALIZED_MANIFEST": Path(tmp) / "whatsapp.contacts.normalized.jsonl.manifest.json",
                "DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE": whatsapp_count_cache,
                "ARCHIVE_ROOT": Path(tmp) / "archive",
            }

            args = SimpleNamespace(
                command="run",
                ledger=ledger_path,
                contacts=contacts,
                candidates=candidates,
                research_queue=research_queue,
                review_csv=review_csv,
                retarget_queue=retarget_queue,
                retarget_ledger=retarget_ledger,
            )
            with ExitStack() as stack:
                for attr, value in default_paths.items():
                    stack.enter_context(mock.patch.object(mod, attr, value))
                summary = mod.archive_existing_run_artifacts(args)

            self.assertIsNotNone(summary)
            self.assertFalse(contacts.exists())
            self.assertFalse(imessage.exists())
            self.assertFalse(whatsapp.exists())
            self.assertTrue(whatsapp_count_cache.exists())
            self.assertGreaterEqual(summary["moved_count"], 9)
            self.assertTrue((Path(summary["archive_dir"]) / "manifest.json").exists())
            previous_review = mod.archived_artifact(summary, review_csv)
            self.assertIsNotNone(previous_review)
            self.assertTrue(Path(previous_review).exists())

    def test_continue_does_not_archive_previous_contact_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            contacts = Path(tmp) / "contacts.csv"
            contacts.write_text("old\n", encoding="utf-8")
            args = SimpleNamespace(
                command="continue",
                ledger=Path(tmp) / "import-run.json",
                contacts=contacts,
                candidates=Path(tmp) / "powerset_contacts.csv",
                research_queue=Path(tmp) / "research_queue.csv",
                review_csv=Path(tmp) / "research_review.csv",
                retarget_queue=Path(tmp) / "retarget_queue.csv",
                retarget_ledger=Path(tmp) / "retarget_attempts.json",
            )
            self.assertIsNone(mod.archive_existing_run_artifacts(args))
            self.assertTrue(contacts.exists())

    def test_review_url_defaults_to_yes_tab(self):
        args = SimpleNamespace(review_host="127.0.0.1", review_port=8766)
        self.assertEqual(mod.review_url(args), "http://127.0.0.1:8766/?tab=yes")

    def test_review_server_match_requires_current_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_csv = Path(tmp) / "research_review.csv"
            other_csv = Path(tmp) / "old_review.csv"
            review_csv.write_text("bucket,handle\n", encoding="utf-8")
            other_csv.write_text("bucket,handle\n", encoding="utf-8")
            args = SimpleNamespace(review_host="127.0.0.1", review_port=8766, review_csv=review_csv)
            health = {
                "status": "ok",
                "csv": str(review_csv.resolve()),
                "source_sha256": mod.current_review_research_web_sha256(),
            }
            with mock.patch.object(mod, "read_review_server_health", return_value=health):
                self.assertTrue(mod.review_server_matches_current_csv(args))
            stale_csv_health = {**health, "csv": str(other_csv.resolve())}
            with mock.patch.object(mod, "read_review_server_health", return_value=stale_csv_health):
                self.assertFalse(mod.review_server_matches_current_csv(args))
            with mock.patch.object(mod, "read_review_server_health", return_value=None):
                self.assertFalse(mod.review_server_matches_current_csv(args))

    def test_extract_whatsapp_reuses_only_completed_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            ledger["steps"]["extract_whatsapp"] = {"id": "extract_whatsapp", "status": "completed"}
            ledger.setdefault("artifacts", {})["whatsapp_provider"] = "wacli"
            whatsapp_csv = tmp_path / "whatsapp.contacts.csv"
            whatsapp_count_cache = tmp_path / "whatsapp.message-count-cache.json"
            whatsapp_csv.write_text(
                ",".join(mod.CONTACT_CSV_HEADERS) + "\n"
                "+15550000001,Ada,whatsapp,false,,3,2026-01-01,,,,,,,,\n",
                encoding="utf-8",
            )
            whatsapp_count_cache.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(force_whatsapp=False, timeout=30, parallel_timeout=30, env_file=".env")
            with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", whatsapp_csv), \
                    mock.patch.object(mod, "DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE", whatsapp_count_cache), \
                    mock.patch.object(mod, "run_command") as run_command:
                mod.extract_whatsapp(args, ledger_path, ledger)

            run_command.assert_not_called()
            saved = mod.read_json(ledger_path)
            summary = saved["steps"]["extract_whatsapp"]["summary"]
            self.assertEqual(summary["reason"], "active_run_completed")

    def test_extract_whatsapp_does_not_reuse_legacy_provider_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            ledger["steps"]["extract_whatsapp"] = {"id": "extract_whatsapp", "status": "completed"}
            whatsapp_csv = tmp_path / "whatsapp.contacts.csv"
            whatsapp_jsonl = tmp_path / "whatsapp.contacts.jsonl"
            whatsapp_manifest = tmp_path / "whatsapp.contacts.csv.manifest.json"
            whatsapp_csv.write_text(
                ",".join(mod.CONTACT_CSV_HEADERS) + "\n"
                "+15550000001,Ada,whatsapp,false,,3,2026-01-01,,,,,,,,\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                force_whatsapp=False,
                whatsapp_provider="wacli",
                wacli_max_messages=0,
                wacli_max_group_participants=30,
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            payload = {
                "primitive": "import_whatsapp_wacli",
                "status": "completed",
                "counts": {"contacts": 2},
            }
            with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", whatsapp_csv), \
                    mock.patch.object(mod, "DEFAULT_WHATSAPP_JSONL", whatsapp_jsonl), \
                    mock.patch.object(mod, "DEFAULT_WHATSAPP_MANIFEST", whatsapp_manifest), \
                    mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": payload}) as run_command:
                mod.extract_whatsapp(args, ledger_path, ledger)

            run_command.assert_called_once()
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["artifacts"]["whatsapp_provider"], "wacli")
            self.assertEqual(saved["steps"]["extract_whatsapp"]["summary"], payload)

    def test_extract_whatsapp_uses_wacli_provider_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            whatsapp_csv = tmp_path / "whatsapp.contacts.csv"
            whatsapp_jsonl = tmp_path / "whatsapp.contacts.jsonl"
            whatsapp_manifest = tmp_path / "whatsapp.contacts.csv.manifest.json"
            args = SimpleNamespace(
                force_whatsapp=True,
                whatsapp_provider="wacli",
                wacli_max_messages=0,
                wacli_max_group_participants=30,
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            payload = {
                "primitive": "import_whatsapp_wacli",
                "status": "completed",
                "counts": {"contacts": 2},
            }
            with mock.patch.object(mod, "DEFAULT_WHATSAPP_CONTACTS", whatsapp_csv), \
                    mock.patch.object(mod, "DEFAULT_WHATSAPP_JSONL", whatsapp_jsonl), \
                    mock.patch.object(mod, "DEFAULT_WHATSAPP_MANIFEST", whatsapp_manifest), \
                    mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": payload}) as run_command:
                mod.extract_whatsapp(args, ledger_path, ledger)

            cmd = run_command.call_args.args[0]
            self.assertIn("import_whatsapp_wacli.py", cmd[1])
            self.assertIn("--output-csv", cmd)
            self.assertIn(str(whatsapp_csv), cmd)
            self.assertIn("--max-messages", cmd)
            self.assertEqual(cmd[cmd.index("--max-messages") + 1], "0")
            self.assertIn("--max-group-participants", cmd)
            self.assertIn("30", cmd)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["artifacts"]["whatsapp_provider"], "wacli")
            self.assertEqual(saved["steps"]["extract_whatsapp"]["summary"], payload)

    def test_extract_whatsapp_preserves_wacli_qr_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                force_whatsapp=True,
                whatsapp_provider="wacli",
                wacli_max_messages=0,
                wacli_max_group_participants=30,
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            payload = {
                "primitive": "import_whatsapp_wacli",
                "status": "blocked_user_action",
                "message": "WhatsApp needs a QR scan.",
                "qr_page": ".powerpacks/messages/wacli-login-qr.html",
            }
            with mock.patch.object(mod, "run_command", return_value={"returncode": 21, "json": payload}):
                with self.assertRaises(mod.PipelineBlocked):
                    mod.extract_whatsapp(args, ledger_path, ledger)

            saved = mod.read_json(ledger_path)
            block = saved["current_block"]
            self.assertEqual(block["whatsapp_provider"], "wacli")
            self.assertEqual(block["qr_page"], ".powerpacks/messages/wacli-login-qr.html")
            self.assertNotIn("qr_path", block)
            self.assertEqual(saved["steps"]["extract_whatsapp"]["status"], "blocked_user_action")

    def test_extract_whatsapp_rejects_non_wacli_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(
                force_whatsapp=True,
                whatsapp_provider="waha",
                wacli_max_messages=0,
                wacli_max_group_participants=30,
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            with mock.patch.object(mod, "run_command") as run_command:
                with self.assertRaises(mod.PipelineFailed) as ctx:
                    mod.extract_whatsapp(args, ledger_path, ledger)

            run_command.assert_not_called()
            self.assertIn("wacli is the only supported provider", str(ctx.exception))

    def test_import_contacts_task_uses_wacli_browser_qr_flow(self):
        task = json.loads((ROOT / "packs/messages/tasks/import-contacts.task.json").read_text(encoding="utf-8"))
        steps = task["steps"]
        whatsapp_step = next(step for step in steps if step["id"] == "extract_whatsapp")
        self.assertEqual(whatsapp_step["primitive"], "import_whatsapp_wacli")
        self.assertIn("browser QR page", whatsapp_step["user_action"])
        self.assertFalse(any(str(step.get("primitive", "")).startswith("waha") for step in steps))

    def test_existing_contacts_with_legacy_queue_schema_fails_actionably(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            contacts = Path(tmp) / "contacts.csv"
            contacts.write_text("handle,display_name,phone_e164,total_messages\nphone-1,Ada Lovelace,+15550000001,5\n", encoding="utf-8")
            args = SimpleNamespace(contacts=contacts)
            with self.assertRaises(mod.PipelineFailed) as ctx:
                mod.ensure_contacts(args, ledger_path, ledger)
            msg = str(ctx.exception)
            self.assertIn("Please convert this file", msg)
            self.assertIn("packs/messages/schemas/contacts-csv.md", msg)
            self.assertIn("phone_e164/phone_number -> phone", msg)
            self.assertEqual(mod.read_json(ledger_path)["steps"]["ensure_contacts"]["status"], "failed")

    def test_retarget_hints_block_before_upload_with_parallel_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            review_csv = tmp_path / "research_review.csv"
            review_csv.write_text(
                "bucket,handle,full_name,phone_e164,area_code,total_messages,message_source,group_names,top_title_company_pairs,retarget_hint\n"
                "yes,phone-1,Ada Lovelace,+15550000001,555,5,phone,,Engineer @ Example,LinkedIn: https://linkedin.test/ada\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=review_csv,
                research_queue=tmp_path / "research_queue.csv",
                retarget_queue=tmp_path / "retarget_queue.csv",
                retarget_ledger=tmp_path / "retarget_attempts.json",
                retarget_research_dir=tmp_path / "research_retarget",
                processor="core2x",
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            with mock.patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "", "RAPIDAPI_KEY": ""}, clear=False), \
                    self.assertRaises(mod.PipelineBlocked) as ctx:
                mod.retarget_research_after_review(args, ledger_path, ledger)
            block = ctx.exception.payload
            self.assertEqual(block["status"], "blocked_approval")
            self.assertEqual(block["approval_type"], "parallel")
            self.assertEqual(
                block["message"],
                "Feedback found; approve another re-research pass? Completion time is up to 10-15 min.",
            )
            self.assertTrue(args.retarget_queue.exists())
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["prepare_retarget_queue"]["status"], "completed")
            self.assertEqual(saved["steps"]["retarget_research"]["status"], "blocked_approval")

    def test_exact_linkedin_retarget_uses_single_reresearch_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            review_csv = tmp_path / "research_review.csv"
            review_csv.write_text(
                "bucket,handle,full_name,phone_e164,retarget_hint\n"
                "yes,phone-1,Charles Lin,+15550000001,https://www.linkedin.com/in/charles-lin/\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=review_csv,
                research_queue=tmp_path / "research_queue.csv",
                retarget_queue=tmp_path / "retarget_queue.csv",
                retarget_ledger=tmp_path / "retarget_attempts.json",
                retarget_research_dir=tmp_path / "research_retarget",
                processor="core2x",
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            estimate = {
                "primitive": "refresh_retarget_linkedin_profiles",
                "command": "estimate",
                "status": "ok",
                "api_key_present": True,
                "would_fetch": 1,
                "counts": {"with_linkedin_url": 1, "would_fetch": 1},
            }
            prepare = {
                "primitive": "prepare_retarget_queue",
                "command": "prepare",
                "status": "ok",
                "rows_written": 1,
                "counts": {"queued": 1},
            }
            parallel_estimate = {
                "primitive": "deep_research_contacts",
                "command": "estimate",
                "would_submit": 1,
                "processor": "core2x",
                "estimated_usd": 0.05,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
            }
            with mock.patch.object(mod, "run_command", side_effect=[
                {"returncode": 0, "json": estimate},
                {"returncode": 0, "json": prepare},
                {"returncode": 0, "json": parallel_estimate},
            ]) as run_command:
                with self.assertRaises(mod.PipelineBlocked) as ctx:
                    mod.retarget_research_after_review(args, ledger_path, ledger)

            block = ctx.exception.payload
            self.assertEqual(block["approval_type"], "parallel")
            self.assertEqual(
                block["message"],
                "Feedback found; approve another re-research pass? Completion time is up to 10-15 min.",
            )
            self.assertEqual(block["payload"]["rapidapi_would_fetch"], 1)
            self.assertEqual(run_command.call_count, 3)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["retarget_rapidapi_estimate"]["status"], "completed")
            self.assertEqual(saved["steps"]["prepare_retarget_queue"]["status"], "completed")
            self.assertEqual(saved["steps"]["retarget_research"]["status"], "blocked_approval")

    def test_retarget_approval_continue_reuses_prepared_feedback_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            retarget_queue = tmp_path / "retarget_queue.csv"
            retarget_queue.write_text("handle\nphone-1__retarget_abc\n", encoding="utf-8")
            ledger = mod.load_ledger(ledger_path)
            ledger["steps"]["prepare_retarget_queue"] = {
                "id": "prepare_retarget_queue",
                "status": "completed",
                "summary": {"rows_written": 1, "counts": {"queued": 1}},
            }
            ledger["steps"]["retarget_research"] = {"id": "retarget_research", "status": "blocked_approval"}
            ledger["steps"]["retarget_rapidapi_estimate"] = {
                "id": "retarget_rapidapi_estimate",
                "status": "completed",
                "summary": {"api_key_present": False, "would_fetch": 0},
            }
            approval_payload = {
                "kind": "retarget_research",
                "estimated_usd": 0.05,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
                "processor": "core2x",
                "would_submit": 1,
                "feedback_rows": 1,
                "rapidapi_would_fetch": 0,
            }
            ledger["approvals"][mod.approval_id("parallel", approval_payload)] = {"confirmed": True}
            mod.save_ledger(ledger_path, ledger)
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=tmp_path / "research_review.csv",
                research_queue=tmp_path / "research_queue.csv",
                retarget_queue=retarget_queue,
                retarget_ledger=tmp_path / "retarget_attempts.json",
                retarget_research_dir=tmp_path / "research_retarget",
                processor="core2x",
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )
            estimate = {
                "primitive": "deep_research_contacts",
                "command": "estimate",
                "would_submit": 1,
                "processor": "core2x",
                "estimated_usd": 0.05,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
            }
            run_payload = {"primitive": "deep_research_contacts", "command": "poll", "status": "completed"}
            with mock.patch.object(mod, "run_command", side_effect=[
                {"returncode": 0, "json": estimate},
                {"returncode": 0, "json": estimate},
                {"returncode": 0, "json_objects": [run_payload]},
                {"returncode": 0, "json": {"status": "ok", "completed_marked": 1}},
            ]) as run_command:
                mod.retarget_research_after_review(args, ledger_path, ledger)
            self.assertEqual(run_command.call_count, 4)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["retarget_research"]["status"], "completed")

    def test_retarget_continue_uses_harness_for_remaining_rows_after_rapidapi_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            retarget_queue = tmp_path / "retarget_queue.csv"
            retarget_queue.write_text("handle\nphone-1__retarget_abc\nphone-2__retarget_def\n", encoding="utf-8")
            ledger = mod.load_ledger(ledger_path)
            ledger["steps"]["prepare_retarget_queue"] = {
                "id": "prepare_retarget_queue",
                "status": "completed",
                "summary": {"rows_written": 2, "counts": {"queued": 2}},
            }
            ledger["steps"]["retarget_research"] = {"id": "retarget_research", "status": "blocked_approval"}
            ledger["steps"]["retarget_rapidapi_estimate"] = {
                "id": "retarget_rapidapi_estimate",
                "status": "completed",
                "summary": {"api_key_present": True, "would_fetch": 1},
            }
            approval_payload = {
                "kind": "retarget_research",
                "estimated_usd": 0.10,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
                "processor": "core2x",
                "would_submit": 2,
                "feedback_rows": 2,
                "rapidapi_would_fetch": 1,
            }
            ledger["approvals"][mod.approval_id("parallel", approval_payload)] = {"confirmed": True}
            mod.save_ledger(ledger_path, ledger)
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=tmp_path / "research_review.csv",
                research_queue=tmp_path / "research_queue.csv",
                retarget_queue=retarget_queue,
                retarget_ledger=tmp_path / "retarget_attempts.json",
                retarget_research_dir=tmp_path / "research_retarget",
                processor="core2x",
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
                retarget_harness="auto",
                retarget_harness_threshold=100,
                retarget_harness_timeout=30,
                retarget_harness_max_workers=10,
                retarget_harness_prompt_dir=tmp_path / "prompts",
            )
            pre_estimate = {
                "primitive": "deep_research_contacts",
                "command": "estimate",
                "would_submit": 2,
                "processor": "core2x",
                "estimated_usd": 0.10,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
            }
            refresh = {"primitive": "refresh_retarget_linkedin_profiles", "status": "ok", "refreshed": 1}
            after_refresh_estimate = {
                "primitive": "deep_research_contacts",
                "command": "estimate",
                "would_submit": 1,
                "processor": "core2x",
                "estimated_usd": 0.05,
            }
            harness = {"primitive": "harness_retarget_research", "status": "ok", "processed": 1, "failed": 0}
            mark = {"status": "ok", "completed_marked": 1, "review_rows_merged": 1}
            with mock.patch.object(mod, "run_command", side_effect=[
                {"returncode": 0, "json": pre_estimate},
                {"returncode": 0, "json": refresh},
                {"returncode": 0, "json": after_refresh_estimate},
                {"returncode": 0, "json": harness},
                {"returncode": 0, "json": mark},
            ]) as run_command:
                mod.retarget_research_after_review(args, ledger_path, ledger)

            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertTrue(any(any(str(part).endswith("harness_retarget_research.py") for part in command) for command in commands))
            self.assertFalse(any(
                any(str(part).endswith("deep_research_contacts.py") for part in command) and "run" in command
                for command in commands
            ))
            saved = mod.read_json(ledger_path)
            summary = saved["steps"]["retarget_research"]["summary"]
            self.assertEqual(summary["mode"], "harness")
            self.assertEqual(summary["estimate"]["would_submit"], 1)
            messages = [event["message"] for event in saved.get("status_events", [])]
            self.assertEqual(messages.count("Researching again on review feedback."), 1)
            self.assertEqual(messages[-1], "Review completed.")

    def test_cached_retarget_results_are_merged_without_rerunning_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            review_csv = tmp_path / "research_review.csv"
            hint = "Jane Doe at Acme"
            review_csv.write_text(
                "bucket,handle,full_name,phone_e164,total_messages,retarget_hint\n"
                f"yes,phone-1,Jane Doe,+14155550101,5,{hint}\n",
                encoding="utf-8",
            )
            h = hashlib.sha256(hint.lower().encode("utf-8")).hexdigest()[:16]
            queue_handle = f"phone-1__retarget_{h[:10]}"
            retarget_research_dir = tmp_path / "research_retarget"
            profile_dir = retarget_research_dir / queue_handle
            profile_dir.mkdir(parents=True)
            (profile_dir / "01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jane Acme", "confidence": 0.91},
                "social": {"linkedin_url": "https://linkedin.test/jane-acme"},
                "positions": [{"title": "Founder", "company_name": "Acme"}],
                "metadata": {"research_notes": "cached retarget"},
            }), encoding="utf-8")
            args = SimpleNamespace(
                ledger=ledger_path,
                review_csv=review_csv,
                research_queue=tmp_path / "research_queue.csv",
                retarget_queue=tmp_path / "retarget_queue.csv",
                retarget_ledger=tmp_path / "retarget_attempts.json",
                retarget_research_dir=retarget_research_dir,
                processor="core2x",
                timeout=30,
                parallel_timeout=60,
                env_file=".env",
            )

            mod.retarget_research_after_review(args, ledger_path, ledger)

            saved = mod.read_json(ledger_path)
            summary = saved["steps"]["retarget_research"]["summary"]
            self.assertEqual(summary["status"], "cached_results_merged")
            self.assertEqual(summary["mark_completed"]["review_rows_merged"], 1)
            with review_csv.open(newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["retarget_status"], "re_researched")
            self.assertEqual(row["retarget_linkedin_url"], "https://linkedin.test/jane-acme")

    def test_build_review_creates_empty_review_csv_without_research_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            review_csv = Path(tmp) / "research_review.csv"
            args = SimpleNamespace(
                research_dir=Path(tmp) / "missing-research",
                research_queue=Path(tmp) / "research_queue.csv",
                review_csv=review_csv,
                force_build_review=False,
                timeout=30,
                env_file=".env",
            )
            with mock.patch.object(mod, "run_command") as run_command:
                mod.build_review_csv(args, ledger_path, ledger)
            run_command.assert_not_called()
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["build_research_review_csv"]["status"], "skipped")
            self.assertEqual(saved["steps"]["build_research_review_csv"]["summary"]["reason"], "no_research_packets")
            self.assertTrue(review_csv.exists())
            self.assertIn("top_title_company_pairs", review_csv.read_text(encoding="utf-8").splitlines()[0])

    def test_build_review_runs_network_review_by_default_and_auto_approves_small_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            research_dir = tmp_path / "research"
            handle_dir = research_dir / "phone-1"
            handle_dir.mkdir(parents=True)
            (handle_dir / "01_research_parallel.json").write_text("{}", encoding="utf-8")
            args = SimpleNamespace(
                ledger=ledger_path,
                research_dir=research_dir,
                research_queue=tmp_path / "research_queue.csv",
                review_csv=tmp_path / "research_review.csv",
                force_build_review=False,
                timeout=30,
                env_file=".env",
                llm_auto_approve_usd=10.0,
            )
            payload = {
                "primitive": "build_research_review_csv",
                "command": "build",
                "status": "ok",
                "rows_written": 1,
                "counts": {"scored_via_llm": 1, "network_review_written": 1},
            }
            with mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": payload}) as run_command:
                mod.build_review_csv(args, ledger_path, ledger)

            cmd = run_command.call_args.args[0]
            self.assertIn("--model", cmd)
            self.assertIn("openai/gpt-4.1", cmd)
            saved = mod.read_json(ledger_path)
            summary = saved["steps"]["build_research_review_csv"]["summary"]
            self.assertEqual(summary["scoring_estimate"]["candidates"], 1)
            self.assertIn("auto_approved_reason", summary)

    def test_build_review_passes_archived_previous_review_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            research_dir = tmp_path / "research"
            handle_dir = research_dir / "phone-1"
            handle_dir.mkdir(parents=True)
            (handle_dir / "01_research_parallel.json").write_text("{}", encoding="utf-8")
            previous_review = tmp_path / "archive" / "research_review.csv"
            previous_review.parent.mkdir()
            previous_review.write_text(
                "bucket,handle,phone_e164,exclude,enrich_decision,retarget_hint\n"
                "medium,phone-1,+14155550101,no,yes,https://linkedin.test/rina\n",
                encoding="utf-8",
            )
            ledger.setdefault("artifacts", {})["previous_research_review_csv"] = str(previous_review)
            args = SimpleNamespace(
                ledger=ledger_path,
                research_dir=research_dir,
                research_queue=tmp_path / "research_queue.csv",
                review_csv=tmp_path / "research_review.csv",
                force_build_review=False,
                timeout=30,
                env_file=".env",
                llm_auto_approve_usd=10.0,
            )
            payload = {
                "primitive": "build_research_review_csv",
                "command": "build",
                "status": "ok",
                "rows_written": 1,
                "counts": {"scored_via_network_review": 1, "previous_review_decisions_applied": 1},
            }
            with mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": payload}) as run_command:
                mod.build_review_csv(args, ledger_path, ledger)

            cmd = run_command.call_args.args[0]
            self.assertIn("--previous-csv", cmd)
            self.assertIn(str(previous_review), cmd)

    def test_build_review_falls_back_to_latest_archived_review_with_human_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            research_dir = tmp_path / "research"
            (research_dir / "phone-1").mkdir(parents=True)
            ((research_dir / "phone-1") / "01_research_parallel.json").write_text("{}", encoding="utf-8")
            archive_root = tmp_path / "archive"
            old_review = archive_root / "messages-old" / "research_review.csv"
            old_review.parent.mkdir(parents=True)
            old_review.write_text(
                "bucket,handle,phone_e164,exclude,enrich_decision,retarget_hint\n"
                "medium,phone-1,+14155550101,no,yes,https://linkedin.test/rina\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                ledger=ledger_path,
                research_dir=research_dir,
                research_queue=tmp_path / "research_queue.csv",
                review_csv=tmp_path / "research_review.csv",
                force_build_review=False,
                timeout=30,
                env_file=".env",
                llm_auto_approve_usd=10.0,
            )
            payload = {
                "primitive": "build_research_review_csv",
                "command": "build",
                "status": "ok",
                "rows_written": 1,
                "counts": {"scored_via_network_review": 1, "previous_review_decisions_applied": 1},
            }
            with mock.patch.object(mod, "ARCHIVE_ROOT", archive_root), \
                 mock.patch.object(mod, "run_command", return_value={"returncode": 0, "json": payload}) as run_command:
                mod.build_review_csv(args, ledger_path, ledger)

            cmd = run_command.call_args.args[0]
            self.assertIn("--previous-csv", cmd)
            self.assertIn(str(old_review), cmd)

    def test_continue_reapplies_archived_review_state_to_existing_review_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            review_csv = tmp_path / "research_review.csv"
            old_review = tmp_path / "archive" / "research_review.csv"
            old_review.parent.mkdir(parents=True)
            review_csv.write_text(
                "bucket,handle,phone_e164,retarget_hint\n"
                "medium,phone-1,+14155550101,\n",
                encoding="utf-8",
            )
            old_review.write_text(
                "bucket,handle,phone_e164,exclude,enrich_decision,retarget_hint\n"
                "medium,phone-1,+14155550101,no,yes,https://linkedin.test/rina\n",
                encoding="utf-8",
            )
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(review_csv=review_csv, force_build_review=False)
            with mock.patch.object(mod, "ARCHIVE_ROOT", tmp_path / "archive"):
                mod.reapply_previous_review_state(args, ledger_path, ledger)

            with review_csv.open(newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["exclude"], "no")
            self.assertEqual(row["enrich_decision"], "yes")
            self.assertEqual(row["retarget_hint"], "https://linkedin.test/rina")
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["reapply_previous_review_state"]["summary"]["decisions_applied"], 1)
            self.assertEqual(saved["steps"]["reapply_previous_review_state"]["summary"]["feedback_applied"], 1)

    def test_continue_scans_all_prior_review_runs_and_latest_decision_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger_path = tmp_path / "import-run.json"
            review_csv = tmp_path / "research_review.csv"
            archive_root = tmp_path / "archive"
            older = archive_root / "messages-old" / "research_review.csv"
            newer = archive_root / "messages-new" / "research_review.csv"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            review_csv.write_text(
                "bucket,handle,phone_e164\n"
                "medium,phone-1,+14155550101\n"
                "medium,phone-2,+14155550202\n",
                encoding="utf-8",
            )
            older.write_text(
                "bucket,handle,phone_e164,exclude,retarget_hint\n"
                "medium,phone-1,+14155550101,yes,old no\n"
                "medium,phone-2,+14155550202,no,only old\n",
                encoding="utf-8",
            )
            newer.write_text(
                "bucket,handle,phone_e164,exclude,retarget_hint\n"
                "medium,phone-1,+14155550101,no,new yes\n",
                encoding="utf-8",
            )
            os.utime(older, (1_700_000_000, 1_700_000_000))
            os.utime(newer, (1_700_000_100, 1_700_000_100))
            ledger = mod.load_ledger(ledger_path)
            args = SimpleNamespace(review_csv=review_csv, force_build_review=False)
            with mock.patch.object(mod, "archived_review_candidates", return_value=[newer, older]):
                mod.reapply_previous_review_state(args, ledger_path, ledger)

            with review_csv.open(newline="", encoding="utf-8-sig") as handle:
                rows = {row["handle"]: row for row in csv.DictReader(handle)}
            self.assertEqual(rows["phone-1"]["exclude"], "no")
            self.assertEqual(rows["phone-1"]["retarget_hint"], "new yes")
            self.assertEqual(rows["phone-2"]["exclude"], "no")
            self.assertEqual(rows["phone-2"]["retarget_hint"], "only old")
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["reapply_previous_review_state"]["summary"]["previous_run_count"], 2)
            self.assertEqual(saved["steps"]["reapply_previous_review_state"]["summary"]["matched_rows"], 2)

    def test_run_pipeline_extracts_channels_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            args = SimpleNamespace(
                ledger=ledger_path,
                processor="core2x",
                contacts=Path(tmp) / "contacts.csv",
                candidates=Path(tmp) / "powerset_contacts.csv",
                research_queue=Path(tmp) / "research_queue.csv",
                research_dir=Path(tmp) / "research",
                review_csv=Path(tmp) / "research_review.csv",
                model="anthropic/claude-sonnet-4-6",
                no_open_review=False,
                stop_before_upload=False,
                review_host="127.0.0.1",
                review_port=8766,
                force_imessage=False,
                force_whatsapp=False,
            )
            calls = []

            with self.patch_pipeline_steps(calls):
                with mock.patch.object(mod, "has_research_review", return_value=False):
                    with mock.patch.object(mod, "open_raw_contacts_review_server", side_effect=lambda *_args, **_kwargs: calls.append("open_raw_contacts_review_server")):
                        with self.assertRaises(mod.PipelineBlocked):
                            mod.run_pipeline(args)
            self.assertEqual(calls[:5], ["extract_imessage", "normalize_imessage", "extract_whatsapp", "normalize_whatsapp", "ensure_contacts"])
            self.assertIn("open_raw_contacts_review_server", calls)

    def test_run_pipeline_include_flags_run_only_selected_contact_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            args = SimpleNamespace(
                ledger=ledger_path,
                processor="core2x",
                contacts=Path(tmp) / "contacts.csv",
                candidates=Path(tmp) / "powerset_contacts.csv",
                research_queue=Path(tmp) / "research_queue.csv",
                research_dir=Path(tmp) / "research",
                review_csv=Path(tmp) / "research_review.csv",
                model="anthropic/claude-sonnet-4-6",
                no_open_review=True,
                stop_before_upload=False,
                review_host="127.0.0.1",
                review_port=8766,
                force_imessage=False,
                force_whatsapp=False,
                include_imessage=True,
                include_whatsapp=True,
                include_contact_merge=True,
            )
            calls = []

            with self.patch_pipeline_steps(calls):
                payload = mod.run_pipeline(args)

            self.assertEqual(payload["status"], "selected_steps_completed")
            self.assertEqual(calls, ["extract_imessage", "normalize_imessage", "extract_whatsapp", "normalize_whatsapp", "ensure_contacts"])
            self.assertFalse(payload["privacy"]["ran_powerset_sync"])
            self.assertFalse(payload["privacy"]["ran_research"])
            self.assertFalse(payload["privacy"]["uploaded"])

    def test_raw_review_continue_builds_review_csv_then_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            contacts = Path(tmp) / "contacts.csv"
            contacts.write_text(
                ",".join(mod.CONTACT_CSV_HEADERS + ["enrich_decision"]) + "\n"
                "+15550000001,Ada Lovelace,imessage,false,,3,2026-01-01,false,,,,,,,,yes\n",
                encoding="utf-8",
            )
            ledger = mod.load_ledger(ledger_path)
            ledger["steps"]["review_contacts_web_fallback"] = {"id": "review_contacts_web_fallback", "status": "completed"}
            mod.save_ledger(ledger_path, ledger)
            args = SimpleNamespace(
                ledger=ledger_path,
                processor="core2x",
                contacts=contacts,
                candidates=Path(tmp) / "powerset_contacts.csv",
                research_queue=Path(tmp) / "research_queue.csv",
                research_dir=Path(tmp) / "research",
                review_csv=Path(tmp) / "research_review.csv",
                model="anthropic/claude-sonnet-4-6",
                no_open_review=False,
                stop_before_upload=False,
                review_host="127.0.0.1",
                review_port=8766,
                force_imessage=False,
                force_whatsapp=False,
                force_build_review=False,
            )
            calls = []

            with self.patch_pipeline_steps(
                calls,
                upload_review=lambda *_args, **_kwargs: calls.append("upload_review"),
                sync_contact_datalake=lambda *_args, **_kwargs: calls.append("sync_contact_datalake"),
            ):
                with mock.patch.object(mod, "has_research_review", return_value=False):
                    mod.run_pipeline(args)
            self.assertIn("upload_review", calls)
            self.assertIn("sync_contact_datalake", calls)
            self.assertTrue(args.review_csv.exists())
            review_text = args.review_csv.read_text(encoding="utf-8")
            self.assertIn("top_title_company_pairs", review_text.splitlines()[0])
            self.assertIn("Ada Lovelace", review_text)

    def test_raw_review_upload_resume_does_not_overwrite_review_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "import-run.json"
            ledger = mod.load_ledger(ledger_path)
            review_csv = Path(tmp) / "research_review.csv"
            review_csv.write_text(
                ",".join(mod.REVIEW_CSV_HEADERS) + "\n"
                "yes,phone-5550000001,Ada Lovelace,+15550000001,555,3,imessage,,,,,,,,Raw contact review fallback,,,,false,yes\n",
                encoding="utf-8",
            )
            ledger["steps"]["build_raw_review_csv"] = {"id": "build_raw_review_csv", "status": "completed"}
            mod.save_ledger(ledger_path, ledger)
            args = SimpleNamespace(
                research_dir=Path(tmp) / "research",
                research_queue=Path(tmp) / "research_queue.csv",
                review_csv=review_csv,
                force_build_review=False,
                timeout=30,
                env_file=".env",
            )
            with mock.patch.object(mod, "run_command") as run_command:
                mod.build_review_csv(args, ledger_path, ledger)
            run_command.assert_not_called()
            self.assertEqual(mod.csv_data_rows(review_csv), 1)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["build_research_review_csv"]["summary"]["reason"], "raw_review_csv_active")


if __name__ == "__main__":
    unittest.main()
