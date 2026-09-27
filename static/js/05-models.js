// ---- "모델" 탭(전역) — 모델 등록부: 파드와 무관한 기준 데이터 ----
// 서버의 models 테이블이 원본이다. (종류, 파일명)마다 베이스 모델·페이지 주소·다운로드 주소·트리거 키워드(LoRA)·
// 태그·메모를 적어 두고, 어느 파드에서든 이 정보를 보고 모델을 받거나 트리거를 쓴다. 파일이 실제로 놓이는 곳만
// 파드라서, 파드별 설치 여부(modelInventory)는 등록부 항목 위에 "어느 파드에 있나"로 얹어 보여준다.
let modelRegistry = { kinds: [], base_models: [], items: {} };
let modelKindActive = 'checkpoints';
let modelExpandedKey = null;
// 지금 펼쳐진 편집 패널의 임시 값(저장 버튼을 눌러야 서버로 나간다) — 파일명도 포함해서
// 여기서만 고친다(원본은 modelRegistry.items). 행을 접거나 다른 행/종류로 옮기면 버린다.
let modelEditDraft = null;

function modelKey(kind, name){ return `${kind}\u0000${name}`; }

// 서버의 model_registry.base_id와 같은 규칙 — 마법사가 LoRA를 걸러낼 때 체크포인트 그룹 id와 맞춰 본다.
function baseIdOf(v){ return String(v || '').trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^[-.]+|[-.]+$/g, ''); }

function rebuildLoraTriggersFromRegistry(){
  const out = {};
  for(const e of Object.values(modelRegistry.items)){
    if(e.kind === 'loras' && (e.trigger_keyword || e.base_model)) out[e.filename] = { trigger: e.trigger_keyword || '', base_id: baseIdOf(e.base_model) };
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
    || { kind, filename: name, base_model: '', notes: '', tags: [], trigger_keyword: '', page_url: '', download_url: '' };
}

// 베이스 모델은 이제 자유 입력이 아니라 서버가 정해 둔 기준 목록(modelRegistry.base_models,
// server/model_registry.py의 BASE_MODELS)에서만 고른다 — 대소문자가 미묘하게 다른 값이
// 새로 생겨 필터가 못 알아보는 일이 없도록(전에 "Krea.2"/"krea.2"가 따로 놀던 문제).
// selected가 그 목록에 없으면(예전에 API로 직접 넣은 값 등) 지우지 않고 맨 끝에 얹어 둔다.
function baseModelSelectOptions(selected){
  const canon = modelRegistry.base_models || [];
  const opts = ['<option value="">선택 안 함</option>'];
  for(const b of canon) opts.push(`<option value="${escapeHtml(b)}"${b === selected ? ' selected' : ''}>${escapeHtml(b)}</option>`);
  if(selected && !canon.includes(selected)) opts.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)} (목록에 없음)</option>`);
  return opts.join('');
}

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
  return { origKind: kind, kind, origName: name, filename: name, base_model: e.base_model, notes: e.notes,
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
  const fields = { base_model: d.base_model, notes: d.notes, tags: d.tags,
                    trigger_keyword: d.trigger_keyword, page_url: d.page_url, download_url: d.download_url };
  try{
    await putModelEntry({ kind: d.kind, filename: newName, ...fields });
    if(moved){
      await putModelEntry({ kind: d.origKind, filename: d.origName, base_model: '', notes: '', tags: [],
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

// 파드별 설치 현황(modelInventory)에서 — 연결된 파드만 "있다/없다"를 말할 수 있다.
function invPods(){ return (modelInventory && modelInventory.pods) || []; }
function livePods(){ return invPods().filter(p => p.connected); }
function podsHaving(kind, name){ return livePods().filter(p => (p.models[kind] || []).includes(name)); }
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
    <span></span><span>파일명</span><span>베이스 모델</span>${isLora ? '<span>트리거 키워드</span>' : ''}<span>태그</span><span>사용</span><span>파드</span><span>주소</span><span></span>
  </div>`;
}

