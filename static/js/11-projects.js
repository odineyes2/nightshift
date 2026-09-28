// ---- 프로젝트 ----
// 프로젝트는 파드와 무관하게 작업(job)과 결과물(asset)을 묶는다(서버 db.py/projects.py).
// project_id가 없는 것은 "미분류"다. 홈이 프로젝트 대시보드이고, 프로젝트 안
// (#project/{id}/...)에서는 그 프로젝트의 작업·갤러리·영상만 보인다. 파드는 새 작업 폼에서
// 고르는 값이다(파드가 일시적이라 프로젝트가 파드에 종속되면 안 된다).
const PROJECT_TABS = ['prboard', 'jobs', 'prgallery', 'prvideo'];   // 프로젝트 바의 탭 순서
// 프로젝트에 들어갔을 때 처음 여는 화면 — 보드. "미분류"는 진짜 프로젝트가 아니라 보드가 없으니 다음 탭(작업).
function projectDefaultTab(projectId){ return projectId === 'unassigned' ? 'jobs' : 'prboard'; }
const PROJECT_TAB_META = {
  jobs:      { icon: 'list-checks',  label: 'Jobs' },
  prgallery: { icon: 'images',       label: 'Gallery' },
  prvideo:   { icon: 'clapperboard', label: 'Videos' },
  prboard:   { icon: 'network',      label: 'Board' },
};
const UNASSIGNED_LABEL = '미분류';
let projectsCache = [];
let projectsById = {};
let unassignedSummary = { job_count: 0, active_jobs: 0, asset_count: 0, cover_path: null };

function projectName(id){
  return id === 'unassigned' ? UNASSIGNED_LABEL : ((projectsById[id] || {}).name || '');
}

// filter: null(전체) | 'unassigned'(미분류) | 프로젝트 id
function projectMatches(itemProjectId, filter){
  if(filter === null) return true;
  return filter === 'unassigned' ? itemProjectId == null : itemProjectId === filter;
}

// 없는 프로젝트(지웠거나 아직 못 읽었거나 옛 링크)면 null — showTab이 홈으로 돌려보낸다.
function resolveProjectId(id){
  if(id === 'unassigned') return 'unassigned';
  const n = Number(id);
  return (Number.isInteger(n) && projectsById[n]) ? n : null;
}

async function fetchProjects(){
  try{
    const res = await fetch('/api/projects?include_archived=true');
    if(res.ok){
      const data = await res.json();
      projectsCache = data.projects || [];
      unassignedSummary = data.unassigned || unassignedSummary;
    }
  }catch(e){
    // 못 받아오면 이전 값을 그대로 둔다.
  }
  projectsById = Object.fromEntries(projectsCache.map(p => [p.id, p]));
  renderHome();
  renderProjectBar();
  updateJobTargetRow();
  return projectsCache;
}

// ---- 홈 (프로젝트 대시보드) ----
function fmtUsd(v){ return v < 0.01 ? '<$0.01' : `$${v.toFixed(2)}`; }

