// ---- 영상 갤러리 ----
// 이미지 갤러리(위)와 구조는 같되(전체/작업별/날짜별 보기, 선택, 다운로드, 삭제,
// 라이트박스) 회전/세트추가/작업불러오기/자세히 보기는 뺐다 — 왜 뺐는지는 마크업
// 쪽 주석 참고. galleryJobLabels/fetchGalleryJobLabels/fetchAllJobsIncludingDeleted/
// fmtBytes/fmtGalleryDateTime/fmtDateKey/fmtDateLabel/triggerBlobDownload/
// triggerAnchorDownload/escapeHtml은 이미지 갤러리 것을 그대로 재사용한다(미디어
// 종류와 무관하게 동작하는 공용 함수들).
let galleryVideos = [];
let vLightboxIndex = -1;
let selectedGalleryVideoNames = new Set();
let videoGalleryViewMode = 'all'; // 'all' | 'byjob' | 'byproject' | 'bydate'
let displayedGalleryVideos = [];
const videoGalleryLightbox = document.getElementById('video-gallery-lightbox');

function updateVideoGalleryToolbar(){
  const toolbar = document.getElementById('video-gallery-toolbar');
  if(selectedGalleryVideoNames.size === 0){
    toolbar.style.display = 'none';
    return;
  }
  toolbar.style.display = 'flex';
  document.getElementById('video-gallery-toolbar-count').textContent = `${selectedGalleryVideoNames.size}개 선택됨`;
  document.getElementById('video-gallery-concat-btn').style.display = selectedGalleryVideoNames.size >= 2 ? '' : 'none';
  document.getElementById('video-gallery-trim-btn').style.display = selectedGalleryVideoNames.size === 1 ? '' : 'none';
  applyShareButtons();
}

