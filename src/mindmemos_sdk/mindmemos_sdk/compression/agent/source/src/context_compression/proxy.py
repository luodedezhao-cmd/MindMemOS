"""Per-task loopback Chat Completions proxy; no API or SSE format conversion."""

import argparse
import json
import math
import os
from pathlib import Path
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import uuid
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .compressor import (OpenAICompressor, chat_completions_url, emit, open_upstream,
                         safe_usage, strict_json_loads, upstream_headers)
from .core import ContextManager, HistoryError
from .async_core import AsyncContextManager
from .types import CompressionConfig
from .transport import main_response
from .audit import CompressionAudit


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
MAX_BODY_BYTES = 64 * 1024 * 1024


class EventLog:
    """Thread-safe metadata logs, with separate main and compressor usage files."""

    def __init__(self, log_dir, instance_id=None, phase_path='/tmp/host_meter/phase'):
        self.directory = Path(log_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.instance_id = instance_id
        self.phase_path = Path(phase_path)
        self.sealed = False
        self._phases = {}
        self._calls = {}
        self._jobs = {}

    def _write(self, clean, filename):
        with (self.directory / filename).open('a', encoding='utf-8') as output:
            output.write(json.dumps(clean, ensure_ascii=False, allow_nan=False) + '\n')

    def finalize(self, compression_drained):
        """Seal a stable ledger before acknowledging shutdown; never invent usage."""
        with self.lock:
            if self.sealed:
                return
            base = dict(schema_version=3, time=time.time(), phase='execution', instance_id=self.instance_id)
            for call in self._calls.values():
                if call.get('source') == 'compressor':
                    self._write({**call, **base, 'phase': call['phase'],
                                 'event': 'compressor_abandoned', 'cause': 'shutdown_drain_timeout'}, 'events.jsonl')
            for job in self._jobs.values():
                self._write({**job, **base, 'phase': job['phase'],
                             'event': 'compression_shutdown_incomplete',
                             'cause': 'shutdown_drain_timeout'}, 'events.jsonl')
            self._write({**base, 'event': 'proxy_finalized', 'compression_drained': bool(compression_drained),
                         'unused_compression_usage': 'unknown',
                         'unfinished_compressor_calls': sum(c.get('source') == 'compressor' for c in self._calls.values()),
                         'unfinished_compression_jobs': len(self._jobs)}, 'events.jsonl')
            self.sealed = True
            for name in ('events.jsonl', 'main_usage.jsonl', 'compressor_usage.jsonl'):
                path = self.directory / name
                if path.exists():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())

    def __call__(self, event):
        if not isinstance(event, dict):
            return
        try:
            phase = 'scorer' if self.phase_path.read_text().strip() == 'scorer' else 'execution'
        except OSError:
            phase = 'execution'
        clean = {"schema_version": 3, "time": time.time(), "phase": phase, "instance_id": self.instance_id}
        text_fields = {"event", "mode", "error_type", "source", "cause", "call_id", "native_call_id",
                       "compression_id", "compression_mode", "decision_source", "trigger_reason", "execution_mode"}
        for key, value in event.items():
            if key == 'phase':
                if value in ('execution', 'scorer'):
                    clean[key] = value
            elif key == "usage":
                clean[key] = safe_usage(value)
            elif key in {'model', 'returned_model'}:
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/@+\-]{1,200}', value):
                    clean[key] = value
            elif key in {"old_memory_hash", "memory_hash"}:
                if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
                    clean[key] = value
            elif key in text_fields:
                if isinstance(value, str) and len(value) <= 80 and all(c.isalnum() or c in "_-." for c in value):
                    clean[key] = value
            elif key not in {"reason", "C", "content", "messages", "task", "error", "api_key"}:
                if type(value) is bool or value is None:
                    clean[key] = value
                elif type(value) in (int, float):
                    try:
                        if math.isfinite(value):
                            clean[key] = value
                    except OverflowError:
                        pass
        name = clean.get("event")
        with self.lock:
            call_id = clean.get('call_id')
            if call_id in self._phases:
                clean['phase'] = self._phases[call_id]
            elif call_id and name in ('main_started', 'compressor_started', 'compression_started'):
                self._phases[call_id] = clean['phase']
            if self.sealed:
                # Evidence only. Never silently append to a previously collected ledger.
                self._write({**clean, 'after_finalization': True}, 'late_events.jsonl')
                return
            self._write(clean, 'events.jsonl')
            if name in ("main_usage", "compressor_usage"):
                self._write(clean, name + '.jsonl')
                self._calls.pop(call_id, None)
            elif name in ('main_started', 'compressor_started'):
                self._calls[call_id] = clean
            if name == 'compression_started':
                self._jobs[call_id] = clean
            elif name in ('compression_applied', 'compression_discarded', 'compression_failed'):
                self._jobs.pop(call_id, None)


