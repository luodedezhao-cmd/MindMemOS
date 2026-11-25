"""Run the original ADyn Agent on the shared task list; preflight by default."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import os
from pathlib import Path
import subprocess

from bootstrap import HERE, load_runtime, validate_source
from common import JUDGE, MODEL, UPSTREAM, digest, read, save, select_tasks, validate_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-root', required=True, type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--task', help='One task_id from compression/task_list_31.json')
    choice.add_argument('--all', action='store_true', help='All 31 tasks')
    parser.add_argument('--output', type=Path, help='New directory, required with --execute')
    parser.add_argument('--env-file', type=Path, help='Private dotenv file')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    tasks = select_tasks(args.task)
    identity = validate_benchmark(root, tasks)
    validate_source()
    config = read(HERE / 'config.json')
    plan = {'condition': 'ADyn', 'model': MODEL, 'judge': JUDGE, 'thinking_override': None,
            'compression': config, 'image_id': identity['image_id'], 'workers': args.workers,
            'repeats': 1, 'automatic_task_retries': False, 'tasks': tasks,
            'source_manifest_sha256': digest(HERE / 'source_manifest.json')}
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
        parser.error('Set OPENROUTER_API_KEY in the environment or --env-file')
    if subprocess.run(['docker', 'image', 'inspect', identity['image_id']], capture_output=True).returncode:
        parser.error('The pinned Docker image is not installed locally')
    for name in ('WILDCLAW_CONTEXT_RUNTIME', 'NODE_OPTIONS'):
        os.environ.pop(name, None)
    os.environ.update(DOCKER_IMAGE=identity['image_id'], JUDGE_MODEL=JUDGE,
                      OPENROUTER_API_KEY=key, OPENROUTER_BASE_URL=UPSTREAM)
    os.chdir(root)
    run_batch = load_runtime(root)
    from src.context_compression.types import CompressionConfig
    CompressionConfig.from_dict(config)
    from src.utils.task_parser import parse_task_md
    parsed = {t['task_id']: parse_task_md(root / t['task']) for t in tasks}
    for task in tasks:
        spec = parsed[task['task_id']]
        if spec['task_id'] != task['task_id'] or spec['timeout_seconds'] != task['timeout_seconds']:
            raise ValueError('Task identity or deadline changed')
        if not Path(spec['workspace_path']).is_dir():
            raise ValueError('Missing clean workspace for ' + task['task_id'])
    from agent import make_agent
    backend = make_agent(run_batch.GATEWAY_PORT, key, UPSTREAM)
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'manifest.json', plan)
    records = []

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

    try:
        save(output / 'status.json', {'status': 'running', 'planned': len(tasks)})
        record(one(tasks[0]))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(one, task) for task in tasks[1:]]):
                record(future.result())
        save(output / 'status.json', {'status': 'finished', 'completed': len(records)})
    except BaseException as error:
        save(output / 'status.json', {'status': 'stopped', 'completed': len(records),
                                      'error_type': type(error).__name__})
        raise


if __name__ == '__main__':
    main()
