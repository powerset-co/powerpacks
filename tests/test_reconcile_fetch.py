"""Offline tests for reconcile's prefer-cache-always-retrieve profile fetch.

The RapidAPI client is mocked where reconcile_linkedin binds it; everything else
(candidate selection, view rebuild from the cache, keyless skip, counts) runs
for real against synthetic fixtures.
"""

import json
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context.enrich import profile_projection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.realize.apply_retargets import ApplyRetargets
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    IdentityMachineProjection,
    ProjectionStatus,
    IdentityOrigin,
    ReviewExportRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIUsage
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.shared import openai_responses
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.db.identity_views import (
    attached_identity_queue,
    human_settled_identities,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver, projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelExecutionResult,
    ParallelProviderResult,
    ProviderGroupStatus,
    ProviderStatusCounts,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ContactChannel,
    ResearchQueueRow,
)
import packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue as queue
import packs.ingestion.primitives.deep_context.enrich.identity_reconcile.runner as reconcile_runner
import packs.ingestion.primitives.deep_context.enrich.identity_reconcile.reconcile_linkedin as reconcile
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.reconcile_linkedin import ReconcileLinkedin
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidedResearch
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    GuidedProviderResult,
    IdentityProfileSource,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityJudgeResult,
    IdentityTask,
    IdentityUsage,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.profile_models import (
    ProfileHydration,
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.enrich import rapidapi_client as rapid
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path
from packs.shared.csv_io import CsvIO
from deep_context_sqlite_test_helpers import seed_identity


def task(
    pub="jordan-bravo",
    url="https://www.linkedin.com/in/jordan-bravo",
    has_profile=False,
    from_connections=False,
    pid="pid-1",
):
    return IdentityTask(
        parent_slug="jordan-bravo-ab12cd34",
        parent_id="parent-1",
        name="Jordan Bravo",
        candidate_key=pub,
        person_ids=(pid,),
        from_connections=from_connections,
        evidence=DossierEvidence(name="Jordan Bravo"),
        linkedin=JudgeProfile.from_payload(
            {
                "public_identifier": pub,
                "linkedin_url": url,
                "has_profile": has_profile,
                "source": "people_csv",
            }
        ),
    )


def profile_db(root: Path) -> Db:
    db = Db(root / "deep-context.sqlite")
    seed_identity(
        db,
        parent_id="parent-1",
        person_id="pid-1",
        row_key="jordan-bravo",
        name="Jordan Bravo",
        machine_worth="maybe",
        linkedin_url="https://www.linkedin.com/in/jordan-bravo",
    )
    return db


def judge_result(
    payload: dict[str, object],
    fingerprint: str = "fixture-judge-fingerprint",
) -> IdentityJudgeResult:
    return IdentityJudgeResult(
        verdict=IdentityVerdict.from_payload(payload),
        usage=IdentityUsage(),
        error="",
        fingerprint=fingerprint,
    )


def enrichment_row(
    *,
    row_key: str = "jordan-bravo",
    parent_slug: str = "jordan-bravo-p",
) -> EnrichmentQueueRow:
    return EnrichmentQueueRow(
        parent_id="parent-1",
        parent_slug=parent_slug,
        name="Jordan Bravo",
        person_ids=("pid-1",),
        row_key=row_key,
        candidate_exists=True,
        linkedin_url=f"https://www.linkedin.com/in/{row_key}",
        verdict="",
        verdict_reason="",
        match_emails=(),
        match_phones=(),
        candidate_origin=False,
    )



# The answer the stubbed provider returns for the attached-identity judge. Kept
# next to the stub so a reader sees the fixture and the assertion are the same
# object, not two copies that can drift.
JUDGE_ANSWER = {
    "verdict": "confirmed",
    "confidence": 0.93,
    "supporting_evidence": ["headline matches the dossier employer"],
    "contradicting_evidence": [],
    "linkedin_plausibly_absent": False,
    "recommend_deep_research": False,
    "reason": "same employer and role as the dossier",
}


def _stub_identity_judge(answer: dict[str, object]):
    """Replace the OpenAI caller with one that returns `answer`, spending nothing.

    Patched where `judge_batch` looks the class up, so the stage builds a caller
    exactly as it does in production and only the network call is fake.
    """

    class _StubCaller:
        def __init__(self, config) -> None:
            self.usage = OpenAIUsage()

        async def call(self, **_kwargs):
            return SimpleNamespace(payload=dict(answer), usage=OpenAIUsage())

        async def close(self) -> None:
            """judge_batch closes the caller in a finally; nothing to release here."""

    return mock.patch.object(judge, "OpenAIResponsesCaller", _StubCaller)


class FetchCandidateTests(unittest.TestCase):
    def test_selects_only_urled_profileless_judge_targets(self):
        rows = [
            task(),  # wanted
            task(has_profile=True),  # already judgeable
            task(url=""),  # nothing attached
            task(from_connections=True),  # ground truth, never judged
            replace(
                task(),
                linkedin=JudgeProfile.from_payload(
                    {
                        "linkedin_url": "",
                        "has_profile": False,
                    }
                ),
            ),
        ]
        wanted = queue.profile_fetch_candidates(rows)
        self.assertEqual(len(wanted), 1)
        self.assertIs(wanted[0], rows[0])

    def test_fallback_view_has_no_experience_or_education_before_a_fetch(self):
        """No production caller ever populates raw work/education on
        IdentityProfileSource (see queue.linkedin_view's fallback branch) — a
        candidate that hasn't been fetched yet is always empty there."""
        profile = queue.linkedin_view(
            IdentityProfileSource(
                linkedin_url="https://www.linkedin.com/in/jordan-bravo",
                full_name="Jordan Bravo",
                headline="Founder at Bravo Robotics",
            )
        )

        self.assertEqual(profile.full_name, "Jordan Bravo")
        self.assertEqual(profile.headline, "Founder at Bravo Robotics")
        self.assertEqual(profile.experiences, ())
        self.assertEqual(profile.education, ())
        with self.assertRaises(AttributeError):
            queue.linkedin_view({"school": "State University"})  # type: ignore[arg-type]


class FetchMissingProfilesTests(unittest.TestCase):
    def test_old_cache_shape_preserves_judgment_fingerprint_on_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = profile_db(root)
            payload = {
                "public_identifier": "jordan-bravo",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [
                        {
                            "title": "Founder",
                            "company": "Bravo Robotics",
                            "companyName": "Wrong alternate company",
                            "starts_at": {"year": 2020},
                        }
                    ],
                    "education": [
                        {
                            "school": "State University",
                            "schoolName": "Wrong alternate school",
                            "degree": "BS",
                            "field": "Robotics",
                        }
                    ],
                    "city": "San Francisco",
                    "state": "CA",
                    "country": "US",
                },
            }
            db.project_rows(
                (
                    ArtifactRow(
                        "profile:jordan-bravo",
                        ArtifactKind.PROFILE.value,
                        "parent-1",
                        str(root / "profiles" / "jordan-bravo.json"),
                        "legacy-profile-fingerprint",
                        ProjectionStatus.PROJECTED.value,
                        candidate_key="jordan-bravo",
                        payload_json=json.dumps(payload),
                    ),
                )
            )

            projected = profile_projection.profile_payloads(db)["jordan-bravo"]
            profile = queue.linkedin_view(
                IdentityProfileSource(
                    public_identifier="jordan-bravo",
                    linkedin_url="https://www.linkedin.com/in/jordan-bravo",
                ),
                projected,
            )
            evidence = DossierEvidence(
                name="Jordan Bravo",
                relationship="former colleague",
                employers=("Bravo Robotics",),
                school="State University",
            )

        self.assertEqual(
            projected.normalized_profile.education[0].school_name,
            "State University",
        )
        self.assertEqual(
            projected.to_payload(),
            ProfileResult.from_payload(
                "jordan-bravo",
                "https://www.linkedin.com/in/jordan-bravo",
                payload,
            ).to_payload(),
        )
        self.assertEqual(profile.education, ("BS, Robotics — State University",))
        self.assertEqual(
            judge.judgment_fingerprint(
                evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.2", effort="medium",
            ),
            "300c5f06c68bb77b1bdd75f7c8458731713a7a2c52a11ef36aa975c519d90100",
        )

    def test_failed_cache_preserves_row_identity_fingerprint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = profile_db(root)
            payload = {
                "state": rapid.PROFILE_EMPTY,
                "status_code": 404,
                "normalized_profile": {
                    "success": False,
                    "error": "not_found",
                    "public_identifier": "stale-cache-identifier",
                },
            }
            db.project_rows(
                (
                    ArtifactRow(
                        "profile:jordan-bravo",
                        ArtifactKind.PROFILE.value,
                        "parent-1",
                        str(root / "profiles" / "jordan-bravo.json"),
                        "legacy-failed-profile-fingerprint",
                        ProjectionStatus.PROJECTED.value,
                        candidate_key="jordan-bravo",
                        payload_json=json.dumps(payload),
                    ),
                )
            )

            projected = profile_projection.profile_payloads(db)["jordan-bravo"]
            profile = queue.linkedin_view(
                IdentityProfileSource(
                    public_identifier="jordan-bravo",
                    linkedin_url="https://www.linkedin.com/in/jordan-bravo",
                ),
                projected,
            )
            evidence = DossierEvidence(
                name="Jordan Bravo",
                relationship="former colleague",
                employers=("Bravo Robotics",),
                school="State University",
            )

        self.assertEqual(profile.public_identifier, "jordan-bravo")
        self.assertEqual(
            judge.judgment_fingerprint(
                evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.2", effort="medium",
            ),
            "57a0d8c06b0dee4e3752f8ae19f1b883475e9284ec41aadc5fc355d9bc120cea",
        )

    def test_keyless_install_skips_cleanly(self):
        with (
            TemporaryDirectory() as directory,
            mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "resolve_key",
                return_value="",
            ),
        ):
            root = Path(directory)
            fetched = queue.fetch_missing_profiles(profile_db(root), [task()], root / "cache")
        self.assertEqual(fetched.fetch_skipped_no_key, 1)
        self.assertEqual(fetched.fetch_ok, 0)

    def test_fetch_hydrates_cache_and_rebuilds_view(self):
        with TemporaryDirectory() as d:
            cache_dir = Path(d)
            t = task()
            db = profile_db(cache_dir)

            def fake_fetch(self, pub, url, *, cache_dir=None, **kw):
                profile = {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                    "education": [],
                    "city": "SF",
                    "state": "",
                    "country": "",
                }
                return {"state": rapid.PROFILE_CONTENT, "status_code": 200, "normalized_profile": profile}

            with (
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "resolve_key",
                    return_value="k",
                ),
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "__init__",
                    return_value=None,
                ),
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "get_profile",
                    fake_fetch,
                ),
            ):
                fetched = queue.fetch_missing_profiles(db, [t], cache_dir)

        refreshed = fetched.tasks[0]
        self.assertEqual(fetched.fetch_ok, 1)
        self.assertEqual(fetched.fetch_failed, 0)
        self.assertTrue(refreshed.linkedin.has_profile)
        self.assertEqual(refreshed.linkedin.source, "cache")
        self.assertIn("Bravo Robotics", " ".join(refreshed.linkedin.experiences))

    def test_failed_fetch_counts_and_leaves_task_unjudgeable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            t = task()
            with (
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "resolve_key",
                    return_value="k",
                ),
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "__init__",
                    return_value=None,
                ),
                mock.patch.object(
                    profile_projection.rapidapi_client.RapidApiClient,
                    "get_profile",
                    return_value={
                        "state": rapid.PROFILE_EMPTY,
                        "status_code": 404,
                        "normalized_profile": {},
                    },
                ),
            ):
                fetched = queue.fetch_missing_profiles(profile_db(root), [t], root / "cache")
        self.assertEqual(fetched.fetch_failed, 1)
        self.assertFalse(fetched.tasks[0].linkedin.has_profile)


