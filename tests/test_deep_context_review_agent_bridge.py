"""Tests for the optional same-thread Codex wake bridge."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import review_agent_bridge as bridge


def _status(action: str, *, updated_at: str = "t1") -> dict:
    return {
        "next_action": action,
        "selection": {"review_revision": "rev-1", "sha256": "sha-1"},
        "progress": {"worth_pending": 0, "linkedin_pending": 3},
        "enrichment": {"status": "needs_approval", "updated_at": updated_at},
    }


class TestBridgeNotifications(unittest.TestCase):
    def test_notify_bridge_is_best_effort_when_absent(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "bridge.sock"
            self.assertFalse(bridge.notify_bridge(socket_path=path))

    def test_notify_bridge_sends_only_event_name(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "bridge.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                server.bind(str(path))
                server.settimeout(1)
                self.assertTrue(bridge.notify_bridge("state_changed", socket_path=path))
                payload = json.loads(server.recv(1024))
                self.assertEqual(payload, {"event": "state_changed"})
            finally:
                server.close()


class TestRolloutHandoff(unittest.TestCase):
    def test_rollout_is_idle_only_after_latest_overlapping_turn_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "old-orphan"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-a"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-b"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "turn-a"},
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(bridge.rollout_turn_is_idle(path))
            self.assertTrue(bridge.rollout_turn_completed(path, "turn-a"))
            self.assertFalse(bridge.rollout_turn_completed(path, "turn-b"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-b"},
                }) + "\n")
            self.assertTrue(bridge.rollout_turn_is_idle(path))
            self.assertEqual(bridge.rollout_turn_lifecycles(path), {
                "old-orphan": "task_started",
                "turn-a": "task_complete",
                "turn-b": "task_complete",
            })

    def test_rollout_is_idle_when_latest_turn_was_aborted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-a"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "turn_id": "turn-a"},
                }),
            ]) + "\n", encoding="utf-8")
            self.assertTrue(bridge.rollout_turn_is_idle(path))

    def test_find_rollout_uses_thread_id_filename_and_newest_match(self):
        thread_id = "019f6cf8-51e7-7ed2-88f4-c2471041e1f4"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / f"old-{thread_id}.jsonl"
            newer = root / "nested" / f"new-{thread_id}.jsonl"
            newer.parent.mkdir()
            older.write_text("{}\n", encoding="utf-8")
            newer.write_text("{}\n", encoding="utf-8")
            older.touch()
            newer.touch()
            newer_mtime = older.stat().st_mtime_ns + 1_000_000
            newer.touch()
            import os
            os.utime(newer, ns=(newer_mtime, newer_mtime))
            self.assertEqual(
                bridge.find_rollout_path(thread_id, sessions_root=root), newer)


class TestBridgeController(unittest.TestCase):
    THREAD_ID = "019f6cf8-51e7-7ed2-88f4-c2471041e1f4"

    def test_human_wait_does_not_wake(self):
        wakes: list[tuple[str, str]] = []
        controller = bridge.BridgeController(
            self.THREAD_ID,
            status_reader=lambda: _status("review_people"),
            waker=lambda thread_id, prompt: wakes.append((thread_id, prompt)) or {},
        )
        result = controller.handle("state_changed")
        self.assertFalse(result["woke"])
        self.assertEqual(wakes, [])

    def test_actionable_state_wakes_once_per_state_token(self):
        status = _status("run_approved_enrichment")
        wakes: list[tuple[str, str]] = []
        controller = bridge.BridgeController(
            self.THREAD_ID,
            status_reader=lambda: status,
            waker=lambda thread_id, prompt: wakes.append((thread_id, prompt)) or {},
        )
        self.assertTrue(controller.handle("state_changed")["woke"])
        self.assertFalse(controller.handle("state_changed")["woke"])
        status["enrichment"]["updated_at"] = "t2"
        self.assertTrue(controller.handle("state_changed")["woke"])
        self.assertEqual(len(wakes), 2)
        self.assertTrue(all(thread_id == self.THREAD_ID for thread_id, _ in wakes))

    def test_failed_wake_keeps_token_so_a_retry_reattempts(self):
        # A wake that raises (Codex thread mid-turn, transient app-server
        # failure) must NOT record the state token — the serve loop's bounded
        # retry (or the next datagram) gets a genuine second attempt instead of
        # being deduped into silence.
        status = _status("run_approved_enrichment")
        attempts: list[int] = []

        def flaky_waker(thread_id: str, prompt: str) -> dict:
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("Codex thread did not reach task_complete")
            return {}

        controller = bridge.BridgeController(
            self.THREAD_ID, status_reader=lambda: status, waker=flaky_waker)
        with self.assertRaises(TimeoutError):
            controller.handle("state_changed")
        self.assertEqual(controller.last_token, "")   # not recorded on failure
        self.assertTrue(controller.handle("state_changed")["woke"])  # retry lands
        self.assertEqual(len(attempts), 2)

    def test_dispatch_is_reported_before_blocking_waker_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        dispatches: list[dict] = []
        results: list[dict] = []

        def blocking_waker(*_: object) -> dict:
            entered.set()
            self.assertTrue(release.wait(2))
            return {}

        controller = bridge.BridgeController(
            self.THREAD_ID,
            status_reader=lambda: _status("run_approved_enrichment"),
            waker=blocking_waker,
        )
        worker = threading.Thread(
            target=lambda: results.append(controller.handle(
                "state_changed", on_dispatch=dispatches.append)))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(dispatches, [{
            "event": "state_changed",
            "action": "run_approved_enrichment",
            "state": "dispatching",
        }])
        self.assertEqual(results, [])
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["woke"])

    def test_terminal_state_wakes_then_stops(self):
        controller = bridge.BridgeController(
            self.THREAD_ID,
            status_reader=lambda: _status("realize"),
            waker=lambda *_: {},
        )
        result = controller.handle("state_changed")
        self.assertTrue(result["woke"])
        self.assertTrue(result["stop"])

    def test_smoke_bypasses_workflow_filter_but_is_read_only(self):
        prompts: list[str] = []
        controller = bridge.BridgeController(
            self.THREAD_ID,
            status_reader=lambda: self.fail("smoke should not read workflow status"),
            waker=lambda _thread_id, prompt: prompts.append(prompt) or {},
        )
        self.assertTrue(controller.handle("smoke")["woke"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("Do not change files", prompts[0])


class TestAppServerSession(unittest.TestCase):
    THREAD_ID = "019f6cf8-51e7-7ed2-88f4-c2471041e1f4"

    def test_rollout_completion_releases_turn_when_notification_is_missing(self):
        session = bridge.AppServerSession(timeout_seconds=2)
        sent: list[dict] = []
        resume_calls: list[dict] = []

        def request(method: str, params: dict) -> dict:
            self.assertEqual(method, "thread/resume")
            resume_calls.append(params)
            if len(resume_calls) == 1:
                return {
                    "result": {
                        "thread": {"id": self.THREAD_ID},
                        "approvalPolicy": "on-request",
                        "sandbox": {"type": "workspaceWrite"},
                    },
                }
            return {"result": {"thread": {"id": self.THREAD_ID}}}

        session.request = request  # type: ignore[method-assign]
        session.send = sent.append  # type: ignore[method-assign]
        session._read_once = mock.Mock(  # type: ignore[method-assign]
            return_value={"id": 1, "result": {"turn": {"id": "turn-a"}}})
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rollout.write_text("\n".join([
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-a"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-b"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-a"},
                }),
            ]) + "\n", encoding="utf-8")
            with mock.patch.object(
                    bridge, "find_rollout_path", return_value=rollout):
                result = session.resume_and_turn(self.THREAD_ID, "read-only test")

        self.assertEqual(result["completed_via"], "rollout")
        self.assertEqual(result["completed"]["turn"]["id"], "turn-a")
        self.assertTrue(result["policy_restored"])
        self.assertEqual(sent[0]["params"]["approvalPolicy"], "never")
        self.assertEqual(
            sent[0]["params"]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertEqual(resume_calls[-1]["approvalPolicy"], "on-request")
        self.assertEqual(resume_calls[-1]["sandbox"], "workspace-write")

    def test_wrong_turn_completion_notification_is_ignored(self):
        session = bridge.AppServerSession(timeout_seconds=2)
        sent: list[dict] = []
        resume_calls: list[dict] = []

        def request(method: str, params: dict) -> dict:
            self.assertEqual(method, "thread/resume")
            resume_calls.append(params)
            if len(resume_calls) == 1:
                return {
                    "result": {
                        "thread": {"id": self.THREAD_ID},
                        "approvalPolicy": "on-request",
                        "sandbox": {"type": "workspaceWrite"},
                    },
                }
            return {"result": {"thread": {"id": self.THREAD_ID}}}

        session.request = request  # type: ignore[method-assign]
        session.send = sent.append  # type: ignore[method-assign]
        session._read_once = mock.Mock(side_effect=[  # type: ignore[method-assign]
            {"id": 1, "result": {"turn": {"id": "turn-a"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-b", "status": "completed"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-a", "status": "completed"}},
            },
        ])
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rollout.write_text("\n".join([
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-b"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-b"},
                }),
            ]) + "\n", encoding="utf-8")
            with mock.patch.object(
                    bridge, "find_rollout_path", return_value=rollout):
                result = session.resume_and_turn(self.THREAD_ID, "read-only test")

        self.assertEqual(result["completed_via"], "notification")
        self.assertEqual(result["completed"]["turn"]["id"], "turn-a")
        self.assertEqual(session._read_once.call_count, 3)


class TestReviewStatusRunner(unittest.TestCase):
    def test_review_status_does_not_need_uv_cache_access(self):
        env = os.environ.copy()
        env["UV_CACHE_DIR"] = "/dev/null/powerpacks-uv-cache"
        result = subprocess.run(
            [str(bridge.REPO_ROOT / "bin" / "deep-context"), "review-status"],
            cwd=bridge.REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["primitive"],
                         "deep_context_review_status")


if __name__ == "__main__":
    unittest.main()
