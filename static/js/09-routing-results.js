// ---- 화면 전환 ----
// 화면은 두 층이다.
//     전역   #dashboard · #gallery · #danbooru      어느 파드와도 무관한 화면
//     파드   #pod/{id}/{sub}   (sub: jobs 등, POD_SUBTABS 참고)
// 작업을 파드 안에 넣은 이유는 그 파드가 무엇을 갖고 있는가"를 읽어서 그리기
// 때문이다 — 작업 목록은 그 파드의 큐를 본다. 전역 탭으로 두면 파드가 여럿일 때 화면이 어느
// 파드 이야기를 하는지 말해주지 못한다(그래서 예전엔 늘 기본 파드만 봤다).
// 반대로 Danbooru는 태그를 조립할 뿐 파드를 쓰지 않으므로 전역에 남는다.
//
// 주소창 해시로 화면을 기억한다. 서버 라우트를 늘리지 않고도 새로고침·뒤로가기·링크
// 공유가 되는 가장 가벼운 방법이라 해시를 쓴다.
const TAB_MAINS = {
  dashboard: document.getElementById('tab-home'),        // 홈 = 프로젝트 대시보드
  pods: document.getElementById('tab-dashboard'),        // 파드(워커) 그리드
  jobs: document.getElementById('tab-jobs'),
  gallery: document.getElementById('tab-gallery'),
  'video-gallery': document.getElementById('tab-video-gallery'),
  danbooru: document.getElementById('tab-danbooru'),
  admin: document.getElementById('tab-admin'),           // 회원 관리 (admin 전용)
  db: document.getElementById('tab-db'),                 // DB — RunPod 세션 등 기록 조회 (admin 전용)
  models: document.getElementById('tab-lora'),          // 모델 — 파드와 무관한 기준 데이터
  // 파드 갤러리("그 파드가 만든 것만")는 전역 갤러리와 같은 화면을 그대로 쓴다 —
  // ~500줄짜리 갤러리 구현(격자/작업별·날짜별 보기/선택/삭제/라이트박스)을 통째로
  // 복제하는 대신, galleryPodFilter 하나로 같은 코드가 "전체"와 "이 파드 것만"을
  // 둘 다 그리게 한다. 같은 DOM을 두 이름이 가리키므로 아래 표시 전환 루프는
  // "이름"이 아니라 "실제 화면 요소" 기준으로 처리한다(그래야 pgallery로 보여준
  // 요소를 gallery 키가 다시 숨기는 일이 없다).
  pgallery: document.getElementById('tab-gallery'),
  // pvideo("그 파드가 만든 영상만")도 pgallery와 같은 이유로 전역 영상 갤러리
  // DOM을 그대로 쓴다 — videoGalleryPodFilter가 "전체"와 "이 파드 것만"을 가른다.
  pvideo: document.getElementById('tab-video-gallery'),
  // 프로젝트 안의 갤러리/영상도 같은 DOM을 쓰고, galleryProjectFilter/videoGalleryProjectFilter가
  // "이 프로젝트 것만"을 가른다.
  prgallery: document.getElementById('tab-gallery'),
  prvideo: document.getElementById('tab-video-gallery'),
  // 보드(무한 캔버스)는 프로젝트 전용 개념이라(전역/파드에는 없음) 공유 DOM이 아닌
  // 자기만의 <main>을 쓴다.
  prboard: document.getElementById('tab-prboard'),
  // Claude 글쓰기 파드의 결과는 이미지가 아니라 글이라 갤러리(썸네일 격자)로 못
  // 보여준다 — pgallery와 달리 이건 전용 DOM(#tab-results)과 전용 렌더러
  // (renderResultsList)를 쓰는 완전히 별개의 화면이다.
  results: document.getElementById('tab-results'),
};

