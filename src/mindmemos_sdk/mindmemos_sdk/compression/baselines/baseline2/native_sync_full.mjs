// OpenClaw 2026.3.11 / Pi 0.57.1: synchronous eligible-history full summaries, v2.
import fs from 'node:fs';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {setImmediate as nextTick} from 'node:timers/promises';
import {estimateTokens, generateSummary} from './compaction/compaction.js';
import {convertToLlm} from './messages.js';
import {convertMessages, getCompat} from '../../../pi-ai/dist/providers/wcb-sync-accounting-provider.mjs';
import {selectHistory} from './wcb-sync-full-accounting.mjs';

const installed = Symbol.for('wildclaw.nativeSyncFull.v2');
const contextWrapped = Symbol.for('wildclaw.nativeSyncFull.pinnedTask.v2');
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

// Estimate the imminent logical model input, including system prompt and tools.
// This is not the previous response's usage or an exact provider tokenizer.
export function estimateRequestTokens(context) {
  return context.messages.reduce((n, m) => n + estimateTokens(m), 0)
    + Math.ceil(Buffer.byteLength(context.systemPrompt ?? '', 'utf8') / 4)
    + Math.ceil(Buffer.byteLength(JSON.stringify(context.tools ?? []), 'utf8') / 4);
}

export function installNativeSyncFull(session) {
  if (session[installed]) return session[installed];
  const config = JSON.parse(fs.readFileSync(new URL('./wcb-sync-full-config.json', import.meta.url), 'utf8'));
  const threshold = config.threshold_tokens;
  if (!Number.isInteger(threshold) || threshold <= 0) throw new Error('Invalid sync-full threshold');
  const agent = session.agent;
  const auditDir = path.join('/tmp/openclaw/sync_full', randomUUID());
  fs.mkdirSync(auditDir, {recursive: true, mode: 0o700});
  let request = 0, loopMessages, streamDepth = 0;
  const write = data => fs.appendFileSync(path.join(auditDir, 'events.jsonl'),
    JSON.stringify({time: new Date().toISOString(), session_id: session.sessionId, request, ...data}) + '\n', {mode: 0o600});

  // Native firstKeptEntryId retains the raw suffix. Preserve TASK independently
  // of that suffix, including subsequent turns and file-backed session reloads.
  const manager = session.sessionManager;
  if (!manager[contextWrapped]) {
    const build = manager.buildSessionContext;
    manager.buildSessionContext = function(...args) {
      const context = build.apply(this, args);
      const latest = this.getBranch().findLast(e => e.type === 'compaction');
      if (latest?.details?.mode === 'sync_full_eligible_v2') {
        context.messages.unshift(structuredClone(latest.details.pinnedTask));
      }
      return context;
    };
    manager[contextWrapped] = true;
  }
  if (manager.getBranch().findLast(e => e.type === 'compaction')?.details?.mode === 'sync_full_eligible_v2') {
    agent.replaceMessages(manager.buildSessionContext().messages);
  }

  const transform = agent.transformContext;
  agent.transformContext = async function(messages, signal) {
    loopMessages = messages; // retain the loop's array, so old history cannot return
    return transform ? await transform.call(this, messages, signal) : messages;
  };
  // This variant has one scheduler: the pending-request gate below. In particular,
  // do not run the old usage-based check at prompt entry or after the final answer.
  session._checkCompaction = async () => {};

  async function beforeRequest(model, context, options) {
    request++;
    if (options.signal?.aborted) return;
    // The historical bridge captured execution before native context transforms.
    // Count the same raw representation, not static prefixes or provider usage.
    const selection = selectHistory(loopMessages, messages => convertMessages(model,
      {messages: convertToLlm(messages)}, getCompat(model)));
    const before = selection.estimatedTokens;
    write({event: 'request_check', estimated_input_tokens: estimateRequestTokens(context),
      trigger_estimated_tokens: before, raw_estimated_tokens: selection.rawEstimatedTokens,
      memory_estimated_tokens: selection.memoryEstimatedTokens, eligible_messages: selection.eligibleMessages,
      retained_native_messages: selection.retained.length, threshold_tokens: threshold,
      trigger_scope: 'ADyn canonical memory_messages(M) + eligible raw; excludes TASK/system/tools/recent group',
      input_sha256: hash(context), model_context_window: model.contextWindow,
      system_bytes: Buffer.byteLength(context.systemPrompt ?? '', 'utf8'),
      tool_schema_bytes: Buffer.byteLength(JSON.stringify(context.tools ?? []), 'utf8')});
    if (before < threshold || !session.settingsManager.getCompactionEnabled()) return;
    if (!selection.selected.length) {
      write({event: 'compaction_skipped', reason: 'no_eligible_history'});
      return;
    }
    const tip = loopMessages.at(-1);
    const deadline = Date.now() + 30000;
    while (agent.state.messages.at(-1) !== tip) {
      if (options.signal?.aborted) return;
      if (Date.now() > deadline) throw new Error('Message persistence barrier timed out');
      await nextTick();
    }
    await session._agentEventQueue;
    if (options.signal?.aborted) return;
    const branch = manager.getBranch();
    // Pi can remove failed responses from active state during retries while the
    // append-only log retains them. ADyn likewise never ingests those responses.
    const persistedTip = branch.findLast(e => e.type === 'message' && !(e.message.role === 'assistant'
      && ['error', 'aborted'].includes(e.message.stopReason)))?.message;
    if (persistedTip !== tip)
      throw new Error('Pending input does not match persisted history');
    const firstKept = branch.findLast(e => e.type === 'message' && e.message === selection.retained[0]);
    if (!firstKept) throw new Error('Retained group has no persisted entry');

    // TASK is reference for the native summary prompt, but remains verbatim in
    // the main context and is excluded from scheduling. Recent raw stays out.
    const selected = structuredClone([selection.task, ...(selection.memory ? [selection.memory] : []), ...selection.selected]);
    const started = performance.now();
    write({event: 'compaction_start', selected_messages: selected.length,
      selected_input_sha256: hash(selected), trigger_estimated_tokens: before,
      eligible_messages: selection.eligibleMessages, retained_native_messages: selection.retained.length,
      retained_sha256: hash(selection.retained), pinned_task_sha256: hash(selection.task),
      reserve_tokens: session.settingsManager.getCompactionReserveTokens()});
    let summary;
    try {
      const apiKey = await session.modelRegistry.getApiKey(model);
      if (!apiKey) throw new Error('Missing summarizer API key');
      summary = await generateSummary(selected, model,
        session.settingsManager.getCompactionReserveTokens(), apiKey, options.signal);
      if (options.signal?.aborted) {
        write({event: 'compaction_end', applied: false, aborted: true,
          seconds: (performance.now() - started) / 1000});
        return;
      }
      if (!summary?.trim()) throw new Error('Empty summary');
    } catch (error) {
      write({event: 'compaction_end', applied: false, aborted: Boolean(options.signal?.aborted),
        error_type: error?.constructor?.name ?? 'Error', seconds: (performance.now() - started) / 1000});
      return; // failed summary never deletes history
    }

    manager.appendCompaction(summary, firstKept.id, before,
      {mode: 'sync_full_eligible_v2', selectedMessages: selection.selected.length,
        retainedRawMessages: selection.retained.length, keepRecentGroups: 1,
        pinnedTask: structuredClone(selection.task)}, true);
    agent.replaceMessages(manager.buildSessionContext().messages);
    loopMessages.splice(0, loopMessages.length, ...agent.state.messages);
    const transformed = agent.transformContext ? await agent.transformContext(loopMessages, options.signal) : loopMessages;
    context.messages = await agent.convertToLlm(transformed);
    write({event: 'compaction_end', applied: true, aborted: false,
      seconds: (performance.now() - started) / 1000, summary_characters: summary.length,
      summary_sha256: hash(summary), estimated_input_tokens_after: estimateRequestTokens(context),
      retained_raw_messages: selection.retained.length,
      pinned_task_present: hash(agent.state.messages[0]) === hash(selection.task),
      retained_raw_present: hash(agent.state.messages.slice(2).filter(m => !(m.role === 'assistant'
        && ['error', 'aborted'].includes(m.stopReason)))) === hash(selection.retained),
      summary_present: JSON.stringify(context.messages).includes(JSON.stringify(summary).slice(1, -1)),
      input_sha256_after: hash(context)});
    // One summary attempt per request. No loop or rejection if still above threshold.
  }

  // OpenClaw replaces/wraps streamFn during setup. Follow those assignments and
  // gate only the outer invocation, after the native context transforms ran.
  function wrap(fn) {
    return async function(model, context, options = {}) {
      streamDepth++;
      try {
        if (streamDepth === 1) {
          try { await beforeRequest(model, context, options); }
          catch (error) {
            write({event: 'boundary_error', error_type: error?.constructor?.name ?? 'Error', reason: error?.message});
            agent.abort(); // let the native provider emit its normal aborted stream
          }
        }
        return await fn.call(this, model, context, options);
      } finally { streamDepth--; }
    };
  }
  let stream = wrap(agent.streamFn);
  Object.defineProperty(agent, 'streamFn', {configurable: true, enumerable: true,
    get: () => stream, set: fn => {stream = wrap(fn);}});
  const control = {auditDir, threshold, policy: 'eligible_history_v2'};
  session[installed] = control;
  write({event: 'installed', threshold_tokens: threshold, scheduler: 'pending_request_input',
    summary: 'unmodified native generateSummary prompt and implementation',
    trigger_accounting: 'ADyn canonical UTF8/4 of M + eligible raw', comparison: '>=',
    task_in_trigger: false, task_preserved: true, keep_recent_groups: 1,
    safeguard_loaded: false, original_usage_checks: 'disabled for this variant'});
  return control;
}
