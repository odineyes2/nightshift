// ---- 참조 이미지(안 써도 되는 슬롯)/참조 비디오/참조 오디오 — 카드 하나 + 선택 모달 ----
// 갤러리를 항상 펼쳐두지 않는다 — 기본은 "+" 카드 하나뿐이고, 누르면 선택 모달
// (#ref-asset-picker-modal, 아래)이 열려 "선택 안 함"/이미 올린 자산/업로드 중 하나를
// 고르게 한다. 고르고 나면 다시 카드 하나(그 자산의 썸네일 + 지우기 배지)만 보인다.
// MiniMax-H3 r2v의 ref_image_1~9/ref_video_1~3/ref_audio_1~3, i2v의 first_frame_image/
// last_frame_image가 이 컨트롤을 쓴다.
function buildRefAssetCard(kind, value, rawUrl){
  const card = document.createElement('div');
  card.className = 'ref-card';
  card.title = value;
  if(kind === 'image'){
    const img = document.createElement('img');
    img.src = rawUrl(value);
    img.loading = 'lazy';
    card.appendChild(img);
  }else if(kind === 'video'){
    const video = document.createElement('video');
    video.src = rawUrl(value);
    video.muted = true;
    video.preload = 'metadata';
    // 서버에서 썸네일을 만들지 않는 대신, 메타데이터가 로드되면 살짝 재생 위치를 옮겨
    // 브라우저가 그 프레임을 그리게 한다(0초는 대부분 검은 화면만 보여줌).
    video.addEventListener('loadedmetadata', () => { try{ video.currentTime = 0.05; }catch(e){} });
    card.appendChild(video);
    const badge = document.createElement('span');
    badge.className = 'ref-card-badge';
    badge.innerHTML = ico('play');
    card.appendChild(badge);
  }else{
    card.classList.add('ref-card-audio');
    card.innerHTML = ico('music');
    const label = document.createElement('div');
    label.className = 'ref-card-audio-label';
    label.textContent = value;
    card.appendChild(label);
  }
  return card;
}

// 선택 모달은 하나를 공유해서 매번 kind/선택 콜백만 바꿔 다시 그린다.
let refAssetPickerCfg = null;
let refAssetPickerOnPick = null;

async function openRefAssetPicker(cfg, onPick){
  refAssetPickerCfg = cfg;
  refAssetPickerOnPick = onPick;
  document.getElementById('ref-asset-picker-title').textContent = cfg.pickerTitle;
  document.getElementById('ref-asset-picker-error').textContent = '';
  await cfg.fetchFn(false);
  renderRefAssetPickerGrid();
  document.getElementById('ref-asset-picker-modal').style.display = 'flex';
}

function closeRefAssetPicker(){
  document.getElementById('ref-asset-picker-modal').style.display = 'none';
  refAssetPickerCfg = null;
  refAssetPickerOnPick = null;
}

function pickRefAsset(value){
  const onPick = refAssetPickerOnPick;
  closeRefAssetPicker();
  if(onPick) onPick(value);
}

function renderRefAssetPickerGrid(){
  const cfg = refAssetPickerCfg;
  const grid = document.getElementById('ref-asset-picker-grid');
  grid.innerHTML = '';

  const noneCard = document.createElement('div');
  noneCard.className = 'ref-card ref-card-none';
  noneCard.textContent = '선택 안 함';
  noneCard.title = '선택 안 함';
  noneCard.addEventListener('click', () => pickRefAsset(''));
  grid.appendChild(noneCard);

  for(const choice of cfg.choicesFn()){
    const card = buildRefAssetCard(cfg.kind, choice.value, cfg.rawUrl);
    card.addEventListener('click', () => pickRefAsset(choice.value));
    grid.appendChild(card);
  }

  const uploadCard = document.createElement('div');
  uploadCard.className = 'ref-card ref-card-upload';
  uploadCard.innerHTML = ico('upload');
  uploadCard.title = cfg.uploadTitle;
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = cfg.accept;
  fileInput.style.display = 'none';
  uploadCard.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    fileInput.value = '';
    if(!file) return;
    const original = uploadCard.innerHTML;
    uploadCard.innerHTML = ico('loader-circle', true);
    try{
      const form = new FormData();
      form.append(cfg.uploadField, file);
      const res = await fetch(cfg.uploadUrl, { method: 'POST', body: form });
      const data = await res.json().catch(() => ({}));
      if(!res.ok) throw new Error(data.detail || '업로드에 실패했어요.');
      await cfg.fetchFn(true);
      pickRefAsset(data.name);
    }catch(e){
      document.getElementById('ref-asset-picker-error').textContent = e.message || '업로드에 실패했어요.';
      uploadCard.innerHTML = original;
    }
  });
  grid.appendChild(uploadCard);
  grid.appendChild(fileInput);
}

document.getElementById('ref-asset-picker-close').addEventListener('click', closeRefAssetPicker);
document.getElementById('ref-asset-picker-modal').addEventListener('click', (e) => {
  if(e.target.id === 'ref-asset-picker-modal') closeRefAssetPicker();
});

