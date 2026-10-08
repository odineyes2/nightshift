// ---- 백틱 빠른 실행창 (NS-52, DESIGN.md 8절 "빠른 입력창") ----
// 백틱으로 열고, 입력칸 밖 백틱이나 Esc로 닫는다(초안·첨부는 남는다). Enter 한 번이 작업 하나 —
// POST /api/quick-run이 프리셋을 분류해 템플릿·옵션을 정하고 파드 없이 대기 큐(queued)에 넣는다.
// 보내면 GPU 비용이 들 수 있으니 도구 줄에 프로젝트·프리셋을 늘 글자로 보인다.
const composerEl = document.getElementById('composer');
const cmpInput = document.getElementById('cmp-input');
const cmpFilesEl = document.getElementById('cmp-files');
const cmpErrorEl = document.getElementById('cmp-error');
const cmpProjectSel = document.getElementById('cmp-project');
const cmpPresetBtn = document.getElementById('cmp-preset');
const cmpPresetMenu = document.getElementById('cmp-preset-menu');
const cmpFileInput = document.getElementById('cmp-file');
const cmpSendBtn = document.getElementById('cmp-send');

let cmpPresets = [];                                   // GET /api/quick-run/presets
let cmpPresetId = localStorage.getItem('cmp-preset') || null;
let cmpAttachments = [];                               // {key, filename, name, state: 'uploading'|'done'|'failed'}
let cmpLibrary = null;                                 // 라이브러리 칩(NS-52-3) — {kind, mode, image, tags, name}
let cmpSaving = false;
let cmpServerError = '';
let cmpAttachSeq = 0;
let cmpNewWorkflow = null;                             // (새로 만들기)로 마법사에서 만든 것 — {id:'__new__', filename, workflow, ...분류}

function cmpCurrentPreset(){
  if(cmpPresetId === '__new__') return cmpNewWorkflow;
  return cmpPresets.find(p => p.id === cmpPresetId) || null;
}

// 서버 quick_run.classify_workflow와 같은 규칙 — 마법사에서 막 만든 워크플로우는 아직 최근 목록에 없어서
// 보내기 전 안내에만 쓴다. 보낼 때는 서버가 다시 분류·검사한다.
function cmpClassify(wf){
  const nodes = Object.entries(wf || {}).filter(([, n]) => n && typeof n === 'object');
  const classes = new Set(nodes.map(([, n]) => n.class_type));
  const consumers = id => nodes.filter(([, n]) => Object.values(n.inputs || {})
    .some(v => Array.isArray(v) && String(v[0]) === String(id))).map(([, n]) => n.class_type);
  const inputs = nodes.filter(([k, n]) => n.class_type === 'LoadImage'
    && !(consumers(k).length && consumers(k).every(c => c === 'DWPreprocessor')));
  const r = (kind, slots, unsupported = null) => ({ kind, image_slots: slots, openpose: classes.has('DWPreprocessor'), unsupported });
  if(classes.has('WanFirstLastFrameToVideo')) return r('i2v', ['start_image', 'end_image']);
  if(classes.has('WanImageToVideo')) return r('i2v', ['start_image']);
  if(classes.has('MiniMaxH3ImageToVideo')){
    const titles = new Set(inputs.map(([, n]) => (n._meta || {}).title));
    let slots = ['first_frame_image', 'last_frame_image'].filter(s => titles.has(s));
    if(!slots.length && inputs.length) slots = ['first_frame_image'];
    return r(slots.length ? 'i2v' : 't2v', slots);
  }
  if([...classes].some(c => String(c).toLowerCase().includes('video'))) return r(inputs.length ? 'i2v' : 't2v', [], '빠른 실행에서는 아직 못 써요');
  return inputs.length ? r('i2i', ['input_image']) : r('t2i', []);
}

// 마법사 "다음"(06-wizard.js goToNewJobDetails)이 quickRunReturn일 때 부른다 — 만든 워크플로우를 고른 채 창으로 돌아온다.
async function cmpUseNewWorkflow(file){
  const workflow = JSON.parse(await file.text());
  cmpNewWorkflow = { id: '__new__', filename: file.name, workflow, ...cmpClassify(workflow) };
  cmpPresetId = '__new__';
  cmpServerError = '';
  openComposer();
}

// 다른 모달·라이트박스가 열려 있으면 백틱으로 열지 않는다(그 화면의 키와 겹치지 않게).
function cmpOtherOverlayOpen(){
  return Array.from(document.querySelectorAll('.modal-overlay, .lightbox-overlay'))
    .some(el => el.style.display && el.style.display !== 'none');
}

