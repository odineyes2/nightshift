// ---- 갤러리 ----
let galleryImages = [];
let lightboxIndex = -1;
let selectedGalleryNames = new Set();
let galleryViewMode = 'all'; // 'all' | 'byjob' | 'byproject' | 'bydate'
let galleryDisplayMode = 'grid'; // 'grid' | 'details' — 그룹 방식(galleryViewMode)과는 별개로, 카드/한 줄 목록 중 어떻게 그릴지
let galleryJobLabels = {}; // job_id -> 사람이 읽을 수 있는 라벨(템플릿명 · 옵션)
// 현재 화면에 실제로 그려진 순서 — 전체 보기는 galleryImages와 같고, 작업별 보기는
// 그룹별로 재배열된 순서다. 라이트박스의 이전/다음 탐색은 항상 이 배열을 따른다.
let displayedGalleryImages = [];
const galleryLightbox = document.getElementById('gallery-lightbox');

function fmtBytes(n){
  if(n < 1024) return `${n}B`;
  if(n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

function triggerBlobDownload(blob, filename){
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function triggerAnchorDownload(url, filename){
  const a = document.createElement('a');
  a.href = window.__nightshiftMediaUrl(url);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function updateGalleryToolbar(){
  const toolbar = document.getElementById('gallery-toolbar');
  if(selectedGalleryNames.size === 0){
    toolbar.style.display = 'none';
    return;
  }
  toolbar.style.display = 'flex';
  document.getElementById('gallery-toolbar-count').textContent = `${selectedGalleryNames.size}개 선택됨`;
  applyShareButtons();
}

// 갤러리에서 이미지를 지운다 — 한 장이든 여러 장이든 이 함수 하나로 처리한다
// (그리드 아이템의 개별 삭제 아이콘, 라이트박스의 삭제 버튼, 툴바의 선택 삭제 모두
// 여기로 모인다). 확인창을 통과하고 실제로 지워졌으면 true를 돌려준다.
async function deleteGalleryImages(names){
  if(names.length === 0) return false;
  const label = names.length === 1 ? `'${names[0]}' 이미지를` : `선택한 이미지 ${names.length}개를`;
  if(!confirm(`${label} 삭제할까요? 되돌릴 수 없어요.`)) return false;

  const errorEl = document.getElementById('gallery-error');
  errorEl.textContent = '';
  try{
    const res = await fetch('/api/output-images/delete-selected', {
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

  for(const name of names) selectedGalleryNames.delete(name);
  updateGalleryToolbar();
  await fetchGalleryImages();
  return true;
}

async function downloadGalleryImages(names){
  if(names.length === 0) return;
  const errorEl = document.getElementById('gallery-error');
  errorEl.textContent = '';
  try{
    if(names.length === 1){
      // 한 장이면 zip으로 감싸지 않고 원본을 그대로 받는다.
      triggerAnchorDownload(`/api/output-images/${encodeURIComponent(names[0])}`, names[0]);
      return;
    }
    const res = await fetch('/api/output-images/download-selected', {
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
    triggerBlobDownload(blob, match ? match[1] : 'nightshift_selected.zip');
  }catch(e){
    errorEl.textContent = '다운로드에 실패했어요.';
  }
}

// 5번 요구사항 — 선택한 이미지 중 가로형만 시계 방향 90도로 돌린 "다운로드본"을
// 내려받는다. 예전에는 원본 파일 자체를 영구히 돌려버렸는데(디스크에 덮어씀),
// 그 회전은 이 다운로드 한 번을 위한 것일 뿐이라 갤러리에 계속 남아있을 이유가
// 없다 — 그래서 원본은 절대 건드리지 않고, 서버가 회전한 바이트만 담은 zip을
// 받아온다(build_zip_from_paths의 rotate_landscape 옵션, 세로형/정사각형은
// 회전 없이 그대로 담김). 여러 장을 고르면 서버가 순회하며 회전+압축하는 동안
// 시간이 걸릴 수 있어 버튼에 스피너+안내 문구를 보여주고 비활성화해 중복
// 클릭을 막는다.
async function rotateThenDownloadGalleryImages(names){
  if(names.length === 0) return;
  const errorEl = document.getElementById('gallery-error');
  errorEl.textContent = '';
  const btn = document.getElementById('gallery-rotate-download-selected-btn');
  const originalLabel = btn.innerHTML;
  const setBusy = (text) => { btn.disabled = true; btn.innerHTML = `<span class="btn-spinner"></span><span class="btn-label"> ${text}</span>`; };
  const setIdle = () => { btn.disabled = false; btn.innerHTML = originalLabel; };

  setBusy('회전 및 다운로드 준비 중…');
  try{
    const res = await fetch('/api/output-images/download-selected-rotated', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.detail || '회전 후 다운로드에 실패했어요.';
      return;
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    triggerBlobDownload(blob, match ? match[1] : 'nightshift_selected_rotated.zip');
  }catch(e){
    errorEl.textContent = '회전 후 다운로드에 실패했어요.';
  }finally{
    setIdle();
  }
}

// 선택한 이미지를 전부 시계 방향 90도로 돌려 원본에 실제로 덮어쓴다 — 위
// rotateThenDownloadGalleryImages(다운로드본만 돌림)와 달리 갤러리에 남는 파일
// 자체를 바꾸는 명시적인 동작이다.
async function rotateGalleryImages(names){
  if(names.length === 0) return;
  const errorEl = document.getElementById('gallery-error');
  errorEl.textContent = '';
  const btn = document.getElementById('gallery-rotate-selected-btn');
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="btn-spinner"></span><span class="btn-label"> 회전 중…</span>`;
  try{
    const res = await fetch('/api/output-images/rotate-selected', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.detail || '회전에 실패했어요.';
      return;
    }
  }catch(e){
    errorEl.textContent = '회전에 실패했어요.';
    return;
  }finally{
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
  await fetchGalleryImages();
}

// 활성 작업 + 삭제된 작업(보관 기간 안이면) 전체를 합쳐 돌려준다 — 갤러리가 job_id로
// 그 작업의 라벨/설정을 찾아야 할 때(작업별 보기 소제목, 라이트박스의 "작업 불러오기")
// 공용으로 쓴다. 실패하면 조용히 빈 배열(호출부가 각자 사정에 맞게 처리).
async function fetchAllJobsIncludingDeleted(){
  try{
    const [activeRes, deletedRes] = await Promise.all([
      fetch('/api/jobs'),
      fetch('/api/jobs/deleted'),
    ]);
    const activeData = await activeRes.json().catch(() => ({ jobs: [] }));
    const deletedData = await deletedRes.json().catch(() => ({ jobs: [] }));
    return [...(activeData.jobs || []), ...(deletedData.jobs || [])];
  }catch(e){
    return [];
  }
}

// 작업별 보기의 그룹 소제목에 쓸 job_id -> 라벨 맵.
async function fetchGalleryJobLabels(){
  const labels = {};
  for(const j of await fetchAllJobsIncludingDeleted()){
    const templateText = j.template_label || j.template_id;
    labels[j.id] = `${templateText} · ${fmtOptions(j.options)}`;
  }
  return labels;
}

// "📋 자세히" 보기 한 줄의 헤더/행 — 둘 다 같은 grid-template-columns
// (.gallery-details-columns, CSS)를 써서 열이 맞춰진다.
function galleryDetailsHeaderHtml(){
  return `
    <div class="gallery-details-columns gallery-details-header">
      <span></span><span></span>
      <span>파일명</span><span>작업</span><span>해상도</span><span>용량</span><span>수정 시각</span>
      <span></span>
    </div>
  `;
}

function galleryDetailsJobLabel(img){
  if(!img.job_id) return '-';
  return galleryJobLabels[img.job_id] || `알 수 없는 작업 (${img.job_id})`;
}

function galleryItemHtml(img, index){
  const selected = selectedGalleryNames.has(img.name);
  const blur = !!img.nsfw && nsfwMode === 'blur';
  const blurOverlay = `<div class="gallery-item-blur-overlay" title="NSFW 블러 — 열어서 보기">${ico('eye-off')}</div>`;
  if(galleryDisplayMode === 'details'){
    const dims = (img.width && img.height) ? `${img.width}×${img.height}` : '-';
    const jobLabel = galleryDetailsJobLabel(img);
    const thumbSrc = blur ? '' : `src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(img.name)}/thumbnail?size=80&v=${encodeURIComponent(img.mtime)}`)}"`;
    return `
      <div class="gallery-item details gallery-details-columns${selected ? ' selected' : ''}" data-index="${index}">
        <label class="gallery-item-select" title="선택">
          <input type="checkbox" ${selected ? 'checked' : ''}>
        </label>
        <div class="gallery-item-thumb">
          ${blur ? blurOverlay : ''}
          <img ${thumbSrc} alt="${escapeHtml(img.name)}" loading="lazy">
        </div>
        <div class="gallery-details-col name" title="${escapeHtml(img.name)}">${img.favorite ? '<svg class="ico fav"><use href="#i-star"/></svg> ' : ''}${img.nsfw ? '<svg class="ico nsfw"><use href="#i-flame"/></svg> ' : ''}${escapeHtml(img.name)}</div>
        <div class="gallery-details-col job" title="${escapeHtml(jobLabel)}">${escapeHtml(jobLabel)}</div>
        <div class="gallery-details-col dim">${dims}</div>
        <div class="gallery-details-col size">${fmtBytes(img.size)}</div>
        <div class="gallery-details-col mtime">${fmtGalleryDateTime(img.mtime)}</div>
      </div>
    `;
  }
  const thumbSrc = blur ? '' : `src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(img.name)}/thumbnail?size=400&fit=cover&v=${encodeURIComponent(img.mtime)}`)}"`;
  return `
    <div class="gallery-item${selected ? ' selected' : ''}" data-index="${index}">
      <label class="gallery-item-select" title="선택">
        <input type="checkbox" ${selected ? 'checked' : ''}>
      </label>
      <button class="gallery-item-fav${img.favorite ? ' on' : ''}" type="button" aria-label="즐겨찾기" aria-pressed="${img.favorite ? 'true' : 'false'}" title="즐겨찾기">${ico('star')}</button>
      ${img.nsfw ? `<span class="gallery-item-nsfw" title="NSFW로 표시됨">${ico('flame')}</span>` : ''}
      ${blur ? blurOverlay : ''}
      <img ${thumbSrc} alt="${escapeHtml(img.name)}" loading="lazy">
      <div class="gallery-item-name">${escapeHtml(img.name)}</div>
    </div>
  `;
}

function wireGalleryItems(){
  document.querySelectorAll('#gallery-grid .gallery-item:not([data-wired])').forEach(el => {
    el.dataset.wired = '1';   // "더 보기"로 이어 붙일 때마다 부르므로 이미 묶은 칸은 건너뛴다
    const index = Number(el.dataset.index);
    const name = displayedGalleryImages[index].name;

    // 격자 모드는 썸네일 이미지만 눌러야 열리고(체크박스/삭제 버튼과 겹치지 않게),
    // 자세히 보기는 탐색기 행처럼 줄 전체 아무 데나 눌러도 열린다.
    const openTarget = el.classList.contains('details') ? el : el.querySelector('img');
    openTarget.addEventListener('click', () => openLightbox(index));

    const checkbox = el.querySelector('.gallery-item-select input');
    checkbox.addEventListener('click', (e) => e.stopPropagation());
    checkbox.addEventListener('change', () => {
      if(checkbox.checked) selectedGalleryNames.add(name);
      else selectedGalleryNames.delete(name);
      el.classList.toggle('selected', checkbox.checked);
      updateGalleryToolbar();
      const groupEl = el.closest('.gallery-group');
      if(groupEl) updateGroupCheckboxState(groupEl);
    });

    const favBtn = el.querySelector('.gallery-item-fav');
    if(favBtn) favBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setAssetsFavorite('image', [name], !favBtn.classList.contains('on'));
    });
  });
}

// 길게 눌러 삭제 — 파드 대시보드 카드에서 쓰던 것과 같은 패턴을 갤러리 항목에도
// 그대로 적용한다. 컨테이너 하나에 포인터 이벤트를 위임해 두면(itemSelector로
// 걸러서), 매번 innerHTML로 다시 그려지는 항목들에 대해 따로 연결할 필요가 없다.
function setupItemLongPressDelete(container, itemSelector, onFire){
  const MS = 600, VISUAL_MS = 220, MOVE_PX = 10;
  const state = { active: false, timer: null, visualTimer: null, el: null, x: 0, y: 0, firedAt: 0, touch: false };
  const cancel = () => {
    clearTimeout(state.timer);
    clearTimeout(state.visualTimer);
    if(state.el) state.el.classList.remove('item-pressing');
    state.active = false;
    state.el = null;
  };
  container.addEventListener('pointerdown', (e) => {
    state.touch = e.pointerType === 'touch' || e.pointerType === 'pen';
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    const el = e.target.closest(itemSelector);
    if(!el || e.target.closest('button, select, a, input, label')) return;
    cancel();
    state.active = true;
    state.el = el;
    state.x = e.clientX;
    state.y = e.clientY;
    state.visualTimer = setTimeout(() => el.classList.add('item-pressing'), VISUAL_MS);
    state.timer = setTimeout(() => {
      state.firedAt = Date.now();
      cancel();
      if(navigator.vibrate) navigator.vibrate(40);
      onFire(el);
    }, MS);
  });
  container.addEventListener('pointermove', (e) => {
    if(!state.active) return;
    if(Math.hypot(e.clientX - state.x, e.clientY - state.y) > MOVE_PX) cancel();
  });
  for(const type of ['pointerup', 'pointercancel', 'pointerleave']) container.addEventListener(type, cancel);
  // 롱프레스가 끝난 직후의 클릭(라이트박스 열기)은 삼킨다 — 캡처 단계에서 먼저 막는다.
  container.addEventListener('click', (e) => {
    if(Date.now() - state.firedAt < 900){ e.stopPropagation(); e.preventDefault(); }
  }, true);
  // 터치로 길게 누르면 브라우저가 이미지 저장 메뉴를 띄우려 한다 — 항목 위에서는 막는다.
  container.addEventListener('contextmenu', (e) => {
    if(state.touch && e.target.closest(itemSelector)) e.preventDefault();
  });
}

// 터치는 비수동 touchmove로 제어한다. 롱프레스 뒤 touch-action을 바꾸면
// 이미 시작한 터치에는 적용되지 않으므로 포인터 취소 후에도 터치 식별자를 유지한다.
function setupGallerySelection(container){
  let gesture = null, blockedUntil = 0;
  const finish = () => {
    if(!gesture) return;
    const g = gesture;
    gesture = null;
    clearTimeout(g.timer);
    clearTimeout(g.visualTimer);
    cancelAnimationFrame(g.frame);
    g.el.classList.remove('item-pressing');
    if(g.active) blockedUntil = Date.now() + 900;
    if(container.hasPointerCapture(g.pointerId)) container.releasePointerCapture(g.pointerId);
  };
  const cards = () => Array.from(container.querySelectorAll('.gallery-item')).map(el => ({el, rect:el.getBoundingClientRect()}));
  const select = (items) => {
    for(const {el} of items){
      const img = displayedGalleryImages[Number(el.dataset.index)];
      if(!img) continue;
      selectedGalleryNames.add(img.name);
      el.classList.add('selected');
      el.querySelector('.gallery-item-select input').checked = true;
    }
    updateGalleryToolbar();
    refreshAllGroupCheckboxStates();
  };
  const extend = () => {
    const g = gesture;
    if(!g?.active) return;
    const layout = cards();
    const start = layout.find(item => item.el === g.el);
    if(!start){ finish(); return; }
    const hit = layout.find(({rect:r}) => g.x >= r.left && g.x <= r.right && g.y >= r.top && g.y <= r.bottom);
    if(!hit) return; // 간격·제목은 대상에서 제외하되 제스처를 유지한다.
    const row = layout.filter(({rect:r}) => Math.abs(r.top - start.rect.top) < 2);
    if(!g.locked){
      g.right = Math.max(g.right, hit.rect.right);
      if(hit.rect.right >= Math.max(...row.map(item => item.rect.right)) - 2) g.locked = true;
    }
    const left = start.rect.left;
    select(layout.filter(({rect:r}) => r.top >= start.rect.top - 2 && r.top <= hit.rect.top + 2 &&
      (r.left + r.right) / 2 >= left && (r.left + r.right) / 2 <= g.right));
  };
  const tick = () => {
    const g = gesture;
    if(!g?.active) return;
    if(g.y > window.innerHeight - 48){
      // 카드가 속한 실제 스크롤 영역부터 찾고, 없으면 문서를 스크롤한다.
      let scroll = container.parentElement;
      while(scroll && !(scroll.scrollHeight > scroll.clientHeight && /auto|scroll/.test(getComputedStyle(scroll).overflowY))) scroll = scroll.parentElement;
      (scroll || document.scrollingElement).scrollBy(0, 8);
      extend();
    }
    g.frame = requestAnimationFrame(tick);
  };
  container.addEventListener('pointerdown', e => {
    if(gesture){ finish(); return; }
    if(!e.isPrimary || e.button !== 0) return;
    blockedUntil = 0; // 다음 독립적인 누름은 체크박스·버튼도 바로 사용할 수 있다.
    const el = e.target.closest('.gallery-item');
    if(!el || e.target.closest('button, select, a, input, label')) return;
    const g = gesture = {el, pointerId:e.pointerId, touch:e.pointerType === 'touch', touchId:null,
      x:e.clientX, y:e.clientY, startX:e.clientX, startY:e.clientY, active:false, locked:false};
    g.visualTimer = setTimeout(() => el.classList.add('item-pressing'), 220);
    g.timer = setTimeout(() => {
      if(gesture !== g || !el.isConnected) return;
      g.active = true;
      select([{el}]);
      g.right = el.getBoundingClientRect().right;
      if(!g.touch) container.setPointerCapture(g.pointerId);
      g.frame = requestAnimationFrame(tick);
    }, 600);
  });
  const move = (x, y) => {
    const g = gesture;
    if(!g) return;
    g.x = x; g.y = y;
    if(!g.active){
      if(Math.hypot(x - g.startX, y - g.startY) > 10) finish();
    }else extend();
  };
  window.addEventListener('pointermove', e => {
    if(gesture && !gesture.touch && e.pointerId === gesture.pointerId) move(e.clientX, e.clientY);
  });
  window.addEventListener('pointerup', e => { if(gesture && !gesture.touch && e.pointerId === gesture.pointerId) finish(); });
  container.addEventListener('pointercancel', e => {
    if(gesture && e.pointerId === gesture.pointerId && !(gesture.touch && gesture.active)) finish();
  });
  container.addEventListener('lostpointercapture', e => {
    if(gesture && !gesture.touch && e.pointerId === gesture.pointerId) finish();
  });
  container.addEventListener('touchstart', e => {
    if(e.touches.length !== 1){ finish(); return; }
    if(gesture?.touch) gesture.touchId = e.changedTouches[0].identifier;
  }, {passive:true});
  window.addEventListener('touchmove', e => {
    if(!gesture?.touch) return;
    const t = Array.from(e.touches).find(t => t.identifier === gesture.touchId);
    if(!t || e.touches.length !== 1){ finish(); return; }
    if(gesture.active) e.preventDefault();
    move(t.clientX, t.clientY);
  }, {passive:false});
  for(const type of ['touchend', 'touchcancel']) window.addEventListener(type, e => {
    if(gesture?.touch && Array.from(e.changedTouches).some(t => t.identifier === gesture.touchId)) finish();
  });
  window.addEventListener('blur', finish);
  container.addEventListener('click', e => {
    if(gesture?.active || Date.now() < blockedUntil){ e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  container.addEventListener('contextmenu', e => { if(e.target.closest('.gallery-item')) e.preventDefault(); });
  container.addEventListener('dragstart', e => { if(e.target.closest('.gallery-item')) e.preventDefault(); });
  return finish;
}
const cancelGallerySelection = setupGallerySelection(document.getElementById('gallery-grid'));

// 그룹(작업별/날짜별 보기의 소제목) 체크박스를 그 그룹 안 이미지들의 현재 선택
// 상태에 맞춘다 — 전부 선택돼 있으면 체크, 하나도 없으면 빈 채, 일부만 선택돼
// 있으면 indeterminate(가로줄) 표시. 개별 이미지 체크박스를 바꿀 때, 그리고
// 매번 그룹을 새로 그린 직후 호출한다.
// 묶음 소제목의 체크박스 상태 — 화면에 그린 칸이 아니라 그 묶음 전체(아직 안 그린 것 포함) 기준이다.
function updateGroupCheckboxState(groupEl){
  const groupCheckbox = groupEl.querySelector('.gallery-group-checkbox');
  if(!groupCheckbox) return;
  const names = galleryGroupNames.get(groupEl.dataset.groupKey) || [];
  const selectedCount = names.filter(n => selectedGalleryNames.has(n)).length;
  groupCheckbox.checked = names.length > 0 && selectedCount === names.length;
  groupCheckbox.indeterminate = selectedCount > 0 && selectedCount < names.length;
}

function refreshAllGroupCheckboxStates(){
  document.querySelectorAll('.gallery-group').forEach(updateGroupCheckboxState);
}

// 그룹 소제목의 체크박스를 눌렀을 때 그 그룹 안 이미지 전체를 한꺼번에 선택/해제한다
// (요구사항: "작업별 보기를 할 때 작업별로 한꺼번에 선택"). 아직 안 그린 칸도 선택에 들어가고,
// 그려 둔 칸은 그 자리에서 체크 표시를 맞춘다.
function wireGalleryGroupCheckbox(groupEl){
  const groupCheckbox = groupEl.querySelector('.gallery-group-checkbox');
  groupCheckbox.addEventListener('change', () => {
    const checked = groupCheckbox.checked;
    groupCheckbox.indeterminate = false;
    for(const name of galleryGroupNames.get(groupEl.dataset.groupKey) || []){
      if(checked) selectedGalleryNames.add(name);
      else selectedGalleryNames.delete(name);
    }
    groupEl.querySelectorAll('.gallery-item').forEach(itemEl => {
      itemEl.classList.toggle('selected', checked);
      const itemCheckbox = itemEl.querySelector('.gallery-item-select input');
      if(itemCheckbox) itemCheckbox.checked = checked;
    });
    updateGalleryToolbar();
  });
}

// 현재 galleryViewMode에서 img가 속할 그룹의 키를 정한다 — byjob은 job_id,
// bydate는 mtime을 로컬 날짜로 뭉친 문자열("YYYY-MM-DD").
function galleryGroupKeyFor(img){
  if(galleryViewMode === 'byjob') return img.job_id || '';
  if(galleryViewMode === 'byproject') return projectGroupKey(img);
  if(galleryViewMode === 'bydate') return fmtDateKey(img.mtime);
  return '';
}

function galleryGroupLabelFor(key){
  if(galleryViewMode === 'byjob'){
    return key ? (galleryJobLabels[key] || `알 수 없는 작업 (${key})`) : '작업 정보 없음';
  }
  if(galleryViewMode === 'byproject') return projectGroupLabel(key);
  if(galleryViewMode === 'bydate') return fmtDateLabel(key);
  return '';
}

// 프로젝트별 보기의 그룹 — 프로젝트가 없는 것은 "미분류", 지워졌거나 아직 못 읽은 프로젝트를
// 가리키는 것은 번호로만 알 수 있어 따로 묶는다(이미지·영상 갤러리 공용).
function projectGroupKey(item){ return item.project_id == null ? '' : String(item.project_id); }
function projectGroupLabel(key){
  if(key === '') return UNASSIGNED_LABEL;
  return projectName(Number(key)) || `알 수 없는 프로젝트 (${key})`;
}

// 프로젝트 안에서 보는 갤러리는 전부 한 프로젝트라 "프로젝트별"이 의미가 없다 — 버튼을 숨기고,
// 그 보기 중이었다면 전체 보기로 돌린다. 바뀐 보기 방식을 돌려준다.
function applyProjectViewScope(toggleId, projectScoped, mode){
  const btn = document.querySelector(`#${toggleId} [data-view="byproject"]`);
  if(btn) btn.style.display = projectScoped ? 'none' : '';
  if(projectScoped && mode === 'byproject'){
    mode = 'all';
    document.querySelectorAll(`#${toggleId} .enhance-mode-btn`).forEach(b => b.classList.toggle('active', b.dataset.view === mode));
  }
  return mode;
}

// galleryImages(전체 목록)를 galleryViewMode에 맞춰 그린다. byjob/bydate는 각각
// job_id/날짜로 묶고, 각 그룹은 그 안의 가장 최근 이미지 기준으로 정렬된다 —
// galleryImages가 이미 mtime 내림차순이라 각 그룹 키가 처음 등장하는 순서를
// 그대로 쓰면 된다(Map은 키를 처음 넣은 순서를 유지한다).
// 전체 순서는 displayedGalleryImages(라이트박스·선택이 쓰는 목록)에 두고, 화면에는 앞에서부터 galleryLimit개만
// 그린다 — 나머지는 "더 보기"가 이어 붙인다(appendGalleryItems). 보기 방식·필터·범위가 바뀌면 다시 LIST_PAGE개부터.
let galleryLimit = LIST_PAGE;
let galleryRendered = 0;
let galleryGroupNames = new Map();   // 묶음 키 -> 그 묶음의 이미지 이름 전부(안 그린 것 포함)
let galleryScopeKey = '';
function renderGalleryGrid(){
  cancelGallerySelection();
  const grid = document.getElementById('gallery-grid');
  const isDetails = galleryDisplayMode === 'details';
  grid.classList.toggle('details-mode', isDetails);
  const scope = JSON.stringify([galleryViewMode, galleryDisplayMode, galleryPodFilter, galleryProjectFilter, galleryFilterQs(galleryFilters)]);
  if(scope !== galleryScopeKey){ galleryScopeKey = scope; galleryLimit = LIST_PAGE; }
  if(galleryViewMode !== 'all'){
    const groups = new Map();
    for(const img of galleryImages){
      const key = galleryGroupKeyFor(img);
      if(!groups.has(key)) groups.set(key, []);
      groups.get(key).push(img);
    }
    displayedGalleryImages = [].concat(...groups.values());
    galleryGroupNames = new Map([...groups].map(([key, list]) => [key, list.map(img => img.name)]));
  }else{
    displayedGalleryImages = galleryImages;
    galleryGroupNames = new Map();
  }
  // 자세히 보기일 때만 맨 위에 열 제목 행을 한 번 붙인다 — 그룹들보다 위, #gallery-grid 바로 아래 한 곳에만
  // 둔다(그룹마다 반복 안 함). .gallery-item 클래스가 없어서 선택/삭제 로직은 이 행을 그냥 지나친다.
  grid.innerHTML = isDetails ? galleryDetailsHeaderHtml() : '';
  galleryRendered = 0;
  appendGalleryItems();
}

// displayedGalleryImages에서 아직 안 그린 칸을 galleryLimit까지 이어 붙인다. 묶음 보기면 마지막 묶음이 이어지면
// 그 안에, 아니면 새 묶음(소제목의 개수는 묶음 전체)을 만들어 넣는다.
function appendGalleryItems(){
  const grid = document.getElementById('gallery-grid');
  const isDetails = galleryDisplayMode === 'details';
  const end = Math.min(galleryLimit, displayedGalleryImages.length);
  for(let i = galleryRendered; i < end; i++){
    const img = displayedGalleryImages[i];
    let target = grid;
    if(galleryViewMode !== 'all'){
      const key = galleryGroupKeyFor(img);
      let groupEl = grid.lastElementChild;
      if(!groupEl || !groupEl.classList.contains('gallery-group') || groupEl.dataset.groupKey !== key){
        grid.insertAdjacentHTML('beforeend', `
          <div class="gallery-group" data-group-key="${escapeHtml(key)}">
            <div class="gallery-group-header">
              <label class="gallery-group-select-label" title="이 그룹 전체 선택">
                <input type="checkbox" class="gallery-group-checkbox">
                ${escapeHtml(galleryGroupLabelFor(key))} · ${(galleryGroupNames.get(key) || []).length}장
              </label>
            </div>
            <div class="gallery-group-grid${isDetails ? ' details-mode' : ''}"></div>
          </div>
        `);
        groupEl = grid.lastElementChild;
        wireGalleryGroupCheckbox(groupEl);
      }
      target = groupEl.querySelector('.gallery-group-grid');
    }
    target.insertAdjacentHTML('beforeend', galleryItemHtml(img, i));
  }
  galleryRendered = end;
  wireGalleryItems();
  refreshAllGroupCheckboxStates();
  updateLoadMore('gallery-more-btn', displayedGalleryImages.length - galleryRendered, '장');
}
setupLoadMore('gallery-more-btn', () => { galleryLimit += LIST_PAGE; appendGalleryItems(); });

document.querySelectorAll('#gallery-view-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.view === galleryViewMode) return;
    galleryViewMode = btn.dataset.view;
    document.querySelectorAll('#gallery-view-toggle .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.view === galleryViewMode));
    if(galleryImages.length > 0) renderGalleryGrid();
  });
});
document.querySelectorAll('#gallery-display-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.display === galleryDisplayMode) return;
    galleryDisplayMode = btn.dataset.display;
    document.querySelectorAll('#gallery-display-toggle .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.display === galleryDisplayMode));
    if(galleryImages.length > 0) renderGalleryGrid();
  });
});

// job_id로 그 작업이 배정된 파드를 찾는다. lastJobs는 2초마다 갱신되는 전역 작업
// 목록(작업 화면과 공유)이라 따로 다시 받아올 필요가 없다. job_id가 없거나(=
// nightshift 큐를 거치지 않고 ComfyUI에서 직접 돌린 결과) 오래전에 완전히 삭제된
// 작업이면, "⬇ 결과 가져오기"가 남긴 동기화 기록(synced_pod_id, app.py가
// comfy_output_sync.json에서 채워 준다)으로 한 번 더 찾아본다. 그것도 없으면
// 정말로 "이 파드 것"이라고 확신할 근거가 없는 것이므로 파드 갤러리에서는 뺀다
// (전체 갤러리에서는 그대로 보인다).
function podIdForImage(img){
  if(img.job_id){
    const job = lastJobs.find(j => j.id === img.job_id);
    if(job) return job.pod_id;
  }
  return img.synced_pod_id || null;
}

// podIdForImage와 완전히 같은 규칙 — 영상도 output-videos API가 job_id/
// synced_pod_id를 그대로 노출하므로 로직을 그대로 재사용할 수 있지만, 파드
// 갤러리는 podIdForImage라는 이름 자체가 "이미지 얘기"로 읽혀서 헷갈리지
// 않게 이름만 따로 둔다.
function podIdForVideo(vid){
  if(vid.job_id){
    const job = lastJobs.find(j => j.id === vid.job_id);
    if(job) return job.pod_id;
  }
  return vid.synced_pod_id || null;
}

// 갤러리 화면이 "전체"인지 "이 파드 것만"인지에 따라 제목/가져오기 버튼을 맞춘다.
// showTab()이 galleryPodFilter를 정한 직후, 그리고 상태 폴링(fetchComfyStatus)마다
// 부른다 — 둘 다 곧바로 반영돼야 5초씩 기다리다 헷갈리지 않는다.
function updateGalleryScopeUI(){
  galleryViewMode = applyProjectViewScope('gallery-view-toggle', galleryProjectFilter !== null, galleryViewMode);
  const label = document.getElementById('gallery-section-label');
  if(label){
    label.innerHTML = galleryPodFilter
      ? ico('images') + ' ' + escapeHtml(`Gallery · ${podName(galleryPodFilter) || galleryPodFilter}`)
      : (galleryProjectFilter !== null
        ? ico('images') + ' ' + escapeHtml(`Gallery · ${projectName(galleryProjectFilter)}`)
        : 'Gallery');
  }
  applyPullOutputsVisibility();
}

// updateGalleryScopeUI와 같은 자리(파드 스코프 진입/전환마다)에서 영상 갤러리
// 제목만 맞춘다 — applyPullOutputsVisibility는 이미지·영상 버튼을 한 번에
// 처리하므로 여기서 다시 부르지 않는다(showTab이 둘 다 순서대로 호출한다).
function updateVideoGalleryScopeUI(){
  videoGalleryViewMode = applyProjectViewScope('video-gallery-view-toggle', videoGalleryProjectFilter !== null, videoGalleryViewMode);
  const label = document.getElementById('video-gallery-section-label');
  if(label){
    label.innerHTML = ico('clapperboard') + ' ' + escapeHtml(videoGalleryPodFilter
      ? `Videos · ${podName(videoGalleryPodFilter) || videoGalleryPodFilter}`
      : (videoGalleryProjectFilter !== null
        ? `Videos · ${projectName(videoGalleryProjectFilter)}`
        : 'Videos'));
  }
}

// 4초마다 목록을 다시 받아 오지만, 그릴 내용(파일·즐겨찾기·평점·태그·프로젝트·작업 이름·보기 방식)이 그대로면 격자를
// 통째로 다시 만들지 않는다 — 다시 만들면 <img>/<video> 썸네일이 전부 새로 만들어져 깜빡이고 네트워크도 매번 다시 쓴다.
function gallerySignature(items, extra){
  return JSON.stringify([
    items.map(i => [i.name, i.mtime, i.size, i.favorite, i.rating, i.nsfw, i.tags, i.project_id, i.owner_name, i.synced_pod_id]),
    galleryJobLabels, extra,
    // 블러 모드에서는 썸네일 자체(<img src>가 있는지)가 nsfw 값과 렌더링 방식에 따라 달라지니,
    // 지금 모드도 서명에 넣는다 — 안 그러면 목록 내용은 그대로인데 토글만 바꿨을 때(예: 보기 -> 블러)
    // "바뀐 게 없다"고 오판해서 블러 처리를 건너뛴다.
    nsfwMode,
  ]);
}
let lastImageGallerySig = '';
let lastVideoGallerySig = '';

async function fetchGalleryImages(){
  const grid = document.getElementById('gallery-grid');
  const emptyMsg = document.getElementById('gallery-empty');
  const countEl = document.getElementById('gallery-count');
  try{
    const [imgRes, labels] = await Promise.all([
      fetch('/api/output-images' + galleryFilterQs(galleryFilters)),
      fetchGalleryJobLabels(),
    ]);
    const data = await imgRes.json();
    galleryImages = data.images || [];
    // 파드 갤러리면 그 파드가 배정된 작업의 이미지만 남긴다.
    if(galleryPodFilter){
      galleryImages = galleryImages.filter(img => podIdForImage(img) === galleryPodFilter);
    }
    if(galleryProjectFilter !== null){
      galleryImages = galleryImages.filter(img => projectMatches(img.project_id, galleryProjectFilter));
    }
    galleryJobLabels = labels;
  }catch(e){
    galleryImages = [];
    galleryJobLabels = {};
  }

  // 그 사이 지워진 이미지의 선택은 버린다.
  const stillPresent = new Set(galleryImages.map(img => img.name));
  for(const name of Array.from(selectedGalleryNames)){
    if(!stillPresent.has(name)) selectedGalleryNames.delete(name);
  }
  updateGalleryToolbar();

  countEl.textContent = galleryImages.length ? `총 ${galleryImages.length}장` : '';
  document.getElementById('gallery-select-all-btn').disabled = galleryImages.length === 0;

  if(galleryImages.length === 0){
    lastImageGallerySig = '';
    grid.innerHTML = '';
    displayedGalleryImages = [];
    updateLoadMore('gallery-more-btn', 0, '장');
    emptyMsg.textContent = galleryFiltersActive(galleryFilters) ? '조건에 맞는 이미지가 없어요'
      : galleryPodFilter ? '이 워커가 만든 이미지가 아직 없어요'
      : (galleryProjectFilter !== null ? '이 프로젝트의 이미지가 아직 없어요' : '서버에 저장된 이미지가 없어요');
    emptyMsg.style.display = 'block';
    return;
  }
  emptyMsg.style.display = 'none';

  const sig = gallerySignature(galleryImages, [galleryViewMode, galleryDisplayMode, galleryPodFilter, galleryProjectFilter]);
  if(sig === lastImageGallerySig && grid.childElementCount > 0) return;   // 바뀐 게 없다
  lastImageGallerySig = sig;
  renderGalleryGrid();
}

document.getElementById('gallery-refresh-btn').addEventListener('click', fetchGalleryImages);

// 원격 ComfyUI에서 결과 이미지를 끌어온다. 작업이 끝날 때마다 서버가 자동으로도
// 가져오지만(접속 주소 설정의 "결과 이미지를 이 서버로 가져오기"), pod를 껐다 켠 뒤
// 밀린 것을 한꺼번에 받거나 설정을 뒤늦게 켠 경우를 위해 수동 버튼도 둔다.
document.getElementById('gallery-pull-btn').addEventListener('click', async () => {
  const btn = document.getElementById('gallery-pull-btn');
  const errorEl = document.getElementById('gallery-error');
  errorEl.textContent = '';
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = ico('loader-circle', true) + ' 가져오는 중…';
  try{
    const res = await fetch('/api/comfy-outputs/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(galleryPodFilter ? { pod_id: galleryPodFilter } : {}),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '가져오지 못했어요.');
    const got = (data.downloaded || []).length;
    const failed = (data.errors || []).length;
    errorEl.textContent = failed
      ? `${got}장 가져왔어요 (실패 ${failed}건: ${data.errors.slice(0, 2).join('; ')})`
      : (got ? '' : '새로 가져올 이미지가 없어요.');
    if(got) await fetchGalleryImages();
  }catch(e){
    errorEl.textContent = e.message || '가져오지 못했어요.';
  }finally{
    btn.disabled = false;
    btn.innerHTML = original;
  }
});
document.getElementById('gallery-select-all-btn').addEventListener('click', () => {
  for(const img of galleryImages) selectedGalleryNames.add(img.name);
  document.querySelectorAll('.gallery-item').forEach(el => el.classList.add('selected'));
  document.querySelectorAll('.gallery-item-select input').forEach(cb => { cb.checked = true; });
  refreshAllGroupCheckboxStates();
  updateGalleryToolbar();
});
document.getElementById('gallery-download-selected-btn').addEventListener('click', () => {
  downloadGalleryImages(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-rotate-download-selected-btn').addEventListener('click', () => {
  rotateThenDownloadGalleryImages(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-rotate-selected-btn').addEventListener('click', () => {
  rotateGalleryImages(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-send-to-pose-btn').addEventListener('click', () => {
  openPoseImportModal(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-library-selected-btn').addEventListener('click', () => {
  openLibraryFromGallery(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-delete-selected-btn').addEventListener('click', () => {
  deleteGalleryImages(Array.from(selectedGalleryNames));
});
document.getElementById('gallery-clear-selection-btn').addEventListener('click', () => {
  selectedGalleryNames.clear();
  document.querySelectorAll('.gallery-item.selected').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll('.gallery-item-select input').forEach(cb => { cb.checked = false; });
  refreshAllGroupCheckboxStates();
  updateGalleryToolbar();
});

// ---- 갤러리 → 참조 세트로 보내기 ----
// 마음에 든 결과 이미지를 pose_batch/depth_batch/lineart_batch(및 각 csv 버전)가
// 참조하는 세트 폴더(NIGHTSHIFT_ASSETS_DIR/<kind>/<char_no>/<세트>/)에 사본으로
// 넣는다. 원본은 지우지 않는다(복사). 종류(kind)/char_no/참조 세트 드롭다운은
// "새 작업 추가" 폼과 같은 assetsTreeByKind/refSetsForCharNo를 재사용하고,
// char_no/세트 둘 다 "새로 만들기"를 고르면 텍스트 입력이 나타나 없는 인물 수/
// 세트도 그 자리에서 새로 만들 수 있다(실제 생성은 서버의 save_ref_image가
// 폴더를 그때 만든다).
const poseImportModal = document.getElementById('pose-import-modal');
let poseImportTargetNames = [];

function populatePoseImportCharNoSelect(){
  const kind = document.getElementById('pose-import-kind-select').value;
  const select = document.getElementById('pose-import-char-no-select');
  const tree = assetsTreeByKind[kind] || [];
  select.innerHTML = tree.map(t => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}명</option>`).join('')
    + '<option value="__new__">+ 새 인물 수 만들기</option>';
}

function populatePoseImportSetSelect(charNo){
  const kind = document.getElementById('pose-import-kind-select').value;
  const select = document.getElementById('pose-import-set-select');
  const sets = charNo === '__new__' ? [] : refSetsForCharNo(kind, charNo);
  select.innerHTML = sets.map(s => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)} (${s.count}장)</option>`).join('')
    + '<option value="__new__">+ 새 참조 세트 만들기</option>';
  // 고를 수 있는 기존 세트가 없으면(새 인물 수 포함) "새로 만들기"가 유일한
  // 선택지이니 처음부터 그 상태로 보여준다.
  if(sets.length === 0) select.value = '__new__';
  updatePoseImportNewInputsVisibility();
}

function updatePoseImportNewInputsVisibility(){
  const charNoSelect = document.getElementById('pose-import-char-no-select');
  const setSelect = document.getElementById('pose-import-set-select');
  document.getElementById('pose-import-char-no-new').style.display = charNoSelect.value === '__new__' ? '' : 'none';
  document.getElementById('pose-import-set-new').style.display = setSelect.value === '__new__' ? '' : 'none';
}

document.getElementById('pose-import-kind-select').addEventListener('change', () => {
  populatePoseImportCharNoSelect();
  populatePoseImportSetSelect(document.getElementById('pose-import-char-no-select').value);
});
document.getElementById('pose-import-char-no-select').addEventListener('change', (e) => {
  populatePoseImportSetSelect(e.target.value);
});
document.getElementById('pose-import-set-select').addEventListener('change', updatePoseImportNewInputsVisibility);

async function openPoseImportModal(names){
  if(names.length === 0) return;
  poseImportTargetNames = names;
  const statusEl = document.getElementById('pose-import-status');
  statusEl.textContent = '';
  statusEl.classList.remove('error', 'success');
  document.getElementById('pose-import-count-hint').textContent = `선택한 이미지 ${names.length}장을 보낼 종류/인물 수/참조 세트를 고르세요.`;
  document.getElementById('pose-import-char-no-new').value = '';
  document.getElementById('pose-import-set-new').value = '';
  document.getElementById('pose-import-kind-select').value = 'pose';

  await fetchAssets(); // 방금 새로 만든 세트까지 반영해서 최신 목록으로 연다.
  populatePoseImportCharNoSelect();
  populatePoseImportSetSelect(document.getElementById('pose-import-char-no-select').value);
  poseImportModal.style.display = 'flex';
}

function closePoseImportModal(){
  poseImportModal.style.display = 'none';
}

document.getElementById('pose-import-modal-close').addEventListener('click', closePoseImportModal);
document.getElementById('pose-import-cancel-btn').addEventListener('click', closePoseImportModal);
poseImportModal.addEventListener('click', (e) => { if(e.target === poseImportModal) closePoseImportModal(); });
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && poseImportModal.style.display !== 'none') closePoseImportModal();
});

document.getElementById('pose-import-confirm-btn').addEventListener('click', async () => {
  const statusEl = document.getElementById('pose-import-status');
  statusEl.textContent = '';
  statusEl.classList.remove('error', 'success');

  const kind = document.getElementById('pose-import-kind-select').value;
  const charNoSelect = document.getElementById('pose-import-char-no-select');
  const setSelect = document.getElementById('pose-import-set-select');
  const charNo = charNoSelect.value === '__new__'
    ? document.getElementById('pose-import-char-no-new').value.trim()
    : charNoSelect.value;
  const refSet = setSelect.value === '__new__'
    ? document.getElementById('pose-import-set-new').value.trim()
    : setSelect.value;

  if(!charNo){
    statusEl.textContent = '인물 수를 입력하세요.';
    statusEl.classList.add('error');
    return;
  }
  if(!refSet){
    statusEl.textContent = '참조 세트 이름을 입력하세요.';
    statusEl.classList.add('error');
    return;
  }

  const confirmBtn = document.getElementById('pose-import-confirm-btn');
  confirmBtn.disabled = true;
  try{
    const res = await fetch('/api/assets/import-from-output', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: poseImportTargetNames, kind: kind, char_no: charNo, set_name: refSet }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok){
      statusEl.textContent = data.detail || '전송에 실패했어요.';
      statusEl.classList.add('error');
      return;
    }
    const skippedCount = (data.skipped || []).length;
    if(skippedCount === 0){
      statusEl.textContent = `${data.added}장을 '${refSet}'에 추가했어요.`;
      statusEl.classList.add('success');
      await fetchAssets();
      setTimeout(closePoseImportModal, 900);
    }else if(data.added > 0){
      statusEl.textContent = `${data.added}장 추가, ${skippedCount}장은 건너뛰었어요 (이미 지워진 이미지 등).`;
      statusEl.classList.add('success');
      await fetchAssets();
    }else{
      statusEl.textContent = `전부 건너뛰었어요 — ${(data.skipped && data.skipped[0] && data.skipped[0].reason) || '알 수 없는 이유'}`;
      statusEl.classList.add('error');
    }
  }catch(e){
    statusEl.textContent = '전송에 실패했어요.';
    statusEl.classList.add('error');
  }finally{
    confirmBtn.disabled = false;
  }
});

// 다음/이전 이미지를 실제로 받아오는 동안 화면이 멈춘 것처럼 보이지 않게,
// 살짝 흐리고 어둡게 + 스피너를 보여준다. 로딩이 아주 빠르면(로컬 네트워크라
// 대부분 그렇다) 깜빡임만 남으니, 150ms 넘게 걸릴 때만 실제로 보여준다.
let lightboxLoadingTimer = null;
function showLightboxLoading(){
  clearTimeout(lightboxLoadingTimer);
  lightboxLoadingTimer = setTimeout(() => {
    document.getElementById('gallery-lightbox-img').classList.add('lightbox-loading');
    document.getElementById('gallery-lightbox-spinner').style.display = '';
  }, 150);
}
function hideLightboxLoading(){
  clearTimeout(lightboxLoadingTimer);
  document.getElementById('gallery-lightbox-img').classList.remove('lightbox-loading');
  document.getElementById('gallery-lightbox-spinner').style.display = 'none';
}
// 파일명이 그대로라 URL도 그대로면 브라우저가 캐시를 그냥 쓸 수 있어서(회전
// 등으로 파일이 바뀌어도 안 보임), mtime을 쿼리에 붙여 내용이 바뀔 때마다
// 다른 URL이 되게 한다 — 썸네일(galleryItemHtml)도 같은 이유로 이렇게 한다.
function lightboxOriginalUrl(img){
  return window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(img.name)}?v=${encodeURIComponent(img.mtime)}`);
}
function lightboxThumbUrl(img, size){
  return window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(img.name)}/thumbnail?size=${size}&v=${encodeURIComponent(img.mtime)}`);
}
// 라이트박스는 원본(수 MB PNG) 대신 화면 크기에 맞춘 사본을 받는다. 크기는 서버 단계(1280·2048)에
// 맞춰 골라야 미리 받은 것과 주소가 같아 브라우저 캐시를 그대로 쓴다.
function lightboxScreenUrl(img){
  const px = Math.max(window.innerWidth, window.innerHeight) * (window.devicePixelRatio || 1);
  return lightboxThumbUrl(img, px <= 1280 ? 1280 : 2048);
}
let lightboxOriginalName = null;   // "원본 보기"를 누른 사진 이름 — 다른 사진으로 넘기면 저절로 화면용 사본으로 돌아간다
let lightboxLoadSeq = 0;
function preloadLightboxNeighbors(){
  [lightboxIndex - 1, lightboxIndex + 1].forEach(i => {
    const img = displayedGalleryImages[i];
    if(img) new Image().src = lightboxScreenUrl(img);
  });
}

function renderLightbox(){
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  document.getElementById('gallery-lightbox-error').textContent = '';
  const imgEl = document.getElementById('gallery-lightbox-img');
  const showOriginal = lightboxOriginalName === img.name;
  const fullUrl = showOriginal ? lightboxOriginalUrl(img) : lightboxScreenUrl(img);
  const seq = ++lightboxLoadSeq;
  const hi = new Image();
  const done = () => { if(seq !== lightboxLoadSeq) return; imgEl.src = fullUrl; hideLightboxLoading(); };
  hi.onload = done;
  hi.onerror = done;
  hi.src = fullUrl;
  if(hi.complete && hi.naturalWidth){
    done();   // 미리 받아 둔 사본 — 바로 바꿔 끼운다
  }else{
    // 점진 표시: 작은 400px 사본을 먼저 늘려 보여 주고, 큰 사본이 오면 바꿔 끼운다.
    imgEl.src = lightboxThumbUrl(img, 400);
    showLightboxLoading();
  }
  imgEl.alt = img.name;
  const origBtn = document.getElementById('gallery-lightbox-original-btn');
  origBtn.disabled = showOriginal;
  origBtn.title = showOriginal ? `원본을 보고 있어요 (${fmtBytes(img.size)})` : `원본 보기 (${fmtBytes(img.size)})`;
  preloadLightboxNeighbors();
  document.getElementById('gallery-lightbox-info').textContent =
    `${img.name} · ${fmtBytes(img.size)} · ${fmtGalleryDateTime(img.mtime)} (${lightboxIndex + 1}/${displayedGalleryImages.length})${img.owner_name ? ' · ' + img.owner_name : ''}`;
  imageMetaPanel.show(img);
  document.getElementById('gallery-prev').disabled = lightboxIndex <= 0;
  document.getElementById('gallery-next').disabled = lightboxIndex >= displayedGalleryImages.length - 1;
}

function openLightbox(index){
  lightboxIndex = index;
  renderLightbox();
  galleryLightbox.style.display = 'flex';
}

function closeLightbox(){
  if(isLightboxFullscreen(galleryLightbox)) setLightboxFullscreen(galleryLightbox, false);
  galleryLightbox.style.display = 'none';
  lightboxIndex = -1;
}

function showPrevImage(){
  if(lightboxIndex > 0){ lightboxIndex--; renderLightbox(); }
}

function showNextImage(){
  if(lightboxIndex < displayedGalleryImages.length - 1){ lightboxIndex++; renderLightbox(); }
}

// 모바일에서 화살표 버튼 대신 스와이프로도 이미지를 넘길 수 있게 — 왼쪽으로
// 밀면 다음, 오른쪽으로 밀면 이전. 세로로 더 많이 움직였으면(스크롤 의도로
// 보고) 무시하고, 임계값(50px)을 넘어야만 넘긴다. 손가락을 움직이는 동안
// 이미지가 그 방향으로 살짝 따라와서(damped) "지금 스와이프가 인식되고
// 있다"는 걸 눈으로 바로 알 수 있게 하고, 손을 떼면 놓친 스와이프든 넘어가는
// 스와이프든 일단 트랜지션과 함께 원위치로 돌아온다(넘어갈 땐 그사이 다음
// 이미지가 로딩 상태로 바뀐다).
// ---- 라이트박스 전체 화면 (이미지·영상 공용) ----
// 상태표시줄 숨김을 우선해 브라우저 전체 화면을 요청한다(NS-29). 종료 안내는 브라우저가 관리한다.
// 지원하지 않거나 거절한 환경에서는 기존 창 채우기를 유지하고 한계를 알린다.
let lightboxNativeFullscreen = null;
const lightboxFullscreenPending = new WeakSet();
function lightboxFullscreenElement(){ return document.fullscreenElement || document.webkitFullscreenElement; }
function isLightboxFullscreen(overlay){ return overlay.classList.contains('lightbox-fullscreen'); }
function paintLightboxFullscreen(overlay, on){
  const btn = overlay.querySelector('.lightbox-fs-btn');
  overlay.classList.toggle('lightbox-fullscreen', on);
  if(btn){
    btn.innerHTML = ico(on ? 'minimize' : 'maximize');
    btn.title = on ? '전체 화면 끝내기 (F · Esc)' : '전체 화면 (F)';
    btn.setAttribute('aria-label', on ? '전체 화면 끝내기' : '전체 화면');
  }
}
async function exitLightboxFullscreen(overlay){
  if(lightboxFullscreenElement() !== overlay) return; // 영상 controls나 다른 요소의 전체 화면은 건드리지 않는다.
  const exit = document.exitFullscreen || document.webkitExitFullscreen;
  if(exit) await exit.call(document);
}
async function setLightboxFullscreen(overlay, on){
  if(on && lightboxFullscreenPending.has(overlay)) return;
  paintLightboxFullscreen(overlay, on);
  if(!on){
    try{ await exitLightboxFullscreen(overlay); }
    catch(e){ alert('전체 화면을 끝내지 못했어요 — 기기의 뒤로가기나 Esc로 나가 주세요.'); }
    return;
  }
  if(lightboxFullscreenElement() === overlay) return;
  const request = overlay.requestFullscreen || overlay.webkitRequestFullscreen;
  if(!request){
    alert('이 브라우저에서는 상태표시줄을 숨길 수 없어요. 화면 안에서 이미지를 크게 보여 드려요.');
    return;
  }
  lightboxFullscreenPending.add(overlay);
  try{
    // 사용자 클릭·F 입력 안에서 바로 호출해야 전체 화면 요청이 허용된다.
    await request.call(overlay, { navigationUI: 'hide' });
    if(!isLightboxFullscreen(overlay) || overlay.style.display === 'none'){
      await exitLightboxFullscreen(overlay); // 요청 중 닫힌 뷰어의 늦은 진입을 취소한다.
    }
  }catch(e){
    if(isLightboxFullscreen(overlay) && overlay.style.display !== 'none')
      alert('상태표시줄 숨김 요청을 브라우저가 허용하지 않았어요. 전체 화면을 끝낸 뒤 다시 시도해 주세요.');
  }finally{
    lightboxFullscreenPending.delete(overlay);
  }
}
function syncLightboxFullscreen(){
  const active = lightboxFullscreenElement();
  if(lightboxNativeFullscreen && active !== lightboxNativeFullscreen && !lightboxNativeFullscreen.contains(active)){
    paintLightboxFullscreen(lightboxNativeFullscreen, false);
    lightboxNativeFullscreen = null;
  }
  if(active && active.matches('.lightbox-overlay') && isLightboxFullscreen(active)){
    lightboxNativeFullscreen = active;
  }
}
document.addEventListener('fullscreenchange', syncLightboxFullscreen);
document.addEventListener('webkitfullscreenchange', syncLightboxFullscreen);
function toggleLightboxFullscreen(overlay){ setLightboxFullscreen(overlay, !isLightboxFullscreen(overlay)); }
// 보기 회전(NS-24) — 각도는 overlay의 data-rot(0/90/180/270)에만 둔다. 저장하지 않으므로 새로고침하면 0이고,
// 그 전까지는 넘김·닫았다 열기에도 남는다. CSS가 전체 화면일 때만 돌려 보여 준다(원본 파일은 그대로).
function rotateLightboxView(overlay, delta){
  overlay.dataset.rot = (((Number(overlay.dataset.rot) || 0) + delta) % 360 + 360) % 360;
}
document.querySelectorAll('.lightbox-rot-btn').forEach(btn => btn.addEventListener('click', () =>
  rotateLightboxView(btn.closest('.lightbox-overlay'), Number(btn.dataset.rotDelta))));
// 화면 기준 손가락 이동(dx, dy)을 돌려 보는 이미지 기준으로 바꾼다 — 기기를 돌려 들고 이미지의 좌우로 밀면 넘어가게.
function lightboxLocalDelta(overlay, dx, dy){
  const rad = (isLightboxFullscreen(overlay) ? Number(overlay.dataset.rot) || 0 : 0) * Math.PI / 180;
  const c = Math.round(Math.cos(rad)), s = Math.round(Math.sin(rad));
  return [dx * c + dy * s, -dx * s + dy * c];
}

const DRAG_DAMPING = 0.4;
const SWIPE_THRESHOLD = 50;
// 이미지·영상 라이트박스 공용 — overlay 안에서 한 손가락으로 좌우로 밀면 onNext/onPrev를 부른다.
// ignore(e)가 true를 돌려주는 터치(스크롤·조작 의도)는 무시한다.
function setupLightboxSwipe(overlay, mediaEl, { onNext, onPrev, ignore }){
  let startX = null, startY = null;
  const settle = () => { mediaEl.classList.remove('dragging'); mediaEl.style.transform = ''; };
  overlay.addEventListener('touchstart', (e) => {
    if(e.touches.length !== 1 || e.target.closest('.asset-meta') || (ignore && ignore(e))){ startX = startY = null; return; }
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    mediaEl.classList.add('dragging');
  }, { passive: true });
  overlay.addEventListener('touchmove', (e) => {
    if(startX === null || e.touches.length !== 1) return;
    const [dx, dy] = lightboxLocalDelta(overlay, e.touches[0].clientX - startX, e.touches[0].clientY - startY);
    if(Math.abs(dx) < Math.abs(dy)) return; // 세로 스크롤 의도로 보이면 따라가지 않음
    // transform은 CSS rotate보다 안쪽에 적용되므로 translateX가 돌려 본 이미지의 가로축을 따라간다.
    mediaEl.style.transform = `translateX(${dx * DRAG_DAMPING}px)`;
  }, { passive: true });
  overlay.addEventListener('touchend', (e) => {
    settle();
    if(startX === null) return;
    const touch = e.changedTouches[0];
    const [dx, dy] = lightboxLocalDelta(overlay, touch.clientX - startX, touch.clientY - startY);
    startX = startY = null;
    if(Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dx) < Math.abs(dy)) return;
    if(dx < 0) onNext(); else onPrev();
  }, { passive: true });
  overlay.addEventListener('touchcancel', () => { startX = startY = null; settle(); }, { passive: true });
}

// 모바일에서 화살표 버튼 대신 스와이프로도 이미지를 넘길 수 있게 — 왼쪽으로 밀면 다음, 오른쪽으로
// 밀면 이전. 세로로 더 많이 움직였으면(스크롤 의도로 보고) 무시하고, 임계값(50px)을 넘어야만
// 넘긴다. 손가락을 움직이는 동안 이미지가 그 방향으로 살짝 따라와서(damped) "지금 스와이프가
// 인식되고 있다"는 걸 바로 알 수 있고, 손을 떼면 트랜지션과 함께 원위치로 돌아온다.
setupLightboxSwipe(galleryLightbox, document.getElementById('gallery-lightbox-img'), {
  onNext: showNextImage, onPrev: showPrevImage,
});

document.getElementById('gallery-lightbox-close').addEventListener('click', closeLightbox);
document.getElementById('gallery-lightbox-fs-btn').addEventListener('click', () => toggleLightboxFullscreen(galleryLightbox));
document.getElementById('gallery-prev').addEventListener('click', showPrevImage);
document.getElementById('gallery-next').addEventListener('click', showNextImage);
document.getElementById('gallery-lightbox-download-btn').addEventListener('click', () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(img) triggerAnchorDownload(`/api/output-images/${encodeURIComponent(img.name)}`, img.name);
});
document.getElementById('gallery-lightbox-original-btn').addEventListener('click', () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  lightboxOriginalName = img.name;
  renderLightbox();
});
document.getElementById('gallery-lightbox-send-to-pose-btn').addEventListener('click', () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(img) openPoseImportModal([img.name]);
});
document.getElementById('gallery-lightbox-library-btn').addEventListener('click', () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(img) openLibraryFromGallery([img.name]);
});
document.getElementById('gallery-lightbox-load-job-btn').addEventListener('click', async () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  const errorEl = document.getElementById('gallery-lightbox-error');
  errorEl.textContent = '';
  if(!img.job_id){
    errorEl.textContent = '이 이미지는 작업 정보가 없어요 (작업 하위 폴더 없이 저장됨).';
    return;
  }
  const job = (await fetchAllJobsIncludingDeleted()).find(j => j.id === img.job_id);
  if(!job){
    errorEl.textContent = `이 작업(${img.job_id})은 더 이상 남아있지 않아요 (삭제 보관 기간이 지났을 수 있어요).`;
    return;
  }
  closeLightbox();
  // 그 작업을 돌렸던 파드로 들어간다(그 파드가 사라졌으면 resolvePodId가 기본 파드로
  // 떨어뜨린다) — 같은 설정을 다른 종류의 파드에서 열면 템플릿부터 맞지 않는다.
  showTab('jobs', { podId: job.pod_id });
  await loadJobSettings(job);
});
async function rotateCurrentLightboxImage(){
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  const errorEl = document.getElementById('gallery-lightbox-error');
  errorEl.textContent = '';
  const btn = document.getElementById('gallery-lightbox-rotate-btn');
  btn.disabled = true;
  try{
    const res = await fetch(`/api/output-images/${encodeURIComponent(img.name)}/rotate`, { method: 'POST' });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.detail || '회전에 실패했어요.';
      return;
    }
  }catch(e){
    errorEl.textContent = '회전에 실패했어요.';
    return;
  }finally{
    btn.disabled = false;
  }
  // fetchGalleryImages가 목록을 다시 받아오면 이 이미지의 mtime도 바뀐 값으로
  // 갱신되고, renderLightbox는 그 mtime을 URL 쿼리에 붙이므로(캐시 무효화)
  // 다시 그리면 자동으로 회전된 새 이미지를 받아온다. 목록은 mtime 내림차순이라
  // 방금 돈 이미지가 맨 앞으로 튈 수 있어, 같은 파일명을 다시 찾아 인덱스를
  // 맞춘다(못 찾으면 — 이론상 없지만 — 그대로 둔다).
  await fetchGalleryImages();
  const newIndex = displayedGalleryImages.findIndex(i => i.name === img.name);
  if(newIndex >= 0) lightboxIndex = newIndex;
  renderLightbox();
}
async function deleteCurrentLightboxImage(){
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  const indexBeforeDelete = lightboxIndex;
  const ok = await deleteGalleryImages([img.name]);
  if(!ok) return;
  // deleteGalleryImages()가 이미 목록을 다시 받아와 그려뒀다(fetchGalleryImages
  // → renderGalleryGrid) — 지운 이미지 뒤에 있던 게 같은 자리로 당겨오므로, 인덱스를
  // 그대로 두면 자연스럽게 "다음 이미지"가 보인다. 지운 게 마지막 장이었으면
  // 하나 앞(남은 것 중 마지막)으로, 아예 하나도 안 남았으면 라이트박스를 닫는다.
  if(displayedGalleryImages.length === 0){
    closeLightbox();
    return;
  }
  lightboxIndex = Math.min(indexBeforeDelete, displayedGalleryImages.length - 1);
  renderLightbox();
}
// 옮기고 나면 목록이 이미 다시 그려져 있다 — 프로젝트 범위 밖으로 나갔으면 그 자리로 다음 이미지가
// 당겨오고(삭제와 같다), 전체 갤러리라 그대로 남아 있으면 같은 이미지를 다시 찾아 계속 보여준다.
async function moveCurrentLightboxImage(){
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  const indexBefore = lightboxIndex;
  if(!await openAssetMoveModal('image', [img.name])) return;
  if(displayedGalleryImages.length === 0){ closeLightbox(); return; }
  const same = displayedGalleryImages.findIndex(i => i.name === img.name);
  lightboxIndex = same >= 0 ? same : Math.min(indexBefore, displayedGalleryImages.length - 1);
  renderLightbox();
}
document.getElementById('gallery-lightbox-move-btn').addEventListener('click', moveCurrentLightboxImage);

