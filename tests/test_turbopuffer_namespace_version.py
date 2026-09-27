"""TurboPuffer namespace family selection."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packs/search/primitives/lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packs/search/primitives/shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packs/search/primitives/turbopuffer"))

import turbopuffer_search_backend as backend  # noqa: E402


class NamespaceVersionTests(unittest.TestCase):
    def test_default_stays_v1(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(backend.namespace_name("people"), "aleph_people_v1")

    def test_v3_selects_complete_family(self):
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "v3"}, clear=True):
            self.assertEqual(backend.namespace_name("people"), "aleph_people_v3")
            self.assertEqual(backend.namespace_name("schools"), "aleph_education_v3")
            self.assertEqual(backend.namespace_name("education"), "aleph_people_education_v3")

    def test_staging_suffix_follows_version(self):
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "v3", "ALEPH_ENV": "staging"}, clear=True):
            self.assertEqual(backend.namespace_name("summaries"), "aleph_summaries_v3_dev")

    def test_explicit_namespace_wins(self):
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "v3", "POWERPACKS_TURBOPUFFER_PEOPLE_NAMESPACE": "test_people"}, clear=True):
            self.assertEqual(backend.namespace_name("people"), "test_people")

    def test_invalid_version_fails(self):
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "three"}, clear=True):
            with self.assertRaises(ValueError):
                backend.namespace_name("people")


if __name__ == "__main__":
    unittest.main()
