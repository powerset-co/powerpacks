"""Memory status and actions; all paid work runs in the memory daemon thread."""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast
from urllib.parse import ParseResult, urlparse

from packs.ingestion.primitives.refresh.api import _send_json
from packs.memory import job, runner
from packs.memory.cost import Usage
from packs.memory.generate import Generate
from packs.memory.sources import V2
from packs.memory.store import Store


class MemoryApi:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, handler: BaseHTTPRequestHandler, parsed: ParseResult) -> bool:
        if parsed.path != "/api/memory":
            return False
        record = job.read(self.root)
        payload = asdict(record)
        if not record.enabled:
            adapter = V2(self.root / ".powerpacks")
            if adapter.database.is_file():
                payload["inventory"] = adapter.inventory()
                payload["sources"] = ["saved facts", "human tags and notes", "cached profiles", "display names"]
                client = runner.provider()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        store = Store(Path(directory) / "memory.sqlite", create=True)
                        try:
                            store.import_source(adapter)
                            estimate = Generate(store, client).estimate(limit=2**31 - 1)
                            usage = Usage(cast(int, estimate["generation_input_tokens"]),
                                          cast(int, estimate["generation_output_token_cap"]),
                                          cast(int, estimate["embedding_input_tokens"]))
                            estimate["estimated_cost_usd"] = usage.cost(client.model, client.embedding_key)
                            payload["estimate"] = estimate
                        finally:
                            store.close()
                finally:
                    client.close()
            else:
                payload.update(inventory={"counts": {}}, sources=[], estimate=None)
        _send_json(handler, payload)
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: ParseResult) -> bool:
        if parsed.path not in ("/api/memory/enable", "/api/memory/update", "/api/memory/settings"):
            return False
        origin = handler.headers.get("Origin")
        if origin and urlparse(origin).netloc != handler.headers.get("Host"):
            _send_json(handler, {"error": "Use the local Powerpacks page"}, HTTPStatus.FORBIDDEN)
            return True
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError("invalid request size")
            body = json.loads(handler.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object")
            if parsed.path.endswith("/enable"):
                amount = body.get("approved_spend_usd")
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise ValueError("approved_spend_usd must be a number")
                record = runner.start(self.root, float(amount))
            elif parsed.path.endswith("/update"):
                record = runner.update(self.root)
            else:
                if not isinstance(body.get("keep_updated"), bool):
                    raise ValueError("keep_updated must be a boolean")
                with job.LOCK:
                    record = job.read(self.root)
                    record.keep_updated = body["keep_updated"]
                    job.write(self.root, record)
            _send_json(handler, asdict(record), HTTPStatus.OK if parsed.path.endswith("/settings") else HTTPStatus.ACCEPTED)
        except (ValueError, OSError) as exc:
            _send_json(handler, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return True
