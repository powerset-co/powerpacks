from __future__ import annotations

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

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/ingestion/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py"

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

    def test_resolve_effective_max(self) -> None:
        # Incremental target grows from what the store already has (existing +
        # headroom), computed via the unchanged effective_max_messages helper.
        incr = mod.effective_max_messages(mod.DEFAULT_INCREMENTAL_BUDGET, 25000)
        self.assertGreater(incr, 25000)  # only the delta beyond existing

        # auto: full on the first run (empty store), incremental once populated.
        self.assertEqual(mod.resolve_effective_max("auto", 0, 0), 0)
        self.assertEqual(mod.resolve_effective_max("auto", 0, 25000), incr)

        # full: always a full re-backfill (0 = unlimited), regardless of store.
        self.assertEqual(mod.resolve_effective_max("full", 0, 0), 0)
        self.assertEqual(mod.resolve_effective_max("full", 0, 25000), 0)

        # incremental: bounded delta even on a fresh store.
        self.assertEqual(mod.resolve_effective_max("incremental", 0, 25000), incr)
        self.assertEqual(
            mod.resolve_effective_max("incremental", 0, 0),
            mod.effective_max_messages(mod.DEFAULT_INCREMENTAL_BUDGET, 0),
        )

        # An explicit positive --max-messages wins over the mode.
        self.assertEqual(mod.resolve_effective_max("full", 10000, 25000), 26000)
        self.assertEqual(mod.resolve_effective_max("auto", 10000, 0), 10000)

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
                rows = list(CsvIO.dict_reader(handle))
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

    def test_wacli_device_env_defaults_to_macos_desktop_identity(self) -> None:
        # Only DESKTOP makes WhatsApp render the OS label; specific device enums
        # (e.g. CATALINA) get overridden with WhatsApp's own fixed device name.
        with mock.patch.object(mod, "DEFAULT_DEVICE_PLATFORM", "DESKTOP"), \
             mock.patch.object(mod, "DEFAULT_DEVICE_LABEL", "Mac OS"), \
             mock.patch.dict(mod.os.environ):
            mod.os.environ.pop("WACLI_DEVICE_PLATFORM", None)
            mod.os.environ.pop("WACLI_DEVICE_LABEL", None)
            env = mod.wacli_device_env()
        self.assertEqual(env["WACLI_DEVICE_PLATFORM"], "DESKTOP")
        self.assertEqual(env["WACLI_DEVICE_LABEL"], "Mac OS")

    def test_wacli_device_env_respects_preset_environment(self) -> None:
        with mock.patch.dict(mod.os.environ, {
            "WACLI_DEVICE_PLATFORM": "DESKTOP",
            "WACLI_DEVICE_LABEL": "my custom label",
            "WACLI_DEVICE_FULL_SYNC_DAYS": "1000",
        }):
            env = mod.wacli_device_env()
        self.assertEqual(env["WACLI_DEVICE_PLATFORM"], "DESKTOP")
        self.assertEqual(env["WACLI_DEVICE_LABEL"], "my custom label")
        self.assertEqual(env["WACLI_DEVICE_FULL_SYNC_DAYS"], "1000")

    def test_wacli_device_env_defaults_include_full_sync(self) -> None:
        with mock.patch.dict(mod.os.environ):
            for key in ("WACLI_DEVICE_PLATFORM", "WACLI_DEVICE_LABEL", "WACLI_DEVICE_FULL_SYNC_DAYS"):
                mod.os.environ.pop(key, None)
            env = mod.wacli_device_env()
        self.assertEqual(env["WACLI_DEVICE_FULL_SYNC_DAYS"], "3650")

    def test_wa_qr_payload_handles_bare_and_wa_me_url_forms(self) -> None:
        # wacli <=0.11 bare ref
        self.assertEqual(mod.wa_qr_payload("2@abc,def,ghi"), "2@abc,def,ghi")
        # wacli 0.13 wa.me URL form — must encode the WHOLE url, not just the 2@ tail
        url = "https://wa.me/settings/linked_devices#2@abc,def,ghi"
        self.assertEqual(mod.wa_qr_payload(url), url)
        self.assertEqual(mod.wa_qr_payload(f"  {url}  "), url)
        # non-QR text
        self.assertIsNone(mod.wa_qr_payload("just a log line"))
        self.assertIsNone(mod.wa_qr_payload('{"event":"connected"}'))

    def test_wa_me_url_qr_is_redacted(self) -> None:
        url = "https://wa.me/settings/linked_devices#2@secret-ref,keys"
        self.assertEqual(mod.redact_qr_payloads(f"pre\n{url}\npost"),
                         f"pre\n{mod.QR_REDACTION}\npost")

    def test_pinned_release_points_at_powerset_fork(self) -> None:
        self.assertEqual(mod.WACLI_REPO, "powerset-co/wacli")
        self.assertTrue(mod.WACLI_PINNED_VERSION.startswith("v0.13.0"))
        self.assertIn("powerset-co/wacli/releases/download", mod.WACLI_RELEASE_BASE)

    def test_wacli_asset_name_and_download_url_by_platform(self) -> None:
        with mock.patch.object(mod.platform, "system", return_value="Darwin"), \
             mock.patch.object(mod.platform, "machine", return_value="arm64"):
            self.assertEqual(mod.wacli_asset_name(), "wacli-darwin-arm64")
        with mock.patch.object(mod.platform, "system", return_value="Linux"), \
             mock.patch.object(mod.platform, "machine", return_value="x86_64"):
            self.assertEqual(mod.wacli_asset_name(), "wacli-linux-amd64")
            url = mod.wacli_download_url()
            self.assertTrue(url.endswith(f"/{mod.WACLI_PINNED_VERSION}/wacli-linux-amd64"))
        # unsupported platform -> no asset / url
        with mock.patch.object(mod.platform, "system", return_value="Windows"), \
             mock.patch.object(mod.platform, "machine", return_value="AMD64"):
            self.assertIsNone(mod.wacli_asset_name())
            self.assertIsNone(mod.wacli_download_url())

    def test_wacli_bin_prefers_pinned_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pinned = Path(td) / "wacli"
            pinned.write_text("#!/bin/sh\n"); pinned.chmod(0o755)
            with mock.patch.object(mod, "WACLI_PINNED_BIN", pinned), \
                 mock.patch.object(mod.shutil, "which", return_value="/opt/homebrew/bin/wacli"):
                self.assertEqual(mod.wacli_bin(), str(pinned))
            # falls back to PATH when the pinned binary is absent
            with mock.patch.object(mod, "WACLI_PINNED_BIN", Path(td) / "absent"), \
                 mock.patch.object(mod.shutil, "which", return_value="/opt/homebrew/bin/wacli"):
                self.assertEqual(mod.wacli_bin(), "/opt/homebrew/bin/wacli")

    def _fake_install(self, td: Path):
        """Context helpers pinning the fork binary + stamp into a temp dir."""
        binp = td / "wacli"
        binp.write_text("#!/bin/sh\n"); binp.chmod(0o755)
        stamp = td / ".wacli-version"
        return binp, stamp

    def test_wacli_pinned_current_requires_matching_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            binp, stamp = self._fake_install(td)
            with mock.patch.object(mod, "WACLI_PINNED_BIN", binp), \
                 mock.patch.object(mod, "WACLI_VERSION_STAMP", stamp), \
                 mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.13.0-fullsync"):
                # binary present but no stamp -> not current
                self.assertFalse(mod.wacli_pinned_current())
                # stamp matches -> current
                stamp.write_text("v0.13.0-fullsync\n")
                self.assertTrue(mod.wacli_pinned_current())
                # a bumped pin makes the old stamp stale
                with mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.14.0-fullsync"):
                    self.assertFalse(mod.wacli_pinned_current())

    def test_ensure_wacli_auto_downloads_even_with_no_install(self) -> None:
        # The pinned fork is our own component, so it auto-downloads regardless of
        # the --no-install flag (which only ever gated the old brew path).
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            binp, stamp = self._fake_install(td)
            stamp.write_text("v0.13.0-fullsync\n")  # installed = old pin (stale)
            got = {}

            def fake_download(url, dest, *, timeout=120):
                got["url"] = url
                Path(dest).write_text("#!/bin/sh\n")

            with mock.patch.object(mod, "WACLI_PINNED_BIN", binp), \
                 mock.patch.object(mod, "WACLI_VERSION_STAMP", stamp), \
                 mock.patch.object(mod, "WACLI_BIN_DIR", td), \
                 mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.14.0-fullsync"), \
                 mock.patch.object(mod.platform, "system", return_value="Darwin"), \
                 mock.patch.object(mod.platform, "machine", return_value="arm64"), \
                 mock.patch.object(mod, "download_file", side_effect=fake_download), \
                 mock.patch.object(mod, "wacli_version", return_value={"path": str(binp), "version": "0.14.0", "pinned": True}):
                mod.ensure_wacli_installed(install=False)  # --no-install still auto-downloads
            self.assertTrue(got["url"].endswith("/v0.14.0-fullsync/wacli-darwin-arm64"))
            self.assertEqual(stamp.read_text().strip(), "v0.14.0-fullsync")

    def test_ensure_wacli_blocks_on_unsupported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            binp, stamp = self._fake_install(td)  # stale / no matching stamp
            with mock.patch.object(mod, "WACLI_PINNED_BIN", binp), \
                 mock.patch.object(mod, "WACLI_VERSION_STAMP", stamp), \
                 mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.14.0-fullsync"), \
                 mock.patch.object(mod.platform, "system", return_value="Windows"), \
                 mock.patch.object(mod.platform, "machine", return_value="AMD64"):
                with self.assertRaises(mod.PrimitiveBlocked) as ctx:
                    mod.ensure_wacli_installed(install=False)
            self.assertIn("No prebuilt wacli", ctx.exception.payload["message"])

    def test_ensure_wacli_downloads_on_pin_bump_and_restamps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            binp, stamp = self._fake_install(td)
            stamp.write_text("v0.13.0-fullsync\n")  # stale
            got = {}

            def fake_download(url, dest, *, timeout=120):
                got["url"] = url
                Path(dest).write_text("#!/bin/sh\n")

            with mock.patch.object(mod, "WACLI_PINNED_BIN", binp), \
                 mock.patch.object(mod, "WACLI_VERSION_STAMP", stamp), \
                 mock.patch.object(mod, "WACLI_BIN_DIR", td), \
                 mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.14.0-fullsync"), \
                 mock.patch.object(mod.platform, "system", return_value="Linux"), \
                 mock.patch.object(mod.platform, "machine", return_value="x86_64"), \
                 mock.patch.object(mod, "download_file", side_effect=fake_download), \
                 mock.patch.object(mod, "wacli_version", return_value={"path": str(binp), "version": "0.14.0", "pinned": True}):
                out = mod.ensure_wacli_installed(install=True)
            self.assertTrue(got["url"].endswith("/v0.14.0-fullsync/wacli-linux-amd64"))
            self.assertEqual(stamp.read_text().strip(), "v0.14.0-fullsync")
            self.assertEqual(out["version"], "0.14.0")

    def test_cmd_ensure_wacli_reports_ok_when_current(self) -> None:
        import argparse as _argparse
        buf = io.StringIO()
        with mock.patch.object(mod, "ensure_wacli_installed",
                               return_value={"path": "/x/wacli", "version": "wacli 0.13.0", "pinned": True}), \
             redirect_stdout(buf):
            rc = mod.cmd_ensure_wacli(_argparse.Namespace())
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["command"], "ensure-wacli")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["pinned_version"], mod.WACLI_PINNED_VERSION)

    def test_ensure_wacli_does_not_stamp_on_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            binp, stamp = self._fake_install(td)  # no matching stamp -> needs install
            def boom(url, dest, *, timeout=120):
                raise OSError("network down")
            with mock.patch.object(mod, "WACLI_PINNED_BIN", binp), \
                 mock.patch.object(mod, "WACLI_VERSION_STAMP", stamp), \
                 mock.patch.object(mod, "WACLI_BIN_DIR", td), \
                 mock.patch.object(mod, "WACLI_PINNED_VERSION", "v0.14.0-fullsync"), \
                 mock.patch.object(mod.platform, "system", return_value="Darwin"), \
                 mock.patch.object(mod.platform, "machine", return_value="arm64"), \
                 mock.patch.object(mod, "download_file", side_effect=boom):
                with self.assertRaises(mod.PrimitiveBlocked) as ctx:
                    mod.ensure_wacli_installed(install=True)
            self.assertIn("Failed to download", ctx.exception.payload["message"])
            self.assertFalse(stamp.exists())  # not stamped -> next run retries


if __name__ == "__main__":
    unittest.main()
