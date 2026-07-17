import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
LOCAL = PRIMITIVES / "local"
TURBOPUFFER = PRIMITIVES / "turbopuffer"
for _path in [LIB, SHARED, LOCAL, TURBOPUFFER]:
    sys.path.insert(0, str(_path))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


local_backend = load_module("local_search_backend", LOCAL / "local_search_backend.py")
search_common = load_module("search_common", SHARED / "search_common.py")
turbopuffer_client = load_module("turbopuffer_search_backend", TURBOPUFFER / "turbopuffer_search_backend.py")
local_filter_eval = load_module("local_filter_eval", LOCAL / "local_filter_eval.py")
apply_prefilters = load_module("apply_prefilters", ROOT / "packs/search/primitives/apply_prefilters" / "apply_prefilters.py")
resolve_education = load_module("local_resolve_education", LOCAL / "local_resolve_education.py")
execute_role_search = load_module("execute_role_search", ROOT / "packs/search/primitives/execute_role_search" / "execute_role_search.py")
resolve_companies = load_module("local_resolve_companies", LOCAL / "local_resolve_companies.py")
hydrate_people = load_module("hydrate_people", ROOT / "packs/search/primitives/hydrate_people" / "hydrate_people.py")
build_local_duckdb_shim = load_module("build_local_duckdb_shim", ROOT / "scripts" / "build-local-duckdb-shim.py")


LONG_FOUNDER_QUERY = (
    "Started and built a company from scratch as a founder or co-founder, hired early teams, "
    "owned company-building outcomes, raised capital, and led strategy for a startup."
)
LONG_BACKEND_QUERY = (
    "Builds production backend software systems, APIs, data infrastructure, distributed services, "
    "and scalable platform engineering with hands-on coding responsibilities."
)


def write_records(path: Path, rows: list[dict]) -> None:
    table_by_name = {
        Path(rel).name: table
        for table, rel in {
            **build_local_duckdb_shim.LOCAL_TABLES,
            **build_local_duckdb_shim.OPTIONAL_LOCAL_TABLES,
            "local_person_profiles": build_local_duckdb_shim.PERSON_PROFILE_RECORD,
        }.items()
    }
    table = table_by_name[path.name]
    build_local_duckdb_shim.write_parquet_rows(
        path,
        rows,
        float_array_fields=("vector",),
        schema=build_local_duckdb_shim.ALL_LOCAL_TABLE_CONTRACT[table],
    )