function buildAssetGalleryControl(opt, cfg){
  const wrap = document.createElement('div');
  wrap.className = 'ref-gallery-single';

  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.className = 'option-input';
  hidden.dataset.name = opt.name;
  hidden.value = opt.default || '';

  const openPicker = () => openRefAssetPicker(cfg, (value) => {
    hidden.value = value;
    renderSlot();
  });

  function renderSlot(){
    wrap.querySelectorAll('.ref-card').forEach(el => el.remove());
    if(hidden.value){
      const card = buildRefAssetCard(cfg.kind, hidden.value, cfg.rawUrl);
      card.classList.add('selected');
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'ref-card-remove';
      removeBtn.innerHTML = '&times;';
      removeBtn.title = '선택 해제';
      removeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        hidden.value = '';
        renderSlot();
      });
      card.appendChild(removeBtn);
      card.addEventListener('click', (e) => { if(e.target !== removeBtn) openPicker(); });
      wrap.insertBefore(card, hidden);
    }else{
      const addCard = document.createElement('div');
      addCard.className = 'ref-card ref-card-add';
      addCard.innerHTML = ico('plus');
      addCard.title = cfg.uploadTitle;
      addCard.addEventListener('click', openPicker);
      wrap.insertBefore(addCard, hidden);
    }
  }

  wrap.appendChild(hidden);
  renderSlot();
  return wrap;
}

function buildInputImageOptionalControl(opt){
  return buildAssetGalleryControl(opt, {
    kind: 'image', choicesFn: inputImageChoices, fetchFn: fetchInputImages,
    uploadUrl: '/api/input-images', uploadField: 'image', accept: '.png,.jpg,.jpeg,.webp',
    uploadTitle: '내 컴퓨터에서 이미지 업로드', pickerTitle: '참조 이미지 선택',
    rawUrl: (name) => `/api/input-images/${encodeURIComponent(name)}/raw`,
  });
}

let inputVideosCache = null;
async function fetchInputVideos(force){
  if(inputVideosCache && !force) return inputVideosCache;
  try{
    const res = await fetch('/api/input-videos');
    inputVideosCache = res.ok ? (await res.json()).videos || [] : [];
  }catch(e){ inputVideosCache = []; }
  return inputVideosCache;
}
function inputVideoChoices(){
  return (inputVideosCache || []).map(v => ({ value: v.name, label: v.name, disabled: false }));
}
function buildInputVideoControl(opt){
  return buildAssetGalleryControl(opt, {
    kind: 'video', choicesFn: inputVideoChoices, fetchFn: fetchInputVideos,
    uploadUrl: '/api/input-videos', uploadField: 'video', accept: '.mp4,.webm,.mov,.mkv',
    uploadTitle: '내 컴퓨터에서 비디오 업로드', pickerTitle: '참조 비디오 선택',
    rawUrl: (name) => `/api/input-videos/${encodeURIComponent(name)}/raw`,
  });
}

let inputAudiosCache = null;
async function fetchInputAudios(force){
  if(inputAudiosCache && !force) return inputAudiosCache;
  try{
    const res = await fetch('/api/input-audios');
    inputAudiosCache = res.ok ? (await res.json()).audios || [] : [];
  }catch(e){ inputAudiosCache = []; }
  return inputAudiosCache;
}
function inputAudioChoices(){
  return (inputAudiosCache || []).map(a => ({ value: a.name, label: a.name, disabled: false }));
}
function buildInputAudioControl(opt){
  return buildAssetGalleryControl(opt, {
    kind: 'audio', choicesFn: inputAudioChoices, fetchFn: fetchInputAudios,
    uploadUrl: '/api/input-audios', uploadField: 'audio', accept: '.mp3,.wav,.flac,.m4a,.ogg,.aac',
    uploadTitle: '내 컴퓨터에서 오디오 업로드', pickerTitle: '참조 오디오 선택',
  });
}

// ---- 입력 이미지 — 갤러리에서 선택 모달 ----
// 결과 이미지 갤러리(output-images)의 썸네일을 격자로 보여주고, 하나를 클릭하면
// onPick(name)을 불러 실제 가져오기(복사)를 맡긴다 — 호출부(업로드 vs 다른 용도)에
// 따라 가져온 뒤 할 일이 다를 수 있어 이 모달 자체는 "고르기"에만 집중한다.
let inputImageGalleryOnPick = null;

