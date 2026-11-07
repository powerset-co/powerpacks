import os
import unittest
from unittest import mock

from packs.shared import endpoints


class EndpointsTests(unittest.TestCase):
    def test_defaults_are_production(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(endpoints.gateway_url(), "https://proxy.powerset.dev")
            self.assertEqual(endpoints.openai_base_url(), "https://api.openai.com/v1")

    def test_gateway_override(self) -> None:
        with mock.patch.dict(os.environ, {"POWERPACKS_GATEWAY_URL": "http://127.0.0.1:9000/"}, clear=True):
            self.assertEqual(endpoints.gateway_url(), "http://127.0.0.1:9000")

    def test_openai_override_and_older_names(self) -> None:
        for env in ({"OPENAI_BASE_URL": "http://mock/v1"}, {"POWERPACKS_OPENAI_BASE": "http://mock/v1/"},
                    {"OPENAI_API_BASE": "http://mock"}, {"OPENAI_BASE_URL": "http://mock/v1", "OPENAI_API_BASE": "http://other"}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(endpoints.openai_base_url(), "http://mock/v1")


if __name__ == "__main__":
    unittest.main()
