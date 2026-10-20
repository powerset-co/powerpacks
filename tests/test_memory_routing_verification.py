"""Grounded routing checks use command events and real synthetic CLI output."""
from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('memory_routing_verification', str(ROOT / 'scripts/verify-memory-routing'))
spec = spec_from_loader(loader.name, loader)
runner = module_from_spec(spec)
loader.exec_module(runner)
CASES = json.loads((ROOT / 'packs/memory/evals/memory/cases.json').read_text())


def completed(command: str, output: object) -> dict:
    return {'command': command, 'aggregated_output': json.dumps(output), 'exit_code': 0, 'status': 'completed'}


class RoutingVerificationTests(unittest.TestCase):
    def test_native_lookup_preserves_closed_fixture_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as home:
            workspace = Path(temp)
            runner.snapshot(workspace)
            context = runner.fixture(workspace)
            source = Path(context['source_db'])
            wal = Path(str(source) + '-wal')
            self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0, 'fixture leaves source writes in WAL')
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            env = {'PATH': os.environ['PATH'], 'HOME': home, 'CODEX_HOME': home, 'UV_NO_SYNC': '1', 'UV_OFFLINE': '1', 'UV_CACHE_DIR': str(workspace / '.uv-cache'),
                   'TIKTOKEN_CACHE_DIR': str(workspace / '.tokenizer-cache'), 'PYTHONDONTWRITEBYTECODE': '1'}
            process = subprocess.run(['bin/deep-context-v2', 'lookup', '--name', 'Jordan Bravo', '--json'], cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)[0]['relationship_to_owner'], 'Former synthetic teammate')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_final_prose_never_proves_execution(self) -> None:
        final = 'Read AGENTS.md, memory skill; resolved SoF and enumerated 3 people.'
        commands, message, errors = runner.evidence(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': final}}))
        self.assertEqual((commands, message, errors), ([], final, []))
        self.assertTrue(runner.validate(CASES[0], commands, message))
        failed = completed('cat AGENTS.md packs/memory/skills/memory/SKILL.md', {})
        failed['exit_code'] = 1
        self.assertTrue(runner.validate(CASES[0], [failed], final))

    def test_real_fixture_cli_enumeration_and_semantic_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            context = runner.fixture(Path(temp))
            prefix = [sys.executable, '-m', 'packs.memory', '--db', context['derived']]
            def cli(*args: str) -> dict:
                process = subprocess.run([*prefix, *args], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                return completed(' '.join([*prefix, *args]), json.loads(process.stdout))
            reads = [completed('cat AGENTS.md', ''), completed('cat packs/memory/skills/memory/SKILL.md', '')]
            resolved = cli('resolve', 'SoF')
            page = cli('enumerate', 'source_name', 'sof', '--limit', '2')
            after = json.loads(page['aggregated_output'])['next_after']
            last = cli('enumerate', 'source_name', 'sof', '--limit', '2', '--after', after)
            self.assertEqual(runner.validate(CASES[0], [*reads, resolved, page, last], '3 saved contacts'), [])
            query = cli('query', 'permission architecture', '--kind', 'work', '--embedding-model', context['embedding_model'])
            hit = json.loads(query['aggregated_output'])['hits'][0]
            opened = cli('open', str(hit['claim_id']))
            self.assertEqual(runner.validate(CASES[1], [*reads, query, opened], 'Jordan has saved IAM work evidence.'), [])
            self.assertTrue(runner.validate(CASES[1], [*reads, query], 'I opened it.'))
            ambiguous = cli('resolve', 'South of France')
            meanings = json.loads(ambiguous['aggregated_output'])
            self.assertGreaterEqual(len(meanings), 2)
            self.assertEqual(runner.validate(CASES[3], [*reads, ambiguous], 'Do you mean the tag or the location?'), [])
            command = [sys.executable, '-m', 'packs.ingestion.primitives.deep_context_v2.lookup', '--data-root', context['source'], '--name', 'Jordan Bravo', '--json']
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            dossier = completed(' '.join(command), json.loads(process.stdout))
            self.assertEqual(runner.validate(CASES[2], [*reads, completed('cat packs/memory/skills/memory/person-lookup.md', ''), dossier], 'Jordan is a former synthetic teammate.'), [])

    def test_completed_command_event_retains_output_and_exit(self) -> None:
        item = {'type': 'command_execution', **completed('python -m packs.memory --db fixture query IAM', {'hits': []})}
        commands, final, errors = runner.evidence(json.dumps({'type': 'item.completed', 'item': item}))
        self.assertEqual(commands[0]['exit_code'], 0)
        self.assertEqual(json.loads(commands[0]['aggregated_output']), {'hits': []})
        self.assertEqual((final, errors), ('', []))


if __name__ == '__main__':
    unittest.main()