async function openInputImageGalleryPicker(onPick){
  inputImageGalleryOnPick = onPick;
  const modal = document.getElementById('input-image-picker-modal');
  const grid = document.getElementById('input-image-picker-grid');
  const empty = document.getElementById('input-image-picker-empty');
  const errorEl = document.getElementById('input-image-picker-error');
  errorEl.textContent = '';
  grid.innerHTML = '<div class="empty">불러오는 중…</div>';
  modal.style.display = 'flex';
  try{
    const res = await fetch('/api/output-images');
    const data = await res.json();
    const images = data.images || [];
    if(images.length === 0){
      grid.innerHTML = '';
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    grid.innerHTML = images.map(img => `
      <div class="gallery-item" data-name="${escapeHtml(img.name)}" title="${escapeHtml(img.name)}">
        <img src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(img.name)}/thumbnail?size=200`)}" alt="${escapeHtml(img.name)}" loading="lazy">
        <div class="gallery-item-name">${escapeHtml(img.name)}</div>
      </div>
    `).join('');
    grid.querySelectorAll('.gallery-item').forEach(el => {
      el.addEventListener('click', async () => {
        errorEl.textContent = '';
        try{
          await inputImageGalleryOnPick(el.dataset.name);
          closeInputImageGalleryPicker();
        }catch(e){
          errorEl.textContent = e.message || '가져오지 못했어요.';
        }
      });
    });
  }catch(e){
    grid.innerHTML = '';
    errorEl.textContent = '이미지 목록을 가져오지 못했어요.';
  }
}

function closeInputImageGalleryPicker(){
  document.getElementById('input-image-picker-modal').style.display = 'none';
  inputImageGalleryOnPick = null;
}
document.getElementById('input-image-picker-close').addEventListener('click', closeInputImageGalleryPicker);
document.getElementById('input-image-picker-modal').addEventListener('click', (e) => {
  if(e.target.id === 'input-image-picker-modal') closeInputImageGalleryPicker();
});

function buildOptionControl(opt){
  if(opt.name === 'main_prompt' && opt.type === 'textarea'){
    return buildPromptEnhanceControl(opt);
  }
  if(opt.type === 'comfy_model'){
    return buildComfyModelControl(opt);
  }
  if(opt.type === 'input_image'){
    return buildInputImageControl(opt);
  }
  if(opt.type === 'input_image_optional'){
    return buildInputImageOptionalControl(opt);
  }
  if(opt.type === 'input_video'){
    return buildInputVideoControl(opt);
  }
  if(opt.type === 'input_audio'){
    return buildInputAudioControl(opt);
  }
  if(opt.type === 'select' || opt.type === 'asset_folder' || opt.type === 'char_no'){
    return buildSelectControl(opt);
  }
  if(opt.type === 'textarea'){
    const textarea = document.createElement('textarea');
    textarea.className = 'option-input';
    textarea.dataset.name = opt.name;
    textarea.rows = 4;
    if(opt.placeholder) textarea.placeholder = opt.placeholder;
    if(opt.default !== undefined && opt.default !== null) textarea.value = opt.default;
    return textarea;
  }
  const input = document.createElement('input');
  input.className = 'option-input';
  input.type = (opt.type === 'number' || opt.type === 'number_optional') ? 'number' : 'text';
  input.dataset.name = opt.name;
  if(opt.placeholder) input.placeholder = opt.placeholder;
  if(opt.default !== undefined && opt.default !== null) input.value = opt.default;
  return input;
}

// 마법사가 minimax_h3_r2v/i2v 템플릿을 적용한 그래프에 실제로 있는 참조 슬롯 개수 —
// wizardApply()가 채우고, 수동으로 템플릿을 고르면(직접 템플릿 선택) null로 비운다.
// {image, video, audio}(r2v) 또는 {useFirstFrame, useLastFrame}(i2v) 형태.
let wizardRefCounts = null;

function isWizardRefSlotHidden(optionName){
  if(!wizardRefCounts) return false;
  let m = optionName.match(/^ref_image_(\d+)$/);
  if(m) return Number(m[1]) > wizardRefCounts.image;
  m = optionName.match(/^ref_video_(\d+)$/);
  if(m) return Number(m[1]) > wizardRefCounts.video;
  m = optionName.match(/^ref_audio_(\d+)$/);
  if(m) return Number(m[1]) > wizardRefCounts.audio;
  if(optionName === 'first_frame_image') return !wizardRefCounts.useFirstFrame;
  if(optionName === 'last_frame_image') return !wizardRefCounts.useLastFrame;
  return false;
}

function renderOptionFields(template){
  optionsFields.innerHTML = '';
  // 템플릿을 바꿀 때마다 이전 템플릿이 등록해둔 "제출 직전 훅"(시드별 Danbooru
  // 프롬프트 재계산 등)을 비운다 — 안 그러면 이미 사라진 DOM을 참조하는 낡은
  // 훅이 다음 제출 때도 계속 불려서 에러가 나거나 아무 의미 없는 일을 한다.
  danbooruPreSubmitHooks.length = 0;
  const options = template ? template.options || [] : [];
  // width/height가 둘 다 있는 템플릿(seed_batch/pose_batch)은 두 옵션을 따로
  // 그리지 않고 "이미지 크기" 필드 하나로 묶어서, 직접 입력/화면비·해상도 두
  // 방식을 오갈 수 있게 한다.
  const widthOpt = options.find(o => o.name === 'width');
  const heightOpt = options.find(o => o.name === 'height');
  const hasResolutionPair = !!(widthOpt && heightOpt);
  // 시드 개수/시드 방식은 값 하나하나가 짧아서(숫자 하나, 선택지 하나) 각각 한 줄을
  // 다 차지하면 어색하다 — 있으면 반씩 나눠 한 줄에 나란히 둔다(.field-pair, 모바일
  // 폭에서는 flex-wrap으로 자동으로 위아래로 쌓인다).
  const seedCountOpt = options.find(o => o.name === 'seed_count');
  const seedModeOpt = options.find(o => o.name === 'seed_mode');
  const hasSeedPair = !!(seedCountOpt && seedModeOpt);

  for(const opt of options){
    if(hasResolutionPair && opt.name === 'height') continue; // width 자리에서 함께 그려짐
    if(hasSeedPair && opt.name === 'seed_mode') continue; // seed_count 자리에서 함께 그려짐
    // danbooru_seed_prompts는 자체 필드/라벨 없이 buildPromptEnhanceControl이
    // main_prompt 옆에 숨겨진 입력으로 직접 넣는다(아래 참고).
    if(opt.name === 'danbooru_seed_prompts') continue;
    // 마법사가 이 템플릿을 방금 채웠다면(wizardRefCounts), 실제로 그래프에 없는 참조
    // 슬롯은 옵션 폼에 아예 안 그린다 — 수동으로 템플릿을 고른 경우엔 wizardRefCounts가
    // null이라 항상 전부 보인다(wizardApplyRefCountsFor/템플릿 select 리스너 참고).
    if(isWizardRefSlotHidden(opt.name)) continue;
    const field = document.createElement('div');
    field.className = 'field';

    if(hasResolutionPair && opt.name === 'width'){
      const label = document.createElement('label');
      label.className = 'field-label';
      label.textContent = '이미지 크기';
      field.appendChild(label);
      field.appendChild(buildResolutionControl(widthOpt, heightOpt));
      optionsFields.appendChild(field);
      continue;
    }

    if(hasSeedPair && opt.name === 'seed_count'){
      const pair = document.createElement('div');
      pair.className = 'field-pair';
      for(const pairOpt of [seedCountOpt, seedModeOpt]){
        const pairField = document.createElement('div');
        pairField.className = 'field';
        const pairLabel = document.createElement('label');
        pairLabel.className = 'field-label';
        pairLabel.textContent = pairOpt.label;
        pairField.appendChild(pairLabel);
        pairField.appendChild(buildOptionControl(pairOpt));
        pair.appendChild(pairField);
      }
      optionsFields.appendChild(pair);
      continue;
    }

    // 체크포인트/LoRA는 이제 마법사(1/3단계)로만 고른다 — 이 옵션들은 여전히 존재하고
    // wizardApply()가 값을 채워 넣지만(preset 조합일 때), 화면에는 안 보이게 감춘다(제출
    // 시에는 .option-input이라 그대로 같이 올라간다).
    const hideFromView = opt.name === 'checkpoint' || opt.name === 'lora_name' || opt.name === 'lora_strength';
    if(hideFromView) field.style.display = 'none';

    const label = document.createElement('label');
    label.className = 'field-label';
    label.textContent = opt.label;

    field.appendChild(label);
    field.appendChild(buildOptionControl(opt));
    optionsFields.appendChild(field);
  }

  // 캐스케이딩 드롭다운 — "인물 수"(char_no) 옵션을 바꾸면 그걸 스코프로 쓰는
  // asset_folder 옵션이, "secondary_kind" 같은 select를 바꾸면 그 kind_from을
  // 따르는 char_no/asset_folder 옵션이 그 자리에서 다시 채워져야 한다. 각
  // 옵션이 의존하는 "소스" 옵션 이름들(kind_from 대상, asset_folder라면
  // char_no_option 대상)을 모아 소스 -> 의존 옵션 목록의 역방향 맵을 만들고,
  // 소스 select에 change 리스너를 하나씩 붙인다. 의존 옵션 자신도 다른 옵션의
  // 소스일 수 있으므로(예: secondary_char_no가 secondary_set의 char_no_option
  // 대상) 다시 채운 뒤 그 select에도 change 이벤트를 전파해 체인이 이어지게
  // 한다 — 페이지를 다시 그리지 않으므로 다른 필드 값은 그대로 유지된다.
  const dependents = {};
  for(const opt of options){
    const sources = new Set();
    if(opt.kind_from) sources.add(opt.kind_from);
    if(opt.type === 'asset_folder') sources.add(opt.char_no_option || 'char_no');
    for(const src of sources){
      if(!dependents[src]) dependents[src] = [];
      dependents[src].push(opt.name);
    }
  }
  for(const [sourceName, depNames] of Object.entries(dependents)){
    const sourceSelect = optionsFields.querySelector(`[data-name="${sourceName}"]`);
    if(!sourceSelect) continue;
    sourceSelect.addEventListener('change', () => {
      for(const depName of depNames){
        const depOpt = options.find(o => o.name === depName);
        const depSelect = optionsFields.querySelector(`[data-name="${depName}"]`);
        if(!depOpt || !depSelect) continue;
        populateSelectOptions(depSelect, choicesFor(depOpt), emptyTextFor(depOpt));
        depSelect.dispatchEvent(new Event('change'));
      }
    });
  }
}

// 영상 생성(WAN2.2) 템플릿은 템플릿을 고르는 순간 nightshift가 함께 배포하는
// 고정 그래프(templates/video_workflows/*.json)를 GET /api/video-workflows/{name}로
// 받아 기본값으로 채워준다 — 다만 첨부 칸 자체는 계속 보여주고 그대로 두므로,
// 사용자가 원하면 다른 .json 워크플로우를 올려서 이 기본값을 덮어쓸 수 있다.
// i2v 계열 두 템플릿(시드/CSV)은 같은 그래프를, flf2v 계열 두 템플릿도 같은
// 그래프를 공유한다.
const WAN22_BUNDLED_WORKFLOWS = {
  wan22_i2v_batch: 'wan22_i2v',
  wan22_i2v_csv_batch: 'wan22_i2v',
  wan22_flf2v_batch: 'wan22_flf2v',
  wan22_flf2v_csv_batch: 'wan22_flf2v',
};

// 이미지→영상 복합 CSV 배치 템플릿(img2video_*)은 워크플로우 첨부 칸이 하나뿐이지만
// "두 개"처럼 보일 수 있다 — 여기 올리는 건 이미지 생성 단계 워크플로우뿐이고,
// 영상 단계(i2v/flf2v)는 스크립트 자신이 templates/video_workflows/*.json을 직접
// 읽어 쓰므로 화면에 올릴 필요가 없다. 이 첨부 칸의 라벨/안내문을 바꿔서 그 사실을
// 명확히 한다 — 값(오른쪽)은 사람이 읽을 영상 워크플로우 이름표일 뿐, 실제로 어떤
// 파일을 쓰는지는 스크립트 쪽 VIDEO_WORKFLOW_PATH가 결정한다.
const IMG2VIDEO_COMBO_TEMPLATES = {
  img2video_i2v_csv_batch: 'WAN2.2 i2v',
  img2video_flf2v_csv_batch: 'WAN2.2 flf2v',
};

async function onTemplateChange(){
  const template = templatesById[templateSelect.value];
  csvField.style.display = (template && template.requires_csv) ? '' : 'none';
  const csvEditorBtn = document.getElementById('csv-editor-btn');
  if(csvEditorBtn) csvEditorBtn.style.display = (template && template.requires_csv && (template.csv_columns || []).length) ? '' : 'none';
  // 워크플로우 자체가 필요 없는 템플릿(셸 명령 등)만 첨부 칸을 숨긴다. 고정
  // 그래프를 자동으로 붙이는 영상 생성 템플릿도 칸은 계속 보여준다 — 기본값을
  // 자동으로 채워줄 뿐, 사용자가 직접 다른 워크플로우로 덮어쓸 수 있어야 한다.
  const bundledWorkflowName = template && WAN22_BUNDLED_WORKFLOWS[template.id];
  const wfField = document.getElementById('workflow-field');
  if(wfField) wfField.style.display = (template && template.requires_workflow === false) ? 'none' : '';
  if(bundledWorkflowName){
    try{
      const res = await fetch(`/api/video-workflows/${bundledWorkflowName}`);
      if(!res.ok) throw new Error();
      const workflow = await res.json();
      setWorkflowFile(new File([JSON.stringify(workflow)], `${bundledWorkflowName}.json`, { type: 'application/json' }));
    }catch(e){
      setWorkflowFile(null);
      console.error('영상 워크플로우를 받아오지 못했어요:', e);
    }
  }

  const comboVideoLabel = template && IMG2VIDEO_COMBO_TEMPLATES[template.id];
  const wfLabelRow = document.getElementById('workflow-field-label-row');
  const wfLabel = document.getElementById('workflow-field-label');
  const wfHint = document.getElementById('workflow-field-hint');
  // 기본은 슬롯 안내문("…또는 워크플로우(.json) 직접 업로드")만 보인다 — img2video 복합
  // 템플릿이나 WAN2.2처럼 내장 기본값이 자동으로 채워지는 특수한 경우에만, 그
  // 사실을 알려주는 구체적인 안내(레이블+힌트)가 추가로 나온다.
  if(comboVideoLabel){
    wfLabelRow.style.display = '';
    wfLabel.innerHTML = '이미지 생성 워크플로우 (.json) <span class="opt">— 필수</span>';
    wfHint.textContent = `여기 올리는 건 이미지 생성 단계 워크플로우예요 — 영상 단계(${comboVideoLabel})는 바로 아래 "영상 생성 워크플로우" 칸에서 따로 올릴 수 있어요(선택, 비워두면 nightshift 내장 기본값을 씁니다).`;
    wfHint.style.display = '';
  }else if(bundledWorkflowName){
    wfLabelRow.style.display = '';
    wfLabel.innerHTML = '워크플로우 (.json) <span class="opt">— 필수</span>';
    wfHint.textContent = `기본값으로 nightshift 내장 워크플로우(${bundledWorkflowName}.json)가 자동으로 채워져 있어요 — 다른 .json 파일을 끌어다 놓거나 클릭해서 올리면 그걸로 바뀝니다.`;
    wfHint.style.display = '';
  }else{
    wfLabelRow.style.display = 'none';
    wfHint.style.display = 'none';
  }

  // img2video 복합 템플릿만 "영상 생성 워크플로우" 칸을 따로 보여준다(선택 —
  // 비워두면 템플릿 스크립트가 내장 기본값을 그대로 씀). 템플릿을 바꾸면 이전에
  // 골라둔 파일은 비운다 — 다른 템플릿엔 의미 없는 값이라 그대로 들고 있으면
  // 헷갈린다.
  const videoWfField = document.getElementById('video-workflow-field');
  const hasOptionalVideoWorkflow = !!(template && template.optional_video_workflow);
  if(videoWfField) videoWfField.style.display = hasOptionalVideoWorkflow ? '' : 'none';
  if(!hasOptionalVideoWorkflow) setVideoWorkflowFile(null);
  // char_no/asset_folder 옵션이 있는 템플릿을 고를 때마다 목록을 새로 받아와서, 방금
  // 서버에 새로 올려둔 포즈 세트도 페이지를 새로고침하지 않고 바로 반영되게 한다.
  const needsAssets = template && (template.options || []).some(o => o.type === 'asset_folder' || o.type === 'char_no');
  if(needsAssets) await fetchAssets();
  // comfy_model 옵션(체크포인트/LoRA)이 있으면 설치 목록을 먼저 받아둔다 — 그려진
  // 다음에 비동기로 채우면 "설정 불러오기"가 값을 넣는 시점과 엇갈릴 수 있어서,
  // 아예 그리기 전에 확보한다. 못 받아와도(ComfyUI 꺼짐 등) 폼은 그대로 그린다.
  const needsComfyModels = template && (template.options || []).some(o => o.type === 'comfy_model');
  if(needsComfyModels){
    try{ await fetchComfyObjectInfo(false); }catch(e){ /* 목록 없이 "그대로"만 고를 수 있게 둔다 */ }
  }
  const needsInputImages = template && (template.options || []).some(o => o.type === 'input_image' || o.type === 'input_image_optional');
  if(needsInputImages) await fetchInputImages(false);
  const needsInputVideos = template && (template.options || []).some(o => o.type === 'input_video');
  if(needsInputVideos) await fetchInputVideos(false);
  const needsInputAudios = template && (template.options || []).some(o => o.type === 'input_audio');
  if(needsInputAudios) await fetchInputAudios(false);
  renderOptionFields(template);
  uploadError.textContent = '';
  updateNewJobTabs();
}

// "초기화" 버튼 — 템플릿 선택은 그대로 두고, 첨부한 파일과 옵션 입력값만 그
// 템플릿의 기본값으로 되돌린다(onTemplateChange가 옵션 필드를 다시 그리므로
// 자연스럽게 기본값으로 리셋된다).
async function resetNewJobForm(){
  resetForm();
  await onTemplateChange();
  document.getElementById('load-error').textContent = '';
  document.getElementById('load-notice').textContent = '';
}

async function fetchTemplates(){
  const res = await fetch('/api/templates');
  // 페이지를 열 때 한 번만 부르는데, 401이면(키가 아직 없거나 틀림) 템플릿
  // 목록 없이 빈 채로 두고 넘어간다 — 키를 맞게 넣은 뒤 새로고침하면 다시 시도된다.
  if(!res.ok) return;
  allTemplates = await res.json();
  await renderTemplateOptions();
}

// 템플릿마다 어느 종류의 파드에서 쓸 수 있는지가 정해져 있다(셸 명령은 셸 파드 전용,
// 나머지는 ComfyUI 파드용). 고른 파드에서 못 쓰는 템플릿을 목록에 남겨두면 "추가"를
// 눌러야 400을 보게 되므로, 아예 목록에서 뺀다.
function templateAllowedOnPod(template, podKind){
  if(podKind === 'auto') return true;   // 파드를 안 정했으면 어느 종류든 구상할 수 있다(배정 때 종류가 맞는 파드를 찾는다)
  const kinds = template.pod_kinds;
  if(kinds && kinds.length) return kinds.includes(podKind);
  return podKind === 'comfyui';
}

async function renderTemplateOptions(){
  const podKind = formAutoMode() ? 'auto' : ((podsById[formPodId()] || {}).kind
    || (podsCache.find(p => p.enabled) || {}).kind || 'comfyui');
  // "워크플로우 마법사"(베이스 모델/워크플로우 유형/LoRA/실행 방식)와 "결과 이미지
  // 관리"/"결과 이미지 이메일 전송"(둘 다 OUTPUT_DIR의 이미지 파일을 다룸)은 전부
  // ComfyUI 개념이다 — 다른 종류의 파드(셸 명령, Claude 글쓰기)에서는 아무 의미가
  // 없으니 숨긴다. 그 위 템플릿 select + options-fields(그 템플릿의 옵션만 자동으로
  // 그려주는 부분)는 파드 종류와 무관하게 이미 쓸 수 있으므로 그대로 둔다.
  const comfyOnlyDisplay = (podKind === 'comfyui' || podKind === 'auto') ? '' : 'none';
  const wizardBar = document.getElementById('wizard-bar');
  if(wizardBar) wizardBar.style.display = comfyOnlyDisplay;
  const prev = templateSelect.value;
  templatesById = {};
  templateSelect.innerHTML = '';
  for(const t of allTemplates){
    templatesById[t.id] = t;
    if(!templateAllowedOnPod(t, podKind)) continue;
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.label;
    templateSelect.appendChild(opt);
  }
  if(prev && selectHasOptionValue(templateSelect, prev)) templateSelect.value = prev;
  await onTemplateChange();
}

// select 요소가 (disabled 여부와 무관하게) 해당 value의 <option>을 가지고 있는지 —
// 매니페스트/포즈 폴더가 그 작업을 돌린 뒤 바뀌어서 저장된 값이 더 이상 유효하지
// 않은 경우를 가려내는 데 쓴다.
function selectHasOptionValue(select, value){
  if(value === undefined || value === null) return false;
  const target = String(value);
  return Array.from(select.options).some(o => o.value === target);
}

// 작업 목록의 "설정 불러오기" 버튼 — 그 작업의 템플릿/옵션/워크플로우/CSV로 "새 작업
// 추가" 폼을 채운다. 여기서는 아무것도 큐에 등록하지 않는다(등록은 여전히 기존
// "큐에 추가" 버튼 → POST /api/upload 경로로만 이뤄짐). 매니페스트나 포즈 폴더가
// 그 작업을 돌린 뒤 바뀌었을 수 있으므로, 저장된 값이 더 이상 유효하지 않은 경우는
// 조용히 무시하지 않고 사용자에게 안내한다.
// prepared를 주면(보드 생성 카드의 "자세히") 워크플로우를 등록된 작업에서 받지 않고 그 안의 것을 쓴다.
async function loadJobSettings(job, prepared){
  const loadError = document.getElementById('load-error');
  const loadNotice = document.getElementById('load-notice');
  loadError.textContent = '';
  loadNotice.textContent = '';

  if(Object.keys(templatesById).length === 0) await fetchTemplates();
  const template = templatesById[job.template_id];
  if(!template){
    loadError.textContent = '이 템플릿은 더 이상 등록되어 있지 않아요.';
    return;
  }

  const warnings = [];

  resetForm();
  templateSelect.value = job.template_id;
  await onTemplateChange();

  const savedOptions = job.options || {};
  for(const opt of (template.options || [])){
    const hasSaved = Object.prototype.hasOwnProperty.call(savedOptions, opt.name);
    if(!hasSaved) continue; // 옵션이 새로 추가됨 — 이미 렌더링된 default를 그대로 둔다
    const el = optionsFields.querySelector(`[data-name="${opt.name}"]`);
    if(!el) continue;
    const savedValue = savedOptions[opt.name];

    if(opt.name === 'main_prompt' && opt.type === 'textarea'){
      // main_prompt는 hidden input(el) 하나가 아니라 "직접 입력"/"Prompt Enhance
      // 결과" 두 텍스트 에어리어로 이뤄져 있다. 저장된 값이 원래 개선된 결과였는지
      // 직접 입력이었는지는 구분할 수 없으므로, 일단 "직접 입력" 쪽에 그대로
      // 채워두고(다시 Prompt Enhance를 눌러 개선할 수 있음) 결과 칸은 비운다.
      const raw = optionsFields.querySelector('.enhance-raw');
      const enhanced = optionsFields.querySelector('.enhance-result');
      if(raw) raw.value = savedValue;
      if(enhanced) enhanced.value = '';
      if(raw) raw.dispatchEvent(new Event('input'));
      continue;
    }

    if(opt.type === 'char_no' || opt.type === 'asset_folder'){
      if(!selectHasOptionValue(el, savedValue)){
        warnings.push(`'${opt.label}' 값 '${savedValue}'을 더 이상 사용할 수 없어 비워뒀어요.`);
        continue;
      }
      el.value = String(savedValue);
      // char_no를 먼저 채우고 그 값으로 포즈 세트 목록을 다시 채운 뒤에 포즈 세트를
      // 채워야 하므로(캐스케이딩), char_no 필드에서만 change를 쏴서 asset_folder
      // 필드의 선택지를 그 자리에서 갱신한다. template.options 순서상 char_no가
      // asset_folder보다 앞에 선언돼 있어야 하고, 실제로 매니페스트가 그렇게 되어 있다.
      if(opt.type === 'char_no') el.dispatchEvent(new Event('change'));
      continue;
    }

    if(opt.type === 'select'){
      if(!(opt.choices || []).includes(savedValue)){
        warnings.push(`'${opt.label}' 값이 더 이상 유효하지 않아 기본값으로 되돌렸어요.`);
        continue;
      }
      el.value = savedValue;
      continue;
    }

    el.value = savedValue;
  }

  const wfRes = prepared ? null : await fetch(`/api/jobs/${job.id}/workflow`).catch(() => null);
  if(prepared){
    if(prepared.workflow) setWorkflowFile(new File([JSON.stringify(prepared.workflow)], 'board_gen_workflow.json', { type: 'application/json' }));
    if(template.optional_video_workflow && prepared.video_workflow){
      setVideoWorkflowFile(new File([JSON.stringify(prepared.video_workflow)], 'board_gen_video_workflow.json', { type: 'application/json' }));
    }
  }else if(wfRes && wfRes.ok){
    const text = await wfRes.text();
    const filename = job.workflow_original_name || 'workflow.json';
    setWorkflowFile(new File([text], filename, { type: 'application/json' }));
  } else {
    warnings.push('워크플로우 파일을 찾을 수 없어 직접 첨부해야 해요.');
  }

  // 영상 생성 워크플로우는 애초에 선택이라(안 올렸으면 내장 기본값을 씀) 그
  // 작업이 실제로 올렸을 때만 불러온다 — video_workflow_filename이 없으면
  // "내장 기본값을 썼다"는 뜻이라 그냥 건너뛴다(경고 아님).
  if(!prepared && template.optional_video_workflow && job.video_workflow_filename){
    const videoWfRes = await fetch(`/api/jobs/${job.id}/video-workflow`).catch(() => null);
    if(videoWfRes && videoWfRes.ok){
      const text = await videoWfRes.text();
      const filename = job.video_workflow_original_name || 'video_workflow.json';
      setVideoWorkflowFile(new File([text], filename, { type: 'application/json' }));
    } else {
      warnings.push('영상 생성 워크플로우 파일을 찾을 수 없어 직접 첨부해야 해요.');
    }
  }

  if(!prepared && template.requires_csv){
    const csvRes = await fetch(`/api/jobs/${job.id}/csv`).catch(() => null);
    if(csvRes && csvRes.ok){
      const text = await csvRes.text();
      const filename = job.csv_original_name || 'data.csv';
      setCsvFile(new File([text], filename, { type: 'text/csv' }));
    } else {
      warnings.push('CSV 파일을 찾을 수 없어 직접 첨부해야 해요.');
    }
  }

  openNewJobModal();

  let msg = prepared ? `보드 생성 카드 '${prepared.label}'의 설정을 불러왔어요.` : `'${job.id}' 작업의 설정을 불러왔어요.`;
  if(warnings.length) msg += '\n' + warnings.join('\n');
  loadNotice.textContent = msg;
  loadNotice.style.color = warnings.length ? 'var(--running)' : 'var(--done)';
}

templateSelect.addEventListener('change', () => {
  // 사람이 직접(수동으로) 템플릿을 바꾸면 마법사가 채운 슬롯 개수 정보는 더 이상
  // 유효하지 않다 — 옵션 폼은 그 템플릿의 슬롯을 전부 보여줘야 한다.
  wizardRefCounts = null;
  onTemplateChange();
});

document.getElementById('submit-btn').addEventListener('click', async () => {
  uploadError.textContent = '';
  const templateId = templateSelect.value;
  const template = templatesById[templateId];

  const needsWorkflow = !(template && template.requires_workflow === false);
  if(needsWorkflow && !selectedFiles.workflow){
    uploadError.textContent = '워크플로우(.json)는 필수예요.';
    return;
  }
  if(template && template.requires_csv && !selectedFiles.csv){
    uploadError.textContent = '이 템플릿은 csv 파일이 필요해요.';
    return;
  }
  for(const opt of (template ? template.options || [] : [])){
    if(opt.type !== 'asset_folder' && opt.type !== 'char_no') continue;
    const select = optionsFields.querySelector(`[data-name="${opt.name}"]`);
    if(!select || !select.value){
      const hint = opt.type === 'char_no'
        ? '(사용 가능한 인물 수 폴더가 없으면 서버에 먼저 포즈 세트 폴더를 만들어두세요)'
        : '(사용 가능한 포즈 세트가 없으면 서버에 먼저 이미지를 올려두세요)';
      uploadError.textContent = `'${opt.label}'을 선택하세요 ${hint}.`;
      return;
    }
  }

  // 폼 값을 모으기 직전에 마지막으로 한 번 더 갱신 — "시드마다 다른 Danbooru
  // 프롬프트" 체크박스가 켜져 있으면 지금 시드 개수/Danbooru 상태로 다시 계산한다.
  danbooruPreSubmitHooks.forEach(fn => fn());

  // 마법사로 만든 작업이면, 고른 체크포인트/LoRA 중 켜진 파드 어디에도 없는 게 있는지 먼저 알려 준다.
  // 막지는 않는다 — 대기 칸의 "없는 모델 받기"로 나중에 받을 수 있다. 켜진 파드가 없으면 판단을 안 한다.
  if(lastWizardModels){
    await fetchModelInventory(false);
    const wanted = [['checkpoints', lastWizardModels.checkpoint], ...lastWizardModels.loras.map(n => ['loras', n])];
    const absent = wanted.filter(([kind, name]) => { const pods = podsWithModel(kind, name); return pods !== null && pods.length === 0; })
                         .map(([, name]) => name);
    if(absent.length && !confirm(`켜진 워커 어디에도 없는 모델이 있어요:\n- ${absent.join('\n- ')}\n\n그래도 추가할까요? 이 작업은 대기 칸에서 주황색 "조치 필요"로 보이고, 카드의 "없는 모델 받기"로 받을 수 있어요.`)) return;
  }

  const form = new FormData();
  form.append('template_id', templateId);
  // "추가"는 항상 일시정지(pending) 상태로 대기 칸에 넣는다 — 자동으로 곧장
  // 돌기 시작하지 않고, 카드의 ▶ 시작을 직접 눌러야 돈다(서버의 start_paused
  // 처리 참고, POST /api/jobs로 프로그램이 만드는 작업은 이 값을 안 보내므로 영향 없음).
  form.append('start_paused', '1');
  // 새 작업은 지금 열려 있는 파드로 간다 — 파드 스코프 바가 그 파드를 말해준다.
  // (비어 있으면 서버가 기본 파드로 보낸다.)
  const podId = formPodId();
  if(podId) form.append('pod_id', podId);
  const projectId = formProjectId();
  if(projectId !== null) form.append('project_id', String(projectId));
  if(needsWorkflow) form.append('workflow', selectedFiles.workflow);
  if(template && template.optional_video_workflow && selectedFiles.video_workflow){
    form.append('video_workflow', selectedFiles.video_workflow);
  }
  const loraField = document.getElementById('lora-trigger-field');
  const loraTrigger = loraField && loraField.style.display !== 'none'
    ? document.getElementById('lora-trigger-input').value.trim() : '';
  if(template && template.requires_csv){
    form.append('csv', loraTrigger ? await csvWithLoraTrigger(selectedFiles.csv, template, loraTrigger) : selectedFiles.csv);
  }
  optionsFields.querySelectorAll('.option-input').forEach(input => {
    let value = input.value;
    if(loraTrigger && input.dataset.name === 'main_prompt') value = prependLoraTrigger(value, loraTrigger);
    if(loraTrigger && input.dataset.name === 'danbooru_seed_prompts' && value.trim()){
      // 시드마다 다른 프롬프트(JSON 배열)는 MAIN_PROMPT를 대신하므로 각각에도 붙인다.
      try{ value = JSON.stringify(JSON.parse(value).map(p => typeof p === 'string' && p.trim() ? prependLoraTrigger(p, loraTrigger) : p)); }catch(e){}
    }
    form.append(input.dataset.name, value);
  });

  const res = await fetch('/api/upload', { method: 'POST', body: form });
  if(!res.ok){
    const data = await res.json().catch(() => ({}));
    uploadError.textContent = data.detail || '업로드에 실패했어요.';
    return;
  }
  // 워크플로우/CSV/옵션 값은 일부러 그대로 둔다 — 같은 워크플로우로 옵션만 바꿔가며
  // 여러 개를 잇달아 추가하는 경우가 많아서, 다시 열었을 때(+ 카드) 매번 파일을
  // 다시 첨부하지 않아도 되게 하기 위함이다. 필요할 때는 "↺ 초기화" 버튼으로
  // 직접 비운다.
  const createdJob = await res.json().catch(() => null);
  closeNewJobModal();
  if(createdJob && preflightCount(createdJob.preflight) > 0){
    flashNotice('대기 칸에 추가했어요 — 이 워커에 없는 모델이 있어요: ' + preflightLines(createdJob.preflight).slice(0, 3).join(' / '));
  }else if(createdJob){
    flashNotice('대기 칸에 추가했어요 — 카드의 ▶ 시작을 누르면 돌아가요.');
  }
  fetchJobs();
});

document.getElementById('reset-form-btn').addEventListener('click', () => {
  resetNewJobForm();
});

