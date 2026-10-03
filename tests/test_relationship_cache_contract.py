"""Relationship cache reuse requires the same source identity and judge request."""
import json
import unittest
from unittest.mock import AsyncMock, patch

import tests.test_deep_context_identity_disagreements as fixtures
import packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship as relationship
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import ResearchRow


class RelationshipCacheContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ParentIdentityTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.parent()
        self.db = self.fixture.db
        self.answer = {'candidates': [{'url': 'https://www.linkedin.com/in/jordan-bravo',
                                      'verdict': 'review', 'reason': 'Two plausible histories', 'confidence': .5}]}

    def cache(self):
        return self.fixture.run_stage(self.answer, approve_spend=True)

    def assert_pending(self, **kwargs):
        before = self.db.db_path.read_bytes()
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock) as call:
            result = relationship.ReviewRelationships(db=self.db, **kwargs).run()
        self.assertEqual((result['status'], result['calls'], result['reused']), ('needs_approval', 1, 0))
        call.assert_not_called()
        self.assertEqual(self.db.db_path.read_bytes(), before)

    def test_policy_requires_a_connection_instead_of_a_default_name_match(self):
        self.assertIn("affirmative independently attributable contact-to-profile connection", relationship.SYSTEM_PROMPT)
        self.assertIn("external research excerpt", relationship.SYSTEM_PROMPT)
        self.assertIn("work-email domain", relationship.SYSTEM_PROMPT)
        self.assertNotIn("accept the match unless", relationship.SYSTEM_PROMPT)
        self.assertNotIn("should remain associated unless", relationship.SYSTEM_PROMPT)
        self.assertNotIn("compatible professional context can also support yes", relationship.SYSTEM_PROMPT)

    def test_research_input_contains_only_actual_citations(self):
        output = {"type": "json", "content": {"real_name": "Jordan Bravo", "summary": "INVENTED normalized biography",
                              "work_experience": [], "education": []},
                  "basis": [{"field": "summary", "reasoning": "INVENTED email-to-employer bridge",
                             "citations": [{"url": "https://example.com/jordan", "title": "Jordan bio",
                                            "excerpts": ["Jordan Bravo founded Oriel."]}]}]}
        self.db.project_rows((ResearchRow("jordan", "jordan", "complete", result_json=json.dumps(output)),))
        _, call = self.cache()
        prompt = call.call_args.kwargs["user_prompt"]
        self.assertNotIn("INVENTED normalized biography", prompt)
        self.assertNotIn("INVENTED email-to-employer bridge", prompt)
        self.assertIn("https://example.com/jordan", prompt)
        self.assertIn("Jordan bio", prompt)
        self.assertIn("Jordan Bravo founded Oriel.", prompt)
        output["content"]["summary"] = "Different uncited biography"
        output["basis"][0]["reasoning"] = "Different invented connection"
        self.db.project_rows((ResearchRow("jordan", "jordan", "complete", result_json=json.dumps(output)),))
        result, call = self.fixture.run_stage(self.answer)
        self.assertEqual((result["calls"], result["reused"]), (0, 1))
        call.assert_not_called()
        output["basis"][0]["citations"][0]["excerpts"] = ["Jordan Bravo founded ExampleWorks."]
        self.db.project_rows((ResearchRow("jordan", "jordan", "complete", result_json=json.dumps(output)),))
        self.assert_pending()

    def test_unchanged_complete_request_reuses_without_a_call(self):
        self.cache()
        result, call = self.fixture.run_stage(self.answer)
        self.assertEqual((result['status'], result['calls'], result['reused']), ('completed', 0, 1))
        call.assert_not_called()

    def test_model_change_requires_new_judgment_and_spend_approval(self):
        self.cache()
        self.assert_pending(model='gpt-6-sol')

    def test_effort_change_requires_new_judgment_and_spend_approval(self):
        self.cache()
        self.assert_pending(reasoning_effort='high')

    def test_system_prompt_change_requires_new_judgment(self):
        self.cache()
        with patch.object(relationship, 'SYSTEM_PROMPT', 'Revised source identity question'):
            self.assert_pending()

    def test_schema_change_requires_new_judgment(self):
        self.cache()
        with patch.object(relationship, 'SCHEMA', {**relationship.SCHEMA, 'description': 'Revised output contract'}):
            self.assert_pending()

    def test_original_name_change_requires_new_judgment(self):
        self.cache()
        source = queries.imported_people(self.db)[0]
        self.db.replace_imported_people((source.model_copy(update={'full_name': 'Casey Delta'}),))
        self.assert_pending()

    def test_original_email_change_requires_new_judgment(self):
        self.cache()
        source = queries.imported_people(self.db)[0]
        self.db.replace_imported_people((source.model_copy(update={'primary_email': 'changed@example.test'}),))
        self.assert_pending()

    def test_original_phone_change_requires_new_judgment(self):
        self.cache()
        source = queries.imported_people(self.db)[0]
        self.db.replace_imported_people((source.model_copy(update={'primary_phone': '+15550100123'}),))
        self.assert_pending()

    def test_original_source_identity_is_sent_to_the_model(self):
        _, call = self.cache()
        prompt = json.loads(call.call_args.kwargs['user_prompt'])
        self.assertIn('Source contact names:', prompt['dossier'])
        self.assertIn('person:jordan', prompt['dossier'])
        self.assertIn('jordan@example.test', prompt['dossier'])
        self.assertNotIn('identity_verdict', prompt['candidates'][0])
        self.assertNotIn('identity_reason', prompt['candidates'][0])


if __name__ == '__main__':
    unittest.main()
