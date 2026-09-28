// ---- 결과물 메타: 즐겨찾기·평점·태그·메모, 그리고 그걸로 하는 필터/검색 ----
// 서버(asset_meta.py)가 결과물 색인(assets)에 저장하고, 갤러리 목록 항목에도 favorite/rating/tags가
// 같이 온다. 식별은 갤러리가 다루는 이름(출력 폴더 기준 상대경로)이다.
const galleryFilters = { q: '', favorite: false, tag: '', minRating: 0, model: '' };
const videoGalleryFilters = { q: '', favorite: false, tag: '', minRating: 0, model: '' };
const AM_OPEN_KEY = 'nightshift-meta-open';
let assetMetaOpen = false;
try{ assetMetaOpen = localStorage.getItem(AM_OPEN_KEY) === '1'; }catch(e){ /* 기본값 유지 */ }
let allTagsCache = [];

function galleryFilterQs(f){
  const p = new URLSearchParams();
  if(f.q.trim()) p.set('q', f.q.trim());
  if(f.favorite) p.set('favorite', 'true');
  if(f.tag) p.set('tag', f.tag);
  if(f.minRating) p.set('min_rating', String(f.minRating));
  if(f.model) p.set('model', f.model);
  if(isNsfwHidden()) p.set('hide_nsfw', 'true');
  const s = p.toString();
  return s ? `?${s}` : '';
}
function galleryFiltersActive(f){ return !!(f.q.trim() || f.favorite || f.tag || f.minRating || f.model); }