function cmpIsEditable(el){
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}

function openComposer(){
  composerEl.hidden = false;
  cmpRenderProjects();
  cmpLoadPresets();
  cmpAutosize();
  cmpInput.focus();
}

function closeComposer(){
  cmpClosePresetMenu();
  cmpCloseLibraryMenu();
  composerEl.hidden = true;
  if(composerEl.contains(document.activeElement)) document.activeElement.blur();
}

// 프로젝트 — 프로젝트 서브탭에서 열면 그 프로젝트, 아니면 마지막에 고른 것. 고른 값은 다음에도 유지한다.
function cmpRenderProjects(){
  const live = projectsCache.filter(p => !p.archived);
  const remembered = localStorage.getItem('cmp-project') || 'unassigned';
  const want = typeof currentProjectId === 'number' ? String(currentProjectId) : remembered;
  cmpProjectSel.innerHTML = [`<option value="unassigned">${escapeHtml(UNASSIGNED_LABEL)}</option>`]
    .concat(live.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`)).join('');
  cmpProjectSel.value = live.some(p => String(p.id) === want) ? want : 'unassigned';
  cmpProjectSel.title = '프로젝트: ' + cmpProjectSel.selectedOptions[0].textContent;
}

async function cmpLoadPresets(){
  try{
    const res = await fetch('/api/quick-run/presets');
    if(res.ok) cmpPresets = (await res.json()).presets || [];
  }catch(e){
    // 못 받아오면 이전 목록을 그대로 둔다.
  }
  if(!cmpCurrentPreset()) cmpPresetId = (cmpPresets.find(p => !p.unsupported) || {}).id || null;
  cmpRenderPreset();
}

function cmpPresetLabel(p){ return p.filename.replace(/\.json$/i, ''); }

function cmpRenderPreset(){
  const p = cmpCurrentPreset();
  const label = x => x.id === '__new__' ? `새 워크플로우: ${cmpPresetLabel(x)}` : cmpPresetLabel(x);
  cmpPresetBtn.textContent = p ? `${label(p)} · ${p.kind || '?'}` : '프리셋 고르기';
  cmpPresetBtn.title = p ? `워크플로우 프리셋: ${p.filename} (${p.kind || '?'})` : '워크플로우 프리셋';
  const list = (cmpNewWorkflow ? [cmpNewWorkflow] : []).concat(cmpPresets);
  cmpPresetMenu.innerHTML = (list.length ? list.map(x => `
    <button type="button" role="menuitemradio" aria-checked="${x.id === cmpPresetId}" data-preset="${escapeHtml(x.id)}"
      ${x.unsupported ? `disabled title="${escapeHtml(x.unsupported)}"` : `title="${escapeHtml(x.filename)}"`}>
      <svg class="ico cmp-check"><use href="#i-check"/></svg>
      <span class="cmp-name">${escapeHtml(label(x))}</span>
      <span class="cmp-kind">${x.unsupported ? '못 써요' : escapeHtml(x.kind || '')}</span>
    </button>`).join('')
    : '<div class="cmp-empty">최근 워크플로우가 없어요 — 새 작업을 한 번 보내면 여기에 나와요.</div>')
    + `<hr class="cmp-sep"><button type="button" role="menuitem" data-new-workflow>
      <svg class="ico cmp-check"><use href="#i-check"/></svg><span class="cmp-name">(새로 만들기)</span></button>`;
  cmpUpdate();
}

function cmpOpenPresetMenu(){
  cmpPresetMenu.hidden = false;
  cmpPresetBtn.setAttribute('aria-expanded', 'true');
  (cmpPresetMenu.querySelector('[aria-checked="true"]') || cmpPresetMenu.querySelector('button:not(:disabled)'))?.focus();
}
function cmpClosePresetMenu(){
  cmpPresetMenu.hidden = true;
  cmpPresetBtn.setAttribute('aria-expanded', 'false');
}

function cmpRenderFiles(unusedFrom, libraryUnused){
  const L = cmpLibrary;
  const libText = L && `${L.kind === 'position' ? 'Position' : 'Pose'}: ${L.name} · ${L.mode === 'openpose' ? 'Openpose CN' : '프롬프트'}`;
  cmpFilesEl.innerHTML = (L ? `<span class="cmp-chip${libraryUnused ? ' unused' : ''}">${escapeHtml(libText)}
      <button type="button" data-cmp-library-remove title="라이브러리 빼기" aria-label="${escapeHtml(libText)} 빼기"><svg class="ico"><use href="#i-x"/></svg></button></span>` : '')
    + cmpAttachments.map((a, i) => {
    const suffix = a.state === 'uploading' ? ' · 업로드 중…' : a.state === 'failed' ? ' · 실패' : '';
    const cls = a.state === 'failed' ? ' bad' : (a.state === 'done' && i >= unusedFrom ? ' unused' : '');
    return `<span class="cmp-chip${cls}">${escapeHtml(a.filename)}${suffix}
      <button type="button" data-cmp-remove="${a.key}" title="첨부 빼기" aria-label="${escapeHtml(a.filename)} 첨부 빼기"><svg class="ico"><use href="#i-x"/></svg></button></span>`;
  }).join('');
}

// 보낼 수 있는지 판단하고, 막는 이유·쓰이지 않는 첨부를 오류 줄에 한 줄로 적는다(조용히 버리지 않는다).
function cmpUpdate(){
  const p = cmpCurrentPreset();
  const slots = p && p.image_slots ? p.image_slots.length : 0;
  const done = cmpAttachments.filter(a => a.state === 'done');
  let block = '';
  let note = '';
  if(p && p.unsupported) block = p.unsupported;
  else if(p && done.length < slots) block = `이 프리셋은 이미지 ${slots}장이 필요해요 — + 로 첨부해 주세요.`;
  else if(p && p.openpose && !(cmpLibrary && cmpLibrary.mode === 'openpose'))
    block = '이 프리셋은 포즈 이미지가 필요해요 — 라이브러리에서 Pose를 Openpose CN으로 골라 주세요.';
  if(p && done.length > slots) note = slots ? `이 프리셋은 이미지를 ${slots}장만 써요 — 나머지 첨부는 쓰지 않아요.` : '이 프리셋은 이미지를 쓰지 않아요.';
  // NS-51처럼 OpenPose를 못 쓰는 프리셋이면 포즈·태그를 모두 적용하지 않는다(서버도 같은 규칙).
  const poseUnused = !!(p && cmpLibrary && cmpLibrary.mode === 'openpose' && !p.openpose);
  if(poseUnused) note = '이 프리셋은 Openpose CN을 쓸 수 없어 포즈 방식이 적용되지 않아요.' + (note ? ' ' + note : '');
  cmpRenderFiles(p ? slots : Infinity, poseUnused);
  cmpErrorEl.textContent = cmpServerError || block || note;
  const busy = cmpAttachments.some(a => a.state !== 'done');
  cmpSendBtn.disabled = cmpSaving || !p || !!block || busy || !cmpInput.value.trim();
}

function cmpAutosize(){
  cmpInput.style.height = 'auto';
  cmpInput.style.height = cmpInput.scrollHeight + 'px';
}

async function cmpUpload(file){
  const a = { key: String(++cmpAttachSeq), filename: file.name, name: null, state: 'uploading' };
  cmpAttachments.push(a);
  cmpUpdate();
  try{
    const fd = new FormData();
    fd.append('image', file);
    const res = await fetch('/api/input-images', { method: 'POST', body: fd });
    if(!res.ok) throw new Error();
    a.name = (await res.json()).name;
    a.state = 'done';
  }catch(e){
    a.state = 'failed';
  }
  cmpUpdate();
}

async function cmpSend(){
  if(cmpSendBtn.disabled) return;
  const p = cmpCurrentPreset();
  cmpSaving = true;
  cmpServerError = '';
  cmpUpdate();
  try{
    const res = await fetch('/api/quick-run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...(p.id === '__new__' ? { workflow: p.workflow, workflow_filename: p.filename } : { workflow_id: p.id }),
        prompt: cmpInput.value,
        project_id: cmpProjectSel.value === 'unassigned' ? null : Number(cmpProjectSel.value),
        images: cmpAttachments.map(a => a.name),
        library: cmpLibrary,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `서버 오류 (${res.status})`);
    cmpInput.value = '';
    cmpAttachments = [];
    cmpLibrary = null;
    cmpRenderLibraryBtn();
    if(p.id === '__new__'){ cmpNewWorkflow = null; cmpPresetId = null; }   // 이제 최근 목록 맨 앞에 있다 — 다시 받으면 그것이 골라진다
    cmpAutosize();
    flashNotice(`작업을 대기열에 넣었어요 (${data.job && data.job.id})` + (data.pose_skipped ? ' — 포즈 방식은 적용하지 않았어요' : ''));
    fetchJobs();
    fetchProjects();
    cmpLoadPresets();
  }catch(e){
    cmpServerError = `${e.message} — 다시 보내 주세요.`;
  }finally{
    cmpSaving = false;
    cmpUpdate();
  }
}

document.addEventListener('keydown', e => {
  if(e.key !== '`' || e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
  if(composerEl.hidden){
    if(cmpIsEditable(e.target) || cmpOtherOverlayOpen()) return;
    e.preventDefault();
    openComposer();
  }else if(e.target !== cmpInput && !cmpOtherOverlayOpen() && (!cmpIsEditable(e.target) || composerEl.contains(e.target))){
    e.preventDefault();
    closeComposer();
  }
});

