// ---- Library 탭 — Pose·Position·Lighting 공용 화면 ----
// 목록은 서버(/api/library/poses)가 기준이다. 회원은 자기가 올린 것만, admin은 전부 받는다.
// 보기(Grid/Details)는 갤러리처럼 토글로 바꾸고 localStorage에 기억한다.
const LIBRARY_DISPLAY_KEY = 'nightshift.libraryDisplay';
let libraryPoses = [];
let libraryDisplayMode = (() => { try{ return localStorage.getItem(LIBRARY_DISPLAY_KEY) === 'details' ? 'details' : 'grid'; }catch(e){ return 'grid'; } })();
let poseAddImageMode = 'file';   // 'file' | 'url'

// 서브탭 — 세 종류가 같은 목록·아티클·라이트박스·추가 모달을 쓰고 API 주소·문구·생성 동작만 다르다.
let libraryTab = 'pose';   // 'pose' | 'position' | 'lighting'
const LIBRARY_API = { pose: '/api/library/poses', position: '/api/library/positions', lighting: '/api/library/lightings' };
const libraryApi = () => LIBRARY_API[libraryTab];
const isPositionTab = () => libraryTab === 'position';
const libraryKindLabel = kind => ({ pose: 'Pose', position: 'Position', lighting: 'Lighting' })[kind];
const libraryNoun = () => libraryTab === 'pose' ? '포즈' : libraryKindLabel(libraryTab);

function setLibraryError(msg){ document.getElementById('library-error').textContent = msg || ''; }

function setLibraryTab(tab){
  libraryTab = tab;
  librarySelected.clear();
  document.querySelectorAll('#library-subtabs [data-library-tab]')
    .forEach(b => b.classList.toggle('active', b.dataset.libraryTab === tab));
  document.getElementById('library-sequence-btn').title =
    `체크한 게시물(없으면 지금 목록 전부)의 모든 이미지를 ${libraryNoun()}로 차례로 생성`;
}

// 탭 진입·새로고침은 목록으로 돌아간다(아티클 갤러리는 닫힌다).
async function openLibraryTab(){
  closeLibraryArticle();
  syncLibraryDisplayToggle();
  setLibraryError('');
  libraryPoses = [];
  try{
    const res = await fetch(libraryApi());
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `불러오지 못했어요 (${res.status})`);
    libraryPoses = data.items || [];
    renderLibraryPoses();
  }catch(err){ setLibraryError(err.message); }
  const want = libraryPendingArticle;
  libraryPendingArticle = null;
  if(want != null){
    if(libraryPoses.some(p => p.id === want)) openLibraryArticle(want);
    else setLibraryError('게시물을 찾을 수 없어요');
  }
}

// 갤러리 정보의 "포즈" 링크(NS-42) — Library 탭으로 가서 목록을 받은 뒤 그 게시물을 연다.
// 볼 수 없거나 지워진 게시물은 목록에 없으므로 안내만 띄운다.
let libraryPendingArticle = null;
function openLibraryPose(id, tab = 'pose'){
  libraryPendingArticle = id;
  setLibraryTab(tab);
  showTab('library');
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
  syncLibrarySelection();
  if(!libraryPoses.length){
    el.innerHTML = `<div class="dl-meta">아직 ${libraryNoun()}가 없어요. 게시물 추가하기로 첫 ${libraryNoun()}를 올려 보세요.</div>`;
    return;
  }
  el.innerHTML = libraryPoses.map(p => `
    <div class="library-card" role="button" tabindex="0" data-pose-id="${p.id}" title="이미지 모두 보기">
      <span class="library-thumb">
        <img src="${escapeHtml(p.thumb_url)}" alt="${escapeHtml(p.name)}" loading="lazy">
        ${p.image_count > 1 ? `<span class="library-thumb-count">${ico('layout-grid')} ${p.image_count}</span>` : ''}
        ${p.images.length ? poseGenerateButton('library-card-pose-btn', p.images[0].id) : ''}
        <button class="library-check-btn" type="button" aria-pressed="${librarySelected.has(p.id)}" title="순차 생성에 넣기" aria-label="순차 생성에 넣기">${ico('square-check-big')}</button>
      </span>
      <span class="library-info">
        <span class="library-name">${escapeHtml(p.name)}</span>
        <span class="library-desc">${escapeHtml(p.description || '')}</span>
        ${isDetails && p.danbooru_prompt ? `<span class="library-prompt">${escapeHtml(p.danbooru_prompt)}</span>` : ''}
      </span>
    </div>`).join('');
}

