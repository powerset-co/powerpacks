import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs" / "apollo" / "primitives" / "apollo_mcp" / "apollo_mcp.py"


def load_module():
    spec = importlib.util.spec_from_file_location("apollo_mcp", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApolloMcpInstallTests(unittest.TestCase):
    def test_codex_install_writes_stdio_config_and_status_redacts_secret(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td)

            result = module.codex_install("apollo", "apollo-mcp@0.2.0", "apollo-secret-key")
            config = Path(td) / "config.toml"
            text = config.read_text()
            state = module.codex_status("apollo")

        self.assertTrue(result["ok"])
        self.assertEqual(result["api_key"], "apol…-key")
        self.assertNotIn("apollo-secret-key", json.dumps(result))
        self.assertIn("[mcp_servers.apollo]", text)
        self.assertIn('command = "npx"', text)
        self.assertIn('args = ["-y", "apollo-mcp@0.2.0"]', text)
        self.assertIn('env = { APOLLO_API_KEY = "apollo-secret-key" }', text)
        self.assertTrue(state["installed"])
        self.assertTrue(state["has_api_key_env"])
        self.assertNotIn("apollo-secret-key", json.dumps(state))

    def test_codex_status_reads_only_apollo_section(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td)
            config = Path(td) / "config.toml"
            config.write_text(
                "[mcp_servers.apollo]\n"
                'command = "npx"\n\n'
                "[mcp_servers.other]\n"
                'args = ["-y", "other"]\n'
                'env = { APOLLO_API_KEY = "other-secret" }\n'
            )

            state = module.codex_status("apollo")

        self.assertTrue(state["installed"])
        self.assertFalse(state["has_api_key_env"])
        self.assertIsNone(state["args"])

    def test_install_command_reads_env_file_without_printing_key(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.DEFAULT_CODEX_HOME = Path(td) / ".codex"
            env_file = Path(td) / ".env"
            env_file.write_text("APOLLO_API_KEY=apollo-secret-key\n")
            stdout = io.StringIO()
            with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(stdout):
                module.main(["install", "--host", "codex", "--env-file", str(env_file)])
            output = stdout.getvalue()

        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertNotIn("apollo-secret-key", output)
        self.assertEqual(payload["hosts"][0]["api_key"], "apol…-key")

    def test_claude_install_error_redacts_env_secret(self):
        module = load_module()
        calls = []

        def fake_host_cli(host):
            return "/bin/claude" if host == "claude" else None

        def fake_run(cmd, *, timeout=30):
            calls.append(cmd)
            if cmd[:3] == ["claude", "mcp", "get"]:
                return 1, "", "not found"
            return 1, "", "failed APOLLO_API_KEY=apollo-secret-key"

        module.host_cli = fake_host_cli
        module.run = fake_run
        result = module.claude_install("apollo", "apollo-mcp@0.2.0", "user", "apollo-secret-key")

        self.assertFalse(result["ok"])
        self.assertNotIn("apollo-secret-key", json.dumps(result))
        self.assertIn("APOLLO_API_KEY=<REDACTED>", result["command_line"])
        self.assertIn("APOLLO_API_KEY=apollo-secret-key", calls[-1])

    def test_claude_install_requires_replace_for_existing_registration(self):
        module = load_module()

        def fake_host_cli(host):
            return "/bin/claude" if host == "claude" else None

        def fake_run(cmd, *, timeout=30):
            if cmd[:3] == ["claude", "mcp", "get"]:
                return 0, "apollo configured", ""
            raise AssertionError(f"unexpected command: {cmd}")

        module.host_cli = fake_host_cli
        module.run = fake_run
        result = module.claude_install("apollo", "apollo-mcp@0.2.0", "user", "apollo-secret-key")

        self.assertFalse(result["ok"])
        self.assertIn("--replace", result["error"])


class ApolloLeadPrepTests(unittest.TestCase):
    def test_prepare_leads_normalizes_dedupes_and_batches(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "leads.csv"
            input_path.write_text(
                "name,title,company,linkedin_url,email,location\n"
                "Ada Lovelace,CTO,Analytical Engines,linkedin.com/in/ada,,London\n"
                "Grace Hopper,VP Eng,Compilers Inc,https://linkedin.com/in/grace,grace@example.com,NYC\n"
                "Duplicate Hopper,VP Eng,Compilers Inc,https://linkedin.com/in/grace,grace@example.com,NYC\n"
                ",Founder,Stealth,https://linkedin.com/in/unknown,,SF\n"
                ",,,,,\n"
            )
            out_dir = Path(td) / "apollo-out"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main(["prepare-leads", "--input", str(input_path), "--out-dir", str(out_dir), "--batch-size", "1"])

            manifest = json.loads(stdout.getvalue())
            contacts = json.loads((out_dir / "contacts.json").read_text())
            create_ready = json.loads((out_dir / "create_ready_contacts.json").read_text())
            manual_review = json.loads((out_dir / "manual_review_contacts.json").read_text())
            enrich = json.loads((out_dir / "enrich_requests.json").read_text())
            batches = json.loads((out_dir / "contact_batches.json").read_text())

        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["contacts"], 3)
        self.assertEqual(manifest["create_ready_contacts"], 1)
        self.assertEqual(manifest["needs_enrichment_or_review"], 2)
        self.assertEqual(manifest["with_email"], 1)
        self.assertEqual(manifest["linkedin_only"], 2)
        self.assertEqual(manifest["skipped"], 2)
        self.assertEqual(contacts[0]["first_name"], "Ada")
        self.assertEqual(contacts[0]["last_name"], "Lovelace")
        self.assertEqual(contacts[0]["linkedin_url"], "https://linkedin.com/in/ada")
        self.assertTrue(all(contact.get("first_name") and contact.get("last_name") and contact.get("email") for contact in create_ready))
        self.assertEqual(manual_review[0]["linkedin_url"], "https://linkedin.com/in/ada")
        self.assertEqual(manual_review[1]["linkedin_url"], "https://linkedin.com/in/unknown")
        self.assertEqual(enrich[0]["linkedin_url"], "https://linkedin.com/in/ada")
        self.assertEqual(len(batches), 1)
        self.assertTrue(all(contact.get("first_name") and contact.get("last_name") and contact.get("email") for batch in batches for contact in batch))

    def test_prepare_leads_rejects_non_linkedin_profile_urls(self):
        module = load_module()
        self.assertEqual(module.normalize_linkedin_url("https://example.com/in/not-linkedin"), "")
        self.assertEqual(module.normalize_linkedin_url("linkedin.com/in/ada"), "https://linkedin.com/in/ada")


if __name__ == "__main__":
    unittest.main()
