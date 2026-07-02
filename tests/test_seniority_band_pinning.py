"""Tests for pinned seniority bands on the search pipelines.

The deep-search JD/profile flow derives canonical seniority bands from a
job description's explicit level language and pins them on every profile
search via --seniority-bands. The pin must validate against the canonical band
vocabulary, REPLACE expansion-derived bands in the final payload, survive role
shortcuts, and actually gate local DuckDB retrieval.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
LOCAL = PRIMITIVES / "local"
TURBOPUFFER = PRIMITIVES / "turbopuffer"
NETWORK_PIPELINE = PRIMITIVES / "search_network_pipeline/search_network_pipeline.py"
for _path in [LIB, SHARED, LOCAL, TURBOPUFFER]:
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import seniority_bands as sb  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Reuse the local pipeline DuckDB fixture (people across seniority bands).
_fixture = load_module("_seniority_pin_fixture", ROOT / "tests/test_local_search_pipeline.py")


class ParsePinnedSeniorityBandsTests(unittest.TestCase):
    def test_normalizes_aliases_case_and_dedupes(self) -> None:
        self.assertEqual(
            sb.parse_pinned_seniority_bands("Senior, staff,c_suite,Vice President,senior"),
            ["senior", "staff", "c-suite", "vice-president"],
        )

    def test_canonical_values_match_indexing_enum(self) -> None:
        enrich = load_module(
            "_enrich_roles_for_bands",
            ROOT / "packs/indexing/primitives/enrich_roles_checkpointed/enrich_roles_checkpointed.py",
        )
        self.assertEqual(sb.CANONICAL_SENIORITY_BANDS, enrich.VALID_SENIORITY_BANDS)

    def test_accepts_legacy_index_values(self) -> None:
        self.assertEqual(sb.parse_pinned_seniority_bands("senior_ic,ic"), ["senior_ic", "ic"])

    def test_rejects_unknown_band_loudly(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            sb.parse_pinned_seniority_bands("senior,principal_engineer")
        self.assertIn("unknown seniority band", str(ctx.exception))

    def test_rejects_empty_value(self) -> None:
        with self.assertRaises(ValueError):
            sb.parse_pinned_seniority_bands(" , ")


class PinPayloadSeniorityBandsTests(unittest.TestCase):
    def test_pin_replaces_expansion_bands_and_marks_pinned(self) -> None:
        payload = {
            "normalized_query": "data engineers",
            "role_search_filters": {"semantic_query": "x" * 90, "seniority_bands": ["mid", "junior"]},
        }
        pinned = sb.pin_payload_seniority_bands(payload, ["senior", "staff"])
        filters = pinned["role_search_filters"]
        self.assertEqual(filters["seniority_bands"], ["senior", "staff"])
        self.assertTrue(filters["seniority_bands_pinned"])
        self.assertTrue(any("replaced expansion bands: mid, junior" in note for note in pinned["notes"]))
        # Original payload is not mutated.
        self.assertEqual(payload["role_search_filters"]["seniority_bands"], ["mid", "junior"])

    def test_founder_shortcut_preserves_pinned_bands(self) -> None:
        search_common = load_module("_search_common_for_bands", SHARED / "search_common.py")
        base = {"role_ids": ["founder"], "seniority_bands": ["director", "vice-president"]}
        # Without the pin, founder shortcut drops seniority bands for recall.
        self.assertNotIn("seniority_bands", search_common.apply_role_shortcuts(dict(base)))
        # With the pin, the JD-level hard constraint survives.
        pinned = search_common.apply_role_shortcuts({**base, "seniority_bands_pinned": True})
        self.assertEqual(pinned["seniority_bands"], ["director", "vice-president"])

    def test_filters_from_role_payload_uses_pinned_bands(self) -> None:
        search_common = load_module("_search_common_for_bands2", SHARED / "search_common.py")
        filters = search_common.filters_from_role_payload(
            {"seniority_bands": ["director", "vice-president"], "seniority_bands_pinned": True}
        )
        # Other suite modules may configure backend mode/env that appends extra
        # clauses (e.g. allowed_operator_ids), so assert on the seniority clause.
        clauses = filters[1] if isinstance(filters, tuple) and filters[0] == "And" else [filters]
        self.assertIn(("seniority_band", "In", ["director", "vice-president"]), clauses)


class PinPayloadCurrentRoleTests(unittest.TestCase):
    def test_pins_is_current_role_and_marks_pinned(self) -> None:
        payload = {
            "normalized_query": "backend engineers",
            "role_search_filters": {"semantic_query": "x" * 90, "seniority_bands": ["senior"]},
        }
        pinned = sb.pin_payload_current_role(payload, True)
        filters = pinned["role_search_filters"]
        self.assertIs(filters["is_current_role"], True)
        self.assertTrue(filters["is_current_role_pinned"])
        self.assertTrue(any("is_current_role pinned" in note for note in pinned["notes"]))
        # Does not disturb existing filters.
        self.assertEqual(filters["seniority_bands"], ["senior"])
        # Original payload is not mutated.
        self.assertNotIn("is_current_role", payload["role_search_filters"])

    def test_composes_with_seniority_pin(self) -> None:
        payload = {"role_search_filters": {"seniority_bands": ["mid"]}}
        out = sb.pin_payload_current_role(sb.pin_payload_seniority_bands(payload, ["senior", "staff"]), True)
        rf = out["role_search_filters"]
        self.assertEqual(rf["seniority_bands"], ["senior", "staff"])
        self.assertTrue(rf["seniority_bands_pinned"])
        self.assertIs(rf["is_current_role"], True)
        self.assertTrue(rf["is_current_role_pinned"])


class PipelineCliTests(unittest.TestCase):
    def test_both_pipelines_accept_seniority_bands_flag(self) -> None:
        network = load_module("_network_pipeline_for_bands", NETWORK_PIPELINE)
        args = network.build_parser().parse_args(
            ["run", "--query", "q", "--payload-json", "p.json", "--seniority-bands", "senior,staff"]
        )
        self.assertEqual(args.seniority_bands, "senior,staff")
        prepare_args = network.build_parser().parse_args(
            ["prepare", "--query", "q", "--seniority-bands", "director"]
        )
        self.assertEqual(prepare_args.seniority_bands, "director")

    def test_both_pipelines_accept_current_role_flag(self) -> None:
        network = load_module("_network_pipeline_for_current", NETWORK_PIPELINE)
        args = network.build_parser().parse_args(
            ["run", "--query", "q", "--payload-json", "p.json", "--current-role"]
        )
        self.assertTrue(args.current_role)
        prepare_args = network.build_parser().parse_args(
            ["prepare", "--query", "q", "--current-role"]
        )
        self.assertTrue(prepare_args.current_role)
        # Defaults off when not passed.
        self.assertFalse(network.build_parser().parse_args(["run", "--state", "s.json"]).current_role)

    def test_network_resume_from_state_rejects_pin(self) -> None:
        network = load_module("_network_pipeline_for_bands_state", NETWORK_PIPELINE)
        args = argparse.Namespace(state="some-state.json", query=None, payload_json=None, seniority_bands="senior")
        with self.assertRaises(network.Failed) as ctx:
            network.init_state(args, Path("unused-ledger.json"), {})
        self.assertIn("--seniority-bands", str(ctx.exception))

    def test_network_resume_from_state_rejects_current_role_pin(self) -> None:
        network = load_module("_network_pipeline_for_current_state", NETWORK_PIPELINE)
        args = argparse.Namespace(state="some-state.json", query=None, payload_json=None, seniority_bands=None, current_role=True)
        with self.assertRaises(network.Failed) as ctx:
            network.init_state(args, Path("unused-ledger.json"), {})
        self.assertIn("--current-role", str(ctx.exception))


class LocalPipelineSeniorityPinningTests(unittest.TestCase):
    PAYLOAD = {
        "original_query": "software engineers",
        "filters": {
            "role_bm25_queries": ["software engineer"],
            "role_ids": [{"id": "software_engineer", "display_value": "Software Engineer"}],
            "role_core_patterns": [{"regex": "software\\s+engineer", "examples": ["Software Engineer"]}],
            # Expansion-derived bands that the pin must override.
            "seniority_bands": [{"id": "mid", "display_value": "Mid"}],
        },
    }

    def run_local(self, tmp: Path, *, bands: str | None) -> subprocess.CompletedProcess:
        db = tmp / "local-search.duckdb"
        if not db.exists():
            _fixture.write_local_search_db(db)
        suffix = bands.replace(",", "-") if bands else "none"
        payload_path = tmp / f"payload-{suffix}.json"
        payload_path.write_text(json.dumps(self.PAYLOAD, indent=2), encoding="utf-8")
        cmd = [
            sys.executable,
            str(NETWORK_PIPELINE),
            "run",
            "--backend",
            "local",
            "--search-only",
            "--db",
            str(db),
            "--ledger",
            str(tmp / f"ledger-{suffix}.json"),
            "--query",
            "software engineers",
            "--payload-json",
            str(payload_path),
            "--limit",
            "0",
            "--top-k",
            "50",
            "--timeout",
            "30",
        ]
        if bands:
            cmd.extend(["--seniority-bands", bands])
        return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=120)

    def csv_person_ids(self, out: dict) -> list[str]:
        with Path(out["artifacts"]["csv"]).open(newline="") as handle:
            return [row["person_id"] for row in CsvIO.dict_reader(handle)]

    def test_pinned_bands_gate_local_retrieval_and_override_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)

            pinned = self.run_local(tmp, bands="senior,staff")
            self.assertEqual(pinned.returncode, 0, pinned.stderr + pinned.stdout)
            pinned_out = json.loads(pinned.stdout)
            state = json.loads(Path(pinned_out["state"]).read_text())
            expand = next(step for step in state["steps"] if step["id"] == "expand_search_request")
            filters = expand["output"]["role_search_filters"]
            self.assertEqual(filters["seniority_bands"], ["senior", "staff"])
            self.assertTrue(filters["seniority_bands_pinned"])
            pinned_ids = self.csv_person_ids(pinned_out)
            # Senior software engineer is retained; mid-band engineer is excluded.
            self.assertIn(_fixture.PERSON_STANFORD, pinned_ids)
            self.assertNotIn(_fixture.PERSON_OTHER, pinned_ids)

            unpinned = self.run_local(tmp, bands=None)
            self.assertEqual(unpinned.returncode, 0, unpinned.stderr + unpinned.stdout)
            unpinned_out = json.loads(unpinned.stdout)
            unpinned_state = json.loads(Path(unpinned_out["state"]).read_text())
            unpinned_expand = next(step for step in unpinned_state["steps"] if step["id"] == "expand_search_request")
            # Without the flag, expansion bands pass through untouched.
            self.assertEqual(unpinned_expand["output"]["role_search_filters"]["seniority_bands"], ["mid"])
            unpinned_ids = self.csv_person_ids(unpinned_out)
            self.assertIn(_fixture.PERSON_OTHER, unpinned_ids)
            self.assertNotIn(_fixture.PERSON_STANFORD, unpinned_ids)

    def test_unknown_band_fails_loudly_before_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            proc = self.run_local(tmp, bands="principal_engineer")
            self.assertNotEqual(proc.returncode, 0)
            out = json.loads(proc.stdout)
            self.assertEqual(out["status"], "failed")
            self.assertIn("unknown seniority band", out["error"])


if __name__ == "__main__":
    unittest.main()
