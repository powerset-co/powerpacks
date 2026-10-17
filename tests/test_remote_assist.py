"""Exercise support delivery locally without paid Codex calls."""
import asyncio
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from packs.ingestion.primitives.common import jsonio
from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.shared.web import account, asks_loop, features, support
from packs.shared.web.server import persistent_handler


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


def test_refuses_sender_without_email(install):
    message = request(install, sender={"operator_id": str(uuid4()), "name": "Casey Delta"})
    with patch.object(support.subprocess, "run") as run, patch.object(agent_inbox, "send") as send:
        support.answer_pending(repo_root=install, env_file=install / ".env")
    run.assert_not_called()
    send.assert_not_called()
    assert saved(install, message)["refused"] == "not @powerset.co"


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


@pytest.fixture
def team_page(tmp_path, monkeypatch):
    monkeypatch.setattr(account, "read", lambda root: {"email": "jordan@powerset.co"})
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
            body = json.dumps(payload).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer synthetic-token"
            relay_calls.append(("GET", self.path))
            assert self.path == "/v2/agents/presence"
            self.respond(state["presence"] if state["status"] == 200 else {"detail": "Team accounts only"})

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer synthetic-token"
            assert self.path == "/v2/agent-messages"
            relay_calls.append(("POST", json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.respond({"id": str(uuid4()), "status": "sent"} if state["status"] == 200
                         else {"detail": "Team accounts only"})

    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as relay, \
            ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(tmp_path)) as page:
        workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (relay, page)]
        for worker in workers:
            worker.start()
        monkeypatch.setattr(agent_inbox.auth, "bearer_token", lambda env: "synthetic-token")
        monkeypatch.setattr(agent_inbox.auth, "api_base", lambda env: f"http://127.0.0.1:{relay.server_port}")
        try:
            yield tmp_path, f"http://127.0.0.1:{page.server_port}", operator_id, state, relay_calls
        finally:
            page.shutdown()
            relay.shutdown()
            for worker in workers:
                worker.join()


def team_http(base, path, body=None, headers=None):
    call = urllib.request.Request(base + path, headers=headers or {},
                                  data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(call, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_team_presence_excludes_me_preserves_times_and_passes_relay_403(team_page):
    root, base, operator_id, state, calls = team_page
    status, reply = team_http(base, "/api/team")
    assert status == 200
    assert reply == {"me": {"email": "jordan@powerset.co"},
                     "members": [{**row, "last_seen_at": row.get("last_seen_at", "")}
                                 for row in state["presence"]["agents"][1:]]}
    assert calls == [("GET", "/v2/agents/presence")]
    state["status"] = 403
    assert team_http(base, "/api/team") == (403, {"detail": "Team accounts only"})


def test_team_ask_persists_requests_and_returns_thread_with_only_teammates_results(team_page):
    root, base, operator_id, state, calls = team_page
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


@pytest.mark.parametrize("email", [None, "jordan@example.com", "jordan@powerset.co.evil"])
def test_team_routes_refuse_non_team_local_account(team_page, monkeypatch, email):
    root, base, operator_id, state, calls = team_page
    monkeypatch.setattr(account, "read", lambda root: {"email": email})
    for path, body in [("/api/team", None), (f"/api/team/thread?operator_id={operator_id}", None),
                       ("/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"})]:
        assert team_http(base, path, body)[0] == 403
    assert calls == []


def test_team_routes_refuse_cross_origin_requests(team_page):
    root, base, operator_id, state, calls = team_page
    for origin, extra in [("https://evil.example", {}), (base.replace("http:", "https:"), {}),
                          ("http://evil.example", {"Host": "evil.example"})]:
        for path, body in [("/api/team", None), (f"/api/team/thread?operator_id={operator_id}", None),
                           ("/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"})]:
            assert team_http(base, path, body, headers={"Origin": origin, **extra})[0] == 403
    assert calls == []


@pytest.mark.parametrize("text", ["", "  \n", None])
def test_team_ask_rejects_empty_text_without_sending(team_page, text):
    root, base, operator_id, state, calls = team_page
    assert team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": text})[0] == 400
    assert calls == []
    assert not (root / ".powerpacks/support/sent").exists()


def test_team_ask_relay_failure_does_not_store_a_sent_request(team_page):
    root, base, operator_id, state, calls = team_page
    state["status"] = 403
    assert team_http(base, "/api/team/ask", {"operator_id": operator_id, "text": "Fix setup"}) == (
        403, {"detail": "Team accounts only"})
    assert not (root / ".powerpacks/support/sent").exists()
