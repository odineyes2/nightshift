// ---- "모델" 탭(전역) — 모델 등록부: 파드와 무관한 기준 데이터 ----
// 서버의 models 테이블이 원본이다. (종류, 파일명)마다 베이스 모델·페이지 주소·다운로드 주소·트리거 키워드(LoRA)·
// 태그·메모를 적어 두고, 어느 파드에서든 이 정보를 보고 모델을 받거나 트리거를 쓴다. 파일이 실제로 놓이는 곳만
// 파드라서, 워커마다 무엇이 설치돼 있는지는 여기서 보지 않고 각 워커의 Models 탭(08b-worker-models.js)에서 본다 —
// 이 탭은 등록부(순수 모델 정보)만 다룬다. modelInventory는 새 작업 폼·마법사가 "어느 워커에 있나"를 볼 때만 쓴다.
let modelRegistry = { kinds: [], base_models: [], items: {} };
let modelKindActive = 'checkpoints';
let modelExpandedKey = null;
// 지금 펼쳐진 편집 패널의 임시 값(저장 버튼을 눌러야 서버로 나간다) — 파일명도 포함해서
// 여기서만 고친다(원본은 modelRegistry.items). 행을 접거나 다른 행/종류로 옮기면 버린다.
let modelEditDraft = null;

function modelKey(kind, name){ return `${kind}\u0000${name}`; }

// 서버의 model_registry.base_id와 같은 규칙 — 마법사가 LoRA를 걸러낼 때 체크포인트 그룹 id와 맞춰 본다.
function baseIdOf(v){ return String(v || '').trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^[-.]+|[-.]+$/g, ''); }

// 등록부 항목의 소속 베이스 모델 목록(NS-38 — 여럿일 수 있다). base_models가 없는 옛 응답은 base_model 하나로 본다.
function basesOf(e){ return e.base_models || (e.base_model ? [e.base_model] : []); }
function basesText(e){ return basesOf(e).join(', '); }

function rebuildLoraTriggersFromRegistry(){
  const out = {};
  for(const e of Object.values(modelRegistry.items)){
    if(e.kind === 'loras' && (e.trigger_keyword || basesOf(e).length)){
      const ids = basesOf(e).map(baseIdOf);
      out[e.filename] = { trigger: e.trigger_keyword || '', base_id: ids[0] || '', base_ids: ids };
    }
  }
  loraTriggers = out;
}

async function fetchModelRegistry(){
  try{
    const res = await fetch('/api/models');
    if(!res.ok) return;
    const data = await res.json();
    modelRegistry.kinds = data.kinds || [];
    modelRegistry.base_models = data.base_models || [];
    modelRegistry.base_model_usage = data.base_model_usage || [];
    modelRegistry.items = {};
    for(const e of data.items || []) modelRegistry.items[modelKey(e.kind, e.filename)] = e;
    rebuildLoraTriggersFromRegistry();
  }catch(e){
    // 못 받아와도 목록만 메타데이터 없이 보일 뿐이다
  }
}

let modelUsage = {};   // modelKey -> {count, images, videos, favorites, last_used, samples}
let modelSort = 'name';
let modelBaseFilter = '';

async function fetchModelUsage(){
  try{
    const res = await fetch('/api/models/usage');
    if(!res.ok) return;
    modelUsage = {};
    for(const u of (await res.json()).usage || []) modelUsage[modelKey(u.kind, u.filename)] = u;
  }catch(e){ /* 통계가 안 보일 뿐이다 */ }
}

function usageOf(kind, name){ return modelUsage[modelKey(kind, name)] || null; }

function openGalleryForModel(name, mediaKind){
  const isVideo = mediaKind === 'video';
  const filters = isVideo ? videoGalleryFilters : galleryFilters;
  const prefix = isVideo ? 'video' : 'image';
  Object.assign(filters, { q: '', favorite: false, tag: '', minRating: 0, model: name });
  document.getElementById(`gf-${prefix}-q`).value = '';
  document.getElementById(`gf-${prefix}-tag`).value = '';
  document.getElementById(`gf-${prefix}-rating`).value = '0';
  const fav = document.getElementById(`gf-${prefix}-fav`);
  fav.classList.remove('on'); fav.setAttribute('aria-pressed', 'false');
  galleryFilterSyncs[prefix]();
  // location.hash를 직접 대입하면(예전 코드) history 기록이 하나 더 쌓인다 — 이 앱의
  // 다른 모든 화면 전환은 showTab()이 history.replaceState로만 주소를 바꿔서 기록이
  // 안 쌓이는데, 여기만 예외였다. 설치된 앱(standalone)에서는 "뒤로 가기로 종료" 가드
  // (setupBackToExit)가 기록 쌓임에 민감해서, 이 버튼을 누르면 정작 뒤로 가기를 누르지
  // 않았는데도 "한 번 더 누르면 앱이 종료돼요" 안내가 뜨는 버그가 있었다.
  showTab(isVideo ? 'video-gallery' : 'gallery');
}

function usageHtml(kind, name){
  const u = usageOf(kind, name);
  if(!u) return '<div class="model-usage"><div class="comfy-model-empty">이 모델로 만든 결과물(메타가 남아 있는 것)이 아직 없어요.</div></div>';
  const thumbs = (u.samples || []).map(sm => sm.kind === 'video'
    ? `<video class="model-sample" muted preload="metadata" src="${window.__nightshiftMediaUrl(`/api/output-videos/${encodeURI(sm.path)}`)}#t=0.1"></video>`
    : `<img class="model-sample" loading="lazy" alt="" src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(sm.path)}/thumbnail?size=200&fit=cover`)}">`).join('');
  const btns = [];
  if(u.images) btns.push(`<button type="button" class="load-btn model-usage-gallery-btn" data-media="image" data-name="${escapeHtml(name)}">이미지 ${u.images}장 보기</button>`);
  if(u.videos) btns.push(`<button type="button" class="load-btn model-usage-gallery-btn" data-media="video" data-name="${escapeHtml(name)}">영상 ${u.videos}개 보기</button>`);
  return `<div class="model-usage">
    <div class="model-usage-stats">사용 ${u.count}회 · 즐겨찾기 ${u.favorites} · 마지막 ${fmtTime(u.last_used)}</div>
    <div class="model-samples">${thumbs}</div>
    <div class="model-usage-actions">${btns.join('')}</div>
  </div>`;
}

