#!/usr/bin/env python3
"""Fail when Deep Context bypasses its SQLite projection boundary.

Durable stage artifacts remain useful for inspection and paid-work reuse. The
only general artifact reader is ``db/legacy.py``. Current writers may hand a
just-written output to ``db/projectors.py`` (or hash that output at the writer
boundary); all later consumers hydrate from SQLite payloads.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "packs/ingestion/primitives/deep_context"
LEGACY_READER = PACKAGE / "db/legacy.py"
PROJECTOR_READER = PACKAGE / "db/projectors.py"
DB_PACKAGE = PACKAGE / "db"

FORBIDDEN_STATE_TEXT = (
    "stage_state",
    "spend_approvals",
    "approved_count",
    "count_exceeds_approval",
)
FORBIDDEN_HELPERS = {
    "derive_lookup_maps",
    "load_index",
    "parent_identifiers",
    "read_enrichment_manifest",
    "write_index",
}
SQL_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b|"
    r"CREATE\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b|PRAGMA\b)",
    re.IGNORECASE | re.DOTALL,
)
DIRECT_FILE_READ_METHODS = {"read_bytes", "read_text"}
CSV_READER_CALLS = {"csv.DictReader", "csv.reader"}
KNOWN_READER_HELPERS = {"_load_bundle", "load_owner", "read_jsonl"}
NON_FILE_OPENERS = {"webbrowser.open"}
WRITER_HASH_BOUNDARIES = {
    (
        "packs/ingestion/primitives/deep_context/parallel_research/driver.py",
        "research_artifact_inventory",
    ): "project_artifacts",
}
WRITER_REUSE_BOUNDARIES = {
    (
        "packs/ingestion/primitives/deep_context/build_owner.py",
        "BuildOwner.execute",
        "self.out.read_text",
    ): "_project",
    (
        "packs/ingestion/primitives/deep_context/build_owner.py",
        "BuildOwner.execute",
        "self.out.read_bytes",
    ): "_project",
    (
        "packs/ingestion/primitives/deep_context/collect_person_context.py",
        "_load_bundle",
        "path.read_text",
    ): "project_source_bundle",
}
READER_HELPER_BOUNDARIES = {
    "_load_bundle": {
        (
            "packs/ingestion/primitives/deep_context/collect_person_context.py",
            "_purge_group_scoped_or_untrusted_bundles",
        ),
        (
            "packs/ingestion/primitives/deep_context/collect_person_context.py",
            "CollectPersonContext.execute",
        ),
    },
}
PROJECTOR_CALL_BOUNDARIES = {
    "project_artifacts": {
        (
            "packs/ingestion/primitives/deep_context/assemble_synthetic_profile.py",
            "AssembleSyntheticProfile.execute",
        ),
        (
            "packs/ingestion/primitives/deep_context/enrichment_receipt.py",
            "EnrichmentReceipt.write",
        ),
        (
            "packs/ingestion/primitives/deep_context/enrichment_receipt.py",
            "project_enrichment_artifacts",
        ),
        (
            "packs/ingestion/primitives/deep_context/parallel_research/driver.py",
            "report_progress",
        ),
    },
    "project_facts": {
        (
            "packs/ingestion/primitives/deep_context/synthesize_person_context.py",
            "SynthesizePersonContext.execute",
        ),
    },
    "project_source_bundle": {
        (
            "packs/ingestion/primitives/deep_context/collect_person_context.py",
            "CollectPersonContext.execute",
        ),
    },
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names))


def _mode(call: ast.Call, *, method: bool) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    position = 0 if method else 1
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return str(call.args[position].value)
    return None


def _opens_for_read(call: ast.Call, called: str) -> bool:
    if called in NON_FILE_OPENERS or not (
        called == "open" or called.endswith(".open")
    ):
        return False
    mode = _mode(call, method=called != "open")
    return mode is None or "r" in mode or "+" in mode


def _is_csv_reader(call: ast.Call) -> bool:
    called = _name(call.func)
    if called in CSV_READER_CALLS or called.startswith("CsvIO.read"):
        return True
    return bool(
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "read"
        and isinstance(call.func.value, ast.Call)
        and _name(call.func.value.func).rsplit(".", 1)[-1] == "CsvIO"
    )


def _inside_hash(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(call)
    while current is not None and not isinstance(current, (ast.stmt, ast.comprehension)):
        if isinstance(current, ast.Call) and _name(current.func) == "hashlib.sha256":
            return True
        current = parents.get(current)
    return False


def _inside_static_asset_branch(
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    static_names: set[str],
) -> bool:
    """Recognize the web server's fixed CSS/JS map, not arbitrary path reads."""
    if _name(call.func) != "path.read_bytes":
        return False
    current = parents.get(call)
    branch: ast.If | None = None
    function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    while current is not None:
        if branch is None and isinstance(current, ast.If):
            branch = current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = current
            break
        current = parents.get(current)
    if branch is None or function is None or "parsed.path in assets" not in ast.unparse(branch.test):
        return False
    for candidate in ast.walk(function):
        if not (
            isinstance(candidate, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "assets" for target in candidate.targets)
            and isinstance(candidate.value, ast.Dict)
        ):
            continue
        values = candidate.value.values
        return bool(values) and all(
            isinstance(value, ast.Tuple)
            and value.elts
            and isinstance(value.elts[0], ast.Name)
            and value.elts[0].id in static_names
            for value in values
        )
    return False


