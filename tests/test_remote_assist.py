"""Exercise support delivery locally without paid Codex calls."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from packs.ingestion.primitives.common import jsonio
from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.shared.web import account, asks_loop, features, support
from packs.shared.web.server import persistent_handler


def request(root, *, sender=None, thread="support-thread", text="Please fix setup", at="2026-10-17T12:00:00Z"):
    message = {"id": str(uuid4()), "kind": "support_request",
               "from": sender or {"operator_id": str(uuid4()), "email": "casey@powerset.co", "name": "Casey Delta"},
               "created_at": at, "payload": {"text": text, "thread_id": thread}}
    write_json(root / ".powerpacks/inbox" / f"{message['id']}.json", message)
    return message


def saved(root, message):
    return json.loads((root / ".powerpacks/inbox" / f"{message['id']}.json").read_text())


def team_http(base, path, body=None, headers=None):
    call = urllib.request.Request(base + path, headers=headers or {},
                                  data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(call, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


class SupportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp_path = self.root = Path(temporary.name).resolve()
        self.enterContext(patch.object(account, "read", return_value={"email": "jordan@powerset.co"}))
        self.app = self.enterContext(patch.object(support, "permission_app", return_value=None))
        guide = tmp_path / support.GUIDE
        guide.parent.mkdir(parents=True)
        guide.write_text((Path(__file__).resolve().parents[1] / support.GUIDE).read_text())
        binary = tmp_path / "bin/codex"
        binary.parent.mkdir()
        binary.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("--output-last-message") + 1]).write_text("Setup repaired and verified.")
print(json.dumps({"binary": sys.argv[0], "args": args, "cwd": os.getcwd(), "home": os.environ["CODEX_HOME"]}))
''')
        binary.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {"PATH": str(binary.parent) + os.pathsep + os.environ["PATH"]}))

    def test_codex_persists_session_and_passes_home_guide_history_and_result(self):
        root = self.root
        first = request(root, text="First question", at="2026-10-17T11:00:00Z")
        with patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=root, env_file=root / ".env")
            current = request(root, sender=first["from"], text="Check again")
            request(root, text="UNRELATED THREAD", thread="other-thread")
            support.answer_pending(repo_root=root, env_file=root / ".env")
        record = json.loads((root / support.REPORTS / f"{current['id']}.log").read_text())
        args = record["args"]
        assert args[:-2] == ["-c", "cli_auth_credentials_store=file", "-c",
                            "sandbox_workspace_write.network_access=true", "exec", "--skip-git-repo-check",
                            "--sandbox", "workspace-write",
                            "--add-dir", str((root / ".powerpacks").resolve()), "--output-last-message"]
        assert record["cwd"] == str(root)
        assert record["home"] == str(root / support.CODEX_HOME)
        assert args[-2] == str(root / support.REPORTS / f"{current['id']}.md")
        assert (root / support.GUIDE).read_text() in args[-1]
        assert "First question" in args[-1] and "Setup repaired and verified." in args[-1]
        assert args[-1].endswith("Request:\nCheck again")
        assert "UNRELATED THREAD" not in args[-1]
        assert "answered_at" in saved(root, current)
        assert any(call.args == (root / ".env", current["from"]["operator_id"], "support_result",
                                 {"text": "Setup repaired and verified.", "thread_id": "support-thread",
                                  "request_id": current["id"]}) for call in send.call_args_list)

    def test_refuses_non_team_sender(self):
        for email in ["casey@example.com", "casey@powerset.co.evil", None]:
            with self.subTest(email=email):
                message = request(self.root, sender={"operator_id": str(uuid4()), "email": email})
                with self.assertLogs(support._LOG, level="WARNING") as logs, \
                        patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
                    support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
                run.assert_not_called()
                send.assert_not_called()
                assert saved(self.root, message)["refused"] == "not @powerset.co"
                assert "refused" in "\n".join(logs.output)

    def test_refuses_sender_without_email(self):
        message = request(self.root, sender={"operator_id": str(uuid4()), "name": "Casey Delta"})
        with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        run.assert_not_called()
        send.assert_not_called()
        assert saved(self.root, message)["refused"] == "not @powerset.co"

    def test_switch_off_answers_without_running_codex(self):
        features.save(self.root, features.FeatureFlags(support=False))
        message = request(self.root)
        with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        run.assert_not_called()
        send.assert_called_once()
        assert "off" in send.call_args.args[3]["text"]
        assert send.call_args.args[3]["request_id"] == message["id"]
        assert "answered_at" in saved(self.root, message)

    def test_non_team_local_account_does_not_handle_support(self):
        message = request(self.root)
        self.enterContext(patch.object(account, "read", return_value={"email": "jordan@example.com"}))
        with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        run.assert_not_called()
        send.assert_not_called()
        assert "answered_at" not in saved(self.root, message)

    def test_failed_send_reuses_codex_report(self):
        message = request(self.root)
        with patch.object(agent_inbox, "send", side_effect=ConnectionError):
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        assert "answered_at" not in saved(self.root, message)
        with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        run.assert_not_called()
        assert send.call_args.args[3]["text"] == "Setup repaired and verified."
        assert "answered_at" in saved(self.root, message)

    def test_update_request_is_handed_over_after_the_report_is_sent(self):
        root = self.root
        request(root)
        staged = root / ".powerpacks/support/update-requested"
        update = root / ".powerpacks/desktop/update-requested"

        def run(message, repo_root):
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text("")
            return "Update requested"

        with patch.object(support, "_run", side_effect=run), patch.object(agent_inbox, "send"):
            support.answer_pending(repo_root=root, env_file=root / ".env")
        self.assertFalse(staged.exists())
        self.assertTrue(update.exists())

    def test_restart_waits_until_result_sent_and_request_answered(self):
        root = self.root
        message = request(root)
        staged = root / ".powerpacks/support/restart-requested"
        restart = root / ".powerpacks/desktop/restart-requested"

        def run(message, repo_root):
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text("")
            self.assertFalse(restart.exists())
            return "Restart requested"

        def sent(*args):
            self.assertTrue(staged.exists())
            self.assertFalse(restart.exists())
            self.assertNotIn("answered_at", saved(root, message))

        with patch.object(support, "_run", side_effect=run), \
                patch.object(agent_inbox, "send", side_effect=ConnectionError):
            support.answer_pending(repo_root=root, env_file=root / ".env")
        self.assertTrue(staged.exists())
        self.assertFalse(restart.exists())
        self.assertNotIn("answered_at", saved(root, message))

        write = support.write_json

        def answered(path, value):
            self.assertTrue(staged.exists())
            self.assertFalse(restart.exists())
            write(path, value)

        with patch.object(support, "_run", return_value="Restart requested"), \
                patch.object(agent_inbox, "send", side_effect=sent) as send, \
                patch.object(support, "write_json", side_effect=answered):
            support.answer_pending(repo_root=root, env_file=root / ".env")
        send.assert_called_once()
        self.assertIn("answered_at", saved(root, message))
        self.assertFalse(staged.exists())
        self.assertTrue(restart.is_file())

    def test_bundled_codex_precedes_path(self):
        app = self.root / "Powerpacks.app"
        bundled = app / "Contents/MacOS/codex"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes((self.root / "bin/codex").read_bytes())
        bundled.chmod(0o755)
        self.app.return_value = str(app)
        message = request(self.root)
        with patch.object(support.sys, "platform", "darwin"), patch.object(agent_inbox, "send"):
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        record = json.loads((self.root / support.REPORTS / f"{message['id']}.log").read_text())
        self.assertEqual(record["binary"], str(bundled))

    def test_missing_bundled_codex_uses_path(self):
        self.app.return_value = str(self.root / "Powerpacks.app")
        message = request(self.root)
        with patch.object(support.sys, "platform", "darwin"), patch.object(agent_inbox, "send"):
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        record = json.loads((self.root / support.REPORTS / f"{message['id']}.log").read_text())
        self.assertEqual(record["binary"], str(self.root / "bin/codex"))

    def test_windows_uses_path_without_mac_app_discovery(self):
        message = request(self.root)
        with patch.object(support.sys, "platform", "win32"), patch.object(agent_inbox, "send"):
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        self.app.assert_not_called()
        record = json.loads((self.root / support.REPORTS / f"{message['id']}.log").read_text())
        self.assertEqual(record["binary"], str(self.root / "bin/codex"))

    def test_support_worker_logs_unexpected_error_and_retries_unanswered_request(self):
        root = self.root
        message = request(root)
        answer = support.answer_pending
        calls = []

        def attempt(**kwargs):
            calls.append(True)
            self.assertNotIn("answered_at", saved(root, message))
            if len(calls) == 1:
                raise OSError("inbox unavailable")
            answer(**kwargs)

        async def exercise():
            with patch.object(support, "answer_pending", side_effect=attempt), \
                    patch.object(agent_inbox, "send") as send, \
                    patch.object(asks_loop.asyncio, "sleep", new=AsyncMock(
                        side_effect=[None, asyncio.CancelledError])):
                with self.assertLogs(asks_loop._LOG, level="WARNING") as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await asks_loop._support(repo_root=root, env_file=root / ".env")
                self.assertIn("OSError", "\n".join(logs.output))
                send.assert_called_once()

        asyncio.run(exercise())
        self.assertEqual(len(calls), 2)
        self.assertIn("answered_at", saved(root, message))

    def test_codex_failure_returns_a_report_without_repeating_the_run(self):
        message = request(self.root)
        with patch.object(support.subprocess, "run", side_effect=OSError) as run, \
                patch.object(agent_inbox, "send") as send:
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
            support.answer_pending(repo_root=self.root, env_file=self.root / ".env")
        run.assert_called_once()
        assert "did not finish" in send.call_args.args[3]["text"]
        assert "answered_at" in saved(self.root, message)

    def test_inbox_keeps_both_kinds_acks_strangers_and_delivers_result_over_http(self):
        root = self.root
        team = request(root)
        stranger = request(root, sender={"operator_id": str(uuid4()), "name": "Jordan Bravo", "email": "jordan@example.com"})
        incoming_result = {**team, "id": str(uuid4()), "kind": "support_result",
                           "payload": {"text": "Earlier report", "thread_id": "other", "request_id": str(uuid4())}}
        messages = [team, stranger, incoming_result]
        acked, sent = [], []

        class Relay(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer synthetic-token"
                self.respond({"messages": messages})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/ack"):
                    acked.append(self.path.split("/")[-2])
                    self.respond({})
                else:
                    sent.append(payload)
                    self.respond({"id": str(uuid4()), "status": "queued"})

        with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as relay:
            thread = threading.Thread(target=relay.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(agent_inbox.auth, "bearer_token", return_value="synthetic-token"), \
                        patch.object(agent_inbox.auth, "api_base", return_value=f"http://127.0.0.1:{relay.server_port}"):
                    assert agent_inbox.pull(repo_root=root, env_file=root / ".env") == messages
                    support.answer_pending(repo_root=root, env_file=root / ".env")
                assert set(acked) == {row["id"] for row in messages}
                assert len(list((root / ".powerpacks/inbox").glob("*.json"))) == 3
                assert sent == [{"to": team["from"]["operator_id"], "kind": "support_result",
                                 "payload": {"text": "Setup repaired and verified.", "thread_id": "support-thread",
                                             "request_id": team["id"]}}]
                assert saved(root, stranger)["refused"] == "not @powerset.co"
            finally:
                relay.shutdown()
                thread.join()

    def test_poll_continues_and_second_request_waits_for_running_codex(self):
        root = self.root
        first = request(root)
        started, release, second_answered = threading.Event(), threading.Event(), threading.Event()
        calls, polls = [], []

        def run(message, repo_root):
            calls.append(message["id"])
            if message["id"] == first["id"]:
                started.set()
                assert release.wait(5)
            return "Checked setup"

        def pulled(**kwargs):
            polls.append(True)
            if started.is_set() and not release.is_set():
                assert calls == [first["id"]]
                second = request(root, at="2026-10-17T12:01:00Z")
                assert "answered_at" not in saved(root, second)
                release.set()

        def sent(*args):
            if args[3]["request_id"] != first["id"]:
                second_answered.set()

        async def exercise():
            with patch.object(support, "_run", side_effect=run), patch.object(agent_inbox, "send", side_effect=sent), \
                    patch.object(agent_inbox, "pull", side_effect=pulled), \
                    patch.object(asks_loop, "HEARTBEAT_SECONDS", 0.02), \
                    patch.object(asks_loop, "SUPPORT_POLL_SECONDS", 0.02):
                tasks = [asyncio.create_task(asks_loop._poll(repo_root=root, env_file=root / ".env")),
                         asyncio.create_task(asks_loop._support(repo_root=root, env_file=root / ".env"))]
                try:
                    assert await asyncio.to_thread(second_answered.wait, 5)
                    assert len(calls) == 2
                    assert len(polls) >= 2
                finally:
                    release.set()
                    for task in tasks:
                        task.cancel()
                    asks_loop.WAKE.set()
                    await asyncio.gather(*tasks, return_exceptions=True)

        asks_loop.WAKE.clear()
        asyncio.run(exercise())


class TeamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name).resolve()
        self.enterContext(patch.object(account, "read", return_value={"email": "jordan@powerset.co"}))
        operator_id = str(uuid4())
        relay_calls = []
        state = {"status": 200, "presence": {"agents": [
            {"operator_id": str(uuid4()), "name": "Jordan Bravo", "email": "JORDAN@powerset.co",
             "last_seen_at": "2026-10-17T12:00:00Z"},
            {"operator_id": operator_id, "name": "Casey Delta", "email": "casey@powerset.co",
             "last_seen_at": "2026-10-17T11:55:00+00:00"},
            {"operator_id": str(uuid4()), "name": "Alex Example", "email": "alex@powerset.co"}]}}

        class Relay(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, payload):
                body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(state["status"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer synthetic-token"
                relay_calls.append(("GET", self.path))
                assert self.path == "/v2/agents/presence"
                self.respond(state["presence"] if state["status"] == 200 else state.get("error", {"detail": "Team accounts only"}))

            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer synthetic-token"
                assert self.path == "/v2/agent-messages"
                relay_calls.append(("POST", json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.respond({"id": str(uuid4()), "status": "sent"} if state["status"] == 200
                             else state.get("error", {"detail": "Team accounts only"}))

        relay = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        page = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(tmp_path))
        for server in (relay, page):
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(worker.join)
            self.addCleanup(server.shutdown)
        self.enterContext(patch.object(agent_inbox.auth, "bearer_token", return_value="synthetic-token"))
        self.enterContext(patch.object(agent_inbox.auth, "api_base", return_value=f"http://127.0.0.1:{relay.server_port}"))
        self.team_page = tmp_path, f"http://127.0.0.1:{page.server_port}", operator_id, state, relay_calls

    def test_team_presence_excludes_me_preserves_times_and_passes_relay_403(self):
        root, base, operator_id, state, calls = self.team_page
        status, reply = team_http(base, "/api/team")
        assert status == 200
        assert reply == {"me": {"email": "jordan@powerset.co"},
                         "members": [{**row, "last_seen_at": row.get("last_seen_at", "")}
                                     for row in state["presence"]["agents"][1:]]}
        assert calls == [("GET", "/v2/agents/presence")]
        state["status"] = 403
        assert team_http(base, "/api/team") == (403, {"detail": "Team accounts only"})

    def test_team_ask_persists_requests_and_returns_thread_with_only_teammates_results(self):
        root, base, operator_id, state, calls = self.team_page
        path = f"/api/team/thread?operator_id={operator_id}"
        assert team_http(base, path) == (200, {"messages": []})
        features.save(root, features.FeatureFlags(sets=False, support=False))
        with patch.object(jsonio, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-17T12:00:00+00:00")
            status, reply = team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"},
                                      headers={"Origin": base})
        assert status == 200
        first = reply["messages"][0]
        assert first == {"id": first["id"], "direction": "out", "text": "Fix setup", "at": "2026-10-17T12:00:00Z"}
        sent = json.loads((root / ".powerpacks/support/sent" / f"{first['id']}.json").read_text())
        assert sent == {"id": first["id"], "thread_id": operator_id, "text": "Fix setup",
                        "created_at": "2026-10-17T12:00:00Z"}
        incoming = {"id": str(uuid4()), "kind": "support_result", "from": {"operator_id": operator_id},
                    "created_at": "2026-10-17T14:01:00+02:00",
                    "payload": {"text": "Setup fixed", "thread_id": operator_id, "request_id": first["id"]}}
        write_json(root / ".powerpacks/inbox" / f"{incoming['id']}.json", incoming)
        for row in [{**incoming, "id": str(uuid4()), "from": {"operator_id": str(uuid4())}},
                    {**incoming, "id": str(uuid4()), "kind": "debug_result"}]:
            write_json(root / ".powerpacks/inbox" / f"{row['id']}.json", row)
        other_id = str(uuid4())
        team_http(base, "/api/team/ask", {"operator_id": other_id, "text": "Unrelated"})
        with patch.object(jsonio, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-17T12:02:00+00:00")
            status, reply = team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": "Thanks"})
        assert status == 200
        assert [(row["direction"], row["text"]) for row in reply["messages"]] == [
            ("out", "Fix setup"), ("in", "Setup fixed"), ("out", "Thanks")]
        assert team_http(base, path) == (status, reply)
        assert calls == [("POST", {"to": target, "kind": "support_request",
                                   "payload": {"thread_id": target, "text": text}})
                         for target, text in [(operator_id, "Fix setup"), (other_id, "Unrelated"), (operator_id, "Thanks")]]

    def test_team_routes_refuse_non_team_local_account(self):
        for email in [None, "jordan@example.com", "jordan@powerset.co.evil"]:
            with self.subTest(email=email):
                root, base, operator_id, state, calls = self.team_page
                self.enterContext(patch.object(account, "read", return_value={"email": email}))
                for path, body in [("/api/team", None), (f"/api/team/thread?operator_id={operator_id}", None),
                                   ("/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"})]:
                    assert team_http(base, path, body)[0] == 403
                assert calls == []

    def test_team_routes_refuse_cross_origin_requests(self):
        root, base, operator_id, state, calls = self.team_page
        for origin, extra in [("https://evil.example", {}), (base.replace("http:", "https:"), {}),
                              ("http://evil.example", {"Host": "evil.example"})]:
            for path, body in [("/api/team", None), (f"/api/team/thread?operator_id={operator_id}", None),
                               ("/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"})]:
                assert team_http(base, path, body, headers={"Origin": origin, **extra})[0] == 403
        assert calls == []

    def test_team_ask_rejects_empty_text_without_sending(self):
        for text in ["", "  \n", None]:
            with self.subTest(text=text):
                root, base, operator_id, state, calls = self.team_page
                assert team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": text})[0] == 400
                assert calls == []
                assert not (root / ".powerpacks/support/sent").exists()

    def test_team_ask_relay_failure_does_not_store_a_sent_request(self):
        root, base, operator_id, state, calls = self.team_page
        state["status"] = 403
        assert team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"}) == (
            403, {"detail": "Team accounts only"})
        assert not (root / ".powerpacks/support/sent").exists()

    def test_team_routes_return_non_json_relay_error_text_and_status(self):
        root, base, operator_id, state, calls = self.team_page
        state.update(status=502, error="<html>Bad Gateway</html>")
        for path, body in [("/api/team", None),
                           ("/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"})]:
            with self.subTest(path=path):
                self.assertEqual(team_http(base, path, body), (502, {"error": state["error"]}))
        self.assertFalse((root / ".powerpacks/support/sent").exists())