class SqliteReconcileTests(unittest.TestCase):
    def test_cli_dry_run_estimates_without_running_the_stage(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "deep-context.sqlite"
            Db(db_path)
            with (
                mock.patch.object(reconcile, "dry_run_estimate", return_value={"status": "dry_run"}) as estimate,
                mock.patch.object(reconcile, "ReconcileLinkedin") as node,
                mock.patch.object(reconcile, "emit") as emit,
            ):
                self.assertEqual(reconcile.main(["--db", str(db_path), "--dry-run"]), 0)
            estimate.assert_called_once()
            node.assert_not_called()
            emit.assert_called_once_with({"status": "dry_run"})

    def test_attached_link_judgment_is_file_first_and_sqlite_projected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-1",
                person_id="person-1",
                row_key="jordan-bravo",
                name="Jordan Bravo",
                machine_worth="maybe",
                display_slug="jordan-bravo-p",
                parent_public_identifier="jordan-bravo",
                linkedin_url="https://www.linkedin.com/in/jordan-bravo",
            )
            facts, raw, cache, output = (root / "facts", root / "raw", root / "cache", root / "reconcile")
            for path in (facts, raw, cache):
                path.mkdir()
            profile_path = profile_cache_path(cache, "jordan-bravo")
            profile_payload = {
                "raw_response": {},
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [],
                    "education": [],
                },
            }
            profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")
            profile_projection.project_profile_results(
                db,
                [
                    (
                        ProfileTarget(
                            "jordan-bravo",
                            "https://www.linkedin.com/in/jordan-bravo",
                            "jordan-bravo",
                            "parent-1",
                        ),
                        ProfileResult.from_payload(
                            "jordan-bravo",
                            "https://www.linkedin.com/in/jordan-bravo",
                            profile_payload,
                        ),
                    )
                ],
                cache,
            )
            verdicts = output / "verdicts.jsonl"
            # Stub the PROVIDER, not the stage: this runs the same judging path
            # production runs, with a fixed answer standing in for the model.
            # (It used to pass no_llm=True, which ran a different code path
            # entirely and asserted on the offline stub's own verdict.)
            with _stub_identity_judge(JUDGE_ANSWER):
                payload = ReconcileLinkedin(
                    db=db,
                    profile_cache_dir=cache,
                    verdicts_jsonl=verdicts,
                ).run()

            link = db.query("SELECT * FROM links WHERE row_key='jordan-bravo'")[0]
            self.assertEqual((link["machine_action"], link["machine_approved"]), ("verify", "auto"))
            self.assertEqual(link["judgment_artifact_path"], str(verdicts))
            self.assertTrue(verdicts.exists())
            expected = {
                "parent_slug": "jordan-bravo-p",
                "parent_id": "parent-1",
                "name": "Jordan Bravo",
                "candidate_key": "jordan-bravo",
                "person_ids": ["person-1"],
                "conflict": False,
                "linkedin": {
                    "public_identifier": "jordan-bravo",
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "profile_pic_url": "",
                    "experiences": [],
                    "education": [],
                    "location": "",
                    "source": "cache",
                    "has_profile": True,
                },
                "verdict": dict(JUDGE_ANSWER),
                "error": "",
            }
            self.assertEqual(
                verdicts.read_bytes(),
                (json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
            )
            self.assertEqual(payload.tasks, 1)
            self.assertFalse((output / "verdicts.csv").exists())
            self.assertFalse((output / "summary.md").exists())
            self.assertFalse((root / "consolidate.csv").exists())

    def test_llm_error_is_not_replaced_by_a_deterministic_verdict(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = profile_db(root)
            verdicts = root / "reconcile" / "verdicts.jsonl"
            failed = IdentityJudgeResult(
                verdict=None,
                usage=IdentityUsage(),
                error="TimeoutError: exhausted retries",
                fingerprint="failed-judge-fingerprint",
            )
            with (
                mock.patch.object(
                    reconcile_runner,
                    "build_tasks",
                    return_value=[task(has_profile=True)],
                ),
                mock.patch.object(
                    judge,
                    "judge_batch",
                    return_value=[failed],
                ) as judge_batch,
            ):
                manifest = ReconcileLinkedin(
                    db=db,
                    profile_cache_dir=root / "profiles",
                    verdicts_jsonl=verdicts,
                ).execute()

            receipt = json.loads(verdicts.read_text(encoding="utf-8"))
            link = db.query(
                "SELECT machine_action, machine_approved, judgment_fingerprint, "
                "judgment_payload_json "
                "FROM links WHERE row_key='jordan-bravo'"
            )[0]

        judge_batch.assert_called_once()
        self.assertIs(judge_batch.call_args.kwargs["use_llm"], True)
        self.assertEqual((manifest.errors, manifest.needs_review), (1, 1))
        self.assertEqual(receipt["verdict"], {})
        self.assertEqual(receipt["error"], "TimeoutError: exhausted retries")
        self.assertEqual(
            tuple(link),
            ("verify", None, "failed-judge-fingerprint", "{}"),
        )


if __name__ == "__main__":
    unittest.main()


class HydrateProfilesTests(unittest.TestCase):
    """The one home for prefer-cache-always-retrieve."""

    def test_projection_wrapper_counts_keyless_cache_states(self):
        results = {
            "cached": {"state": rapid.PROFILE_CONTENT},
            "empty": {"state": rapid.PROFILE_EMPTY},
            "unknown": {"state": rapid.PROFILE_ERROR},
        }
        targets = [
            ProfileTarget(
                public_identifier,
                f"https://www.linkedin.com/in/{public_identifier}",
            )
            for public_identifier in results
        ]
        with (
            mock.patch.object(profile_projection, "provider_key_available", return_value=False),
            mock.patch.object(
                profile_projection.rapidapi_client,
                "rapidapi_profile",
                side_effect=lambda public_identifier, _url, **_kwargs: results[public_identifier],
            ),
        ):
            hydrated = profile_projection.hydrate_profiles(targets, Path("unused"))

        self.assertEqual(
            (hydrated.wanted, hydrated.ok, hydrated.failed, hydrated.skipped_no_key),
            (3, 1, 1, 1),
        )
        self.assertEqual(
            {key: value.to_payload() for key, value in hydrated.profiles.items()},
            results,
        )

    def test_keyless_skips_without_fetching(self):
        with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""):
            counts = rapid.hydrate_profiles([("jordan-bravo", "https://x")], Path("unused"))
        self.assertEqual(counts, {"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1})

    def test_counts_ok_and_failed(self):
        calls = []

        def fake(self, pub, url, *, cache_dir=None, **kw):
            calls.append(pub)
            state = rapid.PROFILE_CONTENT if pub == "good" else rapid.PROFILE_EMPTY
            return {"state": state, "normalized_profile": {}}

        with (
            mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value="k"),
            mock.patch.object(rapid.RapidApiClient, "__init__", return_value=None),
            mock.patch.object(rapid.RapidApiClient, "get_profile", fake),
        ):
            counts = rapid.hydrate_profiles(
                [("good", "https://a"), ("bad", "https://b"), ("", "https://c")], Path("unused")
            )
        self.assertEqual(counts["wanted"], 2)  # the empty public_identifier is dropped
        self.assertEqual((counts["ok"], counts["failed"]), (1, 1))
        self.assertEqual(sorted(calls), ["bad", "good"])


class RetargetProposalHydrationTests(unittest.TestCase):
    """The retarget judge must see the REAL profile, not Parallel's payload."""

    def test_cleared_retarget_hydrates_settles_then_realizes_offline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            db = profile_db(root)
            result = ResearchResult.from_payload(
                {
                    "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                    "positions": [
                        {"title": "Founder", "company_name": "Bravo Robotics"},
                    ],
                    "social": {
                        "linkedin_url": "https://www.linkedin.com/in/jordan-correct",
                    },
                    "metadata": {"research_notes": "matched employer"},
                }
            )
            profile_result = {
                "state": "content",
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "experiences": [
                        {"title": "Founder", "company_name": "Bravo Robotics"},
                    ],
                    "education": [],
                },
                "data": {
                    "full_name": "Jordan Bravo",
                    "public_identifier": "jordan-correct",
                    "experiences": [
                        {"title": "Founder", "company_name": "Bravo Robotics"},
                    ],
                },
                "from_cache": False,
            }
            hydrated: list[ProfileTarget] = []

            def hydrate(targets, cache_dir, *, db, **_kwargs):
                hydrated.extend(targets)
                parsed = {
                    target.public_identifier: ProfileResult.from_payload(
                        target.public_identifier or "",
                        target.linkedin_url or "",
                        profile_result,
                    )
                    for target in targets
                    if target.public_identifier
                }
                profile_projection.project_profile_results(
                    db,
                    [(target, parsed[target.public_identifier]) for target in targets],
                    cache_dir,
                )
                return ProfileHydration(len(targets), len(targets), 0, 0, parsed)

            subset = [enrichment_row()]
            verdict = {
                "verdict": "confirmed",
                "confidence": 0.91,
                "reason": "matched employer",
            }
            with (
                mock.patch.object(
                    profile_projection,
                    "hydrate_profiles",
                    side_effect=hydrate,
                ),
                mock.patch.object(
                    judge,
                    "judge_batch",
                    return_value=[judge_result(verdict)],
                ),
            ):
                judging.propose_retargets(
                    subset,
                    db=db,
                    profile_cache_dir=cache,
                    provided_results={"jordan-bravo-p": result},
                )

            self.assertEqual(
                [target.public_identifier for target in hydrated],
                ["jordan-correct"],
            )
            decision = db.query("SELECT machine_action, machine_approved FROM links WHERE row_key='jordan-bravo'")[0]
            self.assertEqual(tuple(decision), ("retarget", "auto"))

            out = root / "retarget.csv"
            with mock.patch.object(
                profile_projection,
                "hydrate_profiles",
                side_effect=AssertionError("realize must not hydrate profiles"),
            ):
                realized = ApplyRetargets(
                    db=db,
                    profile_cache_dir=cache,
                    out_csv=out,
                ).run()

            self.assertEqual((realized["approved_retargets"], realized["rows"]), (1, 1))
            self.assertEqual(
                CsvIO.read_dict_rows(out)[0]["public_identifier"],
                "jordan-correct",
            )

    def test_cached_profile_replaces_the_research_view(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, cache = base / "research", base / "facts", base / "raw", base / "cache"
            for p in (out, facts, raw, cache):
                p.mkdir(parents=True, exist_ok=True)
            (out / "jordan-bravo-p").mkdir()
            # Parallel found the URL but returned NO positions — the bug's shape.
            (out / "jordan-bravo-p" / "01_research_parallel.json").write_text(
                json.dumps(
                    {
                        "person": {"full_name": "Jordan Bravo", "confidence": 0.9, "notes": "found via web"},
                        "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
                        "positions": [],
                        "education": [],
                        "metadata": {"research_notes": "confirmed by employer page"},
                    }
                )
            )
            (facts / "pid-1.jsonl").write_text(
                json.dumps({"chunk_index": 0, "usage": {}, "facts": {"canonical_name": "Jordan Bravo"}}) + "\n"
            )
            # The real profile is projected before any judge consumes it.
            profile_path = profile_cache_path(cache, "jordan-bravo")
            profile_payload = {
                "raw_response": {},
                "normalized_profile": {
                    "success": True,
                    "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                    "education": [],
                    "city": "SF",
                    "state": "",
                    "country": "",
                },
            }
            profile_path.write_text(json.dumps(profile_payload))
            subset = [enrichment_row(row_key="jordan-old")]
            seen = {}
            db = Db(base / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-1",
                person_id="pid-1",
                row_key="jordan-old",
                name="Jordan Bravo",
                machine_worth="maybe",
                linkedin_url="https://www.linkedin.com/in/jordan-old",
            )
            db.project_rows(
                (
                    ArtifactRow(
                        "profile:jordan-old",
                        ArtifactKind.PROFILE.value,
                        "parent-1",
                        str(profile_path),
                        hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                        ProjectionStatus.PROJECTED.value,
                        candidate_key="jordan-old",
                        payload_json=json.dumps(profile_payload),
                    ),
                )
            )
            queue_row = ResearchQueueRow(
                parent_id="parent-1",
                candidate_exists=True,
                row_key="jordan-old",
                handle="jordan-bravo-p",
                source_parent_slug="jordan-bravo-p",
                source_person_ids=("pid-1",),
                source_candidate_public_identifier="jordan-old",
                display_name="Jordan Bravo",
                source_channel=ContactChannel.EMAIL,
            )
            db.project_rows(
                projection.research_artifact_projections(
                    SimpleNamespace(
                        db=db,
                        output_dir=out,
                        rows=(queue_row,),
                        selection_fingerprint="",
                    )
                )
            )

            def capture(tasks, **kw):
                seen.update(tasks[0].linkedin.as_judge_dict())
                return [
                    judge_result(
                        {
                            "verdict": "confirmed",
                            "confidence": 0.9,
                            "reason": "ok",
                        }
                    )
                ]

            with (
                mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""),
                mock.patch.object(judge, "judge_batch", capture),
            ):
                judging.propose_retargets(subset, db=db, profile_cache_dir=cache)

        # The judge saw the cached profile's experiences, not Parallel's empty positions.
        self.assertTrue(seen.get("has_profile"))
        self.assertIn("Bravo Robotics", " ".join(seen.get("experiences") or []))

    def test_cleared_retargets_stay_settled_without_rejudging(self):
        for mode in ("cached", "grandfathered"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "cache"
                db = profile_db(root)
                result = ResearchResult.from_payload(
                    {
                        "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                        "positions": [
                            {"title": "Founder", "company_name": "Bravo Robotics"},
                        ],
                        "social": {
                            "linkedin_url": "https://www.linkedin.com/in/jordan-correct",
                        },
                        "metadata": {"research_notes": "matched employer"},
                    }
                )
                profile_result = {
                    "state": "content",
                    "normalized_profile": {
                        "success": True,
                        "full_name": "Jordan Bravo",
                        "experiences": [
                            {"title": "Founder", "company_name": "Bravo Robotics"},
                        ],
                        "education": [],
                    },
                    "data": {
                        "full_name": "Jordan Bravo",
                        "public_identifier": "jordan-correct",
                        "experiences": [
                            {"title": "Founder", "company_name": "Bravo Robotics"},
                        ],
                    },
                    "from_cache": True,
                }
                fingerprint = None
                if mode == "cached":
                    profile_projection.project_profile_results(
                        db,
                        [
                            (
                                ProfileTarget(
                                    "jordan-correct",
                                    result.linkedin_url,
                                    "jordan-bravo",
                                    "parent-1",
                                ),
                                ProfileResult.from_payload("jordan-correct", result.linkedin_url, profile_result),
                            )
                        ],
                        cache,
                    )
                    evidence = DossierEvidence.from_db(db, ("parent-1",))
                    profile = judge.prefer_cached_profile(
                        result.identity_profile(),
                        queue.linkedin_view(
                            IdentityProfileSource(linkedin_url=result.linkedin_url),
                            profile_projection.profile_payloads(db)["jordan-bravo"],
                        ),
                    )
                    fingerprint = judging.proposal_fingerprint(
                        evidence,
                        profile,
                        model="",
                        effort="medium",
                    )
                db.project_rows(
                    (
                        IdentityMachineProjection(
                            "jordan-bravo",
                            machine_action="retarget",
                            machine_approved="auto",
                            machine_proposed_url=result.linkedin_url,
                            machine_proposed_public_identifier="jordan-correct",
                            machine_reject=None,
                            judgment_fingerprint=fingerprint,
                            source=WriterSource.RECONCILE.value,
                        ),
                    )
                )
                subset = [enrichment_row()]

                hydrated: list[ProfileTarget] = []

                def hydrate(targets, cache_dir, *, db, **_kwargs):
                    hydrated.extend(targets)
                    parsed = {
                        target.public_identifier: ProfileResult.from_payload(
                            target.public_identifier or "",
                            target.linkedin_url or "",
                            profile_result,
                        )
                        for target in targets
                        if target.public_identifier
                    }
                    profile_projection.project_profile_results(
                        db,
                        [(target, parsed[target.public_identifier]) for target in targets],
                        cache_dir,
                    )
                    return ProfileHydration(len(targets), len(targets), 0, 0, parsed)

                with (
                    mock.patch.object(
                        profile_projection,
                        "hydrate_profiles",
                        side_effect=hydrate,
                    ),
                    mock.patch.object(
                        judge,
                        "judge_batch",
                        side_effect=AssertionError("cached adoption must not judge"),
                    ),
                ):
                    judging.propose_retargets(
                        subset,
                        db=db,
                            profile_cache_dir=cache,
                        provided_results={"jordan-bravo-p": result},
                    )

                self.assertEqual(
                    [row.public_identifier for row in hydrated],
                    ["jordan-correct"],
                )
                row = db.query(
                    "SELECT machine_action, machine_approved, machine_reject FROM links WHERE row_key='jordan-bravo'"
                )[0]
                self.assertEqual(tuple(row), ("retarget", "auto", None))