// 이 이미지를 시작 이미지로 WAN2.2 i2v 작업을 만들 준비를 한다 — 입력 이미지 풀에 사본을 두고 "새 작업"
// 폼을 템플릿(wan22_i2v_batch)과 시작 이미지가 채워진 채로 열어 준다. 큐에 넣는 것은 사용자가 영상
// 프롬프트를 적고 "큐에 추가"를 눌러서 한다. 이 파드에서 돌아갈 워크플로우인지는 폼이 워크플로우를
// 붙일 때 하는 호환성 검사(checkWorkflowCompatibility)가 알려 준다.
const I2V_TEMPLATE_ID = 'wan22_i2v_batch';
async function startI2vFromImage(img){
  const stored = await importLightboxImageForJob(img, document.getElementById('gallery-lightbox-i2v-btn'));
  if(!stored) return;
  const loadError = document.getElementById('load-error');
  const loadNotice = document.getElementById('load-notice');
  loadError.textContent = '';
  loadNotice.textContent = '';
  if(Object.keys(templatesById).length === 0) await fetchTemplates();
  openNewJobModal();
  if(!selectHasOptionValue(templateSelect, I2V_TEMPLATE_ID)){
    loadError.textContent = '이 워커에서는 영상 생성(WAN2.2 i2v) 템플릿을 쓸 수 없어요 — ComfyUI 워커를 골라 주세요.';
    return;
  }
  resetForm();
  templateSelect.value = I2V_TEMPLATE_ID;
  await onTemplateChange();
  const startEl = optionsFields.querySelector('[data-name="start_image"]');
  if(startEl) startEl.value = stored;
  loadNotice.textContent = `'${stored}'을(를) 시작 이미지로 골랐어요. 영상 프롬프트를 적고 "추가"를 누르세요.`;
  const promptEl = optionsFields.querySelector('[data-name="user_prompt"]');
  if(promptEl) promptEl.focus({ preventScroll: true });
}