function projectCardHtml(p){
  const inbox = p === null;
  const d = inbox
    ? { id: 'unassigned', name: UNASSIGNED_LABEL, description: '프로젝트에 속하지 않은 작업과 결과물',
        archived: false, favorite_count: 0, last_activity: null, ...unassignedSummary }
    : p;
  const coverHidden = d.is_mature && nsfwMode !== 'show';
  const cover = coverHidden
    ? `<div class="project-cover-empty" title="NSFW 콘텐츠가 '보기'가 아니라서 대표 이미지를 가렸어요">${ico('eye-off')}</div>`
    : d.cover_path
    ? `<img class="project-cover-img" loading="lazy" alt="" src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(d.cover_path)}/thumbnail?size=400`)}">`
    : `<div class="project-cover-empty">${ico(inbox ? 'inbox' : 'folder')}</div>`;
  const mature = d.is_mature ? `<span class="badge mature" title="성인 콘텐츠 포함으로 표시된 프로젝트">${ico('flame')} 18+</span>` : '';
  const active = d.active_jobs > 0
    ? `<span class="badge running"><span class="bdot"></span>실행 중 ${d.active_jobs}</span>` : '';
  const edit = inbox ? '' : `<button class="load-btn project-card-edit" type="button" data-project-edit="${d.id}" title="프로젝트 설정" aria-label="프로젝트 설정">${ico('settings')}</button>`;
  const last = d.last_activity ? `<span>마지막 활동 ${escapeHtml(fmtGalleryDateTime(d.last_activity))}</span>` : '';
  const favs = d.favorite_count > 0 ? `<span title="즐겨찾기한 결과물">${ico('star')}<b>${d.favorite_count}</b></span>` : '';
  // 추정 비용 = 작업 실행 시간 × 그 작업이 돈 파드의 시간당 비용. 비용을 모르는 작업은 빠지므로 "+"로 표시한다.
  const costText = d.est_cost > 0
    ? `<span title="${d.uncosted_jobs > 0 ? `비용을 모르는 작업 ${d.uncosted_jobs}개는 빠진 값이에요 — ` : ''}작업 실행 시간 × 워커 시간당 비용">추정 <b>${fmtUsd(d.est_cost)}${d.uncosted_jobs > 0 ? '+' : ''}</b></span>`
    : '';
  return `
    <div class="project-card${inbox ? ' inbox' : ''}${d.archived ? ' archived' : ''}" data-project-open="${d.id}" role="button" tabindex="0">
      <div class="project-cover">${cover}${inbox ? '' : `<button class="project-cover-btn" type="button" data-project-cover="${d.id}" title="대표 이미지 고르기" aria-label="대표 이미지 고르기">${ico('images')}</button>`}</div>
      <div class="project-card-body">
        <div class="project-card-head">
          <span class="project-card-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}${d.archived ? ' (보관됨)' : ''}</span>
          ${d.owner_name ? `<span class="pod-badge owner-badge" title="이 프로젝트의 주인">${ico('user')} ${escapeHtml(d.owner_name)}</span>` : ''}
          ${mature}${active}${edit}
        </div>
        ${d.description ? `<div class="project-card-desc">${escapeHtml(d.description)}</div>` : ''}
        <div class="project-card-stats">
          <span>작업 <b>${d.job_count}</b></span>
          <span>결과물 <b>${d.asset_count}</b></span>
          ${favs}${costText}
          ${last}
        </div>
      </div>
    </div>`;
}

function renderHome(){
  const canvas = document.getElementById('project-canvas');
  if(!canvas) return;
  const t = (dashSummary && dashSummary.totals) || {};
  document.getElementById('home-strip').innerHTML = [
    ['워커', `${t.online ?? 0}/${t.pods ?? 0}`],
    ['실행 중', `${t.running_jobs ?? 0}건`],
    ['대기', `${t.pending ?? 0}건`],
  ].map(([label, value]) => `
    <div class="dash-total"><span class="dash-total-label">${label}</span><span class="dash-total-value">${escapeHtml(String(value))}</span></div>`).join('')
    + `<button class="load-btn home-strip-pods" type="button" data-go-pods title="워커 탭으로">${ico('server')} 워커 보기</button>`;

  const showArchived = document.getElementById('home-show-archived').checked;
  const list = projectsCache.filter(p => showArchived || !p.archived);
  const hasUnassigned = (unassignedSummary.job_count + unassignedSummary.asset_count) > 0;
  const cards = list.map(projectCardHtml);
  if(hasUnassigned) cards.push(projectCardHtml(null));
  // 카드 이미지를 매번 다시 만들면 깜빡이므로 내용이 같으면 손대지 않는다.
  const html = cards.join('');
  if(canvas.dataset.html !== html){
    canvas.innerHTML = html;
    canvas.dataset.html = html;
  }
  document.getElementById('project-empty').style.display = (list.length === 0 && !hasUnassigned) ? '' : 'none';
}

// ---- 프로젝트 대표 이미지 고르기 ----
// 홈 카드의 커버 이미지는 기본으로 그 프로젝트의 가장 최근 이미지지만, 카드 오른쪽 위 버튼으로 그 프로젝트에 든 이미지 중에서
// 직접 고를 수 있다("자동"을 누르면 다시 가장 최근 이미지). 고른 이미지가 프로젝트 밖으로 옮겨지면 자동으로 돌아간다.
let coverPickerProjectId = null;