// ---- 외부 편집기(OpenCut)로 열기: 서버가 선택 파일용 임시 세션을 만들고, 편집기 탭이 그 주소로 파일을 가져간다 ----
let shareConfigPromise = null;
function applyShareButtons(){
  if(!shareConfigPromise){
    shareConfigPromise = fetch('/api/share/config').then(r => r.ok ? r.json() : {}).catch(() => ({}));
  }
  shareConfigPromise.then(cfg => {
    document.querySelectorAll('.share-editor-btn').forEach(btn => { btn.style.display = cfg.opencut_url ? '' : 'none'; });
  });
}
async function openInEditor(names){
  if(!names.length) return;
  // 팝업 차단을 피하려면 클릭 직후 동기적으로 창을 열어 둬야 한다
  const win = window.open('about:blank', '_blank');
  try{
    const res = await fetch('/api/share/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'nightshift' },
      body: JSON.stringify({ names }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '편집기로 보내지 못했어요.');
    if(win) win.location.href = data.url; else window.location.href = data.url;
  }catch(e){
    if(win) win.close();
    flashNotice(e.message || '편집기로 보내지 못했어요.');
  }
}
document.getElementById('video-gallery-open-editor-btn').addEventListener('click', () => openInEditor(Array.from(selectedGalleryVideoNames)));
document.getElementById('gallery-open-editor-btn').addEventListener('click', () => openInEditor(Array.from(selectedGalleryNames)));

// ---- 영상 편집(자르기 / 이어 붙이기) — 서버가 ffmpeg로 새 영상을 만든다(원본은 그대로) ----
const veState = { op: null, names: [], jobId: null, timer: null, duration: 0 };
const veEl = id => document.getElementById(id);

function veMediaUrl(name){ return window.__nightshiftMediaUrl(`/api/output-videos/${encodeURI(name)}`); }

function veSetStatus(text, isError){
  const el = veEl('ve-status');
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
}

function veRenderConcatList(){
  veEl('ve-list').innerHTML = veState.names.map((name, i) => `
    <div class="ve-row" data-i="${i}">
      <span class="ve-idx">${i + 1}</span>
      <video class="ve-thumb" muted preload="metadata" playsinline src="${veMediaUrl(name)}#t=0.1"></video>
      <span class="ve-name" title="${escapeHtml(name)}">${escapeHtml(name.split('/').pop())}</span>
      <button class="load-btn ve-up" type="button" ${i === 0 ? 'disabled' : ''} title="위로">▲</button>
      <button class="load-btn ve-down" type="button" ${i === veState.names.length - 1 ? 'disabled' : ''} title="아래로">▼</button>
      <button class="del-btn ve-remove" type="button" ${veState.names.length <= 2 ? 'disabled' : ''} title="목록에서 빼기"><svg class="ico"><use href="#i-x"/></svg></button>
    </div>`).join('');
}

function veTrimInfo(){
  const start = parseFloat(veEl('ve-start').value) || 0;
  const end = parseFloat(veEl('ve-end').value);
  const len = Number.isFinite(end) ? end - start : NaN;
  veEl('ve-trim-info').textContent = Number.isFinite(len)
    ? `결과 길이 약 ${Math.max(0, len).toFixed(1)}초 (원본 ${veState.duration ? veState.duration.toFixed(1) + '초' : '?'})` : '';
}

function openVideoEditModal(op){
  const names = Array.from(selectedGalleryVideoNames);
  if(op === 'concat' ? names.length < 2 : names.length !== 1) return;
  Object.assign(veState, { op, names, jobId: null, duration: 0 });
  clearInterval(veState.timer);
  veEl('ve-title').textContent = op === 'concat' ? 'Join videos' : 'Trim video';
  veEl('ve-hint').textContent = op === 'concat'
    ? '고른 순서대로 이어 붙여요. 같은 설정의 클립은 화질 손실 없이 바로 붙이고, 해상도·프레임률이 다르면 첫 영상에 맞춰 다시 인코딩해요. 원본은 그대로 남아요.'
    : '재생하면서 시작/끝 위치를 정하세요. 새 영상으로 저장돼요(원본은 그대로).';
  veEl('ve-concat').style.display = op === 'concat' ? '' : 'none';
  veEl('ve-trim').style.display = op === 'trim' ? '' : 'none';
  veEl('ve-name').value = '';
  veEl('ve-progress').style.display = 'none';
  veEl('ve-progress-fill').style.width = '0%';
  veSetStatus('');
  const startBtn = veEl('ve-start-btn');
  startBtn.disabled = false;
  startBtn.innerHTML = `${ico('play')} ${op === 'concat' ? '이어 붙이기' : '자르기'}`;
  veEl('ve-cancel').innerHTML = `${ico('x')} 닫기`;
  if(op === 'concat'){
    veRenderConcatList();
  }else{
    const video = veEl('ve-video');
    video.src = veMediaUrl(names[0]);
    veEl('ve-start').value = '0';
    veEl('ve-end').value = '';
    veEl('ve-trim-info').textContent = '';
    video.onloadedmetadata = () => {
      veState.duration = video.duration || 0;
      veEl('ve-end').value = veState.duration ? veState.duration.toFixed(1) : '';
      veEl('ve-end').max = veState.duration || '';
      veTrimInfo();
    };
  }
  veEl('video-edit-modal').style.display = 'flex';
}

function closeVideoEditModal(){
  clearInterval(veState.timer);
  const video = veEl('ve-video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  veEl('video-edit-modal').style.display = 'none';
}

async function veStart(){
  veSetStatus('');
  const body = { op: veState.op, inputs: veState.names, name: veEl('ve-name').value.trim() };
  if(veState.op === 'trim'){
    body.start = parseFloat(veEl('ve-start').value) || 0;
    const end = parseFloat(veEl('ve-end').value);
    if(Number.isFinite(end)) body.end = end;
    if(body.end !== undefined && body.end <= body.start){ veSetStatus('끝 시각이 시작 시각보다 뒤여야 해요.', true); return; }
  }
  const startBtn = veEl('ve-start-btn');
  startBtn.disabled = true;
  try{
    const res = await fetch('/api/video-edits', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '시작하지 못했어요.');
    veState.jobId = data.id;
  }catch(e){
    veSetStatus(e.message, true);
    startBtn.disabled = false;
    return;
  }
  veEl('ve-progress').style.display = '';
  veEl('ve-cancel').innerHTML = `${ico('x')} 취소`;
  veSetStatus('만드는 중…');
  veState.timer = setInterval(vePoll, 1000);
}

async function vePoll(){
  if(!veState.jobId) return;
  let job;
  try{
    const res = await fetch(`/api/video-edits/${veState.jobId}`);
    if(!res.ok) throw new Error();
    job = await res.json();
  }catch(e){ return; }
  veEl('ve-progress-fill').style.width = `${Math.round((job.progress || 0) * 100)}%`;
  if(job.status === 'running' || job.status === 'queued'){
    veSetStatus(`만드는 중… ${Math.round((job.progress || 0) * 100)}%`);
    return;
  }
  clearInterval(veState.timer);
  veState.jobId = null;
  veEl('ve-cancel').innerHTML = `${ico('x')} 닫기`;
  if(job.status === 'done'){
    veEl('ve-progress-fill').style.width = '100%';
    veSetStatus(`완료 — 새 영상 "${job.output}"이(가) 영상 갤러리에 추가됐어요${job.method === 'copy' ? ' (재인코딩 없이 이어 붙임)' : ''}.`);
    flashNotice('새 영상을 만들었어요.');
    veEl('ve-start-btn').disabled = true;
    fetchGalleryVideos();
  }else if(job.status === 'cancelled'){
    veSetStatus('취소했어요.');
    veEl('ve-start-btn').disabled = false;
  }else{
    veSetStatus(job.error || '편집에 실패했어요.', true);
    veEl('ve-start-btn').disabled = false;
  }
}

veEl('video-gallery-concat-btn').addEventListener('click', () => openVideoEditModal('concat'));
veEl('video-gallery-trim-btn').addEventListener('click', () => openVideoEditModal('trim'));
veEl('ve-close').addEventListener('click', closeVideoEditModal);
veEl('ve-cancel').addEventListener('click', async () => {
  if(veState.jobId){
    try{ await fetch(`/api/video-edits/${veState.jobId}/cancel`, { method: 'POST' }); }catch(e){ /* 폴링이 결과를 알려준다 */ }
    return;
  }
  closeVideoEditModal();
});
veEl('ve-start-btn').addEventListener('click', veStart);
veEl('ve-set-start').addEventListener('click', () => { veEl('ve-start').value = veEl('ve-video').currentTime.toFixed(1); veTrimInfo(); });
veEl('ve-set-end').addEventListener('click', () => { veEl('ve-end').value = veEl('ve-video').currentTime.toFixed(1); veTrimInfo(); });
veEl('ve-start').addEventListener('input', veTrimInfo);
veEl('ve-end').addEventListener('input', veTrimInfo);
veEl('ve-list').addEventListener('click', (e) => {
  const row = e.target.closest('.ve-row');
  if(!row) return;
  const i = Number(row.dataset.i);
  const names = veState.names;
  if(e.target.closest('.ve-up') && i > 0) [names[i - 1], names[i]] = [names[i], names[i - 1]];
  else if(e.target.closest('.ve-down') && i < names.length - 1) [names[i + 1], names[i]] = [names[i], names[i + 1]];
  else if(e.target.closest('.ve-remove') && names.length > 2) names.splice(i, 1);
  else return;
  veRenderConcatList();
});

async function deleteGalleryVideos(names){
  if(names.length === 0) return false;
  const label = names.length === 1 ? `'${names[0]}' 영상을` : `선택한 영상 ${names.length}개를`;
  if(!confirm(`${label} 삭제할까요? 되돌릴 수 없어요.`)) return false;

  const errorEl = document.getElementById('video-gallery-error');
  errorEl.textContent = '';
  try{
    const res = await fetch('/api/output-videos/delete-selected', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.detail || '삭제에 실패했어요.';
      return false;
    }
  }catch(e){
    errorEl.textContent = '삭제에 실패했어요.';
    return false;
  }

  for(const name of names) selectedGalleryVideoNames.delete(name);
  updateVideoGalleryToolbar();
  await fetchGalleryVideos();
  return true;
}

async function downloadGalleryVideos(names){
  if(names.length === 0) return;
  const errorEl = document.getElementById('video-gallery-error');
  errorEl.textContent = '';
  try{
    if(names.length === 1){
      triggerAnchorDownload(`/api/output-videos/${encodeURIComponent(names[0])}`, names[0]);
      return;
    }
    const res = await fetch('/api/output-videos/download-selected', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.detail || '다운로드에 실패했어요.';
      return;
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    triggerBlobDownload(blob, match ? match[1] : 'nightshift_videos_selected.zip');
  }catch(e){
    errorEl.textContent = '다운로드에 실패했어요.';
  }
}

// 영상 카드 썸네일 — ffmpeg 없이 브라우저의 <video> 태그만으로 미리보기를 낸다.
// src에 #t=0.1을 붙이면(미디어 프래그먼트) 재생하지 않아도 0.1초 지점 프레임을
// 포스터처럼 보여준다(대부분 브라우저에서 첫 프레임은 검은 화면일 수 있어서).
// controls 없이 두고 클릭은 카드 전체가 라이트박스를 여는 데 쓴다(재생 버튼
// 아이콘만 장식으로 얹음).
let videoGalleryDisplayMode = 'grid'; // 'grid' | 'details'

// 길이·해상도는 서버가 ffprobe로 읽지 않고, 썸네일용 <video>가 메타데이터를 받는 순간 브라우저가 알려 준다.
// 4초마다 다시 그려도 깜빡이지 않게 이미 읽은 값은 기억해 둔다.
const videoMetaCache = new Map();   // "이름|수정시각" -> {duration, width, height}
function videoMetaKey(vid){ return `${vid.name}|${vid.mtime}`; }
function fmtDuration(sec){
  if(!Number.isFinite(sec) || sec < 0) return '-';
  const total = Math.round(sec);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), r = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`;
}
function paintVideoMetaCells(row, meta){
  const dur = row.querySelector('.vgallery-details-col.dur');
  const dim = row.querySelector('.vgallery-details-col.dim');
  if(dur) dur.textContent = fmtDuration(meta.duration);
  if(dim) dim.textContent = meta.width && meta.height ? `${meta.width}×${meta.height}` : '-';
}

function videoGalleryDetailsHeaderHtml(){
  return `
    <div class="vgallery-details-columns vgallery-details-header">
      <span></span><span></span>
      <span>파일명</span><span>작업</span><span>길이</span><span>해상도</span><span>용량</span><span>수정 시각</span>
      <span></span>
    </div>
  `;
}

function videoGalleryItemHtml(vid, index){
  const selected = selectedGalleryVideoNames.has(vid.name);
  const blur = !!vid.nsfw && nsfwMode === 'blur';
  const src = blur ? '' : window.__nightshiftMediaUrl(`/api/output-videos/${encodeURIComponent(vid.name)}`) + '#t=0.1';
  const blurOverlay = `<div class="vgallery-item-blur-overlay" title="NSFW 블러 — 열어서 보기">${ico('eye-off')}</div>`;
  if(videoGalleryDisplayMode === 'details'){
    const meta = videoMetaCache.get(videoMetaKey(vid));
    const jobLabel = galleryDetailsJobLabel(vid);
    return `
      <div class="vgallery-item details vgallery-details-columns${selected ? ' selected' : ''}" data-index="${index}">
        <label class="vgallery-item-select" title="선택">
          <input type="checkbox" ${selected ? 'checked' : ''}>
        </label>
        <div class="vgallery-item-thumb">${blur ? blurOverlay : ''}<video ${src ? `src="${src}"` : ''} preload="metadata" muted playsinline></video></div>
        <div class="vgallery-details-col name" title="${escapeHtml(vid.name)}">${vid.favorite ? '<svg class="ico fav"><use href="#i-star"/></svg> ' : ''}${vid.nsfw ? '<svg class="ico nsfw"><use href="#i-flame"/></svg> ' : ''}${escapeHtml(vid.name)}</div>
        <div class="vgallery-details-col job" title="${escapeHtml(jobLabel)}">${escapeHtml(jobLabel)}</div>
        <div class="vgallery-details-col dur">${meta ? fmtDuration(meta.duration) : '…'}</div>
        <div class="vgallery-details-col dim">${meta ? (meta.width && meta.height ? `${meta.width}×${meta.height}` : '-') : '…'}</div>
        <div class="vgallery-details-col size">${fmtBytes(vid.size)}</div>
        <div class="vgallery-details-col mtime">${fmtGalleryDateTime(vid.mtime)}</div>
      </div>
    `;
  }
  return `
    <div class="vgallery-item${selected ? ' selected' : ''}" data-index="${index}">
      <label class="vgallery-item-select" title="선택">
        <input type="checkbox" ${selected ? 'checked' : ''}>
      </label>
      <button class="vgallery-item-fav${vid.favorite ? ' on' : ''}" type="button" aria-label="즐겨찾기" aria-pressed="${vid.favorite ? 'true' : 'false'}" title="즐겨찾기">${ico('star')}</button>
      ${vid.nsfw ? `<span class="vgallery-item-nsfw" title="NSFW로 표시됨">${ico('flame')}</span>` : ''}
      ${blur ? blurOverlay : ''}
      <video ${src ? `src="${src}"` : ''} preload="metadata" muted playsinline></video>
      <div class="vgallery-item-play"><svg class="ico"><use href="#i-play"/></svg></div>
      <div class="vgallery-item-name">${escapeHtml(vid.name)}</div>
    </div>
  `;
}

function wireVideoGalleryItems(){
  document.querySelectorAll('#video-gallery-grid .vgallery-item:not([data-wired])').forEach(el => {
    el.dataset.wired = '1';   // "더 보기"로 이어 붙일 때마다 부르므로 이미 묶은 칸은 건너뛴다
    const index = Number(el.dataset.index);
    const name = displayedGalleryVideos[index].name;

    // 격자는 썸네일만 눌러야 열리고, 자세히 보기는 줄 어디를 눌러도 열린다(이미지 갤러리와 같다).
    const openTarget = el.classList.contains('details') ? el : el.querySelector('video');
    openTarget.addEventListener('click', () => openVideoLightbox(index));

    if(el.classList.contains('details')){
      const vid = displayedGalleryVideos[index];
      const videoEl = el.querySelector('video');
      const readMeta = () => {
        const meta = { duration: videoEl.duration, width: videoEl.videoWidth, height: videoEl.videoHeight };
        videoMetaCache.set(videoMetaKey(vid), meta);
        paintVideoMetaCells(el, meta);
      };
      if(videoEl.readyState >= 1) readMeta();
      else if(!videoMetaCache.has(videoMetaKey(vid))) videoEl.addEventListener('loadedmetadata', readMeta, { once: true });
    }

    const checkbox = el.querySelector('.vgallery-item-select input');
    checkbox.addEventListener('click', (e) => e.stopPropagation());
    checkbox.addEventListener('change', () => {
      if(checkbox.checked) selectedGalleryVideoNames.add(name);
      else selectedGalleryVideoNames.delete(name);
      el.classList.toggle('selected', checkbox.checked);
      updateVideoGalleryToolbar();
      const groupEl = el.closest('.vgallery-group');
      if(groupEl) updateVideoGroupCheckboxState(groupEl);
    });

    const favBtn = el.querySelector('.vgallery-item-fav');
    if(favBtn) favBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setAssetsFavorite('video', [name], !favBtn.classList.contains('on'));
    });
  });
}

setupItemLongPressDelete(document.getElementById('video-gallery-grid'), '.vgallery-item', (el) => {
  const vid = displayedGalleryVideos[Number(el.dataset.index)];
  if(vid) deleteGalleryVideos([vid.name]);
});

// 묶음 소제목의 체크박스 상태 — 화면에 그린 칸이 아니라 그 묶음 전체(아직 안 그린 것 포함) 기준(이미지 갤러리와 같다).
function updateVideoGroupCheckboxState(groupEl){
  const groupCheckbox = groupEl.querySelector('.vgallery-group-checkbox');
  if(!groupCheckbox) return;
  const names = videoGalleryGroupNames.get(groupEl.dataset.groupKey) || [];
  const selectedCount = names.filter(n => selectedGalleryVideoNames.has(n)).length;
  groupCheckbox.checked = names.length > 0 && selectedCount === names.length;
  groupCheckbox.indeterminate = selectedCount > 0 && selectedCount < names.length;
}

function refreshAllVideoGroupCheckboxStates(){
  document.querySelectorAll('#video-gallery-grid .vgallery-group').forEach(updateVideoGroupCheckboxState);
}

function wireVideoGalleryGroupCheckbox(groupEl){
  const groupCheckbox = groupEl.querySelector('.vgallery-group-checkbox');
  groupCheckbox.addEventListener('change', () => {
    const checked = groupCheckbox.checked;
    groupCheckbox.indeterminate = false;
    for(const name of videoGalleryGroupNames.get(groupEl.dataset.groupKey) || []){
      if(checked) selectedGalleryVideoNames.add(name);
      else selectedGalleryVideoNames.delete(name);
    }
    groupEl.querySelectorAll('.vgallery-item').forEach(itemEl => {
      itemEl.classList.toggle('selected', checked);
      const itemCheckbox = itemEl.querySelector('.vgallery-item-select input');
      if(itemCheckbox) itemCheckbox.checked = checked;
    });
    updateVideoGalleryToolbar();
  });
}

function videoGalleryGroupKeyFor(vid){
  if(videoGalleryViewMode === 'byjob') return vid.job_id || '';
  if(videoGalleryViewMode === 'byproject') return projectGroupKey(vid);
  if(videoGalleryViewMode === 'bydate') return fmtDateKey(vid.mtime);
  return '';
}

function videoGalleryGroupLabelFor(key){
  if(videoGalleryViewMode === 'byjob'){
    return key ? (galleryJobLabels[key] || `알 수 없는 작업 (${key})`) : '작업 정보 없음';
  }
  if(videoGalleryViewMode === 'byproject') return projectGroupLabel(key);
  if(videoGalleryViewMode === 'bydate') return fmtDateLabel(key);
  return '';
}

// 이미지 갤러리의 renderGalleryGrid/appendGalleryItems와 같은 방식 — 전체 순서는 displayedGalleryVideos에,
// 화면에는 앞에서부터 videoGalleryLimit개만 그리고 나머지는 "더 보기"가 이어 붙인다.
let videoGalleryLimit = LIST_PAGE;
let videoGalleryRendered = 0;
let videoGalleryGroupNames = new Map();   // 묶음 키 -> 그 묶음의 영상 이름 전부(안 그린 것 포함)
let videoGalleryScopeKey = '';
function renderVideoGalleryGrid(){
  const grid = document.getElementById('video-gallery-grid');
  const isDetails = videoGalleryDisplayMode === 'details';
  grid.classList.toggle('details-mode', isDetails);
  const scope = JSON.stringify([videoGalleryViewMode, videoGalleryDisplayMode, videoGalleryPodFilter, videoGalleryProjectFilter, galleryFilterQs(videoGalleryFilters)]);
  if(scope !== videoGalleryScopeKey){ videoGalleryScopeKey = scope; videoGalleryLimit = LIST_PAGE; }
  if(videoGalleryViewMode !== 'all'){
    const groups = new Map();
    for(const vid of galleryVideos){
      const key = videoGalleryGroupKeyFor(vid);
      if(!groups.has(key)) groups.set(key, []);
      groups.get(key).push(vid);
    }
    displayedGalleryVideos = [].concat(...groups.values());
    videoGalleryGroupNames = new Map([...groups].map(([key, list]) => [key, list.map(vid => vid.name)]));
  }else{
    displayedGalleryVideos = galleryVideos;
    videoGalleryGroupNames = new Map();
  }
  grid.innerHTML = isDetails ? videoGalleryDetailsHeaderHtml() : '';
  videoGalleryRendered = 0;
  appendVideoGalleryItems();
}

function appendVideoGalleryItems(){
  const grid = document.getElementById('video-gallery-grid');
  const isDetails = videoGalleryDisplayMode === 'details';
  const end = Math.min(videoGalleryLimit, displayedGalleryVideos.length);
  for(let i = videoGalleryRendered; i < end; i++){
    const vid = displayedGalleryVideos[i];
    let target = grid;
    if(videoGalleryViewMode !== 'all'){
      const key = videoGalleryGroupKeyFor(vid);
      let groupEl = grid.lastElementChild;
      if(!groupEl || !groupEl.classList.contains('vgallery-group') || groupEl.dataset.groupKey !== key){
        grid.insertAdjacentHTML('beforeend', `
          <div class="vgallery-group" data-group-key="${escapeHtml(key)}">
            <div class="vgallery-group-header">
              <label class="vgallery-group-select-label" title="이 그룹 전체 선택">
                <input type="checkbox" class="vgallery-group-checkbox">
                ${escapeHtml(videoGalleryGroupLabelFor(key))} · ${(videoGalleryGroupNames.get(key) || []).length}개
              </label>
            </div>
            <div class="vgallery-group-grid${isDetails ? ' details-mode' : ''}"></div>
          </div>
        `);
        groupEl = grid.lastElementChild;
        wireVideoGalleryGroupCheckbox(groupEl);
      }
      target = groupEl.querySelector('.vgallery-group-grid');
    }
    target.insertAdjacentHTML('beforeend', videoGalleryItemHtml(vid, i));
  }
  videoGalleryRendered = end;
  wireVideoGalleryItems();
  refreshAllVideoGroupCheckboxStates();
  updateLoadMore('video-gallery-more-btn', displayedGalleryVideos.length - videoGalleryRendered, '개');
}
setupLoadMore('video-gallery-more-btn', () => { videoGalleryLimit += LIST_PAGE; appendVideoGalleryItems(); });

document.querySelectorAll('#video-gallery-display-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.display === videoGalleryDisplayMode) return;
    videoGalleryDisplayMode = btn.dataset.display;
    document.querySelectorAll('#video-gallery-display-toggle .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.display === videoGalleryDisplayMode));
    if(galleryVideos.length > 0) renderVideoGalleryGrid();
  });
});
document.querySelectorAll('#video-gallery-view-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.view === videoGalleryViewMode) return;
    videoGalleryViewMode = btn.dataset.view;
    document.querySelectorAll('#video-gallery-view-toggle .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.view === videoGalleryViewMode));
    if(galleryVideos.length > 0) renderVideoGalleryGrid();
  });
});

async function fetchGalleryVideos(){
  const grid = document.getElementById('video-gallery-grid');
  const emptyMsg = document.getElementById('video-gallery-empty');
  const countEl = document.getElementById('video-gallery-count');
  try{
    const [vidRes, labels] = await Promise.all([
      fetch('/api/output-videos' + galleryFilterQs(videoGalleryFilters)),
      fetchGalleryJobLabels(),
    ]);
    const data = await vidRes.json();
    galleryVideos = data.videos || [];
    // 파드 갤러리면 그 파드가 배정된 작업의 영상만 남긴다(fetchGalleryImages와 동일한 규칙).
    if(videoGalleryPodFilter){
      galleryVideos = galleryVideos.filter(vid => podIdForVideo(vid) === videoGalleryPodFilter);
    }
    if(videoGalleryProjectFilter !== null){
      galleryVideos = galleryVideos.filter(vid => projectMatches(vid.project_id, videoGalleryProjectFilter));
    }
    galleryJobLabels = labels; // 이미지 갤러리와 공유하는 전역(작업 라벨은 미디어 종류와 무관)
  }catch(e){
    galleryVideos = [];
  }

  const stillPresent = new Set(galleryVideos.map(vid => vid.name));
  for(const name of Array.from(selectedGalleryVideoNames)){
    if(!stillPresent.has(name)) selectedGalleryVideoNames.delete(name);
  }
  updateVideoGalleryToolbar();

  countEl.textContent = galleryVideos.length ? `총 ${galleryVideos.length}개` : '';
  document.getElementById('video-gallery-select-all-btn').disabled = galleryVideos.length === 0;

  if(galleryVideos.length === 0){
    lastVideoGallerySig = '';
    grid.innerHTML = '';
    displayedGalleryVideos = [];
    updateLoadMore('video-gallery-more-btn', 0, '개');
    emptyMsg.textContent = galleryFiltersActive(videoGalleryFilters) ? '조건에 맞는 영상이 없어요'
      : videoGalleryPodFilter ? '이 워커가 만든 영상이 아직 없어요'
      : (videoGalleryProjectFilter !== null ? '이 프로젝트의 영상이 아직 없어요' : '서버에 저장된 영상이 없어요');
    emptyMsg.style.display = 'block';
    return;
  }
  emptyMsg.style.display = 'none';

  const sig = gallerySignature(galleryVideos, [videoGalleryViewMode, videoGalleryDisplayMode, videoGalleryPodFilter, videoGalleryProjectFilter]);
  if(sig === lastVideoGallerySig && grid.childElementCount > 0) return;   // 바뀐 게 없다
  lastVideoGallerySig = sig;
  renderVideoGalleryGrid();
}

document.getElementById('video-gallery-refresh-btn').addEventListener('click', fetchGalleryVideos);

// 이미지 갤러리의 "⬇ 결과 가져오기"와 같은 엔드포인트를 쓴다(comfy_outputs.py가
// 이미지/영상을 같은 출력 폴더로 함께 받아온다) — nightshift 큐를 거치지 않고
// ComfyUI에서 직접 돌린 작업도 ComfyUI 히스토리에 남아있으면 이걸로 받아온다.
document.getElementById('video-gallery-pull-btn').addEventListener('click', async () => {
  const btn = document.getElementById('video-gallery-pull-btn');
  const errorEl = document.getElementById('video-gallery-error');
  errorEl.textContent = '';
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = ico('loader-circle', true) + ' 가져오는 중…';
  try{
    const res = await fetch('/api/comfy-outputs/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(videoGalleryPodFilter ? { pod_id: videoGalleryPodFilter } : {}),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '가져오지 못했어요.');
    const got = (data.downloaded || []).length;
    const failed = (data.errors || []).length;
    errorEl.textContent = failed
      ? `${got}개 가져왔어요 (실패 ${failed}건: ${data.errors.slice(0, 2).join('; ')})`
      : (got ? '' : '새로 가져올 영상이 없어요.');
    if(got) await fetchGalleryVideos();
  }catch(e){
    errorEl.textContent = e.message || '가져오지 못했어요.';
  }finally{
    btn.disabled = false;
    btn.innerHTML = original;
  }
});
document.getElementById('video-gallery-select-all-btn').addEventListener('click', () => {
  for(const vid of galleryVideos) selectedGalleryVideoNames.add(vid.name);
  document.querySelectorAll('#video-gallery-grid .vgallery-item').forEach(el => el.classList.add('selected'));
  document.querySelectorAll('#video-gallery-grid .vgallery-item-select input').forEach(cb => { cb.checked = true; });
  refreshAllVideoGroupCheckboxStates();
  updateVideoGalleryToolbar();
});
document.getElementById('video-gallery-download-selected-btn').addEventListener('click', () => {
  downloadGalleryVideos(Array.from(selectedGalleryVideoNames));
});
document.getElementById('video-gallery-delete-selected-btn').addEventListener('click', () => {
  deleteGalleryVideos(Array.from(selectedGalleryVideoNames));
});
document.getElementById('video-gallery-clear-selection-btn').addEventListener('click', () => {
  selectedGalleryVideoNames.clear();
  document.querySelectorAll('#video-gallery-grid .vgallery-item.selected').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll('#video-gallery-grid .vgallery-item-select input').forEach(cb => { cb.checked = false; });
  refreshAllVideoGroupCheckboxStates();
  updateVideoGalleryToolbar();
});

// 이미지 라이트박스와 같은 로딩 표시 — 다음 영상을 받아오는 동안 흐리게 + 스피너(150ms 넘게 걸릴 때만).
let videoLightboxLoadingTimer = null;
function showVideoLightboxLoading(){
  clearTimeout(videoLightboxLoadingTimer);
  videoLightboxLoadingTimer = setTimeout(() => {
    document.getElementById('video-gallery-lightbox-video').classList.add('lightbox-loading');
    document.getElementById('video-gallery-lightbox-spinner').style.display = '';
  }, 150);
}
function hideVideoLightboxLoading(){
  clearTimeout(videoLightboxLoadingTimer);
  document.getElementById('video-gallery-lightbox-video').classList.remove('lightbox-loading');
  document.getElementById('video-gallery-lightbox-spinner').style.display = 'none';
}
(() => {
  const videoEl = document.getElementById('video-gallery-lightbox-video');
  for(const ev of ['loadeddata', 'error', 'emptied']) videoEl.addEventListener(ev, hideVideoLightboxLoading);
})();

function renderVideoLightbox(){
  const vid = displayedGalleryVideos[vLightboxIndex];
  if(!vid) return;
  document.getElementById('video-gallery-lightbox-error').textContent = '';
  const videoEl = document.getElementById('video-gallery-lightbox-video');
  showVideoLightboxLoading();
  videoEl.loop = videoRepeatMode === 'one';
  videoEl.src = window.__nightshiftMediaUrl(`/api/output-videos/${encodeURIComponent(vid.name)}`);
  if(videoAutoplayOnRender){
    videoAutoplayOnRender = false;
    videoEl.play().catch(() => { /* 자동 재생이 막히면 재생 버튼을 눌러 시작한다 */ });
  }
  document.getElementById('video-gallery-lightbox-info').textContent =
    `${vid.name} · ${fmtBytes(vid.size)} · ${fmtGalleryDateTime(vid.mtime)} (${vLightboxIndex + 1}/${displayedGalleryVideos.length})${vid.owner_name ? ' · ' + vid.owner_name : ''}`;
  videoMetaPanel.show(vid);
  document.getElementById('video-gallery-prev').disabled = vLightboxIndex <= 0;
  document.getElementById('video-gallery-next').disabled = vLightboxIndex >= displayedGalleryVideos.length - 1;
}

function openVideoLightbox(index){
  vLightboxIndex = index;
  renderVideoLightbox();
  videoGalleryLightbox.style.display = 'flex';
}

function closeVideoLightbox(){
  if(isLightboxFullscreen(videoGalleryLightbox)) setLightboxFullscreen(videoGalleryLightbox, false);
  videoGalleryLightbox.style.display = 'none';
  vLightboxIndex = -1;
  videoAutoplayOnRender = false;
  hideVideoLightboxLoading();
  // 라이트박스를 닫아도 <video>가 계속 재생/버퍼링되지 않게 멈춘다.
  const videoEl = document.getElementById('video-gallery-lightbox-video');
  videoEl.pause();
  videoEl.removeAttribute('src');
  videoEl.load();
}

// ---- 영상 자동 재생 / 반복 방식 ----
// 라이트박스에서 영상이 끝났을 때 무엇을 할지 — 다음 영상 자동 재생(기본) / 한 영상 반복 / 전체 반복 / 없음.
// 선택은 브라우저에 기억한다. 손으로 다음·이전으로 넘길 때도, 재생 중이었다면 새 영상을 이어서 재생한다.
const VIDEO_REPEAT_MODES = ['next', 'one', 'all', 'off'];
const VIDEO_REPEAT_META = {
  next: { icon: 'skip-forward', label: '다음 영상 자동', hint: '영상이 끝나면 다음 영상을 자동으로 재생해요' },
  one:  { icon: 'repeat-1',     label: '한 영상 반복',   hint: '지금 영상을 계속 반복해요' },
  all:  { icon: 'repeat',       label: '전체 반복',     hint: '다음 영상을 이어 재생하고, 마지막 뒤에는 처음부터 다시 시작해요' },
  off:  { icon: 'ban',          label: '자동 재생 없음', hint: '한 번 재생하고 멈춰요' },
};
const VIDEO_REPEAT_KEY = 'nightshift-video-repeat';
let videoRepeatMode = (() => {
  try{ const v = localStorage.getItem(VIDEO_REPEAT_KEY); return VIDEO_REPEAT_MODES.includes(v) ? v : 'next'; }
  catch(e){ return 'next'; }
})();
let videoAutoplayOnRender = false;

function paintVideoRepeatButton(){
  const meta = VIDEO_REPEAT_META[videoRepeatMode];
  const btn = document.getElementById('video-gallery-lightbox-repeat-btn');
  btn.innerHTML = ico(meta.icon) + `<span class="btn-label">${meta.label}</span>`;
  btn.title = `${meta.label} — ${meta.hint} (눌러서 바꾸기)`;
  btn.setAttribute('aria-label', `재생 방식: ${meta.label}`);
  document.getElementById('video-gallery-lightbox-video').loop = videoRepeatMode === 'one';
}
document.getElementById('video-gallery-lightbox-repeat-btn').addEventListener('click', () => {
  videoRepeatMode = VIDEO_REPEAT_MODES[(VIDEO_REPEAT_MODES.indexOf(videoRepeatMode) + 1) % VIDEO_REPEAT_MODES.length];
  try{ localStorage.setItem(VIDEO_REPEAT_KEY, videoRepeatMode); }catch(e){ /* 무시 */ }
  paintVideoRepeatButton();
  flashNotice(`재생 방식: ${VIDEO_REPEAT_META[videoRepeatMode].label}`);
});
paintVideoRepeatButton();

function videoIsPlaying(){
  const v = document.getElementById('video-gallery-lightbox-video');
  return !v.paused && !v.ended;
}
document.getElementById('video-gallery-lightbox-video').addEventListener('ended', () => {
  if(videoGalleryLightbox.style.display === 'none') return;
  const videoEl = document.getElementById('video-gallery-lightbox-video');
  const last = displayedGalleryVideos.length - 1;
  if(videoRepeatMode === 'next' && vLightboxIndex < last){
    videoAutoplayOnRender = true; vLightboxIndex++; renderVideoLightbox();
  }else if(videoRepeatMode === 'all'){
    if(last === 0){ videoEl.currentTime = 0; videoEl.play().catch(() => {}); return; }
    videoAutoplayOnRender = true;
    vLightboxIndex = vLightboxIndex < last ? vLightboxIndex + 1 : 0;
    renderVideoLightbox();
  }
  // 한 영상 반복은 <video loop>이 처리한다(끝났다는 이벤트가 오지 않는다).
});

function showPrevVideo(){
  if(vLightboxIndex > 0){ videoAutoplayOnRender = videoIsPlaying(); vLightboxIndex--; renderVideoLightbox(); }
}
function showNextVideo(){
  if(vLightboxIndex < displayedGalleryVideos.length - 1){ videoAutoplayOnRender = videoIsPlaying(); vLightboxIndex++; renderVideoLightbox(); }
}

// 이미지 라이트박스와 같은 스와이프. 다만 <video controls>의 재생 막대(탐색·볼륨)를 끌 때 영상이
// 넘어가면 안 되니, 영상 아래쪽 띠에서 시작한 터치와 <video> 자체의 네이티브 전체 화면 중의 터치는 무시한다.
const VIDEO_CONTROLS_STRIP = 64;
setupLightboxSwipe(videoGalleryLightbox, document.getElementById('video-gallery-lightbox-video'), {
  onNext: showNextVideo, onPrev: showPrevVideo,
  ignore: (e) => {
    const videoEl = document.getElementById('video-gallery-lightbox-video');
    if(document.fullscreenElement === videoEl || document.webkitFullscreenElement === videoEl) return true;   // 네이티브 영상 전체 화면
    if(e.target !== videoEl) return false;
    return e.touches[0].clientY > videoEl.getBoundingClientRect().bottom - VIDEO_CONTROLS_STRIP;
  },
});

document.getElementById('video-gallery-lightbox-close').addEventListener('click', closeVideoLightbox);
document.getElementById('video-gallery-lightbox-fs-btn').addEventListener('click', () => toggleLightboxFullscreen(videoGalleryLightbox));
document.getElementById('video-gallery-prev').addEventListener('click', showPrevVideo);
document.getElementById('video-gallery-next').addEventListener('click', showNextVideo);
document.getElementById('video-gallery-lightbox-download-btn').addEventListener('click', () => {
  const vid = displayedGalleryVideos[vLightboxIndex];
  if(vid) triggerAnchorDownload(`/api/output-videos/${encodeURIComponent(vid.name)}`, vid.name);
});
async function deleteCurrentVideoLightboxItem(){
  const vid = displayedGalleryVideos[vLightboxIndex];
  if(!vid) return;
  const indexBeforeDelete = vLightboxIndex;
  const ok = await deleteGalleryVideos([vid.name]);
  if(!ok) return;
  if(displayedGalleryVideos.length === 0){
    closeVideoLightbox();
    return;
  }
  vLightboxIndex = Math.min(indexBeforeDelete, displayedGalleryVideos.length - 1);
  renderVideoLightbox();
}
async function moveCurrentVideoLightboxItem(){
  const vid = displayedGalleryVideos[vLightboxIndex];
  if(!vid) return;
  const indexBefore = vLightboxIndex;
  if(!await openAssetMoveModal('video', [vid.name])) return;
  if(displayedGalleryVideos.length === 0){ closeVideoLightbox(); return; }
  const same = displayedGalleryVideos.findIndex(v => v.name === vid.name);
  vLightboxIndex = same >= 0 ? same : Math.min(indexBefore, displayedGalleryVideos.length - 1);
  renderVideoLightbox();
}
document.getElementById('video-gallery-lightbox-move-btn').addEventListener('click', moveCurrentVideoLightboxItem);
document.getElementById('video-gallery-lightbox-delete-btn').addEventListener('click', deleteCurrentVideoLightboxItem);
videoGalleryLightbox.addEventListener('click', (e) => {
  if(e.target === videoGalleryLightbox) closeVideoLightbox();
});
document.addEventListener('keydown', (e) => {
  if(videoGalleryLightbox.style.display === 'none' || assetMoveModalOpen()) return;
  if(e.target.closest && e.target.closest('input, textarea, select')) return;
  if(e.key === 'Escape'){ if(isLightboxFullscreen(videoGalleryLightbox)) setLightboxFullscreen(videoGalleryLightbox, false); else closeVideoLightbox(); }
  else if(e.key === 'f' || e.key === 'F') toggleLightboxFullscreen(videoGalleryLightbox);
  else if(e.key === ']' && isLightboxFullscreen(videoGalleryLightbox)) rotateLightboxView(videoGalleryLightbox, 90);
  else if(e.key === '[' && isLightboxFullscreen(videoGalleryLightbox)) rotateLightboxView(videoGalleryLightbox, -90);
  else if(e.key === 'ArrowLeft') showPrevVideo();
  else if(e.key === 'ArrowRight') showNextVideo();
  else if(e.key === 'Delete' || e.key === 'Backspace') deleteCurrentVideoLightboxItem();
});

// ============================================================
