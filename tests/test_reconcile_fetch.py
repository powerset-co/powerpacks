"""Offline tests for LinkedIn identity evidence: the cached profile view,
profile hydration counts, research-proposed retargets, and verdict reuse by
fingerprint.

The RapidAPI client is mocked where profiles.projection binds it and the judge
where judging binds it; everything else runs for real against synthetic
fixtures.

Changelog:
- 2026-09-25: the standalone reconcile node's fetch/keyless/CLI tests left with
  the node; the remaining suites cover code the review UI still uses.
"""

import json
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from parallel.types import TaskGroupStatus, TaskRunJsonOutput

from packs.ingestion.primitives.deep_context.db.identity_queries import stored_judgments
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import StoredJudgment

from packs.ingestion.primitives.deep_context.enrich.profiles import projection as profile_projection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    IdentityMachineProjection,
    ProjectionStatus,
    IdentityOrigin,
    LinkRow,
    ResearchRow,
    ResearchStatus,
    ReviewExportRow,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.shared import openai_responses
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver, projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
)
import packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue as queue
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidedResearch
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
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
from packs.ingestion.primitives.deep_context.enrich.profiles.models import (
    ProfileHydration,
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.enrich import rapidapi_client as rapid
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path
from packs.shared.csv_io import CsvIO
from deep_context_sqlite_test_helpers import seed_identity
from deep_context_sqlite_test_helpers import stub_identity_judge


def task(
    pub="jordan-bravo",
    url="https://www.linkedin.com/in/jordan-bravo",
    has_profile=False,
):
    return IdentityTask(
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


def stub_mapped_identity_judge(answer):
    def results(tasks, **kwargs):
        return [IdentityJudgeResult(IdentityVerdict.from_payload(answer), IdentityUsage(), '', 'fixture-jev')
                for task in tasks]
    return mock.patch.object(judging.jev_judge, 'judge_batch', side_effect=results)


class MappedCandidateJudgeTests(unittest.TestCase):
    def test_failed_profile_is_not_judged_and_remains_retryable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            db = profile_db(root)
            target = ProfileTarget('jordan-bravo', 'https://www.linkedin.com/in/jordan-bravo',
                                   'jordan-bravo', 'parent-1')
            failed = ProfileResult.from_payload(target.public_identifier, target.linkedin_url, {
                'state': 'error', 'status_code': 503, 'detail': 'fetch failed (503)',
                'normalized_profile': {},
            })
            profile_projection.project_profile_results(db, ((target, failed),), root / 'cache')
            with stub_mapped_identity_judge({'verdict': 'confirmed', 'confidence': .99, 'reason': 'matched'}) as judge_call:
                self.assertEqual(judging.judge_mapped_candidates(db).judge_calls, 0)
                judge_call.assert_not_called()
                self.assertIsNone(db.query('SELECT machine_judgment FROM links')[0][0])
                recovered = ProfileResult.from_payload(target.public_identifier, target.linkedin_url, {
                    'state': 'content', 'normalized_profile': {'success': True,
                        'full_name': 'Jordan Bravo', 'experiences': [{'title': 'Founder', 'company_name': 'Example'}]},
                })
                profile_projection.project_profile_results(db, ((target, recovered),), root / 'cache')
                self.assertEqual(judging.judge_mapped_candidates(db).judge_calls, 1)

    def test_research_only_person_stays_reviewable_after_uncertain_verdict(self) -> None:
        from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_queue

        with TemporaryDirectory() as temp:
            db = Db(Path(temp) / "deep-context.sqlite")
            seed_identity(db, parent_id="parent-1", person_id="person-jordan",
                          row_key="unused", name="Jordan Bravo", machine_worth="yes",
                          include_link=False)
            result = current_research_result()
            row = ResearchQueueRow(
                parent_id="parent-1", candidate_exists=False, row_key="research:parent-1",
                handle="jordan-bravo", source_person_ids=("person-jordan",),
                display_name="Jordan Bravo", bio="", known_info="", primary_email="",
                retarget_hint="",
            )
            data = json.dumps(result.output.model_dump(mode="json")).encode()
            path = Path(temp) / "00_parallel_result.json"
            path.write_bytes(data)
            db.project_rows((projection.research_artifact_projection(
                ResearchRunParams(output_dir=Path(temp), rows=(row,), db=db), row, result, path, data
            ),))
            with stub_mapped_identity_judge({"verdict": "needs_review", "confidence": 0.6, "reason": "uncertain"}):
                self.assertEqual(judging.judge_mapped_candidates(db).judge_calls, 1)
            self.assertEqual([candidate.row_key for parent in linkedin_queue(db)
                              for candidate in parent.candidates], [row.row_key])

    def test_researched_raw_candidate_enters_prefetch_and_judge(self) -> None:
        from packs.ingestion.primitives.deep_context.db.identity_views import judge_candidates, linkedin_queue
        from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles

        with TemporaryDirectory() as temp:
            db = Db(Path(temp) / "deep-context.sqlite")
            seed_identity(
                db, parent_id="parent-1", person_id="candidate:email:jordan@example.test",
                row_key="candidate:email:jordan@example.test", name="Jordan Bravo",
                machine_worth="yes", linkedin_url="", kind=RowKind.CANDIDATE_EMAIL.value,
                link_updates={"candidate_origin": True, "raw_import": True},
            )
            result = current_research_result(linkedin_url="https://linkedin.com/in/Jordan-Researched/?trk=x")
            row = ResearchQueueRow(
                parent_id="parent-1", candidate_exists=True,
                row_key="candidate:email:jordan@example.test", handle="jordan-bravo",
                source_person_ids=("candidate:email:jordan@example.test",),
                display_name="Jordan Bravo", bio="", known_info="",
                primary_email="jordan@example.test", retarget_hint="",
            )
            data = json.dumps(result.output.model_dump(mode="json")).encode()
            path = Path(temp) / "00_parallel_result.json"
            path.write_bytes(data)
            params = ResearchRunParams(output_dir=Path(temp), rows=(row,), db=db)
            db.project_rows((projection.research_artifact_projection(params, row, result, path, data),))
            self.assertEqual([item.row_key for item in judge_candidates(db)], [row.row_key])
            self.assertEqual([item.parent_id for item in linkedin_queue(db)], ["parent-1"])
            prefetch = PrefetchProfiles(db=db, profile_cache_dir=Path(temp)).run()
            self.assertGreater(prefetch.queue_links, 0)
            with stub_mapped_identity_judge({"verdict": "confirmed", "confidence": 0.94, "reason": "same work"}):
                judged = judging.judge_mapped_candidates(db)
            self.assertEqual(judged.judge_calls, 1)
            self.assertEqual(db.query("SELECT machine_proposed_url FROM links WHERE row_key=?", (row.row_key,))[0][0],
                             "https://www.linkedin.com/in/jordan-researched")
            self.assertEqual(judging.judge_mapped_candidates(db).judge_calls, 0)

    def test_attached_verdict_is_persisted_once_and_human_choice_wins(self) -> None:
        with TemporaryDirectory() as temp:
            db = profile_db(Path(temp))
            answer = {"verdict": "confirmed", "confidence": 0.94, "reason": "same work"}
            with stub_mapped_identity_judge(answer):
                first = judging.judge_mapped_candidates(db)
                second = judging.judge_mapped_candidates(db)
            self.assertEqual((first.judge_calls, second.judge_calls), (1, 0))
            row = db.query("SELECT machine_action, machine_approved FROM links WHERE row_key='jordan-bravo'")[0]
            self.assertEqual(tuple(row), ("verify", "auto"))
            db.decide_identity("jordan-bravo", "detach")
            with stub_mapped_identity_judge(answer):
                after_human = judging.judge_mapped_candidates(db)
            self.assertEqual(after_human.judge_calls, 0)

    def test_malformed_machine_verdict_is_retried(self) -> None:
        with TemporaryDirectory() as temp:
            db = profile_db(Path(temp))
            with db.transaction() as conn:
                conn.execute("UPDATE links SET judgment_fingerprint='failed', judgment_payload_json='{}'")
            with stub_mapped_identity_judge({"verdict": "needs_review", "confidence": 0.6, "reason": "uncertain"}):
                outcome = judging.judge_mapped_candidates(db)
            self.assertEqual(outcome.judge_calls, 1)

    def test_each_valid_verdict_skips_even_without_fingerprint(self) -> None:
        for verdict in ("confirmed", "wrong_person", "needs_review"):
            with self.subTest(verdict=verdict), TemporaryDirectory() as temp:
                db = profile_db(Path(temp))
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE links SET judgment_fingerprint='', judgment_payload_json=?",
                        (json.dumps({"verdict": verdict, "confidence": 0.7}),),
                    )
                self.assertEqual(judging.judge_mapped_candidates(
                    db
                ).judge_calls, 0)


def current_research_result(
    *,
    linkedin_url: str = "https://www.linkedin.com/in/jordan-correct",
    real_name: str = "Jordan Bravo",
    positions: list[dict[str, object]] | None = None,
    reason: str = "matched employer",
) -> ResearchResult:
    return ResearchResult.from_output(TaskRunJsonOutput(
        type="json",
        content={
            "real_name": real_name,
            "work_experience": positions if positions is not None else [],
            "education": [],
            "location_city": "",
            "location_country": "",
            "linkedin_url": linkedin_url,
            "github_url": "",
            "summary": "",
        },
        basis=[{"field": "linkedin_url", "reasoning": reason, "citations": []}],
    ))


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


class LinkedinViewTests(unittest.TestCase):
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
            "012e36347158045a6644624a1b2fa7c7ad356e8e57ebb4aea8e63f2dd6c1c864",
        )

    def test_failed_cache_is_not_judgeable(self):
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
        self.assertEqual(profile.public_identifier, "jordan-bravo")
        self.assertFalse(profile.has_profile)



