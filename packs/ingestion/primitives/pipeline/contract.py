#!/usr/bin/env python3
"""What a stage reads, what it writes, and the row shape of each — declared once.

A stage's inputs and outputs are today implied by whatever paths its code happens
to open, and its manifest is whatever dict it happens to build. This module makes
both DECLARATIONS, checkable without running anything:

  Artifact  one file a node reads or writes: its path, the row model its header
            must match, how it writes (full_rewrite/upsert/append/annotate), and
            `owns_columns` — which columns are ITS to write. Two nodes may name
            the same path only when their owned columns are disjoint (the real
            shape of `messages/contacts.csv`: discovery owns the metadata columns,
            the import matcher owns the match_* columns).
  RowModel  a pydantic model whose FIELDS ARE the CSV columns, generated from the
            existing column constant so column order stays the output contract.
            Every column is an optional string, so a people.csv written before a
            column existed still reads (`extra="ignore"` + defaults).
  Node      `run()` is a template, not a hook: validate declared inputs -> call
            the subclass's `execute()` -> validate declared outputs -> write the
            typed manifest. Subclasses may not override it. A node that fails to
            declare name/inputs/outputs/payload/manifest raises TypeError at
            IMPORT time, not on the run that would have needed it. `run()`
            returns the TYPED payload; a caller that wants the dict form calls
            `to_payload()`.

Per-node IO stats: the manifest's `fingerprints` block is computed HERE, from the
declarations — `input_artifacts` is what the node read, `output_artifacts` what it
wrote, both keyed by declared path, and each entry of a `row_model` artifact
carries `rows`. That is what a consumer reads instead of opening an output file to
count it (`imports/status.py`), so a file that existed only to be counted can be
deleted. Nothing here caches, ids, or sequences a run: the declaration is
compile-time and the only durable artifacts stay each stage's outputs plus its one
manifest.json.

Flow (Node.run):
  declared inputs readable? -> no  -> typed NotReady payload (never an exception)
                            -> yes -> execute() -> completed? -> every declared
                               output present, header == its row model -> stats
                               from the declarations -> manifest

Changelog:
  2026-07-26 (per-node IO stats): `Node.run()` returns the typed `StageManifest`
    instead of the written manifest dict (which unblocked converting the enrich
    stage's store, whose caller consumes the payload by attribute), and
    `artifact_stats()` computes the manifest's `fingerprints` block from the
    DECLARATIONS — including a `rows` count per row-model artifact. The
    `output_paths` parameter this used to pass into
    `common/manifests.write_stage_manifest` went with it: the block arrives
    precomputed now, so the parameter had no caller left.
  2026-07-25 (import stage): `Artifact.owns_rows_where` added — the ROW-slice axis
    `owns_columns` cannot express. `directory.csv` has two legitimate writers that
    each own every column of their own source's rows (gmail, messages); on the
    column axis they can only be described as two whole-file writers. The field is
    declaration-only: the graph checker compares the predicate STRINGS and never
    evaluates them.
  2026-07-25: created. pydantic v2 replaces the hand-rolled StagePayload dataclass
    (`to_payload` None-dropping is `model_dump(exclude_none=True)`) and the manual
    column dict-filling in `normalize_people_row`.
"""

from __future__ import annotations

import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, create_model, model_validator

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import read_json  # noqa: E402
from packs.ingestion.primitives.common.manifests import artifact_fingerprint, write_stage_manifest  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
    normalize_linkedin_url,
    row_public_identifier,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

STATUS_COMPLETED = "completed"
STATUS_NOT_READY = "not_ready"


class ContractError(RuntimeError):
    """A declaration was violated at run time (missing output, drifted header)."""


class StageManifest(BaseModel):
    """Base for a stage's TYPED manifest payload — the pydantic successor to
    `StagePayload`. `extra="forbid"` is the point: a stage cannot invent a field
    inline, and `to_payload()` drops None-valued optionals exactly as
    `StagePayload.to_payload()` did (verified against nested dicts, which it
    leaves alone)."""

    model_config = ConfigDict(extra="forbid")

    status: str = ""

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class NotReady(StageManifest):
    """The payload `Node.run()` returns when a declared, required input is not
    readable — so a missing upstream artifact is a status, not a traceback."""

    stage: str = ""
    status: str = STATUS_NOT_READY
    reason: str = "missing_inputs"
    missing_inputs: tuple[str, ...] = ()


