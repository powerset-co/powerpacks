"""Unit tests for the shared OpenAI client factory (base-URL normalization)."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

SHARED = Path(__file__).resolve().parents[1] / "packs" / "search" / "primitives" / "shared"

_spec = importlib.util.spec_from_file_location("openai_client", SHARED / "openai_client.py")
openai_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(openai_client)


class TestOpenAIBaseURL(unittest.TestCase):
    def test_default_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(openai_client.openai_base_url(), "https://api.openai.com/v1")

    def test_env_without_v1_gets_suffix(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_BASE": "https://proxy.example.com"}, clear=True):
            self.assertEqual(openai_client.openai_base_url(), "https://proxy.example.com/v1")

    def test_env_with_v1_unchanged(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_BASE": "https://proxy.example.com/v1"}, clear=True):
            self.assertEqual(openai_client.openai_base_url(), "https://proxy.example.com/v1")

    def test_trailing_slash_stripped(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_BASE": "https://proxy.example.com/v1/"}, clear=True):
            self.assertEqual(openai_client.openai_base_url(), "https://proxy.example.com/v1")

    def test_explicit_arg_beats_env(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_BASE": "https://env.example.com"}, clear=True):
            self.assertEqual(openai_client.openai_base_url("https://arg.example.com"), "https://arg.example.com/v1")


class TestMakeOpenAIClient(unittest.TestCase):
    def test_client_gets_normalized_base_url(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_BASE": "https://proxy.example.com"}, clear=True):
            client = openai_client.make_openai_client("sk-test")
            self.assertEqual(str(client.base_url).rstrip("/"), "https://proxy.example.com/v1")


if __name__ == "__main__":
    unittest.main()
