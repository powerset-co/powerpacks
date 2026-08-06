"""Offline tests for reconcile's prefer-cache-always-retrieve profile fetch.

The RapidAPI client is mocked where reconcile_linkedin binds it; everything else
(candidate selection, view rebuild from the cache, keyless skip, counts) runs
for real against synthetic fixtures.
"""
import json
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context import identity_evidence, profile_projection
from packs.ingestion.primitives.deep_context.apply_retargets import ApplyRetargets
from packs.ingestion.primitives.deep_context.research_reconcile import judging
from packs.ingestion.primitives.deep_context.research_reconcile import selection
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    IdentityMachineProjection,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    RowKind,
    IdentityOrigin,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parallel_research import driver
import packs.ingestion.primitives.deep_context.identity_reconcile.queue as queue
import packs.ingestion.primitives.deep_context.reconcile_linkedin as reconcile
from packs.ingestion.primitives.deep_context.reconcile_linkedin import ReconcileLinkedin
from packs.ingestion.primitives.deep_context.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.identity_reconcile.guided import GuidedResearch
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.enrich import rapidapi_client as rapid
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path
from packs.shared.csv_io import CsvIO


def task(pub="jordan-bravo", url="https://www.linkedin.com/in/jordan-bravo",
         has_profile=False, no_link=False, from_connections=False, pid="pid-1"):
    return {
        "parent_slug": "jordan-bravo-ab12cd34", "parent_id": "parent-1",
        "name": "Jordan Bravo",
        "candidate_key": pub, "person_ids": [pid],
        "no_link": no_link, "from_connections": from_connections,
        "linkedin": {"public_identifier": pub, "linkedin_url": url,
                     "has_profile": has_profile, "source": "people_csv"},
    }


def profile_db(root: Path) -> Db:
    db = Db(root / "deep-context.sqlite")
    db.project_rows((
        ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
        PersonRow("pid-1", "parent-1", display_name="Jordan Bravo"),
        LinkRow(
            "jordan-bravo",
            "parent-1",
            "jordan-bravo",
            RowKind.PUB.value,
            "https://www.linkedin.com/in/jordan-bravo",
        ),
    ))
    return db


class FetchCandidateTests(unittest.TestCase):
    def test_selects_only_urled_profileless_judge_targets(self):
        rows = [
            task(),                                    # wanted
            task(has_profile=True),                    # already judgeable
            task(no_link=True, url=""),                # nothing attached
            task(from_connections=True),               # ground truth, never judged
            {**task(), "linkedin": {"linkedin_url": "", "has_profile": False}},  # no URL
        ]
        wanted = queue.profile_fetch_candidates(rows)
        self.assertEqual(len(wanted), 1)
        self.assertIs(wanted[0], rows[0])


