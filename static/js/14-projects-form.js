// ---- 새 작업 폼의 대상(프로젝트/파드) ----
// 파드 스코프 안에서는 파드가 정해져 있으니 프로젝트만, 프로젝트 스코프 안에서는 프로젝트가
// 정해져 있으니 실행 파드만 고른다.
const FORM_PROJECT_KEY = 'nightshift-form-project';
let jobTargetSignature = '';

function updateJobTargetRow(){
  const row = document.getElementById('job-target-row');
  if(!row) return;
  const inPod = currentPodId !== null;
  const inProject = currentProjectId !== null;
  // 파드 안에서는 파드가 정해져 있으니 프로젝트만. 그 밖(전역 작업 화면·프로젝트 안)에서는 실행 파드를 고를 수 있고,
  // 기본값 "자동"은 파드를 정하지 않고 대기 큐로 보내는 것이다.
  row.style.display = '';
  document.getElementById('job-project-field').style.display = inProject ? 'none' : '';
  document.getElementById('job-pod-field').style.display = inPod ? 'none' : '';

  const enabledPods = podsCache.filter(p => p.enabled);
  const activeProjects = projectsCache.filter(p => !p.archived);
  if(formPodChoice && !(podsById[formPodChoice] && podsById[formPodChoice].enabled)) formPodChoice = '';
  const signature = JSON.stringify([inPod, inProject, enabledPods.map(p => [p.id, p.name, p.kind_label]),
                                    activeProjects.map(p => [p.id, p.name])]);
  if(signature !== jobTargetSignature){
    jobTargetSignature = signature;
    document.getElementById('job-pod-select').innerHTML =
      `<option value="">자동 — 모델이 갖춰진 워커</option>`
      + enabledPods.map(p =>
        `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}${p.kind_label ? ' · ' + escapeHtml(p.kind_label) : ''}</option>`).join('');
    document.getElementById('job-project-select').innerHTML =
      `<option value="">${UNASSIGNED_LABEL}</option>`
      + activeProjects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
  }
  if(!inPod) document.getElementById('job-pod-select').value = formPodChoice || '';
  if(!inProject){
    let stored = '';
    try{ stored = localStorage.getItem(FORM_PROJECT_KEY) || ''; }catch(e){ /* 저장 못 해도 기능엔 지장 없음 */ }
    const sel = document.getElementById('job-project-select');
    sel.value = (stored !== '' && projectsById[Number(stored)] && !projectsById[Number(stored)].archived) ? stored : '';
  }
}

// 새 작업이 어느 프로젝트로 갈지(null = 미분류).
function formProjectId(){
  if(currentProjectId !== null) return currentProjectId === 'unassigned' ? null : currentProjectId;
  const v = document.getElementById('job-project-select').value;
  const id = v === '' ? null : Number(v);
  return (id !== null && projectsById[id]) ? id : null;
}

document.getElementById('job-project-select').addEventListener('change', (e) => {
  try{ localStorage.setItem(FORM_PROJECT_KEY, e.target.value); }catch(err){ /* 무시 */ }
});
document.getElementById('job-pod-select').addEventListener('change', async (e) => {
  const prevKind = formAutoMode() ? 'auto' : (podsById[formPodId()] || {}).kind;
  formPodChoice = e.target.value;
  const nextKind = formAutoMode() ? 'auto' : (podsById[formPodId()] || {}).kind;
  // 파드 종류가 바뀔 때만 템플릿 목록을 다시 그린다 — 안 바뀌었는데 그리면 채워둔 옵션이 날아간다.
  if(allTemplates.length && prevKind !== nextKind) await renderTemplateOptions();
  // 고르는 파드(또는 "자동")가 바뀌면 모델 선택지도 그 기준으로 다시 받는다.
  try{ await fetchComfyObjectInfo(false); }catch(err){ /* 목록 없이도 계속 */ }
  refreshComfyModelSelects();
});

// ---- 프로젝트 만들기/수정 모달 ----
let projectModalId = null;   // null이면 "새 프로젝트"

