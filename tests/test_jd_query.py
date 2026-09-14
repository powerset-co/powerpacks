"""Generate a located query directly from the complete JD."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import decompose_jd, fetch_jd


class JdQueryTests(unittest.TestCase):
    def test_full_jd_generates_one_query_without_plan_or_location_rewriting(self):
        job = json.loads((Path(__file__).parent / "fixtures/listenlabs-product-ashby.json").read_text())
        body, _ = fetch_jd.extract(job["descriptionHtml"])
        locations = [job["location"], *[row["location"] for row in job["secondaryLocations"]]]
        listen_jd = f"{job['title']}\nLocations: {'; '.join(locations)}\n\n{body}"
        for jd, query in (
            (listen_jd, "Software Engineer with AI experience in San Francisco, CA or New York, NY"),
            ("Office Manager\nLocation: Toronto or Vancouver, Canada\n"
             "Manage office supplies, vendors, and facilities.",
             "Office Manager in Toronto or Vancouver, Canada"),
            ("Technical Writer\nWork remotely from anywhere.\nWrite product documentation.",
             "Technical Writer"),
        ):
            with self.subTest(query=query):
                client = mock.Mock()
                client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"seeds": [query]})))])
                result = decompose_jd.generate_queries(jd=jd, client=client, use_precedents=False)
                self.assertEqual(result, [{"key": "q00", "query": query}])
                client.chat.completions.create.assert_called_once()
                messages = client.chat.completions.create.call_args.kwargs["messages"]
                self.assertEqual(messages[0]["content"], decompose_jd.SYSTEM)
                self.assertEqual(messages[1]["content"], f"Produce the primary recruiter query for this JD.\n\n{jd}")
                self.assertIn("OR alternatives", messages[0]["content"])
                self.assertIn("Before returning", messages[0]["content"])

    def test_precedents_use_only_the_jd_and_first_chain_link(self):
        card = {"job": "Office Manager", "chain": [
            {"query": "Office Manager"}, {"query": "Facilities Manager"},
        ]}
        with mock.patch.object(decompose_jd.precedents, "retrieve_jd_precedents",
                               return_value=[card]) as retrieve:
            result = decompose_jd.retrieve_precedent_cards("Manage facilities and vendors.")
        retrieve.assert_called_once_with("Manage facilities and vendors.", {}, collection="pond", limit=1)
        self.assertEqual(result, [{**card, "chain": card["chain"][:1]}])

    def test_cli_generates_query_without_plan_file(self):
        jd = "Office Manager in Toronto or Vancouver, Canada. Manage facilities and vendors."
        query = "Office Manager in Toronto or Vancouver, Canada"
        client = mock.Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"seeds": [query]})))])
        with tempfile.TemporaryDirectory() as tmp:
            jd_path = Path(tmp) / "jd.txt"
            jd_path.write_text(jd)
            out = Path(tmp) / "queries.json"
            stdout = io.StringIO()
            with (
                mock.patch("sys.argv", ["decompose_jd", "--jd-file", str(jd_path),
                                        "--out", str(out), "--api-key", "synthetic-test-key"]),
                mock.patch.object(decompose_jd, "make_openai_client", return_value=client),
                mock.patch.object(decompose_jd, "retrieve_precedent_cards", return_value=[]),
                contextlib.redirect_stdout(stdout),
            ):
                decompose_jd.main()
            self.assertEqual(json.loads(out.read_text()), [{"key": "q00", "query": query}])
            self.assertEqual(json.loads(out.with_suffix(".raw.json").read_text()),
                             {"seeds": [query], "precedent_cards": []})
            self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")
        client.chat.completions.create.assert_called_once()

    def test_cached_query_reuses_raw_and_precedents_without_credentials(self):
        query = "Office Manager in Toronto or Vancouver, Canada"
        raw = json.dumps({"seeds": [query], "precedent_cards": [{"job": "Office Manager"}]}, indent=4)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.raw.json"
            path.write_text(raw)
            with (mock.patch.dict("os.environ", {}, clear=True),
                  mock.patch.object(decompose_jd, "make_openai_client") as create_client,
                  mock.patch.object(decompose_jd, "retrieve_precedent_cards") as retrieve):
                result = decompose_jd.generate_queries(jd="Full JD", raw_response_path=path)
            self.assertEqual(result, [{"key": "q00", "query": query}])
            self.assertEqual(path.read_text(), raw)
            create_client.assert_not_called()
            retrieve.assert_not_called()

    def test_malformed_saved_query_does_not_repeat_model_call(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"seeds": ['))])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.raw.json"
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(json.JSONDecodeError):
                    decompose_jd.generate_queries(
                        jd="Full JD", client=client, use_precedents=False, raw_response_path=path)
            self.assertEqual(path.read_text(), '{"seeds": [')
        client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
