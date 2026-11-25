"""Portable launch plumbing for the two fixed OpenClaw conditions.

The benchmark supplies execution/grading. This module does not implement a
compression policy. No containers or model calls are started without --execute.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
COMPRESSION = HERE.parent
MODEL = 'openrouter/openai/gpt-5.5'
JUDGE = 'openai/gpt-5.4'
UPSTREAM = 'https://openrouter.ai/api/v1'


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def docker(*args, check=True, input=None):
    result = subprocess.run(['docker', *args], capture_output=True, text=True,
                            timeout=90, input=input)
    if check and result.returncode:
        # Commands/configuration may contain credentials. Do not echo them.
        raise RuntimeError('Container operation failed: ' + args[0])
    return result


def select_tasks(task_id=None):
    tasks = read(COMPRESSION / 'task_list_31.json')['tasks']
    if len(tasks) != 31 or len({t['task_id'] for t in tasks}) != 31:
        raise ValueError('Expected the fixed 31 unique tasks')
    if task_id is None:
        return tasks
    selected = [t for t in tasks if t['task_id'] == task_id]
    if not selected:
        raise ValueError('Task is not in task_list_31.json')
    return selected


def validate_benchmark(root, tasks):
    identity = read(HERE / 'pinned_benchmark.json')
    for relative, expected in identity['source_sha256'].items():
        path = root / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Benchmark source mismatch: ' + relative)
    for task in tasks:
        path = root / task['task']
        if not path.is_file() or digest(path) != task['task_sha256']:
            raise ValueError('Task input mismatch: ' + task['task_id'])
    return identity


def main(condition):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-root', required=True, type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--task', help='One task_id from task_list_31.json')
    choice.add_argument('--all', action='store_true', help='Run all 31 tasks')
    parser.add_argument('--output', type=Path, help='New directory, required with --execute')
    parser.add_argument('--env-file', type=Path, help='Private dotenv file, never copied to output')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1 if condition == 'baseline1' else 2)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    tasks = select_tasks(args.task)
    identity = validate_benchmark(root, tasks)
    # Preserve the existing batch's arXiv-first ordering for the synchronous condition.
    if condition == 'baseline2':
        tasks = sorted(tasks, key=lambda t: (t['task_id'] != '01_Productivity_Flow_task_1_arxiv_digest', t['index']))
    plan = {'condition': condition, 'model': MODEL, 'judge': JUDGE,
            'compressor_model': 'qwen/qwen3.5-122b-a10b' if condition == 'baseline2' else None,
            'threshold_tokens': 16384 if condition == 'baseline2' else None,
            'image_id': identity['image_id'], 'thinking_override': None,
            'repeats': 1, 'workers': args.workers, 'automatic_task_retries': False,
            'tasks': tasks}
    if not args.execute:
        print(json.dumps({'status': 'source_and_task_hashes_verified_no_execution', **plan}, ensure_ascii=False))
        return
    if args.output is None:
        parser.error('--execute requires a new --output directory')
    output = args.output.resolve()
    if output.exists():
        parser.error('--output already exists; choose a fresh directory')
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error('--env-file is not a file')
        load_dotenv(args.env_file.resolve(), override=True)
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if not key:
        parser.error('OPENROUTER_API_KEY must be supplied in the environment or --env-file')
    if subprocess.run(['docker', 'image', 'inspect', identity['image_id']], capture_output=True).returncode:
        parser.error('The pinned Docker image is not installed locally')
    for name in ('WILDCLAW_CONTEXT_RUNTIME', 'NODE_OPTIONS'):
        os.environ.pop(name, None)
    os.environ.update(DOCKER_IMAGE=identity['image_id'], JUDGE_MODEL=JUDGE,
                      OPENROUTER_API_KEY=key, OPENROUTER_BASE_URL=UPSTREAM)
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Parse fixtures before creating any run output or starting the relay.
    from src.utils.task_parser import parse_task_md
    parsed = {t['task_id']: parse_task_md(root / t['task']) for t in tasks}
    for task in tasks:
        spec = parsed[task['task_id']]
        if spec['task_id'] != task['task_id'] or spec['timeout_seconds'] != task['timeout_seconds']:
            raise ValueError('Task identity or deadline changed')
        if not Path(spec['workspace_path']).is_dir():
            raise ValueError('Missing clean workspace for ' + task['task_id'])
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'manifest.json', plan)
    server = None
    records = []
    try:
        if condition == 'baseline2':
            from baseline2.qwen_transport import start_gateway
            server, ledger, token, gateway = start_gateway(output, key)
            os.environ.update(OPENROUTER_API_KEY=token, OPENROUTER_BASE_URL=gateway)
            for name in ('NO_PROXY', 'no_proxy', 'NO_PROXY_INNER'):
                os.environ[name] = os.environ.get(name, '') + ',172.17.0.1,127.0.0.1,localhost'
        from eval import run_batch
        if Path(run_batch.__file__).resolve() != root / 'eval/run_batch.py':
            raise RuntimeError('Wrong benchmark module imported')
        module = importlib.import_module(condition + '.agent')
        backend = module.make_agent(run_batch.GATEWAY_PORT,
                                    os.environ['OPENROUTER_API_KEY'],
                                    os.environ['OPENROUTER_BASE_URL'])

        def one(task):
            target = output / 'runs' / task['task_id']
            target.mkdir(parents=True, exist_ok=False)
            result = run_batch.run_single_task(parsed[task['task_id']], MODEL, backend,
                                               target, thinking=None, models_config=None)
            record = {'task_id': task['task_id'], 'result': result}
            save(target / 'result.json', record)
            return record

        def record(value):
            records.append(value)
            save(output / 'summary.json', {'completed': len(records), 'planned': len(tasks),
                                          'rows': sorted(records, key=lambda r: r['task_id'])})
            print(json.dumps({'completed': len(records), 'task_id': value['task_id'],
                              'scores': value['result'].get('scores')}, ensure_ascii=False), flush=True)

        save(output / 'status.json', {'status': 'running', 'planned': len(tasks)})
        # Execute the first task before opening the rest of the bounded queue.
        record(one(tasks[0]))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(one, task) for task in tasks[1:]]):
                record(future.result())
        save(output / 'status.json', {'status': 'finished', 'completed': len(records)})
    except BaseException as error:
        save(output / 'status.json', {'status': 'stopped', 'completed': len(records),
                                      'error_type': type(error).__name__})
        raise
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
