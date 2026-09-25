#!/usr/bin/env python3
"""Static discovery input/output contract.

Changelog:
  2026-09-23 (typed rows): the config document is parsed ONCE, in
    `SourceConfig.from_document` — the only place its JSON shape (`.get`) is read.
    `source_config` now returns that typed `SourceConfig`, and `config_path` asks
    it for a declared section/key instead of walking the raw dict, so no caller
    probes the config document. Same config file, same keys, same KeyError text.
  2026-07-26 (contacts.csv deleted): the gmail `outputs.contacts_csv` key went
    with the file — the stage writes `linkedin_resolution_queue_csv` only, so
    nothing called `output_path("gmail", "contacts_csv")` anymore.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("discovery.config.json")


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


@dataclass(frozen=True)
class SourceConfig:
    """One discovery source's declared config, parsed once — `from_document` is the
    ONE reader of the config document's shape. Readers ask this object for a
    declared section or value; nobody walks the raw JSON dict."""

    source: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_document(cls, document: Any, source: str) -> "SourceConfig":
        document = document if isinstance(document, dict) else {}
        sources = document.get("sources")
        sources = sources if isinstance(sources, dict) else {}
        raw = sources.get(source)
        if not isinstance(raw, dict):
            raise KeyError(f"unknown discovery source: {source}")
        return cls(
            source=source,
            inputs=_string_map(raw.get("inputs")),
            outputs=_string_map(raw.get("outputs")),
        )

    def section(self, name: str) -> dict[str, str]:
        if name == "inputs":
            return self.inputs
        if name == "outputs":
            return self.outputs
        return {}

    def value(self, section: str, key: str) -> str:
        values = self.section(section)
        if key not in values:
            raise KeyError(f"missing discovery config path: {self.source}.{section}.{key}")
        return values[key]

    def optional_value(self, section: str, key: str) -> str:
        values = self.section(section)
        return values[key] if key in values else ""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_config(source: str, path: Path = CONFIG_PATH) -> SourceConfig:
    return SourceConfig.from_document(load_config(path), source)


def config_path(source: str, section: str, key: str, path: Path = CONFIG_PATH) -> Path:
    return Path(source_config(source, path).value(section, key))


def output_path(source: str, key: str, path: Path = CONFIG_PATH) -> Path:
    return config_path(source, "outputs", key, path)
