"""Preemptive background compression for dual_a: a newer trigger wins.

v4 async is single-flight (a trigger during a job is skipped). dual_a instead
supersedes the in-flight job, because the older summary would cover a stale
prefix of a history the agent has already moved past.
"""
from copy import deepcopy
import threading
import unittest

from src.context_compression.async_core import AsyncContextManager
from src.context_compression.core import memory_messages
from src.context_compression.types import CompressionConfig, CompressionResult
from tests.context_compression.test_async_core import GateCompressor
from tests.context_compression.test_core import IDENTITY, PREFIX, TASK, msg, payload


class PreemptibleCompressor(GateCompressor):
    """Unblocks a cancelled call the way a cancelled socket would."""

    def __call__(self, snapshot, cancellation=None):
        if cancellation is not None:
            # A real cancellation interrupts the socket; here it opens the gate.
            threading.Thread(target=self._watch, args=(cancellation, len(self.snapshots)),
                             daemon=True).start()
        return super().__call__(snapshot, cancellation)

    def _watch(self, cancellation, index):
        cancellation.cancelled.wait(5)
        if index < len(self.release):
            self.release[index].set()


class DualAAsyncTests(unittest.TestCase):
    # An escalated successor runs in full mode, so its result must replace.
    ESCALATION = [CompressionResult(False, 1.0, 'Capacity policy: incremental', 'C1'),
                  CompressionResult(True, 1.0, 'Capacity policy: full', 'C2')]

    def manager(self, results=None, method='dual_a', **config):
        compressor = PreemptibleCompressor(results or list(self.ESCALATION))
        events, audit = [], []
        options = dict(method=method, execution_mode='async', trigger_tokens=80,
                       full_trigger_tokens=500000, retry_after_steps=1)  # full unreachable by default
        options.update(config)
        manager = AsyncContextManager(compressor, CompressionConfig(**options), task='the task',
                                      instance_id=IDENTITY, event_sink=events.append,
                                      audit_sink=audit.append)
        self.addCleanup(lambda: (manager.close(), manager.wait_closed(2)))
        return manager, compressor, events, audit

    def test_newer_trigger_supersedes_inflight_job(self):
        manager, comp, events, audit = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        first = manager._pending
        # An escalation to full arrives while the delta is still blocked upstream.
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        self.assertTrue(first.cancellation.cancelled.is_set())
        self.assertIsNot(manager._pending, first)
        superseded = [e for e in events if e['event'] == 'compression_superseded']
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]['compression_id'], first.snapshot.call_id)
        self.assertEqual(superseded[0]['successor_id'], manager._pending.snapshot.call_id)
        # The successor covers a strictly longer prefix than the job it replaced.
        self.assertGreater(comp.snapshots[1].end, comp.snapshots[0].end)
        self.assertEqual(comp.snapshots[1].raw[:len(comp.snapshots[0].raw)], comp.snapshots[0].raw)
        # Only the successor's summary may reach memory.
        comp.release[1].set()
        self.assertTrue(manager._pending.done.wait(2))
        manager.transform(payload([msg('after')], start=5))
        self.assertEqual(manager._memory, ('C2',))   # full replaced everything
        self.assertNotIn('C1', manager._memory)

    def test_result_replaces_only_its_own_snapshot_prefix(self):
        manager, comp, _, _ = self.manager()
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        comp.release[0].set()
        self.assertTrue(manager._pending.done.wait(2))
        # 'during' arrived while the job ran; it is outside the snapshot prefix.
        manager.transform(payload([msg('during')], start=3))
        self.assertEqual(manager._memory, ('C1',))
        self.assertEqual([m['content'] for m in manager._raw], ['keep1', 'during'])

    def test_incremental_is_not_superseded_by_another_incremental(self):
        """Same-mode triggers must not discard work: that is what starved runs."""
        manager, comp, events, _ = self.manager(results=[
            CompressionResult(False, 1.0, 'Capacity policy: incremental', 'C1')])
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        job = manager._pending
        for index in range(3):
            manager.transform(payload([msg('b' * 800), msg('k%d' % index)], start=3 + index * 2))
            self.assertIs(manager._pending, job, 'incremental must not preempt incremental')
        self.assertEqual(len(comp.snapshots), 1)
        self.assertFalse(job.cancellation.cancelled.is_set())
        inflight = [e for e in events if e['event'] == 'compression_inflight']
        self.assertTrue(inflight)
        self.assertTrue(all(e['inflight_mode'] == 'incremental'
                            and e['prospective_mode'] == 'incremental' for e in inflight), inflight)

    def test_full_supersedes_a_running_incremental(self):
        """The one escalation worth the loss: the in-flight delta is now too small."""
        manager, comp, events, _ = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        victim = manager._pending
        self.assertEqual(victim.snapshot.compression_mode, 'incremental')
        manager._memory = ('M' * 2000,)  # memory now pushes M + R past the full bar
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        self.assertIsNot(manager._pending, victim)
        self.assertEqual(manager._pending.snapshot.compression_mode, 'full')
        self.assertTrue(victim.cancellation.cancelled.is_set())
        event = next(e for e in events if e['event'] == 'compression_superseded')
        self.assertEqual(event['call_id'], victim.snapshot.call_id)

    def test_a_running_full_is_never_superseded(self):
        """Once escalated, the chain terminates: full is the last word."""
        manager, comp, events, _ = self.manager(full_trigger_tokens=400, results=[
            CompressionResult(True, 1.0, 'Capacity policy: full', 'C1')])
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        job = manager._pending
        self.assertEqual(job.snapshot.compression_mode, 'full')
        for index in range(3):
            manager.transform(payload([msg('b' * 800), msg('k%d' % index)], start=3 + index * 2))
            self.assertIs(manager._pending, job, 'a running full must never be discarded')
        self.assertEqual(len(comp.snapshots), 1)
        self.assertFalse(job.cancellation.cancelled.is_set())
        self.assertTrue(all(e['inflight_mode'] == 'full'
                            for e in events if e['event'] == 'compression_inflight'))

    def test_failed_compression_may_retry_on_the_next_step(self):
        """retry_after_steps=1 leaves no cooldown: a failure is retried at once."""
        manager, comp, events, _ = self.manager(
            results=[RuntimeError('upstream blew up'),
                     CompressionResult(False, 1.0, 'Capacity policy: incremental', 'C2')])
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        comp.release[0].set()
        self.assertTrue(manager._pending.done.wait(2))
        # The failure surfaces at poll time and costs exactly one step.
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(any(e['event'] == 'compression_failed' for e in events))
        self.assertIsNone(manager._pending)
        manager.transform(payload([msg('c' * 800)], start=5))
        self.assertTrue(comp.entered[1].wait(2), 'retry must resume on the next step')
        comp.release[1].set()
        self.assertTrue(manager._pending.done.wait(2))
        manager.transform(payload([msg('after')], start=6))
        self.assertEqual(manager._memory, ('C2',))

    def test_superseded_job_is_closed_out_in_the_ledger(self):
        """A preempted call may already be billed, so it needs a terminal event.

        Without one the run summarises as an unexplained accounting gap and the
        whole measurement is rejected.
        """
        manager, comp, events, _ = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        victim = manager._pending
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        failed = [e for e in events if e['event'] == 'compression_failed']
        self.assertEqual(len(failed), 1, failed)
        self.assertEqual(failed[0]['cause'], 'cancelled')
        self.assertEqual(failed[0]['call_id'], victim.snapshot.call_id)
        self.assertEqual(failed[0]['compression_id'], victim.snapshot.call_id)
        self.assertEqual(failed[0]['execution_mode'], 'async')
        self.assertEqual((failed[0]['start'], failed[0]['end']),
                         (victim.snapshot.start, victim.snapshot.end))
        # The successor keeps its own identity and is untouched by the closeout.
        self.assertNotEqual(manager._pending.snapshot.call_id, victim.snapshot.call_id)
        self.assertFalse(manager._pending.cancellation.cancelled.is_set())

    def test_successor_snapshot_keeps_its_own_call_id(self):
        """Closing out the victim must not blank the id the audit keys on.

        compression_snapshot is written right after preemption; if the current
        id is left cleared or pointing at the victim, the successor's snapshot
        is unkeyed and the run fails its audit.
        """
        manager, comp, events, audit = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        victim = manager._pending
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        successor = manager._pending
        snapshots = [r for r in audit if r['event'] == 'compression_snapshot']
        self.assertEqual([r['call_id'] for r in snapshots],
                         [victim.snapshot.call_id, successor.snapshot.call_id])
        self.assertTrue(all(r['call_id'] for r in snapshots), 'snapshot lost its call_id')
        # Both closeout events belong to the victim, not to the successor.
        for name in ('compression_superseded', 'compression_failed'):
            event = next(e for e in events if e['event'] == name)
            self.assertEqual(event['call_id'], victim.snapshot.call_id)
            self.assertEqual(event['compression_id'], victim.snapshot.call_id)
        self.assertEqual(next(e for e in events if e['event'] == 'compression_superseded')
                         ['successor_id'], successor.snapshot.call_id)

    def test_shutdown_waits_for_superseded_workers(self):
        """The orphan writes its usage row while unwinding; the log must have it."""
        manager, comp, _, _ = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        victim = manager._pending
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        self.assertIn(victim, manager._orphans)
        manager.close()
        self.assertTrue(manager.wait_closed(5))
        self.assertFalse(victim.thread.is_alive(), 'orphan outlived shutdown')

    def test_cancelling_one_job_never_touches_the_other(self):
        """Each job owns its handle, so preemption cannot miss or cross-cancel."""
        manager, comp, _, _ = self.manager(full_trigger_tokens=400)
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        first = manager._pending
        manager._memory = ('M' * 2000,)
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertTrue(comp.entered[1].wait(2))
        second = manager._pending
        self.assertIsNot(first.cancellation, second.cancellation)
        self.assertTrue(first.cancellation.cancelled.is_set())
        self.assertFalse(second.cancellation.cancelled.is_set())
        self.assertEqual(comp.cancellations[:2], [first.cancellation, second.cancellation])

    def test_v4_async_remains_single_flight(self):
        manager, comp, events, _ = self.manager(
            results=[CompressionResult(False, .9, 'active', 'INC')], method='v4')
        manager.transform(payload([msg('a' * 800), msg('keep1')]))
        self.assertTrue(comp.entered[0].wait(2))
        job = manager._pending
        manager.transform(payload([msg('b' * 800), msg('keep2')], start=3))
        self.assertIs(manager._pending, job)
        self.assertEqual(len(comp.snapshots), 1)
        self.assertFalse(job.cancellation.cancelled.is_set())
        self.assertFalse(any(e['event'] == 'compression_superseded' for e in events))


if __name__ == '__main__':
    unittest.main()