def write_people_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["id,public_identifier,linkedin_url,full_name,headline,summary,city,state,country,work_experiences,education,source_channels"]
    for slug, full_name in rows:
        lines.append(f"{slug},{slug},https://www.linkedin.com/in/{slug},{full_name},Headline,Summary,San Francisco,CA,US,[],[],test")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_shim_json(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build-local-duckdb-shim.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"shim failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


def query_duckdb(db_path: Path, sql: str):
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


class LocalDuckDBFixtureMixin:
    def setUp(self) -> None:
        self._old_env = {key: os.environ.get(key) for key in ["POWERPACKS_LOCAL_SEARCH_DB", "POWERPACKS_ENABLE_LEGACY_LOCAL_SEARCH_ENV", "POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH"]}
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "local-search.duckdb")
        self._create_fixture(self.db_path)
        os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
        os.environ.pop("POWERPACKS_ENABLE_LEGACY_LOCAL_SEARCH_ENV", None)
        os.environ.pop("POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH", None)
        local_backend.configure_local_backend(self.db_path)
        local_backend._local_store_for_path.cache_clear()

    def tearDown(self) -> None:
        local_backend.configure_local_backend(None)
        local_backend._local_store_for_path.cache_clear()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def _create_fixture(self, db_path: str) -> None:
        import duckdb

        con = duckdb.connect(db_path)
        try:
            con.execute(
                """
                create table local_people_positions (
                    id varchar,
                    position_id varchar,
                    person_id varchar,
                    base_id varchar,
                    position_title varchar,
                    company_id varchar,
                    company_name varchar,
                    city varchar,
                    state varchar,
                    country varchar,
                    macro_region varchar,
                    metro_areas varchar[],
                    role_track varchar,
                    seniority_band varchar,
                    role_ids varchar[],
                    is_current boolean,
                    total_years_experience integer,
                    tenure_years double,
                    allowed_operator_ids varchar[],
                    start_date_epoch integer,
                    end_date_epoch integer,
                    inferred_birth_year integer,
                    x_twitter_followers integer,
                    linkedin_followers integer,
                    linkedin_connections integer,
                    ig_followers integer,
                    phrase_tokens varchar[],
                    word_tokens varchar[],
                    char_tokens varchar[],
                    d2q_tokens varchar[],
                    vector double[]
                )
                """
            )
            people_rows = [
                (
                    "person-founder-0", "pos-founder-current", "person-founder", "person-founder",
                    "Founder and CEO", "company-startup", "Acme AI", "San Francisco", "California", "United States",
                    "North America", ["San Francisco Bay Area"], "founder", "c_suite", ["founder", "chief_executive_officer"], True,
                    12, 5.0, ["op1", "op-founder"], 1577836800, 0, 1988, 1000, 5000, 3000, 100,
                    ["founder", "founder ceo", "co founder", "startup founder"],
                    ["founder", "ceo", "startup", "company", "builder", "product"],
                    [" fo", "fou", "oun"], ["founder", "ceo", "startup"], [1.0, 0.0, 0.0],
                ),
                (
                    "person-founder-1", "pos-founder-past-product", "person-founder", "person-founder",
                    "Product Manager", "company-product", "Box", "San Francisco", "California", "United States",
                    "North America", ["San Francisco Bay Area"], "product", "manager", ["product_manager"], False,
                    12, 4.0, ["op1", "op-founder"], 1420070400, 1546300800, 1988, 1000, 5000, 3000, 100,
                    ["product manag", "product"], ["product", "manager", "roadmap", "experiments"],
                    [" pr", "pro", "rod"], ["product", "roadmap"], [0.4, 0.4, 0.0],
                ),
                (
                    "person-engineer-0", "pos-engineer-current", "person-engineer", "person-engineer",
                    "Backend Engineer", "company-infra", "InfraDB", "New York", "New York", "United States",
                    "North America", ["New York City Metropolitan Area"], "engineering", "senior", ["software_engineer", "backend_engineer"], True,
                    8, 4.0, ["op1", "op-eng"], 1609459200, 0, 1992, 120, 1500, 1200, 40,
                    ["backend engin", "softwar engin", "distribut system"],
                    ["backend", "engineer", "python", "distributed", "systems", "api", "services"],
                    [" ba", "bac", "ack"], ["backend", "distributed systems", "python"], [0.0, 1.0, 0.0],
                ),
            ]
            con.executemany("insert into local_people_positions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", people_rows)
            for ddl in [
                "alter table local_people_positions add column description varchar",
                "alter table local_people_positions add column dense_text varchar",
                "alter table local_people_positions add column company_domain varchar",
                "alter table local_people_positions add column company_linkedin_url varchar",
                "alter table local_people_positions add column company_description varchar",
                "alter table local_people_positions add column company_sector_types varchar[]",
                "alter table local_people_positions add column company_entity_types varchar[]",
                "alter table local_people_positions add column company_headcount bigint",
                "alter table local_people_positions add column company_funding_total double",
                "alter table local_people_positions add column company_stage varchar",
                "alter table local_people_positions add column investor_names varchar[]",
                "alter table local_people_positions add column title_hash varchar",
                "alter table local_people_positions add column raw_title varchar",
                "alter table local_people_positions add column role_type_category varchar",
            ]:
                con.execute(ddl)
            con.execute(
                """
                update local_people_positions
                set description = case
                        when id = 'person-engineer-0' then 'Built Python APIs and distributed database services.'
                        when id = 'person-founder-0' then 'Founded the company and led fundraising.'
                        else 'Managed product roadmap and experiments.'
                    end,
                    dense_text = case
                        when id = 'person-engineer-0' then 'Backend Engineer InfraDB Built Python APIs and distributed database services.'
                        when id = 'person-founder-0' then 'Founder and CEO Acme AI Founded the company and led fundraising.'
                        else 'Product Manager Box Managed product roadmap and experiments.'
                    end,
                    company_domain = case when company_id = 'company-infra' then 'infradb.example' else 'acme.example' end,
                    company_linkedin_url = case when company_id = 'company-infra' then 'https://www.linkedin.com/company/infradb' else 'https://www.linkedin.com/company/acme-ai' end,
                    company_description = case when company_id = 'company-infra' then 'Database infrastructure for software teams.' else 'AI products for teams.' end,
                    company_sector_types = case when company_id = 'company-infra' then ['developer_tools'] else ['ai'] end,
                    company_entity_types = case when company_id = 'company-infra' then ['developer_tool'] else ['company'] end,
                    company_headcount = case when company_id = 'company-infra' then 120 else 40 end,
                    company_funding_total = case when company_id = 'company-infra' then 25000000 else 5000000 end,
                    company_stage = case when company_id = 'company-infra' then 'growth' else 'seed' end,
                    investor_names = case when company_id = 'company-infra' then ['OpenAI Startup Fund'] else [] end,
                    title_hash = id || '-title',
                    raw_title = position_title,
                    role_type_category = role_track
                """
            )

            con.execute(
                """
                create table local_person_profiles (
                    id varchar,
                    person_id varchar,
                    base_id varchar,
                    public_identifier varchar,
                    linkedin_url varchar,
                    public_profile_url varchar,
                    first_name varchar,
                    last_name varchar,
                    full_name varchar,
                    headline varchar,
                    summary varchar,
                    city varchar,
                    state varchar,
                    country varchar,
                    location_raw varchar,
                    profile_picture_url varchar,
                    current_title varchar,
                    current_company varchar,
                    current_company_urn varchar,
                    primary_email varchar,
                    all_emails varchar[],
                    primary_phone varchar,
                    all_phones varchar[],
                    source_channels varchar[],
                    source_artifacts varchar[],
                    twitter_handle varchar,
                    x_twitter_handle varchar,
                    x_twitter_followers bigint,
                    linkedin_followers bigint,
                    linkedin_connections bigint,
                    ig_followers bigint,
                    inferred_birth_year bigint,
                    work_experiences json,
                    education json,
                    hydrated_context json,
                    interaction_counts json,
                    total_interactions bigint,
                    last_interaction varchar,
                    allowed_operator_ids varchar[]
                )
                """
            )
            con.executemany(
                "insert into local_person_profiles values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "person-founder", "person-founder", "person-founder", "founder-example",
                        "https://www.linkedin.com/in/founder-example", "https://www.linkedin.com/in/founder-example",
                        "Founder", "Example", "Founder Example", "Founder and CEO at Acme AI",
                        "Founder with product leadership background", "San Francisco", "California", "United States",
                        "San Francisco, California, United States", "", "Founder and CEO", "Acme AI", "company-startup",
                        "founder@example.com", ["founder@example.com"], "", [], ["linkedin"], ["fixture"],
                        "founder_x", "founder_x", 1000, 5000, 3000, 100, 1988,
                        json.dumps([{"title": "Founder and CEO", "company": "Acme AI"}]),
                        json.dumps([{"school": "Stanford University"}]),
                        json.dumps({"positions": [{"title": "Founder and CEO", "company": "Acme AI"}]}),
                        json.dumps({"gmail": 12}), 12, "2026-01-01T00:00:00+00:00",
                        ["op1", "op-founder"],
                    ),
                    (
                        "person-engineer", "person-engineer", "person-engineer", "engineer-example",
                        "https://www.linkedin.com/in/engineer-example", "https://www.linkedin.com/in/engineer-example",
                        "Engineer", "Example", "Engineer Example", "Backend Engineer at InfraDB",
                        "Backend engineer building Python services", "New York", "New York", "United States",
                        "New York, New York, United States", "", "Backend Engineer", "InfraDB", "company-infra",
                        "engineer@example.com", ["engineer@example.com"], "", [], ["linkedin"], ["fixture"],
                        "engineer_x", "engineer_x", 120, 1500, 1200, 40, 1992,
                        json.dumps([{"title": "Backend Engineer", "company": "InfraDB"}]),
                        json.dumps([{"school": "Massachusetts Institute of Technology"}]),
                        json.dumps({"positions": [{"title": "Backend Engineer", "company": "InfraDB"}]}),
                        None, 0, "",
                        ["op1", "op-eng"],
                    ),
                ],
            )

            con.execute(
                """
                create table local_summaries (
                    id varchar,
                    person_id varchar,
                    base_id varchar,
                    summary varchar,
                    tech_skills varchar[],
                    allowed_operator_ids varchar[],
                    summary_tokens varchar[],
                    phrase_tokens varchar[],
                    word_tokens varchar[],
                    vector double[]
                )
                """
            )
            con.executemany(
                "insert into local_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("person-founder", "person-founder", "person-founder", "Founder with product leadership background", ["Product", "Go"], ["op1"], ["founder", "product"], ["founder product"], ["founder", "product", "startup"], [1.0, 0.0, 0.0]),
                    ("person-engineer", "person-engineer", "person-engineer", "Backend engineer building Python services", ["Python", "DuckDB", "Kubernetes"], ["op1"], ["backend", "engineer", "python"], ["backend engin"], ["backend", "engineer", "python", "duckdb"], [0.0, 1.0, 0.0]),
                ],
            )

            con.execute(
                """
                create table local_people_education (
                    id varchar,
                    person_id varchar,
                    base_id varchar,
                    education_id varchar,
                    canonical_education_id varchar,
                    school_name varchar,
                    degree varchar,
                    degree_normalized varchar,
                    field_of_study varchar,
                    start_year integer,
                    end_year integer,
                    graduation_year integer,
                    school_canonical_key varchar,
                    allowed_operator_ids varchar[]
                )
                """
            )
            con.executemany(
                "insert into local_people_education values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("edu-founder-stanford", "person-founder", "person-founder", "edu-stanford", "school-stanford", "Stanford University", "BS", "Bachelors", "Computer Science", 2006, 2010, 2010, "stanford university", ["op1"]),
                    ("edu-engineer-mit", "person-engineer", "person-engineer", "edu-mit", "school-mit", "Massachusetts Institute of Technology", "MS", "Masters", "Electrical Engineering and Computer Science", 2012, 2014, 2014, "massachusetts institute of technology", ["op1"]),
                ],
            )

            con.execute(
                """
                create table local_education (
                    id varchar,
                    canonical_education_id varchar,
                    school_name_tokens varchar[],
                    school_name varchar,
                    display_value varchar,
                    canonical_key varchar,
                    person_count integer
                )
                """
            )
            con.executemany(
                "insert into local_education values (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("school-stanford", "school-stanford", ["stanford", "university"], "Stanford University", "Stanford University", "stanford university", 200000),
                    ("school-mit", "school-mit", ["massachusetts", "institute", "of", "technology", "mit"], "Massachusetts Institute of Technology", "MIT", "massachusetts institute of technology", 150000),
                ],
            )

            con.execute(
                """
                create table local_companies (
                    id varchar,
                    company_urn varchar,
                    company_name varchar,
                    aliases varchar,
                    name_aliases_text varchar,
                    semantic_text varchar,
                    doc2query varchar,
                    d2q_text varchar,
                    doc2query_text varchar,
                    entity_sector_text varchar,
                    word_text varchar,
                    entity_types varchar[],
                    sector_types varchar[],
                    technology_types varchar[],
                    customer_type varchar[],
                    investor_urns varchar[],
                    accelerators varchar[],
                    yc_batches varchar[],
                    stage varchar,
                    headcount integer,
                    funding_stage integer,
                    funding_total integer,
                    last_funding_at integer,
                    valuation integer,
                    founded_year integer,
                    city varchar,
                    state varchar,
                    country varchar,
                    metro_area varchar,
                    macro_region varchar,
                    website_domain varchar,
                    linkedin_url varchar,
                    logo_url varchar,
                    description varchar,
                    allowed_operator_ids varchar[],
                    vector double[]
                )
                """
            )
            con.executemany(
                "insert into local_companies values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "company-infra", "company-infra", "InfraDB", "InfraDB database infrastructure",
                        "InfraDB database infrastructure", "Database infrastructure and developer tooling for application data.",
                        "developer database tools", "developer database tools", "developer database tools", "data infrastructure developer tools",
                        "database infrastructure developer tools", ["company", "developer_tool"], ["data", "developer_tools"],
                        ["database", "infrastructure"], ["B2B"], ["investor-openai"], ["Techstars"], ["YC S20"], "growth",
                        120, 3, 25_000_000, 20240315, 100_000_000, 2020, "New York", "New York", "United States",
                        "New York City Metropolitan Area", "North America", "infradb.example",
                        "https://www.linkedin.com/company/infradb", "https://img.example/infradb.png",
                        "Builds hosted database infrastructure for software teams.", ["op1"], [0.0, 1.0, 0.0],
                    ),
                    (
                        "company-startup", "company-startup", "Acme AI", "Acme AI artificial intelligence",
                        "Acme AI artificial intelligence", "AI startup building productivity software.",
                        "ai productivity software", "ai productivity software", "ai productivity software", "artificial intelligence software",
                        "ai artificial intelligence productivity", ["company"], ["ai"],
                        ["ai"], ["B2B"], [], [], [], "seed",
                        40, 2, 5_000_000, 20230115, 20_000_000, 2021, "San Francisco", "California", "United States",
                        "San Francisco Bay Area", "North America", "acme.example",
                        "https://www.linkedin.com/company/acme-ai", "https://img.example/acme.png",
                        "Builds AI products for teams.", ["op1"], [1.0, 0.0, 0.0],
                    ),
                ],
            )
            con.execute(
                """
                create table local_company_signals (
                    id varchar,
                    company_id varchar,
                    company_urn varchar,
                    signals_text varchar,
                    summary varchar,
                    doc2query_text varchar,
                    signal_tokens varchar[],
                    signals_tokens varchar[],
                    summary_tokens varchar[],
                    word_tokens varchar[],
                    vector double[],
                    allowed_operator_ids varchar[]
                )
                """
            )
            con.executemany(
                "insert into local_company_signals values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "signal-infra", "company-infra", "company-infra",
                        "database infrastructure backend engineering hiring signal",
                        "InfraDB is hiring backend infrastructure engineers.",
                        "backend database infrastructure engineering",
                        ["database", "infrastructure", "backend"],
                        ["database", "infrastructure", "backend"],
                        ["backend", "engineers"],
                        ["backend", "engineer", "database", "infrastructure", "backend engineer"],
                        [0.0, 1.0, 0.0],
                        ["op1"],
                    )
                ],
            )
        finally:
            con.close()


