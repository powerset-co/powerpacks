"""Small standalone Postgres client for Powerpacks primitives."""

from __future__ import annotations

import json
import os
import base64
from pathlib import Path
from typing import Any

from powerpacks_contracts import POSTGRES_TABLES, assert_columns_in_contract, postgres_required_columns


def fixture_path() -> Path | None:
    configured = os.getenv("POWERPACKS_POSTGRES_FIXTURE_JSON")
    return Path(configured) if configured else None


def fixture_tables() -> dict[str, list[dict[str, Any]]] | None:
    path = fixture_path()
    if not path:
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"Postgres fixture must be a JSON object: {path}")
    return {str(k): list(v or []) for k, v in data.items()}


def fixture_rows(table: str) -> list[dict[str, Any]] | None:
    tables = fixture_tables()
    if tables is None:
        return None
    return [dict(row) for row in tables.get(table, []) if isinstance(row, dict)]


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
        raise RuntimeError(
            "Missing required search package: psycopg2. "
            "Run `bin/setup-python` from the Powerpacks repo, or rerun the Powerpacks install script."
        ) from exc


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def credentials_subject(credentials_path: Path | None = None) -> str | None:
    path = credentials_path or Path.home() / ".powerpacks" / "credentials.json"
    if not path.exists():
        return None
    try:
        creds = json.loads(path.read_text())
    except Exception:
        return None
    token = str(creds.get("access_token") or "")
    if not token:
        return None
    return _decode_jwt_payload(token).get("sub")


def fetch_default_set_id(env_file: Path | None = None, credentials_path: Path | None = None) -> dict[str, Any]:
    load_env_file(env_file)
    explicit = os.getenv("POWERPACKS_DEFAULT_SET_ID") or os.getenv("POWERSET_DEFAULT_SET_ID")
    if explicit:
        return {"set_id": explicit, "source": "env"}

    subject = credentials_subject(credentials_path)
    if not subject:
        return {"set_id": None, "source": "missing_credentials_subject"}

    assert_columns_in_contract("sets", ["id", "name", "created_by", "is_active", "is_personal"])
    assert_columns_in_contract("users", ["id", "user_id"])
    psycopg2 = ensure_psycopg2()
    query = """
        SELECT id::text, name, is_personal
        FROM sets
        WHERE created_by = %s
          AND is_active IS TRUE
        ORDER BY is_personal DESC, created_at DESC
        LIMIT 1
    """
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (subject,))
            row = cur.fetchone()
    if not row:
        return {"set_id": None, "source": "no_active_set_for_credentials_subject", "subject": subject}
    return {"set_id": str(row[0]), "set_name": row[1], "is_personal": bool(row[2]), "source": "credentials_subject", "subject": subject}