async function openCoverPicker(projectId){
  const project = projectsById[projectId];
  if(!project) return;
  coverPickerProjectId = projectId;
  const grid = document.getElementById('cover-picker-grid');
  const empty = document.getElementById('cover-picker-empty');
  const errorEl = document.getElementById('cover-picker-error');
  errorEl.textContent = '';
  document.getElementById('cover-picker-hint').textContent = `'${project.name}'의 대표 이미지로 쓸 이미지를 고르세요.`;
  document.getElementById('cover-picker-auto').classList.toggle('active', project.cover_asset_id == null);
  grid.innerHTML = '<div class="empty">불러오는 중…</div>';
  empty.style.display = 'none';
  document.getElementById('cover-picker-modal').style.display = 'flex';
  try{
    const res = await fetch(`/api/output-assets?project_id=${projectId}&kind=image&limit=300`);
    if(!res.ok) throw new Error('failed');
    const assets = (await res.json()).assets || [];
    if(coverPickerProjectId !== projectId) return;   // 그 사이 닫았거나 다른 프로젝트를 열었다
    if(assets.length === 0){ grid.innerHTML = ''; empty.style.display = 'block'; return; }
    grid.innerHTML = assets.map(a => `
      <button class="cover-picker-item${project.cover_asset_id === a.id ? ' current' : ''}" type="button" data-asset-id="${a.id}" title="${escapeHtml(a.path)}">
        <img src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(a.path)}/thumbnail?size=200&fit=cover`)}" alt="" loading="lazy">
      </button>`).join('');
  }catch(e){
    grid.innerHTML = '';
    errorEl.textContent = '이미지 목록을 불러오지 못했어요.';
  }
}

function closeCoverPicker(){
  document.getElementById('cover-picker-modal').style.display = 'none';
  coverPickerProjectId = null;
}

async function setProjectCover(assetId){
  const projectId = coverPickerProjectId;
  if(projectId === null) return;
  const errorEl = document.getElementById('cover-picker-error');
  errorEl.textContent = '';
  try{
    const res = await fetch(`/api/projects/${projectId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cover_asset_id: assetId }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '바꾸지 못했어요.');
  }catch(e){
    errorEl.textContent = e.message || '바꾸지 못했어요.';
    return;
  }
  closeCoverPicker();
  await fetchProjects();
  flashNotice(assetId === null ? '대표 이미지를 자동으로 되돌렸어요' : '대표 이미지를 바꿨어요');
}

document.getElementById('cover-picker-close').addEventListener('click', closeCoverPicker);
document.getElementById('cover-picker-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('cover-picker-modal')) closeCoverPicker();
});
document.getElementById('cover-picker-auto').addEventListener('click', () => setProjectCover(null));
document.getElementById('cover-picker-grid').addEventListener('click', (e) => {
  const item = e.target.closest('[data-asset-id]');
  if(item) setProjectCover(Number(item.dataset.assetId));
});

document.getElementById('project-canvas').addEventListener('click', (e) => {
  const edit = e.target.closest('[data-project-edit]');
  if(edit){ e.stopPropagation(); openProjectModal(Number(edit.dataset.projectEdit)); return; }
  const cover = e.target.closest('[data-project-cover]');
  if(cover){ e.stopPropagation(); openCoverPicker(Number(cover.dataset.projectCover)); return; }
  const card = e.target.closest('[data-project-open]');
  if(!card) return;
  const raw = card.dataset.projectOpen;
  const projectId = raw === 'unassigned' ? 'unassigned' : Number(raw);
  showTab(projectDefaultTab(projectId), { projectId });
});
document.getElementById('project-canvas').addEventListener('keydown', (e) => {
  if(e.key !== 'Enter' && e.key !== ' ') return;
  const card = e.target.closest('[data-project-open]');
  if(!card || e.target !== card) return;
  e.preventDefault();
  card.click();
});
document.getElementById('home-strip').addEventListener('click', (e) => {
  if(e.target.closest('[data-go-pods]')) showTab('pods');
});
document.getElementById('home-refresh-btn').addEventListener('click', () => { fetchDashboard(); fetchProjects(); });
document.getElementById('home-show-archived').addEventListener('change', renderHome);

// ---- 프로젝트 스코프 바 ----
let projectBarSignature = '';
function renderProjectBar(){
  const bar = document.getElementById('project-bar');
  if(!bar) return;
  if(currentProjectId === null){ bar.style.display = 'none'; projectBarSignature = ''; return; }
  bar.style.display = '';
  const options = projectsCache.filter(p => !p.archived || p.id === currentProjectId);
  const signature = JSON.stringify([options.map(p => [p.id, p.name, p.archived]), currentProjectId, currentTab]);
  if(signature === projectBarSignature) return;   // 4초/15초 갱신이 열어둔 드롭다운을 닫지 않게
  projectBarSignature = signature;
  const select = document.getElementById('project-bar-select');
  select.innerHTML = options.map(p =>
    `<option value="${p.id}">${escapeHtml(p.name)}${p.archived ? ' (보관됨)' : ''}</option>`).join('')
    + `<option value="unassigned">${UNASSIGNED_LABEL}</option>`;
  select.value = String(currentProjectId);
  document.getElementById('project-bar-settings').style.display = currentProjectId === 'unassigned' ? 'none' : '';
  document.getElementById('project-bar-subtabs').innerHTML = PROJECT_TABS.map(t =>
    `<button class="pod-subtab-btn${t === currentTab ? ' active' : ''}" data-project-tab="${t}" type="button" title="${PROJECT_TAB_META[t].label}" aria-label="${PROJECT_TAB_META[t].label}"><svg class="ico"><use href="#i-${PROJECT_TAB_META[t].icon}"/></svg><span class="btn-label">${PROJECT_TAB_META[t].label}</span></button>`).join('');
}

