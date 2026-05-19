import csv
import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/package-operator-bootstrap.py"


def load_module():
    spec = importlib.util.spec_from_file_location("package_operator_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class PackageOperatorBootstrapTests(unittest.TestCase):
    def test_packages_import_enrich_processing_without_raw_sync_data(self) -> None:
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mapping = tmp / "operator_mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "_users": {"17d602f7-f073-40b4-97a1-dba00c574442": "patrick"},
                        "17d602f7-f073-40b4-97a1-dba00c574442": ["f48f06f0-db0c-4743-b588-da475a62e49b"],
                    }
                ),
                encoding="utf-8",
            )
            access = tmp / "operator-access.csv"
            with access.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["operator_id", "person_id", "operator_email"])
                writer.writeheader()
                writer.writerow(
                    {
                        "operator_id": "17d602f7-f073-40b4-97a1-dba00c574442",
                        "person_id": "person-1",
                        "operator_email": "",
                    }
                )
            source_dir = tmp / "source"
            source_dir.mkdir()
            uploads: list[list[str]] = []

            def fake_run(cmd, cwd=ROOT):
                if cmd[:2] == [mod.sys.executable, "packs/ingestion/primitives/bootstrap_network_from_exports/bootstrap_network_from_exports.py"]:
                    out = Path(cmd[cmd.index("--output-root") + 1])
                    operator_dir = out / "operators/patrick"
                    (operator_dir / "inputs").mkdir(parents=True)
                    (operator_dir / "outputs").mkdir(parents=True)
                    (operator_dir / "resolution").mkdir(parents=True)
                    (operator_dir / "enrichment/profile_cache_v2").mkdir(parents=True)
                    (operator_dir / "inputs/contact_rows_min.csv").write_text("display_name,primary_email\nPat,pat@example.com\n", encoding="utf-8")
                    (operator_dir / "outputs/commands.txt").write_text("import-network\n", encoding="utf-8")
                    (operator_dir / "outputs/counts.json").write_text(json.dumps({"contact_min_rows": 1}), encoding="utf-8")
                    (operator_dir / "resolution/linkedin_resolutions_cached.csv").write_text(
                        "handle,status,linkedin_url,confidence,matched_name,matched_headline,evidence,reasoning\n",
                        encoding="utf-8",
                    )
                    (operator_dir / "enrichment/profile_cache_v2/patrick.json").write_text("{}", encoding="utf-8")
                    manifest = {
                        "operator": "patrick",
                        "operator_id": "17d602f7-f073-40b4-97a1-dba00c574442",
                        "counts": {
                            "contact_min_rows": 1,
                            "linkedin_resolution_rows": 1,
                            "linkedin_resolution_cached_rows": 1,
                            "linkedin_resolution_uncached_rows": 0,
                            "profile_cache_files": 1,
                        },
                    }
                    (operator_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    summary = {"status": "ok", "operators": [manifest]}
                    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                    return json.dumps(summary)
                if cmd[:2] == [mod.sys.executable, "scripts/bootstrap-local-from-aleph.py"]:
                    out = Path(cmd[cmd.index("--output-dir") + 1])
                    (out / "stats").mkdir(parents=True)
                    (out / "records").mkdir(parents=True)
                    (out / "local-search.duckdb").write_text("duckdb", encoding="utf-8")
                    (out / "records/people.records.jsonl").write_text("{}\n", encoding="utf-8")
                    (out / "stats/bootstrap_from_aleph.json").write_text(
                        json.dumps(
                            {
                                "status": "ok",
                                "counts": {"people_records": 1},
                                "duckdb_tables": {"local_people_positions": 1},
                            }
                        ),
                        encoding="utf-8",
                    )
                    return json.dumps({"status": "ok", "run_dir": str(out)})
                if cmd[:3] == ["gcloud", "storage", "cp"]:
                    uploads.append(cmd)
                    return ""
                raise AssertionError(f"unexpected command: {cmd}")

            original = mod.run_command
            mod.run_command = fake_run
            try:
                code = mod.main(
                    [
                        "generate",
                        "--operator-mapping",
                        str(mapping),
                        "--operators",
                        "patrick",
                        "--operator-access",
                        str(access),
                        "--seed",
                        str(tmp / "seed"),
                        "--source-dir",
                        str(source_dir),
                        "--output-root",
                        str(tmp / "out"),
                        "--gcs-uri",
                        "gs://bucket/bootstrap",
                        "--force",
                    ]
                )
            finally:
                mod.run_command = original

            self.assertEqual(code, 0)
            manifest = json.loads((tmp / "out/operators/patrick/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["operator"], "patrick")
            self.assertEqual(manifest["access"]["person_count"], 1)
            self.assertEqual(manifest["stages"]["import"]["counts"]["contact_min_rows"], 1)
            self.assertEqual(manifest["stages"]["processing"]["counts"]["people_records"], 1)
            self.assertFalse(manifest["privacy"]["raw_msgvault_db_copied"])
            self.assertFalse(manifest["privacy"]["message_bodies_copied"])
            self.assertEqual(
                manifest["gcs"]["bundle"],
                "gs://bucket/bootstrap/users/patrick/operators/17d602f7-f073-40b4-97a1-dba00c574442/operator-bootstrap.tar.gz",
            )
            bundle = Path(manifest["artifacts"]["bundle"])
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("patrick/import/inputs/contact_rows_min.csv", names)
            self.assertIn("patrick/enrich/resolution/linkedin_resolutions_cached.csv", names)
            self.assertIn("patrick/processing/search-index/local-search.duckdb", names)
            self.assertIn("patrick/sync/manifest.json", names)
            self.assertFalse(any("msgvault" in name.lower() for name in names))
            self.assertEqual(len(uploads), 3)


if __name__ == "__main__":
    unittest.main()
