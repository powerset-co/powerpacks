"""Disconnected provider reads never submit another paid task group."""
import ssl
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from parallel.types import TaskGroupStatus, TaskGroupStatusEvent, TaskRunEvent, TaskRunJsonOutput
from packs.ingestion.primitives.deep_context.enrich.parallel_research import parallel_client


def write_receipt(path, receipt):
    from packs.ingestion.primitives.common.jsonio import write_json
    write_json(path, {"parallel": receipt})


class Events:
    def __init__(self, values):
        self.values = values
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def __iter__(self):
        for value in self.values:
            if isinstance(value, Exception):
                raise value
            yield value


class ParallelStreamResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.params = SimpleNamespace(batch_size=500, stream_timeout=60, output_dir=Path(self.tmp.name))

    def test_disconnected_active_stream_reads_same_group_until_terminal(self):
        self._assert_stream_resumes_after(httpx.RemoteProtocolError('SSE closed'))

    def test_any_stream_error_reads_same_group_until_terminal(self):
        self._assert_stream_resumes_after(ssl.SSLError('bad record mac'))

    def _assert_stream_resumes_after(self, error):
        active = TaskGroupStatus(is_active=True, num_task_runs=1, task_run_status_counts={'running': 1})
        terminal = TaskGroupStatus(is_active=False, num_task_runs=1, task_run_status_counts={'completed': 1})
        output = TaskRunJsonOutput.model_validate({'type': 'json', 'content': {'real_name': 'Jordan Bravo'}, 'basis': []})
        completed = TaskRunEvent.model_validate({'type': 'task_run.state', 'event_id': 'run-event', 'run': {
            'interaction_id': 'interaction', 'run_id': 'run', 'processor': 'core2x',
            'is_active': False, 'status': 'completed', 'metadata': {'handle': 'jordan'},
        }, 'output': output.model_dump()})
        group = SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(task_group_id='group')),
            add_runs=Mock(),
            events=Mock(side_effect=[Events([
                TaskGroupStatusEvent(type='task_group_status', event_id='active', status=active),
                error,
            ]), Events([TaskGroupStatusEvent(type='task_group_status', event_id='done', status=terminal)])]),
            retrieve=Mock(return_value=SimpleNamespace(status=active)),
            get_runs=Mock(return_value=Events([completed])),
        )
        received = []
        with patch.object(parallel_client.time, 'sleep'), patch.object(parallel_client, 'Parallel', return_value=SimpleNamespace(task_group=group)):
            errors = parallel_client.ParallelClient('fixture', 'https://parallel.test', '').execute(
                [{'input': {}, 'metadata': {'handle': 'jordan'}, 'processor': 'core2x'}],
                self.params, lambda _: None,
                lambda handle, result: received.append((handle, result)),
            )
        self.assertEqual(errors, ())
        self.assertEqual(len(received), 1)
        group.create.assert_called_once()
        group.add_runs.assert_called_once()
        self.assertEqual(group.events.call_count, 2)
        self.assertTrue(all(call.args[0] == 'group' for call in group.events.call_args_list))

    def test_resume_remaining_subset_does_not_submit_existing_runs(self):
        write_json = write_receipt
        inputs = [{"input": {}, "metadata": {"handle": handle}, "processor": "core2x"}
                  for handle in ("jordan", "casey")]
        write_json(self.params.output_dir / "manifest.json", {
            "provider_contract": {"task_spec": parallel_client.config.TASK_SPEC, "beta_header": ""},
            "task_group_id": "prior-group", "inputs": {item["metadata"]["handle"]: item for item in inputs},
        })
        terminal = TaskGroupStatus(is_active=False, num_task_runs=2, task_run_status_counts={"completed": 2})
        output = TaskRunJsonOutput(type="json", content={}, basis=[])
        runs = [TaskRunEvent.model_validate({"type": "task_run.state", "run": {
            "interaction_id": handle, "run_id": handle, "processor": "core2x",
            "is_active": False, "status": "completed", "metadata": {"handle": handle},
        }, "output": output.model_dump()}) for handle in ("jordan", "casey")]
        group = SimpleNamespace(create=Mock(), add_runs=Mock(),
            get_runs=Mock(return_value=Events(runs)),
            events=Mock(return_value=Events([TaskGroupStatusEvent(type="task_group_status", event_id="done", status=terminal)])))
        received = []
        with patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            errors = parallel_client.ParallelClient("fixture", "https://parallel.test", "").execute(
                inputs[1:], self.params, lambda _: None, lambda handle, _: received.append(handle))
        self.assertEqual(errors, ())
        self.assertEqual(received, ["casey"])
        group.create.assert_not_called()
        group.add_runs.assert_not_called()
        self.assertEqual(group.get_runs.call_args.args[0], "prior-group")

    def test_ambiguous_submission_preserves_group_for_resume(self):
        inputs = [{"input": {}, "metadata": {"handle": "jordan"}, "processor": "core2x"}]
        terminal = TaskGroupStatus(is_active=False, num_task_runs=1, task_run_status_counts={"completed": 1})
        completed = TaskRunEvent.model_validate({"type": "task_run.state", "run": {
            "interaction_id": "jordan", "run_id": "jordan", "processor": "core2x",
            "is_active": False, "status": "completed", "metadata": {"handle": "jordan"},
        }, "output": {"type": "json", "content": {}, "basis": []}})
        group = SimpleNamespace(create=Mock(return_value=SimpleNamespace(task_group_id="paid-group")),
            add_runs=Mock(side_effect=httpx.ReadTimeout("POST response lost")),
            get_runs=Mock(return_value=Events([completed])),
            events=Mock(return_value=Events([TaskGroupStatusEvent(type="task_group_status", event_id="done", status=terminal)])))
        with patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            client = parallel_client.ParallelClient("fixture", "https://parallel.test", "")
            with self.assertRaises(httpx.ReadTimeout):
                client.execute(inputs, self.params, lambda _: None, lambda *_: None)
            self.assertTrue((self.params.output_dir / "manifest.json").exists())
            self.assertEqual(client.execute(inputs, self.params, lambda _: None, lambda *_: None), ())
        group.create.assert_called_once()
        group.add_runs.assert_called_once()

    def test_historical_terminal_event_does_not_finish_newly_added_run(self):
        write_json = write_receipt
        inputs = [{"input": {}, "metadata": {"handle": "casey"}, "processor": "core2x"}]
        write_json(self.params.output_dir / "manifest.json", {
            "task_group_id": "group", "inputs": {"casey": inputs[0]},
            "provider_contract": {"task_spec": parallel_client.config.TASK_SPEC, "beta_header": ""},
        })
        old = TaskGroupStatus(is_active=False, num_task_runs=0, task_run_status_counts={})
        active = TaskGroupStatus(is_active=True, num_task_runs=1, task_run_status_counts={"running": 1})
        final = TaskGroupStatus(is_active=False, num_task_runs=1, task_run_status_counts={"completed": 1})
        done = TaskRunEvent.model_validate({"type": "task_run.state", "run": {
            "interaction_id": "casey", "run_id": "casey", "processor": "core2x",
            "is_active": False, "status": "completed", "metadata": {"handle": "casey"},
        }, "output": {"type": "json", "content": {}, "basis": []}})
        group = SimpleNamespace(create=Mock(), add_runs=Mock(),
            get_runs=Mock(side_effect=[Events([]), Events([done])]),
            events=Mock(side_effect=[Events([TaskGroupStatusEvent(type="task_group_status", event_id="old", status=old)]),
                Events([TaskGroupStatusEvent(type="task_group_status", event_id="new", status=final)])]),
            retrieve=Mock(side_effect=[SimpleNamespace(status=active), SimpleNamespace(status=final)]))
        received = []
        with patch.object(parallel_client.time, "sleep"), patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            errors = parallel_client.ParallelClient("fixture", "https://parallel.test", "").execute(
                inputs, self.params, lambda _: None, lambda handle, _: received.append(handle))
        self.assertEqual(errors, ())
        self.assertEqual(received, ["casey"])
        self.assertEqual(group.events.call_count, 2)

    def test_growing_queue_reuses_group_and_adds_only_new_handle(self):
        self._assert_receipt_reuses_group(grow=True)

    def test_failed_run_is_retried_once_without_replaying_old_error(self):
        self._assert_receipt_reuses_group(grow=False)

    def _assert_receipt_reuses_group(self, *, grow, active=False):
        write_json = write_receipt
        import json
        def input_for(handle):
            return {"input": {}, "metadata": {"handle": handle}, "processor": "core2x"}
        def run_for(handle, run_id, state):
            return TaskRunEvent.model_validate({"type": "task_run.state", "run": {
                "interaction_id": run_id, "run_id": run_id, "processor": "core2x",
                "is_active": state == "running", "status": state, "metadata": {"handle": handle},
            }, "output": {"type": "json", "content": {}, "basis": []} if state == "completed" else None})
        original = input_for("jordan")
        write_json(self.params.output_dir / "manifest.json", {
            "task_group_id": "prior", "inputs": {"jordan": original},
            "provider_contract": {"task_spec": parallel_client.config.TASK_SPEC, "beta_header": ""},
        })
        requested = [original, input_for("casey")] if grow else [original]
        old = run_for("jordan", "old", "running" if active else "completed" if grow else "failed")
        new = run_for("casey" if grow else "jordan", "new", "completed")
        final = TaskGroupStatus(is_active=False, num_task_runs=2, task_run_status_counts={"completed": 2})
        def add(group_id, **kwargs):
            receipt = json.loads((self.params.output_dir / "manifest.json").read_text())
            self.assertEqual(set(receipt["parallel"]["inputs"]), {item["metadata"]["handle"] for item in requested})
        group = SimpleNamespace(create=Mock(), add_runs=Mock(side_effect=add),
            get_runs=Mock(side_effect=[Events([old]), Events([old, new])]),
            events=Mock(return_value=Events([TaskGroupStatusEvent(type="task_group_status", event_id="done", status=final)])),
            retrieve=Mock(return_value=SimpleNamespace(status=final)))
        with patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            errors = parallel_client.ParallelClient("fixture", "https://parallel.test", "").execute(
                requested, self.params, lambda _: None, lambda *_: None)
        self.assertEqual(errors, ())
        group.create.assert_not_called()
        self.assertEqual(group.add_runs.call_args.kwargs["inputs"], [requested[-1]])

    def test_growing_queue_preserves_active_paid_run(self):
        self._assert_receipt_reuses_group(grow=True, active=True)

    def test_read_outage_exits_after_three_failures_with_receipt(self):
        group = SimpleNamespace(create=Mock(return_value=SimpleNamespace(task_group_id="paid")),
            add_runs=Mock(), events=Mock(return_value=Events([TaskGroupStatusEvent(
                type="task_group_status", event_id="done", status=TaskGroupStatus(
                    is_active=False, num_task_runs=1, task_run_status_counts={"completed": 1}))])),
            get_runs=Mock(side_effect=httpx.RemoteProtocolError("offline")))
        inputs = [{"input": {}, "metadata": {"handle": "jordan"}, "processor": "core2x"}]
        with patch.object(parallel_client.time, "sleep"), patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            with self.assertRaises(httpx.RemoteProtocolError):
                parallel_client.ParallelClient("fixture", "https://parallel.test", "").execute(
                    inputs, self.params, lambda _: None, lambda *_: None)
        self.assertEqual(group.get_runs.call_count, 3)
        group.add_runs.assert_called_once()
        self.assertTrue((self.params.output_dir / "manifest.json").exists())

    def test_provider_receipt_survives_native_progress_and_failure_writes(self):
        import json
        from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import EnrichmentReceipt
        ui = EnrichmentReceipt(self.params.output_dir / "manifest.json")
        ui.write({"status": "running", "stage": "enrich", "counts": {"total": 1}})
        terminal = TaskGroupStatus(is_active=False, num_task_runs=1, task_run_status_counts={"completed": 1})
        def add(*args, **kwargs):
            document = json.loads(ui.path.read_text())
            self.assertEqual(document["status"], "running")
            ui.write({"status": "running", "counts": {"completed": 0}})
        def progress(_):
            ui.write({"status": "failed", "error": "stream disconnected"})
        group = SimpleNamespace(create=Mock(return_value=SimpleNamespace(task_group_id="paid")),
            add_runs=Mock(side_effect=add), get_runs=Mock(return_value=Events([])),
            events=Mock(return_value=Events([TaskGroupStatusEvent(type="task_group_status", event_id="done", status=terminal)])))
        inputs = [{"input": {}, "metadata": {"handle": "jordan"}, "processor": "core2x"}]
        with patch.object(parallel_client, "Parallel", return_value=SimpleNamespace(task_group=group)):
            parallel_client.ParallelClient("fixture", "https://parallel.test", "").execute(
                inputs, self.params, progress, lambda *_: None)
        document = json.loads(ui.path.read_text())
        self.assertEqual(document["parallel"]["task_group_id"], "paid")
        self.assertEqual(document["status"], "failed")
