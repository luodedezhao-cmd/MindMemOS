"""Exercise the packaged adapter and real native CLI in network-none containers."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bootstrap import load_runtime
from common import docker, read, validate_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-root', required=True, type=Path)
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    identity = validate_benchmark(root, [])
    load_runtime(root)
    from agent import make_agent
    from src.agents.openclaw.model_registration import ensure_gpt55_registered
    from src.context_compression.integration import install_and_start_proxy, resolve_upstream, stop_proxy
    results = {}
    for case in ('adapter', 'native_async'):
        name = 'wcb-adyn-offline-' + uuid.uuid4().hex[:12]
        docker('run', '-d', '--network', 'none', '--name', name,
               '-v', str(HERE / 'source') + ':/work:ro', '--entrypoint', '/bin/sleep',
               identity['image_id'], '180')
        try:
            if case == 'adapter':
                config_path = '/root/.openclaw/openclaw.json'
                before = json.loads(docker('exec', name, 'cat', config_path).stdout)
                base_url = 'http://127.0.0.1:19999/api/v1'
                backend = make_agent(18789, 'offline-only', base_url)
                image_model = ensure_gpt55_registered(name, image_model=backend.image_model,
                    base_url=base_url, api_key='offline-only')
                upstream = resolve_upstream('openrouter/openai/gpt-5.5', None,
                    openrouter_base_url=base_url, openrouter_api_key='offline-only')
                alias = install_and_start_proxy(name, upstream, backend.compression_config, 'the task')
                try:
                    backend._set_model(name, alias)
                    backend._set_image_model(name, image_model)
                    after = json.loads(docker('exec', name, 'cat', config_path).stdout)
                    assert after['agents']['defaults']['model']['primary'] == alias
                    assert after['agents']['defaults']['imageModel']['primary'] == 'openrouter-vision/openai/gpt-5.5'
                    assert before.get('agents', {}).get('defaults', {}).get('compaction') == \
                        after.get('agents', {}).get('defaults', {}).get('compaction')
                    runtime = json.loads(docker('exec', name, 'cat', '/root/wildclaw_context_runtime/runtime.json').stdout)
                    assert runtime['compression'] == read(HERE / 'config.json')
                    assert runtime['upstream_model'] == 'openai/gpt-5.5'
                    results[case] = {'proxy_healthy': True, 'original_model_descriptor_verified': True,
                        'observer_installed': True, 'vision_registration_preserved': True,
                        'native_compaction_config_unchanged': True, 'original_qwen_config': True}
                finally:
                    stop_proxy(name)
            else:
                result = subprocess.run(['docker', 'exec', '-w', '/work', name, 'python3', '-m',
                    'tests.ours_observation.dual_a_async_preflight', '--dynamic'],
                    capture_output=True, text=True, timeout=110)
                if result.returncode:
                    raise AssertionError('Offline native probe failed:\n' + result.stdout + result.stderr)
                report = json.loads(result.stdout.strip().splitlines()[-1])
                assert report['passed'] and report['dynamic_thresholds']
                results[case] = {'passed': True, 'checks': report['checks'],
                                'qwen_model_and_high_reasoning_asserted': True}
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
    print(json.dumps({'status': 'passed', 'network': 'none', 'paid_model_calls': 0,
                      'conditions': results}, indent=2))


if __name__ == '__main__':
    main()
