import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.discover_contacts_pipeline import messages as discover_messages
from packs.ingestion.primitives.discover_contacts_pipeline.common import write_csv_rows
from packs.ingestion.primitives.discover_contacts_pipeline.directory import DIRECTORY_COLUMNS
from packs.ingestion.primitives.import_contacts_pipeline import messages as import_messages
from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
INGESTION = ROOT / "packs/ingestion"


class IngestionMessagesContractTests(unittest.TestCase):
    def test_messages_source_tree_is_consolidated_under_ingestion(self) -> None:
        self.assertFalse((ROOT / "packs/messages").exists())

        expected = [
            "skills/import-messages/SKILL.md",
            "skills/import-whatsapp/SKILL.md",
            "schemas/contacts-csv.schema.json",
            "primitives/extract_imessage_contacts/extract_imessage_contacts.py",
            "primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
            "primitives/merge_message_contacts/merge_message_contacts.py",
            "primitives/match_local_candidates/match_local_candidates.py",
            "primitives/llm_review_contacts/llm_review_contacts.py",
            "primitives/prepare_research_queue/prepare_research_queue.py",
            "primitives/deep_research_contacts/deep_research_contacts.py",
            "primitives/build_research_review_csv/build_research_review_csv.py",
            "primitives/review_research_web/review_research_web.py",
            "primitives/import_contacts_pipeline/messages.py",
        ]
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertTrue((INGESTION / relative).is_file())

    def test_setup_gmail_and_messages_remain_distinct_skills(self) -> None:
        setup = (INGESTION / "skills/setup/SKILL.md").read_text(encoding="utf-8")
        gmail = (INGESTION / "skills/import-gmail/SKILL.md").read_text(encoding="utf-8")
        messages = (INGESTION / "skills/import-messages/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("LinkedIn-only", setup)
        self.assertIn("linkedin_modal_pipeline.py import-linkedin", setup)
        self.assertNotIn("discover_contacts_pipeline/messages.py discover", setup)
        self.assertNotIn("discover_contacts_pipeline/gmail.py discover", setup)

        self.assertIn("discover_contacts_pipeline/gmail.py discover", gmail)
        self.assertIn("import_contacts_pipeline/gmail.py run", gmail)
        self.assertNotIn("discover_contacts_pipeline/messages.py discover", gmail)

        self.assertIn("$import-messages", messages)
        self.assertIn("import_contacts_pipeline/messages.py run", messages)
        self.assertIn("index_contacts_pipeline.py fan-in", messages)
        self.assertIn("linkedin_modal_pipeline.py index-people", messages)
        self.assertNotIn("discover_contacts_pipeline/gmail.py discover", messages)

    def test_installers_source_message_skills_from_ingestion(self) -> None:
        expected = {
            "import-messages": "packs/ingestion/skills/import-messages/SKILL.md",
            "import-whatsapp": "packs/ingestion/skills/import-whatsapp/SKILL.md",
        }
        for relative in (
            "adapters/codex/install.sh",
            "adapters/claude-code/install.sh",
            "adapters/pi/install.sh",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for skill, source in expected.items():
                with self.subTest(installer=relative, skill=skill):
                    self.assertIn(f'install_skill {skill} "$REPO_ROOT/{source}"', text)

    def test_no_stale_messages_pack_source_paths(self) -> None:
        stale_source_path = "packs" + "/messages/"
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        offenders = []
        text_suffixes = {
            ".html", ".json", ".md", ".py", ".sh", ".toml", ".ts", ".tsx",
            ".yaml", ".yml",
        }
        for relative in result.stdout.splitlines():
            # Gitleaks scans deleted commits, so its allowlist must retain the
            # historical pack path even though no current source uses it.
            if relative == ".gitleaks.toml":
                continue
            path = ROOT / relative
            if not path.is_file() or path.suffix not in text_suffixes:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            content = content.replace(".powerpacks/messages/", "")
            if stale_source_path in content:
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_canonical_messages_route_has_no_powerset_or_upload_surface(self) -> None:
        canonical_files = [
            INGESTION / "skills/import-messages/SKILL.md",
            INGESTION / "primitives/discover_contacts_pipeline/messages.py",
            INGESTION / "primitives/import_contacts_pipeline/messages.py",
            INGESTION / "primitives/build_research_review_csv/build_research_review_csv.py",
        ]
        forbidden = [
            "--include-powerset-candidates",
            "--include-upload",
            "--confirm-upload",
            "primitives/powerset_contacts_harness",
            "primitives/sync_powerset_candidates",
            "primitives/sync_contact_datalake",
            "primitives/upload_research_review",
        ]
        for path in canonical_files:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.relative_to(ROOT), token=token):
                    self.assertNotIn(token, text)

        reviewer = (
            INGESTION / "primitives/review_research_web/review_research_web.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Saved: upload", reviewer)

    def test_messages_discovery_uses_fixed_outputs_and_one_stage_manifest(self) -> None:
        path = INGESTION / "primitives/discover_contacts_pipeline/messages.py"
        text = path.read_text(encoding="utf-8")

        self.assertIn('DEFAULT_MESSAGES_OUTPUT_DIR = DEFAULT_BASE_DIR / "discover" / "messages"', text)
        self.assertIn('MESSAGES_DIR = Path(".powerpacks/messages")', text)
        self.assertIn('manifest_json = DEFAULT_MESSAGES_OUTPUT_DIR / "manifest.json"', text)
        self.assertIn("write_stage_manifest(manifest_json", text)

        for token in (
            '"ledger.json"',
            "--ledger",
            "--import-ledger",
            "--output-dir",
            "run_id",
            "--run-id",
            "run_root",
            "uuid",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

        self.assertEqual(discover_messages.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES, 0)
        config = json.loads(
            (INGESTION / "primitives/discover_contacts_pipeline/discovery.config.json").read_text()
        )
        self.assertEqual(config["sources"]["whatsapp"]["inputs"]["max_messages"], 0)
        self.assertEqual(
            config["sources"]["imessage"]["outputs"]["contacts_csv"],
            ".powerpacks/messages/imessage.contacts.csv",
        )
        self.assertEqual(
            config["sources"]["imessage"]["outputs"]["manifest_json"],
            ".powerpacks/messages/imessage.manifest.json",
        )
        self.assertEqual(
            config["sources"]["whatsapp"]["outputs"]["contacts_csv"],
            ".powerpacks/messages/whatsapp.contacts.csv",
        )
        self.assertEqual(
            config["sources"]["whatsapp"]["outputs"]["manifest_json"],
            ".powerpacks/messages/whatsapp.contacts.csv.manifest.json",
        )
        self.assertEqual(
            config["sources"]["messages"]["outputs"]["contacts_csv"],
            ".powerpacks/network-import/discover/messages/contacts.csv",
        )

        for relative in (
            "primitives/extract_imessage_contacts/extract_imessage_contacts.py",
            "primitives/normalize_message_contacts/normalize_message_contacts.py",
        ):
            primitive_text = (INGESTION / relative).read_text(encoding="utf-8").lower()
            for token in ("run_id", "run-id", "uuid"):
                with self.subTest(relative=relative, token=token):
                    self.assertNotIn(token, primitive_text)

    def test_messages_import_is_fixed_output_and_stateless(self) -> None:
        path = INGESTION / "primitives/import_contacts_pipeline/messages.py"
        text = path.read_text(encoding="utf-8").lower()
        for token in (
            "ledger",
            "run_id",
            "run-id",
            "run_dir",
            "run-dir",
            "uuid",
            "begin_step",
            "mark_step",
            "save_ledger",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_app_messages_surface_is_harness_only(self) -> None:
        source_page = (ROOT / "app/src/local/LocalSourcePage.tsx").read_text()
        setup_route = (ROOT / "app/local-api/routes/setup.ts").read_text()
        commands = (ROOT / "app/local-api/lib/commands.ts").read_text()
        jobs = (ROOT / "app/local-api/jobs.ts").read_text()
        sources = (ROOT / "app/local-api/lib/sources.ts").read_text()

        self.assertIn("$import-messages", source_page)
        self.assertNotIn("MessagesSyncPanel", source_page)
        self.assertIn('runnable: id === "messages" || id === "twitter" ? false', setup_route)
        self.assertIn("Messages import is harness-only", setup_route)

        forbidden_runtime_paths = (
            "discover_contacts_pipeline/messages.py",
            "import_contacts_pipeline/messages.py",
            "import_whatsapp_wacli/import_whatsapp_wacli.py",
            "extract_imessage_contacts/extract_imessage_contacts.py",
        )
        for text in (setup_route, commands, jobs):
            for runtime_path in forbidden_runtime_paths:
                with self.subTest(runtime_path=runtime_path):
                    self.assertNotIn(runtime_path, text)

        for probe in ("chat.db", "wacli-login-qr", "messagesLinkStatus"):
            with self.subTest(probe=probe):
                self.assertNotIn(probe, sources)

        for relative in (
            "app/src/local/MessagesSyncPanel.tsx",
            "app/src/local/LocalMessagesReviewPage.tsx",
            "app/local-api/routes/messages.ts",
            "app/local-api/lib/messagesReview.ts",
        ):
            with self.subTest(relative=relative):
                self.assertFalse((ROOT / relative).exists())

    def test_whatsapp_discovery_passes_unbounded_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "whatsapp.csv"
            with mock.patch.object(discover_messages, "WHATSAPP_CONTACTS", missing), \
                    mock.patch.object(discover_messages, "run_cmd", return_value=(0, {"status": "completed"}, "")) as run_cmd:
                result = discover_messages._extract_whatsapp(
                    {},
                    Path(td) / "accounts.json",
                    discover_messages.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                    False,
                )
        self.assertIsNone(result)
        command = run_cmd.call_args.args[0]
        self.assertEqual(command[command.index("--max-messages") + 1], "0")
        self.assertIn("--no-install", command)

    def test_existing_channel_exports_are_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            imessage = root / "imessage.csv"
            whatsapp = root / "whatsapp.csv"
            imessage.write_text("phone,name\n+14155550101,Old iMessage\n", encoding="utf-8")
            whatsapp.write_text("phone,name\n+14155550102,Old WhatsApp\n", encoding="utf-8")

            with mock.patch.object(discover_messages, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(discover_messages, "run_cmd", side_effect=[
                        (0, {"status": "ok"}, ""),
                        (0, {"status": "completed"}, ""),
                    ]) as imessage_run:
                result = discover_messages._extract_imessage({}, root / "accounts.json", False)
            self.assertIsNone(result)
            self.assertEqual(imessage_run.call_count, 2)
            self.assertIn("extract", imessage_run.call_args_list[1].args[0])

            with mock.patch.object(discover_messages, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(discover_messages, "run_cmd", return_value=(0, {"status": "completed"}, "")) as whatsapp_run:
                result = discover_messages._extract_whatsapp(
                    {},
                    root / "accounts.json",
                    discover_messages.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                    False,
                )
            self.assertIsNone(result)
            self.assertEqual(whatsapp_run.call_count, 1)
            self.assertIn("run", whatsapp_run.call_args.args[0])

    def test_messages_discovery_merges_only_selected_channels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            imessage = root / "imessage.csv"
            whatsapp = root / "whatsapp.csv"
            merged = root / "contacts.csv"
            manifest = root / "contacts.manifest.json"
            imessage.write_text("phone,name\n+14155550101,Jane\n", encoding="utf-8")
            whatsapp.write_text("phone,name\n+14155550102,John\n", encoding="utf-8")
            with mock.patch.object(discover_messages, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(discover_messages, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS_MANIFEST", manifest), \
                    mock.patch.object(discover_messages, "run_cmd", return_value=(0, {"status": "ok"}, "")) as run_cmd:
                result = discover_messages._merge_contacts(
                    {},
                    include_imessage=True,
                    include_whatsapp=False,
                )

        self.assertIsNone(result)
        command = run_cmd.call_args.args[0]
        self.assertIn(str(imessage), command)
        self.assertNotIn(str(whatsapp), command)

    def test_refreshed_export_overwrites_downstream_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            imessage = root / "imessage.csv"
            merged = root / "contacts.csv"
            manifest = root / "contacts.manifest.json"
            with mock.patch.object(discover_messages, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(discover_messages, "WHATSAPP_CONTACTS", root / "missing.csv"), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS_MANIFEST", manifest):
                imessage.write_text(
                    "phone,name,source,message_count\n+14155550101,Old Name,imessage,1\n",
                    encoding="utf-8",
                )
                self.assertIsNone(discover_messages._merge_contacts(
                    {}, include_imessage=True, include_whatsapp=False
                ))
                self.assertIn("Old Name", merged.read_text(encoding="utf-8"))

                imessage.write_text(
                    "phone,name,source,message_count\n+14155550101,Fresh Name,imessage,2\n",
                    encoding="utf-8",
                )
                self.assertIsNone(discover_messages._merge_contacts(
                    {}, include_imessage=True, include_whatsapp=False
                ))
                merged_text = merged.read_text(encoding="utf-8")
                self.assertIn("Fresh Name", merged_text)
                self.assertNotIn("Old Name", merged_text)

    def test_messages_discovery_cli_exit_codes(self) -> None:
        cases = {
            "completed": 0,
            "skipped": 0,
            "blocked_user_action": 20,
            "blocked_approval": 20,
            "failed": 1,
        }
        for status, expected in cases.items():
            with self.subTest(status=status), \
                    mock.patch.object(discover_messages, "discover", return_value={"status": status}), \
                    mock.patch.object(sys, "argv", ["messages.py", "discover"]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(discover_messages.main(), expected)


class MessagesImportRuntimeTests(unittest.TestCase):
    REVIEW_FIELDS = [
        "bucket",
        "full_name",
        "phone_e164",
        "message_source",
        "imessage_message_count",
        "whatsapp_message_count",
        "last_message",
        "imessage_last_message",
        "whatsapp_last_message",
        "exclude",
        "approved",
        "enrich_decision",
        "in_network",
        "network_person_id",
        "network_name",
        "network_linkedin_url",
        "linkedin_url",
        "retarget_linkedin_url",
        "review_source",
        "top_title_company_pairs",
        "short_reason",
        "text",
        "body",
    ]

    @staticmethod
    def review_row(**overrides: str) -> dict[str, str]:
        row = {
            "bucket": "maybe",
            "full_name": "Jane Doe",
            "phone_e164": "+14155550123",
            "message_source": "imessage",
            "imessage_message_count": "87",
            "last_message": "2026-06-01T05:44:31+00:00",
            "imessage_last_message": "2026-06-01T05:44:31+00:00",
            "exclude": "",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "review_source": "deep_research",
            "top_title_company_pairs": "Finance Lead at Sail",
        }
        row.update(overrides)
        return row

    @contextmanager
    def sandbox(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            state = root / ".powerpacks" / "network-import"
            import_dir = state / "import"
            directory = state / "directory.csv"
            accounts = root / ".powerpacks" / "ingestion" / "accounts.json"
            accounts.parent.mkdir(parents=True, exist_ok=True)
            accounts.write_text("{}\n", encoding="utf-8")
            calls: list[Path] = []

            def fake_enrich(input_csv: Path, enrichment_dir: Path) -> dict[str, object]:
                calls.append(input_csv)
                enrichment_dir.mkdir(parents=True, exist_ok=True)
                output = enrichment_dir / "people.csv"
                shutil.copyfile(input_csv, output)
                return {
                    "status": "completed",
                    "people_csv": str(output),
                    "prepare": {"input_rows": self.csv_count(input_csv)},
                    "provider": {"processed": 0, "fetched": 0},
                    "merge": {"rows": self.csv_count(output)},
                    "artifacts": {"people_csv": str(output)},
                }

            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_BASE_DIR", state))
            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_IMPORT_DIR", import_dir))
            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_DIRECTORY_CSV", directory))
            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_PROFILE_CACHE_DIR", state / "profile_cache_v2"))
            stack.enter_context(mock.patch.object(import_messages, "enrich_messages_people", side_effect=fake_enrich))
            previous = Path.cwd()
            os.chdir(root)
            try:
                yield {
                    "root": root,
                    "state": state,
                    "import_dir": import_dir / "messages",
                    "directory": directory,
                    "accounts": accounts,
                    "review": root / ".powerpacks" / "messages" / "research_review.csv",
                    "queue": root / ".powerpacks" / "messages" / "research_queue.csv",
                    "calls": calls,
                }
            finally:
                os.chdir(previous)

    @staticmethod
    def csv_count(path: Path) -> int:
        with path.open(newline="", encoding="utf-8") as handle:
            return sum(1 for _ in CsvIO.dict_reader(handle))

    def write_review(self, env: dict[str, object], rows: list[dict[str, str]]) -> None:
        write_csv_rows(env["review"], self.REVIEW_FIELDS, rows)

    @staticmethod
    def write_queue(env: dict[str, object], rows: list[dict[str, str]]) -> None:
        write_csv_rows(env["queue"], import_messages.RESEARCH_COLUMNS, rows)

    @staticmethod
    def artifact_snapshot(env: dict[str, object]) -> dict[str, bytes]:
        paths = {
            "people_input": env["import_dir"] / "people.input.csv",
            "people": env["import_dir"] / "people.csv",
            "manifest": env["import_dir"] / "manifest.json",
            "review": env["review"],
            "directory": env["directory"],
        }
        return {name: path.read_bytes() for name, path in paths.items()}

    @staticmethod
    def run_import(env: dict[str, object], *, confirm: bool) -> dict[str, object]:
        return import_messages.run(
            SimpleNamespace(
                accounts=env["accounts"],
                operator_id="local",
                confirm_import=confirm,
            )
        )

    def test_block_confirm_refresh_noop_and_exclusion_lifecycle(self) -> None:
        sentinel = "SECRET MESSAGE BODY MUST NOT LEAK"
        with self.sandbox() as env:
            unrelated = {column: "" for column in DIRECTORY_COLUMNS}
            unrelated.update({
                "source": "gmail_msgvault",
                "source_key": "gmail:me@example.com:friend@example.com",
                "source_account": "me@example.com",
                "source_channels": "gmail_msgvault",
                "status": "found",
                "name": "Existing Friend",
                "linkedin_url": "https://www.linkedin.com/in/existing-friend",
                "public_identifier": "existing-friend",
                "confidence": "1.00",
            })
            write_csv_rows(env["directory"], DIRECTORY_COLUMNS, [unrelated])
            self.write_review(env, [self.review_row(text=sentinel, body=sentinel)])

            blocked = self.run_import(env, confirm=False)
            self.assertEqual(blocked["status"], "blocked_approval")
            self.assertEqual(env["calls"], [])
            self.assertFalse((env["import_dir"] / "people.csv").exists())
            self.assertEqual(
                [path.name for path in env["import_dir"].iterdir()],
                ["manifest.json"],
            )

            completed = self.run_import(env, confirm=True)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(len(env["calls"]), 1)
            people_csv = env["import_dir"] / "people.csv"
            self.assertEqual(self.csv_count(people_csv), 1)
            manifests = list(env["import_dir"].rglob("manifest.json"))
            self.assertEqual(manifests, [env["import_dir"] / "manifest.json"])
            self.assertEqual(list(env["import_dir"].rglob("*ledger*")), [])

            manifest = json.loads((env["import_dir"] / "manifest.json").read_text())
            self.assertNotIn(str(env["directory"]), json.dumps(manifest["fingerprints"]))
            self.assertEqual(manifest["directory"]["path"], str(env["directory"]))
            derived_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in [
                    env["import_dir"] / "people.input.csv",
                    people_csv,
                    env["import_dir"] / "manifest.json",
                    env["directory"],
                ]
            )
            self.assertNotIn(sentinel, derived_text)

            noop = self.run_import(env, confirm=False)
            self.assertTrue(noop["noop"])
            self.assertEqual(len(env["calls"]), 1)

            self.write_review(env, [self.review_row(
                top_title_company_pairs="VP Finance at Sail",
                imessage_message_count="120",
                last_message="2026-06-10T00:00:00+00:00",
                imessage_last_message="2026-06-10T00:00:00+00:00",
            )])
            refreshed = self.run_import(env, confirm=False)
            self.assertEqual(refreshed["status"], "completed")
            self.assertEqual(len(env["calls"]), 2)
            with people_csv.open(newline="", encoding="utf-8") as handle:
                person = next(CsvIO.dict_reader(handle))
            self.assertEqual(person["current_title"], "VP Finance")
            self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 120})

            self.write_review(env, [self.review_row(exclude="yes")])
            excluded = self.run_import(env, confirm=False)
            self.assertEqual(excluded["status"], "completed")
            self.assertEqual(self.csv_count(people_csv), 0)
            with env["directory"].open(newline="", encoding="utf-8") as handle:
                directory_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["source"] for row in directory_rows], ["gmail_msgvault"])

    def test_review_policy_merges_duplicates_and_honors_explicit_exclusion(self) -> None:
        sentinel = "DO NOT COPY THIS BODY"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            review = root / "review.csv"
            output = root / "people.csv"
            self.write_review(
                {"review": review},
                [
                    self.review_row(text=sentinel),
                    self.review_row(
                        phone_e164="+14155550999",
                        message_source="whatsapp",
                        imessage_message_count="40",
                        whatsapp_message_count="9",
                        last_message="2026-06-05T00:00:00+00:00",
                        whatsapp_last_message="2026-06-05T00:00:00+00:00",
                    ),
                    self.review_row(
                        phone_e164="+14155550888",
                        message_source="imessage",
                        imessage_message_count="12",
                    ),
                    self.review_row(
                        full_name="Rejected Person",
                        phone_e164="+14155550000",
                        linkedin_url="https://www.linkedin.com/in/rejected-person",
                        exclude="yes",
                    ),
                ],
            )
            summary = import_messages.materialize_messages_review_people(review, output)
            self.assertEqual(summary["eligible_rows"], 3)
            self.assertEqual(summary["rows_written"], 1)
            with output.open(newline="", encoding="utf-8") as handle:
                person = next(CsvIO.dict_reader(handle))
            self.assertEqual(
                json.loads(person["all_phones"]),
                ["+14155550123", "+14155550999", "+14155550888"],
            )
            self.assertEqual(person["source_channels"], "imessage,whatsapp")
            self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 87, "whatsapp": 9})
            self.assertEqual(person["last_interaction"], "2026-06-05T00:00:00+00:00")
            self.assertNotIn(sentinel, output.read_text())

    def test_empty_queue_reconciliation_clears_stale_messages_slice(self) -> None:
        with self.sandbox() as env:
            unrelated = {column: "" for column in DIRECTORY_COLUMNS}
            unrelated.update({
                "source": "gmail_msgvault",
                "source_key": "gmail:me@example.com:friend@example.com",
                "source_account": "me@example.com",
                "source_channels": "gmail_msgvault",
                "status": "found",
                "name": "Existing Friend",
                "linkedin_url": "https://www.linkedin.com/in/existing-friend",
                "public_identifier": "existing-friend",
                "confidence": "1.00",
            })
            write_csv_rows(env["directory"], DIRECTORY_COLUMNS, [unrelated])
            self.write_review(env, [self.review_row()])
            self.assertEqual(self.run_import(env, confirm=True)["status"], "completed")
            self.assertEqual(self.csv_count(env["import_dir"] / "people.csv"), 1)

            preserved = self.artifact_snapshot(env)
            missing = import_messages.reconcile_empty(
                SimpleNamespace(
                    accounts=env["accounts"], operator_id="local", queue=env["queue"]
                )
            )
            self.assertEqual(missing["reason"], "messages_research_queue_missing")
            self.assertEqual(self.artifact_snapshot(env), preserved)

            write_csv_rows(env["queue"], ["handle"], [])
            malformed = import_messages.reconcile_empty(
                SimpleNamespace(
                    accounts=env["accounts"], operator_id="local", queue=env["queue"]
                )
            )
            self.assertEqual(malformed["reason"], "messages_research_queue_schema_invalid")
            self.assertEqual(self.artifact_snapshot(env), preserved)

            self.write_queue(env, [{"handle": "phone-4155550123"}])
            non_empty = import_messages.reconcile_empty(
                SimpleNamespace(
                    accounts=env["accounts"], operator_id="local", queue=env["queue"]
                )
            )
            self.assertEqual(non_empty["reason"], "messages_research_queue_not_empty")
            self.assertEqual(self.artifact_snapshot(env), preserved)

            self.write_queue(env, [])
            cleared = import_messages.reconcile_empty(
                SimpleNamespace(
                    accounts=env["accounts"], operator_id="local", queue=env["queue"]
                )
            )
            self.assertEqual(cleared["status"], "completed")
            self.assertEqual(cleared["reason"], "empty_current_research_queue")
            self.assertEqual(self.csv_count(env["import_dir"] / "people.csv"), 0)
            self.assertEqual(self.csv_count(env["import_dir"] / "people.input.csv"), 0)
            self.assertFalse(env["review"].exists())
            self.assertEqual(len(env["calls"]), 1)
            with env["directory"].open(newline="", encoding="utf-8") as handle:
                directory_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["source"] for row in directory_rows], ["gmail_msgvault"])
            manifest = json.loads((env["import_dir"] / "manifest.json").read_text())
            self.assertEqual(manifest["input"]["research_queue_csv"], str(env["queue"]))
            self.assertIn(str(env["queue"]), manifest["fingerprints"]["input_artifacts"])

            self.write_review(env, [self.review_row(
                full_name="New Person",
                phone_e164="+14155550777",
                linkedin_url="https://www.linkedin.com/in/new-person",
            )])
            blocked = self.run_import(env, confirm=False)
            self.assertEqual(blocked["status"], "blocked_approval")
            self.assertFalse(blocked.get("noop", False))

    def test_cli_status_exit_codes(self) -> None:
        cases = [
            ({"status": "blocked_approval"}, 20),
            ({"status": "failed"}, 1),
            ({"status": "completed"}, 0),
        ]
        for payload, expected in cases:
            with self.subTest(status=payload["status"]), \
                    mock.patch.object(import_messages, "run", return_value=payload), \
                    mock.patch("sys.argv", ["messages.py", "run"]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(import_messages.main(), expected)


if __name__ == "__main__":
    unittest.main()
