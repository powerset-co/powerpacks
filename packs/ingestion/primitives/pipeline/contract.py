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
            IMPORT time, not on the run that would have needed it.

Nothing here caches, ids, or sequences a run: the declaration is compile-time and
the only durable artifacts stay each stage's outputs plus its one manifest.json.

Flow (Node.run):
  declared inputs readable? -> no  -> typed NotReady payload (never an exception)
                            -> yes -> execute() -> completed? -> every declared
                               output present, header == its row model -> manifest

Changelog:
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

from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
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
    external      no node in the graph produces it (a msgvault db, a LinkedIn
                  export). The graph checker only flags a producer-less input
                  when this is False.
    required      the path must be on disk: as an input, before `execute()`; as
                  an output, after a completed `execute()`. False where absence
                  is real, existing behavior (the merge tolerates an absent
                  per-source people.csv; the gmail extractor legitimately writes
                  no queue for an account with no matching mail).
    consumers_optional  nothing is expected to read it, so the checker must not
                  report it as a dead output."""

    model_config = ConfigDict(frozen=True)

    path: str
    row_model: type[RowModel] | None = None
    writes: Literal["full_rewrite", "upsert", "append", "annotate"] | None = None
    external: bool = False
    owns_columns: tuple[str, ...] = ()
    consumers_optional: bool = False
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
        if "run" in vars(cls):
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

    def run(self) -> dict[str, Any]:
        """Template: validate inputs -> execute -> validate outputs -> manifest."""
        missing = [item.path for item in self.resolved(self.inputs) if item.required and not _readable(item.path)]
        if missing:
            return self._write(NotReady(stage=self.name, missing_inputs=tuple(missing)))
        payload = self.execute()
        if payload.status == STATUS_COMPLETED:
            self.verify_outputs()
        return self._write(payload)

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

    def _write(self, payload: StageManifest) -> dict[str, Any]:
        """Write the one manifest, fingerprinting the DECLARED output paths."""
        body = payload.to_payload()
        manifest_path = self.bindings().get(self.manifest, self.manifest)
        if not manifest_path:
            return body
        return write_stage_manifest(
            Path(manifest_path), body, output_paths=[item.path for item in self.resolved(self.outputs)]
        )


def _brief(columns: list[str], limit: int = 5) -> str:
    """Column list for an error message, capped so a 37-column drift stays readable."""
    return str(columns) if len(columns) <= limit else f"{columns[:limit]}+{len(columns) - limit} more"


def _readable(path_text: str) -> bool:
    path = Path(path_text)
    return path.is_file() and os.access(path, os.R_OK)
