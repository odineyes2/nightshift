// CSV 작업 상세의 LoRA 표시와 늦은 응답 방어 — node tests/test_job_detail_lora_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static/js/01-core-jobs.js'), 'utf8');
const section = source.slice(source.indexOf('let openJobDetailId = null;'), source.indexOf('function openNewJobModal(){'));
const escape = source.slice(source.indexOf('function escapeHtml(str){'), source.indexOf('// Lucide 아이콘'));
assert.ok(section.includes('async function loadJobDetailLora'));
const modal = { style: {} };
const ctx = vm.createContext({
  document: { getElementById: () => modal },
  lastJobs: [], jobDetailOutputs: null,
  renderJobDetailModalBody: () => {}, renderJobDetailOutputs: () => {},
  loadLog: () => {}, loadJobDetailOutputs: () => {}, fetch: () => {},
});
vm.runInContext(`${escape}\n${section}`, ctx);
const run = (code) => vm.runInContext(code, ctx);
const entries = (workflow, options = {}) => {
  ctx.workflow = workflow; ctx.options = options;
  return run('jobLoraEntries(workflow, options)');
};
const loader = (name, strength, clip) => ({ class_type: 'LoraLoader', inputs: {
  lora_name: name, strength_model: strength, strength_clip: clip,
} });

assert.equal(entries({}).length, 0);
assert.equal(entries({ 1: loader('one.safetensors', 0.8, 0.6) })[0].name, 'one.safetensors');
assert.equal(entries({ 1: loader('one', 0.8, 0.6) })[0].clip, 0.6);
const multi = {
  1: loader('old', 1, 0.7),
  2: { ...loader('second', 0.5, 0.3), _meta: { title: 'Portrait LoRA' } },
  3: { class_type: 'Sampler', inputs: { model: ['2', 0] } },
};
let result = entries(multi, { lora_name: 'new', lora_strength: '0' });
assert.equal(result.length, 2);
assert.equal(result[0].name, 'old');
assert.equal(result[1].name, 'new');
assert.equal(result[1].model, 0);
assert.equal(result[1].clip, 0.3);
result = entries(multi, { lora_node_title: 'portrait', lora_name: 'named' });
assert.equal(result[1].name, 'named');
result = entries(multi, { lora_node_title: 'missing', lora_name: 'connected' });
assert.equal(result[1].name, 'connected');
result = entries({ 1: loader('first', 1, 1), 2: loader('second', 1, 1) }, { lora_name: 'fallback' });
assert.equal(result[0].name, 'fallback');
result = entries({ 1: loader(['4', 0], ['5', 0], 0.4),
  4: { class_type: 'PrimitiveString', inputs: { value: 'linked.safetensors' } },
  5: { class_type: 'PrimitiveFloat', inputs: { value: 0.2 } } });
assert.equal(result[0].name, 'linked.safetensors');
assert.equal(result[0].model, 0.2);
assert.throws(() => entries(null));
assert.throws(() => entries({ 1: loader(['missing', 0], 1, 1) }));

