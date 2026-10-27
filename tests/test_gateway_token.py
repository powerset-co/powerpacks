"""What the vendor gateway takes from this machine: the provisioned key, else the login."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.auth import auth


class GatewayTokenTests(unittest.TestCase):
    def test_a_provisioned_key_wins(self):
        with patch.dict(os.environ, {"POWERSET_API_KEY": " ps_api_key "}, clear=True):
            self.assertEqual(auth.gateway_token(), "ps_api_key")

    def test_no_key_and_no_login_is_empty(self):
        with patch.dict(os.environ, {"POWERPACKS_CREDENTIALS_PATH": "/nonexistent/credentials.json"}, clear=True):
            self.assertEqual(auth.gateway_token(), "")

    def test_the_login_token_stands_in_for_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"access_token": "stale", "refresh_token": "r", "expires_at": 0}))
            env = {"POWERPACKS_CREDENTIALS_PATH": str(path), "POWERPACKS_AUTH0_DOMAIN": "tenant.auth0.com",
                   "POWERPACKS_AUTH0_CLIENT_ID": "client"}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(auth, "_credentials_with_fresh_token", return_value={"access_token": "fresh"}) as refresh:
                self.assertEqual(auth.gateway_token(), "fresh")
            self.assertEqual(refresh.call_args.args, (path, "tenant.auth0.com", "client"))


if __name__ == "__main__":
    unittest.main()
