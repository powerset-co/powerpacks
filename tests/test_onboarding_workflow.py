"""Local onboarding reuses source outputs, preserves gates, and stops on errors."""
from __future__ import annotations

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
from packs.ingestion.primitives.setup.automations import msgvault_home
from packs.ingestion.primitives.setup.automations.browser_flows import BrowserSetup, TestUsers
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery
from packs.ingestion.primitives.discover.messages.extract_imessage import IMessageExtractor
from packs.ingestion.primitives.discover.messages.extract_whatsapp import WhatsAppExtractor
from packs.ingestion.primitives.discover.messages.wacli import auth
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.imports import common as import_common
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.primitives.setup.automations import accounts
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallStep
from packs.powerset.primitives.install.tools import ImportTools
from packs.powerset.primitives.install.workflow import SourceOnboarding
from packs.ingestion.schemas.message_contacts import CSV_HEADERS
from packs.shared.csv_io import CsvIO


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
        InstallStatus(self.root).write('install.skills_ready', pid=os.getpid())
        self.tools = patch.object(ImportTools, 'run', return_value={'status': 'ok'}).start()
        self.linkedin_run = LinkedInConnections.run
        self.linkedin_login = LinkedInConnections.login
        patch.object(LinkedInConnections, 'run', return_value={
            'status': 'completed', 'outcome': 'read', 'read': 1, 'total': 1, 'connections': 1, 'added': 1}).start()
        patch.object(LinkedInConnections, 'login', return_value={'status': 'completed'}).start()
        # Never reach real Google, gcloud or ~/.msgvault from a test.
        self.test_users = patch.object(TestUsers, 'run', autospec=True, return_value={'status': 'ok'}).start()
        patch.object(msgvault_home, 'load_setup_state', return_value=SimpleNamespace(test_users=())).start()
        patch.object(auth, 'auth_status', return_value=SimpleNamespace(authenticated=False)).start()
        self.history = patch.object(auth, 'wait_for_history').start()
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

    def test_no_source_defaults_to_linkedin_gmail_messages_and_whatsapp_with_a_year_of_history(self):
        InstallStatus(self.root).write('tools.ready', step=InstallStep.NETWORK, pid=os.getpid(), account_email='casey@example.com')
        flow = SourceOnboarding(self.root, sources=())
        sources = flow.plan.index('sources')
        # Every login comes before every sync, the order the run takes them.
        self.assertEqual(flow.plan[sources + 1:flow.plan.index('deep_context')], [
            'linkedin_login', 'gmail_tools', 'gmail_login', 'imessage_access', 'whatsapp_tools', 'whatsapp_login',
            'linkedin', 'gmail_sync', 'gmail_import', 'imessage_import', 'whatsapp_sync', 'whatsapp_import'])
        self.assertIn('--source linkedin --source gmail --source imessage --source whatsapp', flow.retry_command)
        self.assertNotIn('--gmail-email', flow.retry_command)
        self.assertIn((date.today() - timedelta(days=365)).isoformat(), flow.retry_command)
        self.tools.assert_not_called()

    def test_gmail_asks_which_accounts_only_without_a_powerset_login(self):
        with patch.object(accounts, 'status_payload', return_value={'accounts': []}), \
             patch.object(LinkedInConnections, 'login') as linkedin:
            result = SourceOnboarding(self.root, sources=()).run()
        self.assertEqual((result['step'], result['status'], result['installer_pid']), ('gmail_login', 'waiting', 0))
        self.assertIn('Which Gmail accounts', result['message'])
        self.tools.assert_not_called()
        linkedin.assert_not_called()

    def test_every_missing_mailbox_becomes_a_test_user_before_consent(self):
        added = self.test_users
        health = {'status': 'needs_user_action', 'accounts': [
            {'email': 'casey@example.com', 'status': 'authenticated'},
            {'email': 'jordan@example.com', 'status': 'missing_token'}]}
        with patch.object(msgvault_home, 'load_setup_state', return_value=SimpleNamespace(test_users=('casey@example.com',))), \
             patch.object(accounts, 'check_accounts_payload', side_effect=[health, {'status': 'ok'}]), \
             patch.object(accounts, 'add_account', return_value={'status': 'ok'}), \
             patch.object(GmailDiscovery, 'run', return_value=payload(status='needs_user_action')):
            SourceOnboarding(self.root, sources=('gmail',),
                             gmail_emails=('casey@example.com', 'jordan@example.com')).run()
        self.assertEqual((added.call_args.args[0].test_users, added.call_args.args[0].login_email),
                         (('jordan@example.com',), 'casey@example.com'))

    def test_default_sources_collect_every_login_before_any_sync(self):
        InstallStatus(self.root).write('tools.ready', step=InstallStep.NETWORK, pid=os.getpid(), account_email='casey@example.com')
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=(), gmail_emails=('casey@example.com',)).run()
        self.assertEqual(result['step'], 'imessage_access')
        self.assertNotIn('gmail_import', result['steps'])
        health.assert_called_once()
        self.assertEqual(health.call_args.args[1], ['casey@example.com'])
        sync.assert_not_called()

    def test_gmail_reuses_every_mailbox_msgvault_already_holds(self):
        for configured, expected in (([{'identifier': 'casey@example.com'}], ('casey@example.com',)),
                                     ([{'email': 'other@example.com'}, {'email': 'casey@example.com'}],
                                      ('casey@example.com', 'other@example.com'))):
            with self.subTest(configured=configured), \
                 patch.object(accounts, 'status_payload', return_value={'accounts': configured}):
                flow = SourceOnboarding(self.root, sources=())
            self.assertEqual(flow.gmail_emails, expected)

    def test_default_rerun_preserves_saved_source_account_history_store_and_skips(self):
        store = self.root / 'selected store'
        original = SourceOnboarding(self.root, sources=('gmail', 'whatsapp'),
            gmail_emails=('casey@example.com', 'jordan@example.com'), sync_after='2025-10-03',
            wacli_store=store, refresh=True, skip_sources=('whatsapp',))
        original._write('gmail.connect.waiting')
        repeated = SourceOnboarding(self.root, sources=())
        self.assertEqual(shlex.split(repeated.retry_command),
                         [arg for arg in shlex.split(original.retry_command) if arg != '--refresh'])
        self.assertFalse(repeated.refresh)
        override = SourceOnboarding(self.root, sources=('imessage',))
        self.assertEqual(tuple(override.sources), ('imessage',))
        self.assertEqual(override.gmail_emails, ('casey@example.com', 'jordan@example.com'))
        self.assertEqual(override.sync_after, '2025-10-03')
        self.assertEqual(override.wacli_store, store)
        self.assertEqual(override.skip_sources, ())

    def test_explicit_source_account_and_history_override_saved_choices(self):
        flow = SourceOnboarding(self.root, sources=('gmail',),
            gmail_emails=('casey@example.com',), sync_after='2023-01-01')
        flow._write('gmail.connect.waiting')
        override = SourceOnboarding(self.root, sources=('imessage',), skip_sources=('imessage',),
            gmail_emails=('jordan@example.com',), sync_after='2025-10-03')
        self.assertEqual(tuple(override.sources), ('imessage',))
        self.assertEqual(override.gmail_emails, ('jordan@example.com',))
        self.assertEqual(override.sync_after, '2025-10-03')
        self.assertEqual(override.run()['step'], 'ready')

    def test_refresh_is_used_once_then_normal_rerun_reuses_gmail_and_saved_window(self):
        current = import_common.ImportManifest.from_payload('gmail', {
            'status': 'completed', 'input': {'accounts': [{'account_email': 'casey@example.com'}]}})
        with patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(import_common, 'import_manifest_current', return_value=current), \
             patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')) as discover, \
             patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            first = SourceOnboarding(self.root, sources=('gmail',), gmail_emails=('casey@example.com',),
                                     sync_after='2023-01-01', refresh=True).run()
            repeated = SourceOnboarding(self.root, sources=())
            second = repeated.run()
        self.assertEqual((first['step'], second['step']), ('deep_context', 'deep_context'))
        self.assertEqual(repeated.sync_after, '2023-01-01')
        self.assertEqual(health.call_count, 2)
        self.assertEqual(discover.call_count, 1)
        self.assertNotIn('--refresh', second['retry_command'])
        self.test_users.assert_not_called()

    def test_powerset_gmail_choice_survives_other_mailboxes_added_before_rerun(self):
        InstallStatus(self.root).write('tools.ready', step=InstallStep.NETWORK, pid=os.getpid(),
                                       account_email='casey@example.com')
        with patch.object(SourceOnboarding, '_gmail_connect', return_value=False):
            first = SourceOnboarding(self.root, sources=('gmail',))
            first.run()
        with patch.object(accounts, 'status_payload', return_value={'accounts': [
                {'email': 'other@example.com'}, {'email': 'casey@example.com'}]}):
            repeated = SourceOnboarding(self.root, sources=())
        self.assertEqual(repeated.gmail_emails, ('casey@example.com',))
        self.assertEqual(repeated.sync_after, first.sync_after)

    def test_skip_finishes_without_calling_source_tools(self):
        result = SourceOnboarding(self.root, sources=('gmail',), skip_sources=('gmail',)).run()
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
                 patch.object(SourceOnboarding, '_gmail_connect') as gmail, \
                 patch.object(SourceOnboarding, '_messages_sync') as messages:
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
        InstallStatus(self.root).write('gmail.import.current', pid=os.getpid(), plan=['skills', 'sources', 'gmail_import'])
        result = SourceOnboarding(self.root, sources=('gmail', 'linkedin'), skip_sources=('gmail',)).run()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['gmail_import']['status'], 'skipped')
        self.assertEqual({path: path.read_bytes() for path in imported.parent.iterdir()}, saved)
        self.assert_preserved()

    def test_all_selected_sources_skipped_finish_without_processing(self):
        sources = ('gmail', 'imessage', 'whatsapp', 'linkedin')
        with patch.object(SourceOnboarding, '_gmail_connect') as gmail, \
             patch.object(SourceOnboarding, '_messages_sync') as messages:
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
        result = SourceOnboarding(self.root, sources=('gmail', 'linkedin'), skip_sources=('linkedin',),
                                  gmail_emails=('casey@example.com',)).run()
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
        result = SourceOnboarding(self.root, sources=('gmail', 'whatsapp'),
                                  gmail_emails=('casey@example.com', 'jordan@example.com'),
                                  sync_after='2023-01-01', wacli_store=store, refresh=True,
                                  skip_sources=('gmail', 'whatsapp')).run()
        self.assertEqual(result['step'], 'ready')
        self.assertEqual(shlex.split(result['retry_command'])[1:], command[1:])

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
            status.write('tools.ready', step=step, pid=os.getpid(),
                         plan=['skills', 'sources', 'deep_context', 'index', 'validate', 'ready'])
        result = SourceOnboarding(self.root, sources=('imessage',)).run()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['step'], 'imessage_access')
        for step in ('deep_context', 'index', 'validate', 'ready'):
            self.assertNotIn(step, result['steps'])
        self.assertEqual(result['plan'][-5:], ['deep_context', 'enrich', 'index', 'validate', 'ready'])
        self.assert_preserved()

    def test_dead_installer_does_not_block_recovery(self):
        process = subprocess.Popen([sys.executable, '-c', 'pass'])
        process.wait(timeout=5)
        InstallStatus(self.root).write('gmail.syncing', pid=process.pid)
        result = SourceOnboarding(self.root, sources=('imessage',), skip_sources=('imessage',)).run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['message'], 'Powerpacks is installed')

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
        InstallStatus(self.root).write('gmail.import.current', pid=os.getpid(), plan=['skills', 'sources', 'gmail_import', 'deep_context'])
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

    def test_pending_linkedin_login_stops_before_the_next_login_and_any_sync(self):
        with patch.object(IMessageExtractor, 'check') as access, \
             patch.object(MessagesDiscovery, 'run') as sync, \
             patch.object(LinkedInConnections, 'login', return_value={
                 'status': 'needs_user_action', 'message': 'Log in to LinkedIn in the Chrome window Powerpacks opened.'}):
            result = SourceOnboarding(self.root, sources=('linkedin', 'imessage')).run()
        self.assertEqual((result['step'], result['status']), ('linkedin_login', 'waiting'))
        self.assertEqual(result['message'], 'Log in to LinkedIn in the Chrome window Powerpacks opened.')
        access.assert_not_called()
        sync.assert_not_called()

    def test_processing_reports_saved_source_counts_without_changing_hosted_network_count(self):
        import_common.write_manifest('gmail', {'status': 'completed', 'stats': {'people': 12}})
        import_common.write_manifest('messages', {'status': 'completed', 'stats': {'people': 8}})
        InstallStatus(self.root).write('tools.ready', step=InstallStep.NETWORK, pid=os.getpid(), person_count=91)
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

    def test_gmail_defaults_to_the_powerset_login_without_asking(self):
        InstallStatus(self.root).write('tools.ready', step=InstallStep.NETWORK, pid=os.getpid(), account_email='casey@example.com')
        seen = []
        with patch.object(SourceOnboarding, '_gmail_connect', autospec=True,
                          side_effect=lambda flow: seen.append(flow.gmail_emails) or False):
            result = SourceOnboarding(self.root, sources=('gmail',), sync_after='2023-01-01').run()
        self.assertEqual(seen, [('casey@example.com',)])
        self.assertNotEqual(result['event'], 'gmail.which_accounts')

    def test_fresh_gmail_creates_the_oauth_app_in_process_before_any_account_sync(self):
        with patch.object(accounts, 'status_payload', return_value={
                'config': {'oauth_configured': False}, 'database': {'exists': False}}), \
             patch.object(accounts, 'check_accounts_payload', return_value={'status': 'ok'}) as health, \
             patch.object(BrowserSetup, 'run', autospec=True,
                          return_value={'status': 'needs_user_action', 'message': 'Sign in to Google'}) as create, \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                                      gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        self.assertEqual((result['status'], result['step']), ('waiting', 'gmail_login'))
        self.assertEqual(create.call_args.args[0].email, 'casey@example.com')
        # The user is not handed Google Cloud's manual steps; the agent reads the details.
        self.assertEqual(result['message'], "Gmail setup stopped in Google Cloud. I'm looking into it.")
        self.assertEqual(result['action']['details']['message'], 'Sign in to Google')
        health.assert_not_called()
        sync.assert_not_called()

    def test_each_google_cloud_stage_shows_as_it_happens(self):
        seen = []
        def create(setup):
            for stage in ('naming', 'permissions', 'client'):
                setup.on_stage(stage)
                seen.append(InstallStatus(self.root).read()['message'])
            return {'status': 'needs_user_action', 'message': 'test stop'}
        with patch.object(accounts, 'status_payload', return_value={
                'config': {'oauth_configured': False}, 'database': {'exists': False}}), \
             patch.object(BrowserSetup, 'run', autospec=True, side_effect=create):
            SourceOnboarding(self.root, sources=('gmail',), gmail_emails=('casey@example.com',)).run()
        self.assertEqual(seen, ['Creating your Gmail app in Google Cloud: naming the app',
                                'Creating your Gmail app in Google Cloud: adding Gmail read access',
                                'Creating your Gmail app in Google Cloud: creating its sign-in key'])

    def test_linkedin_read_shows_its_count_as_it_scrolls(self):
        seen = []
        def read(*, on_count):
            on_count(40, 298)
            seen.append(InstallStatus(self.root).read()['message'])
            return {'status': 'completed', 'outcome': 'read', 'read': 298, 'total': 298, 'connections': 298, 'added': 298}
        with patch.object(LinkedInConnections, 'run', side_effect=read):
            SourceOnboarding(self.root, sources=('linkedin',)).run()
        self.assertEqual(seen, ['Reading your LinkedIn connections: 40 of 298'])

    def test_gmail_missing_or_expired_authorization_continues_in_the_same_run(self):
        for verdict in ('missing_token', 'reauthorization_required'):
            health = {'status': 'needs_user_action', 'accounts': [
                {'email': 'casey@example.com', 'status': verdict},
                {'email': 'other@example.com', 'status': 'healthy'}]}

            def authorize(home, email, app_name, *, force, on_progress):
                on_progress({"stage": "sign_in"})
                waiting = InstallStatus(self.root).read()
                self.assertEqual(waiting['step'], 'gmail_login')
                self.assertEqual(waiting['status'], 'waiting')
                self.assertEqual(waiting['installer_pid'], os.getpid())
                self.assertEqual(waiting['action']['kind'], 'gmail')
                self.assertEqual(waiting['action']['details']['email'], 'casey@example.com')
                self.assertEqual(email, "casey@example.com")
                self.assertEqual(force, verdict == "reauthorization_required")
                return {"status": "ok"}

            with self.subTest(verdict=verdict), \
                 patch.object(accounts, 'check_accounts_payload', side_effect=[health, {'status': 'ok'}]) as check, \
                 patch.object(accounts.oauth_browser, 'authorize_account', side_effect=authorize) as login, \
                 patch.object(import_common, 'import_manifest_current', return_value=None), \
                 patch.object(GmailDiscovery, '__init__', return_value=None) as init, \
                 patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')) as sync, \
                 patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
                result = SourceOnboarding(self.root, sources=('gmail',),
                    gmail_emails=('casey@example.com', 'other@example.com'), sync_after='2023-01-01').run()
            self.assertEqual(login.call_args.args, (Path('~/.msgvault').expanduser(), 'casey@example.com', ''))
            self.assertEqual(login.call_args.kwargs['force'], verdict == 'reauthorization_required')
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
             patch.object(accounts.oauth_browser, 'authorize_account', return_value={'status': 'ok'}) as login, \
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
             patch.object(accounts.oauth_browser, 'authorize_account', return_value={'status': 'error', 'message': 'OAuth app rejected'}) as login, \
             patch.object(GmailDiscovery, 'run') as sync:
            result = SourceOnboarding(self.root, sources=('gmail',),
                gmail_emails=('casey@example.com',), sync_after='2023-01-01').run()
        login.assert_called_once()
        check.assert_called_once()
        sync.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['step'], 'gmail_login')
        self.assertEqual(result['event'], 'gmail.connect.failed')
        self.assertEqual(result['action']['details']['message'], 'OAuth app rejected')
        self.assert_preserved()

    def test_gmail_callback_for_other_account_does_not_start_sync(self):
        local = {'msgvault': {'installed': True}, 'config': {'oauth_configured': True},
                 'database': {'exists': True}, 'accounts': []}
        other = {**local, 'accounts': [{'email': 'other@example.com'}]}
        with patch.object(accounts, 'status_payload', side_effect=[local, local, other]), \
             patch.object(accounts.oauth_browser, 'authorize_account', return_value={'status': 'ok'}) as login, \
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

    def test_gmail_consent_human_handoff_stops_before_sync_and_rerun_resumes(self):
        health = {'status': 'needs_user_action', 'accounts': [
            {'email': 'casey@example.com', 'status': 'reauthorization_required'}]}
        with patch.object(accounts, 'check_accounts_payload', side_effect=[health, health, {'status': 'ok'}]), \
             patch.object(accounts.oauth_browser, 'authorize_account', side_effect=[
                 {'status': 'needs_user_action', 'message': 'Complete sign-in in Chrome.'}, {'status': 'ok'}]) as login, \
             patch.object(import_common, 'import_manifest_current', return_value=None), \
             patch.object(GmailDiscovery, '__init__', return_value=None), \
             patch.object(GmailDiscovery, 'run', return_value=payload(status='completed')), \
             patch.object(GmailImport, 'run', lambda instance: setattr(instance, 'written', {'status': 'completed'})):
            first = SourceOnboarding(self.root, sources=('gmail',), gmail_emails=('casey@example.com',)).run()
            self.assertEqual(first['status'], 'waiting')
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

    def test_whatsapp_sync_waits_for_the_history_download_before_reading_the_store(self):
        order = []
        self.history.side_effect = lambda store, on_count: order.append('history')
        with patch.object(auth, 'auth_report', return_value={'status': 'linked'}), \
             patch.object(MessagesDiscovery, 'run', side_effect=lambda: order.append('discover') or SimpleNamespace(
                 to_payload=lambda: payload(status='failed', error='test stop'))):
            result = SourceOnboarding(self.root, sources=('whatsapp',)).run()
        self.assertEqual(order, ['history', 'discover'])
        self.assertEqual(result['steps']['whatsapp_sync']['status'], 'failed')

    def test_already_linked_whatsapp_completes_its_login(self):
        with patch.object(auth, 'auth_status', return_value=SimpleNamespace(authenticated=True)), \
             patch.object(auth, 'auth_report', return_value={'status': 'linked'}), \
             patch.object(MessagesDiscovery, 'run', side_effect=lambda: SimpleNamespace(
                 to_payload=lambda: payload(status='failed', error='test stop'))):
            result = SourceOnboarding(self.root, sources=('whatsapp',)).run()
        self.assertEqual(result['steps']['whatsapp_login'], {'status': 'completed', 'message': 'WhatsApp is linked'})

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
        self.assertEqual(result['action']['details']['message'], 'Allow linked devices on your phone')
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

    def test_real_message_sources_import_once_reuse_twice_and_refresh_explicitly(self):
        def extract_imessage(extractor, *, output_csv, output_jsonl, manifest):
            CsvIO.write_dict_rows(output_csv, CSV_HEADERS, [{
                'phone': '+15550100123', 'name': 'Jordan Bravo', 'source': 'imessage',
                'message_count': '3', 'imessage_message_count': '3'}])
            return {'status': 'completed'}

        def extract_whatsapp(extractor, *, output_csv, manifest, **kwargs):
            CsvIO.write_dict_rows(output_csv, CSV_HEADERS, [{
                'phone': '+15550100456', 'name': 'Casey Delta', 'source': 'whatsapp',
                'message_count': '4', 'whatsapp_message_count': '4'}])
            manifest.write_text(json.dumps({'status': 'completed', 'store': str(extractor.store)}))
            return {'status': 'completed'}

        with patch.object(IMessageExtractor, 'check', return_value={'status': 'ok'}) as check, \
             patch.object(IMessageExtractor, 'extract', autospec=True, side_effect=extract_imessage) as imessage, \
             patch.object(auth, 'auth_status', return_value=SimpleNamespace(authenticated=True)), \
             patch.object(auth, 'auth_report', return_value={'status': 'linked'}) as link, \
             patch.object(WhatsAppExtractor, 'run', autospec=True, side_effect=extract_whatsapp) as whatsapp:
            first = SourceOnboarding(self.root, sources=('imessage', 'whatsapp')).run()
            self.assertEqual(first['step'], 'deep_context', first.get('action'))
            artifacts = [self.data, self.data.with_name('manifest.json'),
                         self.root / '.powerpacks/messages/contacts.csv']
            original = [(path.read_bytes(), path.stat().st_mtime_ns) for path in artifacts]
            for _ in range(2):
                repeated = SourceOnboarding(self.root, sources=()).run()
                self.assertEqual(repeated['step'], 'deep_context')
                self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in artifacts], original)
            self.assertEqual((imessage.call_count, whatsapp.call_count), (1, 1))
            self.assertEqual(link.call_count, 3)
            self.assertEqual(check.call_count, 4)  # Login checks plus the initial extractor check.
            refreshed = SourceOnboarding(self.root, sources=(), refresh=True).run()
            self.assertEqual(refreshed['step'], 'deep_context')
            self.assertEqual((imessage.call_count, whatsapp.call_count), (2, 2))
            SourceOnboarding(self.root, sources=()).run()
            self.assertEqual((imessage.call_count, whatsapp.call_count), (2, 2))

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
                'status': 'completed', 'outcome': 'partial', 'read': 2, 'total': 3, 'connections': 2, 'added': 2}) as scrape:
            result = SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_called_once()
        self.assertEqual(result['step'], 'deep_context')
        self.assertEqual(result['steps']['linkedin']['message'], '2 LinkedIn connections (2 new); LinkedIn shows 3')
        manifest = self.root / '.powerpacks/network-import/discover/linkedin/connections.json'
        manifest.parent.mkdir(parents=True)
        manifest.with_name('Connections.csv').write_text('First Name,Last Name,URL\nJordan,Bravo,https://www.linkedin.com/in/jordan-bravo\n')
        manifest.write_text(json.dumps({'status': 'completed', 'complete': True}))
        with patch.object(LinkedInConnections, 'run') as scrape:
            result = SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_not_called()
        self.assertEqual(result['steps']['linkedin']['message'], 'LinkedIn connections ready')
        manifest.write_text(json.dumps({'status': 'completed', 'complete': False}))
        with patch.object(LinkedInConnections, 'run', return_value={
                'status': 'completed', 'outcome': 'read', 'read': 3, 'total': 3, 'connections': 3, 'added': 1}) as scrape:
            SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_called_once()
        self.assertEqual(self.tools.call_count, 2)

    def test_linkedin_completed_record_without_csv_does_not_skip_read(self):
        manifest = self.root / '.powerpacks/network-import/discover/linkedin/connections.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'status': 'completed', 'complete': True}))
        with patch.object(LinkedInConnections, 'run', return_value={
                'status': 'completed', 'outcome': 'read', 'connections': 1, 'added': 1}) as scrape:
            SourceOnboarding(self.root, sources=('linkedin',)).run()
        scrape.assert_called_once()

    def test_real_linkedin_import_reuses_csv_on_normal_rerun_and_refreshes_once(self):
        def browser(connections, *args, **kwargs):
            if '--login-only' in args:
                return {'status': 'ok'}
            return {'status': 'ok', 'total': 1, 'stopped': 'end', 'loads': 1, 'owner_slug': 'casey',
                    'connections': [{'name': 'Jordan Bravo', 'slug': 'jordan-bravo',
                                     'headline': 'Engineer', 'connected_on': '2026-10-01'}]}

        with patch.object(LinkedInConnections, 'run', self.linkedin_run), \
             patch.object(LinkedInConnections, 'login', self.linkedin_login), \
             patch.object(LinkedInConnections, '_browser', autospec=True, side_effect=browser) as external:
            first = SourceOnboarding(self.root, sources=('linkedin',)).run()
            csv = self.root / '.powerpacks/network-import/discover/linkedin/Connections.csv'
            original = (csv.read_bytes(), csv.stat().st_mtime_ns)
            for _ in range(2):
                repeated = SourceOnboarding(self.root, sources=()).run()
                self.assertEqual(repeated['step'], 'deep_context')
                self.assertEqual((csv.read_bytes(), csv.stat().st_mtime_ns), original)
            self.assertEqual(first['step'], 'deep_context')
            self.assertEqual(sum('--login-only' not in call.args for call in external.call_args_list), 1)
            SourceOnboarding(self.root, sources=(), refresh=True).run()
            SourceOnboarding(self.root, sources=()).run()
            self.assertEqual(sum('--login-only' not in call.args for call in external.call_args_list), 2)

    def test_linkedin_completed_import_still_checks_expired_browser_login(self):
        manifest = self.root / '.powerpacks/network-import/discover/linkedin/connections.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'status': 'completed', 'complete': True}))
        manifest.with_name('Connections.csv').write_text('First Name,Last Name,URL\nJordan,Bravo,https://www.linkedin.com/in/jordan-bravo\n')
        with patch.object(LinkedInConnections, 'login', return_value={'status': 'needs_user_action'}) as login, \
             patch.object(LinkedInConnections, 'run') as scrape:
            result = SourceOnboarding(self.root, sources=('linkedin',)).run()
        login.assert_called_once()
        scrape.assert_not_called()
        self.assertEqual((result['step'], result['status']), ('linkedin_login', 'waiting'))


if __name__ == '__main__':
    unittest.main()
