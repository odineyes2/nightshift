// Jobs 화면의 날짜·프로젝트 필터, 청소 범위, 완료 썸네일, 상세 창 유지를 실제 스크립트로 검사한다.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/01-core-jobs.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');

// 정오 고정 시각 — 시험 기계의 시간대와 무관하게 앞뒤 날짜가 같다.
const local = (y, m, d) => new Date(y, m - 1, d, 12).toISOString();
const YESTERDAY = local(2026, 11, 6), TODAY = local(2026, 11, 7);

function element(){
  return { value: '', innerHTML: '', textContent: '', hidden: false, checked: false, disabled: false, listeners: {},
    addEventListener(type, fn){ this.listeners[type] = fn; }, setAttribute(){}, contains: () => false,
    querySelectorAll: () => [] };
}

function harness({ now = [2026, 11, 7, 12], confirmResult = true } = {}){
  const elements = {};
  const get = id => (elements[id] ||= element());
  const calls = { confirm: [], fetch: [], closed: 0, detail: 0, logs: [] };
  const clock = { now };
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args){
      if(args.length) super(...args);
      else{ const [y, m, d, h] = clock.now; super(y, m - 1, d, h); }
    }
  }
  const projects = { 1: '나중', 2: '가나다' };
  const ctx = vm.createContext({
    Date: FakeDate,
    document: { getElementById: get, querySelectorAll: () => [], activeElement: null },
    window: { getSelection: () => ({ isCollapsed: true }), __nightshiftMediaUrl: url => url + '#media' },
    setInterval: () => 0, URLSearchParams, console,
    confirm: text => { calls.confirm.push(text); return confirmResult; },
    fetch: async (url, options) => {
      calls.fetch.push({ url, options });
      return { ok: true, json: async () => ({ cleared: 1, jobs: [] }) };
    },
    nsfwMode: 'show',
    projectName: id => projects[id] || '',
    projectMatches: (id, filter) => filter === null || (filter === 'unassigned' ? id == null : id === filter),
    modelEntry: () => ({}), basesOf: () => [], basesText: () => '',
    openNewJobModal(){}, preflightCount: () => 0, preflightLines: () => [],
  });
  vm.runInContext(source, ctx);
  // 상세 창은 이 시험의 대상이 아니라 호출만 센다.
  Object.assign(ctx, { renderJobDetailModalBody(){ calls.detail++; }, loadLog(){}, loadJobDetailOutputs(){},
    closeJobDetailModal(){ calls.closed++; vm.runInContext('openJobDetailId = null', ctx); } });
  ctx.setScope = code => vm.runInContext(code, ctx);
  const render = jobs => { ctx.items = jobs; vm.runInContext('render(items)', ctx); };
  const board = get('job-board');
  const ids = () => [...board.innerHTML.matchAll(/class="job-card" data-id="([^"]+)"/g)].map(m => m[1]);
  return { ctx, calls, clock, get, render, ids, board, date: get('job-date-filter'), select: get('job-project-select'),
    label: get('job-project-label'), showDone: get('job-show-done') };
}

function ready(options){
  const h = harness(options);
  h.get('job-status-filter').value = 'all';
  h.showDone.checked = true;
  return h;
}

const done = (id, at, extra = {}) => ({ id, status: 'done', queued_at: at, started_at: at, finished_at: at, ...extra });

test('툴바에 날짜와 프로젝트가 완료 작업도와 상태 사이에 있다', () => {
  const toolbar = html.slice(html.indexOf('class="job-list-toolbar"'), html.indexOf('id="job-board"'));
  const order = ['id="job-show-done"', 'id="job-date-filter"', 'id="job-project-label"', 'id="job-project-select"', 'id="job-status-filter"'];
  const places = order.map(text => toolbar.indexOf(text));
  assert.ok(places.every(place => place >= 0));
  assert.deepEqual(places, [...places].sort((a, b) => a - b));
  assert.match(toolbar, /<option value="">모든 프로젝트<\/option>/);
});

test('날짜 기본값은 오늘이고 값을 지우면 모든 날짜를 본다', () => {
  const h = ready();
  assert.equal(h.date.value, '2026-11-07');
  const jobs = [done('old', YESTERDAY), done('new', TODAY)];
  h.render(jobs);
  assert.deepEqual(h.ids(), ['new']);
  h.date.value = '';
  h.date.listeners.change();
  assert.deepEqual(h.ids(), ['old', 'new']);
});

