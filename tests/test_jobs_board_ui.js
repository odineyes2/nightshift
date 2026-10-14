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

const calls = [];
const board = { innerHTML: '', handlers: {}, contains: () => false,
  querySelectorAll(selector){
    const cls = selector.slice(1);
    const matches = [...this.innerHTML.matchAll(/<[^>]*class="([^"]*)"[^>]*>/g)]
      .filter(match => match[1].split(/\s+/).includes(cls));
    this.handlers[cls] = [];
    return matches.map(match => {
      const tag = match[0];
      const dataset = {};
      for(const [, key, value] of tag.matchAll(/data-([\w-]+)="([^"]*)"/g))
        dataset[key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
      const element = { dataset, disabled: false, handlers: {},
        addEventListener(type, fn){ this.handlers[type] = fn; } };
      (this.handlers[cls] ||= []).push(element);
      return element;
    });
  },
};
const search = { value: '' }, filter = { value: 'all' }, showDone = { checked: true };
const ctx = vm.createContext({
  document: { getElementById(id){ return id === 'job-board' ? board : null; }, activeElement: null },
  window: { getSelection: () => ({ isCollapsed: true }) },
  lastJobs: [], lastJobsRowsSignature: null, currentTab: 'jobs', openJobDetailId: null,
  jobPodFilter: null, jobProjectFilter: null, jobSearch: search, jobStatusFilter: filter, jobShowDone: showDone,
  projectMatches: (id, scope) => String(id) === String(scope),
  projectName: id => `프로젝트 ${id}`, podName: id => id ? `워커 ${id}` : '',
  jobBatchModeLabel: (_, label) => label, jobBaseModelLabel: j => j.base || '',
  jobMediaKind: () => 'image', preflightCount: () => 0,
  STATUS_LABEL: { pending:'대기중', queued:'대기중', running:'실행중', done:'완료', failed:'실패', interrupted:'중단됨' },
  escapeHtml: text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  ico: () => '', fmtDateKey: iso => new Date(iso).toISOString().slice(0, 10), pad2: n => String(n).padStart(2, '0'),
  jobElapsedHtml: () => '', updateClearDoneJobs: () => {},
  openJobDetailModal: id => calls.push(['detail', id]), showTab: (...args) => calls.push(['tab', ...args]),
  fetch: async (url, options) => { calls.push(['fetch', url, options.method]); return { ok: true, json: async () => ({started:[], no_url:[], failed:[]}) }; },
  fetchJobs: () => {}, alert: () => {},
});
vm.runInContext(['jobMatchesSearch', 'jobFinishedLabel', 'buildJobCard', 'render'].map(pick).join('\n'), ctx);
const jobs = [
  { id:'1', status:'done', template_label:'인물 사진', base:'Flux', project_id:2, pod_id:'gpu', finished_at:'2026-10-13T10:20:00Z' },
  { id:'2', status:'failed', template_label:'풍경', finished_at:'2026-10-13T10:20:00Z' },
  { id:'3', status:'queued', template_label:'영상', missing_models:{ names:['model'], pod_id:'gpu', pod_name:'GPU' }, waiting_reason:'모델 없음' },
  { id:'4', status:'running', template_label:'애니메이션', progress:{ done:1, total:4 } },
  { id:'5', status:'pending', template_label:'초안' },
  { id:'6', status:'interrupted', template_label:'중단' },
  { id:'7', status:'queued', template_label:'설치', fetching_models:{ names:['model'], pod_id:'gpu' } },
];
const render = (...args) => vm.runInContext(`render(...renderArgs)`, Object.assign(ctx, { renderArgs: args }));
const click = async (cls, index = 0, target = { closest: () => null }) => {
  const row = board.handlers[cls][index];
  assert.ok(row, cls);
  await row.handlers.click({ stopPropagation(){}, preventDefault(){}, target });
};

test('날짜, 상태, 모델 조치와 상세/설정 진입점을 그린다', () => {
  render(jobs);
  assert.match(vm.runInContext('jobFinishedLabel(renderArgs[0][0])', ctx), /^2026-10-13 \d\d:\d\d$/);
  assert.equal(vm.runInContext('jobFinishedLabel(renderArgs[0][1])', ctx), '—');
  assert.equal(vm.runInContext('jobFinishedLabel({status:"done"})', ctx), '—');
  assert.match(board.innerHTML, /없는 모델 받기/);
  assert.match(board.innerHTML, /모델 받는 중/);
  assert.match(board.innerHTML, /job-card-models-link/);
  assert.match(source, /job-detail-load-btn[\s\S]*loadJobSettings\(job\)/);
  assert.match(source, /job-add-btn'\)\.addEventListener\('click', openNewJobModal\)/);
  assert.equal(board.handlers['job-card'].length, 7);
});

test('검색, 상태, 워커, 프로젝트 범위를 결합한다', () => {
  search.value = 'Flux'; filter.value = 'done'; render(jobs);
  assert.equal(board.handlers['job-card'].length, 1);
  search.value = ''; filter.value = 'all'; ctx.jobPodFilter = 'gpu'; ctx.jobProjectFilter = 2; render(jobs);
  assert.equal(board.handlers['job-card'].length, 1);
  ctx.jobPodFilter = null; ctx.jobProjectFilter = null; render(jobs);
});

test('행·모델 링크와 기존 제어 버튼이 각각 동작한다', async () => {
  await click('job-card', 0);
  assert.deepEqual(calls.at(-1), ['detail', '1']);
  await click('job-card', 2, { closest: selector => selector === '.job-card-models-link' ? { dataset:{podId:'gpu'} } : null });
  assert.equal(calls.at(-1)[0], 'tab');
  for(const [cls, method, suffix] of [['start-btn','POST','start'], ['pause-btn','POST','pause'], ['stop-btn','POST','stop'], ['del-btn','DELETE',''], ['fetch-models-btn','POST','fetch-missing']]){
    await click(cls);
    const request = calls.filter(c => c[0] === 'fetch').at(-1);
    assert.equal(request[2], method);
    assert.match(request[1], new RegExp(`/api/jobs/\\d+${suffix ? `/${suffix}` : ''}$`));
  }
});

test('같은 폴링 결과는 DOM을 유지한다', () => {
  render(jobs); const html = board.innerHTML;
  const firstRow = board.handlers['job-card'][0];
  render(jobs);
  assert.equal(board.innerHTML, html);
  assert.equal(board.handlers['job-card'][0], firstRow);
  ctx.document.activeElement = { focused:true }; board.contains = () => true;
  render([...jobs, { id:'8', status:'done', template_label:'신규' }]);
  assert.equal(board.innerHTML, html);
  ctx.document.activeElement = null; board.contains = () => false;
  render([...jobs, { id:'8', status:'done', template_label:'신규' }]);
  assert.equal(board.handlers['job-card'].length, 8);
});

test('"완료 작업도"는 기본 숨김, 체크 시 표시, 해제 시 다시 숨기며 상태 "완료" 선택은 존중한다', () => {
  showDone.checked = false; render(jobs);
  assert.equal(board.handlers['job-card'].length, 6);
  assert.doesNotMatch(board.innerHTML, /인물 사진/);
  showDone.checked = true; render(jobs);
  assert.equal(board.handlers['job-card'].length, 7);
  assert.match(board.innerHTML, /인물 사진/);
  showDone.checked = false; render(jobs);
  assert.equal(board.handlers['job-card'].length, 6);
  filter.value = 'done'; render(jobs);
  assert.equal(board.handlers['job-card'].length, 1);
  filter.value = 'all';
});
