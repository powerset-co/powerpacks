from __future__ import annotations

import csv
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py"

spec = importlib.util.spec_from_file_location("import_whatsapp_wacli", PRIMITIVE)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def create_wacli_db(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store / "wacli.db")
    try:
        conn.executescript(
            """
            CREATE TABLE chats (
                jid TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT,
                last_message_ts INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                muted_until INTEGER NOT NULL DEFAULT 0,
                unread INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE contacts (
                jid TEXT PRIMARY KEY,
                phone TEXT,
                push_name TEXT,
                full_name TEXT,
                first_name TEXT,
                business_name TEXT,
                system_name TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE groups (
                jid TEXT PRIMARY KEY,
                name TEXT,
                owner_jid TEXT,
                created_ts INTEGER,
                is_parent INTEGER NOT NULL DEFAULT 0,
                linked_parent_jid TEXT,
                left_at INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE group_participants (
                group_jid TEXT NOT NULL,
                user_jid TEXT NOT NULL,
                role TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_jid, user_jid)
            );
            CREATE TABLE messages (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid TEXT NOT NULL,
                chat_name TEXT,
                msg_id TEXT NOT NULL,
                sender_jid TEXT,
                sender_name TEXT,
                ts INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                text TEXT,
                display_text TEXT,
                revoked INTEGER NOT NULL DEFAULT 0,
                deleted_for_me INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_jid, msg_id)
            );
            """
        )
        conn.executemany(
            "INSERT INTO contacts (jid, phone, push_name, full_name, first_name, business_name, system_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("14155550101@s.whatsapp.net", "+14155550101", "Jane", "Jane Doe", "", "", "", 1),
                ("14155550202@s.whatsapp.net", "+14155550202", "Bob", "", "", "", "", 1),
                ("14155550303@s.whatsapp.net", "+14155550303", "Charlie", "", "", "", "", 1),
                ("14155550404@s.whatsapp.net", "+14155550404", "Left Group", "", "", "", "", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO chats (jid, kind, name, last_message_ts) VALUES (?, ?, ?, ?)",
            [
                ("14155550101@s.whatsapp.net", "dm", "Jane Chat", 1735689600),
                ("987654321@g.us", "group", "Founders", 1735689700),
                ("111222333@g.us", "group", "Old Group", 1735689800),
            ],
        )
        conn.executemany(
            "INSERT INTO groups (jid, name, owner_jid, created_ts, is_parent, linked_parent_jid, left_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("987654321@g.us", "Founders", "", 1, 0, None, None, 1),
                ("111222333@g.us", "Old Group", "", 1, 0, None, 1735689900, 1),
            ],
        )
        conn.executemany(
            "INSERT INTO group_participants (group_jid, user_jid, role, updated_at) VALUES (?, ?, ?, ?)",
            [
                ("987654321@g.us", "14155550202@s.whatsapp.net", "participant", 1),
                ("987654321@g.us", "14155550303@s.whatsapp.net", "participant", 1),
                ("111222333@g.us", "14155550404@s.whatsapp.net", "participant", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO messages (chat_jid, chat_name, msg_id, sender_jid, sender_name, ts, from_me, text, display_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "14155550101@s.whatsapp.net",
                    "Jane Chat",
                    "m1",
                    "14155550101@s.whatsapp.net",
                    "Jane",
                    1735689600,
                    0,
                    "SECRET BODY ONE",
                    "SECRET BODY ONE",
                ),
                (
                    "14155550101@s.whatsapp.net",
                    "Jane Chat",
                    "m2",
                    "14155550101@s.whatsapp.net",
                    "Jane",
                    1735689700,
                    1,
                    "SECRET BODY TWO",
                    "SECRET BODY TWO",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class ImportWhatsAppWacliTests(unittest.TestCase):
    def test_default_max_messages_is_unlimited(self) -> None:
        self.assertEqual(mod.DEFAULT_MAX_MESSAGES, 0)
        self.assertEqual(mod.DEFAULT_IDLE_EXIT, "30s")
        self.assertEqual(mod.effective_max_messages(0, 25000), 0)
        self.assertEqual(mod.effective_max_messages(10000, 25000), 26000)

    def test_qr_payloads_are_redacted_from_diagnostics(self) -> None:
        text = 'before\n2@secret-whatsapp-pairing-payload\n{"event":"qr_code","data":{"code":"2@secret"}}\nafter'
        self.assertEqual(
            mod.redact_qr_payloads(text),
            f"before\n{mod.QR_REDACTION}\n{mod.QR_REDACTION}\nafter",
        )

    def test_auth_requires_qrencode_for_browser_qr(self) -> None:
        with mock.patch.object(mod.shutil, "which", return_value=None), \
                self.assertRaises(mod.PrimitiveBlocked) as ctx:
            mod.run_auth(Path("/tmp/wacli-store"), timeout=1, idle_exit="1s")

        self.assertEqual(ctx.exception.payload["install_command"], "brew install qrencode")
        self.assertIn("qrencode is required", ctx.exception.payload["message"])

    def test_auth_interrupts_wacli_bootstrap_sync_after_connected_event(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("2@qr-payload\n")
                self.stderr = io.StringIO('{"event":"connected","ts":1}\n')
                self.returncode = None
                self.signals: list[int] = []

            def poll(self):
                return self.returncode

            def send_signal(self, sig: int) -> None:
                self.signals.append(sig)
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

            def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        fake = FakeProc()
        with mock.patch.object(mod.shutil, "which", return_value="/opt/homebrew/bin/qrencode"), \
                mock.patch.object(mod.subprocess, "Popen", return_value=fake), \
                mock.patch.object(mod, "update_qr_page") as update_qr_page:
            result = mod.run_auth(Path("/tmp/wacli-store"), timeout=5, idle_exit="30s")

        self.assertEqual(fake.signals, [mod.signal.SIGINT])
        self.assertTrue(result["connected_event"])
        self.assertTrue(result["auth_bootstrap_sync_interrupted"])
        self.assertIn("--events", result["command"])
        update_qr_page.assert_called()

    def test_auth_can_render_qr_without_opening_browser(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("2@qr-payload\n")
                self.stderr = io.StringIO('{"event":"connected","ts":1}\n')
                self.returncode = None

            def poll(self):
                return self.returncode

            def send_signal(self, sig: int) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

            def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        fake = FakeProc()
        with mock.patch.object(mod.shutil, "which", return_value="/opt/homebrew/bin/qrencode"), \
                mock.patch.object(mod.subprocess, "Popen", return_value=fake), \
                mock.patch.object(mod, "update_qr_page") as update_qr_page:
            mod.run_auth(Path("/tmp/wacli-store"), timeout=5, idle_exit="30s", open_qr_page=False)

        self.assertFalse(update_qr_page.call_args.kwargs["open_page"])

    def test_export_reads_metadata_without_message_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = tmp / "wacli"
            create_wacli_db(store)
            contacts, diagnostics = mod.export_contacts_from_store(store, name_fallback_csv=None)
            csv_path = tmp / "contacts.csv"
            jsonl_path = tmp / "contacts.jsonl"

            self.assertEqual(mod.write_csv(csv_path, contacts), 3)
            self.assertEqual(mod.write_jsonl(jsonl_path, contacts), 3)
            self.assertFalse(diagnostics["queries_read_message_body_columns"])
            self.assertEqual(diagnostics["left_groups_skipped"], 1)

            with csv_path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            by_phone = {row["phone"]: row for row in rows}
            self.assertEqual(by_phone["+14155550101"]["message_count"], "2")
            self.assertEqual(by_phone["+14155550101"]["whatsapp_message_count"], "2")
            self.assertEqual(by_phone["+14155550202"]["is_in_group_chats"], "true")
            self.assertEqual(by_phone["+14155550202"]["group_names"], "Founders")
            self.assertEqual(by_phone["+14155550303"]["group_names"], "Founders")
            self.assertNotIn("+14155550404", by_phone)

            exported_text = csv_path.read_text(encoding="utf-8") + jsonl_path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET BODY", exported_text)

    def test_export_command_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = tmp / "wacli"
            create_wacli_db(store)
            output_csv = tmp / "contacts.csv"
            output_jsonl = tmp / "contacts.jsonl"
            manifest = tmp / "manifest.json"
            args = type("Args", (), {
                "store": store,
                "output_csv": output_csv,
                "output_jsonl": output_jsonl,
                "manifest": manifest,
                "include_left_groups": False,
                "max_group_participants": 30,
                "name_fallback_csv": None,
            })()

            stdout = io.StringIO()
            with mock.patch.object(mod, "wacli_version", return_value={"path": "/tmp/wacli", "version": "wacli test"}), \
                    redirect_stdout(stdout):
                rc = mod.cmd_export(args)
            self.assertEqual(rc, 0)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["counts"]["contacts"], 3)
            self.assertFalse(payload["privacy"]["export_reads_message_bodies"])

    def test_export_uses_live_group_participant_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = tmp / "wacli"
            create_wacli_db(store)
            cache_path = mod.group_participants_cache_path(store)
            mod.write_json(cache_path, {
                "version": 1,
                "groups": {
                    "cached-group@g.us": {
                        "jid": "cached-group@g.us",
                        "name": "Cached Group",
                        "participant_count": 2,
                        "participants": [
                            {"phone": "+14155558888", "name": "Cached One"},
                            {"phone": "+14155559999", "name": "Cached Two"},
                        ],
                    },
                },
            })

            contacts, diagnostics = mod.export_contacts_from_store(store, name_fallback_csv=None)
            self.assertEqual(diagnostics["group_participant_cache_groups"], 1)
            self.assertEqual(diagnostics["group_participant_cache_rows"], 2)
            self.assertIn("+14155558888", contacts)
            self.assertIn("+14155559999", contacts)
            self.assertEqual(contacts["+14155558888"].group_names, {"Cached Group"})

    def test_export_fills_missing_names_from_contacts_csv_by_phone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = tmp / "wacli"
            create_wacli_db(store)
            cache_path = mod.group_participants_cache_path(store)
            mod.write_json(cache_path, {
                "version": 1,
                "groups": {
                    "cached-group@g.us": {
                        "jid": "cached-group@g.us",
                        "name": "Cached Group",
                        "participant_count": 1,
                        "participants": [
                            {"phone": "+14155557777", "name": ""},
                        ],
                    },
                },
            })
            fallback = tmp / "contacts.csv"
            fallback.write_text("phone,name\n+14155557777,Fallback Name\n", encoding="utf-8")

            contacts, diagnostics = mod.export_contacts_from_store(store, name_fallback_csv=fallback)

            self.assertEqual(diagnostics["name_fallback_rows"], 1)
            self.assertEqual(contacts["+14155557777"].name, "Fallback Name")


if __name__ == "__main__":
    unittest.main()