class RowModel(BaseModel):
    """Base for artifact ROW models. Every field is a string column; unknown
    columns are ignored and absent columns default to "", which IS the
    backwards-compatibility story — an older CSV with fewer columns reads clean.

    The before-validator is the one that replaces hand-rolled normalization:
    None -> "", dict/list -> compact JSON, everything else -> str()."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _stringify(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out: dict[str, Any] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue  # csv.DictReader's short-row None key
            if value is None:
                out[key] = ""
            elif isinstance(value, (dict, list)):
                out[key] = json.dumps(value, ensure_ascii=False)
            else:
                out[key] = value if isinstance(value, str) else str(value)
        return out

    @classmethod
    def columns(cls) -> list[str]:
        """The CSV columns, in declaration order — the output contract."""
        return list(cls.model_fields)

    def to_row(self) -> dict[str, str]:
        return self.model_dump()


def row_model_for(name: str, columns: list[str]) -> type[RowModel]:
    """A RowModel generated FROM a column constant, so field order cannot drift
    from CSV order: there is still exactly one home for the column list."""
    return create_model(name, __base__=RowModel, **{column: (str, "") for column in columns})


_PeopleFields = row_model_for("_PeopleFields", PEOPLE_SCHEMA_COLUMNS)


class PeopleRow(_PeopleFields):  # type: ignore[misc, valid-type]
    """One `people.csv` row — the pydantic form of `normalize_people_row`.

    The two identity rules keep their single home in `people_schema`
    (`normalize_linkedin_url`, `row_public_identifier`); this validator is what
    makes them run on every row read through the contract instead of only where a
    caller remembered to call the normalizer. The slug is RE-DERIVED from the
    normalized URL, so a row carrying a percent-encoded `public_identifier` and a
    decoded URL stops keying as two people."""

    @model_validator(mode="after")
    def _normalize_identity(self) -> "PeopleRow":
        self.linkedin_url = normalize_linkedin_url(self.linkedin_url)
        self.public_identifier = row_public_identifier(
            {"linkedin_url": self.linkedin_url, "public_identifier": self.public_identifier}
        )
        return self


class Artifact(BaseModel):
    """One file a node reads or writes.

    owns_columns  the columns THIS node writes. Empty means it owns the whole
                  file. Two nodes may declare the same path only when their owned
                  column sets are disjoint and neither claims the whole file —
                  that is the difference between `messages/contacts.csv`
                  (legitimate: discovery's 11 metadata columns vs the import
                  matcher's 8 match columns) and `index.json` before #337
                  (illegitimate: two whole-file writers, 494 duplicate rows).
    owns_rows_where  the ROW slice this node writes, as a human-readable
                  predicate over a row (`"source == 'messages'"`). Columns are
                  the wrong axis for `directory.csv`: gmail and messages each
                  own every column of their own source's rows and touch no other
                  source's row, so `owns_columns` can only describe them as two
                  whole-file writers. DECLARATION ONLY — the checker compares
                  these strings for equality and never evaluates them, so two
                  writers naming different slices are not a conflict and two
                  naming the same slice are.
    external      no node in the graph produces it (a msgvault db, a LinkedIn
                  export). The graph checker only flags a producer-less input
                  when this is False.
    required      the path must be on disk: as an input, before `execute()`; as
                  an output, after a completed `execute()`. False where absence
                  is real, existing behavior (the merge tolerates an absent
                  per-source people.csv; the gmail extractor legitimately writes
                  no queue for an account with no matching mail).
    """

    model_config = ConfigDict(frozen=True)

    path: str
    row_model: type[RowModel] | None = None
    writes: Literal["full_rewrite", "upsert", "append", "annotate"] | None = None
    external: bool = False
    owns_columns: tuple[str, ...] = ()
    owns_rows_where: str = ""
    required: bool = True


class Node(ABC):
    """A pipeline step that declares its contract as ClassVars and inherits the
    run template. Subclasses implement `execute()` and never `run()`."""

    name: ClassVar[str]
    inputs: ClassVar[tuple[Artifact, ...]]
    outputs: ClassVar[tuple[Artifact, ...]]
    payload: ClassVar[type[StageManifest]]
    # "" means this node reports into its PARENT stage's manifest (the gmail
    # per-account channel has no manifest.json of its own) — declared, not absent.
    manifest: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject an under-declared node at IMPORT time."""
        super().__init_subclass__(**kwargs)
        errors: list[str] = []
        if not isinstance(getattr(cls, "name", None), str) or not getattr(cls, "name"):
            errors.append("name must be a non-empty str")
        for attr in ("inputs", "outputs"):
            value = getattr(cls, attr, None)
            if not isinstance(value, tuple) or not all(isinstance(item, Artifact) for item in value):
                errors.append(f"{attr} must be a tuple[Artifact, ...]")
        payload = getattr(cls, "payload", None)
        if not (isinstance(payload, type) and issubclass(payload, StageManifest)):
            errors.append("payload must be a StageManifest subclass")
        if not isinstance(getattr(cls, "manifest", None), str):
            errors.append('manifest must be a str ("" = reports into the parent stage manifest)')
        # Resolve through the MRO, not vars(cls): a base listed BEFORE Node
        # (a mixin like MessageChannel) shadows Node.run without ever putting
        # "run" in the subclass's own __dict__, which silently skips every
        # declared input/output check.
        if cls.run is not Node.run:
            errors.append("run() is the template method and must not be overridden; implement execute()")
        if errors:
            raise TypeError(f"{cls.__name__} is not a valid Node: " + "; ".join(errors))

    def bindings(self) -> dict[str, str]:
        """Declared path -> the concrete path THIS instance uses. Declared paths
        are the graph's fixed names; an instance built with an explicit output dir
        (or a test's temp dir) says so here. Default: declared paths verbatim."""
        return {}

    def resolved(self, artifacts: tuple[Artifact, ...]) -> list[Artifact]:
        binding = self.bindings()
        return [item.model_copy(update={"path": binding.get(item.path, item.path)}) for item in artifacts]

    @abstractmethod
    def execute(self) -> StageManifest:
        """Do the work and return the typed payload. Writes only declared outputs."""

    def run(self) -> StageManifest:
        """Template: validate inputs -> execute -> validate outputs -> manifest.

        Returns the TYPED payload, not the written manifest dict: a caller that
        branches on `status` reads an attribute, and a caller that needs the dict
        (to nest the payload in a parent manifest, or to emit it) calls
        `to_payload()`. Returning the dict is what kept the enrich stage's store
        out of this template — its caller consumes `.status`/`.counts`/`.artifacts`
        off the typed manifest."""
        missing = [item.path for item in self.resolved(self.inputs) if item.required and not _readable(item.path)]
        if missing:
            payload: StageManifest = NotReady(stage=self.name, missing_inputs=tuple(missing))
        else:
            payload = self.execute()
            if payload.status == STATUS_COMPLETED:
                self.verify_outputs()
        self._write(payload)
        return payload

    def verify_outputs(self) -> None:
        """Every declared output exists and its header still matches its model."""
        for item in self.resolved(self.outputs):
            path = Path(item.path)
            if not path.is_file():
                if item.required:
                    raise ContractError(f"{self.name}: declared output was not written: {item.path}")
                continue
            if item.row_model is None:
                continue
            header = CsvIO.read_header(path)
            if item.owns_columns:
                absent = [column for column in item.owns_columns if column not in header]
                if absent:
                    raise ContractError(f"{self.name}: {item.path} is missing owned columns {absent}")
            elif header != item.row_model.columns():
                raise ContractError(
                    f"{self.name}: {item.path} header drifted from {item.row_model.__name__}: "
                    f"missing={_brief([c for c in item.row_model.columns() if c not in header])} "
                    f"unexpected={_brief([c for c in header if c not in item.row_model.columns()])}"
                )

    def artifact_stats(self, manifest_path: Path) -> dict[str, Any]:
        """This node's IO stats: what it read and what it wrote, per DECLARED path.

        The manifest's `fingerprints` block, and the reason a consumer never has to
        open an output file to count it. Each entry is
        `common/manifests.artifact_fingerprint` (size/mtime/sha256, reusing the
        prior entry when the file has not changed) plus `rows` for an artifact that
        declares a `row_model` — the count is taken in the same pass and carried
        forward with the reused fingerprint, so an unchanged 11MB people.csv is
        neither re-hashed nor re-counted. An artifact with no row model (a sqlite
        store, a JSON gate file) has no `rows`, and neither does one that is not on
        disk."""
        existing = (read_json(manifest_path, {}) or {}).get("fingerprints") or {}
        return {
            "input_artifacts": self._stats(self.inputs, existing.get("input_artifacts")),
            "output_artifacts": self._stats(self.outputs, existing.get("output_artifacts")),
        }

    def _stats(self, artifacts: tuple[Artifact, ...], existing: Any) -> dict[str, Any]:
        existing = existing if isinstance(existing, dict) else {}
        stats: dict[str, Any] = {}
        for item in self.resolved(artifacts):
            # An unbound per-account TEMPLATE (gmail's `{account_slug}`) names no
            # file, so there is nothing to stat: a run with no selected account
            # would otherwise record a literal "{account_slug}" path.
            if "{" in item.path:
                continue
            stats[item.path] = _artifact_stat(item.path, existing.get(item.path), item.row_model)
        return stats

    def _write(self, payload: StageManifest) -> None:
        """Write the one manifest, with this node's declared IO stats."""
        manifest_path = self.bindings().get(self.manifest, self.manifest)
        if not manifest_path:
            return
        body = payload.to_payload()
        body["fingerprints"] = self.artifact_stats(Path(manifest_path))
        write_stage_manifest(Path(manifest_path), body)


def _artifact_stat(path_text: str, existing: dict[str, Any] | None, row_model: type[RowModel] | None) -> dict[str, Any]:
    """One artifact's fingerprint, plus `rows` when it declares a row model.

    `artifact_fingerprint` returns the PRIOR entry verbatim when size and mtime
    still match, so `"rows" in stat` is exactly "the count was carried forward" —
    and its absence is either a fresh/changed file or a manifest written before
    row counts existed. Both cases count."""
    stat = artifact_fingerprint(path_text, existing)
    if row_model is None or not stat.get("exists") or "rows" in stat:
        return stat
    return {**stat, "rows": CsvIO.count_rows(Path(stat["path"]))}


def _brief(columns: list[str], limit: int = 5) -> str:
    """Column list for an error message, capped so a 37-column drift stays readable."""
    return str(columns) if len(columns) <= limit else f"{columns[:limit]}+{len(columns) - limit} more"


def _readable(path_text: str) -> bool:
    path = Path(path_text)
    return path.is_file() and os.access(path, os.R_OK)
