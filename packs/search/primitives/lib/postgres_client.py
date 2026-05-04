"""Small standalone Postgres client for Powerpacks primitives."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from powerpacks_contracts import POSTGRES_TABLES, assert_columns_in_contract, postgres_required_columns


def load_env_file(path: Path | None) -> None:
    if not path or not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def database_url() -> str:
    configured = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if configured:
        return configured

    user = os.getenv("SUPABASE_DB_USER") or os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD", "postgres")
    host = os.getenv("SUPABASE_DB_HOST") or os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("SUPABASE_DB_PORT") or os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("SUPABASE_DB_NAME") or os.getenv("POSTGRES_DB", "postgres")
    sslmode = os.getenv("POSTGRES_SSLMODE", "require")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}?sslmode={sslmode}"


def ensure_psycopg2() -> Any:
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore

        return psycopg2
    except ModuleNotFoundError as exc:
        if os.getenv("POWERPACKS_PSYCOPG_UV_REEXEC") == "1":
            raise RuntimeError("psycopg2 is required. Install psycopg2-binary or run through uv.") from exc
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("psycopg2 is required. Install psycopg2-binary or install uv for auto re-exec.") from exc
        env = os.environ.copy()
        env["POWERPACKS_PSYCOPG_UV_REEXEC"] = "1"
        os.execvpe(
            uv,
            [uv, "run", "--with", "psycopg2-binary", "python", str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
            env,
        )
        raise AssertionError("unreachable")


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def fetch_person_rows(person_ids: list[str], env_file: Path | None = None) -> list[dict[str, Any]]:
    load_env_file(env_file)
    selected_columns = [
        "id",
        "public_identifier",
        "public_profile_url",
        "full_name",
        "headline",
        "summary",
        "profile_picture_url",
        "location_raw",
        "city",
        "state",
        "country",
        "hydrated_context",
        "x_twitter_handle",
        "x_twitter_followers",
        "linkedin_followers",
        "linkedin_connections",
        "ig_handle",
        "ig_followers",
        "inferred_birth_year",
    ]
    assert_columns_in_contract("persons", selected_columns)
    psycopg2 = ensure_psycopg2()
    query = """
        SELECT
            id::text,
            public_identifier,
            public_profile_url,
            full_name,
            headline,
            summary,
            profile_picture_url,
            location_raw,
            city,
            state,
            country,
            hydrated_context,
            x_twitter_handle,
            x_twitter_followers,
            linkedin_followers,
            linkedin_connections,
            ig_handle,
            ig_followers,
            inferred_birth_year
        FROM persons
        WHERE id = ANY(%s::uuid[])
          AND hydrated_context IS NOT NULL
    """
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (person_ids,))
            rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        row["hydrated_context"] = json_value(row.get("hydrated_context"))
    return rows


def fetch_interaction_counts(person_ids: list[str], env_file: Path | None = None) -> dict[str, int]:
    load_env_file(env_file)
    assert_columns_in_contract("person_source_summary", ["person_id", "total_interactions"])
    psycopg2 = ensure_psycopg2()
    query = """
        SELECT person_id::text, SUM(total_interactions)::int AS total
        FROM person_source_summary
        WHERE person_id = ANY(%s::uuid[])
        GROUP BY person_id
    """
    try:
        with psycopg2.connect(database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (person_ids,))
                return {str(row[0]): int(row[1] or 0) for row in cur.fetchall()}
    except Exception:
        return {}


def resolve_person_investors(names: list[str], env_file: Path | None = None, *, limit_per_name: int = 5) -> list[dict[str, Any]]:
    load_env_file(env_file)
    selected_columns = [
        "id",
        "full_name",
        "public_identifier",
        "public_profile_url",
        "provider_entity_urn",
        "headline",
        "linkedin_followers",
    ]
    assert_columns_in_contract("persons", selected_columns)
    psycopg2 = ensure_psycopg2()
    rows: list[dict[str, Any]] = []
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for name in names:
                normalized_identifier = "".join(ch for ch in name.lower() if ch.isalnum())
                query = """
                    SELECT
                        id::text,
                        full_name,
                        public_identifier,
                        public_profile_url,
                        provider_entity_urn,
                        headline,
                        linkedin_followers
                    FROM persons
                    WHERE provider_entity_urn IS NOT NULL
                      AND (
                        lower(full_name) = lower(%s)
                        OR lower(public_identifier) = %s
                      )
                    ORDER BY
                        CASE WHEN lower(full_name) = lower(%s) THEN 0 ELSE 1 END,
                        linkedin_followers DESC NULLS LAST
                    LIMIT %s
                """
                cur.execute(query, (name, normalized_identifier, name, limit_per_name))
                for row in cur.fetchall():
                    item = dict(row)
                    item["query_name"] = name
                    rows.append(item)
    return rows


def live_table_columns(env_file: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    load_env_file(env_file)
    psycopg2 = ensure_psycopg2()
    tables = list(POSTGRES_TABLES.keys())
    query = """
        SELECT
            table_schema,
            table_name,
            column_name,
            data_type,
            udt_name,
            is_nullable
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
          AND table_name = ANY(%s)
        ORDER BY table_schema, table_name, ordinal_position
    """
    result: dict[str, list[dict[str, Any]]] = {table: [] for table in tables}
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (tables,))
            for row in cur.fetchall():
                table = str(row["table_name"])
                result.setdefault(table, []).append({
                    "schema": row["table_schema"],
                    "name": row["column_name"],
                    "data_type": row["data_type"],
                    "udt_name": row["udt_name"],
                    "nullable": row["is_nullable"] == "YES",
                })
    return result


def check_required_postgres_columns(env_file: Path | None = None) -> dict[str, Any]:
    live = live_table_columns(env_file=env_file)
    tables: dict[str, Any] = {}
    ok = True
    for table, meta in POSTGRES_TABLES.items():
        live_columns = {str(column["name"]) for column in live.get(table, [])}
        required = postgres_required_columns(table)
        missing = [column for column in required if column not in live_columns]
        optional = bool(meta.get("optional"))
        table_ok = not missing or optional
        ok = ok and table_ok
        tables[table] = {
            "optional": optional,
            "required_columns": required,
            "live_columns": sorted(live_columns),
            "missing_required_columns": missing,
            "ok": table_ok,
        }
    return {
        "ok": ok,
        "tables": tables,
    }