// 작업(Jobs)은 전역 Jobs 탭에서만 관리한다 — 파드 안에는 결과물(갤러리/영상/글)과 설정만 둔다.
const POD_TABS = ['pgallery', 'pvideo', 'results'];
// 폰에서는 글자를 숨기고 아이콘만 보이므로(CSS .btn-label), 라벨은 title/aria-label에도 넣는다.
const POD_TAB_META = {
  jobs:     { icon: 'list-checks', label: 'Jobs' },
  pgallery: { icon: 'images',      label: 'Gallery' },
  pvideo:   { icon: 'clapperboard', label: 'Videos' },
  results:  { icon: 'file-text',   label: 'Results' },
  // 화면이 아니라 "이 파드 설정" 모달을 여는 서브탭 버튼이다(POD_TABS에 없음 — 탭 전환이 일어나지 않는다).
  psettings: { icon: 'settings',   label: 'Settings' },
};
// 파드 종류가 서브탭 구성을 정한다 — 셸 파드에는 워크플로우도 설치된 모델도 없다.
// (예전에는 셸 파드만 등록해도 상단에 🧩·🎛 탭이 그대로 보였고, 눌러봐야 빈
// 목록이었다.) 새 종류의 워커를 붙일 때 여기 한 줄이 그 워커의 화면 구성이 된다.
// pgallery/pvideo는 이미지·영상을 만드는 종류에만 둔다 — Claude 글쓰기는 같은
// 자리에 결과가 이미지/영상이 아니라 글이라는 걸 정직하게 반영해 results 탭을
// 대신 쓴다.
const POD_SUBTABS = {
  comfyui: ['pgallery', 'pvideo', 'psettings'],
  shell: ['pgallery', 'pvideo', 'psettings'],
  claude_writer: ['results', 'psettings'],
};

function podSubtabs(pod){ return POD_SUBTABS[(pod || {}).kind] || ['pgallery', 'psettings']; }
// 파드에 들어갔을 때 처음 여는 화면 — 그 종류의 첫 번째 "진짜" 서브탭(설정 버튼 제외).
function podDefaultTab(pod){ return podSubtabs(pod).find(t => POD_TABS.includes(t)) || 'pgallery'; }

// ---- "📝 결과" 탭 (Claude 글쓰기 파드) ----
// 이미지 갤러리와 달리 별도 폴링/캐시 API가 없다 — 목록 자체는 이미 2초마다 도는
// 작업 폴링(lastJobs)에서 그대로 걸러 쓰고, 전문(全文)만 펼칠 때 한 번 받아온다.
let expandedResultId = null;
const resultTextCache = {};   // job_id -> 받아온 전문(못 받아왔으면 안내 문구)

function claudeResultsForPod(podId){
  return lastJobs
    .filter(j => j.pod_id === podId && j.template_id === 'claude_write' && !j.deleted)
    .sort((a, b) => (b.finished_at || b.queued_at || '').localeCompare(a.finished_at || a.queued_at || ''));
}

function renderResultsList(){
  const list = document.getElementById('results-list');
  const empty = document.getElementById('results-empty');
  if(!list || !currentPodId){ if(list) list.innerHTML = ''; return; }
  const rows = claudeResultsForPod(currentPodId);
  empty.style.display = rows.length ? 'none' : '';
  list.innerHTML = rows.map(j => {
    const prompt = (j.options && j.options.prompt) || '(프롬프트 없음)';
    const isOpen = expandedResultId === j.id;
    const bodyHtml = isOpen
      ? `<div class="result-full">${
          resultTextCache[j.id] != null ? escapeHtml(resultTextCache[j.id]) : '불러오는 중…'
        }</div>`
      : '';
    return `
      <div class="result-row${isOpen ? ' open' : ''}" data-result-id="${escapeHtml(j.id)}">
        <div class="result-row-head">
          <span class="badge ${j.status}"><span class="bdot"></span>${STATUS_LABEL[j.status] || j.status}</span>
          <span class="result-prompt" title="${escapeHtml(prompt)}">${escapeHtml(prompt)}</span>
          <span class="result-time">${fmtGalleryDateTime(j.finished_at || j.queued_at)}</span>
        </div>
        ${bodyHtml}
      </div>`;
  }).join('');
}

async function toggleResultRow(jobId){
  expandedResultId = (expandedResultId === jobId) ? null : jobId;
  if(expandedResultId && resultTextCache[jobId] == null){
    renderResultsList();   // "불러오는 중…"을 먼저 보여준 뒤 받아온다
    try{
      const res = await fetch(`/api/jobs/${jobId}/text-result`);
      const data = await res.json().catch(() => ({}));
      resultTextCache[jobId] = data.text || '(아직 결과가 없어요 — 작업 로그를 확인해보세요.)';
    }catch(e){
      resultTextCache[jobId] = '(불러오지 못했어요.)';
    }
  }
  renderResultsList();
}

document.getElementById('results-list').addEventListener('click', (e) => {
  const row = e.target.closest('.result-row');
  if(row) toggleResultRow(row.dataset.resultId);
});

document.getElementById('results-refresh-btn').addEventListener('click', fetchJobs);