// "이 포즈로 생성"(NS-41) — 카드·아티클 격자에 얹는 작은 아이콘 버튼. 라이트박스 버튼은 index.html에 있다.
function poseGenerateButton(cls, imageId){
  const label = libraryTab === 'pose' ? '이 포즈로 생성' : `이 ${libraryNoun()}으로 생성 (txt2img)`;
  return `<button class="library-pose-btn ${cls}" type="button" data-image-id="${imageId}" title="${label}" aria-label="${label}">${ico('list-checks')}</button>`;
}

// 아티클 격자의 "이 장 삭제"(NS-43, Pose는 NS-44) — 썸네일 오른쪽 위
function libraryImageDeleteButton(imageId){
  return `<button class="library-image-delete-btn" type="button" data-image-id="${imageId}" title="이 장 삭제" aria-label="이 장 삭제">${ico('trash-2')}</button>`;
}

// 순차 생성(NS-42) — 카드 체크로 고른 게시물. 아무것도 없으면 지금 목록 전부를 쓴다.
const librarySelected = new Set();
function syncLibrarySelection(){
  [...librarySelected].forEach(id => { if(!libraryPoses.some(p => p.id === id)) librarySelected.delete(id); });
  const all = libraryPoses.length > 0 && librarySelected.size === libraryPoses.length;
  const btn = document.getElementById('library-select-all-btn');
  btn.innerHTML = `${ico('square-check-big')} ${all ? '선택 해제' : '전체선택'}`;
  btn.disabled = !libraryPoses.length;
  document.getElementById('library-sequence-btn').disabled = !libraryPoses.length;
  document.getElementById('library-count').textContent = libraryPoses.length
    ? (librarySelected.size ? `${librarySelected.size}/${libraryPoses.length}개 선택` : `${libraryPoses.length}개`) : '';
}

// 생성 방식 고르기(NS-51) — Pose·Position 공통. 요청(탭·게시물·장)을 누른 순간에 고정해 두고,
// 사람이 "프롬프트"(프롬프트만) 또는 "Openpose CN"(이미지를 포즈로 + 프롬프트)을 누른 뒤에만 복사·마법사를 연다.
// 닫으면 아무것도 하지 않는다.
const libraryTransferModal = document.getElementById('library-transfer-modal');
let libraryTransferRun = null;     // 고른 방식('prompt' | 'openpose')으로 진행하는 함수
let libraryTransferFocus = null;   // 닫을 때 포커스를 돌려줄 버튼
function askLibraryTransfer(run){
  libraryTransferRun = run;
  libraryTransferFocus = document.activeElement;
  libraryTransferModal.style.display = 'flex';
  libraryTransferModal.querySelector('.library-transfer-btn').focus();
}
function closeLibraryTransfer(){
  libraryTransferRun = null;
  libraryTransferModal.style.display = 'none';
  if(libraryTransferFocus && libraryTransferFocus.isConnected) libraryTransferFocus.focus();
  libraryTransferFocus = null;
}
libraryTransferModal.querySelectorAll('.library-transfer-btn').forEach(b => b.addEventListener('click', () => {
  const run = libraryTransferRun;
  if(!run) return;   // 중복 클릭은 한 번만 진행한다
  closeLibraryTransfer();
  run(b.dataset.transfer);
}));
document.getElementById('library-transfer-close').addEventListener('click', closeLibraryTransfer);
libraryTransferModal.addEventListener('click', e => { if(e.target === libraryTransferModal) closeLibraryTransfer(); });
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && libraryTransferModal.style.display !== 'none') closeLibraryTransfer();
});

async function libraryToInput(url, body){
  const res = await fetch(url, body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.detail || `이미지를 가져오지 못했어요 (${res.status})`);
  return data;
}

