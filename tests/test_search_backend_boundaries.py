import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TypedSearchBackendImportBoundaryTests(unittest.TestCase):
    def _run(self, code: str) -> None:
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_runner_imports_with_remote_modules_blocked(self) -> None:
        self._run(
            """
import builtins
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if 'turbopuffer' in name or name == 'postgres_client':
        raise ModuleNotFoundError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
from packs.search.backends.local.runner import LocalSearchRunner
assert LocalSearchRunner
"""
        )

    def test_remote_runner_imports_with_local_modules_blocked(self) -> None:
        self._run(
            """
import builtins
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if 'duckdb' in name or 'local_duckdb' in name:
        raise ModuleNotFoundError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
assert TurboPufferSearchRunner
"""
        )

    def test_composition_root_does_not_eager_import_runners(self) -> None:
        self._run(
            """
import sys
import packs.search.pipeline.search
assert 'packs.search.backends.local.runner' not in sys.modules
assert 'packs.search.backends.turbopuffer.runner' not in sys.modules
"""
        )

    def test_typed_local_runner_uses_only_public_store_connection_door(self) -> None:
        source = (ROOT / "packs/search/backends/local/runner.py").read_text()
        for forbidden in ("import duckdb", "duckdb.connect", "store.conn", "store._"):
            self.assertNotIn(forbidden, source)

    def test_local_execution_ignores_ambient_remote_scope_and_cannot_fallback(self) -> None:
        self._run(
            """
import builtins
import os
import tempfile
from pathlib import Path

os.environ.pop('POWERPACKS_LOCAL_SEARCH_DB', None)
os.environ['POWERPACKS_DEFAULT_SET_ID'] = 'ambient-set'
os.environ['POWERSET_DEFAULT_SET_ID'] = 'ambient-set-2'
os.environ['POWERPACKS_DEFAULT_OPERATOR_ID'] = 'ambient-operator'

real = builtins.__import__
def blocked(name, *args, **kwargs):
    if 'turbopuffer' in name or name == 'postgres_client' or name.endswith('.postgres_client'):
        raise AssertionError(f'remote access attempted: {name}')
    return real(name, *args, **kwargs)
builtins.__import__ = blocked

from packs.search.pipeline.models import Backend, LocalCorpus, LookupSpec, PersonFilters, Profile, RoleIntent, SearchBounds, SearchSpec
from packs.search.pipeline.search import run_search
from tests.local_search_fixture import PERSON_STANFORD, write_local_search_db

with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / 'local.duckdb'
    write_local_search_db(db)
    lookup = SearchSpec(
        'search.spec.v1', 'synthetic lookup', Profile.LOOKUP, Backend.LOCAL,
        LocalCorpus(str(db)), lookup=LookupSpec('person_id', PERSON_STANFORD),
        bounds=SearchBounds(20, 20, 20),
    )
    result = run_search(lookup)
    assert result.status == 'completed'
    assert result.frontier.candidates[0].person_id == PERSON_STANFORD
    assert not ({'set_id', 'operator_ids', 'allowed_operator_ids'} & set(lookup.corpus.to_dict()))

    empty = SearchSpec(
        'search.spec.v1', 'synthetic empty local search', Profile.GTM, Backend.LOCAL,
        LocalCorpus(str(db)), role=RoleIntent(('software_engineer',), (), ('software engineer',)),
        person_filters=PersonFilters(cities=('Nowhere',), is_current_role=True),
        bounds=SearchBounds(20, 20, 20),
    )
    assert run_search(empty).status == 'completed_empty'
"""
        )

    def test_remote_runner_is_reconstructed_with_final_corpus_and_snapshot_schemas(self) -> None:
        source = (ROOT / "packs/search/pipeline/search.py").read_text()
        self.assertNotIn("runner.corpus =", source)


if __name__ == "__main__":
    unittest.main()
