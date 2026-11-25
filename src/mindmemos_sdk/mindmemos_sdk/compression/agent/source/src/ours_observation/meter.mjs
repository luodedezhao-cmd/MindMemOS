// Ours-only native observer. The correlation header goes only to loopback;
// the proxy strips it. Host's observer and historical records are unchanged.
import {appendFileSync,readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
export {recordRawUsage, observeSession} from './host-meter-base.mjs';
import {observe as nativeObserve} from './host-meter-base.mjs';
const dir = '/tmp/host_meter';
function emit(e) {
  try {appendFileSync(dir + '/routing.jsonl', JSON.stringify({schema_version:3,...e})+'\n',{mode:0o600});} catch {}
}
export function observe(invoke, model, context, options) {
  if (model.provider !== 'context-compression') return nativeObserve(() => invoke(options), model, context, options);
  const call_id = randomUUID().replaceAll('-','');
  let phase = 'execution';
  try {if(readFileSync(dir+'/phase','utf8').trim()==='scorer') phase='scorer';} catch {}
  const base={call_id,phase,source:options?.sessionId==='chat'?'agent':'host_auxiliary',model:model.id};
  const start=performance.now();
  emit({event:'native_started',...base});
  const observed={...options,headers:{...options?.headers,'X-Wildclaw-Observation-Id':call_id}};
  try {
    const stream=invoke(observed);
    stream.result().then(m=>emit({event:'native_finished',...base,elapsed_seconds:(performance.now()-start)/1000,
      success:!['error','aborted'].includes(m?.stopReason),stop_reason:m?.stopReason}),
      ()=>emit({event:'native_finished',...base,success:false,elapsed_seconds:(performance.now()-start)/1000}));
    return stream;
  } catch(e) {emit({event:'native_finished',...base,success:false,elapsed_seconds:(performance.now()-start)/1000});throw e;}
}