function modelEntry(kind, name){
  return modelRegistry.items[modelKey(kind, name)]
    || { kind, filename: name, base_model: '', base_models: [], notes: '', tags: [], trigger_keyword: '', page_url: '', download_url: '' };
}

// 베이스 모델은 자유 입력이 아니라 서버가 정해 둔 기준 목록(modelRegistry.base_models, DB base_models 표)에서만
// 고른다 — 대소문자가 미묘하게 다른 값이 새로 생겨 필터가 못 알아보는 일이 없도록. 한 모델이 여러 베이스 모델에
// 속할 수 있어(예: Anima·Qwen-Image가 같은 VAE를 씀) 체크박스로 여럿 고른다. 아무것도 안 고르면 "선택 안 함"이다.
// selected 중 목록에 없는 값(예전에 API로 직접 넣은 값 등)은 지우지 않고 맨 끝에 얹어 둔다.
function baseModelChecks(selected, dis){
  const canon = modelRegistry.base_models || [];
  const on = new Set((selected || []).map(b => b.toLowerCase()));
  const extra = (selected || []).filter(b => !canon.some(c => c.toLowerCase() === b.toLowerCase()));
  const box = (b, label) => `<label class="model-base-pick"><input type="checkbox" value="${escapeHtml(b)}"${on.has(b.toLowerCase()) ? ' checked' : ''} ${dis || ''}>${escapeHtml(label)}</label>`;
  const boxes = canon.map(b => box(b, b)).concat(extra.map(b => box(b, `${b} (목록에 없음)`))).join('');
  return `<div class="model-base-picks" data-field="base_models">${boxes || '<span class="dim">베이스 모델 목록이 비어 있어요.</span>'}</div>`;
}

function checkedBases(el){ return [...el.querySelectorAll('input[type="checkbox"]:checked')].map(i => i.value); }

function setModelError(text){ document.getElementById('lora-tab-error').textContent = text || ''; }

async function putModelEntry(payload){
  const res = await fetch('/api/models', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.detail || '저장하지 못했어요.');
  const key = modelKey(payload.kind, payload.filename);
  if(data.entry) modelRegistry.items[key] = data.entry; else delete modelRegistry.items[key];
  rebuildLoraTriggersFromRegistry();
  return data.entry;
}

// ---- 편집 패널 임시 값(저장 버튼을 눌러야 반영) ----
function modelDraftFromEntry(kind, name){
  const e = modelEntry(kind, name);
  return { origKind: kind, kind, origName: name, filename: name, base_models: [...basesOf(e)], notes: e.notes,
           tags: [...(e.tags || [])], trigger_keyword: e.trigger_keyword, page_url: e.page_url, download_url: e.download_url };
}

function modelDraftDirty(){
  if(!modelEditDraft) return false;
  return JSON.stringify(modelEditDraft) !== JSON.stringify(modelDraftFromEntry(modelEditDraft.origKind, modelEditDraft.origName));
}

// 지금 열려 있는 편집 패널에 저장하지 않은 변경이 있으면 한 번 확인한다 — 행을 접거나
// 다른 행/종류로 옮기기 전에 부른다. 확인 없이 넘어가도 되면 true를 돌려준다.
function confirmDiscardDraft(){
  if(!modelDraftDirty()) return true;
  return confirm('저장하지 않은 변경 내용이 있어요. 저장하지 않고 닫을까요?');
}

async function saveModelDraft(){
  const d = modelEditDraft;
  if(!d) return;
  const newName = d.filename.trim();
  if(!newName){ setModelError('파일명을 입력하세요.'); return; }
  const moved = d.kind !== d.origKind || newName !== d.origName;
  if(moved && modelRegistry.items[modelKey(d.kind, newName)]){
    setModelError('이미 등록된 항목이에요(종류+파일명이 같음).');
    return;
  }
  const fields = { base_models: d.base_models, notes: d.notes, tags: d.tags,
                    trigger_keyword: d.trigger_keyword, page_url: d.page_url, download_url: d.download_url };
  try{
    await putModelEntry({ kind: d.kind, filename: newName, ...fields });
    if(moved){
      await putModelEntry({ kind: d.origKind, filename: d.origName, base_models: [], notes: '', tags: [],
                             trigger_keyword: '', page_url: '', download_url: '' });
      modelKindActive = d.kind;   // 종류를 옮겼으면 그 종류 탭으로 같이 옮겨야 보인다
      modelExpandedKey = modelKey(d.kind, newName);
    }
    setModelError('');
    modelEditDraft = modelDraftFromEntry(d.kind, newName);
    renderModelRegistry();
    flashNotice('저장했어요.');
  }catch(err){
    setModelError(err.message || '저장하지 못했어요 — 연결을 확인하세요.');
  }
}

function isWebUrl(u){ return /^https?:\/\//i.test(u || ''); }

function modelThumbHtml(kind, name){
  const u = usageOf(kind, name);
  const sm = u && (u.samples || []).find(x => x.kind === 'image');
  return sm
    ? `<div class="model-thumb"><img loading="lazy" alt="" src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(sm.path)}/thumbnail?size=80&fit=cover`)}"></div>`
    : `<div class="model-thumb">${ico('boxes')}</div>`;
}

function modelHeaderHtml(isLora){
  return `<div class="model-cols${isLora ? ' lora' : ''} model-header">
    <span></span><span>파일명</span><span>베이스 모델</span>${isLora ? '<span>트리거 키워드</span>' : ''}<span>태그</span><span>사용</span><span>주소</span><span></span>
  </div>`;
}