test('사람이 바꾸지 않았으면 자정 뒤 오늘을 따라가고, 바꿨으면 그대로 둔다', () => {
  const h = ready();
  const jobs = [done('a', TODAY), done('b', local(2026, 11, 8))];
  h.render(jobs);
  assert.deepEqual(h.ids(), ['a']);
  h.clock.now = [2026, 11, 8, 0];
  h.render(jobs);
  assert.equal(h.date.value, '2026-11-08');
  assert.deepEqual(h.ids(), ['b']);

  const fixed = ready();
  fixed.date.value = '2026-11-06';
  fixed.date.listeners.change();
  fixed.clock.now = [2026, 11, 8, 0];
  fixed.render(jobs);
  assert.equal(fixed.date.value, '2026-11-06');

  const cleared = ready();
  cleared.date.value = '';
  cleared.date.listeners.change();
  cleared.clock.now = [2026, 11, 8, 0];
  cleared.render(jobs);
  assert.equal(cleared.date.value, '');

  const back = ready();
  back.date.value = '2026-11-06';
  back.date.listeners.change();
  back.date.value = '2026-11-07';
  back.date.listeners.change();
  back.clock.now = [2026, 11, 8, 0];
  back.render(jobs);
  assert.equal(back.date.value, '2026-11-08');
});

test('어제 넣은 대기·실행 잡은 오늘에 보이고 지난 날짜에는 보이지 않는다', () => {
  const h = ready();
  const jobs = [
    { id: 'p', status: 'pending', queued_at: YESTERDAY }, { id: 'q', status: 'queued', queued_at: YESTERDAY },
    { id: 'r', status: 'running', queued_at: YESTERDAY, started_at: YESTERDAY },
    { id: 'f', status: 'failed', queued_at: YESTERDAY, started_at: YESTERDAY },
    { id: 'n', status: 'interrupted', queued_at: YESTERDAY, started_at: TODAY },
    { id: 'w', status: 'failed', queued_at: TODAY },
  ];
  h.render(jobs);
  assert.deepEqual(h.ids(), ['p', 'q', 'r', 'n', 'w']);
  h.date.value = '2026-11-06';
  h.date.listeners.change();
  assert.deepEqual(h.ids(), ['f']);
});

test('프로젝트 선택지는 이름순이고 고른 값과 미분류로 거르며, 잡이 사라지면 풀린다', () => {
  const h = ready();
  const jobs = [done('a', TODAY, { project_id: 1 }), done('b', TODAY, { project_id: 2 }),
    done('c', TODAY), done('d', TODAY, { project_id: 9 }), done('e', TODAY, { project_id: 2 })];
  h.render(jobs);
  assert.deepEqual([...h.select.innerHTML.matchAll(/<option value="([^"]*)">([^<]*)/g)].map(m => [m[1], m[2]]), [
    ['', '모든 프로젝트'], ['unassigned', '미분류'], ['2', '가나다'], ['1', '나중'], ['9', '프로젝트 9'],
  ]);
  h.select.value = '2';
  h.select.listeners.change();
  assert.deepEqual(h.ids(), ['b', 'e']);
  h.select.value = 'unassigned';
  h.select.listeners.change();
  assert.deepEqual(h.ids(), ['c']);
  h.select.value = '1';
  h.select.listeners.change();
  assert.deepEqual(h.ids(), ['a']);
  h.render(jobs.filter(j => j.id !== 'a'));
  assert.equal(h.select.value, '');
  assert.equal(h.ids().length, 4);
});

test('드롭다운 내용이 그대로면 innerHTML을 다시 쓰지 않는다', () => {
  const h = ready();
  const jobs = [done('a', TODAY, { project_id: 1 })];
  h.render(jobs);
  let writes = 0, content = h.select.innerHTML;
  Object.defineProperty(h.select, 'innerHTML', { get: () => content, set: value => { writes++; content = value; } });
  h.render([...jobs]);
  assert.equal(writes, 0);
});

test('프로젝트 화면에서는 라벨과 선택을 숨기고 고른 값을 무시한다', () => {
  const h = ready();
  const jobs = [done('a', TODAY, { project_id: 1 }), done('b', TODAY, { project_id: 2 })];
  h.render(jobs);
  h.select.value = '2';
  h.ctx.setScope('jobProjectFilter = 1');
  h.render(jobs);
  assert.deepEqual(h.ids(), ['a']);
  assert.equal(h.select.hidden, true);
  assert.equal(h.label.hidden, true);
  assert.equal(h.select.value, '2');
  h.ctx.setScope('jobProjectFilter = null');
  h.render(jobs);
  assert.deepEqual(h.ids(), ['b']);
  assert.equal(h.select.hidden, false);
  assert.equal(h.label.hidden, false);
});

