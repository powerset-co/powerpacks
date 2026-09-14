import io
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.discover.common import read_csv_rows, write_csv_rows
from packs.ingestion.primitives.discover.messages.models import (
    MessageChannelExtracted,
    MessagesDiscoveryCompleted,
)
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.schemas.message_contacts import CSV_HEADERS, MessageContact


ROOT = Path(__file__).resolve().parents[1]
INGESTION = ROOT / "packs/ingestion"
discover_messages = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.discover"
)
# The channel classes and their owned path constants live in channels/; the
# path constants patched below (IMESSAGE_*/WHATSAPP_*) live on the concrete
# channel module. The channels now call the leaf primitive CLASSES in-process
# (no self-spawned subprocess), so behavior is patched on the class method where
# it is DEFINED — IMessageExtractor.check/extract, WhatsAppExtractor.run,
# ContactsMerger.merge — not on a channel-module run_cmd global.
i_message_channel = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.channels.i_message_channel"
)
whats_app_channel = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.channels.whats_app_channel"
)
extract_imessage = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.extract_imessage"
)
extract_whatsapp = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.extract_whatsapp"
)
whatsapp_wacli = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.whatsapp_wacli"
)
merge_contacts = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.merge_contacts"
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
            "primitives/discover/messages/extract_whatsapp.py",
            "primitives/discover/messages/merge_contacts.py",
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
        self.assertNotIn("index_contacts_pipeline.py fan-in", messages)
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
        # One stage manifest, now DECLARED rather than written by hand: the Node
        # run template writes `manifest` after validating the declared outputs.
        self.assertIn('manifest = str(DEFAULT_MESSAGES_OUTPUT_DIR / "manifest.json")', text)
        self.assertNotIn("write_stage_manifest(", text)

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
        # blocks were pruned (only the gmail block is consumed).
        config = json.loads(
            (INGESTION / "primitives/discover/discovery.config.json").read_text()
        )
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
            "primitives/discover/messages/merge_contacts.py",
        ):
            primitive_text = (INGESTION / relative).read_text(encoding="utf-8").lower()
            for token in ("run_id", "run-id", "uuid"):
                with self.subTest(relative=relative, token=token):
                    self.assertNotIn(token, primitive_text)

    def test_discover_lets_whatsapp_primitive_choose_sync_strategy(self) -> None:
        # The channel calls WhatsAppExtractor.run in-process and lets the
        # primitive choose the sync strategy: it passes no sync-mode kwarg and
        # forwards its own sync-phase timeout, not a synthetic outer wall-clock cap.
        with mock.patch.object(
            extract_whatsapp.WhatsAppExtractor, "run", return_value={"status": "completed"},
        ) as run:
            channel = whats_app_channel.WhatsAppChannel(
                other_enabled=False,
                max_messages=0,
            )
            result = channel.execute()

        self.assertEqual(result.status, "completed")
        kwargs = run.call_args.kwargs
        self.assertNotIn("sync_mode", kwargs)
        self.assertEqual(kwargs["sync_timeout"], whats_app_channel.DEFAULT_WACLI_SYNC_TIMEOUT)

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
                    mock.patch.object(extract_whatsapp.WhatsAppExtractor, "run", return_value={"status": "completed"}) as run:
                result = whats_app_channel.WhatsAppChannel(
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                ).execute()
        self.assertEqual(result.status, "completed")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["max_messages"], 0)
        # The pinned wacli fork auto-builds; discovery no longer suppresses install.
        self.assertNotIn("no_install", kwargs)

    def test_extract_whatsapp_returns_pre_full_sync_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "whatsapp.csv"
            payload = {
                "status": "completed",
                "pairing": {"state": "pre_full_sync",
                            "hint": "Re-link to pull years more history."},
            }
            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", missing), \
                    mock.patch.object(extract_whatsapp.WhatsAppExtractor, "run", return_value=payload):
                channel = whats_app_channel.WhatsAppChannel(
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                )
                result = channel.execute()
        # The channel RETURNS its contribution; the store renders the manifest's
        # `whatsapp_pairing_*` artifacts from it.
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.pairing_state, "pre_full_sync")
        self.assertIn("Re-link", result.pairing_notice)

    def test_whatsapp_channel_reports_no_nudge_for_other_pairing_states(self) -> None:
        # Only `pre_full_sync` earns the nudge — any other pairing state
        # contributes nothing to the manifest.
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "whatsapp.csv"
            payload = {"status": "completed", "pairing": {"state": "full_sync", "hint": "n/a"}}
            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", missing), \
                    mock.patch.object(extract_whatsapp.WhatsAppExtractor, "run", return_value=payload):
                result = whats_app_channel.WhatsAppChannel(
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                ).execute()
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.pairing_state)
        self.assertIsNone(result.pairing_notice)

    def test_discover_hoists_pre_full_sync_nudge_to_top_level(self) -> None:
        # A fast-path completed run must surface the nudge at the top level, not
        # bury it under child.artifacts where a happy-path agent won't look.
        # The channel and store are declared nodes now, so their run templates
        # verify the DECLARED outputs on a completed run — hence the stubbed
        # execute/merge still have to leave those two CSVs on disk.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "discover"
            whatsapp = Path(td) / "whatsapp.contacts.csv"
            merged = Path(td) / "merged-contacts.csv"
            write_csv_rows(whatsapp, CSV_HEADERS, [])
            write_csv_rows(merged, CSV_HEADERS, [])

            def fake_execute(self):
                return MessageChannelExtracted(
                    channel=self.channel,
                    contacts_csv=str(self.contacts_csv),
                    provider="wacli",
                    pairing_state="pre_full_sync",
                    pairing_notice="Re-link to pull years more history.",
                )

            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(discover_messages, "MERGED_CONTACTS", merged), \
                    mock.patch.object(discover_messages.WhatsAppChannel, "execute", fake_execute), \
                    mock.patch.object(discover_messages.MessagesDiscovery, "_merge", lambda self: None):
                result = discover_messages.MessagesDiscovery(
                    include_imessage=False, include_whatsapp=True, out_dir=out).run().to_payload()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["whatsapp_pairing_state"], "pre_full_sync")
        self.assertIn("Re-link", result["whatsapp_pairing_notice"])
        artifacts = result["child"]["artifacts"]
        self.assertEqual(artifacts["contacts_csv"], str(merged))
        # The artifacts map is rendered from the channel's return, key for key.
        self.assertEqual(artifacts["whatsapp_contacts_csv"], str(whatsapp))
        self.assertEqual(artifacts["whatsapp_provider"], "wacli")
        self.assertEqual(artifacts["whatsapp_pairing_state"], "pre_full_sync")
        self.assertIn("Re-link", artifacts["whatsapp_pairing_notice"])

    def test_existing_channel_exports_are_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            imessage = root / "imessage.csv"
            whatsapp = root / "whatsapp.csv"
            imessage.write_text("phone,name\n+14155550101,Old iMessage\n", encoding="utf-8")
            whatsapp.write_text("phone,name\n+14155550102,Old WhatsApp\n", encoding="utf-8")

            with mock.patch.object(i_message_channel, "IMESSAGE_CONTACTS", imessage), \
                    mock.patch.object(extract_imessage.IMessageExtractor, "check",
                                      return_value={"status": "ok"}) as imessage_check, \
                    mock.patch.object(extract_imessage.IMessageExtractor, "extract",
                                      return_value={"status": "completed"}) as imessage_extract:
                result = i_message_channel.IMessageChannel(
                    other_enabled=False).execute()
            self.assertEqual(result.contacts_csv, str(imessage))
            # The channel gates on check (Full Disk Access) then runs extract.
            self.assertEqual(imessage_check.call_count, 1)
            self.assertEqual(imessage_extract.call_count, 1)
            self.assertTrue(imessage_check.call_args.kwargs["strict"])

            with mock.patch.object(whats_app_channel, "WHATSAPP_CONTACTS", whatsapp), \
                    mock.patch.object(extract_whatsapp.WhatsAppExtractor, "run",
                                      return_value={"status": "completed"}) as whatsapp_run:
                result = whats_app_channel.WhatsAppChannel(
                    other_enabled=False,
                    max_messages=whats_app_channel.DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
                ).execute()
            self.assertEqual(result.contacts_csv, str(whatsapp))
            self.assertEqual(whatsapp_run.call_count, 1)

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
                    mock.patch.object(merge_contacts.ContactsMerger, "merge",
                                      return_value={"status": "ok"}) as merge:
                result = discover_messages.MessagesDiscovery(
                    include_imessage=True, include_whatsapp=False, out_dir=root,
                )._merge()

        self.assertIsNone(result)
        inputs = merge.call_args.kwargs["inputs"]
        self.assertIn(imessage, inputs)
        self.assertNotIn(whatsapp, inputs)

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
            fake_store.run.return_value = MessagesDiscoveryCompleted(status=status)
            with self.subTest(status=status), \
                    mock.patch.object(discover_messages, "MessagesDiscovery", return_value=fake_store), \
                    mock.patch.object(sys, "argv", ["messages.py", "discover"]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(discover_messages.main(), expected)


class MessagesImportRuntimeTests(unittest.TestCase):
    @contextmanager
    def sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yield {
                "root": root,
                "contacts": root / "messages/contacts.csv",
                "import_dir": root / "import",
                "directory": root / "directory.csv",
            }

    @staticmethod
    def contact_row(**overrides):
        return {
            "phone": "+15550100123", "name": "Jordan Bravo", "source": "imessage",
            "message_count": "4", "imessage_message_count": "4",
            "last_message": "2026-09-01T12:00:00+00:00", **overrides,
        }

    @staticmethod
    def run_import(env):
        importer = import_messages.MessagesImport(
            contacts_csv=env["contacts"], import_dir=env["import_dir"],
        )
        importer.run()
        return importer.written

    def test_import_parses_each_contact_once(self):
        with self.sandbox() as env:
            write_csv_rows(env["contacts"], CSV_HEADERS, [
                self.contact_row(), self.contact_row(phone="casey@example.com", name=""),
            ])
            with mock.patch.object(
                MessageContact, "from_csv_row", wraps=MessageContact.from_csv_row,
            ) as parse:
                completed = self.run_import(env)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(completed["stats"], {"people": 2, "candidates": 2})

    def test_noop_refresh_and_empty_source_preserve_directory(self):
        with self.sandbox() as env:
            env["directory"].write_text("source-owned directory sentinel\n")
            directory_bytes = env["directory"].read_bytes()
            write_csv_rows(env["contacts"], CSV_HEADERS, [self.contact_row()])
            completed = self.run_import(env)
            self.assertEqual(completed["status"], "completed")
            output = env["import_dir"] / "messages/people.csv"
            manifest = output.with_name("manifest.json")
            before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (output, manifest)]
            with mock.patch.object(MessageContact, "from_csv_row", side_effect=AssertionError("no-op parsed contacts")):
                noop = self.run_import(env)
            self.assertTrue(noop["noop"])
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (output, manifest)], before)

            write_csv_rows(env["contacts"], CSV_HEADERS, [self.contact_row(
                imessage_message_count="7", message_count="7", last_message="2026-09-03T12:00:00+00:00",
            )])
            refreshed = self.run_import(env)
            self.assertEqual(refreshed["status"], "completed")
            self.assertFalse(refreshed.get("noop", False))
            person = read_csv_rows(output)[1][0]
            self.assertEqual(json.loads(person["interaction_counts"]), {"imessage": 7})
            self.assertEqual(person["last_interaction"], "2026-09-03T12:00:00+00:00")

            write_csv_rows(env["contacts"], CSV_HEADERS, [])
            emptied = self.run_import(env)
            self.assertEqual(emptied["stats"], {"people": 0, "candidates": 0})
            self.assertEqual(read_csv_rows(output), (PEOPLE_SCHEMA_COLUMNS, []))
            self.assertEqual(env["directory"].read_bytes(), directory_bytes)

    def test_previous_matched_contract_reruns_with_unchanged_input(self):
        with self.sandbox() as env:
            write_csv_rows(env["contacts"], CSV_HEADERS, [self.contact_row(
                match_status="matched", matched_person_id="old-person", matched_name="Wrong Person",
            )])
            output = env["import_dir"] / "messages/people.csv"
            write_csv_rows(output, PEOPLE_SCHEMA_COLUMNS, [{"id": "old-person"}])
            write_manifest("messages", {
                "status": "completed",
                "input": {
                    "pipeline_contract": "messages-contacts-direct-v6",
                    "contacts_csv": str(env["contacts"]),
                },
                "outputs": {"people_csv": str(output)},
            }, import_dir=env["import_dir"])
            completed = self.run_import(env)
            self.assertFalse(completed.get("noop", False))
            self.assertEqual(completed["input"]["pipeline_contract"], import_messages.MESSAGES_IMPORT_CONTRACT)
            self.assertEqual(read_csv_rows(output)[1][0]["id"], "candidate:phone:+15550100123")
            self.assertTrue(self.run_import(env)["noop"])

    def test_existing_review_and_match_artifacts_are_untouched(self):
        with self.sandbox() as env:
            write_csv_rows(env["contacts"], CSV_HEADERS, [self.contact_row()])
            legacy = [
                env["contacts"].with_name("research_review.csv"),
                env["contacts"].with_name("contacts.csv.match.manifest.json"),
                env["import_dir"] / "messages/candidates.csv",
            ]
            for path in legacy:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unused legacy data\n")
            self.assertEqual(self.run_import(env)["status"], "completed")
            for path in legacy:
                self.assertEqual(path.read_text(), "unused legacy data\n")
            self.assertEqual(set(self.run_import(env)["fingerprints"]["input_artifacts"]), {str(env["contacts"])})

    def test_missing_contacts_fails_without_creating_people(self):
        with self.sandbox() as env:
            missing = self.run_import(env)
            self.assertEqual(missing["status"], "failed")
            self.assertEqual(missing["reason"], "messages_contacts_missing")
            self.assertFalse((env["import_dir"] / "messages/people.csv").exists())

    def test_cli_missing_contacts_and_removed_flags(self):
        cases = [(["run"], 1), (["--help"], 0)]
        for flag in ("--confirm-import", "--allow-unmatched", "--include-group-only"):
            cases.append((["run", flag], 2))
        cases.extend([
            (["run", "--min-message-count", "1"], 2),
            (["run", "--operator-id", "local"], 2),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            for args, expected in cases:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, str(Path(import_messages.__file__).resolve()), *args],
                        cwd=tmp, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if args == ["run"]:
                        self.assertEqual(json.loads(result.stdout)["reason"], "messages_contacts_missing")


if __name__ == "__main__":
    unittest.main()