function modelRowHtml(kind, name, isAdmin){
  const e = modelEntry(kind, name);
  const key = modelKey(kind, name);
  const open = modelExpandedKey === key;
  const isLora = kind === 'loras';
  const usage = usageOf(kind, name);
  const tagList = e.tags || [];
  const tagsHtml = tagList.length ? `<div class="gallery-details-col model-cell-tags">${tagList.map(t => `<span class="model-badge">${escapeHtml(t)}</span>`).join('')}</div>`
    : '<div class="gallery-details-col dim">-</div>';
  const links = e.download_url
    ? (isAdmin && kind !== 'custom_nodes'   // 노드팩은 파드를 시작할 때 설치된다 — 받기 명령이 없다
        ? `<button type="button" class="icon-btn icon-btn-neutral model-dlcmd-btn" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}" title="다운로드 명령 복사(RunPod 터미널용)">${ico('download')}</button>`
        : `<span title="다운로드 주소 등록됨">${ico('download')}</span>`)
    : '';
  const head = `
    <div class="model-cols${isLora ? ' lora' : ''} model-row-head" role="button" tabindex="0">
      ${modelThumbHtml(kind, name)}
      <div class="gallery-details-col name lora-tab-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
      <div class="gallery-details-col" title="${escapeHtml(basesText(e))}">${basesOf(e).length ? escapeHtml(basesText(e)) : '<span class="dim">-</span>'}</div>
      ${isLora ? `<div class="gallery-details-col" title="${escapeHtml(e.trigger_keyword)}">${e.trigger_keyword ? escapeHtml(e.trigger_keyword) : '<span class="dim">-</span>'}</div>` : ''}
      ${tagsHtml}
      <div class="gallery-details-col dim">${usage ? usage.count : '-'}</div>
      <div class="gallery-details-col dim">${links || '-'}</div>
      <svg class="ico model-chevron"><use href="#i-chevron-down"/></svg>
    </div>`;
  let editor = '';
  if(open){
    const d = modelEditDraft || modelDraftFromEntry(kind, name);
    const dis = isAdmin ? '' : 'disabled';
    const link = u => isWebUrl(u) ? `
      <button type="button" class="icon-btn icon-btn-neutral model-link" data-copy-url="${escapeHtml(u)}" title="주소 복사">${ico('copy')}</button>
      <button type="button" class="icon-btn icon-btn-neutral model-link-open" data-open-url="${escapeHtml(u)}" title="새 탭에서 열기">${ico('external-link')}</button>` : '';
    const dirty = modelDraftDirty();
    const kindOptions = modelRegistry.kinds.map(k => `<option value="${escapeHtml(k.id)}"${k.id === d.kind ? ' selected' : ''}>${escapeHtml(k.label)}</option>`).join('');
    editor = `
    <div class="model-editor" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}">
      ${usageHtml(kind, name)}
      <label class="model-field"><span>종류</span>
        <select class="option-input" data-field="kind" ${dis}>${kindOptions}</select></label>
      ${d.kind === 'custom_nodes' ? `<label class="model-field"><span>폴더 이름 <span style="opacity:.6">(custom_nodes 아래, 받을 주소는 https://github.com/소유자/저장소 — 파드를 시작할 때 설치돼요)</span></span>
        <input type="text" class="option-input" data-field="filename" ${dis} placeholder="예: ComfyUI-Impact-Pack" value="${escapeHtml(d.filename)}"></label>`
      : `<label class="model-field"><span>파일명 <span style="opacity:.6">(하위 폴더는 "폴더/이름.safetensors")</span></span>
        <input type="text" class="option-input" data-field="filename" ${dis} placeholder="${d.kind === 'ultralytics' ? '예: bbox/face_yolov8m.pt' : '예: mmh3/my_style_v2.safetensors'}" value="${escapeHtml(d.filename)}"></label>`}
      <div class="model-field"><span>베이스 모델 <span style="opacity:.6">(여럿 고를 수 있어요)</span></span>
        ${baseModelChecks(d.base_models, dis)}</div>
      ${isLora ? `<label class="model-field"><span>트리거 키워드</span>
        <input type="text" class="option-input" data-field="trigger_keyword" ${dis} placeholder="없으면 비워둠 — 워크플로우에서 고르면 프롬프트에 자동으로 붙어요" value="${escapeHtml(d.trigger_keyword)}"></label>` : ''}
      <label class="model-field"><span>페이지 주소${link(d.page_url)}</span>
        <input type="url" class="option-input" data-field="page_url" ${dis} placeholder="https://civitai.com/models/… 또는 https://huggingface.co/…" value="${escapeHtml(d.page_url)}"></label>
      <label class="model-field"><span>다운로드 주소${link(d.download_url)}</span>
        <input type="url" class="option-input" data-field="download_url" ${dis} placeholder="https://civitai.com/api/download/models/… 또는 https://huggingface.co/…/resolve/…" value="${escapeHtml(d.download_url)}"></label>
      <label class="model-field"><span>태그 (쉼표로 구분)</span>
        <input type="text" class="option-input" data-field="tags" ${dis} placeholder="예: 캐릭터, 스타일, 실사" value="${escapeHtml((d.tags || []).join(', '))}"></label>
      <label class="model-field wide"><span>메모</span>
        <textarea class="option-input" data-field="notes" rows="3" ${dis} placeholder="권장 가중치, 잘 어울리는 조합 등" style="max-width:none;">${escapeHtml(d.notes)}</textarea></label>
      <div class="email-hint model-full">워커마다 설치됐는지는 각 워커의 Models 탭에서 보고 받아요.</div>
      ${isAdmin ? `<div class="model-full model-editor-actions">
          <button type="button" class="submit-btn model-save-btn" ${dirty ? '' : 'disabled'}>저장</button>
          <button type="button" class="modal-btn-secondary model-clear-btn">등록 정보 지우기</button>
        </div>`
                : '<div class="email-hint model-full">등록 정보는 관리자만 고칠 수 있어요.</div>'}
    </div>`;
  }
  return `
  <div class="model-row${open ? ' open' : ''}" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}">
    ${head}
    ${editor}
  </div>`;
}

function renderModelRegistry(){
  const tabsEl = document.getElementById('model-kind-tabs');
  const listEl = document.getElementById('lora-tab-list');
  // 등록부에 적힌 모델만 — 워커에 설치만 돼 있고 등록 안 된 것은 워커의 Models 탭에서 "등록"으로 들여온다.
  const namesOf = kind => Object.values(modelRegistry.items).filter(e => e.kind === kind).map(e => e.filename)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  tabsEl.innerHTML = modelRegistry.kinds.map(k =>
    `<button type="button" class="enhance-mode-btn model-kind-tab${k.id === modelKindActive ? ' active' : ''}" data-kind="${escapeHtml(k.id)}">${escapeHtml(k.label)}<span class="model-kind-count">${namesOf(k.id).length}</span></button>`
  ).join('');
  const all = namesOf(modelKindActive);

  // 베이스 모델 드롭다운 — "베이스 모델"은 체크포인트냐 디퓨전 모델(UNet)이냐 같은
  // 구현 형태와 무관하게 사용자가 인식하는 기술 단위라, 지금 고른 종류 탭과 상관없이
  // 등록부 전체에서 실제로 쓰인 값만 보여준다(종류 탭을 바꿔도 안 바뀜). 대소문자
  // 차이는 같은 베이스 모델로 본다(전에 등록된 값이 기준 표기와 살짝 다를 수 있어서).
  const baseFilterEl = document.getElementById('model-base-filter');
  const usedBasesLower = new Set(
    Object.values(modelRegistry.items).flatMap(e => basesOf(e).map(b => b.trim().toLowerCase())).filter(Boolean));
  const bases = (modelRegistry.base_models || []).filter(b => usedBasesLower.has(b.toLowerCase()));
  if(modelBaseFilter && !bases.some(b => b.toLowerCase() === modelBaseFilter.toLowerCase())) modelBaseFilter = '';
  baseFilterEl.innerHTML = '<option value="">베이스 모델: 전체</option>'
    + bases.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('');
  baseFilterEl.value = modelBaseFilter;

  const filter = document.getElementById('lora-tab-filter').value.trim().toLowerCase();
  let shown = all.filter(n => {
    const e = modelEntry(modelKindActive, n);
    if(modelBaseFilter && !basesOf(e).some(b => b.toLowerCase() === modelBaseFilter.toLowerCase())) return false;
    if(!filter) return true;
    return (n + ' ' + basesText(e) + ' ' + (e.tags || []).join(' ') + ' ' + e.notes + ' ' + e.trigger_keyword + ' ' + e.page_url).toLowerCase().includes(filter);
  });
  if(modelSort !== 'name'){
    const val = n => {
      const u = usageOf(modelKindActive, n);
      return modelSort === 'count' ? (u ? u.count : 0) : (u && u.last_used ? Date.parse(u.last_used) : 0);
    };
    shown = [...shown].sort((a, b) => val(b) - val(a));
  }
  const filtered = !!(filter || modelBaseFilter);
  document.getElementById('model-count').textContent = `${filtered ? shown.length + ' / ' : ''}${all.length}개`;
  const notice = '';
  const addBtn = document.getElementById('model-add-toggle');
  if(addBtn) addBtn.style.display = isAdminUser() ? '' : 'none';
  const baseBtn = document.getElementById('base-model-manage-toggle');
  if(baseBtn) baseBtn.style.display = isAdminUser() ? '' : 'none';
  if(shown.length === 0){
    listEl.innerHTML = notice + `<div class="comfy-model-empty">${filtered && all.length > 0 ? '거른 결과가 없어요' : '이 종류의 모델이 없어요'}</div>`;
    return;
  }
  const isAdmin = isAdminUser();
  listEl.innerHTML = notice + `<div class="model-table">${modelHeaderHtml(modelKindActive === 'loras')}${shown.map(n => modelRowHtml(modelKindActive, n, isAdmin)).join('')}</div>`;
}

// ---- 모델 직접 추가 (파드에 아직 없어도 기준 데이터로 먼저 적어 둘 수 있다) ----
function renderModelAddForm(){
  const el = document.getElementById('model-add-form');
  const kind = modelKindActive;
  const kindLabel = (modelRegistry.kinds.find(k => k.id === kind) || {}).label || kind;
  el.innerHTML = `
    <div class="model-editor" style="border:1px solid var(--border); border-radius:var(--radius-sm); padding:12px;">
      <div class="dl-meta model-full">${escapeHtml(kindLabel)} 종류로 추가해요 — 위 종류 탭에서 바꿀 수 있어요.</div>
      <label class="model-field"><span>파일명 (ComfyUI에 놓일 이름, 하위 폴더는 "폴더/이름.safetensors")</span>
        <input type="text" class="option-input" id="model-add-filename" placeholder="예: my_style_v2.safetensors"></label>
      <label class="model-field"><span>페이지 주소</span><input type="url" class="option-input" id="model-add-page" placeholder="https://…"></label>
      <label class="model-field"><span>다운로드 주소</span><input type="url" class="option-input" id="model-add-download" placeholder="https://…"></label>
      <div class="model-field" id="model-add-base"><span>베이스 모델 <span style="opacity:.6">(여럿 고를 수 있어요)</span></span>${baseModelChecks([])}</div>
      ${kind === 'loras' ? '<label class="model-field"><span>트리거 키워드</span><input type="text" class="option-input" id="model-add-trigger"></label>' : ''}
      <div class="dl-row model-full" style="margin-top:6px;">
        <button type="button" class="submit-btn" id="model-add-save">추가</button>
        <button type="button" class="modal-btn-secondary" id="model-add-cancel">취소</button>
      </div>
    </div>`;
}

document.getElementById('model-add-toggle').addEventListener('click', () => {
  const el = document.getElementById('model-add-form');
  if(el.innerHTML.trim()){ el.innerHTML = ''; return; }
  renderModelAddForm();
});

document.getElementById('model-add-form').addEventListener('click', async (e) => {
  if(e.target.closest('#model-add-cancel')){ document.getElementById('model-add-form').innerHTML = ''; return; }
  if(!e.target.closest('#model-add-save')) return;
  const v = id => (document.getElementById(id) || { value: '' }).value.trim();
  const payload = { kind: modelKindActive, filename: v('model-add-filename'), page_url: v('model-add-page'),
                    download_url: v('model-add-download'), base_models: checkedBases(document.getElementById('model-add-base')) };
  if(modelKindActive === 'loras') payload.trigger_keyword = v('model-add-trigger');
  if(!payload.filename){ setModelError('파일명을 입력하세요.'); return; }
  if(!(payload.page_url || payload.download_url || payload.base_models.length || payload.trigger_keyword)){
    setModelError('페이지 주소·다운로드 주소·베이스 모델 중 하나는 적어야 등록돼요.'); return;
  }
  try{
    await putModelEntry(payload);
    setModelError('');
    document.getElementById('model-add-form').innerHTML = '';
    modelExpandedKey = modelKey(payload.kind, payload.filename);
    renderModelRegistry();
    flashNotice('등록했어요.');
  }catch(err){ setModelError(err.message); }
});

// ---- 베이스 모델 관리 (관리자) — 목록은 서버 DB(base_models)가 기준이다 ----
// 코드가 이름(식별자)에 기대는 베이스 모델 — 이름을 바꾸거나 지우면 전용 워크플로우·프리셋 연결이 끊길 수 있다.
const BASE_MODEL_CODE_BOUND = /^(krea\.2|minimax-h3|wan.*|illustrious|pony|noobai)$/i;

function renderBaseModelManage(){
  const el = document.getElementById('base-model-manage');
  const rows = (modelRegistry.base_model_usage || []).map(b => `
      <div class="dl-row model-full" data-name="${escapeHtml(b.name)}" style="flex-wrap:wrap;">
        <input type="text" class="option-input" value="${escapeHtml(b.name)}" maxlength="100" aria-label="베이스 모델 이름" style="flex:1 1 160px; min-width:0;">
        <span class="dl-meta">사용 ${b.count}개</span>
        <button type="button" class="modal-btn-secondary base-model-rename">저장</button>
        <button type="button" class="modal-btn-secondary base-model-delete" style="color:var(--failed);">삭제</button>
      </div>`).join('');
  el.innerHTML = `
    <div class="model-editor" style="border:1px solid var(--border); border-radius:var(--radius-sm); padding:12px;">
      <div class="dl-meta model-full">베이스 모델 목록이에요. 이름을 바꾸면 그 이름을 쓰는 모델도 함께 바뀌어요.</div>
      ${rows || '<div class="dl-meta model-full">아직 베이스 모델이 없어요</div>'}
      <div class="dl-row model-full" style="flex-wrap:wrap; margin-top:6px;">
        <input type="text" class="option-input" id="base-model-new" maxlength="100" placeholder="새 베이스 모델 이름 (예: Anima)" style="flex:1 1 160px; min-width:0;">
        <button type="button" class="submit-btn" id="base-model-add">추가</button>
        <button type="button" class="modal-btn-secondary" id="base-model-close">닫기</button>
      </div>
    </div>`;
}

// 저장 중에는 버튼을 막고, 끝나면 등록부를 다시 받아 편집 드롭다운·추가 폼·필터·패널을 새로 그린다.
async function baseModelRequest(btn, method, url, body){
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '저장 중…';
  try{
    const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
                                   body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setModelError('');
    await fetchModelRegistry();
    renderModelRegistry();
    if(document.getElementById('model-add-form').innerHTML.trim()) renderModelAddForm();
    renderBaseModelManage();
    return true;
  }catch(err){
    setModelError(err.message);
    btn.disabled = false; btn.textContent = label;
    return false;
  }
}

document.getElementById('base-model-manage-toggle').addEventListener('click', () => {
  const el = document.getElementById('base-model-manage');
  if(el.innerHTML.trim()){ el.innerHTML = ''; return; }
  renderBaseModelManage();
});

document.getElementById('base-model-manage').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if(!btn) return;
  if(btn.id === 'base-model-close'){ document.getElementById('base-model-manage').innerHTML = ''; return; }
  if(btn.id === 'base-model-add'){
    const name = document.getElementById('base-model-new').value.trim();
    if(!name){ setModelError('베이스 모델 이름을 적어 주세요.'); return; }
    if(await baseModelRequest(btn, 'POST', '/api/base-models', { name })) flashNotice('추가했어요.');
    return;
  }
  const row = btn.closest('[data-name]');
  if(!row) return;
  const old = row.dataset.name;
  const count = ((modelRegistry.base_model_usage || []).find(b => b.name === old) || {}).count || 0;
  const bound = BASE_MODEL_CODE_BOUND.test(old) ? '\n이 이름에 묶인 전용 워크플로우가 동작하지 않을 수 있어요.' : '';
  if(btn.classList.contains('base-model-rename')){
    const name = row.querySelector('input').value.trim();
    if(!name || name === old) return;
    const msg = ((count ? `모델 ${count}개의 베이스 모델도 함께 바뀌어요.` : '') + bound).trim();
    if(msg && !confirm(`"${old}" → "${name}"\n${msg}`)) return;
    if(await baseModelRequest(btn, 'PUT', '/api/base-models', { old, new: name })) flashNotice('이름을 바꿨어요.');
  }else if(btn.classList.contains('base-model-delete')){
    if(!confirm(`"${old}" 베이스 모델을 삭제할까요?${bound}`)) return;
    if(await baseModelRequest(btn, 'DELETE', '/api/base-models?name=' + encodeURIComponent(old))) flashNotice('삭제했어요.');
  }
});

