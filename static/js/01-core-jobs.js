// ---- 파드(워커) 목록 ----
// 파드가 하나뿐이면 화면에 파드 이야기를 아예 꺼내지 않는다(예전과 똑같이 보인다).
// 두 개 이상일 때만 업로드 폼의 "실행할 파드" 선택과 작업 행의 파드 배지가 나타난다.
let podsCache = [];
let podsById = {};
let podKinds = [];      // 등록할 수 있는 파드 종류(드라이버) 목록
let allTemplates = [];  // 서버가 준 템플릿 전체(고른 파드 종류에 맞는 것만 드롭다운에 넣는다)
// 작업 목록을 한 파드 것만 보고 있을 때 그 파드 id. null이면 전체.
let jobPodFilter = null;
// 갤러리를 한 파드 것만 보고 있을 때 그 파드 id. null이면 전체(jobPodFilter와
// 같은 자리, 같은 생명주기 — showTab에서 같이 정해진다).
let galleryPodFilter = null;
// 영상 갤러리판 galleryPodFilter — 별도 변수인 이유는 이미지 갤러리와 영상
// 갤러리가 같은 화면 안에서도 서로 다른 파드로 스코프될 수 있어서가 아니라
// (실제로는 같이 바뀐다), 두 갤러리가 완전히 독립된 렌더러/데이터를 쓰기
// 때문에 하나로 묶으면 오히려 "이 필터가 지금 어느 갤러리 얘기인지" 헷갈린다.
let videoGalleryPodFilter = null;
// 지금 열려 있는 파드 스코프(#pod/{id}/...). 전역 화면(대시보드·갤러리·Danbooru)에
// 있으면 null이다.
let currentPodId = null;
// 전역 화면으로 나가도 기억하는 "마지막에 보던 파드". Danbooru에서 프롬프트를
// 만들어 작업 폼으로 보낼 때처럼, 파드 밖에 있으면서 파드를 골라야 하는 경로가
// 엉뚱한 파드로 떨어지지 않게 하는 용도다.
let lastPodId = null;
let currentTab = 'dashboard';
// 프로젝트 스코프(#project/{id}/...). null이면 프로젝트 밖, 'unassigned'면 "미분류".
// 프로젝트는 파드와 무관하게 작업·결과물을 묶는다 — 파드 스코프와는 서로 배타적이다.
let currentProjectId = null;
let lastProjectId = null;
let currentRouteHash = '';   // showTab이 주소창에 써 둔 현재 화면의 #경로 (뒤로 가기 처리에서 쓴다)
let lastScopeKind = null;   // 마지막으로 들어가 있던 스코프 종류('pod'|'project') — Danbooru 등 밖에서 돌아올 때 쓴다
let jobProjectFilter = null;
let galleryProjectFilter = null;
let videoGalleryProjectFilter = null;
// 프로젝트 스코프의 새 작업 폼에서 고른 "실행 파드"(파드 스코프에서는 그 파드가 정해져 있다).
let formPodChoice = null;

function multiPod(){ return podsCache.length > 1; }
function podName(podId){ return (podsById[podId] || {}).name || ''; }

async function fetchPods(){
  try{
    const res = await fetch('/api/pods');
    const data = await res.json();
    podsCache = data.pods || [];
    podKinds = data.kinds || [];
  }catch(e){
    podsCache = [];
  }
  podsById = Object.fromEntries(podsCache.map(p => [p.id, p]));
  // 이름이 바뀌거나 파드가 늘고 줄면 스코프 바의 파드 선택도 따라와야 한다.
  if(typeof renderPodBar === 'function') renderPodBar();
  return podsCache;
}

const STATUS_LABEL = {
  pending: "대기중", queued: "대기중", running: "실행중", done: "완료",
  failed: "실패", interrupted: "중단됨"
};

// 접기/펼치기 버튼뿐 아니라 그 옆 제목 텍스트를 눌러도 토글되도록, 클릭 리스너는
// 버튼이 아니라 이 둘을 감싼 .panel-header-title에 하나만 단다(버튼 클릭도 이
// 리스너까지 그대로 버블링되므로 따로 달 필요가 없다).
document.querySelectorAll('.panel > .panel-header > .panel-header-title').forEach(title => {
  const btn = title.querySelector('.collapse-btn');
  if(!btn) return;
  title.addEventListener('click', () => {
    const panel = title.closest('.panel');
    const collapsed = panel.classList.toggle('collapsed');
    btn.setAttribute('aria-expanded', String(!collapsed));
  });
});

function pad2(n){ return String(n).padStart(2, '0'); }

function fmtTime(iso){
  if(!iso) return "—";
  const d = new Date(iso);
  return `<span class="time-hm">${pad2(d.getHours())}:${pad2(d.getMinutes())}</span><span class="time-s">:${pad2(d.getSeconds())}</span>`;
}

// 갤러리 "날짜별 보기" 그룹핑에 씀 — mtime(UTC ISO 문자열)을 브라우저의 로컬
// 시간대 기준 달력 날짜로 변환한다(fmtTime과 같은 방식으로 new Date(iso)의
// 로컬 getter를 쓰므로 표시되는 시:분과 날짜 경계가 항상 일치한다).
const WEEKDAY_KO = ['일', '월', '화', '수', '목', '금', '토'];

