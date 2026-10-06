// ---- Library 탭 — 지금은 Pose 서브탭 하나(NS-39) ----
// 목록은 서버(/api/library/poses)가 기준이다. 회원은 자기가 올린 것만, admin은 전부 받는다.
// 보기(Grid/Details)는 갤러리처럼 토글로 바꾸고 localStorage에 기억한다.
const LIBRARY_DISPLAY_KEY = 'nightshift.libraryDisplay';
let libraryPoses = [];
let libraryDisplayMode = (() => { try{ return localStorage.getItem(LIBRARY_DISPLAY_KEY) === 'details' ? 'details' : 'grid'; }catch(e){ return 'grid'; } })();
let poseAddImageMode = 'file';   // 'file' | 'url'

function setLibraryError(msg){ document.getElementById('library-error').textContent = msg || ''; }

// 탭 진입·새로고침은 목록으로 돌아간다(아티클 갤러리는 닫힌다).
async function openLibraryTab(){
  closeLibraryArticle();
  syncLibraryDisplayToggle();
  setLibraryError('');
  try{
    const res = await fetch('/api/library/poses');
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `불러오지 못했어요 (${res.status})`);
    libraryPoses = data.items || [];
    renderLibraryPoses();
  }catch(err){ setLibraryError(err.message); }
}

function syncLibraryDisplayToggle(){
  document.querySelectorAll('#library-display-toggle .enhance-mode-btn')
    .forEach(b => b.classList.toggle('active', b.dataset.display === libraryDisplayMode));
}

// 카드를 누르면 아티클 갤러리(그 게시물의 이미지 전부)가 열린다(NS-40).
function renderLibraryPoses(){
  const el = document.getElementById('library-list');
  const isDetails = libraryDisplayMode === 'details';
  el.className = isDetails ? 'library-details' : 'library-grid';
  document.getElementById('library-count').textContent = libraryPoses.length ? `${libraryPoses.length}개` : '';
  if(!libraryPoses.length){
    el.innerHTML = '<div class="dl-meta">아직 포즈가 없어요. 게시물 추가하기로 첫 포즈를 올려 보세요.</div>';
    return;
  }
  el.innerHTML = libraryPoses.map(p => `
    <button class="library-card" type="button" data-pose-id="${p.id}" title="이미지 모두 보기">
      <span class="library-thumb">
        <img src="${escapeHtml(p.thumb_url)}" alt="${escapeHtml(p.name)}" loading="lazy">
        ${p.image_count > 1 ? `<span class="library-thumb-count">${ico('layout-grid')} ${p.image_count}</span>` : ''}
      </span>
      <span class="library-info">
        <span class="library-name">${escapeHtml(p.name)}</span>
        <span class="library-desc">${escapeHtml(p.description || '')}</span>
        ${isDetails && p.danbooru_prompt ? `<span class="library-prompt">${escapeHtml(p.danbooru_prompt)}</span>` : ''}
      </span>
    </button>`).join('');
}

// ---- 아티클 갤러리 — 목록을 숨기고 게시물 하나의 이미지를 갤러리 격자로 ----
let libraryArticleId = null;
const libraryArticle = () => libraryPoses.find(p => p.id === libraryArticleId);

function openLibraryArticle(id){
  libraryArticleId = id;
  document.getElementById('library-list').hidden = true;
  document.getElementById('library-article').hidden = false;
  renderLibraryArticle();
}

function closeLibraryArticle(){
  libraryArticleId = null;
  document.getElementById('library-list').hidden = false;
  document.getElementById('library-article').hidden = true;
}

function renderLibraryArticle(){
  const p = libraryArticle();
  if(!p) return closeLibraryArticle();   // 새로고침 뒤 사라진 게시물
  document.getElementById('library-article-name').textContent = p.name;
  document.getElementById('library-article-count').textContent = `${p.images.length}장`;
  document.getElementById('library-article-desc').textContent = p.description || '';
  document.getElementById('library-article-prompt').textContent = p.danbooru_prompt || '';
  document.getElementById('library-article-grid').innerHTML = p.images.map((im, i) => `
    <div class="gallery-item" data-index="${i}" role="button" tabindex="0" title="크게 보기">
      <img src="${escapeHtml(im.thumb_url)}" alt="${escapeHtml(p.name)} ${i + 1}" loading="lazy">
    </div>`).join('');
}

document.getElementById('library-list').addEventListener('click', (e) => {
  const card = e.target.closest('.library-card');
  if(card) openLibraryArticle(Number(card.dataset.poseId));
});
document.getElementById('library-article-back').addEventListener('click', closeLibraryArticle);
document.getElementById('library-article-grid').addEventListener('click', (e) => {
  const item = e.target.closest('.gallery-item');
  if(item) openLibraryLightbox(Number(item.dataset.index));
});
document.getElementById('library-article-grid').addEventListener('keydown', (e) => {
  const item = e.target.closest('.gallery-item');
  if(item && (e.key === 'Enter' || e.key === ' ')){ e.preventDefault(); openLibraryLightbox(Number(item.dataset.index)); }
});
document.querySelectorAll('#library-subtabs [data-library-tab]').forEach(btn => btn.addEventListener('click', closeLibraryArticle));

