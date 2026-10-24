// Jobs 화면의 family→모델 선택지와 작업 범위 필터를 검사한다.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/01-core-jobs.js'), 'utf8');
function pick(name){
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  const body = source.indexOf('{', start);
  let depth = 0, quote = '', escaped = false;
  for(let i = body; i < source.length; i++){
    const c = source[i];
    if(quote){
      if(escaped) escaped = false;
      else if(c === '\\') escaped = true;
      else if(c === quote) quote = '';
    }else if(c === "'" || c === '"' || c === '`') quote = c;
    else if(c === '{') depth++;
    else if(c === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(name);
}

const family = { value: '', innerHTML: '' }, model = { value: '', innerHTML: '', disabled: true };
const board = { innerHTML: '', contains: () => false, querySelectorAll: () => [] };
const entries = {
  'checkpoints:flux-a.safetensors': { base_models: ['Flux'] },
  'checkpoints:shared.safetensors': { base_models: ['Flux', 'SDXL'] },
  'diffusion_models:unet.safetensors': { base_models: ['SDXL'] },
};
const cache = { upload: 'unet.safetensors' };
const ctx = vm.createContext({
  jobFamilyFilter: family, jobModelFilter: model, jobWorkflowCheckpointCache: cache,
  fetchJobWorkflowCheckpoint: () => {}, modelEntry: (kind, name) => entries[`${kind}:${name}`] || { base_models: [] },
  basesOf: entry => entry.base_models || [], escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
  jobPodFilter: null, jobProjectFilter: null, projectMatches: (id, scope) => id === scope,
  jobSearch: { value: '' }, jobStatusFilter: { value: 'all' }, jobShowDone: { checked: true },
  updateClearDoneJobs: () => {}, currentTab: 'jobs', openJobDetailId: null,
  document: { getElementById: () => board, activeElement: null }, window: { getSelection: () => ({ isCollapsed: true }) },
  preflightCount: () => 0, podName: () => '', projectName: () => '',
  buildJobCard: j => `<article data-id="${j.id}"></article>`, jobMatchesSearch: () => true,
  lastJobsRowsSignature: null, lastJobs: [],
});
vm.runInContext(['jobModelInfo', 'updateJobModelFilters', 'render'].map(pick).join('\n'), ctx);
const jobs = [
  { id: 'a', status: 'queued', pod_id: 'one', options: { checkpoint: 'flux-a.safetensors' } },
  { id: 'b', status: 'queued', pod_id: 'one', options: { checkpoint: 'shared.safetensors' } },
  { id: 'upload', status: 'queued', pod_id: 'one', workflow_filename: 'upload.json' },
  { id: 'wan', status: 'queued', pod_id: 'two', template_id: 'wan22_video_batch' },
  { id: 'unknown', status: 'queued', pod_id: 'two' },
];
const render = items => vm.runInContext('render(items)', Object.assign(ctx, { items }));
const ids = () => [...board.innerHTML.matchAll(/data-id="([^"]+)"/g)].map(match => match[1]);

test('등록부와 워크플로우 캐시에서 모델 및 여러 family를 읽는다', () => {
  assert.deepEqual(Array.from(vm.runInContext('jobModelInfo(items[1]).families', Object.assign(ctx, { items: jobs }))), ['Flux', 'SDXL']);
  assert.deepEqual(Array.from(vm.runInContext('jobModelInfo(items[2]).families', ctx)), ['SDXL']);
  assert.match(family.innerHTML || (render(jobs), family.innerHTML), /미분류/);
  assert.match(family.innerHTML, /Wan 2.2/);
});

test('family 선택 후 해당 파일만 보여주고 선택지가 사라지면 초기화한다', () => {
  family.value = 'Flux'; render(jobs);
  assert.deepEqual(ids(), ['a', 'b']);
  assert.match(model.innerHTML, /flux-a.safetensors/);
  assert.doesNotMatch(model.innerHTML, /unet.safetensors/);
  model.value = 'shared.safetensors'; render(jobs);
  assert.deepEqual(ids(), ['b']);
  family.value = 'SDXL'; model.value = ''; render(jobs);
  assert.deepEqual(ids(), ['b', 'upload']);
  assert.match(model.innerHTML, /unet.safetensors/);
  family.value = 'Flux'; model.value = 'flux-a.safetensors';
  ctx.jobPodFilter = 'two'; render(jobs);
  assert.equal(family.value, '');
  assert.equal(model.value, '');
  assert.equal(model.disabled, true);
  assert.deepEqual(ids(), ['wan', 'unknown']);
});

test('미분류와 파일 없는 고정 family를 선택할 수 있다', () => {
  ctx.jobPodFilter = null;
  family.value = '__uncategorized__'; render(jobs);
  assert.deepEqual(ids(), ['unknown']);
  assert.equal(model.disabled, true);
  family.value = 'Wan 2.2'; render(jobs);
  assert.deepEqual(ids(), ['wan']);
  assert.equal(model.disabled, true);
  assert.match(source, /jobFamilyFilter\.addEventListener\('change', \(\) => \{\s*jobModelFilter\.value = '';/);
});