function fmtDateKey(iso){
  if(!iso) return '';
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function fmtDateLabel(dateKey){
  if(!dateKey) return '날짜 정보 없음';
  const [y, m, d] = dateKey.split('-').map(Number);
  const weekday = WEEKDAY_KO[new Date(y, m - 1, d).getDay()];
  return `${y}.${pad2(m)}.${pad2(d)} (${weekday})`;
}

// 갤러리 라이트박스/"자세히 보기" 둘 다 쓰는 짧은 날짜+시간 표기("MM/DD HH:MM").
function fmtGalleryDateTime(iso){
  if(!iso) return '-';
  const d = new Date(iso);
  return `${pad2(d.getMonth() + 1)}/${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function fmtDuration(ms){
  if(ms == null || !isFinite(ms) || ms < 0) return "—";
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return `<span class="time-hm">${pad2(h)}:${pad2(m)}</span><span class="time-s">:${pad2(s)}</span>`;
}

// 실행 중인 작업은 지금까지의 진행 속도(경과 시간 / 완료한 이미지 수)로
// 남은 시간을 추정하고, 끝난 작업은 실제 걸린 시간을 그대로 보여준다.
function estimateJob(j){
  if(j.status === 'done' || j.status === 'failed' || j.status === 'interrupted'){
    if(j.started_at && j.finished_at){
      const durationMs = new Date(j.finished_at) - new Date(j.started_at);
      return { durationMs, finishIso: j.finished_at };
    }
    return { durationMs: null, finishIso: null };
  }
  if(j.status === 'running' && j.started_at && j.progress && j.progress.total > 0 && j.progress.done > 0){
    const startMs = new Date(j.started_at).getTime();
    const elapsedMs = Date.now() - startMs;
    const estTotalMs = elapsedMs / j.progress.done * j.progress.total;
    return { durationMs: estTotalMs, finishIso: new Date(startMs + estTotalMs).toISOString() };
  }
  return { durationMs: null, finishIso: null };
}

function fmtOptions(options){
  const entries = Object.entries(options || {});
  if(entries.length === 0) return '-';
  return entries.map(([k, v]) => `${k}=${v}`).join(', ');
}

function escapeHtml(str){
  return String(str).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Lucide 아이콘(body 맨 위 SVG 스프라이트) 마크업 — 버튼 라벨 등을 JS로 바꿀 때 쓴다.
// 이름은 sprite의 #i-이름과 같다. spin=true면 빙글빙글 돈다(로딩 표시용).
function ico(name, spin){
  return `<svg class="ico${spin ? ' ico-spin' : ''}"><use href="#i-${name}"/></svg>`;
}

// ---- 긴 목록 나눠 그리기(갤러리·영상·DB 탭) ----
// 앞에서부터 LIST_PAGE개만 그리고, 목록 끝의 "더 보기" 버튼이 화면 가까이 오면(또는 누르면) 다음 묶음을 이어 붙인다.
// 받아 온 목록은 그대로 전부 들고 있으므로 전체 선택·라이트박스 넘기기는 안 그린 것까지 다룬다.
// ponytail: 그리는 양만 나눈다 — 목록 자체(4초마다 전체)는 그대로 받으니, 수천 개가 되면 서버에서 나눠 받기로.
const LIST_PAGE = 50;
function setupLoadMore(btnId, onMore){
  const btn = document.getElementById(btnId);
  const nearView = () => { const r = btn.getBoundingClientRect(); return !btn.hidden && r.height > 0 && r.top < innerHeight + 200; };
  // 한 번 이어 붙인 뒤에도 버튼이 여전히 화면 안이면(큰 화면) 관찰자가 다시 알려 주지 않으니 직접 이어서 부른다.
  const more = () => { onMore(); requestAnimationFrame(() => { if(nearView()) more(); }); };
  btn.addEventListener('click', more);
  new IntersectionObserver(entries => { if(entries.some(e => e.isIntersecting) && !btn.hidden) more(); }, { rootMargin: '200px 0px' }).observe(btn);
}
function updateLoadMore(btnId, remaining, unit){
  const btn = document.getElementById(btnId);
  btn.hidden = remaining <= 0;
  btn.textContent = `더 보기 (남은 ${remaining}${unit})`;
}

async function fetchJobs(){
  const res = await fetch('/api/jobs');
  // 401(API 키 없음/오류) 등으로 실패하면 목록을 비우지 않고 마지막으로 받아온
  // 상태를 그대로 둔다 — 헤더의 "🔑 API 키" 버튼이 이미 빨간색으로 문제를
  // 알려주므로, 여기서는 2초 뒤 다음 폴링 때 다시 시도하기만 하면 된다.
  if(!res.ok) return false;
  const data = await res.json();
  render(data.jobs, data.pending_count, data.running, data.pods || {});
  return true;
}

// 마지막으로 그린 작업 목록. render()의 지역 변수 jobs는 그 함수 밖에서는 못 보는데,
// 모달처럼 나중에 열리는 화면도 작업 정보를 봐야 해서 여기 남겨둔다.
let lastJobs = [];
// 작업 목록은 2초마다 폴링되는데, 매번 tbody.innerHTML을 통째로 다시 그리면 로그를
// 펼쳐서 에러 메시지를 드래그로 선택/복사하려는 순간 DOM이 갈아끼워져서 선택이
// 풀려버린다(내용이 하나도 안 바뀌었어도). 그래서 "보이는 내용이 실제로 바뀌었을
// 때만" 다시 그린다 — 바뀐 게 없으면 이번 tick은 손대지 않고 그대로 둔다.
let lastJobsRowsSignature = null;

let clearingDoneJobs = false;
function visibleDoneJobs(){
  return lastJobs.filter(j => j.status === 'done' && !j.deleted
    && (!jobPodFilter || j.pod_id === jobPodFilter)
    && (jobProjectFilter === null || projectMatches(j.project_id, jobProjectFilter)));
}
function updateClearDoneJobs(){
  const btn = document.getElementById('clear-done-jobs');
  const count = visibleDoneJobs().length;
  btn.disabled = clearingDoneJobs || count === 0;
  btn.setAttribute('aria-busy', String(clearingDoneJobs));
  btn.innerHTML = clearingDoneJobs ? `${ico('loader-circle', true)} 청소 중…` : `완료 잡 청소 (${count})`;
}
document.getElementById('clear-done-jobs').addEventListener('click', async () => {
  if(clearingDoneJobs) return;
  const count = visibleDoneJobs().length;
  if(!count) return;
  // 확인 당시 범위를 고정한다 — 요청 중 화면을 옮겨도 다른 범위를 지우지 않는다.
  const params = new URLSearchParams({ done_only: 'true' });
  const scope = [];
  if(jobPodFilter){ params.set('pod_id', jobPodFilter); scope.push(`파드 ${podName(jobPodFilter) || jobPodFilter}`); }
  if(jobProjectFilter !== null){
    params.set('project_id', jobProjectFilter);
    scope.push(jobProjectFilter === 'unassigned' ? '미분류' : `프로젝트 ${projectName(jobProjectFilter)}`);
  }
  const label = scope.join(' / ') || '전체';
  if(!confirm(`${label} 범위의 완료 잡 ${count}개를 청소할까요?\n실패·중단·대기·실행 중 잡은 유지해요. 오래된 작업 설정은 영구 정리될 수 있어요.`)) return;
  clearingDoneJobs = true;
  updateClearDoneJobs();
  const result = document.getElementById('clear-done-jobs-result');
  result.textContent = '';
  try{
    const res = await fetch(`/api/jobs/clear-completed?${params}`, { method: 'POST' });
    if(!res.ok) throw new Error('cleanup');
    const data = await res.json();
    result.textContent = `${label} 범위의 완료 잡 ${data.cleared}개를 청소했어요.`;
    try{ if(!await fetchJobs()) throw new Error('refresh'); }
    catch(e){ result.textContent += ' 목록을 갱신하지 못했어요. 잠시 후 다시 확인해 주세요.'; }
  }catch(e){
    result.textContent = '청소하지 못했어요. 연결과 로그인 상태를 확인하고 다시 시도해 주세요.';
  }finally{
    clearingDoneJobs = false;
    updateClearDoneJobs();
  }
});

// 상태를 대기/실행/완료/실패 4섹션으로 묶는다 — "중단됨"(interrupted)은 실패
// 섹션에 같이 묶이지만 배지 자체(.badge.interrupted)는 그대로 렌더링해 색으로
// 실제 실패와 구분된다.
const JOB_SECTIONS = [
  { key: 'queue', label: '대기', match: j => j.status === 'pending' || j.status === 'queued' },
  { key: 'running', label: '실행', match: j => j.status === 'running' },
  { key: 'done', label: '완료', match: j => j.status === 'done' },
  { key: 'failed', label: '실패', match: j => j.status === 'failed' || j.status === 'interrupted' },
];

function render(jobs, pendingCount, running, perPod){
  lastJobs = jobs;
  // "📝 결과" 탭이 열려 있는 동안 새 작업이 끝나면 목록에 바로 반영되게, 작업
  // 폴링(2초)에 얹어 같이 새로고침한다 — 별도 폴링 루프를 안 둔다.
  if(currentTab === 'results') renderResultsList();
  // 작업 목록은 언제나 지금 들어와 있는 파드 것만 보여준다(파드 스코프 바 참고).
  if(jobPodFilter) jobs = jobs.filter(j => j.pod_id === jobPodFilter);
  if(jobProjectFilter !== null) jobs = jobs.filter(j => projectMatches(j.project_id, jobProjectFilter));
  updateClearDoneJobs();
  const board = document.getElementById('job-board');

  // 상세 모달이 열려 있는 작업이 이번 목록에서 사라졌으면(삭제됐거나 필터로
  // 빠졌으면) 모달을 닫는다. 남아있으면 카드 그리드의 시그니처 가드와 무관하게
  // 매 tick마다 다시 그린다 — 로그 <pre>만 loadLog()의 "텍스트가 실제로 바뀔 때만"
  // 가드로 보호되므로(그 <pre>는 이 재렌더 대상 밖에 고정 배치돼 있다), 드래그
  // 선택이 폴링으로 풀리는 문제가 재발하지 않는다.
  if(openJobDetailId){
    const openJob = jobs.find(j => j.id === openJobDetailId);
    if(openJob){
      renderJobDetailModalBody();
      loadLog(openJobDetailId);
      // 실행 중인 동안만 생성물 목록을 다시 받는다 — 끝난 작업은 더 안 늘어나므로
      // openJobDetailModal이 연 시점의 한 번으로 충분하다.
      if(openJob.status === 'running') loadJobDetailOutputs(openJobDetailId);
    }else{
      closeJobDetailModal();
    }
  }

  // 화면에 실제로 영향을 주는 값만 서명에 넣는다 — template_label/options 같은
  // 것들은 작업이 생성될 때 정해지고 이후 안 바뀌므로 뺐다.
  const rowsSignature = JSON.stringify(jobs.map(j => [
    j.id, j.status, j.waiting_for_comfy, j.pod_id, j.started_at, j.finished_at,
    j.progress && j.progress.done, j.progress && j.progress.total,
    j.project_id, j.project_id != null ? projectName(j.project_id) : '',
    preflightCount(j.preflight), j.waiting_reason, j.pinned_pod_id,
    j.missing_models && j.missing_models.names.join('|'), !!j.fetching_models, j.fetch_error,
    (j.model_events || []).length,
  ]));
  if(rowsSignature === lastJobsRowsSignature) return;
  lastJobsRowsSignature = rowsSignature;

  // 카드가 없는 단계도 칸 자체는 늘 보여준다(4단계 칸반 구조를 항상 그대로 유지).
  board.innerHTML = JOB_SECTIONS.map(section => {
    const sectionJobs = jobs.filter(section.match);
    // 대기 칸 맨 끝에는 항상 "+" 카드를 붙인다(openNewJobModal) — 그래서 대기 칸은
    // 절대 "없음" 플레이스홀더로 안 빠지고 늘 그리드로 그려진다.
    const cardsHtml = sectionJobs.map(buildJobCard).join('')
      + (section.key === 'queue' ? '<button type="button" class="job-card-add" id="job-add-card" title="새 작업 추가"><svg class="ico"><use href="#i-plus"/></svg></button>' : '');
    return `
      <div class="job-section">
        <div class="job-section-header">${section.label} <span class="job-section-count">${sectionJobs.length}</span></div>
        ${cardsHtml ? `<div class="job-board-grid">${cardsHtml}</div>` : `<div class="job-section-empty">없음</div>`}
      </div>`;
  }).join('');

  board.querySelectorAll('.job-card').forEach(card => {
    card.addEventListener('click', (e) => {
      // 모델 안내 링크는 해시 이동 대신 showTab으로 간다 — 해시 링크는 기록을 쌓고 popstate를 내서
      // 설치된 앱에서 뒤로 가기 가드가 "앱이 종료돼요"를 띄우고 이동을 막는다(openGalleryForModel과 같은 이유)
      const modelsLink = e.target.closest('.job-card-models-link');
      if(modelsLink){ e.preventDefault(); showTab('pmodels', { podId: modelsLink.dataset.podId }); return; }
      if(e.target.closest('.start-btn, .stop-btn, .del-btn')) return;
      openJobDetailModal(card.dataset.id);
    });
  });

  const addCard = document.getElementById('job-add-card');
  if(addCard) addCard.addEventListener('click', openNewJobModal);

  board.querySelectorAll('.del-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await fetch('/api/jobs/' + btn.dataset.del, { method: 'DELETE' });
      fetchJobs();
    });
  });

  board.querySelectorAll('.start-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      try{
        const res = await fetch(`/api/jobs/${btn.dataset.start}/start`, { method: 'POST' });
        if(!res.ok){
          const data = await res.json().catch(() => ({}));
          alert(data.detail || '시작하지 못했어요.');
        }
      }finally{
        fetchJobs();
      }
    });
  });

  board.querySelectorAll('.fetch-models-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      try{
        const res = await fetch(`/api/jobs/${btn.dataset.fetchModels}/fetch-missing`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if(!res.ok){ alert(data.detail || '모델을 받지 못했어요.'); return; }
        const lines = [];
        if(data.started.length) lines.push(`받기 시작: ${data.started.join(', ')}\n다 받으면 작업이 저절로 시작해요.`);
        if(data.no_url.length) lines.push(`다운로드 주소가 없어 못 받음: ${data.no_url.join(', ')}\n→ "모델" 탭에서 다운로드 주소를 등록한 뒤 다시 눌러 주세요.`);
        if(data.failed.length) lines.push(`실패: ${data.failed.map(f => `${f.name} (${f.detail})`).join(', ')}`);
        if(lines.length) alert(lines.join('\n\n'));
      }finally{
        fetchJobs();
      }
    });
  });

  board.querySelectorAll('.pause-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      try{
        const res = await fetch(`/api/jobs/${btn.dataset.pause}/pause`, { method: 'POST' });
        if(!res.ok){
          const data = await res.json().catch(() => ({}));
          alert(data.detail || '일시정지하지 못했어요.');
        }
      }finally{
        fetchJobs();
      }
    });
  });

  board.querySelectorAll('.stop-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      try{
        const res = await fetch(`/api/jobs/${btn.dataset.stop}/stop`, { method: 'POST' });
        if(!res.ok){
          const data = await res.json().catch(() => ({}));
          alert(data.detail || '정지하지 못했어요.');
        }
      }finally{
        fetchJobs();
      }
    });
  });
}

// 작업 하나를 카드로 만든다 — 최소 정보만(상태·배지·템플릿·워크플로우 파일명·진행률
// 또는 대기 사유 + 시작/정지/삭제). 옵션 요약/CSV 파일명/시간 4종/전체 preflight
// 목록/로그/불러오기·수정·이동 버튼은 카드를 눌러 여는 상세 모달에만 있다
// (openJobDetailModal/renderJobDetailModalBody).
// job 객체 자체엔 "이미지/영상 중 뭘 만드는지"·"시드 반복/CSV 배치 중 어떤 방식인지"·
// "베이스 모델이 뭔지"를 나타내는 필드가 없다(서버 템플릿 매니페스트에도 없음, 20개
// 템플릿을 직접 조사해 확인함) — template_id 문자열 패턴과 options.checkpoint로
// 추정한다. 카드 제목을 짧게 줄이기 위한 용도라, 못 맞혀도(빈 문자열) 카드가 깨지진
// 않는다.
function jobMediaKind(templateId){
  const tid = templateId || '';
  if(tid === 'shell_command' || tid === 'claude_write') return null;
  return /i2v|flf2v|r2v/.test(tid) ? 'video' : 'image';
}
function jobBatchModeLabel(templateId, templateLabel){
  const tid = templateId || '';
  if(tid.includes('csv_batch')) return 'CSV 배치';
  if(tid.includes('_batch')) return '시드 반복';
  return templateLabel || tid || '-';
}
// 마법사로 만든 작업은 체크포인트가 업로드되는 워크플로우 JSON 안에 직접 박히고,
// options.checkpoint(템플릿 자체의 체크포인트 드롭다운 — "직접 템플릿 선택"으로 쓸 때만
// 채워짐)는 거의 항상 빈 채로 남는다 — 즉 실제로 만드는 작업 대부분(마법사 경로)에서
// 카드 둘째 줄이 비게 된다. 정확히 알아내려면 결국 워크플로우 파일을 열어 체크포인트
// 로더 노드를 찾아야 한다: /api/jobs/{id}/workflow(이미 "워크플로우 수정" 기능이 쓰는
// 엔드포인트, 읽기는 상태 제한 없음)로 한 번 받아 jobWorkflowCheckpointCache에 영구
// 캐시한다 — 카드가 몇 번을 다시 그려져도 같은 작업은 다시 안 받는다. 동기 함수인
// jobBaseModelLabel 안에서 fetch를 기다릴 수는 없어서, 캐시에 없으면 백그라운드로
// 한 번만 받아오기 시작하고(fetchJobWorkflowCheckpoint) 이번 렌더에는 빈 줄로 둔다 —
// 받아오면 시그니처를 지워 다음 폴링 tick에 카드를 다시 그리게 한다.
const jobWorkflowCheckpointCache = {};   // jobId -> 체크포인트 파일명 | null(못 찾음/실패)
const jobWorkflowCheckpointPending = new Set();

function findCheckpointInWorkflow(wf){
  for(const node of Object.values(wf || {})){
    if(!node || typeof node !== 'object') continue;
    const inputs = node.inputs || {};
    if(node.class_type === 'CheckpointLoaderSimple' && inputs.ckpt_name) return inputs.ckpt_name;
    if((node.class_type === 'UNETLoader' || node.class_type === 'UnetLoaderGGUF') && inputs.unet_name) return inputs.unet_name;
  }
  return null;
}

async function fetchJobWorkflowCheckpoint(jobId){
  if(jobId in jobWorkflowCheckpointCache || jobWorkflowCheckpointPending.has(jobId)) return;
  jobWorkflowCheckpointPending.add(jobId);
  try{
    const res = await fetch(`/api/jobs/${jobId}/workflow`);
    jobWorkflowCheckpointCache[jobId] = res.ok ? findCheckpointInWorkflow(await res.json()) : null;
  }catch(e){
    jobWorkflowCheckpointCache[jobId] = null;
  }finally{
    jobWorkflowCheckpointPending.delete(jobId);
    lastJobsRowsSignature = null;   // 알아낸 값을 카드에 반영하도록 다음 tick에 강제로 다시 그린다
  }
}

function jobBaseModelLabel(j){
  const ckpt = j.options && j.options.checkpoint;
  if(ckpt) return modelEntry('checkpoints', ckpt).base_model || ckpt;
  // wan22/minimax-h3 영상 템플릿은 체크포인트 드롭다운이 아예 없다(고정 내장 모델).
  const tid = j.template_id || '';
  if(tid.startsWith('wan22_')) return 'Wan 2.2';
  if(tid.startsWith('minimax_h3_')) return 'MiniMax-H3';
  if(j.workflow_filename){
    if(j.id in jobWorkflowCheckpointCache){
      const found = jobWorkflowCheckpointCache[j.id];
      if(found) return modelEntry('checkpoints', found).base_model || found;
    }else{
      fetchJobWorkflowCheckpoint(j.id);
    }
  }
  return '';
}

// 정사각형 카드 — 최소 정보만: 종류 아이콘+배치 방식(1줄), 베이스 모델(1줄), 진행률
// (실행 중일 때만), 시작/정지/삭제 아이콘 버튼. 소유자/파드/프로젝트/워크플로우
// 파일명/상태 텍스트/대기 사유는 전부 상세 모달에서만 보여준다(openJobDetailModal).
// 작업 카드의 경과 시간 — 실행 중이면 시작부터 지금까지(1초마다 tickJobElapsed가 글자만 갱신해서 카드
// 전체를 다시 그리지 않는다), 끝난 작업(완료/실패/중단)은 시작부터 끝까지 걸린 시간을 고정으로 보여준다.
function fmtElapsed(sec){
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function jobElapsedHtml(j){
  const start = Date.parse(j.started_at || '');
  if(!start) return '';
  if(j.status === 'running'){
    return `<span class="job-card-elapsed running" data-elapsed-start="${start}" title="실행 경과 시간">${ico('history')} ${fmtElapsed((Date.now() - start) / 1000)}</span>`;
  }
  const end = Date.parse(j.finished_at || '');
  if(!end || !['done', 'failed', 'interrupted'].includes(j.status)) return '';
  return `<span class="job-card-elapsed" title="걸린 시간">${ico('history')} ${fmtElapsed((end - start) / 1000)}</span>`;
}
function tickJobElapsed(){
  document.querySelectorAll('.job-card-elapsed[data-elapsed-start]').forEach(el => {
    const text = ' ' + fmtElapsed((Date.now() - Number(el.dataset.elapsedStart)) / 1000);
    if(el.lastChild && el.lastChild.nodeType === 3) el.lastChild.textContent = text;
  });
}
setInterval(tickJobElapsed, 1000);

function buildJobCard(j){
  const mediaKind = jobMediaKind(j.template_id);
  const kindIcon = mediaKind === 'video' ? '<svg class="ico"><use href="#i-clapperboard"/></svg>'
    : mediaKind === 'image' ? '<svg class="ico"><use href="#i-images"/></svg>' : '';
  const batchLabel = jobBatchModeLabel(j.template_id, j.template_label);
  const baseModel = jobBaseModelLabel(j);
  const progress = j.progress;
  const progressPct = progress && progress.total > 0 ? Math.min(100, Math.round((progress.done / progress.total) * 100)) : 0;
  const waitingForPod = j.status === 'queued' && !j.pod_id;   // 대기 큐에서 갖춘 파드를 기다리는 중
  // 조치 필요 — 연결된 파드는 있는데 모델이 없어서 못 도는 상태(스케줄러의 missing_models). 모델을
  // 받으면 저절로 시작되므로 실패로 치지 않고, 카드를 주황으로 구분하고 "없는 모델 받기"를 준다.
  const blocked = waitingForPod && !!(j.missing_models && j.missing_models.names && j.missing_models.names.length);
  const fetching = waitingForPod && !!j.fetching_models;
  const pfCount = ['pending', 'queued', 'interrupted', 'failed'].includes(j.status) ? preflightCount(j.preflight) : 0;
  const preflightBadge = pfCount
    ? `<span class="pod-badge preflight-badge" title="${escapeHtml('등록할 때 이 작업이 갈 워커에 없던 것들:\n' + preflightLines(j.preflight).join('\n'))}">${ico('triangle-alert')} 모델 ${pfCount}</span>` : '';
  // 대기 칸의 두 상태를 카드 모양과 버튼으로 가른다 — pending(일시정지: 빈 파드가 있어도 시작 안 함)은
  // 회색 카드에 ▶, queued(빈 파드가 생기면 바로 실행)는 보통 카드에 ⏸(POST /api/jobs/{id}/pause).
  const startable = ['pending', 'interrupted', 'failed'].includes(j.status);
  const deletable = j.status !== 'running' && (j.status !== 'queued' || waitingForPod);

  return `
    <div class="job-card" data-id="${j.id}" data-status="${j.status}"${blocked ? ' data-blocked="1"' : ''}>
      ${preflightBadge}
      <div>
        <div class="job-card-title">${kindIcon}<span>${escapeHtml(batchLabel)}</span></div>
        ${baseModel ? `<div class="job-card-base">${escapeHtml(baseModel)}</div>` : ''}
        ${fetching
          ? `<a class="job-card-wait fetching job-card-models-link" href="#pod/${encodeURIComponent(j.fetching_models.pod_id)}/pmodels" data-pod-id="${escapeHtml(j.fetching_models.pod_id)}" title="${escapeHtml(j.fetching_models.names.join('\n') + '\n\n눌러서 워커의 모델 탭에서 진행 보기')}">${ico('loader-circle', true)}<span>${j.fetching_models.auto ? '모델 자동 설치 중' : '모델 받는 중'} ${j.fetching_models.names.length}개 — 눌러서 진행 보기</span></a>`
          : blocked && j.fetch_error ? `<div class="job-card-wait" title="${escapeHtml('모델 받기 실패 — ' + j.fetch_error + '\n\n' + (j.waiting_reason || ''))}">${ico('triangle-alert')}<span>받기 실패 — ${escapeHtml(/HTTP 40[13]/.test(j.fetch_error) ? 'Civitai 토큰 필요(HTTP ' + j.fetch_error.match(/HTTP (40[13])/)[1] + ') · 카드를 눌러 자세히' : j.fetch_error)}</span></div>`
          : blocked ? `<a class="job-card-wait job-card-models-link" href="#pod/${encodeURIComponent(j.missing_models.pod_id)}/pmodels" data-pod-id="${escapeHtml(j.missing_models.pod_id)}" title="${escapeHtml((j.waiting_reason || '') + '\n\n눌러서 워커의 모델 탭 보기')}">${ico('triangle-alert')}<span>${escapeHtml(j.waiting_reason || '없는 모델이 있어요')}</span></a>`
          : waitingForPod && j.waiting_reason ? `<div class="job-card-wait" title="${escapeHtml(j.waiting_reason)}">${ico('triangle-alert')}<span>${escapeHtml(j.waiting_reason)}</span></div>` : ''}
      </div>
      ${progress ? `
        <div class="progress-wrap">
          <div class="progress-track"><div class="progress-fill ${j.status}" style="width:${progressPct}%"></div></div>
          <span class="progress-text">${progress.done}/${progress.total}</span>
        </div>` : ''}
      <div class="job-card-actions">
        ${jobElapsedHtml(j)}
        ${deletable
          ? `<button class="del-btn" data-del="${j.id}" title="삭제"><svg class="ico"><use href="#i-trash-2"/></svg></button>`
          : `<button class="del-btn" disabled title="진행 중인 작업은 먼저 정지해야 삭제할 수 있어요"><svg class="ico"><use href="#i-trash-2"/></svg></button>`}
        ${j.status === 'running' ? `<button class="stop-btn" data-stop="${j.id}" title="이 작업만 정지해요 — 상태가 '정지'로 바뀌면 삭제할 수 있어요"><svg class="ico"><use href="#i-pause"/></svg></button>` : ''}
        ${blocked && !fetching ? `<button class="fetch-models-btn" data-fetch-models="${j.id}" title="'${escapeHtml(j.missing_models.pod_name)}'에 없는 모델(${escapeHtml(j.missing_models.names.join(', '))})을 등록부의 다운로드 주소로 받아요 — 다 받으면 저절로 시작해요"><svg class="ico"><use href="#i-download"/></svg></button>` : ''}
        ${j.status === 'queued' ? `<button class="pause-btn" data-pause="${j.id}" title="실행 대기 중 — 누르면 일시정지해요(빈 워커가 있어도 시작하지 않아요)"><svg class="ico"><use href="#i-pause"/></svg></button>` : ''}
        ${startable ? `<button class="start-btn" data-start="${j.id}" title="${j.status === 'pending' ? '일시정지 중 — 누르면 실행 대기로 넘어가요(빈 워커가 있으면 바로 시작)' : '이 작업을 다시 시작해요'}"><svg class="ico"><use href="#i-play"/></svg></button>` : ''}
      </div>
    </div>`;
}

// ---- 작업 카드를 눌러 여는 상세 모달 ----
// 카드에서 뺀 옵션 요약/CSV 파일명/시간 4종/전체 preflight 목록/불러오기·워크플로우·CSV
// 수정·프로젝트·파드 이동 버튼/로그를 여기 모아 보여준다. jobMoveTargetId와 같은
// 패턴으로 지금 열려 있는 작업의 id만 들고 있는다.
let openJobDetailId = null;

function openJobDetailModal(jobId){
  openJobDetailId = jobId;
  renderJobDetailModalBody();
  // 이전에 열었던 다른 작업의 로그/생성물이 잠깐 남아 보이지 않도록, 새로 받아오는
  // 동안은 안내 문구/빈 상태로 되돌려 둔다 — <pre>는 재생성하지 않고(위 HTML 주석
  // 참고) 텍스트만 바꾼다.
  document.getElementById('job-detail-log').textContent = '불러오는 중…';
  jobDetailOutputs = null;
  renderJobDetailOutputs();
  document.getElementById('job-detail-modal').style.display = 'flex';
  loadLog(jobId);
  loadJobDetailOutputs(jobId);
}

function closeJobDetailModal(){
  document.getElementById('job-detail-modal').style.display = 'none';
  openJobDetailId = null;
}

// 대기 칸 맨 끝 "+" 카드(job-card-add)나 "불러오기"류 진입점들이 이 모달을 연다 —
// 안의 폼/마법사 id는 전부 예전 New Job 패널 그대로라 별도 초기화가 필요 없다.
function openNewJobModal(){
  document.getElementById('new-job-modal').style.display = 'flex';
}
function closeNewJobModal(){
  document.getElementById('new-job-modal').style.display = 'none';
}
document.getElementById('new-job-modal-close').addEventListener('click', closeNewJobModal);
document.getElementById('new-job-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('new-job-modal')) closeNewJobModal();
});

// render()가 매 폴링 tick마다(카드 그리드의 시그니처 가드와 무관하게) 다시 부른다 —
// 로그 <pre>는 여기서 손대지 않고 loadLog()의 자체 가드로만 갱신된다.
function renderJobDetailModalBody(){
  const job = lastJobs.find(j => j.id === openJobDetailId);
  if(!job){ closeJobDetailModal(); return; }
  const templateText = job.template_label || job.template_id;
  // 프롬프트는 "옵션" 요약 문자열에 섞여 들어가면 한눈에 안 들어와서 따로 뺀다.
  const optsEntries = Object.entries(job.options || {});
  const promptText = job.options && job.options.main_prompt;
  const otherOptionsText = fmtOptions(Object.fromEntries(optsEntries.filter(([k]) => k !== 'main_prompt')));
  const est = estimateJob(job);
  const waitingForComfy = !!job.waiting_for_comfy;
  const waitingForPod = job.status === 'queued' && !job.pod_id;
  const pinnedNote = job.pinned_pod_id ? ` (${escapeHtml(podName(job.pinned_pod_id) || '지정한 워커')}만)` : '';
  const movable = podsCache.some(p => p.enabled) && ['pending', 'queued', 'interrupted'].includes(job.status);
  const pfLines = preflightLines(job.preflight);

  document.getElementById('job-detail-title').innerHTML =
    `<span class="badge ${(waitingForComfy || waitingForPod) ? 'waiting' : job.status}"><span class="bdot"></span>${waitingForComfy ? 'GPU 대기' : (waitingForPod ? '워커 대기' + pinnedNote : (STATUS_LABEL[job.status] || job.status))}</span> ${escapeHtml(templateText)}`;

  document.getElementById('job-detail-fields').innerHTML = `
    ${promptText ? `<div class="field"><label class="field-label">프롬프트</label><div class="job-detail-prompt">${escapeHtml(promptText)}</div></div>` : ''}
    <div class="field"><label class="field-label">옵션</label><div>${escapeHtml(otherOptionsText)}</div></div>
    <div class="field"><label class="field-label">워크플로우</label><div>${escapeHtml(job.workflow_original_name || '-')}</div></div>
    <div class="field"><label class="field-label">CSV</label><div>${escapeHtml(job.csv_original_name || '-')}</div></div>
    ${job.owner_name ? `<div class="field"><label class="field-label">만든 사람</label><div>${escapeHtml(job.owner_name)}</div></div>` : ''}
    ${job.pod_id ? `<div class="field"><label class="field-label">워커</label><div>${escapeHtml(podName(job.pod_id) || job.pod_id)}</div></div>` : ''}
    ${job.project_id != null && projectsById[job.project_id] ? `<div class="field"><label class="field-label">프로젝트</label><div>${escapeHtml(projectName(job.project_id))}</div></div>` : ''}
    <div class="field">
      <label class="field-label">시간</label>
      <div>시작 ${fmtTime(job.started_at)} · 종료 ${fmtTime(job.finished_at)} · 예상 작업시간 ${fmtDuration(est.durationMs)} · 예상 종료 ${fmtTime(est.finishIso)}</div>
    </div>
    ${waitingForPod && job.waiting_reason ? `<div class="job-wait-reason">${escapeHtml(job.waiting_reason)}</div>` : ''}
    ${waitingForPod && job.fetch_error ? `<div class="job-wait-reason">모델 받기 실패 — ${escapeHtml(job.fetch_error)}${/HTTP 40[13]/.test(job.fetch_error) ? '<br>→ 계정 창(오른쪽 위 계정 이름)의 CIVITAI 토큰을 확인하고, 모델 탭의 다운로드 주소가 civitai.com/civitai.red 주소인지 확인하세요.' : ''}</div>` : ''}
    ${(job.model_events || []).length ? `<div class="field"><label class="field-label">모델 받기 기록</label><div class="job-detail-preflight">${escapeHtml(job.model_events.map(e => `${fmtTime(e.at)}  ${e.text}`).join('\n'))}</div></div>` : ''}
    ${pfLines.length ? `<div class="field"><label class="field-label">모델 확인 필요</label><div class="job-detail-preflight">${escapeHtml(pfLines.join('\n'))}</div></div>` : ''}`;

  document.getElementById('job-detail-actions').innerHTML = `
    <button class="load-btn" id="job-detail-load-btn" title="설정 불러오기"><svg class="ico"><use href="#i-folder-open"/></svg> 불러오기</button>
    ${job.status === 'pending' ? `<button class="edit-btn" id="job-detail-edit-workflow-btn" title="워크플로우 수정"><svg class="ico"><use href="#i-pencil"/></svg> 워크플로우 수정</button>` : ''}
    ${job.status === 'pending' && job.csv_filename ? `<button class="edit-btn" id="job-detail-edit-csv-btn" title="CSV 수정"><svg class="ico"><use href="#i-pencil"/></svg> CSV 수정</button>` : ''}
    <button class="load-btn" id="job-detail-project-btn" title="이 작업(과 결과물)을 다른 프로젝트로 옮겨요"><svg class="ico"><use href="#i-folder-input"/></svg> 프로젝트 이동</button>
    ${movable ? `<button class="load-btn" id="job-detail-move-btn" title="이 작업을 다른 워커로 옮겨요"><svg class="ico"><use href="#i-arrow-right"/></svg> 워커 이동</button>` : ''}`;

  document.getElementById('job-detail-load-btn').addEventListener('click', () => {
    closeJobDetailModal();
    loadJobSettings(job);
  });
  const editWfBtn = document.getElementById('job-detail-edit-workflow-btn');
  if(editWfBtn) editWfBtn.addEventListener('click', () => openAttachmentEditor(job.id, 'workflow'));
  const editCsvBtn = document.getElementById('job-detail-edit-csv-btn');
  if(editCsvBtn) editCsvBtn.addEventListener('click', () => openAttachmentEditor(job.id, 'csv'));
  document.getElementById('job-detail-project-btn').addEventListener('click', () => openJobProjectModal(job.id));
  const moveBtn = document.getElementById('job-detail-move-btn');
  if(moveBtn) moveBtn.addEventListener('click', () => openJobMoveModal(job.id));
}

// 이 작업이 만든 생성물(이미지/영상) — 서버에 job_id로 거르는 API가 따로 없어서
// (MCP list_output_images의 job_id 인자도 같은 이유로 전체를 받아 여기서 거른다고
// 문서화돼 있다) /api/output-images·/api/output-videos를 통째로 받아 job_id로
// 거른다. 대기 중인 작업은 당연히 아직 아무것도 없으니 건너뛴다.
let jobDetailOutputs = null;   // { images: [...], videos: [...] } | null(아직 못 받음)

async function loadJobDetailOutputs(jobId){
  const job = lastJobs.find(j => j.id === jobId);
  if(!job || ['pending', 'queued'].includes(job.status)){
    if(openJobDetailId === jobId){ jobDetailOutputs = { images: [], videos: [] }; renderJobDetailOutputs(); }
    return;
  }
  try{
    // 헤더의 NSFW "숨김" 모드를 여기선 안 따른다 — 이 모달은 이미 그 작업의 원본
    // 프롬프트(NSFW일 수 있음)를 그대로 보여주는 화면이라, 생성물만 따로 가리면
    // 같은 모달 안에서 앞뒤가 안 맞는다. 본인이 만든 작업을 직접 눌러 들어온
    // 상세 화면이라 메인 갤러리(둘러보다 우연히 마주칠 수 있는 곳)와는 성격이 다르다.
    const [imgRes, vidRes] = await Promise.all([
      fetch('/api/output-images'),
      fetch('/api/output-videos'),
    ]);
    const imgData = await imgRes.json();
    const vidData = await vidRes.json();
    if(openJobDetailId !== jobId) return;   // 그 사이 다른 작업으로 넘어갔으면 버린다
    jobDetailOutputs = {
      images: (imgData.images || []).filter(a => a.job_id === jobId),
      videos: (vidData.videos || []).filter(a => a.job_id === jobId),
    };
  }catch(e){
    if(openJobDetailId !== jobId) return;
    jobDetailOutputs = { images: [], videos: [] };
  }
  renderJobDetailOutputs();
}

function renderJobDetailOutputs(){
  const field = document.getElementById('job-detail-outputs-field');
  const wrap = document.getElementById('job-detail-outputs');
  const items = jobDetailOutputs
    ? [...jobDetailOutputs.images.map(a => ({ ...a, kind: 'image' })), ...jobDetailOutputs.videos.map(a => ({ ...a, kind: 'video' }))]
    : [];
  if(items.length === 0){ field.style.display = 'none'; wrap.innerHTML = ''; return; }
  field.style.display = '';
  wrap.innerHTML = items.map(a => {
    const thumb = a.kind === 'video'
      ? `<video class="job-detail-output-thumb" muted preload="metadata" src="${window.__nightshiftMediaUrl(`/api/output-videos/${encodeURIComponent(a.name)}`)}#t=0.1"></video>`
      : `<img class="job-detail-output-thumb" src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(a.name)}/thumbnail?size=128&fit=cover&v=${encodeURIComponent(a.mtime)}`)}" alt="" loading="lazy">`;
    return `<button type="button" class="job-detail-output" data-name="${escapeHtml(a.name)}" data-kind="${a.kind}" title="${escapeHtml(a.name)}">${thumb}<span class="job-detail-output-name">${escapeHtml(a.name)}</span></button>`;
  }).join('');
  wrap.querySelectorAll('.job-detail-output').forEach(btn => {
    btn.addEventListener('click', () => openGalleryForJobOutput(btn.dataset.name, btn.dataset.kind));
  });
}

