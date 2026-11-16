"""Check authenticated HTTP inbox polling and optional NATS presence/nudges locally."""
from __future__ import annotations

import asyncio
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import nats
from nats.js.errors import NotFoundError

from packs.shared.web import asks_loop, server

REAL_PULL = asks_loop.agent_inbox.pull


class AskLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_device_created_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = asks_loop._device_id(root)
            path = root / ".powerpacks/device-id"
            modified = path.stat().st_mtime_ns
            self.assertEqual(UUID(first).version, 4)
            self.assertEqual(asks_loop._device_id(root), first)
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_connection_asks_for_the_default_set(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env_file = Path(temporary.name) / ".env"
        env_file.write_text("POWERPACKS_DEFAULT_SET_ID=c41bf0d2-d68e-4afb-a0cd-58d6ef14cfa0\n", encoding="utf-8")
        seen = {}

        def opened(request, timeout):
            seen["url"] = request.full_url
            return io.BytesIO(b'{"url": "nats://localhost:4222"}')

        with patch.object(asks_loop.auth, "bearer_token", return_value="t"), \
                patch.object(asks_loop.auth, "api_base", return_value="https://api.example"), \
                patch.object(asks_loop.urllib.request, "urlopen", side_effect=opened):
            asks_loop._connection(env_file)
        self.assertEqual(seen["url"], "https://api.example/v2/nats/connection?set_id=c41bf0d2-d68e-4afb-a0cd-58d6ef14cfa0")

    def test_connection_uses_upload_auth_helpers(self):
        env_file = Path("/synthetic/.env")
        with patch.object(asks_loop.auth, "bearer_token", return_value="synthetic-token") as token, \
                patch.object(asks_loop.auth, "api_base", return_value="http://localhost:8000") as base, \
                patch.object(asks_loop.urllib.request, "urlopen") as get:
            get.return_value.__enter__.return_value = io.BytesIO(b'{"url":"nats://localhost:4222"}')
            self.assertEqual(asks_loop._connection(env_file), {"url": "nats://localhost:4222"})
            request = get.call_args.args[0]
            self.assertEqual(request.full_url, "http://localhost:8000/v2/nats/connection")
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
            token.assert_called_once_with(env_file)
            base.assert_called_once_with(env_file)
            get.side_effect = urllib.error.HTTPError(request.full_url, 401, "signed out", {}, None)
            with self.assertRaises(SystemExit):
                asks_loop._connection(env_file)


    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env_file = self.root / ".env"
        self.pull = self.enterContext(patch.object(asks_loop.agent_inbox, "pull", return_value=[]))
        self.enterContext(patch.dict(asks_loop.STATUS, {"state": "offline"}))
        asks_loop.WAKE.clear()
        self.addCleanup(asks_loop.WAKE.set)

    async def poll(self, cycles=1):
        with patch.object(asks_loop.WAKE, "wait", side_effect=[False] * (cycles - 1) + [asyncio.CancelledError]) as wait:
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._poll(repo_root=self.root, env_file=self.env_file)
        return wait

    async def test_success_polls_immediately_then_every_thirty_seconds(self):
        wait = await self.poll(2)
        self.assertEqual(self.pull.call_count, 2)
        self.pull.assert_called_with(repo_root=self.root, env_file=self.env_file)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [30, 30])
        self.assertEqual(asks_loop.STATUS["state"], "connected")

    async def test_failed_pull_marks_offline_and_backs_off_then_resets_after_success(self):
        self.pull.side_effect = [ConnectionError(), ConnectionError(), [], ConnectionError()]
        states = []

        def wait(delay):
            states.append((asks_loop.STATUS["state"], delay))
            if len(states) == 4:
                raise asyncio.CancelledError

        with patch.object(asks_loop.WAKE, "wait", side_effect=wait), self.assertLogs(asks_loop.__name__, level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._poll(repo_root=self.root, env_file=self.env_file)
        self.assertEqual(states, [("offline", 1), ("offline", 2), ("connected", 30), ("offline", 1)])

    async def test_http_errors_other_than_unauthorized_are_offline(self):
        self.pull.side_effect = urllib.error.HTTPError("http://localhost/inbox", 503, "unavailable", {}, None)
        with self.assertLogs(asks_loop.__name__, level="WARNING"):
            wait = await self.poll()
        self.assertEqual(asks_loop.STATUS["state"], "offline")
        wait.assert_called_once_with(1)

    async def test_signed_out_waits_five_minutes_for_credentials_or_http_401(self):
        for error in (SystemExit("signed out"), urllib.error.HTTPError("http://localhost/inbox", 401, "signed out", {}, None)):
            with self.subTest(error=type(error).__name__):
                self.pull.side_effect = error
                wait = await self.poll()
                wait.assert_called_once_with(300)
                self.assertEqual(asks_loop.STATUS["state"], "signed_out")

    async def test_sign_in_wakes_signed_out_poll(self):
        self.pull.side_effect = [SystemExit("signed out"), []]
        waiting = threading.Event()
        resumed = threading.Event()
        original_wait = asks_loop.WAKE.wait

        def wait(delay):
            if delay == 300:
                waiting.set()
                return original_wait(delay)
            resumed.set()
            raise asyncio.CancelledError

        with patch.object(asks_loop.WAKE, "wait", side_effect=wait):
            loop = asyncio.create_task(asks_loop._poll(repo_root=self.root, env_file=self.env_file))
            try:
                self.assertTrue(await asyncio.to_thread(waiting.wait, 1))
                self.assertEqual(asks_loop.STATUS["state"], "signed_out")
                asks_loop.WAKE.set()
                self.assertTrue(await asyncio.to_thread(resumed.wait, 1))
                await asyncio.gather(loop, return_exceptions=True)
                self.assertEqual(asks_loop.STATUS["state"], "connected")
            finally:
                loop.cancel()
                asks_loop.WAKE.set()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_real_http_poll_persists_and_acks_messages_despite_nats_422(self):
        sender = str(uuid4())
        created_at = "2026-10-12T12:00:00Z"
        messages = [
            {"id": str(uuid4()), "kind": "support_result", "from": {"operator_id": sender, "name": "Casey Delta"},
             "created_at": created_at, "payload": {"text": text, "thread_id": "synthetic-thread", "request_id": str(uuid4())}}
            for text in ("First synthetic result", "Second synthetic result")
        ]
        acked = set()
        delivered = threading.Event()
        failed_ack = False
        offline = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body):
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                if self.path != "/v2/agent-messages" or self.headers.get("Authorization") != "Bearer synthetic-token":
                    self.respond(401, {})
                    return
                self.respond(200, {"messages": [message for message in messages if message["id"] not in acked]})

            def do_POST(self):
                nonlocal failed_ack
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if not failed_ack:
                    failed_ack = True
                    self.respond(503, {})
                    return
                acked.add(self.path.split("/")[-2])
                self.respond(200, {"status": "acked"})
                if len(acked) == len(messages):
                    delivered.set()

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as api:
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{api.server_port}"
            nats_error = urllib.error.HTTPError(base + "/v2/nats/connection", 422, "Specify set_id", {}, None)
            states = []
            original_wait = asks_loop.WAKE.wait

            def wait(delay):
                states.append(asks_loop.STATUS["state"])
                if asks_loop.STATUS["state"] == "offline":
                    offline.set()
                return original_wait(0.02)

            with patch.object(asks_loop.agent_inbox, "pull", REAL_PULL), \
                    patch.object(asks_loop.auth, "bearer_token", return_value="synthetic-token"), \
                    patch.object(asks_loop.auth, "api_base", return_value=base), \
                    patch.object(asks_loop, "_connection", side_effect=nats_error), \
                    patch.object(asks_loop.WAKE, "wait", side_effect=wait), \
                    self.assertLogs(asks_loop.__name__, level="WARNING"):
                loop = asyncio.create_task(asks_loop._run(repo_root=self.root, env_file=self.env_file,
                                                         device_id=str(uuid4())))
                try:
                    self.assertTrue(await asyncio.to_thread(offline.wait, 2))
                    self.assertTrue(await asyncio.to_thread(delivered.wait, 2))
                    async with asyncio.timeout(2):
                        while asks_loop.STATUS["state"] != "connected":
                            await asyncio.sleep(0.01)
                    self.assertIn("offline", states)
                    for message in messages:
                        path = self.root / ".powerpacks" / "inbox" / f"{message['id']}.json"
                        self.assertEqual(json.loads(path.read_text()), message)
                finally:
                    loop.cancel()
                    asks_loop.WAKE.set()
                    await asyncio.gather(loop, return_exceptions=True)
                    api.shutdown()
                    thread.join()


