"""The review server loads dotenv before constructing its routes."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.review import cli


class ReviewEnvTests(unittest.TestCase):
    def test_startup_loads_dotenv_before_routes_without_overriding_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.env').write_text('TURBOPUFFER_API_KEY=synthetic-key\nKEEP_EXPORTED=file-value\n')

            def routes():
                self.assertEqual(os.environ['TURBOPUFFER_API_KEY'], 'synthetic-key')
                self.assertEqual(os.environ['KEEP_EXPORTED'], 'exported')
                raise StopIteration

            with (
                patch.dict(os.environ, {'KEEP_EXPORTED': 'exported'}, clear=True),
                patch('pathlib.Path.cwd', return_value=root),
                patch.object(cli, 'CANONICAL_DB', root / 'missing'),
                patch.object(cli.urllib.request, 'urlopen', side_effect=OSError),
                patch.object(cli, 'searches_only_handler', side_effect=routes),
            ):
                with self.assertRaises(StopIteration):
                    cli.main(['serve', '--stage', 'searches'])
