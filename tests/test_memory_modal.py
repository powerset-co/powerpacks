"""Memory dispatch and recovery with an in-memory volume; no provider or Modal calls."""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from aiohttp import ClientError
from modal import Sandbox
from modal.exception import NotFoundError, ServiceError

from packs.indexing.modal import linkedin_modal_pipeline as pipeline

from test_memory import STORIES, source_db
from test_memory_job import JobProvider
from packs.memory import job, modal_entrypoint, modal_run, runner
from packs.memory.job import Counts, Failure, Job, Status
from packs.memory.progress import Step
from packs.memory.provider import SpendReached
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store
from packs.shared.web.memory import MemoryApi
from urllib.parse import urlparse


class Volume:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploads = 0

    @contextmanager
    def batch_upload(self, *, force):
        self.uploads += 1
        yield self

    def put_file(self, source, target):
        self.files[target] = source.read() if isinstance(source, io.BytesIO) else source.read_bytes()

    def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        yield self.files[path]


class MemoryModalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.volume = Volume()
        self.client = JobProvider()
        self.addCleanup(self.client.close)
        for target, value in (("packs.indexing.modal.linkedin_modal_pipeline.get_volume", self.volume),
                              ("packs.memory.runner.provider", self.client)):
            patched = patch(target, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        environment = patch.dict(os.environ, {"POWERPACKS_MEMORY_REMOTE": "1", "POWERPACKS_OPERATOR_ID": "synthetic", "MODAL_TOKEN_ID": "synthetic",
                                             "MODAL_TOKEN_SECRET": "synthetic", "OPENAI_API_KEY": "synthetic"})
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = modal_run.Remote(self.root, "synthetic")
        self.status_path = f"{self.remote.directory}/job.json"
        self.database_path = f"{self.remote.directory}/memory.sqlite"

    def wait(self) -> Job:
        thread = runner._threads[self.root]
        thread.join(10)
        self.assertFalse(thread.is_alive())
        return job.read(self.root)

    def test_stdout_mirrors_progress_before_exit_and_preserves_local_settings(self) -> None:
        local_failures = [Failure(Step.IMPORT, "1:0", "synthetic import failure", "now")]
        job.write(self.root, Job(enabled=True, status=Status.RUNNING, keep_updated=False, pid=123, started_at="synthetic",
                                 failures=local_failures))
        status = Job(status=Status.RUNNING, step=Step.GENERATE, steps={Step.GENERATE: Counts(2, 5)},
                     current_item="3:0", spent_usd=0.04, approved_spend_usd=0.1,
                     tokens={"input": 100, "output": 20, "embedding": 0},
                     failures=[Failure(Step.GENERATE, "2:0", "synthetic failure", "now")])
        sandbox = Mock(object_id="sb-synthetic")
        self.volume.files[self.database_path] = b"finished database"
        job.database(self.root).write_bytes(b"original database")

        def lines():
            yield json.dumps(asdict(status)) + "\n"
            sandbox.wait.assert_not_called()
            self.assertEqual(job.database(self.root).read_bytes(), b"original database")
            during = job.read(self.root)
            self.assertEqual(during.steps[Step.GENERATE], Counts(2, 5))
            self.assertEqual(during.current_item, "3:0")
            self.assertEqual(during.tokens, status.tokens)
            self.assertEqual(during.failures, local_failures)
            self.assertEqual(during.spent_usd, 0.04)
            self.assertFalse(during.keep_updated)
            self.assertEqual(during.pid, 123)
            status.status = Status.DONE
            status.stopped = "approved spend reached"
            status.people_indexed = 4
            yield json.dumps(asdict(status)) + "\n"
            self.assertEqual(job.read(self.root).status, Status.RUNNING)
            self.assertEqual(job.database(self.root).read_bytes(), b"original database")

        sandbox.stdout = lines()
        sandbox.wait.side_effect = lambda: self.volume.files.update(
            {self.status_path: json.dumps(asdict(status)).encode()})
        download = self.remote._download

        def download_while_running():
            self.assertEqual(job.read(self.root).status, Status.RUNNING)
            download()
            self.assertEqual(job.read(self.root).status, Status.RUNNING)

        with patch.object(self.volume, "read_file", wraps=self.volume.read_file) as read, \
                patch.object(self.remote, "_download", side_effect=download_while_running):
            self.remote._watch(sandbox)
        self.assertEqual(read.call_args_list, [call(self.status_path), call(self.database_path)])
        sandbox.wait.assert_called_once()
        result = job.read(self.root)
        self.assertEqual(result.status, Status.DONE)
        self.assertEqual(result.stopped, "approved spend reached")
        self.assertEqual(result.people_indexed, 4)
        self.assertEqual(result.failures, status.failures)
        self.assertEqual(job.database(self.root).read_bytes(), b"finished database")
        self.assertEqual(job.database(self.root).with_suffix(".sqlite.bkup").read_bytes(), b"original database")

    def test_update_reattaches_running_sandbox_without_upload_or_second_start(self) -> None:
        record = Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE, started_at="synthetic",
                     approved_spend_usd=0.1)
        job.write(self.root, record)
        job.database(self.root).write_bytes(b"local database")
        self.volume.files[self.database_path] = b"remote database"
        self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
        sandbox = Mock(object_id="sb-same-synthetic")

        def lines():
            record.steps[Step.GENERATE] = Counts(1, 3)
            record.spent_usd = 0.03
            yield json.dumps(asdict(record)) + "\n"
            self.assertEqual(job.read(self.root).steps[Step.GENERATE], Counts(1, 3))
            sandbox.wait.assert_not_called()
            record.status, record.step = Status.DONE, Step.DONE
            self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
            yield json.dumps(asdict(record)) + "\n"

        sandbox.stdout = lines()
        with patch.object(Sandbox, "from_name", return_value=sandbox) as lookup, \
                patch.object(pipeline, "memory_sandbox") as create, \
                patch.object(Store, "import_progress") as import_progress:
            runner.update(self.root)
            self.assertEqual(self.wait().status, Status.DONE)
        lookup.assert_called_once_with(pipeline.APP_NAME, "memory-synthetic")
        create.assert_not_called()
        import_progress.assert_not_called()
        self.assertEqual(self.volume.uploads, 0)
        self.assertEqual(job.database(self.root).read_bytes(), b"remote database")

    def test_handoff_runs_existing_steps_and_downloads_searchable_database(self) -> None:
        source_db(V2(self.root / ".powerpacks").database, STORIES)
        sandbox = Mock(object_id="sb-new-synthetic")

        def execute(name, operator_id, *, discover):
            self.assertEqual(name, "memory-synthetic")
            self.assertEqual(operator_id, "synthetic")
            with tempfile.TemporaryDirectory() as remote_dir:
                remote_root = Path(remote_dir)
                database = job.database(remote_root)
                database.parent.mkdir(parents=True)
                database.write_bytes(self.volume.files[self.database_path])
                (database.parent / "job.json").write_bytes(self.volume.files[self.status_path])
                with patch.object(Store, "import_progress", side_effect=AssertionError("remote import")):
                    runner._run(remote_root, True, None, self.client, discover, steps=job.STEPS[1:])
                self.volume.files[self.database_path] = database.read_bytes()
                self.volume.files[self.status_path] = (database.parent / "job.json").read_bytes()
                sandbox.stdout = [json.dumps(asdict(job.read(remote_root))) + "\n"]
            return sandbox

        with patch.object(Sandbox, "from_name", side_effect=NotFoundError("synthetic")), \
                patch.object(pipeline, "memory_sandbox", side_effect=execute) as create:
            runner.start(self.root, 1)
            record = self.wait()
        create.assert_called_once()
        self.assertEqual(self.volume.uploads, 1)
        self.assertEqual(record.status, Status.DONE)
        self.assertGreater(record.people_indexed, 0)
        store = Store(job.database(self.root))
        self.addCleanup(store.close)
        self.assertTrue(Query(store, self.client).run("IAM").hits)

    def test_operator_id_is_resolved_from_env_at_start_and_missing_id_fails(self) -> None:
        error = ("memory: no operator id. Run `$powerset login` (it writes POWERPACKS_OPERATOR_ID "
                 "to .env).")
        for value in ("", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(operator_id=value), patch.dict(os.environ, {"POWERPACKS_OPERATOR_ID": value}), \
                    patch.object(pipeline, "get_volume") as volume:
                result = runner.start(self.root, 0.50)
                self.assertEqual(result.status, Status.FAILED)
                self.assertEqual(result.error, error)
                self.assertEqual(job.read(self.root).error, error)
                volume.assert_not_called()
        (self.root / ".env").write_text("POWERPACKS_OPERATOR_ID=synthetic-account\n")
        with patch.dict(os.environ), patch.object(runner, "_run") as run:
            os.environ.pop("POWERPACKS_OPERATOR_ID", None)
            result = runner.start(self.root, 0.50)
            self.wait()
        self.assertEqual(result.status, Status.RUNNING)
        self.assertEqual(run.call_args.kwargs["operator_id"], "synthetic-account")
        os.close(run.call_args.args[2])

    def test_runtime_operator_owns_volume_directory_and_sandbox_root(self) -> None:
        remote = modal_run.Remote(self.root, "synthetic-account")
        self.assertEqual(remote.directory, "operators/synthetic-account/.powerpacks/memory")
        with patch.object(pipeline.modal.App, "lookup"), \
                patch.object(pipeline, "build_image"), \
                patch.object(pipeline.modal.Secret, "from_name"), \
                patch.object(pipeline.modal.Sandbox, "create") as create:
            pipeline.memory_sandbox(remote.name, "synthetic-account", discover=False)
        self.assertEqual(create.call_args.args[3], "/data/operators/synthetic-account")

    def test_remote_stop_lowers_only_local_budget_and_mirror_preserves_it(self) -> None:
        job.write(self.root, Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE,
                                 approved_spend_usd=0.50, spent_usd=0.03))
        handler = Mock(headers={}, rfile=io.BytesIO(b"{}"))
        with patch("packs.shared.web.memory._send_json") as send:
            self.assertTrue(MemoryApi(self.root).post(handler, urlparse("/api/memory/stop")))
        self.assertEqual(send.call_args.args[1]["approved_spend_usd"], 0.03)
        modal_run._mirror(self.root, Job(status=Status.RUNNING, step=Step.GENERATE,
                                        approved_spend_usd=0.50, spent_usd=0.04))
        self.assertEqual(job.read(self.root).approved_spend_usd, 0.03)
        self.assertEqual(job.read(self.root).spent_usd, 0.04)
        self.assertEqual(self.volume.uploads, 0)

    def test_missing_modal_token_fails_before_upload(self) -> None:
        (self.root / ".env").write_text("POWERPACKS_MEMORY_REMOTE=1\n")
        with patch.dict(os.environ, {"MODAL_TOKEN_SECRET": ""}), \
                patch.object(pipeline, "get_volume") as volume:
            os.environ.pop("POWERPACKS_MEMORY_REMOTE")
            result = runner.start(self.root, 0.1)
        self.assertEqual(result.status, Status.FAILED)
        self.assertEqual(result.error, "Missing Modal token: MODAL_TOKEN_SECRET")
        volume.assert_not_called()
        self.assertEqual(self.volume.uploads, 0)

    def test_stdout_and_wait_errors_retry_and_recover(self) -> None:
        record = Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE)
        job.write(self.root, record)
        job.database(self.root).write_bytes(b"local database")
        self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
        self.volume.files[self.database_path] = b"finished database"
        sandbox = Mock(object_id="sb-synthetic")
        running_line = json.dumps(asdict(record)) + "\n"

        def lines():
            yield running_line
            self.assertEqual(job.read(self.root).status, Status.RUNNING)
            raise ClientError("synthetic connection lost")

        sandbox.stdout = lines()
        recovered = Mock(object_id=sandbox.object_id)
        record.status, record.step = Status.DONE, Step.DONE
        recovered.stdout = [json.dumps(asdict(record)) + "\n"]
        self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
        recovered.wait.side_effect = [OSError("synthetic wait lost"), None]

        with patch.object(Sandbox, "from_id", return_value=recovered), \
                patch.object(modal_run.time, "sleep") as sleep:
            self.remote._watch(sandbox)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(recovered.wait.call_count, 2)
        self.assertEqual(job.read(self.root).status, Status.DONE)
        self.assertEqual(job.database(self.root).read_bytes(), b"finished database")

    def test_fresh_start_downloads_newer_finished_remote_job_before_import_or_upload(self) -> None:
        record = Job(enabled=True, status=Status.FAILED, step=Step.GENERATE,
                     started_at="2026-10-01T00:00:00+00:00", approved_spend_usd=0.1)
        job.write(self.root, record)
        database = job.database(self.root)
        database.write_bytes(b"older local database")
        os.utime(database, (1, 1))
        record.status, record.step = Status.DONE, Step.DONE
        self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
        self.volume.files[self.database_path] = b"finished database"

        with patch.object(Sandbox, "from_name", side_effect=NotFoundError("synthetic")), \
                patch.object(pipeline, "memory_sandbox") as create, \
                patch.object(Store, "import_progress") as import_progress:
            runner.start(self.root, 0.1)
            self.assertEqual(self.wait().status, Status.DONE)
        create.assert_not_called()
        import_progress.assert_not_called()
        self.assertEqual(self.volume.uploads, 0)
        self.assertEqual(database.read_bytes(), b"finished database")

    def test_status_read_retries_are_bounded(self) -> None:
        for error in (ServiceError, OSError, ClientError):
            with self.subTest(error=error), \
                    patch.object(self.volume, "read_file", side_effect=error("synthetic outage")) as read, \
                    patch.object(modal_run.time, "sleep") as sleep:
                with self.assertRaisesRegex(error, "synthetic outage"):
                    self.remote._status()
            self.assertEqual(read.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(3), call(3)])

    def test_stdout_and_wait_retries_are_bounded_and_fail_job(self) -> None:
        for operation in ("stdout", "wait"):
            with self.subTest(operation=operation):
                record = Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE)
                job.write(self.root, record)
                sandbox = Mock(object_id="sb-synthetic")
                sandbox.stdout = []
                if operation == "stdout":
                    sandbox.stdout = MagicMock()
                    sandbox.stdout.__iter__.side_effect = OSError("synthetic outage")
                else:
                    sandbox.wait.side_effect = ServiceError("synthetic outage")
                with patch.object(Sandbox, "from_name", return_value=sandbox), \
                        patch.object(Sandbox, "from_id", return_value=sandbox), \
                        patch.object(modal_run.time, "sleep") as sleep:
                    runner.update(self.root)
                    result = self.wait()
                self.assertEqual(result.status, Status.FAILED)
                self.assertEqual(result.error, "synthetic outage")
                self.assertEqual(sleep.call_count, 2)

    def test_stdout_eof_waits_then_reads_final_volume_status(self) -> None:
        record = Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE)
        job.write(self.root, record)
        job.database(self.root).write_bytes(b"local database")
        self.volume.files[self.database_path] = b"finished database"
        sandbox = Mock(object_id="sb-synthetic", stdout=[json.dumps(asdict(record)) + "\n"])

        def finish():
            record.status, record.step = Status.DONE, Step.DONE
            self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()

        sandbox.wait.side_effect = finish
        self.remote._watch(sandbox)
        self.assertEqual(job.read(self.root).status, Status.DONE)

    def test_terminal_stdout_still_reads_final_volume_failures_once(self) -> None:
        record = Job(enabled=True, status=Status.DONE, step=Step.DONE)
        job.write(self.root, record)
        job.database(self.root).write_bytes(b"local database")
        self.volume.files[self.database_path] = b"finished database"
        sandbox = Mock(object_id="sb-synthetic", stdout=[json.dumps(asdict(record)) + "\n"])
        record.failures = [Failure(Step.GENERATE, "2:0", "synthetic final failure", "now")]

        def finish():
            self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()

        sandbox.wait.side_effect = finish
        with patch.object(self.remote, "_status", wraps=self.remote._status) as read:
            self.remote._watch(sandbox)
        read.assert_called_once()
        self.assertEqual(job.read(self.root).failures, record.failures)

    def test_entrypoint_streams_progress_while_run_is_active_and_final_status(self) -> None:
        failures = [Failure(Step.IMPORT, str(index), "synthetic failure", "now") for index in range(275)]
        job.write(self.root, Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE, failures=failures))
        printed = threading.Event()

        class Output(io.StringIO):
            def write(self, value):
                if '"current_item": "synthetic"' in value:
                    printed.set()
                return super().write(value)

        output = Output()

        def run(*args, **kwargs):
            record = job.read(self.root)
            record.steps[Step.GENERATE] = Counts(1, 3)
            record.spent_usd = 0.03
            record.current_item = "synthetic"
            job.write(self.root, record)
            self.assertTrue(printed.wait(2), "progress was not printed while _run was active")
            record.status, record.step = Status.DONE, Step.DONE
            record.current_item = ""
            job.write(self.root, record)

        with patch("sys.argv", ["modal_entrypoint", str(self.root), "1"]), \
                patch("sys.stdout", output), patch.object(modal_entrypoint, "provider", return_value=self.client), \
                patch.object(modal_entrypoint, "_run", side_effect=run):
            self.assertEqual(modal_entrypoint.main(), 0)
        records = [job.decode(line) for line in output.getvalue().splitlines()]
        progress = next(record for record in records if record.current_item == "synthetic")
        self.assertEqual(progress.steps[Step.GENERATE], Counts(1, 3))
        self.assertEqual(progress.spent_usd, 0.03)
        self.assertEqual(records[-1].status, Status.DONE)
        self.assertTrue(all(record.failures == [] for record in records))
        self.assertTrue(all(len(line.encode()) < 1024 for line in output.getvalue().splitlines()))
        self.assertEqual(job.read(self.root).failures, failures)

    def test_entrypoint_missing_key_prints_without_failures_and_preserves_volume_record(self) -> None:
        failures = [Failure(Step.IMPORT, "1:0", "synthetic import failure", "now")]
        job.write(self.root, Job(failures=failures))
        output = io.StringIO()
        with patch("sys.argv", ["modal_entrypoint", str(self.root), "1"]), \
                patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("sys.stdout", output):
            self.assertEqual(modal_entrypoint.main(), 1)
        self.assertEqual(job.decode(output.getvalue()).failures, [])
        self.assertEqual(job.read(self.root).failures, failures)

    def test_entrypoint_publishes_spend_stop_without_importing(self) -> None:
        source_db(V2(self.root / ".powerpacks").database, STORIES)
        store = Store(job.database(self.root), create=True)
        store.import_source(V2(self.root / ".powerpacks"))
        store.close()
        record = Job(enabled=True, status=Status.RUNNING, step=Step.GENERATE, approved_spend_usd=0.1)
        job.write(self.root, record)
        records = []
        write = job.write

        def observe(root, record):
            write(root, record)
            records.append(job.read(root))

        def stop(_):
            self.client.usage.input = 100
            raise SpendReached("synthetic budget")

        with patch("sys.argv", ["modal_entrypoint", str(self.root), "1"]), \
                patch.object(modal_entrypoint, "provider", return_value=self.client), \
                patch.object(job, "write", side_effect=observe), \
                patch.object(self.client, "generate", side_effect=stop), \
                patch.object(Store, "import_progress", side_effect=AssertionError("remote import")):
            self.assertEqual(modal_entrypoint.main(), 0)
        self.assertGreater(len(records), 1)
        self.assertEqual(records[0].step, Step.GENERATE)
        self.assertEqual(records[-1].status, Status.DONE)
        self.assertEqual(records[-1].stopped, "approved spend reached")
        self.assertGreater(records[-1].spent_usd, 0)
        self.assertLess(records[-1].spent_usd, 0.1)
        self.assertEqual(records[-1].people_indexed, 0)

    def test_finished_while_disconnected_downloads_without_second_start(self) -> None:
        record = Job(enabled=True, status=Status.RUNNING, step=Step.INDEX, started_at="synthetic",
                     approved_spend_usd=0.1)
        job.write(self.root, record)
        job.database(self.root).write_bytes(b"local database")
        record.status, record.step = Status.DONE, Step.DONE
        self.volume.files[self.status_path] = json.dumps(asdict(record)).encode()
        self.volume.files[self.database_path] = b"finished database"
        with patch.object(Sandbox, "from_name", side_effect=NotFoundError("synthetic")), \
                patch.object(pipeline, "memory_sandbox") as create:
            runner.update(self.root)
            self.assertEqual(self.wait().status, Status.DONE)
        create.assert_not_called()
        self.assertEqual(job.database(self.root).read_bytes(), b"finished database")


if __name__ == "__main__":
    unittest.main()
