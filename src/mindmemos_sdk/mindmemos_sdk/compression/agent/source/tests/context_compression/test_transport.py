"""Offline only: real localhost sockets and deterministic mock upstreams."""

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.context_compression.compressor import CompressionError, OpenAICompressor, parse_result
from src.context_compression.prompts import JOINT_SYSTEM_PROMPT, build_messages
from src.context_compression.proxy import EventLog, ProxyHandler, create_server, shutdown_server
from src.context_compression.types import CompressionConfig, CompressionSnapshot


ROOT = Path(__file__).resolve().parents[2]
VALID = {"stage_end": False, "confidence": 0.85, "reason": "The current investigation remains open.", "C": "Result retained."}


def envelope(result=None, finish_reason="stop", usage=None):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(VALID if result is None else result)},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 51, "completion_tokens": 12, "total_tokens": 63} if usage is None else usage}


def snapshot():
    return CompressionSnapshot(1, 2, 3, 4, 5, "Make report", ("Earlier exact identifier: item-17",),
                               ({"role": "assistant", "content": "Recent evidence only."},))


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        pass  # Tests deliberately disconnect timed-out upstream requests.


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        captured = {"path": self.path, "headers": dict(self.headers.items()), "payload": json.loads(body)}
        with self.server.capture_lock:
            self.server.captured.append(captured)
        self.server.respond(self, captured)


def respond_json(handler, value, status=200, headers=None):
    body = json.dumps(value).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


@contextmanager
def mock_upstream(responder=None):
    server = MockServer(("127.0.0.1", 0), MockHandler)
    server.captured = []
    server.capture_lock = threading.Lock()
    server.respond = responder or (lambda h, req: respond_json(h, envelope()))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


class PromptTests(unittest.TestCase):
    @unittest.skip('Historical v1 research-plan archive is outside this ADyn package')
    def test_historical_detector_archive_is_preserved(self):
        original = (ROOT / "research/stage_boundary_probe/prompt_v1.txt").read_text(encoding="utf-8")
        self.assertEqual((ROOT / "src/context_compression/boundary_prompt_v1.txt").read_text(), original)
        self.assertIn("```text\n" + original + "```", (ROOT / "plan.md").read_text(encoding="utf-8"))

    def test_original_v1_prompt_is_the_entire_system_message(self):
        import hashlib
        expected = (ROOT / "src/context_compression/joint_prompt_v1.txt").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(),
                         "4e78abbf4017876c63d042851b788a836a27cebf6390d9ec8120e2dec9f0fdb0")
        self.assertEqual(JOINT_SYSTEM_PROMPT, expected)
        self.assertEqual(build_messages(snapshot())[0], {"role": "system", "content": expected})
        self.assertNotIn("Do not summarize or rewrite", expected)
        self.assertIn("DEFINITION\n", expected)
        self.assertIn("C must summarize ONLY NEW_RAW_TRAJECTORY (R)", expected)
        self.assertIn("C must consolidate BOTH", expected)

    def test_user_v2_prompt_archive_is_preserved(self):
        import hashlib
        expected = (ROOT / "src/context_compression/joint_prompt_v2.txt").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(),
                         "8ec7ab2468618d3fe6cfd33e4fe931f00095e27c7b3043bb8389d7faaea4946a")

    def test_only_frozen_task_memory_raw_are_inputs(self):
        frozen = snapshot()
        messages = build_messages(frozen)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(json.loads(messages[1]["content"]), {
            "TASK": frozen.task, "EXISTING_COMPRESSED_MEMORY": list(frozen.memory),
            "NEW_RAW_TRAJECTORY": list(frozen.raw),
        })