class LocalDuckDBBackendTests(LocalDuckDBFixtureMixin, unittest.TestCase):
    def test_local_table_contract_fields_and_vectors(self) -> None:
        import duckdb

        con = duckdb.connect(self.db_path, read_only=True)
        try:
            for table, contract in build_local_duckdb_shim.LOCAL_TABLE_CONTRACT.items():
                columns = {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
                self.assertTrue(set(contract).issubset(columns), f"{table} missing {sorted(set(contract) - columns)}")
        finally:
            con.close()

        self.assertTrue(local_backend.local_namespace_has_vectors("people"))
        self.assertTrue(local_backend.local_namespace_has_vectors("summaries"))
        self.assertTrue(local_backend.local_namespace_has_vectors("companies"))

    def test_local_rich_fields_are_filterable_and_projectable(self) -> None:
        store = local_backend.local_store()
        original_rows_for_namespace = store._rows_for_namespace
        store._rows_for_namespace = lambda _logical_name: (_ for _ in ()).throw(AssertionError("filter path must query DuckDB SQL directly"))
        try:
            people = asyncio.run(local_backend.filter_only_rows_for_namespace(
                "people",
                ("role_ids", "ContainsAny", ["backend_engineer"]),
                ["base_id", "role_ids", "position_title"],
                page_size=1000,
                max_results=10,
            ))
        finally:
            store._rows_for_namespace = original_rows_for_namespace
        self.assertEqual([row["base_id"] for row in people], ["person-engineer"])
        self.assertIn("backend_engineer", people[0]["role_ids"])

        summaries = asyncio.run(local_backend.filter_only_rows_for_namespace(
            "summaries",
            ("tech_skills", "ContainsAny", ["DuckDB"]),
            ["base_id", "tech_skills"],
            page_size=1000,
            max_results=10,
        ))
        self.assertEqual([row["base_id"] for row in summaries], ["person-engineer"])
        self.assertIn("DuckDB", summaries[0]["tech_skills"])

        companies = asyncio.run(local_backend.filter_only_rows_for_namespace(
            "companies",
            ("And", [
                ("entity_types", "ContainsAny", ["developer_tool"]),
                ("sector_types", "ContainsAny", ["developer_tools"]),
                ("technology_types", "ContainsAny", ["database"]),
            ]),
            ["company_name", "entity_types", "sector_types", "technology_types", "customer_type", "metro_area", "macro_region"],
            page_size=1000,
            max_results=10,
        ))
        self.assertEqual([row["id"] for row in companies], ["company-infra"])
        self.assertEqual(companies[0]["company_name"], "InfraDB")
        self.assertEqual(companies[0]["customer_type"], ["B2B"])

    def test_classified_company_fields_filter_bm25_and_vector_search(self) -> None:
        rich = asyncio.run(local_backend.filter_only_rows_for_namespace(
            "companies",
            ("And", [
                ("entity_types", "ContainsAny", ["developer_tool"]),
                ("sector_types", "ContainsAny", ["developer_tools"]),
                ("technology_types", "ContainsAny", ["infrastructure"]),
                ("customer_type", "ContainsAny", ["B2B"]),
                ("investor_urns", "ContainsAny", ["investor-openai"]),
                ("accelerators", "ContainsAny", ["Techstars"]),
                ("funding_stage", "Gte", 3),
                ("funding_total", "Gte", 20_000_000),
                ("headcount", "Gte", 100),
                ("stage", "Eq", "growth"),
            ]),
            [
                "company_name", "entity_types", "sector_types", "technology_types", "customer_type",
                "investor_urns", "accelerators", "yc_batches", "funding_stage", "funding_total",
                "headcount", "stage", "founded_year", "last_funding_at", "valuation",
            ],
            page_size=1000,
            max_results=10,
        ))
        self.assertEqual([row["id"] for row in rich], ["company-infra"])
        self.assertEqual(rich[0]["technology_types"], ["database", "infrastructure"])
        self.assertEqual(rich[0]["customer_type"], ["B2B"])
        self.assertEqual(rich[0]["investor_urns"], ["investor-openai"])
        self.assertEqual(rich[0]["accelerators"], ["Techstars"])
        self.assertEqual(rich[0]["yc_batches"], ["YC S20"])
        self.assertEqual(rich[0]["stage"], "growth")
        self.assertEqual(rich[0]["funding_stage"], 3)
        self.assertEqual(rich[0]["headcount"], 120)

        for field, query in [
            ("name_aliases_text", "InfraDB database"),
            ("semantic_text", "developer tooling application data"),
            ("doc2query_text", "developer database tools"),
            ("entity_sector_text", "data infrastructure"),
        ]:
            response = local_backend.namespace("companies").query(
                rank_by=(field, "BM25", query),
                top_k=5,
                include_attributes=["company_name", field],
            )
            self.assertTrue(response.rows, field)
            self.assertEqual(response.rows[0].id, "company-infra", field)
            self.assertEqual(response.rows[0].company_name, "InfraDB", field)
            self.assertGreater(response.rows[0].score, 0.0, field)

        vector_response = local_backend.namespace("companies").query(
            rank_by=("vector", "kNN", [0.0, 1.0, 0.0]),
            top_k=2,
            include_attributes=["company_name", "entity_types", "sector_types", "technology_types"],
        )
        self.assertEqual(vector_response.rows[0].id, "company-infra")
        self.assertEqual(vector_response.rows[0].company_name, "InfraDB")
        self.assertGreater(vector_response.rows[0].score, 0.99)

        resolved = asyncio.run(resolve_companies.run(SimpleNamespace(
            state=None,
            payload_json=json.dumps({
                "entity_types": ["developer_tool"],
                "sector_types": ["developer_tools"],
                "technology_types": ["database"],
                "customer_types": ["B2B"],
                "investors": ["investor-openai"],
                "accelerators": ["Techstars"],
                "yc_batches": ["YC S20"],
                "stages": ["growth"],
                "funding_stage_min": "series_a",
                "funding_amount_min": 20_000_000,
                "headcount_min": 100,
                "operator_ids": ["op1"],
            }),
            env_file=None,
            name_top_k=10,
            semantic_top_k=10,
            company_sector_strategy="soft_union",
            company_sector_min_results=1,
            page_size=1000,
            max_soft_companies=0,
            max_companies=0,
            no_ce=True,
            ce_all=False,
            ce_threshold=500,
            ce_top_n=0,
            ce_model=None,
            ce_batch_size=20,
            ce_concurrency=10,
        )))
        self.assertEqual(resolved["namespace"], "local_companies")
        self.assertEqual(resolved["company_ids"], ["company-infra"])
        self.assertEqual(resolved["sample_companies"][0]["technology_types"], ["database", "infrastructure"])
        self.assertEqual(resolved["sample_companies"][0]["customer_type"], ["B2B"])
        self.assertEqual(resolved["sample_companies"][0]["funding_stage"], 3)
        self.assertEqual(resolved["sample_companies"][0]["stage"], "growth")

    def test_local_company_query_vector_does_not_call_embedding_api(self) -> None:
        original_embedding = resolve_companies.embedding

        async def fail_embedding(text: str):
            raise AssertionError("supplied company_query_vector must avoid embedding API calls")

        resolve_companies.embedding = fail_embedding
        os.environ.pop("POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH", None)
        try:
            resolved = asyncio.run(resolve_companies.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_query_vector": [0.0, 1.0, 0.0],
                    "operator_ids": ["op1"],
                }),
                env_file=None,
                name_top_k=10,
                semantic_top_k=10,
                company_sector_strategy="semantic_only",
                company_sector_min_results=1,
                page_size=1000,
                max_soft_companies=0,
                max_companies=0,
                no_ce=True,
                ce_all=False,
                ce_threshold=500,
                ce_top_n=0,
                ce_model=None,
                ce_batch_size=20,
                ce_concurrency=10,
            )))
        finally:
            resolve_companies.embedding = original_embedding

        self.assertTrue(resolved["used_semantic_search"])
        self.assertEqual(resolved["company_ids"][0], "company-infra")
        self.assertEqual(resolved["sample_companies"][0]["technology_types"], ["database", "infrastructure"])
        self.assertEqual(resolved["sample_companies"][0]["accelerators"], ["Techstars"])

    def test_build_duckdb_from_records_artifacts_and_resolve_company_locally(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            records = tmp / "normal-pipeline" / "records"
            write_records(records / "people.records.parquet", [
                {
                    "id": "pos-artifact-engineer",
                    "position_id": "pos-artifact-engineer",
                    "person_id": "person-artifact",
                    "base_id": "person-artifact",
                    "vector": [0.0, 1.0, 0.0],
                    "word_tokens": ["backend", "engineer", "backend engineer"],
                    "char_tokens": [" ba", "bac", "ack"],
                    "d2q_tokens": ["database", "infrastructure"],
                    "phrase_tokens": ["backend engin"],
                    "position_title": "Backend Engineer",
                    "seniority_band": "senior",
                    "company_id": "company-artifact-infra",
                    "company_name": "ArtifactDB",
                    "city": "New York",
                    "state": "New York",
                    "country": "United States",
                    "macro_region": "North America",
                    "is_current": True,
                    "total_years_experience": 7,
                    "start_date_epoch": 1609459200,
                    "end_date_epoch": 0,
                    "tenure_years": 3.5,
                    "role_track": "engineering",
                    "metro_areas": ["New York City Metropolitan Area"],
                    "allowed_operator_ids": ["op-artifact"],
                    "role_ids": ["software_engineer", "backend_engineer"],
                }
            ])
            write_records(records / "summaries.records.parquet", [
                {
                    "id": "person-artifact",
                    "person_id": "person-artifact",
                    "base_id": "person-artifact",
                    "summary": "Backend engineer using DuckDB and Python",
                    "summary_tokens": ["backend", "engineer", "duckdb"],
                    "tech_skills": ["DuckDB", "Python"],
                    "allowed_operator_ids": ["op-artifact"],
                    "word_tokens": ["backend", "duckdb"],
                    "phrase_tokens": ["backend engin"],
                    "vector": [0.0, 1.0, 0.0],
                }
            ])
            write_records(records / "companies.records.parquet", [
                {
                    "id": "company-artifact-infra",
                    "company_urn": "company-artifact-infra",
                    "vector": [0.0, 1.0, 0.0],
                    "company_name": "ArtifactDB",
                    "name_aliases_text": "ArtifactDB database infrastructure developer tools",
                    "semantic_text": "Database infrastructure and developer tools for software teams.",
                    "doc2query_text": "database infrastructure developer tools backend data platform",
                    "entity_sector_text": "database infrastructure developer tools",
                    "description": "Builds local-first database tooling.",
                    "headcount": 42,
                    "funding_stage": 2,
                    "funding_total": 5000000,
                    "city": "New York",
                    "state": "New York",
                    "country": "United States",
                    "metro_area": "New York City Metropolitan Area",
                    "macro_region": "North America",
                    "entity_types": ["developer_tool"],
                    "sector_types": ["developer_tools"],
                    "technology_types": ["database"],
                    "customer_type": ["B2B"],
                    "investor_urns": ["investor-artifact"],
                    "yc_batches": [],
                    "founded_year": 2022,
                    "last_funding_at": 20240115,
                    "valuation": 20000000,
                    "allowed_operator_ids": ["op-artifact"],
                }
            ])
            write_records(records / "education.records.parquet", [
                {
                    "id": "edu-artifact",
                    "person_id": "person-artifact",
                    "base_id": "person-artifact",
                    "education_id": "school-artifact",
                    "canonical_education_id": "school-artifact",
                    "school_name": "Artifact University",
                    "degree": "BS",
                    "degree_normalized": "Bachelors",
                    "field_of_study": "Computer Science",
                    "start_year": 2010,
                    "end_year": 2014,
                    "graduation_year": 2014,
                    "allowed_operator_ids": ["op-artifact"],
                }
            ])
            write_records(records / "schools.records.parquet", [
                {"id": "school-artifact", "canonical_education_id": "school-artifact", "school_name": "Artifact University", "display_value": "Artifact University", "person_count": 1}
            ])

            payload = run_shim_json(
                "--records-dir", str(records),
                "--output-dir", str(tmp / ".powerpacks/search-index"),
                "--operator-id", "op-artifact",
                "--operator-email", "artifact@example.com",
                "--force",
            )
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["tables"]["local_companies"], 1)
            self.assertEqual(payload["tables"]["local_people_positions"], 1)

            os.environ.pop("POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH", None)
            previous_db = local_backend.explicit_local_backend_path()
            local_backend.configure_local_backend(payload["duckdb"])
            local_backend._local_store_for_path.cache_clear()
            original_embedding = resolve_companies.embedding

            async def fail_embedding(text: str):
                raise AssertionError("artifact-backed local company lookup should not call OpenAI by default")

            resolve_companies.embedding = fail_embedding
            try:
                people_knn = local_backend.namespace("people").query(
                    rank_by=("vector", "kNN", [0.0, 1.0, 0.0]),
                    top_k=1,
                    include_attributes=["base_id", "position_title", "role_ids"],
                )
                company_knn = local_backend.namespace("companies").query(
                    rank_by=("vector", "kNN", [0.0, 1.0, 0.0]),
                    top_k=1,
                    include_attributes=["company_name", "entity_types", "sector_types"],
                )
                out = asyncio.run(resolve_companies.run(SimpleNamespace(
                    state=None,
                    payload_json=json.dumps({
                        "company_semantic_queries": ["database infrastructure developer tools"],
                        "company_sector_strategy": "semantic_only",
                        "operator_ids": ["op-artifact"],
                    }),
                    env_file=None,
                    name_top_k=10,
                    semantic_top_k=10,
                    company_sector_strategy="semantic_only",
                    company_sector_min_results=1,
                    page_size=1000,
                    max_soft_companies=0,
                    max_companies=0,
                    no_ce=True,
                    ce_all=False,
                    ce_threshold=500,
                    ce_top_n=0,
                    ce_model=None,
                    ce_batch_size=20,
                    ce_concurrency=10,
                )))
            finally:
                resolve_companies.embedding = original_embedding
                local_backend.configure_local_backend(previous_db)
                local_backend._local_store_for_path.cache_clear()

            self.assertEqual(people_knn.rows[0].id, "pos-artifact-engineer")
            self.assertEqual(people_knn.rows[0].position_title, "Backend Engineer")
            self.assertEqual(company_knn.rows[0].id, "company-artifact-infra")
            self.assertEqual(company_knn.rows[0].company_name, "ArtifactDB")
            self.assertEqual(out["namespace"], "local_companies")
            self.assertEqual(out["company_ids"], ["company-artifact-infra"])
            self.assertEqual(out["sample_companies"][0]["entity_types"], ["developer_tool"])

    def test_hard_filters_distinguish_founder_and_engineer(self) -> None:
        founder_payload = {
            "cities": ["San Francisco"],
            "states": ["California"],
            "role_ids": ["founder"],
            "company_ids": ["company-startup"],
            "is_current_company": True,
            "operator_ids": ["op-founder"],
        }
        filters = search_common.filters_from_role_payload(founder_payload)
        rows = asyncio.run(local_backend.filter_only_rows_for_namespace("people", filters, ["base_id", "position_title", "role_track"]))
        self.assertEqual([row["base_id"] for row in rows], ["person-founder"])
        self.assertEqual(rows[0]["position_title"], "Founder and CEO")

        engineer_filters = search_common.filters_from_role_payload({
            "cities": ["New York"],
            "role_tracks": ["engineering"],
            "is_current_role": True,
            "operator_ids": ["op-eng"],
        })
        engineer_rows = asyncio.run(local_backend.filter_only_rows_for_namespace("people", engineer_filters, ["base_id", "position_title"]))
        self.assertEqual([row["base_id"] for row in engineer_rows], ["person-engineer"])

    def test_person_location_fields_are_or_grouped(self) -> None:
        payload = {
            "cities": ["San Francisco"],
            "metro_areas": ["New York City Metropolitan Area"],
            "is_current_role": True,
        }
        filters = search_common.filters_from_role_payload(payload)

        self.assertEqual(filters[0], "And")
        location = next(clause for clause in filters[1] if clause[0] == "Or")
        self.assertIn(("city", "In", ["San Francisco"]), location[1])
        self.assertIn(("metro_areas", "ContainsAny", ["New York City Metropolitan Area"]), location[1])

        rows = asyncio.run(local_backend.filter_only_rows_for_namespace(
            "people",
            filters,
            ["base_id", "position_title"],
        ))
        self.assertEqual({row["base_id"] for row in rows}, {"person-founder", "person-engineer"})

    def test_social_filters_are_duckdb_sql_and_combine_with_base_ids(self) -> None:
        store = local_backend.local_store()
        original_rows_for_namespace = store._rows_for_namespace
        store._rows_for_namespace = lambda _logical_name: (_ for _ in ()).throw(AssertionError("social filters must query DuckDB SQL directly"))
        try:
            high_linkedin_filters = search_common.filters_from_role_payload({
                "base_candidate_ids": ["person-founder", "person-engineer"],
                "li_followers_min": 4000,
                "is_current_role": True,
            })
            high_rows = asyncio.run(local_backend.filter_only_rows_for_namespace(
                "people",
                high_linkedin_filters,
                ["base_id", "position_title", "linkedin_followers"],
            ))
            low_x_filters = search_common.filters_from_role_payload({
                "base_candidate_ids": ["person-founder", "person-engineer"],
                "x_followers_max": 200,
                "is_current_role": True,
            })
            low_rows = asyncio.run(local_backend.filter_only_rows_for_namespace(
                "people",
                low_x_filters,
                ["base_id", "position_title", "x_twitter_followers"],
            ))
        finally:
            store._rows_for_namespace = original_rows_for_namespace

        self.assertEqual([row["base_id"] for row in high_rows], ["person-founder"])
        self.assertEqual(high_rows[0]["linkedin_followers"], 5000)
        self.assertEqual([row["base_id"] for row in low_rows], ["person-engineer"])
        self.assertEqual(low_rows[0]["x_twitter_followers"], 120)

    def test_education_prefilter_feeds_role_search(self) -> None:
        payload = {"education_ids": ["school-stanford"], "degree_levels": ["bachelors"], "fields_of_study": ["computer science"]}
        base_ids, meta = asyncio.run(apply_prefilters.education_base_ids(payload, page_size=1000, max_ids=10))
        self.assertEqual(base_ids, ["person-founder"])
        self.assertEqual(meta["stage"], "education")

        role_payload = {"base_candidate_ids": base_ids, "role_ids": ["founder"], "is_current_role": True}
        rows = asyncio.run(local_backend.hybrid_role_rows(
            role_payload,
            search_common.filters_from_role_payload(role_payload),
            top_k=10,
            include_attributes=["base_id", "position_title"],
        ))
        self.assertEqual([row["person_id"] for row in rows], ["person-founder"])
        self.assertEqual(rows[0]["retrieval_mode"], "filter_only")

    def test_filter_only_role_search_respects_top_k_before_dedupe(self) -> None:
        payload = {"countries": ["United States"], "is_current_role": True}
        rows = asyncio.run(local_backend.hybrid_role_rows(
            payload,
            search_common.filters_from_role_payload(payload),
            top_k=1,
            include_attributes=["base_id", "position_title"],
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["retrieval_mode"], "filter_only")

    def test_tech_skill_prefilter(self) -> None:
        ids, meta = asyncio.run(apply_prefilters.tech_skill_base_ids({"tech_skills": ["DuckDB"]}, page_size=1000, max_ids=10))
        self.assertEqual(ids, ["person-engineer"])
        self.assertEqual(meta["matched"], 1)

    def test_school_resolver_and_namespace_prefix_tokens(self) -> None:
        response = local_backend.namespace("schools").query(
            filters=("school_name", "ContainsAllTokens", "Stan", {"last_as_prefix": True}),
            top_k=10,
            include_attributes=["school_name", "person_count"],
        )
        self.assertEqual(response.rows[0].id, "school-stanford")
        self.assertEqual(response.rows[0].school_name, "Stanford University")

        out = asyncio.run(resolve_education.run(SimpleNamespace(
            state=None,
            payload_json=json.dumps({"education_names": ["Stan"]}),
            env_file=None,
            max_rows_per_name=10,
        )))
        self.assertEqual(out["education_ids"], ["school-stanford"])

    def test_local_company_namespace_and_resolver_use_duckdb(self) -> None:
        response = local_backend.namespace("companies").query(
            rank_by=("name_aliases_text", "BM25", "InfraDB"),
            top_k=5,
            include_attributes=["company_name", "headcount", "sector_types"],
        )
        self.assertEqual(response.rows[0].id, "company-infra")
        self.assertEqual(response.rows[0].company_name, "InfraDB")

        exact = asyncio.run(resolve_companies.run(SimpleNamespace(
            state=None,
            payload_json=json.dumps({"company_names": ["InfraDB"]}),
            env_file=None,
            name_top_k=10,
            semantic_top_k=10,
            company_sector_strategy="soft_union",
            company_sector_min_results=500,
            page_size=1000,
            max_soft_companies=0,
            max_companies=0,
            no_ce=True,
            ce_all=False,
            ce_threshold=500,
            ce_top_n=0,
            ce_model=None,
            ce_batch_size=20,
            ce_concurrency=10,
        )))
        self.assertEqual(exact["company_ids"], ["company-infra"])

    def test_local_company_semantic_lookup_uses_bm25_without_embedding(self) -> None:
        original_embedding = resolve_companies.embedding

        async def fail_embedding(text: str):
            raise AssertionError("local company semantic lookup should not require OpenAI embeddings without vectors")

        resolve_companies.embedding = fail_embedding
        try:
            out = asyncio.run(resolve_companies.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_semantic_queries": ["database infrastructure developer tools"],
                    "sector_types": ["data"],
                    "company_sector_strategy": "staged",
                }),
                env_file=None,
                name_top_k=10,
                semantic_top_k=10,
                company_sector_strategy="soft_union",
                company_sector_min_results=1,
                page_size=1000,
                max_soft_companies=0,
                max_companies=0,
                no_ce=True,
                ce_all=False,
                ce_threshold=500,
                ce_top_n=0,
                ce_model=None,
                ce_batch_size=20,
                ce_concurrency=10,
            )))
        finally:
            resolve_companies.embedding = original_embedding

        self.assertEqual(out["company_ids"][0], "company-infra")
        self.assertTrue(out["used_semantic_search"])
        self.assertEqual(out["sample_companies"][0]["company_name"], "InfraDB")
        self.assertEqual(out["sample_companies"][0]["technology_types"], ["database", "infrastructure"])
        self.assertEqual(out["sample_companies"][0]["customer_type"], ["B2B"])
        self.assertEqual(out["sample_companies"][0]["investor_urns"], ["investor-openai"])
        self.assertEqual(out["sample_companies"][0]["yc_batches"], ["YC S20"])

    def test_local_company_vector_search_requires_explicit_opt_in(self) -> None:
        calls: list[str] = []
        original_embedding = resolve_companies.embedding

        async def fail_embedding(text: str):
            calls.append(text)
            raise AssertionError("vector company search should only request embeddings after explicit opt-in")

        resolve_companies.embedding = fail_embedding
        os.environ["POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH"] = "1"
        try:
            with self.assertRaises(AssertionError):
                asyncio.run(resolve_companies.run(SimpleNamespace(
                    state=None,
                    payload_json=json.dumps({
                        "company_semantic_queries": ["database infrastructure developer tools"],
                        "company_sector_strategy": "semantic_only",
                    }),
                    env_file=None,
                    name_top_k=10,
                    semantic_top_k=10,
                    company_sector_strategy="semantic_only",
                    company_sector_min_results=1,
                    page_size=1000,
                    max_soft_companies=0,
                    max_companies=0,
                    no_ce=True,
                    ce_all=False,
                    ce_threshold=500,
                    ce_top_n=0,
                    ce_model=None,
                    ce_batch_size=20,
                    ce_concurrency=10,
                )))
        finally:
            resolve_companies.embedding = original_embedding
            os.environ.pop("POWERPACKS_LOCAL_COMPANY_VECTOR_SEARCH", None)

        self.assertEqual(calls, ["database infrastructure developer tools"])

    def test_namespace_rank_by_includes_score(self) -> None:
        response = local_backend.namespace("people").query(
            rank_by=("vector", "kNN", [1.0, 0.0, 0.0]),
            filters=("is_current", "Eq", True),
            top_k=1,
            include_attributes=["base_id"],
        )
        self.assertEqual(response.rows[0].id, "person-founder-0")
        self.assertGreater(response.rows[0].score, 0.0)

    def test_vector_rank_pushes_filters_and_cosine_into_duckdb(self) -> None:
        store = local_backend.local_store()
        original_filtered_rows = store._filtered_rows

        def fail_filtered_rows(*_args, **_kwargs):
            raise AssertionError("vector search must not materialize filtered rows in Python")

        store._filtered_rows = fail_filtered_rows
        try:
            response = store.query_namespace(
                "people",
                ("vector", "kNN", [0.0, 1.0, 0.0]),
                ("And", [
                    ("country", "Eq", "United States"),
                    ("is_current", "Eq", True),
                ]),
                1,
                ["base_id", "position_title"],
            )
        finally:
            store._filtered_rows = original_filtered_rows

        self.assertEqual(response.rows[0].id, "person-engineer-0")
        self.assertEqual(response.rows[0].base_id, "person-engineer")
        self.assertGreater(response.rows[0].score, 0.99)

    def test_vector_only_hybrid_role_search_uses_duckdb_sql(self) -> None:
        store = local_backend.local_store()
        original_filtered_rows = store._filtered_rows

        def fail_filtered_rows(*_args, **_kwargs):
            raise AssertionError("vector-only hybrid search must not materialize filtered rows in Python")

        store._filtered_rows = fail_filtered_rows
        try:
            rows = asyncio.run(store.hybrid_role_rows(
                {"semantic_query": "backend", "query_embedding": [0.0, 1.0, 0.0]},
                ("And", [
                    ("country", "Eq", "United States"),
                    ("is_current", "Eq", True),
                ]),
                top_k=1,
                include_attributes=["base_id", "position_title"],
            ))
        finally:
            store._filtered_rows = original_filtered_rows

        self.assertEqual(rows[0]["person_id"], "person-engineer")
        self.assertEqual(rows[0]["position_title"], "Backend Engineer")
        self.assertEqual(rows[0]["retrieval_mode"], "hybrid")

    def test_local_semantic_role_search_requires_query_embedding(self) -> None:
        with self.assertRaisesRegex(Exception, "requires query_embedding"):
            asyncio.run(local_backend.local_store().hybrid_role_rows(
                {"semantic_query": LONG_BACKEND_QUERY, "bm25_queries": ["backend engin"], "is_current_role": True},
                search_common.filters_from_role_payload({"is_current_role": True}),
                top_k=5,
                include_attributes=["base_id", "position_title"],
            ))

    def test_explicit_query_embedding_does_not_force_filter_only_for_short_query(self) -> None:
        rows = asyncio.run(local_backend._hybrid_role_rows_single(
            {"semantic_query": "backend", "bm25_queries": ["backend engin"]},
            ("is_current", "Eq", True),
            top_k=5,
            include_attributes=["base_id", "position_title"],
            query_embedding=[0.0, 1.0, 0.0],
        ))
        self.assertEqual(rows[0]["person_id"], "person-engineer")
        self.assertEqual(rows[0]["retrieval_mode"], "hybrid")

    def test_bm25_adjacency_path(self) -> None:
        rows = asyncio.run(local_backend.bm25_adjacency_rows(
            ["backend engin"],
            ("is_current", "Eq", True),
            top_k=5,
            include_attributes=["base_id", "position_title"],
        ))
        self.assertEqual(rows[0]["person_id"], "person-engineer")
        self.assertEqual(rows[0]["retrieval_mode"], "company_adjacency_bm25")
        self.assertEqual(rows[0]["adjacency_query_indexes"], [0])

    def test_execute_role_search_local_payload_candidate_shape(self) -> None:
        original_hybrid_role_rows = local_backend.hybrid_role_rows

        async def fake_hybrid_role_rows(payload, filters, *, top_k, include_attributes):
            local_payload = dict(payload)
            local_payload["query_embedding"] = [0.0, 1.0, 0.0]
            return await local_backend.local_store().hybrid_role_rows(local_payload, filters, top_k, include_attributes)

        local_backend.hybrid_role_rows = fake_hybrid_role_rows
        try:
            out = asyncio.run(execute_role_search.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({"semantic_query": LONG_BACKEND_QUERY, "bm25_queries": ["backend engin"], "is_current_role": True}),
                env_file=None,
                top_k=5,
                limit=0,
                write_state=False,
                write_artifact=False,
            )))
        finally:
            local_backend.hybrid_role_rows = original_hybrid_role_rows

        self.assertEqual(out["candidate_ids"][0], "person-engineer")
        candidate = out["candidates"][0]
        self.assertEqual(candidate["person_id"], "person-engineer")
        self.assertEqual(candidate["position_id"], "pos-engineer-current")
        self.assertIn("hybrid", candidate["vertical_sources"])
        self.assertIn("matched_position_ids", candidate)

    def test_local_person_attribution_marks_non_current_matched_position(self) -> None:
        out = asyncio.run(execute_role_search.run(SimpleNamespace(
            state=None,
            payload_json=json.dumps({
                "company_ids": ["company-product"],
                "search_mode": "COMPANY_INTERSECTION",
                "is_current_role": False,
            }),
            env_file=None,
            top_k=10,
            limit=0,
            write_state=False,
            write_artifact=False,
        )))
        self.assertEqual(out["candidate_ids"], ["person-founder"])
        self.assertEqual(out["candidates"][0]["position_id"], "pos-founder-past-product")

        state = {"steps": [{"id": "execute_role_search", "output": out}]}
        rows = hydrate_people.fetch_local_person_rows(["person-founder"], db_path=self.db_path, workers=1, batch_size=1)
        profile = hydrate_people.normalize_hydrated_context(rows[0])
        enriched = hydrate_people.apply_candidate_metadata(
            profile,
            hydrate_people.candidate_metadata(state)["person-founder"],
        )

        self.assertEqual(enriched["matched_position_indexes"], [1])
        self.assertIn("filter_only", enriched["vertical_sources"])

    def test_local_hydration_projects_profile_fields_without_vectors(self) -> None:
        rows = hydrate_people.fetch_local_person_rows(["person-engineer"], db_path=self.db_path, workers=2, batch_size=1)
        self.assertEqual(len(rows), 1)
        context = rows[0]["hydrated_context"]
        position = context["positions"][0]
        self.assertEqual(position["position_title"], "Backend Engineer")
        self.assertIn("Python APIs", position["description"])
        self.assertIn("distributed database", position["dense_text"])
        self.assertEqual(position["company_domain"], "infradb.example")
        self.assertEqual(position["company_sector_types"], ["developer_tools"])
        self.assertNotIn("vector", position)
        self.assertNotIn("word_tokens", position)

    def test_local_hydration_exposes_inferred_birth_year_and_age(self) -> None:
        rows = hydrate_people.fetch_local_person_rows(["person-founder"], db_path=self.db_path, workers=1, batch_size=1)
        profile = hydrate_people.normalize_hydrated_context(rows[0])
        self.assertEqual(profile["inferred_birth_year"], 1988)
        self.assertEqual(profile["inferred_age"], date.today().year - 1988)
        view = hydrate_people.llm_profile_view(profile)
        self.assertEqual(view["inferred_birth_year"], 1988)
        self.assertEqual(view["inferred_age"], profile["inferred_age"])
        self.assertEqual(view["years_of_experience"], profile["years_of_experience"])

    def test_local_store_lazy_import_and_clear_namespace_errors(self) -> None:
        import local_duckdb_store

        missing_path = str(Path(self.tmpdir.name) / "missing-table.duckdb")
        import duckdb
        con = duckdb.connect(missing_path)
        con.close()
        with self.assertRaisesRegex(local_duckdb_store.LocalDuckDBError, "local_people_positions"):
            local_duckdb_store.LocalDuckDBSearchStore(missing_path).namespace("people")
        with self.assertRaisesRegex(local_duckdb_store.LocalDuckDBError, "unknown local DuckDB namespace"):
            local_backend.local_store().namespace("unsupported")

    def test_filter_only_respects_max_results_and_projects_id(self) -> None:
        rows = asyncio.run(local_backend.filter_only_rows_for_namespace(
            "people",
            None,
            ["base_id"],
            page_size=1,
            max_results=2,
        ))
        self.assertEqual([row["id"] for row in rows], ["person-engineer-0", "person-founder-0"])
        self.assertEqual([row["base_id"] for row in rows], ["person-engineer", "person-founder"])

    def test_default_mode_safety(self) -> None:
        os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
        local_backend.configure_local_backend(None)
        local_backend._local_store_for_path.cache_clear()
        self.assertFalse(local_backend.is_local_backend())
        self.assertIsInstance(local_backend.namespace_name("people"), str)
        with self.assertRaises(RuntimeError):
            local_backend.local_store()


