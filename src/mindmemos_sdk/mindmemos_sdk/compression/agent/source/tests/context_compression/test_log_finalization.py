"""Stable accounting cutoff and call-phase attribution, entirely offline."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.context_compression.async_core import AsyncContextManager
from src.context_compression.compressor import OpenAICompressor
from src.context_compression.proxy import EventLog, ProxyHandler
from src.context_compression.types import CompressionConfig
from src.ours_observation.metrics import summarize, accounting_allows_queue
from tests.context_compression.test_core import IDENTITY, msg, payload
from tests.context_compression.test_transport import mock_upstream, respond_json, envelope
from tests.ours_observation.test_metrics import e, sample


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.phase = self.root/'phase'
        self.phase.write_text('execution')
        self.log = EventLog(self.root/'logs', IDENTITY, self.phase)

    def read(self, name='events.jsonl'):
        p = self.log.directory/name
        return [json.loads(line) for line in p.read_text().split('\n') if line] if p.exists() else []

    def start(self):
        proxy, routing, _ = sample()
        for event in proxy:
            self.log(event)
        self.log(e('compression_started', 'c', mode='v4', compression_mode='boundary'))
        self.log(e('compressor_started', 'c', source='compressor', compression_id='c', compression_mode='boundary'))
        return routing

    def usage(self):
        return e('compressor_usage', 'c', source='compressor', compression_id='c', compression_mode='boundary',
                 usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': .2}, success=True)

    def test_started_phase_survives_global_switch_even_without_explicit_finish_phase(self):
        routing = self.start()
        self.phase.write_text('scorer')
        usage = self.usage()
        usage.pop('phase')
        self.log(usage)
        self.log(e('compression_discarded', 'c', cause='shutdown'))
        self.log.finalize(True)
        result = summarize(self.read(), routing, [], 1)
        self.assertTrue(result['coverage_verified'])
        self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])
        self.assertEqual(result['actual_usage']['execution']['compressor']['billed_cost_usd']['total'], .2)
        self.assertEqual(result['scorer']['compressor']['calls'], 0)

    def test_late_usage_cannot_mutate_sealed_ledger_or_turn_unknown_into_zero(self):
        routing = self.start()
        self.log.finalize(False)
        before = (self.log.directory/'events.jsonl').read_bytes()
        self.phase.write_text('scorer')
        self.log({**self.usage(), 'phase': 'scorer'})
        self.log(e('compression_discarded', 'c', cause='shutdown'))
        self.log.finalize(True)  # idempotent; do not rewrite the historical cutoff
        self.assertEqual((self.log.directory/'events.jsonl').read_bytes(), before)
        self.assertFalse((self.log.directory/'compressor_usage.jsonl').exists())
        late = self.read('late_events.jsonl')
        self.assertEqual(late[0]['phase'], 'execution')
        self.assertEqual(late[0]['usage']['cost'], .2)
        self.assertTrue(late[0]['after_finalization'])
        result = summarize(self.read(), routing, [], 1)
        self.assertFalse(result['coverage_verified'])
        self.assertFalse(result['billing_complete'])
        self.assertTrue(result['shutdown_only_incomplete'])
        self.assertTrue(accounting_allows_queue(result))
        self.assertIsNone(result['execution']['compressor']['total_tokens']['total'])
        self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])
        self.assertFalse(next(r for r in result['calls'] if r['source'] == 'compressor')['complete'])
        with self.assertRaises(ValueError):
            summarize(self.read()+late, routing, [], 1)

    def test_shutdown_exception_does_not_hide_other_meter_defects(self):
        routing = self.start()
        self.log.finalize(False)
        events = self.read()
        for defect in ('main_usage', 'audit', 'identity', 'missing_marker'):
            with self.subTest(defect=defect):
                altered = [dict(row) for row in events]
                if defect == 'main_usage':
                    next(r for r in altered if r['event'] == 'main_usage')['usage'] = None
                elif defect == 'audit':
                    altered.insert(-1, e('compression_audit_error', 'c'))
                elif defect == 'identity':
                    next(r for r in altered if r['event'] == 'compressor_abandoned')['phase'] = 'scorer'
                else:
                    altered = [r for r in altered if r['event'] != 'proxy_finalized']
                try:
                    result = summarize(altered, routing, [], 1)
                except ValueError:
                    continue
                self.assertFalse(accounting_allows_queue(result))

    def test_received_usage_remains_known_when_only_job_cleanup_misses_deadline(self):
        routing = self.start()
        self.log(self.usage())
        self.log.finalize(False)
        result = summarize(self.read(), routing, [], 1)
        self.assertFalse(result['billing_complete'])
        self.assertTrue(result['actual_usage']['billing_complete'])
        self.assertTrue(result['shutdown_only_incomplete'])
        self.assertTrue(accounting_allows_queue(result))
        self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])
        self.assertEqual(result['actual_usage']['execution']['compressor']['billed_cost_usd']['total'], .2)
        self.assertEqual(result['compression']['unfinished_at_shutdown'], 1)

    def test_cancelled_call_finished_but_parent_cleanup_is_still_pending(self):
        routing = self.start()
        self.log({**self.usage(), 'usage': None, 'cancelled': True,
                  'request_dispatched': True, 'success': False})
        self.log.finalize(False)
        result = summarize(self.read(), routing, [], 1)
        self.assertTrue(result['shutdown_only_incomplete'])
        self.assertTrue(accounting_allows_queue(result))
        self.assertFalse(result['billing_complete'])
        self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])

    def test_two_stage_usage_follows_parent_application(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                log = EventLog(self.root/str(applied), IDENTITY, self.phase)
                proxy, routing, _ = sample()
                proxy += [e('compression_started', 'c', mode='v4', compression_mode='boundary')]
                for call, mode in (('c', 'boundary'), ('f', 'full')):
                    proxy += [e('compressor_started', call, source='compressor', compression_id='c', compression_mode=mode),
                              {**self.usage(), 'call_id': call, 'compression_mode': mode}]
                    if call == 'c':
                        proxy.append(e('compression_decision', 'c', stage_end=True))
                proxy.append(e('compression_applied' if applied else 'compression_discarded', 'c',
                               stage_end=True, cause='shutdown'))
                for event in proxy:
                    log(event)
                log.finalize(True)
                events = [json.loads(s) for s in (log.directory/'events.jsonl').read_text().split('\n') if s]
                result = summarize(events, routing, [], 1)
                self.assertTrue(accounting_allows_queue(result))
                self.assertEqual(result['actual_usage']['execution']['compressor']['billed_cost_usd']['total'], .4)
                self.assertEqual(result['actual_usage']['execution']['compressor']['total_tokens']['total'], 30)
                self.assertEqual(result['execution']['agent']['billed_cost_usd']['total'], .123)
                self.assertEqual(result['compression']['unused_model_calls'], 0 if applied else 2)
                if applied:
                    self.assertEqual(result['execution']['compressor']['billed_cost_usd']['total'], .4)
                    self.assertTrue(result['billing_complete'])
                else:
                    self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])
                    self.assertEqual(result['execution']['compressor']['billed_cost_usd']['unknown_calls'], 2)
                    self.assertEqual(result['execution']['compressor']['billed_cost_usd']['known_sum'], 0)
                    self.assertTrue(all(r['missing_usage_reason'] == 'unused_compression_policy'
                                        for r in result['calls'] if r['source'] == 'compressor'))

    def test_unused_not_dispatched_is_unknown_in_experiment_but_zero_in_actual_view(self):
        routing = self.start()
        self.log({**self.usage(), 'usage': None, 'cancelled': True,
                  'request_dispatched': False, 'success': False})
        self.log(e('compression_failed', 'c', cause='cancelled'))
        self.log.finalize(True)
        result = summarize(self.read(), routing, [], 1)
        self.assertTrue(accounting_allows_queue(result))
        self.assertIsNone(result['execution']['compressor']['billed_cost_usd']['total'])
        self.assertEqual(result['actual_usage']['execution']['compressor']['billed_cost_usd']['total'], 0)

    def test_unused_policy_cannot_hide_missing_agent_usage(self):
        routing = self.start()
        self.log(self.usage())
        self.log(e('compression_discarded', 'c', cause='shutdown'))
        self.log.finalize(True)
        events = self.read()
        next(r for r in events if r['event'] == 'main_usage')['usage'] = None
        result = summarize(events, routing, [], 1)
        self.assertFalse(result['unused_policy_allows_queue'])
        self.assertFalse(accounting_allows_queue(result))

    def test_adyn_transport_keeps_execution_after_phase_switch(self):
        first, release = threading.Event(), threading.Event()
        def responder(handler, request):
            first.set()
            release.wait(3)
            respond_json(handler, envelope())
        config = CompressionConfig(method='dual_a', execution_mode='async', trigger_tokens=80, full_trigger_tokens=500)
        try:
            with mock_upstream(responder) as upstream:
                compressor = OpenAICompressor(f'http://127.0.0.1:{upstream.server_port}/v1', '', 'model', config,
                                              event_sink=self.log)
                manager = AsyncContextManager(compressor, config, task='the task', instance_id=IDENTITY,
                                              event_sink=self.log)
                self.addCleanup(manager.close)
                manager.transform(payload([msg('x'*800), msg('keep')]))
                self.assertTrue(first.wait(2))
                self.phase.write_text('scorer')
                release.set()
                self.assertTrue(manager._pending.done.wait(2))
                manager.close()
                self.assertTrue(manager.wait_closed())
                self.log.finalize(True)
                calls = [r for r in self.read() if r['event'] in ('compressor_started', 'compressor_usage')]
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(r['phase'] == 'execution' for r in calls))
        finally:
            release.set()

    def test_shutdown_endpoint_reports_false_drain_and_seals_before_ack(self):
        from unittest.mock import Mock
        self.start()
        handler = object.__new__(ProxyHandler)
        handler.path = '/shutdown'
        handler.server = Mock()
        handler.server.event_sink = self.log
        handler.server.context_manager.wait_closed.return_value = False
        def response(status, value):
            self.assertTrue(self.log.sealed)
            self.assertEqual(status, 200)
            self.assertFalse(value['compression_drained'])
            self.assertTrue(value['ledger_finalized'])
        handler._json = response
        with patch('src.context_compression.proxy.threading.Thread'):
            handler.do_POST()
