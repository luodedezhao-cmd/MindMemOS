"""Route native summary requests to Qwen; preserve main and grading bodies."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time

import requests

from .transport_helpers import (
    category, usage_from_response, summarize_usage as basic_usage,
)

UPSTREAM = 'https://openrouter.ai/api/v1'
COMPRESSOR_MODEL = 'qwen/qwen3.5-122b-a10b'
COMPRESSOR_REASONING = {'effort': 'high'}


def summarize_usage(entries):
    result = basic_usage(entries)
    for role, summary in result.items():
        usages = [e['usage'] for e in entries if e.get('category') == role and isinstance(e.get('usage'), dict)]
        costs = [u['cost'] for u in usages if isinstance(u.get('cost'), (int, float))]
        summary['api_reported_cost_usd_known'] = sum(costs)
        summary['requests_with_api_reported_cost'] = len(costs)
        summary['requests_without_api_reported_cost'] = summary['requests'] - len(costs)
        summary['cached_input_tokens_known'] = sum(u.get('prompt_tokens_details', {}).get('cached_tokens', 0) or 0 for u in usages)
    return result


def start_gateway(batch: Path, key: str):
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    ledger = []
    ledger_path = batch / 'request_ledger.jsonl'

    def record(entry):
        with lock:
            with ledger_path.open('a') as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + '\n')

    class Gateway(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def reply(self, status, data, ctype='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.path.endswith('/models'):
                self.reply(404, b'{}')
                return
            try:
                response = requests.get(UPSTREAM + '/models', timeout=30)
                self.reply(response.status_code, response.content)
            except requests.RequestException:
                self.reply(502, b'{}')

        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.reply(401, b'{}')
                return
            if not self.path.endswith('/chat/completions'):
                self.reply(403, b'{}')
                return
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            try:
                body = json.loads(raw)
            except ValueError:
                self.reply(400, b'{}')
                return
            task = self.path.split('/')[1]
            task = None if task in ('api', 'v1') else task
            role = category(body)
            forwarded = raw
            actual_model = body.get('model')
            if role == 'compressor':
                if actual_model != 'openai/gpt-5.5':
                    self.reply(400, b'{"error":{"message":"Unexpected native summarizer model"}}')
                    return
                body = {**body, 'model': COMPRESSOR_MODEL, 'reasoning': COMPRESSOR_REASONING}
                forwarded = json.dumps(body, ensure_ascii=False).encode()
                actual_model = COMPRESSOR_MODEL
            started = time.monotonic()
            with lock:
                entry = {'id': len(ledger), 'task_id': task, 'started_at': time.time(),
                         'category': role, 'model_in': json.loads(raw).get('model'), 'model_out': actual_model,
                         'compressor_reasoning': COMPRESSOR_REASONING if role == 'compressor' else None,
                         'forwarded_request_sha256': hashlib.sha256(forwarded).hexdigest(),
                         'changed_fields': ['model', 'reasoning'] if role == 'compressor' else [],
                         'stream': bool(body.get('stream')), 'input_bytes': len(raw),
                         'request_sha256': hashlib.sha256(raw).hexdigest()}
                ledger.append(entry)
            record({'event': 'request_start', **entry})
            try:
                response = requests.post(UPSTREAM + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                    data=forwarded, timeout=900)
                data = response.content
                entry.update(status=response.status_code, usage=usage_from_response(data, body.get('stream')),
                    wall_seconds=time.monotonic() - started, finished_at=time.time())
                request_id = response.headers.get('x-request-id')
                if request_id:
                    entry['upstream_request_id'] = request_id
                if response.status_code >= 400:
                    try:
                        error = response.json().get('error', {})
                        message = error.get('message', '') if isinstance(error, dict) else str(error)
                        entry['upstream_error'] = str(message).replace(key, '[REDACTED]').replace(token, '[REDACTED]')[:1500]
                        entry['upstream_error_code'] = error.get('code') if isinstance(error, dict) else None
                    except (ValueError, AttributeError):
                        entry['upstream_error'] = 'Non-JSON upstream error'
                record({'event': 'request_end', **entry})
                self.reply(response.status_code, data, response.headers.get('Content-Type', 'application/json'))
            except requests.RequestException as exc:
                entry.update(status=None, error_type=type(exc).__name__,
                    wall_seconds=time.monotonic() - started, finished_at=time.time())
                record({'event': 'request_end', **entry})
                self.reply(502, b'{"error":{"message":"OpenRouter upstream request failed"}}')

    server = ThreadingHTTPServer(('0.0.0.0', 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ledger, token, f'http://172.17.0.1:{server.server_address[1]}/api/v1'
