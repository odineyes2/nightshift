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
assert.match(html, /LORA \(총 1개\)/);
assert.equal((html.match(/class="field"/g) || []).length, 1);
assert.doesNotMatch(html, /LoRA 적용 여부|LoRA 이름|LoRA 강도|저장된 워크플로우|실제 GPU/);
assert.match(html, /&lt;img src=x&gt;/);
assert.match(html, /강도 0 · CLIP &lt;b&gt;/);
assert.doesNotMatch(html, /<img/);
run("jobDetailLora = { status: 'ready', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /적용 안 함/);
run("jobDetailLora = { status: 'error', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /확인할 수 없어요/);

// 목록별 개수와 이름·강도 대응, CLIP 생략, 행 번호 이스케이프.
run("jobDetailLora = { status: 'loading', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /field-label">LORA<.*확인 중…/);
run("jobDetailLora = { status: 'ready', entries: [] }");
assert.match(run('jobDetailLoraHtml()'), /LORA \(총 0개\).*적용 안 함/);
ctx.listEntries = [
  { name: 'long_name_'.repeat(30) + '.safetensors', model: 0, clip: null },
  { name: 'second&file', model: '0.8<b>', clip: 0.7 },
];
run("jobDetailLora = { status: 'ready', entries: listEntries, rows: [{ line: '<2>', entries: [] }] }");
html = run('jobDetailLoraHtml()');
assert.match(html, /LORA · 공통 \(총 2개\)/);
assert.match(html, /1\. long_name_.*\.safetensors · 강도 0<br>2\. second&amp;file · 강도 0\.8&lt;b&gt; · CLIP 0\.7/);
assert.equal((html.match(/ · CLIP /g) || []).length, 1);
assert.match(html, /&lt;2&gt;행 LORA \(총 0개\).*적용 안 함/);
assert.doesNotMatch(html, /<2>|<b>|저장된 워크플로우|실제 GPU/);

// 실제 상세 필드 렌더에 LoRA 항목이 들어가고, 일반 작업에는 표시되지 않는다.
const body = source.slice(source.indexOf('function renderJobDetailModalBody(){'),
  source.indexOf('// 이 작업이 만든 생성물'));
const elements = new Map();
ctx.document.getElementById = id => {
  if(!elements.has(id)) elements.set(id, { style: {}, listeners: {}, addEventListener(event, fn){ this.listeners[event] = fn; } });
  return elements.get(id);
};
Object.assign(ctx, {
  estimateJob: () => ({}), podName: () => '', preflightLines: () => [],
  fmtDuration: () => '-', projectsById: {}, podsCache: [], STATUS_LABEL: {},
});
vm.runInContext(source.slice(source.indexOf('function pad2(n){'), source.indexOf('// 갤러리 "날짜별 보기"')), ctx);
vm.runInContext(body, ctx);
ctx.lastJobs = [{ id: 'a', template_id: 'csv_batch', status: 'pending', workflow_filename: 'a.json', options: {} }];
run("openJobDetailId = 'a'; jobDetailLora = { status: 'ready', entries: [{ name: 'visible', model: 0, clip: null }] }; renderJobDetailModalBody()");
assert.match(elements.get('job-detail-fields').innerHTML, /LORA \(총 1개\)/);
assert.match(elements.get('job-detail-fields').innerHTML, /visible/);
assert.match(elements.get('job-detail-fields').innerHTML, /강도 0/);
ctx.lastJobs[0].template_id = 'seed_batch';
for(const status of ['pending', 'queued', 'done']){
  ctx.lastJobs[0].status = status;
  run('renderJobDetailModalBody()');
  assert.match(elements.get('job-detail-fields').innerHTML, /LORA \(총 1개\)/);
}
run('renderJobDetailModalBody()');
assert.match(elements.get('job-detail-fields').innerHTML, /LORA/);

ctx.lastJobs[0].options = {
  main_prompt: 'separate prompt',
  '<img src=x>': 'long_model_name_'.repeat(12) + '&<b>',
  seed_count: 5,
  seed_mode: 'fixed',
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
assert.equal((html.match(/class="job-detail-option"/g) || []).length, 2);
assert.match(html, /class="job-detail-option-key">&lt;img src=x&gt;/);
assert.match(html, /long_model_name_.*&amp;&lt;b&gt;/);
assert.match(html, /<label class="field-label">seed_count<\/label><div>5<\/div>/);
assert.match(html, /<label class="field-label">seed_mode<\/label><div>fixed<\/div>/);
assert.match(html, /class="job-detail-option-value">비어 있음<\/span>/);
assert.doesNotMatch(html, /class="job-detail-option-key">seed_count/);
assert.match(html, /<details class="field job-detail-options-details" id="job-detail-options" >/);
assert.match(html, /이미지 크기 \(width × height\).*확인할 수 없어요/);
ctx.lastJobs[0].options = { main_prompt: 'only prompt' };
run('renderJobDetailModalBody()');
assert.match(elements.get('job-detail-fields').innerHTML, /옵션 없음/);
ctx.lastJobs[0].options = { main_prompt: '', width: 512, height: 768 };
run('renderJobDetailModalBody()');
html = elements.get('job-detail-fields').innerHTML;
assert.match(html, /프롬프트가 비어 있어요/);
assert.match(html, /512 × 768/);
assert.match(html, /id="job-detail-prompt-copy-btn"[^>]*disabled/);
ctx.lastJobs[0].prompt_overrides = [{ seed: 7, ar: [2, 3], res: 1024 }, { seed: 8 }];
run('renderJobDetailModalBody()');
html = elements.get('job-detail-fields').innerHTML;
assert.match(html, /832 × 1216/);
assert.match(html, /seed 7 · 외 1줄/);
assert.doesNotMatch(html, /프롬프트 값 사용 중/);
elements.get('job-detail-options').open = true;
run('renderJobDetailModalBody()');
assert.match(elements.get('job-detail-fields').innerHTML, /id="job-detail-options" open/);
run('jobDetailOptionsJobId = null; renderJobDetailModalBody()');
assert.doesNotMatch(elements.get('job-detail-fields').innerHTML, /id="job-detail-options" open/);

result = entries(multi, { lora_name: 'single', lora_strength: '0.9', loras: 'folder:one:0;two:0.4' });
assert.equal(result[0].name, 'folder:one');
assert.equal(result[0].model, 0);
assert.equal(result[1].name, 'two');
assert.equal(result[1].clip, 0.3);
result = entries(multi, { loras: [['array', 0]] });
assert.equal(result[0].name, 'array');
assert.equal(result[0].model, 0);
assert.equal(result[1].name, 'second');
assert.throws(() => entries(multi, { loras: 'bad' }));
assert.throws(() => entries(multi, { loras: 'a:1;b:1;c:1' }));
assert.throws(() => entries({ 1: loader(['2', 0], 1, 1), 2: { class_type: 'Unsupported', inputs: { value: 'unknown' } } }));
assert.throws(() => entries({ 1: loader('good', 1, 1), 2: loader(['missing', 0], 1, 1) }));
assert.equal(entries({ 1: { class_type: 'LoraLoaderModelOnly', inputs: { lora_name: 'model', strength_model: 0 } } })[0].clip, null);

async function main(){
  const copied = [];
  ctx.navigator = { clipboard: { writeText: async text => copied.push(text) } };
  ctx.lastJobs[0].options.main_prompt = 'copy this';
  run('renderJobDetailModalBody()');
  await elements.get('job-detail-prompt-copy-btn').listeners.click();
  assert.deepEqual(copied, ['copy this']);
  assert.equal(elements.get('job-detail-prompt-copy-status').textContent, '복사했어요');
  ctx.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  await elements.get('job-detail-prompt-copy-btn').listeners.click();
  assert.match(elements.get('job-detail-prompt-copy-status').textContent, /복사하지 못했어요/);
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
  ctx.lastJobs[0].template_id = 'seed_batch';
  ctx.lastJobs[0].options = { lora_name: 'single', lora_strength: '0', loras: 'common:0.2' };
  ctx.lastJobs[0].prompt_overrides = [{ line: 2, loras: [['<row>', 0]] }, { line: 5, loras: 'last:0.8' }, {}];
  run("openJobDetailModal('a')");
  pending[4].resolve({ ok: true, json: async () => ({ 1: loader('saved', 1, 0.6) }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('jobDetailLora.entries[0].name'), 'common');
  assert.equal(run('jobDetailLora.rows.length'), 2);
  html = run('jobDetailLoraHtml()');
  assert.match(html, /2행 LORA \(총 1개\)/);
  assert.match(html, /5행 LORA \(총 1개\)/);
  assert.match(html, /1\. &lt;row&gt; · 강도 0 · CLIP 0\.6/);
  assert.match(html, /LORA · 공통 \(총 1개\)/);
  assert.match(html, /1\. common · 강도 0\.2 · CLIP 0\.6/);
  assert.match(html, /1\. last · 강도 0\.8 · CLIP 0\.6/);
  assert.doesNotMatch(html, /공통 설정|저장된 워크플로우|실제 GPU/);
  ctx.lastJobs[0].options = { lora_name: 'seed override', lora_strength: '0' };
  ctx.lastJobs[0].prompt_overrides = [];
  run("openJobDetailModal('a')");
  pending[5].resolve({ ok: true, json: async () => ({ 1: loader('saved', 1, 0.6) }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run('jobDetailLora.entries[0].name'), 'seed override');
  assert.equal(run('jobDetailLora.entries[0].model'), 0);
  for(const workflow of [{}, null]){
    run("openJobDetailModal('a')");
    pending.at(-1).resolve({ ok: true, json: async () => workflow });
    await new Promise(resolve => setImmediate(resolve));
    assert.match(run('jobDetailLoraHtml()'), workflow ? /적용 안 함/ : /확인할 수 없어요/);
  }
  const count = pending.length;
  for(const template of ['shell_command', 'claude_write']){
    ctx.lastJobs[0].template_id = template;
    run("openJobDetailModal('a')");
  }
  delete ctx.lastJobs[0].workflow_filename;
  ctx.lastJobs[0].template_id = 'seed_batch';
  run("openJobDetailModal('a')");
  assert.equal(pending.length, count);
  assert.doesNotMatch(elements.get('job-detail-fields').innerHTML, /LORA/);
  console.log('ok');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
