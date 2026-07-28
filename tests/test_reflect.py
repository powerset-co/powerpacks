"""Focused contract tests for the reporting-only ``$reflect`` primitive."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from packs.observability.primitives.reflect import reflect
from packs.observability.primitives.reflect.manifests import stage_projection


class _Response:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ReflectTests(unittest.TestCase):
    maxDiff = None

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_stage(
        self,
        root: Path,
        workflow: str,
        payload: object,
        *,
        index: int = 0,
    ) -> tuple[str, Path]:
        stage, relative = reflect.WORKFLOW_MANIFESTS[workflow][index]
        path = root / relative
        self._write_json(path, payload)
        return stage, path

    @staticmethod
    def _read_export(root: Path) -> dict:
        return json.loads(
            (root / ".powerpacks/reflect/export.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _read_report(root: Path) -> dict:
        return json.loads(
            (root / ".powerpacks/reflect/report.json").read_text(encoding="utf-8")
        )

    def test_each_workflow_reads_only_its_fixed_manifest_allowlist(self) -> None:
        for workflow, specs in reflect.WORKFLOW_MANIFESTS.items():
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                first_stage, _ = self._write_stage(
                    root,
                    workflow,
                    {
                        "status": (
                            "selected_steps_completed"
                            if workflow == "import-messages"
                            else "completed"
                        ),
                        "elapsed_ms": 42_000,
                        "counts": {"contacts": 27},
                        "account_email": "casey@example.com",
                    },
                )
                unrelated = root / ".powerpacks/unrelated/manifest.json"
                self._write_json(
                    unrelated,
                    {
                        "status": "failed",
                        "error": "UNRELATED-PII-CANARY",
                        "phone": "+15550100",
                    },
                )

                with mock.patch.object(
                    reflect, "fresh_access_token", side_effect=AssertionError("auth called")
                ):
                    result = reflect.Reflect(
                        root=root, workflow=workflow, local_only=True
                    ).run()

                self.assertEqual(result["status"], "local")
                export = self._read_export(root)
                self.assertEqual(
                    [row["stage"] for row in export["stages"]],
                    [stage for stage, _path in specs],
                )
                first = export["stages"][0]
                self.assertEqual(first["stage"], first_stage)
                self.assertEqual(first["artifact_state"], "present")
                self.assertEqual(first["status"], "completed")
                self.assertEqual(first["duration_bucket"], "10-60s")
                self.assertEqual(
                    first["count_buckets"],
                    [{"metric": "contacts", "bucket": "11-100"}],
                )
                self.assertTrue(
                    all(row["artifact_state"] == "missing" for row in export["stages"][1:])
                )
                serialized = json.dumps(export, sort_keys=True)
                self.assertNotIn("UNRELATED-PII-CANARY", serialized)
                self.assertNotIn("casey@example.com", serialized)
                self.assertNotIn("+15550100", serialized)

    def test_export_is_a_closed_projection_without_pii_or_raw_text(self) -> None:
        pii = {
            "status": "failed",
            "duration_seconds": 66,
            "counts": {"contacts": 22, "failed": 1},
            "full_name": "Jordan Bravo",
            "account_email": "casey@example.com",
            "phone": "+15550100",
            "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
            "artifact_path": "/Users/casey/private/people.csv",
            "prompt": "Find my secret contact",
            "message_body": "PRIVATE MESSAGE BODY",
            "error": "Traceback: secret provider response",
            "run_id": "019fa5ca-ed78-7dc0-8d91-c06bd09de689",
            "artifact_hash": "abc123secret",
            "children": [
                {
                    "account_email": "nested@example.com",
                    "path": "/tmp/nested-private",
                    "subject": "Confidential thread",
                }
            ],
            # Invalid free-form model metadata must not escape via the runtime block.
            "model": "casey@example.com",
            "reasoning_effort": "private effort notes",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(root, "import-gmail", pii)

            export = reflect.Reflect(
                root=root, workflow="import-gmail", local_only=True
            )._export()

        self.assertEqual(
            set(export),
            {
                "schema_version",
                "report_kind",
                "workflow",
                "scope",
                "product",
                "runtime",
                "os",
                "stages",
                "observations",
                "privacy",
            },
        )
        self.assertEqual(
            set(export["runtime"]),
            {
                "harness",
                "provider",
                "model",
                "effort",
                "role",
                "fallback_or_reroute",
                "token_bucket",
                "cost_bucket",
                "call_count_bucket",
                "latency_buckets",
            },
        )
        self.assertEqual(
            set(export["stages"][0]),
            {
                "stage",
                "artifact_state",
                "status",
                "model",
                "effort",
                "duration_bucket",
                "count_buckets",
                "cache_outcome",
                "approval_category",
                "error_code",
            },
        )
        self.assertEqual(
            export["privacy"],
            {
                "projection": "closed_allowlist",
                "raw_manifests_included": False,
                "session_transcript_included": False,
                "free_text_included": False,
                "sanitizer_version": 1,
                "dropped_raw_field_count_bucket": "11-100",
            },
        )
        self.assertEqual(export["runtime"]["model"], "unknown")
        self.assertEqual(export["runtime"]["effort"], "unknown")
        self.assertEqual(export["stages"][0]["model"], "unknown")
        self.assertEqual(export["stages"][0]["effort"], "unknown")
        self.assertEqual(
            export["observations"],
            [{"code": "stage_failed", "stage": "gmail_discovery"}],
        )
        serialized = json.dumps(export, sort_keys=True)
        for canary in (
            "Jordan Bravo",
            "casey@example.com",
            "nested@example.com",
            "+15550100",
            "linkedin.com",
            "/Users/casey",
            "Find my secret contact",
            "PRIVATE MESSAGE BODY",
            "Traceback",
            "019fa5ca",
            "abc123secret",
            "Confidential thread",
        ):
            with self.subTest(canary=canary):
                self.assertNotIn(canary, serialized)

    def test_model_effort_and_usage_are_normalized_and_bucketed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(
                root,
                "deep-context",
                {
                    "status": "completed",
                    "duration_seconds": 12,
                    "usage": {
                        "tokens": {
                            "input_tokens": 900,
                            "output_tokens": 300,
                            "reasoning_tokens": 34,
                        },
                        "estimated_cost_usd": 0.25,
                        "llm_calls": 3,
                    },
                },
            )

            export = reflect.Reflect(
                root=root,
                workflow="deep-context",
                local_only=True,
                harness="claude",
                model="gpt-5.6-sol",
                provider="unknown",
                effort="extra-high",
                role="reviewer",
                fallback=True,
            )._export()

        self.assertEqual(
            export["runtime"],
            {
                "harness": "claude-code",
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "role": "reviewer",
                "fallback_or_reroute": True,
                "token_bucket": "1k-10k",
                "cost_bucket": "$0.10-1",
                "call_count_bucket": "1-10",
                "latency_buckets": [
                    {"stage": "context_collection", "bucket": "10-60s"}
                ],
            },
        )

    def test_duration_projection_uses_only_exact_stage_timing_paths(self) -> None:
        cases = (
            (
                {
                    "timing": {"duration_seconds": 5},
                    "duration_seconds": 90,
                    "elapsed_ms": 900_000,
                },
                "1-10s",
            ),
            (
                {
                    "elapsed_seconds": 90,
                    "duration_ms": 5_000,
                },
                "1-5m",
            ),
            ({"total_ms": 5_000}, "1-10s"),
            (
                {
                    "assembly": {"elapsed_ms": 90_000},
                    "steps": [{"duration_seconds": 45}],
                },
                "unknown",
            ),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                stage, path = self._write_stage(
                    root,
                    "deep-context",
                    {"status": "completed", **payload},
                    index=8,
                )

                projected, _observation = stage_projection(stage, path)

                self.assertEqual(projected["duration_bucket"], expected)

    def test_profile_prefetch_timing_is_reported_separately_from_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifests = dict(reflect.WORKFLOW_MANIFESTS["deep-context"])
            self._write_json(
                root / manifests["deep_research"],
                {
                    "status": "research_complete",
                    "assembly": {"elapsed_ms": 90_000},
                },
            )
            self._write_json(
                root / manifests["profile_prefetch"],
                {
                    "status": "completed",
                    "duration_seconds": 22,
                    "model": "gpt-5-mini",
                    "reasoning_effort": "low",
                },
            )

            export = reflect.Reflect(
                root=root, workflow="deep-context", local_only=True
            )._export()

        stages = {stage["stage"]: stage for stage in export["stages"]}
        self.assertEqual(stages["deep_research"]["duration_bucket"], "unknown")
        self.assertEqual(stages["profile_prefetch"]["duration_bucket"], "10-60s")
        self.assertEqual(stages["profile_prefetch"]["model"], "gpt-5-mini")
        self.assertEqual(stages["profile_prefetch"]["effort"], "low")
        self.assertEqual(
            export["runtime"]["latency_buckets"],
            [{"stage": "profile_prefetch", "bucket": "10-60s"}],
        )

    def test_messages_nested_user_action_is_an_expected_permission_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(
                root,
                "import-messages",
                {
                    "status": "failed",
                    "child": {
                        "status": "blocked_user_action",
                        "continue_command": "discover --include-imessage",
                        "message": "PRIVATE FULL DISK ACCESS TEXT",
                    },
                },
            )

            export = reflect.Reflect(
                root=root, workflow="import-messages", local_only=True
            )._export()

        first = export["stages"][0]
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["approval_category"], "os_permission")
        self.assertEqual(
            export["observations"],
            [{"code": "expected_gate", "stage": "messages_discovery"}],
        )
        self.assertNotIn("PRIVATE FULL DISK ACCESS TEXT", json.dumps(export))

    def test_signed_in_run_posts_safe_export_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(
                root,
                "setup",
                {
                    "status": "completed",
                    "contacts": 12,
                    "account_email": "casey@example.com",
                    "path": "/Users/casey/Connections.csv",
                },
            )
            opened: list[tuple[urllib.request.Request, int]] = []

            def fake_urlopen(
                request: urllib.request.Request, timeout: int
            ) -> _Response:
                opened.append((request, timeout))
                return _Response({"receipt": "receipt_123"})

            with mock.patch.object(
                reflect, "fresh_access_token", return_value="test-token"
            ) as auth, mock.patch.object(
                reflect.urllib.request, "urlopen", side_effect=fake_urlopen
            ):
                result = reflect.Reflect(
                    root=root,
                    workflow="setup",
                    upload_url="https://telemetry.example.test/v1/reflections",
                ).run()

            auth.assert_called_once_with()
            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["delivery"]["mode"], "powerset")
            self.assertEqual(result["delivery"]["receipt"], "receipt_123")
            self.assertEqual(len(opened), 1)
            request, timeout = opened[0]
            self.assertEqual(
                request.full_url, "https://telemetry.example.test/v1/reflections"
            )
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(request.get_header("Content-type"), "application/json")
            self.assertEqual(timeout, 20)
            posted = json.loads((request.data or b"{}").decode("utf-8"))
            self.assertEqual(posted, self._read_export(root))
            serialized = json.dumps(posted, sort_keys=True)
            self.assertNotIn("casey@example.com", serialized)
            self.assertNotIn("/Users/casey", serialized)
            self.assertEqual(self._read_report(root)["delivery"]["status"], "sent")

    def test_signed_out_run_offers_public_issue_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(
                root,
                "import-gmail",
                {"status": "completed", "account_email": "casey@example.com"},
            )

            with mock.patch.object(
                reflect,
                "fresh_access_token",
                side_effect=reflect.PowersetNotLoggedIn("signed out"),
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ) as urlopen:
                result = reflect.Reflect(root=root, workflow="import-gmail").run()

            urlopen.assert_not_called()
            self.assertEqual(result["status"], "github_issue_offer")
            self.assertEqual(result["delivery"]["mode"], "github_issue")
            self.assertEqual(
                result["delivery"]["status"], "confirmation_required"
            )
            preview = result["delivery"]["preview"]
            self.assertEqual(preview["url"], reflect.GITHUB_NEW_ISSUE_URL)
            self.assertIn("An anonymized Powerpacks reflection report", preview["body"])
            self.assertNotIn("casey@example.com", preview["body"])

    def test_auth_refresh_failure_stays_private_without_github_offer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(root, "setup", {"status": "completed"})
            with mock.patch.object(
                reflect,
                "fresh_access_token",
                side_effect=reflect.PowersetAuthUnavailable("refresh failed"),
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ):
                result = reflect.Reflect(root=root, workflow="setup").run()

            self.assertEqual(result["status"], "upload_failed")
            self.assertEqual(result["delivery"]["error_code"], "auth_unavailable")
            self.assertNotIn("preview", result["delivery"])
            self.assertNotIn("github", json.dumps(result["delivery"]).lower())

    def test_upload_failure_stays_private_and_never_falls_back_to_github(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(root, "import-messages", {"status": "completed"})

            with mock.patch.object(
                reflect, "fresh_access_token", return_value="test-token"
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                result = reflect.Reflect(
                    root=root,
                    workflow="import-messages",
                    upload_url="https://telemetry.example.test/v1/reflections",
                ).run()

            self.assertEqual(result["status"], "upload_failed")
            self.assertEqual(
                result["delivery"],
                {
                    "mode": "powerset",
                    "status": "not_sent",
                    "error_code": "network_error",
                },
            )
            report = self._read_report(root)
            self.assertNotIn("preview", report["delivery"])
            self.assertNotIn("github", json.dumps(report["delivery"]).lower())

    def test_local_cli_never_checks_auth_or_uses_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_stage(root, "setup", {"status": "completed"})
            stdout = io.StringIO()

            with mock.patch.object(
                reflect, "fresh_access_token", side_effect=AssertionError("auth called")
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ), contextlib.redirect_stdout(stdout):
                code = reflect.main(
                    ["--workflow", "setup", "--root", str(root), "--local"]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "local")
            self.assertEqual(
                payload["delivery"], {"mode": "local", "status": "not_sent"}
            )

    def test_all_missing_artifacts_are_not_sent_or_called_friction_free(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                reflect, "fresh_access_token", side_effect=AssertionError("auth called")
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ):
                result = reflect.Reflect(root=root, workflow="setup").run()

            self.assertEqual(result["status"], "no_artifacts")
            self.assertEqual(result["delivery"]["mode"], "none")
            export = self._read_export(root)
            self.assertEqual(
                export["observations"],
                [{"code": "artifact_state_unavailable", "stage": "workflow"}],
            )
            self.assertNotIn("no_friction_observed", json.dumps(export))

    def test_rerun_overwrites_only_three_fixed_outputs_without_run_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.py"
            agents = root / "AGENTS.md"
            source.write_text("DO NOT EDIT\n", encoding="utf-8")
            agents.write_text("DO NOT EDIT EITHER\n", encoding="utf-8")
            _stage, manifest_path = self._write_stage(
                root, "setup", {"status": "running", "contacts": 1}
            )
            runner = reflect.Reflect(root=root, workflow="setup", local_only=True)

            first = runner.run()
            first_report = self._read_report(root)
            self._write_json(
                manifest_path, {"status": "completed", "contacts": 101}
            )
            second = runner.run()
            second_report = self._read_report(root)

            self.assertEqual(
                (first["report"], first["export"], first["manifest"]),
                (second["report"], second["export"], second["manifest"]),
            )
            out_dir = root / ".powerpacks/reflect"
            self.assertEqual(
                sorted(path.name for path in out_dir.iterdir()),
                ["export.json", "manifest.json", "report.json"],
            )
            self.assertTrue(all(path.is_file() for path in out_dir.iterdir()))
            self.assertNotEqual(first_report["stages"], second_report["stages"])
            self.assertEqual(source.read_text(encoding="utf-8"), "DO NOT EDIT\n")
            self.assertEqual(
                agents.read_text(encoding="utf-8"), "DO NOT EDIT EITHER\n"
            )

    def test_malformed_manifest_is_reported_without_leaking_parser_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stage, relative = reflect.WORKFLOW_MANIFESTS["deep-context"][0]
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"status": "completed", "secret": "casey@example.com"', encoding="utf-8")

            result = reflect.Reflect(
                root=root, workflow="deep-context", local_only=True
            ).run()

            self.assertEqual(result["status"], "local")
            export = self._read_export(root)
            first = export["stages"][0]
            self.assertEqual(first["stage"], stage)
            self.assertEqual(first["artifact_state"], "unreadable")
            self.assertEqual(first["status"], "unknown")
            self.assertEqual(first["error_code"], "manifest_unreadable")
            self.assertEqual(
                export["observations"],
                [{"code": "manifest_unreadable", "stage": stage}],
            )
            serialized = json.dumps(export, sort_keys=True)
            self.assertNotIn("casey@example.com", serialized)
            self.assertNotIn("JSONDecodeError", serialized)
            self.assertNotIn("Expecting", serialized)

    def test_future_schema_expansion_fails_closed_before_auth_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = reflect.Reflect(root=root, workflow="setup")
            unsafe = runner._export()
            unsafe["runtime"]["unexpected_free_text"] = "casey@example.com"
            with mock.patch.object(
                reflect.Reflect, "_export", return_value=unsafe
            ), mock.patch.object(
                reflect, "fresh_access_token", side_effect=AssertionError("auth called")
            ), mock.patch.object(
                reflect.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ):
                result = runner.run()

            self.assertEqual(result["status"], "privacy_failed")
            self.assertEqual(
                self._read_export(root),
                {"schema_version": 1, "status": "privacy_failed"},
            )
            serialized = json.dumps(self._read_report(root), sort_keys=True)
            self.assertNotIn("casey@example.com", serialized)


if __name__ == "__main__":
    unittest.main()
