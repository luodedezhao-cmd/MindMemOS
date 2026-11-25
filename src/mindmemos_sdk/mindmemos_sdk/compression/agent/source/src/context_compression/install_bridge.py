"""Version-checked, container-local adapter for the installed pi Agent loop.

This does not disable compaction, modify prompts, or patch the original repo.
Fail installation if the required call sites differ; never silently run without
the main-request marker. Host auxiliary calls bypass the Agent loop adapter.
"""

from pathlib import Path
import argparse


IMPORT = ('import { capture as wildclawCapture, prepare as wildclawPrepare, '
          'acknowledge as wildclawAcknowledge } from "./wildclaw-bridge.mjs";\n')
VERSION = '// wildclaw execution-provenance bridge v2\n'


def patched_loop(source):
    if IMPORT in source:
        if VERSION in source:
            return source
        raise ValueError('Older bridge already installed; recreate the task container')
    replacements = [
        ('stream.push({ type: "message_end", message: prompt });',
         'wildclawCapture(config, { type: "message_end", message: prompt }, "prompt");\n'
         '            stream.push({ type: "message_end", message: prompt });', 1),
        ('stream.push({ type: "message_end", message });',
         'wildclawCapture(config, { type: "message_end", message }, "steering");\n'
         '                stream.push({ type: "message_end", message });', 1),
        ('async function streamAssistantResponse(context, config, signal, stream, streamFn) {',
         'async function streamAssistantResponse(context, config, signal, stream, streamFn) {\n'
         '    config = wildclawPrepare(config, context);', 1),
        ('const finalMessage = await response.result();',
         'const finalMessage = await response.result();\n'
         '                wildclawAcknowledge(config, finalMessage);\n'
         '                wildclawCapture(config, { type: "message_end", message: finalMessage }, "model_response");', 1),
        ('executeToolCalls(currentContext.tools, message, signal, stream, config.getSteeringMessages)',
         'executeToolCalls(currentContext.tools, message, signal, stream, config.getSteeringMessages, config)', 1),
        ('async function executeToolCalls(tools, assistantMessage, signal, stream, getSteeringMessages) {',
         'async function executeToolCalls(tools, assistantMessage, signal, stream, getSteeringMessages, config) {', 1),
        ('skipToolCall(skipped, stream)', 'skipToolCall(skipped, stream, config)', 1),
        ('function skipToolCall(toolCall, stream) {', 'function skipToolCall(toolCall, stream, config) {', 1),
        ('stream.push({ type: "message_end", message: toolResultMessage });',
         'wildclawCapture(config, { type: "message_end", message: toolResultMessage }, "tool_result");\n'
         '    stream.push({ type: "message_end", message: toolResultMessage });', 2),
    ]
    for old, new, count in replacements:
        if source.count(old) != count:
            raise ValueError('Unsupported Agent loop version: bridge anchor mismatch')
        source = source.replace(old, new)
    return IMPORT + VERSION + source


def install(root):
    loop = root / 'node_modules/@mariozechner/pi-agent-core/dist/agent-loop.js'
    provider = root / 'node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js'
    source = loop.read_text()
    updated = patched_loop(source)
    provider_source = provider.read_text()
    export = ('\nexport function wildclawConvertMessages(model, context) {\n'
              '    return convertMessages(model, context, getCompat(model));\n}\n')
    if 'function getCompat(model)' not in provider_source or 'export function convertMessages(' not in provider_source:
        raise ValueError('Unsupported Chat Completions converter')
    # Complete all checks before writing. Files are in the isolated task container.
    loop.with_name('wildclaw-bridge.mjs').write_text(Path(__file__).with_name('openclaw_bridge.mjs').read_text())
    if 'export function wildclawConvertMessages(' not in provider_source:
        provider.write_text(provider_source + export)
    loop.write_text(updated)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openclaw-root', type=Path, default=Path('/usr/lib/node_modules/openclaw'))
    install(parser.parse_args().openclaw_root)