composerEl.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  e.stopPropagation();
  if(!cmpPresetMenu.hidden){ cmpClosePresetMenu(); cmpPresetBtn.focus(); }
  else if(!cmpLibraryMenu.hidden){ cmpCloseLibraryMenu(); cmpLibraryBtn.focus(); }
  else closeComposer();
});

cmpInput.addEventListener('keydown', e => {
  // 한글 조합 중 Enter는 보내지 않는다 — 보내면 마지막 글자가 두 번 들어간다.
  if(e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  cmpSend();
});
cmpInput.addEventListener('input', () => { cmpServerError = ''; cmpAutosize(); cmpUpdate(); });

cmpProjectSel.addEventListener('change', () => {
  localStorage.setItem('cmp-project', cmpProjectSel.value);
  cmpProjectSel.title = '프로젝트: ' + cmpProjectSel.selectedOptions[0].textContent;
});

cmpPresetBtn.addEventListener('click', () => cmpPresetMenu.hidden ? cmpOpenPresetMenu() : cmpClosePresetMenu());
cmpPresetMenu.addEventListener('click', e => {
  if(e.target.closest('[data-new-workflow]')){
    // (새로 만들기) — 마법사를 열고, "다음"을 누르면 새 작업 폼 대신 이 창으로 돌아온다(quickRunReturn).
    cmpClosePresetMenu();
    closeComposer();
    openNewJobModal();
    resetWizardAndWorkflow();
    quickRunReturn = true;
    return;
  }
  const b = e.target.closest('[data-preset]');
  if(!b || b.disabled) return;
  cmpPresetId = b.dataset.preset;
  localStorage.setItem('cmp-preset', cmpPresetId);
  cmpServerError = '';
  cmpClosePresetMenu();
  cmpRenderPreset();
  cmpInput.focus();
});
document.addEventListener('click', e => {
  if(!cmpPresetMenu.hidden && !e.target.closest('.cmp-menu-wrap')) cmpClosePresetMenu();
  if(!cmpLibraryMenu.hidden && !e.target.closest('.cmp-menu-wrap')) cmpCloseLibraryMenu();
});
// 마법사를 "다음" 없이 닫으면 돌아오기 표지를 지운다(다른 진입점의 마법사가 창으로 새지 않게).
document.getElementById('new-job-modal-close').addEventListener('click', () => { quickRunReturn = false; });
document.getElementById('new-job-modal').addEventListener('click', e => { if(e.target.id === 'new-job-modal') quickRunReturn = false; });

// ---- 라이브러리 프리셋(NS-52-3) — Pose/Position 갤러리에서 한 장 → NS-51 두 버튼 모달 → 칩 ----
// 실제 대기열 추가는 Enter/보내기에서만. 외부 이미지·URL과 순차 생성(여러 포즈)은 이번 범위가 아니다.
const cmpLibraryBtn = document.getElementById('cmp-library');
const cmpLibraryMenu = document.getElementById('cmp-library-menu');
const cmpLibraryModal = document.getElementById('cmp-library-modal');

function cmpRenderLibraryBtn(){
  const L = cmpLibrary;
  cmpLibraryBtn.textContent = L ? (L.kind === 'position' ? 'Position' : 'Pose') : '라이브러리';
  cmpLibraryBtn.title = L ? `라이브러리: ${L.name}` : '라이브러리 프리셋 (없음)';
  cmpLibraryMenu.querySelectorAll('[data-library]').forEach(b =>
    b.setAttribute('aria-checked', String((L ? L.kind : '') === b.dataset.library)));
}
function cmpCloseLibraryMenu(){
  cmpLibraryMenu.hidden = true;
  cmpLibraryBtn.setAttribute('aria-expanded', 'false');
}
cmpLibraryBtn.addEventListener('click', () => {
  if(!cmpLibraryMenu.hidden) return cmpCloseLibraryMenu();
  cmpRenderLibraryBtn();
  cmpLibraryMenu.hidden = false;
  cmpLibraryBtn.setAttribute('aria-expanded', 'true');
  cmpLibraryMenu.querySelector('[aria-checked="true"]')?.focus();
});
cmpLibraryMenu.addEventListener('click', e => {
  const b = e.target.closest('[data-library]');
  if(!b) return;
  cmpCloseLibraryMenu();
  if(!b.dataset.library){ cmpSetLibrary(null); return cmpInput.focus(); }
  cmpOpenLibraryPicker(b.dataset.library);
});

function cmpSetLibrary(lib){
  cmpLibrary = lib;
  cmpServerError = '';
  cmpRenderLibraryBtn();
  cmpUpdate();
}

async function cmpOpenLibraryPicker(kind){
  const err = document.getElementById('cmp-library-error');
  const grid = document.getElementById('cmp-library-grid');
  document.getElementById('cmp-library-title').textContent = kind === 'position' ? 'Position 고르기' : 'Pose 고르기';
  err.textContent = '';
  grid.innerHTML = '<div class="dl-meta">불러오는 중…</div>';
  cmpLibraryModal.style.display = 'flex';
  try{
    const res = await fetch(LIBRARY_API[kind]);
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `불러오지 못했어요 (${res.status})`);
    const posts = data.items || [];
    grid.innerHTML = posts.flatMap(p => p.images.map((im, i) => `
      <button type="button" class="gallery-item" data-post="${p.id}" data-image="${im.id}" title="${escapeHtml(p.name)} ${i + 1}">
        <img src="${escapeHtml(im.thumb_url)}" alt="${escapeHtml(p.name)} ${i + 1}" loading="lazy"></button>`)).join('')
      || `<div class="dl-meta">아직 ${kind === 'position' ? 'Position' : '포즈'}가 없어요 — Library 탭에서 먼저 올려 주세요.</div>`;
    grid.onclick = e => {
      const b = e.target.closest('[data-image]');
      const post = b && posts.find(p => p.id === Number(b.dataset.post));
      if(!post) return;
      cmpCloseLibraryPicker();
      askLibraryTransfer(mode => cmpPickLibrary(kind, post, Number(b.dataset.image), mode));
    };
  }catch(e){ grid.innerHTML = ''; err.textContent = e.message; }
}
function cmpCloseLibraryPicker(){ cmpLibraryModal.style.display = 'none'; }
document.getElementById('cmp-library-close').addEventListener('click', cmpCloseLibraryPicker);
cmpLibraryModal.addEventListener('click', e => { if(e.target === cmpLibraryModal) cmpCloseLibraryPicker(); });
cmpLibraryModal.addEventListener('keydown', e => { if(e.key === 'Escape') cmpCloseLibraryPicker(); });

