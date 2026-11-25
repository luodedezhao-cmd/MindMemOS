import json
import threading
import time
import unittest

from src.context_compression.cancellation import CompressionCancelled
from src.context_compression.compressor import OpenAICompressor
from src.context_compression.types import CompressionConfig
from tests.context_compression.test_transport import mock_upstream, snapshot, envelope, respond_json
from tests.context_compression import test_transport as transport_tests
from tests.context_compression.test_core import payload, msg


class CancellationTests(unittest.TestCase):
    def compressor(self, upstream, events, audit=None):
        return OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}/v1', '', 'model',
                               CompressionConfig(timeout_seconds=10), event_sink=events.append, audit_sink=audit)

    def test_cancel_before_dispatch_does_not_call_upstream(self):
        events=[]
        with mock_upstream() as upstream:
            comp=self.compressor(upstream, events)
            comp.cancel()
            with self.assertRaises(CompressionCancelled): comp(snapshot())
            self.assertEqual(upstream.captured, [])
        self.assertTrue(events[-1]['cancelled'])
        self.assertFalse(events[-1]['request_dispatched'])
        self.assertIsNone(events[-1]['usage'])

    def test_cancel_between_request_audit_and_dispatch(self):
        events=[]
        def audit(e):
            if e['event']=='compressor_request': comp.cancel()
        with mock_upstream() as upstream:
            comp=self.compressor(upstream,events,audit)
            with self.assertRaises(CompressionCancelled): comp(snapshot())
            self.assertEqual(upstream.captured, [])

    def test_cancel_interrupts_headers_success_body_and_error_body(self):
        for status in (None,200,402):
            with self.subTest(status=status):
                entered,release=threading.Event(),threading.Event()
                events=[]; errors=[]; audit=[]
                def respond(h,q):
                    if status is not None:
                        h.send_response(status); h.send_header('Content-Length','10000'); h.end_headers()
                        h.wfile.write(b'{'); h.wfile.flush()
                    entered.set(); release.wait(3)
                try:
                    with mock_upstream(respond) as upstream:
                        comp=self.compressor(upstream,events,audit.append)
                        def run():
                            try: comp(snapshot())
                            except Exception as exc: errors.append(exc)
                        t=threading.Thread(target=run,daemon=True); t.start()
                        self.assertTrue(entered.wait(1))
                        start=time.monotonic(); comp.cancel(); t.join(1)
                        self.assertFalse(t.is_alive())
                        self.assertLess(time.monotonic()-start,.9)
                        self.assertIsInstance(errors[0],CompressionCancelled)
                        self.assertTrue(events[-1]['cancelled'])
                        self.assertTrue(events[-1]['request_dispatched'])
                        self.assertIsNone(events[-1]['usage'])
                        self.assertFalse(any(e['event']=='compressor_response' and e['body_complete'] for e in audit))
                finally: release.set()

    def test_usage_received_before_cancel_is_not_deleted(self):
        events=[]
        raw={'prompt_tokens':51,'completion_tokens':12,'total_tokens':63,'cost':.123}
        def audit(e):
            if e['event']=='compressor_response': comp.cancel()
        with mock_upstream(lambda h,q:respond_json(h,envelope(usage=raw))) as upstream:
            comp=self.compressor(upstream,events,audit)
            with self.assertRaises(CompressionCancelled): comp(snapshot())
        self.assertEqual(events[-1]['usage']['cost'],.123)
        self.assertTrue(events[-1]['cancelled'])


class ProxyCancellationTests(unittest.TestCase):
    setUp = transport_tests.ProxyHTTPTests.setUp
    tearDown = transport_tests.ProxyHTTPTests.tearDown
    proxy = transport_tests.ProxyHTTPTests.proxy
    request = transport_tests.ProxyHTTPTests.request
    wait_log = transport_tests.ProxyHTTPTests.wait_log
    def test_task_shutdown_cancels_compressor_without_main_forward(self):
        entered,release=threading.Event(),threading.Event()
        def respond(h,q):
            entered.set(); release.wait(3)
        try:
            with mock_upstream(respond) as upstream:
                proxy=self.proxy(upstream,{'trigger_tokens':80})
                p=payload([msg('x'*800),msg('keep')])
                p['_wildclaw_context']['messages'][0]['content']='Make report'
                outputs=[]
                t=threading.Thread(target=lambda:outputs.append(self.request(proxy,p)),daemon=True); t.start()
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request(proxy,{},path='/shutdown')[0],200)
                t.join(1); self.assertFalse(t.is_alive())
                self.assertEqual(outputs[0][0],503)
                self.assertEqual(len(upstream.captured),1)
                self.assertEqual(proxy.context_manager._memory,())
                rows=self.wait_log('events.jsonl')
                self.assertTrue(any(e['event']=='compression_failed' and e['cause']=='cancelled' for e in rows))
                self.assertFalse(any(e['event']=='main_started' for e in rows))
        finally: release.set()
