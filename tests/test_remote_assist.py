"""Exercise support delivery locally without paid Codex calls."""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.shared.web import account, asks_loop, features, support


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setattr(account, "read", lambda root: {"email": "jordan@powerset.co"})
    guide = tmp_path / support.GUIDE
    guide.parent.mkdir(parents=True)
    guide.write_text((Path(__file__).resolve().parents[1] / support.GUIDE).read_text())
    binary = tmp_path / "bin/codex"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("--output-last-message") + 1]).write_text("Setup repaired and verified.")
print(json.dumps({"args": args, "cwd": os.getcwd(), "home": os.environ["CODEX_HOME"]}))
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    return tmp_path


def request(root, *, sender=None, thread="support-thread", text="Please fix setup", at="2026-10-17T12:00:00Z"):
    message = {"id": str(uuid4()), "kind": "support_request",
               "from": sender or {"operator_id": str(uuid4()), "email": "casey@powerset.co", "name": "Casey Delta"},
               "created_at": at, "payload": {"text": text, "thread_id": thread}}
    write_json(root / ".powerpacks/inbox" / f"{message['id']}.json", message)
    return message


def saved(root, message):
    return json.loads((root / ".powerpacks/inbox" / f"{message['id']}.json").read_text())


def test_codex_flags_home_guide_history_and_result(install):
    root = install
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
                        "--ephemeral", "--sandbox", "workspace-write", "--output-last-message"]
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


@pytest.mark.parametrize("email", ["casey@example.com", "casey@powerset.co.evil", None])
def test_refuses_non_team_sender(install, email, caplog):
    message = request(install, sender={"operator_id": str(uuid4()), "email": email})
    with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_not_called()
    send.assert_not_called()
    assert saved(install, message)["refused"] == "not @powerset.co"
    assert "refused" in caplog.text


def test_switch_off_answers_without_running_codex(install):
    features.save(install, features.FeatureFlags(support=False))
    message = request(install)
    with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_not_called()
    send.assert_called_once()
    assert "off" in send.call_args.args[3]["text"]
    assert send.call_args.args[3]["request_id"] == message["id"]
    assert "answered_at" in saved(install, message)


def test_non_team_local_account_does_not_handle_support(install, monkeypatch):
    message = request(install)
    monkeypatch.setattr(account, "read", lambda root: {"email": "jordan@example.com"})
    with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_not_called()
    send.assert_not_called()
    assert "answered_at" not in saved(install, message)


def test_failed_send_reuses_codex_report(install):
    message = request(install)
    with patch.object(agent_inbox, "send", side_effect=ConnectionError):
        support.answer_pending(repo_root=install, env_file=install / ".env")
    assert "answered_at" not in saved(install, message)
    with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_not_called()
    assert send.call_args.args[3]["text"] == "Setup repaired and verified."
    assert "answered_at" in saved(install, message)


def test_codex_failure_returns_a_report_without_repeating_the_run(install):
    message = request(install)
    with patch.object(support.subprocess, "run", side_effect=OSError) as run, \
            patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_called_once()
    assert "did not finish" in send.call_args.args[3]["text"]
    assert "answered_at" in saved(install, message)


def test_inbox_keeps_both_kinds_acks_strangers_and_delivers_result_over_http(install):
    root = install
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


def test_poll_continues_and_second_request_waits_for_running_codex(install):
    root = install
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