// ---- Library 라이트박스 — 갤러리 라이트박스와 같은 UI, 공용 함수(15-gallery.js)를 overlay만 바꿔 부른다 ----
const libraryLightbox = document.getElementById('library-lightbox');
const libraryLightboxImg = document.getElementById('library-lightbox-img');
let libraryLightboxIndex = 0;

function renderLibraryLightbox(){
  const p = libraryArticle();
  const im = p && p.images[libraryLightboxIndex];
  if(!im) return closeLibraryLightbox();
  const spinner = document.getElementById('library-lightbox-spinner');
  spinner.style.display = '';
  libraryLightboxImg.onload = libraryLightboxImg.onerror = () => { spinner.style.display = 'none'; };
  libraryLightboxImg.src = im.image_url;
  libraryLightboxImg.alt = p.name;
  document.getElementById('library-lightbox-info').textContent = `${p.name} · ${libraryLightboxIndex + 1}/${p.images.length}`;
  const many = p.images.length > 1;
  document.getElementById('library-lightbox-prev').style.display = many ? '' : 'none';
  document.getElementById('library-lightbox-next').style.display = many ? '' : 'none';
}

function openLibraryLightbox(i){
  libraryLightboxIndex = i;
  libraryLightbox.style.display = 'flex';
  renderLibraryLightbox();
}

function closeLibraryLightbox(){
  if(isLightboxFullscreen(libraryLightbox)) setLightboxFullscreen(libraryLightbox, false);
  libraryLightbox.style.display = 'none';
  libraryLightboxImg.removeAttribute('src');
}

function stepLibraryLightbox(d){
  const n = (libraryArticle()?.images || []).length;
  if(n < 2) return;
  libraryLightboxIndex = (libraryLightboxIndex + d + n) % n;
  renderLibraryLightbox();
}

setupLightboxSwipe(libraryLightbox, libraryLightboxImg, {
  onNext: () => stepLibraryLightbox(1), onPrev: () => stepLibraryLightbox(-1),
});
document.getElementById('library-lightbox-close').addEventListener('click', closeLibraryLightbox);
document.getElementById('library-lightbox-fs-btn').addEventListener('click', () => toggleLightboxFullscreen(libraryLightbox));
document.getElementById('library-lightbox-prev').addEventListener('click', () => stepLibraryLightbox(-1));
document.getElementById('library-lightbox-next').addEventListener('click', () => stepLibraryLightbox(1));
document.getElementById('library-lightbox-download-btn').addEventListener('click', () => {
  const p = libraryArticle(), im = p && p.images[libraryLightboxIndex];
  if(im) triggerAnchorDownload(im.image_url, `${p.name}-${libraryLightboxIndex + 1}`);
});
document.getElementById('library-lightbox-original-btn').addEventListener('click', () => {
  const im = libraryArticle()?.images[libraryLightboxIndex];
  if(im) window.open(im.image_url, '_blank', 'noopener');
});
document.addEventListener('keydown', (e) => {
  if(libraryLightbox.style.display === 'none') return;
  if(e.target.closest && e.target.closest('input, textarea, select')) return;
  if(e.key === 'Escape'){ if(isLightboxFullscreen(libraryLightbox)) setLightboxFullscreen(libraryLightbox, false); else closeLibraryLightbox(); }
  else if(e.key === 'f' || e.key === 'F') toggleLightboxFullscreen(libraryLightbox);
  else if(e.key === ']' && isLightboxFullscreen(libraryLightbox)) rotateLightboxView(libraryLightbox, 90);
  else if(e.key === '[' && isLightboxFullscreen(libraryLightbox)) rotateLightboxView(libraryLightbox, -90);
  else if(e.key === 'ArrowLeft') stepLibraryLightbox(-1);
  else if(e.key === 'ArrowRight') stepLibraryLightbox(1);
});

document.querySelectorAll('#library-display-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.display === libraryDisplayMode) return;
    libraryDisplayMode = btn.dataset.display;
    try{ localStorage.setItem(LIBRARY_DISPLAY_KEY, libraryDisplayMode); }catch(e){ /* 무시 */ }
    syncLibraryDisplayToggle();
    renderLibraryPoses();
  });
});
document.getElementById('library-refresh-btn').addEventListener('click', openLibraryTab);

// ---- 게시물 추가 모달 ----
const poseAddModal = document.getElementById('pose-add-modal');
const poseAddPreview = document.getElementById('pose-add-preview');