// 체크한 게시물(목록 순서)로 순차 생성 마법사를 연다 — 장마다 한 항목.
function generatePoseSequence(){
  setLibraryError('');
  const kind = libraryTab;
  const posts = libraryPoses.filter(p => !librarySelected.size || librarySelected.has(p.id)).map(p => ({ ...p, images: p.images.map(im => ({ ...im })) }));
  if(!posts.length) return;
  if(kind === 'lighting') return runLibrarySequence(kind, posts, 'prompt');
  askLibraryTransfer(mode => runLibrarySequence(kind, posts, mode));
}
async function runLibrarySequence(kind, posts, mode){
  if(kind === 'lighting') mode = 'prompt';   // Lighting은 입력 이미지 복사·Openpose CN을 지원하지 않는다.
  if(mode === 'prompt'){
    // 프롬프트만 — 이미지 복사 없이 목록 응답으로 장마다 한 항목을 만든다(positionSequenceValue 모양).
    const items = posts.flatMap(p => p.images.map(im => ({
      source_kind: kind, article_id: p.id, source_image_id: im.id, generation_mode: mode,
      preview_url: im.thumb_url || im.image_url, article_name: p.name,
      position_id: p.id, position_name: p.name, danbooru_prompt: p.danbooru_prompt || '',
    })));
    if(!items.length) return setLibraryError('고른 게시물에 이미지가 없어요');
    return startPositionSequenceWizard(items);
  }
  const ids = posts.map(p => p.id);
  try{
    const data = kind === 'position'
      ? await libraryToInput('/api/library/positions/to-input', { position_ids: ids })
      : await libraryToInput('/api/library/poses/to-input', { pose_ids: ids });
    // Position 응답은 Pose 순차 항목(poseSequenceValue가 읽는 pose_id/pose_name) 모양으로 바꾼다.
    const items = (Array.isArray(data) ? data : []).map(it => {
      const articleId = kind === 'position' ? it.position_id : it.pose_id;
      const post = posts.find(p => p.id === articleId);
      const im = post?.images.find(im => im.id === it.image_id);
      return { ...it, source_kind: kind, article_id: articleId, source_image_id: it.image_id,
        generation_mode: mode, article_name: post?.name, preview_url: im?.thumb_url || im?.image_url };
    });
    if(!items.length) throw new Error('고른 게시물에 이미지가 없어요');
    await startOpenPoseSequenceWizard(items);
  }catch(err){ setLibraryError(err.message); }
}

// 고른 장 하나로 생성 — 프롬프트만이면 txt2img 시드 배치, Openpose CN이면 장을 입력 풀로 복사하고 OpenPose 마법사.
// 어느 쪽이든 danbooru prompt는 메인 프롬프트 끝에 붙는다.
function generateFromPose(poseId, imageId){
  setLibraryError('');
  const kind = libraryTab;
  const p = libraryPoses.find(x => x.id === poseId);
  if(!p) return;
  if(kind === 'lighting') return runLibrarySingle(kind, { ...p, images: p.images.map(im => ({ ...im })) }, imageId, 'prompt');
  askLibraryTransfer(mode => runLibrarySingle(kind, { ...p, images: p.images.map(im => ({ ...im })) }, imageId, mode));
}
async function runLibrarySingle(kind, p, imageId, mode){
  if(kind === 'lighting') mode = 'prompt';
  try{
    const data = mode === 'prompt' ? null
      : await libraryToInput(`/api/library/${kind === 'position' ? 'positions' : 'poses'}/${p.id}/images/${imageId}/to-input`);
    if(libraryLightbox.style.display !== 'none') closeLibraryLightbox();
    const im = p.images.find(im => im.id === imageId);
    const context = { source_kind: kind, article_id: p.id, source_image_id: imageId,
      generation_mode: mode, article_name: p.name, preview_url: im?.thumb_url || im?.image_url };
    if(data) await startOpenPoseWizard(data.name, data.danbooru_prompt, context);
    else await startPositionWizard(p.danbooru_prompt, null, context);
  }catch(err){ setLibraryError(err.message); }
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
  // 수정·삭제는 두 서브탭 모두(NS-44) — 마크업의 hidden을 연다
  document.getElementById('library-article-edit-btn').hidden = false;
  document.getElementById('library-article-delete-btn').hidden = false;
  const moveBtn = document.getElementById('library-article-move-btn');
  moveBtn.hidden = libraryTab === 'lighting';
  moveBtn.style.display = moveBtn.hidden ? 'none' : '';   // 버튼의 display 규칙보다 우선해 숨긴다.
  const moveLabel = isPositionTab() ? 'Pose로 이동' : 'Position으로 이동';
  moveBtn.title = moveLabel;
  moveBtn.setAttribute('aria-label', moveLabel);
  moveBtn.querySelector('.btn-label').textContent = moveLabel;
  document.getElementById('library-article-grid').innerHTML = p.images.map((im, i) => `
    <div class="gallery-item" data-index="${i}" role="button" tabindex="0" title="크게 보기">
      <img src="${escapeHtml(im.thumb_url)}" alt="${escapeHtml(p.name)} ${i + 1}" loading="lazy">
      ${poseGenerateButton('library-item-pose-btn', im.id)}
      ${libraryImageDeleteButton(im.id)}
    </div>`).join('');
}

