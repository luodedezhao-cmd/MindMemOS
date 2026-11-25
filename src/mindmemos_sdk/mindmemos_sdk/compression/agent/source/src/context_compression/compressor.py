"""Direct OpenAI-compatible compression, with opt-in two-stage orchestration."""

from dataclasses import replace
import json
import math
import time
import threading
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .prompts import build_messages
from .types import BoundaryDecision, CompressionConfig, CompressionResult, CompressionSnapshot
from .audit import AuditWriteError, persist
from .cancellation import Cancellation, CompressionCancelled, handlers


class CompressionError(ValueError):
    """A safe diagnostic: never includes response bodies, prompts, or credentials."""

    @property
    def code(self):
        value = str(self)
        if 'timed out' in value:
            return 'timeout'
        if value == 'compressor upstream HTTP error':
            return 'upstream_http_error'
        if value == 'compressor transport failed':
            return 'transport_error'
        return 'invalid_result'


def _reject_constant(_value):
    raise CompressionError("non-finite JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CompressionError("duplicate JSON field")
        result[key] = value
    return result


def strict_json_loads(content):
    try:
        return json.loads(content, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CompressionError("invalid JSON") from None


def safe_usage(value):
    """Keep standard numeric usage metadata, never provider free-form strings."""
    if not isinstance(value, dict):
        return None
    allowed = {
        "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
        "cached_tokens", "cache_write_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens",
        "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens", "cost",
        "prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details",
        "cost_details", "upstream_inference_cost", "upstream_inference_prompt_cost", "upstream_inference_completions_cost",
    }
    result = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if item is None:
            result[key] = None
        elif isinstance(item, dict):
            nested = safe_usage(item)
            if nested:
                result[key] = nested
        elif type(item) in (int, float) and item >= 0:
            try:
                if math.isfinite(item):
                    result[key] = item
            except OverflowError:
                pass
    return result or None


def parse_result(content, finish_reason="stop", usage=None, latency_seconds=0.0, *, boundary=False):
    if finish_reason != "stop":
        raise CompressionError("compressor did not finish normally")
    if not isinstance(content, str):
        raise CompressionError("compressor content must be a string")
    data = strict_json_loads(content)
    if not isinstance(data, dict) or set(data) != {"stage_end", "confidence", "reason", "C"}:
        raise CompressionError("compressor fields do not match contract")
    if type(data["stage_end"]) is not bool:
        raise CompressionError("stage_end must be Boolean")
    confidence = data["confidence"]
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence):
        raise CompressionError("confidence must be finite and between zero and one")
    for key in ("reason", "C"):
        if key == "C" and boundary and data["stage_end"]:
            if data[key] is not None:
                raise CompressionError("boundary requires C=null")
            continue
        if not isinstance(data[key], str) or not data[key].strip():
            raise CompressionError("reason and C must be nonempty strings")
    if type(latency_seconds) not in (int, float) or not math.isfinite(latency_seconds) or latency_seconds < 0:
        raise CompressionError("invalid latency metadata")
    return (BoundaryDecision if boundary else CompressionResult)(
        data["stage_end"], float(confidence), data["reason"], data["C"],
        safe_usage(usage), float(latency_seconds),
    )


def chat_completions_url(base_url):
    if not isinstance(base_url, str):
        raise ValueError("upstream_base_url must be an HTTP(S) URL")
    parsed = urlsplit(base_url.strip())
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("upstream_base_url must be an HTTP(S) URL without credentials, query or fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_upstream(url, body, headers, timeout, cancellation=None):
    """Do not follow redirects with credentials or recursively call the proxy."""
    request = Request(url, data=body, headers=headers, method="POST")
    extra = handlers(cancellation) if cancellation is not None else ()
    return build_opener(_NoRedirect(), *extra).open(request, timeout=timeout)


def upstream_headers(api_key, headers=None):
    result = {"Content-Type": "application/json", "Accept": "application/json"}
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or "\n" in key + value or "\r" in key + value:
            raise ValueError("upstream headers must contain single-line strings")
        if key.lower() not in {"host", "content-length", "connection", "transfer-encoding", "authorization"}:
            result[key] = value
    if api_key:
        result["Authorization"] = "Bearer " + api_key
    return result


def emit(event_sink, event):
    if event_sink is not None:
        try:
            event_sink(event)
        except Exception:
            # Logging cannot change the trajectory or make a successful call fail.
            pass


class OpenAICompressor:
    def __init__(self, base_url, api_key, model, config: CompressionConfig, headers=None, event_sink=None,
                 audit_sink=None):
        self.url = chat_completions_url(base_url)
        self.headers = upstream_headers(api_key, headers)
        self.model = model
        self.config = config
        self.event_sink = event_sink
        self.audit_sink = audit_sink
        self._cancel_lock = threading.Lock()
        self._closed = False
        # A set, not one slot: preemption overlaps a superseded call with its
        # successor, and one slot lets either clobber or miss the other.
        self._active = set()

    def cancel(self):
        """Terminal task shutdown, safe while __call__ is blocked in network I/O."""
        with self._cancel_lock:
            self._closed = True
            for cancellation in tuple(self._active):
                cancellation.cancel()

    def __call__(self, snapshot: CompressionSnapshot, cancellation=None) -> CompressionResult:
        if self.config.method not in ("v3", "v4"):
            return self._call(snapshot, cancellation=cancellation)
        if snapshot.compression_mode not in ("boundary", "full"):
            raise CompressionError(f"{self.config.method} requires boundary or full mode")
        started = time.monotonic()
        snapshot = replace(snapshot, call_id=snapshot.call_id or uuid.uuid4().hex)
        snapshot = replace(snapshot, compression_id=snapshot.call_id)
        decision_event = {"event": "compression_decision", "call_id": snapshot.call_id,
                          "phase": "execution",
                          "step": snapshot.step, "generation": snapshot.generation,
                          "revision": snapshot.revision}
        if snapshot.compression_mode == "full":
            emit(self.event_sink, {**decision_event, "stage_end": None, "confidence": None,
                                  "decision_source": "capacity_policy", "trigger_reason": "full_capacity"})
            return self._call(snapshot, cancellation=cancellation)
        decision = self._call(snapshot, cancellation=cancellation)
        record = {**decision_event, "stage_end": decision.stage_end, "confidence": decision.confidence,
                  "reason": decision.reason, "C": decision.C, "decision_source": "semantic_boundary",
                  "trigger_reason": "semantic_boundary" if decision.stage_end else "incremental_threshold"}
        persist(self.audit_sink, {**record, "event": "boundary_result"}, self.event_sink)
        emit(self.event_sink, {k: v for k, v in record.items() if k not in ("reason", "C")})
        if not decision.stage_end:
            return CompressionResult(False, decision.confidence, decision.reason, decision.C,
                                     decision.usage, time.monotonic() - started)
        # No state is committed between stages. Use the original frozen M/R,
        # never the decision/reason as summary evidence. Both calls share one deadline.
        with self._cancel_lock:
            if self._closed:
                raise CompressionCancelled()
        remaining = self.config.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise CompressionError("compressor timed out")
        full = self._call(replace(snapshot, compression_mode="full", call_id=uuid.uuid4().hex),
                          timeout_seconds=remaining, cancellation=cancellation)
        # Usage is authoritative per request in compressor_usage, not in this
        # combined result. Do not expose only the second call as total usage.
        return CompressionResult(True, decision.confidence, decision.reason, full.C,
                                 None, time.monotonic() - started)

    def _call(self, snapshot: CompressionSnapshot, timeout_seconds=None, cancellation=None):
        started = time.monotonic()
        timeout_seconds = self.config.timeout_seconds if timeout_seconds is None else timeout_seconds
        boundary = self.config.method in ("v3", "v4") and snapshot.compression_mode == "boundary"
        # This compressor serves owned Agent history only, never the grader.
        # Freeze classification in this per-call record; late completion and a
        # second-stage full call must not consult the mutable host phase file.
        event = {"event": "compressor_usage", "step": snapshot.step, "generation": snapshot.generation,
                 "phase": "execution",
                 "revision": snapshot.revision, "usage": None, "success": False,
                 "call_id": snapshot.call_id or uuid.uuid4().hex, "source": "compressor", "model": self.model}
        if self.config.method in ("v3", "v4"):
            event.update(compression_id=snapshot.compression_id, compression_mode=snapshot.compression_mode)
        emit(self.event_sink, {**event, "event": "compressor_started"})
        # The caller may own the handle so it can cancel before this thread
        # even reaches here; otherwise one is created for this call alone.
        cancellation = cancellation or Cancellation()
        with self._cancel_lock:
            self._active.add(cancellation)
            if self._closed:
                cancellation.cancel()
        try:
            cancellation.check()
            request = {"model": self.model, "messages": build_messages(snapshot, self.config.method), "stream": False}
            if self.config.compressor_reasoning is not None:
                request["reasoning"] = dict(self.config.compressor_reasoning)
            if self.config.method == "ours" or boundary:
                request["response_format"] = {"type": "json_object"}
            # Both policies use the provider default; do not override sampling.
            body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
            persist(self.audit_sink, {'event': 'compressor_request', 'call_id': event['call_id'],
                                     'compression_id': snapshot.compression_id,
                                     'compression_mode': snapshot.compression_mode,
                                     'request': request}, self.event_sink)
            cancellation.check()
            with open_upstream(self.url, body, self.headers, timeout_seconds, cancellation) as response:
                chunks = []
                length = 0
                while True:
                    cancellation.check()
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise CompressionError("compressor timed out")
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(65536)
                    if not chunk:
                        cancellation.check()
                        break
                    length += len(chunk)
                    if length > 16 * 1024 * 1024:
                        raise CompressionError("compressor response exceeds size limit")
                    chunks.append(chunk)
            response_body = b"".join(chunks)
            # Keep returned billing metadata even if durable audit storage fails.
            # Malformed JSON must still be persisted before surfacing its error.
            try:
                envelope = strict_json_loads(response_body)
            except CompressionError:
                envelope = None
            if isinstance(envelope, dict):
                event['usage'] = safe_usage(envelope.get('usage'))
            persist(self.audit_sink, {'event': 'compressor_response', 'call_id': event['call_id'],
                                     'body': response_body.decode('utf-8', errors='replace'),
                                     'body_complete': True}, self.event_sink)
            cancellation.check()
            if envelope is None:
                envelope = strict_json_loads(response_body)
            if not isinstance(envelope, dict):
                raise CompressionError("invalid compressor response envelope")
            event["usage"] = safe_usage(envelope.get("usage"))
            if isinstance(envelope.get("model"), str):
                event["returned_model"] = envelope["model"]
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise CompressionError("compressor must return one choice")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("tool_calls") or message.get("refusal"):
                raise CompressionError("invalid compressor message")
            if self.config.method in ("p0", "p1", "dual_threshold", "dual_p1", "dual_mini", "dual_neutral", "dual_a") or (self.config.method in ("v3", "v4") and not boundary):
                content = message.get("content")
                if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                    raise CompressionError("summary incomplete or empty")
                # Full-replacement policy, not a semantic boundary prediction.
                full = self.config.method in ("p0", "p1") or snapshot.compression_mode == "full"
                reason = (self.config.method.upper() + " full replacement policy" if self.config.method in ("p0", "p1") else
                          "Capacity policy: " + snapshot.compression_mode)
                result = CompressionResult(full, 1.0, reason, content,
                                           event["usage"], time.monotonic() - started)
            else:
                result = parse_result(message.get("content"), choice.get("finish_reason"), event["usage"],
                                      time.monotonic() - started, boundary=boundary)
            event["success"] = True
            return result
        except AuditWriteError:
            raise
        except CompressionCancelled:
            raise
        except HTTPError as exc:
            event["status"] = exc.code
            try:
                body = exc.read(16 * 1024 * 1024 + 1)
                if len(body) <= 16 * 1024 * 1024:
                    try:
                        data = strict_json_loads(body)
                    except CompressionError:
                        data = None
                    if isinstance(data, dict):
                        event['usage'] = safe_usage(data.get('usage'))
                    persist(self.audit_sink, {'event': 'compressor_response', 'call_id': event['call_id'],
                            'status': exc.code, 'body': body.decode('utf-8', errors='replace'),
                            'body_complete': not cancellation.cancelled.is_set()}, self.event_sink)
            except Exception:
                pass
            exc.close()
            cancellation.check()
            raise CompressionError("compressor upstream HTTP error") from None
        except TimeoutError:
            cancellation.check()
            raise CompressionError('compressor timed out') from None
        except CompressionError:
            cancellation.check()
            raise
        except Exception:
            cancellation.check()
            raise CompressionError("compressor transport failed") from None
        finally:
            event['cancelled'] = cancellation.cancelled.is_set()
            event['request_dispatched'] = cancellation.request_dispatched
            event["latency_seconds"] = time.monotonic() - started
            emit(self.event_sink, event)
            with self._cancel_lock:
                self._active.discard(cancellation)
