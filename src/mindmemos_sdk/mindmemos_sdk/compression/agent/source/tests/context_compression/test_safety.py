"""Offline adversarial checks for complete tool groups and content barriers."""

from copy import deepcopy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from src.context_compression.core import eligible_end
from src.context_compression.types import CompressionConfig, CompressionResult


LONG_TEXT = "Historical evidence with an exact artifact identifier: build-314. " * 80
GOOD_RESULT = CompressionResult(False, 0.8, "The investigation remains active.", "build-314 inspected.")


def message(role, content):
    return {"role": role, "content": content}


def payload(raw=None, task="Primary task: inspect the build"):
    return {
        "model": "main-model",
        "messages": [
            message("system", "Preserve evidence."),
            message("developer", "Keep exact artifact identifiers."),
            message("user", task),
        ] + deepcopy(raw if raw is not None else [
            message("assistant", LONG_TEXT),
            message("user", "Continue the investigation."),
        ]),
        "temperature": 0.3,
        "metadata": {"nested": ["unchanged"]},
    }


def tool_group(*ids):
    return [{
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "inspect", "arguments": "{}"},
        } for call_id in ids],
    }] + [{"role": "tool", "tool_call_id": call_id, "content": LONG_TEXT}
          for call_id in reversed(ids)]


class GateCompressor:
    """A worker controlled by the test, with no network or scheduling sleeps."""

    def __init__(self, result=GOOD_RESULT, error=None, mutate=False):
        self.result = result
        self.error = error
        self.mutate = mutate
        self.entered = threading.Event()
        self.release = threading.Event()
        self.snapshots = []

    def __call__(self, snapshot):
        self.snapshots.append(snapshot)
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("test failed to release the compressor")
        if self.mutate:
            snapshot.raw[0]["content"] = "Worker-local mutation"
        if self.error:
            raise self.error
        return self.result


class ToolBoundarySafetyTests(unittest.TestCase):
    def test_parallel_calls_and_reversed_results_are_one_group(self):
        first = tool_group("first-a", "first-b")
        recent = tool_group("recent-a", "recent-b")
        raw = first + recent
        self.assertEqual(eligible_end(raw, 0, 1), len(first))
        self.assertEqual(eligible_end(raw, len(first), 1), len(first))

    def test_keep_recent_groups_counts_complete_tool_groups(self):
        groups = [tool_group("a", "b"), tool_group("c"), tool_group("d", "e", "f")]
        raw = sum(groups, [])
        self.assertEqual(eligible_end(raw, 0, 2), len(groups[0]))
        self.assertEqual(eligible_end(raw, 0, 3), 0)

    def test_malformed_or_unsupported_history_is_a_barrier(self):
        missing_result = tool_group("a", "b")[:-1]
        duplicate_calls = tool_group("a", "a")
        duplicate_results = tool_group("a", "b")
        duplicate_results[2]["tool_call_id"] = duplicate_results[1]["tool_call_id"]
        wrong_result = tool_group("a")
        wrong_result[1]["tool_call_id"] = "unknown"
        interleaved_result = tool_group("a", "b")
        interleaved_result.insert(2, message("assistant", "interrupted"))
        malformed_call = tool_group("a")
        malformed_call[0]["tool_calls"][0]["id"] = None
        multimodal_result = tool_group("a")
        multimodal_result[1]["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        cases = {
            "missing result": missing_result,
            "duplicate calls": duplicate_calls,
            "duplicate results": duplicate_results,
            "wrong result": wrong_result,
            "interleaved result": interleaved_result,
            "missing call id": malformed_call,
            "non-list calls": [{"role": "assistant", "content": None, "tool_calls": {"id": "a"}}],
            "user tool call": [{"role": "user", "content": None, "tool_calls": [{"id": "a"}]}],
            "orphan result": [{"role": "tool", "content": "orphan", "tool_call_id": "a"}],
            "multimodal result": multimodal_result,
            "multimodal message": [message("user", [{"type": "text", "text": "Look"}, {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}])],
            "legacy function": [{"role": "assistant", "content": None, "function_call": {"name": "inspect", "arguments": "{}"}}],
            "alternate tool encoding": [{"role": "assistant", "content": None, "toolCalls": [{"id": "a"}]}],
            "assistant audio": [{"role": "assistant", "content": None, "audio": {"id": "audio-1"}}],
        }
        safe_prefix = [message("assistant", "old"), message("assistant", "protected before barrier")]
        for name, barrier in cases.items():
            with self.subTest(name=name):
                raw = safe_prefix + barrier + [message("user", "later")]
                self.assertEqual(eligible_end(raw, 0, 1), 1)

    def test_text_content_parts_remain_eligible(self):
        raw = [message("assistant", [{"type": "text", "text": "supported text"}]), message("user", "recent")]
        self.assertEqual(eligible_end(raw, 0, 1), 1)