// ---- Position 수정·삭제(NS-43) — 삭제는 confirm을 거친다. 마지막 한 장은 서버가 400으로 막는다 ----
async function libraryRequest(url, opts, failMsg){
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.detail || `${failMsg} (${res.status})`);
  return data;
}

async function deleteLibraryPost(){
  const p = libraryArticle();
  if(!p || !confirm(`'${p.name}' 게시물과 이미지 ${p.images.length}장을 모두 지울까요?`)) return;
  setLibraryError('');
  try{
    await libraryRequest(`${libraryApi()}/${p.id}`, { method: 'DELETE' }, '삭제하지 못했어요');
    await openLibraryTab();
  }catch(err){ setLibraryError(err.message); }
}

// Pose↔Position 이동(NS-46) — 서버가 대상 쪽에 새 게시물을 만들고 원본을 지운다(id가 바뀐다).
// 끝나면 대상 서브탭으로 바꾸고 새 게시물 아티클을 연다.
async function moveLibraryPost(){
  if(libraryTab === 'lighting') return;
  const p = libraryArticle();
  if(!p) return;
  const [target, path, label] = isPositionTab()
    ? ['pose', 'move-to-pose', 'Pose'] : ['position', 'move-to-position', 'Position'];
  if(!confirm(`'${p.name}' 게시물(이미지 ${p.images.length}장)을 ${label}로 옮길까요? 여기서는 사라져요.`)) return;
  setLibraryError('');
  try{
    const moved = await libraryRequest(`${libraryApi()}/${p.id}/${path}`, { method: 'POST' }, '옮기지 못했어요');
    libraryPendingArticle = moved.id;
    setLibraryTab(target);
    await openLibraryTab();
  }catch(err){ setLibraryError(err.message); }
}

async function deleteLibraryImage(imageId){
  const p = libraryArticle();
  if(!p) return;
  if(p.images.length < 2) return setLibraryError('마지막 한 장은 지울 수 없어요. 게시물 삭제를 쓰세요.');
  if(!confirm('이 이미지를 지울까요?')) return;
  setLibraryError('');
  try{
    await libraryRequest(`${libraryApi()}/${p.id}/images/${imageId}`, { method: 'DELETE' }, '삭제하지 못했어요');
    if(libraryLightbox.style.display !== 'none') closeLibraryLightbox();
    await openLibraryTab();
    openLibraryArticle(p.id);
  }catch(err){ setLibraryError(err.message); }
}

document.getElementById('library-list').addEventListener('click', (e) => {
  const card = e.target.closest('.library-card');
  if(!card) return;
  const check = e.target.closest('.library-check-btn');
  if(check){
    const id = Number(card.dataset.poseId);
    if(librarySelected.has(id)) librarySelected.delete(id); else librarySelected.add(id);
    check.setAttribute('aria-pressed', String(librarySelected.has(id)));
    syncLibrarySelection();
    return;
  }
  const btn = e.target.closest('.library-pose-btn');
  if(btn) generateFromPose(Number(card.dataset.poseId), Number(btn.dataset.imageId));
  else openLibraryArticle(Number(card.dataset.poseId));
});
document.getElementById('library-list').addEventListener('keydown', (e) => {
  const card = e.target.closest('.library-card');
  if(card && e.target === card && (e.key === 'Enter' || e.key === ' ')){ e.preventDefault(); openLibraryArticle(Number(card.dataset.poseId)); }
});
document.getElementById('library-select-all-btn').addEventListener('click', () => {
  const all = librarySelected.size === libraryPoses.length;
  librarySelected.clear();
  if(!all) libraryPoses.forEach(p => librarySelected.add(p.id));
  renderLibraryPoses();
});
document.getElementById('library-sequence-btn').addEventListener('click', generatePoseSequence);
document.getElementById('library-article-back').addEventListener('click', closeLibraryArticle);
document.getElementById('library-article-grid').addEventListener('click', (e) => {
  const del = e.target.closest('.library-image-delete-btn');
  if(del) return deleteLibraryImage(Number(del.dataset.imageId));
  const btn = e.target.closest('.library-pose-btn');
  if(btn) return generateFromPose(libraryArticleId, Number(btn.dataset.imageId));
  const item = e.target.closest('.gallery-item');
  if(item) openLibraryLightbox(Number(item.dataset.index));
});
document.getElementById('library-article-grid').addEventListener('keydown', (e) => {
  if(e.target.closest('.library-pose-btn, .library-image-delete-btn')) return;   // 버튼의 Enter는 버튼 자신의 click으로
  const item = e.target.closest('.gallery-item');
  if(item && (e.key === 'Enter' || e.key === ' ')){ e.preventDefault(); openLibraryLightbox(Number(item.dataset.index)); }
});
document.getElementById('library-article-edit-btn').addEventListener('click', () => openPoseAddModal(null, libraryArticle()));
document.getElementById('library-article-delete-btn').addEventListener('click', deleteLibraryPost);
document.getElementById('library-article-move-btn').addEventListener('click', moveLibraryPost);
document.querySelectorAll('#library-subtabs [data-library-tab]').forEach(btn => btn.addEventListener('click', () => {
  if(btn.dataset.libraryTab === libraryTab) return closeLibraryArticle();
  setLibraryTab(btn.dataset.libraryTab);
  openLibraryTab();
}));