if __name__ == "__main__":
    unittest.main()


class HydrateProfilesTests(unittest.TestCase):
    """The one home for prefer-cache-always-retrieve."""

    def test_projection_wrapper_counts_keyless_cache_states(self):
        results = {
            "cached": {
                "state": rapid.PROFILE_CONTENT,
                "normalized_profile": {
                    "success": True,
                    "experiences": [{"title": "Founder", "company_name": "Example"}],
                },
                "from_cache": True,
                "fetched": False,
            },
            "empty": {
                "state": rapid.PROFILE_EMPTY,
                "normalized_profile": {"success": False},
                "from_cache": True,
                "fetched": False,
            },
            "unknown": {
                "state": rapid.PROFILE_ERROR,
                "normalized_profile": {},
                "from_cache": False,
                "fetched": False,
            },
        }
        targets = [
            ProfileTarget(
                public_identifier,
                f"https://www.linkedin.com/in/{public_identifier}",
            )
            for public_identifier in results
        ]
        with (
            mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""),
            mock.patch.object(
                rapid.RapidApiClient,
                "get_profile",
                side_effect=lambda public_identifier, _url, **_kwargs: results[public_identifier],
            ),
        ):
            hydrated = profile_projection.hydrate_profiles(targets, Path("unused"))

        self.assertEqual(
            (hydrated.wanted, hydrated.ok, hydrated.failed, hydrated.skipped_no_key),
            (3, 1, 0, 1),
        )
        self.assertEqual(
            {key: value.state for key, value in hydrated.profiles.items()},
            {key: value["state"] for key, value in results.items()},
        )

    def test_keyless_skips_without_fetching(self):
        with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""):
            counts = rapid.hydrate_profiles([("jordan-bravo", "https://x")], Path("unused"))
        self.assertEqual(counts, {"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1})

    def test_counts_ok_and_failed(self):
        calls = []

        def fake(self, pub, url, *, cache_dir=None, **kw):
            calls.append(pub)
            state = rapid.PROFILE_CONTENT if pub == "good" else rapid.PROFILE_ERROR
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
            result = current_research_result(
                positions=[{"title": "Founder", "company_name": "Bravo Robotics"}],
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

            db.replace_imported_people((PeopleRow(
                id="pid-1", full_name="Jordan Bravo", public_identifier="jordan-bravo",
                linkedin_url="https://www.linkedin.com/in/jordan-bravo",
            ),))
            with mock.patch.object(
                profile_projection,
                "hydrate_profiles",
                side_effect=AssertionError("realize must not hydrate profiles"),
            ):
                realized = ExportPeople(db=db, out_dir=root / "merged").run()

            self.assertEqual((realized["accepted_identities"], realized["rows"]), (1, 1))
            (row,) = CsvIO.read_dict_rows(root / "merged" / "people.csv")
            self.assertEqual(row["public_identifier"], "jordan-correct")
            self.assertEqual(json.loads(row["work_experiences"])[0]["title"], "Founder")

    def test_judge_error_does_not_persist_a_reusable_proposal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = profile_db(root)
            result = current_research_result(
                positions=[{"title": "Founder", "company_name": "Bravo Robotics"}],
            )
            judge_error = IdentityJudgeResult(
                verdict=None,
                usage=IdentityUsage(),
                error="judge timed out",
                fingerprint="failed-fingerprint",
            )
            with (
                mock.patch.object(
                    profile_projection,
                    "hydrate_profiles",
                    return_value=ProfileHydration(1, 0, 0, 1, {}),
                ),
                mock.patch.object(judge, "judge_batch", return_value=[judge_error]),
            ):
                run = judging.propose_retargets(
                    [enrichment_row()],
                    db=db,
                    profile_cache_dir=root / "cache",
                    provided_results={"jordan-bravo-p": result},
                )

            self.assertEqual(run.judge_errors, 1)
            self.assertEqual(run.proposed, 0)
            link = db.query(
                "SELECT machine_action, judgment_fingerprint, judgment_payload_json "
                "FROM links WHERE row_key='jordan-bravo'"
            )[0]
            self.assertEqual(tuple(link), (None, None, None))

    def test_parent_research_result_applies_to_each_candidate_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = profile_db(root)
            db.project_rows((
                LinkRow(
                    "jordan-bravo-second",
                    "parent-1",
                    "jordan-bravo-second",
                    RowKind.PUB.value,
                    linkedin_url="https://www.linkedin.com/in/jordan-bravo-second",
                    candidate_origin=True,
                    source=WriterSource.RECONCILE.value,
                ),
            ))
            result = current_research_result(
                positions=[{"title": "Founder", "company_name": "Bravo Robotics"}],
            )
            # The paid result is parent-level. Its legacy candidate_key points
            # at just one sibling and must not hide it from the other sibling.
            db.project_rows((
                ResearchRow(
                    "jordan-bravo-p",
                    "parent-1",
                    ResearchStatus.COMPLETE.value,
                    candidate_key="jordan-bravo",
                    result_json=result.output.model_dump_json(exclude_none=True),
                ),
            ))
            rows = [
                enrichment_row(),
                enrichment_row(row_key="jordan-bravo-second"),
            ]
            verdict = {
                "verdict": "confirmed",
                "confidence": 0.91,
                "reason": "matched employer",
            }
            with (
                mock.patch.object(
                    profile_projection,
                    "hydrate_profiles",
                    return_value=ProfileHydration(2, 0, 0, 2, {}),
                ),
                mock.patch.object(
                    judge,
                    "judge_batch",
                    return_value=[judge_result(verdict), judge_result(verdict)],
                ) as judge_batch,
            ):
                run = judging.propose_retargets(
                    rows,
                    db=db,
                    profile_cache_dir=root / "cache",
                )

            self.assertEqual(run.proposed, 2)
            self.assertEqual(len(judge_batch.call_args.args[0]), 2)

    def test_cached_profile_replaces_the_research_view(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, cache = base / "research", base / "facts", base / "raw", base / "cache"
            for p in (out, facts, raw, cache):
                p.mkdir(parents=True, exist_ok=True)
            (out / "jordan-bravo-p").mkdir()
            # Parallel found the URL but returned NO positions — the bug's shape.
            provider_result = current_research_result(
                linkedin_url="https://www.linkedin.com/in/jordan-bravo",
                reason="confirmed by employer page",
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
                source_person_ids=("pid-1",),
                display_name="Jordan Bravo",
            )
            result_path = out / "jordan-bravo-p" / "00_parallel_result.json"
            result_data = (
                json.dumps(provider_result.output.model_dump(mode="json"), sort_keys=True)
                + "\n"
            ).encode()
            result_path.write_bytes(result_data)
            params = ResearchRunParams(db=db, output_dir=out, rows=(queue_row,))
            db.project_rows((projection.research_artifact_projection(
                params, queue_row, provider_result, result_path, result_data
            ),))

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
                result = current_research_result(
                    positions=[{"title": "Founder", "company_name": "Bravo Robotics"}],
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
                            judgment_fingerprint=fingerprint,
                            judgment_payload_json=(
                                json.dumps({
                                    "verdict": "confirmed",
                                    "confidence": 0.91,
                                    "reason": "matched employer",
                                })
                                if mode == "cached"
                                else None
                            ),
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
                    "SELECT machine_action, machine_approved, machine_judgment "
                    "FROM links WHERE row_key='jordan-bravo'"
                )[0]
                self.assertEqual(tuple(row), ("retarget", "auto", None))


class ResearchProposalPolicyTests(unittest.TestCase):
    def test_non_provider_research_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            ResearchResult.from_payload({"person": {"full_name": "Jordan Bravo"}})

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
        not silently reuse a verdict answered under a different model/effort."""
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
                [
                    task(),
                    replace(task(), evidence=DossierEvidence(name="Casey Delta")),
                ],
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

    def proposal(self, prior, stored=None):
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
            reason="matched employer",
            source="deep-research",
            prior=prior,
            stored=stored,
            model="fixture-model",
            effort="medium",
        )

    def test_exact_fingerprint_reuses_existing_retarget_verdict(self):
        initial = self.proposal(None)
        stored = StoredJudgment(
            IdentityVerdict.from_payload({
                "verdict": "confirmed",
                "confidence": 0.9,
                "reason": "matched employer",
            }),
            initial.proposal.judge_fingerprint,
        )
        cached = self.proposal(
            ReviewExportRow(
                key="jordan-old",
                action="retarget",
                llm_judge_fingerprint=initial.proposal.judge_fingerprint,
            ),
            stored,
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
        stored = StoredJudgment(
            IdentityVerdict.from_payload({
                "verdict": "wrong_person",
                "confidence": 0.9,
                "reason": "different person",
            }),
            initial.proposal.judge_fingerprint,
        )
        for prior_action in ("verify", "detach", "review"):
            with self.subTest(prior_action=prior_action):
                prepared = self.proposal(
                    ReviewExportRow(
                        key="jordan-old",
                        action=prior_action,
                        llm_judge_fingerprint=initial.proposal.judge_fingerprint,
                    ),
                    stored,
                )
                self.assertEqual(prepared.disposition, "cached")
                self.assertIsNone(prepared.task)

    def test_matching_fingerprint_without_a_valid_stored_verdict_is_not_cached(self):
        initial = self.proposal(None)
        prepared = self.proposal(
            ReviewExportRow(
                key="jordan-old",
                action="verify",
                llm_judge_fingerprint=initial.proposal.judge_fingerprint,
            )
        )
        self.assertEqual(prepared.disposition, "pending")
        self.assertIsNotNone(prepared.task)


class ResearchSelectionTests(unittest.TestCase):
    def test_guided_research_creates_missing_bare_person_candidate(self):
        class Provider:
            def __init__(self, *_args, **_kwargs):
                pass

            def execute(self, inputs, _params, on_status, on_result):
                handle = str(inputs[0]["metadata"]["handle"])
                final = TaskGroupStatus(is_active=False, num_task_runs=1, task_run_status_counts={"completed": 1})
                on_status(final)
                payload = {
                    "real_name": "Jordan Bravo",
                    "work_experience": [{"title": "Founder", "company_name": "Example", "is_current": True}],
                    "education": [],
                    "location_city": "Oakland",
                    "location_country": "US",
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    "summary": "Founder",
                }
                output = TaskRunJsonOutput(type="json", content=payload, basis=[{
                    "field": "linkedin_url", "reasoning": "official profile", "citations": []
                }])
                on_result(handle, output)
                return ()

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
            result.linkedin_url,
            "https://www.linkedin.com/in/jordan-bravo",
        )
        self.assertEqual(
            [tuple(row) for row in link],
            [("parent-1", "research", "jordan-bravo")],
        )
        self.assertEqual([tuple(row) for row in artifact], [("person-a",)])

    def test_guided_apply_with_no_linkedin_url_records_no_match(self):
        """apply_provider_result takes a typed ResearchResult; a result with no LinkedIn URL records a
        no_match outcome without needing to touch `parent` at all."""
        with TemporaryDirectory() as directory:
            db = profile_db(Path(directory))
            request = GuidanceRequest("parent-1", "jordan-bravo", "Jordan Bravo", "Find the founder")
            result = current_research_result(linkedin_url="", real_name="")

            outcome = GuidedResearch(db).apply_provider_result("parent-1", {}, request, result)

        self.assertEqual(outcome.state, "no_match")
        self.assertEqual(outcome.detail, "deep research: matched employer")

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

        self.assertEqual(len(batch.pending), 0)
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
                    fingerprint=ReviewSelection("fixture-selection", 0, 0, 0, 0, ""),
                )
        self.assertEqual(result.fingerprint.fingerprint, "fixture-selection")


class IdentityVerdictReuseTests(unittest.TestCase):
    """A verdict already bought for this exact input is not bought again."""

    def _stored(self, value: str = "confirmed", fingerprint: str = "fp-1"):
        return StoredJudgment(
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

    def test_malformed_stored_confidence_is_unreadable_and_rejudged(self):
        with TemporaryDirectory() as directory:
            db = profile_db(Path(directory))
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE links SET judgment_fingerprint=?, judgment_payload_json=? "
                    "WHERE row_key='jordan-bravo'",
                    ("fp-1", '{"verdict":"confirmed","confidence":"high"}'),
                )

            self.assertEqual(stored_judgments(db), {})

    def test_force_pays_even_on_an_exact_match(self):
        self.assertFalse(
            judgment_policy.reuses_stored_verdict(self._stored(), "fp-1", force=True)
        )
