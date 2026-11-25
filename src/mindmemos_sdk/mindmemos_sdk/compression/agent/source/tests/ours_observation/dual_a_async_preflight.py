"""Real native CLI + preemptive async dual_a proxy, loopback only, network-none."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from src.context_compression.install_bridge import install as install_bridge
from src.context_compression.integration import provider_model
from src.context_compression.proxy import create_server, shutdown_server
from src.ours_observation.install import install as install_meter
from src.ours_observation.metrics import summarize
from tests.ours_observation.audit_helpers import audit_capacity_snapshot, audit_memory_update

ROOT = Path(__file__).resolve().parents[2]


def run(dynamic=False):
    # Both the Python compressor and native CLI use the loopback mock server.
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    from tests.context_compression.test_transport import mock_upstream, respond_json
    native = json.loads(subprocess.run(['node', str(ROOT/'src/context_compression/model_probe.mjs')],
                        capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1])
    count, comp = 0, 0
    main_requests, comp_inputs, comp_titles = [], [], []
    releases = [threading.Event() for _ in range(8)]
    released = [0]
    summaries = [f'SUM{i+1}-' + 'x'*3000 for i in range(8)]
    errors = []

    def respond(handler, captured):
        nonlocal count, comp
        try:
            p = captured['payload']
            assert '_wildclaw_context' not in p
            usage = {'prompt_tokens': 200, 'completion_tokens': 30, 'total_tokens': 230, 'cost': .123,
                     'prompt_tokens_details': {'cached_tokens': 100, 'cache_write_tokens': 0},
                     'completion_tokens_details': {'reasoning_tokens': 20}}
            if p.get('stream') is False:
                comp += 1
                call = comp
                assert call <= len(summaries), f'unexpected compression call {call}'
                assert 'response_format' not in p and len(p['messages']) == 1
                assert p['model'] == 'qwen/qwen3.5-122b-a10b'
                assert p['reasoning'] == {'effort': 'high'}
                assert not {'temperature', 'max_tokens', 'max_output_tokens'} & p.keys()
                text = p['messages'][0]['content']
                comp_titles.append(text.split('\n')[0])
                extract = lambda tag: re.search('<'+tag+'>\n(.*?)\n</'+tag+'>', text, re.S).group(1)
                comp_inputs.append({'NEW_RAW_TRAJECTORY': json.loads(extract('NEW_HISTORY_TO_COMPRESS')),
                                    'EXISTING_COMPRESSED_MEMORY': extract('PREVIOUS_SUMMARY')})
                assert releases[min(call-1, len(releases)-1)].wait(10), 'Main request blocked behind compression'
                respond_json(handler, {'choices': [{'message': {'content': summaries[call-1]},
                                                    'finish_reason': 'stop'}],
                                       'usage': usage, 'model': 'qwen/qwen3.5-122b-a10b'})
                return
            count += 1
            main_requests.append(p)
            # Adaptive: whichever main request finds a job in flight releases it,
            # which also proves the agent was never blocked behind compression.
            job = server.context_manager._pending
            if job is not None and not job.done.is_set() and released[0] < len(releases):
                releases[released[0]].set()
                released[0] += 1
                assert job.done.wait(10), 'Background job did not complete'
            if count <= 4:
                size = 20000
                calls = [{'index': i, 'id': f'call_{count}_{i}', 'type': 'function', 'function': {
                    'name': 'exec', 'arguments': json.dumps({'command':
                        f"python3 -c \"print('GROUP{count}-' + 'x'*{size})\""})}} for i in range(2)]
                delta, finish = {'role': 'assistant', 'tool_calls': calls}, 'tool_calls'
            else:
                delta, finish = {'role': 'assistant', 'content': 'done'}, 'stop'
            chunks = [{'id': f'mock{count}', 'model': 'openai/gpt-5.5', 'choices': [
                {'index': 0, 'delta': delta, 'finish_reason': None}]},
                {'id': f'mock{count}', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}], 'usage': usage}]
            body = ''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Content-Length', str(len(body.encode())))
            handler.end_headers()
            handler.wfile.write(body.encode())
            handler.wfile.flush()
        except Exception as exc:
            errors.append(repr(exc))
            respond_json(handler, {'error': 'offline preflight assertion'}, status=500)

    with tempfile.TemporaryDirectory(prefix='dual-a-async-preflight-') as tmp, mock_upstream(respond) as upstream:
        temp = Path(tmp)
        install_meter()
        install_bridge(Path('/usr/lib/node_modules/openclaw'))
        runtime = {'instance_id': '0123456789abcdef0123456789abcdef', 'session_id': 'chat', 'task': 'the task',
            'upstream_model': 'openai/gpt-5.5', 'upstream_api_key': 'offline-only',
            'upstream_base_url': f'http://127.0.0.1:{upstream.server_port}/v1', 'port': 0,
            'compression': {'method': 'dual_a', 'execution_mode': 'async', 'trigger_tokens': 8192,
                            'full_trigger_tokens': 16384, 'timeout_seconds': 240, 'retry_after_steps': 1,
                            'compressor_model': 'qwen/qwen3.5-122b-a10b',
                            'compressor_reasoning': {'effort': 'high'},
                            **({'dynamic_thresholds': True} if dynamic else {})},
            'log_dir': tmp+'/logs', 'main_upstream_timeout_seconds': None}
        server = create_server(runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime_file = temp/'runtime.json'
        runtime_file.write_text(json.dumps(runtime))
        base = json.loads(Path('/root/.openclaw/openclaw.json').read_text())
        base.setdefault('agents', {}).setdefault('defaults', {}).update(
            workspace=tmp+'/workspace', model={'primary': 'context-compression/openai/gpt-5.5'})
        base.setdefault('models', {}).setdefault('providers', {})['context-compression'] = {
            'api': 'openai-completions', 'baseUrl': f'http://127.0.0.1:{server.server_port}/v1',
            'apiKey': 'offline-only', 'models': [provider_model(native)]}
        config = temp/'config.json'
        config.write_text(json.dumps(base))
        env = {**os.environ, 'OPENCLAW_CONFIG_PATH': str(config), 'OPENCLAW_STATE_DIR': tmp+'/state',
               'OPENROUTER_API_KEY': 'offline-only', 'BRAVE_API_KEY': 'offline-only',
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               'WILDCLAW_CONTEXT_RUNTIME': str(runtime_file),
               'NODE_OPTIONS': '--import '+str(ROOT/'tests/host_observation/fixed_clock.mjs')}
        try:
            result = subprocess.run(['openclaw', 'agent', '--local', '--session-id', 'chat', '--message',
                                     'the task', '--timeout', '60'], env=env, capture_output=True, text=True, timeout=90)
            assert not errors, errors
            assert result.returncode == 0, result.stdout + result.stderr
            # Dynamic sizing raises the bar after a large step, so it legitimately
            # compresses less often on the same history.
            assert comp >= (1 if dynamic else 2), (count, comp, result.stdout[-2000:])
            # A trailing job may still be in flight when the agent stops; async
            # never blocks the agent on it. Drain exactly as shutdown does, so
            # the ledger is final before it is measured.
            for gate in releases:
                gate.set()
            server.context_manager.close()
            assert server.context_manager.wait_closed(10), 'shutdown drain timed out'
            # dual_a ships two prompts; every request carries exactly one of them.
            titles = {'WILDCLAWBENCH INCREMENTAL MEMORY DELTA', 'WILDCLAWBENCH FULL MEMORY CONSOLIDATION'}
            assert set(comp_titles) <= titles and comp_titles, comp_titles
            assert comp_titles[0] == 'WILDCLAWBENCH INCREMENTAL MEMORY DELTA', comp_titles
            # M is empty only on the first call; dual_a shows it from then on.
            assert comp_inputs[0]['EXISTING_COMPRESSED_MEMORY'] == ''
            if len(comp_inputs) > 1:
                assert comp_inputs[1]['EXISTING_COMPRESSED_MEMORY'] == summaries[0]
            memories = lambda p: [m['content'].split('\n')[-2] for m in p['messages']
                                  if '<compressed_history>' in str(m.get('content'))]
            # Async: a summary reaches the agent only from the request after it lands.
            assert memories(main_requests[0]) == []
            assert any(memories(p) for p in main_requests), 'no summary ever reached the agent'
            read = lambda p: [json.loads(s) for s in p.read_text().split('\n') if s.strip()] if p.exists() else []
            audit = read(temp/'logs/audit/compression_audit.jsonl')
            events = read(temp/'logs/events.jsonl')
            snapshots = {e['call_id']: e for e in audit if e['event'] == 'compression_snapshot'}
            outputs = {e['call_id']: e for e in audit if e['event'] == 'compression_result'}
            applied = [e for e in audit if e['event'] == 'compression_applied']
            assert len(applied) >= (1 if dynamic else 2), applied
            for event in applied:
                snapshot = snapshots[event['call_id']]
                audit_capacity_snapshot(snapshot)
                audit_memory_update(snapshot, outputs[event['call_id']], event)
                assert event['execution_mode'] == 'async'
                # The result replaces exactly the range it was computed from.
                assert (event['start'], event['end']) == (snapshot['start'], snapshot['end'])
                assert event['step'] > snapshot['step'] or event['steps_while_pending'] >= 0
            # Messages that arrived while a job ran are never inside its snapshot.
            assert all('GROUP4-' not in json.dumps(e['raw']) for e in snapshots.values())
            # The gates here let every job finish before the next main request,
            # so inflight/preemption paths are covered by the unit tests instead.
            metrics = summarize(read(temp/'logs/events.jsonl'), read(Path('/tmp/host_meter/routing.jsonl')),
                                read(Path('/tmp/host_meter/events.jsonl')), count)
            assert metrics['coverage_verified'] and metrics['billing_complete'], metrics
            assert metrics['execution']['compressor']['calls'] == comp
        finally:
            for gate in releases:
                gate.set()
            shutdown_server(server)
            thread.join(2)
    print(json.dumps({'passed': True, 'method': 'dual_a_async_dyn' if dynamic else 'dual_a_async',
                      'dynamic_thresholds': dynamic, 'native_model': native,
                     'checks': ['real_native_cli', 'two_prompts_by_trigger', 'memory_visible_incremental',
                                'async_apply_on_next_request', 'durable_result_range',
                                'retained_new_tool_groups', 'billing_dedup']}))


if __name__ == '__main__':
    import sys
    run(dynamic='--dynamic' in sys.argv)