document.getElementById('project-bar-back').addEventListener('click', () => showTab('dashboard'));
document.getElementById('project-bar-settings').addEventListener('click', () => {
  if(typeof currentProjectId === 'number') openProjectModal(currentProjectId);
});
document.getElementById('project-bar-subtabs').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-project-tab]');
  if(btn) showTab(btn.dataset.projectTab, { projectId: currentProjectId });
});
document.getElementById('project-bar-select').addEventListener('change', (e) => {
  const raw = e.target.value;
  showTab(currentTab, { projectId: raw === 'unassigned' ? 'unassigned' : Number(raw) });
});

// ---- 보드 프리셋 저장(새 작업 폼) ----
// 새 작업 폼에서 평소처럼 템플릿·워크플로우·옵션을 정한 뒤 "보드 프리셋으로 저장"을 누르면, 그
// 설정을 회원별 프리셋으로 저장한다(서버 board_presets). 프로젝트 보드의 생성 카드가 이걸 꺼내 쓴다.
// 폼을 두 벌 만들지 않으려고 값은 전부 지금 폼에서 읽는다 — 입력 이미지 칸은 보드에서 선으로
// 채우는 입구가 되므로 저장하지 않고, 카드에서 바로 고칠 옵션(꺼낼 옵션)만 사람이 고른다.
const BOARD_PRESET_EXPOSABLE = ['textarea', 'number', 'number_optional', 'select'];
const BOARD_PRESET_SLOT_TYPES = ['input_image', 'input_image_optional'];
let boardPresetDraft = null;   // 저장 창을 여는 순간 폼에서 읽은 값 { template_id, workflow, video_workflow, options, lora_trigger }

async function openBoardPresetSave(){
  const errorEl = document.getElementById('upload-error');
  errorEl.textContent = '';
  const template = templatesById[templateSelect.value];
  if(!template){ errorEl.textContent = '템플릿을 먼저 골라 주세요.'; return; }
  if(template.requires_csv){ errorEl.textContent = 'CSV로 여러 줄을 도는 템플릿은 보드 프리셋으로 저장할 수 없어요.'; return; }
  let workflow = null, videoWorkflow = null;
  try{
    if(template.requires_workflow !== false){
      if(!selectedFiles.workflow){ errorEl.textContent = '워크플로우를 먼저 넣어 주세요.'; return; }
      workflow = JSON.parse(await selectedFiles.workflow.text());
    }
    if(template.optional_video_workflow && selectedFiles.video_workflow){
      videoWorkflow = JSON.parse(await selectedFiles.video_workflow.text());
    }
  }catch(e){
    errorEl.textContent = '워크플로우 파일을 JSON으로 읽지 못했어요.';
    return;
  }
  const options = {};
  optionsFields.querySelectorAll('.option-input[data-name]').forEach(input => { options[input.dataset.name] = input.value; });
  const loraField = document.getElementById('lora-trigger-field');
  const loraTrigger = loraField && loraField.style.display !== 'none' ? document.getElementById('lora-trigger-input').value.trim() : '';
  const opts = template.options || [];
  // 마법사가 칸 수를 정하는 템플릿(MiniMax r2v 참조 이미지 0~9, i2v 첫/끝 프레임)은 워크플로우에 실제로
  // 있는 칸만 새 작업 창에 나온다 — 보드 카드의 입구도 그 칸만 만들도록 여기서 골라 보낸다.
  const slotNames = opts.filter(o => BOARD_PRESET_SLOT_TYPES.includes(o.type) && !isWizardRefSlotHidden(o.name)).map(o => o.name);
  boardPresetDraft = { template_id: template.id, workflow, video_workflow: videoWorkflow, options, lora_trigger: loraTrigger, slots: slotNames };

  document.getElementById('board-preset-name').value = template.label || template.id;
  const exposable = opts.filter(o => BOARD_PRESET_EXPOSABLE.includes(o.type));
  document.getElementById('board-preset-exposed').innerHTML = exposable.length
    ? exposable.map(o => `<label><input type="checkbox" value="${escapeHtml(o.name)}" ${o.type === 'textarea' ? 'checked' : ''}>`
        + `<span>${escapeHtml(o.label || o.name)}</span><span class="board-preset-opt-name">${escapeHtml(o.name)}</span></label>`).join('')
    : '<div class="board-preset-hint">이 템플릿에는 카드에서 고칠 만한 옵션이 없어요.</div>';
  const slots = opts.filter(o => slotNames.includes(o.name));
  document.getElementById('board-preset-slots').textContent = (slots.length
    ? `보드에서 선으로 이어 채울 입구: ${slots.map(o => (o.label || o.name) + (o.type === 'input_image_optional' ? '(선택)' : '')).join(', ')}. `
    : '이 템플릿에는 이미지 입구가 없어요. ')
    + '고르지 않은 옵션은 지금 폼의 값으로 고정돼요.';
  document.getElementById('board-preset-error').textContent = '';
  document.getElementById('board-preset-modal').style.display = 'flex';
  refreshBoardPresetList();
  document.getElementById('board-preset-name').focus();
}

