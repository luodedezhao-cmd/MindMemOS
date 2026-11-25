"""Check both adapters and native summary boundaries in network-disabled containers.

Requires the pinned image and benchmark dependencies. No task or model API runs.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import docker, read, validate_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-root', required=True, type=Path)
    args = parser.parse_args()
    benchmark = args.benchmark_root.resolve()
    identity = validate_benchmark(benchmark, [])
    sys.path.insert(0, str(benchmark))
    from baseline1.agent import make_agent as make_native
    from baseline2.agent import make_agent as make_sync
    reports = {}
    for condition, factory in [('baseline1', make_native), ('baseline2', make_sync)]:
        name = 'wcb-package-offline-' + uuid.uuid4().hex[:12]
        docker('run', '-d', '--network', 'none', '--name', name,
               '--entrypoint', '/bin/sleep', identity['image_id'], '180')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                config_path = '/root/.openclaw/openclaw.json'
                before = json.loads(docker('exec', name, 'cat', config_path).stdout)
                agent = factory(18789, 'offline-fixture-key', 'http://172.17.0.1:19999/api/v1')
                if condition == 'baseline2':
                    agent._outputs[name] = output
                agent._set_model(name, 'openrouter/openai/gpt-5.5')
                agent._set_image_model(name, agent.image_model)
                after = json.loads(docker('exec', name, 'cat', config_path).stdout)
                if condition == 'baseline1':
                    old_compaction = before.get('agents', {}).get('defaults', {}).get('compaction')
                    assert after.get('agents', {}).get('defaults', {}).get('compaction') == old_compaction
                    assert 'image' in after['models']['providers']['openrouter-vision']['models'][0]['input']
                    meter = json.loads(docker('exec', name, 'cat', '/tmp/host_meter/installation.json').stdout)
                    assert meter['native_compaction_config_changed'] is False
                    assert meter['context_reconstruction'] is False
                    reports[condition] = {'observer_installed': True, 'compaction_config_unchanged': True,
                                          'gpt55_image_registration_preserved': True}
                else:
                    assert after['agents']['defaults']['compaction']['mode'] == 'default'
                    provider = after['models']['providers']['openrouter']
                    assert provider['baseUrl'].endswith('/' + name + '/api/v1')
                    report = read(output / 'native_sync_full_patch.json')
                    assert report['threshold_tokens'] == 16384
                    assert report['helper_sha256'] == '6d1a13330b833615502540f5e698eb0d58c00deb6d6516a9507e3f6e19c6866f'
                    fixture = (ROOT / 'tests/native_probe.mjs').read_text()
                    result = docker('exec', '-i', name, 'node', '--input-type=module', input=fixture)
                    behavior = json.loads(result.stdout.strip().splitlines()[-1])
                    reports[condition] = {'installed': True, 'task_routing_preserved': True,
                                          'original_helper_hash_verified': True, 'behavior': behavior}
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
    print(json.dumps({'status': 'passed', 'network': 'none', 'paid_model_calls': 0,
                      'conditions': reports}, indent=2))


if __name__ == '__main__':
    main()