// ---- Library 라이트박스 — 갤러리 라이트박스와 같은 UI, 공용 함수(15-gallery.js)를 overlay만 바꿔 부른다 ----
const libraryLightbox = document.getElementById('library-lightbox');
const libraryLightboxImg = document.getElementById('library-lightbox-img');
let libraryLightboxIndex = 0;

function renderLibraryLightbox(){
  resetLightboxZoom(libraryLightbox);
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
  document.getElementById('library-lightbox-delete-btn').hidden = false;
  document.querySelector('#library-lightbox-pose-btn .btn-label').textContent = libraryTab === 'pose' ? '이 포즈로 생성' : `이 ${libraryNoun()}으로 생성`;
  document.getElementById('library-lightbox-prev').style.display = many ? '' : 'none';
  document.getElementById('library-lightbox-next').style.display = many ? '' : 'none';
}

function openLibraryLightbox(i){
  libraryLightboxIndex = i;
  libraryLightbox.style.display = 'flex';
  renderLibraryLightbox();
}

function closeLibraryLightbox(){
  resetLightboxZoom(libraryLightbox);
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
document.getElementById('library-lightbox-pose-btn').addEventListener('click', () => {
  const im = libraryArticle()?.images[libraryLightboxIndex];
  if(im) generateFromPose(libraryArticleId, im.id);
});
document.getElementById('library-lightbox-delete-btn').addEventListener('click', () => {
  const im = libraryArticle()?.images[libraryLightboxIndex];
  if(im) deleteLibraryImage(im.id);
});
document.getElementById('library-lightbox-original-btn').addEventListener('click', () => {
  const im = libraryArticle()?.images[libraryLightboxIndex];
  if(im) window.open(im.image_url, '_blank', 'noopener');
});
document.addEventListener('keydown', (e) => {
  if(libraryLightbox.style.display === 'none' || libraryTransferModal.style.display !== 'none') return;
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
  document.getElementById('pose-add-gallery').hidden = mode !== 'gallery';
  updatePoseAddPreview();
}

// 갤러리 모드(NS-48): 결과 이미지 이름(job_id/파일.png)을 골라 output_name으로 보낸다 — 서버가 권한·경로를 검사한다.
let poseAddGalleryNames = [];

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
  }else if(poseAddImageMode === 'gallery'){
    srcs = poseAddGalleryNames.map(n => window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(n)}/thumbnail?size=80&fit=cover`));
    document.getElementById('pose-add-gallery-count').textContent = srcs.length ? `${srcs.length}장 골랐어요` : '';
  }else{
    srcs = poseAddUrls().filter(u => /^https?:\/\//i.test(u));
  }
  poseAddPreview.hidden = !srcs.length;
  poseAddPreview.innerHTML = srcs.map(s => `<img src="${escapeHtml(s)}" alt="미리보기">`).join('');
}

// 세 번째 쓰임(NS-43, Pose는 NS-44): 수정(edit=게시물) — 값을 채우고 이미지 칸을 숨긴다.
let poseAddEdit = null;
function openPoseAddModal(targetId = null, edit = null){
  poseAddTargetId = targetId;
  poseAddEdit = edit;
  document.getElementById('pose-add-form').reset();
  const kind = libraryKindLabel(libraryTab);
  document.getElementById('pose-add-title').textContent = targetId ? '이미지 추가' : edit ? `${kind} 수정` : `${kind} 추가`;
  document.querySelector('label[for="pose-add-name"]').textContent = `${kind} 명칭`;
  document.querySelectorAll('#pose-add-form .pose-add-meta').forEach(el => { el.hidden = !!targetId; });
  document.getElementById('pose-add-image-field').hidden = !!edit;
  document.getElementById('pose-add-name').required = !targetId;
  if(edit){
    document.getElementById('pose-add-name').value = edit.name || '';
    document.getElementById('pose-add-desc').value = edit.description || '';
    document.getElementById('pose-add-prompt').value = edit.danbooru_prompt || '';
  }
  setPoseAddError('');
  poseAddGalleryNames = [];
  setPoseAddImageMode('file');
  poseAddModal.style.display = 'flex';
  if(!targetId) document.getElementById('pose-add-name').focus();
}

async function savePoseEdit(){
  const name = document.getElementById('pose-add-name').value.trim();
  if(!name) return setPoseAddError(`${libraryKindLabel(libraryTab)} 명칭을 적어주세요.`);
  const id = poseAddEdit.id;
  const btn = document.getElementById('pose-add-save');
  btn.disabled = true; btn.textContent = '저장 중…';
  setPoseAddError('');
  try{
    await libraryRequest(`${libraryApi()}/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, description: document.getElementById('pose-add-desc').value,
        danbooru_prompt: document.getElementById('pose-add-prompt').value,
      }),
    }, '저장하지 못했어요');
    closePoseAddModal();
    await openLibraryTab();
    openLibraryArticle(id);
  }catch(err){ setPoseAddError(err.message); }
  finally{ btn.disabled = false; btn.textContent = '저장'; }
}