class ResearchProposalPolicyTests(unittest.TestCase):
    def test_malformed_nested_research_payload_is_safe_and_round_trips(self):
        payload = {
            "person": ["not", "an", "object"],
            "location": "unknown",
            "social": 42,
            "headline": {"text": ""},
            "positions": [],
            "education": [],
        }

        result = ResearchResult.from_payload(payload)
        profile = result.identity_profile()

        self.assertEqual(result.to_payload(), payload)
        self.assertEqual(
            (profile.full_name, profile.linkedin_url, profile.location),
            ("", "", ""),
        )
        self.assertFalse(profile.has_profile)

    def test_fingerprint_is_shared_by_batch_and_guided_research(self):
        evidence = DossierEvidence(
            name="Jordan Bravo",
            relationship="former colleague",
            employers=("Bravo Robotics",),
        )
        profile = JudgeProfile.from_payload(
            {
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "full_name": "Jordan Bravo",
                "experiences": ["Founder @ Bravo Robotics"],
            }
        )
        batch = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.RESEARCH, "OWNER: Casey", model="gpt-5.2", effort="medium",
        )
        guided = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.RESEARCH, "OWNER: Casey", model="gpt-5.2", effort="medium",
        )
        attached = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "OWNER: Casey", model="gpt-5.2", effort="medium",
        )
        self.assertEqual(batch, guided)
        self.assertNotEqual(batch, attached)

    def test_fingerprint_changes_with_model_and_effort(self):
        """Proves the fix: a model or reasoning-effort swap must miss cache,
        not silently reuse a verdict answered under a different model/effort
        — see identity_reconcile/healing.py's rejudge(), which deliberately
        asks for effort="high" specifically to avoid this."""
        evidence = DossierEvidence(
            name="Jordan Bravo",
            relationship="former colleague",
            employers=("Bravo Robotics",),
        )
        profile = JudgeProfile.from_payload(
            {
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "full_name": "Jordan Bravo",
                "experiences": ["Founder @ Bravo Robotics"],
            }
        )
        medium = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.2", effort="medium",
        )
        high = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.2", effort="high",
        )
        other_model = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.1", effort="medium",
        )
        same_again = judge.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "", model="gpt-5.2", effort="medium",
        )
        self.assertNotEqual(medium, high)
        self.assertNotEqual(medium, other_model)
        self.assertEqual(medium, same_again)

    def test_judge_batch_offline_fingerprint_reflects_model_and_effort(self):
        """End-to-end through the real public entrypoint (not just the hash
        helper): judge_batch's offline/deterministic path must still produce
        a fingerprint that moves when --model or --reasoning-effort does."""
        medium = judge.judge_batch(
            [task()],
            use_llm=False,
            owner_block="",
            model="gpt-5.2",
            effort="medium",
            concurrency=None,
            timeout=30,
            max_retries=0,
        )
        high = judge.judge_batch(
            [task()],
            use_llm=False,
            owner_block="",
            model="gpt-5.2",
            effort="high",
            concurrency=None,
            timeout=30,
            max_retries=0,
        )
        self.assertNotEqual(medium[0].fingerprint, high[0].fingerprint)

    def test_batch_uses_one_client_and_one_event_loop(self):
        client = mock.MagicMock()
        client.close = mock.AsyncMock()
        judge_identity = mock.AsyncMock(
            side_effect=[
                IdentityJudgeResult(
                    IdentityVerdict.from_payload(
                        {
                            "verdict": "confirmed",
                            "confidence": 0.9,
                        }
                    ),
                    IdentityUsage(),
                    "",
                    "fixture-confirmed-fingerprint",
                ),
                IdentityJudgeResult(
                    IdentityVerdict.from_payload(
                        {
                            "verdict": "wrong_person",
                            "confidence": 0.9,
                        }
                    ),
                    IdentityUsage(),
                    "",
                    "fixture-wrong-fingerprint",
                ),
            ]
        )
        progress = []
        with (
            mock.patch.object(
                openai_responses,
                "AsyncOpenAI",
                return_value=client,
            ) as make,
            # Patched on the class that defines it, not on a module-level
            # wrapper — judge_batch builds one IdentityJudge for the batch.
            mock.patch.object(judge.IdentityJudge, "judge_identity", judge_identity),
        ):
            results = judge.judge_batch(
                [task(), replace(task(), name="Casey Delta")],
                use_llm=True,
                owner_block="",
                model="fixture-model",
                effort="medium",
                concurrency=2,
                timeout=30,
                max_retries=1,
                on_done=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(
            [row.verdict.value for row in results if row.verdict],
            [
                "confirmed",
                "wrong_person",
            ],
        )
        make.assert_called_once()
        self.assertEqual(make.call_args.kwargs["timeout"], 30)
        self.assertEqual(make.call_args.kwargs["max_retries"], 1)
        self.assertEqual(judge_identity.await_count, 2)
        client.close.assert_awaited_once()
        self.assertEqual(progress, [(1, 2), (2, 2)])

    def proposal(self, prior):
        return judging.prepare_research_proposal(
            row_key="jordan-old",
            new_url="https://www.linkedin.com/in/jordan-new",
            dossier=DossierEvidence(
                name="Jordan Bravo",
                relationship="former colleague",
            ),
            profile=JudgeProfile.from_payload(
                {
                    "linkedin_url": "https://www.linkedin.com/in/jordan-new",
                }
            ),
            name="Jordan Bravo",
            confidence=0.9,
            unverified=False,
            reason="matched employer",
            source="deep-research",
            prior=prior,
            model="fixture-model",
            effort="medium",
        )

    def test_exact_fingerprint_reuses_existing_retarget_verdict(self):
        initial = self.proposal(None)
        cached = self.proposal(
            ReviewExportRow(
                key="jordan-old",
                action="retarget",
                llm_judge_fingerprint=initial.proposal.judge_fingerprint,
            )
        )
        self.assertEqual(cached.disposition, "cached")
        self.assertIsNone(cached.task)

    def test_legacy_retarget_to_same_url_is_grandfathered(self):
        prepared = self.proposal(
            ReviewExportRow(
                key="jordan-old",
                action="retarget",
                new_linkedin_url="https://www.linkedin.com/in/jordan-new",
            )
        )
        self.assertEqual(prepared.disposition, "grandfathered")
        self.assertIsNone(prepared.task)

    def test_matching_fingerprint_is_reused_whatever_the_prior_verdict_said(self):
        """A bought verdict is bought, whichever way it went.

        The cached test used to also require action == "retarget", which only
        a CLEARED proposal ever reaches — so a rejected or human-resolved row
        re-entered the paid queue on byte-identical input every single pass.
        Skipping cannot lose a human decision: the row is left untouched, and
        IdentityPolicy.effective_decision already ranks human over machine.
        """
        initial = self.proposal(None)
        for prior_action in ("verify", "detach", "review"):
            with self.subTest(prior_action=prior_action):
                prepared = self.proposal(
                    ReviewExportRow(
                        key="jordan-old",
                        action=prior_action,
                        llm_judge_fingerprint=initial.proposal.judge_fingerprint,
                    )
                )
                self.assertEqual(prepared.disposition, "cached")
                self.assertIsNone(prepared.task)


class ResearchSelectionTests(unittest.TestCase):
    def test_guided_research_creates_missing_bare_person_candidate(self):
        class Provider:
            def __init__(self, *_args, **_kwargs):
                pass

            def execute(self, inputs, _params, on_status):
                handle = inputs[0].handle
                on_status(ProviderStatusCounts.from_payload({"completed": 1}))
                payload = {
                    "real_name": "Jordan Bravo",
                    "name_confidence": 0.9,
                    "name_evidence": "official profile",
                    "work_experience": "[]",
                    "education": "[]",
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    "summary": "Founder",
                    "research_notes": "matched",
                }
                return ParallelExecutionResult(
                    1,
                    ((handle, ParallelProviderResult.from_payload(payload)),),
                    (),
                    ProviderGroupStatus.from_payload({"is_active": False}),
                )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-1",
                person_id="person-a",
                row_key="unused",
                name="Jordan Bravo",
                machine_worth="yes",
                display_slug="jordan-bravo",
                include_link=False,
            )
            request = GuidanceRequest(
                "jordan-bravo",
                "person-a",
                "Jordan Bravo",
                "Find the founder",
                person_ids=("person-a",),
            )
            with (
                mock.patch.object(driver, "_api_key", return_value="test-key"),
                mock.patch.object(driver.parallel_client, "ParallelClient", Provider),
            ):
                result = GuidedResearch(
                    db,
                    research_dir=root / "research",
                ).research(request)

            link = db.query("SELECT parent_id, kind, public_identifier FROM links WHERE row_key='person-a'")
            artifact = db.query("SELECT candidate_key FROM artifacts WHERE artifact_key='research:jordan-bravo'")

        self.assertEqual(
            result.research_result.linkedin_url,
            "https://www.linkedin.com/in/jordan-bravo",
        )
        self.assertEqual(
            [tuple(row) for row in link],
            [("parent-1", "research", "jordan-bravo")],
        )
        self.assertEqual([tuple(row) for row in artifact], [("person-a",)])

    def test_guided_apply_with_no_linkedin_url_records_no_match(self):
        """apply_provider_result takes a real GuidedProviderResult (its typed
        parameter); a research result that found no LinkedIn URL records a
        no_match outcome without needing to touch `parent` at all."""
        with TemporaryDirectory() as directory:
            db = profile_db(Path(directory))
            request = GuidanceRequest("parent-1", "jordan-bravo", "Jordan Bravo", "Find the founder")
            result = GuidedProviderResult(
                "no LinkedIn found",
                ResearchResult.from_payload({}),
            )

            outcome = GuidedResearch(db).apply_provider_result("parent-1", {}, request, result)

        self.assertEqual(outcome.state, "no_match")
        self.assertEqual(outcome.detail, "no LinkedIn found")

    def test_batch_and_guided_use_parent_id_when_display_slug_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-1",
                person_id="pid-1",
                row_key="jordan-old",
                name="Jordan Bravo",
                machine_worth="yes",
                parent_public_identifier="public-fallback",
                display_slug="",
                linkedin_url="https://www.linkedin.com/in/jordan-old",
                human_worth="yes",
                link_updates={
                    "machine_judgment": "wrong_person",
                    "machine_confidence": 0.9,
                    "judgment_payload_json": json.dumps({"recommend_deep_research": True}),
                },
            )
            batch = selection.select_research(
                db,
                processor="core2x",
                confirm_threshold=0.8,
                include_plausibly_absent=False,
                include_candidates=False,
                fingerprint=ReviewSelection("fixture", 0, 0, 0, 0, ""),
            )
            parent = person_detail(db, "parent-1")
            self.assertIsNotNone(parent)
            request = GuidanceRequest(
                "parent-1",
                "jordan-old",
                "Jordan Bravo",
                "Try the founder",
                person_ids=("pid-1",),
            )
            guided = GuidedResearch(db).research_row(request, parent)

        self.assertEqual(len(batch.queue), 1)
        self.assertEqual(batch.queue[0].handle, "parent-1")
        self.assertEqual(guided.handle, "parent-1")

    def test_supplied_fingerprint_does_not_requery_workflow_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            with mock.patch.object(
                selection,
                "workflow_state",
                side_effect=AssertionError("selection was already supplied"),
            ):
                result = selection.select_research(
                    db,
                    processor="core2x",
                    confirm_threshold=0.8,
                    include_plausibly_absent=True,
                    include_candidates=True,
                    fingerprint=ReviewSelection("fixture-selection", 0, 0, 0, 0, ""),
                )
        self.assertEqual(result.fingerprint.fingerprint, "fixture-selection")


