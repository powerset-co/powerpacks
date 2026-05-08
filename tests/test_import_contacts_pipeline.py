import importlib.util
from contextlib import ExitStack
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
            "sync_research_cache": record("sync_research_cache"),
            "parallel_research": record("parallel_research"),
            "build_review_csv": record("build_review_csv"),
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
            self.assertIn("We're syncing WhatsApp", step["user_message"])
            self.assertIn("taking a bit longer", step["user_message"])
            self.assertNotIn("WAHA", step["user_message"])

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
            imessage.write_text("old imessage\n", encoding="utf-8")
            whatsapp.write_text("old whatsapp\n", encoding="utf-8")
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
            self.assertGreaterEqual(summary["moved_count"], 9)
            self.assertTrue((Path(summary["archive_dir"]) / "manifest.json").exists())

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
            with self.assertRaises(mod.PipelineBlocked) as ctx:
                mod.retarget_research_after_review(args, ledger_path, ledger)
            block = ctx.exception.payload
            self.assertEqual(block["status"], "blocked_approval")
            self.assertEqual(block["approval_type"], "parallel")
            self.assertIn("Feedback found; approve another deep research pass for $", block["message"])
            self.assertTrue(args.retarget_queue.exists())
            saved = mod.read_json(ledger_path)
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
            approval_payload = {
                "kind": "retarget_parallel",
                "estimated_usd": 0.05,
                "estimated_latency": {"rough_wall_clock": "about 10-15 min once submitted"},
                "processor": "core2x",
                "would_submit": 1,
                "feedback_rows": 1,
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
                {"returncode": 0, "json_objects": [run_payload]},
                {"returncode": 0, "json": {"status": "ok", "completed_marked": 1}},
            ]) as run_command:
                mod.retarget_research_after_review(args, ledger_path, ledger)
            self.assertEqual(run_command.call_count, 3)
            saved = mod.read_json(ledger_path)
            self.assertEqual(saved["steps"]["retarget_research"]["status"], "completed")

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
            self.assertNotIn("--bucket-mode", cmd)
            self.assertIn("--model", cmd)
            self.assertIn("openai/gpt-4.1", cmd)
            saved = mod.read_json(ledger_path)
            summary = saved["steps"]["build_research_review_csv"]["summary"]
            self.assertEqual(summary["scoring_estimate"]["candidates"], 1)
            self.assertIn("auto_approved_reason", summary)

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