// 어느 파드로 들어갈지 정한다. 없는 id로 들어오면(파드를 지웠거나 옛 링크) 마지막에
// 보던 파드 → 기본 파드 순으로 떨어진다. 파드가 하나도 없으면 null(=갈 곳이 없음).
function resolvePodId(podId){
  if(podId && podsById[podId]) return podId;
  if(lastPodId && podsById[lastPodId]) return lastPodId;
  const fallback = podsCache.find(p => p.enabled) || podsCache[0];
  return fallback ? fallback.id : null;
}

// "지금 새 작업을 만들면 어느 파드로 가는가". 새 작업 폼은 파드 스코프 안에만 있어서
// 보통 currentPodId와 같지만, Danbooru → "메인 프롬프트로 보내기"처럼 파드 밖에서
// 폼을 건드리는 경로가 있어서 별도 함수로 둔다.
// 파드를 정하지 않은 "자동" 상태인가 — 전역 작업 화면이나 프로젝트 안에서 실행 파드를 안 골랐을 때.
function formAutoMode(){
  return currentPodId === null && (currentTab === 'jobs' || currentProjectId !== null) && !formPodChoice;
}
function formPodId(){
  if(formAutoMode()) return null;
  return resolvePodId(currentPodId || formPodChoice);
}

function showTab(tab, { podId = null, projectId = null } = {}){
  if(!TAB_MAINS[tab]) tab = 'dashboard';
  if(tab === 'admin' && !isAdminUser()) tab = 'dashboard';
  if(tab === 'db' && !isAdminUser()) tab = 'dashboard';
  // 지금 "새 작업 추가" 폼이 겨냥하고 있는 파드의 종류. currentPodId가 아니라
  // formPodId()로 재는 이유는, 전역 화면(Danbooru 등)에서는 currentPodId가 null이라
  // 종류가 바뀐 것처럼 보여서 아래에서 폼을 괜히 다시 그리기 때문이다 —
  // 그러면 Danbooru에서 보낸 프롬프트가 도착하자마자 지워진다.
  const prevKind = (podsById[formPodId()] || {}).kind;

  // 프로젝트 스코프인지 먼저 정한다 — projectId를 명시했거나, 파드를 명시하지 않고 이미
  // 프로젝트 안에서 그 프로젝트의 서브탭으로 옮겨갈 때다. 전역 화면(Home·Jobs·Danbooru 등)에서 "작업"을 열면
  // 마지막에 보던 프로젝트로 끌려 들어가지 않고 전역 Jobs가 열린다(그래야 상단 내비게이션이 유지된다).
  // 파드를 정해서 "작업"을 열면(카드의 최근 작업, 라이트박스의 "설정 불러오기" 등) 전역 Jobs를 열고
  // 새 작업 폼의 실행 파드만 그 파드로 맞춘다 — 작업 화면은 이제 파드 안에 없다.
  if(tab === 'jobs' && podId){ formPodChoice = podId; podId = null; }

  let scopedProjectId = null;
  const explicitProject = projectId !== null;
  const stayInProject = podId === null && currentProjectId !== null && PROJECT_TABS.includes(tab);
  if(explicitProject || stayInProject){
    scopedProjectId = resolveProjectId(explicitProject ? projectId : currentProjectId);
    if(scopedProjectId === null) tab = 'dashboard';   // 지워졌거나 아직 못 읽은 프로젝트
    else if(!PROJECT_TABS.includes(tab)) tab = 'prgallery';
  }

  let scopedPodId = null;
  // "작업" 탭은 파드 없이도 열린다 — 파드를 고르지 않은 채 작업을 구상해서 대기 큐로 보내고, 어느 파드에서 돌지는
  // 스케줄러가 정한다. 파드 안에서 열면(파드 스코프 바의 Jobs) 그 파드 것만 본다.
  const globalJobs = tab === 'jobs' && scopedProjectId === null && podId === null && currentPodId === null;
  if(scopedProjectId === null && !globalJobs && POD_TABS.includes(tab)){
    scopedPodId = resolvePodId(podId || currentPodId);
    // 파드 목록을 아직 못 읽었으면(첫 로드가 401이었다든지) 갈 곳이 없다.
    if(!scopedPodId) tab = 'dashboard';
    else if(!podSubtabs(podsById[scopedPodId]).includes(tab)) tab = podDefaultTab(podsById[scopedPodId]);
  }
  currentPodId = scopedPodId;
  currentProjectId = scopedProjectId;
  if(scopedPodId){ lastPodId = scopedPodId; lastScopeKind = 'pod'; }
  if(scopedProjectId !== null){ lastProjectId = scopedProjectId; lastScopeKind = 'project'; }
  currentTab = tab;
  // 작업 목록은 언제나 지금 들어와 있는 파드(또는 프로젝트) 것만 보여준다.
  jobPodFilter = (tab === 'jobs') ? scopedPodId : null;
  jobProjectFilter = (tab === 'jobs') ? scopedProjectId : null;

  // 파드 안에 있을 때는 상단 내비게이션의 PODS를 켜 둔다(파드 화면은 PODS 아래 층이다).
  document.querySelectorAll('.tab-bar .tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab || (!!scopedPodId && b.dataset.tab === 'pods')));
  const targetMain = TAB_MAINS[tab];
  for(const el of new Set(Object.values(TAB_MAINS))){
    el.style.display = (el === targetMain) ? '' : 'none';
  }
  // 갤러리 화면(gallery/pgallery가 같은 요소)이 지금 "전체"인지 "이 파드 것만"인지.
  // jobPodFilter와 같은 자리에서 같이 정한다 — 화면을 나가면(다른 탭으로 이동하면)
  // 필터도 같이 풀린다.
  galleryPodFilter = (tab === 'pgallery') ? scopedPodId : null;
  videoGalleryPodFilter = (tab === 'pvideo') ? scopedPodId : null;
  galleryProjectFilter = (tab === 'prgallery') ? scopedProjectId : null;
  videoGalleryProjectFilter = (tab === 'prvideo') ? scopedProjectId : null;
  updateGalleryScopeUI();
  updateVideoGalleryScopeUI();
  renderPodBar();
  renderProjectBar();
  updateJobTargetRow();

  // 파드 종류가 바뀌면 그 종류에서 쓸 수 있는 템플릿만 남긴다. 종류가 그대로면
  // (ComfyUI 파드에서 다른 ComfyUI 파드로) 목록이 같으므로 손대지 않는다 — 여기서
  // 다시 그리면 사용자가 채워둔 옵션이 날아간다.
  const nextKind = (podsById[formPodId()] || {}).kind;
  if(allTemplates.length && prevKind !== nextKind) renderTemplateOptions();

  // 파드 상태 폴링 — 대시보드 카드와 파드 스코프 바가 같은 데이터로 점을 그린다.
  // 둘 다 안 보이는 화면(갤러리·Danbooru)에서는 멈춘다.
  if(tab === 'dashboard' || tab === 'pods' || scopedPodId){
    fetchDashboard();
    if(!dashTimer) dashTimer = setInterval(fetchDashboard, 4000);
  }else if(dashTimer){
    clearInterval(dashTimer);
    dashTimer = null;
  }

  if(tab === 'gallery' || tab === 'pgallery' || tab === 'prgallery') fetchGalleryImages();
  if(tab === 'video-gallery' || tab === 'pvideo' || tab === 'prvideo') fetchGalleryVideos();
  if(tab === 'dashboard' || scopedProjectId !== null) fetchProjects();
  if(['gallery', 'pgallery', 'prgallery', 'video-gallery', 'pvideo', 'prvideo'].includes(tab)) refreshTagOptions();
  if(tab === 'models') initModelsTab();
  if(tab === 'jobs') fetchJobs();
  if(tab === 'results') renderResultsList();
  if(tab === 'admin') fetchAdminUsers();
  if(tab === 'db'){ renderDbPanelTabs(); fetchRunpodSessions(); fetchGitLog(); fetchGenerationLog(); }
  if(tab === 'prboard'){ initBoardCanvas(); resizeBoardViewport(); fetchBoard(); }

  const want = scopedPodId ? `#pod/${scopedPodId}/${tab}`
    : (scopedProjectId !== null ? `#project/${scopedProjectId}/${tab}` : `#${tab}`);
  currentRouteHash = want;
  if(location.hash !== want) history.replaceState(null, '', want);
}

// 파드 스코프 바를 통째로 다시 그린다(이동했을 때 · 파드 목록이 바뀌었을 때).
function renderPodBar(){
  const bar = document.getElementById('pod-bar');
  if(!bar) return;
  // 파드 안에 있는 동안은 전역 탭(대시보드·갤러리·Danbooru)을 아예 숨긴다 — 그
  // 탭들은 "파드 밖" 개념이라, 파드 스코프 바와 같이 떠 있으면 지금 어느 층에
  // 있는지 헷갈린다. 대시보드로는 파드 스코프 바의 "🏠 Home"으로 돌아간다.
  const globalBar = document.getElementById('global-tab-bar');
  // 파드 안에서도 상단 내비게이션은 그대로 둔다(프로젝트 안에서만 숨긴다).
  if(globalBar) globalBar.style.display = currentProjectId !== null ? 'none' : '';
  if(!currentPodId){ bar.style.display = 'none'; return; }
  bar.style.display = '';
  const pod = podsById[currentPodId] || {};

  const select = document.getElementById('pod-bar-select');
  // 꺼둔 파드도 목록에 남긴다 — 그 파드의 지난 작업을 보러 들어갈 수 있어야 한다.
  select.innerHTML = podsCache.map(p =>
    `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}${p.enabled ? '' : ' (사용 안 함)'}</option>`).join('');
  select.value = currentPodId;
  document.getElementById('pod-bar-kind').textContent = pod.kind_label || pod.kind || '';

  document.getElementById('pod-bar-subtabs').innerHTML = podSubtabs(pod).map(t =>
    `<button class="pod-subtab-btn${t === currentTab ? ' active' : ''}" data-pod-tab="${t}" type="button" title="${POD_TAB_META[t].label}" aria-label="${POD_TAB_META[t].label}"><svg class="ico"><use href="#i-${POD_TAB_META[t].icon}"/></svg><span class="btn-label">${POD_TAB_META[t].label}</span></button>`).join('');

  updatePodBarStatus();
}

// 연결 상태 점만 갱신한다. 4초마다 바를 통째로 다시 그리면 열어둔 파드 드롭다운이
// 닫혀버리므로 폴링에서는 이쪽만 부른다.
function updatePodBarStatus(){
  const dot = document.getElementById('pod-bar-dot');
  if(!dot || !currentPodId) return;
  const row = (dashSummary.pods || []).find(p => p.id === currentPodId);
  const state = POD_STATE[row ? podState(row) : 'offline'];
  dot.className = `dash-dot ${state.cls}`;
  dot.title = state.label;
  // 바로가기도 같은 폴링 데이터(card.links)로 그린다 — 바뀌었을 때만 다시 그려서 깜빡이지 않게.
  const links = document.getElementById('pod-bar-links');
  const html = renderPodLinks(row && row.card && row.card.links);
  if(links && links.dataset.html !== html){ links.innerHTML = html; links.dataset.html = html; }
}

function applyHashRoute(){
  const raw = (location.hash || '').replace(/^#/, '');
  const podMatch = raw.match(/^pod\/([^/]+)(?:\/([^/]+))?$/);
  const projectMatch = raw.match(/^project\/([^/]+)(?:\/([^/]+))?$/);
  // 모델 탭은 파드 밖으로 나갔다 — 옛 링크(#pod/{id}/lora · #lora)는 전역 모델 탭으로 보낸다.
  if(podMatch && podMatch[2] === 'lora') showTab('models');
  else if(podMatch) showTab(podMatch[2] || podDefaultTab(podsById[podMatch[1]]), { podId: podMatch[1] });
  else if(projectMatch) showTab(projectMatch[2] || 'prgallery', { projectId: projectMatch[1] === 'unassigned' ? 'unassigned' : Number(projectMatch[1]) });
  // 파드 안으로 옮겨간 옛 링크(#jobs · #lora)는 마지막/기본 파드의 같은
  // 화면으로 보낸다 — showTab이 주소도 새 형태로 고쳐 쓴다. #builder처럼 완전히
  // 없어진 탭은 TAB_MAINS에 없어 showTab이 'dashboard'로 떨어진다(showTab 맨 위 참고).
  else showTab(raw === 'lora' ? 'models' : (raw || 'dashboard'));
}

document.querySelectorAll('.tab-bar .tab-btn').forEach(btn => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});
document.getElementById('brand-home').addEventListener('click', (e) => {
  e.preventDefault();
  showTab('dashboard');
});
document.getElementById('pod-bar-back').addEventListener('click', () => showTab('pods'));
document.getElementById('pod-bar-settings').addEventListener('click', () => {
  if(currentPodId) openPodEditModal(currentPodId);
});
document.getElementById('pod-bar-subtabs').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-pod-tab]');
  if(!btn) return;
  if(btn.dataset.podTab === 'psettings'){ openPodEditModal(currentPodId); return; }
  showTab(btn.dataset.podTab, { podId: currentPodId });
});
// 파드를 바꾸면 같은 서브탭을 그 파드에서 연다. "새 작업 추가" 폼은 그대로 두므로,
// 프롬프트와 옵션을 다 채운 뒤에 어디서 돌릴지 정하는 순서도 그대로 살아 있다
// (파드 종류가 바뀌는 경우에만 템플릿 목록이 다시 그려진다).
document.getElementById('pod-bar-select').addEventListener('change', (e) => {
  showTab(currentTab, { podId: e.target.value });
});
