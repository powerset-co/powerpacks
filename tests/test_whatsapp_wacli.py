from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/ingestion/primitives/discover/messages/whatsapp_wacli.py"

spec = importlib.util.spec_from_file_location("whatsapp_wacli", PRIMITIVE)
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

        # The primitive makes the only decision: full on an empty store,
        # incremental once populated.
        self.assertEqual(mod.resolve_effective_max(0, 0), 0)
        self.assertEqual(mod.resolve_effective_max(0, 25000), incr)

        # An explicit positive low-level cap still wins.
        self.assertEqual(mod.resolve_effective_max(10000, 25000), 26000)
        self.assertEqual(mod.resolve_effective_max(10000, 0), 10000)

    def test_cold_bootstrap_default_allows_three_hours(self) -> None:
        self.assertEqual(mod.DEFAULT_AUTH_TIMEOUT, 10800)
        self.assertEqual(mod.DEFAULT_SYNC_TIMEOUT, 10800)

    def test_history_depth_defaults_are_paced(self) -> None:
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT, 1)
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_BATCH_SIZE, 10)
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT, 10)
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT, "10s")
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_BATCH_DELAY, "10s")
        self.assertEqual(mod.DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF, "1m")

    def test_auth_status_carries_linked_jid_for_self_exclusion(self) -> None:
        linked_jid = "15550009999@s.whatsapp.net"
        with mock.patch.object(
            mod,
            "wacli_json",
            return_value={
                "success": True,
                "data": {
                    "authenticated": True,
                    "linked_jid": linked_jid,
                },
            },
        ):
            public_status = mod.auth_status(Path("/tmp/wacli-store"))
            status = mod.auth_status(
                Path("/tmp/wacli-store"),
                include_linked_jid=True,
            )

        self.assertNotIn("linked_jid", public_status)
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["linked_jid"], linked_jid)

    def test_history_depth_cutoff_is_three_calendar_years(self) -> None:
        leap_day = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)
        cutoff = datetime.fromtimestamp(
            mod.history_depth_cutoff_ts(leap_day),
            timezone.utc,
        )
        self.assertEqual(cutoff, datetime(2021, 2, 28, 12, tzinfo=timezone.utc))

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

    def test_auth_waits_for_wacli_bootstrap_sync_after_connected_event(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("2@qr-payload\n")
                self.stderr = io.StringIO('{"event":"connected","ts":1}\n')
                self.returncode = None
                self.signals: list[int] = []
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                if self.poll_calls > 1:
                    self.returncode = 0
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

        self.assertEqual(fake.signals, [])
        self.assertTrue(result["connected_event"])
        self.assertTrue(result["auth_bootstrap_sync_completed"])
        self.assertIn("--events", result["command"])
        update_qr_page.assert_called()

    def test_auth_can_render_qr_without_opening_browser(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("2@qr-payload\n")
                self.stderr = io.StringIO('{"event":"connected","ts":1}\n')
                self.returncode = None
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                if self.poll_calls > 1:
                    self.returncode = 0
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

    def test_auth_fails_when_bootstrap_exits_nonzero_after_connected(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO('{"event":"connected","ts":1}\n')
                self.returncode = None
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                if self.poll_calls > 1:
                    self.returncode = 1
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

            def wait(self) -> int:
                return self.returncode if self.returncode is not None else 1

        with mock.patch.object(mod.shutil, "which", return_value="/opt/homebrew/bin/qrencode"), \
                mock.patch.object(mod.subprocess, "Popen", return_value=FakeProc()), \
                self.assertRaises(mod.PrimitiveFailed) as ctx:
            mod.run_auth(Path("/tmp/wacli-store"), timeout=5, idle_exit="30s")

        self.assertIn("initial history sync did not finish", str(ctx.exception))

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
        self.assertEqual(mod.WACLI_PINNED_VERSION, "v0.14.0-fullsync")
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
            pinned.write_text("#!/bin/sh\n")
            pinned.chmod(0o755)
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
        binp.write_text("#!/bin/sh\n")
        binp.chmod(0o755)
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

    def test_cmd_ensure_wacli_reports_action_and_version(self) -> None:
        import argparse as _argparse
        # already current -> action "current"
        buf = io.StringIO()
        with mock.patch.object(mod, "wacli_pinned_current", return_value=True), \
             mock.patch.object(mod, "ensure_wacli_installed",
                               return_value={"path": "/x/wacli", "version": "wacli 0.13.0", "pinned": True}), \
             redirect_stdout(buf):
            rc = mod.cmd_ensure_wacli(_argparse.Namespace())
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["command"], "ensure-wacli")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["action"], "current")
        self.assertEqual(payload["pinned_version"], mod.WACLI_PINNED_VERSION)
        # was stale/missing -> action "downloaded"
        buf = io.StringIO()
        with mock.patch.object(mod, "wacli_pinned_current", return_value=False), \
             mock.patch.object(mod, "ensure_wacli_installed",
                               return_value={"path": "/x/wacli", "version": "wacli 0.13.0", "pinned": True}), \
             redirect_stdout(buf):
            mod.cmd_ensure_wacli(_argparse.Namespace())
        self.assertEqual(json.loads(buf.getvalue())["action"], "downloaded")

    def test_pairing_full_sync_status_detects_pre_full_sync_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            # not authenticated
            self.assertEqual(mod.pairing_full_sync_status(store, authenticated=False)["state"],
                             "not_authenticated")
            # authenticated but no marker -> paired the old way, can deepen
            pre = mod.pairing_full_sync_status(store, authenticated=True)
            self.assertEqual(pre["state"], "pre_full_sync")
            self.assertTrue(pre["can_deepen"])
            self.assertIn("Re-link", pre["hint"])
            # after our flow stamps the pairing -> full_sync, no re-link needed
            mod.write_pairing_marker(store)
            full = mod.pairing_full_sync_status(store, authenticated=True)
            self.assertEqual(full["state"], "full_sync")
            self.assertFalse(full["can_deepen"])
            self.assertEqual(full["paired_wacli_version"], mod.WACLI_PINNED_VERSION)

    def test_pairing_marker_is_written_with_full_sync_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            mod.write_pairing_marker(store)
            marker = json.loads((store / mod.PAIRING_MARKER_NAME).read_text())
            self.assertIs(marker["full_sync"], True)
            self.assertEqual(marker["wacli_version"], mod.WACLI_PINNED_VERSION)
            self.assertEqual(marker["full_sync_days"], mod.DEFAULT_FULL_SYNC_DAYS)

    def test_read_pairing_marker_tolerates_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            (store / mod.PAIRING_MARKER_NAME).write_text("{not json")
            self.assertIsNone(mod.read_pairing_marker(store))

    def test_logout_is_noop_when_not_authenticated_but_clears_marker(self) -> None:
        import argparse as _argparse
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            mod.write_pairing_marker(store)  # a stale marker should still be cleared
            buf = io.StringIO()
            with mock.patch.object(mod, "ensure_wacli_installed", return_value={"pinned": True}), \
                 mock.patch.object(mod, "auth_status", return_value={"authenticated": False}), \
                 mock.patch.object(mod, "wacli_json") as wacli_json, \
                 redirect_stdout(buf):
                rc = mod.cmd_logout(_argparse.Namespace(store=str(store)))
            self.assertEqual(rc, 0)
            wacli_json.assert_not_called()  # nothing to invalidate
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["command"], "logout")
            self.assertEqual(payload["status"], "ok")
            self.assertFalse(payload["authenticated_before"])
            self.assertTrue(payload["marker_removed"])
            self.assertFalse((store / mod.PAIRING_MARKER_NAME).exists())

    def test_logout_invalidates_session_when_authenticated(self) -> None:
        import argparse as _argparse
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            buf = io.StringIO()
            with mock.patch.object(mod, "ensure_wacli_installed", return_value={"pinned": True}), \
                 mock.patch.object(mod, "auth_status",
                                   side_effect=[{"authenticated": True}, {"authenticated": False}]), \
                 mock.patch.object(mod, "wacli_json", return_value={"success": True}) as wacli_json, \
                 redirect_stdout(buf):
                rc = mod.cmd_logout(_argparse.Namespace(store=str(store)))
            self.assertEqual(rc, 0)
            wacli_json.assert_called_once_with(store, ["auth", "logout"], timeout=60)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["authenticated_before"])
            self.assertFalse(payload["authenticated_after"])

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

    def test_history_depth_targets_use_actual_timestamps_and_changed_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "wacli"
            create_wacli_db(store)
            conn = sqlite3.connect(store / "wacli.db")
            try:
                recent_ts = 1768000000
                conn.executemany(
                    "INSERT INTO chats (jid, kind, name, last_message_ts) VALUES (?, ?, ?, ?)",
                    [
                        ("15550001111@s.whatsapp.net", "dm", "Recent", 1),
                        ("15550002222@s.whatsapp.net", "dm", "Stale", recent_ts),
                        ("15550003333@g.us", "group", "Group", recent_ts),
                    ],
                )
                conn.executemany(
                    "INSERT INTO messages "
                    "(chat_jid, chat_name, msg_id, sender_jid, sender_name, ts, from_me, text, display_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "15550001111@s.whatsapp.net",
                            "Recent",
                            "recent-message",
                            "15550001111@s.whatsapp.net",
                            "Recent",
                            recent_ts,
                            0,
                            "PRIVATE RECENT BODY",
                            "PRIVATE RECENT BODY",
                        ),
                        (
                            "15550002222@s.whatsapp.net",
                            "Stale",
                            "stale-message",
                            "15550002222@s.whatsapp.net",
                            "Stale",
                            1700000000,
                            0,
                            "PRIVATE STALE BODY",
                            "PRIVATE STALE BODY",
                        ),
                        (
                            "15550003333@g.us",
                            "Group",
                            "group-message",
                            "15550003333@g.us",
                            "Group",
                            recent_ts,
                            0,
                            "PRIVATE GROUP BODY",
                            "PRIVATE GROUP BODY",
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            targets = mod.history_depth_targets(
                store,
                active_since_ts=1767225600,
                max_count=20,
                bootstrap=True,
            )
            self.assertEqual([target.chat_jid for target in targets], ["15550001111@s.whatsapp.net"])

            unchanged = mod.history_depth_targets(
                store,
                active_since_ts=1767225600,
                max_count=20,
                before_states={
                    "15550001111@s.whatsapp.net": (1, recent_ts),
                    "15550002222@s.whatsapp.net": (1, 1700000000),
                    "15550003333@g.us": (1, recent_ts),
                },
            )
            self.assertEqual(unchanged, [])

            changed = mod.history_depth_targets(
                store,
                active_since_ts=1767225600,
                max_count=20,
                before_states={"15550001111@s.whatsapp.net": (0, 0)},
            )
            self.assertEqual([target.chat_jid for target in changed], ["15550001111@s.whatsapp.net"])
            self.assertEqual(changed[0].current_latest_ts, recent_ts)
            self.assertTrue(changed[0].state_changed)

            resumed = mod.history_depth_targets(
                store,
                active_since_ts=1767225600,
                max_count=20,
                before_states={"15550001111@s.whatsapp.net": (1, recent_ts)},
                resume_refs={mod.history_chat_ref("15550001111@s.whatsapp.net")},
            )
            self.assertEqual([target.chat_jid for target in resumed], ["15550001111@s.whatsapp.net"])
            self.assertFalse(resumed[0].state_changed)

            self.assertEqual(
                mod.history_depth_targets(
                    store,
                    active_since_ts=1767225600,
                    max_count=20,
                    bootstrap=True,
                    exclude_jids={"15550001111@s.whatsapp.net"},
                ),
                [],
            )

            states = mod.history_depth_chat_states(store)
            self.assertEqual(states["15550001111@s.whatsapp.net"], (1, recent_ts))

    def test_history_backfill_attempt_uses_throttled_ten_request_command(self) -> None:
        target = mod.HistoryDepthTarget(
            chat_jid="15550001111@s.whatsapp.net",
            chat_ref=mod.history_chat_ref("15550001111@s.whatsapp.net"),
            kind="dm",
            current_count=1,
        )
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "json": {
                    "data": {
                        "chats": [{
                            "chat": target.chat_jid,
                            "requests_sent": 4,
                            "responses_seen": 4,
                            "messages_received": 5,
                            "end_type": "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY",
                            "error": "",
                        }],
                    },
                },
            }

        with mock.patch.object(
                mod,
                "history_depth_counts",
                side_effect=[(1, 10, 1768000000), (6, 18, 1768000000)],
            ), \
                mock.patch.object(
                    mod,
                    "history_depth_total_count",
                    side_effect=[10, 18],
                ), \
                mock.patch.object(mod, "wacli_bin", return_value="/tmp/wacli"), \
                mock.patch.object(mod, "run_command", side_effect=fake_run):
            attempt = mod.run_history_backfill_attempt(
                Path("/tmp/store"),
                target,
                request_delay="10s",
                timeout=900,
            )

        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("--requests") + 1], "10")
        self.assertEqual(cmd[cmd.index("--count") + 1], "500")
        self.assertEqual(cmd[cmd.index("--request-delay") + 1], "10s")
        self.assertIn("backfill-batch", cmd)
        self.assertEqual(cmd[cmd.index("--batch-size") + 1], "1")
        self.assertEqual(cmd[cmd.index("--max-inflight") + 1], "1")
        self.assertEqual(attempt.target_added, 5)
        self.assertEqual(attempt.unrelated_added, 3)
        self.assertEqual(attempt.requests_sent, 4)
        self.assertEqual(attempt.messages_received, 5)
        self.assertEqual(attempt.after_latest_ts, 1768000000)
        self.assertEqual(captured["kwargs"]["timeout"], 900)

    def test_history_backfill_success_without_protocol_response_is_retryable(self) -> None:
        target = mod.HistoryDepthTarget(
            chat_jid="15550001111@s.whatsapp.net",
            chat_ref=mod.history_chat_ref("15550001111@s.whatsapp.net"),
            kind="dm",
            current_count=1,
        )
        with mock.patch.object(
                mod,
                "history_depth_counts",
                side_effect=[(1, 10, 1768000000), (1, 10, 1768000000)],
            ), mock.patch.object(
                mod,
                "wacli_bin",
                return_value="/tmp/wacli",
            ), mock.patch.object(
                mod,
                "run_command",
                return_value={
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "json": {
                        "data": {
                            "chats": [{
                                "chat": target.chat_jid,
                                "requests_sent": 1,
                                "responses_seen": 0,
                                "messages_received": 0,
                                "error": "timed out waiting for on-demand history sync response",
                            }],
                        },
                    },
                },
            ), mock.patch.object(
                mod,
                "history_depth_total_count",
                return_value=10,
            ):
            attempt = mod.run_history_backfill_attempt(
                Path("/tmp/store"),
                target,
            )

        self.assertEqual(attempt.error_category, "timeout")
        self.assertTrue(attempt.retryable)

    def test_history_backfill_batch_normalizes_mixed_per_chat_results(self) -> None:
        targets = [
            mod.HistoryDepthTarget(
                f"1555000{suffix}@s.whatsapp.net",
                mod.history_chat_ref(f"1555000{suffix}@s.whatsapp.net"),
                "dm",
                1,
                1768000000,
            )
            for suffix in ("1111", "2222", "3333")
        ]
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "json": {
                    "data": {
                        "chats": [
                            {
                                "chat": targets[1].chat_jid,
                                "requests_sent": 2,
                                "responses_seen": 0,
                                "messages_received": 0,
                                "error": "timed out waiting for on-demand history sync response",
                            },
                            {
                                "chat": targets[0].chat_jid,
                                "requests_sent": 1,
                                "responses_seen": 1,
                                "messages_received": 2,
                                "end_type": "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY",
                                "error": "",
                            },
                        ],
                    },
                },
            }

        with mock.patch.object(
                mod,
                "history_depth_counts",
                side_effect=[
                    (1, 10, 1768000000),
                    (1, 10, 1768000000),
                    (1, 10, 1768000000),
                    (3, 13, 1768000000),
                    (1, 13, 1768000000),
                    (1, 13, 1768000000),
                ],
            ), mock.patch.object(
                mod,
                "history_depth_total_count",
                side_effect=[10, 13],
            ), mock.patch.object(
                mod,
                "wacli_bin",
                return_value="/tmp/wacli",
            ), mock.patch.object(
                mod,
                "run_command",
                side_effect=fake_run,
            ):
            attempts, unrelated = mod.run_history_backfill_batch_attempt(
                Path("/tmp/store"),
                targets,
                timeout=123,
            )

        cmd = captured["cmd"]
        self.assertEqual(cmd.count("--chat"), 3)
        self.assertEqual(cmd[cmd.index("--batch-size") + 1], "10")
        self.assertEqual(cmd[cmd.index("--max-inflight") + 1], "10")
        self.assertEqual(cmd[cmd.index("--timeout-backoff") + 1], "1m")
        self.assertEqual(captured["kwargs"]["timeout"], 123)
        self.assertEqual(attempts[targets[0].chat_ref].target_added, 2)
        self.assertEqual(
            attempts[targets[0].chat_ref].end_type,
            "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY",
        )
        self.assertEqual(attempts[targets[1].chat_ref].error_category, "timeout")
        self.assertTrue(attempts[targets[1].chat_ref].retryable)
        self.assertEqual(attempts[targets[2].chat_ref].error_category, "missing_result")
        self.assertTrue(attempts[targets[2].chat_ref].retryable)
        self.assertEqual(unrelated, 1)

    def test_history_depth_zero_response_idle_exit_stays_pending(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(
            jid,
            mod.history_chat_ref(jid),
            "dm",
            1,
        )
        no_response = mod.HistoryDepthAttempt(
            0, 1, 0, 0, 0, 1, "timeout", True
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=no_response,
                ) as run_attempt:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(
                out_dir / "results.csv"
            )[target.chat_ref]

        run_attempt.assert_called_once()
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["no_growth_attempts"], "0")
        self.assertEqual(row["transient_failures"], "1")

    def test_history_depth_received_duplicates_are_not_server_zero(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(
            jid,
            mod.history_chat_ref(jid),
            "dm",
            1,
        )
        duplicates = mod.HistoryDepthAttempt(
            0, 1, 1, 0, 0, 1, "none", False, messages_received=3
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=duplicates,
                ):
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(
                out_dir / "results.csv"
            )[target.chat_ref]

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["no_growth_attempts"], "0")

    def test_history_depth_includes_legacy_unknown_direct_chat(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "wacli"
            create_wacli_db(store)
            conn = sqlite3.connect(store / "wacli.db")
            try:
                conn.execute(
                    "INSERT INTO chats (jid, kind, name, last_message_ts) VALUES (?, ?, ?, ?)",
                    (jid, "unknown", "Legacy Direct", 1768000000),
                )
                conn.execute(
                    "INSERT INTO messages "
                    "(chat_jid, chat_name, msg_id, sender_jid, sender_name, ts, from_me, text, display_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        jid,
                        "Legacy Direct",
                        "legacy-message",
                        jid,
                        "Legacy",
                        1768000000,
                        0,
                        "PRIVATE BODY",
                        "PRIVATE BODY",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            targets = mod.history_depth_targets(
                store,
                active_since_ts=1767225600,
                bootstrap=True,
            )

        self.assertEqual([target.chat_jid for target in targets], [jid])

    def test_history_depth_clean_zero_is_terminal_and_private(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(
            chat_jid=jid,
            chat_ref=mod.history_chat_ref(jid),
            kind="dm",
            current_count=1,
        )
        zero = mod.HistoryDepthAttempt(
            returncode=0,
            requests_sent=1,
            responses_seen=1,
            target_added=0,
            unrelated_added=0,
            after_count=1,
            error_category="none",
            retryable=False,
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(mod, "run_history_backfill_attempt", return_value=zero) as run_attempt, \
                mock.patch.object(mod.time, "sleep") as sleep:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in out_dir.iterdir()
                if path.is_file()
            )
            rows = mod.read_history_depth_results(out_dir / "results.csv")

        run_attempt.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counts"]["server_zero"], 1)
        self.assertEqual(rows[target.chat_ref]["outcome"], "server_zero")
        self.assertEqual(rows[target.chat_ref]["no_growth_attempts"], "1")
        self.assertNotIn(jid, artifact_text)
        self.assertNotIn("15550001111", artifact_text)

    def test_history_depth_zero_with_more_remaining_stays_pending(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(
            chat_jid=jid,
            chat_ref=mod.history_chat_ref(jid),
            kind="dm",
            current_count=1,
        )
        zero_with_more = mod.HistoryDepthAttempt(
            returncode=0,
            requests_sent=1,
            responses_seen=1,
            target_added=0,
            unrelated_added=0,
            after_count=1,
            error_category="none",
            retryable=False,
            messages_received=0,
            end_type="COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY",
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=zero_with_more,
                ):
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(
                out_dir / "results.csv"
            )[target.chat_ref]

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["no_growth_attempts"], "0")

    def test_history_depth_pre_request_failure_does_not_consume_no_growth(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 1)
        connection = mod.HistoryDepthAttempt(
            1, 0, 0, 0, 0, 1, "connection", True
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=connection,
                ) as run_attempt, \
                mock.patch.object(mod.time, "sleep") as sleep:
            out_dir = Path(td) / "history-depth"
            mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(out_dir / "results.csv")[target.chat_ref]

        run_attempt.assert_called_once()
        self.assertEqual(row["no_growth_attempts"], "0")
        self.assertEqual(row["transient_failures"], "1")
        self.assertEqual(row["outcome"], "pending")
        sleep.assert_not_called()

    def test_history_depth_timeout_with_partial_growth_defers_chat(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 1)
        grew = mod.HistoryDepthAttempt(
            124, 1, 0, 3, 2, 4, "timeout", True
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=grew,
                ) as run_attempt, \
                mock.patch.object(mod.time, "sleep") as sleep:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(out_dir / "results.csv")[target.chat_ref]

        run_attempt.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["counts"]["target_rows_added"], 3)
        self.assertEqual(summary["counts"]["unrelated_rows_added"], 2)
        self.assertEqual(row["transient_failures"], "1")
        self.assertEqual(row["outcome"], "pending")

    def test_history_depth_uses_one_native_batch_without_python_pauses(self) -> None:
        targets = [
            mod.HistoryDepthTarget(
                f"1555000{suffix}@s.whatsapp.net",
                mod.history_chat_ref(f"1555000{suffix}@s.whatsapp.net"),
                "dm",
                1,
            )
            for suffix in ("1111", "2222", "3333")
        ]
        timeout = mod.HistoryDepthAttempt(
            124, 1, 0, 0, 0, 1, "timeout", True
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=targets), \
                mock.patch.object(
                    mod,
                    "run_history_backfill_batch_attempt",
                    return_value=(
                        {target.chat_ref: timeout for target in targets},
                        2,
                    ),
                ) as run_batch, \
                mock.patch.object(mod, "emit_status") as emit_status, \
                mock.patch.object(mod.time, "sleep") as sleep:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
                batch_size=10,
            )
            events = [
                json.loads(line)
                for line in (out_dir / "progress.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        run_batch.assert_called_once()
        self.assertEqual(run_batch.call_args.args[1], targets)
        sleep.assert_not_called()
        emit_status.assert_not_called()
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["policy"]["batch_size"], 10)
        self.assertEqual(summary["policy"]["max_in_flight"], 10)
        self.assertEqual(summary["policy"]["batch_delay"], "10s")
        self.assertTrue(summary["policy"]["native_batch_command"])
        self.assertTrue(summary["policy"]["one_command_per_run"])
        self.assertEqual(summary["counts"]["unrelated_rows_added"], 2)
        started = [
            event
            for event in events
            if event["event"] == "history_depth_batch_started"
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["targets"], 3)

    def test_history_depth_native_batch_excludes_completed_resume_rows(self) -> None:
        targets = [
            mod.HistoryDepthTarget(
                f"1555000{suffix}@s.whatsapp.net",
                mod.history_chat_ref(f"1555000{suffix}@s.whatsapp.net"),
                "dm",
                1,
                1768000000,
            )
            for suffix in ("1111", "2222", "3333")
        ]
        timeout = mod.HistoryDepthAttempt(
            124, 1, 0, 0, 0, 1, "timeout", True
        )
        zero = mod.HistoryDepthAttempt(
            0, 1, 1, 0, 0, 1, "none", False
        )
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            mod.write_history_depth_results(out_dir / "results.csv", {
                targets[1].chat_ref: {
                    "chat_ref": targets[1].chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 1,
                    "current_latest_ts": 1768000000,
                    "target_rows_added": 0,
                    "unrelated_rows_added": 0,
                    "attempts": 1,
                    "requests_sent": 1,
                    "responses_seen": 1,
                    "transient_failures": 0,
                    "no_growth_attempts": 1,
                    "outcome": "server_zero",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            current_states = {
                target.chat_jid: (target.current_count, target.current_latest_ts)
                for target in targets
            }
            with mock.patch.object(
                    mod,
                    "history_depth_chat_states",
                    return_value=current_states,
                ), mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=targets,
                ), mock.patch.object(
                    mod,
                    "run_history_backfill_batch_attempt",
                    return_value=({
                        targets[0].chat_ref: timeout,
                        targets[2].chat_ref: zero,
                    }, 0),
                ) as run_batch, mock.patch.object(
                    mod,
                    "emit_status",
                ), mock.patch.object(
                    mod.time,
                    "sleep",
                ) as sleep:
                mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    batch_size=10,
                )
            events = [
                json.loads(line)
                for line in (out_dir / "progress.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        run_batch.assert_called_once()
        attempted_targets = run_batch.call_args.args[1]
        self.assertEqual(
            [target.chat_ref for target in attempted_targets],
            [targets[0].chat_ref, targets[2].chat_ref],
        )
        sleep.assert_not_called()
        started = [
            event
            for event in events
            if event["event"] == "history_depth_batch_started"
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["targets"], 2)

    def test_history_depth_timeout_growth_past_shallow_threshold_is_recovered(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 1)
        grew = mod.HistoryDepthAttempt(
            124, 1, 0, 23, 0, 24, "timeout", True
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(mod, "run_history_backfill_attempt", return_value=grew) as run_attempt:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            row = mod.read_history_depth_results(out_dir / "results.csv")[target.chat_ref]

        run_attempt.assert_called_once()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(row["outcome"], "completed_threshold")

    def test_history_depth_seeds_all_targets_before_budget_check(self) -> None:
        targets = [
            mod.HistoryDepthTarget(
                f"1555000{suffix}@s.whatsapp.net",
                mod.history_chat_ref(f"1555000{suffix}@s.whatsapp.net"),
                "dm",
                1,
            )
            for suffix in ("1111", "2222")
        ]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=targets), \
                mock.patch.object(mod, "history_depth_total_count", return_value=2), \
                mock.patch.object(mod.time, "monotonic", side_effect=[0, 2]), \
                mock.patch.object(mod, "run_history_backfill_attempt") as run_attempt:
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
                time_budget_seconds=1,
            )
            rows = mod.read_history_depth_results(out_dir / "results.csv")

        run_attempt.assert_not_called()
        self.assertEqual(set(rows), {target.chat_ref for target in targets})
        self.assertTrue(all(row["outcome"] == "pending" for row in rows.values()))
        self.assertEqual(summary["status"], "partial")

    def test_history_depth_recovers_pre_sync_count_drift_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            out_dir.mkdir(parents=True)
            mod.write_history_depth_results(out_dir / "results.csv", {})
            (out_dir / "manifest.json").write_text(
                json.dumps({
                    "policy": {"version": mod.HISTORY_DEPTH_POLICY_VERSION},
                    "counts": {"source_total_messages": 10},
                    "source": {
                        "dm_state_sha256": mod.history_depth_state_digest({}),
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(mod, "history_depth_targets", return_value=[]) as targets, \
                    mock.patch.object(mod, "history_depth_total_count", return_value=12):
                summary = mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    before_total_messages=12,
                )

        self.assertTrue(targets.call_args.kwargs["bootstrap"])
        self.assertTrue(summary["policy"]["recovered_pre_sync_changes"])

    def test_history_depth_recovers_pre_sync_timestamp_drift_from_manifest(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        old_states = {jid: (1, 1768000000)}
        new_states = {jid: (1, 1769000000)}
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            out_dir.mkdir(parents=True)
            mod.write_history_depth_results(out_dir / "results.csv", {})
            (out_dir / "manifest.json").write_text(
                json.dumps({
                    "policy": {"version": mod.HISTORY_DEPTH_POLICY_VERSION},
                    "counts": {"source_total_messages": 10},
                    "source": {
                        "dm_state_sha256": mod.history_depth_state_digest(old_states),
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                    mod,
                    "history_depth_chat_states",
                    return_value=new_states,
                ), mock.patch.object(
                    mod,
                    "history_depth_total_count",
                    return_value=10,
                ), mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=[],
                ) as targets:
                summary = mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    before_states=new_states,
                    before_total_messages=10,
                )

        self.assertTrue(targets.call_args.kwargs["bootstrap"])
        self.assertTrue(summary["policy"]["recovered_pre_sync_changes"])

    def test_history_depth_keeps_pre_target_watermark_for_catch_up(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 1)
        grew = mod.HistoryDepthAttempt(
            0, 1, 1, 20, 3, 21, "none", False
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                mock.patch.object(mod, "history_depth_total_count", return_value=10), \
                mock.patch.object(mod, "run_history_backfill_attempt", return_value=grew):
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=Path(td) / "history-depth",
                active_since_ts=1767225600,
                before_total_messages=10,
            )

        self.assertEqual(summary["counts"]["source_total_messages"], 10)
        self.assertEqual(summary["counts"]["unrelated_rows_added"], 3)

    def test_history_depth_reconciles_pending_target_above_threshold(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        chat_ref = mod.history_chat_ref(jid)
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            mod.write_history_depth_results(out_dir / "results.csv", {
                chat_ref: {
                    "chat_ref": chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 1,
                    "target_rows_added": 0,
                    "unrelated_rows_added": 0,
                    "attempts": 0,
                    "requests_sent": 0,
                    "responses_seen": 0,
                    "transient_failures": 0,
                    "no_growth_attempts": 0,
                    "outcome": "pending",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            with mock.patch.object(
                    mod,
                    "history_depth_chat_states",
                    return_value={jid: (21, 1768000000)},
                ), mock.patch.object(
                    mod,
                    "history_depth_total_count",
                    return_value=21,
                ), mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=[],
                ):
                mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                )
            row = mod.read_history_depth_results(out_dir / "results.csv")[chat_ref]

        self.assertEqual(row["current_count"], "21")
        self.assertEqual(row["outcome"], "completed_threshold")

    def test_history_depth_zero_targets_writes_complete_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "history_depth_targets", return_value=[]):
            out_dir = Path(td) / "history-depth"
            summary = mod.run_history_depth_stage(
                Path(td) / "wacli",
                out_dir=out_dir,
                active_since_ts=1767225600,
            )
            events = [
                json.loads(line)
                for line in (out_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertTrue((out_dir / "results.csv").is_file())
            self.assertTrue((out_dir / "manifest.json").is_file())

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(
            [event["event"] for event in events],
            ["history_depth_started", "history_depth_completed"],
        )
        self.assertEqual(events[-1]["eligible"], 0)

    def test_history_depth_resume_skips_unchanged_terminal_target(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 1)
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            mod.write_history_depth_results(out_dir / "results.csv", {
                target.chat_ref: {
                    "chat_ref": target.chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 1,
                    "target_rows_added": 0,
                    "unrelated_rows_added": 0,
                    "attempts": 2,
                    "requests_sent": 2,
                    "responses_seen": 2,
                    "transient_failures": 0,
                    "no_growth_attempts": 2,
                    "outcome": "server_zero",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            with mock.patch.object(mod, "history_depth_targets", return_value=[target]), \
                    mock.patch.object(mod, "run_history_backfill_attempt") as run_attempt:
                summary = mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                )

        run_attempt.assert_not_called()
        self.assertEqual(summary["status"], "completed")

    def test_history_depth_timestamp_change_reactivates_terminal_target(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(
            jid,
            mod.history_chat_ref(jid),
            "dm",
            1,
            state_changed=True,
        )
        zero = mod.HistoryDepthAttempt(
            0, 1, 1, 0, 0, 1, "none", False
        )
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            mod.write_history_depth_results(out_dir / "results.csv", {
                target.chat_ref: {
                    "chat_ref": target.chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 1,
                    "target_rows_added": 0,
                    "unrelated_rows_added": 0,
                    "attempts": 2,
                    "requests_sent": 2,
                    "responses_seen": 2,
                    "transient_failures": 0,
                    "no_growth_attempts": 2,
                    "outcome": "server_zero",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            with mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=[target],
                ), mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=zero,
                ) as run_attempt:
                summary = mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    no_growth_limit=1,
                )

        run_attempt.assert_called_once()
        self.assertEqual(summary["status"], "completed")

    def test_history_depth_digest_recovery_reopens_exact_timestamp_drift(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        old_ts = 1768000000
        new_ts = 1769000000
        target = mod.HistoryDepthTarget(
            jid,
            mod.history_chat_ref(jid),
            "dm",
            1,
            current_latest_ts=new_ts,
            state_changed=False,
        )
        zero = mod.HistoryDepthAttempt(
            0, 1, 1, 0, 0, 1, "none", False, after_latest_ts=new_ts
        )
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            out_dir.mkdir(parents=True)
            mod.write_history_depth_results(out_dir / "results.csv", {
                target.chat_ref: {
                    "chat_ref": target.chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 1,
                    "current_latest_ts": old_ts,
                    "target_rows_added": 0,
                    "unrelated_rows_added": 0,
                    "attempts": 2,
                    "requests_sent": 2,
                    "responses_seen": 2,
                    "transient_failures": 0,
                    "no_growth_attempts": 2,
                    "outcome": "server_zero",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            (out_dir / "manifest.json").write_text(
                json.dumps({
                    "policy": {"version": mod.HISTORY_DEPTH_POLICY_VERSION},
                    "counts": {"source_total_messages": 1},
                    "source": {
                        "dm_state_sha256": mod.history_depth_state_digest(
                            {jid: (1, old_ts)}
                        ),
                    },
                }),
                encoding="utf-8",
            )
            current_states = {jid: (1, new_ts)}
            with mock.patch.object(
                    mod,
                    "history_depth_chat_states",
                    return_value=current_states,
                ), mock.patch.object(
                    mod,
                    "history_depth_total_count",
                    return_value=1,
                ), mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=[target],
                ) as targets, mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=zero,
                ) as run_attempt:
                mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    before_states=current_states,
                    before_total_messages=1,
                    no_growth_limit=1,
                )

        self.assertTrue(targets.call_args.kwargs["bootstrap"])
        run_attempt.assert_called_once()

    def test_history_depth_preserves_completed_recovered_row(self) -> None:
        jid = "15550001111@s.whatsapp.net"
        target = mod.HistoryDepthTarget(jid, mod.history_chat_ref(jid), "dm", 6)
        zero = mod.HistoryDepthAttempt(
            0, 1, 1, 0, 0, 6, "none", False
        )
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "history-depth"
            mod.write_history_depth_results(out_dir / "results.csv", {
                target.chat_ref: {
                    "chat_ref": target.chat_ref,
                    "kind": "dm",
                    "initial_count": 1,
                    "current_count": 6,
                    "target_rows_added": 5,
                    "unrelated_rows_added": 0,
                    "attempts": 1,
                    "requests_sent": 1,
                    "responses_seen": 1,
                    "transient_failures": 0,
                    "no_growth_attempts": 0,
                    "outcome": "recovered",
                    "error_category": "none",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            with mock.patch.object(
                    mod,
                    "history_depth_targets",
                    return_value=[target],
                ), mock.patch.object(
                    mod,
                    "run_history_backfill_attempt",
                    return_value=zero,
                ) as run_attempt:
                mod.run_history_depth_stage(
                    Path(td) / "wacli",
                    out_dir=out_dir,
                    active_since_ts=1767225600,
                    no_growth_limit=1,
                )

        run_attempt.assert_not_called()

    def test_history_backfill_error_classification(self) -> None:
        cases = [
            (124, "", 1, ("timeout", True)),
            (1, "lookup web.whatsapp.com: no such host", 0, ("connection", True)),
            (1, "database is locked", 0, ("store_lock", True)),
            (1, "not authenticated", 0, ("unauthenticated", False)),
            (1, "forbidden: no access", 1, ("access_limited", False)),
            (1, "unexpected failure", 1, ("request_error", False)),
            (1, "unexpected failure", 0, ("command_error", False)),
        ]
        for returncode, stderr, requests_sent, expected in cases:
            with self.subTest(stderr=stderr, requests_sent=requests_sent):
                self.assertEqual(
                    mod.classify_history_backfill_error(
                        returncode=returncode,
                        stderr=stderr,
                        requests_sent=requests_sent,
                    ),
                    expected,
                )

    def test_run_command_normalizes_fast_path_timeout(self) -> None:
        expired = mod.subprocess.TimeoutExpired(
            cmd=["wacli"],
            timeout=1,
            output="partial output",
            stderr="partial error",
        )
        with mock.patch.object(mod.subprocess, "run", side_effect=expired):
            result = mod.run_command(["wacli"], timeout=1)
        self.assertEqual(result["returncode"], 124)
        self.assertEqual(result["stdout"], "partial output")
        self.assertIn("command timed out after 1s", result["stderr"])

    def test_cmd_run_chooses_sync_and_depth_from_store_state(self) -> None:
        diagnostics = {
            "contacts_with_message_count": 0,
            "contacts_in_groups": 0,
        }
        for existing_messages, expected_strategy, requested_max in (
            (0, "cold_full", 100),
            (10, "incremental", 0),
        ):
            with self.subTest(existing_messages=existing_messages), tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                args = type("Args", (), {
                    "store": tmp / "wacli",
                    "output_csv": tmp / "contacts.csv",
                    "output_jsonl": tmp / "contacts.jsonl",
                    "manifest": tmp / "contacts.manifest.json",
                    "progress_jsonl": tmp / "progress.jsonl",
                    "name_fallback_csv": None,
                    "no_install": False,
                    "auth_timeout": 60,
                    "idle_exit": "5s",
                    "no_open_qr_page": True,
                    "max_messages": requested_max,
                    "sync_timeout": 60,
                    "group_info_timeout": 60,
                    "group_info_interval": 0,
                    "include_left_groups": False,
                    "max_group_participants": 30,
                })()
                with mock.patch.object(
                    mod,
                    "ensure_wacli_installed",
                    return_value={"path": "/tmp/wacli", "version": "test"},
                ), mock.patch.object(
                    mod,
                    "wacli_json",
                    return_value={"status": "ok"},
                ), mock.patch.object(
                    mod,
                    "auth_status",
                    return_value={
                        "authenticated": True,
                        "linked_jid": "15550009999@s.whatsapp.net",
                    },
                ), mock.patch.object(
                    mod,
                    "pairing_full_sync_status",
                    return_value={"state": "full_sync"},
                ), mock.patch.object(
                    mod,
                    "store_stats",
                    side_effect=[
                        {"data": {"messages": existing_messages}},
                        {"data": {"messages": existing_messages}},
                    ],
                ), mock.patch.object(
                    mod,
                    "history_depth_chat_states",
                    return_value={"15550001111@s.whatsapp.net": (1, 1768000000)},
                ), mock.patch.object(
                    mod,
                    "history_depth_total_count",
                    side_effect=[existing_messages, existing_messages],
                ), mock.patch.object(
                    mod,
                    "run_sync",
                    return_value={"returncode": 0},
                ) as sync, mock.patch.object(
                    mod,
                    "run_history_depth_stage",
                    return_value={"status": "completed", "counts": {}},
                ) as depth, mock.patch.object(
                    mod,
                    "refresh_group_info",
                    return_value={"status": "ok"},
                ), mock.patch.object(
                    mod,
                    "refresh_contacts",
                    return_value={"status": "ok"},
                ), mock.patch.object(
                    mod,
                    "export_contacts_from_store",
                    return_value=({}, diagnostics),
                ), redirect_stdout(io.StringIO()):
                    rc = mod.cmd_run(args)

                self.assertEqual(rc, 0)
                depth.assert_called_once()
                self.assertEqual(
                    depth.call_args.kwargs["cold_start"],
                    existing_messages == 0,
                )
                self.assertEqual(
                    depth.call_args.kwargs["before_states"],
                    {"15550001111@s.whatsapp.net": (1, 1768000000)},
                )
                self.assertEqual(
                    depth.call_args.kwargs["before_total_messages"],
                    existing_messages,
                )
                self.assertEqual(
                    depth.call_args.kwargs["exclude_jids"],
                    {"15550009999@s.whatsapp.net"},
                )
                effective_max = sync.call_args.kwargs["max_messages"]
                if existing_messages == 0:
                    self.assertEqual(effective_max, 0)
                else:
                    self.assertGreater(effective_max, existing_messages)
                payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
                self.assertEqual(payload["sync"]["strategy"], expected_strategy)


if __name__ == "__main__":
    unittest.main()
