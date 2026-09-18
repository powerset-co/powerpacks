"""Allowlisted render-model snapshots, rendered by the same local viewer."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import types
from dataclasses import asdict, fields, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

from . import RESULTS_CSS, RESULTS_JS
from .model import Candidate, CandidateJudgment, SearchResult, load_searches
from .rendering import render_page

SCHEMA_VERSION = 1
_field_types = lru_cache(maxsize=None)(get_type_hints)


def _decode(kind: Any, value: Any, path: str) -> Any:
    """Reject fields that the renderer does not consume, before persistence."""
    if get_origin(kind) is types.UnionType:
        if value is None and type(None) in get_args(kind):
            return None
        return _decode(next(item for item in get_args(kind) if item is not type(None)), value, path)
    if get_origin(kind) is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{path} must be an array")
        return tuple(_decode(get_args(kind)[0], item, f"{path}[{index}]")
                     for index, item in enumerate(value))
    if is_dataclass(kind):
        hints = _field_types(kind)
        if not isinstance(value, dict) or set(value) != set(hints):
            raise ValueError(f"{path} must contain exactly the renderer fields")
        result = kind(**{field.name: _decode(hints[field.name], value[field.name], f"{path}.{field.name}")
                         for field in fields(kind)})
        scores = ((result.human_score,) if kind is Candidate else
                  (result.domain_score, result.opportunity_cap, result.overall_score)
                  if kind is CandidateJudgment else ())
        if any(score is not None and score not in range(1, 6) for score in scores):
            raise ValueError(f"{path} ratings must be between 1 and 5")
        return result
    if kind is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{path} must be a finite number")
        return float(value)
    if type(value) is not kind:
        raise ValueError(f"{path} must be {kind.__name__}")
    if kind is str and path.endswith("_url") and value:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
            raise ValueError(f"{path} must be an HTTP(S) URL without credentials")
    return value


def validate_snapshot(payload: Any) -> dict[str, Any]:
    """Validate the complete wire contract; never accept HTML or raw run artifacts."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "search", "tags"}:
        raise ValueError("Snapshot requires schema_version, search and tags")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported snapshot schema_version")
    search = _decode(SearchResult, payload["search"], "search")
    if not search.run_id or not search.title:
        raise ValueError("Search run_id and title are required")
    tags = payload["tags"]
    if not isinstance(tags, dict) or set(tags) != {"tags", "assignments"}:
        raise ValueError("Snapshot tags require tags and assignments")
    names, assignments = tags["tags"], tags["assignments"]
    if not isinstance(names, list) or any(type(tag) is not str or not tag or len(tag) > 40 for tag in names):
        raise ValueError("Tags must be non-empty strings of at most 40 characters")
    people = {row.person_id for row in search.candidates}
    if not isinstance(assignments, dict) or any(
        person not in people or not isinstance(labels, list)
        or any(type(label) is not str or label not in names for label in labels)
        for person, labels in assignments.items()
    ):
        raise ValueError("Tag assignments must reference candidates and declared tags")
    return {"schema_version": SCHEMA_VERSION, "search": asdict(search), "tags": tags}


def export_snapshot(run_dir: Path) -> dict[str, Any]:
    """Read the same model and saved labels as the local viewer, without changing them."""
    searches = load_searches(run_dir.parent, run_dir.name)
    if not searches:
        raise ValueError("Run has no completed search results")
    search = searches[0]
    tags_path = run_dir / "tags.json"
    tags = json.loads(tags_path.read_text()) if tags_path.is_file() else {"tags": [], "assignments": {}}
    people = {row.person_id for row in search.candidates}
    tags["assignments"] = {person: labels for person, labels in tags["assignments"].items() if person in people}
    return validate_snapshot({"schema_version": SCHEMA_VERSION, "search": asdict(search), "tags": tags})


def search_from_snapshot(payload: Any) -> SearchResult:
    """Restore a validated render model for hosted rendering and feedback context."""
    snapshot = validate_snapshot(payload)
    return _decode(SearchResult, snapshot["search"], "search")


def render_snapshot(payload: Any, *, asset_base_url: str | None = None,
                    feedback_enabled: bool = False) -> str:
    """Render a snapshot with optional authenticated-parent feedback, never direct writes."""
    snapshot = validate_snapshot(payload)
    search = _decode(SearchResult, snapshot["search"], "search")
    document = render_page((search,), readonly=True, tags=snapshot["tags"], feedback_enabled=feedback_enabled)
    if asset_base_url:
        url = urlsplit(asset_base_url)
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
            raise ValueError("asset_base_url must be an HTTP(S) URL without credentials")
        base = html.escape(asset_base_url.rstrip("/"), quote=True)
        document = document.replace("/assets/results.css", f"{base}/results.css")
        document = document.replace("/assets/results.js", f"{base}/results.js")
        origin = f"{url.scheme}://{url.netloc}"
        policy = f"default-src 'none'; script-src {origin}; style-src {origin}; "
    else:
        document = document.replace("<link rel='stylesheet' href='/assets/results.css'>",
                                    f"<style>{RESULTS_CSS.read_text()}</style>")
        script = RESULTS_JS.read_text()
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        document = document.replace("<script src='/assets/results.js' defer></script>",
                                    f"<script>{script}</script>")
        policy = f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; "
    policy += "img-src https: http:; connect-src 'none'; base-uri 'none'; form-action 'none'"
    document = document.replace("<head>", "<head><meta name='referrer' content='no-referrer'>"
                                "<meta name='robots' content='noindex,nofollow'>"
                                f'<meta http-equiv="Content-Security-Policy" content="{policy}">')
    return document
