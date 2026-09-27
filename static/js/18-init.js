// ---- 초기 DOM 구성 ----
function danbooruCategoryCardHtml(cat){
  if(cat.kind === 'text'){
    return `
      <div class="danbooru-cat" data-key="${cat.key}">
        <div class="danbooru-cat-header">
          <div class="danbooru-cat-label">${escapeHtml(cat.label)}</div>
        </div>
        <textarea class="option-input danbooru-text-input" data-key="${cat.key}" rows="2" placeholder="${escapeHtml(cat.placeholder || '')}"></textarea>
      </div>
    `;
  }
  return `
    <div class="danbooru-cat" data-key="${cat.key}">
      <div class="danbooru-cat-header">
        <div class="danbooru-cat-label">${escapeHtml(cat.label)}</div>
        <div class="danbooru-cat-actions">
          <button type="button" class="danbooru-icon-btn danbooru-lock-btn${cat.lockedByDefault ? ' active' : ''}" data-key="${cat.key}" title="고정 (랜덤 조합에서 제외)"><svg class="ico"><use href="#i-lock"/></svg></button>
          <button type="button" class="danbooru-icon-btn danbooru-reroll-btn" data-key="${cat.key}" title="이 항목만 다시 뽑기"><svg class="ico"><use href="#i-dices"/></svg></button>
          <button type="button" class="danbooru-icon-btn danbooru-edit-toggle-btn" data-key="${cat.key}" title="태그 풀 편집"><svg class="ico"><use href="#i-pencil"/></svg></button>
        </div>
      </div>
      <div class="danbooru-chips" data-key="${cat.key}"></div>
      <div class="danbooru-pool-editor" data-key="${cat.key}" hidden>
        ${cat.key === 'artist' ? '<div class="danbooru-pool-hint">"by artistname" 형식으로 추가하세요 (예: by wlop)</div>' : ''}
        ${cat.key === 'nsfw' ? '<div class="danbooru-pool-hint">원하는 danbooru 태그를 직접 추가하세요.</div>' : ''}
        ${cat.key === 'extra' ? '<div class="danbooru-pool-hint">자유롭게 태그나 문구를 추가하세요 — 여기 추가한 항목은 다음에도 다시 골라 쓸 수 있어요.</div>' : ''}
        <div class="danbooru-pool-chips" data-key="${cat.key}"></div>
        <div class="danbooru-pool-add-row">
          <input type="text" class="option-input danbooru-pool-add-input" data-key="${cat.key}" placeholder="쉼표로 구분해 여러 개 추가">
          <button type="button" class="load-btn danbooru-pool-add-btn" data-key="${cat.key}" title="추가"><svg class="ico"><use href="#i-plus"/></svg> 추가</button>
        </div>
      </div>
    </div>
  `;
}

function danbooruBuildCategoriesDOM(){
  const mainContainer = document.getElementById('danbooru-categories');
  mainContainer.innerHTML = DANBOORU_CATEGORIES.map(danbooruCategoryCardHtml).join('');

  document.querySelectorAll('.danbooru-lock-btn').forEach(btn => {
    btn.addEventListener('click', () => danbooruToggleLock(btn.dataset.key));
  });
  document.querySelectorAll('.danbooru-reroll-btn').forEach(btn => {
    btn.addEventListener('click', () => danbooruRerollCategory(btn.dataset.key));
  });
  document.querySelectorAll('.danbooru-edit-toggle-btn').forEach(btn => {
    btn.addEventListener('click', () => danbooruTogglePoolEditor(btn.dataset.key));
  });
  document.querySelectorAll('.danbooru-pool-add-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.querySelector(`.danbooru-pool-add-input[data-key="${btn.dataset.key}"]`);
      danbooruAddPoolTags(btn.dataset.key, input.value);
      input.value = '';
    });
  });
  document.querySelectorAll('.danbooru-pool-add-input').forEach(input => {
    input.addEventListener('keydown', (e) => {
      if(e.key === 'Enter'){
        e.preventDefault();
        danbooruAddPoolTags(input.dataset.key, input.value);
        input.value = '';
      }
    });
  });
  document.querySelectorAll('.danbooru-text-input').forEach(ta => {
    ta.addEventListener('input', () => {
      danbooruState.texts[ta.dataset.key] = ta.value;
      danbooruUpdateAll();
    });
  });
}