class AskLoopServerTests(unittest.TestCase):
    def test_server_starts_one_inbox_daemon_with_only_team_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            started = threading.Event()
            daemon = []

            def run(**kwargs):
                daemon.append(threading.current_thread().daemon)
                started.set()

            with ThreadingHTTPServer(("127.0.0.1", 0), server.persistent_handler(root)) as page, \
                    patch.object(Path, "cwd", return_value=root), \
                    patch.object(server, "owned_listener", return_value=None), \
                    patch.object(server, "ThreadingHTTPServer", return_value=page), \
                    patch.object(server, "_announce"), \
                    patch.object(server, "mounted_handler") as mount, \
                    patch.object(server.account, "read", return_value={"email": "jordan@powerset.co"}), \
                    patch("packs.shared.web.team.members", return_value={"members": []}), \
                    patch.object(asks_loop, "run", side_effect=run) as loop:
                thread = threading.Thread(target=server.main, args=(["serve", "--port", "0"],), daemon=True)
                thread.start()
                try:
                    self.assertTrue(started.wait(2))
                    for path in ("/team", "/api/team", "/api/account", "/api/features"):
                        client = http.client.HTTPConnection(*page.server_address, timeout=5)
                        client.request("GET", path)
                        response = client.getresponse()
                        response.read()
                        client.close()
                        self.assertEqual(response.status, 200, path)
                    loop.assert_called_once_with(repo_root=root, env_file=root / ".env")
                    self.assertEqual(daemon, [True])
                    # Only the start-up warm-up mounts (in the background); a Team request never does.
                    self.assertLessEqual(mount.call_count, 1)
                    self.assertFalse((root / server.STORE).exists())
                finally:
                    page.shutdown()
                    thread.join()

    def test_relay_routes_open_a_fresh_store(self):
        for method, path in (("GET", "/api/relay"), ("POST", "/api/relay/connect")):
            with self.subTest(method=method, path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("packs.ingestion.primitives.deep_context_v2.openai.load_env"), \
                        patch("packs.ingestion.primitives.share.web.logbook.default_paths",
                              return_value={"gmail": root / "synthetic-gmail.sqlite"}), \
                        ThreadingHTTPServer(("127.0.0.1", 0), server.persistent_handler(root)) as page:
                    thread = threading.Thread(target=page.serve_forever, daemon=True)
                    thread.start()
                    try:
                        self.assertFalse((root / server.STORE).exists())
                        client = http.client.HTTPConnection(*page.server_address, timeout=5)
                        client.request(method, path)
                        response = client.getresponse()
                        payload = json.loads(response.read())
                        client.close()
                        self.assertEqual(response.status, 200, payload)
                        self.assertTrue((root / server.STORE).is_file())
                        client = http.client.HTTPConnection(*page.server_address, timeout=5)
                        client.request("GET", "/api/people/sets")
                        self.assertEqual(client.getresponse().status, 404)
                        client.close()
                    finally:
                        page.shutdown()
                        thread.join()


class AskLoopNatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device_id = asks_loop._device_id(self.root)
        self.env_file = self.root / ".env"
        self.operator_id = str(uuid4())
        self.subjects = {"tasks": f"set.synthetic.op.{self.operator_id}",
                         "presence": "set.synthetic.presence", "inbox": f"op.{self.operator_id}"}
        self.connection = {"url": "nats://localhost:4222", "token": "", "subjects": self.subjects}
        asks_loop.WAKE.clear()
        self.addCleanup(asks_loop.WAKE.set)
        self.enterContext(patch.dict(asks_loop.STATUS, {"state": "connected"}))

    async def test_local_nats_presence_and_nudge_wakes_canonical_http_poll(self):
        async def quiet_error(exc):
            pass

        try:
            nc = await nats.connect("nats://127.0.0.1:4222", allow_reconnect=False,
                                    connect_timeout=1, error_cb=quiet_error)
        except (OSError, nats.errors.Error) as exc:
            self.skipTest(f"Local NATS unavailable: {type(exc).__name__}")
        self.addAsyncCleanup(nc.close)
        self.connection["url"] = "nats://127.0.0.1:4222"
        js = nc.jetstream()
        try:
            await js.stream_info("asks")
        except NotFoundError:
            await js.add_stream(name="asks", subjects=["set.>", "op.>"])
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
            self.skipTest("Local NATS has no JetStream (start it with -js)")
        presence = await nc.subscribe(self.subjects["presence"])
        await nc.flush()
        first_pull, next_pull = threading.Event(), threading.Event()

        def pulled(**kwargs):
            if first_pull.is_set():
                next_pull.set()
            first_pull.set()

        with patch.object(asks_loop, "_connection", return_value=self.connection), \
                patch.object(asks_loop.agent_inbox, "pull", side_effect=pulled) as pull:
            loop = asyncio.create_task(asks_loop._run(repo_root=self.root, env_file=self.env_file,
                                                     device_id=self.device_id))
            try:
                self.assertTrue(await asyncio.to_thread(first_pull.wait, 2))
                beat = json.loads((await presence.next_msg(timeout=2)).data)
                self.assertEqual(beat["operator_id"], self.operator_id)
                self.assertEqual(beat["device_id"], self.device_id)
                self.assertIsNotNone(datetime.fromisoformat(beat["at"]).tzinfo)
                consumer = await js.consumer_info("asks", self.device_id + "-inbox")
                self.assertEqual(consumer.config.filter_subject, self.subjects["inbox"])
                await js.publish(self.subjects["inbox"], b'{"kind":"message"}')
                self.assertTrue(await asyncio.to_thread(next_pull.wait, 2))
                pull.assert_called_with(repo_root=self.root, env_file=self.env_file)
                self.assertEqual(asks_loop.STATUS["state"], "connected")
            finally:
                loop.cancel()
                asks_loop.WAKE.set()
                await asyncio.gather(loop, return_exceptions=True)
                try:
                    await js.delete_consumer("asks", self.device_id + "-inbox")
                except NotFoundError:
                    pass
            self.assertEqual(asks_loop.STATUS["state"], "connected")

    async def test_presence_failures_back_off_without_changing_http_status(self):
        with patch.object(asks_loop, "_connection", side_effect=ConnectionError), \
                patch.object(asks_loop.asyncio, "sleep", side_effect=[None, None, asyncio.CancelledError]) as sleep, \
                self.assertLogs(asks_loop.__name__, level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._presence(repo_root=self.root, env_file=self.env_file, device_id=self.device_id)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2, 4])
        self.assertEqual(asks_loop.STATUS["state"], "connected")

    async def test_presence_signed_out_waits_without_consuming_poll_wake(self):
        with patch.object(asks_loop, "_connection", side_effect=SystemExit("signed out")), \
                patch.object(asks_loop.asyncio, "sleep", side_effect=asyncio.CancelledError) as sleep, \
                patch.object(asks_loop.WAKE, "wait") as wait:
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._presence(repo_root=self.root, env_file=self.env_file, device_id=self.device_id)
        sleep.assert_awaited_once_with(300)
        wait.assert_not_called()
        self.assertEqual(asks_loop.STATUS["state"], "connected")

    async def test_presence_refresh_gets_new_credentials_without_warning(self):
        with patch.object(asks_loop, "_connection", return_value=self.connection) as connection, \
                patch.object(asks_loop, "_connected", side_effect=[None, asyncio.CancelledError]), \
                self.assertNoLogs(asks_loop.__name__, level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._presence(repo_root=self.root, env_file=self.env_file, device_id=self.device_id)
        self.assertEqual(connection.call_count, 2)


if __name__ == "__main__":
    unittest.main()
