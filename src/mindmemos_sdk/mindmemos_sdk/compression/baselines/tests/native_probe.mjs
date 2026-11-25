import fs from 'node:fs';
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
const root = '/usr/lib/node_modules/openclaw';
const core = root + '/node_modules/@mariozechner/pi-coding-agent/dist/core';
const {createAgentSession} = await import(core + '/sdk.js');
const {SessionManager} = await import(core + '/session-manager.js');
const {SettingsManager} = await import(core + '/settings-manager.js');
const accounting = await import(core + '/wcb-sync-full-accounting.mjs');
const {convertMessages, getCompat} = await import(root + '/node_modules/@mariozechner/pi-ai/dist/providers/wcb-sync-accounting-provider.mjs');
const ai = await import(root + '/node_modules/@mariozechner/pi-ai/dist/index.js');
const nativeBundle = fs.readFileSync(root + '/dist/pi-embedded-D6PpOsxP.js', 'utf8');
fs.writeFileSync(root + '/dist/wcb-sync-probe.mjs', nativeBundle +
  '\nexport {installToolResultContextGuard, buildEmbeddedExtensionFactories, loadConfig};\n');
const {installToolResultContextGuard, buildEmbeddedExtensionFactories, loadConfig} = await import(root + '/dist/wcb-sync-probe.mjs');
const cfg = loadConfig();
assert.equal(cfg.agents.defaults.compaction.mode, 'default');
const model = {id: 'offline-fixture', name: 'Offline fixture', api: 'wcb-offline', provider: 'wcb-offline',
  baseUrl: 'http://offline.invalid', contextWindow: 200000, maxTokens: 32000, reasoning: false, input: ['text', 'image'],
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}};
const message = (content, stopReason = 'stop', input = 100) => ({role: 'assistant', content,
  api: model.api, provider: model.provider, model: model.id, stopReason, timestamp: Date.now(),
  usage: {input, output: 1, totalTokens: input + 1, cacheRead: 0, cacheWrite: 0,
    cost: {input: 0, output: 0, total: 0, cacheRead: 0, cacheWrite: 0}}});
