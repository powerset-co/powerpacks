#!/usr/bin/env python3
"""Stdlib-only setup orchestration primitive for local ingestion setup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
SETUP_LEDGER = Path('.powerpacks/setup/setup-run.json')
DEFAULT_REFRESH_INTERVAL_HOURS = 168
DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS = int(os.environ.get('POWERPACKS_SETUP_GMAIL_SYNC_LOOKBACK_DAYS', '14'))
EMPTY_LOCAL_SEARCH_DUCKDB_MAX_BYTES = int(os.environ.get('POWERPACKS_SETUP_EMPTY_DUCKDB_MAX_BYTES', str(1024 * 1024)))
SETUP_REFRESH_LEDGER = Path('.powerpacks/network-import/discover/ledger.setup.json')
SETUP_SOURCE_CHANNELS = ['gmail', 'linkedin_csv', 'messages', 'twitter']
SETUP_PHASES = ['link', 'import', 'index']
APPROVALS = [
    ('browser_auth', 'Browser/Gmail OAuth authorization requires user approval.'),
    ('gcp_console_oauth_app', 'GCP Console/OAuth app automation requires user approval.'),
    ('oauth_test_users', 'OAuth test-user changes require user approval.'),
    ('provider_spend', 'RapidAPI/Parallel/OpenAI spend requires explicit allow flags.'),
    ('uploads_research', 'Uploads/research actions require approval.'),
    ('provider_allow_flags', 'Provider allow flags require approval.'),
]


def approval_payload() -> list[dict[str, str]]:
    return [{'id': k, 'description': d} for k, d in APPROVALS]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def parse_json_fragment(text: str) -> Any:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text or ''):
        if char not in '[{':
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError('no JSON object or array found', text or '', 0)


def tail(text: str, limit: int = 4000) -> str:
    if not text:
        return ''
    return text[-limit:]


def progress(message: str) -> None:
    print(f"[setup {now()}] {message}", file=sys.stderr, flush=True)


def run_json_command(cmd: list[str], timeout: int = 6 * 60 * 60, *, stream_stderr: bool = False) -> tuple[int, dict[str, Any], str]:
    if not stream_stderr:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        payload: dict[str, Any]
        try:
            parsed = parse_json_fragment(proc.stdout)
            payload = parsed if isinstance(parsed, dict) else {'payload': parsed}
        except json.JSONDecodeError:
            payload = {}
        return proc.returncode, payload, proc.stderr

    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stream(stream: Any, chunks: list[str], *, mirror_stderr: bool = False) -> None:
        try:
            for line in iter(stream.readline, ''):
                chunks.append(line)
                if mirror_stderr:
                    print(line, end='', file=sys.stderr, flush=True)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=read_stream, args=(proc.stdout, stdout_chunks), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=(proc.stderr, stderr_chunks), kwargs={'mirror_stderr': True}, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        stderr_chunks.append(f"\ncommand timed out after {timeout} seconds\n")
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    stdout = ''.join(stdout_chunks)
    stderr = ''.join(stderr_chunks)
    payload: dict[str, Any]
    try:
        parsed = parse_json_fragment(stdout)
        payload = parsed if isinstance(parsed, dict) else {'payload': parsed}
    except json.JSONDecodeError:
        payload = {}
    return code, payload, stderr


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_setup_ledger(path: Path = SETUP_LEDGER) -> dict[str, Any]:
    if path.exists():
        try:
            return read_json(path)
        except Exception:
            pass
    return {
        'schema_version': 1,
        'status': 'pending',
        'phases': {phase: {'status': 'pending'} for phase in SETUP_PHASES},
        'approval_requirements': [],
    }


def save_setup_ledger(ledger: dict[str, Any], path: Path = SETUP_LEDGER) -> None:
    ledger['updated_at'] = now()
    write_json(path, ledger)


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_hours(value: Any) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def empty_account_summary_channel() -> dict[str, Any]:
    return {'status': 'unlinked', 'linked': False, 'skipped': False, 'usernames_count': 0, 'artifacts': [], 'config': {}}


def accounts_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'exists': False, 'channels': {channel: empty_account_summary_channel() for channel in SETUP_SOURCE_CHANNELS}}
    try:
        data = read_json(path)
    except Exception as exc:
        return {'exists': True, 'error': str(exc), 'channels': {channel: empty_account_summary_channel() for channel in SETUP_SOURCE_CHANNELS}}
    channels: dict[str, Any] = {}
    for k, v in (data.get('channels') or data.get('accounts') or {}).items():
        if isinstance(v, dict):
            if 'status' in v:
                status = v.get('status') or 'unknown'
            elif v.get('linked'):
                status = 'linked'
            elif v.get('skipped'):
                status = 'skipped'
            else:
                status = 'unlinked'
            artifacts = v.get('artifacts') or []
            artifact_names = sorted(artifacts.keys()) if isinstance(artifacts, dict) else sorted(str(x) for x in artifacts)
            channels[k] = {'status': status, 'linked': bool(v.get('linked')), 'skipped': bool(v.get('skipped')), 'usernames_count': len(v.get('usernames') or []), 'artifacts': artifact_names, 'config': v.get('config') or {}}
    for channel in SETUP_SOURCE_CHANNELS:
        channels.setdefault(channel, empty_account_summary_channel())
    return {'exists': True, 'version': data.get('version'), 'channels': channels}


def linked_source_fingerprint(accounts: dict[str, Any]) -> str:
    channels = accounts.get('channels') or {}
    linked = {
        ch: {
            'status': rec.get('status'),
            'linked': bool(rec.get('linked')),
            'config': rec.get('config') or {},
        }
        for ch, rec in sorted(channels.items())
        if isinstance(rec, dict) and rec.get('linked')
    }
    raw = json.dumps(linked, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def linked_sources(accounts: dict[str, Any]) -> list[str]:
    channels = accounts.get('channels') or {}
    return sorted(ch for ch, rec in channels.items() if isinstance(rec, dict) and rec.get('linked'))


def link_state(accounts: dict[str, Any]) -> dict[str, list[str] | bool]:
    channels = accounts.get('channels') or {}
    source_names = sorted(set(SETUP_SOURCE_CHANNELS) | set(channels.keys()))
    linked = sorted(ch for ch in source_names if isinstance(channels.get(ch), dict) and channels[ch].get('linked'))
    skipped = sorted(ch for ch in source_names if isinstance(channels.get(ch), dict) and channels[ch].get('skipped'))
    unlinked = sorted(
        ch for ch in source_names
        if not (isinstance(channels.get(ch), dict) and (channels[ch].get('linked') or channels[ch].get('skipped')))
    )
    return {
        'ready': bool(linked),
        'linked': linked,
        'skipped': skipped,
        'unlinked': unlinked,
    }


def resolve_artifact_path(path_text: Any) -> Path:
    path = Path(str(path_text or ''))
    return path if path.is_absolute() else ROOT / path


def completed_refresh_artifact_issue(refresh: dict[str, Any]) -> dict[str, Any] | None:
    people_csv = ROOT / '.powerpacks/network-import/merged/people.csv'
    expected_people_hash = str(refresh.get('after_people_sha256') or '')
    if expected_people_hash:
        if not people_csv.exists():
            return {'reason': 'missing_import_artifact', 'artifact': '.powerpacks/network-import/merged/people.csv'}
        actual_people_hash = sha256_file(people_csv)
        if actual_people_hash != expected_people_hash:
            return {
                'reason': 'import_artifact_drift',
                'artifact': '.powerpacks/network-import/merged/people.csv',
                'expected_sha256': expected_people_hash,
                'actual_sha256': actual_people_hash,
            }

    promoted = refresh.get('promoted') if isinstance(refresh.get('promoted'), dict) else {}
    for key, path_text in sorted(promoted.items()):
        path = resolve_artifact_path(path_text)
        if not path.exists():
            return {'reason': 'missing_import_artifact', 'artifact_key': key, 'artifact': str(path_text)}

    artifact_hashes = refresh.get('artifact_hashes') if isinstance(refresh.get('artifact_hashes'), dict) else {}
    for key, expected_hash in sorted(artifact_hashes.items()):
        if not expected_hash:
            continue
        path_text = promoted.get(key)
        if not path_text:
            continue
        path = resolve_artifact_path(path_text)
        if not path.exists():
            return {'reason': 'missing_import_artifact', 'artifact_key': key, 'artifact': str(path_text)}
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            return {
                'reason': 'import_artifact_drift',
                'artifact_key': key,
                'artifact': str(path_text),
                'expected_sha256': expected_hash,
                'actual_sha256': actual_hash,
            }
    return None


def artifact_hashes(paths: dict[str, str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, path_text in sorted(paths.items()):
        path = resolve_artifact_path(path_text)
        if path.is_file():
            hashes[key] = sha256_file(path)
    return hashes


def normal_setup_gmail_sync_after(ledger: dict[str, Any], accounts: dict[str, Any], lookback_days: int) -> str:
    refresh = ((ledger.get('phases') or {}).get('import') or {}).get('live_refresh')
    refresh = refresh if isinstance(refresh, dict) else {}
    if refresh.get('status') != 'completed':
        return ''
    if refresh.get('source_fingerprint') != linked_source_fingerprint(accounts):
        return ''
    if completed_refresh_artifact_issue(refresh):
        return ''
    completed_at = parse_iso(refresh.get('completed_at'))
    if completed_at is None:
        return ''
    bounded = completed_at - timedelta(days=max(0, int(lookback_days)))
    return bounded.date().isoformat()


def processing_plan_command_text() -> str:
    return 'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index'


def processing_dry_run_command_text(operator_id: str) -> str:
    return f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --dry-run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {operator_id}'


def processing_dry_run_command_args(operator_id: str, args: argparse.Namespace | None = None) -> list[str]:
    cmd = [
        sys.executable,
        'packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py',
        'run',
        '--dry-run',
        '--input',
        '.powerpacks/network-import/merged/people.csv',
        '--output-dir',
        '.powerpacks/search-index',
        '--default-operator-id',
        operator_id,
    ]
    return cmd


def processing_run_command_text(operator_id: str, *, allow_paid: bool = False, args: argparse.Namespace | None = None) -> str:
    text = f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {operator_id}'
    if allow_paid:
        text += ' --allow-paid-role-provider --allow-paid-embeddings --allow-paid-company-provider'
    return text


def processing_run_command_args(operator_id: str, *, allow_paid: bool = False, args: argparse.Namespace | None = None) -> list[str]:
    cmd = [
        sys.executable,
        'packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py',
        'run',
        '--input',
        '.powerpacks/network-import/merged/people.csv',
        '--output-dir',
        '.powerpacks/search-index',
        '--default-operator-id',
        operator_id,
    ]
    if allow_paid:
        cmd.extend(['--allow-paid-role-provider', '--allow-paid-embeddings', '--allow-paid-company-provider'])
    return cmd


def build_local_duckdb_shim_command_text(operator_id: str) -> str:
    return f'uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --operator-id {operator_id} --force'


def build_local_duckdb_shim_command_args(operator_id: str) -> list[str]:
    return [
        sys.executable,
        'scripts/build-local-duckdb-shim.py',
        '--records-dir',
        '.powerpacks/search-index',
        '--operator-id',
        operator_id,
        '--force',
    ]


def _processing_selected_person_ids(processing_payload: dict[str, Any]) -> list[str]:
    counts = processing_payload.get('counts') if isinstance(processing_payload.get('counts'), dict) else {}
    flatten = counts.get('flatten_people') if isinstance(counts.get('flatten_people'), dict) else {}
    selected = flatten.get('selected_person_ids') if isinstance(flatten.get('selected_person_ids'), list) else []
    out: list[str] = []
    seen: set[str] = set()
    for value in selected:
        text = str(value or '').strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _duckdb_columns(con: Any, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f'pragma table_info({table})').fetchall()}
    except Exception:
        return set()


def local_duckdb_processing_dq(processing_payload: dict[str, Any], duckdb_payload: dict[str, Any]) -> dict[str, Any]:
    selected_ids = _processing_selected_person_ids(processing_payload)
    counts = processing_payload.get('counts') if isinstance(processing_payload.get('counts'), dict) else {}
    flatten = counts.get('flatten_people') if isinstance(counts.get('flatten_people'), dict) else {}
    selection = flatten.get('selection') if isinstance(flatten.get('selection'), dict) else {}
    selected_count = int(selection.get('selected_people') or 0)
    if selected_count == 0:
        return {'status': 'ok', 'reason': 'no_people_selected', 'selected_people': 0}
    if not selected_ids:
        return {'status': 'skipped', 'reason': 'processing_payload_missing_selected_person_ids', 'selected_people': selected_count}
    db_path = Path(str(duckdb_payload.get('duckdb') or '.powerpacks/search-index/local-search.duckdb'))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if not db_path.exists():
        return {'status': 'failed', 'reason': 'missing_duckdb', 'duckdb': str(db_path), 'selected_person_ids': selected_ids}
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError:
        return {'status': 'skipped', 'reason': 'duckdb_python_module_missing', 'duckdb': str(db_path), 'selected_person_ids': selected_ids}
    # Retry with short delay to handle transient locks from the rebuild process
    con = None
    for attempt in range(5):
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            break
        except Exception as lock_err:
            if attempt < 4 and 'lock' in str(lock_err).lower():
                import time
                time.sleep(1 + attempt)
                continue
            return {'status': 'skipped', 'reason': 'duckdb_locked', 'duckdb': str(db_path), 'error': f'{type(lock_err).__name__}: {lock_err}', 'selected_person_ids': selected_ids}
    try:
        tables = {row[0] for row in con.execute("select table_name from information_schema.tables where table_schema = 'main'").fetchall()}
        profile_hits: set[str] = set()
        vector_hits: set[str] = set()
        position_vector_hits: set[str] = set()
        summary_vector_hits: set[str] = set()
        if 'local_person_profiles' in tables:
            profile_cols = _duckdb_columns(con, 'local_person_profiles')
            profile_id_cols = [col for col in ['person_id', 'base_id', 'id'] if col in profile_cols]
            for person_id in selected_ids:
                if not profile_id_cols:
                    continue
                where = ' OR '.join([f"cast({col} as varchar) = ?" for col in profile_id_cols])
                params = [person_id] * len(profile_id_cols)
                if int(con.execute(f"select count(*) from local_person_profiles where {where}", params).fetchone()[0] or 0) > 0:
                    profile_hits.add(person_id)
        for table in ['local_people_positions', 'local_summaries']:
            if table not in tables:
                continue
            cols = _duckdb_columns(con, table)
            if 'vector' not in cols:
                continue
            id_cols = [col for col in ['person_id', 'base_id', 'id'] if col in cols]
            if not id_cols:
                continue
            for person_id in selected_ids:
                where_ids = ' OR '.join([f"cast({col} as varchar) = ?" for col in id_cols])
                params = [person_id] * len(id_cols)
                query = f"select count(*) from {table} where ({where_ids}) and vector is not null and len(vector) > 0"
                if int(con.execute(query, params).fetchone()[0] or 0) > 0:
                    vector_hits.add(person_id)
                    if table == 'local_people_positions':
                        position_vector_hits.add(person_id)
                    elif table == 'local_summaries':
                        summary_vector_hits.add(person_id)
        people_counts = counts.get('build_people_records') if isinstance(counts.get('build_people_records'), dict) else {}
        expected_position_vectors = int(people_counts.get('with_vectors') or 0) > 0
        missing_vectors = [person_id for person_id in selected_ids if person_id not in vector_hits]
        missing_position_vectors = [person_id for person_id in selected_ids if expected_position_vectors and person_id not in position_vector_hits]
        missing_profiles = [person_id for person_id in selected_ids if person_id not in profile_hits]
        failed = bool(missing_vectors or missing_position_vectors)
        return {
            'status': 'ok' if not failed else 'failed',
            'duckdb': str(db_path),
            'selected_people': selected_count,
            'selected_person_ids': selected_ids,
            'profile_hits': len(profile_hits),
            'vector_hits': len(vector_hits),
            'position_vector_hits': len(position_vector_hits),
            'summary_vector_hits': len(summary_vector_hits),
            'expected_position_vectors': expected_position_vectors,
            'missing_profile_person_ids': missing_profiles,
            'missing_vector_person_ids': missing_vectors,
            'missing_position_vector_person_ids': missing_position_vectors,
        }
    except Exception as exc:
        return {'status': 'failed', 'reason': 'duckdb_dq_query_failed', 'duckdb': str(db_path), 'error': f'{type(exc).__name__}: {exc}', 'selected_person_ids': selected_ids}
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass


def search_record_summary() -> dict[str, Any]:
    records = ROOT / '.powerpacks/search-index/records'
    files = sorted(records.glob('*.records.parquet')) if records.exists() else []
    nonempty = []
    for path in files:
        try:
            if path.stat().st_size > 0:
                nonempty.append(path)
        except OSError:
            continue
    return {
        'records_dir': str(records),
        'record_files': len(files),
        'nonempty_record_files': len(nonempty),
        'empty_record_files': max(0, len(files) - len(nonempty)),
    }


def search_records_have_data() -> bool:
    return bool(search_record_summary().get('nonempty_record_files'))


def search_records_missing_or_empty() -> bool:
    return not search_records_have_data()


def local_search_duckdb_missing_or_tiny() -> bool:
    duckdb = ROOT / '.powerpacks/search-index/local-search.duckdb'
    if not duckdb.exists():
        return True
    try:
        return duckdb.stat().st_size <= EMPTY_LOCAL_SEARCH_DUCKDB_MAX_BYTES
    except OSError:
        return True


def run_processing_dry_run(operator_id: str, args: argparse.Namespace | None = None) -> dict[str, Any]:
    code, payload, stderr = run_json_command(processing_dry_run_command_args(operator_id, args), timeout=60 * 60)
    if code != 0:
        return {'status': 'failed', 'command': processing_dry_run_command_text(operator_id), 'error': tail(stderr) or payload}
    return payload


def estimated_paid_calls(estimate: dict[str, Any]) -> int:
    paid = estimate.get('estimated_paid_calls') if isinstance(estimate.get('estimated_paid_calls'), dict) else {}
    total = 0
    for value in paid.values():
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def estimated_cost_usd(estimate: dict[str, Any]) -> float | None:
    costs = estimate.get('estimated_costs') if isinstance(estimate.get('estimated_costs'), dict) else {}
    value = estimate.get('estimated_cost_usd')
    if value is None:
        value = costs.get('total_estimated_usd')
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_processing_index(args: argparse.Namespace, ledger: dict[str, Any], ledger_path: Path) -> tuple[dict[str, Any], int]:
    people = ROOT / '.powerpacks/network-import/merged/people.csv'
    idx = indexing_readiness(args.operator_id)
    account_state = accounts_summary(Path(args.accounts))
    if idx.get('status') == 'search_ready' and not linked_sources(account_state):
        payload = {
            'status': 'ready',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'people_sha256': idx.get('people_sha256') or (sha256_file(people) if people.exists() else ''),
            'duckdb': idx.get('duckdb', '.powerpacks/search-index/local-search.duckdb'),
            'ledger': idx.get('ledger', ''),
            'manifest': idx.get('manifest', ''),
            'updated_at': now(),
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'ready'
        save_setup_ledger(ledger, ledger_path)
        return payload, 0

    if idx.get('status') == 'records_only_duckdb_missing':
        code, duckdb_payload, duckdb_stderr = run_json_command(build_local_duckdb_shim_command_args(args.operator_id), timeout=60 * 60)
        if code != 0:
            payload = {
                'status': 'failed',
                'step': 'local_duckdb',
                'local_duckdb': duckdb_payload,
                'error': tail(duckdb_stderr) or duckdb_payload,
            }
            ledger.setdefault('phases', {})['index'] = payload
            ledger['status'] = 'failed'
            save_setup_ledger(ledger, ledger_path)
            return payload, 1
        payload = {
            'status': 'ready',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'people_sha256': sha256_file(people) if people.exists() else '',
            'local_duckdb': duckdb_payload,
            'duckdb': duckdb_payload.get('duckdb', '.powerpacks/search-index/local-search.duckdb') if isinstance(duckdb_payload, dict) else '.powerpacks/search-index/local-search.duckdb',
            'manifest': duckdb_payload.get('manifest', '') if isinstance(duckdb_payload, dict) else '',
            'updated_at': now(),
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'ready'
        save_setup_ledger(ledger, ledger_path)
        return payload, 0

    if not people.exists():
        payload = {'status': 'not_ready', 'reason': 'missing_people_csv', 'missing': ['.powerpacks/network-import/merged/people.csv']}
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'not_ready'
        save_setup_ledger(ledger, ledger_path)
        return payload, 0

    estimate = run_processing_dry_run(args.operator_id, args)
    if estimate.get('status') == 'failed':
        payload = {'status': 'failed', 'step': 'index_dry_run', 'processing_estimate': estimate}
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'failed'
        save_setup_ledger(ledger, ledger_path)
        return payload, 1

    paid_calls = estimated_paid_calls(estimate)
    total_cost = estimated_cost_usd(estimate)
    allow_paid = paid_calls > 0 or bool(total_cost and total_cost > 0)
    code, processing_payload, processing_stderr = run_json_command(
        processing_run_command_args(args.operator_id, allow_paid=allow_paid, args=args),
        timeout=6 * 60 * 60,
    )
    if code != 0:
        payload = {
            'status': 'failed',
            'step': 'index_processing',
            'processing_estimate': estimate,
            'processing': processing_payload,
            'error': tail(processing_stderr) or processing_payload,
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'failed'
        save_setup_ledger(ledger, ledger_path)
        return payload, 1

    code, duckdb_payload, duckdb_stderr = run_json_command(build_local_duckdb_shim_command_args(args.operator_id), timeout=60 * 60)
    if code != 0:
        payload = {
            'status': 'failed',
            'step': 'local_duckdb',
            'processing_estimate': estimate,
            'processing': processing_payload,
            'local_duckdb': duckdb_payload,
            'error': tail(duckdb_stderr) or duckdb_payload,
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'failed'
        save_setup_ledger(ledger, ledger_path)
        return payload, 1

    duckdb_dq = local_duckdb_processing_dq(processing_payload, duckdb_payload if isinstance(duckdb_payload, dict) else {})
    if duckdb_dq.get('status') == 'failed' and duckdb_dq.get('reason') != 'duckdb_locked':
        payload = {
            'status': 'failed',
            'step': 'local_duckdb_dq',
            'processing_estimate': estimate,
            'processing': processing_payload,
            'local_duckdb': duckdb_payload,
            'local_duckdb_dq': duckdb_dq,
            'error': 'processed people were not found with vectors in canonical DuckDB',
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'failed'
        save_setup_ledger(ledger, ledger_path)
        return payload, 1

    payload = {
        'status': 'ready',
        'people_csv': '.powerpacks/network-import/merged/people.csv',
        'people_sha256': sha256_file(people),
        'estimated_cost_usd': total_cost,
        'estimated_paid_calls': estimate.get('estimated_paid_calls', {}),
        'processing_estimate': estimate,
        'processing': processing_payload,
        'local_duckdb': duckdb_payload,
        'local_duckdb_dq': duckdb_dq,
        'duckdb': duckdb_payload.get('duckdb', '.powerpacks/search-index/local-search.duckdb') if isinstance(duckdb_payload, dict) else '.powerpacks/search-index/local-search.duckdb',
        'updated_at': now(),
    }
    ledger.setdefault('phases', {})['index'] = payload
    ledger['status'] = 'ready'
    save_setup_ledger(ledger, ledger_path)
    return payload, 0


def import_refresh_due(
    ledger: dict[str, Any],
    accounts: dict[str, Any],
    refresh_interval_hours: int,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    sources = linked_sources(accounts)
    if not sources:
        return {'due': False, 'reason': 'no_linked_sources', 'linked_sources': sources}
    fingerprint = linked_source_fingerprint(accounts)
    refresh = ((ledger.get('phases') or {}).get('import') or {}).get('live_refresh')
    refresh = refresh if isinstance(refresh, dict) else {}
    if force_refresh:
        return {'due': True, 'reason': 'forced', 'linked_sources': sources, 'source_fingerprint': fingerprint}
    if refresh.get('status') != 'completed':
        return {'due': True, 'reason': 'never_synced_after_link', 'linked_sources': sources, 'source_fingerprint': fingerprint}
    if refresh.get('source_fingerprint') != fingerprint:
        return {'due': True, 'reason': 'linked_sources_changed', 'linked_sources': sources, 'source_fingerprint': fingerprint}
    artifact_issue = completed_refresh_artifact_issue(refresh)
    if artifact_issue:
        return {'due': True, **artifact_issue, 'linked_sources': sources, 'source_fingerprint': fingerprint}
    last_age = age_hours(refresh.get('completed_at'))
    if last_age is None:
        return {'due': True, 'reason': 'missing_refresh_timestamp', 'linked_sources': sources, 'source_fingerprint': fingerprint}
    if refresh_interval_hours >= 0 and last_age >= refresh_interval_hours:
        return {
            'due': True,
            'reason': 'refresh_interval_elapsed',
            'age_hours': round(last_age, 2),
            'refresh_interval_hours': refresh_interval_hours,
            'linked_sources': sources,
            'source_fingerprint': fingerprint,
        }
    return {
        'due': False,
        'reason': 'recent_refresh',
        'age_hours': round(last_age, 2),
        'refresh_interval_hours': refresh_interval_hours,
        'linked_sources': sources,
        'source_fingerprint': fingerprint,
        'last_refresh': refresh,
    }


def indexing_readiness(operator_id: str) -> dict[str, Any]:
    si = ROOT / '.powerpacks/search-index'
    duck = si / 'local-search.duckdb'
    ledger = si / 'ledger.json'
    manifest = si / 'manifest.json'
    people = ROOT / '.powerpacks/network-import/merged/people.csv'
    people_hash = sha256_file(people) if people.exists() else ''
    processing_needed = {
        'status': 'people_csv_ready_for_processing',
        'people_csv': str(people),
        'people_sha256': people_hash,
        'plan_command': processing_plan_command_text(),
        'dry_run_command': processing_dry_run_command_text(operator_id),
    }
    if duck.exists() and ledger.exists():
        lg = read_json(ledger) if ledger.exists() else {}
        if lg.get('status') in ('completed', 'restored') and (not operator_id or lg.get('default_operator_id') in (None, operator_id) or lg.get('restored_operator_id') in (None, operator_id)):
            index_input_hash = str(lg.get('input_sha256') or '')
            if people_hash and index_input_hash and index_input_hash != people_hash:
                return {
                    **processing_needed,
                    'reason': 'search_index_stale_for_people_csv',
                    'index_input_sha256': index_input_hash,
                }
            return {'status': 'search_ready', 'duckdb': str(duck), 'ledger': str(ledger), 'people_sha256': people_hash, 'index_input_sha256': index_input_hash}
    if duck.exists() and manifest.exists():
        mf = read_json(manifest)
        if mf.get('status') == 'ok' and (not operator_id or mf.get('operator_id') in (None, operator_id)):
            return {
                'status': 'search_ready',
                'duckdb': str(duck),
                'manifest': str(manifest),
                'people_sha256': people_hash,
                'index_input_sha256': people_hash,
            }
    if search_records_have_data() and not duck.exists():
        return {'status': 'records_only_duckdb_missing', 'repair_command': f'uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --derive-positions-from-person-profiles --operator-id {operator_id} --force'}
    if people.exists():
        return processing_needed
    return {'status': 'not_ready', 'missing': ['.powerpacks/network-import/merged/people.csv']}


def normalize_setup_phases(
    ledger: dict[str, Any],
    accounts: dict[str, Any],
    idx: dict[str, Any],
    operator_id: str = '',
    refresh_interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
) -> None:
    phases = ledger.setdefault('phases', {phase: {'status': 'pending'} for phase in SETUP_PHASES})
    phases.pop('bootstrap', None)
    for phase in SETUP_PHASES:
        phases.setdefault(phase, {'status': 'pending'})

    current_link = link_state(accounts)
    if current_link['linked']:
        phases['link'] = {
            'status': 'ready',
            'linked_sources': current_link['linked'],
            'skipped_sources': current_link['skipped'],
            'optional_unlinked_sources': current_link['unlinked'],
        }
    else:
        phases['link'] = {
            'status': 'no_linked_sources',
            'linked_sources': current_link['linked'],
            'skipped_sources': current_link['skipped'],
            'optional_unlinked_sources': current_link['unlinked'],
        }

    people_csv = ROOT / '.powerpacks/network-import/merged/people.csv'
    prior_import = dict(phases.get('import') or {})
    refresh_state = import_refresh_due(ledger, accounts, refresh_interval_hours)
    live_refresh = prior_import.get('live_refresh') if isinstance(prior_import.get('live_refresh'), dict) else {}
    if refresh_state.get('due'):
        phases['import'] = {
            'status': 'refresh_due',
            'source': 'linked_sources',
            'people_csv': '.powerpacks/network-import/merged/people.csv' if people_csv.exists() else '',
            'refresh_due': refresh_state,
            'live_refresh': live_refresh,
        }
    elif people_csv.exists() and live_refresh:
        phases['import'] = {
            'status': 'ready',
            'source': 'live_refresh',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'live_refresh': live_refresh,
        }
    elif people_csv.exists() and not current_link['linked']:
        phases['import'] = {
            'status': 'ready',
            'source': 'existing_artifact',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'refresh_due': refresh_state,
            'live_refresh': live_refresh,
        }
    elif not current_link['linked']:
        phases['import'] = {
            'status': 'no_linked_sources',
            'reason': 'no_linked_sources',
            'people_csv': '',
            'refresh_due': refresh_state,
            'live_refresh': live_refresh,
        }

    prior_index = phases.get('index') if isinstance(phases.get('index'), dict) else {}
    if idx.get('status') == 'search_ready':
        if prior_index.get('status') == 'needs_processing':
            if idx.get('index_input_sha256') and idx.get('index_input_sha256') == idx.get('people_sha256'):
                ready_index = dict(prior_index)
                ready_index.update({
                    'status': 'ready',
                    'duckdb': idx.get('duckdb', ''),
                    'ledger': idx.get('ledger', ''),
                })
                phases['index'] = ready_index
            else:
                estimate = run_processing_dry_run(operator_id) if operator_id else prior_index.get('processing_estimate', {})
                prior_index['processing_estimate'] = estimate
                phases['index'] = prior_index
        else:
            ready_index = dict(prior_index) if prior_index.get('status') == 'ready' else {}
            ready_index.update({
                'status': 'ready',
                'duckdb': idx.get('duckdb', ''),
                'ledger': idx.get('ledger', ''),
            })
            phases['index'] = ready_index
    elif prior_index.get('status') == 'needs_processing':
        phases['index'] = prior_index
    elif idx.get('status') == 'people_csv_ready_for_processing':
        phases['index'] = {
            'status': 'needs_processing',
            'reason': idx.get('reason') or 'people_csv_ready_for_processing',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'plan_command': idx.get('plan_command') or processing_plan_command_text(),
            'dry_run_command': idx.get('dry_run_command') or processing_dry_run_command_text(operator_id),
            'people_sha256': idx.get('people_sha256', ''),
            'index_input_sha256': idx.get('index_input_sha256', ''),
        }

    statuses = {phase: phases.get(phase, {}).get('status') for phase in SETUP_PHASES}
    if statuses['import'] in ('ready', 'completed') and statuses['index'] == 'ready':
        ledger['status'] = 'ready'
    elif not current_link['linked'] and statuses['index'] == 'ready' and idx.get('status') == 'search_ready':
        ledger['status'] = 'ready'
    elif statuses['import'] == 'refresh_due':
        ledger['status'] = 'refresh_due'
    elif statuses['index'] == 'needs_processing':
        ledger['status'] = 'needs_index_processing'
    elif not current_link['linked'] and statuses['import'] == 'no_linked_sources' and idx.get('status') == 'not_ready':
        ledger['status'] = 'needs_linking'


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    ledger = load_setup_ledger(Path(args.setup_ledger))
    idx = indexing_readiness(args.operator_id)
    accounts = accounts_summary(Path(args.accounts))
    normalize_setup_phases(ledger, accounts, idx, args.operator_id, int(getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS)))
    if isinstance(ledger.get('handoff'), dict):
        ledger['handoff']['commands'] = setup_commands(args)
        ledger['handoff']['requires_approval'] = approval_payload()
    next_actions = []
    link_phase = ledger.get('phases', {}).get('link', {})
    import_phase = ledger.get('phases', {}).get('import', {})
    has_linked_sources = bool(link_phase.get('linked_sources'))
    has_indexable_artifacts = idx['status'] in ('search_ready', 'records_only_duckdb_missing', 'people_csv_ready_for_processing')
    if idx['status'] == 'records_only_duckdb_missing':
        next_actions.append(idx['repair_command'])
    elif idx['status'] == 'people_csv_ready_for_processing':
        next_actions.append(idx.get('dry_run_command') or idx['plan_command'])
    if import_phase.get('status') == 'refresh_due':
        next_actions.append('run setup refresh')
    if not has_linked_sources and not has_indexable_artifacts and import_phase.get('status') != 'refresh_due':
        next_actions.append('link sources with onboarding')
    return {
        'status': 'ok',
        'operator_id': args.operator_id,
        'accounts': accounts,
        'setup_ledger': ledger,
        'search_index_readiness': idx,
        'canonical_people_csv': {
            'path': '.powerpacks/network-import/merged/people.csv',
            'exists': (ROOT / '.powerpacks/network-import/merged/people.csv').exists(),
        },
        'next_actions': next_actions,
    }


def run_status(args: argparse.Namespace) -> int:
    payload = status_payload(args)
    emit(payload)
    return 0


def quote_arg(value: Any) -> str:
    return shlex.quote(str(value))


def setup_phase_command(args: argparse.Namespace, phase: str) -> str:
    cmd = [
        'uv', 'run', '--project', '.', 'python',
        'packs/ingestion/primitives/setup/setup.py',
        phase,
        '--operator-id', args.operator_id,
    ]
    if phase in ('link', 'import', 'index', 'next', 'status'):
        cmd.extend(['--accounts', args.accounts])
    if phase in ('link', 'import', 'index', 'next', 'status'):
        cmd.extend(['--setup-ledger', args.setup_ledger])
    return ' '.join(quote_arg(part) for part in cmd)


def next_action_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = status_payload(args)
    ledger = payload['setup_ledger']
    phases = ledger.get('phases') or {}
    link = phases.get('link') if isinstance(phases.get('link'), dict) else {}
    import_phase = phases.get('import') if isinstance(phases.get('import'), dict) else {}
    index = phases.get('index') if isinstance(phases.get('index'), dict) else {}
    idx = payload.get('search_index_readiness') if isinstance(payload.get('search_index_readiness'), dict) else {}

    action: dict[str, Any]
    if import_phase.get('status') == 'refresh_due':
        action = {
            'status': 'run_command',
            'phase': 'import',
            'auto_safe': True,
            'reason': (import_phase.get('refresh_due') or {}).get('reason') or 'refresh_due',
            'command': setup_phase_command(args, 'import'),
        }
    elif idx.get('status') in ('records_only_duckdb_missing', 'people_csv_ready_for_processing') or index.get('status') == 'needs_processing':
        action = {
            'status': 'run_command',
            'phase': 'index',
            'auto_safe': True,
            'reason': idx.get('status') or index.get('reason') or 'index_not_ready',
            'command': setup_phase_command(args, 'index'),
        }
    elif not link.get('linked_sources') and import_phase.get('status') == 'no_linked_sources' and idx.get('status') == 'not_ready':
        action = {
            'status': 'run_command',
            'phase': 'link',
            'auto_safe': False,
            'reason': 'no linked sources or local network artifacts yet',
            'command': setup_phase_command(args, 'link'),
        }
    else:
        action = {
            'status': 'done',
            'phase': 'ready',
            'auto_safe': True,
            'reason': ledger.get('status') or 'ready',
        }

    return {
        'status': 'ok',
        'operator_id': args.operator_id,
        'next': action,
        'setup_status': ledger.get('status'),
        'phases': phases,
        'next_actions': payload.get('next_actions') or [],
        'search_index_readiness': idx,
    }


def run_next(args: argparse.Namespace) -> int:
    payload = next_action_payload(args)
    emit(payload)
    return 0


def onboarding_step_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        'packs/ingestion/primitives/onboarding/onboarding.py',
        'step',
        '--accounts',
        args.accounts,
        '--operator-id',
        args.operator_id,
    ]
    for email in getattr(args, 'gmail_account', []) or []:
        cmd.extend(['--gmail-account', email])
    for email in getattr(args, 'gmail_add_email', []) or []:
        cmd.extend(['--gmail-add-email', email])
    for email in getattr(args, 'gmail_authorized_email', []) or []:
        cmd.extend(['--gmail-authorized-email', email])
    for source in getattr(args, 'skip_source', []) or []:
        cmd.extend(['--skip-source', source])
    if getattr(args, 'gmail_all', False):
        cmd.append('--gmail-all')
    if getattr(args, 'gmail_db', ''):
        cmd.extend(['--gmail-db', args.gmail_db])
    if getattr(args, 'linkedin_csv', ''):
        cmd.extend(['--linkedin-csv', args.linkedin_csv])
    if getattr(args, 'linkedin_source_user', ''):
        cmd.extend(['--linkedin-source-user', args.linkedin_source_user])
    if getattr(args, 'messages_check', False):
        cmd.append('--messages-check')
    if getattr(args, 'skip_messages_whatsapp', False):
        cmd.append('--skip-messages-whatsapp')
    if getattr(args, 'twitter_handle', ''):
        cmd.extend(['--twitter-handle', args.twitter_handle])
    return cmd


def run_link_phase(args: argparse.Namespace) -> int:
    cmd = onboarding_step_command(args)
    code, payload, stderr = run_json_command(cmd, timeout=120)
    out = {
        'status': payload.get('status') or ('ok' if code == 0 else 'failed'),
        'phase': 'link',
        'operator_id': args.operator_id,
        'command': cmd,
        'payload': payload,
        'stderr': tail(stderr),
    }
    if code == 0:
        status_args = argparse.Namespace(
            operator_id=args.operator_id,
            accounts=args.accounts,
            setup_ledger=args.setup_ledger,
            refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
        )
        final_status = status_payload(status_args)
        save_setup_ledger(final_status['setup_ledger'], Path(args.setup_ledger))
        out['setup_ledger'] = final_status['setup_ledger']
        out['next'] = next_action_payload(status_args)['next']
    emit(out)
    return code


def run_import_phase(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    ledger = load_setup_ledger(ledger_path)
    accounts = accounts_summary(Path(args.accounts))

    due = import_refresh_due(
        ledger,
        accounts,
        int(getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS)),
        force_refresh=not getattr(args, 'only_if_due', False),
    )
    if not due.get('due'):
        status_args = argparse.Namespace(
            operator_id=args.operator_id,
            accounts=args.accounts,
            setup_ledger=args.setup_ledger,
            refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
        )
        final_status = status_payload(status_args)
        save_setup_ledger(final_status['setup_ledger'], ledger_path)
        emit({
            'status': 'skipped',
            'phase': 'import',
            'operator_id': args.operator_id,
            'reason': due.get('reason'),
            'refresh_due': due,
            'setup_ledger': final_status['setup_ledger'],
            'next': next_action_payload(status_args)['next'],
        })
        return 0

    import_phase = {
        'status': 'refresh_due',
        'source': 'explicit_import_phase',
        'people_csv': '.powerpacks/network-import/merged/people.csv'
        if (ROOT / '.powerpacks/network-import/merged/people.csv').exists()
        else '',
        'refresh_due': due,
        'live_refresh': ((ledger.get('phases') or {}).get('import') or {}).get('live_refresh', {}),
    }
    ledger.setdefault('phases', {})['import'] = import_phase
    ledger['status'] = 'refresh_due'
    save_setup_ledger(ledger, ledger_path)

    refresh_payload, refresh_code = run_live_refresh(args, ledger, accounts, due)
    ledger = load_setup_ledger(ledger_path)
    if refresh_code:
        ledger.setdefault('phases', {}).setdefault('import', {})['live_refresh'] = {
            'status': refresh_payload.get('status'),
            'updated_at': now(),
            'payload': refresh_payload,
        }
        ledger['status'] = refresh_payload.get('status', 'blocked')
        save_setup_ledger(ledger, ledger_path)
        emit({'status': refresh_payload.get('status'), 'phase': 'import', **refresh_payload})
        return refresh_code

    refresh = refresh_payload['refresh']
    ledger.setdefault('phases', {})['import'] = {
        'status': 'ready',
        'source': 'live_refresh',
        'people_csv': '.powerpacks/network-import/merged/people.csv',
        'live_refresh': refresh,
    }
    ledger['status'] = 'ready'
    save_setup_ledger(ledger, ledger_path)
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=args.accounts,
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
    )
    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': 'completed',
        'phase': 'import',
        'operator_id': args.operator_id,
        'refresh': refresh,
        'results': refresh_payload.get('results', {}),
        'setup_ledger': final_status['setup_ledger'],
        'next': next_action_payload(status_args)['next'],
    })
    return 0


def run_fan_in_phase(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    ledger = load_setup_ledger(ledger_path)
    accounts = accounts_summary(Path(args.accounts))

    started_at = now()
    before_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    code, payload, stderr = run_json_command(network_fan_in_command(args, force=bool(getattr(args, 'force', False))))
    if code == 20 or payload.get('status') == 'blocked_approval':
        emit({'status': 'blocked_approval', 'phase': 'fan-in', 'payload': payload, 'stderr': tail(stderr)})
        return 20
    if code != 0:
        emit({'status': 'failed', 'phase': 'fan-in', 'payload': payload, 'stderr': tail(stderr)})
        return 1

    promoted = promote_network_artifacts(payload.get('artifacts') or {})
    after_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    completed = {
        'status': 'completed',
        'started_at': started_at,
        'completed_at': now(),
        'artifact_dir': payload.get('artifact_dir') or '.powerpacks/network-import/final',
        'ledger': str(SETUP_REFRESH_LEDGER),
        'source_fingerprint': linked_source_fingerprint(accounts),
        'linked_sources': linked_sources(accounts),
        'network_changed': bool(after_hash and before_hash != after_hash),
        'before_people_sha256': before_hash,
        'after_people_sha256': after_hash,
        'promoted': promoted,
        'artifact_hashes': artifact_hashes(promoted),
    }
    ledger.setdefault('phases', {})['import'] = {
        'status': 'ready',
        'source': 'fan_in',
        'people_csv': '.powerpacks/network-import/merged/people.csv',
        'live_refresh': completed,
    }
    ledger['status'] = 'ready'
    save_setup_ledger(ledger, ledger_path)
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=args.accounts,
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
    )
    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': 'completed',
        'phase': 'fan-in',
        'operator_id': args.operator_id,
        'refresh': completed,
        'payload': payload,
        'setup_ledger': final_status['setup_ledger'],
        'next': next_action_payload(status_args)['next'],
    })
    return 0


def run_index_phase(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    ledger = load_setup_ledger(ledger_path)
    process_started_at = now()
    process_code, process_payload, process_stderr = run_json_command(network_fan_in_command(args, force=True, merge_only=True))
    if process_code == 20 or process_payload.get('status') == 'blocked_approval':
        emit({'status': 'blocked_approval', 'phase': 'process', 'payload': process_payload, 'stderr': tail(process_stderr)})
        return 20
    if process_code != 0:
        emit({'status': 'failed', 'phase': 'process', 'payload': process_payload, 'stderr': tail(process_stderr)})
        return 1
    promoted = promote_network_artifacts(process_payload.get('artifacts') or {})
    ledger = load_setup_ledger(ledger_path)
    ledger.setdefault('phases', {})['process'] = {
        'status': 'completed',
        'source': 'index_phase',
        'started_at': process_started_at,
        'completed_at': now(),
        'artifact_dir': process_payload.get('artifact_dir') or '.powerpacks/network-import/final',
        'ledger': str(SETUP_REFRESH_LEDGER),
        'people_csv': '.powerpacks/network-import/merged/people.csv',
        'promoted': promoted,
        'artifact_hashes': artifact_hashes(promoted),
    }
    ledger.setdefault('phases', {})['import'] = {
        **(ledger.get('phases', {}).get('import') or {}),
        'status': 'ready',
        'source': 'process',
        'people_csv': '.powerpacks/network-import/merged/people.csv',
    }
    ledger['status'] = 'ready'
    save_setup_ledger(ledger, ledger_path)
    index_payload, index_code = run_processing_index(args, ledger, ledger_path)
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=getattr(args, 'accounts', '.powerpacks/ingestion/accounts.json'),
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
    )
    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    index_summary = {
        'status': index_payload.get('status'),
        'source': index_payload.get('source', ''),
        'step': index_payload.get('step', ''),
        'estimated_cost_usd': index_payload.get('estimated_cost_usd', 0),
        'estimated_paid_calls': index_payload.get('estimated_paid_calls', {}),
        'processing_estimate': index_payload.get('processing_estimate', {}),
        'local_records_restore': index_payload.get('local_records_restore', {}),
        'people_csv': index_payload.get('people_csv', '.powerpacks/network-import/merged/people.csv'),
        'people_sha256': index_payload.get('people_sha256', ''),
        'duckdb': index_payload.get('duckdb', '.powerpacks/search-index/local-search.duckdb'),
        'processing_counts': (index_payload.get('processing') or {}).get('counts', {}) if isinstance(index_payload.get('processing'), dict) else {},
        'local_duckdb': index_payload.get('local_duckdb', {}),
        'local_duckdb_dq': index_payload.get('local_duckdb_dq', {}),
        'error': index_payload.get('error', ''),
    }
    emit({
        'status': index_payload.get('status'),
        'phase': 'index',
        'operator_id': args.operator_id,
        'index': index_summary,
        'setup_ledger_path': str(ledger_path),
        'next': next_action_payload(status_args)['next'],
    })
    return index_code


def setup_commands(args: argparse.Namespace) -> dict[str, str]:
    return {
        'discover_contacts_dry_run': f'uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run --dry-run --from-accounts {args.accounts} --operator-id {args.operator_id} --include-existing-artifacts',
        'discover_contacts_run': f'uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run --from-accounts {args.accounts} --operator-id {args.operator_id} --include-existing-artifacts',
        'discover_contacts_fan_in': f'uv run --project . python packs/ingestion/primitives/setup/setup.py fan-in --operator-id {args.operator_id} --accounts {args.accounts} --setup-ledger {args.setup_ledger} --force',
        'discover_contacts_status': 'uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py status',
        'discover_contacts_continue': 'uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py continue',
        'discover_contacts_approve': 'uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py approve',
        'processing_plan': processing_plan_command_text(),
        'processing_dry_run': processing_dry_run_command_text(args.operator_id),
        'processing_run': processing_run_command_text(args.operator_id),
        'processing_continue': 'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue --ledger .powerpacks/search-index/ledger.json',
        'build_local_duckdb_shim': build_local_duckdb_shim_command_text(args.operator_id),
}


def handoff_payload(args: argparse.Namespace) -> dict[str, Any]:
    approvals = approval_payload()
    idx = indexing_readiness(args.operator_id)
    commands = setup_commands(args)
    worker_jobs = []
    acct = accounts_summary(Path(args.accounts))
    for ch, rec in sorted((acct.get('channels') or {}).items()):
        if isinstance(rec, dict) and rec.get('status') != 'linked':
            continue
        cfg = rec.get('config') if isinstance(rec.get('config'), dict) else {}
        if ch == 'gmail':
            emails = cfg.get('selected_accounts') or cfg.get('account_emails') or []
            worker_jobs.append({
                'id': 'gmail',
                'source': 'gmail',
                'account_emails': emails,
                'account_count': len(emails),
                'parallelizable': False,
                'ledger': str(SETUP_REFRESH_LEDGER),
                'command': commands['discover_contacts_run'] + f' --ledger {SETUP_REFRESH_LEDGER} --only-source gmail --force',
            })
        else:
            if ch == 'messages':
                command = ''
                requires_approval = []
                ledger = ''
            elif ch == 'twitter':
                command = ''
                requires_approval = []
                ledger = ''
            else:
                command = commands['discover_contacts_run'] + f' --ledger {SETUP_REFRESH_LEDGER} --only-source {ch} --force'
                requires_approval = []
                ledger = str(SETUP_REFRESH_LEDGER)
            worker_jobs.append({
                'id': ch,
                'source': ch,
                'parallelizable': False,
                'ledger': ledger,
                'command': command,
                'requires_approval': requires_approval,
                **(
                    {'status': 'recorded_only', 'reason': 'Run $import-messages through the agent harness.'}
                    if ch == 'messages'
                    else {'status': 'recorded_only', 'reason': 'Twitter/X handle is recorded; follower import is not wired into setup yet.'}
                    if ch == 'twitter'
                    else {}
                ),
            })
    worker_group = {'parallel': False, 'fan_in': 'setup import uses the single setup refresh ledger', 'jobs': worker_jobs}
    if not worker_jobs:
        worker_group.update({'status': 'no_linked_sources', 'reason': 'No linked account sources were discovered; run the link phase before dispatching import workers.'})
    payload = {
        'status': 'ok',
        'operator_id': args.operator_id,
        'commands': commands,
        'worker_groups': {'import': worker_group},
        'indexing': idx,
        'requires_approval': approvals,
        'local_only_command_ids': [
            'discover_contacts_dry_run',
            'discover_contacts_status',
            'processing_plan',
            'processing_dry_run',
            'build_local_duckdb_shim',
        ],
    }
    ledger = load_setup_ledger(Path(args.setup_ledger))
    ledger['handoff'] = {'generated_at': now(), 'requires_approval': approvals, 'commands': commands}
    save_setup_ledger(ledger, Path(args.setup_ledger))
    return payload


def run_handoff(args: argparse.Namespace) -> int:
    emit(handoff_payload(args))
    return 0


def network_refresh_command(args: argparse.Namespace, *, force: bool, gmail_sync_after: str = '') -> list[str]:
    cmd = [
        sys.executable,
        'packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py',
        'run',
        '--from-accounts',
        args.accounts,
        '--operator-id',
        args.operator_id,
        '--include-existing-artifacts',
        '--ledger',
        str(SETUP_REFRESH_LEDGER),
        '--source-import-only',
    ]
    if force:
        cmd.append('--force')
    if gmail_sync_after:
        cmd.extend(['--gmail-sync-after', gmail_sync_after])
    return cmd


def network_fan_in_command(args: argparse.Namespace, *, force: bool, merge_only: bool = False) -> list[str]:
    return [
        sys.executable,
        'packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py',
        'fan-in',
        '--operator-id',
        args.operator_id,
        '--accounts',
        args.accounts,
        '--manifest',
        '.powerpacks/network-import/index/contacts/manifest.json',
    ]


def promote_network_artifacts(artifacts: dict[str, Any]) -> dict[str, str]:
    promoted: dict[str, str] = {}
    merged_people = artifacts.get('merged_people_csv')
    if merged_people:
        source_dir = Path(str(merged_people)).parent
        dest_dir = ROOT / '.powerpacks/network-import/merged'
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in [
            'people.csv',
            'people_harmonic_all.merged.csv',
            'network_contacts.csv',
            'network_contact_sources.csv',
            'network_companies.csv',
            'merge_manifest.json',
            'possible_duplicates_review.csv',
        ]:
            src = source_dir / name
            if src.exists():
                dst = dest_dir / name
                shutil.copy2(src, dst)
                promoted[f'merged_{name}'] = str(dst)
    return promoted


def run_live_refresh(args: argparse.Namespace, ledger: dict[str, Any], accounts: dict[str, Any], due: dict[str, Any]) -> tuple[dict[str, Any], int]:
    started_at = now()
    before_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    results: dict[str, Any] = {'status': 'running', 'started_at': started_at, 'reason': due.get('reason')}
    progress(f"Import refresh started: reason={due.get('reason')}, linked_sources={','.join(due.get('linked_sources') or linked_sources(accounts)) or 'none'}")

    refresh_reason = str(due.get('reason') or '')
    force_reasons = {'forced', 'refresh_interval_elapsed', 'linked_sources_changed', 'missing_import_artifact', 'import_artifact_drift'}
    force_network = refresh_reason in force_reasons
    # Leave Gmail mailbox cursoring to discover_contacts_pipeline. That layer can
    # inspect msgvault per account and pass sync-full --after from the newest
    # local source/message timestamp. The setup wrapper should not replace that
    # with a broad run-level lookback.
    gmail_sync_after = ''
    network_cmd = network_refresh_command(args, force=force_network, gmail_sync_after=gmail_sync_after)
    progress("Starting network import refresh (Gmail/LinkedIn/Twitter as linked)...")
    progress("Command: " + ' '.join(shlex.quote(part) for part in network_cmd))
    code, payload, stderr = run_json_command(network_cmd, stream_stderr=True)
    progress(f"Network import step finished with code={code}, status={payload.get('status') or 'unknown'}")
    results['network'] = {'code': code, 'payload': payload, 'stderr': tail(stderr)}
    if code == 20 or payload.get('status') == 'blocked_approval':
        results['status'] = 'blocked_approval'
        results['completed_at'] = now()
        results['failed_step'] = 'network_import'
        return {'status': 'blocked_approval', 'step': 'network_import', 'refresh': results, 'payload': payload}, 20
    if code != 0:
        results['status'] = 'failed'
        results['completed_at'] = now()
        results['failed_step'] = 'network_import'
        return {'status': 'failed', 'step': 'network_import', 'refresh': results, 'error': payload or tail(stderr)}, 1

    promoted = promote_network_artifacts(payload.get('artifacts') or {})
    after_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    progress(f"Import refresh completed: network_changed={bool(after_hash and before_hash != after_hash)}")
    completed = {
        'status': 'completed',
        'started_at': started_at,
        'completed_at': now(),
        'artifact_dir': payload.get('artifact_dir') or '.powerpacks/network-import/discover',
        'ledger': str(SETUP_REFRESH_LEDGER),
        'source_fingerprint': due.get('source_fingerprint') or linked_source_fingerprint(accounts),
        'linked_sources': due.get('linked_sources') or linked_sources(accounts),
        'network_changed': bool(after_hash and before_hash != after_hash),
        'before_people_sha256': before_hash,
        'after_people_sha256': after_hash,
        'promoted': promoted,
        'artifact_hashes': artifact_hashes(promoted),
        'gmail_sync_after': gmail_sync_after,
        'gmail_sync_lookback_days': int(getattr(args, 'gmail_sync_lookback_days', DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS)),
    }
    return {'status': 'completed', 'refresh': completed, 'results': results}, 0


def run_setup(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=args.accounts,
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=args.refresh_interval_hours,
    )
    status = status_payload(status_args)
    save_setup_ledger(status['setup_ledger'], ledger_path)
    ledger = status['setup_ledger']
    accounts = status['accounts']
    import_phase = ledger.get('phases', {}).get('import', {})
    idx = status.get('search_index_readiness') if isinstance(status.get('search_index_readiness'), dict) else {}
    has_linked_sources = bool(linked_sources(accounts))
    has_indexable_artifacts = idx.get('status') in ('search_ready', 'records_only_duckdb_missing', 'people_csv_ready_for_processing')
    if not has_linked_sources and not has_indexable_artifacts and import_phase.get('status') != 'refresh_due':
        return run_link_phase(args)

    due = import_phase.get('refresh_due') if isinstance(import_phase.get('refresh_due'), dict) else {}
    forced_due = import_refresh_due(
        ledger,
        accounts,
        int(getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS)),
        force_refresh=True,
    )
    if forced_due.get('due'):
        import_phase = {
            'status': 'refresh_due',
            'source': 'forced_setup_refresh',
            'people_csv': '.powerpacks/network-import/merged/people.csv'
            if (ROOT / '.powerpacks/network-import/merged/people.csv').exists()
            else '',
            'refresh_due': forced_due,
            'live_refresh': import_phase.get('live_refresh') if isinstance(import_phase.get('live_refresh'), dict) else {},
        }
        ledger.setdefault('phases', {})['import'] = import_phase
        ledger['status'] = 'refresh_due'
        save_setup_ledger(ledger, ledger_path)
        due = forced_due

    if import_phase.get('status') == 'refresh_due':
        refresh_payload, refresh_code = run_live_refresh(args, ledger, accounts, due)
        ledger = load_setup_ledger(ledger_path)
        if refresh_code:
            ledger.setdefault('phases', {}).setdefault('import', {})['live_refresh'] = {
                'status': refresh_payload.get('status'),
                'updated_at': now(),
                'payload': refresh_payload,
            }
            ledger['status'] = refresh_payload.get('status', 'blocked')
            save_setup_ledger(ledger, ledger_path)
            emit({'status': refresh_payload.get('status'), **refresh_payload})
            return refresh_code
        refresh = refresh_payload['refresh']
        ledger.setdefault('phases', {})['import'] = {
            'status': 'ready',
            'source': 'live_refresh',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'live_refresh': refresh,
        }
        ledger['status'] = 'ready'
        save_setup_ledger(ledger, ledger_path)

    ledger = load_setup_ledger(ledger_path)
    index_payload, index_code = run_processing_index(args, ledger, ledger_path)
    if index_code:
        emit({
            'status': index_payload.get('status'),
            'operator_id': args.operator_id,
            'index': index_payload,
        })
        return index_code

    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': final_status['setup_ledger'].get('status'),
        'operator_id': args.operator_id,
        'setup_ledger': final_status['setup_ledger'],
        'search_index_readiness': final_status['search_index_readiness'],
        'next_actions': final_status['next_actions'],
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('status')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.set_defaults(func=run_status)

    s = sub.add_parser('next')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.set_defaults(func=run_next)

    s = sub.add_parser('link')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--gmail-db', default='')
    s.add_argument('--gmail-account', action='append', default=[])
    s.add_argument('--gmail-add-email', action='append', default=[])
    s.add_argument('--gmail-authorized-email', action='append', default=[])
    s.add_argument('--gmail-all', action='store_true')
    s.add_argument('--skip-source', action='append', choices=['messages', 'gmail', 'linkedin_csv', 'twitter'], default=[])
    s.add_argument('--linkedin-csv', default='')
    s.add_argument('--linkedin-source-user', default='')
    s.add_argument('--messages-contacts-csv', default='', help=argparse.SUPPRESS)
    s.add_argument('--messages-check', action='store_true')
    s.add_argument('--skip-messages-whatsapp', action='store_true')
    s.add_argument('--twitter-handle', default='')
    s.set_defaults(func=run_link_phase)

    s = sub.add_parser('import')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--gmail-sync-lookback-days', type=int, default=DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS)
    s.add_argument('--only-if-due', action='store_true')
    s.set_defaults(func=run_import_phase)

    s = sub.add_parser('fan-in')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--force', action='store_true')
    s.add_argument('--only-source', action='append', choices=['gmail', 'linkedin_csv', 'messages', 'twitter'], default=[])
    s.add_argument('--resolve-gmail-linkedin', action='store_true', help='Run Gmail email-to-LinkedIn resolution with Parallel during enrichment fan-in.')
    s.add_argument('--approve-parallel-spend', action='store_true', help='Auto-approve Parallel.ai spend without blocking.')
    s.add_argument('--gmail-linkedin-limit', type=int, default=None, help='Max Gmail contacts to resolve via Parallel')
    s.add_argument('--merge-only', action='store_true', help=argparse.SUPPRESS)
    s.set_defaults(func=run_fan_in_phase)

    s = sub.add_parser('index')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.set_defaults(func=run_index_phase)

    s = sub.add_parser('handoff')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', required=True)
    s.add_argument('--setup-ledger', required=True)
    s.set_defaults(func=run_handoff)

    s = sub.add_parser('run')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--gmail-sync-lookback-days', type=int, default=DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS)
    s.add_argument('--gmail-db', default='')
    s.add_argument('--gmail-account', action='append', default=[])
    s.add_argument('--gmail-add-email', action='append', default=[])
    s.add_argument('--gmail-authorized-email', action='append', default=[])
    s.add_argument('--gmail-all', action='store_true')
    s.add_argument('--skip-source', action='append', choices=['messages', 'gmail', 'linkedin_csv', 'twitter'], default=[])
    s.add_argument('--linkedin-csv', default='')
    s.add_argument('--linkedin-source-user', default='')
    s.add_argument('--messages-contacts-csv', default='', help=argparse.SUPPRESS)
    s.add_argument('--messages-check', action='store_true')
    s.add_argument('--skip-messages-whatsapp', action='store_true')
    s.add_argument('--twitter-handle', default='')
    s.set_defaults(func=run_setup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))

if __name__ == '__main__':
    raise SystemExit(main())