function modelRowHtml(kind, name, isAdmin){
  const e = modelEntry(kind, name);
  const key = modelKey(kind, name);
  const open = modelExpandedKey === key;
  const live = livePods();
  const have = podsHaving(kind, name);
  const isLora = kind === 'loras';
  const usage = usageOf(kind, name);
  const tagList = e.tags || [];
  const tagsHtml = tagList.length ? `<div class="gallery-details-col model-cell-tags">${tagList.map(t => `<span class="model-badge">${escapeHtml(t)}</span>`).join('')}</div>`
    : '<div class="gallery-details-col dim">-</div>';
  const podsHtml = live.length
    ? (have.length
        ? `<div class="gallery-details-col" title="${escapeHtml(have.map(p => p.name).join(', '))}">${have.length}/${live.length}</div>`
        : '<div class="gallery-details-col model-missing" title="연결된 파드 어디에도 없어요">없음</div>')
    : '<div class="gallery-details-col dim">-</div>';
  const links = e.download_url
    ? (isAdmin
        ? `<button type="button" class="icon-btn icon-btn-neutral model-dlcmd-btn" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}" title="다운로드 명령 복사(RunPod 터미널용)">${ico('download')}</button>`
        : `<span title="다운로드 주소 등록됨">${ico('download')}</span>`)
    : '';
  const head = `
    <div class="model-cols${isLora ? ' lora' : ''} model-row-head" role="button" tabindex="0">
      ${modelThumbHtml(kind, name)}
      <div class="gallery-details-col name lora-tab-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
      <div class="gallery-details-col" title="${escapeHtml(e.base_model)}">${e.base_model ? escapeHtml(e.base_model) : '<span class="dim">-</span>'}</div>
      ${isLora ? `<div class="gallery-details-col" title="${escapeHtml(e.trigger_keyword)}">${e.trigger_keyword ? escapeHtml(e.trigger_keyword) : '<span class="dim">-</span>'}</div>` : ''}
      ${tagsHtml}
      <div class="gallery-details-col dim">${usage ? usage.count : '-'}</div>
      ${podsHtml}
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
    const podRows = invPods().map(p => {
      const has = p.connected && (p.models[kind] || []).includes(name);
      let state;
      if(!p.connected) state = '<span class="inv-off">연결 안 됨</span>';
      else if(has) state = `<span class="inv-cell yes">${ico('check')} 설치됨</span>`;
      else state = '<span class="inv-cell no">없음</span>';
      return `<div class="model-pod-row"><span class="model-pod-name">${escapeHtml(p.name)}</span>${state}</div>`;
    }).join('');
    const dirty = modelDraftDirty();
    const kindOptions = modelRegistry.kinds.map(k => `<option value="${escapeHtml(k.id)}"${k.id === d.kind ? ' selected' : ''}>${escapeHtml(k.label)}</option>`).join('');
    editor = `
    <div class="model-editor" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}">
      ${usageHtml(kind, name)}
      <label class="model-field"><span>종류</span>
        <select class="option-input" data-field="kind" ${dis}>${kindOptions}</select></label>
      <label class="model-field"><span>파일명 <span style="opacity:.6">(하위 폴더는 "폴더/이름.safetensors")</span></span>
        <input type="text" class="option-input" data-field="filename" ${dis} placeholder="예: mmh3/my_style_v2.safetensors" value="${escapeHtml(d.filename)}"></label>
      <label class="model-field"><span>베이스 모델</span>
        <select class="option-input" data-field="base_model" ${dis}>${baseModelSelectOptions(d.base_model)}</select></label>
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
      <div class="model-field wide"><span>파드별 설치</span>${podRows || '<span class="comfy-model-empty">ComfyUI 파드가 없어요.</span>'}</div>
      ${isAdmin ? `<div class="model-full model-editor-actions">
          <button type="button" class="submit-btn model-save-btn" ${dirty ? '' : 'disabled'}>저장</button>
          <button type="button" class="modal-btn-secondary model-clear-btn">등록 정보 지우기</button>
        </div>`
                : '<div class="email-hint model-full">등록 정보는 관리자만 고칠 수 있어요.</div>'}
    </div>`;
  }
  return `
  <div class="model-row${open ? ' open' : ''}${live.length && !have.length ? ' uninstalled' : ''}" data-kind="${escapeHtml(kind)}" data-name="${escapeHtml(name)}">
    ${head}
    ${editor}
  </div>`;
}

function renderModelRegistry(){
  const tabsEl = document.getElementById('model-kind-tabs');
  const listEl = document.getElementById('lora-tab-list');
  const live = livePods();
  const namesOf = kind => {
    const set = new Set(live.flatMap(p => p.models[kind] || []));
    for(const e of Object.values(modelRegistry.items)) if(e.kind === kind) set.add(e.filename);
    return Array.from(set).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  };
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
    Object.values(modelRegistry.items).map(e => (e.base_model || '').trim().toLowerCase()).filter(Boolean));
  const bases = (modelRegistry.base_models || []).filter(b => usedBasesLower.has(b.toLowerCase()));
  if(modelBaseFilter && !bases.some(b => b.toLowerCase() === modelBaseFilter.toLowerCase())) modelBaseFilter = '';
  baseFilterEl.innerHTML = '<option value="">베이스 모델: 전체</option>'
    + bases.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('');
  baseFilterEl.value = modelBaseFilter;

  const filter = document.getElementById('lora-tab-filter').value.trim().toLowerCase();
  let shown = all.filter(n => {
    const e = modelEntry(modelKindActive, n);
    if(modelBaseFilter && e.base_model.toLowerCase() !== modelBaseFilter.toLowerCase()) return false;
    if(!filter) return true;
    return (n + ' ' + e.base_model + ' ' + (e.tags || []).join(' ') + ' ' + e.notes + ' ' + e.trigger_keyword + ' ' + e.page_url).toLowerCase().includes(filter);
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
  const notice = live.length ? '' : '<div class="comfy-model-empty" style="margin-bottom:8px;">연결된 ComfyUI 파드가 없어 설치 현황은 못 봐요 — 등록해 둔 항목만 보여요.</div>';
  const addBtn = document.getElementById('model-add-toggle');
  if(addBtn) addBtn.style.display = isAdminUser() ? '' : 'none';
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
      <label class="model-field"><span>베이스 모델</span><select class="option-input" id="model-add-base">${baseModelSelectOptions('')}</select></label>
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
                    download_url: v('model-add-download'), base_model: v('model-add-base') };
  if(modelKindActive === 'loras') payload.trigger_keyword = v('model-add-trigger');
  if(!payload.filename){ setModelError('파일명을 입력하세요.'); return; }
  if(!(payload.page_url || payload.download_url || payload.base_model || payload.trigger_keyword)){
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
    putModelEntry({ kind, filename: name, base_model: '', notes: '', tags: [], trigger_keyword: '', page_url: '', download_url: '' })
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
  modelEditDraft[field] = field === 'tags' ? input.value.split(',').map(t => t.trim()).filter(Boolean) : input.value;
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
  await Promise.all([fetchModelRegistry(), fetchModelUsage(), fetchModelInventory(force)]);
  renderModelRegistry();
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
//            가능 — 예: txt2img+hires_fix+usdu). {hires_fix, usdu} 불리언 두 개.
//   preset — ControlNet/IPAdapter처럼 체크포인트마다 배선이 달라 이 서버가 자동
//            조립하지 못하는, family별 완성된 워크플로우. base/post와 동시에
//            쓸 수 없다(하나를 고르면 다른 그룹은 자동으로 비워짐).
function freshWizardPost(){ return { hires_fix: false, usdu: false, hiresScale: 1.5 }; }
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

function wizardComplete(){
  return !!(wizard.familyId && wizard.checkpoint && wizardTypeChosen() && wizard.batchMode);
}
function wizardStale(){
  return wizardComplete() && (wizardAppliedSignature === null || JSON.stringify(wizard) !== wizardAppliedSignature);
}

function wizardBaseMeta(baseId){
  return ((workflowTypesCache || {}).base || []).find(t => t.id === baseId);
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