class IdentityVerdictReuseTests(unittest.TestCase):
    """A verdict already bought for this exact input is not bought again."""

    def _stored(self, value: str = "confirmed", fingerprint: str = "fp-1"):
        return judgment_policy.StoredJudgment(
            IdentityVerdict.from_payload({"verdict": value, "confidence": 0.9}),
            fingerprint,
        )

    def test_same_input_reuses(self):
        self.assertTrue(
            judgment_policy.reuses_stored_verdict(self._stored(), "fp-1", force=False)
        )

    def test_changed_evidence_moves_the_fingerprint_and_pays(self):
        self.assertFalse(
            judgment_policy.reuses_stored_verdict(self._stored(), "fp-2", force=False)
        )

    def test_never_judged_pays(self):
        """A row with no verdict on file holds fingerprint "", which matches nothing."""
        self.assertFalse(judgment_policy.reuses_stored_verdict(None, "fp-1", force=False))

    def test_unreadable_stored_verdict_pays_rather_than_pinning_the_row(self):
        """`from_payload` accepts a missing "verdict" key as "". Reusing that would
        match forever and the row would never be judged again."""
        self.assertFalse(
            judgment_policy.reuses_stored_verdict(self._stored(value=""), "fp-1", force=False)
        )

    def test_force_pays_even_on_an_exact_match(self):
        self.assertFalse(
            judgment_policy.reuses_stored_verdict(self._stored(), "fp-1", force=True)
        )


class HumanSettledRowsAreNotJudgedTests(unittest.TestCase):
    """A row you already answered never reaches the judge.

    settle_machine_identities discards a fresh machine verdict for a
    human-decided row, so judging one is spend whose result is thrown away by
    design. On the owner's store that was 24 rows re-billed on every run.
    """

    def test_a_human_decided_row_leaves_the_queue_but_is_still_counted(self):
        with TemporaryDirectory() as directory:
            db = Db(Path(directory) / "deep-context.sqlite")
            seed_identity(
                db,
                parent_id="parent-1",
                person_id="person-1",
                row_key="jordan-bravo",
                name="Jordan Bravo",
                machine_worth="yes",
                linkedin_url="https://www.linkedin.com/in/jordan-bravo",
            )
            before = len(attached_identity_queue(db))
            self.assertEqual((before, human_settled_identities(db)), (1, 0))

            db.decide_identity("jordan-bravo", "detach", approved="yes")

            self.assertEqual(len(attached_identity_queue(db)), 0)
            self.assertEqual(human_settled_identities(db), 1)