document.getElementById('model-sort').addEventListener('change', (e) => { modelSort = e.target.value; renderModelRegistry(); });
document.getElementById('model-base-filter').addEventListener('change', (e) => { modelBaseFilter = e.target.value; renderModelRegistry(); });

document.getElementById('model-kind-tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.model-kind-tab');
  if(!btn || btn.dataset.kind === modelKindActive) return;
  if(!confirmDiscardDraft()) return;
  modelKindActive = btn.dataset.kind;
  // modelBaseFilter는 이제 종류와 무관한 전체 목록이라(베이스 모델 = 사용자가 인식하는
  // 기술 단위, 구현 형태와 무관) 종류 탭을 옮겨도 그대로 둔다 — renderModelRegistry()가
  // 그 값을 쓰는 항목이 아예 없어졌을 때만 알아서 "전체"로 되돌린다.
  modelExpandedKey = null;
  modelEditDraft = null;
  document.getElementById('model-add-form').innerHTML = '';
  renderModelRegistry();
});

document.getElementById('lora-tab-list').addEventListener('click', (e) => {
  const galleryBtn = e.target.closest('.model-usage-gallery-btn');
  if(galleryBtn){ openGalleryForModel(galleryBtn.dataset.name, galleryBtn.dataset.media); return; }
  const dlBtn = e.target.closest('.model-dlcmd-btn');
  if(dlBtn){
    const kind = dlBtn.dataset.kind, name = dlBtn.dataset.name;
    fetch(`/api/models/download-command?kind=${encodeURIComponent(kind)}&filename=${encodeURIComponent(name)}`)
      .then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if(!r.ok || !data.command){ setModelError(data.detail || '명령을 만들지 못했어요.'); return; }
        await navigator.clipboard.writeText(data.command);
        flashNotice(data.warning ? `복사했어요 — ${data.warning}` : '다운로드 명령을 복사했어요 — RunPod 터미널에 붙여넣으세요.');
      })
      .catch(() => setModelError('명령을 만들지 못했어요.'));
    return;
  }
  const saveBtn = e.target.closest('.model-save-btn');
  if(saveBtn){ saveModelDraft(); return; }
  const clearBtn = e.target.closest('.model-clear-btn');
  if(clearBtn){
    const editor = clearBtn.closest('.model-editor');
    if(!confirm('이 모델의 등록 정보(주소·베이스 모델·트리거 키워드·태그·메모)를 모두 지울까요? 파일은 그대로예요.')) return;
    const kind = editor.dataset.kind, name = editor.dataset.name;
    putModelEntry({ kind, filename: name, base_models: [], notes: '', tags: [], trigger_keyword: '', page_url: '', download_url: '' })
      .then(() => { modelExpandedKey = null; modelEditDraft = null; renderModelRegistry(); })
      .catch(err => setModelError(err.message));
    return;
  }
  const linkBtn = e.target.closest('.model-link');
  if(linkBtn){
    navigator.clipboard.writeText(linkBtn.dataset.copyUrl).then(() => flashNotice('주소를 복사했어요.'));
    return;
  }
  const openBtn = e.target.closest('.model-link-open');
  if(openBtn){
    window.open(openBtn.dataset.openUrl, '_blank', 'noopener');
    return;
  }
  const head = e.target.closest('.model-row-head');
  if(!head) return;
  const row = head.closest('.model-row');
  const key = modelKey(row.dataset.kind, row.dataset.name);
  if(key === modelExpandedKey){
    if(!confirmDiscardDraft()) return;
    modelExpandedKey = null;
    modelEditDraft = null;
  }else{
    if(!confirmDiscardDraft()) return;
    modelExpandedKey = key;
    modelEditDraft = modelDraftFromEntry(row.dataset.kind, row.dataset.name);
  }
  renderModelRegistry();
});

