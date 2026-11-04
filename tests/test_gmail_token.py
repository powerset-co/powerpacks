"""gmail_token: a mailbox's sign-in from its token file and Google, never from msgvault."""

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.setup.automations import accounts, gmail_token

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CLIENT = {"installed": {"client_id": "app-1.apps.googleusercontent.com", "client_secret": "synthetic-secret"}}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class GmailTokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "tokens").mkdir()
        secret = self.home / "client_secret.json"
        secret.write_text(json.dumps(CLIENT))
        (self.home / "config.toml").write_text(f'[oauth]\nclient_secrets = "{secret}"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, **fields):
        body = {"access_token": "at", "token_type": "Bearer", "refresh_token": "rt",
                "expiry": "2026-11-04T11:00:00.123456789-08:00", "client_id": CLIENT["installed"]["client_id"]}
        (self.home / "tokens" / "casey@example.com.json").write_text(json.dumps({**body, **fields}))

    def check(self):
        return gmail_token.check_token(self.home, "casey@example.com", now=NOW)

    def test_a_live_access_token_is_healthy_without_the_network(self):
        self.token(expiry="2026-11-04T13:00:00.5Z")
        with patch("urllib.request.urlopen") as post:
            self.assertEqual(self.check(), gmail_token.TokenCheck("healthy"))
        post.assert_not_called()

    def test_an_expired_access_token_is_refreshed_once_and_the_result_thrown_away(self):
        self.token()
        before = (self.home / "tokens" / "casey@example.com.json").read_text()
        with patch("urllib.request.urlopen", return_value=_Response(b'{"access_token": "new"}')) as post:
            self.assertEqual(self.check().status, "healthy")
        sent = post.call_args.args[0]
        self.assertEqual(sent.full_url, gmail_token.GOOGLE_TOKEN_URL)
        self.assertIn(b"grant_type=refresh_token", sent.data)
        self.assertIn(b"client_id=app-1.apps.googleusercontent.com", sent.data)
        self.assertEqual((self.home / "tokens" / "casey@example.com.json").read_text(), before)

    def test_invalid_grant_needs_the_user_and_anything_else_is_transient(self):
        self.token()

        def refused(code, body):
            return urllib.error.HTTPError(gmail_token.GOOGLE_TOKEN_URL, code, "", {}, io.BytesIO(body))

        with patch("urllib.request.urlopen", side_effect=refused(400, b'{"error": "invalid_grant"}')):
            self.assertEqual(self.check().status, "reauthorization_required")
        with patch("urllib.request.urlopen", side_effect=refused(500, b"{}")):
            self.assertEqual(self.check().status, "transient_error")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(self.check().status, "transient_error")

    def test_msgvault_answers_when_the_token_file_cannot(self):
        self.assertIsNone(self.check())  # no token file
        self.token(client_id="someone-else.apps.googleusercontent.com")
        self.assertIsNone(self.check())  # issued by a client config.toml doesn't name

    def test_check_account_uses_the_token_and_never_runs_msgvault(self):
        self.token(expiry="2026-11-04T23:00:00Z")
        with patch.object(accounts, "run_msgvault") as msgvault:
            verdict = accounts.check_account(self.home, "casey@example.com", stored=True)
        self.assertEqual(verdict.status, "healthy")
        msgvault.assert_not_called()

    def test_stored_mailboxes_read_the_sources_table_without_msgvault(self):
        with closing(sqlite3.connect(self.home / "msgvault.db")) as conn:
            conn.executescript("""
                CREATE TABLE sources (id INTEGER, source_type TEXT, identifier TEXT);
                INSERT INTO sources VALUES (1, 'gmail', 'Casey@Example.com'), (2, 'imap', 'x@example.com'),
                                           (3, 'gmail', 'jordan@example.com');
            """)
            conn.commit()
        with patch.object(accounts, "run_msgvault") as msgvault:
            self.assertEqual(accounts.stored_mailboxes(self.home), ("casey@example.com", "jordan@example.com"))
        msgvault.assert_not_called()

    def test_a_database_without_accounts_holds_no_mailboxes(self):
        with closing(sqlite3.connect(self.home / "msgvault.db")) as conn:
            conn.execute("CREATE TABLE messages (id INTEGER)")
        self.assertEqual(accounts.stored_mailboxes(self.home), ())


if __name__ == "__main__":
    unittest.main()