def fetch_set_operator_ids(
    set_id: str | None = None,
    env_file: Path | None = None,
    credentials_path: Path | None = None,
) -> dict[str, Any]:
    load_env_file(env_file)
    fixture_sets = fixture_rows("sets")
    if fixture_sets is not None:
        users = fixture_rows("users") or []
        members = fixture_rows("set_members") or []
        source = "argument"
        resolved_set_id = set_id or os.getenv("POWERPACKS_DEFAULT_SET_ID") or os.getenv("POWERSET_DEFAULT_SET_ID")
        if not resolved_set_id:
            active_sets = [row for row in fixture_sets if row.get("is_active", True)]
            if not active_sets:
                raise RuntimeError("set_id not provided and fixture contains no active set")
            active_sets.sort(key=lambda row: (not bool(row.get("is_personal")), str(row.get("created_at") or "")))
            resolved_set_id = str(active_sets[0].get("id"))
            source = "fixture_default"
        set_row = next((row for row in fixture_sets if str(row.get("id")) == str(resolved_set_id) and row.get("is_active", True)), None)
        if not set_row:
            raise RuntimeError(f"active set not found in fixture: {resolved_set_id}")
        user_by_auth0 = {str(row.get("user_id")): row for row in users if row.get("user_id")}
        scoped_members = [row for row in members if str(row.get("set_id")) == str(resolved_set_id)]
        out_members: list[dict[str, str]] = []
        auth0_user_ids: list[str] = []
        for member in scoped_members:
            auth0 = str(member.get("user_id") or "")
            if auth0:
                auth0_user_ids.append(auth0)
            user = user_by_auth0.get(auth0, {})
            operator_id = str(user.get("id") or member.get("operator_id") or "")
            if not operator_id:
                continue
            out_members.append({
                "operator_id": operator_id,
                "auth0_user_id": auth0,
                "email": str(user.get("email") or ""),
                "name": str(user.get("name") or ""),
                "role": str(member.get("role") or ""),
            })
        created_by = str(set_row.get("created_by") or "")
        if created_by and created_by not in auth0_user_ids:
            auth0_user_ids.insert(0, created_by)
        return {
            "set_id": str(resolved_set_id),
            "set_name": set_row.get("name"),
            "is_personal": bool(set_row.get("is_personal")),
            "source": source,
            "default_resolution": {},
            "operator_ids": list(dict.fromkeys(member["operator_id"] for member in out_members)),
            "auth0_user_ids": list(dict.fromkeys(auth0_user_ids)),
            "operator_count": len(out_members),
            "members": out_members,
        }

    source = "argument"
    resolved_set_id = set_id
    default_info: dict[str, Any] = {}
    if not resolved_set_id:
        default_info = fetch_default_set_id(env_file, credentials_path)
        resolved_set_id = default_info.get("set_id")
        source = str(default_info.get("source") or "default")
    if not resolved_set_id:
        raise RuntimeError("set_id not provided and no default set could be resolved")

    assert_columns_in_contract("sets", ["id", "name", "created_by", "is_active", "is_personal"])
    assert_columns_in_contract("set_members", ["id", "set_id", "user_id", "role", "joined_at"])
    assert_columns_in_contract("users", ["id", "user_id", "email", "name"])
    psycopg2 = ensure_psycopg2()
    query = """
        SELECT
            s.id::text AS set_id,
            s.name AS set_name,
            s.created_by,
            s.is_personal,
            sm.user_id AS auth0_user_id,
            u.id::text AS operator_id,
            u.email,
            u.name AS operator_name,
            sm.role
        FROM sets s
        LEFT JOIN set_members sm
          ON sm.set_id = s.id
        LEFT JOIN users u
          ON u.user_id = sm.user_id
        WHERE s.id = %s::uuid
          AND s.is_active IS TRUE
        ORDER BY
            CASE WHEN sm.role = 'owner' THEN 0 ELSE 1 END,
            sm.joined_at ASC NULLS LAST,
            sm.user_id ASC NULLS LAST
    """
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (resolved_set_id,))
            rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"active set not found: {resolved_set_id}")

    set_name = rows[0][1]
    created_by = rows[0][2]
    is_personal = bool(rows[0][3])
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    auth0_user_ids: list[str] = []
    for _, _, _, _, auth0_user_id, operator_id, email, operator_name, role in rows:
        if auth0_user_id:
            auth0_user_ids.append(str(auth0_user_id))
        if not operator_id:
            continue
        resolved_operator_id = str(operator_id)
        if resolved_operator_id in seen:
            continue
        seen.add(resolved_operator_id)
        members.append({
            "operator_id": resolved_operator_id,
            "auth0_user_id": str(auth0_user_id or ""),
            "email": str(email or ""),
            "name": str(operator_name or ""),
            "role": str(role or ""),
        })

    if created_by and str(created_by) not in auth0_user_ids:
        auth0_user_ids.insert(0, str(created_by))

    return {
        "set_id": str(resolved_set_id),
        "set_name": set_name,
        "is_personal": is_personal,
        "source": source,
        "default_resolution": default_info,
        "operator_ids": [member["operator_id"] for member in members],
        "auth0_user_ids": list(dict.fromkeys(auth0_user_ids)),
        "operator_count": len(members),
        "members": members,
    }


