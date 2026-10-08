// 새 작업이 어느 워커로 가는지(formPodId) 모의 상태로 검사한다 — node tests/test_job_queue_registration_ui.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', '09-routing-results.js'), 'utf8');
const pick = (name) => {
  const m = src.match(new RegExp(`function ${name}\\([^)]*\\)\\{[\\s\\S]*?\\n\\}`));
  assert.ok(m, `${name} 함수를 찾지 못했다`);
  return m[0];
};
const code = ['resolvePodId', 'formAutoMode', 'formPodId'].map(pick).join('\n');

function run(state){
  const pods = state.pods || [];
  const ctx = vm.createContext({
    podsCache: pods,
    podsById: Object.fromEntries(pods.map(p => [p.id, p])),
    lastPodId: null, currentPodId: null, currentTab: 'dashboard', currentProjectId: null, formPodChoice: null,
    ...state,
  });
  vm.runInContext(code, ctx);
  return vm.runInContext('formPodId()', ctx);
}

const local = { id: 'local', enabled: false };
const gpu = { id: 'gpu', enabled: true };

// 라이브러리: 선택 없음이면 마지막 워커가 사용 안 함이어도 자동(null)
assert.strictEqual(run({ currentTab: 'library', pods: [local], lastPodId: 'local' }), null);
assert.strictEqual(run({ currentTab: 'library', pods: [] }), null);
assert.strictEqual(run({ currentTab: 'library', pods: [local, gpu], lastPodId: 'local', formPodChoice: '' }), null);
// 라이브러리에서 명시적으로 고른 워커는 그대로
assert.strictEqual(run({ currentTab: 'library', pods: [local, gpu], formPodChoice: 'gpu' }), 'gpu');
// 전역 작업·프로젝트는 기존대로 자동
assert.strictEqual(run({ currentTab: 'jobs', pods: [gpu], lastPodId: 'gpu' }), null);
assert.strictEqual(run({ currentTab: 'project', currentProjectId: 3, pods: [gpu], lastPodId: 'gpu' }), null);
assert.strictEqual(run({ currentTab: 'jobs', pods: [local, gpu], formPodChoice: 'local' }), 'local');
// 워커 범위 안은 그 워커
assert.strictEqual(run({ currentTab: 'newjob', currentPodId: 'local', pods: [local, gpu] }), 'local');
assert.strictEqual(run({ currentTab: 'library', currentPodId: 'gpu', pods: [local, gpu] }), 'gpu');
// 그 밖의 화면은 기존 복원 동작(마지막 워커 → 켜진 첫 워커) 유지
assert.strictEqual(run({ currentTab: 'danbooru', pods: [local, gpu], lastPodId: 'local' }), 'local');
assert.strictEqual(run({ currentTab: 'danbooru', pods: [local, gpu] }), 'gpu');

console.log('ok');
