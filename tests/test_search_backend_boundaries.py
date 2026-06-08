import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
LOCAL = PRIMITIVES / "local"
TURBOPUFFER = PRIMITIVES / "turbopuffer"


class SearchBackendImportBoundaryTests(unittest.TestCase):
    def run_import_script(self, code: str, *, env: dict[str, str] | None = None) -> None:
        full_env = os.environ.copy()
        full_env.update(env or {})
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=full_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"import boundary script failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def test_turbopuffer_pipeline_imports_without_local_backend_modules(self) -> None:
        code = textwrap.dedent(f"""
            import importlib
            import os
            import sys
            from pathlib import Path

            os.environ.pop('POWERPACKS_LOCAL_SEARCH_DB', None)

            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)
            sys.path.insert(0, {str(PRIMITIVES / 'execute_role_search')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'apply_prefilters')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'count_candidates')!r})

            blocked = {{'local_search_backend', 'local_search_verticals', 'local_resolve_companies', 'local_resolve_education', 'local_duckdb_store'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked local module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            for module in [
                'search_common',
                'search_embeddings',
                'search_result_merge',
                'turbopuffer_search_backend',
                'turbopuffer_resolve_companies',
                'turbopuffer_resolve_education',
                'execute_role_search',
                'apply_prefilters',
                'count_candidates',
            ]:
                importlib.import_module(module)
        """)
        self.run_import_script(code)

    def test_local_pipeline_imports_without_turbopuffer_client_module(self) -> None:
        code = textwrap.dedent(f"""
            import importlib
            import os
            import sys
            from pathlib import Path

            os.environ['POWERPACKS_LOCAL_SEARCH_DB'] = '/var/tmp/powerpacks-boundary-import-test.duckdb'

            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)
            sys.path.insert(0, {str(PRIMITIVES / 'execute_role_search')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'apply_prefilters')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'count_candidates')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'local_search_pipeline')!r})
            sys.path.insert(0, {str(PRIMITIVES / 'local_duckdb')!r})

            blocked = {{'turbopuffer_search_backend', 'turbopuffer_resolve_companies', 'turbopuffer_resolve_education'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked remote module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            for module in [
                'search_common',
                'search_embeddings',
                'search_result_merge',
                'local_search_backend',
                'local_search_verticals',
                'local_resolve_companies',
                'local_resolve_education',
                'execute_role_search',
                'apply_prefilters',
                'count_candidates',
                'local_search_pipeline',
                '_dispatch',
            ]:
                importlib.import_module(module)
        """)
        self.run_import_script(code)

    def test_local_runtime_works_without_turbopuffer_client_module(self) -> None:
        code = textwrap.dedent(f"""
            import asyncio
            import importlib
            import os
            import sys
            import tempfile
            from pathlib import Path

            sys.path.insert(0, {str(ROOT)!r})

            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)
            from tests.test_local_search_pipeline import write_local_search_db

            blocked = {{'turbopuffer_search_backend', 'turbopuffer_resolve_companies', 'turbopuffer_resolve_education'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked remote module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            import local_search_backend as local_backend
            from search_common import filters_from_role_payload

            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / 'local-search.duckdb'
                write_local_search_db(db)
                os.environ.pop('POWERPACKS_LOCAL_SEARCH_DB', None)
                os.environ['POWERPACKS_DEFAULT_SET_ID'] = 'must-not-be-used-in-local-mode'
                local_backend.configure_local_backend(db)
                local_backend._local_store_for_path.cache_clear()
                payload = {{
                    'bm25_queries': ['software engineer'],
                    'role_ids': ['software_engineer'],
                }}
                filters = filters_from_role_payload(payload)
                assert 'allowed_operator_ids' not in repr(filters), filters
                rows = asyncio.run(local_backend.hybrid_role_rows(
                    payload,
                    filters,
                    top_k=5,
                    include_attributes=['base_id', 'position_title'],
                ))
                assert rows, 'expected local DuckDB runtime rows'
                assert rows[0].get('person_id'), rows[0]
        """)
        self.run_import_script(code)

    def test_local_resolvers_run_without_turbopuffer_client_module(self) -> None:
        code = textwrap.dedent(f"""
            import asyncio
            import json
            import os
            import sys
            import tempfile
            from pathlib import Path
            from types import SimpleNamespace

            sys.path.insert(0, {str(ROOT)!r})

            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)
            from tests.test_local_duckdb_backend import LocalDuckDBBackendTests

            blocked = {{'turbopuffer_search_backend', 'turbopuffer_resolve_companies', 'turbopuffer_resolve_education'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked remote module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            import local_search_backend as local_backend
            import local_resolve_companies as resolve_companies
            import local_resolve_education as resolve_education

            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / 'local-search.duckdb'
                LocalDuckDBBackendTests._create_fixture(object.__new__(LocalDuckDBBackendTests), str(db))
                local_backend.configure_local_backend(db)
                local_backend._local_store_for_path.cache_clear()
                company_out = asyncio.run(resolve_companies.run(SimpleNamespace(
                    state=None,
                    payload_json=json.dumps({{'company_names': ['InfraDB']}}),
                    env_file=None,
                    name_top_k=5,
                    semantic_top_k=5,
                    page_size=1000,
                    max_companies=10,
                    max_soft_companies=10,
                    company_sector_strategy='hard_filter',
                    company_sector_min_results=20,
                    ce_threshold=999999,
                    no_ce=True,
                    ce_all=False,
                    ce_top_n=0,
                    ce_model='unused',
                    ce_batch_size=10,
                    ce_concurrency=1,
                )))
                assert 'company-infra' in company_out['company_ids'], company_out
                edu_out = asyncio.run(resolve_education.run(SimpleNamespace(
                    state=None,
                    payload_json=json.dumps({{'education_names': ['Stanford University']}}),
                    env_file=None,
                    max_rows_per_name=10,
                )))
                assert 'school-stanford' in edu_out['education_ids'], edu_out
        """)
        self.run_import_script(code)

    def test_turbopuffer_runtime_works_without_local_backend_modules(self) -> None:
        code = textwrap.dedent(f"""
            import asyncio
            import os
            import sys
            from types import SimpleNamespace

            os.environ.pop('POWERPACKS_LOCAL_SEARCH_DB', None)

            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)

            blocked = {{'local_search_backend', 'local_search_verticals', 'local_resolve_companies', 'local_resolve_education', 'local_duckdb_store'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked local module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            import turbopuffer_search_backend as turbopuffer_client

            class FakeNamespace:
                def query(self, **kwargs):
                    return SimpleNamespace(rows=[SimpleNamespace(id='person-1-1', base_id='person-1', position_title='Engineer')])

            class FakeClient:
                def namespace(self, name):
                    return FakeNamespace()

            turbopuffer_client.client = lambda: FakeClient()
            rows = asyncio.run(turbopuffer_client.filter_only_rows(
                ('id', 'NotEq', '__never__'),
                ['base_id', 'position_title'],
                page_size=5,
                max_results=1,
            ))
            assert rows == [{{'id': 'person-1-1', 'base_id': 'person-1', 'position_title': 'Engineer'}}], rows
        """)
        self.run_import_script(code)

    def test_turbopuffer_resolvers_run_without_local_backend_modules(self) -> None:
        code = textwrap.dedent(f"""
            import asyncio
            import json
            import os
            import sys
            from types import SimpleNamespace

            os.environ.pop('POWERPACKS_LOCAL_SEARCH_DB', None)
            for path in [{str(LIB)!r}, {str(SHARED)!r}, {str(LOCAL)!r}, {str(TURBOPUFFER)!r}]:
                sys.path.insert(0, path)

            blocked = {{'local_search_backend', 'local_search_verticals', 'local_resolve_companies', 'local_resolve_education', 'local_duckdb_store'}}
            for name in list(sys.modules):
                if name.split('.')[0] in blocked:
                    sys.modules.pop(name, None)

            class Blocker:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        raise ImportError(f'blocked local module: {{fullname}}')
                    return None

            sys.meta_path.insert(0, Blocker())
            import turbopuffer_search_backend as turbopuffer_backend
            import turbopuffer_resolve_companies as resolve_companies
            import turbopuffer_resolve_education as resolve_education

            class FakeNamespace:
                def __init__(self, logical_name):
                    self.logical_name = logical_name

                def query(self, **kwargs):
                    if self.logical_name == 'companies':
                        rows = [SimpleNamespace(id='company-1', company_name='Acme AI', score=1.0)]
                    elif self.logical_name == 'schools':
                        rows = [SimpleNamespace(id='school-stanford', school_name='Stanford University', display_value='Stanford University', person_count=1000, score=1.0)]
                    else:
                        rows = []
                    return SimpleNamespace(rows=rows)

                def multi_query(self, **kwargs):
                    return [self.query()]

            class FakeClient:
                def namespace(self, name):
                    logical = 'companies' if 'compan' in name else 'schools'
                    return FakeNamespace(logical)

            turbopuffer_backend.client = lambda: FakeClient()
            company_out = asyncio.run(resolve_companies.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({{'company_names': ['Acme AI']}}),
                env_file=None,
                name_top_k=5,
                semantic_top_k=5,
                page_size=1000,
                max_companies=10,
                max_soft_companies=10,
                company_sector_strategy='hard_filter',
                company_sector_min_results=20,
                ce_threshold=999999,
                no_ce=True,
                ce_all=False,
                ce_top_n=0,
                ce_model='unused',
                ce_batch_size=10,
                ce_concurrency=1,
            )))
            assert 'company-1' in company_out['company_ids'], company_out
            edu_out = asyncio.run(resolve_education.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({{'education_names': ['Stanford University']}}),
                env_file=None,
                max_rows_per_name=10,
            )))
            assert 'school-stanford' in edu_out['education_ids'], edu_out
        """)
        self.run_import_script(code)


if __name__ == "__main__":
    unittest.main()
