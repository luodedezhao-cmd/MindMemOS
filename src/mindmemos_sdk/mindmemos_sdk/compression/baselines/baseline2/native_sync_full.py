"""Install a synchronous full-summary gate on the pending model request.

The audited Pi AgentSession constructor is patched inside a disposable task
container, and native compaction mode is set to "default" so safeguard is not
loaded. OpenClaw bundles and the native compactor stay byte-identical. The trigger
uses ADyn eligible-history accounting, not static prefixes or previous usage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/usr/lib/node_modules/openclaw')
CORE = Path('node_modules/@mariozechner/pi-coding-agent/dist/core')
SESSION = CORE / 'agent-session.js'
SESSION_SHA256 = 'b19e5a75f7ee6166b5a93602e745390561358a6e7f5f703bd8a3a67dea89cd90'
PROVIDER = Path('node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js')
PROVIDER_SHA256 = '281dd5259bfe2862bb4bf18004b495577cc30fe2b0666f84f78c89cadb618bf1'
HELPER = 'wcb-sync-full.mjs'
IMPORT = f'import {{installNativeSyncFull}} from "./{HELPER}";\n'
ANCHOR = '''        this._buildRuntime({
            activeToolNames: this._initialActiveToolNames,
            includeAllExtensionTools: true,
        });'''
REPLACEMENT = ANCHOR + '\n        installNativeSyncFull(this);'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_native_sync_full(helper_source: bytes, accounting_source: bytes, threshold_tokens: int, root: Path = ROOT) -> dict:
    if type(threshold_tokens) is not int or threshold_tokens <= 0:
        raise ValueError('Sync-full threshold must be a positive integer')
    versions = (
        json.loads((root / 'package.json').read_text())['version'],
        json.loads((root / CORE.parent.parent / 'package.json').read_text())['version'],
    )
    if versions != ('2026.3.11', '0.57.1'):
        raise ValueError('Sync-full compaction requires OpenClaw 2026.3.11 / Pi 0.57.1')
    target = root / SESSION
    current = target.read_bytes()
    source = current.decode()
    already = source.startswith(IMPORT)
    if already:
        source = source[len(IMPORT):].replace(REPLACEMENT, ANCHOR, 1)
    if digest(source.encode()) != SESSION_SHA256 or source.count(ANCHOR) != 1:
        raise ValueError('AgentSession differs from the audited source or this constructor patch')
    patched = (IMPORT + source.replace(ANCHOR, REPLACEMENT, 1)).encode()
    if already and current != patched:
        raise ValueError('Installed sync-full compaction patch differs from this version')
    helper = target.with_name(HELPER)
    if helper.exists() and helper.read_bytes() != helper_source:
        raise ValueError('Existing sync-full compaction helper differs from this version')
    accounting = target.with_name('wcb-sync-full-accounting.mjs')
    provider = root / PROVIDER
    provider_bytes = provider.read_bytes()
    if digest(provider_bytes) != PROVIDER_SHA256:
        raise ValueError('Provider converter differs from the audited source')
    # A sidecar exposes the exact same private compatibility resolver without
    # modifying the actual provider used for main or compression requests.
    converter = provider.with_name('wcb-sync-accounting-provider.mjs')
    converter_bytes = provider_bytes + b'\nexport {getCompat};\n'
    for p, content in ((accounting, accounting_source), (converter, converter_bytes)):
        if p.exists() and p.read_bytes() != content:
            raise ValueError('Existing accounting sidecar differs from this version')

    # Validate both modules before any writes; parsing does not execute imports.
    for code in (helper_source, accounting_source, converter_bytes, patched):
        subprocess.run(['node', '--input-type=module', '--check'], input=code,
                       capture_output=True, check=True, timeout=30)
    unchanged = [*sorted((root / 'dist').rglob('*.js')),
                 root / CORE / 'compaction/compaction.js', provider,
                 root / 'node_modules/@mariozechner/pi-agent-core/dist/agent-loop.js']
    before = {str(p.relative_to(root)): digest(p.read_bytes()) for p in unchanged}
    config = Path('/root/.openclaw/openclaw.json')
    config_bytes = config.read_bytes()
    config_before = digest(config_bytes)
    expected_config = json.loads(config_bytes)
    compaction = expected_config.setdefault('agents', {}).setdefault('defaults', {}).setdefault('compaction', {})
    mode_present = 'mode' in compaction
    previous_mode = compaction.get('mode')
    compaction['mode'] = 'default'
    gate_config = target.with_name('wcb-sync-full-config.json')
    gate_config.write_text(json.dumps({'threshold_tokens': threshold_tokens, 'policy': 'eligible_history_v2'}) + '\n')
    accounting.write_bytes(accounting_source)
    converter.write_bytes(converter_bytes)
    helper.write_bytes(helper_source)
    target.write_bytes(patched)
    # Use OpenClaw's supported mode selection; do not patch safeguard gates.
    # Preserve all other settings and avoid rewriting an already-correct config.
    if json.loads(config_bytes) != expected_config:
        config.write_text(json.dumps(expected_config, indent=2) + '\n')
    if target.read_bytes() != patched or helper.read_bytes() != helper_source:
        raise RuntimeError('Sync-full compaction installation verification failed')
    if before != {str(p.relative_to(root)): digest(p.read_bytes()) for p in unchanged}:
        raise RuntimeError('A native compactor, agent loop or OpenClaw bundle changed during installation')
    if json.loads(config.read_bytes()) != expected_config:
        raise RuntimeError('OpenClaw config differs from the intended mode-only change')
    return {
        'kind': 'openclaw_sync_full_eligible_history_v2',
        'openclaw_version': versions[0], 'pi_version': versions[1],
        'file': str(SESSION), 'original_sha256': SESSION_SHA256,
        'patched_sha256': digest(patched), 'helper_sha256': digest(helper_source),
        'accounting_sha256': digest(accounting_source), 'converter_sidecar_sha256': digest(converter_bytes),
        'already_applied': already, 'unchanged_source_sha256': before,
        'openclaw_config_sha256_before': config_before,
        'openclaw_config_sha256_after': digest(config.read_bytes()),
        'compaction_mode_config': {
            'path': 'agents.defaults.compaction.mode',
            'previous_field_present': mode_present, 'previous_value': previous_mode,
            'value': 'default', 'all_other_fields_unchanged': True,
        },
        'scheduler': 'synchronous gate on pending request; original usage checks disabled',
        'trigger_accounting': 'ADyn canonical JSON UTF8/4 of memory_messages(M) + eligible raw',
        'trigger_comparison': '>=', 'keep_recent_groups': 1, 'task_in_trigger': False,
        'threshold_tokens': threshold_tokens, 'safeguard': 'not loaded (native default mode)',
        'summary_generation': 'unmodified native generateSummary', 'model_and_output_budget': 'unchanged',
        'selection': 'M + eligible history; TASK reference preserved verbatim; recent complete group retained',
        'no_post_answer_compaction': True,
        'post_compaction_hard_cap_added': False,
        'audit_container_path': '/tmp/openclaw/sync_full',
    }


def install_native_sync_full(task_id: str, threshold_tokens: int, output_dir: Path) -> dict:
    helper = Path(__file__).with_suffix('.mjs').read_bytes()
    accounting = Path(__file__).with_name('native_sync_full_accounting.mjs').read_bytes()
    result = subprocess.run(
        ['docker', 'exec', '-i', task_id, 'python3', '-', helper.hex(), accounting.hex(), str(threshold_tokens)],
        input=Path(__file__).read_text(), capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError('Sync-full compaction installation failed: ' + result.stderr[-2000:])
    report = json.loads(result.stdout)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'native_sync_full_patch.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 4:
        raise SystemExit('Usage: native_sync_full.py HELPER_SOURCE_HEX ACCOUNTING_HEX POSITIVE_THRESHOLD')
    print(json.dumps(patch_native_sync_full(bytes.fromhex(sys.argv[1]), bytes.fromhex(sys.argv[2]), int(sys.argv[3]))))