// nsfwMode를 바꾸고 Danbooru 탭 토글 UI도 함께 맞춘다 — 이 탭의 SFW/NSFW 토글뿐
// 아니라 "작업 관리" 탭의 랜덤생성(SFW)/(NSFW), 시드마다 랜덤(SFW)/(NSFW) 버튼도
// 전부 이 함수로 같은 전역 상태를 바꿔서, 어디서 바꾸든 Danbooru 탭에 돌아왔을 때
// 마지막으로 쓴 모드가 그대로 반영돼 있다.
function danbooruSetNsfwMode(mode){
  danbooruState.nsfwMode = mode;
  document.querySelectorAll('#danbooru-nsfw-toggle .enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
}
document.querySelectorAll('#danbooru-nsfw-toggle .enhance-mode-btn').forEach(btn => {
  btn.addEventListener('click', () => danbooruSetNsfwMode(btn.dataset.mode));
});
document.getElementById('danbooru-randomize-btn').addEventListener('click', danbooruRandomizeAll);
document.getElementById('danbooru-history-save-btn').addEventListener('click', danbooruSaveHistory);

// 7번 요구사항 — 프롬프트 초기화: 지금까지 고른 태그(kind:"tags")와 입력한
// 텍스트(kind:"text")만 비운다. 고정(🔒) 상태와 태그 풀 편집(➕ 추가/✎)은 "지금
// 조립된 프롬프트"와는 별개의 설정이라 건드리지 않는다 — 초기화 후 바로 무작위를
// 눌러도 고정해둔 카테고리는 그대로 유지되길 기대할 것이기 때문.
function danbooruResetPrompt(){
  if(!confirm('지금까지 고른 태그와 입력한 텍스트를 모두 지울까요?')) return;
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind === 'tags'){
      danbooruState.selections[cat.key] = [];
    }else{
      danbooruState.texts[cat.key] = '';
      const input = document.querySelector(`.danbooru-text-input[data-key="${cat.key}"]`);
      if(input) input.value = '';
    }
  }
  danbooruUpdateAll();
}
document.getElementById('danbooru-reset-btn').addEventListener('click', danbooruResetPrompt);

// New Job 세부 설정의 "Danbooru로 프롬프트 만들기" 버튼이 부른다 — #tab-danbooru를
// #danbooru-modal-backdrop 안으로 옮겨서 New Job 모달 위에 띄운다(같은 z-index:70인데
// DOM에서 더 나중에 나오는 쪽이 위에 그려지는 앱 규칙 그대로 이용 — 새 z-index 안
// 씀). #tab-danbooru-anchor가 원래 자리를 기억해 뒀다가 닫을 때 그 자리로 되돌린다.
// 평소(직접 #danbooru로 들어왔을 때)엔 이 함수들을 아예 안 거친다.
function openDanbooruModal(){
  const tab = document.getElementById('tab-danbooru');
  const backdrop = document.getElementById('danbooru-modal-backdrop');
  backdrop.appendChild(tab);
  tab.classList.add('as-modal-panel');
  tab.style.display = '';
  backdrop.style.display = 'flex';
}
function closeDanbooruModal(){
  const tab = document.getElementById('tab-danbooru');
  const backdrop = document.getElementById('danbooru-modal-backdrop');
  document.getElementById('tab-danbooru-anchor').replaceWith(tab);
  // replaceWith가 앵커 자리를 tab으로 바꿔치기했으므로, 다음번을 위해 앵커를
  // tab 바로 뒤에 다시 심어 둔다.
  tab.insertAdjacentHTML('afterend', '<div id="tab-danbooru-anchor" style="display:none;"></div>');
  tab.classList.remove('as-modal-panel');
  tab.style.display = 'none';
  backdrop.style.display = 'none';
}
document.getElementById('danbooru-modal-close-btn').addEventListener('click', closeDanbooruModal);
document.getElementById('danbooru-modal-backdrop').addEventListener('click', (e) => {
  if(e.target === document.getElementById('danbooru-modal-backdrop')) closeDanbooruModal();
});

// 1장 — "작업 관리" 탭의 메인 프롬프트로 보내기. main_prompt 옵션이 있는
// 템플릿(시드 반복/포즈 참조 배치)이 선택돼 있을 때만 그 입력란(.enhance-raw)이
// 렌더링돼 있으므로, 없으면 어느 템플릿을 골라야 하는지 안내만 하고 아무것도
// 바꾸지 않는다.
function danbooruSendToMainPrompt(){
  const statusEl = document.getElementById('danbooru-send-status');
  const raw = document.querySelector('#options-fields .enhance-raw');
  if(!raw){
    statusEl.textContent = '현재 선택된 템플릿에는 메인 프롬프트 입력란이 없어요. "작업 관리" 탭에서 메인 프롬프트가 있는 템플릿(시드 반복/포즈 참조 배치)을 먼저 선택하세요.';
    statusEl.classList.remove('success');
    statusEl.classList.add('error');
    return;
  }
  raw.value = danbooruAssemblePrompt();
  raw.dispatchEvent(new Event('input', { bubbles: true }));
  statusEl.textContent = '메인 프롬프트로 보냈어요.';
  statusEl.classList.remove('error');
  statusEl.classList.add('success');
  // 모달로 띄운 상태였으면(New Job 세부 설정에서 열었으면) 여기서 닫는다 — 평소
  // 탭으로 보고 있었을 때는 이 클래스가 없으니 아무 일도 안 일어난다.
  if(document.getElementById('tab-danbooru').classList.contains('as-modal-panel')) closeDanbooruModal();
  // Danbooru는 파드 밖 화면이라 전역 Jobs(파드를 정하지 않고 구상하는 작업 화면)로 간다.
  showTab('jobs');
  raw.scrollIntoView({ behavior: 'smooth', block: 'center' });
  raw.focus();
}
document.getElementById('danbooru-send-btn').addEventListener('click', danbooruSendToMainPrompt);

