"""Offline packaging, routing and launcher checks. No paid model calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import common
from baseline2 import qwen_transport as transport
from baseline2.transport_helpers import SUMMARY_SYSTEM


class BaselineTests(unittest.TestCase):
    def test_source_identity(self):
        for name in ('baseline1', 'baseline2'):
            for entry in common.read(ROOT / name / 'source_manifest.json')['source_files']:
                self.assertEqual(common.digest(ROOT / name / entry['path']), entry['packaged_sha256'])
                if entry['change'] == 'byte-identical copy':
                    self.assertEqual(entry['source_sha256'], entry['packaged_sha256'])

    def test_tasks_and_invalid_selection(self):
        tasks = common.select_tasks()
        self.assertEqual(len(tasks), 31)
        self.assertEqual(len({t['task_id'] for t in tasks}), 31)
        self.assertEqual(common.select_tasks(tasks[0]['task_id']), tasks[:1])
        with self.assertRaises(ValueError):
            common.select_tasks('not-in-the-comparison')

    def test_benchmark_rejects_modified_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'Benchmark source mismatch'):
                common.validate_benchmark(root, [])

    def test_cli_help_has_no_execution(self):
        for name in ('baseline1', 'baseline2'):
            result = subprocess.run([sys.executable, str(ROOT / name / 'run.py'), '--help'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--execute', result.stdout)

    def test_real_relay_with_mocked_upstream(self):
        usage = {'prompt_tokens': 80, 'completion_tokens': 20, 'total_tokens': 100, 'cost': 0.0003}
        cases = [('agent', 'user', 'task', 'openai/gpt-5.5', True),
                 ('agent', 'user', SUMMARY_SYSTEM, 'openai/gpt-5.5', True),
                 ('compressor', 'system', SUMMARY_SYSTEM, 'openai/gpt-5.5', True),
                 ('compressor', 'system', SUMMARY_SYSTEM, 'openai/gpt-5.5', False),
                 ('grader', 'user', 'grade', 'openai/gpt-5.4', False),
                 ('agent_auxiliary', 'user', 'tool', 'openai/gpt-5-mini', False)]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            server, ledger, token, _ = transport.start_gateway(directory, 'offline-test-secret')
            try:
                for role, message_role, content, model, stream in cases:
                    with self.subTest(role=role, message_role=message_role, stream=stream):
                        body = {'model': model, 'messages': [{'role': message_role, 'content': content}],
                                'stream': stream, 'max_completion_tokens': 16000, 'temperature': 0.2}
                        raw = json.dumps(body).encode()
                        data = (('data: ' + json.dumps({'usage': usage}) + '\n\ndata: [DONE]\n\n')
                                if stream else json.dumps({'usage': usage})).encode()
                        response = SimpleNamespace(status_code=200, content=data,
                            headers={'Content-Type': 'text/event-stream' if stream else 'application/json'})
                        with patch.object(transport.requests, 'post', return_value=response) as upstream:
                            request = Request(f'http://127.0.0.1:{server.server_address[1]}/task/api/v1/chat/completions',
                                data=raw, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                            with urlopen(request, timeout=5) as result:
                                self.assertEqual(result.read(), data)
                            sent = upstream.call_args.kwargs['data']
                            if role == 'compressor':
                                self.assertEqual(json.loads(sent), {**body, 'model': transport.COMPRESSOR_MODEL,
                                                                    'reasoning': {'effort': 'high'}})
                            else:
                                self.assertEqual(sent, raw)
                        self.assertEqual(ledger[-1]['category'], role)
                        self.assertEqual(ledger[-1]['usage'], usage)
                log = (directory / 'request_ledger.jsonl').read_text()
                self.assertNotIn('offline-test-secret', log)
                self.assertNotIn(token, log)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