document.getElementById('lora-tab-list').addEventListener('input', (e) => {
  const input = e.target.closest('[data-field]');
  const editor = e.target.closest('.model-editor');
  if(!input || !editor || !isAdminUser() || !modelEditDraft) return;
  const field = input.dataset.field;
  if(field === 'base_models'){
    // 이미 고른 것의 순서는 지키고 새로 고른 것만 끝에 붙인다 — 첫 값이 호환용 base_model이 된다
    const on = checkedBases(input), cur = modelEditDraft.base_models;
    modelEditDraft.base_models = [...cur.filter(b => on.includes(b)), ...on.filter(b => !cur.includes(b))];
  }else{
    modelEditDraft[field] = field === 'tags' ? input.value.split(',').map(t => t.trim()).filter(Boolean) : input.value;
  }
  const saveBtn = editor.querySelector('.model-save-btn');
  if(saveBtn) saveBtn.disabled = !modelDraftDirty();
});

// 파드별 설치 현황 — 등록부 목록에 "어느 파드에 있나"를 얹는 데 쓴다.
let modelInventory = null;

async function fetchModelInventory(force){
  try{
    const res = await fetch(`/api/models/inventory${force ? '?refresh=true' : ''}`);
    modelInventory = res.ok ? await res.json() : null;
  }catch(e){ modelInventory = null; }
}