class LocalFilterEvalTests(unittest.TestCase):
    def test_supported_operators_nested_and_none(self) -> None:
        row = {
            "name": "Alice Founder",
            "age": 35,
            "city": "San Francisco",
            "tags": ["founder", "python"],
            "school_name_tokens": ["stanford", "university"],
        }
        self.assertTrue(local_filter_eval.filter_matches(row, None))
        self.assertTrue(local_filter_eval.filter_matches(row, ("And", [("age", "Gt", 30), ("age", "Gte", 35), ("age", "Lt", 40), ("age", "Lte", 35)])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("Or", [("city", "Eq", "New York"), ("city", "Eq", "San Francisco")])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "In", ["San Francisco"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "NotIn", ["Boston"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "NotEq", "Boston")))
        self.assertTrue(local_filter_eval.filter_matches(row, ("tags", "ContainsAny", ["python"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("school_name", "ContainsAllTokens", "Stanford Univ", {"last_as_prefix": True})))
        self.assertTrue(local_filter_eval.filter_matches(row, ("name", "IGlob", "alice*")))
        self.assertFalse(local_filter_eval.filter_matches(row, ("tags", "ContainsAny", ["rust"])))
        self.assertEqual(local_filter_eval.filter_rows([row, {"age": 20}], ("age", "Gte", 30)), [row])

    def test_malformed_filters_raise(self) -> None:
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, [])
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("And", "not-a-list"))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "Unknown", 1))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "Eq", 1, {}))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "ContainsAllTokens", "x", "bad-options"))