def _static_asset_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").endswith(("deep_context.review_web", "review_web.rendering"))
        ):
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"REVIEW_HTML", "REVIEW_CSS", "REVIEW_JS"}
            )
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        expression = ast.unparse(value)
        if "__file__" not in expression or not any(
            suffix in expression for suffix in (".css", ".html", ".js")
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _static_asset_read(
    relative: str,
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    tree: ast.AST,
) -> bool:
    called = _name(call.func)
    method = called.rsplit(".", 1)[-1]
    receiver = called.rsplit(".", 1)[0] if "." in called else ""
    if relative.endswith("/prompts/loader.py"):
        expression = ast.unparse(call.func.value) if isinstance(call.func, ast.Attribute) else ""
        prompt_root = next(
            (
                ast.unparse(node.value)
                for node in getattr(tree, "body", [])
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_PROMPT_DIR" for target in node.targets)
            ),
            "",
        )
        return (
            method == "read_text"
            and "_PROMPT_DIR" in expression
            and ".txt" in expression
            and "__file__" in prompt_root
        )
    if relative.endswith("/synthesis/prompting.py"):
        expression = ast.unparse(call.func.value) if isinstance(call.func, ast.Attribute) else ""
        return method == "read_text" and "fact_schema.json" in expression and "__file__" in expression
    if relative.endswith(("/review_web/server.py", "/review_web/rendering.py")):
        static_names = _static_asset_names(tree)
        if receiver in static_names:
            return called.rsplit(".", 1)[-1] in DIRECT_FILE_READ_METHODS
        return _inside_static_asset_branch(call, parents, static_names)
    return False


def _allowed_file_read(
    path: Path,
    relative: str,
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    tree: ast.AST,
) -> bool:
    if path in {LEGACY_READER, PROJECTOR_READER}:
        return True
    if _static_asset_read(relative, call, parents, tree):
        return True
    called = _name(call.func)
    scope = _scope(call, parents)
    required_projection = WRITER_REUSE_BOUNDARIES.get((relative, scope, called))
    if required_projection:
        if required_projection == "project_source_bundle" and any(
            isinstance(candidate, ast.Call)
            and _name(candidate.func).rsplit(".", 1)[-1] == required_projection
            for candidate in ast.walk(tree)
        ):
            return True
        enclosing = next(
            (
                candidate
                for candidate in ast.walk(tree)
                if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == scope.rsplit(".", 1)[-1]
                and candidate.lineno <= call.lineno <= getattr(candidate, "end_lineno", call.lineno)
            ),
            None,
        )
        projected_after_read = enclosing is not None and any(
            isinstance(candidate, ast.Call)
            and candidate.lineno > call.lineno
            and _name(candidate.func).rsplit(".", 1)[-1] == required_projection
            for candidate in ast.walk(enclosing)
        )
        actual_db_projection = (
            required_projection != "_project"
            or any(
                isinstance(candidate, ast.Call)
                and _name(candidate.func).rsplit(".", 1)[-1] == "project_rows"
                for candidate in ast.walk(tree)
            )
        )
        if projected_after_read and actual_db_projection:
            return True
    hash_projection = WRITER_HASH_BOUNDARIES.get(
        (relative, scope.rsplit(".", 1)[-1])
    )
    return bool(
        hash_projection
        and called.endswith(".read_bytes")
        and _inside_hash(call, parents)
        and any(
            isinstance(candidate, ast.Call)
            and _name(candidate.func).rsplit(".", 1)[-1] == hash_projection
            for candidate in ast.walk(tree)
        )
    )


def _is_sql(value: str) -> bool:
    normalized = " ".join(value.split()).upper()
    return bool(
        SQL_WRITE.match(value)
        or (normalized.startswith(("SELECT ", "WITH ")) and " FROM " in normalized)
    )


def audit_source(path: Path, source: str) -> list[Violation]:
    """Audit one source string; exposed for focused policy tests."""
    relative = _relative(path)
    tree = ast.parse(source, filename=relative)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations: list[Violation] = []
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    top_level_imports = {
        id(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    projector_aliases = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and str(node.module or "").endswith("deep_context.db.projectors")
        for alias in node.names
        if alias.name in PROJECTOR_CALL_BOUNDARIES
    }
    import_block_closed = False

    def add(node: ast.AST, rule: str, detail: str) -> None:
        violations.append(Violation(relative, getattr(node, "lineno", 1), rule, detail))

    for node in tree.body:
        is_docstring = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if import_block_closed:
                add(node, "top-level-imports", "import appears after executable module code")
        elif not is_docstring:
            import_block_closed = True

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top_level_imports:
            add(node, "top-level-imports", "import is nested instead of module-top")

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [str(node.module or "")]
            )
            if "sqlite3" in names and DB_PACKAGE not in path.parents:
                add(node, "sqlite-home", "sqlite3 is allowed only in deep_context/db")

        if isinstance(node, ast.Call):
            called = _name(node.func)
            if _is_csv_reader(node) and path != LEGACY_READER:
                add(node, "one-legacy-csv-reader", "only db/legacy.py may parse CSV")
            if called.rsplit(".", 1)[-1] in FORBIDDEN_HELPERS:
                add(node, "no-file-state-helper", called)
            method = called.rsplit(".", 1)[-1]
            direct_read = (
                method in DIRECT_FILE_READ_METHODS
                or called == "json.load"
                or _opens_for_read(node, called)
            )
            if direct_read and not _allowed_file_read(path, relative, node, parents, tree):
                add(
                    node,
                    "artifact-file-read",
                    f"{called or ast.unparse(node.func)} in {_scope(node, parents) or '<module>'}",
                )
            if (
                method in KNOWN_READER_HELPERS
                and path != LEGACY_READER
                and (relative, _scope(node, parents))
                not in READER_HELPER_BOUNDARIES.get(method, set())
            ):
                add(node, "artifact-reader-call", called)
            projector = projector_aliases.get(called)
            if projector is None and method in PROJECTOR_CALL_BOUNDARIES:
                projector = method
            if (
                projector is not None
                and path not in {LEGACY_READER, PROJECTOR_READER}
                and (relative, _scope(node, parents))
                not in PROJECTOR_CALL_BOUNDARIES[projector]
            ):
                add(
                    node,
                    "projector-boundary",
                    f"{projector} called from {_scope(node, parents) or '<module>'}",
                )

        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id == "dict" for base in node.bases
        ):
            add(node, "no-payload-dict-shim", node.name)

        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if DB_PACKAGE not in path.parents and _is_sql(node.value):
                add(node, "sql-home", "SQL text is allowed only in deep_context/db")

    if path != LEGACY_READER:
        lowered = source.lower()
        for token in FORBIDDEN_STATE_TEXT:
            for match in re.finditer(rf"\b{re.escape(token)}\b", lowered):
                line = lowered.count("\n", 0, match.start()) + 1
                violations.append(Violation(relative, line, "deleted-control-state", token))
    return sorted(violations, key=lambda item: (item.line, item.rule, item.detail))


def audit_file(path: Path) -> list[Violation]:
    return audit_source(path, path.read_text(encoding="utf-8"))


def audit() -> list[Violation]:
    violations = [
        item
        for path in sorted(PACKAGE.rglob("*.py"))
        for item in audit_file(path)
    ]
    csv_readers: list[Path] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and _is_csv_reader(node)
            for node in ast.walk(tree)
        ):
            csv_readers.append(path)
    if csv_readers != [LEGACY_READER]:
        violations.append(Violation(
            _relative(PACKAGE),
            1,
            "exactly-one-legacy-reader",
            ", ".join(_relative(path) for path in csv_readers) or "none",
        ))
    return sorted(violations, key=lambda item: (item.path, item.line, item.rule))


def main() -> int:
    violations = audit()
    print(json.dumps({
        "scope": _relative(PACKAGE),
        "status": "ok" if not violations else "failed",
        "violations": [asdict(item) for item in violations],
    }, indent=2))
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