async function initModelsTab(force){
  setModelError('');
  document.getElementById('lora-tab-list').innerHTML =
    '<div class="model-list-loading"><span class="btn-spinner"></span>모델 정보를 불러오는 중…</div>';
  await Promise.all([fetchModelRegistry(), fetchModelUsage()]);
  renderModelRegistry();
  if(pendingModelAdd){
    // 워커 Models 탭의 "등록"으로 왔다 — 그 종류 탭에서 추가 칸을 열고 파일명을 채워 둔다.
    const { filename } = pendingModelAdd;
    pendingModelAdd = null;
    renderModelAddForm();
    document.getElementById('model-add-filename').value = filename;
    document.getElementById('model-add-page').focus();
  }
}

// 워커에 설치만 돼 있고 등록 안 된 모델을 등록하러 모델 탭으로 간다(08b-worker-models.js의 "등록").
let pendingModelAdd = null;
function openModelAddFor(kind, filename){
  pendingModelAdd = { kind, filename };
  modelKindActive = kind;
  modelExpandedKey = null;
  showTab('models');
}

document.getElementById('lora-tab-refresh-btn').addEventListener('click', () => initModelsTab(true));
document.getElementById('lora-tab-filter').addEventListener('input', renderModelRegistry);

// ==== "새 작업 추가" 마법사 — (1)베이스 모델 (2)워크플로우 유형 (3)LoRA (4)실행 방식을
// 순서대로 고르면 "적용"이 아래 템플릿 선택/옵션 폼/워크플로우 슬롯을 자동으로 채운다.
// 마법사 자신은 큐에 아무것도 등록하지 않는다 — 채워진 뒤에는 항상 하던 대로 "➕ 추가"를
// 눌러야 실제로 큐에 들어간다(검증/제출 경로를 그대로 재사용하기 위한 설계). ====
// 워크플로우 유형은 세 그룹으로 나뉜다(app.py의 WORKFLOW_TYPES와 정확히 대응):
//   base   — 첫 샘플링을 어디서 시작할지, 반드시 하나만 고른다(txt2img/img2img
//            중 배타적 — 둘 다 "첫 latent를 어디서 만드는지"를 정하는 서로 다른
//            방식이라 동시에 쓸 수 없다).
//   post   — base 뒤에 이어 붙이는 후처리, 0개 이상 동시에 고를 수 있다(체이닝
//            가능 — 예: txt2img+hires_fix+face_detailer+usdu). {hires_fix, face_detailer, usdu} 불리언.
//   preset — ControlNet/IPAdapter처럼 체크포인트마다 배선이 달라 이 서버가 자동
//            조립하지 못하는, family별 완성된 워크플로우. base/post와 동시에
//            쓸 수 없다(하나를 고르면 다른 그룹은 자동으로 비워짐).
function freshWizardPost(){ return { hires_fix: false, face_detailer: false, usdu: false, hiresScale: 1.5, prompt_enhance: false }; }
let wizard = {
  familyId: null, checkpoint: null,
  base: null, post: freshWizardPost(), preset: null,
  loras: [], batchMode: null,
  // krea2_t2i / minimax_h3_r2v / minimax_h3_i2v / minimax_h3_t2v 전용(다른 유형에서는 안 씀)
  refinePrompt: true, refImageCount: 2, refVideoCount: 0, refAudioCount: 0, videoRefMode: 'vhs',
  useFirstFrame: true, useLastFrame: false, t2vWidth: 704, t2vHeight: 1280,
};
// 마지막으로 wizardApply()를 성공적으로 실행했을 때의 wizard 상태 스냅샷(JSON) — null이면
// 아직 한 번도 적용 안 한 상태. 지금 상태와 다르면(wizardStale()) "다음"을 눌렀을 때 다시
// 만든다(goToNewJobDetails 참고) — 자동으로는 절대 다시 안 만든다(입력해 둔 프롬프트 등을
// 조용히 지우지 않기 위해, wizardApply()의 resetForm() 참고).
let wizardAppliedSignature = null;
// "새 작업" 패널의 두 하위 탭 중 지금 보이는 쪽 — renderNewJobTabs/goToNewJobDetails 참고.
let newJobActiveTab = 'select';   // 'select' | 'details'

