from copy import deepcopy
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from src.context_compression.core import ContextManager, HistoryError, eligible_end, memory_messages
from src.context_compression.types import CompressionConfig, CompressionResult

IDENTITY = "1234567890abcdef1234567890abcdef"
PREFIX = [{"role": "system", "content": "fixed rules"}]
TASK = {"role": "user", "content": "the task"}


def msg(text):
    return {"role": "assistant", "content": text}


def payload(raw, start=0, host=None, **metadata):
    messages = ([TASK] if start == 0 else []) + raw
    return {"model": "alias", "stream": False, "tools": [],
            "messages": host if host is not None else PREFIX + [TASK] + raw,
            "_wildclaw_context": {"version": 1, "instance_id": IDENTITY, "session_id": "chat",
                                  "start": start, "end": start + len(messages),
                                  "messages": deepcopy(messages), "prefix": deepcopy(PREFIX), **metadata}}


class CoreTests(unittest.TestCase):
    def manager(self, results=None, **config):
        compressor = Mock(side_effect=results or [CompressionResult(False, .9, "active", "M1")])
        events = []
        manager = ContextManager(compressor, CompressionConfig(trigger_tokens=80, **config),
                                 task="the task", instance_id=IDENTITY, event_sink=events.append)
        self.addCleanup(manager.close)
        return manager, compressor, events

    def test_append_append_full_one_call_and_physically_release_raw(self):
        manager, compressor, events = self.manager([
            CompressionResult(False, .9, "active", "M1"),
            CompressionResult(False, .9, "active", "M2"),
            CompressionResult(True, .9, "closed", "all facts"),
        ])
        original = payload([msg("a" * 800), msg("keep1")])
        copy = deepcopy(original)
        first = manager.transform(original)
        self.assertEqual(original, copy)
        self.assertNotIn("_wildclaw_context", first)
        self.assertEqual(manager._raw, [msg("keep1")])
        self.assertEqual(first["messages"], PREFIX + [TASK] + memory_messages(("M1",)) + manager._raw)
        second = manager.transform(payload([msg("b" * 800), msg("keep2")], start=3, host=[msg("host summary")]))
        self.assertEqual(first["messages"][:-1], second["messages"][:-2])
        self.assertEqual(manager._memory, ("M1", "M2"))
        final = manager.transform(payload([msg("c" * 800), msg("keep3")], start=5))
        self.assertEqual(manager._memory, ("all facts",))
        self.assertEqual(manager._raw, [msg("keep3")])
        self.assertEqual(compressor.call_count, 3)
        snapshots = [c.args[0] for c in compressor.call_args_list]
        self.assertEqual(snapshots[0].raw, (msg("a" * 800),))
        self.assertEqual(snapshots[1].raw, (msg("keep1"), msg("b" * 800)))
        self.assertEqual([(s.start, s.end) for s in snapshots], [(0, 1), (1, 3), (3, 5)])
        self.assertNotIn("keep3", str(snapshots[2]))
        self.assertEqual(final["messages"][-1], msg("keep3"))

    def test_host_rewrites_or_drops_all_old_messages_do_not_reset_memory(self):
        manager, compressor, _ = self.manager()
        manager.transform(payload([msg("a" * 800), msg("protected")]))
        result = manager.transform(payload([msg("latest")], start=3, host=[]))
        self.assertEqual(manager._memory, ("M1",))
        self.assertEqual(manager._raw, [msg("protected"), msg("latest")])
        self.assertEqual(result["messages"][:2], PREFIX + [TASK])
        self.assertEqual(compressor.call_count, 1)

    def test_exact_retry_does_not_resurrect_compressed_raw_or_compress_again(self):
        manager, compressor, _ = self.manager()
        request = payload([msg("a" * 800), msg("keep")])
        first = manager.transform(request)
        self.assertEqual(manager.transform(request), first)
        self.assertEqual(compressor.call_count, 1)
        self.assertEqual(manager._raw, [msg("keep")])

    def test_repeated_text_is_new_when_sequence_is_new(self):
        manager, _, _ = self.manager()
        manager.transform(payload([msg("same")]))
        manager.transform(payload([msg("same")], start=2))
        self.assertEqual(manager._raw, [msg("same"), msg("same")])

    def test_overlapping_retry_with_new_messages_appends_only_new_suffix(self):
        manager, _, _ = self.manager()
        manager.transform(payload([msg("same")]))
        manager.transform(payload([msg("same"), msg("new")]))
        self.assertEqual(manager._raw, [msg("same"), msg("new")])
        self.assertEqual(manager._cursor, 3)

    def test_changed_trusted_prefix_does_not_reset_memory(self):
        manager, _, _ = self.manager()
        manager.transform(payload([msg("a" * 800), msg("keep")]))
        request = payload([msg("later")], start=3)
        request["_wildclaw_context"]["prefix"] = [{"role": "developer", "content": "new trusted rule"}]
        result = manager.transform(request)
        self.assertEqual(result["messages"][0]["content"], "new trusted rule")
        self.assertEqual(manager._memory, ("M1",))

    def test_elapsed_deadline_discards_result(self):
        from unittest.mock import patch
        manager, _, events = self.manager(timeout_seconds=1)
        with patch('src.context_compression.core.time.monotonic', side_effect=[0, 2, 2]):
            manager.transform(payload([msg("a" * 800), msg("keep")]))
        self.assertEqual(manager._memory, ())
        self.assertEqual(len(manager._raw), 2)
        self.assertEqual(events[-2]['error_type'], 'TimeoutError')

    def test_null_main_marker_is_not_auxiliary(self):
        manager, _, _ = self.manager()
        with self.assertRaises(HistoryError):
            manager.transform({'messages': [], '_wildclaw_context': None})

    def test_missing_conflicting_and_wrong_identity_fail_without_mutation(self):
        for mode in ("gap", "rewrite", "session", "instance"):
            with self.subTest(mode=mode):
                manager, _, _ = self.manager()
                initial = payload([msg("small")])
                manager.transform(initial)
                old = deepcopy(manager._raw)
                request = payload([msg("new")], start=2)
                delta = request["_wildclaw_context"]
                if mode == "gap":
                    delta.update(start=3, end=4)
                elif mode == "rewrite":
                    request = payload([msg("changed")])
                elif mode == "session":
                    delta["session_id"] = "other"
                else:
                    delta["instance_id"] = "other"
                with self.assertRaises(HistoryError):
                    manager.transform(request)
                self.assertEqual(manager._raw, old)
                self.assertEqual(manager._cursor, 2)

    def test_failure_preserves_raw_and_retry_backoff_counts_new_steps(self):
        manager, compressor, _ = self.manager([RuntimeError("private"), CompressionResult(False, .9, "active", "M")])
        request = payload([msg("a" * 800), msg("keep")])
        manager.transform(request)
        self.assertEqual(manager._memory, ())
        self.assertEqual(manager._raw, request["_wildclaw_context"]["messages"][1:])
        manager.transform(request)
        for seq in (3, 4):
            manager.transform(payload([msg("new")], start=seq))
        self.assertEqual(compressor.call_count, 1)
        manager.transform(payload([msg("new")], start=5))
        self.assertEqual(compressor.call_count, 2)

    def test_expansion_and_invalid_result_preserve_state(self):
        for bad in (CompressionResult(False, .9, "active", "X" * 3000),
                    CompressionResult(False, .9, "active", ""), None):
            manager, _, _ = self.manager([bad])
            request = payload([msg("a" * 800), msg("keep")])
            manager.transform(request)
            self.assertEqual(manager._memory, ())
            self.assertEqual(manager._raw, request["_wildclaw_context"]["messages"][1:])

    def test_snapshot_mutation_cannot_change_live_raw(self):
        manager, compressor, _ = self.manager()
        def mutate(snapshot):
            snapshot.raw[0]["content"] = "bad"
            raise ValueError("fail")
        compressor.side_effect = mutate
        manager.transform(payload([msg("a" * 800), msg("keep")]))
        self.assertEqual(manager._raw[0], msg("a" * 800))

    def test_auxiliary_passthrough_does_not_ingest_or_compress(self):
        manager, compressor, events = self.manager()
        auxiliary = {"messages": [{"role": "user", "content": "summarize the task"}]}
        self.assertEqual(manager.transform(auxiliary), auxiliary)
        self.assertEqual(manager._raw, [])
        compressor.assert_not_called()
        self.assertEqual(events[-1]["cause"], "host_auxiliary")

    def test_partial_tool_group_can_be_completed_but_not_forwarded_early(self):
        manager, _, _ = self.manager()
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "inspect", "arguments": "{}"}}]}
        with self.assertRaises(HistoryError):
            manager.transform(payload([call]))
        self.assertEqual(manager._raw, [call])
        result = {"role": "tool", "tool_call_id": "t1", "content": "result"}
        output = manager.transform(payload([result], start=2))
        self.assertEqual(output["messages"][-2:], [call, result])

    def test_p0_trigger_includes_memory_but_ours_does_not(self):
        for method, expected in (("p0", 1), ("ours", 0)):
            manager, compressor, _ = self.manager(method=method)
            manager._memory = ("old " * 200,)
            manager.transform(payload([msg("small"), msg("keep")]))
            self.assertEqual(compressor.call_count, expected)

    def test_p0_protected_large_tool_group_does_not_trigger(self):
        manager, compressor, _ = self.manager(method='p0')
        call = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'large', 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{}'}}]}
        result = {'role': 'tool', 'tool_call_id': 'large', 'content': 'x' * 10000}
        manager.transform(payload([msg('small'), call, result]))
        compressor.assert_not_called()
        self.assertEqual(manager._raw, [msg('small'), call, result])

    def test_p0_large_group_counts_once_it_becomes_eligible(self):
        manager, compressor, _ = self.manager(method='p0', results=[CompressionResult(True, 1.0, 'policy', 'FULL')])
        call = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'large', 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{}'}}]}
        result = {'role': 'tool', 'tool_call_id': 'large', 'content': 'x' * 10000}
        manager.transform(payload([msg('small'), call, result]))
        compressor.assert_not_called()
        manager.transform(payload([msg('keep')], start=4))
        compressor.assert_called_once()
        self.assertEqual(list(compressor.call_args.args[0].raw), [msg('small'), call, result])
        self.assertEqual(manager._raw, [msg('keep')])
        self.assertEqual(manager._memory, ('FULL',))

    def test_synchronous_compression_waits_but_close_does_not(self):
        manager, compressor, _ = self.manager()
        entered, release = threading.Event(), threading.Event()
        def compress(snapshot):
            entered.set()
            release.wait(2)
            return CompressionResult(False, .9, "active", "M")
        compressor.side_effect = compress
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (manager.transform(payload([msg("a" * 800), msg("keep")])), finished.set()))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(finished.is_set())
        manager.close()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(manager._memory, ())

    def test_config_validation_and_no_output_limit(self):
        self.assertEqual(CompressionConfig().trigger_tokens, 8192)
        for value in (0, -1, True, 1.1, "8"):
            with self.assertRaises(ValueError):
                CompressionConfig(trigger_tokens=value)
        for value in (0, float("nan"), float("inf"), True, "120", 10 ** 400):
            with self.assertRaises(ValueError):
                CompressionConfig(timeout_seconds=value)
        for data in ({"max_output_tokens": 2048}, {"method": "async"}, {"force_full_at_tokens": 10000}):
            with self.assertRaises(ValueError):
                CompressionConfig.from_dict(data)

    def test_nullable_protocol_fields(self):
        for key in ("audio", "images", "image_url", "attachments", "video",
                    "function_call", "toolCalls", "tool_call_id"):
            self.assertEqual(eligible_end([dict(msg("plain"), **{key: None}), msg("keep")], 0, 1), 1)
            for invalid in ({}, [], "", False, 0):
                self.assertEqual(eligible_end([dict(msg("plain"), **{key: invalid}), msg("keep")], 0, 1), 0)



if __name__ == "__main__":
    unittest.main()
