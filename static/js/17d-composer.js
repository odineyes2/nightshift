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

function cmpCurrentPreset(){ return cmpPresets.find(p => p.id === cmpPresetId) || null; }

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
  cmpPresetBtn.textContent = p ? `${cmpPresetLabel(p)} · ${p.kind || '?'}` : '프리셋 고르기';
  cmpPresetBtn.title = p ? `워크플로우 프리셋: ${p.filename} (${p.kind || '?'})` : '워크플로우 프리셋';
  cmpPresetMenu.innerHTML = cmpPresets.length ? cmpPresets.map(x => `
    <button type="button" role="menuitemradio" aria-checked="${x.id === cmpPresetId}" data-preset="${escapeHtml(x.id)}"
      ${x.unsupported ? `disabled title="${escapeHtml(x.unsupported)}"` : `title="${escapeHtml(x.filename)}"`}>
      <svg class="ico cmp-check"><use href="#i-check"/></svg>
      <span class="cmp-name">${escapeHtml(cmpPresetLabel(x))}</span>
      <span class="cmp-kind">${x.unsupported ? '못 써요' : escapeHtml(x.kind || '')}</span>
    </button>`).join('')
    : '<div class="cmp-empty">최근 워크플로우가 없어요 — 새 작업을 한 번 보내면 여기에 나와요.</div>';
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

function cmpRenderFiles(unusedFrom){
  cmpFilesEl.innerHTML = cmpAttachments.map((a, i) => {
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
  cmpRenderFiles(p ? slots : Infinity);
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
        workflow_id: p.id,
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
  }else if(e.target !== cmpInput && (!cmpIsEditable(e.target) || composerEl.contains(e.target))){
    e.preventDefault();
    closeComposer();
  }
});

composerEl.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  e.stopPropagation();
  if(!cmpPresetMenu.hidden){ cmpClosePresetMenu(); cmpPresetBtn.focus(); }
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
