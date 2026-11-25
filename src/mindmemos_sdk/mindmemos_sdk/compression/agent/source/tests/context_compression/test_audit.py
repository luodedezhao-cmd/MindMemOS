"""Durable audit contracts; all API traffic is localhost/mock only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
import threading

from src.context_compression.audit import AuditWriteError, CompressionAudit
from src.context_compression.compressor import OpenAICompressor
from src.context_compression.core import ContextManager, memory_messages
from src.context_compression.proxy import create_server, shutdown_server
from src.context_compression.types import CompressionConfig, CompressionResult
from tests.context_compression.test_core import IDENTITY, msg, payload
from tests.context_compression.test_transport import envelope, mock_upstream, respond_json


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='compression-audit-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = CompressionAudit(self.root, IDENTITY)
        self.events = []
        self.config = CompressionConfig(trigger_tokens=80)

    def records(self):
        return [json.loads(s) for s in self.audit.path.read_text().splitlines()]

    def manager(self, compressor, **options):
        return ContextManager(compressor, options.get('config', self.config), task='the task',
                              instance_id=IDENTITY, event_sink=self.events.append,
                              audit_sink=options.get('audit_sink', self.audit))

    def test_append_append_full_exact_inputs_and_results(self):
        results = [CompressionResult(False, .8, 'still active', '中文 M1'),
                   CompressionResult(False, .9, 'active', 'M2'),
                   CompressionResult(True, .95, 'stage closed', 'Full facts')]
        comp = Mock(side_effect=results)
        manager = self.manager(comp)
        before = payload([msg('a'*800), msg('protected1')])
        original = deepcopy(before)
        manager.transform(before)
        manager.transform(payload([msg('b'*800), msg('protected2')], start=3))
        manager.transform(payload([msg('c'*800), msg('protected3')], start=5))
        self.assertEqual(before, original)
        rows = self.records()
        snapshots = [r for r in rows if r['event']=='compression_snapshot']
        outputs = [r for r in rows if r['event']=='compression_result']
        ends = [r for r in rows if r['event']=='compression_applied']
        self.assertEqual(len(snapshots), 3)
        for saved, call in zip(snapshots, comp.call_args_list):
            s = call.args[0]
            self.assertEqual(saved['raw'], list(s.raw))
            self.assertEqual(saved['memory'], list(s.memory))
            self.assertEqual((saved['start'], saved['end']), (s.start, s.end))
            self.assertEqual(saved['task'], s.task)
            self.assertEqual(saved['call_id'], s.call_id)
        self.assertNotIn('protected3', str(snapshots))
        self.assertEqual([r['C'] for r in outputs], [r.C for r in results])
        self.assertEqual([r['memory_after'] for r in ends], [['中文 M1'], ['中文 M1', 'M2'], ['Full facts']])
        self.assertEqual([r['call_id'] for r in outputs], [r['call_id'] for r in ends])
        self.assertEqual(manager._raw, [msg('protected3')])
        self.assertEqual({r['instance_id'] for r in rows}, {IDENTITY})

    def test_unshortened_and_shutdown_results_are_saved_not_applied(self):
        for close in (False, True):
            with self.subTest(close=close):
                def comp(s):
                    if close:
                        manager.close()
                    return CompressionResult(True, .9, 'closed', 'C'*4000)
                manager = self.manager(comp)
                manager.transform(payload([msg('a'*800), msg('keep')]))
                rows = self.records()
                self.assertEqual(rows[-2]['event'], 'compression_result')
                self.assertEqual(rows[-2]['C'], 'C'*4000)
                self.assertEqual(rows[-1]['event'], 'compression_discarded')
                self.assertEqual(rows[-1]['cause'], 'shutdown' if close else 'not_smaller')
                self.assertEqual(rows[-1]['memory_after'], [])
                self.assertEqual(manager._memory, ())

    def test_timeout_keeps_snapshot_and_failure_no_fake_response(self):
        manager = self.manager(Mock(side_effect=TimeoutError()))
        manager.transform(payload([msg('a'*800), msg('keep')]))
        self.assertEqual([r['event'] for r in self.records()], ['compression_snapshot', 'compression_failed'])
        self.assertEqual(self.records()[-1]['cause'], 'timeout')
        self.assertEqual(len(manager._raw), 2)

    def test_fsync_private_append_and_concurrent_records(self):
        event = {'event':'test', 'call_id':'one', 'C':'private summary'}
        with patch('src.context_compression.audit.os.fsync', wraps=__import__('os').fsync) as sync:
            self.audit(event)
            self.assertGreaterEqual(sync.call_count, 1)
        self.assertEqual(stat.S_IMODE(self.audit.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.audit.directory.stat().st_mode), 0o700)
        reopened = CompressionAudit(self.root, IDENTITY)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: reopened({'event':'test', 'call_id':str(i)}), range(20)))
        self.assertEqual(len(self.records()), 21)
        self.assertEqual(self.records()[0]['C'], 'private summary')

    def test_symlink_and_truncated_tail_not_overwritten(self):
        other = self.root / 'target'
        other.write_text('unchanged')
        self.audit.path.symlink_to(other)
        with self.assertRaises(AuditWriteError):
            self.audit({'event':'test'})
        self.assertEqual(other.read_text(), 'unchanged')
        fresh = CompressionAudit(self.root / 'other', IDENTITY)
        fresh.path.write_text('{"partial":')
        with self.assertRaises(AuditWriteError):
            fresh({'event':'test'})
        self.assertEqual(fresh.path.read_text(), '{"partial":')

    def test_snapshot_or_result_write_failure_keeps_memory_and_reports(self):
        for failing_event in ('compression_snapshot', 'compression_result'):
            with self.subTest(failing_event=failing_event):
                def sink(event):
                    if event['event']==failing_event:
                        raise OSError('sensitive OS path or data')
                    self.audit(event)
                comp = Mock(return_value=CompressionResult(False, .9, 'active', 'M'))
                manager = self.manager(comp, audit_sink=sink)
                manager.transform(payload([msg('a'*800), msg('keep')]))
                self.assertEqual(manager._memory, ())
                self.assertEqual(len(manager._raw), 2)
                self.assertEqual(comp.call_count, int(failing_event=='compression_result'))
                self.assertTrue(any(e['event']=='compression_audit_error' for e in self.events))
                self.assertNotIn('sensitive', str(self.events))

    def test_terminal_write_failure_does_not_mislabel_applied_state(self):
        def sink(event):
            if event['event']=='compression_applied':
                raise OSError('disk full')
            self.audit(event)
        manager = self.manager(Mock(return_value=CompressionResult(False, .9, 'active', 'M')), audit_sink=sink)
        manager.transform(payload([msg('a'*800), msg('keep')]))
        self.assertEqual(manager._memory, ('M',))
        self.assertTrue(any(e['event']=='compression_applied' for e in self.events))
        self.assertFalse(any(e['event']=='compression_failed' for e in self.events))
        self.assertEqual(self.records()[-1]['C'], 'M')

    def test_raw_responses_persist_before_validation(self):
        for response in (envelope(finish_reason='length'), {'not':'a valid envelope'}):
            with self.subTest(response=response), mock_upstream(lambda h,r: respond_json(h,response)) as upstream:
                comp = OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}', 'SECRET-KEY',
                                        'model', self.config, event_sink=self.events.append, audit_sink=self.audit)
                manager = self.manager(comp)
                manager.transform(payload([msg('a'*800), msg('keep')]))
                saved = [r for r in self.records() if r['event']=='compressor_response'][-1]
                self.assertEqual(json.loads(saved['body']), response)
                self.assertEqual(manager._memory, ())
                self.assertNotIn('SECRET-KEY', self.audit.path.read_text())

    def test_response_storage_failure_preserves_returned_usage(self):
        def sink(event):
            if event['event']=='compressor_response':
                raise OSError('disk full')
            self.audit(event)
        with mock_upstream() as upstream:
            comp = OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}', '', 'model', self.config,
                                    event_sink=self.events.append, audit_sink=sink)
            manager = self.manager(comp, audit_sink=sink)
            manager.transform(payload([msg('a'*800), msg('keep')]))
        usage = next(e for e in self.events if e['event']=='compressor_usage')
        self.assertEqual(usage['usage']['prompt_tokens'], 51)
        self.assertEqual(manager._memory, ())

    def test_invalid_json_response_and_http_error_body_are_saved(self):
        for status, body in ((200, b'{bad JSON'), (402, b'{"error":{"message":"quota"}}')):
            def respond(h, req):
                h.send_response(status)
                h.send_header('Content-Length',str(len(body)))
                h.end_headers()
                h.wfile.write(body)
            with self.subTest(status=status), mock_upstream(respond) as upstream:
                comp = OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}', '', 'model', self.config,
                                        event_sink=self.events.append, audit_sink=self.audit)
                manager = self.manager(comp)
                manager.transform(payload([msg('a'*800),msg('keep')]))
                response = [e for e in self.records() if e['event']=='compressor_response'][-1]
                self.assertEqual(response['body'], body.decode())
                self.assertEqual(manager._memory, ())

    def test_fsync_failure_latches_writer_and_surfaces_sanitized_error(self):
        with patch('src.context_compression.audit.os.fsync', side_effect=OSError('private details')):
            with self.assertRaisesRegex(AuditWriteError, '^Compression audit write failed$'):
                self.audit({'event':'test','C':'saved before failed fsync'})
        with self.assertRaises(AuditWriteError):
            self.audit({'event':'next'})

    def test_production_proxy_wires_private_audit_without_metadata_body(self):
        def respond(h, req):
            respond_json(h, envelope() if 'response_format' in req['payload'] else {'choices': [], 'usage': {}})
        with mock_upstream(respond) as upstream:
            server = create_server({'instance_id':IDENTITY, 'task':'the task', 'upstream_model':'model',
                'upstream_base_url':f'http://127.0.0.1:{upstream.server_port}', 'upstream_api_key':'SECRET-KEY',
                'port':0, 'compression':{'trigger_tokens':80}, 'log_dir':str(self.root)})
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.02}, daemon=True)
            thread.start()
            try:
                request = Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                    data=json.dumps(payload([msg('a'*800),msg('keep')])).encode(),
                    headers={'Content-Type':'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                rows = self.records()
                self.assertEqual([r['event'] for r in rows], ['compression_snapshot','compressor_request',
                    'compressor_response','compression_result','compression_applied'])
                actual = upstream.captured
                saved_request = next(r['request'] for r in rows if r['event']=='compressor_request')
                self.assertEqual(saved_request, actual[0]['payload'])
                result = next(r for r in rows if r['event']=='compression_result')
                self.assertIn(memory_messages((result['C'],))[0], actual[1]['payload']['messages'])
                metadata = (self.root/'events.jsonl').read_text()
                self.assertNotIn(result['C'], metadata)
                self.assertNotIn('SECRET-KEY', self.audit.path.read_text())
            finally:
                shutdown_server(server)
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