function closePoseAddModal(){
  poseAddModal.style.display = 'none';
  revokePoseAddBlobs();
}

async function savePoseAdd(e){
  e.preventDefault();
  if(poseAddEdit) return savePoseEdit();
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
  }else if(poseAddImageMode === 'gallery'){
    poseAddGalleryNames.forEach(n => fd.append('output_name', n));
    count = poseAddGalleryNames.length;
  }else{
    const urls = poseAddUrls();
    urls.forEach(u => fd.append('image_url', u));
    count = urls.length;
  }
  if(!poseAddTargetId && !name) return setPoseAddError(`${libraryKindLabel(libraryTab)} 명칭을 적어주세요.`);
  if(!count) return setPoseAddError('이미지를 올리거나, 주소를 적거나, 갤러리에서 골라주세요.');
  if(count > POSE_ADD_MAX) return setPoseAddError(`이미지는 한 번에 ${POSE_ADD_MAX}장까지 넣을 수 있어요.`);
  const btn = document.getElementById('pose-add-save');
  btn.disabled = true; btn.textContent = '저장 중…';
  setPoseAddError('');
  const targetId = poseAddTargetId;
  try{
    const url = targetId ? `${libraryApi()}/${targetId}/images` : libraryApi();
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
document.getElementById('pose-add-gallery-btn').addEventListener('click', () => openInputImageGalleryPicker(names => {
  poseAddGalleryNames = names;
  updatePoseAddPreview();
}, { multiple: true, max: POSE_ADD_MAX, selected: poseAddGalleryNames }));
document.querySelectorAll('#pose-add-image-mode .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => setPoseAddImageMode(btn.dataset.mode));
});

// ---- 갤러리에서 Library로 넣기(NS-45) — 결과 이미지 이름(output_name)만 보내고 서버가 경로·권한을 검사해 읽는다 ----
const lfgModal = document.getElementById('library-from-gallery-modal');
let lfgNames = [];
let lfgKind = 'pose';       // 'pose' | 'position' | 'lighting'
let lfgTarget = 'new';      // 'new' | 'existing'
let lfgDone = null;         // 저장한 게시물 { id, kind }
const lfgKindLabel = () => libraryKindLabel(lfgKind);
function setLfgError(msg){ document.getElementById('lfg-error').textContent = msg || ''; }