class UsageObserver:
    """Observe bytes without changing them; usage may be absent from upstream SSE."""

    def __init__(self, streaming):
        self.streaming = streaming
        self.buffer = bytearray()
        self.usage = None
        self.discarded = False
        self.model = None

    def _observe(self, value):
        try:
            data = strict_json_loads(value)
            if isinstance(data, dict):
                if isinstance(data.get('model'), str):
                    self.model = data['model']
                usage = safe_usage(data.get("usage"))
                if usage is not None:
                    self.usage = usage
        except ValueError:
            pass

    def feed(self, chunk):
        if self.discarded:
            return
        self.buffer.extend(chunk)
        if self.streaming:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if line.startswith(b"data:"):
                    self._observe(line[5:].strip())
        if len(self.buffer) > (1024 * 1024 if self.streaming else 16 * 1024 * 1024):
            self.discarded = True
            self.buffer.clear()

    def finish(self):
        if not self.discarded:
            if self.streaming and self.buffer.startswith(b"data:"):
                self._observe(self.buffer[5:].strip())
            elif not self.streaming:
                self._observe(self.buffer)
        return self.usage


class CompressionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, address, handler):
        self.request_slots = threading.BoundedSemaphore(16)
        self.closing = threading.Event()
        self.context_manager = None
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30)
        return request, address

    def process_request(self, request, client_address):
        if self.closing.is_set() or not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        emit(self.event_sink, {"event": "request_handler_error"})


def shutdown_server(server):
    server.closing.set()
    if server.context_manager is not None:
        server.context_manager.close()
        drained = server.context_manager.wait_closed()
        server.event_sink.finalize(drained)
    server.shutdown()
    server.server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ContextCompressionProxy/1"

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler's free-form log may contain request paths or input.
        pass

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _unsupported(self):
        self._json(404, {"error": {"type": "unsupported_path", "message": "Supported endpoints: GET /health, POST /shutdown, POST /v1/chat/completions"}})

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "instance_id": self.server.instance_id})
        else:
            self._unsupported()

    def do_POST(self):
        if self.path == "/shutdown":
            self.server.closing.set()
            self.server.context_manager.close()
            try:
                drained = self.server.context_manager.wait_closed()
                self.server.event_sink.finalize(drained)
                self._json(200, {"status": "shutting_down", "compression_drained": bool(drained),
                                 "ledger_finalized": True})
            finally:
                # A disconnected control client must not leave a closed manager
                # attached to a live listener that returns 503 forever.
                threading.Thread(target=shutdown_server, args=(self.server,), daemon=True).start()
            return
        if self.path != "/v1/chat/completions":
            self._unsupported()
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked requests are not supported")
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0:
                raise ValueError("Content-Length is required")
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": {"type": "request_too_large", "message": "Request exceeds proxy's 64 MiB limit"}})
                return
            payload = strict_json_loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, OSError):
            self._json(400, {"error": {"type": "invalid_request", "message": "Expected a JSON object with Content-Length and no Transfer-Encoding"}})
            return
        self.native_call_id = self.headers.get('X-Wildclaw-Observation-Id')
        if self.native_call_id is not None and not re.fullmatch(r'[a-f0-9]{32}', self.native_call_id):
            self._json(400, {"error": {"type": "invalid_observation_id"}})
            return
        emit(self.server.event_sink, {'event': 'request_received', 'native_call_id': self.native_call_id})
        try:
            transformed = self.server.context_manager.transform(payload)
        except HistoryError:
            emit(self.server.event_sink, {"event": "history_rejected", "status": 409, "native_call_id": self.native_call_id})
            self._json(409, {"error": {"type": "history_ingestion_failed", "message": "Invalid or incomplete execution delta"}})
            return
        if self.server.closing.is_set():
            emit(self.server.event_sink, {"event": "request_cancelled", "cause": "task_ended",
                                         "native_call_id": self.native_call_id})
            self._json(503, {"error": {"type": "session_closed", "message": "Task has ended"}})
            return
        transformed = dict(transformed)
        transformed["model"] = self.server.upstream_model
        body = json.dumps(transformed, ensure_ascii=False, allow_nan=False).encode("utf-8")
        excluded = HOP_HEADERS | {"host", "content-length", "authorization", "x-wildclaw-observation-id"}
        excluded |= {name.strip().lower() for name in self.headers.get("Connection", "").split(",")}
        headers = {key.lower(): value for key, value in self.headers.items() if key.lower() not in excluded}
        headers.update(self.server.upstream_headers)
        headers["content-type"] = "application/json"
        self._forward(body, headers, bool(transformed.get("stream")),
                      "agent" if "_wildclaw_context" in payload else "host_auxiliary")

    def _forward(self, body, headers, streaming, source="host_auxiliary"):
        started = time.monotonic()
        status = None
        sent_headers = False
        observer = UsageObserver(streaming)
        completed = False
        base = {'call_id': uuid.uuid4().hex, 'native_call_id': getattr(self, 'native_call_id', None),
                'source': source, 'model': self.server.upstream_model}
        emit(self.server.event_sink, {**base, 'event': 'main_started'})
        try:
            with main_response(self.server.upstream_url, body, headers, self.server.upstream_timeout, self.connection) as response:
                status = response.status
                excluded = HOP_HEADERS | {name.strip().lower() for name in response.headers.get("Connection", "").split(",")}
                self.send_response_only(status, response.reason)
                for name, value in response.headers.items():
                    if name.lower() not in excluded:
                        self.send_header(name, value)
                # urllib removes HTTP chunk framing; a closing connection frames the forwarded body.
                self.send_header("Connection", "close")
                self.end_headers()
                sent_headers = True
                self.close_connection = True
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    if not response.headers.get("Content-Encoding"):
                        observer.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                completed = True
        except (OSError, ValueError, HTTPException):
            completed = False
            emit(self.server.event_sink, {"event": "main_transport_error", "status": status})
            if not sent_headers:
                self._json(502, {"error": {"type": "upstream_unavailable", "message": "Upstream request failed"}})
            self.close_connection = True
        finally:
            emit(self.server.event_sink, {
                **base,
                "event": "main_usage", "usage": observer.finish(), "status": status,
                "source": source,
                "success": completed and status is not None and 200 <= status < 300,
                "stream": streaming, "latency_seconds": time.monotonic() - started,
                "returned_model": observer.model,
            })