// 생성물 썸네일을 누르면 그 이미지/영상을 갤러리에서 바로 연다 —
// openGalleryForModel과 같은 패턴(필터를 비우고 탭을 전환한 뒤 라이트박스를 연다).
async function openGalleryForJobOutput(name, kind){
  const isVideo = kind === 'video';
  const filters = isVideo ? videoGalleryFilters : galleryFilters;
  const prefix = isVideo ? 'video' : 'image';
  Object.assign(filters, { q: '', favorite: false, tag: '', minRating: 0, model: '' });
  document.getElementById(`gf-${prefix}-q`).value = '';
  document.getElementById(`gf-${prefix}-tag`).value = '';
  document.getElementById(`gf-${prefix}-rating`).value = '0';
  const fav = document.getElementById(`gf-${prefix}-fav`);
  fav.classList.remove('on'); fav.setAttribute('aria-pressed', 'false');
  galleryFilterSyncs[prefix]();
  closeJobDetailModal();
  showTab(isVideo ? 'video-gallery' : 'gallery');
  await (isVideo ? fetchGalleryVideos() : fetchGalleryImages());
  const list = isVideo ? displayedGalleryVideos : displayedGalleryImages;
  const idx = list.findIndex(x => x.name === name);
  if(idx >= 0) (isVideo ? openVideoLightbox : openLightbox)(idx);
}

document.getElementById('job-detail-modal-close').addEventListener('click', closeJobDetailModal);
document.getElementById('job-detail-close-btn').addEventListener('click', closeJobDetailModal);
document.getElementById('job-detail-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('job-detail-modal')) closeJobDetailModal();
});
// 로그 전체를 클립보드로 복사 — <pre id="job-detail-log">는 절대 다시 안 만들어지는
// 고정 요소라(위 HTML 주석 참고), 예전 #job-rows 델리게이션과 달리 딱 한 번만
// 붙이면 된다.
document.getElementById('job-detail-log-copy-btn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const el = document.getElementById('job-detail-log');
  const text = el ? el.textContent : '';
  const original = btn.innerHTML;
  try{
    await navigator.clipboard.writeText(text);
    btn.innerHTML = ico('circle-check') + ' 복사됨';
  }catch(err){
    btn.innerHTML = ico('triangle-alert') + ' 복사 실패 — 직접 드래그해서 복사해주세요';
  }
  setTimeout(() => { btn.innerHTML = original; }, 1500);
});

