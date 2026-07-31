"""$send-feedback primitive: request validation, dry-run shape, and the
submit/needs-auth/failed branches. All offline — post_json and bearer_token are
patched where defined; fixtures are synthetic."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

from packs.powerset.primitives.send_feedback import send_feedback as sf

SET_ID = "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b"


class TestFeedbackRequest(unittest.TestCase):
    def test_requires_comment_and_known_type(self):
        with self.assertRaises(SystemExit):
            sf.FeedbackRequest(comment="   ")
        with self.assertRaises(SystemExit):
            sf.FeedbackRequest(comment="x", feedback_type="rant")

    def test_uuid_fields_are_validated_not_forwarded_raw(self):
        # A local slug in person_id would poison downstream ::uuid casts.
        with self.assertRaises(SystemExit):
            sf.FeedbackRequest(comment="x", person_id="jordan-bravo-a1b2")
        request = sf.FeedbackRequest(comment="x", set_id=SET_ID)
        self.assertEqual(request.set_id, SET_ID)

    def test_body_carries_only_populated_fields(self):
        request = sf.FeedbackRequest(
            comment=" wrong person ", category="linkedin",
            field_value="https://www.linkedin.com/in/jordan-namesake",
            metadata={"query": "series b lead", "local_slug": "jordan-bravo-p"},
            set_id=SET_ID)
        body = request.body()
        self.assertEqual(body["feedback_type"], "data_inconsistency")
        self.assertEqual(body["comment"], "wrong person")
        self.assertEqual(body["set_id"], SET_ID)
        self.assertEqual(body["metadata"]["local_slug"], "jordan-bravo-p")
        self.assertNotIn("person_id", body)
        self.assertNotIn("conversation_id", body)

    def test_oversized_body_is_refused_before_the_wire(self):
        request = sf.FeedbackRequest(comment="x" * (sf.MAX_BODY_BYTES + 10))
        with self.assertRaises(SystemExit):
            request.body()


class TestSendFeedback(unittest.TestCase):
    def _request(self):
        return sf.FeedbackRequest(comment="found the wrong Jordan Bravo",
                                  category="linkedin", set_id=SET_ID)

    def test_dry_run_never_touches_auth_or_network(self):
        with mock.patch.object(sf, "bearer_token") as token, \
             mock.patch.object(sf, "post_json") as post:
            payload = sf.SendFeedback(self._request(), dry_run=True).run()
        token.assert_not_called()
        post.assert_not_called()
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["path"], "/v2/feedback")
        self.assertEqual(payload["body"]["category"], "linkedin")

    def test_submitted_payload_carries_the_feedback_id(self):
        with mock.patch.object(sf, "api_base", return_value="https://api.example.com"), \
             mock.patch.object(sf, "bearer_token", return_value="tok"), \
             mock.patch.object(sf, "post_json",
                               return_value=(201, {"id": "fb-123"})) as post:
            payload = sf.SendFeedback(self._request()).run()
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(payload["feedback_id"], "fb-123")
        base, path, token, body = post.call_args[0]
        self.assertEqual((base, path, token), ("https://api.example.com", "/v2/feedback", "tok"))
        self.assertEqual(body["set_id"], SET_ID)

    def test_signed_out_maps_to_needs_auth(self):
        with mock.patch.object(sf, "api_base", return_value="https://api.example.com"), \
             mock.patch.object(sf, "bearer_token",
                               side_effect=SystemExit("not signed in; run `$powerset login`")):
            payload = sf.SendFeedback(self._request()).run()
        self.assertEqual(payload["status"], "needs_auth")
        self.assertIn("login", payload["error"])

    def test_http_403_maps_to_needs_auth(self):
        error = urllib.error.HTTPError("u", 403, "forbidden", None, io.BytesIO(b""))
        with mock.patch.object(sf, "api_base", return_value="https://api.example.com"), \
             mock.patch.object(sf, "bearer_token", return_value="tok"), \
             mock.patch.object(sf, "post_json", side_effect=error):
            payload = sf.SendFeedback(self._request()).run()
        self.assertEqual(payload["status"], "needs_auth")
        self.assertEqual(payload["http_status"], 403)

    def test_network_error_maps_to_failed(self):
        with mock.patch.object(sf, "api_base", return_value="https://api.example.com"), \
             mock.patch.object(sf, "bearer_token", return_value="tok"), \
             mock.patch.object(sf, "post_json",
                               side_effect=urllib.error.URLError("refused")):
            payload = sf.SendFeedback(self._request()).run()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("refused", payload["error"])


class TestCli(unittest.TestCase):
    def test_dry_run_exit_zero_and_emits_body(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sf.main(["--comment", "wrong person", "--category", "linkedin",
                            "--metadata", '{"query": "series b lead"}', "--dry-run"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["body"]["metadata"]["query"], "series b lead")

    def test_bad_metadata_and_bad_person_id_exit_2(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sf.main(["--comment", "x", "--metadata", "not-json"]), 2)
            self.assertEqual(sf.main(["--comment", "x", "--metadata", '["list"]']), 2)
            self.assertEqual(
                sf.main(["--comment", "x", "--person-id", "local-slug", "--dry-run"]), 2)

    def test_needs_auth_exit_3(self):
        with mock.patch.object(sf, "api_base", return_value="https://api.example.com"), \
             mock.patch.object(sf, "bearer_token", side_effect=SystemExit("not signed in")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(sf.main(["--comment", "x"]), 3)


if __name__ == "__main__":
    unittest.main()