def fetch_person_rows(person_ids: list[str], env_file: Path | None = None) -> list[dict[str, Any]]:
    load_env_file(env_file)
    fixture = fixture_rows("persons")
    if fixture is not None:
        wanted = {str(pid) for pid in person_ids}
        order = {str(pid): idx for idx, pid in enumerate(person_ids)}
        rows = [dict(row) for row in fixture if str(row.get("id")) in wanted and row.get("hydrated_context") is not None]
        for row in rows:
            row["hydrated_context"] = json_value(row.get("hydrated_context"))
        return sorted(rows, key=lambda row: order.get(str(row.get("id")), len(order)))

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
    # Batch into chunks to avoid giant queries timing out
    BATCH_SIZE = 500
    all_rows: list[dict[str, Any]] = []
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for i in range(0, len(person_ids), BATCH_SIZE):
                batch = person_ids[i:i + BATCH_SIZE]
                cur.execute(query, (batch,))
                all_rows.extend(dict(row) for row in cur.fetchall())

    for row in all_rows:
        row["hydrated_context"] = json_value(row.get("hydrated_context"))
    return all_rows


def fetch_interaction_counts(
    person_ids: list[str],
    env_file: Path | None = None,
    allowed_operator_ids: list[str] | None = None,
) -> dict[str, int]:
    """Total interactions per person.

    When ``allowed_operator_ids`` is provided, only interactions from those
    operators are counted; an empty list yields zero counts (fail closed). When
    None, no operator scope is applied (legacy/global behavior).
    """
    load_env_file(env_file)
    scope = None if allowed_operator_ids is None else {str(op) for op in allowed_operator_ids}
    fixture = fixture_rows("person_source_summary")
    if fixture is not None:
        wanted = {str(pid) for pid in person_ids}
        counts: dict[str, int] = {}
        for row in fixture:
            pid = str(row.get("person_id") or "")
            if pid not in wanted:
                continue
            if scope is not None and str(row.get("operator_id") or "") not in scope:
                continue
            counts[pid] = counts.get(pid, 0) + int(row.get("total_interactions") or 0)
        return counts

    columns = ["person_id", "total_interactions"]
    if scope is not None:
        columns.append("operator_id")
    assert_columns_in_contract("person_source_summary", columns)
    psycopg2 = ensure_psycopg2()
    params: list[Any] = [person_ids]
    scope_sql = ""
    if scope is not None:
        scope_sql = " AND operator_id::uuid = ANY(%s::uuid[])"
        params.append(list(allowed_operator_ids))
    query = f"""
        SELECT person_id::text, SUM(total_interactions)::int AS total
        FROM person_source_summary
        WHERE person_id = ANY(%s::uuid[]){scope_sql}
        GROUP BY person_id
    """
    try:
        with psycopg2.connect(database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                return {str(row[0]): int(row[1] or 0) for row in cur.fetchall()}
    except Exception:
        return {}


def fetch_source_attribution(
    person_ids: list[str],
    env_file: Path | None = None,
    allowed_operator_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-person source attribution for the sendable shortlist.

    Returns {person_id: {
        "operators": [names by interaction desc],
        "channels": [channels with >0 interactions by interaction desc],
        "primary_operator": str | None,   # strongest connection
        "primary_channel": str | None,
    }}.

    Source = operator name (whose network the person is in). Channel =
    source_channel (linkedin, gmail, imessage, ...). Missing table/rows or any
    error degrades gracefully to an empty mapping.
    """
    if not person_ids:
        return {}
    load_env_file(env_file)
    scope = None if allowed_operator_ids is None else {str(op) for op in allowed_operator_ids}

    def _build(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        wanted = {str(pid) for pid in person_ids}
        # person -> operator_name -> interactions ; person -> channel -> interactions
        op_by_person: dict[str, dict[str, int]] = {}
        ch_by_person: dict[str, dict[str, int]] = {}
        for row in rows:
            pid = str(row.get("person_id") or "")
            if pid not in wanted:
                continue
            interactions = int(row.get("total_interactions") or 0)
            op = (row.get("operator_name") or "").strip()
            ch = (row.get("source_channel") or "").strip()
            if op:
                op_by_person.setdefault(pid, {})[op] = op_by_person.setdefault(pid, {}).get(op, 0) + interactions
            if ch:
                ch_by_person.setdefault(pid, {})[ch] = ch_by_person.setdefault(pid, {}).get(ch, 0) + interactions
        out: dict[str, dict[str, Any]] = {}
        for pid in wanted:
            ops = op_by_person.get(pid, {})
            chs = ch_by_person.get(pid, {})
            # operators ranked by interactions desc, then name
            operators = [name for name, _ in sorted(ops.items(), key=lambda kv: (-kv[1], kv[0]))]
            # channels: only those with >0 interactions ranked desc; if all zero,
            # still expose the channel names so we are not silently blank.
            nonzero = {c: n for c, n in chs.items() if n > 0}
            ranked = nonzero if nonzero else chs
            channels = [name for name, _ in sorted(ranked.items(), key=lambda kv: (-kv[1], kv[0]))]
            if not operators and not channels:
                continue
            out[pid] = {
                "operators": operators,
                "channels": channels,
                "primary_operator": operators[0] if operators else None,
                "primary_channel": channels[0] if channels else None,
            }
        return out

    fixture = fixture_rows("person_source_summary")
    if fixture is not None:
        users_fixture = fixture_rows("users") or []
        name_by_op = {str(u.get("id") or ""): (u.get("name") or u.get("email") or "") for u in users_fixture}
        rows = []
        for row in fixture:
            if scope is not None and str(row.get("operator_id") or "") not in scope:
                continue
            enriched = dict(row)
            enriched["operator_name"] = name_by_op.get(str(row.get("operator_id") or ""), "")
            rows.append(enriched)
        return _build(rows)

    assert_columns_in_contract("person_source_summary", ["person_id", "operator_id", "total_interactions"])
    psycopg2 = ensure_psycopg2()
    params: list[Any] = [person_ids]
    scope_sql = ""
    if scope is not None:
        scope_sql = " AND pss.operator_id::uuid = ANY(%s::uuid[])"
        params.append(list(allowed_operator_ids))
    query = f"""
        SELECT pss.person_id::text AS person_id,
               COALESCE(u.name, u.email, '') AS operator_name,
               pss.source_channel AS source_channel,
               pss.total_interactions AS total_interactions
        FROM person_source_summary pss
        LEFT JOIN users u ON u.id::text = pss.operator_id::text
        WHERE pss.person_id = ANY(%s::uuid[]){scope_sql}
    """
    try:
        with psycopg2.connect(database_url()) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, tuple(params))
                rows = [dict(r) for r in cur.fetchall()]
        return _build(rows)
    except Exception:
        return {}


def _threshold_conditions(prefix: str, min_value: Any, max_value: Any, column: str = "total_interactions") -> tuple[list[str], dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if min_value is not None:
        conditions.append(f"{column} >= %({prefix}_min)s")
        params[f"{prefix}_min"] = int(min_value)
    if max_value is not None:
        conditions.append(f"{column} <= %({prefix}_max)s")
        params[f"{prefix}_max"] = int(max_value)
    return conditions, params


def fetch_social_filter_person_ids(payload: dict[str, Any], env_file: Path | None = None) -> list[str]:
    """Return base person IDs matching social follower/connection thresholds."""
    social_fields = [
        ("x_followers_min", "x_twitter_followers", ">="),
        ("x_followers_max", "x_twitter_followers", "<="),
        ("li_followers_min", "linkedin_followers", ">="),
        ("li_followers_max", "linkedin_followers", "<="),
        ("li_connections_min", "linkedin_connections", ">="),
        ("li_connections_max", "linkedin_connections", "<="),
        ("ig_followers_min", "ig_followers", ">="),
        ("ig_followers_max", "ig_followers", "<="),
    ]
    active = [(key, column, op) for key, column, op in social_fields if payload.get(key) is not None]
    if not active:
        return []
    load_env_file(env_file)
    assert_columns_in_contract("persons", ["id"] + list(dict.fromkeys(column for _, column, _ in active)))
    psycopg2 = ensure_psycopg2()
    conditions = []
    params: dict[str, Any] = {}
    for key, column, op in active:
        conditions.append(f"{column} {op} %({key})s")
        params[key] = payload[key]
    query = f"SELECT id::text FROM persons WHERE {' AND '.join(conditions)} ORDER BY id"
    with psycopg2.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [str(row[0]) for row in cur.fetchall()]


def fetch_interaction_filter_person_ids(payload: dict[str, Any], env_file: Path | None = None) -> list[str]:
    """Return base person IDs matching operator/set interaction thresholds.

    Operator scope uses payload.searcher_operator_id when present, otherwise the
    single resolved operator_id from the active set. Set scope aggregates across
    payload.operator_ids when present, or all operators otherwise.
    """
    has_operator = payload.get("operator_interaction_min") is not None or payload.get("operator_interaction_max") is not None
    has_set = payload.get("set_interaction_min") is not None or payload.get("set_interaction_max") is not None
    if not has_operator and not has_set:
        return []

    load_env_file(env_file)
    assert_columns_in_contract("person_source_summary", ["person_id", "operator_id", "total_interactions"])
    psycopg2 = ensure_psycopg2()
    operator_ids = [str(v) for v in payload.get("operator_ids") or payload.get("allowed_operator_ids") or [] if v]
    searcher_operator_id = str(payload.get("searcher_operator_id") or "").strip()
    if not searcher_operator_id and len(operator_ids) == 1:
        searcher_operator_id = operator_ids[0]

    def query_operator(cur) -> set[str]:
        if not searcher_operator_id:
            return set()
        conditions = ["operator_id = %(operator_id)s"]
        params: dict[str, Any] = {"operator_id": searcher_operator_id}
        more, more_params = _threshold_conditions("op", payload.get("operator_interaction_min"), payload.get("operator_interaction_max"))
        conditions.extend(more)
        params.update(more_params)
        cur.execute(f"SELECT person_id::text FROM person_source_summary WHERE {' AND '.join(conditions)}", params)
        return {str(row[0]) for row in cur.fetchall()}

    def query_set(cur) -> set[str]:
        conditions = []
        params: dict[str, Any] = {}
        if operator_ids:
            conditions.append("operator_id = ANY(%(operator_ids)s)")
            params["operator_ids"] = operator_ids
        having, having_params = _threshold_conditions("set", payload.get("set_interaction_min"), payload.get("set_interaction_max"), "SUM(total_interactions)")
        params.update(having_params)
        where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
        having_sql = "HAVING " + " AND ".join(having) if having else ""
        cur.execute(f"""
            SELECT person_id::text
            FROM person_source_summary
            {where_sql}
            GROUP BY person_id
            {having_sql}
        """, params)
        return {str(row[0]) for row in cur.fetchall()}

    with psycopg2.connect(database_url()) as conn:
        with conn.cursor() as cur:
            operator_matches = query_operator(cur) if has_operator else None
            set_matches = query_set(cur) if has_set else None

    if has_operator and has_set:
        if operator_matches is None:
            operator_matches = set()
        if set_matches is None:
            set_matches = set()
        return sorted(operator_matches & set_matches)
    if has_operator:
        return sorted(operator_matches or set())
    return sorted(set_matches or set())


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