// 라이트박스 이미지를 입력 이미지 풀에 사본으로 두고, 라이트박스를 닫고 그 이미지의 프로젝트 작업 화면으로
// 간다("영상 만들기"·"Face Detailer" 공통). 사본 파일명을 돌려주고, 실패하면 라이트박스에 안내하고 null.
async function importLightboxImageForJob(img, btn){
  const errorEl = document.getElementById('gallery-lightbox-error');
  errorEl.textContent = '';
  btn.disabled = true;
  let stored;
  try{
    const res = await fetch('/api/input-images/import-from-output', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: [img.name] }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok || !(data.added || []).length){
      throw new Error(data.detail || (data.skipped && data.skipped[0] && data.skipped[0].reason) || '이미지를 준비하지 못했어요.');
    }
    stored = data.added[0];
    await fetchInputImages(true);
  }catch(e){
    errorEl.textContent = e.message || '이미지를 준비하지 못했어요.';
    return null;
  }finally{
    btn.disabled = false;
  }

  closeLightbox();
  // 결과 영상이 이 이미지와 같은 프로젝트에 쌓이도록 그 프로젝트의 작업 화면으로 간다. 파드 갤러리에서
  // 보고 있었다면 그 파드에 그대로 머물고, 프로젝트만 폼의 선택값으로 맞춘다.
  const projectScope = img.project_id == null ? 'unassigned' : (resolveProjectId(img.project_id) ?? 'unassigned');
  if(currentPodId){
    showTab('jobs', { podId: currentPodId });
    try{ localStorage.setItem(FORM_PROJECT_KEY, projectScope === 'unassigned' ? '' : String(projectScope)); }catch(e){ /* 무시 */ }
    updateJobTargetRow();
  }else{
    showTab('jobs', { projectId: projectScope });
  }
  return stored;
}
document.getElementById('gallery-lightbox-i2v-btn').addEventListener('click', () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(img) startI2vFromImage(img);
});

