// ---- Library 탭 — 지금은 Pose 서브탭 하나(NS-39) ----
// 목록은 서버(/api/library/poses)가 기준이다. 회원은 자기가 올린 것만, admin은 전부 받는다.
// 보기(Grid/Details)는 갤러리처럼 토글로 바꾸고 localStorage에 기억한다.
const LIBRARY_DISPLAY_KEY = 'nightshift.libraryDisplay';
let libraryPoses = [];
let libraryDisplayMode = (() => { try{ return localStorage.getItem(LIBRARY_DISPLAY_KEY) === 'details' ? 'details' : 'grid'; }catch(e){ return 'grid'; } })();
let poseAddImageMode = 'file';   // 'file' | 'url'

function setLibraryError(msg){ document.getElementById('library-error').textContent = msg || ''; }

async function openLibraryTab(){
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

// 썸네일을 누르면 원본을 새 창으로 연다 — 기존 라이트박스는 갤러리 파일 목록에 묶여 있다.
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
    <div class="library-card">
      <a class="library-thumb" href="${escapeHtml(p.image_url)}" target="_blank" rel="noopener" title="원본 보기">
        <img src="${escapeHtml(p.thumb_url)}" alt="${escapeHtml(p.name)}" loading="lazy">
      </a>
      <div class="library-info">
        <div class="library-name">${escapeHtml(p.name)}</div>
        <div class="library-desc">${escapeHtml(p.description || '')}</div>
        ${isDetails && p.danbooru_prompt ? `<div class="library-prompt">${escapeHtml(p.danbooru_prompt)}</div>` : ''}
      </div>
    </div>`).join('');
}

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

function updatePoseAddPreview(){
  if(poseAddPreview.src.startsWith('blob:')) URL.revokeObjectURL(poseAddPreview.src);
  let src = '';
  if(poseAddImageMode === 'file'){
    const f = document.getElementById('pose-add-file').files[0];
    if(f) src = URL.createObjectURL(f);
  }else{
    const u = document.getElementById('pose-add-url').value.trim();
    if(/^https?:\/\//i.test(u)) src = u;
  }
  poseAddPreview.hidden = !src;
  if(src) poseAddPreview.src = src; else poseAddPreview.removeAttribute('src');
}

function openPoseAddModal(){
  document.getElementById('pose-add-form').reset();
  setPoseAddError('');
  setPoseAddImageMode('file');
  poseAddModal.style.display = 'flex';
  document.getElementById('pose-add-name').focus();
}

function closePoseAddModal(){
  poseAddModal.style.display = 'none';
  if(poseAddPreview.src.startsWith('blob:')) URL.revokeObjectURL(poseAddPreview.src);
}

async function savePoseAdd(e){
  e.preventDefault();
  const name = document.getElementById('pose-add-name').value.trim();
  const fd = new FormData();
  fd.append('name', name);
  fd.append('description', document.getElementById('pose-add-desc').value);
  fd.append('danbooru_prompt', document.getElementById('pose-add-prompt').value);
  if(poseAddImageMode === 'file'){
    const f = document.getElementById('pose-add-file').files[0];
    if(f) fd.append('image', f);
  }else{
    fd.append('image_url', document.getElementById('pose-add-url').value.trim());
  }
  if(!name) return setPoseAddError('Pose 명칭을 적어주세요.');
  if(!fd.has('image') && !fd.get('image_url')) return setPoseAddError('이미지를 올리거나 이미지 주소를 적어주세요.');
  const btn = document.getElementById('pose-add-save');
  btn.disabled = true; btn.textContent = '저장 중…';
  setPoseAddError('');
  try{
    const res = await fetch('/api/library/poses', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `저장하지 못했어요 (${res.status})`);
    closePoseAddModal();
    await openLibraryTab();
  }catch(err){ setPoseAddError(err.message); }
  finally{ btn.disabled = false; btn.textContent = '저장'; }
}

document.getElementById('library-add-btn').addEventListener('click', openPoseAddModal);
document.getElementById('pose-add-modal-close').addEventListener('click', closePoseAddModal);
document.getElementById('pose-add-cancel').addEventListener('click', closePoseAddModal);
document.getElementById('pose-add-form').addEventListener('submit', savePoseAdd);
document.getElementById('pose-add-file').addEventListener('change', updatePoseAddPreview);
document.getElementById('pose-add-url').addEventListener('change', updatePoseAddPreview);
document.querySelectorAll('#pose-add-image-mode .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => setPoseAddImageMode(btn.dataset.mode));
});