function setPoseAddError(msg){ document.getElementById('pose-add-error').textContent = msg || ''; }

function setPoseAddImageMode(mode){
  poseAddImageMode = mode;
  document.querySelectorAll('#pose-add-image-mode .enhance-mode-btn')
    .forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('pose-add-file').hidden = mode !== 'file';
  document.getElementById('pose-add-url').hidden = mode !== 'url';
  updatePoseAddPreview();
}

// 같은 모달을 두 가지로 쓴다 — 새 게시물(poseAddTargetId=null)과 열린 게시물에 이미지 추가(명칭·설명 칸 숨김).
let poseAddTargetId = null;
let poseAddBlobs = [];
const POSE_ADD_MAX = 20;   // 서버 pose_library.MAX_IMAGES와 같다

function poseAddUrls(){
  return document.getElementById('pose-add-url').value.split('\n').map(u => u.trim()).filter(Boolean);
}

function revokePoseAddBlobs(){ poseAddBlobs.forEach(u => URL.revokeObjectURL(u)); poseAddBlobs = []; }

function updatePoseAddPreview(){
  revokePoseAddBlobs();
  let srcs;
  if(poseAddImageMode === 'file'){
    srcs = poseAddBlobs = [...document.getElementById('pose-add-file').files].map(f => URL.createObjectURL(f));
  }else{
    srcs = poseAddUrls().filter(u => /^https?:\/\//i.test(u));
  }
  poseAddPreview.hidden = !srcs.length;
  poseAddPreview.innerHTML = srcs.map(s => `<img src="${escapeHtml(s)}" alt="미리보기">`).join('');
}

function openPoseAddModal(targetId = null){
  poseAddTargetId = targetId;
  document.getElementById('pose-add-form').reset();
  document.getElementById('pose-add-title').textContent = targetId ? '이미지 추가' : 'Pose 추가';
  document.querySelectorAll('#pose-add-form .pose-add-meta').forEach(el => { el.hidden = !!targetId; });
  document.getElementById('pose-add-name').required = !targetId;
  setPoseAddError('');
  setPoseAddImageMode('file');
  poseAddModal.style.display = 'flex';
  if(!targetId) document.getElementById('pose-add-name').focus();
}

function closePoseAddModal(){
  poseAddModal.style.display = 'none';
  revokePoseAddBlobs();
}

async function savePoseAdd(e){
  e.preventDefault();
  const name = document.getElementById('pose-add-name').value.trim();
  const fd = new FormData();
  if(!poseAddTargetId){
    fd.append('name', name);
    fd.append('description', document.getElementById('pose-add-desc').value);
    fd.append('danbooru_prompt', document.getElementById('pose-add-prompt').value);
  }
  let count;
  if(poseAddImageMode === 'file'){
    const files = [...document.getElementById('pose-add-file').files];
    files.forEach(f => fd.append('image', f));
    count = files.length;
  }else{
    const urls = poseAddUrls();
    urls.forEach(u => fd.append('image_url', u));
    count = urls.length;
  }
  if(!poseAddTargetId && !name) return setPoseAddError('Pose 명칭을 적어주세요.');
  if(!count) return setPoseAddError('이미지를 올리거나 이미지 주소를 적어주세요.');
  if(count > POSE_ADD_MAX) return setPoseAddError(`이미지는 한 번에 ${POSE_ADD_MAX}장까지 넣을 수 있어요.`);
  const btn = document.getElementById('pose-add-save');
  btn.disabled = true; btn.textContent = '저장 중…';
  setPoseAddError('');
  const targetId = poseAddTargetId;
  try{
    const url = targetId ? `/api/library/poses/${targetId}/images` : '/api/library/poses';
    const res = await fetch(url, { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `저장하지 못했어요 (${res.status})`);
    closePoseAddModal();
    await openLibraryTab();
    if(targetId) openLibraryArticle(targetId);   // 이미지를 덧붙였으면 그 게시물 갤러리로 돌아간다
  }catch(err){ setPoseAddError(err.message); }
  finally{ btn.disabled = false; btn.textContent = '저장'; }
}

document.getElementById('library-add-btn').addEventListener('click', () => openPoseAddModal());
document.getElementById('library-article-add-btn').addEventListener('click', () => openPoseAddModal(libraryArticleId));
document.getElementById('pose-add-modal-close').addEventListener('click', closePoseAddModal);
document.getElementById('pose-add-cancel').addEventListener('click', closePoseAddModal);
document.getElementById('pose-add-form').addEventListener('submit', savePoseAdd);
document.getElementById('pose-add-file').addEventListener('change', updatePoseAddPreview);
document.getElementById('pose-add-url').addEventListener('input', updatePoseAddPreview);
document.querySelectorAll('#pose-add-image-mode .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => setPoseAddImageMode(btn.dataset.mode));
});