def create_server(runtime):
    if not isinstance(runtime, dict):
        raise ValueError("runtime config must be a JSON object")
    config = CompressionConfig.from_dict(runtime.get("compression", {}))
    model = runtime.get("upstream_model")
    api_key = runtime.get("upstream_api_key", "")
    task = runtime.get("task")
    port = runtime.get("port", 18181)
    extra_headers = runtime.get("upstream_headers", {})
    instance_id = runtime.get("instance_id")
    session_id = runtime.get("session_id", "chat")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be nonempty")
    if instance_id is not None and (not isinstance(instance_id, str)
                                    or len(instance_id) != 32
                                    or any(c not in "0123456789abcdef" for c in instance_id)):
        raise ValueError("instance_id must be a lowercase UUID hex string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("upstream_model is required")
    if not isinstance(api_key, str) or "\n" in api_key or "\r" in api_key:
        raise ValueError("upstream_api_key must be a single-line string")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("the original task prompt is required")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between zero and 65535")
    if not isinstance(extra_headers, dict):
        raise ValueError("upstream_headers must be an object")
    url = chat_completions_url(runtime.get("upstream_base_url"))
    headers = {key.lower(): value for key, value in upstream_headers(api_key, extra_headers).items()}
    # Accept is supplied by the caller when streaming; this proxy does not change stream_options.
    if not any(key.lower() == "accept" for key in extra_headers):
        headers.pop("accept", None)
    sink = EventLog(runtime.get("log_dir", "./context_compression_logs"), instance_id)
    audit = CompressionAudit(sink.directory, instance_id, session_id)
    server = CompressionHTTPServer(("127.0.0.1", port), ProxyHandler)
    try:
        parsed = urlsplit(url)
        upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and upstream_port == server.server_port:
            raise ValueError("upstream must not point at the context compression proxy")
        server.event_sink = sink
        server.instance_id = instance_id
        server.upstream_url = url
        server.upstream_model = model
        server.upstream_headers = headers
        # The original native client owns request/task deadlines. No extra proxy
        # idle deadline; compressor's 120 s is entirely independent.
        server.upstream_timeout = runtime.get('main_upstream_timeout_seconds')
        if server.upstream_timeout is not None and (type(server.upstream_timeout) not in (int, float)
                or not math.isfinite(server.upstream_timeout) or server.upstream_timeout <= 0):
            raise ValueError('main_upstream_timeout_seconds must be null or positive')
        compressor = OpenAICompressor(runtime["upstream_base_url"], api_key, config.compressor_model or model, config, extra_headers, sink,
                                      audit_sink=audit)
        manager_type = AsyncContextManager if config.execution_mode == 'async' else ContextManager
        server.context_manager = manager_type(compressor, config, task=task, event_sink=sink,
                                               instance_id=instance_id,
                                               session_id=session_id, audit_sink=audit)
        return server
    except Exception:
        server.server_close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to private runtime JSON")
    args = parser.parse_args()
    try:
        runtime = strict_json_loads(Path(args.config).read_text(encoding="utf-8"))
        server = create_server(runtime)
    except Exception:
        parser.exit(2, "Invalid or unreadable context compression runtime configuration\n")
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.closing.set()
        server.context_manager.close()
        drained = server.context_manager.wait_closed()
        server.event_sink.finalize(drained)
        server.server_close()


if __name__ == "__main__":
    main()