// 반대 방향 — "작업 관리" 탭의 "직접 입력"/"Prompt Enhance 결과" 텍스트를 Danbooru
// 탭의 "자유 추가" 카테고리로 보낸다(createSendToDanbooruButton이 호출). 태그 풀
// 편집의 "➕ 추가"와 똑같이 쉼표로 나눠 여러 태그로 넣고(danbooruAddPoolTags가
// 풀에 추가 + 서버에 영구 저장까지 처리), 거기에 더해 그 태그들을 바로 선택 상태로
// 만들어서 "조립된 프롬프트"에 즉시 반영되게 한다. 이미 선택돼 있던 다른 자유
// 추가 태그는 그대로 두고 새 태그만 덧붙인다(비워두고 새로 시작하고 싶으면
// Danbooru 탭에서 직접 해제).
function danbooruSendTextToExtra(text){
  const trimmed = (text || '').trim();
  if(!trimmed) return false;
  danbooruAddPoolTags('extra', trimmed);
  const newTags = trimmed.split(',').map(t => t.trim()).filter(Boolean);
  danbooruState.selections.extra = danbooruDedupe([...(danbooruState.selections.extra || []), ...newTags]);
  danbooruUpdateAll();
  showTab('danbooru');
  document.getElementById('danbooru-prompt-text').scrollIntoView({ behavior: 'smooth', block: 'center' });
  return true;
}

async function initDanbooru(){
  try{
    const res = await fetch('/api/danbooru/tag-edits');
    danbooruState.tagEdits = res.ok ? await res.json() : {};
  }catch(e){
    danbooruState.tagEdits = {};
  }
  danbooruBuildCategoriesDOM();
  document.getElementById('danbooru-copy-slot').appendChild(createCopyButton(() => danbooruAssemblePrompt()));
  danbooruUpdateAll();
  await danbooruLoadHistory();
}

// 파드 목록 → 첫 화면 → 템플릿 목록 순서를 지킨다. 템플릿 목록은 "어느 파드에
// 들어와 있는가"에 따라 달라지므로(셸 파드면 셸 명령만), 파드와 주소가 정해지기
// 전에 먼저 받아버리면 잘못된 목록을 그린 뒤 다시 그리는 경합이 생긴다.
window.__nightshiftAuthReady.then(user => {
  if(!user) return;   // 로그인 화면이 떠 있다 — 로그인하면 화면을 새로 불러온다
  fetchPods().then(fetchProjects).then(applyHashRoute).then(fetchTemplates);
});
fetchJobs();
fetchComfyStatus();
fetchLoraTriggers();
fetchBaseModelFamilies();
// Job 카드의 베이스 모델 줄(jobBaseModelLabel)이 modelRegistry.items를 읽는데, 그건
// 원래 모델 탭을 열어야만(initModelsTab) 받아오던 것이라 — 모델 탭을 한 번도 안 열면
// 카드에 베이스 모델이 안 보인다. 시작할 때 미리 한 번 받아 두고, 받아온 뒤 작업
// 목록을 강제로 다시 그린다(시그니처를 지워서 — 안 그러면 그 사이 작업 필드가 하나도
// 안 바뀌었으면 다음 폴링에도 카드가 안 새로 그려진다).
fetchModelRegistry().then(() => { lastJobsRowsSignature = null; fetchJobs(); });
initDanbooru();
setInterval(fetchJobs, 2000);
setInterval(fetchPods, 15000);
setInterval(() => { if(currentProjectId !== null) fetchProjects(); }, 15000);
setInterval(fetchComfyStatus, 5000);
// 갤러리 탭이 보이는 동안에는 새 이미지가 생겨도 자동으로 반영되게 주기적으로 새로 받아온다.
// 라이트박스가 열려 있을 때는 건드리지 않는다 — 보고 있던 이미지가 목록 순서 변화로
// 갑자기 바뀌거나 닫혀버리면 안 되니까.
setInterval(() => {
  if(document.hidden || TAB_MAINS.gallery.style.display === 'none') return;
  if(galleryLightbox.style.display !== 'none') return;
  fetchGalleryImages();
}, 4000);
// 영상 갤러리도 같은 이유로 주기적으로 새로 받아온다 — 지금은 영상을 만드는 파드가
// 없어 늘 빈 목록이겠지만, 생기면 바로 자동 반영되게 미리 켜둔다.
setInterval(() => {
  if(document.hidden || TAB_MAINS['video-gallery'].style.display === 'none') return;
  if(videoGalleryLightbox.style.display !== 'none') return;
  fetchGalleryVideos();
}, 4000);

