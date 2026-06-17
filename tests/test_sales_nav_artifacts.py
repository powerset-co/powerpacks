import csv
import contextlib
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Optional
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py"

SPEC = importlib.util.spec_from_file_location("sales_nav_artifacts_primitive", PRIMITIVE)
assert SPEC and SPEC.loader
ARTIFACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARTIFACTS)


def artifact_fixture() -> dict:
    return {
        "id": "artifact-1",
        "conversation_id": "conv-123",
        "content": {
            "extended_results": {
                "leads": [
                    {
                        "member_id": 101,
                        "name": "Ada Lovelace",
                        "title": "VP Engineering",
                        "company": "Stripe",
                        "location": "San Francisco",
                        "source_account_ids": ["acct-a"],
                        "mutual_member_ids": [201],
                        "mutuals": [{"member_id": 201, "name": "Mallory Mutual"}],
                    },
                    {"member_id": 102, "name": "Grace Hopper", "title": "Head of Engineering", "company": "Stripe"},
                ]
            }
        },
    }


class ArtifactHandler(http.server.BaseHTTPRequestHandler):
    payload: bytes = b"{}"
    status: int = 200
    expected_auth: str | None = "Bearer secret-token"
    seen_auth: str | None = None
    seen_api_key: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).seen_auth = self.headers.get("Authorization")
        type(self).seen_api_key = self.headers.get("X-API-Key")
        if self.path != "/v2/artifacts/artifact-1/download?include_content=true&download=true":
            self.send_response(404)
            self.end_headers()
            return
        if self.expected_auth and self.headers.get("Authorization") != self.expected_auth:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def artifact_server(payload: object = None, *, status: int = 200, expected_auth: str | None = "Bearer secret-token"):
    ArtifactHandler.payload = payload if isinstance(payload, bytes) else json.dumps(payload if payload is not None else artifact_fixture()).encode()
    ArtifactHandler.status = status
    ArtifactHandler.expected_auth = expected_auth
    ArtifactHandler.seen_auth = None
    ArtifactHandler.seen_api_key = None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ArtifactHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SalesNavArtifactsTests(unittest.TestCase):
    def run_json(self, args: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PRIMITIVE), *args],
            cwd=str(cwd or ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return json.loads(proc.stdout)

    def run_fail(self, args: list[str], *, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(PRIMITIVE), *args],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc

    def clean_env(self, td: Path) -> dict[str, str]:
        env = os.environ.copy()
        for key in ["NETWORK_SEARCH_API_TOKEN", "POWERPACKS_CREDENTIALS_PATH", "TEST_API_KEY", "NETWORK_SEARCH_API_BASE_URL", "POWERPACKS_API_BASE_URL"]:
            env.pop(key, None)
        env["HOME"] = str(td / "home")
        return env

    def test_ingest_pagination_urls_export_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            out_dir = td_path / "sales-nav-run"
            init = self.run_json([
                "init",
                "--query", "VP engineering at Stripe",
                "--set-id", "set-123",
                "--conversation-id", "conv-123",
                "--out-dir", str(out_dir),
                "--run-id", "run-123",
            ])
            state_path = Path(init["state"])

            page1 = {
                "artifact_id": "artifact-1",
                "total_count": 100,
                "results_returned": 2,
                "has_more": True,
                "next_start_offset": 25,
                "filters_used": {"title": "VP Engineering"},
                "leads": [
                    {
                        "member_id": 101,
                        "profile_id": "profile-101",
                        "name": "Ada Lovelace",
                        "title": "VP Engineering",
                        "headline": "VP Engineering at Stripe",
                        "summary": "Builds financial infrastructure teams.",
                        "company": "Stripe",
                        "location": "San Francisco",
                        "linkedin_url": "https://www.linkedin.com/in/ada",
                        "source_account_ids": ["acct-a"],
                        "mutual_count": 2,
                        "total_mutual_count": 2,
                        "total_interactions": 4,
                        "mutual_member_ids": [201, 202],
                        "mutuals": [
                            {
                                "member_id": 201,
                                "name": "Mallory Mutual",
                                "person_id": "person-201",
                                "total_interactions": 12,
                                "operators": [
                                    {
                                        "operator_id": "op-1",
                                        "operator_name": "Op One",
                                        "source_channels": ["sales_nav"],
                                        "total_interactions": 12,
                                        "gmail_accounts": ["op-one@example.com"],
                                    }
                                ],
                            },
                            {"member_id": 202, "first_name": "Pat"},
                        ],
                    },
                    {
                        "member_id": 102,
                        "name": "Grace Hopper",
                        "title": "Head of Engineering",
                        "company": "Stripe",
                        "location": "New York",
                        "source_account_ids": ["acct-b"],
                        "mutual_member_ids": [],
                        "mutuals": [],
                    },
                ],
            }
            page1_path = td_path / "page1.json"
            page1_path.write_text(json.dumps(page1))
            ingest1 = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(page1_path)])
            self.assertEqual(ingest1["lead_count"], 2)
            self.assertEqual(ingest1["mutual_edge_count"], 2)

            # get_artifact compact page shape for pagination. Lead 101 appears
            # again with an added source account/mutual; lead 103 is new.
            page2 = {
                "id": "artifact-1",
                "conversation_id": "conv-123",
                "page": {
                    "kind": "sales_nav_leads",
                    "offset": 25,
                    "limit": 25,
                    "returned": 2,
                    "total": 100,
                    "has_more": True,
                    "next_offset": 50,
                    "results": [
                        {
                            "member_id": 101,
                            "name": "Ada Lovelace",
                            "title": "VP Engineering",
                            "company": "Stripe",
                            "location": "San Francisco",
                            "total_interactions": 9,
                            "operators": [
                                {"operator_id": "op-2", "operator_name": "Op Two", "source_channels": ["sales_nav"]}
                            ],
                            "mutuals": [
                                {
                                    "member_id": 203,
                                    "name": "Quinn Mutual",
                                    "linkedin_url": "https://www.linkedin.com/in/quinn",
                                    "total_interactions": 3,
                                }
                            ],
                        },
                        {
                            "member_id": 103,
                            "name": "Barbara Liskov",
                            "title": "Engineering Leader",
                            "company": "Stripe",
                            "location": "Boston",
                            "mutuals": [{"member_id": 204, "name": "Riley Mutual"}],
                        },
                    ],
                },
            }
            page2_path = td_path / "page2.json"
            page2_path.write_text(json.dumps(page2))
            ingest2 = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(page2_path)])
            self.assertEqual(ingest2["lead_count"], 3)
            self.assertEqual(ingest2["mutual_edge_count"], 4)

            pending = self.run_json(["pending-mutual-ids", "--state", str(state_path)])
            self.assertEqual(pending["member_ids"], [201, 202, 204])

            urls = {"resolved": {"201": "https://www.linkedin.com/in/mallory", "202": "https://www.linkedin.com/in/pat"}, "unresolved": [204], "cache_only": True}
            urls_path = td_path / "urls.json"
            urls_path.write_text(json.dumps(urls))
            url_out = self.run_json(["ingest-member-urls", "--state", str(state_path), "--response", str(urls_path)])
            self.assertEqual(url_out["resolved_count"], 2)
            pending_after_cache_only = self.run_json(["pending-mutual-ids", "--state", str(state_path)])
            self.assertEqual(pending_after_cache_only["member_ids"], [])
            pending_retry = self.run_json(["pending-mutual-ids", "--state", str(state_path), "--include-unresolved"])
            self.assertEqual(pending_retry["member_ids"], [204])

            enriched_artifact = {
                "id": "artifact-1",
                "conversation_id": "conv-123",
                "content": {
                    "extended_results": {
                        "leads": [
                            {
                                "member_id": 101,
                                "name": "Ada Lovelace",
                                "title": "VP Engineering",
                                "company": "Stripe",
                                "headline": "VP Engineering, Payments Platform",
                                "summary": "Led real estate finance and payments platform engineering.",
                                "enriched": True,
                                "experiences": [
                                    {"company_name": "Stripe", "title": "VP Engineering", "start_year": 2021, "end_year": None, "is_current": True},
                                    {"company_name": "Brookfield", "title": "Engineering Advisor", "start_year": 2018, "end_year": 2020, "is_current": False},
                                ],
                                "education": [
                                    {"school": "MIT", "degree": "BS", "field_of_study": "Computer Science", "end_year": 2010}
                                ],
                            }
                        ]
                    }
                },
            }
            enriched_path = td_path / "enriched.json"
            enriched_path.write_text(json.dumps(enriched_artifact))
            ingest_enriched = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(enriched_path), "--prefer-content"])
            self.assertEqual(ingest_enriched["lead_count"], 3)

            exported = self.run_json(["export", "--state", str(state_path)])
            leads_csv = Path(exported["leads_csv"])
            mutuals_csv = Path(exported["mutuals_csv"])
            self.assertEqual(leads_csv.parent.name, "exports")
            self.assertEqual(mutuals_csv.parent.name, "exports")
            self.assertTrue(leads_csv.exists())
            self.assertTrue(mutuals_csv.exists())
            with leads_csv.open(newline="") as handle:
                lead_rows = list(csv.DictReader(handle))
            with mutuals_csv.open(newline="") as handle:
                mutual_rows = list(csv.DictReader(handle))
            self.assertEqual(len(lead_rows), 3)
            self.assertEqual(len(mutual_rows), 4)
            ada = next(row for row in lead_rows if row["member_id"] == "101")
            self.assertNotIn("source_account_id", ada)
            self.assertEqual(json.loads(ada["source_account_ids"]), ["acct-a"])
            self.assertEqual(ada["total_interactions"], "9")
            self.assertEqual(ada["enriched"], "True")
            self.assertIn("real estate finance", ada["summary"])
            self.assertIn("Brookfield", ada["experiences"])
            self.assertIn("MIT", ada["education"])
            self.assertIn("203", ada["mutual_member_ids"])
            mallory = next(row for row in mutual_rows if row["mutual_member_id"] == "201")
            self.assertEqual(mallory["mutual_linkedin_url"], "https://www.linkedin.com/in/mallory")
            self.assertEqual(mallory["total_interactions"], "12")
            self.assertEqual(json.loads(mallory["operators"])[0]["gmail_accounts"], ["op-one@example.com"])

            lookup = self.run_json(["lookup", "--state", str(state_path), "--query", "ada"])
            self.assertEqual(lookup["count"], 1)
            self.assertEqual(lookup["results"][0]["member_id"], "101")
            self.assertEqual(lookup["results"][0]["total_interactions"], 9)
            self.assertEqual(len(lookup["results"][0]["mutuals"]), 3)
            self.assertEqual(lookup["results"][0]["mutuals"][0]["total_interactions"], 12)
            profile_lookup = self.run_json(["lookup", "--state", str(state_path), "--query", "real estate"])
            self.assertEqual(profile_lookup["count"], 1)
            self.assertEqual(profile_lookup["results"][0]["member_id"], "101")

            state = json.loads(state_path.read_text())
            self.assertEqual(state["counts"], {"leads": 3, "member_urls": 2, "mutual_edges": 4})
            self.assertEqual(state["artifact_ids"], ["artifact-1"])

    def test_download_artifact_local_http_and_cli_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td, artifact_server() as api_base:
            output = Path(td) / "artifact.json"
            result = self.run_json([
                "download-artifact", "--artifact-id", "artifact-1", "--out", str(output), "--api-base", api_base, "--token", "secret-token"
            ], env=self.clean_env(Path(td)))
            self.assertEqual(result["response"], "downloaded")
            self.assertEqual(result["artifact_id"], "artifact-1")
            self.assertEqual(result["output"], str(output))
            self.assertGreater(result["byte_size"], 0)
            self.assertEqual(result["lead_count"], 2)
            self.assertEqual(json.loads(output.read_text()), artifact_fixture())
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer secret-token")

    def test_downloader_ingest_export_matches_old_saved_fixture_path(self) -> None:
        old_now = ARTIFACTS.now_iso
        ARTIFACTS.now_iso = lambda: "2025-01-01T00:00:00Z"
        try:
            with tempfile.TemporaryDirectory() as td, artifact_server() as api_base:
                td_path = Path(td)
                downloaded = td_path / "downloaded.json"
                ARTIFACTS.download_artifact_to_file(artifact_id="artifact-1", output=downloaded, api_base=api_base, token="secret-token")
                fixture_path = td_path / "fixture.json"
                fixture_path.write_text(json.dumps(artifact_fixture()))

                def run_path(name: str, response: Path) -> Path:
                    out_dir = td_path / name
                    state = ARTIFACTS.init_state(Namespace(query="q", set_id="set", conversation_id="conv", out_dir=str(out_dir), run_id="run", state=None))
                    state_path = Path(state["state"])
                    with open(os.devnull, "w") as devnull:
                        with contextlib.redirect_stdout(devnull):
                            ARTIFACTS.cmd_ingest_page(Namespace(state=str(state_path), response=str(response), artifact_id=None, offset=None, prefer_content=True))
                            ARTIFACTS.cmd_export(Namespace(state=str(state_path)))
                    return out_dir

                old_dir = run_path("old", fixture_path)
                new_dir = run_path("new", downloaded)
                for rel in ["leads.jsonl", "mutuals.jsonl", "exports/leads.csv", "exports/mutuals.csv"]:
                    self.assertEqual((old_dir / rel).read_bytes(), (new_dir / rel).read_bytes(), rel)
        finally:
            ARTIFACTS.now_iso = old_now

    def test_token_loading_from_args_env_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth="Bearer arg-token") as api_base:
            out = Path(td) / "arg.json"
            self.run_json(["download-artifact", "--artifact-id", "artifact-1", "--out", str(out), "--api-base", api_base, "--token", "arg-token"], env=self.clean_env(Path(td)))
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer arg-token")

        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth="Bearer env-token") as api_base:
            env = self.clean_env(Path(td))
            env["NETWORK_SEARCH_API_TOKEN"] = "env-token"
            env["NETWORK_SEARCH_API_BASE_URL"] = api_base
            self.run_json(["download-artifact", "--artifact-id", "artifact-1", "--out", str(Path(td) / "env.json")], env=env)
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer env-token")

        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth="Bearer cred-token") as api_base:
            td_path = Path(td)
            creds = td_path / "credentials.json"
            creds.write_text(json.dumps({"access_token": "cred-token", "expires_at": time.time() + 3600}))
            env = self.clean_env(td_path)
            env["POWERPACKS_CREDENTIALS_PATH"] = str(creds)
            env["POWERPACKS_API_BASE_URL"] = api_base
            self.run_json(["download-artifact", "--artifact-id", "artifact-1", "--out", str(td_path / "cred.json")], env=env)
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer cred-token")

    def test_direct_helper_loads_bearer_before_test_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth="Bearer env-token") as api_base:
            env = self.clean_env(Path(td))
            env["NETWORK_SEARCH_API_TOKEN"] = "env-token"
            env["TEST_API_KEY"] = "dev-key"
            with mock.patch.dict(os.environ, env, clear=True):
                ARTIFACTS.download_artifact_to_file(
                    artifact_id="artifact-1",
                    output=Path(td) / "direct-env.json",
                    api_base=api_base,
                    token=None,
                )
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer env-token")
            self.assertIsNone(ArtifactHandler.seen_api_key)

        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth="Bearer cred-token") as api_base:
            td_path = Path(td)
            creds = td_path / "credentials.json"
            creds.write_text(json.dumps({"access_token": "cred-token", "expires_at": time.time() + 3600}))
            env = self.clean_env(td_path)
            env["POWERPACKS_CREDENTIALS_PATH"] = str(creds)
            with mock.patch.dict(os.environ, env, clear=True):
                ARTIFACTS.download_artifact_to_file(
                    artifact_id="artifact-1",
                    output=td_path / "direct-cred.json",
                    api_base=api_base,
                    token=None,
                )
            self.assertEqual(ArtifactHandler.seen_auth, "Bearer cred-token")

        with tempfile.TemporaryDirectory() as td:
            env = self.clean_env(Path(td))
            env["TEST_API_KEY"] = "dev-key"
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "artifact download auth token is required"):
                    ARTIFACTS.download_artifact_to_file(
                        artifact_id="artifact-1",
                        output=Path(td) / "nonlocal.json",
                        api_base="https://api.example.com",
                        token=None,
                    )

        with tempfile.TemporaryDirectory() as td, artifact_server(expected_auth=None) as api_base:
            env = self.clean_env(Path(td))
            env["TEST_API_KEY"] = "dev-key"
            with mock.patch.dict(os.environ, env, clear=True):
                ARTIFACTS.download_artifact_to_file(
                    artifact_id="artifact-1",
                    output=Path(td) / "local-test-key.json",
                    api_base=api_base,
                    token=None,
                )
            self.assertEqual(ArtifactHandler.seen_api_key, "dev-key")
            self.assertIsNone(ArtifactHandler.seen_auth)

    def test_expired_and_missing_credentials_guidance_no_token_leak(self) -> None:
        secret = "super-secret-token"
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            creds = td_path / "credentials.json"
            creds.write_text(json.dumps({"access_token": secret, "expires_at": time.time() - 1}))
            env = self.clean_env(td_path)
            env["POWERPACKS_CREDENTIALS_PATH"] = str(creds)
            env["NETWORK_SEARCH_API_BASE_URL"] = "http://127.0.0.1:9"
            proc = self.run_fail(["download-artifact", "--artifact-id", "artifact-1", "--out", str(td_path / "x.json")], env=env)
            self.assertIn("access token expired; run powerset_auth token to refresh", proc.stderr)
            self.assertNotIn(secret, proc.stderr + proc.stdout)

        with tempfile.TemporaryDirectory() as td:
            env = self.clean_env(Path(td))
            env["NETWORK_SEARCH_API_BASE_URL"] = "http://127.0.0.1:9"
            proc = self.run_fail(["download-artifact", "--artifact-id", "artifact-1", "--out", str(Path(td) / "x.json")], env=env)
            self.assertIn("artifact download auth token is required; run powerset_auth login", proc.stderr)

    def test_download_failures_do_not_write_output_or_leave_temps(self) -> None:
        cases = [
            (401, artifact_fixture(), "http 401"),
            (404, artifact_fixture(), "http 404"),
            (200, b"not-json", "malformed JSON response"),
            (200, {"content": {"extended_results": {}}}, "missing content.extended_results.leads"),
        ]
        for status, payload, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td, artifact_server(payload, status=status) as api_base:
                out = Path(td) / "artifact.json"
                proc = self.run_fail(["download-artifact", "--artifact-id", "artifact-1", "--out", str(out), "--api-base", api_base, "--token", "secret-token"], env=self.clean_env(Path(td)))
                self.assertIn("artifact download failed", proc.stderr)
                self.assertIn(reason, proc.stderr)
                self.assertFalse(out.exists())
                self.assertEqual(list(Path(td).glob(".artifact.json.*.tmp")), [])
                self.assertNotIn("secret-token", proc.stderr + proc.stdout)

    def test_ingest_delta_dedupes_new_incoming_member_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            init = self.run_json(["init", "--query", "q", "--set-id", "set", "--conversation-id", "conv", "--out-dir", str(td_path / "run"), "--run-id", "run"])
            state_path = Path(init["state"])
            first = td_path / "first.json"
            first.write_text(json.dumps({"leads": [{"member_id": 1, "name": "One"}]}))
            self.run_json(["ingest-page", "--state", str(state_path), "--response", str(first)])

            second = td_path / "second.json"
            second.write_text(json.dumps({"leads": [{"member_id": 1, "name": "One Again"}, {"member_id": 2, "name": "Two"}, {"member_id": 2, "name": "Two Duplicate"}, {"member_id": 3, "name": "Three"}]}))
            ingest = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(second)])
            self.assertEqual(ingest["lead_count_before"], 1)
            self.assertEqual(ingest["lead_count_after"], 3)
            self.assertEqual(ingest["new_leads_ingested"], 2)
            self.assertEqual(ingest["new_member_ids"], ["2", "3"])


if __name__ == "__main__":
    unittest.main()