class FetchMissingProfilesTests(unittest.TestCase):
    def test_keyless_install_skips_cleanly(self):
        with TemporaryDirectory() as directory, mock.patch.object(
            profile_projection.rapidapi_client.RapidApiClient,
            "resolve_key",
            return_value="",
        ):
            root = Path(directory)
            counts = queue.fetch_missing_profiles(
                profile_db(root), [task()], root / "cache"
            )
        self.assertEqual(counts["fetch_skipped_no_key"], 1)
        self.assertEqual(counts["fetch_ok"], 0)

    def test_fetch_hydrates_cache_and_rebuilds_view(self):
        with TemporaryDirectory() as d:
            cache_dir = Path(d)
            t = task()
            db = profile_db(cache_dir)

            def fake_fetch(self, pub, url, *, cache_dir=None, **kw):
                profile = {
                    "success": True, "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                    "education": [], "city": "SF", "state": "", "country": "",
                }
                return {"state": rapid.PROFILE_CONTENT,
                        "status_code": 200, "normalized_profile": profile}

            with mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "resolve_key",
                return_value="k",
            ), mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "__init__",
                return_value=None,
            ), mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "get_profile",
                fake_fetch,
            ):
                counts = queue.fetch_missing_profiles(db, [t], cache_dir)

        self.assertEqual(counts["fetch_ok"], 1)
        self.assertEqual(counts["fetch_failed"], 0)
        self.assertTrue(t["linkedin"]["has_profile"])       # view rebuilt from cache
        self.assertEqual(t["linkedin"]["source"], "cache")
        self.assertIn("Bravo Robotics", " ".join(t["linkedin"]["experiences"]))

    def test_failed_fetch_counts_and_leaves_task_unjudgeable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            t = task()
            with mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "resolve_key",
                return_value="k",
            ), mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "__init__",
                return_value=None,
            ), mock.patch.object(
                profile_projection.rapidapi_client.RapidApiClient,
                "get_profile",
                return_value={
                    "state": rapid.PROFILE_EMPTY,
                    "status_code": 404,
                    "normalized_profile": {},
                },
            ):
                counts = queue.fetch_missing_profiles(
                    profile_db(root), [t], root / "cache"
                )
        self.assertEqual(counts["fetch_failed"], 1)
        self.assertFalse(t["linkedin"]["has_profile"])


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
            db.project_rows((
                ParentRow("parent-1", "jordan-bravo", "Jordan Bravo", "jordan-bravo-p"),
                PersonRow("person-1", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "jordan-bravo", "parent-1", "jordan-bravo", RowKind.PUB.value,
                    "https://www.linkedin.com/in/jordan-bravo", "Jordan Bravo",
                ),
            ))
            facts, raw, cache, output = (
                root / "facts", root / "raw", root / "cache", root / "reconcile"
            )
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
            db.project_rows((ArtifactRow(
                "profile:jordan-bravo",
                ArtifactKind.PROFILE.value,
                "parent-1",
                str(profile_path),
                hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                candidate_key="jordan-bravo",
                payload_json=json.dumps(profile_payload),
            ),))
            verdicts = output / "verdicts.jsonl"
            payload = ReconcileLinkedin(
                db=db,
                profile_cache_dir=cache,
                verdicts_jsonl=verdicts,
                no_llm=True,
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
                "verdict": {
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "supporting_evidence": ["attached profile (offline stub)"],
                    "contradicting_evidence": [],
                    "linkedin_plausibly_absent": False,
                    "recommend_deep_research": False,
                    "reason": "offline stub trusts the attached profile",
                },
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
            {
                "public_identifier": public_identifier,
                "linkedin_url": f"https://www.linkedin.com/in/{public_identifier}",
            }
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
            counts, profiles = profile_projection.hydrate_profiles(
                targets, Path("unused")
            )

        self.assertEqual(
            counts,
            {"wanted": 3, "ok": 1, "failed": 1, "skipped_no_key": 1},
        )
        self.assertEqual(profiles, results)

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

        with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value="k"), \
             mock.patch.object(rapid.RapidApiClient, "__init__", return_value=None), \
             mock.patch.object(rapid.RapidApiClient, "get_profile", fake):
            counts = rapid.hydrate_profiles(
                [("good", "https://a"), ("bad", "https://b"), ("", "https://c")], Path("unused"))
        self.assertEqual(counts["wanted"], 2)          # the empty public_identifier is dropped
        self.assertEqual((counts["ok"], counts["failed"]), (1, 1))
        self.assertEqual(sorted(calls), ["bad", "good"])


