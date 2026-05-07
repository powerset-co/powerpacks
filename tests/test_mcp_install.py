import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs" / "powerset" / "primitives" / "mcp_install" / "mcp_install.py"


def load_module():
    spec = importlib.util.spec_from_file_location("mcp_install", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_jwt(exp: int) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": exp}

    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


class McpInstallCodexAuthTests(unittest.TestCase):
    def test_codex_bearer_token_state_reports_valid_token(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td)
            config = Path(td) / "config.toml"
            config.write_text(
                "[mcp_servers.powerset-search]\n"
                'url = "https://example.test/mcp/"\n\n'
                "[mcp_servers.powerset-search.http_headers]\n"
                f'Authorization = "Bearer {fake_jwt(4102444800)}"\n'
            )

            state = module.codex_bearer_token_state("powerset-search")

        self.assertEqual(state["auth_status"], "valid")
        self.assertFalse(state["token_expired"])
        self.assertGreater(state["token_seconds_remaining"], 0)

    def test_codex_bearer_token_state_reports_expired_token(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td)
            config = Path(td) / "config.toml"
            config.write_text(
                "[mcp_servers.powerset-search.http_headers]\n"
                f'Authorization = "Bearer {fake_jwt(1)}"\n'
            )

            state = module.codex_bearer_token_state("powerset-search")

        self.assertEqual(state["auth_status"], "expired")
        self.assertTrue(state["token_expired"])
        self.assertEqual(state["token_seconds_remaining"], 0)

    def test_codex_bearer_token_state_reports_missing_header(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td)
            (Path(td) / "config.toml").write_text("[mcp_servers.powerset-search]\n")

            state = module.codex_bearer_token_state("powerset-search")

        self.assertEqual(state["auth_status"], "missing_authorization_header")


if __name__ == "__main__":
    unittest.main()