class ParserTests(unittest.TestCase):
    def test_strict_valid_result_and_safe_usage(self):
        for stage_end in (False, True):
            with self.subTest(stage_end=stage_end):
                result = parse_result(json.dumps(dict(VALID, stage_end=stage_end)), usage={
                    "prompt_tokens": 42, "provider_secret": "do-not-log", "prompt_tokens_details": {"cached_tokens": 21},
                })
                self.assertIs(result.stage_end, stage_end)
                self.assertEqual(result.C, VALID["C"])
                self.assertEqual(result.usage, {"prompt_tokens": 42, "prompt_tokens_details": {"cached_tokens": 21}})

    def test_invalid_fields_types_empty_summary_and_nonfinite(self):
        invalid = [dict(VALID, extra=1), {k: v for k, v in VALID.items() if k != "C"},
                   dict(VALID, stage_end=1), dict(VALID, stage_end="false"), dict(VALID, confidence=True),
                   dict(VALID, confidence="0.5"), dict(VALID, confidence=-0.1), dict(VALID, confidence=1.1),
                   dict(VALID, confidence=10 ** 1000), dict(VALID, confidence=float("nan")),
                   dict(VALID, confidence=float("inf")), dict(VALID, C=""), dict(VALID, C=" \n "),
                   dict(VALID, C={}), dict(VALID, reason=""), dict(VALID, reason=None)]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(CompressionError):
                    parse_result(json.dumps(value))

    def test_invalid_json_duplicate_keys_extra_text_and_truncation(self):
        text = json.dumps(VALID)
        for value in ["[]", "null", text + " trailing", "```json\n" + text + "\n```", text[:-1],
                      text[:-1] + ', "stage_end": true}', text.replace("0.85", "1e400")]:
            with self.subTest(value=value):
                with self.assertRaises(CompressionError):
                    parse_result(value)
        for finish in [None, "length", "content_filter", "tool_calls", ""]:
            with self.subTest(finish=finish):
                with self.assertRaises(CompressionError):
                    parse_result(text, finish_reason=finish)