class RetargetProposalHydrationTests(unittest.TestCase):
    """The retarget judge must see the REAL profile, not Parallel's payload."""

    def test_cleared_retarget_hydrates_settles_then_realizes_offline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            db = profile_db(root)
            result = ResearchResult.from_payload({
                "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                "positions": [
                    {"title": "Founder", "company_name": "Bravo Robotics"},
                ],
                "social": {
                    "linkedin_url": "https://www.linkedin.com/in/jordan-correct",
                },
                "metadata": {"research_notes": "matched employer"},
            })
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
            hydrated: list[dict[str, str]] = []

            def hydrate(targets, cache_dir, *, db, **_kwargs):
                hydrated.extend(targets)
                profile_projection.project_profile_results(
                    db,
                    [(target, profile_result) for target in targets],
                    cache_dir,
                )
                return {"wanted": len(targets), "ok": len(targets)}, {
                    target["public_identifier"]: profile_result for target in targets
                }

            subset = [{
                "parent_slug": "jordan-bravo-p",
                "parent_id": "parent-1",
                "name": "Jordan Bravo",
                "person_ids": ["pid-1"],
                "candidate_key": "jordan-bravo",
                "linkedin": {
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                },
                "match_emails": [],
                "match_phones": [],
            }]
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
                    identity_evidence,
                    "judge_batch",
                    return_value=[{"verdict": verdict, "usage": {}, "error": ""}],
                ),
            ):
                judging.propose_retargets(
                    subset,
                    db=db,
                    use_llm=True,
                    profile_cache_dir=cache,
                    provided_results={"jordan-bravo-p": result},
                )

            self.assertEqual(
                [target["public_identifier"] for target in hydrated],
                ["jordan-correct"],
            )
            decision = db.query(
                "SELECT machine_action, machine_approved FROM links "
                "WHERE row_key='jordan-bravo'"
            )[0]
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
            out, facts, raw, cache = base/"research", base/"facts", base/"raw", base/"cache"
            for p in (out, facts, raw, cache):
                p.mkdir(parents=True, exist_ok=True)
            (out/"jordan-bravo-p").mkdir()
            # Parallel found the URL but returned NO positions — the bug's shape.
            (out/"jordan-bravo-p"/"01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jordan Bravo", "confidence": 0.9, "notes": "found via web"},
                "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
                "positions": [], "education": [],
                "metadata": {"research_notes": "confirmed by employer page"},
            }))
            (facts/"pid-1.jsonl").write_text(json.dumps(
                {"chunk_index": 0, "usage": {}, "facts": {"canonical_name": "Jordan Bravo"}}) + "\n")
            # The real profile is projected before any judge consumes it.
            profile_path = profile_cache_path(cache, "jordan-bravo")
            profile_payload = {
                "raw_response": {}, "normalized_profile": {
                    "success": True, "full_name": "Jordan Bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                    "education": [], "city": "SF", "state": "", "country": "",
                }}
            profile_path.write_text(json.dumps(profile_payload))
            subset = [{"parent_slug": "jordan-bravo-p", "name": "Jordan Bravo",
                       "person_ids": ["pid-1"], "candidate_key": "jordan-old",
                       "linkedin": {"linkedin_url": "https://www.linkedin.com/in/jordan-old"},
                       "match_emails": [], "match_phones": []}]
            seen = {}
            db = Db(base / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-1", "jordan-old", "Jordan Bravo"),
                PersonRow("pid-1", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "jordan-old", "parent-1", "jordan-old", "pub",
                    "https://www.linkedin.com/in/jordan-old", "Jordan Bravo",
                ),
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
            ))
            queue_row = {
                "parent_id": "parent-1",
                "candidate_exists": "1",
                "handle": "jordan-bravo-p",
                "source_person_ids": '["pid-1"]',
                "source_candidate_public_identifier": "jordan-old",
                "display_name": "Jordan Bravo",
            }
            db.project_rows(driver.research_artifact_projections(SimpleNamespace(
                db=db,
                output_dir=out,
                rows=(queue_row,),
                selection_fingerprint="",
            )))

            def capture(tasks, **kw):
                seen.update(tasks[0].get("linkedin") or {})
                return [{
                    "verdict": {"verdict": "confirmed", "confidence": 0.9, "reason": "ok"},
                    "usage": {},
                    "error": "",
                }]

            with mock.patch.object(rapid.RapidApiClient, "resolve_key", return_value=""), \
                 mock.patch.object(identity_evidence, "judge_batch", capture):
                judging.propose_retargets(
                    subset, db=db,
                    use_llm=True, profile_cache_dir=cache)

        # The judge saw the cached profile's experiences, not Parallel's empty positions.
        self.assertTrue(seen.get("has_profile"))
        self.assertIn("Bravo Robotics", " ".join(seen.get("experiences") or []))

    def test_cached_and_grandfathered_cleared_retargets_are_adopted_without_rejudging(self):
        for mode in ("cached", "grandfathered"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "cache"
                db = profile_db(root)
                result = ResearchResult.from_payload({
                    "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                    "positions": [
                        {"title": "Founder", "company_name": "Bravo Robotics"},
                    ],
                    "social": {
                        "linkedin_url": "https://www.linkedin.com/in/jordan-correct",
                    },
                    "metadata": {"research_notes": "matched employer"},
                })
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
                        [(
                            {
                                "public_identifier": "jordan-correct",
                                "linkedin_url": result.linkedin_url,
                                "candidate_key": "jordan-bravo",
                                "parent_id": "parent-1",
                            },
                            profile_result,
                        )],
                        cache,
                    )
                    graph = canonical_snapshot(db)
                    evidence = DossierEvidence.from_parent("parent-1", graph)
                    profile = identity_evidence.prefer_cached_profile(
                        result.identity_profile(),
                        queue.linkedin_view(
                            {"linkedin_url": result.linkedin_url},
                            profile_projection.profile_payloads(graph)["jordan-bravo"],
                        ),
                    )
                    fingerprint = judging.proposal_fingerprint(
                        "jordan-bravo", result.linkedin_url, evidence, profile,
                    )
                db.project_rows((IdentityMachineProjection(
                    "jordan-bravo",
                    machine_action="retarget",
                    machine_proposed_url=result.linkedin_url,
                    machine_proposed_public_identifier="jordan-correct",
                    machine_reject=None,
                    judgment_fingerprint=fingerprint,
                ),))
                subset = [{
                    "parent_slug": "jordan-bravo-p",
                    "parent_id": "parent-1",
                    "name": "Jordan Bravo",
                    "person_ids": ["pid-1"],
                    "candidate_key": "jordan-bravo",
                    "linkedin": {
                        "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    },
                    "match_emails": [],
                    "match_phones": [],
                }]

                hydrated: list[dict[str, str]] = []

                def hydrate(targets, cache_dir, *, db, **_kwargs):
                    hydrated.extend(targets)
                    profile_projection.project_profile_results(
                        db,
                        [(target, profile_result) for target in targets],
                        cache_dir,
                    )
                    return {"wanted": len(targets), "ok": len(targets)}, {
                        target["public_identifier"]: profile_result for target in targets
                    }

                with (
                    mock.patch.object(
                        profile_projection,
                        "hydrate_profiles",
                        side_effect=hydrate,
                    ),
                    mock.patch.object(
                        identity_evidence,
                        "judge_batch",
                        side_effect=AssertionError("cached adoption must not judge"),
                    ),
                ):
                    judging.propose_retargets(
                        subset,
                        db=db,
                        use_llm=True,
                        profile_cache_dir=cache,
                        provided_results={"jordan-bravo-p": result},
                    )

                self.assertEqual(
                    [row["public_identifier"] for row in hydrated],
                    ["jordan-correct"],
                )
                row = db.query(
                    "SELECT machine_action, machine_approved, machine_reject "
                    "FROM links WHERE row_key='jordan-bravo'"
                )[0]
                self.assertEqual(tuple(row), ("retarget", "auto", None))


class ResearchProposalPolicyTests(unittest.TestCase):
    def test_fingerprint_is_shared_by_batch_and_guided_research(self):
        evidence = DossierEvidence(
            name="Jordan Bravo",
            relationship="former colleague",
            employers=("Bravo Robotics",),
        )
        profile = {
            "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
            "full_name": "Jordan Bravo",
            "experiences": ["Founder @ Bravo Robotics"],
        }
        batch = identity_evidence.judgment_fingerprint(
            evidence, profile, IdentityOrigin.RESEARCH, "OWNER: Casey"
        )
        guided = identity_evidence.judgment_fingerprint(
            evidence, profile, IdentityOrigin.RESEARCH, "OWNER: Casey"
        )
        attached = identity_evidence.judgment_fingerprint(
            evidence, profile, IdentityOrigin.ATTACHED, "OWNER: Casey"
        )
        self.assertEqual(batch, guided)
        self.assertNotEqual(batch, attached)

    def test_batch_uses_one_client_and_one_event_loop(self):
        client = mock.MagicMock()
        client.close = mock.AsyncMock()
        judge = mock.AsyncMock(side_effect=[
            {"verdict": {"verdict": "confirmed", "confidence": 0.9}, "usage": {}, "error": ""},
            {"verdict": {"verdict": "wrong_person", "confidence": 0.9}, "usage": {}, "error": ""},
        ])
        progress = []
        with (
            mock.patch.object(identity_evidence, "load_env"),
            mock.patch.object(identity_evidence, "make_async_client", return_value=client) as make,
            mock.patch.object(identity_evidence, "judge_task", judge),
        ):
            results = identity_evidence.judge_batch(
                [{"name": "Jordan Bravo"}, {"name": "Casey Delta"}],
                use_llm=True,
                owner_block="",
                model="fixture-model",
                effort="medium",
                concurrency=2,
                timeout=30,
                max_retries=1,
                on_done=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual([row["verdict"]["verdict"] for row in results], [
            "confirmed", "wrong_person",
        ])
        make.assert_called_once_with(timeout=30)
        self.assertEqual(judge.await_count, 2)
        client.close.assert_awaited_once()
        self.assertEqual(progress, [(1, 2), (2, 2)])

    def proposal(self, prior):
        return judging.prepare_research_proposal(
            old_pub="jordan-old",
            new_url="https://www.linkedin.com/in/jordan-new",
            old_url="https://www.linkedin.com/in/jordan-old",
            dossier={"relationship": "former colleague"},
            profile={"linkedin_url": "https://www.linkedin.com/in/jordan-new"},
            name="Jordan Bravo",
            match_emails=[],
            match_phones=[],
            person_id="pid-1",
            confidence=0.9,
            unverified=False,
            reason="matched employer",
            source="deep-research",
            prior=prior,
        )

    def test_exact_fingerprint_reuses_existing_retarget_verdict(self):
        initial = self.proposal({})
        cached = self.proposal(
            {
                "action": "retarget",
                "llm_judge_fingerprint": initial.proposal["judge_fingerprint"],
            }
        )
        self.assertEqual(cached.disposition, "cached")
        self.assertIsNone(cached.task)

    def test_legacy_retarget_to_same_url_is_grandfathered(self):
        prepared = self.proposal(
            {
                "action": "retarget",
                "llm_judge_fingerprint": "",
                "new_linkedin_url": "https://www.linkedin.com/in/jordan-new",
            }
        )
        self.assertEqual(prepared.disposition, "grandfathered")
        self.assertIsNone(prepared.task)


class ResearchSelectionTests(unittest.TestCase):
    def test_guided_research_creates_missing_bare_person_candidate(self):
        class Provider:
            def __init__(self, *_args, **_kwargs):
                pass

            def execute(self, inputs, _params, on_status):
                handle = inputs[0]["metadata"]["handle"]
                on_status({"completed": 1})
                return 1, {handle: {
                    "real_name": "Jordan Bravo",
                    "name_confidence": 0.9,
                    "name_evidence": "official profile",
                    "work_experience": "[]",
                    "education": "[]",
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                    "summary": "Founder",
                    "research_notes": "matched",
                }}, [], {"is_active": False}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow(
                    "parent-1", "parent-worth:parent-1", "Jordan Bravo",
                    display_slug="jordan-bravo",
                ),
                PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
            ))
            request = GuidanceRequest(
                "jordan-bravo", "person-a", "Jordan Bravo", "Find the founder",
                person_ids=("person-a",), queue_slug="jordan-bravo",
            )
            with (
                mock.patch.object(driver, "_api_key", return_value="test-key"),
                mock.patch.object(driver.sdk_client, "ParallelClient", Provider),
            ):
                result = GuidedResearch(
                    db, research_dir=root / "research",
                ).research(request)

            link = db.query(
                "SELECT parent_id, kind FROM links WHERE row_key='person-a'"
            )
            artifact = db.query(
                "SELECT candidate_key FROM artifacts "
                "WHERE artifact_key='research:jordan-bravo'"
            )

        self.assertEqual(
            result["new_url"], "https://www.linkedin.com/in/jordan-bravo"
        )
        self.assertEqual([tuple(row) for row in link], [("parent-1", "research")])
        self.assertEqual([tuple(row) for row in artifact], [("person-a",)])

    def test_batch_and_guided_use_parent_id_when_display_slug_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-1", "public-fallback", "Jordan Bravo"),
                PersonRow("pid-1", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "jordan-old", "parent-1", "jordan-old", RowKind.PUB.value,
                    "https://www.linkedin.com/in/jordan-old",
                    machine_judgment="wrong_person", machine_confidence=0.9,
                    judgment_payload_json=json.dumps({"recommend_deep_research": True}),
                ),
                ArtifactRow(
                    "facts:parent-1", ArtifactKind.FACTS.value, "parent-1",
                    str(root / "facts.jsonl"), "1" * 64,
                    ProjectionStatus.PROJECTED.value, payload_json="{}",
                ),
                FactRow(
                    "parent-1", "parent-1", "facts:parent-1",
                    machine_worth="yes", facts_json="{}",
                ),
            ))
            db.decide_worth("parent-1", "yes")
            batch = selection.select_research(
                db,
                processor="core2x",
                confirm_threshold=0.8,
                include_plausibly_absent=False,
                include_candidates=False,
                fingerprint={"fingerprint": "fixture"},
            )
            parent = person_detail(db, "parent-1") or {}
            request = GuidanceRequest(
                "parent-1", "jordan-old", "Jordan Bravo", "Try the founder",
                person_ids=("pid-1",), queue_slug="parent-1",
            )
            guided = GuidedResearch(db).research_row(
                request, parent, queue.canonical_snapshot(db),
            )

        self.assertEqual(len(batch.queue), 1)
        self.assertEqual(batch.queue[0]["handle"], "parent-1")
        self.assertEqual(guided["handle"], "parent-1")

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
                    fingerprint={"sha256": "fixture-selection"},
                )
        self.assertEqual(result.fingerprint["fingerprint"], "fixture-selection")
