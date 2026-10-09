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
            ["feedback", "fix-powerpacks", "install-powerpacks", "powerpacks-doctor", "powerset", "powerset-login", "powerset-set", "update-powerpacks"],
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
        self.assertIn("search_network_pipeline.py prepare", text)

    def test_search_company_skill_uses_company_resolver(self) -> None:
        text = (ROOT / "packs/search/skills/search-company/SKILL.md").read_text()
        self.assertIn("resolve_companies", text)
        self.assertIn("company_semantic_queries", text)
        self.assertIn("investor_names", text)
        self.assertIn("company_sector_strategy", text)


    def test_pi_adapter_installs_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td) / "skills"
            # A retired $setup left by an older install is scrubbed.
            (skills_dir / "setup").mkdir(parents=True)
            (skills_dir / "setup" / "SKILL.md").write_text("old")
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
            self.assertTrue((skills_dir / "install-powerpacks" / "SKILL.md").exists())
            self.assertFalse((skills_dir / "setup" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "import-twitter" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "build-outbound" / "SKILL.md").exists())
            self.assertIn(str(ROOT), (skills_dir / "search/SKILL.md").read_text())
            self.assertFalse((skills_dir / "search/powerpacks").exists())

    def test_codex_adapter_uses_checkout_context(self) -> None:
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
            self.assertFalse((codex_home / "powerpacks").exists())
            self.assertFalse((skills_dir / "setup" / "SKILL.md").exists())
            for skill in ("powerset", "import-messages", "install-powerpacks", "build-outbound"):
                self.assertIn(str(ROOT), (skills_dir / skill / "SKILL.md").read_text())
                self.assertFalse((skills_dir / skill / "powerpacks").exists())

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
            self.assertIn(str(ROOT), (skills_dir / "build-outbound/SKILL.md").read_text())
            self.assertFalse((skills_dir / "build-outbound/powerpacks").exists())

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

    def test_install_and_hosted_config_use_separate_urls(self) -> None:
        text = (ROOT / "packs/powerset/skills/install-powerpacks/SKILL.md").read_text()
        self.assertIn("https://powerset.dev/powerpacks", text)

        hosted_env = (ROOT / "packs/powerset/templates/env.powerset.example").read_text()
        self.assertIn(
            "POWERSET_API_URL=https://search-api-7wk4uhe77q-uw.a.run.app",
            hosted_env,
        )
        self.assertIn("POWERPACKS_AUTH0_AUDIENCE=https://api.powerset.dev", hosted_env)
        # One API-base var only — the retired aliases must not creep back.
        self.assertNotIn("POWERPACKS_SEARCH_API_URL", hosted_env)
        self.assertNotIn("POWERPACKS_API_BASE_URL", hosted_env)
        self.assertNotIn("POWERSET_API_URL=https://api.powerset.dev", hosted_env)

    def test_modal_driver_mounts_the_powerset_api_secret(self) -> None:
        modal_driver = (ROOT / "packs/indexing/modal/linkedin_modal_pipeline.py").read_text()
        self.assertIn('modal.Secret.from_name("powerset-api")', modal_driver)
        self.assertIn('os.environ.get("POWERSET_API_KEY_BACKUP"', modal_driver)
        self.assertIn('modal.Secret.from_dict({"POWERSET_API_KEY": backup})', modal_driver)

        env_template = (ROOT / "packs/powerset/templates/env.example").read_text()
        self.assertIn("POWERSET_API_KEY=", env_template)
        self.assertIn("POWERSET_API_KEY_BACKUP=", env_template)
        self.assertIn("RAPIDAPI_KEY=", env_template)

    def test_powerset_setup_runs_the_install_skill(self) -> None:
        text = (ROOT / "packs/powerset/skills/powerset/SKILL.md").read_text()
        self.assertIn("| `$powerset setup` | Load and follow `packs/powerset/skills/install-powerpacks/SKILL.md`. |", text)
        self.assertIn("packs/powerset/primitives/auth/auth.py login", text)
        self.assertIn("packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull", text)
        self.assertIn("packs/powerset/primitives/mcp_install/mcp_install.py install --host all", text)
        self.assertIn("uv run --env-file .env --project . python", text)
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

    def test_search_surface_documents_company_entrypoint(self) -> None:
        text = (ROOT / "packs/search/docs/search-surface.md").read_text()
        self.assertIn("/search-network <query>", text)
        self.assertIn("/search-company <query>", text)
        self.assertIn("company lookup", text.lower())

    def test_search_network_uses_expand_primitive_directly(self) -> None:
        task = json.loads((ROOT / "packs/search/tasks/search-network.task.json").read_text())
        task_step_ids = [step["id"] for step in task["steps"]]
        self.assertIn("resolve_investors", task_step_ids)
        self.assertIn("count_candidates", task_step_ids)
        self.assertIn("execute_role_search", task_step_ids)
        self.assertNotIn("direct_count", task_step_ids)
        self.assertNotIn("direct_execute", task_step_ids)

        expand_step = next(step for step in task["steps"] if step["id"] == "expand_search_request")
        self.assertEqual(expand_step["kind"], "primitive")
        self.assertEqual(expand_step["primitive"], "expand_search_request")
        self.assertNotIn("skill", expand_step)

        text = (ROOT / "packs/search/skills/search/SKILL.md").read_text()
        self.assertIn("search_network_pipeline.py prepare", text)
        self.assertIn("company_directory_fast_path", text)
        self.assertIn("deep-mode.md", text)

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
        self.assertNotIn("match_local_candidates.py", text)
        self.assertIn("imports/messages/importer.py run", text)
        self.assertNotIn("index_contacts_pipeline.py fan-in", text)
        self.assertIn("imports/status.py status", text)
        self.assertIn("people.csv", text)
        self.assertIn("$deep-context", text)
        # Research/review and indexing live in the single deep-context workflow.
        self.assertNotIn("review_research_web.py", text)
        self.assertNotIn("linkedin_modal_pipeline.py index-people", text)

    def test_search_network_continues_requested_execution(self) -> None:
        text = (ROOT / "packs/search/skills/search/SKILL.md").read_text()
        self.assertNotIn("Execute this search or modify it?", text)
        self.assertNotIn("execute`, `modify`, or `search only`", text)
        self.assertIn("--execute-approved", text)
        self.assertIn("do not ask for another approval", " ".join(text.split()))

    def test_task_state_tracks_planned_steps_separately_from_execution_log(self) -> None:
        task_state = ROOT / "packs/search/primitives/task_state/task_state.py"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "run.json"
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "init",
                    "--query",
                    "software engineers in sf",
                    "--out",
                    str(state_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "request-approval",
                    "--state",
                    str(state_path),
                    "--reason",
                    "test",
                    "--proposed-next-step",
                    "run planned steps",
                    "--plan-json",
                    json.dumps({"planned_steps": ["resolve_education", "count_candidates"]}),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "record-step",
                    "--state",
                    str(state_path),
                    "--step-id",
                    "resolve_education",
                    "--output-json",
                    "{}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            state = json.loads(state_path.read_text())
            self.assertEqual([step["id"] for step in state["steps"]], ["resolve_education"])
            planned_by_id = {step["id"]: step for step in state["planned_steps"]}
            self.assertEqual(planned_by_id["resolve_education"]["status"], "completed")
            self.assertIn("completed_at", planned_by_id["resolve_education"])
            self.assertEqual(planned_by_id["count_candidates"]["status"], "pending")

    def test_task_state_accepts_bare_planned_steps_array(self) -> None:
        task_state = ROOT / "packs/search/primitives/task_state/task_state.py"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "run.json"
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "init",
                    "--query",
                    "software engineers in sf",
                    "--out",
                    str(state_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "request-approval",
                    "--state",
                    str(state_path),
                    "--reason",
                    "test",
                    "--proposed-next-step",
                    "run planned steps",
                    "--plan-json",
                    json.dumps(["resolve_education", "count_candidates"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            state = json.loads(state_path.read_text())
            self.assertEqual(
                [step["id"] for step in state["planned_steps"]],
                ["resolve_education", "count_candidates"],
            )

    def test_task_state_appends_feedback_lineage(self) -> None:
        task_state = ROOT / "packs/search/primitives/task_state/task_state.py"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "run.json"

            def append_lineage(kind: str, payload: dict) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        str(task_state),
                        "append-lineage",
                        "--state",
                        str(state_path),
                        "--kind",
                        kind,
                        "--payload-json",
                        json.dumps(payload),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "init",
                    "--query",
                    "cto technical cofounder",
                    "--out",
                    str(state_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            initialized = json.loads(state_path.read_text())
            for field in [
                "search_plan_revisions",
                "candidate_feedback",
                "criteria_mutations",
                "run_lineage",
                "exemplar_sets",
                "fanout_threads",
            ]:
                self.assertEqual(initialized[field], [])

            feedback_result = append_lineage(
                "candidate_feedback",
                {
                    "feedback_id": "fb-1",
                    "person_id": "person-1",
                    "label": "false_positive",
                    "reason": "not technical",
                    "applied_to_next_search": True,
                },
            )
            self.assertIn("candidate_feedback", feedback_result.stdout)
            append_lineage(
                "criteria_mutation",
                {
                    "source_feedback_ids": ["fb-1"],
                    "reason": "tighten technical-depth evidence",
                    "mutation": {"reject_criteria": ["non-technical operator"]},
                },
            )
            append_lineage(
                "search_plan_revision",
                {
                    "reason": "apply candidate feedback",
                    "criteria_delta": {"reject_criteria": ["non-technical operator"]},
                    "plan": {"initial_probes": 5},
                },
            )
            append_lineage(
                "run_lineage",
                {
                    "relationship": "follow_up",
                    "task_id": "search-network-child",
                    "state": ".powerpacks/runs/child.json",
                    "artifact_dir": ".powerpacks/search/child",
                },
            )
            append_lineage(
                "exemplar_set",
                {
                    "name": "above-cutoff technical builders",
                    "person_ids": ["person-1", "person-2"],
                    "selection_reason": "score >= 0.3 and strong technical evidence",
                },
            )
            append_lineage(
                "fanout_thread",
                {
                    "cluster_label": "cloud cost infrastructure builders",
                    "criteria": {"companies": ["Databricks", "Snowflake"]},
                    "state": ".powerpacks/runs/fanout.json",
                    "artifact_dir": ".powerpacks/search/fanout",
                },
            )

            state = json.loads(state_path.read_text())
            self.assertEqual(state["candidate_feedback"][0]["feedback_id"], "fb-1")
            self.assertEqual(state["criteria_mutations"][0]["source_feedback_ids"], ["fb-1"])
            self.assertIn("mutation_id", state["criteria_mutations"][0])
            self.assertEqual(state["search_plan_revisions"][0]["revision"], 1)
            self.assertIn("revision_id", state["search_plan_revisions"][0])
            self.assertEqual(state["run_lineage"][0]["relationship"], "follow_up")
            self.assertIn("lineage_id", state["run_lineage"][0])
            self.assertEqual(state["exemplar_sets"][0]["person_ids"], ["person-1", "person-2"])
            self.assertIn("exemplar_set_id", state["exemplar_sets"][0])
            self.assertEqual(state["fanout_threads"][0]["cluster_label"], "cloud cost infrastructure builders")
            self.assertIn("thread_id", state["fanout_threads"][0])

            events = [json.loads(line) for line in state_path.with_suffix(".json.events.jsonl").read_text().splitlines()]
            lineage_events = [event for event in events if event.get("event") == "append_lineage"]
            self.assertEqual(
                [event["kind"] for event in lineage_events],
                [
                    "candidate_feedback",
                    "criteria_mutation",
                    "search_plan_revision",
                    "run_lineage",
                    "exemplar_set",
                    "fanout_thread",
                ],
            )
            self.assertEqual(lineage_events[0]["feedback_id"], "fb-1")


if __name__ == "__main__":
    unittest.main()
