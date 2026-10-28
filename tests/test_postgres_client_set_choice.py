"""The Search-in picker's set (.env) wins over a stale POWERPACKS_DEFAULT_SET_ID in the process env."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packs/search/primitives/lib"))

import postgres_client  # noqa: E402


class PickedSetWins(unittest.TestCase):
    def test_env_file_beats_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("DATABASE_URL=postgresql://db\nPOWERPACKS_DEFAULT_SET_ID=picked-set\n")
            old = os.environ.get("POWERPACKS_DEFAULT_SET_ID")
            os.environ["POWERPACKS_DEFAULT_SET_ID"] = "stale-set"
            try:
                self.assertEqual(postgres_client.fetch_default_set_id(env_file),
                                 {"set_id": "picked-set", "source": "env_file"})
            finally:
                if old is None:
                    os.environ.pop("POWERPACKS_DEFAULT_SET_ID", None)
                else:
                    os.environ["POWERPACKS_DEFAULT_SET_ID"] = old

    def test_process_env_still_counts_without_a_file_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("DATABASE_URL=postgresql://db\n")
            old = os.environ.get("POWERPACKS_DEFAULT_SET_ID")
            os.environ["POWERPACKS_DEFAULT_SET_ID"] = "from-env"
            try:
                self.assertEqual(postgres_client.fetch_default_set_id(env_file),
                                 {"set_id": "from-env", "source": "env"})
            finally:
                if old is None:
                    os.environ.pop("POWERPACKS_DEFAULT_SET_ID", None)
                else:
                    os.environ["POWERPACKS_DEFAULT_SET_ID"] = old


if __name__ == "__main__":
    unittest.main()
