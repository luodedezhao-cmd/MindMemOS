// Observation only: never edit context/options, consume events, or replace streams.
import { appendFileSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const directory = '/tmp/host_meter';
const rawUsages = new WeakMap(); // Per native stream, never a global "last usage".
function phase() {
  try { return readFileSync(directory + '/phase', 'utf8').trim() === 'scorer' ? 'scorer' : 'execution'; }
  catch { return 'execution'; }
}
function emit(value) {
  try { appendFileSync(directory + '/events.jsonl', JSON.stringify({schema_version: 2, time: Date.now() / 1000, ...value}) + '\n', {mode: 0o600}); }
  catch { /* Observation failures must never change model execution. Completeness audited outside. */ }
}
function number(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null; }
function token(value) { return Number.isSafeInteger(value) && value >= 0 ? value : null; }

export function recordRawUsage(stream, raw) {
  try {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    // Whitelist only numeric accounting fields. No text/metadata/keys in logs.
    const safe = {};
    for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens', 'cost']) {
      if (Object.hasOwn(raw, key)) safe[key] = key === 'cost' ? number(raw[key]) : token(raw[key]);
    }
    for (const [key, fields] of [['prompt_tokens_details', ['cached_tokens', 'cache_write_tokens']],
                               ['completion_tokens_details', ['reasoning_tokens']]]) {
      if (Object.hasOwn(raw, key)) {
        const detail = raw[key];
        safe[key] = detail && typeof detail === 'object' && !Array.isArray(detail) ?
          Object.fromEntries(fields.filter(f => Object.hasOwn(detail, f)).map(f => [f, token(detail[f])])) : null;
      }
    }
    // SSE usage snapshots are cumulative: keep the latest, never add snapshots.
    rawUsages.set(stream, safe);
  } catch { emit({event: 'meter_error'}); }
}

export function normalizedUsage(raw) {
  if (!raw) return null;
  return {
    input_tokens: token(raw.prompt_tokens),
    output_tokens: token(raw.completion_tokens), // Includes reasoning; do not add it.
    total_tokens: token(raw.total_tokens),
    reasoning_tokens: token(raw.completion_tokens_details?.reasoning_tokens),
    cache_read_tokens: token(raw.prompt_tokens_details?.cached_tokens),
    cache_write_tokens: token(raw.prompt_tokens_details?.cache_write_tokens),
  };
}

export function observe(invoke, model, context, options) {
  const started = performance.now();
  const id = randomUUID();
  const base = {call_id: id, phase: phase(), source: options?.sessionId === 'chat' ? 'agent' : 'host_auxiliary',
    provider: model.provider, model: model.id, api: model.api};
  emit({event: 'llm_started', ...base});
  let finished = false;
  let stream;
  const finish = (message, error = false) => {
    if (finished) return;
    finished = true;
    try {
      const raw = stream ? rawUsages.get(stream) : undefined;
      const usage = normalizedUsage(raw);
      // Only OpenRouter's raw cost has the verified account-charge semantics.
      const isOpenRouter = model.provider === 'openrouter' || (() => {
        try { return new URL(model.baseUrl).hostname === 'openrouter.ai'; } catch { return false; }
      })();
      const billed = isOpenRouter ? number(raw?.cost) : null;
      emit({event: 'llm_finished', ...base, elapsed_seconds: (performance.now() - started) / 1000,
        success: !error && !['error', 'aborted'].includes(message?.stopReason),
        stop_reason: ['stop', 'length', 'toolUse', 'error', 'aborted'].includes(message?.stopReason) ? message.stopReason : null,
        usage, raw_usage: raw ?? null, usage_source: raw ? 'raw_chat_completions' : 'unavailable',
        estimated_cost_usd: null, // Never reuse the SDK's corrupted cost estimate.
        cost_source: billed !== null ? 'openrouter_raw_usage_cost' : 'unknown',
        billed_cost_usd: billed});
      if (stream) rawUsages.delete(stream);
    } catch { emit({event: 'meter_error', call_id: id}); }
  };
  try {
    stream = invoke();
    // Observe the already shared result promise; do not iterate or clone the stream.
    stream.result().then(message => finish(message), () => finish(null, true));
    return stream;
  } catch (error) { finish(null, true); throw error; }
}

export function observeSession(event) {
  if (event.type === 'auto_compaction_start' || event.type === 'auto_compaction_end') {
    emit({event: event.type, phase: phase(),
      reason: ['threshold', 'overflow'].includes(event.reason) ? event.reason : null,
      success: event.type === 'auto_compaction_end' ? Boolean(event.result && !event.aborted) : null,
      aborted: event.aborted ?? null, will_retry: event.willRetry ?? null});
  }
}
