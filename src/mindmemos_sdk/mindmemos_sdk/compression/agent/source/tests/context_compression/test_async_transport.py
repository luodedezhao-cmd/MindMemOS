"""Cancellation and billing through real HTTP transport, never a real provider."""
import json
import threading
import unittest

from src.context_compression.async_core import AsyncContextManager
from src.context_compression.compressor import OpenAICompressor
from src.context_compression.types import CompressionConfig
from src.ours_observation.metrics import summarize, accounting_allows_queue
from tests.context_compression.test_core import IDENTITY, msg, payload
from tests.context_compression.test_transport import mock_upstream, respond_json, envelope
from tests.ours_observation.test_metrics import sample


class AsyncTransportTests(unittest.TestCase):
    def manager(self, upstream):
        events = []
        def sink(event):
            events.append({'schema_version': 3, 'instance_id': IDENTITY, 'phase': 'execution', **event})
        config = CompressionConfig(method='v4', execution_mode='async', trigger_tokens=80,
                                   full_trigger_tokens=500, timeout_seconds=3)
        compressor = OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}/v1', '', 'model',
                                      config, event_sink=sink)
        manager = AsyncContextManager(compressor, config, task='the task', instance_id=IDENTITY, event_sink=sink)
        self.addCleanup(manager.close)
        return manager, events

    def measure(self, events):
        proxy, routing, native = sample()
        return summarize(proxy+events, routing, native, 1)

    def test_completed_unused_result_retains_actual_tokens_and_cost(self):
        usage = {'prompt_tokens': 123, 'completion_tokens': 30, 'total_tokens': 153, 'cost': .25}
        with mock_upstream(lambda h, _: respond_json(h, envelope(usage=usage))) as upstream:
            manager, events = self.manager(upstream)
            manager.transform(payload([msg('x'*800), msg('keep')]))
            self.assertTrue(manager._pending.done.wait(2))
            manager.close()
            self.assertTrue(manager.wait_closed())
            measured = self.measure(events)
            self.assertTrue(measured['coverage_verified'])
            self.assertTrue(accounting_allows_queue(measured))
            self.assertEqual(measured['compression']['discarded'], {'shutdown': 1})
            self.assertEqual(measured['compression']['applied_incremental'], 0)
            self.assertEqual(measured['execution']['compressor']['total_tokens']['total'], 153)
            self.assertEqual(measured['execution']['compressor']['billed_cost_usd']['total'], .25)

    def test_cancel_inflight_unknown_billing_does_not_block_queue(self):
        entered, release = threading.Event(), threading.Event()
        def respond(handler, _):
            entered.set()
            release.wait(5)
            respond_json(handler, envelope())
        try:
            with mock_upstream(respond) as upstream:
                manager, events = self.manager(upstream)
                manager.transform(payload([msg('x'*800), msg('keep')]))
                self.assertTrue(entered.wait(2))
                manager.close()
                self.assertTrue(manager.wait_closed())
                measured = self.measure(events)
                self.assertTrue(measured['cancellation_only_incomplete'])
                self.assertTrue(accounting_allows_queue(measured))
                self.assertIsNone(measured['execution']['compressor']['billed_cost_usd']['total'])
                self.assertEqual(manager._memory, ())
        finally:
            release.set()

    def test_second_stage_uses_frozen_original_raw_and_memory(self):
        first, release = threading.Event(), threading.Event()
        def respond(handler, request):
            p = request['payload']
            if 'response_format' in p:
                first.set()
                release.wait(5)
                respond_json(handler, envelope(dict(stage_end=True, confidence=.9, reason='closed', C=None)))
            else:
                respond_json(handler, {'choices': [{'message': {'content': 'FULL'}, 'finish_reason': 'stop'}]})
        try:
            with mock_upstream(respond) as upstream:
                manager, _ = self.manager(upstream)
                manager._memory = ('OLD_ID_17',)
                manager.transform(payload([msg('original-'+'x'*800), msg('keep')]))
                self.assertTrue(first.wait(2))
                manager.transform(payload([msg('unseen_future')], start=3))
                release.set()
                self.assertTrue(manager._pending.done.wait(2))
                self.assertEqual(len(upstream.captured), 2)
                boundary, full = [r['payload'] for r in upstream.captured]
                self.assertNotIn('OLD_ID_17', json.dumps(boundary))
                self.assertIn('OLD_ID_17', json.dumps(full))
                self.assertIn('original-', json.dumps(full))
                self.assertNotIn('unseen_future', json.dumps(full))
                manager.transform(payload([], start=4))
                self.assertEqual(manager._memory, ('FULL',))
                self.assertEqual(manager._raw, [msg('keep'), msg('unseen_future')])
        finally:
            release.set()