function openProjectModal(id){
  projectModalId = (id === undefined || id === null) ? null : id;
  const p = projectModalId === null ? null : projectsById[projectModalId];
  document.getElementById('project-modal-title').textContent = p ? 'Project settings' : 'New project';
  document.getElementById('project-name-input').value = p ? p.name : '';
  document.getElementById('project-desc-input').value = p ? (p.description || '') : '';
  document.getElementById('project-archive-input').checked = !!(p && p.archived);
  document.getElementById('project-archive-row').style.display = p ? '' : 'none';
  document.getElementById('project-mature-input').checked = !!(p && p.is_mature);
  document.getElementById('project-delete-btn').style.display = p ? '' : 'none';
  document.getElementById('project-modal-error').textContent = '';
  document.getElementById('project-modal').style.display = 'flex';
  document.getElementById('project-name-input').focus();
}

function closeProjectModal(){
  document.getElementById('project-modal').style.display = 'none';
  projectModalId = null;
}

document.getElementById('project-add-btn').addEventListener('click', () => openProjectModal(null));
document.getElementById('project-modal-close').addEventListener('click', closeProjectModal);
document.getElementById('project-modal-cancel').addEventListener('click', closeProjectModal);
document.getElementById('project-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('project-modal')) closeProjectModal();
});
document.getElementById('project-name-input').addEventListener('keydown', (e) => {
  if(e.key === 'Enter'){ e.preventDefault(); document.getElementById('project-modal-save').click(); }
});
document.getElementById('project-modal-save').addEventListener('click', async () => {
  const errorEl = document.getElementById('project-modal-error');
  const name = document.getElementById('project-name-input').value.trim();
  if(!name){ errorEl.textContent = '이름을 입력하세요.'; return; }
  const body = { name, description: document.getElementById('project-desc-input').value,
    is_mature: document.getElementById('project-mature-input').checked };
  if(projectModalId !== null) body.archived = document.getElementById('project-archive-input').checked;
  try{
    const res = await fetch(projectModalId === null ? '/api/projects' : `/api/projects/${projectModalId}`, {
      method: projectModalId === null ? 'POST' : 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '저장하지 못했어요.');
    closeProjectModal();
    await fetchProjects();
    projectBarSignature = '';
    renderProjectBar();
  }catch(e){
    errorEl.textContent = e.message || '저장하지 못했어요.';
  }
});
document.getElementById('project-delete-btn').addEventListener('click', async () => {
  const id = projectModalId;
  const p = projectsById[id];
  if(id === null || !p) return;
  if(!confirm(`'${p.name}' 프로젝트를 지울까요? 안의 작업과 결과물은 지워지지 않고 "${UNASSIGNED_LABEL}"으로 돌아와요.`)) return;
  const errorEl = document.getElementById('project-modal-error');
  try{
    const res = await fetch(`/api/projects/${id}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '지우지 못했어요.');
    closeProjectModal();
    const wasInside = currentProjectId === id;
    await fetchProjects();
    if(wasInside) showTab('dashboard');
    fetchJobs();
  }catch(e){
    errorEl.textContent = e.message || '지우지 못했어요.';
  }
});

// ---- 작업을 다른 프로젝트로 옮기기 ----
let jobProjectTargetId = null;

function openJobProjectModal(jobId){
  const job = lastJobs.find(j => j.id === jobId);
  if(!job) return;
  jobProjectTargetId = jobId;
  document.getElementById('job-project-error').textContent = '';
  const currentLabel = job.project_id != null ? (projectName(job.project_id) || '(알 수 없음)') : UNASSIGNED_LABEL;
  document.getElementById('job-project-current').textContent =
    `${job.template_label || job.template_id} · 지금 프로젝트: ${currentLabel}`;
  const options = [];
  if(job.project_id != null) options.push(`<option value="">${UNASSIGNED_LABEL}</option>`);
  for(const p of projectsCache){
    if(!p.archived && p.id !== job.project_id) options.push(`<option value="${p.id}">${escapeHtml(p.name)}</option>`);
  }
  options.push(`<option value="${NEW_PROJECT_VALUE}">${NEW_PROJECT_LABEL}</option>`);
  const moveSelect = document.getElementById('job-project-move-select');
  moveSelect.innerHTML = options.join('');
  document.getElementById('job-project-new-name').value = '';
  syncJobProjectNewField(false);
  document.getElementById('job-project-modal').style.display = 'flex';
}

const syncJobProjectNewField = bindNewProjectField('job-project-move-select', 'job-project-new-name');
document.getElementById('job-project-new-name').addEventListener('keydown', (e) => {
  if(e.key === 'Enter'){ e.preventDefault(); document.getElementById('job-project-confirm').click(); }
});

function closeJobProjectModal(){
  document.getElementById('job-project-modal').style.display = 'none';
  jobProjectTargetId = null;
}

document.getElementById('job-project-modal-close').addEventListener('click', closeJobProjectModal);
document.getElementById('job-project-cancel').addEventListener('click', closeJobProjectModal);
document.getElementById('job-project-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('job-project-modal')) closeJobProjectModal();
});
document.getElementById('job-project-confirm').addEventListener('click', async () => {
  const select = document.getElementById('job-project-move-select');
  const errorEl = document.getElementById('job-project-error');
  if(!jobProjectTargetId || select.options.length === 0){ errorEl.textContent = '옮길 프로젝트가 없어요.'; return; }
  const value = select.value;
  try{
    let projectId = value === '' || value === NEW_PROJECT_VALUE ? null : Number(value);
    if(value === NEW_PROJECT_VALUE){
      const name = document.getElementById('job-project-new-name').value.trim();
      if(!name){ errorEl.textContent = '새 프로젝트 이름을 입력하세요.'; return; }
      projectId = (await createProjectByName(name)).id;
    }
    const res = await fetch(`/api/jobs/${jobProjectTargetId}/project`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '옮기지 못했어요.');
    closeJobProjectModal();
    fetchJobs();
    fetchProjects();
    if(currentTab === 'prgallery') fetchGalleryImages();
    if(currentTab === 'prvideo') fetchGalleryVideos();
  }catch(e){
    errorEl.textContent = e.message || '옮기지 못했어요.';
  }
});

let ignoreHashChangeUntil = 0;
window.addEventListener('hashchange', () => {
  if(Date.now() < ignoreHashChangeUntil) return;   // 뒤로 가기 가드가 만든 주소 변화는 화면 이동이 아니다
  applyHashRoute();
});

// ---- 설치된 앱(standalone)에서 안드로이드 뒤로 가기 ----
// 앱 창에서는 뒤로 가기가 곧바로 앱을 끝낸다(화면 이동은 주소창을 replaceState로만 바꿔서 기록이 안 쌓인다).
// 기록에 가드 항목 하나를 더 쌓아 두고 뒤로 가기를 받으면: 열려 있는 창(라이트박스·모달)이 있으면 그걸 닫고,
// 없으면 안내를 띄운 뒤 2초 안에 한 번 더 눌렀을 때만 진짜로 나간다. 브라우저 탭에서는 켜지 않는다.
// Chrome은 사용자 조작 없이 쌓인 기록 항목을 뒤로 가기에서 건너뛰므로(뒤로 가기 가로채기 방지), 가드는
// 페이지를 연 직후가 아니라 첫 터치·클릭·키 입력 때 쌓는다.
(function setupBackToExit(){
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  if(!standalone) return;
  const EXIT_WINDOW_MS = 2000;
  let armedUntil = 0;
  const pushGuard = () => history.pushState({ nsGuard: true }, '', location.pathname + location.search + (currentRouteHash || location.hash));
  const closeTopOverlay = () => {
    const isOpen = (el) => el.style.display !== 'none' && getComputedStyle(el).display !== 'none';
    const modal = [...document.querySelectorAll('.modal-overlay')].find(isOpen);
    const box = modal || [...document.querySelectorAll('.lightbox-overlay')].find(isOpen);
    const closeBtn = box && box.querySelector('.modal-close, .lightbox-close');
    if(!closeBtn) return false;
    closeBtn.click();
    return true;
  };
  let guardInstalled = false;
  const installGuard = () => {
    if(guardInstalled) return;
    guardInstalled = true;
    pushGuard();
  };
  if(window.navigator.userActivation && window.navigator.userActivation.hasBeenActive) installGuard();
  else for(const ev of ['click', 'touchend', 'keydown']) window.addEventListener(ev, installGuard, { once: true, capture: true });
  window.addEventListener('popstate', () => {
    if(!guardInstalled) return;
    ignoreHashChangeUntil = Date.now() + 300;
    if(closeTopOverlay()){ pushGuard(); return; }
    if(Date.now() < armedUntil){ history.back(); return; }   // 두 번째: 앱 종료
    armedUntil = Date.now() + EXIT_WINDOW_MS;
    flashNotice('한 번 더 누르면 앱이 종료돼요');
    pushGuard();
  });
})();