function assetJson(url, body){
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

// 목록에 있는 항목(과 지금 그려진 항목)의 값을 바꾸고, 화면에 이미 그려진 별 표시도 같이 맞춘다.
function applyAssetPatch(kind, names, patch){
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const shown = kind === 'image' ? displayedGalleryImages : displayedGalleryVideos;
  const wanted = new Set(names);
  for(const arr of [list, shown]) for(const it of arr) if(wanted.has(it.name)) Object.assign(it, patch);
  const itemSel = kind === 'image' ? '#gallery-grid .gallery-item' : '#video-gallery-grid .vgallery-item';
  document.querySelectorAll(itemSel).forEach(el => {
    const it = shown[Number(el.dataset.index)];
    if(!it || !wanted.has(it.name)) return;
    if('favorite' in patch){
      const btn = el.querySelector('.gallery-item-fav, .vgallery-item-fav');
      if(btn){ btn.classList.toggle('on', !!it.favorite); btn.setAttribute('aria-pressed', it.favorite ? 'true' : 'false'); }
    }
    if('nsfw' in patch){
      const badgeClass = kind === 'image' ? 'gallery-item-nsfw' : 'vgallery-item-nsfw';
      let badge = el.querySelector(`.${badgeClass}`);
      if(it.nsfw && !badge){
        badge = document.createElement('span');
        badge.className = badgeClass;
        badge.title = 'NSFW로 표시됨';
        badge.innerHTML = ico('flame');
        el.insertBefore(badge, el.querySelector('img, video'));
      }else if(!it.nsfw && badge){
        badge.remove();
      }
    }
  });
}

// 즐겨찾기 — 화면을 먼저 바꾸고(낙관적) 서버가 거절하면 되돌린다.
async function setAssetsFavorite(kind, names, favorite){
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const before = names.map(n => !!(list.find(i => i.name === n) || {}).favorite);
  applyAssetPatch(kind, names, { favorite });
  try{
    const res = await assetJson('/api/output-assets/update', { paths: names, favorite });
    if(!res.ok) throw new Error('failed');
    return true;
  }catch(e){
    names.forEach((n, i) => applyAssetPatch(kind, [n], { favorite: before[i] }));
    return false;
  }
}

async function setAssetsNsfw(kind, names, nsfw){
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const before = names.map(n => !!(list.find(i => i.name === n) || {}).nsfw);
  applyAssetPatch(kind, names, { nsfw });
  try{
    const res = await assetJson('/api/output-assets/update', { paths: names, nsfw });
    if(!res.ok) throw new Error('failed');
    // '블러'는 목록에 남되 미리보기가 바뀌고, '안 보기'는 서버가 아예 뺀다 — 둘 다 낙관적
    // 패치만으로는 못 맞추니(썸네일 src·목록 자체가 바뀜) 다시 불러온다. '보기'는 항상 그대로
    // 보이니 다시 부를 필요 없다.
    if(nsfwMode !== 'show'){
      if(kind === 'image' && typeof fetchGalleryImages === 'function') fetchGalleryImages();
      if(kind === 'video' && typeof fetchGalleryVideos === 'function') fetchGalleryVideos();
    }
    return true;
  }catch(e){
    names.forEach((n, i) => applyAssetPatch(kind, [n], { nsfw: before[i] }));
    return false;
  }
}

async function setAssetRating(kind, name, rating){
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const before = (list.find(i => i.name === name) || {}).rating || null;
  applyAssetPatch(kind, [name], { rating: rating || null });
  try{
    const res = await assetJson('/api/output-assets/update', { path: name, rating });
    if(!res.ok) throw new Error('failed');
    return true;
  }catch(e){
    applyAssetPatch(kind, [name], { rating: before });
    return false;
  }
}

async function changeAssetTags(kind, names, add, remove){
  try{
    const res = await assetJson('/api/output-assets/tags', { paths: names, add, remove });
    if(!res.ok) throw new Error('failed');
    const data = await res.json();
    for(const [name, tags] of Object.entries(data.tags || {})) applyAssetPatch(kind, [name], { tags });
    refreshTagOptions();
    return data.tags || {};
  }catch(e){
    return null;
  }
}

async function refreshTagOptions(){
  try{
    const res = await fetch('/api/tags');
    if(res.ok) allTagsCache = (await res.json()).tags || [];
  }catch(e){ /* 이전 목록 유지 */ }
  for(const [prefix, f] of [['image', galleryFilters], ['video', videoGalleryFilters]]){
    const sel = document.getElementById(`gf-${prefix}-tag`);
    if(!sel) continue;
    const html = '<option value="">모든 태그</option>'
      + allTagsCache.map(t => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)} (${t.count})</option>`).join('');
    if(sel.dataset.html !== html){ sel.innerHTML = html; sel.dataset.html = html; }
    if(f.tag && !allTagsCache.some(t => t.name === f.tag)) f.tag = '';   // 사라진 태그로 계속 거르지 않게
    sel.value = f.tag;
  }
  const dl = document.getElementById('am-tag-list');
  if(dl) dl.innerHTML = allTagsCache.map(t => `<option value="${escapeHtml(t.name)}"></option>`).join('');
}

// ---- 갤러리 필터 줄 (이미지/영상 공용) ----
const galleryFilterSyncs = {};
function setupGalleryFilters(prefix, filters, refetch){
  const q = document.getElementById(`gf-${prefix}-q`);
  const fav = document.getElementById(`gf-${prefix}-fav`);
  const tag = document.getElementById(`gf-${prefix}-tag`);
  const rating = document.getElementById(`gf-${prefix}-rating`);
  const clear = document.getElementById(`gf-${prefix}-clear`);
  const modelChip = document.getElementById(`gf-${prefix}-model`);
  const sync = () => {
    clear.style.display = galleryFiltersActive(filters) ? '' : 'none';
    modelChip.hidden = !filters.model;
    modelChip.textContent = filters.model ? `모델: ${filters.model}` : '';
    modelChip.title = filters.model || '';
  };
  galleryFilterSyncs[prefix] = sync;
  let timer = null;
  q.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { filters.q = q.value; sync(); refetch(); }, 300);
  });
  fav.addEventListener('click', () => {
    filters.favorite = !filters.favorite;
    fav.classList.toggle('on', filters.favorite);
    fav.setAttribute('aria-pressed', filters.favorite ? 'true' : 'false');
    sync(); refetch();
  });
  tag.addEventListener('change', () => { filters.tag = tag.value; sync(); refetch(); });
  rating.addEventListener('change', () => { filters.minRating = Number(rating.value) || 0; sync(); refetch(); });
  clear.addEventListener('click', () => {
    Object.assign(filters, { q: '', favorite: false, tag: '', minRating: 0, model: '' });
    q.value = ''; tag.value = ''; rating.value = '0';
    fav.classList.remove('on'); fav.setAttribute('aria-pressed', 'false');
    sync(); refetch();
  });
  sync();
}
setupGalleryFilters('image', galleryFilters, () => fetchGalleryImages());
setupGalleryFilters('video', videoGalleryFilters, () => fetchGalleryVideos());

// ---- 일괄 작업 (선택한 여러 장) ----
function parseTagInput(text){
  const add = [], remove = [];
  for(const raw of text.split(',')){
    const t = raw.trim();
    if(!t) continue;
    if(t.startsWith('-') && t.length > 1) remove.push(t.slice(1).trim()); else add.push(t);
  }
  return { add, remove };
}

async function bulkFavorite(kind, names){
  if(names.length === 0) return;
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const allFav = names.every(n => (list.find(i => i.name === n) || {}).favorite);
  await setAssetsFavorite(kind, names, !allFav);
}

async function bulkNsfw(kind, names){
  if(names.length === 0) return;
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const allNsfw = names.every(n => (list.find(i => i.name === n) || {}).nsfw);
  await setAssetsNsfw(kind, names, !allNsfw);
}

async function bulkTags(kind, names){
  if(names.length === 0) return;
  const text = window.prompt(`선택한 ${names.length}장에 붙일 태그를 쉼표로 적으세요.\n앞에 -를 붙이면 그 태그를 뗍니다. 예) 손좋음, -실패작`, '');
  if(text === null) return;
  const { add, remove } = parseTagInput(text);
  if(add.length === 0 && remove.length === 0) return;
  const result = await changeAssetTags(kind, names, add, remove);
  if(result === null) window.alert('태그를 바꾸지 못했어요.');
}

document.getElementById('gallery-fav-selected-btn').addEventListener('click', () => bulkFavorite('image', Array.from(selectedGalleryNames)));
document.getElementById('gallery-nsfw-selected-btn').addEventListener('click', () => bulkNsfw('image', Array.from(selectedGalleryNames)));
document.getElementById('gallery-tag-selected-btn').addEventListener('click', () => bulkTags('image', Array.from(selectedGalleryNames)));
document.getElementById('video-gallery-fav-selected-btn').addEventListener('click', () => bulkFavorite('video', Array.from(selectedGalleryVideoNames)));
document.getElementById('video-gallery-nsfw-selected-btn').addEventListener('click', () => bulkNsfw('video', Array.from(selectedGalleryVideoNames)));
document.getElementById('video-gallery-tag-selected-btn').addEventListener('click', () => bulkTags('video', Array.from(selectedGalleryVideoNames)));

// 이동 창에서 "+ 새 프로젝트 만들기…"를 고르면 이름을 받아 프로젝트를 만든 뒤 바로 그리로 옮긴다(이미지·영상·작업 공용).
const NEW_PROJECT_VALUE = '__new__';
const NEW_PROJECT_LABEL = '+ 새 프로젝트 만들기…';

async function createProjectByName(name){
  const res = await fetch('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.detail || '프로젝트를 만들지 못했어요.');
  await fetchProjects();   // 새 프로젝트를 목록·이름 표에 넣는다
  return data;
}

// select에서 "새 프로젝트"를 고르면 이름 입력칸을 보여 주고, 다른 걸 고르면 숨긴다.
function bindNewProjectField(selectId, inputId){
  const select = document.getElementById(selectId);
  const input = document.getElementById(inputId);
  const sync = (focus) => {
    const isNew = select.value === NEW_PROJECT_VALUE;
    input.style.display = isNew ? '' : 'none';
    if(isNew && focus) input.focus();
  };
  select.addEventListener('change', () => sync(true));
  return sync;
}

// ---- 프로젝트로 이동 (이미지/영상 공용) ----
// 파일은 그대로고 서버 색인의 project_id만 바뀐다. 프로젝트 안에서 보고 있었다면 옮긴 것은
// 목록에서 빠지고, 전체 갤러리에서는 그대로 남는다.
const assetMoveModal = document.getElementById('asset-move-modal');
let assetMoveRequest = null;   // { kind, names, resolve }

function assetMoveModalOpen(){ return assetMoveModal.style.display !== 'none'; }

let noticeTimer = null;
function flashNotice(text){
  let el = document.getElementById('flash-notice');
  if(!el){
    el = document.createElement('div');
    el.id = 'flash-notice';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => el.classList.remove('show'), 2500);
}

function closeAssetMoveModal(moved){
  assetMoveModal.style.display = 'none';
  const req = assetMoveRequest;
  assetMoveRequest = null;
  if(req) req.resolve(!!moved);
}

// 옮겼으면 true, 취소·실패면 false로 끝나는 Promise.
async function openAssetMoveModal(kind, names){
  if(names.length === 0 || assetMoveRequest) return false;
  await fetchProjects();   // 다른 화면에서 만들거나 보관한 프로젝트까지 반영
  const list = kind === 'image' ? galleryImages : galleryVideos;
  const currentIds = new Set(names.map(n => {
    const it = list.find(i => i.name === n);
    return it && it.project_id != null ? it.project_id : 'unassigned';
  }));
  const allInOne = currentIds.size === 1 ? [...currentIds][0] : null;
  const noun = kind === 'image' ? '이미지' : '영상';
  const choices = [{ value: '', id: 'unassigned', label: UNASSIGNED_LABEL },
    ...projectsCache.filter(p => !p.archived).map(p => ({ value: String(p.id), id: p.id, label: p.name }))];
  const select = document.getElementById('asset-move-select');
  select.innerHTML = choices.map(c => {
    const here = allInOne === c.id;
    return `<option value="${escapeHtml(c.value)}"${here ? ' disabled' : ''}>${escapeHtml(c.label)}${here ? ' (현재)' : ''}</option>`;
  }).join('') + `<option value="${NEW_PROJECT_VALUE}">${NEW_PROJECT_LABEL}</option>`;
  const firstOpen = choices.find(c => allInOne !== c.id);
  select.value = firstOpen ? firstOpen.value : NEW_PROJECT_VALUE;   // 갈 만한 프로젝트가 없으면 바로 새로 만들게 한다
  document.getElementById('asset-move-new-name').value = '';
  syncAssetMoveNewField(false);
  document.getElementById('asset-move-confirm-btn').disabled = false;
  document.getElementById('asset-move-hint').textContent = names.length === 1
    ? `이 ${noun}를 옮길 프로젝트를 고르세요. 파일은 그대로 두고 소속만 바뀌어요.`
    : `선택한 ${noun} ${names.length}개를 옮길 프로젝트를 고르세요. 파일은 그대로 두고 소속만 바뀌어요.`;
  const statusEl = document.getElementById('asset-move-status');
  statusEl.textContent = '';
  statusEl.classList.remove('error', 'success');
  return new Promise(resolve => {
    assetMoveRequest = { kind, names, resolve };
    assetMoveModal.style.display = 'flex';
    if(select.value === NEW_PROJECT_VALUE) document.getElementById('asset-move-new-name').focus(); else select.focus();
  });
}
const syncAssetMoveNewField = bindNewProjectField('asset-move-select', 'asset-move-new-name');
document.getElementById('asset-move-new-name').addEventListener('keydown', (e) => {
  if(e.key === 'Enter'){ e.preventDefault(); document.getElementById('asset-move-confirm-btn').click(); }
});

document.getElementById('asset-move-confirm-btn').addEventListener('click', async () => {
  const req = assetMoveRequest;
  if(!req) return;
  const btn = document.getElementById('asset-move-confirm-btn');
  const statusEl = document.getElementById('asset-move-status');
  const value = document.getElementById('asset-move-select').value;
  let projectId = value === '' || value === NEW_PROJECT_VALUE ? null : Number(value);
  btn.disabled = true;
  statusEl.classList.remove('error');
  if(value === NEW_PROJECT_VALUE){
    const name = document.getElementById('asset-move-new-name').value.trim();
    if(!name){
      statusEl.classList.add('error');
      statusEl.textContent = '새 프로젝트 이름을 입력하세요.';
      btn.disabled = false;
      return;
    }
    statusEl.textContent = '프로젝트를 만드는 중…';
    try{
      projectId = (await createProjectByName(name)).id;
    }catch(e){
      statusEl.classList.add('error');
      statusEl.textContent = e.message || '프로젝트를 만들지 못했어요.';
      btn.disabled = false;
      return;
    }
  }
  statusEl.textContent = '옮기는 중…';
  try{
    const res = await assetJson('/api/output-assets/move', { paths: req.names, project_id: projectId });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || '옮기지 못했어요.');
    }
  }catch(e){
    statusEl.classList.add('error');
    statusEl.textContent = e.message || '옮기지 못했어요.';
    btn.disabled = false;
    return;
  }
  applyAssetPatch(req.kind, req.names, { project_id: projectId });
  // 목록을 다시 받아 프로젝트 범위 밖으로 나간 것을 빼고(선택도 함께 정리된다), 프로젝트 카드의 개수·대표 이미지도 갱신한다.
  await (req.kind === 'image' ? fetchGalleryImages() : fetchGalleryVideos());
  fetchProjects();
  flashNotice(`${req.names.length}개를 '${projectId === null ? UNASSIGNED_LABEL : projectName(projectId)}'(으)로 옮겼어요`);
  btn.disabled = false;
  closeAssetMoveModal(true);
});
document.getElementById('asset-move-cancel-btn').addEventListener('click', () => closeAssetMoveModal(false));
document.getElementById('asset-move-modal-close').addEventListener('click', () => closeAssetMoveModal(false));
assetMoveModal.addEventListener('click', (e) => { if(e.target === assetMoveModal) closeAssetMoveModal(false); });
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && assetMoveModalOpen()){ e.stopImmediatePropagation(); closeAssetMoveModal(false); }
}, true);   // 캡처 단계에서 먼저 받아, 뒤의 라이트박스가 같은 Esc로 같이 닫히지 않게 한다

document.getElementById('gallery-move-selected-btn').addEventListener('click', () => openAssetMoveModal('image', Array.from(selectedGalleryNames)));
document.getElementById('video-gallery-move-selected-btn').addEventListener('click', () => openAssetMoveModal('video', Array.from(selectedGalleryVideoNames)));

// ---- 업로드 (이미지/영상 공용) ----
// 밖에서 만든 이미지·영상(뎁스·포즈·참고 자료)을 결과물로 들인다 — 서버가 출력 폴더 uploads/날짜/에 저장하고
// 바로 색인하므로 갤러리·태그·이동·보드 카드를 생성 결과와 똑같이 쓴다. 버튼으로 고르거나 목록에 끌어다
// 놓는다. 프로젝트 안에서 올리면 그 프로젝트가, 밖에서 올리면 미분류가 기본이다.
const assetUploadModal = document.getElementById('asset-upload-modal');
let assetUploadFiles = null;   // 창이 열려 있는 동안 올릴 File 목록
let assetUploadXhr = null;     // 올리는 중인 요청(창을 닫으면 멈춘다)

function assetUploadModalOpen(){ return assetUploadModal.style.display !== 'none'; }

async function openAssetUploadModal(files){
  if(assetUploadFiles) return;
  // 형식은 서버가 확장자로 다시 본다 — 브라우저가 형식을 모르는 파일(빈 type)은 일단 보낸다.
  files = files.filter(f => !f.type || /^(image|video)\//.test(f.type));
  if(!files.length){ flashNotice('이미지나 영상 파일만 올릴 수 있어요'); return; }
  assetUploadFiles = files;
  await fetchProjects();
  const here = typeof currentProjectId === 'number' ? currentProjectId : null;
  const select = document.getElementById('asset-upload-select');
  select.innerHTML = [{ value: '', label: UNASSIGNED_LABEL },
    ...projectsCache.filter(p => !p.archived || p.id === here).map(p => ({ value: String(p.id), label: p.name }))]
    .map(c => `<option value="${escapeHtml(c.value)}">${escapeHtml(c.label)}</option>`).join('')
    + `<option value="${NEW_PROJECT_VALUE}">${NEW_PROJECT_LABEL}</option>`;
  select.value = here === null ? '' : String(here);
  document.getElementById('asset-upload-new-name').value = '';
  syncAssetUploadNewField(false);
  document.getElementById('asset-upload-tags').value = '';
  const videos = files.filter(f => f.type.startsWith('video/')).length;
  const mb = files.reduce((s, f) => s + f.size, 0) / 1024 / 1024;
  const counts = [files.length - videos ? `이미지 ${files.length - videos}개` : '', videos ? `영상 ${videos}개` : ''].filter(Boolean).join(' · ');
  document.getElementById('asset-upload-hint').textContent = `${counts} (${mb < 1 ? mb.toFixed(2) : mb.toFixed(1)}MB)를 올려요. 파일 하나는 200MB까지예요.`;
  const statusEl = document.getElementById('asset-upload-status');
  statusEl.textContent = '';
  statusEl.classList.remove('error', 'success');
  const btn = document.getElementById('asset-upload-confirm-btn');
  btn.disabled = false;
  btn.style.display = '';
  assetUploadModal.style.display = 'flex';
  select.focus();
}
const syncAssetUploadNewField = bindNewProjectField('asset-upload-select', 'asset-upload-new-name');

function closeAssetUploadModal(){
  if(assetUploadXhr){ assetUploadXhr.abort(); assetUploadXhr = null; }
  assetUploadModal.style.display = 'none';
  assetUploadFiles = null;
}

// fetch는 올리는 진행률을 알려 주지 않아서(큰 영상이면 필요) XHR로 보낸다 — 전역 fetch 래퍼를 안 거치므로 헤더를 직접 붙인다.
function uploadAssetFiles(files, projectId, tags, onProgress){
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach(f => form.append('files', f));
    if(projectId !== null) form.append('project_id', String(projectId));
    if(tags) form.append('tags', tags);
    const xhr = assetUploadXhr = new XMLHttpRequest();
    xhr.open('POST', '/api/output-assets/upload');
    xhr.setRequestHeader('X-Requested-With', 'nightshift');
    xhr.upload.onprogress = (e) => { if(e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try{ data = JSON.parse(xhr.responseText); }catch(e){ /* 아래에서 실패로 처리 */ }
      if(xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || '올리지 못했어요.'));
    };
    xhr.onerror = () => reject(new Error('연결이 끊겨 올리지 못했어요.'));
    xhr.onabort = () => reject(new Error('취소했어요.'));
    xhr.send(form);
  });
}

document.getElementById('asset-upload-confirm-btn').addEventListener('click', async () => {
  const files = assetUploadFiles;
  if(!files) return;
  const btn = document.getElementById('asset-upload-confirm-btn');
  const statusEl = document.getElementById('asset-upload-status');
  const value = document.getElementById('asset-upload-select').value;
  let projectId = value === '' || value === NEW_PROJECT_VALUE ? null : Number(value);
  btn.disabled = true;
  statusEl.classList.remove('error');
  if(value === NEW_PROJECT_VALUE){
    const name = document.getElementById('asset-upload-new-name').value.trim();
    if(!name){
      statusEl.classList.add('error');
      statusEl.textContent = '새 프로젝트 이름을 입력하세요.';
      btn.disabled = false;
      return;
    }
    statusEl.textContent = '프로젝트를 만드는 중…';
    try{
      projectId = (await createProjectByName(name)).id;
    }catch(e){
      statusEl.classList.add('error');
      statusEl.textContent = e.message || '프로젝트를 만들지 못했어요.';
      btn.disabled = false;
      return;
    }
  }
  statusEl.textContent = '올리는 중… 0%';
  let data;
  try{
    data = await uploadAssetFiles(files, projectId, document.getElementById('asset-upload-tags').value.trim(),
      (r) => { statusEl.textContent = r < 1 ? `올리는 중… ${Math.floor(r * 100)}%` : '저장하는 중…'; });
  }catch(e){
    if(!assetUploadModalOpen()) return;   // 창을 닫아 멈춘 것
    statusEl.classList.add('error');
    statusEl.textContent = e.message;
    btn.disabled = false;
    return;
  }finally{
    assetUploadXhr = null;
  }
  const where = projectId === null ? UNASSIGNED_LABEL : projectName(projectId);
  const done = data.paths.length;
  fetchGalleryImages();
  fetchGalleryVideos();
  fetchProjects();
  if(data.errors && data.errors.length){
    // 일부만 올라갔으면 창을 닫지 않고 무엇이 빠졌는지 보여 준다(다시 올리면 올라간 것이 겹치므로 버튼은 숨긴다).
    statusEl.classList.add('error');
    statusEl.textContent = `${done}개는 '${where}'에 올렸어요. 올리지 못한 파일: ${data.errors.join(' / ')}`;
    btn.style.display = 'none';
    assetUploadFiles = [];
    return;
  }
  flashNotice(`${done}개를 '${where}'에 올렸어요`);
  closeAssetUploadModal();
});
document.getElementById('asset-upload-cancel-btn').addEventListener('click', closeAssetUploadModal);
document.getElementById('asset-upload-modal-close').addEventListener('click', closeAssetUploadModal);
assetUploadModal.addEventListener('click', (e) => { if(e.target === assetUploadModal) closeAssetUploadModal(); });
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && assetUploadModalOpen()){ e.stopImmediatePropagation(); closeAssetUploadModal(); }
}, true);

const assetUploadInput = document.getElementById('asset-upload-input');
assetUploadInput.addEventListener('change', () => {
  const files = Array.from(assetUploadInput.files);
  assetUploadInput.value = '';   // 같은 파일을 다시 골라도 change가 오게
  openAssetUploadModal(files);
});
for(const id of ['gallery-upload-btn', 'video-gallery-upload-btn']){
  document.getElementById(id).addEventListener('click', () => assetUploadInput.click());
}
// 목록에 파일을 끌어다 놓기 — 바깥 파일일 때만(갤러리 안의 카드 끌기는 types에 Files가 없다).
for(const id of ['tab-gallery', 'tab-video-gallery']){
  const tab = document.getElementById(id);
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files');
  tab.addEventListener('dragover', (e) => {
    if(!hasFiles(e)) return;
    e.preventDefault();
    tab.classList.add('upload-drop');
  });
  tab.addEventListener('dragleave', (e) => { if(!tab.contains(e.relatedTarget)) tab.classList.remove('upload-drop'); });
  tab.addEventListener('drop', (e) => {
    if(!hasFiles(e)) return;
    e.preventDefault();
    tab.classList.remove('upload-drop');
    openAssetUploadModal(Array.from(e.dataTransfer.files));
  });
}

// ---- 라이트박스 정보 패널 ----
// 접힌 상태에선 ★ 즐겨찾기 · 평점 한 줄만 보이고, 펼치면 태그·메모·생성 정보(프롬프트/시드 등)가 나온다.
// 같은 코드를 이미지/영상 라이트박스가 각자의 컨테이너에 붙여 쓴다.
function createAssetMetaPanel(root, kind){
  root.innerHTML = `
    <div class="am-bar">
      <button type="button" class="am-fav" aria-pressed="false" title="즐겨찾기" aria-label="즐겨찾기">${ico('star')}</button>
      <span class="am-stars" role="group" aria-label="평점">${[1, 2, 3, 4, 5].map(n =>
        `<button type="button" class="am-star" data-star="${n}" aria-label="${n}점" title="${n}점">${ico('star')}</button>`).join('')}</span>
      <button type="button" class="am-nsfw" aria-pressed="false" title="NSFW(성인) 콘텐츠로 표시" aria-label="NSFW 표시">${ico('flame')}</button>
      <span class="am-status" aria-live="polite"></span>
      <button type="button" class="am-toggle" aria-expanded="false" title="태그 · 메모 · 생성 정보">${ico('chevron-down')}<span class="am-toggle-label">정보</span><span class="am-toggle-count"></span></button>
    </div>
    <div class="am-body" hidden>
      <div class="am-tags"></div>
      <input type="text" class="option-input am-tag-input" list="am-tag-list" maxlength="40" placeholder="태그 추가 — Enter 또는 쉼표로 확정" autocomplete="off">
      <textarea class="option-input am-note" rows="2" maxlength="4000" placeholder="메모"></textarea>
      <div class="am-info"></div>
    </div>`;
  const $ = (sel) => root.querySelector(sel);
  const favBtn = $('.am-fav'), starBtns = [...root.querySelectorAll('.am-star')], statusEl = $('.am-status');
  const nsfwBtn = $('.am-nsfw');
  const toggle = $('.am-toggle'), body = $('.am-body'), tagsEl = $('.am-tags'), tagInput = $('.am-tag-input');
  const noteEl = $('.am-note'), infoEl = $('.am-info'), countEl = $('.am-toggle-count');
  const wrap = root.closest('.lightbox-img-wrap');
  let path = null, state = { favorite: false, rating: 0, nsfw: false, tags: [] }, savedNote = '', statusTimer = null;
  // 라이트박스를 열자마자(정보를 아직 받아오는 중에) 별점/즐겨찾기/NSFW를 바로 누르면, 뒤늦게 도착한
  // 조회 응답이 그 사이의 내 클릭을 덮어써 버릴 수 있었다 — 클릭한 값은 다시 불러오지 않게 기억해 둔다.
  let dirty = { favorite: false, rating: false, nsfw: false };

  function say(text){
    statusEl.textContent = text;
    clearTimeout(statusTimer);
    if(text) statusTimer = setTimeout(() => { statusEl.textContent = ''; }, 1500);
  }
  function paintBar(){
    favBtn.classList.toggle('on', state.favorite);
    favBtn.setAttribute('aria-pressed', state.favorite ? 'true' : 'false');
    starBtns.forEach((b, i) => b.classList.toggle('on', i < state.rating));
    nsfwBtn.classList.toggle('on', state.nsfw);
    nsfwBtn.setAttribute('aria-pressed', state.nsfw ? 'true' : 'false');
    countEl.textContent = state.tags.length ? ` · 태그 ${state.tags.length}` : '';
  }
  function paintTags(){
    tagsEl.innerHTML = state.tags.length
      ? state.tags.map(t => `<span class="am-chip">${escapeHtml(t)}<button type="button" data-remove="${escapeHtml(t)}" aria-label="${escapeHtml(t)} 태그 떼기">${ico('x')}</button></span>`).join('')
      : '<span class="am-empty">태그가 없어요</span>';
  }
  function setOpen(open){
    assetMetaOpen = open;
    try{ localStorage.setItem(AM_OPEN_KEY, open ? '1' : '0'); }catch(e){ /* 무시 */ }
    body.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.classList.toggle('open', open);
    if(wrap) wrap.classList.toggle('meta-open', open);
  }
  function row(label, value){
    return value == null || value === '' ? '' : `<div class="am-kv"><dt>${label}</dt><dd>${escapeHtml(String(value))}</dd></div>`;
  }
  function paintInfo(d){
    let params = {};
    try{ params = d.params_json ? JSON.parse(d.params_json) : {}; }catch(e){ /* 못 읽으면 빈 값 */ }
    const paramText = [
      params.sampler_name, params.scheduler, params.steps != null ? `${params.steps} steps` : null,
      params.cfg != null ? `cfg ${params.cfg}` : null, params.denoise != null && params.denoise !== 1 ? `denoise ${params.denoise}` : null,
    ].filter(Boolean).join(' · ');
    const dims = d.width && d.height ? `${d.width}×${d.height}` : null;
    const prompt = d.prompt
      ? `<div class="am-prompt-head"><span>프롬프트</span><button type="button" class="load-btn am-copy" data-copy="prompt">${ico('copy')} 복사</button></div><div class="am-prompt">${escapeHtml(d.prompt)}</div>`
      : '';
    const negative = d.negative_prompt
      ? `<details class="am-neg"><summary>네거티브 프롬프트</summary><div class="am-prompt">${escapeHtml(d.negative_prompt)}</div></details>`
      : '';
    const facts = row('작업', d.job_label) + row('워커', d.pod_name) + row('체크포인트', d.checkpoint)
      + row('시드', d.seed) + row('샘플링', paramText) + row('LoRA', (params.loras || []).join(', ')) + row('해상도', dims);
    infoEl.innerHTML = (facts ? `<dl class="am-facts">${facts}</dl>` : '') + prompt + negative
      || '<span class="am-empty">ComfyUI가 남긴 생성 정보가 없어요(회전·편집으로 지워졌거나 직접 넣은 파일일 수 있어요).</span>';
    infoEl._detail = d;
  }

  favBtn.addEventListener('click', async () => {
    if(!path) return;
    dirty.favorite = true;
    const next = !state.favorite;
    state.favorite = next; paintBar();
    if(!await setAssetsFavorite(kind, [path], next)){ state.favorite = !next; paintBar(); say('저장 실패'); }
  });
  starBtns.forEach(btn => btn.addEventListener('click', async () => {
    if(!path) return;
    dirty.rating = true;
    const n = Number(btn.dataset.star);
    const next = state.rating === n ? 0 : n;    // 같은 별을 다시 누르면 평점 해제
    const prev = state.rating;
    state.rating = next; paintBar();
    if(!await setAssetRating(kind, path, next)){ state.rating = prev; paintBar(); say('저장 실패'); }
  }));
  nsfwBtn.addEventListener('click', async () => {
    if(!path) return;
    dirty.nsfw = true;
    const next = !state.nsfw;
    state.nsfw = next; paintBar();
    if(!await setAssetsNsfw(kind, [path], next)){ state.nsfw = !next; paintBar(); say('저장 실패'); }
  });
  toggle.addEventListener('click', () => setOpen(body.hidden));

  async function addTags(text){
    const { add } = parseTagInput(text.replace(/^\s*-/, ''));
    if(!path || add.length === 0) return;
    const result = await changeAssetTags(kind, [path], add, []);
    if(result === null){ say('태그 저장 실패'); return; }
    state.tags = result[path] || state.tags; paintBar(); paintTags(); say('저장됨');
  }
  tagInput.addEventListener('keydown', (e) => {
    if(e.key === 'Enter' || e.key === ','){
      e.preventDefault();
      const value = tagInput.value;
      tagInput.value = '';
      addTags(value);
    }
  });
  tagInput.addEventListener('change', () => {   // 자동완성 목록에서 고른 경우
    if(tagInput.value.trim()){ const value = tagInput.value; tagInput.value = ''; addTags(value); }
  });
  tagsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-remove]');
    if(!btn || !path) return;
    const result = await changeAssetTags(kind, [path], [], [btn.dataset.remove]);
    if(result === null){ say('태그 저장 실패'); return; }
    state.tags = result[path] || []; paintBar(); paintTags(); say('저장됨');
  });
  async function saveNote(){
    if(!path || noteEl.value === savedNote) return;
    const target = path, value = noteEl.value;
    try{
      const res = await assetJson('/api/output-assets/update', { path: target, note: value });
      if(!res.ok) throw new Error('failed');
      savedNote = value; say('저장됨');
    }catch(e){ say('메모 저장 실패'); }
  }
  noteEl.addEventListener('blur', saveNote);
  noteEl.addEventListener('keydown', (e) => { if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); saveNote(); } });
  infoEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy]');
    if(!btn || !infoEl._detail) return;
    try{ await copyTextToClipboard(infoEl._detail.prompt || ''); say('복사됨'); }catch(err){ say('복사 실패'); }
  });

  return {
    async show(item){
      saveNote();
      path = item.name;
      state = { favorite: !!item.favorite, rating: item.rating || 0, nsfw: !!item.nsfw, tags: (item.tags || []).slice() };
      dirty = { favorite: false, rating: false, nsfw: false };
      savedNote = ''; noteEl.value = ''; infoEl.innerHTML = '<span class="am-empty">불러오는 중…</span>'; infoEl._detail = null;
      statusEl.textContent = '';
      paintBar(); paintTags(); setOpen(assetMetaOpen);
      const requested = path;
      try{
        const res = await fetch(`/api/output-assets/detail?path=${encodeURIComponent(requested)}`);
        if(!res.ok) throw new Error('failed');
        const d = await res.json();
        if(path !== requested) return;   // 그 사이 다른 이미지로 넘어갔으면 버린다
        state.tags = d.tags || state.tags;
        if(!dirty.favorite) state.favorite = !!d.favorite;
        if(!dirty.rating) state.rating = d.rating || 0;
        if(!dirty.nsfw) state.nsfw = !!d.nsfw;
        savedNote = d.note || ''; noteEl.value = savedNote;
        paintBar(); paintTags(); paintInfo(d);
      }catch(e){
        if(path === requested) infoEl.innerHTML = '<span class="am-empty">정보를 불러오지 못했어요.</span>';
      }
    },
  };
}

const imageMetaPanel = createAssetMetaPanel(document.getElementById('gallery-lightbox-meta'), 'image');
const videoMetaPanel = createAssetMetaPanel(document.getElementById('video-gallery-lightbox-meta'), 'video');

