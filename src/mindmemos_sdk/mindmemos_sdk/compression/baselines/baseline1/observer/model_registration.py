"""Register GPT-5.5 vision capability in each disposable experiment container."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

MODEL = 'openrouter/openai/gpt-5.5'
VISION_PROVIDER = 'openrouter-vision'
VISION_MODEL = VISION_PROVIDER + '/openai/gpt-5.5'
CONFIG_PATH = '/root/.openclaw/openclaw.json'


def merge_registration(config: dict, native_model: dict, base_url: str, api_key: str) -> dict:
    """Preserve existing providers/overrides; only add the verified image capability."""
    if native_model.get('id') != 'openai/gpt-5.5':
        raise ValueError('Registration is restricted to openai/gpt-5.5')
    result = copy.deepcopy(config)
    models = result.setdefault('models', {})
    models.setdefault('mode', 'merge')
    provider = models.setdefault('providers', {}).setdefault(VISION_PROVIDER, {})
    provider.setdefault('baseUrl', base_url)
    provider.setdefault('apiKey', api_key)
    provider.setdefault('api', 'openai-completions')
    entries = provider.setdefault('models', [])
    existing = [m for m in entries if m.get('id') == native_model['id']]
    if len(existing) > 1:
        raise ValueError('Duplicate GPT-5.5 entries in vision provider configuration')
    if existing:
        entry = existing[0]
    else:
        entry = copy.deepcopy(native_model)
        entries.append(entry)
    entry['input'] = list(dict.fromkeys([*entry.get('input', ['text']), 'image']))
    return result


def install_registration(config_path: Path, native_model: dict, base_url: str, api_key: str) -> None:
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    merged = merge_registration(config, native_model, base_url, api_key)
    if not config_path.exists():
        config_path.touch(mode=0o600)
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(merged, indent=2) + '\n')


def ensure_gpt55_registered(task_id: str, *, image_model: str, base_url: str, api_key: str) -> str:
    if image_model not in (MODEL, VISION_MODEL):
        return image_model
    if not api_key:
        raise ValueError('GPT-5.5 image registration requires an OpenRouter API key')
    from src.context_compression.integration import provider_model

    def docker(*args, **kwargs):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60, **kwargs)
        if result.returncode:
            # Do not expose arbitrary config/credential-bearing stderr.
            raise RuntimeError('GPT-5.5 registration failed during ' + args[0])
        return result.stdout

    probe = Path(__file__).resolve().parents[2] / 'context_compression/model_probe.mjs'
    docker('cp', str(probe), f'{task_id}:/tmp/wildclaw-model-probe.mjs')
    native = json.loads(docker('exec', task_id, 'node', '/tmp/wildclaw-model-probe.mjs',
                               'openrouter', 'openai/gpt-5.5').strip().splitlines()[-1])
    docker('cp', str(Path(__file__).resolve()), f'{task_id}:/tmp/wildclaw-model-registration.py')
    docker('exec', '-i', task_id, 'python3', '/tmp/wildclaw-model-registration.py', input=json.dumps({
        'model': provider_model(native), 'base_url': base_url, 'api_key': api_key,
    }))
    return VISION_MODEL


if __name__ == '__main__':
    payload = json.load(sys.stdin)
    install_registration(Path(CONFIG_PATH), payload['model'], payload['base_url'], payload['api_key'])
