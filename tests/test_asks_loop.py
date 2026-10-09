"""Check Ask the Set delivery using synthetic files and local JetStream.

Changelog:
- 2026-10-08: cover device identity, auth, retry, the inbox (asks answered once) and presence.
"""
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
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import nats
from nats.js.errors import NotFoundError

from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.shared.web import asks_loop, server


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

    async def test_signed_out_waits_five_minutes_or_a_sign_in(self):
        with patch.object(asks_loop.auth, "bearer_token", side_effect=SystemExit("signed out")), \
                patch.object(asks_loop.urllib.request, "urlopen") as get, \
                patch.object(asks_loop, "_connected", new_callable=AsyncMock) as connect, \
                patch.object(asks_loop.WAKE, "wait", side_effect=asyncio.CancelledError) as wait:
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._run(repo_root=Path("/synthetic"), env_file=Path("/synthetic/.env"),
                                     device_id=str(uuid4()))
            wait.assert_called_once_with(300)
            self.assertEqual(asks_loop.STATUS["state"], "signed_out")
            get.assert_not_called()
            connect.assert_not_called()

    async def test_connection_failures_back_off(self):
        with patch.object(asks_loop, "_connection", return_value={}), \
                patch.object(asks_loop, "_connected", side_effect=ConnectionError), \
                patch.object(asks_loop.asyncio, "sleep", side_effect=[None, None, asyncio.CancelledError]) as sleep, \
                self.assertLogs(asks_loop.__name__, level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await asks_loop._run(repo_root=Path("/synthetic"), env_file=Path("/synthetic/.env"),
                                     device_id=str(uuid4()))
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2, 4])


class AskLoopServerTests(unittest.TestCase):
    def test_first_store_request_starts_one_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = threading.Event()
            daemon = []

            def run(**kwargs):
                daemon.append(threading.current_thread().daemon)
                started.set()

            with patch.object(asks_loop, "run", side_effect=run) as loop, \
                    patch("packs.ingestion.primitives.deep_context_v2.openai.load_env"), \
                    patch("packs.ingestion.primitives.share.web.logbook.default_paths",
                          return_value={"gmail": root / "synthetic-gmail.sqlite"}), \
                    ThreadingHTTPServer(("127.0.0.1", 0), server.persistent_handler(root)) as page:
                thread = threading.Thread(target=page.serve_forever, daemon=True)
                thread.start()

                def request():
                    client = http.client.HTTPConnection(*page.server_address, timeout=5)
                    client.request("GET", "/api/review/page")
                    response = client.getresponse()
                    response.read()
                    client.close()
                    return response.status

                try:
                    loop.assert_not_called()
                    self.assertEqual(request(), 503)
                    loop.assert_not_called()
                    conn = open_store(root / server.STORE, shared=True)
                    with patch("packs.ingestion.primitives.deep_context_v2.db.store.open_store", return_value=conn):
                        self.assertEqual(request(), 200)
                        self.assertTrue(started.wait(2))
                        self.assertEqual(request(), 200)
                    conn.close()
                    loop.assert_called_once_with(repo_root=root, env_file=root / ".env")
                    self.assertEqual(daemon, [True])
                finally:
                    page.shutdown()
                    thread.join()


class AskLoopNatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def quiet_error(exc):
            pass

        try:
            self.nc = await nats.connect("nats://localhost:4222", allow_reconnect=False,
                                         connect_timeout=1, error_cb=quiet_error)
        except (OSError, nats.errors.Error) as exc:
            self.skipTest(f"Local NATS unavailable: {type(exc).__name__}")
        self.addAsyncCleanup(self.nc.close)
        self.js = self.nc.jetstream()
        try:
            info = await self.js.stream_info("asks")
            if set(info.config.subjects or []) != {"set.>", "op.>"}:
                await self.js.update_stream(name="asks", subjects=["set.>", "op.>"])
        except NotFoundError:
            await self.js.add_stream(name="asks", subjects=["set.>", "op.>"])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.device_id = asks_loop._device_id(self.root)
        self.env_file = self.root / ".env"
        self.operator_id = str(uuid4())
        prefix = f"set.{uuid4()}"
        self.subjects = {"tasks": f"{prefix}.op.{self.operator_id}",
                         "asks": f"{prefix}.ask.", "presence": f"{prefix}.presence",
                         "inbox": f"op.{self.operator_id}"}
        self.connection = {"url": "nats://localhost:4222", "token": "", "subjects": self.subjects}
        self.asker = str(uuid4())
        self.worker = Mock(return_value=None)
        self.enterContext(patch.object(asks_loop, "_answer", self.worker))
        self.enterContext(patch.object(asks_loop, "_apply_set_messages", return_value={self.asker}))
        self.inbox = self.enterContext(patch.object(asks_loop.agent_inbox, "pull", return_value=[]))
        self.enterContext(patch.object(asks_loop, "HEARTBEAT_SECONDS", 0.05))
        self.loop = None
        self.addAsyncCleanup(self.cleanup_loop)

    async def cleanup_loop(self):
        await self.stop_loop()
        try:
            await self.js.delete_consumer("asks", self.device_id + "-inbox")
        except NotFoundError:
            pass

    async def stop_loop(self):
        if self.loop is not None:
            self.loop.cancel()
            await asyncio.gather(self.loop, return_exceptions=True)
            self.loop = None

    def start_loop(self):
        self.loop = asyncio.create_task(asks_loop._connected(
            self.connection, repo_root=self.root, env_file=self.env_file, device_id=self.device_id))

    async def until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                if self.loop is not None and self.loop.done():
                    self.loop.result()
                await asyncio.sleep(0.01)

    def write_message(self, kind: str, sender: str | None = None) -> Path:
        """One pulled message in the inbox: an ask, or an invite (which the loop leaves alone)."""
        if kind == "ask":
            payload = {"ask_id": str(uuid4()), "question": "Can Jordan Bravo help?", "candidates": [],
                       "role": {"title": "Founding engineer", "company": "", "job_description": ""}}
        else:
            payload = {"set_id": str(uuid4()), "set_name": "Founders", "from_email": "casey@example.com"}
        message = {"id": str(uuid4()), "kind": kind, "created_at": "2026-10-08T00:00:00Z",
                   "from": {"operator_id": sender or self.asker, "name": "Casey Delta"}, "payload": payload}
        inbox = self.root / ".powerpacks" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{message['id']}.json"
        path.write_text(json.dumps(message))
        return path

    async def test_presence_and_connected_state(self):
        presence = await self.nc.subscribe(self.subjects["presence"])
        await self.nc.flush()
        self.start_loop()
        heartbeat = json.loads((await presence.next_msg(timeout=5)).data)
        self.assertEqual(heartbeat["kind"], "heartbeat")
        self.assertEqual(heartbeat["operator_id"], self.operator_id)
        self.assertEqual(heartbeat["device_id"], self.device_id)
        self.assertIsNotNone(datetime.fromisoformat(heartbeat["at"]).tzinfo)
        self.assertEqual(asks_loop.STATUS["state"], "connected")
        info = await self.js.consumer_info("asks", self.device_id + "-inbox")
        self.assertEqual(info.config.filter_subject, self.subjects["inbox"])

    async def test_ask_in_the_inbox_is_answered_once_and_removed(self):
        ask = self.write_message("ask")
        invite = self.write_message("set_invite")
        self.start_loop()
        await self.until(lambda: self.worker.call_count == 1)
        self.assertEqual(self.worker.call_args.args[0].id, ask.stem)
        await self.until(lambda: not ask.exists())
        await self.js.publish(self.subjects["inbox"], json.dumps({"kind": "message", "message_id": "m2"}).encode())
        await self.until(lambda: self.inbox.call_count == 2)
        self.assertEqual(self.worker.call_count, 1)
        self.assertTrue(invite.exists())  # an invite waits for its person

    async def test_an_ask_from_outside_the_sets_is_dropped_without_an_answer(self):
        stranger = self.write_message("ask", sender=str(uuid4()))
        with self.assertLogs(asks_loop.__name__, level="WARNING"):
            self.start_loop()
            await self.until(lambda: not stranger.exists())
        self.worker.assert_not_called()

    async def test_a_failed_answer_is_logged_and_tried_on_the_next_pull(self):
        ask = self.write_message("ask")
        self.worker.side_effect = [RuntimeError("relay down"), None]
        with self.assertLogs(asks_loop.__name__, level="WARNING"):
            self.start_loop()
            await self.until(lambda: self.worker.call_count == 1)
        self.assertTrue(ask.exists())
        await self.js.publish(self.subjects["inbox"], json.dumps({"kind": "message", "message_id": "m2"}).encode())
        await self.until(lambda: self.worker.call_count == 2)

    async def test_durable_replays_a_nudge_sent_while_laptop_is_asleep(self):
        self.start_loop()
        await self.until(lambda: self.inbox.call_count == 1)
        await self.stop_loop()
        await self.js.publish(self.subjects["inbox"], json.dumps({"kind": "message", "message_id": "m1"}).encode())
        self.inbox.reset_mock()
        self.start_loop()
        # The pull on connect plus the replayed nudge.
        await self.until(lambda: self.inbox.call_count == 2)

    async def test_inbox_nudge_pulls_agent_messages(self):
        with patch.object(asks_loop, "_connection", return_value=self.connection):
            self.loop = asyncio.create_task(asks_loop._run(
                repo_root=self.root, env_file=self.env_file, device_id=self.device_id))
            await self.until(lambda: self.inbox.call_count == 1)  # once on connect
            await self.js.publish(self.subjects["inbox"], json.dumps({"kind": "message", "message_id": "m1"}).encode())
            await self.until(lambda: self.inbox.call_count == 2)
            self.inbox.assert_called_with(repo_root=self.root, env_file=self.env_file)
            await self.stop_loop()

    async def test_closed_connection_reconnects_and_pulls_again(self):
        clients = []
        connect = nats.connect

        async def connected(*args, **kwargs):
            client = await connect(*args, **kwargs)
            clients.append(client)
            return client

        with patch.object(asks_loop, "_connection", return_value=self.connection), \
                patch.object(nats, "connect", side_effect=connected), \
                self.assertLogs(asks_loop.__name__, level="WARNING"):
            self.loop = asyncio.create_task(asks_loop._run(
                repo_root=self.root, env_file=self.env_file, device_id=self.device_id))
            await self.until(lambda: self.inbox.call_count == 1)
            await clients[0].close()
            await self.until(lambda: self.inbox.call_count == 2)
            self.assertEqual(len(clients), 2)
            await self.stop_loop()

    async def test_refresh_reconnects_with_fresh_credentials_without_a_warning(self):
        clients = []
        connect = nats.connect

        async def connected(*args, **kwargs):
            client = await connect(*args, **kwargs)
            clients.append(client)
            return client

        connection = Mock(return_value=self.connection)
        with patch.object(asks_loop, "_connection", connection), \
                patch.object(asks_loop, "REFRESH_SECONDS", 0), \
                patch.object(asks_loop, "HEARTBEAT_SECONDS", 0.2), \
                patch.object(nats, "connect", side_effect=connected), \
                self.assertNoLogs(asks_loop.__name__, level="WARNING"):
            self.loop = asyncio.create_task(asks_loop._run(
                repo_root=self.root, env_file=self.env_file, device_id=self.device_id))
            await self.until(lambda: len(clients) >= 2)
            self.assertGreaterEqual(connection.call_count, 2)
            await self.stop_loop()


if __name__ == "__main__":
    unittest.main()
