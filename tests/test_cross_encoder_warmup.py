"""Warm-up is approved, data-free, and never blocks search. No paid calls."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from packs.search.primitives.llm_rerank_candidates import cross_encoder as ce
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from packs.search.primitives.deep_search import search_harness
from tests.test_search_harness import _payload, _start


class WarmupClientTests(unittest.TestCase):
    def call(self, respond, **kwargs):
        requests, threads = [], []
        thread_class = threading.Thread

        def handle(request):
            requests.append(request)
            return respond(request)

        def thread(**kwargs):
            result = thread_class(**kwargs)
            threads.append(result)
            return result

        client = httpx.Client(transport=httpx.MockTransport(handle))
        output = io.StringIO()
        with mock.patch.object(httpx, "Client", return_value=client) as constructor, \
                mock.patch.object(threading, "Thread", side_effect=thread), \
                contextlib.redirect_stderr(output):
            ce.warm_workers(**kwargs)
            for worker in threads:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertTrue(worker.daemon)
        return requests, output.getvalue(), constructor

    def test_authenticated_empty_post_and_bounded_timeout(self):
        requests, output, constructor = self.call(
            lambda request: httpx.Response(200, json={"status": "ready"}), api_key="synthetic-key")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://proxy.powerset.dev/vendor/cross-encoder/warmup")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["x-powerset-key"], "synthetic-key")
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(request.content, b"")
        constructor.assert_called_once_with(timeout=240)
        self.assertIn("warmup: ready", output)

    def test_environment_key(self):
        with mock.patch.dict(os.environ, {"POWERSET_API_KEY": "synthetic-env-key"}):
            requests, _, _ = self.call(lambda request: httpx.Response(200))
        self.assertEqual(requests[0].headers["x-powerset-key"], "synthetic-env-key")

    def test_missing_key_starts_no_client(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            requests, output, constructor = self.call(lambda request: httpx.Response(200))
        constructor.assert_not_called()
        self.assertEqual(requests, [])
        self.assertIn("missing POWERSET_API_KEY", output)

    def test_http_error_does_not_leak_response_or_retry(self):
        requests, output, _ = self.call(
            lambda request: httpx.Response(503, text="private-company synthetic-key"), api_key="synthetic-key")
        self.assertEqual(len(requests), 1)
        self.assertIn("warmup: unavailable", output)
        self.assertNotIn("private-company", output)
        self.assertNotIn("synthetic-key", output)

    def test_timeout_does_not_leak_error_or_retry(self):
        def fail(request):
            raise httpx.ReadTimeout("private-company synthetic-key", request=request)

        requests, output, _ = self.call(fail, api_key="synthetic-key")
        self.assertEqual(len(requests), 1)
        self.assertIn("warmup: unavailable", output)
        self.assertNotIn("private-company", output)
        self.assertNotIn("synthetic-key", output)

    def test_pending_request_does_not_hold_up_process_exit(self):
        script = """
import threading
from unittest.mock import patch
from packs.search.primitives.llm_rerank_candidates import cross_encoder
started = threading.Event()
def post(*args, **kwargs):
    started.set()
    threading.Event().wait(30)
with patch.object(cross_encoder.httpx, "Client") as client:
    client.return_value.__enter__.return_value.post.side_effect = post
    cross_encoder.warm_workers(api_key="synthetic-key")
    assert started.wait(2)
    print("returned while warmup is pending")
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=pipeline.ROOT,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("returned while warmup is pending", result.stdout)


class WarmupPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.state = self.root / "state.json"
        self.ledger = self.root / "pipeline.json"
        self.db = self.root / "local.duckdb"
        self.db.touch()
        self.env = self.root / "custom.env"
        self.env.write_text('POWERSET_API_KEY="synthetic-file-key"\n')
        self.payload = {"normalized_query": "Backend engineers", "traits": [],
                        "role_search_filters": {"semantic_query": "Backend engineers building storage systems"}}
        self.state.write_text(json.dumps({"query": "Backend engineers", "steps": [
            {"id": "expand_search_request", "status": "completed", "output": self.payload}]}))
        self.events = []
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(pipeline, "ROOT", self.root))
        self.enterContext(mock.patch.object(pipeline, "apply_local_title_clustering", side_effect=lambda payload, db: payload))
        self.run = self.enterContext(mock.patch.object(pipeline, "run", side_effect=self.fake_run))
        self.real_warm = ce.warm_workers
        self.warm = self.enterContext(mock.patch.object(ce, "warm_workers", side_effect=lambda **kwargs: self.events.append("warmup")))

    def fake_run(self, command, **kwargs):
        step = Path(command[1]).stem
        self.events.append(step)
        return {"returncode": 0, "json": {
            **self.payload, "state": str(self.state), "hydrated": 1, "passed_count": 1, "ranked_count": 1,
        }}

    def args(self, *flags, backend="powerset"):
        return pipeline.build_parser().parse_args([
            "run", "--backend", backend, "--state", str(self.state), "--ledger", str(self.ledger),
            "--db", str(self.db), "--env-file", str(self.env), "--cross-encoder-beta", *flags])

    def execute(self, args):
        return (pipeline.run_pipeline_local if args.backend == "local" else pipeline.run_pipeline)(args)

    def test_both_backends_warm_once_before_retrieval_using_env_file(self):
        for backend in ("powerset", "local"):
            with self.subTest(backend=backend):
                self.ledger.write_text("{}")
                self.events.clear()
                self.warm.reset_mock()
                result = self.execute(self.args("--execute-approved", backend=backend))
                self.assertEqual(result["status"], "completed")
                self.assertLess(self.events.index("warmup"), self.events.index("execute_role_search"))
                self.warm.assert_called_once_with(api_key="synthetic-file-key")

    def test_process_environment_key_keeps_existing_precedence(self):
        with mock.patch.dict(os.environ, {"POWERSET_API_KEY": "synthetic-process-key"}):
            self.execute(self.args("--confirm-llm"))
        self.warm.assert_called_once_with(api_key="synthetic-process-key")

    def test_disabled_search_only_and_filter_only_never_warm(self):
        for backend in ("powerset", "local"):
            for mode in ("disabled", "--search-only", "--filter-only"):
                with self.subTest(backend=backend, mode=mode):
                    self.ledger.write_text("{}")
                    args = self.args("--execute-approved", *([] if mode == "disabled" else [mode]), backend=backend)
                    if mode == "disabled":
                        args.cross_encoder_beta = False
                    self.execute(args)
        self.warm.assert_not_called()

    def test_unapproved_search_never_warms(self):
        with self.assertRaises(pipeline.Blocked):
            self.execute(self.args())
        self.ledger.write_text("{}")
        self.execute(self.args(backend="local"))
        self.warm.assert_not_called()

    def test_saved_exact_approval_allows_interrupted_continuation(self):
        args = self.args()
        with self.assertRaises(pipeline.Blocked) as blocked:
            self.execute(args)
        self.warm.assert_not_called()
        saved = pipeline.read_json(self.ledger)
        saved["approvals"][blocked.exception.payload["approval_id"]] = {"confirmed": True}
        pipeline.write_json(self.ledger, saved)
        self.events.clear()
        self.execute(args)
        self.warm.assert_called_once_with(api_key="synthetic-file-key")
        self.assertLess(self.events.index("warmup"), self.events.index("llm_filter_candidates"))

    def test_unrelated_saved_approval_does_not_warm(self):
        self.ledger.write_text(json.dumps({"approvals": {"llm_other": {"confirmed": True}}}))
        with self.assertRaises(pipeline.Blocked):
            self.execute(self.args())
        self.warm.assert_not_called()

    def test_completed_rerank_never_warms_even_when_forced(self):
        for backend in ("powerset", "local"):
            for flags in ([], ["--force-llm"], ["--force"]):
                with self.subTest(backend=backend, flags=flags):
                    self.ledger.write_text(json.dumps({"steps": {"llm_rerank_candidates": {
                        "status": "completed", "summary": {"ranked_count": 1}}}}))
                    self.execute(self.args("--execute-approved", *flags, backend=backend))
        self.warm.assert_not_called()

    def test_prepare_never_warms(self):
        for backend in ("powerset", "local"):
            args = pipeline.build_parser().parse_args([
                "prepare", "--backend", backend, "--db", str(self.db), "--query", "Backend engineers",
                "--output-dir", str(self.root / backend), "--cross-encoder-beta"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline.cmd_prepare(args), 0)
        self.warm.assert_not_called()

    def test_reviewed_deep_pond_reaches_warmup_before_retrieval_only_when_enabled(self):
        for backend in ("powerset", "local"):
            for enabled in (False, True):
                with self.subTest(backend=backend, enabled=enabled), \
                        mock.patch.dict(os.environ, {"POWERPACKS_CROSS_ENCODER_BETA": "1" if enabled else "0"}):
                    run_dir = self.root / f"{backend}-{enabled}"
                    run_dir.mkdir()
                    results_path = _start(run_dir)
                    results = pipeline.read_json(results_path)
                    results["retrieval"], _, _ = search_harness.resolve_retrieval_identity(backend, "set-1", str(self.db))
                    pipeline.write_json(run_dir / "decision.json", {"surface": "people", "depth": "deep", "backend": backend})
                    payload = run_dir / "payload.json"
                    pipeline.write_json(payload, _payload())
                    results["status"] = "ready_to_run"
                    results["pending_payload"] = {"pond_n": 1, "query": "Backend engineers",
                                                  "payload_json": str(payload), "ledger": str(self.ledger), "limit": 1000}
                    pipeline.write_json(results_path, results)
                    self.ledger.write_text("{}")
                    self.events.clear()
                    self.warm.reset_mock()

                    def execute_command(command, **kwargs):
                        args = pipeline.build_parser().parse_args(command[2:])
                        self.assertTrue(args.execute_approved)
                        self.assertEqual(args.cross_encoder_beta, enabled)
                        if enabled:
                            self.assertEqual(args.cross_encoder_jd_file, str(run_dir / "jd.txt"))
                        self.assertEqual(self.execute(args)["status"], "completed")
                        raise RuntimeError("offline pipeline completed")

                    with mock.patch.object(search_harness, "_jd_traits", return_value=[]), \
                            mock.patch.object(search_harness, "_run_command", side_effect=execute_command), \
                            self.assertRaisesRegex(RuntimeError, "offline pipeline completed"):
                        search_harness.run_pond(run_dir=run_dir, env_file=str(self.env), backend=backend, db=str(self.db))
                    if enabled:
                        self.warm.assert_called_once_with(api_key="synthetic-file-key")
                        self.assertLess(self.events.index("warmup"), self.events.index("execute_role_search"))
                    else:
                        self.warm.assert_not_called()

    def test_unreviewed_deep_pond_never_warms(self):
        _start(self.root)
        with mock.patch.dict(os.environ, {"POWERPACKS_CROSS_ENCODER_BETA": "1"}), \
                self.assertRaisesRegex(ValueError, "no reviewed payload"):
            search_harness.run_pond(run_dir=self.root, env_file=str(self.env))
        self.warm.assert_not_called()

    def test_retrieval_failure_returns_while_warmup_is_pending(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def handle(request):
            entered.set()
            try:
                release.wait(5)
                return httpx.Response(200)
            finally:
                finished.set()

        def retrieval(command, **kwargs):
            self.assertTrue(entered.wait(2))
            raise pipeline.Failed("synthetic retrieval failure")

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.warm.side_effect = self.real_warm
        self.run.side_effect = retrieval
        try:
            with mock.patch.object(httpx, "Client", return_value=client):
                with self.assertRaisesRegex(pipeline.Failed, "synthetic retrieval failure"):
                    self.execute(self.args("--execute-approved"))
                self.assertFalse(finished.is_set())
        finally:
            release.set()
            self.assertTrue(finished.wait(2))


if __name__ == "__main__":
    unittest.main()
