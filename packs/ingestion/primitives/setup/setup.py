#!/usr/bin/env python3
"""Stdlib-only setup orchestration primitive for ingestion bootstrap safety."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path.cwd()
SETUP_LEDGER = Path('.powerpacks/setup/setup-run.json')
REQUIRED_PRIVACY = [
    'raw_msgvault_db_copied', 'raw_mail_copied', 'message_bodies_copied',
    'attachments_copied', 'secrets_copied',
]
PRIVACY_ALIASES = {
    'raw_msgvault_db_copied': ['raw_msgvault_db_copied', 'raw_msgvault_db'],
    'raw_mail_copied': ['raw_mail_copied', 'raw_mail'],
    'message_bodies_copied': ['message_bodies_copied', 'message_bodies'],
    'attachments_copied': ['attachments_copied', 'attachments'],
    'secrets_copied': ['secrets_copied', 'secrets'],
}
ALLOWED_ROOTS = [
    PurePosixPath('.powerpacks/search-index'),
    PurePosixPath('.powerpacks/network-import/merged'),
    PurePosixPath('.powerpacks/network-import/profile_cache_v2'),
    PurePosixPath('.powerpacks/operator-bootstrap/restore-manifest.json'),
]
APPROVALS = [
    ('browser_auth', 'Browser/Gmail OAuth authorization requires user approval.'),
    ('gcp_console_oauth_app', 'GCP Console/OAuth app automation requires user approval.'),
    ('oauth_test_users', 'OAuth test-user changes require user approval.'),
    ('gcs_download', 'GCS bootstrap download requires --allow-gcs-download and user approval.'),
    ('destructive_restore_overwrite', 'Destructive bootstrap overwrite requires --force and user approval.'),
    ('provider_spend', 'RapidAPI/Parallel/OpenAI spend requires explicit allow flags.'),
    ('uploads_research', 'Uploads/research actions require approval.'),
    ('provider_allow_flags', 'Provider allow flags require approval.'),
]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_setup_ledger(path: Path = SETUP_LEDGER) -> dict[str, Any]:
    if path.exists():
        try:
            return read_json(path)
        except Exception:
            pass
    return {
        'schema_version': 1,
        'status': 'pending',
        'phases': {phase: {'status': 'pending'} for phase in ['bootstrap', 'link', 'import', 'index']},
        'approval_requirements': [],
    }


def save_setup_ledger(ledger: dict[str, Any], path: Path = SETUP_LEDGER) -> None:
    ledger['updated_at'] = now()
    write_json(path, ledger)


def safe_member_name(name: str) -> PurePosixPath:
    pp = PurePosixPath(name)
    if pp.is_absolute() or any(part in ('..', '') for part in pp.parts):
        raise ValueError(f'unsafe tar member path: {name}')
    return pp


def validate_tar_members(tf: tarfile.TarFile) -> list[str]:
    names = []
    for m in tf.getmembers():
        safe_member_name(m.name)
        if m.issym() or m.islnk() or m.isdev():
            raise ValueError(f'unsafe tar member type: {m.name}')
        names.append(m.name)
    return names


def extract_member_bytes(tf: tarfile.TarFile, name: str) -> bytes:
    f = tf.extractfile(name)
    if f is None:
        raise ValueError(f'missing file content: {name}')
    return f.read()


def find_manifests(tf: tarfile.TarFile, names: list[str]) -> tuple[str, str]:
    op = [n for n in names if len(PurePosixPath(n).parts) == 2 and PurePosixPath(n).name == 'manifest.json' and PurePosixPath(n).parts[0] != '.powerpacks']
    restore = '.powerpacks/operator-bootstrap/restore-manifest.json'
    if not op:
        raise ValueError('operator manifest not found')
    if restore not in names:
        raise ValueError('restore manifest not found')
    return sorted(op)[0], restore


def privacy_flags(manifest: dict[str, Any]) -> tuple[dict[str, bool | None], list[str], list[str]]:
    containers = []
    if isinstance(manifest.get('privacy'), dict):
        containers.append(manifest['privacy'])
    sync = ((manifest.get('stages') or {}).get('sync') or {}) if isinstance(manifest.get('stages'), dict) else {}
    if isinstance(sync.get('included'), dict):
        containers.append(sync['included'])
    found: dict[str, bool | None] = {}
    missing, unsafe = [], []
    for flag in REQUIRED_PRIVACY:
        val: Any = None
        present = False
        for container in containers:
            for key in PRIVACY_ALIASES[flag]:
                if key in container:
                    val = container[key]
                    present = True
                    break
            if present:
                break
        found[flag] = val if present else None
        if not present:
            missing.append(flag)
        elif val is not False:
            unsafe.append(flag)
    return found, missing, unsafe


def allowed_restore_path(pp: PurePosixPath) -> bool:
    if str(pp).startswith('.powerpacks/network-import/network-runs/'):
        parts = pp.parts
        return len(parts) >= 4 and re.fullmatch(r'[A-Za-z0-9._-]+', parts[3] or '') is not None
    for root in ALLOWED_ROOTS:
        if pp == root or str(pp).startswith(str(root) + '/'):
            return True
    return False


def classify_restore(paths: list[str]) -> dict[str, list[str]]:
    out = {'allowed': [], 'blocked': []}
    for p in sorted(set(paths)):
        try:
            pp = safe_member_name(p)
        except ValueError:
            out['blocked'].append(p)
            continue
        (out['allowed'] if allowed_restore_path(pp) else out['blocked']).append(p)
    return out


def set_problem(payload: dict[str, Any], status: str, error: Any, *, reason: bool = False) -> None:
    payload['status'] = status
    if reason:
        payload['reason'] = error
    else:
        payload.setdefault('errors', []).append(error)


def inspect_bundle(bundle: Path, allow_legacy: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {'status': 'ok', 'bundle': str(bundle), 'bundle_sha256': sha256_file(bundle)}
    with tarfile.open(bundle, 'r:*') as tf:
        names = validate_tar_members(tf)
        op_name, restore_name = find_manifests(tf, names)
        op_bytes = extract_member_bytes(tf, op_name)
        restore_bytes = extract_member_bytes(tf, restore_name)
        manifest = json.loads(op_bytes.decode('utf-8'))
        restore = json.loads(restore_bytes.decode('utf-8'))
    payload.update({
        'operator_manifest_member': op_name,
        'restore_manifest_member': restore_name,
        'operator_manifest_sha256': sha256_bytes(op_bytes),
        'restore_manifest_sha256': sha256_bytes(restore_bytes),
        'operator_id': manifest.get('operator_id'),
        'operator': manifest.get('operator'),
        'schema_version': manifest.get('schema_version'),
    })
    if manifest.get('schema_version') not in (1,):
        set_problem(payload, 'needs_user_action', 'unsupported schema_version', reason=True)
    if not manifest.get('operator_id') or restore.get('operator_id') != manifest.get('operator_id'):
        set_problem(payload, 'needs_user_action', 'operator_id mismatch or missing')
    flags, missing, unsafe = privacy_flags(manifest)
    payload['privacy_flags'] = flags
    if unsafe:
        set_problem(payload, 'rejected', {'unsafe_privacy_flags': unsafe})
    if missing and not allow_legacy:
        payload['status'] = 'needs_user_action'
        payload['legacy_privacy_override_required'] = True
        payload['missing_privacy_flags'] = missing
    restore_paths = list(restore.get('normal_pipeline_outputs') or []) + ['.powerpacks/operator-bootstrap/restore-manifest.json']
    payload['restore_root_classification'] = classify_restore(restore_paths)
    payload['would_restore'] = payload['restore_root_classification']['allowed']
    payload['would_overwrite'] = [p for p in payload['would_restore'] if (ROOT / p).exists()]
    payload['manifest'] = {'operator': manifest.get('operator'), 'operator_id': manifest.get('operator_id')}
    return payload


def run_inspect(args: argparse.Namespace) -> int:
    try:
        payload = inspect_bundle(Path(args.bundle), args.allow_legacy_bootstrap_manifest)
    except Exception as exc:
        payload = {'status': 'rejected', 'error': str(exc)}
    emit(payload)
    return 0 if payload.get('status') == 'ok' else 2


def redacted_error(text: str) -> str:
    return re.sub(r'/(?:var/tmp|tmp|[^\s]*)/[^\s]*', '<redacted-path>', text or '')


def parse_exact_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith('gs://') or uri.endswith('/') or '*' in uri:
        raise ValueError('gcs-uri must be an exact object gs:// URI')
    rest = uri[5:]
    bucket, sep, object_name = rest.partition('/')
    if not bucket or not sep or not object_name:
        raise ValueError('gcs-uri must be an exact object gs:// URI')
    return bucket, object_name


def materialize_raw_google_credentials(env: dict[str, str], *, needs_config: bool) -> tuple[dict[str, str], Path | None, Path | None]:
    tmp_key = None
    cfg = None
    gac = env.get('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
    if gac.startswith('{'):
        tmp_dir = ROOT / '.powerpacks/setup/tmp'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, key_path = tempfile.mkstemp(prefix='gcloud-key-', suffix='.json', dir=str(tmp_dir))
        os.close(fd)
        tmp_key = Path(key_path)
        tmp_key.write_text(gac, encoding='utf-8')
        tmp_key.chmod(0o600)
        env['GOOGLE_APPLICATION_CREDENTIALS'] = str(tmp_key)
        if needs_config:
            cfg = Path(tempfile.mkdtemp(prefix='gcloud-config-', dir=str(tmp_dir)))
            env['CLOUDSDK_CONFIG'] = str(cfg)
    return env, tmp_key, cfg


def run_gcloud_download(gcs_uri: str, out: Path, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    cp = subprocess.run(
        ['gcloud', 'storage', 'cp', gcs_uri, str(out)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        return 2, {
            'status': 'needs_user_action',
            'reason': 'gcloud storage cp failed',
            'guidance': 'Run gcloud auth login --no-launch-browser, install Google Cloud CLI, or retry with --download-backend python.',
            'stderr': redacted_error(cp.stderr),
        }
    return 0, {'status': 'ok', 'download_backend': 'gcloud'}


def run_python_gcs_download(gcs_uri: str, out: Path, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        from google.cloud import storage  # type: ignore
    except Exception as exc:
        return 2, {
            'status': 'needs_user_action',
            'reason': 'google-cloud-storage is unavailable',
            'guidance': 'Run through uv (`uv run --project . python ...`) so project dependencies are available, or use --download-backend gcloud.',
            'error': str(exc),
        }
    bucket_name, object_name = parse_exact_gcs_uri(gcs_uri)
    try:
        client = storage.Client()
        client.bucket(bucket_name).blob(object_name).download_to_filename(str(out))
    except Exception as exc:
        return 2, {
            'status': 'needs_user_action',
            'reason': 'python google-cloud-storage download failed',
            'guidance': 'Verify ADC/service-account credentials and exact-object permissions, or retry with --download-backend gcloud.',
            'error': redacted_error(str(exc)),
        }
    return 0, {'status': 'ok', 'download_backend': 'python-google-cloud-storage'}


def run_pull(args: argparse.Namespace) -> int:
    if not args.allow_gcs_download:
        emit({
            'status': 'needs_user_action',
            'reason': 'GCS download requires --allow-gcs-download',
            'requires_approval': [{'id': 'gcs_download'}],
        })
        return 2
    try:
        parse_exact_gcs_uri(str(args.gcs_uri))
    except ValueError as exc:
        emit({'status': 'rejected', 'reason': 'gcs-uri must be an exact object gs:// URI'})
        return 2
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    tmp_key = None
    cfg = None
    try:
        backend = getattr(args, 'download_backend', 'auto')
        use_gcloud = backend == 'gcloud' or (backend == 'auto' and shutil.which('gcloud'))
        env, tmp_key, cfg = materialize_raw_google_credentials(env, needs_config=use_gcloud)
        if use_gcloud and tmp_key:
            auth = subprocess.run(
                ['gcloud', 'auth', 'activate-service-account', '--key-file', str(tmp_key)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if auth.returncode != 0:
                if backend == 'auto':
                    code, payload = run_python_gcs_download(args.gcs_uri, out, env)
                    payload['gcloud_fallback_reason'] = 'gcloud service-account activation failed'
                    payload['gcloud_stderr'] = redacted_error(auth.stderr)
                    if code == 0:
                        payload.update({'output': str(out), 'bundle_sha256': sha256_file(out) if out.exists() else ''})
                    emit(payload)
                    return code
                emit({
                    'status': 'needs_user_action',
                    'reason': 'gcloud service-account activation failed',
                    'guidance': 'Run gcloud auth login --no-launch-browser or provide a valid service account.',
                    'stderr': redacted_error(auth.stderr),
                })
                return 2
        if use_gcloud:
            code, payload = run_gcloud_download(args.gcs_uri, out, env)
            if code != 0 and backend == 'auto':
                gcloud_payload = payload
                code, payload = run_python_gcs_download(args.gcs_uri, out, env)
                payload['gcloud_fallback_reason'] = gcloud_payload.get('reason')
                if gcloud_payload.get('stderr'):
                    payload['gcloud_stderr'] = gcloud_payload.get('stderr')
        elif backend in ('auto', 'python'):
            code, payload = run_python_gcs_download(args.gcs_uri, out, env)
        else:
            code, payload = 2, {'status': 'rejected', 'reason': f'unsupported download backend: {backend}'}
        if code == 0:
            payload.update({'output': str(out), 'bundle_sha256': sha256_file(out) if out.exists() else ''})
        emit(payload)
        return code
    finally:
        if tmp_key:
            try:
                tmp_key.unlink()
            except FileNotFoundError:
                pass
        if cfg:
            shutil.rmtree(cfg, ignore_errors=True)


def copy_replace(src: Path, dst: Path, force: bool, backup_root: Path, overwritten: list[str]) -> None:
    if dst.exists():
        if not force:
            raise FileExistsError(f'target exists: {dst}')
        rel = dst.relative_to(ROOT)
        backup = backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_dir():
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(dst, backup)
            shutil.rmtree(dst)
        else:
            shutil.copy2(dst, backup)
            dst.unlink()
        overwritten.append(str(rel))
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def mark_restored_ledgers(paths: list[Path], operator_id: str) -> list[str]:
    touched = []
    for p in paths:
        if p.name.endswith('.json') and p.exists():
            try:
                data = read_json(p)
                if isinstance(data, dict) and ('status' in data or 'steps' in data):
                    data['status'] = 'restored'
                    data['restored_from_operator_bootstrap'] = True
                    data['restored_operator_id'] = operator_id
                    if isinstance(data.get('steps'), list):
                        for s in data['steps']:
                            if isinstance(s, dict):
                                s['status'] = 'restored'
                    elif isinstance(data.get('steps'), dict):
                        for s in data['steps'].values():
                            if isinstance(s, dict):
                                s['status'] = 'restored'
                    write_json(p, data)
                    touched.append(str(p.relative_to(ROOT)))
            except Exception:
                pass
    return touched


def restore_candidates(wanted: set[str]) -> list[str]:
    candidates: list[str] = []
    restore_roots = [
        '.powerpacks/search-index',
        '.powerpacks/network-import/merged',
        '.powerpacks/network-import/profile_cache_v2',
    ]
    for root_rel in restore_roots:
        if any(p == root_rel or p.startswith(root_rel + '/') for p in wanted):
            candidates.append(root_rel)
    if '.powerpacks/operator-bootstrap/restore-manifest.json' in wanted:
        candidates.append('.powerpacks/operator-bootstrap/restore-manifest.json')
    candidates.extend(
        rel for rel in sorted(wanted)
        if rel.startswith('.powerpacks/network-import/network-runs/')
    )
    return list(dict.fromkeys(candidates))


def restored_ledger_paths(restored: list[str]) -> list[Path]:
    paths = [ROOT / rel / 'ledger.json' for rel in restored]
    network_runs = ROOT / '.powerpacks/network-import/network-runs'
    if network_runs.exists():
        paths.extend(network_runs.glob('*/**/*.json'))
    return paths


def apply_bundle(args: argparse.Namespace) -> dict[str, Any]:
    bundle = Path(args.bundle)
    inspect = inspect_bundle(bundle, args.allow_legacy_bootstrap_manifest)
    if inspect.get('status') not in ('ok',):
        return {'status': 'rejected', 'inspect': inspect}
    if inspect.get('operator_id') != args.operator_id:
        return {'status': 'rejected', 'reason': 'operator_id mismatch', 'inspect': inspect}
    if args.inspect_file:
        prior = read_json(Path(args.inspect_file))
        for key in ['bundle_sha256', 'operator_manifest_sha256', 'restore_manifest_sha256']:
            if prior.get(key) != inspect.get(key):
                return {'status': 'rejected', 'reason': f'inspect/apply hash mismatch: {key}', 'inspect': inspect}
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_root = ROOT / '.powerpacks/operator-bootstrap/backups' / ts
    overwritten: list[str] = []
    restored: list[str] = []
    with tempfile.TemporaryDirectory(prefix='setup-restore-') as td:
        tmp = Path(td)
        with tarfile.open(bundle, 'r:*') as tf:
            validate_tar_members(tf)
            try:
                tf.extractall(tmp, filter='data')
            except TypeError:
                tf.extractall(tmp)
        # manual post-extract destination containment check
        for p in tmp.rglob('*'):
            p.resolve().relative_to(tmp.resolve())
        # Copy only restore-manifest allowlisted roots and referenced child ledgers/outputs.
        wanted = set(inspect.get('would_restore') or [])
        for rel in restore_candidates(wanted):
            src = tmp / rel
            if src.exists():
                try:
                    copy_replace(src, ROOT / rel, bool(args.force), backup_root, overwritten)
                except FileExistsError as exc:
                    return {
                        'status': 'rejected',
                        'reason': str(exc),
                        'requires_approval': [{'id': 'destructive_restore_overwrite'}],
                        'inspect': inspect,
                    }
                restored.append(rel)
    touched = mark_restored_ledgers(restored_ledger_paths(restored), args.operator_id)
    op_slug = re.sub(r'[^A-Za-z0-9._-]+', '-', str(inspect.get('operator') or args.operator_id)).strip('-') or 'operator'
    provenance = {
        'applied_at': now(),
        'operator_id': args.operator_id,
        'bundle': str(bundle),
        'bundle_sha256': inspect['bundle_sha256'],
        'operator_manifest_sha256': inspect['operator_manifest_sha256'],
        'restore_manifest_sha256': inspect['restore_manifest_sha256'],
        'restored': restored,
        'overwritten': overwritten,
    }
    write_json(ROOT / '.powerpacks/operator-bootstrap/applied' / op_slug / 'manifest.json', provenance)
    ledger = load_setup_ledger(Path(args.setup_ledger))
    ledger['operator_id'] = args.operator_id
    ledger['phases']['bootstrap'] = {
        'status': 'restored',
        'bundle_sha256': inspect['bundle_sha256'],
        'restored': restored,
        'provenance': str(Path('.powerpacks/operator-bootstrap/applied') / op_slug / 'manifest.json'),
    }
    save_setup_ledger(ledger, Path(args.setup_ledger))
    return {
        'status': 'ok',
        **provenance,
        'backup_root': str(backup_root) if overwritten else '',
        'restored_ledgers': touched,
    }


def run_apply(args: argparse.Namespace) -> int:
    try: payload = apply_bundle(args)
    except Exception as exc: payload = {'status': 'rejected', 'error': str(exc)}
    emit(payload)
    return 0 if payload.get('status') == 'ok' else 2


def accounts_summary(path: Path) -> dict[str, Any]:
    if not path.exists(): return {'exists': False, 'channels': {}}
    try: data = read_json(path)
    except Exception as exc: return {'exists': True, 'error': str(exc), 'channels': {}}
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
    return {'exists': True, 'version': data.get('version'), 'channels': channels}


def indexing_readiness(operator_id: str) -> dict[str, Any]:
    si = ROOT / '.powerpacks/search-index'
    duck = si / 'local-search.duckdb'
    ledger = si / 'ledger.json'
    records = si / 'records'
    people = ROOT / '.powerpacks/network-import/merged/people.csv'
    if duck.exists() and ledger.exists():
        lg = read_json(ledger) if ledger.exists() else {}
        if lg.get('status') in ('completed', 'restored') and (not operator_id or lg.get('default_operator_id') in (None, operator_id) or lg.get('restored_operator_id') in (None, operator_id)):
            return {'status': 'search_ready', 'duckdb': str(duck), 'ledger': str(ledger)}
    if records.exists() and any(records.glob('*.records.jsonl')) and not duck.exists():
        return {'status': 'records_only_duckdb_missing', 'repair_command': f'uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --operator-id {operator_id} --force'}
    if people.exists():
        return {'status': 'people_csv_ready_for_processing', 'people_csv': str(people), 'plan_command': f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {operator_id}', 'requires_approval': ['provider_spend', 'provider_allow_flags']}
    return {'status': 'not_ready', 'missing': ['.powerpacks/network-import/merged/people.csv']}


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    ledger = load_setup_ledger(Path(args.setup_ledger))
    idx = indexing_readiness(args.operator_id)
    if idx['status'] == 'search_ready' and ledger.get('phases', {}).get('index', {}).get('status') == 'restored':
        ledger['phases']['index']['status'] = 'ready'
    bundles = sorted(
        str(p) for p in (ROOT / '.powerpacks/operator-bootstrap/bundles').glob('*.operator-bootstrap.tar.gz')
    )
    next_actions = []
    if bundles and ledger['phases']['bootstrap']['status'] == 'pending':
        next_actions.append('inspect-bootstrap')
    if not Path(args.accounts).exists():
        next_actions.append('link accounts with onboarding')
    if idx['status'] == 'records_only_duckdb_missing':
        next_actions.append(idx['repair_command'])
    elif idx['status'] == 'people_csv_ready_for_processing':
        next_actions.append(idx['plan_command'])
    return {
        'status': 'ok',
        'operator_id': args.operator_id,
        'accounts': accounts_summary(Path(args.accounts)),
        'setup_ledger': ledger,
        'bootstrap_bundle_candidates': bundles,
        'search_index_readiness': idx,
        'canonical_people_csv': {
            'path': '.powerpacks/network-import/merged/people.csv',
            'exists': (ROOT / '.powerpacks/network-import/merged/people.csv').exists(),
        },
        'next_actions': next_actions,
    }


def run_status(args: argparse.Namespace) -> int:
    payload = status_payload(args)
    save_setup_ledger(payload['setup_ledger'], Path(args.setup_ledger))
    emit(payload)
    return 0


def handoff_payload(args: argparse.Namespace) -> dict[str, Any]:
    approvals = [{'id': k, 'description': d} for k, d in APPROVALS]
    idx = indexing_readiness(args.operator_id)
    commands = {
        'import_network_dry_run': f'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --dry-run --from-accounts {args.accounts} --operator-id {args.operator_id}',
        'import_network_run': f'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --from-accounts {args.accounts} --operator-id {args.operator_id}',
        'import_network_fan_in': f'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --from-accounts {args.accounts} --operator-id {args.operator_id} --include-existing-artifacts --fan-in-only --ledger .powerpacks/network-import/import-network-run.fan-in.json --run-id setup-fan-in',
        'import_network_status': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py status',
        'import_network_continue': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue',
        'import_network_approve': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve',
        'processing_plan': f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {args.operator_id}',
        'processing_run': f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {args.operator_id}',
        'processing_continue': 'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue --output-dir .powerpacks/search-index',
        'build_local_duckdb_shim': f'uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --operator-id {args.operator_id} --force',
    }
    worker_jobs = []
    acct = accounts_summary(Path(args.accounts))
    for ch, rec in sorted((acct.get('channels') or {}).items()):
        if isinstance(rec, dict) and rec.get('status') != 'linked':
            continue
        cfg = rec.get('config') if isinstance(rec.get('config'), dict) else {}
        if ch == 'gmail':
            emails = cfg.get('selected_accounts') or cfg.get('account_emails') or []
            if not emails:
                emails = ['']
            for email in emails:
                slug = re.sub(r'[^A-Za-z0-9._-]+', '-', str(email or 'all').lower()).strip('-') or 'all'
                worker_jobs.append({
                    'id': f'gmail:{email or "all"}',
                    'source': 'gmail',
                    'account_email': email,
                    'parallelizable': True,
                    'ledger': f'.powerpacks/network-import/import-network-run.gmail.{slug}.json',
                    'run_id': f'setup-gmail-{slug}',
                    'command': commands['import_network_run'] + f' --only-source gmail --ledger .powerpacks/network-import/import-network-run.gmail.{slug}.json --run-id setup-gmail-{slug}' + (f' --gmail-account-emails {email}' if email else ''),
                })
        else:
            slug = re.sub(r'[^A-Za-z0-9._-]+', '-', ch.lower()).strip('-') or ch
            if ch == 'messages':
                whatsapp_cfg = cfg.get('whatsapp') if isinstance(cfg.get('whatsapp'), dict) else {}
                imessage_cfg = cfg.get('imessage') if isinstance(cfg.get('imessage'), dict) else {}
                include_flags = []
                if imessage_cfg.get('status') != 'skipped':
                    include_flags.append('--include-imessage')
                if whatsapp_cfg.get('status') == 'linked' or whatsapp_cfg.get('authenticated') is True:
                    include_flags.append('--include-whatsapp')
                include_flags.append('--include-contact-merge')
                command = (
                    'uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run'
                    ' --ledger .powerpacks/messages/import-run.setup-messages.json'
                    f' {" ".join(include_flags)}'
                )
                requires_approval = ['whatsapp_qr'] if '--include-whatsapp' in include_flags else []
            else:
                command = commands['import_network_run'] + f' --only-source {ch} --ledger .powerpacks/network-import/import-network-run.{slug}.json --run-id setup-{slug}'
                requires_approval = []
            worker_jobs.append({
                'id': ch,
                'source': ch,
                'parallelizable': True,
                'ledger': f'.powerpacks/messages/import-run.setup-messages.json' if ch == 'messages' else f'.powerpacks/network-import/import-network-run.{slug}.json',
                'run_id': f'setup-{slug}',
                'command': command,
                'requires_approval': requires_approval,
            })
    worker_group = {'parallel': True, 'fan_in': 'run commands.import_network_fan_in after all nonblocked worker jobs complete', 'jobs': worker_jobs}
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
            'import_network_dry_run',
            'import_network_status',
            'processing_plan',
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('status')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.set_defaults(func=run_status)

    s = sub.add_parser('inspect-bootstrap')
    s.add_argument('--bundle', required=True)
    s.add_argument('--allow-legacy-bootstrap-manifest', action='store_true')
    s.set_defaults(func=run_inspect)

    s = sub.add_parser('pull-bootstrap')
    s.add_argument('--gcs-uri', required=True)
    s.add_argument('--output', required=True)
    s.add_argument('--allow-gcs-download', action='store_true')
    s.add_argument('--download-backend', choices=['auto', 'gcloud', 'python'], default='auto', help='Use gcloud storage cp when available, or google-cloud-storage through uv for the Python backend.')
    s.set_defaults(func=run_pull)

    s = sub.add_parser('apply-bootstrap')
    s.add_argument('--bundle', required=True)
    s.add_argument('--operator-id', required=True)
    s.add_argument('--force', action='store_true')
    s.add_argument('--inspect-file', default='')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--allow-legacy-bootstrap-manifest', action='store_true')
    s.set_defaults(func=run_apply)

    s = sub.add_parser('handoff')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', required=True)
    s.add_argument('--setup-ledger', required=True)
    s.set_defaults(func=run_handoff)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))

if __name__ == '__main__':
    raise SystemExit(main())
