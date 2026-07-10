"""Focused tests for the versioned deep-search recruiter policy."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.deep_search import recruiter_policy as rp


class TestRecruiterPolicy(unittest.TestCase):
    def test_checked_in_policy_loads_with_bias_and_safety_defaults(self):
        policy = rp.load_recruiter_policy()
        defaults = policy["defaults"]

        self.assertEqual(policy["version"], "1.0.0")
        self.assertEqual(policy["precedence"], ["user", "jd", "default"])
        self.assertEqual(defaults["evidence_priority"][0], "direct_recent_demonstrated_work")
        self.assertGreater(defaults["excellence_weights"]["trajectory"], defaults["excellence_weights"]["pedigree"])
        self.assertGreater(defaults["excellence_weights"]["impact"], defaults["excellence_weights"]["pedigree"])
        self.assertEqual(sum(defaults["excellence_weights"].values()), 1.0)
        self.assertEqual(defaults["pedigree_policy"], "positive_prior_not_gate")
        self.assertEqual(defaults["current_founder_c_suite_for_non_exec_ic"], "default_out")
        self.assertEqual(defaults["higher_hands_on_ic"], "eligible")
        self.assertTrue(all(defaults["fairness"].values()))

    def test_hire_stage_aliases_canonicalize_current_vocabularies(self):
        self.assertEqual(rp.CANONICAL_HIRE_STAGES, ("founding_early", "scaling_late"))
        for alias in ("early", "seed", "Founding Early"):
            self.assertEqual(rp.canonicalize_hire_stage(alias), "founding_early")
        for alias in ("growth", "scale", "late-stage"):
            self.assertEqual(rp.canonicalize_hire_stage(alias), "scaling_late")
        with self.assertRaises(rp.RecruiterPolicyError):
            rp.canonicalize_hire_stage("enterprise")

    def test_user_overrides_jd_and_partial_weights_keep_leaf_provenance(self):
        resolved = rp.resolve_recruiter_preferences(
            jd_preferences={
                "hire_stage": "growth",
                "excellence_weights": {"impact": 2.0},
                "current_founder_c_suite_for_non_exec_ic": "review",
            },
            user_preferences={
                "hire_stage": "early",
                "excellence_weights": {"trajectory": 3.0},
                "current_founder_c_suite_for_non_exec_ic": "eligible",
            },
        )

        values = resolved["preferences"]
        self.assertEqual(values["hire_stage"], "founding_early")
        self.assertEqual(values["current_founder_c_suite_for_non_exec_ic"], "eligible")
        self.assertEqual(sum(values["excellence_weights"].values()), 1.0)
        self.assertAlmostEqual(values["excellence_weights"]["trajectory"], 3.0 / 5.2)
        self.assertAlmostEqual(values["excellence_weights"]["impact"], 2.0 / 5.2)
        self.assertAlmostEqual(values["excellence_weights"]["pedigree"], 0.2 / 5.2)
        self.assertEqual(resolved["provenance"]["hire_stage"]["source"], "user")
        self.assertEqual(resolved["provenance"]["excellence_weights.trajectory"]["source"], "user")
        self.assertEqual(resolved["provenance"]["excellence_weights.impact"]["source"], "jd")
        self.assertEqual(resolved["provenance"]["excellence_weights.pedigree"]["source"], "default")
        self.assertEqual(
            resolved["provenance"]["excellence_weights.pedigree"]["default_source"],
            "powerpacks.recruiter-defaults@1.0.0",
        )

    def test_pedigree_ignore_zeroes_weight_and_records_derivation(self):
        resolved = rp.resolve_recruiter_preferences(user_preferences={"pedigree_policy": "ignore"})

        self.assertEqual(resolved["preferences"]["excellence_weights"]["pedigree"], 0.0)
        self.assertEqual(sum(resolved["preferences"]["excellence_weights"].values()), 1.0)
        pedigree_origin = resolved["provenance"]["excellence_weights.pedigree"]
        self.assertEqual(pedigree_origin["source"], "user")
        self.assertEqual(pedigree_origin["derived_from"], "pedigree_policy")

    def test_resolved_policy_validation_accepts_an_immutable_snapshot(self):
        resolved = rp.resolve_recruiter_preferences(
            user_preferences={"hire_stage": "growth", "pedigree_policy": "ignore"}
        )

        validated = rp.validate_resolved_recruiter_preferences(resolved)

        self.assertEqual(validated, resolved)
        self.assertIsNot(validated, resolved)

    def test_resolved_policy_validation_rejects_weight_and_provenance_drift(self):
        resolved = rp.resolve_recruiter_preferences()
        resolved["preferences"]["excellence_weights"]["impact"] = 0.3
        with self.assertRaisesRegex(rp.RecruiterPolicyError, "sum to 1"):
            rp.validate_resolved_recruiter_preferences(resolved)

        resolved = rp.resolve_recruiter_preferences()
        resolved["provenance"].pop("hire_stage")
        with self.assertRaisesRegex(rp.RecruiterPolicyError, "provenance"):
            rp.validate_resolved_recruiter_preferences(resolved)

    def test_resolved_policy_validation_rejects_disabled_fairness(self):
        resolved = rp.resolve_recruiter_preferences()
        resolved["preferences"]["fairness"]["exclude_protected_attributes"] = False
        with self.assertRaisesRegex(rp.RecruiterPolicyError, "fairness"):
            rp.validate_resolved_recruiter_preferences(resolved)

    def test_unknown_fields_and_zero_resolved_weights_fail_closed(self):
        with self.assertRaises(rp.RecruiterPolicyError):
            rp.validate_recruiter_preferences({"protected_attributes": "prefer"}, source="user")
        with self.assertRaises(rp.RecruiterPolicyError):
            rp.resolve_recruiter_preferences(
                user_preferences={
                    "excellence_weights": {"trajectory": 0, "impact": 0, "pedigree": 0},
                }
            )

    def test_loader_rejects_disabled_fairness_guardrail(self):
        policy = rp.load_recruiter_policy()
        policy["defaults"]["fairness"]["exclude_protected_attribute_proxies"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaises(rp.RecruiterPolicyError):
                rp.load_recruiter_policy(path)

    def test_validation_copies_and_canonicalizes_preferences(self):
        preferences = {"hire_stage": "scale", "excellence_weights": {"impact": 2}}
        original = copy.deepcopy(preferences)

        validated = rp.validate_recruiter_preferences(preferences, source="jd")

        self.assertEqual(preferences, original)
        self.assertEqual(validated["hire_stage"], "scaling_late")
        self.assertEqual(validated["excellence_weights"], {"impact": 2.0})

    def test_validation_uses_normalized_hire_stage_alias_for_lookup(self):
        for alias in ("Early", "Founding Early", "founding-early"):
            with self.subTest(alias=alias):
                validated = rp.validate_recruiter_preferences({"hire_stage": alias}, source="user")
                self.assertEqual(validated["hire_stage"], "founding_early")

    def test_prompt_states_recruiter_defaults_and_non_discrimination(self):
        prompt = rp.render_recruiter_prompt(rp.resolve_recruiter_preferences())

        self.assertIn("direct, recent demonstrated work first", prompt)
        self.assertIn("trajectory 40%", prompt)
        self.assertIn("impact 40%", prompt)
        self.assertIn("pedigree 20%", prompt)
        self.assertIn("Missing pedigree is floor-neutral", prompt)
        self.assertIn("cannot lower a candidate or block top-tier by itself", prompt)
        self.assertIn("job-relevant evidence", prompt)
        self.assertIn("protected attributes or proxies", prompt)
        self.assertIn("non-executive IC targets only", prompt)
        self.assertIn("Higher hands-on IC levels remain eligible", prompt)

    def test_preferences_schema_matches_public_override_fields(self):
        schema = json.loads(rp.PREFERENCES_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), set(rp.ALLOWED_OVERRIDE_FIELDS))


if __name__ == "__main__":
    unittest.main()
