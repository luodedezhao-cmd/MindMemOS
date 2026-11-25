"""Install an observation-only hook in a disposable container's native pi-ai."""
import argparse
import hashlib
import json
from pathlib import Path

MARKER = '// host-observation-v2\n'


def already_patched(source):
    if '// host-observation-' in source and MARKER not in source:
        raise ValueError('Older observation hook installed; recreate the task container')
    return MARKER in source


def patched_stream(source):
    if already_patched(source):
        return source
    for name in ('stream', 'streamSimple'):
        old = f'return provider.{name}(model, context, options);'
        if source.count(old) != 1:
            raise ValueError('Unsupported native pi-ai stream version')
        source = source.replace(old, f'return observe(() => provider.{name}(model, context, options), model, context, options);')
    return MARKER + 'import { observe } from "./host-meter.mjs";\n' + source


def patched_session(source):
    if already_patched(source):
        return source
    old = '    _emit(event) {\n'
    if source.count(old) != 1:
        raise ValueError('Unsupported native AgentSession version')
    return (MARKER + 'import { observeSession } from "../../../pi-ai/dist/host-meter.mjs";\n' +
            source.replace(old, old + '        observeSession(event);\n'))


def patched_provider(source):
    if already_patched(source):
        return source
    old = '                if (chunk.usage) {\n'
    if source.count(old) != 1:
        raise ValueError('Unsupported native Chat Completions usage anchor')
    # Capture before normalization; do not replace or edit chunk/output usage.
    return (MARKER + 'import { recordRawUsage } from "../host-meter.mjs";\n' +
            source.replace(old, old + '                    recordRawUsage(stream, chunk.usage);\n'))


def install(root=Path('/usr/lib/node_modules/openclaw'), log_dir=Path('/tmp/host_meter')):
    stream = root / 'node_modules/@mariozechner/pi-ai/dist/stream.js'
    session = root / 'node_modules/@mariozechner/pi-coding-agent/dist/core/agent-session.js'
    provider = root / 'node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js'
    original_stream, original_session = stream.read_text(), session.read_text()
    original_provider = provider.read_text()
    new_stream, new_session = patched_stream(original_stream), patched_session(original_session)
    new_provider = patched_provider(original_provider)
    log_dir.mkdir(parents=True, exist_ok=True)
    record = log_dir / 'installation.json'
    if record.exists() and json.loads(record.read_text()).get('version') != 2:
        raise ValueError('Old observation logs present; recreate the task container')
    if not record.exists():
        record.write_text(json.dumps({'version': 2, 'original_stream_sha256': hashlib.sha256(original_stream.encode()).hexdigest(),
                                     'original_session_sha256': hashlib.sha256(original_session.encode()).hexdigest(),
                                     'original_provider_sha256': hashlib.sha256(original_provider.encode()).hexdigest(),
                                     'native_compaction_config_changed': False, 'context_reconstruction': False}))
    stream.with_name('host-meter.mjs').write_text(Path(__file__).with_name('meter.mjs').read_text())
    stream.write_text(new_stream)
    session.write_text(new_session)
    provider.write_text(new_provider)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openclaw-root', type=Path, default=Path('/usr/lib/node_modules/openclaw'))
    install(parser.parse_args().openclaw_root)
