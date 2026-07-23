#!/usr/bin/env python3
"""Static discovery input/output contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("discovery.config.json")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_config(source: str, path: Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = load_config(path)
    sources = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    source_cfg = sources.get(source)
    if not isinstance(source_cfg, dict):
        raise KeyError(f"unknown discovery source: {source}")
    return source_cfg


def config_path(source: str, section: str, key: str, path: Path = CONFIG_PATH) -> Path:
    source_cfg = source_config(source, path)
    section_cfg = source_cfg.get(section)
    if not isinstance(section_cfg, dict) or key not in section_cfg:
        raise KeyError(f"missing discovery config path: {source}.{section}.{key}")
    return Path(str(section_cfg[key]))


def output_path(source: str, key: str, path: Path = CONFIG_PATH) -> Path:
    return config_path(source, "outputs", key, path)


def accounts_path(path: Path = CONFIG_PATH) -> Path:
    cfg = load_config(path)
    return Path(str(cfg.get("accounts_json") or ".powerpacks/ingestion/accounts.json"))


def state_value(data: dict[str, Any], key: str, default: Any = None) -> Any:
    cur: Any = data
    for part in key.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur
