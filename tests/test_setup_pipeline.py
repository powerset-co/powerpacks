import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'packs/ingestion/primitives/setup/setup.py'
SPEC = importlib.util.spec_from_file_location('setup_primitive', SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(setup)

OPERATOR_ID = 'op-123'


def add_file(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def make_bundle(path: Path, *, privacy=True, restore_paths=None, traversal=False) -> None:
    manifest = {
        'schema_version': 1,
        'operator': 'patrick',
        'operator_id': OPERATOR_ID,
        'privacy': {
            'operator_scoped': True,
            'raw_msgvault_db_copied': False,
            'raw_mail_copied': False,
            'message_bodies_copied': False,
            'attachments_copied': False,
            'secrets_copied': False,
        } if privacy else {},
    }
    restore = {
        'status': 'ok',
        'operator': 'patrick',
        'operator_id': OPERATOR_ID,
        'normal_pipeline_outputs': restore_paths or [
            '.powerpacks/search-index',
            '.powerpacks/network-import/merged',
            '.powerpacks/network-import/profile_cache_v2',
            '.powerpacks/network-import/network-runs/run-1/import-network.ledger.json',
        ],
    }
    with tarfile.open(path, 'w:gz') as tf:
        add_file(tf, 'patrick/manifest.json', json.dumps(manifest).encode())
        add_file(tf, '.powerpacks/operator-bootstrap/restore-manifest.json', json.dumps(restore).encode())
        add_file(tf, '.powerpacks/search-index/records/people.records.jsonl', b'{}\n')
        add_file(tf, '.powerpacks/search-index/ledger.json', json.dumps({'status': 'completed', 'default_operator_id': OPERATOR_ID, 'steps': [{'id': 'x', 'status': 'completed'}]}).encode())
        add_file(tf, '.powerpacks/network-import/merged/people.csv', b'id\np1\n')
        add_file(tf, '.powerpacks/network-import/profile_cache_v2/a.json', b'{}')
        add_file(tf, '.powerpacks/network-import/network-runs/run-1/import-network.ledger.json', json.dumps({'status': 'completed', 'steps': {'merge': {'status': 'completed'}}}).encode())
        if traversal:
            add_file(tf, '../evil.txt', b'evil')


def fake_local_duckdb_payload(tmp: Path):
    db = tmp / '.powerpacks/search-index/local-search.duckdb'
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text('duckdb', encoding='utf-8')
    manifest = db.parent / 'manifest.json'
    manifest.write_text(json.dumps({'status': 'ok', 'operator_id': OPERATOR_ID, 'duckdb': str(db)}), encoding='utf-8')
    return 0, {'status': 'ok', 'duckdb': str(db), 'tables': {'local_people_positions': 1}}, ''


class SetupPipelineTests(unittest.TestCase):
    def temp_workspace(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tmp = Path(td.name)
        old_root = setup.ROOT
        setup.ROOT = tmp
        self.addCleanup(lambda: setattr(setup, 'ROOT', old_root))
        return tmp

    def test_inspect_validates_privacy_and_hashes(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle)
        payload = setup.inspect_bundle(bundle)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['operator_id'], OPERATOR_ID)
        self.assertIn('bundle_sha256', payload)
        self.assertIn('.powerpacks/network-import/network-runs/run-1/import-network.ledger.json', payload['would_restore'])

    def test_missing_legacy_privacy_flags_need_user_action(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle, privacy=False)
        payload = setup.inspect_bundle(bundle)
        self.assertEqual(payload['status'], 'needs_user_action')
        self.assertTrue(payload['legacy_privacy_override_required'])
        self.assertEqual(set(payload['missing_privacy_flags']), set(setup.REQUIRED_PRIVACY))
        self.assertEqual(setup.inspect_bundle(bundle, allow_legacy=True)['status'], 'ok')

    def test_tar_member_path_traversal_rejected(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle, traversal=True)
        with self.assertRaises(ValueError):
            setup.inspect_bundle(bundle)

    def test_restore_allowlist_blocks_unrelated_paths(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle, restore_paths=['.powerpacks/search-index', '.powerpacks/messages/raw.db', '.powerpacks/network-import/network-runs/ok-run/x.json'])
        payload = setup.inspect_bundle(bundle)
        self.assertIn('.powerpacks/messages/raw.db', payload['restore_root_classification']['blocked'])
        self.assertIn('.powerpacks/network-import/network-runs/ok-run/x.json', payload['restore_root_classification']['allowed'])

    def test_apply_refuses_overwrite_without_force_and_backs_up_with_force(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle)
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/old.txt').write_text('old', encoding='utf-8')
        args = argparse.Namespace(bundle=str(bundle), operator_id=OPERATOR_ID, force=False, inspect_file='', setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'), allow_legacy_bootstrap_manifest=False)
        self.assertEqual(setup.apply_bundle(args)['status'], 'rejected')
        args.force = True
        payload = setup.apply_bundle(args)
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue((tmp / '.powerpacks/search-index/records/people.records.jsonl').exists())
        self.assertTrue(list((tmp / '.powerpacks/operator-bootstrap/backups').glob('*/.powerpacks/search-index/old.txt')))
        ledger = json.loads((tmp / '.powerpacks/search-index/ledger.json').read_text())
        self.assertEqual(ledger['status'], 'restored')
        self.assertEqual(ledger['steps'][0]['status'], 'restored')
        self.assertTrue((tmp / '.powerpacks/operator-bootstrap/applied/patrick/manifest.json').exists())

    def test_inspect_apply_hash_rebinding(self):
        tmp = self.temp_workspace()
        bundle = tmp / 'bundle.tar.gz'
        make_bundle(bundle)
        inspect = setup.inspect_bundle(bundle)
        inspect['bundle_sha256'] = 'bad'
        inspect_file = tmp / 'inspect.json'
        inspect_file.write_text(json.dumps(inspect), encoding='utf-8')
        args = argparse.Namespace(bundle=str(bundle), operator_id=OPERATOR_ID, force=True, inspect_file=str(inspect_file), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'), allow_legacy_bootstrap_manifest=False)
        payload = setup.apply_bundle(args)
        self.assertEqual(payload['status'], 'rejected')
        self.assertIn('hash mismatch', payload['reason'])

    def test_status_indexing_readiness_cases(self):
        tmp = self.temp_workspace()
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(tmp / 'accounts.json'), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'))
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'default_operator_id': OPERATOR_ID}))
        self.assertEqual(setup.status_payload(ns)['search_index_readiness']['status'], 'search_ready')
        (tmp / '.powerpacks/search-index/local-search.duckdb').unlink()
        (tmp / '.powerpacks/search-index/records').mkdir()
        (tmp / '.powerpacks/search-index/records/people.records.jsonl').write_text('{}\n')
        self.assertEqual(setup.status_payload(ns)['search_index_readiness']['status'], 'records_only_duckdb_missing')
        import shutil
        shutil.rmtree(tmp / '.powerpacks/search-index')
        (tmp / '.powerpacks/network-import/merged').mkdir(parents=True)
        (tmp / '.powerpacks/network-import/merged/people.csv').write_text('id\n')
        self.assertEqual(setup.status_payload(ns)['search_index_readiness']['status'], 'people_csv_ready_for_processing')
        plan_command = setup.status_payload(ns)['search_index_readiness']['plan_command']
        self.assertIn('build_processing_pipeline.py plan', plan_command)
        self.assertNotIn('--default-operator-id', plan_command)
        dry_run_command = setup.status_payload(ns)['search_index_readiness']['dry_run_command']
        self.assertIn('build_processing_pipeline.py run --dry-run', dry_run_command)
        self.assertIn(f'--default-operator-id {OPERATOR_ID}', dry_run_command)

    def test_status_accepts_local_duckdb_manifest_when_processing_ledger_is_not_completed(self):
        tmp = self.temp_workspace()
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(tmp / 'accounts.json'), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'), refresh_interval_hours=168)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\np1\n', encoding='utf-8')
        si = tmp / '.powerpacks/search-index'
        si.mkdir(parents=True)
        (si / 'local-search.duckdb').write_text('db', encoding='utf-8')
        (si / 'ledger.json').write_text(json.dumps({'status': 'running', 'default_operator_id': OPERATOR_ID}), encoding='utf-8')
        (si / 'manifest.json').write_text(json.dumps({'status': 'ok', 'operator_id': OPERATOR_ID}), encoding='utf-8')

        payload = setup.status_payload(ns)
        self.assertEqual(payload['search_index_readiness']['status'], 'search_ready')
        self.assertEqual(payload['search_index_readiness']['manifest'], str(si / 'manifest.json'))

    def test_status_marks_restored_bootstrap_import_refresh_due_until_live_sync(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {}},
            'twitter': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
        }}), encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'pending'},
                'import': {'status': 'pending'},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/network-import/merged').mkdir(parents=True)
        (tmp / '.powerpacks/network-import/merged/people.csv').write_text('id\np1\n', encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')

        payload = setup.status_payload(argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(ledger)))
        phases = payload['setup_ledger']['phases']
        self.assertEqual(payload['setup_ledger']['status'], 'refresh_due')
        self.assertEqual(phases['link']['status'], 'ready')
        self.assertEqual(phases['import']['status'], 'refresh_due')
        self.assertEqual(phases['import']['refresh_due']['reason'], 'never_synced_after_bootstrap')
        self.assertEqual(phases['index']['status'], 'ready')
        self.assertIn('run setup refresh', payload['next_actions'])

    def test_status_ready_after_live_refresh_for_same_sources(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
            'twitter': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'pending'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                }},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/network-import/merged').mkdir(parents=True)
        (tmp / '.powerpacks/network-import/merged/people.csv').write_text('id\np1\n', encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')

        payload = setup.status_payload(argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(ledger), refresh_interval_hours=168))
        self.assertEqual(payload['setup_ledger']['status'], 'ready')
        self.assertEqual(payload['setup_ledger']['phases']['import']['status'], 'ready')
        self.assertNotIn('run setup refresh', payload['next_actions'])

    def test_status_marks_completed_live_refresh_due_when_people_hash_drifts(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\nbefore\n', encoding='utf-8')
        expected_hash = setup.sha256_file(people)
        people.write_text('id\nafter-manual-edit\n', encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                    'after_people_sha256': expected_hash,
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')

        payload = setup.status_payload(argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(ledger), refresh_interval_hours=168))
        self.assertEqual(payload['setup_ledger']['status'], 'refresh_due')
        import_phase = payload['setup_ledger']['phases']['import']
        self.assertEqual(import_phase['status'], 'refresh_due')
        self.assertEqual(import_phase['refresh_due']['reason'], 'import_artifact_drift')
        self.assertEqual(import_phase['refresh_due']['artifact'], '.powerpacks/network-import/merged/people.csv')
        self.assertIn('run setup refresh', payload['next_actions'])

    def test_status_rewrites_stale_handoff_commands_to_current_defaults(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}), encoding='utf-8')
        summary = setup.accounts_summary(accounts)
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'handoff': {
                'generated_at': '2026-01-01T00:00:00Z',
                'commands': {
                    'import_network_run': 'uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --from-accounts accounts.json --operator-id old',
                },
                'requires_approval': [],
            },
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'pending'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(summary),
                }},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/network-import/merged').mkdir(parents=True)
        (tmp / '.powerpacks/network-import/merged/people.csv').write_text('id\np1\n', encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')

        payload = setup.status_payload(argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(ledger), refresh_interval_hours=168))
        commands = payload['setup_ledger']['handoff']['commands']
        self.assertIn('--include-existing-artifacts', commands['import_network_run'])
        self.assertIn('--include-existing-artifacts', commands['import_network_dry_run'])
        self.assertIn('destructive_restore_overwrite', {item['id'] for item in payload['setup_ledger']['handoff']['requires_approval']})

    def test_run_refreshes_linked_sources_and_marks_index_stale_when_network_changes(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
            'messages': {
                'linked': True,
                'skipped': False,
                'usernames': ['imessage'],
                'artifacts': [],
                'config': {'imessage': {'status': 'ready'}},
            },
        }}
        accounts.parent.mkdir(parents=True, exist_ok=True)
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'pending'},
                'import': {'status': 'pending'},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/network-import/merged').mkdir(parents=True)
        (tmp / '.powerpacks/network-import/merged/people.csv').write_text('id\nold\n', encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            if 'import_contacts_pipeline.py' in joined:
                return 0, {'status': 'selected_steps_completed', 'artifacts': {'contacts_csv': '.powerpacks/messages/contacts.csv'}}, ''
            if 'build-local-duckdb-shim.py' in joined:
                return fake_local_duckdb_payload(tmp)
            if 'import_network_pipeline.py' in joined:
                run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
                merged_dir = run_dir / 'merged'
                merged_dir.mkdir(parents=True)
                for name, content in {
                    'people.csv': 'id\nnew\n',
                    'network_contacts.csv': 'id\nc1\n',
                    'network_contact_sources.csv': 'id\ns1\n',
                    'network_companies.csv': 'id\nco1\n',
                    'merge_manifest.json': '{}\n',
                }.items():
                    (merged_dir / name).write_text(content, encoding='utf-8')
                duckdb_dir = run_dir / 'duckdb'
                duckdb_dir.mkdir(parents=True)
                duckdb = duckdb_dir / 'network.setup-refresh-test.duckdb'
                manifest = duckdb_dir / 'manifest.setup-refresh-test.json'
                duckdb.write_text('duckdb', encoding='utf-8')
                manifest.write_text('{}\n', encoding='utf-8')
                return 0, {
                    'status': 'completed',
                    'run_id': 'setup-refresh-test',
                    'artifacts': {
                        'merged_people_csv': str(merged_dir / 'people.csv'),
                        'network_contacts_csv': str(merged_dir / 'network_contacts.csv'),
                        'network_contact_sources_csv': str(merged_dir / 'network_contact_sources.csv'),
                        'network_companies_csv': str(merged_dir / 'network_companies.csv'),
                        'merge_manifest': str(merged_dir / 'merge_manifest.json'),
                        'duckdb': str(duckdb),
                        'duckdb_manifest': str(manifest),
                    },
                }, ''
            if 'build_processing_pipeline.py' in joined and '--dry-run' in cmd:
                return 0, {'status': 'dry_run', 'estimated_cost_usd': 0.25, 'estimated_costs': {'total_estimated_usd': 0.25}, 'estimated_paid_calls': {'role_enrichment': 1}}, ''
            if 'build_processing_pipeline.py' in joined:
                return 0, {'status': 'completed', 'run_dir': '.powerpacks/search-index'}, ''
            raise AssertionError(f'unexpected command: {cmd}')

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        message_cmd = next(cmd for cmd in calls if 'import_contacts_pipeline.py' in ' '.join(cmd))
        self.assertIn('--parallel-timeout', message_cmd)
        self.assertIn('--reuse-existing-artifacts', message_cmd)
        self.assertIn('--include-powerset-candidates', message_cmd)
        self.assertIn('--include-local-match', message_cmd)
        self.assertIn('--include-llm-review', message_cmd)
        self.assertIn('--include-review', message_cmd)
        self.assertIn('--force-imessage', message_cmd)
        self.assertIn('--force-match', message_cmd)
        self.assertIn('--rerun-llm', message_cmd)
        self.assertNotIn('--force-build-review', message_cmd)
        self.assertNotIn('--force-whatsapp', message_cmd)
        network_cmd = next(cmd for cmd in calls if 'import_network_pipeline.py' in ' '.join(cmd))
        self.assertIn('--include-existing-artifacts', network_cmd)
        self.assertEqual((tmp / '.powerpacks/network-import/merged/people.csv').read_text(encoding='utf-8'), 'id\nnew\n')
        self.assertTrue((tmp / '.powerpacks/network-import/duckdb/network.duckdb').exists())
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['phases']['import']['status'], 'ready')
        self.assertEqual(saved['phases']['import']['live_refresh']['status'], 'completed')
        self.assertTrue(saved['phases']['import']['live_refresh']['network_changed'])
        self.assertEqual(saved['phases']['index']['status'], 'ready')
        self.assertEqual(saved['phases']['index']['processing_estimate']['estimated_cost_usd'], 0.25)
        processing_cmd = next(cmd for cmd in calls if 'build_processing_pipeline.py' in ' '.join(cmd) and '--dry-run' not in cmd)
        self.assertIn('--allow-paid-role-provider', processing_cmd)
        self.assertTrue(any('build-local-duckdb-shim.py' in ' '.join(cmd) for cmd in calls))

    def test_run_materializes_restored_records_without_paid_processing(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.parent.mkdir(parents=True, exist_ok=True)
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {'selected_accounts': [], 'account_emails': []}},
            'linkedin_csv': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
            'messages': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {'imessage': {'status': 'skipped'}, 'whatsapp': {'status': 'skipped'}}},
            'twitter': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
        }}), encoding='utf-8')
        summary = setup.accounts_summary(accounts)
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(summary),
                }},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\np1\n', encoding='utf-8')
        records = tmp / '.powerpacks/search-index/records'
        records.mkdir(parents=True)
        (records / 'people.records.jsonl').write_text('{}\n', encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            self.assertIn('build-local-duckdb-shim.py', joined)
            return fake_local_duckdb_payload(tmp)

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('build-local-duckdb-shim.py', ' '.join(calls[0]))
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['phases']['index']['status'], 'ready')
        self.assertTrue((tmp / '.powerpacks/search-index/local-search.duckdb').exists())

    def test_run_materializes_records_even_without_people_csv(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.parent.mkdir(parents=True, exist_ok=True)
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {'selected_accounts': [], 'account_emails': []}},
            'linkedin_csv': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
            'messages': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {'imessage': {'status': 'skipped'}, 'whatsapp': {'status': 'skipped'}}},
            'twitter': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
        }}), encoding='utf-8')
        summary = setup.accounts_summary(accounts)
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'pending',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(summary),
                }},
                'index': {'status': 'pending'},
            },
        }), encoding='utf-8')
        records = tmp / '.powerpacks/search-index/records'
        records.mkdir(parents=True)
        (records / 'people.records.jsonl').write_text('{}\n', encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            self.assertIn('build-local-duckdb-shim.py', ' '.join(cmd))
            return fake_local_duckdb_payload(tmp)

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['phases']['index']['status'], 'ready')
        self.assertEqual(saved['phases']['index']['people_sha256'], '')

    def test_run_forces_network_refresh_when_import_artifact_hash_drifts(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\nbefore\n', encoding='utf-8')
        expected_hash = setup.sha256_file(people)
        people.write_text('id\nafter-manual-edit\n', encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                    'after_people_sha256': expected_hash,
                    'promoted': {'merged_people.csv': str(people)},
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            if 'build-local-duckdb-shim.py' in joined:
                return fake_local_duckdb_payload(tmp)
            if 'build_processing_pipeline.py' in joined and '--dry-run' in cmd:
                return 0, {'status': 'dry_run', 'estimated_cost_usd': 0.5, 'estimated_paid_calls': {'role_enrichment': 1}}, ''
            if 'build_processing_pipeline.py' in joined:
                return 0, {'status': 'completed', 'run_dir': '.powerpacks/search-index'}, ''
            self.assertIn('import_network_pipeline.py', joined)
            run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
            merged_dir = run_dir / 'merged'
            merged_dir.mkdir(parents=True)
            for name, content in {
                'people.csv': 'id\nrestored\n',
                'network_contacts.csv': 'id\nc1\n',
                'network_contact_sources.csv': 'id\ns1\n',
                'network_companies.csv': 'id\nco1\n',
                'merge_manifest.json': '{}\n',
            }.items():
                (merged_dir / name).write_text(content, encoding='utf-8')
            duckdb_dir = run_dir / 'duckdb'
            duckdb_dir.mkdir(parents=True)
            duckdb = duckdb_dir / 'network.setup-refresh-test.duckdb'
            manifest = duckdb_dir / 'manifest.setup-refresh-test.json'
            duckdb.write_text('duckdb', encoding='utf-8')
            manifest.write_text('{}\n', encoding='utf-8')
            return 0, {
                'status': 'completed',
                'run_id': 'setup-refresh-test',
                'artifacts': {
                    'merged_people_csv': str(merged_dir / 'people.csv'),
                    'network_contacts_csv': str(merged_dir / 'network_contacts.csv'),
                    'network_contact_sources_csv': str(merged_dir / 'network_contact_sources.csv'),
                    'network_companies_csv': str(merged_dir / 'network_companies.csv'),
                    'merge_manifest': str(merged_dir / 'merge_manifest.json'),
                    'duckdb': str(duckdb),
                    'duckdb_manifest': str(manifest),
                },
            }, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        network_cmd = calls[0]
        self.assertIn('--force', network_cmd)
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['phases']['import']['live_refresh']['status'], 'completed')
        self.assertIn('artifact_hashes', saved['phases']['import']['live_refresh'])
        self.assertEqual((tmp / '.powerpacks/network-import/merged/people.csv').read_text(encoding='utf-8'), 'id\nrestored\n')
        self.assertEqual(saved['phases']['index']['status'], 'ready')

    def test_run_marks_network_changed_when_missing_people_csv_is_recreated(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\nbefore\n', encoding='utf-8')
        expected_hash = setup.sha256_file(people)
        people.unlink()
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                    'after_people_sha256': expected_hash,
                    'promoted': {'merged_people.csv': str(people)},
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            if 'build-local-duckdb-shim.py' in joined:
                return fake_local_duckdb_payload(tmp)
            if 'build_processing_pipeline.py' in joined and '--dry-run' in cmd:
                return 0, {'status': 'dry_run', 'estimated_cost_usd': 0.75, 'estimated_paid_calls': {'role_enrichment': 1}}, ''
            if 'build_processing_pipeline.py' in joined:
                return 0, {'status': 'completed', 'run_dir': '.powerpacks/search-index'}, ''
            run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
            merged_dir = run_dir / 'merged'
            merged_dir.mkdir(parents=True)
            for name, content in {
                'people.csv': 'id\nrestored\n',
                'network_contacts.csv': 'id\nc1\n',
                'network_contact_sources.csv': 'id\ns1\n',
                'network_companies.csv': 'id\nco1\n',
                'merge_manifest.json': '{}\n',
            }.items():
                (merged_dir / name).write_text(content, encoding='utf-8')
            duckdb_dir = run_dir / 'duckdb'
            duckdb_dir.mkdir(parents=True)
            duckdb = duckdb_dir / 'network.setup-refresh-test.duckdb'
            manifest = duckdb_dir / 'manifest.setup-refresh-test.json'
            duckdb.write_text('duckdb', encoding='utf-8')
            manifest.write_text('{}\n', encoding='utf-8')
            return 0, {
                'status': 'completed',
                'run_id': 'setup-refresh-test',
                'artifacts': {
                    'merged_people_csv': str(merged_dir / 'people.csv'),
                    'network_contacts_csv': str(merged_dir / 'network_contacts.csv'),
                    'network_contact_sources_csv': str(merged_dir / 'network_contact_sources.csv'),
                    'network_companies_csv': str(merged_dir / 'network_companies.csv'),
                    'merge_manifest': str(merged_dir / 'merge_manifest.json'),
                    'duckdb': str(duckdb),
                    'duckdb_manifest': str(manifest),
                },
            }, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        self.assertTrue(any('build_processing_pipeline.py' in ' '.join(cmd) for cmd in calls))
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertTrue(saved['phases']['import']['live_refresh']['network_changed'])
        self.assertEqual(saved['phases']['index']['processing_estimate']['estimated_cost_usd'], 0.75)
        self.assertEqual(saved['phases']['index']['status'], 'ready')

    def test_run_forces_refresh_even_when_recent_import_is_intact(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\ncurrent\n', encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': '2026-05-29T12:00:00Z',
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                    'after_people_sha256': setup.sha256_file(people),
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        (tmp / '.powerpacks/search-index').mkdir(parents=True)
        (tmp / '.powerpacks/search-index/local-search.duckdb').write_text('db', encoding='utf-8')
        (tmp / '.powerpacks/search-index/ledger.json').write_text(json.dumps({'status': 'restored', 'restored_operator_id': OPERATOR_ID}), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            if 'build-local-duckdb-shim.py' in joined:
                return fake_local_duckdb_payload(tmp)
            if 'build_processing_pipeline.py' in joined and '--dry-run' in cmd:
                return 0, {'status': 'dry_run', 'estimated_cost_usd': 0.0, 'estimated_paid_calls': {'role_enrichment': 0}}, ''
            if 'build_processing_pipeline.py' in joined:
                return 0, {'status': 'completed', 'run_dir': '.powerpacks/search-index'}, ''
            run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
            merged_dir = run_dir / 'merged'
            merged_dir.mkdir(parents=True)
            for name, content in {
                'people.csv': 'id\ncurrent\n',
                'network_contacts.csv': 'id\nc1\n',
                'network_contact_sources.csv': 'id\ns1\n',
                'network_companies.csv': 'id\nco1\n',
                'merge_manifest.json': '{}\n',
            }.items():
                (merged_dir / name).write_text(content, encoding='utf-8')
            duckdb_dir = run_dir / 'duckdb'
            duckdb_dir.mkdir(parents=True)
            duckdb = duckdb_dir / 'network.setup-refresh-test.duckdb'
            manifest = duckdb_dir / 'manifest.setup-refresh-test.json'
            duckdb.write_text('duckdb', encoding='utf-8')
            manifest.write_text('{}\n', encoding='utf-8')
            return 0, {
                'status': 'completed',
                'run_id': 'setup-refresh-test',
                'artifacts': {
                    'merged_people_csv': str(merged_dir / 'people.csv'),
                    'network_contacts_csv': str(merged_dir / 'network_contacts.csv'),
                    'network_contact_sources_csv': str(merged_dir / 'network_contact_sources.csv'),
                    'network_companies_csv': str(merged_dir / 'network_companies.csv'),
                    'merge_manifest': str(merged_dir / 'merge_manifest.json'),
                    'duckdb': str(duckdb),
                    'duckdb_manifest': str(manifest),
                },
            }, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
            gmail_sync_lookback_days=14,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 0)
        self.assertIn('--force', calls[0])
        self.assertIn('--gmail-sync-after', calls[0])
        self.assertEqual(calls[0][calls[0].index('--gmail-sync-after') + 1], '2026-05-15')
        self.assertTrue(any('build_processing_pipeline.py' in ' '.join(cmd) and '--dry-run' in cmd for cmd in calls))
        self.assertTrue(any('build_processing_pipeline.py' in ' '.join(cmd) and '--dry-run' not in cmd for cmd in calls))
        self.assertTrue(any('build-local-duckdb-shim.py' in ' '.join(cmd) for cmd in calls))
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['phases']['import']['live_refresh']['status'], 'completed')
        self.assertEqual(saved['phases']['import']['live_refresh']['gmail_sync_after'], '2026-05-15')
        self.assertFalse(saved['phases']['import']['live_refresh']['network_changed'])
        self.assertEqual(saved['status'], 'ready')

    def test_run_blocks_index_when_processing_cost_hits_limit(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        account_payload = {'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
        }}
        accounts.write_text(json.dumps(account_payload), encoding='utf-8')
        accounts_summary = setup.accounts_summary(accounts)
        people = tmp / '.powerpacks/network-import/merged/people.csv'
        people.parent.mkdir(parents=True)
        people.write_text('id\ncurrent\n', encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': '2026-05-29T12:00:00Z',
                    'source_fingerprint': setup.linked_source_fingerprint(accounts_summary),
                    'after_people_sha256': setup.sha256_file(people),
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            if 'build_processing_pipeline.py' in joined and '--dry-run' in cmd:
                return 0, {
                    'status': 'dry_run',
                    'estimated_cost_usd': 10.0,
                    'estimated_paid_calls': {'role_enrichment': 40},
                    'estimated_costs': {'known_pricing': True, 'total_estimated_usd': 10.0},
                }, ''
            self.assertNotIn('build_processing_pipeline.py', joined)
            self.assertNotIn('build-local-duckdb-shim.py', joined)
            run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
            merged_dir = run_dir / 'merged'
            merged_dir.mkdir(parents=True)
            for name, content in {
                'people.csv': 'id\ncurrent\n',
                'network_contacts.csv': 'id\nc1\n',
                'network_contact_sources.csv': 'id\ns1\n',
                'network_companies.csv': 'id\nco1\n',
                'merge_manifest.json': '{}\n',
            }.items():
                (merged_dir / name).write_text(content, encoding='utf-8')
            duckdb_dir = run_dir / 'duckdb'
            duckdb_dir.mkdir(parents=True)
            duckdb = duckdb_dir / 'network.setup-refresh-test.duckdb'
            manifest = duckdb_dir / 'manifest.setup-refresh-test.json'
            duckdb.write_text('duckdb', encoding='utf-8')
            manifest.write_text('{}\n', encoding='utf-8')
            return 0, {
                'status': 'completed',
                'run_id': 'setup-refresh-test',
                'artifacts': {
                    'merged_people_csv': str(merged_dir / 'people.csv'),
                    'network_contacts_csv': str(merged_dir / 'network_contacts.csv'),
                    'network_contact_sources_csv': str(merged_dir / 'network_contact_sources.csv'),
                    'network_companies_csv': str(merged_dir / 'network_companies.csv'),
                    'merge_manifest': str(merged_dir / 'merge_manifest.json'),
                    'duckdb': str(duckdb),
                    'duckdb_manifest': str(manifest),
                },
            }, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
            gmail_sync_lookback_days=14,
            auto_spend_limit_usd=10.0,
        )
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            code = setup.run_setup(args)
        self.assertEqual(code, 20)
        saved = json.loads(ledger.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'blocked_approval')
        self.assertEqual(saved['phases']['index']['status'], 'blocked_approval')
        self.assertEqual(saved['phases']['index']['estimated_cost_usd'], 10.0)
        self.assertFalse(any('build-local-duckdb-shim.py' in ' '.join(cmd) for cmd in calls))

    def test_handoff_structured_approvals_and_worker_group(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com', 'work@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com', 'work@example.com']}},
            'linkedin_csv': {'linked': True, 'skipped': False, 'usernames': ['me'], 'artifacts': ['Connections.csv'], 'config': {'csv_path': 'Connections.csv', 'source_label': 'me'}},
            'twitter': {'linked': False, 'skipped': True, 'usernames': ['stale'], 'artifacts': [], 'config': {'handle': 'stale'}},
        }}), encoding='utf-8')
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'))
        payload = setup.handoff_payload(ns)
        self.assertEqual(payload['status'], 'ok')
        self.assertIn('import', payload['worker_groups'])
        self.assertTrue(payload['worker_groups']['import']['parallel'])
        self.assertIn('import_network_fan_in', payload['commands'])
        self.assertIn('processing_plan', payload['commands'])
        self.assertIn('processing_dry_run', payload['commands'])
        self.assertIn('--include-existing-artifacts', payload['commands']['import_network_run'])
        self.assertIn('--include-existing-artifacts', payload['commands']['import_network_dry_run'])
        jobs = payload['worker_groups']['import']['jobs']
        self.assertEqual([job['id'] for job in jobs if job['source'] == 'gmail'], ['gmail:me@example.com', 'gmail:work@example.com'])
        self.assertTrue(all('--ledger .powerpacks/network-import/import-network-run.' in job['command'] for job in jobs))
        self.assertNotIn('twitter', {job['source'] for job in jobs})
        ids = {x['id'] for x in payload['requires_approval']}
        for required in ['browser_auth', 'gcs_download', 'destructive_restore_overwrite', 'provider_spend']:
            self.assertIn(required, ids)

    def test_handoff_empty_accounts_has_no_executable_fallback_workers(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {}}), encoding='utf-8')
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'))
        payload = setup.handoff_payload(ns)
        group = payload['worker_groups']['import']
        self.assertEqual(group['status'], 'no_linked_sources')
        self.assertEqual(group['jobs'], [])

    def test_handoff_messages_worker_uses_selected_contact_import_steps(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'messages': {
                'linked': True,
                'skipped': False,
                'usernames': ['imessage'],
                'artifacts': [],
                'config': {'planned_contacts_csv': '.powerpacks/messages/contacts.csv'},
            },
        }}), encoding='utf-8')
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'))
        payload = setup.handoff_payload(ns)
        jobs = payload['worker_groups']['import']['jobs']
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job['source'], 'messages')
        self.assertIn('import_contacts_pipeline/import_contacts_pipeline.py run', job['command'])
        self.assertIn('--include-imessage --include-contact-merge', job['command'])
        self.assertIn('--include-powerset-candidates', job['command'])
        self.assertIn('--include-local-match', job['command'])
        self.assertIn('--include-llm-review', job['command'])
        self.assertIn('--include-review', job['command'])
        self.assertNotIn('--include-whatsapp', job['command'])
        self.assertNotIn('--include-research', job['command'])
        self.assertNotIn('--include-upload', job['command'])
        self.assertNotIn('stop-after', job['command'])
        self.assertEqual(job['requires_approval'], [])

    def test_handoff_messages_worker_includes_whatsapp_when_linked(self):
        tmp = self.temp_workspace()
        accounts = tmp / 'accounts.json'
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'messages': {
                'linked': True,
                'skipped': False,
                'usernames': ['imessage', 'whatsapp'],
                'artifacts': [],
                'config': {
                    'planned_contacts_csv': '.powerpacks/messages/contacts.csv',
                    'imessage': {'status': 'ready'},
                    'whatsapp': {'status': 'linked', 'authenticated': True},
                },
            },
        }}), encoding='utf-8')
        ns = argparse.Namespace(operator_id=OPERATOR_ID, accounts=str(accounts), setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'))
        payload = setup.handoff_payload(ns)
        job = payload['worker_groups']['import']['jobs'][0]
        self.assertIn('--include-imessage --include-whatsapp --include-contact-merge', job['command'])
        self.assertIn('--include-powerset-candidates', job['command'])
        self.assertIn('--include-local-match', job['command'])
        self.assertIn('--include-llm-review', job['command'])
        self.assertIn('--include-review', job['command'])
        self.assertNotIn('--include-research', job['command'])
        self.assertEqual(job['requires_approval'], ['whatsapp_qr'])

    def test_setup_skill_documents_product_contract(self):
        text = (ROOT / 'packs/ingestion/skills/setup/SKILL.md').read_text(encoding='utf-8')
        for required in [
            'bootstrap',
            'link-only',
            'automatically refresh linked sources',
            'msgvault',
            'msgvault sync-full',
            'add-test-users',
            'add-account',
            'import-network',
            'parallel worker sub-agents',
            'fan-in',
            'build_processing_pipeline.py plan',
            'browser/Gmail OAuth',
            'GCP Desktop OAuth app',
            'exact-object GCS bootstrap download',
            'destructive bootstrap restore/overwrite',
            'RapidAPI, Parallel, OpenAI',
            'WhatsApp QR',
        ]:
            self.assertIn(required, text)

    def test_setup_and_import_skills_include_user_friendly_import_copy(self):
        setup_text = (ROOT / 'packs/ingestion/skills/setup/SKILL.md').read_text(encoding='utf-8')
        import_text = (ROOT / 'packs/ingestion/skills/import-network/SKILL.md').read_text(encoding='utf-8')
        onboard_text = (ROOT / 'packs/ingestion/skills/onboard/SKILL.md').read_text(encoding='utf-8')
        for required in [
            'How to explain setup to the user',
            'Keep the user\'s view simple',
            'I won’t upload anything automatically',
            'Use jargon only in hidden/internal notes',
            'Use normal user language',
            'refresh anything that is missing or stale',
            'include existing artifacts',
        ]:
            self.assertIn(required, setup_text)
        for required in [
            'User-facing tone',
            'The user should hear what is happening in product terms',
            'I found these connected sources',
            'I won’t upload anything automatically',
            'Do not repeat it to the user',
            'These sources can be imported at the same time',
        ]:
            self.assertIn(required, import_text)
        self.assertIn('handoff.handoff_command', onboard_text)
        self.assertIn('only post-link handoff path', onboard_text)
        self.assertIn('legacy direct onboarding worker phases', onboard_text)
        self.assertNotIn('worker_phases[0]', onboard_text)
        self.assertNotIn('worker_phases[1]', onboard_text)

    def test_pull_refuses_without_allow_flag(self):
        args = argparse.Namespace(gcs_uri='gs://bucket/object.tar.gz', output='out.tar.gz', allow_gcs_download=False)
        self.assertEqual(setup.run_pull(args), 2)

    def test_raw_google_application_credentials_uses_isolated_gcloud_and_cleans_up(self):
        tmp = self.temp_workspace()
        out = tmp / 'bundle.tar.gz'
        calls = []
        def fake_run(cmd, env=None, capture_output=None, text=None, check=None):
            calls.append((cmd, dict(env or {})))
            if cmd[:3] == ['gcloud', 'storage', 'cp']:
                out.write_bytes(b'bundle')
            return subprocess_result(0, '', '')
        def subprocess_result(code, stdout, stderr):
            return type('R', (), {'returncode': code, 'stdout': stdout, 'stderr': stderr})()
        args = argparse.Namespace(gcs_uri='gs://bucket/object.tar.gz', output=str(out), allow_gcs_download=True, download_backend='gcloud')
        with mock.patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': '{"type":"service_account"}'}, clear=False):
            with mock.patch.object(setup.subprocess, 'run', side_effect=fake_run):
                self.assertEqual(setup.run_pull(args), 0)
        self.assertEqual(calls[0][0][:3], ['gcloud', 'auth', 'activate-service-account'])
        cfg = Path(calls[0][1]['CLOUDSDK_CONFIG'])
        key = Path(calls[0][1]['GOOGLE_APPLICATION_CREDENTIALS'])
        self.assertFalse(cfg.exists())
        self.assertFalse(key.exists())
        self.assertEqual(calls[1][0], ['gcloud', 'storage', 'cp', 'gs://bucket/object.tar.gz', str(out)])

    def test_python_google_cloud_storage_backend_downloads_exact_object(self):
        tmp = self.temp_workspace()
        out = tmp / 'bundle.tar.gz'
        seen = {}

        class Blob:
            def __init__(self, name):
                self.name = name
            def download_to_filename(self, path):
                seen['object'] = self.name
                Path(path).write_bytes(b'bundle')

        class Bucket:
            def __init__(self, name):
                self.name = name
            def blob(self, name):
                seen['bucket'] = self.name
                return Blob(name)

        class Client:
            def bucket(self, name):
                return Bucket(name)

        google_mod = types.ModuleType('google')
        cloud_mod = types.ModuleType('google.cloud')
        storage_mod = types.ModuleType('google.cloud.storage')
        storage_mod.Client = Client
        cloud_mod.storage = storage_mod
        google_mod.cloud = cloud_mod
        args = argparse.Namespace(gcs_uri='gs://bucket/path/object.tar.gz', output=str(out), allow_gcs_download=True, download_backend='python')
        with mock.patch.dict(sys.modules, {'google': google_mod, 'google.cloud': cloud_mod, 'google.cloud.storage': storage_mod}):
            with mock.patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': '{"type":"service_account"}'}, clear=False):
                self.assertEqual(setup.run_pull(args), 0)
        self.assertEqual(seen, {'bucket': 'bucket', 'object': 'path/object.tar.gz'})
        self.assertTrue(out.exists())
        self.assertFalse(list((tmp / '.powerpacks/setup/tmp').glob('gcloud-key-*.json')))

    def test_auto_pull_falls_back_to_python_backend_when_gcloud_cp_fails(self):
        tmp = self.temp_workspace()
        out = tmp / 'bundle.tar.gz'

        def fake_python_download(gcs_uri, output, env):
            output.write_bytes(b'bundle')
            return 0, {'status': 'ok', 'download_backend': 'python-google-cloud-storage'}

        args = argparse.Namespace(gcs_uri='gs://bucket/object.tar.gz', output=str(out), allow_gcs_download=True, download_backend='auto')
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(setup.shutil, 'which', return_value='/bin/gcloud'):
                with mock.patch.object(setup, 'run_gcloud_download', return_value=(2, {'status': 'needs_user_action', 'reason': 'gcloud storage cp failed', 'stderr': 'denied'})):
                    with mock.patch.object(setup, 'run_python_gcs_download', side_effect=fake_python_download):
                        self.assertEqual(setup.run_pull(args), 0)
        self.assertTrue(out.exists())

    def test_next_returns_link_phase_when_accounts_are_missing(self):
        tmp = self.temp_workspace()
        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(tmp / '.powerpacks/ingestion/accounts.json'),
            setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'),
            refresh_interval_hours=168,
        )
        payload = setup.next_action_payload(args)

        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['next']['phase'], 'link')
        self.assertEqual(payload['next']['status'], 'run_command')
        self.assertIn('setup.py link', payload['next']['command'])
        self.assertIn(str(tmp / '.powerpacks/ingestion/accounts.json'), payload['next']['command'])
        self.assertIn(str(tmp / '.powerpacks/setup/setup-run.json'), payload['next']['command'])

        index_command = setup.setup_phase_command(args, 'index')
        self.assertIn(str(tmp / '.powerpacks/ingestion/accounts.json'), index_command)
        self.assertIn(str(tmp / '.powerpacks/setup/setup-run.json'), index_command)

    def test_bootstrap_phase_applies_matching_local_bundle_only(self):
        tmp = self.temp_workspace()
        bundle = tmp / '.powerpacks/operator-bootstrap/bundles/patrick.operator-bootstrap.tar.gz'
        bundle.parent.mkdir(parents=True)
        make_bundle(bundle)
        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(tmp / '.powerpacks/ingestion/accounts.json'),
            setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'),
            bootstrap_bundle='',
            force_bootstrap=False,
            refresh_interval_hours=168,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = setup.run_bootstrap_phase(args)
        payload = json.loads(buf.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload['phase'], 'bootstrap')
        self.assertEqual(payload['bootstrap']['status'], 'ok')
        self.assertTrue((tmp / '.powerpacks/search-index/records/people.records.jsonl').exists())
        self.assertEqual(payload['next']['phase'], 'link')

    def test_link_phase_wraps_onboarding_without_importing(self):
        tmp = self.temp_workspace()
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            accounts = tmp / '.powerpacks/ingestion/accounts.json'
            accounts.parent.mkdir(parents=True)
            accounts.write_text(json.dumps({'version': 2, 'accounts': {
                'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
            }}), encoding='utf-8')
            return 0, {'status': 'ok', 'linked_sources': ['gmail']}, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(tmp / '.powerpacks/ingestion/accounts.json'),
            setup_ledger=str(tmp / '.powerpacks/setup/setup-run.json'),
            refresh_interval_hours=168,
            gmail_db='',
            gmail_account=['me@example.com'],
            gmail_add_email=[],
            gmail_authorized_email=[],
            gmail_all=False,
            skip_source=[],
            linkedin_csv='',
            linkedin_source_user='',
            twitter_handle='',
        )
        buf = io.StringIO()
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            with contextlib.redirect_stdout(buf):
                code = setup.run_link_phase(args)
        payload = json.loads(buf.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload['phase'], 'link')
        self.assertIn('onboarding/onboarding.py', ' '.join(calls[0]))
        self.assertIn('--gmail-account', calls[0])
        self.assertEqual(payload['next']['phase'], 'import')
        self.assertFalse(any('import_network_pipeline.py' in ' '.join(cmd) for cmd in calls))

    def test_import_phase_refreshes_linked_sources_without_indexing(self):
        tmp = self.temp_workspace()
        accounts = tmp / '.powerpacks/ingestion/accounts.json'
        accounts.parent.mkdir(parents=True)
        accounts.write_text(json.dumps({'version': 2, 'accounts': {
            'gmail': {'linked': True, 'skipped': False, 'usernames': ['me@example.com'], 'artifacts': [], 'config': {'selected_accounts': ['me@example.com']}},
            'linkedin_csv': {'linked': False, 'skipped': True, 'usernames': [], 'artifacts': [], 'config': {}},
        }}), encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        ledger.parent.mkdir(parents=True)
        summary = setup.accounts_summary(accounts)
        ledger.write_text(json.dumps({
            'schema_version': 1,
            'status': 'ready',
            'phases': {
                'bootstrap': {'status': 'restored'},
                'link': {'status': 'ready'},
                'import': {'status': 'ready', 'live_refresh': {
                    'status': 'completed',
                    'completed_at': setup.now(),
                    'source_fingerprint': setup.linked_source_fingerprint(summary),
                }},
                'index': {'status': 'ready'},
            },
        }), encoding='utf-8')
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            joined = ' '.join(cmd)
            self.assertIn('import_network_pipeline.py', joined)
            run_dir = tmp / '.powerpacks/network-import/network-runs/setup-refresh-test'
            merged_dir = run_dir / 'merged'
            merged_dir.mkdir(parents=True)
            for name, content in {
                'people.csv': 'id\nnew\n',
                'network_contacts.csv': 'id\nc1\n',
                'network_contact_sources.csv': 'id\ns1\n',
                'network_companies.csv': 'id\nco1\n',
                'merge_manifest.json': '{}\n',
            }.items():
                (merged_dir / name).write_text(content, encoding='utf-8')
            return 0, {
                'status': 'completed',
                'run_id': 'setup-refresh-test',
                'artifacts': {'merged_people_csv': str(merged_dir / 'people.csv')},
            }, ''

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(accounts),
            setup_ledger=str(ledger),
            refresh_interval_hours=168,
            gmail_sync_lookback_days=14,
            only_if_due=False,
        )
        buf = io.StringIO()
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            with contextlib.redirect_stdout(buf):
                code = setup.run_import_phase(args)
        payload = json.loads(buf.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload['phase'], 'import')
        self.assertEqual(payload['status'], 'completed')
        self.assertTrue(any('import_network_pipeline.py' in ' '.join(cmd) for cmd in calls))
        self.assertFalse(any('build_processing_pipeline.py' in ' '.join(cmd) for cmd in calls))

    def test_index_phase_materializes_records_without_importing(self):
        tmp = self.temp_workspace()
        records = tmp / '.powerpacks/search-index/records'
        records.mkdir(parents=True)
        (records / 'people.records.jsonl').write_text('{}\n', encoding='utf-8')
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        calls = []

        def fake_run_json_command(cmd, timeout=6 * 60 * 60):
            calls.append(cmd)
            return fake_local_duckdb_payload(tmp)

        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            accounts=str(tmp / '.powerpacks/ingestion/accounts.json'),
            setup_ledger=str(ledger),
            refresh_interval_hours=168,
            auto_spend_limit_usd=10.0,
        )
        buf = io.StringIO()
        with mock.patch.object(setup, 'run_json_command', side_effect=fake_run_json_command):
            with contextlib.redirect_stdout(buf):
                code = setup.run_index_phase(args)
        payload = json.loads(buf.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload['phase'], 'index')
        self.assertEqual(payload['status'], 'ready')
        self.assertTrue(any('build-local-duckdb-shim.py' in ' '.join(cmd) for cmd in calls))
        self.assertFalse(any('import_network_pipeline.py' in ' '.join(cmd) for cmd in calls))

    def test_setup_skips_bootstrap_when_no_local_bundle_matches(self):
        tmp = self.temp_workspace()
        ledger = tmp / '.powerpacks/setup/setup-run.json'
        args = argparse.Namespace(
            operator_id=OPERATOR_ID,
            bootstrap_bundle='',
            force_bootstrap=False,
            setup_ledger=str(ledger),
        )
        payload, code = setup.maybe_apply_bootstrap(args, setup.load_setup_ledger(ledger))

        self.assertEqual(code, 0)
        self.assertEqual(payload, {'status': 'skipped', 'reason': 'no_matching_bootstrap_bundle'})


if __name__ == '__main__':
    unittest.main()
