#!/usr/bin/env python3
"""Stdlib-only setup orchestration primitive for ingestion bootstrap safety."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path.cwd()
SETUP_LEDGER = Path('.powerpacks/setup/setup-run.json')
DEFAULT_REFRESH_INTERVAL_HOURS = 168
DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS = int(os.environ.get('POWERPACKS_SETUP_GMAIL_SYNC_LOOKBACK_DAYS', '14'))
DEFAULT_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS = int(os.environ.get('POWERPACKS_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS', '900'))
DEFAULT_AUTO_SPEND_LIMIT_USD = float(os.environ.get('POWERPACKS_SETUP_AUTO_SPEND_LIMIT_USD', '10'))
SETUP_REFRESH_LEDGER = Path('.powerpacks/network-import/import-network-run.setup-refresh.json')
SETUP_MESSAGES_LEDGER = Path('.powerpacks/messages/import-run.setup-messages.json')
SETUP_SOURCE_CHANNELS = ['gmail', 'linkedin_csv', 'messages', 'twitter']
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


def run_json_command(cmd: list[str], timeout: int = 6 * 60 * 60) -> tuple[int, dict[str, Any], str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    payload: dict[str, Any]
    try:
        parsed = parse_json_fragment(proc.stdout)
        payload = parsed if isinstance(parsed, dict) else {'payload': parsed}
    except json.JSONDecodeError:
        payload = {}
    return proc.returncode, payload, proc.stderr


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


def slugify(value: Any, fallback: str = 'operator') -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-') or fallback


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


def download_gcs_object(gcs_uri: str, output: Path, *, download_backend: str = 'auto') -> tuple[int, dict[str, Any]]:
    try:
        parse_exact_gcs_uri(str(gcs_uri))
    except ValueError:
        return 2, {'status': 'rejected', 'reason': 'gcs-uri must be an exact object gs:// URI'}
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    tmp_key = None
    cfg = None
    try:
        backend = download_backend
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
                    code, payload = run_python_gcs_download(gcs_uri, output, env)
                    payload['gcloud_fallback_reason'] = 'gcloud service-account activation failed'
                    payload['gcloud_stderr'] = redacted_error(auth.stderr)
                    if code == 0:
                        payload.update({'output': str(output), 'bundle_sha256': sha256_file(output) if output.exists() else ''})
                    return code, payload
                return 2, {
                    'status': 'needs_user_action',
                    'reason': 'gcloud service-account activation failed',
                    'guidance': 'Run gcloud auth login --no-launch-browser or provide a valid service account.',
                    'stderr': redacted_error(auth.stderr),
                }
        if use_gcloud:
            code, payload = run_gcloud_download(gcs_uri, output, env)
            if code != 0 and backend == 'auto':
                gcloud_payload = payload
                code, payload = run_python_gcs_download(gcs_uri, output, env)
                payload['gcloud_fallback_reason'] = gcloud_payload.get('reason')
                if gcloud_payload.get('stderr'):
                    payload['gcloud_stderr'] = gcloud_payload.get('stderr')
        elif backend in ('auto', 'python'):
            code, payload = run_python_gcs_download(gcs_uri, output, env)
        else:
            code, payload = 2, {'status': 'rejected', 'reason': f'unsupported download backend: {backend}'}
        if code == 0:
            payload.update({'output': str(output), 'bundle_sha256': sha256_file(output) if output.exists() else ''})
        return code, payload
    finally:
        if tmp_key:
            try:
                tmp_key.unlink()
            except FileNotFoundError:
                pass
        if cfg:
            shutil.rmtree(cfg, ignore_errors=True)


def run_pull(args: argparse.Namespace) -> int:
    if not args.allow_gcs_download:
        emit({
            'status': 'needs_user_action',
            'reason': 'GCS download requires --allow-gcs-download',
            'requires_approval': [{'id': 'gcs_download'}],
        })
        return 2
    code, payload = download_gcs_object(
        str(args.gcs_uri),
        Path(args.output),
        download_backend=getattr(args, 'download_backend', 'auto'),
    )
    emit(payload)
    return code


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


def empty_account_summary_channel() -> dict[str, Any]:
    return {'status': 'unlinked', 'linked': False, 'skipped': False, 'usernames_count': 0, 'artifacts': [], 'config': {}}


def accounts_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'exists': False, 'channels': {channel: empty_account_summary_channel() for channel in SETUP_SOURCE_CHANNELS}}
    try: data = read_json(path)
    except Exception as exc: return {'exists': True, 'error': str(exc), 'channels': {channel: empty_account_summary_channel() for channel in SETUP_SOURCE_CHANNELS}}
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


def processing_dry_run_command_args(operator_id: str) -> list[str]:
    return [
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


def processing_run_command_text(operator_id: str, *, allow_paid: bool = False) -> str:
    text = f'uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --default-operator-id {operator_id}'
    if allow_paid:
        text += ' --allow-paid-role-provider --allow-paid-embeddings --allow-paid-company-provider'
    return text


def processing_run_command_args(operator_id: str, *, allow_paid: bool = False) -> list[str]:
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


def run_processing_dry_run(operator_id: str) -> dict[str, Any]:
    code, payload, stderr = run_json_command(processing_dry_run_command_args(operator_id), timeout=60 * 60)
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


def estimate_has_known_pricing(estimate: dict[str, Any]) -> bool:
    costs = estimate.get('estimated_costs') if isinstance(estimate.get('estimated_costs'), dict) else {}
    return bool(costs.get('known_pricing', True))


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

    spend_limit = float(getattr(args, 'auto_spend_limit_usd', DEFAULT_AUTO_SPEND_LIMIT_USD))
    estimate = run_processing_dry_run(args.operator_id)
    if estimate.get('status') == 'failed':
        payload = {'status': 'failed', 'step': 'index_dry_run', 'processing_estimate': estimate}
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'failed'
        save_setup_ledger(ledger, ledger_path)
        return payload, 1

    paid_calls = estimated_paid_calls(estimate)
    total_cost = estimated_cost_usd(estimate)
    known_pricing = estimate_has_known_pricing(estimate)
    needs_paid_approval = paid_calls > 0 and (not known_pricing or total_cost is None or total_cost >= spend_limit)
    if total_cost is not None and total_cost >= spend_limit:
        needs_paid_approval = True

    if needs_paid_approval:
        payload = {
            'status': 'blocked_approval',
            'step': 'index_processing',
            'reason': 'estimated_processing_cost_requires_approval',
            'auto_spend_limit_usd': spend_limit,
            'estimated_cost_usd': total_cost,
            'estimated_paid_calls': estimate.get('estimated_paid_calls', {}),
            'processing_estimate': estimate,
            'requires_approval': [{'id': 'provider_spend'}],
            'approve_command': processing_run_command_text(args.operator_id, allow_paid=True),
        }
        ledger.setdefault('phases', {})['index'] = payload
        ledger['status'] = 'blocked_approval'
        save_setup_ledger(ledger, ledger_path)
        return payload, 20

    allow_paid = paid_calls > 0 or bool(total_cost and total_cost > 0)
    code, processing_payload, processing_stderr = run_json_command(
        processing_run_command_args(args.operator_id, allow_paid=allow_paid),
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

    payload = {
        'status': 'ready',
        'people_csv': '.powerpacks/network-import/merged/people.csv',
        'people_sha256': sha256_file(people),
        'auto_spend_limit_usd': spend_limit,
        'auto_approved_paid_calls': paid_calls if allow_paid else 0,
        'estimated_cost_usd': total_cost,
        'processing_estimate': estimate,
        'processing': processing_payload,
        'local_duckdb': duckdb_payload,
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
        return {'due': True, 'reason': 'never_synced_after_bootstrap', 'linked_sources': sources, 'source_fingerprint': fingerprint}
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
    records = si / 'records'
    people = ROOT / '.powerpacks/network-import/merged/people.csv'
    people_hash = sha256_file(people) if people.exists() else ''
    processing_needed = {
        'status': 'people_csv_ready_for_processing',
        'people_csv': str(people),
        'people_sha256': people_hash,
        'plan_command': processing_plan_command_text(),
        'dry_run_command': processing_dry_run_command_text(operator_id),
        'requires_approval': ['provider_spend', 'provider_allow_flags'],
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
    if records.exists() and any(records.glob('*.records.jsonl')) and not duck.exists():
        return {'status': 'records_only_duckdb_missing', 'repair_command': f'uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --operator-id {operator_id} --force'}
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
    phases = ledger.setdefault('phases', {phase: {'status': 'pending'} for phase in ['bootstrap', 'link', 'import', 'index']})
    for phase in ['bootstrap', 'link', 'import', 'index']:
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
            'source': 'operator_bootstrap' if phases.get('bootstrap', {}).get('status') == 'restored' else 'linked_sources',
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
    elif people_csv.exists() and phases.get('bootstrap', {}).get('status') == 'restored' and current_link['linked']:
        phases['import'] = {
            'status': 'refresh_due',
            'source': 'operator_bootstrap',
            'people_csv': '.powerpacks/network-import/merged/people.csv',
            'refresh_due': refresh_state,
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
            'requires_approval': idx.get('requires_approval') or ['provider_spend', 'provider_allow_flags'],
            'people_sha256': idx.get('people_sha256', ''),
            'index_input_sha256': idx.get('index_input_sha256', ''),
        }

    statuses = {phase: phases.get(phase, {}).get('status') for phase in ['bootstrap', 'link', 'import', 'index']}
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
    bundles = sorted(
        str(p) for p in (ROOT / '.powerpacks/operator-bootstrap/bundles').glob('*.operator-bootstrap.tar.gz')
    )
    next_actions = []
    if bundles and ledger['phases']['bootstrap']['status'] == 'pending':
        next_actions.append('inspect-bootstrap')
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


def quote_arg(value: Any) -> str:
    return shlex.quote(str(value))


def setup_phase_command(args: argparse.Namespace, phase: str) -> str:
    cmd = [
        'uv', 'run', '--project', '.', 'python',
        'packs/ingestion/primitives/setup/setup.py',
        phase,
        '--operator-id', args.operator_id,
    ]
    if phase in ('bootstrap', 'link', 'import', 'index', 'next', 'status'):
        cmd.extend(['--accounts', args.accounts])
    if phase in ('bootstrap', 'link', 'import', 'index', 'next', 'status'):
        cmd.extend(['--setup-ledger', args.setup_ledger])
    return ' '.join(quote_arg(part) for part in cmd)


def next_action_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = status_payload(args)
    ledger = payload['setup_ledger']
    phases = ledger.get('phases') or {}
    bootstrap = phases.get('bootstrap') if isinstance(phases.get('bootstrap'), dict) else {}
    link = phases.get('link') if isinstance(phases.get('link'), dict) else {}
    import_phase = phases.get('import') if isinstance(phases.get('import'), dict) else {}
    index = phases.get('index') if isinstance(phases.get('index'), dict) else {}
    idx = payload.get('search_index_readiness') if isinstance(payload.get('search_index_readiness'), dict) else {}

    action: dict[str, Any]
    if bootstrap.get('status') == 'pending' and payload.get('bootstrap_bundle_candidates'):
        action = {
            'status': 'run_command',
            'phase': 'bootstrap',
            'auto_safe': True,
            'reason': 'matching bootstrap bundle can be restored',
            'command': setup_phase_command(args, 'bootstrap'),
        }
    elif import_phase.get('status') == 'refresh_due':
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


def run_bootstrap_phase(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    ledger = load_setup_ledger(ledger_path)
    bootstrap_payload, bootstrap_code = maybe_apply_bootstrap(args, ledger)
    if bootstrap_payload is None:
        bootstrap_payload = {'status': 'skipped', 'reason': 'bootstrap_already_completed'}
    if bootstrap_code:
        emit({'status': bootstrap_payload.get('status', 'failed'), 'phase': 'bootstrap', 'bootstrap': bootstrap_payload})
        return bootstrap_code
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=getattr(args, 'accounts', '.powerpacks/ingestion/accounts.json'),
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
    )
    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': bootstrap_payload.get('status', 'ok'),
        'phase': 'bootstrap',
        'operator_id': args.operator_id,
        'bootstrap': bootstrap_payload,
        'setup_ledger': final_status['setup_ledger'],
        'next': next_action_payload(status_args)['next'],
    })
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
    run_id = f"setup-fan-in-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    before_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    code, payload, stderr = run_json_command(network_fan_in_command(args, run_id, force=bool(getattr(args, 'force', False))))
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
        'run_id': payload.get('run_id') or run_id,
        'ledger': str(SETUP_REFRESH_LEDGER),
        'source_fingerprint': linked_source_fingerprint(accounts),
        'linked_sources': linked_sources(accounts),
        'network_changed': bool(after_hash and before_hash != after_hash),
        'before_people_sha256': before_hash,
        'after_people_sha256': after_hash,
        'promoted': promoted,
        'artifact_hashes': artifact_hashes(promoted),
        'messages_ledger': str(SETUP_MESSAGES_LEDGER) if ((accounts.get('channels') or {}).get('messages') or {}).get('linked') else '',
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
    index_payload, index_code = run_processing_index(args, ledger, ledger_path)
    status_args = argparse.Namespace(
        operator_id=args.operator_id,
        accounts=getattr(args, 'accounts', '.powerpacks/ingestion/accounts.json'),
        setup_ledger=args.setup_ledger,
        refresh_interval_hours=getattr(args, 'refresh_interval_hours', DEFAULT_REFRESH_INTERVAL_HOURS),
    )
    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': index_payload.get('status'),
        'phase': 'index',
        'operator_id': args.operator_id,
        'index': index_payload,
        'setup_ledger': final_status['setup_ledger'],
        'next': next_action_payload(status_args)['next'],
    })
    return index_code


def setup_commands(args: argparse.Namespace) -> dict[str, str]:
    return {
        'import_network_dry_run': f'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --dry-run --from-accounts {args.accounts} --operator-id {args.operator_id} --include-existing-artifacts',
        'import_network_run': f'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --from-accounts {args.accounts} --operator-id {args.operator_id} --include-existing-artifacts',
        'import_network_fan_in': f'uv run --project . python packs/ingestion/primitives/setup/setup.py fan-in --operator-id {args.operator_id} --accounts {args.accounts} --setup-ledger {args.setup_ledger} --force',
        'import_network_status': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py status',
        'import_network_continue': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue',
        'import_network_approve': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve',
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
                'command': commands['import_network_run'] + f' --ledger {SETUP_REFRESH_LEDGER} --only-source gmail --force',
            })
        else:
            if ch == 'messages':
                whatsapp_cfg = cfg.get('whatsapp') if isinstance(cfg.get('whatsapp'), dict) else {}
                imessage_cfg = cfg.get('imessage') if isinstance(cfg.get('imessage'), dict) else {}
                include_flags = []
                if imessage_cfg.get('status') != 'skipped':
                    include_flags.append('--include-imessage')
                if whatsapp_cfg.get('status') == 'linked' or whatsapp_cfg.get('authenticated') is True:
                    include_flags.append('--include-whatsapp')
                include_flags.append('--include-contact-merge')
                include_flags.extend([
                    '--include-powerset-candidates',
                    '--include-local-match',
                    '--include-llm-review',
                    '--include-review',
                ])
                command = (
                    'uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run'
                    ' --ledger .powerpacks/messages/import-run.setup-messages.json'
                    f' {" ".join(include_flags)}'
                )
                requires_approval = ['whatsapp_qr'] if '--include-whatsapp' in include_flags else []
                ledger = str(SETUP_MESSAGES_LEDGER)
                run_id = ''
            elif ch == 'twitter':
                command = ''
                requires_approval = []
                ledger = ''
                run_id = ''
            else:
                command = commands['import_network_run'] + f' --ledger {SETUP_REFRESH_LEDGER} --only-source {ch} --force'
                requires_approval = []
                ledger = str(SETUP_REFRESH_LEDGER)
                run_id = ''
            worker_jobs.append({
                'id': ch,
                'source': ch,
                'parallelizable': ch == 'messages',
                'ledger': ledger,
                'run_id': run_id,
                'command': command,
                'requires_approval': requires_approval,
                **({'status': 'recorded_only', 'reason': 'Twitter/X handle is recorded; follower import is not wired into setup yet.'} if ch == 'twitter' else {}),
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
            'import_network_dry_run',
            'import_network_status',
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


def message_refresh_command(accounts: dict[str, Any], ledger_path: Path, *, force: bool = True) -> list[str] | None:
    messages = (accounts.get('channels') or {}).get('messages')
    if not isinstance(messages, dict) or not messages.get('linked'):
        return None
    cfg = messages.get('config') if isinstance(messages.get('config'), dict) else {}
    whatsapp_cfg = cfg.get('whatsapp') if isinstance(cfg.get('whatsapp'), dict) else {}
    imessage_cfg = cfg.get('imessage') if isinstance(cfg.get('imessage'), dict) else {}
    include_flags = []
    if imessage_cfg.get('status') != 'skipped':
        include_flags.append('--include-imessage')
    if whatsapp_cfg.get('status') == 'linked' or whatsapp_cfg.get('authenticated') is True:
        include_flags.append('--include-whatsapp')
    include_flags.append('--include-contact-merge')
    include_flags.extend([
        '--include-powerset-candidates',
        '--include-local-match',
        '--include-llm-review',
        '--include-review',
    ])
    cmd = [
        sys.executable,
        'packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py',
        'run',
        '--ledger',
        str(ledger_path),
        '--parallel-timeout',
        str(DEFAULT_SETUP_MESSAGES_PARALLEL_TIMEOUT_SECONDS),
        '--reuse-existing-artifacts',
        *include_flags,
    ]
    if force and '--include-imessage' in include_flags:
        cmd.append('--force-imessage')
    if force and '--include-whatsapp' in include_flags:
        cmd.append('--force-whatsapp')
    if force:
        cmd.extend(['--force-match', '--rerun-llm'])
    return cmd


def network_refresh_command(args: argparse.Namespace, run_id: str, *, force: bool, gmail_sync_after: str = '') -> list[str]:
    cmd = [
        sys.executable,
        'packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py',
        'run',
        '--from-accounts',
        args.accounts,
        '--operator-id',
        args.operator_id,
        '--include-existing-artifacts',
        '--ledger',
        str(SETUP_REFRESH_LEDGER),
        '--run-id',
        run_id,
    ]
    if force:
        cmd.append('--force')
    if gmail_sync_after:
        cmd.extend(['--gmail-sync-after', gmail_sync_after])
    return cmd


def network_fan_in_command(args: argparse.Namespace, run_id: str, *, force: bool) -> list[str]:
    cmd = [
        sys.executable,
        'packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py',
        'run',
        '--from-accounts',
        args.accounts,
        '--operator-id',
        args.operator_id,
        '--include-existing-artifacts',
        '--fan-in-only',
        '--ledger',
        str(SETUP_REFRESH_LEDGER),
        '--run-id',
        run_id,
    ]
    if force:
        cmd.append('--force')
    return cmd


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
    duckdb = artifacts.get('duckdb')
    if duckdb and Path(str(duckdb)).exists():
        dest_dir = ROOT / '.powerpacks/network-import/duckdb'
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / 'network.duckdb'
        shutil.copy2(Path(str(duckdb)), dst)
        promoted['network_duckdb'] = str(dst)
    manifest = artifacts.get('duckdb_manifest')
    if manifest and Path(str(manifest)).exists():
        dest_dir = ROOT / '.powerpacks/network-import/duckdb'
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / 'manifest.json'
        shutil.copy2(Path(str(manifest)), dst)
        promoted['network_duckdb_manifest'] = str(dst)
    return promoted


def matching_bootstrap_bundle(operator_id: str) -> Path | None:
    for bundle in sorted((ROOT / '.powerpacks/operator-bootstrap/bundles').glob('*.operator-bootstrap.tar.gz')):
        try:
            inspected = inspect_bundle(bundle)
        except Exception:
            continue
        if inspected.get('status') == 'ok' and inspected.get('operator_id') == operator_id:
            return bundle
    return None


def maybe_apply_bootstrap(args: argparse.Namespace, ledger: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    if ledger.get('phases', {}).get('bootstrap', {}).get('status') != 'pending':
        return None, 0
    bundle = Path(args.bootstrap_bundle) if getattr(args, 'bootstrap_bundle', '') else matching_bootstrap_bundle(args.operator_id)
    if not bundle:
        return {'status': 'skipped', 'reason': 'no_matching_bootstrap_bundle'}, 0
    inspected = inspect_bundle(bundle)
    if inspected.get('status') != 'ok':
        return {'status': 'blocked_user_action', 'step': 'bootstrap', 'inspect': inspected}, 20
    force = bool(getattr(args, 'force_bootstrap', False))
    if inspected.get('would_overwrite') and not force:
        return {
            'status': 'blocked_user_action',
            'step': 'bootstrap',
            'reason': 'bootstrap restore would overwrite existing artifacts',
            'requires_approval': [{'id': 'destructive_restore_overwrite'}],
            'inspect': inspected,
        }, 20
    apply_args = argparse.Namespace(
        bundle=str(bundle),
        operator_id=args.operator_id,
        force=force,
        inspect_file='',
        setup_ledger=args.setup_ledger,
        allow_legacy_bootstrap_manifest=False,
    )
    payload = apply_bundle(apply_args)
    if payload.get('status') != 'ok':
        return payload, 20 if payload.get('requires_approval') else 1
    return payload, 0


def run_live_refresh(args: argparse.Namespace, ledger: dict[str, Any], accounts: dict[str, Any], due: dict[str, Any]) -> tuple[dict[str, Any], int]:
    started_at = now()
    run_id = f"setup-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    before_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    results: dict[str, Any] = {'status': 'running', 'started_at': started_at, 'reason': due.get('reason'), 'run_id': run_id}

    refresh_reason = str(due.get('reason') or '')
    force_reasons = {'forced', 'refresh_interval_elapsed', 'linked_sources_changed', 'missing_import_artifact', 'import_artifact_drift'}
    force_messages = refresh_reason in force_reasons
    message_cmd = message_refresh_command(accounts, SETUP_MESSAGES_LEDGER, force=force_messages)
    if message_cmd:
        code, payload, stderr = run_json_command(message_cmd)
        results['messages'] = {'code': code, 'payload': payload, 'stderr': tail(stderr)}
        if code in (20, 21) or str(payload.get('status', '')).startswith('blocked'):
            return {'status': 'blocked_user_action', 'step': 'messages', 'refresh': results, 'payload': payload}, code or 20
        if code != 0:
            return {'status': 'failed', 'step': 'messages', 'refresh': results, 'error': tail(stderr) or payload}, 1

    force_network = refresh_reason in force_reasons
    # Leave Gmail mailbox cursoring to import_network_pipeline. That layer can
    # inspect msgvault per account and pass sync-full --after from the newest
    # local source/message timestamp. The setup wrapper should not replace that
    # with a broad run-level lookback.
    gmail_sync_after = ''
    code, payload, stderr = run_json_command(network_refresh_command(args, run_id, force=force_network, gmail_sync_after=gmail_sync_after))
    results['network'] = {'code': code, 'payload': payload, 'stderr': tail(stderr)}
    if code == 20 or payload.get('status') == 'blocked_approval':
        return {'status': 'blocked_approval', 'step': 'network_import', 'refresh': results, 'payload': payload}, 20
    if code != 0:
        return {'status': 'failed', 'step': 'network_import', 'refresh': results, 'error': tail(stderr) or payload}, 1

    promoted = promote_network_artifacts(payload.get('artifacts') or {})
    after_hash = sha256_file(ROOT / '.powerpacks/network-import/merged/people.csv') if (ROOT / '.powerpacks/network-import/merged/people.csv').exists() else ''
    completed = {
        'status': 'completed',
        'started_at': started_at,
        'completed_at': now(),
        'run_id': payload.get('run_id') or run_id,
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
        'messages_ledger': str(SETUP_MESSAGES_LEDGER) if message_cmd else '',
    }
    return {'status': 'completed', 'refresh': completed, 'results': results}, 0


def run_setup(args: argparse.Namespace) -> int:
    ledger_path = Path(args.setup_ledger)
    ledger = load_setup_ledger(ledger_path)
    bootstrap_payload, bootstrap_code = maybe_apply_bootstrap(args, ledger)
    if bootstrap_code:
        emit(bootstrap_payload or {'status': 'failed', 'step': 'bootstrap'})
        return bootstrap_code

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
            emit({'status': refresh_payload.get('status'), 'bootstrap': bootstrap_payload, **refresh_payload})
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
            'bootstrap': bootstrap_payload,
            'index': index_payload,
        })
        return index_code

    final_status = status_payload(status_args)
    save_setup_ledger(final_status['setup_ledger'], ledger_path)
    emit({
        'status': final_status['setup_ledger'].get('status'),
        'operator_id': args.operator_id,
        'bootstrap': bootstrap_payload,
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

    s = sub.add_parser('bootstrap')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--bootstrap-bundle', default='')
    s.add_argument('--force-bootstrap', action='store_true')
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.set_defaults(func=run_bootstrap_phase)

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
    s.set_defaults(func=run_fan_in_phase)

    s = sub.add_parser('index')
    s.add_argument('--operator-id', required=True)
    s.add_argument('--accounts', default='.powerpacks/ingestion/accounts.json')
    s.add_argument('--setup-ledger', default=str(SETUP_LEDGER))
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--auto-spend-limit-usd', type=float, default=DEFAULT_AUTO_SPEND_LIMIT_USD)
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
    s.add_argument('--bootstrap-bundle', default='')
    s.add_argument('--force-bootstrap', action='store_true')
    s.add_argument('--refresh-interval-hours', type=int, default=DEFAULT_REFRESH_INTERVAL_HOURS)
    s.add_argument('--gmail-sync-lookback-days', type=int, default=DEFAULT_GMAIL_SYNC_LOOKBACK_DAYS)
    s.add_argument('--auto-spend-limit-usd', type=float, default=DEFAULT_AUTO_SPEND_LIMIT_USD)
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
