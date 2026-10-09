import io
import json
import unittest
from unittest import mock

from packs.ingestion.primitives.setup.automations import oauth_browser
from packs.ingestion.primitives.setup.automations.shell import CommandResult

URL = "https://accounts.google.com/o/oauth2/auth?client_id=32555940559.apps.googleusercontent.com&state=S"


class LoginGcloudTests(unittest.TestCase):
    def run_login(self, browser_payload: dict, returncode: int = 0):
        proc = mock.Mock(stdout=io.StringIO(f"Your browser has been opened to visit:\n\n    {URL}\n"))
        proc.wait.return_value = returncode
        proc.poll.return_value = returncode
        with mock.patch.object(oauth_browser, "ensure_playwright_core", return_value={"status": "ok", "node_path": "/n"}), \
                mock.patch.object(oauth_browser.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(oauth_browser, "run_streaming_command",
                                  return_value=CommandResult(ok=True, stdout=json.dumps(browser_payload))) as browser:
            result = oauth_browser.login_gcloud(["gcloud", "auth", "login", "me@example.com"], "me@example.com")
        return result, popen, browser

    def test_hands_the_printed_url_to_the_profile_and_waits_for_gcloud(self):
        result, popen, browser = self.run_login({"status": "ok"})
        self.assertTrue(result.ok)
        self.assertEqual(popen.call_args.kwargs["env"]["BROWSER"], "true")
        self.assertIn("gcloud-login", browser.call_args.args[0])
        request = json.loads(browser.call_args.kwargs["input_text"])
        self.assertEqual((request["url"], request["email"]), (URL, "me@example.com"))

    def test_a_sign_in_left_unfinished_is_reported(self):
        result, _, _ = self.run_login({"status": "needs_user_action", "message": "Google sign-in did not finish."})
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Google sign-in did not finish.")


if __name__ == "__main__":
    unittest.main()