test('청소는 보이는 날짜·프로젝트 범위의 id만 고정해 보내고 버튼 숫자도 같다', async () => {
  const h = ready();
  const jobs = [done('a', TODAY, { project_id: 1 }), done('b', TODAY, { project_id: 2 }),
    done('c', YESTERDAY, { project_id: 1 }), { id: 'd', status: 'failed', queued_at: TODAY, project_id: 1 }];
  h.render(jobs);
  const button = h.get('clear-done-jobs');
  assert.match(button.innerHTML, /\(2\)/);
  h.select.value = '1';
  h.select.listeners.change();
  assert.match(button.innerHTML, /\(1\)/);
  await button.listeners.click();
  assert.match(h.calls.confirm[0], /프로젝트 나중 \/ 2026\.11\.07 \(토\) 범위의 완료 잡 1개/);
  const request = h.calls.fetch.find(call => call.url.includes('clear-completed'));
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(request.options.body), { job_ids: ['a'] });
  const query = new URLSearchParams(request.url.split('?')[1]);
  assert.equal(query.get('done_only'), 'true');
  assert.equal(query.get('project_id'), '1');
});

test('날짜를 지우면 모든 날짜를 지우고, 확인을 취소하면 요청하지 않는다', async () => {
  const cancel = ready({ confirmResult: false });
  const jobs = [done('a', TODAY), done('c', YESTERDAY)];
  cancel.render(jobs);
  await cancel.get('clear-done-jobs').listeners.click();
  assert.equal(cancel.calls.fetch.length, 0);

  const h = ready();
  h.date.value = '';
  h.date.listeners.change();
  h.render(jobs);
  await h.get('clear-done-jobs').listeners.click();
  assert.match(h.calls.confirm[0], /^전체 범위의 완료 잡 2개/);
  const request = h.calls.fetch.find(call => call.url.includes('clear-completed'));
  assert.deepEqual(JSON.parse(request.options.body), { job_ids: ['a', 'c'] });
});

test('썸네일은 이미지·영상·블러·안 보기 규칙을 따르고 모든 행에 칸을 둔다', () => {
  const h = ready();
  const jobs = [
    done('i', TODAY, { cover: { path: 'a b/그림.png', kind: 'image', nsfw: false } }),
    done('v', TODAY, { cover: { path: 'v.mp4', kind: 'video', nsfw: false } }),
    done('n', TODAY, { cover: { path: 'n.png', kind: 'image', nsfw: true } }),
    done('e', TODAY, { cover: null }),
    { id: 'r', status: 'running', queued_at: TODAY, cover: null },
  ];
  h.render(jobs);
  assert.equal(h.board.innerHTML.match(/class="job-thumb"/g).length, 5);
  assert.match(h.board.innerHTML, /<img src="\/api\/output-images\/a%20b%2F%EA%B7%B8%EB%A6%BC\.png\/thumbnail\?size=120&fit=cover#media" width="48" height="48" alt="" loading="lazy">/);
  assert.match(h.board.innerHTML, /job-thumb-tile"><svg class="ico"><use href="#i-clapperboard"/);
  assert.match(h.board.innerHTML, /n\.png/);

  h.ctx.nsfwMode = 'blur';
  h.render(jobs);
  assert.doesNotMatch(h.board.innerHTML, /n\.png/);
  assert.match(h.board.innerHTML, /job-thumb-tile"><svg class="ico"><use href="#i-eye-off"/);

  h.ctx.nsfwMode = 'hide';
  h.render(jobs);
  assert.doesNotMatch(h.board.innerHTML, /n\.png/);
  assert.doesNotMatch(h.board.innerHTML, /i-eye-off/);
  assert.equal(h.board.innerHTML.match(/class="job-thumb"/g).length, 5);
});

test('늦게 색인된 대표 결과물은 다음 폴링에 다시 그린다', () => {
  const h = ready();
  h.render([done('a', TODAY, { cover: null })]);
  assert.doesNotMatch(h.board.innerHTML, /<img/);
  h.render([done('a', TODAY, { cover: { path: 'a.png', kind: 'image', nsfw: false } })]);
  assert.match(h.board.innerHTML, /<img/);
});

test('필터로 빠진 잡의 상세 창은 유지하고 정말 사라지면 닫는다', () => {
  const h = ready();
  const jobs = [done('old', YESTERDAY), done('new', TODAY)];
  h.ctx.setScope('openJobDetailId = "old"');
  h.render(jobs);
  assert.deepEqual(h.ids(), ['new']);
  assert.equal(h.calls.closed, 0);
  assert.equal(h.calls.detail, 1);
  h.render([jobs[1]]);
  assert.equal(h.calls.closed, 1);
});

test('날짜가 있으면 빈 줄 안내가 날짜를 지우라고 알려 준다', () => {
  const h = ready();
  h.render([]);
  assert.match(h.board.innerHTML, /이 날짜에 표시할 작업이 없어요\. 날짜를 지우면 모든 날짜를 볼 수 있어요\./);
  h.date.value = '';
  h.date.listeners.change();
  assert.match(h.board.innerHTML, /job-list-empty">표시할 작업이 없어요\./);
});