// "프롬프트"면 복사 없이 태그만, "Openpose CN"이면 그 장을 입력 풀로 복사해 pose_image로 쓴다(NS-51과 같은 API).
async function cmpPickLibrary(kind, post, imageId, mode){
  try{
    const data = mode === 'openpose'
      ? await libraryToInput(`/api/library/${kind === 'position' ? 'positions' : 'poses'}/${post.id}/images/${imageId}/to-input`)
      : null;
    cmpSetLibrary({ kind, mode, image: data ? data.name : null, tags: post.danbooru_prompt || '', name: post.name });
  }catch(e){
    cmpServerError = e.message;
    cmpUpdate();
  }
  cmpInput.focus();
}

cmpFilesEl.addEventListener('click', e => {
  if(e.target.closest('[data-cmp-library-remove]')){ cmpSetLibrary(null); cmpInput.focus(); }
});

document.getElementById('cmp-attach').addEventListener('click', () => cmpFileInput.click());
cmpFileInput.addEventListener('change', () => {
  Array.from(cmpFileInput.files).forEach(cmpUpload);
  cmpFileInput.value = '';
});
cmpFilesEl.addEventListener('click', e => {
  const b = e.target.closest('[data-cmp-remove]');
  if(!b) return;
  // 입력 이미지 풀은 다른 작업도 같이 쓰는 곳이라 빼기는 이 창에서만 뺀다(서버 파일은 그대로).
  cmpAttachments = cmpAttachments.filter(a => a.key !== b.dataset.cmpRemove);
  cmpServerError = '';
  cmpUpdate();
});
