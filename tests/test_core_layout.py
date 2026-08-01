import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CoreLayoutTests(unittest.TestCase):
    def test_user_facing_skills(self) -> None:
        powerset_pack = sorted(
            path.name for path in (ROOT / "packs/powerset/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(
            powerset_pack,
            ["feedback", "fix-powerpacks", "install-powerpacks", "powerset", "powerset-login", "powerset-set", "update-powerpacks"],
        )
        search_pack = sorted(
            path.name for path in (ROOT / "packs/search/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(search_pack, ["search", "search-company", "search-sql"])
        ingestion_pack = sorted(
            path.name for path in (ROOT / "packs/ingestion/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(
            ingestion_pack,
            [
                "clean-slate",
                "deep-context",
                "import-gmail",
                "import-messages",
                "import-twitter",
                "logbook",
                "msgvault",
                "setup",
            ],
        )
        indexing_pack = sorted(
            path.name for path in (ROOT / "packs/indexing/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(indexing_pack, ["build-local-search-index"])
        outbound_pack = sorted(
            path.name for path in (ROOT / "packs/apollo/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(outbound_pack, ["build-outbound"])

    def test_pack_skills_have_codex_frontmatter(self) -> None:
        for path in sorted((ROOT / "packs").glob("*/skills/*/SKILL.md")):
            with self.subTest(path=path.relative_to(ROOT)):
                lines = path.read_text().splitlines()
                self.assertGreaterEqual(len(lines), 3)
                self.assertEqual(lines[0], "---")
                self.assertIn("---", lines[1:])

    def test_no_legacy_add_skill_references_in_core_skill(self) -> None:
        text = (ROOT / "packs/search/skills/search/SKILL.md").read_text()
        self.assertNotIn("skills/add-", text)
        self.assertNotIn("view_search_results", text)
        self.assertNotIn("workflows/query-decomposition.md", text)
        self.assertIn("packs.search.pipeline.search", text)
        self.assertIn("--spec <run>/search_spec.json", text)
        self.assertIn("--output-dir .powerpacks/search-runs/<run-id>", text)
        self.assertIn("email and phone return `unsupported_capability`", text)
        self.assertNotIn("search_network_pipeline.py prepare", text)
        self.assertNotIn("deep_search_loop.py", text)
        self.assertNotIn("$search-company", text)
        self.assertNotIn("/search-network", text)

    def test_search_company_skill_is_retired(self) -> None:
        self.assertFalse((ROOT / "packs/search/skills/search-company/SKILL.md").exists())


    def test_pi_adapter_installs_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td) / "skills"
            proc = subprocess.run(
                [str(ROOT / "install.sh"), "pi", str(skills_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "POWERPACKS_SKIP_UV_SYNC": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((skills_dir / "powerset" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "search" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "build-local-search-index" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "import-gmail" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "import-messages" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "setup" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "import-twitter" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "build-outbound" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "powerset" / "powerpacks" / "packs").is_dir())
            self.assertTrue((skills_dir / "search" / "powerpacks" / "pyproject.toml").exists())
            self.assertIn(
                "turbopuffer",
                (skills_dir / "search" / "powerpacks" / "pyproject.toml").read_text(),
            )
            self.assertFalse(
                (skills_dir / "powerset" / "powerpacks" / "packs" / "powerset" / "skills" / "powerset" / "SKILL.md").exists()
            )
            nested_skill_files = sorted(
                path.relative_to(skills_dir)
                for path in skills_dir.glob("*/powerpacks/packs/*/skills/*/SKILL.md")
            )
            self.assertEqual(nested_skill_files, [])

    def test_codex_adapter_uses_shared_powerpacks_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / ".codex"
            skills_dir = Path(td) / "skills"
            proc = subprocess.run(
                [str(ROOT / "install.sh"), "codex", str(skills_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "CODEX_HOME": str(codex_home), "POWERPACKS_SKIP_UV_SYNC": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            bundle = codex_home / "powerpacks"
            self.assertTrue((bundle / "packs").is_dir())
            self.assertTrue((bundle / "pyproject.toml").exists())
            self.assertTrue((bundle / "scripts" / "build-local-duckdb-shim.py").exists())
            self.assertTrue((skills_dir / "powerset" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "import-messages" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "setup" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "build-outbound" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "powerset" / "powerpacks").is_symlink())
            self.assertTrue((skills_dir / "import-messages" / "powerpacks").is_symlink())
            self.assertTrue((skills_dir / "setup" / "powerpacks").is_symlink())
            self.assertTrue((skills_dir / "build-outbound" / "powerpacks").is_symlink())
            self.assertEqual((skills_dir / "powerset" / "powerpacks").resolve(), bundle.resolve())
            self.assertEqual((skills_dir / "import-messages" / "powerpacks").resolve(), bundle.resolve())
            self.assertEqual((skills_dir / "setup" / "powerpacks").resolve(), bundle.resolve())
            self.assertEqual((skills_dir / "build-outbound" / "powerpacks").resolve(), bundle.resolve())
            nested_skill_files = sorted(path.relative_to(bundle) for path in bundle.glob("packs/*/skills/*/SKILL.md"))
            self.assertEqual(nested_skill_files, [])

    def test_claude_adapter_installs_build_outbound_skill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td) / "skills"
            proc = subprocess.run(
                [str(ROOT / "install.sh"), "claude-code", str(skills_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "POWERPACKS_SKIP_UV_SYNC": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((skills_dir / "build-outbound" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "build-outbound" / "powerpacks" / "packs" / "apollo").is_dir())
            nested_skill_files = sorted(
                path.relative_to(skills_dir)
                for path in skills_dir.glob("*/powerpacks/packs/*/skills/*/SKILL.md")
            )
            self.assertEqual(nested_skill_files, [])

    def test_powerset_login_skill_uses_api_runtime_key_primitives(self) -> None:
        text = (ROOT / "packs/powerset/skills/powerset-login/SKILL.md").read_text()
        # The setup checker is still the diagnosis entrypoint, but the skill's
        # user-facing contract should stay quiet.
        self.assertIn("packs/powerset/primitives/doctor/doctor.py run", text)
        self.assertIn("Updating your credentials...", text)
        self.assertIn("Credentials updated. Please restart Codex", text)
        self.assertIn("do not\nrun nested fix commands", text)
        self.assertIn("packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull", text)
        self.assertIn("authenticated Powerset API", text)
        self.assertIn("The Google Cloud CLI remains relevant\nonly to", text)
        # The setup classification must be documented.
        self.assertIn("fix_kind", text)
        self.assertNotIn("provision_user_secrets", text)
        self.assertNotIn("provision_runtime_env", text)
        self.assertNotIn("gcloud auth login", text)
        self.assertNotIn("Secret Manager", text)

    def test_install_skill_distinguishes_install_auth_and_provisioning_urls(self) -> None:
        text = (ROOT / "packs/powerset/skills/install-powerpacks/SKILL.md").read_text()
        self.assertIn("using my Powerset account", text)
        self.assertIn("Its Steps 1-3 authenticate the Powerset user", text)
        self.assertIn("Do not run a\n     separate `$powerset setup`", text)
        self.assertIn("cp packs/powerset/templates/env.powerset.example .env", text)
        self.assertIn("https://powerset.dev/powerpacks", text)
        self.assertIn("https://search-api-7wk4uhe77q-uw.a.run.app", text)
        self.assertIn("Auth0 audience identifier only: `https://api.powerset.dev`", text)

        hosted_env = (ROOT / "packs/powerset/templates/env.powerset.example").read_text()
        self.assertIn(
            "POWERSET_API_URL=https://search-api-7wk4uhe77q-uw.a.run.app",
            hosted_env,
        )
        # One API-base var only — the retired aliases must not creep back.
        self.assertNotIn("POWERPACKS_SEARCH_API_URL", hosted_env)
        self.assertNotIn("POWERPACKS_API_BASE_URL", hosted_env)
        self.assertNotIn("POWERSET_API_URL=https://api.powerset.dev", hosted_env)

    def test_setup_skill_asks_about_powerset_account(self) -> None:
        text = (ROOT / "packs/ingestion/skills/setup/SKILL.md").read_text()
        # Step 1 is an explicit choice, not a silent Powerset default.
        self.assertIn("Do you have a Powerset account you'd like to log in with?", text)
        self.assertIn("custom-workspace route", text)
        self.assertIn("1. Choose credentials (Powerset or prepared Modal workspace)", text)
        # Powerset route initializes .env from the hosted template.
        self.assertIn("cp packs/powerset/templates/env.powerset.example .env", text)
        # Powerset keys are verified after provisioning.
        self.assertIn("pull_runtime_keys.py check --env-file .env", text)
        # Large LinkedIn imports need realistic, count-based Modal expectations.
        self.assertIn("Estimate from the Step 4 connection count", text)
        self.assertIn("10,001–20,000 | 60–120 minutes", text)
        self.assertIn("one-hour warm-cache run; allow up to two hours if cache-cold", text)
        self.assertIn('about every **5 minutes**', text)
        # Custom workspaces verify the actual named Modal secrets instead of
        # treating a local OpenAI key as sandbox provisioning.
        self.assertIn("modal secret list --json", text)
        self.assertIn("powerset-openai", text)
        self.assertIn("powerset-rapidapi", text)
        self.assertIn("POWERPACKS_OPERATOR_ID", text)

        installer = (ROOT / "packs/powerset/skills/install-powerpacks/SKILL.md").read_text()
        self.assertIn("only when the user chose Powerset", installer)

    def test_powerset_setup_skill_combines_login_env_and_mcp(self) -> None:
        text = (ROOT / "packs/powerset/skills/powerset/SKILL.md").read_text()
        self.assertIn("$powerset setup", text)
        self.assertIn("$powerset setup                 log in, pull runtime keys, and install/refresh MCP", text)
        self.assertIn("packs/powerset/primitives/auth/auth.py login", text)
        self.assertIn("packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull", text)
        self.assertIn("packs/powerset/primitives/mcp_install/mcp_install.py install --host all", text)
        self.assertIn("uv run --env-file .env --project . python", text)
        self.assertIn("Powerset setup complete. Please restart Codex", text)
        self.assertNotIn("provision_runtime_env", text)
        self.assertNotIn("operator_bootstrap", text)
        self.assertNotIn("GCP Secret Manager", text)

        login_alias = (ROOT / "packs/powerset/skills/powerset-login/SKILL.md").read_text()
        self.assertIn("prefer the unified `$powerset setup`", login_alias)

    def test_feedback_skill_is_identifiers_only_and_consent_gated(self) -> None:
        text = (ROOT / "packs/powerset/skills/feedback/SKILL.md").read_text()
        self.assertIn("packs/powerset/primitives/send_feedback/send_feedback.py", text)
        self.assertIn("--dry-run", text)
        self.assertIn("identifiers only", text)
        self.assertIn("NEVER include: message bodies", text)
        self.assertIn("send with person identifiers", text)
        self.assertIn("data_inconsistency", text)
        self.assertIn("$powerset setup", text)
        self.assertIn("--artifact", text)
        self.assertIn("Never attach dossier files", text)
        self.assertIn("$deep-context", text)

    def test_search_surface_documents_typed_routes(self) -> None:
        text = (ROOT / "packs/search/docs/search-surface.md").read_text()
        self.assertIn("engine + gtm", text)
        self.assertIn("typed `SearchSpec`", text)
        self.assertIn("People at a named company", text)
        self.assertIn("There is no public company-search command", text)
        self.assertIn("needs_input", text)
        self.assertIn("perform no retrieval", text)

    def test_search_skill_uses_typed_composition_root(self) -> None:
        text = (ROOT / "packs/search/skills/search/SKILL.md").read_text()
        self.assertIn("packs.search.pipeline.search", text)
        self.assertIn("target", text)
        self.assertIn("profile", text)
        self.assertIn("deep-mode.md", text)
        self.assertIn("one schema-valid `search.spec.v1` document", text)
        self.assertIn("There is no public company-search target", text)
        self.assertTrue((ROOT / "packs/search/pipeline/search.py").exists())
        self.assertTrue((ROOT / "packs/search/pipeline/recruiting.py").exists())

    def test_json_contracts_and_schemas_parse(self) -> None:
        roots = [
            ROOT / "packs/powerset/schemas",
            ROOT / "packs/search/contracts",
            ROOT / "packs/search/schemas",
            ROOT / "packs/search/tasks",
            ROOT / "packs/search/evals",
            ROOT / "packs/ingestion/schemas",
            ROOT / "packs/indexing/tasks",
        ]
        for root in roots:
            with self.subTest(root=root):
                for path in root.rglob("*.json"):
                    json.loads(path.read_text())

    def test_import_messages_documents_contact_sync_flow(self) -> None:
        text = (ROOT / "packs/ingestion/skills/import-messages/SKILL.md").read_text()
        self.assertIn("$import-messages", text)
        self.assertIn("imports/messages/match_local_candidates.py match", text)
        self.assertIn("imports/messages/importer.py run", text)
        self.assertIn("index_contacts_pipeline.py fan-in", text)
        self.assertIn("imports/status.py status", text)
        self.assertIn("candidates.csv", text)
        self.assertIn("$deep-context", text)
        # Research/review and indexing live in the single deep-context workflow.
        self.assertNotIn("review_research_web.py", text)
        self.assertNotIn("linkedin_modal_pipeline.py index-people", text)

    def test_search_uses_single_persisted_spec_command(self) -> None:
        text = (ROOT / "packs/search/skills/search/SKILL.md").read_text()
        command = "uv run --project . python -m packs.search.pipeline.search"
        self.assertEqual(text.count(command), 1)
        self.assertIn("--spec <run>/search_spec.json", text)
        self.assertNotIn("search_network_pipeline.py prepare", text)
        self.assertNotIn("deep_search_loop.py", text)

    def test_removed_orchestration_is_absent(self) -> None:
        self.assertTrue((ROOT / "packs/search/pipeline/recruiting.py").exists())
        self.assertTrue((ROOT / "packs/search/pipeline/recruiting_stages.py").exists())
        for path in (
            "packs/search/primitives/deep_search/deep_search_loop.py",
            "packs/search/primitives/deep_search/run_wide_search.py",
            "packs/search/primitives/search_network_pipeline",
            "packs/search/primitives/task_state",
            "packs/search/tasks/search-network.task.json",
            "packs/search/tasks/search-network-jd.task.json",
            "packs/search/schemas/search-network-task.schema.json",
            "packs/search/schemas/task-run.schema.json",
            "packs/search/primitives/execute_search_slice",
            "packs/search/primitives/merge_candidate_frontier",
            "packs/search/primitives/agentic_candidate_review",
            "packs/search/primitives/apply_prefilters",
            "packs/search/primitives/execute_role_search",
            "packs/search/primitives/count_candidates",
            "packs/search/primitives/hydrate_people",
            "packs/search/primitives/llm_filter_candidates",
            "packs/search/primitives/persist_search_results",
            "packs/search/primitives/shared/" + "search_backend" + "_mode.py",
            "packs/search/evals/search-network/cases.json",
            "packs/search/tasks/local-prod-parity.task.json",
        ):
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists())
        self.assertTrue((ROOT / "packs/search/schemas/search-network-jd-plan.schema.json").exists())

        repository_gate = (ROOT / "scripts/test-powerpacks").read_text()
        self.assertIn("tests.test_layered_search_engine", repository_gate)
        self.assertNotIn("tests.test_local_search_pipeline", repository_gate)


    def test_search_surface_documents_current_entrypoint(self) -> None:
        text = (ROOT / "packs/search/docs/search-surface.md").read_text()
        self.assertIn("`$search` is the public router", text)
        self.assertIn("People at a named company", text)
        self.assertIn("`$search-sql` and\n`$search-contacts` remain explicit non-engine targets", text)

    def test_search_deep_mode_documents_typed_review_resume_and_paid_boundary(self) -> None:
        deep = (ROOT / "packs/search/skills/search/deep-mode.md").read_text()
        architecture = (ROOT / "packs/search/docs/search-architecture.md").read_text()
        self.assertEqual(deep.count("uv run --project . python -m packs.search.pipeline.search"), 2)
        self.assertIn("`awaiting_review`", deep)
        self.assertIn("`review/binding.json`", deep)
        self.assertIn("`plan_sha256`", deep)
        self.assertIn("`recruiting.reviewed_plan_hash`", deep)
        self.assertIn("`failed_binding`", deep)
        self.assertIn("recruiting.plan_approved=true", deep)
        self.assertIn("recruiting.judge_approved=true", deep)
        self.assertIn("do not authorize a paid quality-validation run", deep)
        self.assertNotIn("deep_search_loop.py", deep)
        self.assertNotIn("deep_search_loop.py", architecture)
        self.assertIn("one engine", architecture)

if __name__ == "__main__":
    unittest.main()