// 이 이미지의 얼굴을 보정하는 Face Detailer 유형 작업을 준비한다 — 사본을 참조 이미지로 두고 마법사를
// Face Detailer 유형으로 연다(베이스 모델은 사용자가 1단계에서 고른다). 큐에 넣는 것은 사용자가 한다.
document.getElementById('gallery-lightbox-face-btn').addEventListener('click', async () => {
  const img = displayedGalleryImages[lightboxIndex];
  if(!img) return;
  const stored = await importLightboxImageForJob(img, document.getElementById('gallery-lightbox-face-btn'));
  if(stored) await startFaceDetailerWizard(stored);
});
document.getElementById('gallery-lightbox-rotate-btn').addEventListener('click', rotateCurrentLightboxImage);
document.getElementById('gallery-lightbox-delete-btn').addEventListener('click', deleteCurrentLightboxImage);
galleryLightbox.addEventListener('click', (e) => {
  if(e.target === galleryLightbox) closeLightbox();
});
document.addEventListener('keydown', (e) => {
  if(galleryLightbox.style.display === 'none' || assetMoveModalOpen()) return;
  // 태그/메모 입력 중의 Backspace·화살표가 삭제/이동으로 새면 안 된다.
  if(e.target.closest && e.target.closest('input, textarea, select')) return;
  if(e.key === 'Escape'){ if(isLightboxFullscreen(galleryLightbox)) setLightboxFullscreen(galleryLightbox, false); else closeLightbox(); }
  else if(e.key === 'f' || e.key === 'F') toggleLightboxFullscreen(galleryLightbox);
  else if(e.key === ']' && isLightboxFullscreen(galleryLightbox)) rotateLightboxView(galleryLightbox, 90);
  else if(e.key === '[' && isLightboxFullscreen(galleryLightbox)) rotateLightboxView(galleryLightbox, -90);
  else if(e.key === 'ArrowLeft') showPrevImage();
  else if(e.key === 'ArrowRight') showNextImage();
  else if(e.key === 'Delete' || e.key === 'Backspace') deleteCurrentLightboxImage();
});