let boardPresetList = [];
async function refreshBoardPresetList(){
  const listEl = document.getElementById('board-preset-list');
  try{
    const res = await fetch('/api/board-presets');
    boardPresetList = res.ok ? (await res.json()).presets || [] : [];
  }catch(e){ boardPresetList = []; }
  listEl.innerHTML = boardPresetList.length
    ? boardPresetList.map(p => `<div class="board-preset-row"><span class="board-preset-row-name">${escapeHtml(p.name)}</span>`
        + `<span class="board-preset-row-tpl">${escapeHtml(p.template_label || p.template_id)}</span>`
        + `<button type="button" class="icon-btn" data-preset-del="${p.id}" title="이 프리셋 지우기" aria-label="지우기"><svg class="ico"><use href="#i-trash-2"/></svg></button></div>`).join('')
    : '<div class="board-preset-hint">아직 저장한 프리셋이 없어요.</div>';
}

async function saveBoardPreset(){
  const errorEl = document.getElementById('board-preset-error');
  errorEl.textContent = '';
  const name = document.getElementById('board-preset-name').value.trim();
  if(!name){ errorEl.textContent = '이름을 적어 주세요.'; return; }
  const exposed = [...document.querySelectorAll('#board-preset-exposed input:checked')].map(cb => cb.value);
  const body = { ...boardPresetDraft, name, exposed };
  // 같은 이름이 이미 있으면 덮어쓸지 묻는다(내용 전체를 새로 저장).
  const same = boardPresetList.find(p => p.name === name);
  if(same && !confirm(`'${name}' 프리셋이 이미 있어요. 지금 설정으로 덮어쓸까요?`)) return;
  const res = await fetch(same ? `/api/board-presets/${same.id}` : '/api/board-presets', {
    method: same ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).catch(() => null);
  if(!res || !res.ok){
    const data = res ? await res.json().catch(() => ({})) : {};
    errorEl.textContent = data.detail || '프리셋을 저장하지 못했어요.';
    return;
  }
  closeBoardPresetModal();
  document.getElementById('load-notice').textContent = `보드 프리셋 '${name}'을(를) 저장했어요.`;
}

function closeBoardPresetModal(){ document.getElementById('board-preset-modal').style.display = 'none'; }
document.getElementById('save-board-preset-btn').addEventListener('click', openBoardPresetSave);
document.getElementById('board-preset-save').addEventListener('click', saveBoardPreset);
document.getElementById('board-preset-cancel').addEventListener('click', closeBoardPresetModal);
document.getElementById('board-preset-close').addEventListener('click', closeBoardPresetModal);
document.getElementById('board-preset-modal').addEventListener('click', (e) => {
  if(e.target.id === 'board-preset-modal') closeBoardPresetModal();
});
document.getElementById('board-preset-name').addEventListener('keydown', (e) => {
  if(e.key === 'Enter'){ e.preventDefault(); saveBoardPreset(); }
});
document.getElementById('board-preset-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-preset-del]');
  if(!btn) return;
  const preset = boardPresetList.find(p => p.id === Number(btn.dataset.presetDel));
  if(!preset || !confirm(`'${preset.name}' 프리셋을 지울까요? 보드에 이미 놓은 카드는 그대로 남아요.`)) return;
  await fetch(`/api/board-presets/${preset.id}`, { method: 'DELETE' }).catch(() => {});
  refreshBoardPresetList();
});

