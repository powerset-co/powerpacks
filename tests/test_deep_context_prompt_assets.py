"""Byte-fidelity checks for Deep Context prompt assets."""

from __future__ import annotations

import hashlib
import importlib
import unittest

from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt


PROMPTS = {
    "identity_merge_system": (
        "packs.ingestion.primitives.deep_context.merge_candidates.judge",
        "JUDGE_SYSTEM",
        "62cc6924033d8965f159412000795998fc4b391190c86abc7a477291493c6d9b",
    ),
    "contact_research_instructions": (
        "packs.ingestion.primitives.deep_context.enrich.parallel_research.config",
        "RESEARCH_INSTRUCTIONS",
        "77d9675720416cf4e68effd10671aa4eba5ab8b28459380330c04876cef59f9e",
    ),
    "linkedin_reconcile_system": (
        "packs.ingestion.primitives.deep_context.enrich.identity_evidence",
        "SYSTEM_PROMPT",
        "c8fb85e39ebf22e18d1c63ad54fc71912f6473aac1a793747cc460a07e6d2903",
    ),
    "person_synthesis_system": (
        "packs.ingestion.primitives.deep_context.synthesis.prompting",
        "SYSTEM_PROMPT",
        "7e0bff6b0617d53d40e93d8932cffe1a7bec6d49df286ce7099363e80a9ace97",
    ),
    "owner_context_suffix": (
        "packs.ingestion.primitives.deep_context.synthesis.prompting",
        "OWNER_PROMPT_SUFFIX",
        "a3a774f1601f1ae9116b9a1e7ce4dd087433579ef96302f68a6b5766c3643d37",
    ),
}

SYNTHESIS_POLICY_ASSETS = {
    "owner_identity_check": "72629fe1f2429acd338960415d076a3698f5a2ca3dfb35fb2b379effeb849806",
    "worth_policy_mixed": "23e13473c99f73fccc686c687383a8921b4a3896f6aedae7654eb58e899dd563",
    "worth_policy_email": "dfbe800e60864ce949f6c6d0a373392b0df38f233ba94178a9095d623fdb8823",
    "worth_policy_phone": "9df44a1e763111c5e89a3009e3ab7589cef8e06e48f1cd1921386a891ea775d9",
    "worth_policy_unknown": "10eed47d354d27ce83976a6631e9807be9efeeeb941ccc6bc28f171ba9c0b290",
}


class PromptAssetTests(unittest.TestCase):
    def test_assets_match_pre_extraction_bytes_and_exported_constants(self) -> None:
        for name, (module_name, constant_name, expected_hash) in PROMPTS.items():
            with self.subTest(name=name):
                prompt = load_prompt(name)
                if name == "owner_context_suffix":
                    prompt = f"\n\n{prompt}\n\n"
                self.assertEqual(
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    expected_hash,
                )
                module = importlib.import_module(module_name)
                self.assertEqual(getattr(module, constant_name), prompt)

    def test_synthesis_policy_prose_is_pinned_in_assets(self) -> None:
        for name, expected_hash in SYNTHESIS_POLICY_ASSETS.items():
            with self.subTest(name=name):
                self.assertEqual(
                    hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest(),
                    expected_hash,
                )

    def test_owner_suffix_keeps_rendering_whitespace(self) -> None:
        suffix = f"\n\n{load_prompt('owner_context_suffix')}\n\n"
        self.assertTrue(suffix.startswith("\n\nUse MY background"))
        self.assertTrue(suffix.endswith("supports it.\n\n"))

    def test_loader_rejects_paths(self) -> None:
        with self.assertRaises(ValueError):
            load_prompt("../person_synthesis_system")


if __name__ == "__main__":
    unittest.main()