class LocalDuckDBIncrementalShimTests(unittest.TestCase):
    def test_records_dir_rejects_legacy_jsonl_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            records = tmp / "records"
            records.mkdir()
            (records / "people.records.jsonl").write_text('{"id":"legacy"}\n', encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build-local-duckdb-shim.py"),
                    "--records-dir",
                    str(records),
                    "--output-dir",
                    str(tmp / "search-index"),
                    "--force",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no records artifacts found", proc.stderr)

    def test_incremental_duplicate_ids_fall_back_without_collapsing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            records = tmp / "records"
            out = tmp / "search-index"
            write_records(records / "people.records.parquet", [
                {"id": "dup", "position_id": "dup", "person_id": "person-a", "position_title": "Alpha"},
                {"id": "dup", "position_id": "dup", "person_id": "person-b", "position_title": "Beta"},
            ])

            first = run_shim_json(
                "--records-dir", str(records), "--output-dir", str(out), "--force"
            )
            second = run_shim_json(
                "--records-dir", str(records), "--output-dir", str(out), "--incremental"
            )

            self.assertEqual(second["table_diffs"]["local_people_positions"]["reason"], "duplicate_ids")
            self.assertEqual(
                query_duckdb(Path(first["duckdb"]), "select count(*) from local_people_positions"),
                [(2,)],
            )

    def test_rebuild_skips_hashes_then_incremental_bootstraps_and_noops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            records = tmp / "records"
            out = tmp / "search-index"
            people_csv = tmp / "people.csv"
            write_people_csv(people_csv, [("person-one", "Person One")])
            write_records(records / "people.records.parquet", [{"id": "pos-1", "position_id": "pos-1", "person_id": "person-one", "base_id": "person-one", "position_title": "Engineer", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])
            write_records(records / "summaries.records.parquet", [{"id": "person-one", "person_id": "person-one", "base_id": "person-one", "summary": "Old", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])
            write_records(records / "companies.records.parquet", [{"company_urn": "company-one", "company_name": "Company One", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])

            first = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--force")
            self.assertEqual(
                query_duckdb(
                    Path(first["duckdb"]),
                    "select count(*) from information_schema.tables where table_name = '_local_record_hashes'",
                ),
                [(0,)],
            )
            bootstrap = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--incremental")
            noop = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--incremental")

            self.assertEqual(first["status"], "ok")
            self.assertEqual(bootstrap["duckdb_update_mode"], "incremental")
            for table in ["local_people_positions", "local_summaries", "local_companies", "local_person_profiles"]:
                self.assertEqual(bootstrap["table_diffs"][table]["mode"], "bootstrap_replace_missing_hashes")
                self.assertEqual(noop["table_diffs"][table]["mode"], "noop")
                self.assertEqual(noop["table_diffs"][table]["inserted_rows"], 0)
                self.assertEqual(noop["table_diffs"][table]["updated_rows"], 0)
                self.assertEqual(noop["table_diffs"][table]["deleted_rows"], 0)
            hash_rows = query_duckdb(Path(first["duckdb"]), "select count(*) from _local_record_hashes")
            self.assertGreater(hash_rows[0][0], 0)

    def test_incremental_updates_inserts_and_deletes_rows_in_existing_duckdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            records = tmp / "records"
            out = tmp / "search-index"
            people_csv = tmp / "people.csv"
            write_people_csv(people_csv, [("person-one", "Person One"), ("person-delete", "Person Delete")])
            write_records(records / "people.records.parquet", [
                {"id": "pos-1", "position_id": "pos-1", "person_id": "person-one", "base_id": "person-one", "position_title": "Engineer", "company_id": "company-one", "company_name": "Company One", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"id": "pos-delete", "position_id": "pos-delete", "person_id": "person-delete", "base_id": "person-delete", "position_title": "Delete", "vector": [0.0, 1.0], "allowed_operator_ids": ["op-test"]},
            ])
            write_records(records / "summaries.records.parquet", [
                {"id": "person-one", "person_id": "person-one", "base_id": "person-one", "summary": "Old", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"id": "person-delete", "person_id": "person-delete", "base_id": "person-delete", "summary": "Delete", "vector": [0.0, 1.0], "allowed_operator_ids": ["op-test"]},
            ])
            write_records(records / "companies.records.parquet", [
                {"company_urn": "company-one", "company_name": "Company One", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"company_urn": "company-delete", "company_name": "Company Delete", "vector": [0.0, 1.0], "allowed_operator_ids": ["op-test"]},
            ])
            first = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--force")
            run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--incremental")

            write_people_csv(people_csv, [("person-one", "Person One Updated"), ("person-two", "Person Two")])
            write_records(records / "people.records.parquet", [
                {"id": "pos-1", "position_id": "pos-1", "person_id": "person-one", "base_id": "person-one", "position_title": "Staff Engineer", "company_id": "company-one", "company_name": "Company One", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"id": "pos-2", "position_id": "pos-2", "person_id": "person-two", "base_id": "person-two", "position_title": "Founder", "company_id": "company-two", "company_name": "Company Two", "vector": [0.5, 0.5], "allowed_operator_ids": ["op-test"]},
            ])
            write_records(records / "summaries.records.parquet", [
                {"id": "person-one", "person_id": "person-one", "base_id": "person-one", "summary": "New", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"id": "person-two", "person_id": "person-two", "base_id": "person-two", "summary": "Two", "vector": [0.5, 0.5], "allowed_operator_ids": ["op-test"]},
            ])
            write_records(records / "companies.records.parquet", [
                {"company_urn": "company-one", "company_name": "Company One Updated", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]},
                {"company_urn": "company-two", "company_name": "Company Two", "vector": [0.5, 0.5], "allowed_operator_ids": ["op-test"]},
            ])
            second = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--incremental")
            db = Path(first["duckdb"])

            for table in ["local_people_positions", "local_summaries", "local_companies", "local_person_profiles"]:
                self.assertEqual(second["table_diffs"][table]["inserted_rows"], 1, table)
                self.assertEqual(second["table_diffs"][table]["updated_rows"], 1, table)
                self.assertEqual(second["table_diffs"][table]["deleted_rows"], 1, table)
            self.assertEqual(query_duckdb(db, "select id, position_title, company_name from local_people_positions order by id"), [("pos-1", "Staff Engineer", "Company One Updated"), ("pos-2", "Founder", "Company Two")])
            self.assertEqual(query_duckdb(db, "select id, summary from local_summaries order by id"), [("person-one", "New"), ("person-two", "Two")])
            self.assertEqual(query_duckdb(db, "select company_urn, company_name from local_companies order by company_urn"), [("company-one", "Company One Updated"), ("company-two", "Company Two")])
            self.assertEqual(query_duckdb(db, "select full_name from local_person_profiles order by full_name"), [("Person One Updated",), ("Person Two",)])

    def test_incremental_missing_hashes_replaces_stale_matching_ids(self) -> None:
        import duckdb

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            records = tmp / "records"
            out = tmp / "search-index"
            people_csv = tmp / "people.csv"
            write_people_csv(people_csv, [("person-one", "Person One")])
            write_records(records / "people.records.parquet", [{"id": "pos-1", "position_id": "pos-1", "person_id": "person-one", "base_id": "person-one", "position_title": "Engineer", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])
            write_records(records / "summaries.records.parquet", [{"id": "person-one", "person_id": "person-one", "base_id": "person-one", "summary": "Correct", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])
            write_records(records / "companies.records.parquet", [{"company_urn": "company-one", "company_name": "Company One", "vector": [1.0, 0.0], "allowed_operator_ids": ["op-test"]}])
            first = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--force")
            db = Path(first["duckdb"])
            con = duckdb.connect(str(db))
            try:
                con.execute("drop table if exists _local_record_hashes")
                con.execute("update local_people_positions set position_title = 'Stale' where id = 'pos-1'")
                con.execute("update local_summaries set summary = 'Stale' where id = 'person-one'")
                con.execute("checkpoint")
            finally:
                con.close()

            payload = run_shim_json("--records-dir", str(records), "--person-profiles-csv", str(people_csv), "--output-dir", str(out), "--operator-id", "op-test", "--incremental")

            self.assertEqual(payload["table_diffs"]["local_people_positions"]["mode"], "bootstrap_replace_missing_hashes")
            self.assertEqual(payload["table_diffs"]["local_people_positions"]["updated_rows"], 1)
            self.assertEqual(query_duckdb(db, "select position_title from local_people_positions where id = 'pos-1'"), [("Engineer",)])
            self.assertEqual(query_duckdb(db, "select summary from local_summaries where id = 'person-one'"), [("Correct",)])


if __name__ == "__main__":
    unittest.main()


class LocalPersonProfilesShimTest(unittest.TestCase):
    def test_shim_creates_person_profiles_and_hydrates_profile_only_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            records = tmp / "records"
            write_records(records / "people.records.parquet", [
                {
                    "id": "role-1",
                    "position_id": "role-1",
                    "person_id": "person-with-role",
                    "base_id": "person-with-role",
                    "vector": [0.0, 1.0],
                    "position_title": "Engineer",
                    "company_name": "RoleCo",
                    "allowed_operator_ids": ["op-test"],
                }
            ])
            write_records(records / "summaries.records.parquet", [])
            write_records(records / "education.records.parquet", [])
            write_records(records / "schools.records.parquet", [])
            write_records(records / "companies.records.parquet", [])
            people_csv = tmp / "people.csv"
            people_csv.write_text(
                "id,linkedin_url,full_name,headline,summary,city,state,country,work_experiences,education,source_channels\n"
                "person-with-role,https://www.linkedin.com/in/with-role,With Role,Engineer,Builds things,San Francisco,CA,US,[],[],linkedin_csv\n"
                "person-profile-only,https://www.linkedin.com/in/profile-only,Profile Only,Founder,Builds startups,New York,NY,US,\"[{\"\"title\"\": \"\"Founder\"\", \"\"company\"\": \"\"OnlyCo\"\"}]\",[],gmail_msgvault\n",
                encoding="utf-8",
            )

            payload = run_shim_json(
                "--records-dir", str(records),
                "--person-profiles-csv", str(people_csv),
                "--output-dir", str(tmp / "search-index"),
                "--operator-id", "op-test",
                "--force",
            )
            self.assertEqual(payload["tables"]["local_person_profiles"], 2)
            self.assertEqual(payload["tables"]["local_people_positions"], 1)

            import duckdb
            with duckdb.connect(payload["duckdb"], read_only=True) as conn:
                profile_only_id = conn.execute("select person_id from local_person_profiles where full_name = 'Profile Only'").fetchone()[0]

            rows = hydrate_people.fetch_local_person_rows([str(profile_only_id)], db_path=payload["duckdb"], workers=1, batch_size=1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["full_name"], "Profile Only")
            self.assertEqual(rows[0]["location_raw"], "New York, New York, United States")
            self.assertEqual(rows[0]["public_profile_url"], "https://www.linkedin.com/in/profile-only")
            self.assertEqual(rows[0]["hydrated_context"]["positions"][0]["title"], "Founder")


class LocalDuckDBHydrationInteractionCountsTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")

    def _build_records_duckdb(self, tmp: Path, *, include_source_summary: bool) -> str:
        records = tmp / "records"
        write_records(records / "person_profiles.records.parquet", [
            {
                "id": "person-interaction",
                "person_id": "person-interaction",
                "base_id": "person-interaction",
                "full_name": "Interaction Person",
                "headline": "Founder",
                "location_raw": "San Francisco, California, United States",
                "hydrated_context": {
                    "person_id": "person-interaction",
                    "name": "Interaction Person",
                    "headline": "Founder",
                    "positions": [],
                    "education": [],
                },
            }
        ])
        write_records(records / "people.records.parquet", [])
        if include_source_summary:
            write_records(records / "person_source_summary.records.parquet", [
                {
                    "person_id": "person-interaction",
                    "operator_id": "operator-a",
                    "source_channel": "gmail",
                    "source_account": "arthur@example.com",
                    "total_interactions": 7,
                },
                {
                    "person_id": "person-interaction",
                    "operator_id": "operator-a",
                    "source_channel": "imessage",
                    "source_account": "arthur@example.com",
                    "total_interactions": 5,
                },
                {
                    "person_id": "other-person",
                    "operator_id": "operator-a",
                    "source_channel": "gmail",
                    "source_account": "arthur@example.com",
                    "total_interactions": 99,
                },
            ])

        payload = run_shim_json(
            "--records-dir",
            str(records),
            "--output-dir",
            str(tmp / "search-index"),
            "--force",
        )
        return str(payload["duckdb"])

    def test_local_hydration_sums_duckdb_person_source_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            db_path = self._build_records_duckdb(Path(tmp_name), include_source_summary=True)

            rows = hydrate_people.fetch_local_person_rows(["person-interaction"], db_path=db_path, workers=1, batch_size=1)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_interactions"], 12)
            self.assertEqual(rows[0]["hydrated_context"]["total_interactions"], 12)
            profile = hydrate_people.normalize_hydrated_context(rows[0])
            self.assertEqual(profile["total_interactions"], 12)
            self.assertEqual(hydrate_people.llm_profile_view(profile)["total_interactions"], 12)

    def test_local_hydration_without_source_summary_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            db_path = self._build_records_duckdb(Path(tmp_name), include_source_summary=False)

            rows = hydrate_people.fetch_local_person_rows(["person-interaction"], db_path=db_path, workers=1, batch_size=1)

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["total_interactions"])
            self.assertIsNone(rows[0]["hydrated_context"]["total_interactions"])


class LocalPersonProfilePrefilterTest(unittest.TestCase):
    def test_profile_filter_constrains_position_search_without_duplicate_position_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            records = tmp / "records"
            write_records(records / "people.records.parquet", [
                {
                    "id": "role-sf",
                    "position_id": "role-sf",
                    "person_id": "person-sf",
                    "base_id": "person-sf",
                    "vector": [0.0, 1.0],
                    "position_title": "Engineer",
                    "company_name": "SFCo",
                    "allowed_operator_ids": ["op-test"],
                    "city": "SHOULD_DROP",
                    "state": "SHOULD_DROP",
                    "country": "SHOULD_DROP",
                },
                {
                    "id": "role-ny",
                    "position_id": "role-ny",
                    "person_id": "person-ny",
                    "base_id": "person-ny",
                    "vector": [0.0, 1.0],
                    "position_title": "Engineer",
                    "company_name": "NYCo",
                    "allowed_operator_ids": ["op-test"],
                },
            ])
            write_records(records / "summaries.records.parquet", [])
            write_records(records / "education.records.parquet", [])
            write_records(records / "schools.records.parquet", [])
            write_records(records / "companies.records.parquet", [])
            people_csv = tmp / "people.csv"
            people_csv.write_text(
                "id,linkedin_url,full_name,headline,city,state,country,work_experiences,education,source_channels\n"
                "person-sf,https://www.linkedin.com/in/person-sf,SF Person,Engineer,San Francisco,CA,US,\"[{\"\"title\"\": \"\"Engineer\"\", \"\"company\"\": \"\"SFCo\"\"}]\",[],gmail_msgvault\n"
                "person-ny,https://www.linkedin.com/in/person-ny,NY Person,Engineer,New York,NY,US,\"[{\"\"title\"\": \"\"Engineer\"\", \"\"company\"\": \"\"NYCo\"\"}]\",[],gmail_msgvault\n",
                encoding="utf-8",
            )
            payload = run_shim_json(
                "--records-dir", str(records),
                "--person-profiles-csv", str(people_csv),
                "--output-dir", str(tmp / "search-index"),
                "--operator-id", "op-test",
                "--derive-positions-from-person-profiles",
                "--force",
            )
            self.assertEqual(payload["tables"]["local_person_profile_position_overlap"], 2)
            self.assertEqual(payload["tables"]["local_people_positions"], 2)
            self.assertEqual(payload["tables"]["local_people_positions_person_columns_dropped"], 1)

            previous_db = local_backend.explicit_local_backend_path()
            local_backend.configure_local_backend(payload["duckdb"])
            local_backend._local_store_for_path.cache_clear()
            try:
                store = local_backend.namespace("people")
                rows = store.query(filters=["city", "IGlob", "*san francisco*"], top_k=10, include_attributes=["person_id", "position_title"]).rows
            finally:
                local_backend.configure_local_backend(previous_db)
                local_backend._local_store_for_path.cache_clear()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0].model_extra["person_id"], "person-sf")
            self.assertRegex(rows[0].model_extra["person_id"], r"^[0-9a-f-]{36}$")

    def test_age_filter_uses_position_birth_year_when_profile_age_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            records = tmp / "records"
            people_csv = tmp / "people.csv"
            people_csv.write_text(
                "id,linkedin_url,full_name,headline,city,state,country,work_experiences,education,source_channels\n"
                "person-young,https://www.linkedin.com/in/person-young,Young Person,Founder,San Francisco,CA,US,\"[{\"\"title\"\": \"\"Founder\"\", \"\"company\"\": \"\"YoungCo\"\"}]\",[],linkedin_csv\n"
                "person-old,https://www.linkedin.com/in/person-old,Old Person,Founder,San Francisco,CA,US,\"[{\"\"title\"\": \"\"Founder\"\", \"\"company\"\": \"\"OldCo\"\"}]\",[],linkedin_csv\n",
                encoding="utf-8",
            )
            # In production the position records and the person-profile rows are
            # flattened from the same people.csv, so they share stable person ids.
            flattened = {p["full_name"]: str(p["id"]) for p in build_local_duckdb_shim.flatten_people(people_csv)}
            young_id = flattened["Young Person"]
            old_id = flattened["Old Person"]
            write_records(records / "people.records.parquet", [
                {
                    "id": "role-young",
                    "position_id": "role-young",
                    "person_id": young_id,
                    "base_id": young_id,
                    "vector": [0.0, 1.0],
                    "position_title": "Founder",
                    "company_name": "YoungCo",
                    "role_ids": ["founder"],
                    "is_current": True,
                    "inferred_birth_year": 1998,
                    "allowed_operator_ids": ["op-test"],
                },
                {
                    "id": "role-old",
                    "position_id": "role-old",
                    "person_id": old_id,
                    "base_id": old_id,
                    "vector": [0.0, 1.0],
                    "position_title": "Founder",
                    "company_name": "OldCo",
                    "role_ids": ["founder"],
                    "is_current": True,
                    "inferred_birth_year": 1975,
                    "allowed_operator_ids": ["op-test"],
                },
            ])
            write_records(records / "summaries.records.parquet", [])
            write_records(records / "education.records.parquet", [])
            write_records(records / "schools.records.parquet", [])
            write_records(records / "companies.records.parquet", [])
            payload = run_shim_json(
                "--records-dir", str(records),
                "--person-profiles-csv", str(people_csv),
                "--output-dir", str(tmp / "search-index"),
                "--operator-id", "op-test",
                "--force",
            )

            import duckdb
            with duckdb.connect(payload["duckdb"], read_only=True) as conn:
                columns = {row[1] for row in conn.execute("pragma table_info('local_people_positions')").fetchall()}
                profile_years = dict(conn.execute(
                    "select person_id, inferred_birth_year from local_person_profiles"
                ).fetchall())
            self.assertIn("inferred_birth_year", columns)
            # The person-profile projection must expose the same birth year the
            # filter runs on (backfilled from role rows by the shim), otherwise
            # an enforced age filter audits as NULL/unsupported.
            self.assertEqual(profile_years, {young_id: 1998, old_id: 1975})

            previous_db = local_backend.explicit_local_backend_path()
            local_backend.configure_local_backend(payload["duckdb"])
            local_backend._local_store_for_path.cache_clear()
            try:
                filters = search_common.filters_from_role_payload({
                    "role_ids": ["founder"],
                    "age_max": 35,
                    "is_current_role": True,
                })
                rows = asyncio.run(local_backend.filter_only_rows_for_namespace(
                    "people",
                    filters,
                    ["base_id", "position_title", "inferred_birth_year"],
                ))
            finally:
                local_backend.configure_local_backend(previous_db)
                local_backend._local_store_for_path.cache_clear()
            self.assertEqual([row["base_id"] for row in rows], [young_id])
            self.assertEqual(rows[0]["inferred_birth_year"], 1998)