run("jobDetailLora = { status: 'ready', entries: [{ name: '<img src=x>', model: 0, clip: '<b>' }] }");
let html = run('jobDetailLoraHtml()');
assert.match(html, /LoRA 적용 여부/);
assert.match(html, /LoRA 이름/);
assert.match(html, /LoRA 강도/);
assert.match(html, /&lt;img src=x&gt;/);
assert.match(html, /0 \(CLIP &lt;b&gt;\)/);
assert.doesNotMatch(html, /<img/);
run("jobDetailLora = { status: 'ready', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /적용 안 함/);
run("jobDetailLora = { status: 'error', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /확인할 수 없어요/);

// 실제 상세 필드 렌더에 LoRA 항목이 들어가고, 일반 작업에는 표시되지 않는다.
const body = source.slice(source.indexOf('function renderJobDetailModalBody(){'),
  source.indexOf('// 이 작업이 만든 생성물'));
const elements = new Map();
ctx.document.getElementById = id => {
  if(!elements.has(id)) elements.set(id, { style: {}, addEventListener: () => {} });
  return elements.get(id);
};
Object.assign(ctx, {
  estimateJob: () => ({}), podName: () => '', preflightLines: () => [],
  fmtDuration: () => '-', projectsById: {}, podsCache: [], STATUS_LABEL: {},
});
vm.runInContext(source.slice(source.indexOf('function pad2(n){'), source.indexOf('// 갤러리 "날짜별 보기"')), ctx);
vm.runInContext(body, ctx);
ctx.lastJobs = [{ id: 'a', template_id: 'csv_batch', status: 'pending', options: {} }];
run("openJobDetailId = 'a'; jobDetailLora = { status: 'ready', entries: [{ name: 'visible', model: 0, clip: null }] }; renderJobDetailModalBody()");
assert.match(elements.get('job-detail-fields').innerHTML, /LoRA 이름/);
assert.match(elements.get('job-detail-fields').innerHTML, /visible/);
assert.match(elements.get('job-detail-fields').innerHTML, /LoRA 강도/);
ctx.lastJobs[0].template_id = 'seed_batch';
run('renderJobDetailModalBody()');
assert.doesNotMatch(elements.get('job-detail-fields').innerHTML, /LoRA 적용 여부/);

ctx.lastJobs[0].options = {
  main_prompt: 'separate prompt',
  '<img src=x>': 'long_model_name_'.repeat(12) + '&<b>',
  seed_count: 5,
  empty_value: '',
};
ctx.lastJobs[0].model_events = [
  { at: '2026-10-14T12:34:56Z', text: '<img src=x onerror=alert(1)> & done' },
  { at: null, text: '시각 없음' },
];
run('renderJobDetailModalBody()');
html = elements.get('job-detail-fields').innerHTML;
assert.match(html, /<span class="time-hm">\d\d:\d\d<\/span><span class="time-s">:\d\d<\/span>/);
assert.match(html, /<span class="job-detail-event-time">—<\/span>/);
assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt; &amp; done/);
assert.doesNotMatch(html, /<img src=x/);
assert.match(html, /<div class="job-detail-prompt">separate prompt<\/div>/);
assert.equal((html.match(/class="job-detail-option"/g) || []).length, 3);
assert.match(html, /class="job-detail-option-key">&lt;img src=x&gt;/);
assert.match(html, /long_model_name_.*&amp;&lt;b&gt;/);
assert.match(html, /class="job-detail-option-value">5<\/span>/);
assert.match(html, /class="job-detail-option-value">비어 있음<\/span>/);
assert.ok(html.indexOf('&lt;img src=x&gt;') < html.indexOf('seed_count'));
ctx.lastJobs[0].options = { main_prompt: 'only prompt' };
run('renderJobDetailModalBody()');
assert.match(elements.get('job-detail-fields').innerHTML, /옵션 없음/);

async function main(){
  const pending = [];
  ctx.fetch = url => new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
  ctx.lastJobs = [
    { id: 'a', template_id: 'csv_batch', workflow_filename: 'a.json', options: {} },
    { id: 'b', template_id: 'csv_batch', workflow_filename: 'b.json', options: {} },
  ];
  run("openJobDetailModal('a')");
  run("openJobDetailModal('b')");
  assert.equal(pending.length, 2);
  pending[1].resolve({ ok: true, json: async () => ({ 1: loader('current', 1, 1) }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('jobDetailLora.entries[0].name'), 'current');
  pending[0].resolve({ ok: true, json: async () => ({ 1: loader('stale', 1, 1) }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('jobDetailLora.entries[0].name'), 'current');
  run('closeJobDetailModal()');
  run("openJobDetailModal('a')");
  run('closeJobDetailModal()');
  pending[2].resolve({ ok: true, json: async () => ({ 1: loader('closed', 1, 1) }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('openJobDetailId'), null);
  assert.equal(run('jobDetailLora.status'), 'loading');
  run("openJobDetailModal('a')");
  pending[3].resolve({ ok: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('jobDetailLora.status'), 'error');
  console.log('ok');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