// 기존 게시물 목록은 Library 탭과 같은 API — 회원은 자기 것만, admin은 전부
async function loadLfgExisting(){
  const sel = document.getElementById('lfg-existing');
  sel.innerHTML = '<option value="">불러오는 중…</option>';
  try{
    const data = await libraryRequest(LIBRARY_API[lfgKind], {}, '목록을 불러오지 못했어요');
    const items = data.items || [];
    sel.innerHTML = items.length
      ? items.map(p => `<option value="${p.id}">${escapeHtml(p.name)} (${p.images.length}장)</option>`).join('')
      : `<option value="">${lfgKindLabel()} 게시물이 없어요</option>`;
  }catch(err){ sel.innerHTML = '<option value=""></option>'; setLfgError(err.message); }
}

function syncLfg(){
  document.querySelectorAll('#lfg-kind .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.kind === lfgKind));
  document.querySelectorAll('#lfg-target .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.target === lfgTarget));
  const isNew = lfgTarget === 'new';
  document.querySelectorAll('#lfg-form .lfg-new').forEach(el => { el.hidden = !isNew; });
  document.getElementById('lfg-existing-field').hidden = isNew;
  document.querySelector('label[for="lfg-name"]').textContent = `${lfgKindLabel()} 명칭`;
  if(!isNew) loadLfgExisting();
}

function openLibraryFromGallery(names){
  if(!names.length) return;
  lfgNames = names;
  lfgDone = null;
  document.getElementById('lfg-form').reset();
  document.getElementById('lfg-done').hidden = true;
  document.getElementById('lfg-save').hidden = false;
  document.getElementById('lfg-count').textContent = `이미지 ${names.length}장`;
  document.getElementById('lfg-preview').innerHTML = names.map(n =>
    `<img src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(n)}/thumbnail?size=80&fit=cover`)}" alt="" loading="lazy">`).join('');
  setLfgError(names.length > POSE_ADD_MAX ? `이미지는 한 번에 ${POSE_ADD_MAX}장까지 넣을 수 있어요.` : '');
  syncLfg();
  lfgModal.style.display = 'flex';
}

function closeLibraryFromGallery(){ lfgModal.style.display = 'none'; }

async function saveLibraryFromGallery(e){
  e.preventDefault();
  if(lfgNames.length > POSE_ADD_MAX) return setLfgError(`이미지는 한 번에 ${POSE_ADD_MAX}장까지 넣을 수 있어요.`);
  const fd = new FormData();
  lfgNames.forEach(n => fd.append('output_name', n));
  let url = LIBRARY_API[lfgKind];
  if(lfgTarget === 'new'){
    const name = document.getElementById('lfg-name').value.trim();
    if(!name) return setLfgError(`${lfgKindLabel()} 명칭을 적어주세요.`);
    fd.append('name', name);
    fd.append('description', document.getElementById('lfg-desc').value);
    fd.append('danbooru_prompt', document.getElementById('lfg-prompt').value);
  }else{
    const id = document.getElementById('lfg-existing').value;
    if(!id) return setLfgError('넣을 게시물을 골라주세요.');
    url += `/${id}/images`;
  }
  const btn = document.getElementById('lfg-save');
  btn.disabled = true; btn.textContent = '저장 중…';
  setLfgError('');
  try{
    const post = await libraryRequest(url, { method: 'POST', body: fd }, '저장하지 못했어요');
    lfgDone = { id: post.id, kind: lfgKind };
    document.getElementById('lfg-done-msg').textContent = `${lfgKindLabel()} '${post.name}'에 ${lfgNames.length}장 넣었어요.`;
    document.getElementById('lfg-done').hidden = false;
    btn.hidden = true;
  }catch(err){ setLfgError(err.message); }
  finally{ btn.disabled = false; btn.textContent = '저장'; }
}

document.querySelectorAll('#lfg-kind .enhance-mode-btn').forEach(b => b.addEventListener('click', () => { lfgKind = b.dataset.kind; syncLfg(); }));
document.querySelectorAll('#lfg-target .enhance-mode-btn').forEach(b => b.addEventListener('click', () => { lfgTarget = b.dataset.target; syncLfg(); }));
document.getElementById('lfg-form').addEventListener('submit', saveLibraryFromGallery);
document.getElementById('lfg-close').addEventListener('click', closeLibraryFromGallery);
document.getElementById('lfg-cancel').addEventListener('click', closeLibraryFromGallery);
document.getElementById('lfg-open-btn').addEventListener('click', () => {
  if(!lfgDone) return;
  closeLibraryFromGallery();
  closeLightbox();
  openLibraryPose(lfgDone.id, lfgDone.kind);
});
