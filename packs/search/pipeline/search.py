"""Sole explicit backend composition root for typed search."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    ROOT = Path(__file__).resolve().parents[3]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from packs.search.pipeline.artifacts import persist_result
    from packs.search.pipeline.gtm import run_with_runner
    from packs.search.pipeline.models import Backend, LocalCorpus, PowersetCorpus, SearchSpec
else:
    from .artifacts import persist_result
    from .gtm import run_with_runner
    from .models import Backend, LocalCorpus, PowersetCorpus, SearchSpec


def run_search(spec: SearchSpec, *, output_dir: str | Path | None = None) -> Any:
    if output_dir is not None:
        repository = Path(__file__).resolve().parents[3]
        allowed = (repository / ".powerpacks" / "search-runs").resolve()
        resolved_output = Path(output_dir).resolve()
        if resolved_output != allowed and allowed not in resolved_output.parents:
            raise ValueError("search output_dir must be under .powerpacks/search-runs")
    if spec.backend == Backend.LOCAL:
        from packs.search.backends.local.runner import LocalSearchRunner

        assert isinstance(spec.corpus, LocalCorpus)
        runner = LocalSearchRunner(spec.corpus.db_path)
        lookup_observation = {"source": "lookup_spec", "backend": "local"}
        if spec.profile.value == "lookup":
            result = replace(
                run_with_runner(spec, runner, artifact_root=str(output_dir) if output_dir is not None else None),
                corpus_observation=lookup_observation,
            )
            if output_dir is not None:
                paths = persist_result(output_dir, spec, result)
                result = replace(result, artifact_paths={**result.artifact_paths, **paths})
            return result
        evidence_person_ids = (
            spec.recruiting.review_pool_person_ids
            if spec.recruiting is not None
            else ()
        )
        identity = runner.snapshot_corpus("local", evidence_person_ids, spec=spec)
        derived = LocalCorpus(
            spec.corpus.db_path,
            identity["scoped_records_hash"],
            __import__("packs.search.reflect.snapshots", fromlist=["canonical_hash"]).canonical_hash(
                identity["namespace_schema_hashes"]
            ),
            identity["membership_hash"],
        )
        for name in ("content_hash", "schema_hash", "membership_hash"):
            supplied = getattr(spec.corpus, name)
            if supplied is not None and supplied != getattr(derived, name):
                raise ValueError(f"supplied local {name} does not match the selected DuckDB")
        spec = replace(spec, corpus=derived)
        corpus_observation = identity
    else:
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner

        assert isinstance(spec.corpus, PowersetCorpus)
        runner = TurboPufferSearchRunner(spec.corpus)
        lookup_observation = {
            "source": "lookup_spec",
            "backend": "powerset",
            "set_id": spec.corpus.set_id,
            "operator_scope_hash": spec.corpus.operator_scope_hash,
        }
        if spec.profile.value == "lookup":
            result = replace(
                run_with_runner(spec, runner, artifact_root=str(output_dir) if output_dir is not None else None),
                corpus_observation={key: value for key, value in lookup_observation.items() if value is not None},
            )
            if output_dir is not None:
                paths = persist_result(output_dir, spec, result)
                result = replace(result, artifact_paths={**result.artifact_paths, **paths})
            return result
        evidence_person_ids = (
            spec.recruiting.review_pool_person_ids
            if spec.recruiting is not None
            else ()
        )
        identity = runner.snapshot_corpus(spec.corpus.set_id, evidence_person_ids, spec=spec)
        spec = replace(
            spec,
            corpus=PowersetCorpus(
                set_id=identity["set_id"],
                operator_ids=spec.corpus.operator_ids,
                operator_scope_hash=identity["operator_scope_hash"],
                membership_hash=identity["membership_hash"],
                namespace_schema_hashes=identity["namespace_schema_hashes"],
                native_content_version=identity.get("native_content_version"),
                scoped_records_hash=identity.get("scoped_records_hash"),
            ),
        )
        runner.corpus = spec.corpus
        corpus_observation = identity
    if spec.profile.value == "recruiting":
        from packs.search.pipeline.recruiting import run_recruiting

        result = run_recruiting(
            spec,
            runner,
            artifact_root=str(output_dir) if output_dir is not None else None,
            corpus_snapshot=identity,
        )
    else:
        result = run_with_runner(spec, runner, artifact_root=str(output_dir) if output_dir is not None else None)
    result = replace(
        result,
        corpus_observation={
            key: corpus_observation[key]
            for key in ("verification_status", "source", "observed_at")
            if key in corpus_observation
        },
    )
    if output_dir is not None:
        paths = persist_result(output_dir, spec, result)
        result = replace(result, artifact_paths={**result.artifact_paths, **paths})
    return result


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run a persisted typed SearchSpec")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.spec.read_text())
    import jsonschema

    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "search-spec.schema.json"
    jsonschema.validate(raw, json.loads(schema_path.read_text()))
    spec = SearchSpec.from_dict(raw)
    result = run_search(spec, output_dir=args.output_dir)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
