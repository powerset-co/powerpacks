"""Settings exposes configured provider names and masked tails without contacting vendors."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.shared.web import providers
from packs.shared.web.server import persistent_handler


class ProviderSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_required_connections_precede_optional_connections(self) -> None:
        rows = list(self.rows().values())
        self.assertEqual([row["id"] for row in rows],
                         ["parallel", "openai", "typesafe", "rapidapi-linkedin", "modal"])
        self.assertEqual(rows[2]["name"], "TypeSafe")

    def test_linkedin_key_is_required_only_without_powerset_key(self) -> None:
        self.assertFalse(self.rows()["rapidapi-linkedin"]["connected"])
        with patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "synthetic-linkedin-1234"}):
            row = self.rows()["rapidapi-linkedin"]
            self.assertTrue(row["connected"])
            self.assertEqual(row["credentials"][0]["masked"], "••••1234")
            with patch.dict(os.environ, {"POWERSET_API_KEY": "synthetic-powerset"}):
                self.assertEqual(self.rows()["rapidapi-linkedin"]["name"], "RapidAPI · LinkedIn (optional)")

    def rows(self) -> dict[str, dict]:
        return {row["id"]: row for row in providers.read(self.root)["providers"]}

    def test_masks_only_catalog_credentials_and_keeps_environment_unchanged(self) -> None:
        content = "OPENAI_API_KEY=synthetic-openai-1234\nUNRELATED_SECRET=synthetic-unrelated-secret\n"
        (self.root / ".env").write_text(content)
        before = dict(os.environ)
        with patch("urllib.request.urlopen") as network:
            payload = providers.read(self.root)
        network.assert_not_called()
        row = next(row for row in payload["providers"] if row["id"] == "openai")
        self.assertTrue(row["connected"])
        self.assertEqual(row["credentials"], [{"name": "OPENAI_API_KEY", "configured": True, "masked": "••••1234"}])
        encoded = json.dumps(payload)
        for secret in ("synthetic-openai", "UNRELATED_SECRET", "synthetic-unrelated-secret"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual((self.root / ".env").read_text(), content)

    def test_process_overrides_file_and_empty_process_value_is_disconnected(self) -> None:
        template = self.root / providers.account.ENV_TEMPLATE
        template.parent.mkdir(parents=True)
        template.write_text("OPENAI_API_KEY=synthetic-template-1111\n")
        (self.root / ".env").write_text("OPENAI_API_KEY=synthetic-file-2222\n")
        self.assertEqual(self.rows()["openai"]["credentials"][0]["masked"], "••••2222")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-process-3333"}):
            self.assertEqual(self.rows()["openai"]["credentials"][0]["masked"], "••••3333")
        with patch.dict(os.environ, {"OPENAI_API_KEY": " "}):
            self.assertFalse(self.rows()["openai"]["connected"])

    def test_modal_requires_both_credentials(self) -> None:
        with patch.dict(os.environ, {"MODAL_TOKEN_ID": "synthetic-token-id"}):
            self.assertFalse(self.rows()["modal"]["connected"])
            with patch.dict(os.environ, {"MODAL_TOKEN_SECRET": "synthetic-token-secret"}):
                self.assertTrue(self.rows()["modal"]["connected"])

    def test_short_credentials_are_never_returned_in_full_and_missing_keys_have_links(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "tiny"}):
            self.assertEqual(self.rows()["openai"]["credentials"][0]["masked"], "••••")
        for row in self.rows().values():
            self.assertFalse(row["connected"])
            self.assertTrue(row["connect_url"].startswith("https://"))
            self.assertTrue(all(credential["masked"] is None for credential in row["credentials"]))

    def test_route_answers_before_dependencies_or_store_mount_and_is_not_cached(self) -> None:
        http = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        with patch("packs.shared.web.server.mounted_handler") as mount:
            with urllib.request.urlopen(f"http://127.0.0.1:{http.server_address[1]}/api/settings/providers") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(response.read()), providers.read(self.root))
        mount.assert_not_called()
        self.assertFalse((self.root / ".powerpacks").exists())


if __name__ == "__main__":
    unittest.main()


class ProviderKeyTests(unittest.TestCase):
    """A key typed into Settings lands in the checkout's .env; only provider credentials, never empty."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_typed_key_is_written_and_the_rows_answer_connected(self) -> None:
        (self.root / ".env").write_text("UNRELATED=keep\n")
        payload = providers.save(self.root, {"values": {"OPENAI_API_KEY": " synthetic-openai-9876 "}})
        row = next(row for row in payload["providers"] if row["id"] == "openai")
        self.assertTrue(row["connected"])
        self.assertEqual(row["credentials"][0]["masked"], "••••9876")
        content = (self.root / ".env").read_text()
        self.assertIn("UNRELATED=keep", content)
        self.assertIn("OPENAI_API_KEY=synthetic-openai-9876", content)

    def test_unknown_names_and_empty_values_are_refused(self) -> None:
        for payload in ({"values": {"POWERSET_API_KEY": "x"}}, {"values": {"OPENAI_API_KEY": " "}},
                        {"values": {}}, {"values": "x"}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                providers.save(self.root, payload)
        self.assertFalse((self.root / ".env").exists())

    def test_the_route_writes_a_key_and_refuses_another_origin(self) -> None:
        http = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = f"http://127.0.0.1:{http.server_address[1]}/api/settings/providers"
        body = json.dumps({"values": {"OPENAI_API_KEY": "synthetic-openai-4321"}}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST",
                                                           headers={"Content-Type": "application/json"})) as response:
            self.assertEqual(response.status, 200)
            row = next(row for row in json.loads(response.read())["providers"] if row["id"] == "openai")
            self.assertEqual(row["credentials"][0]["masked"], "••••4321")
        self.assertIn("OPENAI_API_KEY=synthetic-openai-4321", (self.root / ".env").read_text())
        elsewhere = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Origin": "http://evil.test", "Host": "127.0.0.1"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(elsewhere)
        self.assertEqual(refused.exception.code, 403)
