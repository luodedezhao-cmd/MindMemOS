"""Deterministic background scheduling tests; no API or sleep-based ordering."""
from copy import deepcopy
import threading
import unittest

from src.context_compression.async_core import AsyncContextManager
from src.context_compression.core import HistoryError, estimated_tokens, memory_messages
from src.context_compression.types import CompressionConfig, CompressionResult
from tests.context_compression.test_core import IDENTITY, PREFIX, TASK, msg, payload


class GateCompressor:
    def __init__(self, results):
        self.results = results
        self.snapshots = []
        self.entered = [threading.Event() for _ in results]
        self.release = [threading.Event() for _ in results]
        self.cancellations = []
        self.cancelled = False

    def __call__(self, snapshot, cancellation=None):
        self.cancellations.append(cancellation)
        index = len(self.snapshots)
        self.snapshots.append(snapshot)
        self.entered[index].set()
        if not self.release[index].wait(5):
            raise TimeoutError('test gate was not released')
        result = self.results[index]
        if isinstance(result, Exception):
            raise result
        return result

    def cancel(self):
        self.cancelled = True
        for gate in self.release:
            gate.set()


class AsyncCoreTests(unittest.TestCase):
    def manager(self, results=None, **config):
        compressor = GateCompressor(results or [CompressionResult(False, .9, 'active', 'INC')])
        events, audit = [], []
        options = dict(method='v4', execution_mode='async', trigger_tokens=80, full_trigger_tokens=500)
        options.update(config)
        manager = AsyncContextManager(compressor, CompressionConfig(**options), task='the task',
                                      instance_id=IDENTITY, event_sink=events.append, audit_sink=audit.append)
        def cleanup():
            manager.close()
            self.assertTrue(manager.wait_closed(2))
        self.addCleanup(cleanup)
        return manager, compressor, events, audit

    def finish(self, manager, compressor, index=0):
        self.assertTrue(compressor.entered[index].wait(2))
        job = manager._pending
        compressor.release[index].set()
        self.assertTrue(job.done.wait(2))
        return job

    def test_nonblocking_and_apply_only_on_next_main_request(self):
        manager, comp, events, audit = self.manager()
        request = payload([msg('x' * 800), msg('keep')])
        unchanged = deepcopy(request)
        result = manager.transform(request)
        self.assertTrue(comp.entered[0].wait(2))
        self.assertFalse(manager._pending.done.is_set())
        self.assertEqual(result['messages'], PREFIX + [TASK] + manager._raw)
        self.assertEqual(request, unchanged)
        job = self.finish(manager, comp)
        self.assertEqual(manager._memory, ())
        self.assertEqual([r['C'] for r in audit if r['event'] == 'compression_result'], ['INC'])
        aux = {'messages': [TASK]}
        self.assertEqual(manager.transform(aux), aux)
        self.assertIs(manager._pending, job)
        result = manager.transform(payload([msg('latest')], start=3, host=[]))
        self.assertEqual(result['messages'], PREFIX + [TASK] + memory_messages(('INC',)) + [msg('keep'), msg('latest')])
        self.assertEqual(manager._base_seq, 1)
        applied = next(e for e in events if e['event'] == 'compression_applied')
        self.assertEqual((applied['start'], applied['end'], applied['steps_while_pending']), (0, 1, 1))

    def test_append_keeps_old_memory_byte_stable(self):
        manager, comp, _, _ = self.manager()
        manager._memory = ('OLD1', 'OLD2')
        old_messages = memory_messages(manager._memory)
        request = payload([msg('x' * 800), msg('keep')])
        manager.transform(request)
        self.finish(manager, comp)
        result = manager.transform(request)  # retry may consume readiness, not ingest again
        self.assertEqual(manager._memory, ('OLD1', 'OLD2', 'INC'))
        self.assertEqual(result['messages'][2:4], old_messages)
        self.assertEqual(manager._raw, [msg('keep')])
        self.assertEqual(len(comp.snapshots), 1)

    def test_semantic_full_replaces_snapshot_not_new_raw(self):
        manager, comp, _, _ = self.manager([CompressionResult(True, .9, 'closed', 'FULL')])
        manager._memory = ('OLD',)
        manager.transform(payload([msg('x' * 800), msg('keep')]))
        manager.transform(payload([msg('during')], start=3))
        self.finish(manager, comp)
        manager.transform(payload([msg('after')], start=4))
        self.assertEqual(manager._memory, ('FULL',))
        self.assertEqual(manager._raw, [msg('keep'), msg('during'), msg('after')])
        self.assertEqual(comp.snapshots[0].memory, ('OLD',))
        self.assertEqual(comp.snapshots[0].raw, (msg('x' * 800),))

    def test_threshold_crossing_does_not_launch_concurrent_or_force_full_later(self):
        manager, comp, _, _ = self.manager(full_trigger_tokens=350)
        manager.transform(payload([msg('x' * 1100), msg('keep')]))
        self.assertTrue(comp.entered[0].wait(2))
        # Current M+R now exceeds full threshold; after applying it will not.
        manager.transform(payload([msg('y' * 200)], start=3))
        manager.transform(payload([msg('tail')], start=4))
        self.assertGreaterEqual(estimated_tokens(manager._raw[:-1]), 350)
        self.assertEqual(len(comp.snapshots), 1)
        self.finish(manager, comp)
        manager.transform(payload([msg('tail')], start=4))
        self.assertIsNone(manager._pending)
        self.assertEqual(len(comp.snapshots), 1)

    def test_recompute_after_apply_can_launch_capacity_full(self):
        manager, comp, _, _ = self.manager([
            CompressionResult(False, .9, 'active', 'INC'),
            CompressionResult(True, 1, 'capacity', 'FULL')])
        manager.transform(payload([msg('x' * 800), msg('keep')]))
        self.assertTrue(comp.entered[0].wait(2))
        manager.transform(payload([msg('y' * 2400), msg('tail')], start=3))
        self.assertEqual(len(comp.snapshots), 1)
        self.finish(manager, comp)
        request = payload([msg('next')], start=5)
        manager.transform(request)
        self.assertTrue(comp.entered[1].wait(2))
        snapshot = comp.snapshots[1]
        self.assertEqual(snapshot.compression_mode, 'full')
        self.assertEqual(snapshot.memory, ('INC',))
        self.assertEqual(snapshot.raw, (msg('keep'), msg('y' * 2400), msg('tail')))
        self.assertEqual((snapshot.start, snapshot.end), (1, 4))
        self.finish(manager, comp, 1)
        manager.transform(request)
        self.assertEqual(manager._memory, ('FULL',))
        self.assertEqual(manager._raw, [msg('next')])

    def test_failed_and_expanding_results_preserve_state_and_back_off(self):
        for bad in (ValueError('failed'), None, CompressionResult(False, .9, 'active', ''),
                    CompressionResult(False, .9, 'active', 'x' * 3000)):
            with self.subTest(bad=bad):
                manager, comp, _, _ = self.manager([bad])
                raw = [msg('x' * 800), msg('keep')]
                manager.transform(payload(raw))
                self.finish(manager, comp)
                manager.transform(payload([msg('new')], start=3))
                self.assertEqual(manager._raw, raw + [msg('new')])
                self.assertEqual(manager._memory, ())
                manager.transform(payload([msg('next')], start=4))
                self.assertEqual(len(comp.snapshots), 1)
                self.assertEqual(manager._retry_step, 5)

    def test_stale_revision_or_changed_raw_is_discarded(self):
        for kind in ('revision', 'raw', 'memory'):
            manager, comp, events, _ = self.manager()
            manager.transform(payload([msg('x' * 800), msg('keep')]))
            self.finish(manager, comp)
            if kind == 'revision':
                manager._revision += 1
            elif kind == 'raw':
                manager._raw[0]['content'] = 'changed'
            else:
                manager._memory = ('changed',)
            before = deepcopy((manager._memory, manager._raw))
            manager.transform(payload([], start=3))
            self.assertEqual((manager._memory, manager._raw), before)
            self.assertTrue(any(e.get('cause') == 'stale_snapshot' for e in events))

    def test_ready_time_is_not_counted_as_compression_latency(self):
        manager, comp, events, _ = self.manager()
        request = payload([msg('x' * 800), msg('keep')])
        manager.transform(request)
        job = self.finish(manager, comp)
        job.started -= 1000  # simulate a long interval with no main requests
        job.ready_at -= 1000
        manager.transform(request)
        self.assertEqual(manager._memory, ('INC',))
        applied = next(e for e in events if e['event'] == 'compression_applied')
        self.assertGreater(applied['ready_to_apply_seconds'], 999)
        self.assertLess(applied['elapsed_seconds'], 2)

    def test_timeout_does_not_apply(self):
        manager, comp, events, _ = self.manager()
        request = payload([msg('x' * 800), msg('keep')])
        manager.transform(request)
        job = self.finish(manager, comp)
        job.elapsed = 121
        manager.transform(request)
        self.assertEqual(manager._memory, ())
        self.assertTrue(any(e.get('cause') == 'timeout' for e in events))

    def test_parallel_tool_group_is_covered_atomically_and_tail_is_retained(self):
        manager, comp, _, _ = self.manager()
        calls = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': key, 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{}'}}
            for key in ('a', 'b')]}
        results = [{'role': 'tool', 'tool_call_id': key, 'content': 'x'*350} for key in ('b', 'a')]
        raw = [calls] + results
        manager.transform(payload(raw))  # newest complete group is protected
        self.assertIsNone(manager._pending)
        manager.transform(payload([msg('tail')], start=4))
        self.finish(manager, comp)
        self.assertEqual(list(comp.snapshots[0].raw), raw)
        manager.transform(payload([msg('next')], start=5))
        self.assertEqual(manager._base_seq, 3)
        self.assertEqual(manager._raw, [msg('tail'), msg('next')])

    def test_partial_group_does_not_consume_a_ready_result(self):
        manager, comp, _, _ = self.manager()
        manager.transform(payload([msg('x'*800), msg('tail')]))
        job = self.finish(manager, comp)
        call = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 't', 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{}'}}]}
        with self.assertRaises(HistoryError):
            manager.transform(payload([call], start=3))
        self.assertIs(manager._pending, job)
        self.assertEqual(manager._memory, ())
        result = {'role': 'tool', 'tool_call_id': 't', 'content': 'result'}
        manager.transform(payload([result], start=4))
        self.assertEqual(manager._memory, ('INC',))
        self.assertEqual(manager._raw, [msg('tail'), call, result])

    def test_shutdown_cancels_and_accounts_unused_finished_result(self):
        for ready in (False, True):
            manager, comp, events, audit = self.manager()
            manager.transform(payload([msg('x' * 800), msg('keep')]))
            self.assertTrue(comp.entered[0].wait(2))
            if ready:
                self.finish(manager, comp)
            manager.close()
            self.assertTrue(comp.cancelled)
            self.assertTrue(manager.wait_closed(2))
            self.assertEqual(manager._memory, ())
            self.assertEqual(len(manager._raw), 2)
            self.assertTrue(any(e.get('cause') == 'shutdown' for e in events))
            self.assertTrue(any(e['event'] == 'compression_result' for e in audit))
            with self.assertRaises(HistoryError):
                manager.transform(payload([], start=3))

    def test_snapshot_payload_mutation_cannot_mutate_live_history(self):
        manager, comp, _, _ = self.manager()
        manager.transform(payload([msg('x' * 800), msg('keep')]))
        self.assertTrue(comp.entered[0].wait(2))
        comp.snapshots[0].raw[0]['content'] = 'bad'
        self.assertEqual(manager._raw[0]['content'], 'x' * 800)
        self.finish(manager, comp)
        manager.transform(payload([], start=3))
        self.assertEqual(manager._memory, ('INC',))

    def test_sync_config_round_trip_and_async_scope(self):
        self.assertNotIn('execution_mode', CompressionConfig().to_dict())
        config = CompressionConfig(method='v4', full_trigger_tokens=16384, execution_mode='async')
        self.assertEqual(CompressionConfig.from_dict(config.to_dict()), config)
        with self.assertRaises(ValueError):
            CompressionConfig(method='p0', execution_mode='async')
        with self.assertRaises(ValueError):
            CompressionConfig(execution_mode='other')
