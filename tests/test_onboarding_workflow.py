"""Local onboarding reuses source outputs, preserves gates, and stops on errors."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.discover.gmail.discover import GmailDiscovery
from packs.ingestion.primitives.discover.linkedin.connections import LinkedInConnections
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery
from packs.ingestion.primitives.discover.messages.extract_imessage import IMessageExtractor
from packs.ingestion.primitives.discover.messages.wacli import auth
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.imports import common as import_common
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.primitives.setup.automations import accounts
from packs.ingestion.primitives.setup.automations.shell import CommandResult
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep
from packs.powerset.primitives.install.tools import ImportTools
from packs.powerset.primitives.install.workflow import SourceOnboarding, main


def payload(**record):
    return SimpleNamespace(to_payload=lambda: record)


class SourceOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = self.root / '.env'
        self.env.write_text('CUSTOM_SETTING=keep\n')
        self.data = self.root / '.powerpacks/network-import/import/messages/people.csv'
        self.data.parent.mkdir(parents=True)
        self.data.write_text('person_id,name\ncandidate:phone:+15550100,Jordan Bravo\n')
        InstallStatus(self.root).write(step=InstallStep.SKILLS, status=InstallState.COMPLETED,
                                       message='Skills installed', pid=os.getpid())
        self.tools = patch.object(ImportTools, 'run', return_value={'status': 'ok'}).start()
        patch.object(LinkedInConnections, 'run', return_value={
            'status': 'completed', 'message': '1 LinkedIn connections (1 new)'}).start()
        patch.object(auth, 'auth_status', return_value=SimpleNamespace(authenticated=False)).start()
        patch.object(IMessageExtractor, 'check', return_value={'status': 'blocked_user_action'}).start()
        patch.object(accounts, 'status_payload', return_value={
            'config': {'oauth_configured': True}, 'database': {'exists': True}}).start()
        self.addCleanup(patch.stopall)
        original_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, original_cwd)
        self.cwd = self.root
        self.discovery = self.root / ".powerpacks/network-import/discover/gmail/manifest.json"
        self.discovery.parent.mkdir(parents=True)
        self.discovery.write_text(json.dumps({"children": [{"account_email": "casey@example.com",
            "status": "completed", "sync": {"status": "completed", "sync_after": "2023-01-01",
            "sync_after_source": "explicit_window", "sync_before": "", "query": "", "limit": 0}}]}))

    def assert_preserved(self):
        self.assertEqual(self.env.read_text(), 'CUSTOM_SETTING=keep\n')
        self.assertIn('Jordan Bravo', self.data.read_text())
        self.assertEqual(Path.cwd(), self.cwd)
        self.assertEqual(InstallStatus(self.root).read()['steps']['skills']['status'], 'completed')

    def test_no_source_defaults_to_gmail_messages_whatsapp_and_linkedin_without_history_choice(self):
        result = SourceOnboarding(self.root, sources=()).run()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['step'], 'gmail_login')
        self.assertEqual(result['action']['text'], 'Which Gmail account should I use?')
        self.assertEqual(result['plan'][0], 'skills')
        self.assertIn('gmail_import', result['plan'])
        self.assertIn('imessage_import', result['plan'])
        self.assertIn('whatsapp_import', result['plan'])
        self.assertIn('linkedin', result['plan'])
        self.assertIn((date.today() - timedelta(days=365)).isoformat(), result['retry_command'])
        self.tools.assert_not_called()
        self.assert_preserved()

    def test_default_gmail_uses_verified_install_account_before_configured_accounts(self):
        InstallStatus(self.root).write(step=InstallStep.NETWORK, status=InstallState.COMPLETED,
            message='Network checked', pid=os.getpid(), account_email='casey@example.com')
        with patch.object(accounts, 'status_payload', return_value={
                'config': {'oauth_configured': False}, 'database': {'exists': False},
                'accounts': [{'email': 'other@example.com'}]}):
            result = SourceOnboarding(self.root, sources=()).run()
        self.assertIn('--gmail-email casey@example.com', result['retry_command'])
        self.assertIn('browser-setup --email casey@example.com', result['action']['command'])

    def test_cli_without_source_continues_with_defaults(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['bin/onboard']), contextlib.redirect_stdout(output), \
             self.assertRaises(SystemExit) as exited:
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(exited.exception.code, 10)
        self.assertEqual(result['step'], 'gmail_login')
        self.assertIn('--source gmail --source imessage --source whatsapp', result['retry_command'])

    def test_default_sources_continue_from_reused_gmail_to_messages_permission(self):
        InstallStatus(self.root).write(step=InstallStep.NETWORK, status=InstallState.COMPLETED,
            message='Network checked', pid=os.getpid(), account_email='casey@example.com')
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=()).run()
        self.assertEqual(result['step'], 'imessage_access')
        self.assertEqual(result['steps']['gmail_import']['status'], 'completed')
        health.assert_called_once()
        self.assertEqual(health.call_args.args[1], ['casey@example.com'])
        sync.assert_not_called()

    def test_default_gmail_uses_only_a_unique_configured_account(self):
        for configured, expected in (([{'identifier': 'casey@example.com'}], ('casey@example.com',)),
                                     ([{'email': 'casey@example.com'}, {'email': 'other@example.com'}], ())):
            with self.subTest(configured=configured), \
                 patch.object(accounts, 'status_payload', return_value={'accounts': configured}):
                flow = SourceOnboarding(self.root, sources=())
            self.assertEqual(flow.gmail_emails, expected)

    def test_default_rerun_preserves_saved_source_account_history_store_and_skips(self):
        store = self.root / 'selected store'
        original = SourceOnboarding(self.root, sources=('gmail', 'whatsapp'),
            gmail_emails=('casey@example.com', 'jordan@example.com'), sync_after='2025-10-03',
            wacli_store=store, refresh=True, skip_sources=('whatsapp',))
        original._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, 'Connect Gmail')
        repeated = SourceOnboarding(self.root, sources=())
        self.assertEqual(repeated.retry_command, original.retry_command)
        override = SourceOnboarding(self.root, sources=('imessage',))
        self.assertEqual(tuple(override.sources), ('imessage',))
        self.assertEqual(override.gmail_emails, ('casey@example.com', 'jordan@example.com'))
        self.assertEqual(override.sync_after, '2025-10-03')
        self.assertEqual(override.wacli_store, store)
        self.assertEqual(override.skip_sources, ())

    def test_explicit_source_account_and_history_override_saved_choices(self):
        flow = SourceOnboarding(self.root, sources=('gmail',),
            gmail_emails=('casey@example.com',), sync_after='2023-01-01')
        flow._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, 'Connect Gmail')
        override = SourceOnboarding(self.root, sources=('skip',),
            gmail_emails=('jordan@example.com',), sync_after='2025-10-03')
        self.assertEqual(tuple(override.sources), ('skip',))
        self.assertEqual(override.gmail_emails, ('jordan@example.com',))
        self.assertEqual(override.sync_after, '2025-10-03')
        self.assertEqual(override.run()['step'], 'ready')

    def test_skip_finishes_without_calling_source_tools(self):
        result = SourceOnboarding(self.root, sources=('skip',)).run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['message'], 'Powerpacks is installed')
        self.tools.assert_not_called()
        self.assert_preserved()

    def test_skip_gmail_auth_wait_continues_to_next_selected_source(self):
        for next_source, next_step in (('imessage', 'imessage_access'), ('whatsapp', 'whatsapp_login')):
            with self.subTest(source=next_source), \
                 patch.object(accounts, 'check_accounts_payload', return_value={'status': 'needs_user_action'}), \
                 patch.object(auth, 'auth_report', return_value={'status': 'blocked_user_action'}), \
                 patch.object(GmailDiscovery, 'run') as gmail, \
                 patch.object(MessagesDiscovery, 'run') as messages:
                waiting = SourceOnboarding(self.root, sources=('gmail', next_source),
                    gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
                self.assertEqual(waiting['step'], 'gmail_login')
                result = SourceOnboarding(self.root, sources=('gmail', next_source),
                    skip_sources=('gmail',), gmail_emails=('casey@example.com',),
                    sync_after='2023-01-01').run()
            self.assertEqual(result['step'], next_step)
            self.assertEqual(result['status'], 'waiting')
            for step in ('gmail_tools', 'gmail_login', 'gmail_sync', 'gmail_import'):
                self.assertIn(step, result['plan'])
                self.assertEqual(result['steps'][step]['status'], 'skipped')
            gmail.assert_not_called()
            messages.assert_not_called()
            self.assert_preserved()

    def test_each_source_can_be_skipped_without_running_its_primitives(self):
        for source, steps in (
            ('gmail', ('gmail_tools', 'gmail_login', 'gmail_sync', 'gmail_import')),
            ('imessage', ('imessage_access', 'imessage_import')),
            ('whatsapp', ('whatsapp_tools', 'whatsapp_login', 'whatsapp_sync', 'whatsapp_import')),
            ('linkedin', ('linkedin',)),
        ):
            with self.subTest(source=source), \
                 patch.object(SourceOnboarding, '_gmail') as gmail, \
                 patch.object(SourceOnboarding, '_messages') as messages:
                result = SourceOnboarding(self.root, sources=(source,), skip_sources=(source,)).run()
            self.assertEqual(result['step'], 'ready')
            self.assertEqual(result['status'], 'completed')
            self.assertNotIn('deep_context', result['plan'])
            self.assertIsNone(result['action'])
            for step in steps:
                self.assertEqual(result['steps'][step]['status'], 'skipped')
            gmail.assert_not_called()
            messages.assert_not_called()
        self.tools.assert_not_called()
        self.assert_preserved()

    def test_skip_retains_previously_imported_source_files(self):
        imported = self.root / '.powerpacks/network-import/import/gmail/people.csv'
        imported.parent.mkdir(parents=True)
        imported.write_text('person_id,name\ncandidate:email:casey@example.com,Jordan Bravo\n')
        import_common.write_manifest('gmail', {'status': 'completed', 'stats': {'people': 1}})
        saved = {path: path.read_bytes() for path in imported.parent.iterdir()}
        InstallStatus(self.root).write(step=InstallStep.GMAIL_IMPORT, status=InstallState.COMPLETED,
            message='Gmail contacts ready', pid=os.getpid(), plan=['skills', 'sources', 'gmail_import'])
        result = SourceOnboarding(self.root, sources=('gmail', 'linkedin'), skip_sources=('gmail',)).run()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['gmail_import']['status'], 'skipped')
        self.assertEqual({path: path.read_bytes() for path in imported.parent.iterdir()}, saved)
        self.assert_preserved()

    def test_all_selected_sources_skipped_finish_without_processing(self):
        sources = ('gmail', 'imessage', 'whatsapp', 'linkedin')
        with patch.object(SourceOnboarding, '_gmail') as gmail, \
             patch.object(SourceOnboarding, '_messages') as messages:
            result = SourceOnboarding(self.root, sources=sources, skip_sources=sources).run()
        self.assertEqual(result['step'], 'ready')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['plan'][-1], 'ready')
        self.assertNotIn('deep_context', result['plan'])
        self.assertTrue(all(result['steps'][step]['status'] == 'skipped'
                            for step in result['plan'] if step not in ('skills', 'sources', 'ready')))
        gmail.assert_not_called()
        messages.assert_not_called()
        self.tools.assert_not_called()
        self.assert_preserved()

    def test_repeated_skip_stays_skipped_and_removing_it_resumes_source(self):
        for _ in range(2):
            result = SourceOnboarding(self.root, sources=('gmail', 'linkedin'),
                                      skip_sources=('gmail', 'linkedin')).run()
            self.assertEqual(result['step'], 'ready')
            for step in ('gmail_tools', 'gmail_login', 'gmail_sync', 'gmail_import', 'linkedin'):
                self.assertEqual(result['steps'][step]['status'], 'skipped')
        result = SourceOnboarding(self.root, sources=('gmail', 'linkedin'), skip_sources=('linkedin',)).run()
        self.assertEqual(result['step'], 'gmail_login')
        self.assertEqual(result['status'], 'waiting')
        for step in ('gmail_tools', 'gmail_login', 'gmail_sync', 'gmail_import'):
            self.assertNotEqual(result['steps'][step]['status'], 'skipped')
        self.assertEqual(result['steps']['linkedin']['status'], 'skipped')
        self.assertNotIn('--skip-source gmail', result['retry_command'])

    def test_skip_cli_retains_exact_source_account_window_and_store_options(self):
        store = self.root / 'selected store'
        command = ['bin/onboard', '--source', 'gmail', '--source', 'whatsapp',
                   '--gmail-email', 'casey@example.com', '--gmail-email', 'jordan@example.com',
                   '--sync-after', '2023-01-01', '--wacli-store', str(store), '--refresh',
                   '--skip-source', 'gmail', '--skip-source', 'whatsapp']
        output = io.StringIO()
        with patch.object(sys, 'argv', command), contextlib.redirect_stdout(output), \
             self.assertRaises(SystemExit) as exited:
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(result['step'], 'ready')
        self.assertEqual(shlex.split(result['retry_command'])[1:], command[1:])

    def test_skip_leaves_active_source_worker_and_manifest_untouched(self):
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            status = InstallStatus(self.root)
            status.write(step=InstallStep.GMAIL_SYNC, status=InstallState.RUNNING,
                         message='Syncing Gmail', pid=process.pid)
            original = status.manifest_path.read_bytes()
            result = SourceOnboarding(self.root, sources=('gmail', 'imessage'), skip_sources=('gmail',)).run()
            self.assertEqual(result['step'], 'gmail_sync')
            self.assertEqual(result['installer_pid'], process.pid)
            self.assertIsNone(process.poll())
            self.assertEqual(status.manifest_path.read_bytes(), original)
            self.tools.assert_not_called()
        finally:
            process.stdin.close()
            process.wait(timeout=5)

    def test_missing_tools_give_agent_the_command(self):
        self.tools.return_value = {'status': 'needs_user_action', 'message': 'Mac password needed',
                                   'command': 'prepare-tools'}
        result = SourceOnboarding(self.root, sources=('whatsapp',)).run()
        self.assertEqual(result['step'], 'whatsapp_tools')
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['action']['command'], 'prepare-tools')

    def test_new_source_setup_clears_previous_processing_completion(self):
        status = InstallStatus(self.root)
        for step in (InstallStep.DEEP_CONTEXT, InstallStep.INDEX, InstallStep.VALIDATE, InstallStep.READY):
            status.write(step=step, status=InstallState.COMPLETED, message='Done', pid=os.getpid(),
                         plan=['skills', 'sources', 'deep_context', 'index', 'validate', 'ready'])
        result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['step'], 'imessage_access')
        for step in ('deep_context', 'index', 'validate', 'ready'):
            self.assertNotIn(step, result['steps'])
        self.assertEqual(result['plan'][-5:], ['deep_context', 'enrich', 'index', 'validate', 'ready'])
        self.assert_preserved()

    def test_external_reinstall_keeps_live_work_and_qr_wait_untouched(self):
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            for state, step in ((InstallState.RUNNING, InstallStep.GMAIL_SYNC),
                                (InstallState.WAITING, InstallStep.GMAIL_LOGIN),
                                (InstallState.WAITING, InstallStep.IMESSAGE_ACCESS),
                                (InstallState.WAITING, InstallStep.WHATSAPP_LOGIN),
                                (InstallState.WAITING, InstallStep.ACCOUNT)):
                with self.subTest(state=state, step=step):
                    status = InstallStatus(self.root)
                    status.write(step=step, status=state, message='Existing work', pid=process.pid,
                                 action={'kind': 'qr'} if step is InstallStep.WHATSAPP_LOGIN else None)
                    original = status.manifest_path.read_bytes()
                    result = SourceOnboarding(self.root, sources=('gmail',),
                        gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
                    self.assertEqual(result['step'], step.value)
                    self.assertEqual(result['status'], state.value)
                    self.assertEqual(status.manifest_path.read_bytes(), original)
            self.tools.assert_not_called()
        finally:
            process.stdin.close()
            process.wait(timeout=5)

    def test_cli_returns_still_working_for_live_installer(self):
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            status = InstallStatus(self.root)
            status.write(step=InstallStep.GMAIL_SYNC, status=InstallState.RUNNING,
                         message='Syncing Gmail', pid=process.pid)
            original = status.manifest_path.read_bytes()
            output = io.StringIO()
            with patch.object(sys, 'argv', ['bin/onboard', '--source', 'skip']), \
                 contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                main()
            self.assertEqual(exited.exception.code, 10)
            self.assertEqual(json.loads(output.getvalue())['step'], 'gmail_sync')
            self.assertEqual(status.manifest_path.read_bytes(), original)
        finally:
            process.stdin.close()
            process.wait(timeout=5)

    def test_dead_installer_does_not_block_recovery(self):
        process = subprocess.Popen([sys.executable, '-c', 'pass'])
        process.wait(timeout=5)
        InstallStatus(self.root).write(step=InstallStep.GMAIL_SYNC, status=InstallState.RUNNING,
            message='Interrupted work', pid=process.pid)
        result = SourceOnboarding(self.root, sources=('skip',)).run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['message'], 'Powerpacks is installed')

    def test_missing_gmail_selection_waits_before_tools_or_sync(self):
        with patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',)).run()
        self.assertEqual(result['action']['kind'], 'gmail')
        self.tools.assert_not_called()
        sync.assert_not_called()

    def test_threaded_flow_never_changes_process_directory(self):
        elsewhere = self.root / 'unrelated'
        elsewhere.mkdir()
        os.chdir(elsewhere)
        with patch.object(os, 'chdir', wraps=os.chdir) as change:
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        change.assert_not_called()
        self.assertEqual(Path.cwd(), elsewhere)
        self.assertEqual(result['status'], 'failed')
        os.chdir(self.root)

    def test_completed_source_history_survives_next_source_choice(self):
        InstallStatus(self.root).write(step=InstallStep.GMAIL_IMPORT, status=InstallState.COMPLETED,
            message='Gmail contacts ready', pid=os.getpid(), plan=['skills', 'sources', 'gmail_import', 'deep_context'])
        result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertIn('gmail_import', result['plan'])
        self.assertLess(result['plan'].index('gmail_import'), result['plan'].index('imessage_access'))

    def test_gmail_earlier_or_unknown_history_does_not_reuse_archive(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        for requested in ('2022-01-01', '2004-01-01'):
            with self.subTest(requested=requested), \
                 patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}), \
                 patch.object(import_common, 'import_manifest_current', return_value=current), \
                 patch.object(GmailDiscovery, '__init__', return_value=None) as init, \
                 patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')), \
                 patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
                SourceOnboarding(self.root, sources=('gmail',),
                                 gmail_emails=('casey@example.com',), sync_after=requested).run()
            init.assert_called_once_with(account_emails=['casey@example.com'], sync_after=requested)

    def test_gmail_unknown_or_filtered_history_is_not_proof_of_coverage(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        valid = {'status': 'completed', 'sync_after': '2023-01-01',
                 'sync_after_source': 'explicit_window', 'sync_before': '', 'query': '', 'limit': 0}
        for sync in ({}, {**valid, 'sync_after_source': 'msgvault.sources.last_sync_at'},
                     {**valid, 'sync_before': '2024-01-01'}, {**valid, 'query': 'from:me'},
                     {**valid, 'limit': 10}, {**valid, 'status': 'skipped'}):
            self.discovery.write_text(json.dumps({'children': [{'account_email': 'casey@example.com',
                                                                'status': 'completed', 'sync': sync}]}))
            with self.subTest(sync=sync), \
                 patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}), \
                 patch.object(import_common, 'import_manifest_current', return_value=current), \
                 patch.object(GmailDiscovery, 'run', return_value=payload(status='failed', error='test stop')) as discover:
                SourceOnboarding(self.root, sources=('gmail',),
                                 gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
            discover.assert_called_once()

    def test_gmail_wider_saved_history_covers_a_later_requested_start(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}), \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(GmailDiscovery, 'run') as discover:
            result = SourceOnboarding(self.root, sources=('gmail',),
                                     gmail_emails=('casey@example.com',), sync_after='2024-01-01').run()
        discover.assert_not_called()
        self.assertEqual(result['step'], 'deep_context')

    def test_linkedin_login_wait_does_not_block_messages_selected_after_it(self):
        with patch.object(IMessageExtractor, 'check', return_value={'status': 'ok'}), \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='completed')), \
             patch.object(MessagesImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})), \
             patch.object(LinkedInConnections, 'run', return_value={
                 'status': 'needs_user_action', 'message': 'Log in to LinkedIn in the Chrome window Powerpacks opened.'}):
            result = SourceOnboarding(self.root, sources=('linkedin', 'imessage')).run()
        self.assertEqual((result['step'], result['status']), ('linkedin', 'waiting'))
        self.assertEqual(result['message'], 'Log in to LinkedIn in the Chrome window Powerpacks opened.')
        self.assertEqual(result['steps']['imessage_import']['status'], 'completed')

    def test_processing_reports_saved_source_counts_without_changing_hosted_network_count(self):
        import_common.write_manifest('gmail', {'status': 'completed', 'stats': {'people': 12}})
        import_common.write_manifest('messages', {'status': 'completed', 'stats': {'people': 8}})
        InstallStatus(self.root).write(step=InstallStep.NETWORK, status=InstallState.COMPLETED,
            message='Team checked', pid=os.getpid(), person_count=91)
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}), \
             patch.object(import_common, 'import_manifest_current', return_value=current):
            result = SourceOnboarding(self.root, sources=('gmail',),
                gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        self.assertEqual(result['action']['details']['counts'], {'gmail': 12, 'messages': 8})
        self.assertEqual(result['person_count'], 91)
        self.assertEqual(result['message'], 'Gmail: 12 contacts · Messages: 8 contacts')
        self.assertEqual(result['plan'][-5:], ['deep_context', 'enrich', 'index', 'validate', 'ready'])

    def test_fresh_gmail_waits_for_oauth_app_before_any_account_sync(self):
        with patch.object(accounts, 'status_payload', return_value={
                'config': {'oauth_configured': False}, 'database': {'exists': False}}), \
             patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                                      gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['installer_pid'], 0)
        self.assertIn('browser-setup --email casey@example.com --add-account', result['action']['command'])
        health.assert_not_called()
        sync.assert_not_called()

    def test_gmail_missing_or_expired_authorization_continues_in_the_same_run(self):
        for verdict in ('missing_token', 'reauthorization_required'):
            health = {'status': 'needs_user_action', 'accounts': [
                {'email': 'casey@example.com', 'status': verdict},
                {'email': 'other@example.com', 'status': 'healthy'}]}

            def authorize(command, *, timeout):
                waiting = InstallStatus(self.root).read()
                self.assertEqual(waiting['step'], 'gmail_login')
                self.assertEqual(waiting['status'], 'waiting')
                self.assertEqual(waiting['installer_pid'], os.getpid())
                self.assertEqual(waiting['action']['kind'], 'gmail')
                self.assertEqual(waiting['action']['details']['email'], 'casey@example.com')
                self.assertEqual(timeout, 900)
                return CommandResult(ok=True, returncode=0)

            with self.subTest(verdict=verdict), \
                 patch.object(accounts, 'check_accounts_payload', side_effect=[health, {'status': 'ok'}]) as check, \
                 patch.object(accounts, 'run_visible_command', side_effect=authorize) as login, \
                 patch.object(import_common, 'import_manifest_current', return_value=None), \
                 patch.object(GmailDiscovery, '__init__', return_value=None) as init, \
                 patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')) as sync, \
                 patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
                result = SourceOnboarding(self.root, sources=('gmail',),
                    gmail_emails=('casey@example.com', 'other@example.com'), sync_after='2023-01-01').run()
            command = login.call_args.args[0]
            self.assertEqual(command[:5], ['msgvault', '--home', str(Path('~/.msgvault').expanduser()),
                                           'add-account', 'casey@example.com'])
            self.assertEqual('--force' in command, verdict == 'reauthorization_required')
            self.assertNotIn('--headless', command)
            self.assertEqual(login.call_count, 1)
            self.assertEqual(check.call_count, 2)
            for call in check.call_args_list:
                self.assertEqual(call.args[1], ['casey@example.com', 'other@example.com'])
            init.assert_called_once_with(account_emails=['casey@example.com', 'other@example.com'],
                                         sync_after='2023-01-01')
            sync.assert_called_once()
            self.assertEqual(result['step'], 'deep_context')
            self.assertEqual(result['steps']['gmail_import']['status'], 'completed')
            self.assert_preserved()

    def test_configured_gmail_without_database_authorizes_before_health_and_sync(self):
        with patch.object(accounts, 'status_payload', return_value={
                'config': {'oauth_configured': True}, 'database': {'exists': False}}), \
             patch.object(accounts, 'run_visible_command', return_value=CommandResult(ok=True, returncode=0)) as login, \
             patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')) as sync, \
             patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            result = SourceOnboarding(self.root, sources=('gmail',),
                gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        login.assert_called_once()
        health.assert_called_once()
        sync.assert_called_once()
        self.assertEqual(result['step'], 'deep_context')
        self.assert_preserved()

    def test_gmail_authorization_error_halts_before_sync(self):
        health = {'status': 'needs_user_action', 'accounts': [
            {'email': 'casey@example.com', 'status': 'reauthorization_required'}]}
        with patch.object(accounts, 'check_accounts_payload', return_value=health) as check, \
             patch.object(accounts, 'run_visible_command', return_value=CommandResult(
                 ok=False, returncode=1, message='OAuth app rejected')) as login, \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        login.assert_called_once()
        check.assert_called_once()
        sync.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['step'], 'gmail_login')
        self.assertEqual(result['action']['kind'], 'gmail')
        self.assertEqual(result['message'], 'OAuth app rejected')
        self.assert_preserved()

    def test_gmail_callback_for_other_account_does_not_start_sync(self):
        local = {'msgvault': {'installed': True}, 'config': {'oauth_configured': True},
                 'database': {'exists': True}, 'accounts': []}
        other = {**local, 'accounts': [{'email': 'other@example.com'}]}
        with patch.object(accounts, 'status_payload', side_effect=[local, local, other]), \
             patch.object(accounts, 'run_visible_command', return_value=CommandResult(ok=True, returncode=0)) as login, \
             patch.object(accounts, 'run_msgvault') as verify, \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        login.assert_called_once()
        verify.assert_not_called()
        self.assertEqual(result['step'], 'gmail_login')
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['installer_pid'], 0)
        self.assertEqual(result['action']['details']['requested_accounts'], ['casey@example.com'])
        self.assertEqual(result['action']['details']['accounts_to_authorize'], ['casey@example.com'])
        sync.assert_not_called()

    def test_gmail_transient_failure_does_not_reauthorize_or_sync(self):
        health = {'status': 'error', 'error_accounts': ['casey@example.com'], 'error': 'DNS failure'}
        with patch.object(accounts, 'check_accounts_payload', return_value=health), \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                                      gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['action']['details']['error'], 'DNS failure')
        sync.assert_not_called()

    def test_reinstall_reuses_current_gmail_but_still_checks_token(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]},
        })
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as check, \
             patch.object(accounts, 'add_account') as login, \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                                      gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        check.assert_called_once()
        login.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['steps']['gmail_import']['status'], 'completed')
        self.assert_preserved()

    def test_gmail_new_account_or_refresh_runs_exact_bounded_sync(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'old@example.com'}]},
        })
        for refresh in (False, True):
            with self.subTest(refresh=refresh), \
                 patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}), \
                 patch.object(import_common, 'import_manifest_current', return_value=current), \
                 patch.object(GmailDiscovery, '__init__', return_value=None) as init, \
                 patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')), \
                 patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
                result = SourceOnboarding(self.root, sources=('gmail',),
                                          gmail_emails=('casey@example.com',), sync_after='2023-01-01',
                                          refresh=refresh).run()
            init.assert_called_once_with(account_emails=['casey@example.com'], sync_after='2023-01-01')
            self.assertEqual(result['step'], 'deep_context')
        self.assert_preserved()

    def test_imessage_denied_waits_at_os_gate_and_does_not_read_contacts(self):
        with patch.object(IMessageExtractor, 'check', return_value={'status': 'blocked_user_action', 'chat_db': {'readable': False}}), \
             patch.object(MessagesDiscovery, 'run') as discover:
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(result['action']['kind'], 'permission')
        self.assertIn('Privacy_AllFiles', result['action']['url'])
        discover.assert_not_called()
        self.assert_preserved()

    def test_imessage_permission_granted_continues_in_the_same_run(self):
        blocked = {'status': 'blocked_user_action',
                   'chat_db': {'exists': True, 'readable': False, 'missing_tables': []}}

        def wait(seconds):
            waiting = InstallStatus(self.root).read()
            self.assertEqual(waiting['step'], 'imessage_access')
            self.assertEqual(waiting['status'], 'waiting')
            self.assertEqual(waiting['installer_pid'], os.getpid())
            self.assertEqual(waiting['action']['kind'], 'permission')
            self.assertEqual(seconds, 2)

        with patch.object(IMessageExtractor, 'check', side_effect=[blocked, {'status': 'ok'}]) as check, \
             patch('time.sleep', side_effect=wait) as sleep, \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='completed')) as discover, \
             patch.object(MessagesImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once()
        discover.assert_called_once()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['imessage_import']['status'], 'completed')
        self.assert_preserved()

    def test_imessage_permission_wait_survives_user_distraction(self):
        blocked = {'status': 'blocked_user_action',
                   'chat_db': {'exists': True, 'readable': False, 'missing_tables': []}}
        with patch.object(IMessageExtractor, 'check', side_effect=[blocked] * 4 + [{'status': 'ok'}]), \
             patch('time.monotonic', return_value=100000), patch('time.sleep') as sleep, \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='completed')) as discover, \
             patch.object(MessagesImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(sleep.call_count, 4)
        discover.assert_called_once()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['installer_pid'], 0)
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['imessage_import']['status'], 'completed')
        self.assert_preserved()

    def test_expired_gmail_login_reopens_and_continues_without_chat_nudge(self):
        health = {'status': 'needs_user_action', 'accounts': [
            {'email': 'casey@example.com', 'status': 'reauthorization_required'}]}
        with patch.object(accounts, 'check_accounts_payload', side_effect=[health, {'status': 'ok'}]), \
             patch.object(accounts, 'run_visible_command', side_effect=[
                 CommandResult(ok=False, returncode=124, message='msgvault timed out'), CommandResult(ok=True)]) as login, \
             patch.object(import_common, 'import_manifest_current', return_value=None), \
             patch.object(GmailDiscovery, '__init__', return_value=None), \
             patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')), \
             patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            result = SourceOnboarding(self.root, sources=('gmail',), gmail_emails=('casey@example.com',)).run()
        self.assertEqual(login.call_count, 2)
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['gmail_import']['status'], 'completed')

    def test_imessage_missing_store_or_schema_does_not_wait_for_permission(self):
        for chat in ({'exists': False, 'readable': False},
                     {'exists': True, 'readable': True, 'missing_tables': ['handle']}):
            with self.subTest(chat=chat), \
                 patch.object(IMessageExtractor, 'check', return_value={
                     'status': 'blocked_user_action', 'chat_db': chat}), \
                 patch('time.sleep') as sleep, patch.object(MessagesDiscovery, 'run') as discover:
                result = SourceOnboarding(self.root, sources=('imessage',)).run()
            sleep.assert_not_called()
            discover.assert_not_called()
            self.assertEqual(result['installer_pid'], 0)

    def test_whatsapp_qr_wait_is_written_before_auth_without_opening_browser(self):
        store = self.root / 'isolated-wacli'
        def authenticate(actual_store, *, open_qr_page):
            self.assertEqual(actual_store, store)
            self.assertFalse(open_qr_page)
            self.assertEqual(InstallStatus(self.root).read()['action']['kind'], 'qr')
            return {'status': 'blocked_user_action', 'message': 'Scan QR'}
        with patch.object(auth, 'auth_report', side_effect=authenticate), \
             patch.object(MessagesDiscovery, 'run') as discover:
            result = SourceOnboarding(self.root, sources=('whatsapp',), wacli_store=store).run()
        self.assertEqual(result['status'], 'waiting')
        self.assertIn('--wacli-store', result['retry_command'])
        discover.assert_not_called()
        self.assert_preserved()

    def test_already_linked_whatsapp_never_displays_qr_action(self):
        original_write = InstallStatus.write
        def authenticate(store, *, open_qr_page):
            current = InstallStatus(self.root).read()
            self.assertEqual(current['status'], 'running')
            self.assertEqual(current['message'], 'Checking WhatsApp')
            self.assertIsNone(current['action'])
            return {'status': 'linked'}
        with patch.object(auth, 'auth_status', return_value=SimpleNamespace(authenticated=True)), \
             patch.object(auth, 'auth_report', side_effect=authenticate), \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='failed', error='test stop')), \
             patch.object(InstallStatus, 'write', autospec=True, side_effect=original_write) as writes:
            SourceOnboarding(self.root, sources=('whatsapp',)).run()
        self.assertFalse(any((call.kwargs.get('action') or {}).get('kind') == 'qr'
                             for call in writes.call_args_list))

    def test_message_reinstall_checks_access_then_reuses_import(self):
        current = import_common.ImportManifest.from_payload('messages', {'status': 'completed'})
        contacts = self.root / '.powerpacks/messages/imessage.contacts.csv'
        contacts.parent.mkdir(parents=True)
        contacts.write_text('phone,name\n+15550100,Jordan Bravo\n')
        with patch.object(IMessageExtractor, 'check', return_value={'status': 'ok'}) as check, \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(MessagesDiscovery, 'run') as discover:
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        check.assert_called_once_with(strict=True)
        discover.assert_not_called()
        self.assertEqual(result['step'], 'deep_context')

    def test_expired_whatsapp_qr_renews_inside_the_owned_command(self):
        expired = PrimitiveBlocked({'status': 'blocked_user_action', 'message': 'Scan the QR',
                                    'detail': 'command timed out after 600s'})
        with patch.object(auth, 'auth_report', side_effect=[expired, {'status': 'linked'}]) as link, \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='completed')), \
             patch.object(MessagesImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            result = SourceOnboarding(self.root, sources=('whatsapp',)).run()
        self.assertEqual(link.call_count, 2)
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['whatsapp_login']['status'], 'completed')

    def test_native_whatsapp_user_block_is_waiting_not_a_sync_failure(self):
        blocked = PrimitiveBlocked({'status': 'blocked_user_action', 'message': 'Allow linked devices on your phone'})
        with patch.object(auth, 'auth_report', side_effect=blocked), patch.object(MessagesDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('whatsapp',)).run()
        sync.assert_not_called()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['installer_pid'], 0)
        self.assertEqual(result['steps']['whatsapp_login']['status'], 'waiting')
        self.assertEqual(result['message'], 'Allow linked devices on your phone')
        self.assert_preserved()

    def test_changed_whatsapp_store_never_reuses_other_accounts_contacts(self):
        current = import_common.ImportManifest.from_payload('messages', {'status': 'completed'})
        contacts = self.root / '.powerpacks/messages/whatsapp.contacts.csv'
        contacts.parent.mkdir(parents=True)
        contacts.write_text('phone,name\n+15550100,Jordan Bravo\n')
        old_manifest = contacts.with_name('whatsapp.contacts.csv.manifest.json')
        old_manifest.write_text('{"store": ".powerpacks/messages/wacli"}')
        with patch.object(auth, 'auth_report', return_value={'status': 'linked'}), \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(MessagesDiscovery, '__init__', return_value=None) as init, \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='completed')) as discover, \
             patch.object(MessagesImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            store = self.root / 'other-account'
            SourceOnboarding(self.root, sources=('whatsapp',), wacli_store=store).run()
        discover.assert_called_once()
        init.assert_called_once_with(include_imessage=False, include_whatsapp=True,
                                     wacli_store=store, open_qr_page=False)

    def test_real_manifest_fingerprints_reuse_messages_without_changing_output(self):
        contacts = self.root / '.powerpacks/messages/contacts.csv'
        contacts.parent.mkdir(parents=True)
        contacts.write_text('phone,name\n+15550100,Jordan Bravo\n')
        imessage = contacts.with_name('imessage.contacts.csv')
        imessage.write_text(contacts.read_text())
        original = self.data.read_bytes()
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            import_common.write_manifest('messages', {'status': 'completed',
                'input': {'contacts_csv': str(contacts)}, 'outputs': {'people_csv': str(self.data)}})
        finally:
            os.chdir(previous)
        with patch.object(IMessageExtractor, 'check', return_value={'status': 'ok'}), \
             patch.object(MessagesDiscovery, 'run') as discover:
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        discover.assert_not_called()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(self.data.read_bytes(), original)

    def test_discovery_failure_retains_payload_and_does_not_import(self):
        with patch.object(IMessageExtractor, 'check', return_value={'status': 'ok'}), \
             patch.object(MessagesDiscovery, 'run', return_value=payload(status='failed', error='disk full')), \
             patch.object(MessagesImport, 'run') as importer:
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['action']['details']['error'], 'disk full')
        importer.assert_not_called()
        self.assert_preserved()

    def test_unexpected_failure_is_logged_with_retry_command_and_cwd_restored(self):
        with patch.object(IMessageExtractor, 'check', side_effect=OSError('Permission denied')):
            result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['action']['details']['error_type'], 'OSError')
        self.assertIn('Permission denied', (self.root / '.powerpacks/install/install.log').read_text())
        self.assertIn('--source imessage', result['retry_command'])
        self.assert_preserved()

    def test_linkedin_reads_connections_in_chrome_then_reuses_them(self):
        with patch.object(LinkedInConnections, 'run', return_value={
                'status': 'completed', 'message': '2 LinkedIn connections (2 new)'}) as scrape:
            result = SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_called_once()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['linkedin']['message'], '2 LinkedIn connections (2 new)')
        manifest = self.root / '.powerpacks/network-import/discover/linkedin/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'status': 'completed', 'complete': True}))
        with patch.object(LinkedInConnections, 'run') as scrape:
            result = SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_not_called()
        self.assertEqual(result['steps']['linkedin']['message'], 'LinkedIn connections ready')
        manifest.write_text(json.dumps({'status': 'completed', 'complete': False}))
        with patch.object(LinkedInConnections, 'run', return_value={'status': 'completed', 'message': 'more'}) as scrape:
            SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_called_once()
        self.tools.assert_not_called()


if __name__ == '__main__':
    unittest.main()
