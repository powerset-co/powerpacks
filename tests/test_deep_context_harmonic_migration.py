import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.common.legacy import _scrub_harmonic_profiles
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents


class HarmonicMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context' / 'deep-context.sqlite')
        self.cache = self.root / 'network-import' / 'profile_cache_v2'
        self.cache.mkdir(parents=True)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('data_migration_version','1')")
            for name in ('jordan', 'casey'):
                conn.execute('INSERT INTO parents(parent_id,public_identifier) VALUES (?,?)', (name, name))
            for key, parent in [('jordan', 'jordan'), ('research:jordan', 'jordan'), ('casey', 'casey')]:
                conn.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source,machine_judgment,machine_approved,judgment_payload_json) VALUES (?,?,?,'pub','legacy-migration','confirmed','auto','{}')", (key, parent, parent))
            conn.execute("UPDATE links SET decision_action='verify',decision_approved='yes',decision_source='deep-context-review' WHERE row_key='jordan'")
        self.harmonic = self.profile('jordan', 'harmonic_enriched_example.csv')
        self.gateway = self.profile('casey', None)

    def profile(self, name, source_file):
        payload = {'public_identifier': name, 'normalized_profile': {'success': True, 'public_identifier': name, 'experiences': [{'title': 'Engineer', 'company_name': 'Example'}]}, 'raw_response': {}}
        if source_file:
            payload['source'] = {'provider': 'existing_export_bootstrap', 'source_file': '/old/' + source_file}
        data = json.dumps(payload)
        path = self.cache / (name + '.json')
        path.write_text(data)
        with self.db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO artifacts(artifact_key,kind,parent_id,candidate_key,path,content_fingerprint,status,payload_json) VALUES (?,'profile',?,?,?,'fixture','projected',?)", ('profile:' + name, name, name, str(path), data))
        return path

    def test_removes_harmonic_and_dependent_machine_judgments_but_keeps_humans(self):
        before = tuple(self.db.query("SELECT decision_action,decision_approved,decision_source FROM links WHERE row_key='jordan'")[0])
        self.assertEqual(_scrub_harmonic_profiles(self.db), 1)
        self.assertEqual([r['artifact_key'] for r in self.db.query('SELECT artifact_key FROM artifacts')], ['profile:casey'])
        self.assertIsNone(self.db.query("SELECT machine_judgment FROM links WHERE row_key='research:jordan'")[0][0])
        self.assertEqual(before, tuple(self.db.query("SELECT decision_action,decision_approved,decision_source FROM links WHERE row_key='jordan'")[0]))
        self.assertEqual(self.db.query("SELECT machine_judgment FROM links WHERE row_key='casey'")[0][0], 'confirmed')
        self.assertFalse(self.harmonic.exists())
        self.assertTrue(self.harmonic.with_suffix('.json.bkup-harmonic').exists())
        self.assertTrue(self.gateway.exists())
        backup = self.db.db_path.with_suffix('.sqlite.bkup-harmonic')
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM artifacts').fetchone()[0], 2)
        self.assertEqual(_scrub_harmonic_profiles(self.db), 0)
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])

    def test_ensure_parents_runs_migration_even_without_new_imports(self):
        EnsureParents(db=self.db, people_csv=self.root / 'missing.csv').run()
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '2')
        self.assertFalse(self.harmonic.exists())

    def test_disk_only_harmonic_is_archived_and_other_bootstrap_is_preserved(self):
        other = self.profile('casey', 'other_export.csv')
        orphan = self.cache / 'orphan.json'
        orphan.write_text(self.harmonic.read_text())
        _scrub_harmonic_profiles(self.db)
        self.assertFalse(orphan.exists())
        self.assertTrue(orphan.with_suffix('.json.bkup-harmonic').exists())
        self.assertTrue(other.exists())

    def test_backup_failure_leaves_database_and_cache_untouched(self):
        from unittest.mock import patch
        with patch('packs.ingestion.primitives.deep_context.db.store.DbMaintenance.backup_to', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                _scrub_harmonic_profiles(self.db)
        self.assertTrue(self.harmonic.exists())
        self.assertEqual(self.db.query('SELECT count(*) FROM artifacts')[0][0], 2)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '1')

    def test_interrupted_database_write_reruns_after_cache_archive(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        original = self.db.transaction

        @contextmanager
        def interrupted_write():
            with original() as conn:
                yield conn
                if conn.total_changes:
                    raise OSError('interrupted before commit')

        with patch.object(self.db, 'transaction', interrupted_write):
            with self.assertRaises(OSError):
                _scrub_harmonic_profiles(self.db)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '1')
        self.assertEqual(self.db.query('SELECT count(*) FROM artifacts')[0][0], 2)
        self.assertTrue(self.harmonic.with_suffix('.json.bkup-harmonic').is_file())
        self.assertFalse(self.harmonic.exists())
        self.assertEqual(_scrub_harmonic_profiles(self.db), 1)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '2')
        self.assertTrue(self.gateway.is_file())
