"""Synthetic memory jobs, real HTTP routes and process interruption; no paid requests."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
import io
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlparse

import httpx

from test_memory import FixtureProvider, STORIES, source_db
from packs.memory import job, runner
from packs.memory.job import Counts, Failure, Job, Status
from packs.memory.progress import Step
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store
from packs.shared.web.memory import MemoryApi


class JobProvider(OpenAIProvider):
    def __init__(self) -> None:
        super().__init__("gpt-6-luna", "text-embedding-3-small", approve_spend=True)
        self.fixture = FixtureProvider()
        self.generation_key = self.fixture.generation_key
        self.embedding_key = self.fixture.embedding_key
        self.inputs: list[str] = []

    def generate(self, content: str) -> str:
        self.inputs.append(content)
        return self.fixture.generate(content)

    def embed(self, content: str) -> tuple[float, ...]:
        return self.fixture.embed(content)

    def discover(self, content: str) -> str:
        return json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps({"decision": "ambiguous", "reason": "synthetic", "tags": []})}]}]})


class MemoryJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source_db(V2(self.root / ".powerpacks").database, STORIES)
        self.provider = JobProvider()
        self.addCleanup(self.provider.close)
        self.patch = patch.object(runner, "provider", return_value=self.provider)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def wait(self) -> Job:
        runner._threads[self.root].join(10)
        self.assertFalse(runner._threads[self.root].is_alive())
        return job.read(self.root)

    def test_record_round_trip(self) -> None:
        record = Job(enabled=True, keep_updated=False, step=Step.GENERATE, status=Status.FAILED,
                     steps={"generate": Counts(2, 3)}, people_indexed=4, approved_spend_usd=1,
                     failures=[Failure(Step.GENERATE, "1:0", "synthetic", "now")])
        job.write(self.root, record)
        self.assertEqual(job.read(self.root), record)

    def test_failure_continues_and_update_retries_failed_chunk_first(self) -> None:
        self.provider.fixture.fail_generation = 2
        runner.start(self.root, 1)
        first = self.wait()
        self.assertEqual(first.status, Status.DONE)
        self.assertEqual(len(first.failures), 1)
        self.assertEqual(first.failures[0].step, Step.GENERATE)
        self.assertEqual(first.steps[Step.GENERATE], Counts(5, 5))
        failed_input = self.provider.inputs[1]
        self.provider.fixture.fail_generation = 0
        before = self.provider.fixture.generations
        runner.update(self.root)
        second = self.wait()
        self.assertEqual(second.failures, [])
        self.assertEqual(self.provider.inputs[-1], failed_input)
        self.assertEqual(self.provider.fixture.generations, before + 1)
        self.assertGreater(second.people_indexed, 0)

    def test_running_refuses_second_start_and_settings_survive(self) -> None:
        entered, release = threading.Event(), threading.Event()
        generate = self.provider.generate

        def paused(content):
            entered.set()
            release.wait(5)
            return generate(content)

        with patch.object(self.provider, "generate", side_effect=paused):
            runner.start(self.root, 1)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ValueError, "already running"):
                    runner.start(self.root, 1)
                with job.LOCK:
                    record = job.read(self.root)
                    record.keep_updated = False
                    job.write(self.root, record)
            finally:
                release.set()
            self.assertFalse(self.wait().keep_updated)

    def test_keep_updated_off_preserves_search_and_completion_on_updates(self) -> None:
        runner.start(self.root, 1)
        self.wait()
        record = job.read(self.root)
        record.keep_updated = False
        job.write(self.root, record)
        with patch.object(runner, "update") as update:
            runner.keep_updated(self.root)
            update.assert_not_called()
        store = Store(job.database(self.root))
        self.addCleanup(store.close)
        self.assertTrue(Query(store, self.provider).run("IAM").hits)
        record.keep_updated = True
        job.write(self.root, record)
        with patch.object(runner, "update", wraps=runner.update) as update:
            runner.keep_updated(self.root)
            update.assert_called_once_with(self.root)
        self.assertEqual(job.read(self.root).status, Status.DONE)

    def test_api_before_during_after_enable_and_settings(self) -> None:
        routes = MemoryApi(self.root)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                routes.get(self, urlparse(self.path))

            def do_POST(self):  # noqa: N802
                routes.post(self, urlparse(self.path))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection(*server.server_address)
        self.addCleanup(connection.close)

        def request(method, path, body=None):
            connection.request(method, path, json.dumps(body) if body is not None else None)
            result = connection.getresponse()
            return result.status, json.loads(result.read())

        status, before = request("GET", "/api/memory")
        self.assertEqual(status, 200)
        self.assertFalse(before["enabled"])
        self.assertEqual(before["inventory"]["counts"]["parents"], 4)
        self.assertIn("generation_input_tokens", before["estimate"])
        seen = []
        write = job.write

        def observe(root, record):
            write(root, record)
            if record.status == Status.RUNNING:
                seen.append(asdict(record))

        entered, release = threading.Event(), threading.Event()
        generate = self.provider.generate

        def paused(content):
            entered.set()
            release.wait(5)
            return generate(content)

        with patch.object(job, "write", side_effect=observe), patch.object(self.provider, "generate", side_effect=paused):
            self.assertEqual(request("POST", "/api/memory/enable", {"approved_spend_usd": 1})[0], 202)
            self.assertTrue(entered.wait(5))
            try:
                during = request("GET", "/api/memory")[1]
                self.assertEqual((during["status"], during["step"]), ("running", "generate"))
            finally:
                release.set()
            after = self.wait()
        self.assertEqual(request("GET", "/api/memory")[1]["status"], "done")
        self.assertEqual(after.status, Status.DONE)
        self.assertEqual({record["step"] for record in seen}, set(job.STEPS))
        self.assertTrue(any(record["steps"]["generate"]["done"] for record in seen))
        self.assertEqual(request("POST", "/api/memory/settings", {"keep_updated": False})[0], 200)
        self.assertFalse(request("GET", "/api/memory")[1]["keep_updated"])
        self.assertEqual(request("POST", "/api/memory/update", {"approved_spend_usd": 2})[0], 202)
        self.assertEqual(self.wait().status, Status.DONE)
        self.assertEqual(job.read(self.root).approved_spend_usd, 2)
        meanings = request("GET", "/api/memory/search?q=IAM")[1]
        self.assertTrue(meanings)
        page = request("GET", "/api/memory/people?" + urlencode({**meanings[0]["selector"], "limit": 1}))[1]
        self.assertEqual(page["total"], meanings[0]["count"])
        self.assertEqual(len(page["people"]), 1)
        self.assertEqual(request("GET", "/api/memory/people?origin=topic&value=iam&limit=0")[0], 400)
        self.assertEqual(request("POST", "/api/memory/enable", {"approved_spend_usd": -1})[0], 400)

    def test_deep_context_completion_triggers_incremental_update(self) -> None:
        from packs.ingestion.primitives.deep_context_v2 import run as deep_context
        runner.start(self.root, 1)
        self.wait()
        with ExitStack() as patches, redirect_stdout(io.StringIO()):
            for name, value in (("resolve_operator_id", "synthetic"), ("stages", []),
                                ("realize", self.root / "people.csv"), ("index_in_background", self.root / "index.log"),
                                ("start_review", "http://localhost/"), ("pending_reviews", 0)):
                patches.enter_context(patch.object(deep_context, name, return_value=value))
            updated = patches.enter_context(patch.object(runner, "update", wraps=runner.update))
            self.assertEqual(deep_context.run(self.root / ".powerpacks", 0, self.root / "mail", self.root / "chat", ""), 0)
            updated.assert_called_once_with(self.root)
        self.assertEqual(job.read(self.root).status, Status.DONE)

    def test_provider_budget_and_actual_token_cost(self) -> None:
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 20}})

        transport = httpx.Client(base_url="https://example.com/", transport=httpx.MockTransport(respond))
        self.addCleanup(transport.close)
        client = OpenAIProvider("gpt-6-luna", "text-embedding-3-small", client=transport,
                                approve_spend=True, approved_spend_usd=0)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}):
            with self.assertRaisesRegex(ValueError, "spend limit"):
                client.generate("synthetic")
            self.assertEqual(calls, [])
            client.approved_spend_usd = 1
            client.generate("synthetic")
        self.assertEqual(asdict(client.usage), {"input": 100, "output": 20, "embedding": 0})
        self.assertAlmostEqual(client.usage.cost(client.model, client.embedding_key), 0.00002)

    def test_killed_generate_resumes_without_repeating_paid_calls(self) -> None:
        script = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from test_memory_job import JobProvider
from packs.memory import job, runner
from packs.shared.web.memory import MemoryApi
from unittest.mock import Mock
from urllib.parse import urlparse
root = Path(sys.argv[1])
provider = JobProvider()
generate = provider.generate
def counted(content):
    with (root / "calls").open("a") as log:
        log.write(content + "\\n")
    return generate(content)
provider.generate = counted
write = job.write
def interrupt(root, record):
    write(root, record)
    if sys.argv[2] in ("mid", "between") and record.step == "generate" and record.steps["generate"].done == (1 if sys.argv[2] == "mid" else 0):
        os._exit(9)
from packs.memory.store import Store
import_progress = Store.import_progress
def imported(*args, **kwargs):
    with (root / "imports").open("a") as log:
        log.write("import\\n")
    return import_progress(*args, **kwargs)
with patch.object(runner, "provider", return_value=provider), patch.object(job, "write", side_effect=interrupt), patch.object(Store, "import_progress", imported):
    if sys.argv[2] == "resume":
        with patch("packs.shared.web.memory._send_json"):
            MemoryApi(root).get(Mock(), urlparse("/api/memory"))
    else:
        runner.start(root, 1)
    runner._threads[root].join()
'''
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent) + os.pathsep + str(Path.cwd())}
        for mode in ("mid", "between"):
            with self.subTest(mode=mode):
                root = self.root / mode
                source_db(V2(root / ".powerpacks").database, STORIES)
                first = subprocess.run([sys.executable, "-c", script, str(root), mode], env=env, capture_output=True)
                self.assertEqual(first.returncode, 9, first.stderr)
                second = subprocess.run([sys.executable, "-c", script, str(root), "resume"], env=env, capture_output=True)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(job.read(root).status, Status.DONE)
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(len(calls), 5)
                self.assertEqual(len(set(calls)), 5)
                self.assertEqual((root / "imports").read_text().splitlines(), ["import"])


if __name__ == "__main__":
    unittest.main()
