"""Thresholds that follow the task's own scale, per the dual_a dynamic option.

I = max(trigger_tokens, 2 x largest single-step growth in the recent window)
F = 2 x max(I, memory produced by the last full)

The step being decided is excluded from the history: its group is still
protected from compression, so it must not shape the bar either.
"""
import unittest

from src.context_compression.core import (ContextManager, eligible_end, estimated_tokens,
                                          memory_messages)
from src.context_compression.types import CompressionConfig, CompressionResult
from tests.context_compression.test_core import IDENTITY, msg, payload


class Recording:
    def __init__(self, result=None):
        self.result = result or CompressionResult(False, 1.0, 'Capacity policy: incremental', 'C')
        self.snapshots = []

    def __call__(self, snapshot, cancellation=None):
        self.snapshots.append(snapshot)
        return self.result


class DynamicThresholdTests(unittest.TestCase):
    def manager(self, **config):
        options = dict(method='dual_a', trigger_tokens=8192, full_trigger_tokens=16384,
                       dynamic_thresholds=True)
        options.update(config)
        compressor = Recording()
        manager = ContextManager(compressor, CompressionConfig(**options), task='the task',
                                 instance_id=IDENTITY, event_sink=lambda e: None,
                                 audit_sink=lambda e: None)
        return manager, compressor

    def test_floor_applies_until_a_large_step_is_seen(self):
        manager, _ = self.manager()
        manager._increments = [400, 500, 600, 700]
        self.assertEqual(manager._thresholds(), (8192, 16384))   # 2 x 600 < floor

    def test_largest_step_in_the_window_lifts_the_incremental_bar(self):
        manager, _ = self.manager()
        manager._increments = [400, 11361, 500, 700, 600]   # last entry is this step
        incremental, full = manager._thresholds()
        self.assertEqual(incremental, 2 * 11361)
        self.assertEqual(full, 2 * incremental)

    def test_an_outlier_leaves_the_window_and_stops_pinning_the_bar(self):
        """A window, not a running max: bulk reading must not bind the whole run."""
        manager, _ = self.manager()
        manager._increments = [40000, 300, 300, 300, 300, 200]
        self.assertEqual(manager._thresholds()[0], 80000)    # outlier still in window
        manager._increments = [40000, 300, 300, 300, 300, 300, 200]
        self.assertEqual(manager._thresholds()[0], 8192)     # it has aged out

    def test_the_step_being_decided_is_excluded(self):
        manager, _ = self.manager()
        manager._increments = [400, 500, 40000]          # the last one is this step
        self.assertEqual(manager._thresholds()[0], 8192)  # 40000 must not count
        manager._increments.append(300)                   # it is history now
        self.assertEqual(manager._thresholds()[0], 80000)

    def test_full_bar_follows_the_memory_the_last_full_produced(self):
        manager, _ = self.manager()
        manager._increments = [100, 200]
        manager._memory_after_full = 15848
        self.assertEqual(manager._thresholds(), (8192, 2 * 15848))

    def test_disabled_by_default_keeps_configured_values(self):
        manager, _ = self.manager(dynamic_thresholds=False)
        manager._increments = [40000, 40000]
        manager._memory_after_full = 30000
        self.assertEqual(manager._thresholds(), (8192, 16384))

    def test_increment_is_measured_on_the_eligible_prefix(self):
        """Bars are set from compressible growth only, never the protected tail."""
        # Thresholds out of reach: nothing compresses, so the buffer only grows.
        manager, comp = self.manager(trigger_tokens=100000, full_trigger_tokens=200000)
        manager.transform(payload([msg('a' * 400), msg('keep')]))
        manager.transform(payload([msg('b' * 400)], start=3))
        manager.transform(payload([msg('c' * 400)], start=4))
        self.assertEqual(comp.snapshots, [])
        end = eligible_end(manager._raw, 0, manager.config.keep_recent_groups)
        eligible = estimated_tokens(manager._raw[:end])
        self.assertEqual(manager._prev_eligible, eligible)
        self.assertEqual(sum(manager._increments), eligible)
        self.assertLess(eligible, estimated_tokens(manager._raw))  # tail excluded

    def test_a_single_step_can_never_cross_the_bar_it_set(self):
        """The point of the 2x factor: one step alone is never enough."""
        manager, _ = self.manager()
        manager._increments = [12000, 0]           # 12000 is history, 0 is this step
        incremental, _ = manager._thresholds()
        self.assertEqual(incremental, 24000)
        self.assertLess(12000, incremental)


if __name__ == '__main__':
    unittest.main()