function response(msg) {
  const stream = new ai.AssistantMessageEventStream();
  stream.push({type: 'done', reason: msg.stopReason, message: msg}); stream.end(msg); return stream;
}
const rows = [];
for (const scenario of ['full', 'post_summary_above_threshold', 'large_system_tools', 'large_task',
  'latest_group_only', 'high_old_usage', 'disabled', 'failure', 'empty', 'abort', 'retry_log_tail', 'exact_threshold']) {
  let calls = 0, summaryCalls = 0, inFlight = false, aborted = 0;
  const inputs = [], requests = [], executed = [];
  const manager = scenario === 'full' ? SessionManager.create('/tmp', '/tmp/wcb-sync-reload') : SessionManager.inMemory('/tmp');
  const settings = SettingsManager.inMemory({compaction: {enabled: scenario !== 'disabled', reserveTokens: 20000, keepRecentTokens: 20000}, retry: {enabled: false}});
  assert.equal(buildEmbeddedExtensionFactories({cfg, sessionManager: manager, model, provider: model.provider, modelId: model.id}).length, 0);
  const taskText = 'CURRENT_TASK_MARKER: perform the fixture task.' + (scenario === 'large_task' ? '巨大任务'.repeat(20000) : '');
  if (scenario === 'exact_threshold') {
    manager.appendMessage({role: 'user', content: [{type: 'text', text: taskText}], timestamp: Date.now()});
    const seed = message([{type: 'text', text: 'x'}]);
    const size = () => accounting.estimatedTokens(convertMessages(model, {messages: [seed]}, getCompat(model)));
    seed.content[0].text = 'x'.repeat(65536 - Buffer.byteLength(accounting.canonical(convertMessages(model, {messages: [seed]}, getCompat(model)))) + 1);
    assert.equal(size(), 16384);
    manager.appendMessage(seed);
  }
  const registry = {getApiKey: async () => 'offline-key', getApiKeyForProvider: async () => 'offline-key'};
  const {session} = await createAgentSession({cwd: '/tmp', agentDir: '/tmp/wcb-sync-empty-agent', model,
    tools: [], sessionManager: manager, settingsManager: settings, modelRegistry: registry,
    customTools: [{name: 'fixture', label: 'fixture', description: scenario === 'large_system_tools' ? 'schema'.repeat(20000) : 'fixture',
      parameters: {type: 'object', properties: {}}, execute: async id => {
        executed.push(id);
        return {content: [{type: 'text', text: 'RAW_' + id + ' ' + 'tool result '.repeat(
          ['large_system_tools', 'large_task', 'high_old_usage'].includes(scenario) ? 2 : 5000)}], details: {}};
      }}]});
  const control = session[Symbol.for('wildclaw.nativeSyncFull.v2')];
  assert.equal(control.threshold, 16384);
  installToolResultContextGuard({agent: session.agent, contextWindowTokens: 200000});
  session._extensionRunner = {hasHandlers: () => false,
    emitBeforeAgentStart: async () => scenario === 'large_system_tools' ? {systemPrompt: 'system '.repeat(15000)} : undefined,
    emit: async event => {
      if (event.type === 'message_end') await delay(5);
      if (scenario === 'retry_log_tail' && event.type === 'message_end' && event.message.role === 'toolResult') {
        manager.appendMessage({...message([], 'error'), errorMessage: '503 fixture error retained only in session log'});
      }
    }};
  function summarize(_model, context) {
    assert.equal(executed.length % 2, 0, 'entire parallel group completed');
    summaryCalls++; inFlight = true;
    const input = JSON.stringify(context); inputs.push(input);
    assert(input.includes('Create a structured context checkpoint summary'));
    assert(input.includes('CURRENT_TASK_MARKER'), 'TASK supplied as reference');
    if (scenario !== 'exact_threshold') {
      const latest = executed.at(-1);
      assert(!input.includes('RAW_' + latest), 'latest observation excluded from summary');
      assert(!input.includes('RAW_' + executed.at(-2)), 'all parallel results excluded');
      assert(!input.includes(latest), 'latest assistant tool calls excluded too');
    }
    const stream = new ai.AssistantMessageEventStream();
    (async () => {
      await delay(10);
      let msg;
      if (scenario === 'abort') {session.agent.abort(); msg = message([], 'aborted');}
      else if (scenario === 'failure') msg = {...message([], 'error'), errorMessage: 'OFFLINE_SUMMARY_FAILURE'};
      else if (scenario === 'empty') msg = message([]);
      else msg = message([{type: 'text', text: `SUMMARY_${summaryCalls}_MARKER ` +
        (scenario === 'post_summary_above_threshold' ? 'summary '.repeat(12000) : 'selected historical work')}]);
      inFlight = false; stream.push({type: 'done', reason: msg.stopReason, message: msg}); stream.end(msg);
    })();
    return stream;
  }
  ai.registerApiProvider({api: model.api, stream: summarize, streamSimple: summarize});
  session.agent.streamFn = async (_model, context, options) => {
    assert(!inFlight, 'main waits for synchronous summary');
    if (options.signal?.aborted) {aborted++; return response(message([], 'aborted'));}
    calls++; requests.push(JSON.parse(JSON.stringify(context)));
    assert(JSON.stringify(context.messages[0].content).includes(taskText), 'original TASK stays first and verbatim');
    const toolSteps = scenario === 'latest_group_only' ? 1 : scenario === 'exact_threshold' ? 0 : 4;
    if (calls <= toolSteps) return response(message([
      {type: 'toolCall', id: `${scenario}-${calls}-a`, name: 'fixture', arguments: {}},
      {type: 'toolCall', id: `${scenario}-${calls}-b`, name: 'fixture', arguments: {}}
    ], 'toolUse', scenario === 'high_old_usage' ? 100000 : 100));
    return response(message([{type: 'text', text: 'finished'}], 'stop', 100000));
  };
  const previous = session.agent.streamFn;
  session.agent.streamFn = (m, c, o) => previous(m, c, o);
  await session.prompt(scenario === 'exact_threshold' ? 'LATEST_STEERING_MARKER' : taskText);
  await session._agentEventQueue;
  const readEvents = () => fs.readFileSync(control.auditDir + '/events.jsonl', 'utf8').trim().split('\n').map(JSON.parse);
  let events = readEvents();
  assert(!events.some(e => e.event === 'boundary_error'), JSON.stringify(events));
  const applied = manager.getBranch().filter(e => e.type === 'compaction');
  const shouldApply = ['full', 'post_summary_above_threshold', 'retry_log_tail', 'exact_threshold'].includes(scenario);
  if (shouldApply) {
    assert.equal(summaryCalls, scenario === 'exact_threshold' ? 1 : 3);
    assert.equal(applied.length, summaryCalls);
    assert.equal(events.filter(e => e.event === 'compaction_end' && e.applied).length, summaryCalls);
    assert(events.filter(e => e.event === 'compaction_end' && e.applied).every(e => e.pinned_task_present && e.retained_raw_present));
    if (scenario === 'exact_threshold') assert.equal(events[0 + 1].trigger_estimated_tokens, 16384);
    else {
      assert.equal(requests[1].messages.filter(m => m.role === 'toolResult').length, 2);
      for (let i = 2; i < requests.length; i++) {
        const input = JSON.stringify(requests[i].messages);
        assert(input.includes(`SUMMARY_${i - 1}_MARKER`));
        assert(input.includes(`RAW_${scenario}-${i}-a`) && input.includes(`RAW_${scenario}-${i}-b`));
        assert(!input.includes(`RAW_${scenario}-${i - 1}-a`), 'covered raw never returns');
        assert.equal(requests[i].messages.filter(m => m.role === 'toolResult').length, 2);
      }
      assert(inputs[1].includes('SUMMARY_1_MARKER'));
    }
    if (scenario === 'post_summary_above_threshold') assert(events.find(e => e.event === 'compaction_end').estimated_input_tokens_after > 16384);
  } else {
    assert.equal(applied.length, 0);
    if (['failure', 'empty'].includes(scenario)) assert.equal(summaryCalls, 3);
    else if (scenario === 'abort') {assert.equal(summaryCalls, 1); assert.equal(calls, 2); assert.equal(aborted, 1);}
    else assert.equal(summaryCalls, 0);
    if (scenario === 'large_system_tools' || scenario === 'large_task') assert(events.some(e => e.event === 'request_check' && e.estimated_input_tokens > 16384 && e.trigger_estimated_tokens < 16384));
  }
  // No post-answer extra compaction. Current state and native rebuild agree.
  assert.equal(events.filter(e => e.event === 'request_check').length, calls + aborted);
  if (shouldApply) assert.deepEqual(session.agent.state.messages, manager.buildSessionContext().messages);
  let reloadVerified = false;
  if (scenario === 'full') {
    const path = manager.getSessionFile();
    session.dispose();
    const restored = SessionManager.open(path);
    const {session: resumed} = await createAgentSession({cwd: '/tmp', agentDir: '/tmp/wcb-sync-empty-agent', model,
      tools: [], sessionManager: restored, settingsManager: settings, modelRegistry: registry});
    assert(JSON.stringify(resumed.agent.state.messages[0].content).includes(taskText));
    assert(!JSON.stringify(resumed.agent.state.messages).includes('RAW_full-1-a'));
    const beforeTurns = restored.getBranch().filter(e => e.type === 'compaction').length;
    let resumedSummaries = 0;
    const resumeSummary = (_m, ctx) => {
      const serialized = JSON.stringify(ctx);
      assert(serialized.includes('CURRENT_TASK_MARKER'));
      assert(serialized.includes('RAW_full-4-a'), 'previously retained tools now eligible');
      assert(!serialized.includes('NEW_TURN_MARKER'), 'new user group is retained, not summarized');
      resumedSummaries++;
      return response(message([{type: 'text', text: 'RESUMED_SUMMARY_MARKER'}]));
    };
    ai.registerApiProvider({api: model.api, stream: resumeSummary, streamSimple: resumeSummary});
    resumed.agent.streamFn = (_m, ctx) => {
      assert(JSON.stringify(ctx.messages[0].content).includes(taskText));
      assert(JSON.stringify(ctx.messages).includes('NEW_TURN_MARKER'));
      assert(JSON.stringify(ctx.messages).includes('RESUMED_SUMMARY_MARKER'));
      assert(!JSON.stringify(ctx.messages).includes('RAW_full-4-a'));
      return response(message([{type: 'text', text: 'second turn finished'}]));
    };
    await resumed.prompt('NEW_TURN_MARKER');
    assert.equal(resumedSummaries, 1);
    assert.equal(restored.getBranch().filter(e => e.type === 'compaction').length, beforeTurns + 1);
    resumed.dispose(); reloadVerified = true;
  } else session.dispose();
  rows.push({scenario, main_requests: calls, summaries: applied.length, summary_attempts: summaryCalls,
    reload_and_next_turn_verified: reloadVerified});
}
console.log(JSON.stringify({status: 'passed', paid_model_calls: 0, policy: 'eligible_history_v2',
  threshold: 16384, task_counted: false, task_preserved: true, keep_recent_groups: 1, rows}));