// Danbooru 태그로 학습된 체크포인트 계열만 — 순수 "SDXL"(실사 등)이나 다른
// 아키텍처는 태그식 프롬프트가 안 맞아서 제외한다. baseModelFamilies[id].label은
// server/model_registry.py의 BASE_MODELS 문자열 그대로(예: "Illustrious")다.
const DANBOORU_TAG_FAMILIES = ['Illustrious', 'Pony', 'NoobAI'];
function currentFamilySupportsDanbooru(){
  const fam = wizard.familyId ? baseModelFamilies[wizard.familyId] : null;
  return !!(fam && DANBOORU_TAG_FAMILIES.includes(fam.label));
}

// 전처리(Prompt Enhance)의 모드는 고른 family의 prompt_style(danbooru/natural)로 정한다 — 모르면 자연어.
function wizardEnhanceMode(){
  const fam = wizard.familyId ? baseModelFamilies[wizard.familyId] : null;
  return (fam && fam.prompt_style) === 'danbooru' ? 'danbooru' : 'natural';
}

function wizardComplete(){
  return !!(wizard.familyId && wizard.checkpoint && wizardTypeChosen() && wizard.batchMode);
}
function wizardStale(){
  return wizardComplete() && (wizardAppliedSignature === null || JSON.stringify(wizard) !== wizardAppliedSignature);
}

function wizardBaseMeta(baseId){
  return ((workflowTypesCache || {}).base || []).find(t => t.id === baseId);
}
// 후처리 id가 지금 고른 base에 붙는지 — 화면에 안 보이는 후처리(예: face_detailer 유형에서의 usdu)가
// 켜진 채 남아 있어도 spec에 넣지 않게 한다.
function wizardPostApplies(postId){
  const t = ((workflowTypesCache || {}).post || []).find(p => p.id === postId);
  return !!(wizard.post[postId] && t && (!t.applies_to_base || t.applies_to_base.includes(wizard.base)));
}
function wizardPresetMeta(presetId){
  return ((workflowTypesCache || {}).preset || []).find(t => t.id === presetId);
}
function wizardPostMeta(postId){
  return ((workflowTypesCache || {}).post || []).find(t => t.id === postId);
}
// 지금 고른 조합의 template_ids — preset이면 그 preset 항목의 것, 아니면 base
// 항목의 것을 쓴다(post는 워크플로우 안에서 완결되므로 템플릿 선택에 영향 없음).
function wizardTemplateIds(){
  if(wizard.preset) return (wizardPresetMeta(wizard.preset) || {}).template_ids;
  if(wizard.base) return (wizardBaseMeta(wizard.base) || {}).template_ids;
  return null;
}
function wizardIsPreset(){ return !!wizard.preset; }
function wizardTypeChosen(){ return !!wizard.preset || !!wizard.base; }
function wizardTypeSummary(){
  if(wizard.preset){
    const meta = wizardPresetMeta(wizard.preset);
    return meta ? meta.label : wizard.preset;
  }
  if(!wizard.base) return '선택 안 함';
  const parts = [(wizardBaseMeta(wizard.base) || {}).label || wizard.base];
  if(wizard.post.hires_fix) parts.push(`Hires Fix(${wizard.post.hiresScale}x)`);
  if(wizard.post.usdu) parts.push('USDU');
  if(wizard.post.prompt_enhance) parts.push('Prompt Enhance');
  return parts.join(' + ');
}

