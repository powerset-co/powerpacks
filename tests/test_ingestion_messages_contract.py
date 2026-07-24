import io
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.discover.common import write_csv_rows
from packs.ingestion.primitives.imports.directory import DIRECTORY_COLUMNS
from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
INGESTION = ROOT / "packs/ingestion"
discover_messages = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.discover"
)
# The channel classes and their owned path constants live in channels/; module
# globals patched below (run_cmd, IMESSAGE_*/WHATSAPP_*, wacli timeouts) must be
# patched on the concrete channel module the channel reads them from, not on the
# discover module.
i_message_channel = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.channels.i_message_channel"
)
whats_app_channel = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.channels.whats_app_channel"
)
import_messages = importlib.import_module(
    "packs.ingestion.primitives.imports.messages.importer"
)


class IngestionMessagesContractTests(unittest.TestCase):
    def test_messages_source_tree_is_consolidated_under_ingestion(self) -> None:
        self.assertFalse((ROOT / "packs/messages").exists())

        expected = [
            "skills/import-messages/SKILL.md",
            "schemas/contacts-csv.schema.json",
            "schemas/candidates_schema.py",
            "primitives/discover/messages/extract_imessage.py",
            "primitives/discover/messages/whatsapp_wacli.py",
            "primitives/discover/messages/merge_contacts.py",
            "primitives/imports/messages/match_local_candidates.py",
            "primitives/deep_context/deep_research_contacts.py",
            "primitives/imports/messages/importer.py",
            "primitives/imports/status.py",
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
        self.assertNotIn("discover/messages/discover.py discover", setup)
        self.assertNotIn("discover/gmail/discover.py discover", setup)
        self.assertIn("imports/status.py status", setup)

        self.assertIn("discover/gmail/discover.py discover", gmail)
        self.assertIn("imports/gmail/importer.py run", gmail)
        self.assertNotIn("discover/messages/discover.py discover", gmail)
        self.assertIn("imports/status.py status", gmail)

        self.assertIn("$import-messages", messages)
        self.assertIn("imports/messages/importer.py run", messages)
        self.assertIn("index_contacts_pipeline.py fan-in", messages)
        self.assertIn("imports/status.py status", messages)
        # Pre-full-sync link is surfaced as an explicit re-link prompt wired to
        # the logout primitive, keyed off the hoisted top-level nudge flag.
        self.assertIn("whatsapp_pairing_state", messages)
        self.assertIn("discover/messages/whatsapp_wacli.py logout", messages)
        self.assertNotIn("discover/gmail/discover.py discover", messages)

        # Contact-sync boundary: the import skills never index and never run
        # the retired in-skill research/review flow — $deep-context owns both.
        for skill_name, text in (("gmail", gmail), ("messages", messages)):
            with self.subTest(skill=skill_name):
                self.assertNotIn("linkedin_modal_pipeline.py index-people", text)
                self.assertNotIn("validate_search_index", text)
        for retired in (
            "llm_review_contacts",
            "prepare_research_queue",
            "deep_research_contacts",
            "build_research_review_csv",
            "review_research_web",
            "reconcile-empty",
            "--approve-parallel-spend",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, messages)
                self.assertNotIn(retired, gmail)

    def test_installers_source_message_skills_from_ingestion(self) -> None:
        expected = {
            "import-messages": "packs/ingestion/skills/import-messages/SKILL.md",
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
            INGESTION / "primitives/discover/messages/discover.py",
            INGESTION / "primitives/imports/messages/importer.py",
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


    def test_messages_discovery_uses_fixed_outputs_and_one_stage_manifest(self) -> None:
        path = INGESTION / "primitives/discover/messages/discover.py"
        text = path.read_text(encoding="utf-8")

        self.assertIn('DEFAULT_MESSAGES_OUTPUT_DIR = discover_source_dir("messages")', text)
        self.assertIn("MESSAGES_DIR = MESSAGES_OUT_DIR", text)
        self.assertEqual(discover_messages.MESSAGES_DIR, Path(".powerpacks/messages"))
        self.assertIn('self.manifest_json = self.out_dir / "manifest.json"', text)
        self.assertIn("write_stage_manifest(self.manifest_json", text)

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
        # The message output paths are owned by the discover-messages primitive
        # now, not discovery.config.json — its imessage/whatsapp/messages source
        # blocks were pruned (only the gmail block + top-level accounts_json are
        # consumed). The top-level config keys the primitives still read stay.
        config = json.loads(
            (INGESTION / "primitives/discover/discovery.config.json").read_text()
        )
        self.assertEqual(config["accounts_json"], ".powerpacks/ingestion/accounts.json")
        self.assertNotIn("imessage", config["sources"])
        self.assertNotIn("whatsapp", config["sources"])
        self.assertNotIn("messages", config["sources"])
        self.assertEqual(str(i_message_channel.IMESSAGE_CONTACTS), ".powerpacks/messages/imessage.contacts.csv")
        self.assertEqual(str(i_message_channel.IMESSAGE_MANIFEST), ".powerpacks/messages/imessage.manifest.json")
        self.assertEqual(str(whats_app_channel.WHATSAPP_CONTACTS), ".powerpacks/messages/whatsapp.contacts.csv")
        self.assertEqual(str(whats_app_channel.WHATSAPP_MANIFEST), ".powerpacks/messages/whatsapp.contacts.csv.manifest.json")
        self.assertEqual(
            str(discover_messages.DEFAULT_MESSAGES_OUTPUT_DIR / "contacts.csv"),
            ".powerpacks/network-import/discover/messages/contacts.csv",
        )

        for relative in (
            "primitives/discover/messages/extract_imessage.py",
            "primitives/discover/messages/normalize_contacts.py",
        ):
            primitive_text = (INGESTION / relative).read_text(encoding="utf-8").lower()
            for token in ("run_id", "run-id", "uuid"):
                with self.subTest(relative=relative, token=token):
                    self.assertNotIn(token, primitive_text)

    def test_discover_lets_whatsapp_primitive_choose_sync_strategy(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run_cmd(command, timeout=None):
            captured["command"] = command
            captured["timeout"] = timeout
            return (0, {}, "")

        with mock.patch.object(whats_app_channel, "run_cmd", fake_run_cmd):
            channel = whats_app_channel.WhatsAppChannel(
                accounts_path=Path("accounts.json"),
                other_enabled=False,
                max_messages=0,
            )
            result = channel.extract()

        self.assertIsNone(result)
        cmd = captured["command"]
        self.assertNotIn("--sync-mode", cmd)
        self.assertEqual(
            captured["timeout"],
            whats_app_channel.DEFAULT_WACLI_SYNC_TIMEOUT
            + whats_app_channel.DEFAULT_WACLI_DEPTH_TIMEOUT
            + 900,
        )

        parser = discover_messages.build_parser()
        options = parser.parse_args(["discover", "--include-whatsapp"])
        self.assertFalse(hasattr(options, "wacli_sync_mode"))

    def test_messages_import_is_fixed_output_and_stateless(self) -> None:
        path = INGESTION / "primitives/imports/messages/importer.py"
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
        # Contacts-direct: the importer never touches the retired research/
        # review artifacts and never calls providers.
        for token in (
            "research_review",
            "research_queue",
            "enrich_people",
            "rapidapi",
            "parallel",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_whatsapp_discovery_passes_unbounded_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "whatsapp.csv"
            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", missing), \
                    mock.patch.object(whats_app_channel, "run_cmd", return_value=(0, {"status": "completed"}, "")) as run_cmd:
                result = whats_app_channel.WhatsAppChannel(
                    accounts_path=Path(td) / "accounts.json",
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                ).extract()
        self.assertIsNone(result)
        command = run_cmd.call_args.args[0]
        self.assertEqual(command[command.index("--max-messages") + 1], "0")
        # The pinned wacli fork auto-builds; discovery no longer suppresses install.
        self.assertNotIn("--no-install", command)

    def test_extract_whatsapp_records_pre_full_sync_nudge_in_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "whatsapp.csv"
            payload = {
                "status": "completed",
                "pairing": {"state": "pre_full_sync",
                            "hint": "Re-link to pull years more history."},
            }
            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", missing), \
                    mock.patch.object(whats_app_channel, "run_cmd", return_value=(0, payload, "")):
                channel = whats_app_channel.WhatsAppChannel(
                    accounts_path=Path(td) / "accounts.json",
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                )
                result = channel.extract()
        self.assertIsNone(result)
        self.assertEqual(channel.artifacts["whatsapp_pairing_state"], "pre_full_sync")
        self.assertIn("Re-link", channel.artifacts["whatsapp_pairing_notice"])

    def test_discover_hoists_pre_full_sync_nudge_to_top_level(self) -> None:
        # A fast-path completed run must surface the nudge at the top level, not
        # bury it under child.artifacts where a happy-path agent won't look.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "discover"
            merged = Path(td) / "merged-contacts.csv"  # absent -> empty contacts.csv

            def fake_extract(self):
                self.artifacts["whatsapp_pairing_state"] = "pre_full_sync"
                self.artifacts["whatsapp_pairing_notice"] = "Re-link to pull years more history."
                return None

            with mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages.WhatsAppChannel, "extract", fake_extract), \
                    mock.patch.object(discover_messages.MessageChannel, "normalize", lambda self: None), \
                    mock.patch.object(discover_messages.MessagesDiscovery, "_merge", lambda self: None):
                result = discover_messages.MessagesDiscovery(
                    include_imessage=False, include_whatsapp=True, out_dir=out).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["whatsapp_pairing_state"], "pre_full_sync")
        self.assertIn("Re-link", result["whatsapp_pairing_notice"])
        self.assertEqual(result["child"]["artifacts"]["contacts_csv"], str(merged))

    def test_existing_channel_exports_are_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            imessage = root / "imessage.csv"
            whatsapp = root / "whatsapp.csv"
            imessage.write_text("phone,name\n+14155550101,Old iMessage\n", encoding="utf-8")
            whatsapp.write_text("phone,name\n+14155550102,Old WhatsApp\n", encoding="utf-8")

            with mock.patch.object(i_message_channel, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(i_message_channel, "run_cmd", side_effect=[
                        (0, {"status": "ok"}, ""),
                        (0, {"status": "completed"}, ""),
                    ]) as imessage_run:
                result = i_message_channel.IMessageChannel(
                    accounts_path=root / "accounts.json", other_enabled=False).extract()
            self.assertIsNone(result)
            self.assertEqual(imessage_run.call_count, 2)
            self.assertIn("extract", imessage_run.call_args_list[1].args[0])

            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(whats_app_channel, "run_cmd", return_value=(0, {"status": "completed"}, "")) as whatsapp_run:
                result = whats_app_channel.WhatsAppChannel(
                    accounts_path=root / "accounts.json",
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                ).extract()
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
            with mock.patch.object(i_message_channel, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS_MANIFEST", manifest), \
                    mock.patch.object(discover_messages, "run_cmd", return_value=(0, {"status": "ok"}, "")) as run_cmd:
                result = discover_messages.MessagesDiscovery(
                    include_imessage=True, include_whatsapp=False, out_dir=root,
                )._merge()

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
            with mock.patch.object(i_message_channel, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", root / "missing.csv"), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS_MANIFEST", manifest):
                imessage.write_text(
                    "phone,name,source,message_count\n+14155550101,Old Name,imessage,1\n",
                    encoding="utf-8",
                )
                self.assertIsNone(discover_messages.MessagesDiscovery(
                    include_imessage=True, include_whatsapp=False, out_dir=root,
                )._merge())
                self.assertIn("Old Name", merged.read_text(encoding="utf-8"))

                imessage.write_text(
                    "phone,name,source,message_count\n+14155550101,Fresh Name,imessage,2\n",
                    encoding="utf-8",
                )
                self.assertIsNone(discover_messages.MessagesDiscovery(
                    include_imessage=True, include_whatsapp=False, out_dir=root,
                )._merge())
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
            fake_store = mock.Mock()
            fake_store.run.return_value = {"status": status}
            with self.subTest(status=status), \
                    mock.patch.object(discover_messages, "MessagesDiscovery", return_value=fake_store), \
                    mock.patch.object(sys, "argv", ["messages.py", "discover"]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(discover_messages.main(), expected)


class MessagesImportRuntimeTests(unittest.TestCase):
    CONTACT_FIELDS = [
        "phone",
        "name",
        "source",
        "is_in_group_chats",
        "group_names",
        "message_count",
        "imessage_message_count",
        "whatsapp_message_count",
        "last_message",
        "imessage_last_message",
        "whatsapp_last_message",
        "skip",
        "match_status",
        "matched_person_id",
        "matched_name",
        "matched_linkedin_url",
        "match_confidence",
        "match_method",
        "match_reason",
    ]

    @classmethod
    def contact_row(cls, **overrides: str) -> dict[str, str]:
        row = {field: "" for field in cls.CONTACT_FIELDS}
        row.update({
            "phone": "+14155550123",
            "name": "Jane Doe",
            "source": "imessage",
            "is_in_group_chats": "false",
            "message_count": "87",
            "imessage_message_count": "87",
            "last_message": "2026-06-01T05:44:31+00:00",
            "imessage_last_message": "2026-06-01T05:44:31+00:00",
        })
        row.update(overrides)
        return row

    @classmethod
    def matched_row(cls, **overrides: str) -> dict[str, str]:
        row = cls.contact_row(
            match_status="matched",
            matched_person_id="net-1",
            matched_name="Jane Doe",
            matched_linkedin_url="https://www.linkedin.com/in/jane-doe",
            match_method="phone",
        )
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
            contacts = root / ".powerpacks" / "messages" / "contacts.csv"
            match_manifest = root / ".powerpacks" / "messages" / "contacts.csv.match.manifest.json"

            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_BASE_DIR", state))
            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_IMPORT_DIR", import_dir))
            stack.enter_context(mock.patch.object(import_messages, "DEFAULT_DIRECTORY_CSV", directory))
            stack.enter_context(mock.patch.object(import_messages, "WORKING_CONTACTS_CSV", contacts))
            stack.enter_context(mock.patch.object(import_messages, "MATCH_MANIFEST_JSON", match_manifest))
            previous = Path.cwd()
            os.chdir(root)
            try:
                yield {
                    "root": root,
                    "state": state,
                    "import_dir": import_dir / "messages",
                    "directory": directory,
                    "accounts": accounts,
                    "contacts": contacts,
                    "match_manifest": match_manifest,
                }
            finally:
                os.chdir(previous)

    @staticmethod
    def csv_count(path: Path) -> int:
        with path.open(newline="", encoding="utf-8") as handle:
            return sum(1 for _ in CsvIO.dict_reader(handle))

    @staticmethod
    def csv_rows(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(CsvIO.dict_reader(handle))

    def write_contacts(self, env: dict[str, object], rows: list[dict[str, str]]) -> None:
        write_csv_rows(env["contacts"], self.CONTACT_FIELDS, rows)
        env["match_manifest"].parent.mkdir(parents=True, exist_ok=True)
        env["match_manifest"].write_text("{}\n", encoding="utf-8")

    @staticmethod
    def run_import(env: dict[str, object], *, confirm: bool, **overrides: object) -> dict[str, object]:
        args = {
            "accounts": env["accounts"],
            "operator_id": "local",
            "confirm_import": confirm,
            "min_message_count": import_messages.DEFAULT_MIN_MESSAGE_COUNT,
            "include_group_only": False,
            "allow_unmatched": False,
        }
        args.update(overrides)
        return import_messages.run(SimpleNamespace(**args))

    def seed_directory(self, env: dict[str, object]) -> None:
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

    def test_block_confirm_noop_refresh_and_removal_lifecycle(self) -> None:
        with self.sandbox() as env:
            self.seed_directory(env)
            sentinel = "SECRET GROUP NAME MUST NOT LEAK"
            self.write_contacts(env, [
                self.matched_row(group_names=sentinel),
                self.contact_row(
                    phone="+14155550999",
                    name="John Smith",
                    source="whatsapp",
                    message_count="12",
                    imessage_message_count="",
                    whatsapp_message_count="12",
                    whatsapp_last_message="2026-06-05T00:00:00+00:00",
                    group_names=sentinel,
                ),
                self.contact_row(phone="+14155550777", name="AAA", message_count="4"),
            ])

            blocked = self.run_import(env, confirm=False)
            self.assertEqual(blocked["status"], "blocked_approval")
            self.assertFalse((env["import_dir"] / "people.csv").exists())
            self.assertEqual(
                [path.name for path in env["import_dir"].iterdir()],
                ["manifest.json"],
            )

            completed = self.run_import(env, confirm=True)
            self.assertEqual(completed["status"], "completed")
            people_csv = env["import_dir"] / "people.csv"
            candidates_csv = env["import_dir"] / "candidates.csv"
            self.assertEqual(self.csv_count(people_csv), 1)
            self.assertEqual(self.csv_count(candidates_csv), 1)
            self.assertEqual(completed["stats"], {"people": 1, "candidates": 1})
            self.assertEqual(
                completed["materialized"]["skipped"].get("bad_name"), 1
            )
            manifests = list(env["import_dir"].rglob("manifest.json"))
            self.assertEqual(manifests, [env["import_dir"] / "manifest.json"])
            self.assertEqual(list(env["import_dir"].rglob("*ledger*")), [])
            self.assertFalse((env["import_dir"] / "people.input.csv").exists())
            self.assertFalse((env["import_dir"] / "enrichment").exists())

            person = self.csv_rows(people_csv)[0]
            self.assertEqual(person["id"], "net-1")
            self.assertEqual(person["linkedin_url"], "https://www.linkedin.com/in/jane-doe")
            self.assertEqual(person["enrichment_provider"], "")
            self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 87})
            candidate = self.csv_rows(candidates_csv)[0]
            self.assertEqual(candidate["candidate_key"], "phone:+14155550999")
            self.assertEqual(candidate["source"], "whatsapp")
            self.assertEqual(candidate["full_name"], "John Smith")
            derived_text = people_csv.read_text() + candidates_csv.read_text()
            self.assertNotIn(sentinel, derived_text)

            noop = self.run_import(env, confirm=False)
            self.assertTrue(noop["noop"])

            self.write_contacts(env, [
                self.matched_row(
                    message_count="120",
                    imessage_message_count="120",
                    last_message="2026-06-10T00:00:00+00:00",
                    imessage_last_message="2026-06-10T00:00:00+00:00",
                ),
            ])
            refreshed = self.run_import(env, confirm=True)
            self.assertEqual(refreshed["status"], "completed")
            person = self.csv_rows(people_csv)[0]
            self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 120})
            self.assertEqual(self.csv_count(candidates_csv), 0)

            self.write_contacts(env, [
                self.contact_row(phone="+14155550777", name="AAA", message_count="4"),
            ])
            removed = self.run_import(env, confirm=True)
            self.assertEqual(removed["status"], "completed")
            self.assertEqual(self.csv_count(people_csv), 0)
            self.assertEqual(self.csv_count(candidates_csv), 0)
            directory_rows = self.csv_rows(env["directory"])
            self.assertEqual([row["source"] for row in directory_rows], ["gmail_msgvault"])

    def test_floor_reasons_and_suggested_not_attached(self) -> None:
        cases = [
            ({"phone": "jane@icloud.com"}, "email_handle"),
            ({"phone": "777888"}, "short_code_or_invalid_phone"),
            ({"skip": "true"}, "skip_flag"),
            ({"name": ""}, "no_name"),
            ({"name": "AAA"}, "bad_name"),
            ({"name": "Jane Hinge"}, "blocked_name_token"),
            ({"name": "4155550123", "phone": "+14155550123"}, "name_is_phone"),
            ({"message_count": "0", "imessage_message_count": "0"}, "below_min_messages"),
            ({"is_in_group_chats": "true", "message_count": "3", "imessage_message_count": "3"}, "group_only_low_signal"),
            ({
                "source": "whatsapp",
                "is_in_group_chats": "true",
                "message_count": "3",
                "imessage_message_count": "",
                "whatsapp_message_count": "",
            }, "group_only_low_signal"),
        ]
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                row = self.contact_row(**overrides)
                self.assertEqual(
                    import_messages.contact_floor_reason(
                        row, min_message_count=1, include_group_only=False
                    ),
                    expected,
                )
        # Group-only contacts pass with the opt-in flag.
        row = self.contact_row(is_in_group_chats="true", message_count="3", imessage_message_count="3")
        self.assertEqual(
            import_messages.contact_floor_reason(row, min_message_count=1, include_group_only=True),
            "",
        )

        # Appearing in a group does not make a real WhatsApp DM group-only.
        row = self.contact_row(
            source="whatsapp",
            is_in_group_chats="true",
            message_count="9",
            imessage_message_count="",
            whatsapp_message_count="9",
        )
        self.assertEqual(
            import_messages.contact_floor_reason(
                row, min_message_count=1, include_group_only=False
            ),
            "",
        )

        with tempfile.TemporaryDirectory() as td:
            contacts = Path(td) / "contacts.csv"
            write_csv_rows(contacts, self.CONTACT_FIELDS, [
                self.contact_row(
                    match_status="suggested",
                    matched_person_id="net-9",
                    matched_name="Maybe Jane",
                    matched_linkedin_url="https://www.linkedin.com/in/maybe-jane",
                ),
            ])
            summary, people_rows, candidate_rows = import_messages.selected_contacts_people(contacts)
            self.assertEqual(people_rows, [])
            self.assertEqual(len(candidate_rows), 1)
            self.assertEqual(summary["skipped"].get("suggested_not_attached"), 1)
            evidence = json.loads(candidate_rows[0]["evidence"])
            self.assertEqual(evidence["suggested_person_id"], "net-9")
            self.assertEqual(
                evidence["suggested_linkedin_url"],
                "https://www.linkedin.com/in/maybe-jane",
            )

    def test_unmatched_whatsapp_dm_in_group_becomes_candidate(self) -> None:
        with self.sandbox() as env:
            self.seed_directory(env)
            self.write_contacts(env, [
                self.contact_row(
                    phone="+15550100123",
                    name="Jordan Bravo",
                    source="whatsapp",
                    is_in_group_chats="true",
                    group_names="Startup Circle",
                    message_count="9",
                    imessage_message_count="",
                    whatsapp_message_count="9",
                    imessage_last_message="",
                    whatsapp_last_message="2026-07-14T22:40:31Z",
                    match_status="unmatched",
                ),
            ])

            completed = self.run_import(env, confirm=True)

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["stats"], {"people": 0, "candidates": 1})
            self.assertNotIn(
                "group_only_low_signal", completed["materialized"]["skipped"]
            )
            candidate = self.csv_rows(env["import_dir"] / "candidates.csv")[0]
            self.assertEqual(candidate["candidate_key"], "phone:+15550100123")
            self.assertEqual(candidate["full_name"], "Jordan Bravo")
            self.assertEqual(candidate["source"], "whatsapp")

            # A pre-fix manifest must not make the corrected import a no-op.
            manifest_path = env["import_dir"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["input"]["pipeline_contract"] = "messages-contacts-direct-v3"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rerun = self.run_import(env, confirm=True)
            self.assertFalse(rerun.get("noop", False))
            self.assertEqual(
                rerun["input"]["pipeline_contract"],
                "messages-contacts-direct-v6",
            )

    def test_matched_duplicates_merge_and_email_handles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            contacts = Path(td) / "contacts.csv"
            write_csv_rows(contacts, self.CONTACT_FIELDS, [
                self.matched_row(),
                self.matched_row(
                    phone="+14155550999",
                    source="whatsapp",
                    message_count="9",
                    imessage_message_count="",
                    whatsapp_message_count="9",
                    whatsapp_last_message="2026-06-05T00:00:00+00:00",
                ),
                self.matched_row(
                    phone="jane@icloud.com",
                    source="imessage",
                    message_count="3",
                    imessage_message_count="3",
                ),
            ])
            summary, people_rows, candidate_rows = import_messages.selected_contacts_people(contacts)
            self.assertEqual(candidate_rows, [])
            self.assertEqual(len(people_rows), 1)
            self.assertEqual(summary["skipped"].get("duplicate_matched_person"), 2)
            person = people_rows[0]
            self.assertEqual(
                json.loads(person["all_phones"]),
                ["+14155550123", "+14155550999"],
            )
            self.assertEqual(person["primary_email"], "jane@icloud.com")
            self.assertEqual(person["source_channels"], "imessage,whatsapp")
            self.assertEqual(
                json.loads(person["interaction_counts"]),
                {"imessage": 87, "whatsapp": 9},
            )
            self.assertEqual(person["last_interaction"], "2026-06-05T00:00:00+00:00")

    def test_matched_rows_emit_the_superseded_candidate_identity(self) -> None:
        # Import is the only witness that the phone-axis candidate id and the
        # matched person are the same contact row; the people row must carry
        # the equivalence so parent-building can fold the pre-match identity
        # instead of leaving a floating twin in review.
        with tempfile.TemporaryDirectory() as td:
            contacts = Path(td) / "contacts.csv"
            write_csv_rows(contacts, self.CONTACT_FIELDS, [
                self.matched_row(),
                self.matched_row(
                    phone="+14155550999",
                    source="whatsapp",
                    message_count="9",
                    imessage_message_count="",
                    whatsapp_message_count="9",
                ),
            ])
            _, people_rows, _ = import_messages.selected_contacts_people(contacts)
            self.assertEqual(len(people_rows), 1)
            self.assertEqual(
                json.loads(people_rows[0]["superseded_person_ids"]),
                ["candidate:phone:+14155550123", "candidate:phone:+14155550999"],
            )

    def test_missing_inputs_and_empty_contacts(self) -> None:
        with self.sandbox() as env:
            missing = self.run_import(env, confirm=True)
            self.assertEqual(missing["status"], "failed")
            self.assertEqual(missing["reason"], "messages_contacts_missing")

            write_csv_rows(env["contacts"], self.CONTACT_FIELDS, [self.matched_row()])
            unmatched = self.run_import(env, confirm=True)
            self.assertEqual(unmatched["status"], "failed")
            self.assertEqual(unmatched["reason"], "messages_contacts_not_matched")

            allowed = self.run_import(env, confirm=True, allow_unmatched=True)
            self.assertEqual(allowed["status"], "completed")
            self.assertEqual(self.csv_count(env["import_dir"] / "people.csv"), 1)

            self.seed_directory(env)
            self.write_contacts(env, [])
            cleared = self.run_import(env, confirm=True)
            self.assertEqual(cleared["status"], "completed")
            self.assertEqual(cleared["stats"], {"people": 0, "candidates": 0})
            self.assertEqual(self.csv_count(env["import_dir"] / "people.csv"), 0)
            directory_rows = self.csv_rows(env["directory"])
            self.assertEqual([row["source"] for row in directory_rows], ["gmail_msgvault"])

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
