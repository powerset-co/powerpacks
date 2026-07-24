#!/usr/bin/env python3
"""Static discovery input/output contract.

Changelog:
  2026-07-23 (account-email selection): gmail discovery stopped reading
    accounts.json for account selection, so the accounts_path() and state_value()
    accessors (which served only that read) were removed. The now-orphaned
    top-level accounts_json config key was then pruned from discovery.config.json
    too — nothing read it. Remaining accessors — load_config, source_config,
    config_path, output_path — still back the msgvault_db/sync_query defaults and
    the gmail output paths.
"""

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