function openWizardModal(id){ document.getElementById(id).style.display = 'flex'; }
function closeWizardModal(id){ document.getElementById(id).style.display = 'none'; }

for(const id of ['wizard-family-modal', 'wizard-type-modal', 'wizard-lora-modal', 'wizard-batchmode-modal']){
  const overlay = document.getElementById(id);
  overlay.addEventListener('click', (e) => { if(e.target === overlay) closeWizardModal(id); });
}
document.getElementById('wizard-family-modal-close').addEventListener('click', () => closeWizardModal('wizard-family-modal'));
document.getElementById('wizard-family-modal-cancel').addEventListener('click', () => closeWizardModal('wizard-family-modal'));
document.getElementById('wizard-type-modal-close').addEventListener('click', () => closeWizardModal('wizard-type-modal'));
document.getElementById('wizard-type-modal-cancel').addEventListener('click', () => closeWizardModal('wizard-type-modal'));
document.getElementById('wizard-type-modal-done').addEventListener('click', () => { wizardUpdateStepButtons(); closeWizardModal('wizard-type-modal'); });
document.getElementById('wizard-lora-modal-close').addEventListener('click', () => closeWizardModal('wizard-lora-modal'));
document.getElementById('wizard-lora-modal-cancel').addEventListener('click', () => closeWizardModal('wizard-lora-modal'));
document.getElementById('wizard-lora-modal-done').addEventListener('click', () => { wizardUpdateStepButtons(); closeWizardModal('wizard-lora-modal'); });
document.getElementById('wizard-batchmode-modal-close').addEventListener('click', () => closeWizardModal('wizard-batchmode-modal'));
document.getElementById('wizard-batchmode-modal-cancel').addEventListener('click', () => closeWizardModal('wizard-batchmode-modal'));
document.addEventListener('keydown', (e) => {
  if(e.key !== 'Escape') return;
  for(const id of ['wizard-family-modal', 'wizard-type-modal', 'wizard-lora-modal', 'wizard-batchmode-modal']){
    if(document.getElementById(id).style.display !== 'none') closeWizardModal(id);
  }
});

function wizardUpdateStepButtons(){
  const familyBtn = document.getElementById('wizard-step-family');
  const typeBtn = document.getElementById('wizard-step-type');
  const loraBtn = document.getElementById('wizard-step-lora');
  const batchBtn = document.getElementById('wizard-step-batchmode');

  const family = wizard.familyId ? baseModelFamilies[wizard.familyId] : null;
  const familyLabel = family ? (family.label || wizard.familyId) : '선택 안 함';
  document.getElementById('wizard-step-family-value').textContent = (wizard.familyId && wizard.checkpoint) ? `${familyLabel} · ${wizard.checkpoint}` : familyLabel;
  familyBtn.classList.toggle('done', !!(wizard.familyId && wizard.checkpoint));

  typeBtn.disabled = !wizard.familyId;
  document.getElementById('wizard-step-type-value').textContent = wizardTypeSummary();
  typeBtn.classList.toggle('done', wizardTypeChosen());

  loraBtn.disabled = !wizardTypeChosen();
  document.getElementById('wizard-step-lora-value').textContent = wizard.loras.length === 0 ? '사용 안 함' : wizard.loras.map(l => l.name).join(', ');
  loraBtn.classList.toggle('done', wizard.loras.length > 0);

  batchBtn.disabled = !wizardTypeChosen();
  document.getElementById('wizard-step-batchmode-value').textContent =
    wizard.batchMode === 'seed' ? '시드 반복' : wizard.batchMode === 'csv' ? 'CSV 순회' : '선택 안 함';
  batchBtn.classList.toggle('done', !!wizard.batchMode);

  // 여기서는 절대 자동으로 다시 만들지 않는다 — "다음"/"세부 설정" 탭을 눌러야만
  // (goToNewJobDetails) 지금 상태 기준으로 다시 만든다. 다만 "다음" 버튼이 활성화될지는
  // 마법사가 바뀔 때마다 새로 계산해야 하므로 여기서 renderNewJobTabs()를 불러준다.
  renderNewJobTabs();
}

// "초기화" 버튼 — 마법사 스텝과 업로드된 워크플로우 파일을 둘 다 처음 상태로 되돌린다
// ("1. 워크플로우 설정" 탭 전체를 비우는 버튼이라, 마법사만 지우면 헷갈린다).
function resetWizardAndWorkflow(){
  wizard = {
    familyId: null, checkpoint: null,
    base: null, post: freshWizardPost(), preset: null,
    loras: [], batchMode: null,
    refinePrompt: true, refImageCount: 2, refVideoCount: 0, refAudioCount: 0, videoRefMode: 'vhs',
    useFirstFrame: true, useLastFrame: false, t2vWidth: 704, t2vHeight: 1280,
  };
  wizardAppliedSignature = null;
  document.getElementById('wizard-error').textContent = '';
  document.getElementById('load-notice').textContent = '';
  wizardUpdateStepButtons();
  setWorkflowFile(null);
}
document.getElementById('wizard-reset-btn').addEventListener('click', resetWizardAndWorkflow);