class CompressorHTTPTests(unittest.TestCase):
    def test_p0_plain_summary_and_original_prompt_without_output_cap(self):
        result = {"choices": [{"message": {"content": "### REASONING\nfacts\n### COMPLETED\ninspection"},
                               "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}}
        with mock_upstream(lambda h, req: respond_json(h, result)) as upstream:
            compressor = OpenAICompressor(f"http://127.0.0.1:{upstream.server_port}/v1", "", "model",
                                          CompressionConfig(method="p0"))
            actual = compressor(snapshot())
            self.assertTrue(actual.stage_end)
            body = upstream.captured[0]["payload"]
            self.assertNotIn("temperature", body)
            for key in ("max_tokens", "max_output_tokens", "max_completion_tokens", "response_format"):
                self.assertNotIn(key, body)
            self.assertIn("INITIAL GUIDELINE P0", body["messages"][0]["content"])
            self.assertIn("item-17", body["messages"][0]["content"])

    def test_joint_call_goes_directly_to_upstream_once_and_keeps_usage(self):
        events = []
        with mock_upstream(lambda h, req: respond_json(h, envelope(dict(VALID, stage_end=True)))) as upstream:
            compressor = OpenAICompressor(f"http://127.0.0.1:{upstream.server_port}/v1", "mock-key", "upstream-model",
                                          CompressionConfig(), {"X-App": "test"}, events.append)
            result = compressor(snapshot())
            self.assertIs(result.stage_end, True)
            self.assertEqual(len(upstream.captured), 1)
            request = upstream.captured[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["headers"]["Authorization"], "Bearer mock-key")
            self.assertEqual(request["headers"]["X-App"], "test")
            self.assertEqual(request["payload"]["messages"], build_messages(snapshot()))
            for key in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
                self.assertNotIn(key, request["payload"])
            self.assertNotIn("temperature", request["payload"])
            self.assertEqual(request["payload"]["model"], "upstream-model")
            self.assertEqual(request["payload"]["response_format"], {"type": "json_object"})
            self.assertFalse(request["payload"]["stream"])
            self.assertEqual(events[0]["event"], "compressor_started")
            self.assertEqual(events[-1]["event"], "compressor_usage")
            self.assertEqual(events[0]['call_id'], events[-1]['call_id'])
            self.assertEqual(events[-1]["usage"]["prompt_tokens"], 51)
            self.assertNotIn(VALID["C"], json.dumps(events))

    def test_bad_envelopes_fail_without_logging_body(self):
        values = [envelope(finish_reason="length"), {"choices": []}, {"choices": [{}, {}]},
                  {"choices": [{"finish_reason": "stop", "message": {"content": []}}]},
                  {"choices": [{"finish_reason": "stop", "message": {"refusal": "private", "content": json.dumps(VALID)}}]},
                  {"choices": [{"finish_reason": "stop", "message": {"tool_calls": [{}], "content": json.dumps(VALID)}}]}]
        for value in values:
            with self.subTest(value=value), mock_upstream(lambda h, req: respond_json(h, value)) as upstream:
                compressor = OpenAICompressor(f"http://127.0.0.1:{upstream.server_port}/v1", "mock-key", "model", CompressionConfig())
                with self.assertRaises(CompressionError) as caught:
                    compressor(snapshot())
                self.assertNotIn("private", str(caught.exception))

    def test_http_failure_is_safe_and_has_separate_usage_event(self):
        events = []
        with mock_upstream(lambda h, req: respond_json(h, {"secret": "mock-key private-body"}, 429)) as upstream:
            compressor = OpenAICompressor(f"http://127.0.0.1:{upstream.server_port}/v1", "mock-key", "model", CompressionConfig(), event_sink=events.append)
            with self.assertRaises(CompressionError) as caught:
                compressor(snapshot())
            self.assertNotIn("mock-key", str(caught.exception))
            self.assertNotIn("private-body", json.dumps(events))
            self.assertEqual(events[-1]["status"], 429)
            self.assertFalse(events[-1]["success"])

    def test_compressor_timeout_is_bounded(self):
        release = threading.Event()
        def delayed(handler, request):
            release.wait(1)
            respond_json(handler, envelope())
        try:
            with mock_upstream(delayed) as upstream:
                compressor = OpenAICompressor(f"http://127.0.0.1:{upstream.server_port}", "", "model", CompressionConfig(timeout_seconds=0.08))
                started = time.monotonic()
                with self.assertRaises(CompressionError):
                    compressor(snapshot())
                self.assertLess(time.monotonic() - started, 0.7)
        finally:
            release.set()


class ProxyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.proxies = []

    def tearDown(self):
        for server, thread in self.proxies:
            if thread.is_alive():
                shutdown_server(server)
            thread.join(1)
        self.temp.cleanup()

    def proxy(self, upstream, compression=None):
        server = create_server({"upstream_base_url": f"http://127.0.0.1:{upstream.server_port}/v1",
                                "instance_id": "1234567890abcdef1234567890abcdef",
                                "upstream_api_key": "upstream-test-key", "upstream_model": "real-model",
                                "upstream_headers": {"X-App": "proxy-test"}, "task": "Make report", "port": 0,
                                "compression": compression or {"trigger_tokens": 1000000}, "log_dir": self.temp.name})
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.proxies.append((server, thread))
        return server

    def request(self, server, payload=None, path="/v1/chat/completions", headers=None, method="POST"):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}{path}", data=body,
                          headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers.items()), response.read()

    def wait_log(self, filename, count=1):
        deadline = time.monotonic() + 2
        path = Path(self.temp.name, filename)
        while time.monotonic() < deadline:
            if path.exists():
                lines = path.read_text().splitlines()
                if len(lines) >= count:
                    return [json.loads(line) for line in lines]
            threading.Event().wait(0.01)
        self.fail(f"Missing expected log entries: {filename}")

    def test_model_mapping_fields_headers_and_response_are_preserved(self):
        value = {"choices": [{"message": {"role": "assistant", "content": "unchanged"}}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 3}}}
        with mock_upstream(lambda h, req: respond_json(h, value, headers={"X-Upstream": "preserved"})) as upstream:
            proxy = self.proxy(upstream)
            payload = {"model": "local-alias", "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "Make report"}],
                       "temperature": 0.4, "tools": [{"type": "function", "function": {"name": "run", "parameters": {}}}],
                       "tool_choice": "auto", "stream": False, "stream_options": {"include_usage": True},
                       "max_tokens": 345, "seed": 19, "provider": {"order": ["test-provider"]}}
            status, headers, body = self.request(proxy, payload, headers={"Authorization": "Bearer local-key", "X-Trace": "keep"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Upstream"], "preserved")
            self.assertEqual(body, json.dumps(value).encode())
            captured = upstream.captured[0]
            self.assertEqual(captured["payload"], dict(payload, model="real-model"))
            self.assertEqual(captured["headers"]["Authorization"], "Bearer upstream-test-key")
            self.assertEqual(captured["headers"]["X-App"], "proxy-test")
            self.assertEqual(captured["headers"]["X-Trace"], "keep")
            log = self.wait_log("main_usage.jsonl")[0]
            self.assertEqual(log["usage"], value["usage"])
            self.assertNotIn("upstream-test-key", json.dumps(log))

    def test_nonmain_history_is_not_compressed(self):
        with mock_upstream() as upstream:
            proxy = self.proxy(upstream, {"trigger_tokens": 1})
            payload = {"model": "alias", "messages": [{"role": "user", "content": "An unrelated auxiliary request"},
                       {"role": "assistant", "content": "x" * 4000}, {"role": "user", "content": "continue"}]}
            self.request(proxy, payload)
            self.assertEqual(len(upstream.captured), 1)
            self.assertEqual(upstream.captured[0]["payload"], dict(payload, model="real-model"))
            self.assertFalse(Path(self.temp.name, "compressor_usage.jsonl").exists())

    def test_upstream_http_status_headers_and_error_body_are_preserved(self):
        value = {"error": {"message": "rate limited"}}
        with mock_upstream(lambda h, req: respond_json(h, value, 429, {"Retry-After": "5"})) as upstream:
            status, headers, body = self.request(self.proxy(upstream), {"messages": [], "model": "alias"})
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "5")
            self.assertEqual(body, json.dumps(value).encode())

    def test_sse_flushes_before_upstream_completes_and_preserves_bytes(self):
        release = threading.Event()
        first = b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        last = b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        def stream(handler, captured):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("X-Upstream", "stream")
            handler.end_headers()
            handler.wfile.write((f"{len(first):x}\r\n").encode() + first + b"\r\n")
            handler.wfile.flush()
            release.wait(2)
            handler.wfile.write((f"{len(last):x}\r\n").encode() + last + b"\r\n0\r\n\r\n")
            handler.wfile.flush()
        try:
            with mock_upstream(stream) as upstream:
                proxy = self.proxy(upstream)
                connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=1)
                self.addCleanup(connection.close)
                payload = {"model": "alias", "messages": [], "stream": True, "stream_options": {"include_usage": True}}
                connection.request("POST", "/v1/chat/completions", json.dumps(payload), {"Content-Type": "application/json", "Accept": "text/event-stream"})
                response = connection.getresponse()
                started = time.monotonic()
                initial = response.readline()
                self.assertLess(time.monotonic() - started, 0.6)
                self.assertEqual(initial, first.splitlines(keepends=True)[0])
                self.assertIsNone(response.getheader("Transfer-Encoding"))
                self.assertEqual(response.getheader("X-Upstream"), "stream")
                release.set()
                self.assertEqual(initial + response.read(), first + last)
                self.assertEqual(upstream.captured[0]["headers"]["Accept"], "text/event-stream")
                self.assertEqual(upstream.captured[0]["payload"], dict(payload, model="real-model"))
                usage = self.wait_log("main_usage.jsonl")[0]["usage"]
                self.assertEqual(usage, {"prompt_tokens": 9, "completion_tokens": 1})
        finally:
            release.set()

    def test_sync_compression_precedes_main_and_private_delta_is_stripped(self):
        def respond(handler, captured):
            if captured["payload"].get("response_format") == {"type": "json_object"}:
                respond_json(handler, envelope())
            else:
                respond_json(handler, {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 1}})
        with mock_upstream(respond) as upstream:
            proxy = self.proxy(upstream, {"trigger_tokens": 100, "timeout_seconds": 3})
            messages = [{"role": "system", "content": "prefix"}, {"role": "user", "content": "Make report"},
                        {"role": "assistant", "content": "earlier evidence " * 500}, {"role": "user", "content": "continue"}]
            payload = {"model": "alias", "messages": messages, "_wildclaw_context": {
                "version": 1, "instance_id": proxy.instance_id, "session_id": "chat",
                "start": 0, "end": 3, "messages": messages[1:], "prefix": messages[:1]}}
            self.assertEqual(self.request(proxy, payload)[0], 200)
            self.assertEqual(len(upstream.captured), 2)
            self.assertIn("response_format", upstream.captured[0]["payload"])
            self.assertIn(VALID["C"], upstream.captured[1]["payload"]["messages"][2]["content"])
            self.assertTrue(all("_wildclaw_context" not in c["payload"] for c in upstream.captured))
            self.assertEqual(self.request(proxy, payload)[0], 200)
            self.assertEqual(len(upstream.captured), 3)  # retry: main only, no re-compression
            logs = self.wait_log("main_usage.jsonl", 2)
            self.assertEqual(logs[0]["source"], "agent")
            self.assertEqual(logs[0]["usage"]["prompt_tokens"], 100)
            self.assertEqual(self.wait_log("compressor_usage.jsonl")[0]["usage"]["prompt_tokens"], 51)
            text = "".join(p.read_text() for p in Path(self.temp.name).glob("*.jsonl"))
            for private in ("upstream-test-key", "earlier evidence", VALID["C"]):
                self.assertNotIn(private, text)

    def test_invalid_main_delta_returns_error_not_silent_passthrough(self):
        with mock_upstream() as upstream:
            proxy = self.proxy(upstream)
            self.assertEqual(self.request(proxy, {"messages": [], "_wildclaw_context": {"version": 1}})[0], 409)
            self.assertEqual(upstream.captured, [])
            events = self.wait_log("events.jsonl", 2)
            self.assertEqual(sum(e['event'] == 'history_error' for e in events), 1)
            self.assertEqual(sum(e['event'] == 'history_rejected' and e['status'] == 409 for e in events), 1)

    def test_health_unsupported_path_and_shutdown(self):
        with mock_upstream() as upstream:
            proxy = self.proxy(upstream)
            health_status, _, health_body = self.request(proxy, path="/health", method="GET")
            self.assertEqual(health_status, 200)
            self.assertEqual(json.loads(health_body)["instance_id"], "1234567890abcdef1234567890abcdef")
            status, _, body = self.request(proxy, {}, path="/v1/responses")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["type"], "unsupported_path")
            started = time.monotonic()
            self.assertEqual(self.request(proxy, {}, path="/shutdown")[0], 200)
            self.proxies[-1][1].join(1)
            self.assertFalse(self.proxies[-1][1].is_alive())
            self.assertLess(time.monotonic() - started, 0.8)

    def test_shutdown_still_starts_if_control_client_disconnects(self):
        handler = object.__new__(ProxyHandler)
        handler.path = "/shutdown"
        handler.server = Mock()
        handler._json = Mock(side_effect=BrokenPipeError)
        with patch("src.context_compression.proxy.threading.Thread") as thread:
            with self.assertRaises(BrokenPipeError):
                handler.do_POST()
            handler.server.context_manager.close.assert_called_once()
            thread.assert_called_once_with(target=shutdown_server, args=(handler.server,), daemon=True)
            thread.return_value.start.assert_called_once()

    def test_metadata_log_drops_arbitrary_strings_and_usage_secrets(self):
        logger = EventLog(self.temp.name)
        logger({"event": "compressor_usage", "reason": "private", "C": "private summary", "task": "private task",
                "error": "private key", "api_key": "secret", "usage": {"prompt_tokens": 5, "token": "secret"},
                "old_memory_hash": "a" * 64, "memory_hash": "arbitrary-private-value"})
        value = self.wait_log("compressor_usage.jsonl")[0]
        self.assertEqual(value["usage"], {"prompt_tokens": 5})
        self.assertEqual(value["old_memory_hash"], "a" * 64)
        self.assertNotIn("memory_hash", value)
        self.assertNotIn("private", json.dumps(value))
        self.assertNotIn("secret", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
