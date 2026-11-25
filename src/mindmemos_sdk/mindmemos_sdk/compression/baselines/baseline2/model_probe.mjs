// Disposable-container probe of the PINNED native resolver, without a model call.
import {readFileSync, writeFileSync, readdirSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const root = '/usr/lib/node_modules/openclaw';
const dist = root + '/dist';
const candidates = readdirSync(dist).filter(n => /^pi-embedded-.*\.js$/.test(n));
const file = candidates.find(n => readFileSync(dist + '/' + n, 'utf8').includes('function resolveModel(provider, modelId, agentDir, cfg)'));
if (!file) throw new Error('Unsupported native resolver');
const source = readFileSync(dist + '/' + file, 'utf8');
const exported = dist + '/wildclaw-model-probe.mjs';
writeFileSync(exported, source + '\nexport {resolveModel as wildclawResolveModel};\n');
const {wildclawResolveModel: resolve} = await import(pathToFileURL(exported));
const providerFile = root + '/node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js';
const providerSource = readFileSync(providerFile, 'utf8');
const compatFile = providerFile.replace(/\.js$/, '-wildclaw-probe.mjs');
writeFileSync(compatFile, providerSource + '\nexport {getCompat as wildclawCompat};\n');
const {wildclawCompat} = await import(pathToFileURL(compatFile));
let cfg = {};
try {cfg = JSON.parse(readFileSync(process.env.OPENCLAW_CONFIG_PATH || '/root/.openclaw/openclaw.json', 'utf8'));} catch {}
const provider = process.argv[2] || 'openrouter';
const id = process.argv[3] || 'openai/gpt-5.5';
const result = resolve(provider, id, undefined, cfg);
if (!result.model) throw new Error('Native model resolution failed');
const model = result.model;
if (model.api !== 'openai-completions') throw new Error('Unsupported native API');
// Never export provider headers, credentials or arbitrary runtime configuration.
const safe = Object.fromEntries(['id','name','api','provider','baseUrl','reasoning','input','contextWindow','maxTokens','cost'].map(k => [k,model[k]]));
safe.compat = wildclawCompat(model);
console.log(JSON.stringify(safe));
