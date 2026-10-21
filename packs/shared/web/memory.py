"""Memory status and actions; all paid work runs in the memory daemon thread."""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast
from urllib.parse import ParseResult, parse_qs, urlparse

from packs.ingestion.primitives.refresh.api import _send_json
from packs.memory import job, runner
from packs.memory.cost import Usage
from packs.memory.directory import Directory, Origin, Selector
from packs.memory.generate import Generate
from packs.memory.graph import graph
from packs.memory.sources import V2
from packs.memory.store import Store


def _source_version(database: Path) -> tuple[tuple[int, int], ...]:
    version = []
    for path in (database, Path(str(database) + "-wal")):
        if path.exists():
            stat = path.stat()
            version.append((stat.st_mtime_ns, stat.st_size))
    return tuple(version)


class MemoryApi:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._source_version: tuple[tuple[int, int], ...] | None = None
        self._preview: dict[str, object] = {}

    def get(self, handler: BaseHTTPRequestHandler, parsed: ParseResult) -> bool:
        if parsed.path not in ("/api/memory", "/api/memory/search", "/api/memory/people", "/api/memory/graph"):
            return False
        try:
            if parsed.path != "/api/memory":
                query = parse_qs(parsed.query, keep_blank_values=True)
                with closing(Store(job.database(self.root), read_only=True)) as store:
                    if parsed.path.endswith("/search"):
                        result: object = [asdict(meaning) for meaning in Directory(store).resolve(query.get("q", [""])[0])]
                    elif parsed.path.endswith('/graph'):
                        selector = Selector(Origin(query['origin'][0]), query.get('value', [''])[0]) if 'origin' in query else None
                        result = asdict(graph(store, limit=int(query.get('limit', ['1000'])[0]),
                                              parent=query.get('parent', [''])[0], topic=query.get('topic', [''])[0], selector=selector))
                    else:
                        selector = Selector(Origin(query.get("origin", [""])[0]), query.get("value", [""])[0])
                        result = asdict(Directory(store).enumerate(selector, after=query.get("after", [""])[0],
                                                                  limit=int(query.get("limit", ["50"])[0])))
                _send_json(handler, result)
                return True
            with job.LOCK:
                record = job.read(self.root)
                if record.status == job.Status.RUNNING and not runner.running(self.root):
                    record = runner.start(self.root, record.approved_spend_usd)
                payload = asdict(record)
                if not record.enabled:
                    payload.update(self._before_enable())
            _send_json(handler, payload)
        except (ValueError, OSError) as exc:
            _send_json(handler, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return True

    def _before_enable(self) -> dict[str, object]:
        adapter = V2(self.root / ".powerpacks")
        if not adapter.database.is_file():
            return {"inventory": {"counts": {}}, "sources": [], "estimate": None}
        version = _source_version(adapter.database)
        if version == self._source_version:
            return self._preview
        client = runner.provider()
        try:
            with closing(Store(job.database(self.root), create=True)) as store:
                store.import_source(adapter)
                estimate = Generate(store, client).estimate(limit=2**31 - 1)
                usage = Usage(cast(int, estimate["generation_input_tokens"]),
                              cast(int, estimate["generation_output_token_cap"]),
                              cast(int, estimate["embedding_input_tokens"]))
                estimate["estimated_cost_usd"] = usage.cost(client.model, client.embedding_key)
                self._preview = {
                    "inventory": adapter.inventory(),
                    "sources": ["saved facts", "human tags and notes", "cached profiles", "display names"],
                    "estimate": estimate,
                }
                self._source_version = _source_version(adapter.database)
        finally:
            client.close()
        return self._preview

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
            if parsed.path.endswith(("/enable", "/update")):
                if parsed.path.endswith("/update") and "approved_spend_usd" not in body:
                    record = runner.update(self.root)
                else:
                    amount = body.get("approved_spend_usd")
                    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                        raise ValueError("approved_spend_usd must be a number")
                    record = (runner.start(self.root, float(amount)) if parsed.path.endswith("/enable")
                              else runner.update(self.root, float(amount)))
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
