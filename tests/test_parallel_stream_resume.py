"""Disconnected provider reads never submit another paid task group."""
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from parallel.types import TaskGroupStatus, TaskGroupStatusEvent, TaskRunEvent, TaskRunJsonOutput
from packs.ingestion.primitives.deep_context.enrich.parallel_research import parallel_client


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
                httpx.RemoteProtocolError('SSE closed'),
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
        from packs.ingestion.primitives.common.jsonio import write_json
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
        from packs.ingestion.primitives.common.jsonio import write_json
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
