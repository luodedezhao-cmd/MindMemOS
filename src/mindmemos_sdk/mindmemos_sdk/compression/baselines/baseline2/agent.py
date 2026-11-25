"""Extract the synchronous install and Qwen-provider wiring from the experiment."""
import copy
import json
from pathlib import Path
from threading import Lock

from common import docker, save
from src.agents.openclaw.runner import OpenClawAgent
from .native_sync_full import install_native_sync_full

HERE = Path(__file__).resolve().parent


def provider_model(native):
    result = {k: copy.deepcopy(v) for k, v in native.items() if k not in {'provider', 'baseUrl'}}
    for key in ('reasoningEffortMap', 'openRouterRouting', 'vercelGatewayRouting'):
        if result.get('compat', {}).pop(key, {}) not in ({}, None):
            raise ValueError('Unsupported non-default native routing/effort mapping')
    return result


class SyncFullHost(OpenClawAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._outputs = {}
        self._lock = Lock()

    def run_task(self, spec):
        with self._lock:
            self._outputs[spec.task_id] = spec.output_dir
        try:
            return super().run_task(spec)
        finally:
            with self._lock:
                self._outputs.pop(spec.task_id, None)

    def _set_model(self, task_id, model):
        super()._set_model(task_id, model)
        with self._lock:
            output_dir = self._outputs[task_id]
        docker('cp', str(HERE / 'model_probe.mjs'), f'{task_id}:/tmp/wcb-model-probe.mjs')
        probe = docker('exec', task_id, 'node', '/tmp/wcb-model-probe.mjs', 'openrouter', 'openai/gpt-5.5')
        descriptor = provider_model(json.loads(probe.stdout.strip().splitlines()[-1]))
        base = self.openrouter_base_url.removesuffix('/api/v1') + '/' + task_id + '/api/v1'
        conf = {'baseUrl': base, 'apiKey': self.openrouter_api_key,
                'api': 'openai-completions', 'models': [descriptor]}
        script = ('import json,pathlib,sys;p=pathlib.Path("/root/.openclaw/openclaw.json");'
                  'd=json.loads(p.read_text());d.setdefault("models",{}).setdefault("providers",{})'
                  '["openrouter"]=json.load(sys.stdin);p.write_text(json.dumps(d))')
        docker('exec', '-i', task_id, 'python3', '-c', script, input=json.dumps(conf))
        save(output_dir / 'model_descriptor.json', descriptor)
        # Installed before the gateway starts; same helper, prompt and threshold.
        install_native_sync_full(task_id, 16384, output_dir)

    def collect_usage(self, task_id, output_dir, elapsed_time):
        legacy = super().collect_usage(task_id, output_dir, elapsed_time)
        copied = docker('cp', f'{task_id}:/tmp/openclaw/sync_full', str(output_dir / 'sync_full'), check=False)
        save(output_dir / 'compaction_log_collection.json', {'collected': copied.returncode == 0})
        return legacy


def make_agent(port, key, base_url):
    return SyncFullHost(port, openrouter_api_key=key, openrouter_base_url=base_url,
                        image_model='openrouter/openai/gpt-5.5')
