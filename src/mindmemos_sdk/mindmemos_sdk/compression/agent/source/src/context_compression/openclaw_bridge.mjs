// Installed only in this project's task container. Host compaction is untouched.
import { readFileSync } from 'node:fs';
import { wildclawConvertMessages } from '../../pi-ai/dist/providers/openai-completions.js';

const path = process.env.WILDCLAW_CONTEXT_RUNTIME;
const runtime = path ? JSON.parse(readFileSync(path, 'utf8')) : null;
const states = new Map();

function stateFor(config) {
  if (!runtime || config.model.provider !== 'context-compression' ||
      config.sessionId !== runtime.session_id) return null;
  let state = states.get(config.sessionId);
  if (!state) {
    state = { native: [], pending: [], start: 0, end: 0 };
    states.set(config.sessionId, state);
  }
  return state;
}

export function capture(config, event, source) {
  const state = stateFor(config);
  if (!state || event.type !== 'message_end') return;
  // Only explicit execution-production sites may ingest. A public message_end
  // (including a host-generated assistant summary) is not execution provenance.
  const roles = { prompt: ['user'], steering: ['user'],
    model_response: ['assistant'], tool_result: ['toolResult'] };
  if (!Object.hasOwn(roles, source)) return;
  const message = event.message;
  if (!roles[source].includes(message.role)) {
    throw new Error('Unsupported execution origin/role; refusing silent loss');
  }
  if (message.role === 'assistant' && ['error', 'aborted'].includes(message.stopReason)) return;
  // Capture before host context normalization; never inspect old context here.
  state.native.push(structuredClone(message));
}

export function prepare(config, context) {
  const state = stateFor(config);
  if (!state) return config;
  if (state.native.some(m => !['user', 'assistant', 'toolResult'].includes(m.role))) {
    throw new Error('Unsupported new execution message; refusing silent loss');
  }
  const delta = wildclawConvertMessages(config.model, { messages: state.native });
  state.native = [];
  state.pending.push(...delta);
  state.end += delta.length;
  const metadata = {
    version: 1, instance_id: runtime.instance_id, session_id: config.sessionId,
    start: state.start, end: state.end, messages: structuredClone(state.pending),
    prefix: wildclawConvertMessages(config.model, { messages: [], systemPrompt: context.systemPrompt }),
  };
  const original = config.onPayload;
  return { ...config, onPayload: async (payload, model) => {
    const result = await original?.(payload, model) ?? payload;
    return { ...result, _wildclaw_context: metadata };
  } };
}

export function acknowledge(config, message) {
  const state = stateFor(config);
  if (!state || ['error', 'aborted'].includes(message.stopReason)) return;
  // On transport failure the same delta is resent; Python ingestion is idempotent.
  state.pending = [];
  state.start = state.end;
}
