const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const routing = fs.readFileSync(path.join(__dirname, '../static/js/09-routing-results.js'), 'utf8');
const init = fs.readFileSync(path.join(__dirname, '../static/js/18-init.js'), 'utf8');
const routeStart = routing.indexOf('function showJobLinkNotice(');
const routeFunctions = routing.slice(routeStart, routing.indexOf("document.querySelectorAll('.tab-bar .tab-btn')", routeStart));

function harness({ jobs = [], ok = true, reject = false } = {}){
  const notice = { id: 'job-link-notice', textContent: '', hidden: true, setAttribute(){} };
  const calls = [];
  const context = {
    location: { hash: '#dashboard' },
    document: {
      getElementById: id => id === 'job-link-notice' && notice.inserted ? notice : null,
      createElement: () => notice,
      querySelector: () => ({ before(){ notice.inserted = true; } }),
    },
    jobSearch: { value: 'previous' }, jobStatusFilter: { value: 'failed' },
    lastJobs: [], lastJobsRowsSignature: 'old', jobLinkRouteToken: 0, openJobDetailId: null,
    showTab(tab, options){ calls.push(['tab', tab, options]); context.jobLinkRouteToken++; context.location.hash = (options && options.routeHash) || `#${tab}`; },
    fetchJobs: async () => {
      calls.push(['fetch']);
      if(reject) throw new Error('offline');
      if(ok) context.lastJobs = jobs;
      return ok;
    },
    render: () => { calls.push(['render']); },
    openJobDetailModal: id => { calls.push(['open', id]); context.openJobDetailId = id; },
    closeJobDetailModal: () => { calls.push(['close']); context.openJobDetailId = null; },
    setTimeout: () => 1, clearTimeout: () => {},
    podsById: {}, podDefaultTab: () => 'jobs', projectDefaultTab: () => 'jobs',
  };
  vm.createContext(context);
  vm.runInContext(routeFunctions, context);
  return { context, calls, notice, route: hash => { context.location.hash = hash; return context.applyHashRoute(); } };
}

test('로그인 완료 뒤 전역 작업 목록에서 인코딩된 ID의 상세를 연다', async () => {
  const h = harness({ jobs: [{ id: 'a/b 한글', status: 'done' }] });
  assert.match(init, /__nightshiftAuthReady\.then\(user => \{[\s\S]*fetchPods\(\)\.then\(fetchProjects\)\.then\(applyHashRoute\)/);
  await h.route('#job/a%2Fb%20%ED%95%9C%EA%B8%80');
  assert.deepEqual(h.calls.map(c => c[0]), ['tab', 'render', 'fetch', 'open']);
  assert.equal(h.calls[0][2].globalJobs, true);
  assert.equal(h.calls[0][2].skipJobsFetch, true);
  assert.equal(h.context.jobSearch.value, '');
  assert.equal(h.context.jobStatusFilter.value, 'all');
  assert.equal(h.context.lastJobsRowsSignature, null);
  assert.equal(h.context.location.hash, '#job/a%2Fb%20%ED%95%9C%EA%B8%80');
  assert.equal(h.calls[3][1], 'a/b 한글');
});

test('해시가 바뀌면 진행 중인 이전 상세 요청을 열지 않는다', async () => {
  let finish;
  const h = harness({ jobs: [{ id: 'one' }] });
  h.context.fetchJobs = () => new Promise(resolve => { finish = resolve; });
  const pending = h.route('#job/one');
  h.context.jobLinkRouteToken++;
  h.context.location.hash = '#jobs';
  finish(true);
  await pending;
  assert.equal(h.calls.some(c => c[0] === 'open'), false);
});

test('없는 잡과 타인에게 보이지 않는 잡은 동일한 안내를 표시한다', async () => {
  const h = harness({ jobs: [{ id: 'mine' }, { id: 'removed', deleted: true }] });
  await h.route('#job/other');
  assert.equal(h.calls.some(c => c[0] === 'open'), false);
  assert.match(h.notice.textContent, /찾을 수 없거나 볼 권한이 없어요/);
  assert.equal(h.notice.hidden, false);
  await h.route('#job/removed');
  assert.equal(h.calls.some(c => c[0] === 'open'), false);
});

test('잘못된 인코딩과 목록 조회 실패를 안내한다', async () => {
  const malformed = harness();
  await malformed.route('#job/%XX');
  assert.match(malformed.notice.textContent, /주소를 확인/);
  assert.equal(malformed.calls.some(c => c[0] === 'fetch'), false);
  const failed = harness({ ok: false });
  await failed.route('#job/one');
  assert.match(failed.notice.textContent, /목록을 불러오지 못했어요/);
});

test('기존 #jobs 경로는 기존 탭 라우팅으로 간다', () => {
  const h = harness();
  h.route('#jobs');
  assert.equal(h.calls[0][1], 'jobs');
  assert.equal(h.calls[0][2], undefined);
  assert.equal(h.calls.some(c => c[0] === 'fetch'), false);
});

test('열린 상세에서 해시를 바꾸면 상세를 닫는다', async () => {
  const h = harness({ jobs: [{ id: 'one' }] });
  await h.route('#job/one');
  h.route('#jobs');
  assert.equal(h.calls.at(-2)[0], 'close');
  assert.equal(h.calls.at(-1)[1], 'jobs');
});
