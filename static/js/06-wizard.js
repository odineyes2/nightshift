// ---- 워크플로우를 직접 올리면 마법사를 거꾸로 채운다 ----
// 워크플로우 슬롯에 사람이 직접(드래그/클릭) 파일을 올렸을 때만 불린다(마법사가 만든 파일을
// 채우거나 "최근"/작업 불러오기가 채우는 프로그램적 호출에서는 안 불림 — setupSlot의 fromUser
// 참고). "이런 그래프를 올렸구나"를 최대한 읽어서 마법사 스텝에 반영해, 사용자가 그걸 보고
// LoRA 종류/강도 같은 세부값을 다시 조정할 수 있게 한다. 알아볼 수 없는 구조(체크포인트가
// CheckpointLoaderSimple이 아니거나, 등록부가 모르는 체크포인트)면 아무것도 건드리지 않는다 —
// 마법사가 모르는 워크플로우를 억지로 끼워 맞추면 오히려 헷갈린다.
async function populateWizardFromUploadedWorkflow(file){
  if(!file) return;
  let workflow;
  try{
    workflow = JSON.parse(await file.text());
  }catch(e){
    return; // JSON이 아니면(또는 못 읽으면) 손대지 않는다 — checkWorkflowCompatibility가 따로 안내함
  }
  await tryPopulateWizardFromWorkflow(workflow);
}

async function tryPopulateWizardFromWorkflow(workflow){
  if(!workflow || typeof workflow !== 'object') return;
  const nodes = Object.values(workflow).filter(n => n && typeof n === 'object' && n.class_type);

  const ckptNode = nodes.find(n => n.class_type === 'CheckpointLoaderSimple');
  const ckptName = ckptNode && ckptNode.inputs && ckptNode.inputs.ckpt_name;
  if(!ckptName) return; // CheckpointLoaderSimple 기반(SDXL식) 그래프가 아니면 마법사가 다루는 구조가 아니다

  if(!baseModelFamilies || Object.keys(baseModelFamilies).length === 0){
    await fetchBaseModelFamilies();
  }
  await fetchWorkflowTypes(); // 2단계 표시 라벨(Text to Image 등)에 필요 — 없으면 "txt2img" 그대로 보임(캐시돼 있으면 바로 반환)
  let matchFamilyId = null;
  for(const [fid, fam] of Object.entries(baseModelFamilies || {})){
    if((fam.checkpoints || []).includes(ckptName)){ matchFamilyId = fid; break; }
  }
  if(!matchFamilyId) return; // 등록부가 모르는 체크포인트 — 억지로 끼워 맞추지 않는다

  const hasEmptyLatent = nodes.some(n => n.class_type === 'EmptyLatentImage');
  const hasInputImage = nodes.some(n => n.class_type === 'LoadImage' && n._meta && n._meta.title === 'input_image');
  // KSampler 없이 입력 이미지가 FaceDetailer로만 가면 Face Detailer 유형(base="face_detailer")이다.
  const hasKSampler = nodes.some(n => n.class_type === 'KSampler');
  const base = hasEmptyLatent ? 'txt2img'
    : !hasInputImage ? null
    : (hasKSampler || !workflowHasFaceDetailer(workflow)) ? 'img2img' : 'face_detailer';
  if(!base) return; // 베이스 패스를 못 알아보면(워크플로우 빌더가 만드는 모양이 아니면) 멈춘다

  const loras = nodes
    .filter(n => n.class_type === 'LoraLoader')
    .map(n => ({
      name: (n.inputs || {}).lora_name,
      strength: typeof (n.inputs || {}).strength_model === 'number' ? n.inputs.strength_model : 1,
    }))
    .filter(l => l.name);
  const hiresNode = nodes.find(n => n.class_type === 'KSampler' && n._meta && n._meta.title === 'KSampler (hires-fix)');
  const upscaleNode = nodes.find(n => n.class_type === 'LatentUpscaleBy');
  const hasUsdu = nodes.some(n => n.class_type === 'UltimateSDUpscaleNoUpscale');
  const hasFaceDetailer = workflowHasFaceDetailer(workflow);

  wizard.familyId = matchFamilyId;
  wizard.checkpoint = ckptName;
  wizard.base = base;
  wizard.preset = null;
  wizard.post = freshWizardPost();
  if(hiresNode){
    wizard.post.hires_fix = true;
    if(upscaleNode && typeof (upscaleNode.inputs || {}).scale_by === 'number') wizard.post.hiresScale = upscaleNode.inputs.scale_by;
  }
  wizard.post.usdu = hasUsdu;
  // base가 face_detailer면 FaceDetailer는 베이스 자체라 후처리로 켜지 않는다.
  wizard.post.face_detailer = hasFaceDetailer && base !== 'face_detailer';
  wizard.loras = loras;
  // 실행 방식(시드 반복/CSV 순회)은 워크플로우만 봐서는 알 수 없다 — 건드리지 않고 4단계에서 직접 고르게 둔다.

  // "다음"을 눌렀을 때(goToNewJobDetails) wizardStale()이 방금 올린 이 파일을 마법사가
  // 새로 만든 걸로 덮어써 버리지 않도록, 지금 상태를 먼저 "이미 반영됨"으로 찍어 둔다 —
  // 이후 마법사 스텝을 더 만지면(예: 4단계 실행 방식을 고르면) 그때부터는 다시 stale이 된다.
  wizardAppliedSignature = JSON.stringify(wizard);
  wizardUpdateStepButtons();
  if(typeof flashNotice === 'function'){
    flashNotice('올린 워크플로우를 마법사 스텝에 반영했어요 — 확인하고 필요하면 조정하세요.');
  }
}

// ---- 1단계: 베이스 모델 ----
async function openWizardFamilyModal(){
  const body = document.getElementById('wizard-family-modal-body');
  body.innerHTML = '<div class="comfy-model-empty">불러오는 중…</div>';
  openWizardModal('wizard-family-modal');
  await fetchBaseModelFamilies();
  renderWizardFamilyModal();
}

function renderWizardFamilyModal(){
  const body = document.getElementById('wizard-family-modal-body');
  const entries = Object.entries(baseModelFamilies);
  if(entries.length === 0){
    body.innerHTML = '<div class="comfy-model-empty">고를 수 있는 베이스 모델이 없어요 — "모델" 탭에서 체크포인트의 베이스 모델을 적어 주세요(워커를 고른 경우엔 그 워커에 설치된 체크포인트만 나와요).</div>';
    return;
  }
  body.innerHTML = `<div class="wizard-pick-list">${entries.map(([familyId, family]) => {
    const checkpoints = family.checkpoints || [];
    const disabled = checkpoints.length === 0;
    const headerSelected = wizard.familyId === familyId && wizard.checkpoint === checkpoints[0];
    const header = `
      <div class="wizard-pick-row${disabled ? ' disabled' : ''}${headerSelected ? ' selected' : ''}" data-family-id="${escapeHtml(familyId)}">
        <div class="wizard-pick-row-label">${escapeHtml(family.label || familyId)}
          ${disabled ? '<span class="wizard-pick-row-hint">이 워커에 설치된 체크포인트가 없어요</span>' : ''}
        </div>
      </div>`;
    const rows = checkpoints.length > 1 ? checkpoints.map(ckpt => `
      <div class="wizard-pick-row${(wizard.familyId === familyId && wizard.checkpoint === ckpt) ? ' selected' : ''}"
           data-family-id="${escapeHtml(familyId)}" data-checkpoint="${escapeHtml(ckpt)}" style="margin-left:20px;">
        <div class="wizard-pick-row-label">${escapeHtml(ckpt)}</div>
      </div>`).join('') : '';
    return header + rows;
  }).join('')}</div>`;
}

document.getElementById('wizard-family-modal-body').addEventListener('click', (e) => {
  const row = e.target.closest('.wizard-pick-row');
  if(!row || row.classList.contains('disabled')) return;
  const familyId = row.dataset.familyId;
  const family = baseModelFamilies[familyId];
  if(!family) return;
  let checkpoint = row.dataset.checkpoint;
  if(!checkpoint){
    // family 헤더 줄을 클릭하면 — 체크포인트가 하나면 물론 그것, 여러 개여도 첫 번째
    // 것을 바로 골라 준다(다른 걸 쓰려면 아래 하위 줄에서 다시 고르면 됨). 헤더를
    // 클릭했는데 아무 반응이 없어서 하위 줄을 또 찾아 눌러야 하는 게 불편하다는
    // 피드백으로 이렇게 바꿨다 — "가족만 고르고 체크포인트는 못 정함" 상태를 만들지 않는다.
    const checkpoints = family.checkpoints || [];
    checkpoint = checkpoints[0];
  }
  if(wizard.familyId !== familyId){
    // family가 바뀌면 이전 단계에서 고른 워크플로우 유형/LoRA는 더 이상 호환을
    // 보장할 수 없으니 함께 초기화한다. 단, 라이트박스에서 연 Face Detailer 유형은
    // 이 family에서 쓸 수 있으면(체크포인트형·UNet+CLIP+VAE형) 그대로 둔다.
    const keepFaceDetailer = wizardPendingInputImage && wizard.base === 'face_detailer'
      && (family.kind !== 'diffusion_models' || !!family.unet_image);
    // Library "이 포즈로 생성"으로 연 txt2img + OpenPose도 체크포인트형 family면 그대로 둔다.
    const keepOpenPose = !!wizardPendingPose && wizard.post.openpose && family.kind === 'checkpoints';
    // Library Position(NS-43)은 단순 txt2img라 어떤 family(체크포인트형·UNet형)에서도 유지한다.
    const keepTxt2img = keepOpenPose || !!(wizardPendingPose && wizardPendingPose.position);
    // Openpose CN으로 골랐는데 OpenPose를 못 쓰는 family면(NS-51) 포즈·프롬프트를 적용하지 않고 알린다.
    if(wizardPendingPose && !keepTxt2img){
      wizardPendingPose = null;
      document.getElementById('load-notice').textContent =
        '이 베이스 모델은 Openpose CN을 쓸 수 없어 고른 포즈 방식이 적용되지 않았어요. 체크포인트형 모델로 다시 열어 주세요.';
    }
    wizard.base = keepFaceDetailer ? 'face_detailer' : keepTxt2img ? 'txt2img' : null;
    wizard.post = freshWizardPost();
    wizard.post.openpose = keepOpenPose;
    wizard.preset = null;
    wizard.loras = [];
    wizard.batchMode = keepFaceDetailer || keepTxt2img ? 'seed' : null;
  }
  wizard.familyId = familyId;
  wizard.checkpoint = checkpoint;
  wizardUpdateStepButtons();
  closeWizardModal('wizard-family-modal');
});

document.getElementById('wizard-step-family').addEventListener('click', openWizardFamilyModal);

// ---- 2단계: 워크플로우 유형 — 베이스(택1) + 후처리(다중) + 프리셋(택1, 나머지와 배타적) ----
async function openWizardTypeModal(){
  if(!wizard.familyId) return;
  const body = document.getElementById('wizard-type-modal-body');
  body.innerHTML = '<div class="comfy-model-empty">불러오는 중…</div>';
  openWizardModal('wizard-type-modal');
  await fetchWorkflowTypes();
  await fetchWorkflowPresetsList();
  try{ await fetchComfyObjectInfo(false); }catch(e){ /* node_types 없이도 목록은 보여준다 */ }
  renderWizardTypeModal();
}

// 1단계에서 고른 모델이 checkpoint_match(예: MiniMax-H3의 fl2va/ref2va) 계열인지. 파일 이름에 그 글자가 있거나,
// 모델 탭 등록부의 태그에 있으면(파인튜닝 모델처럼 이름에 계열이 안 드러나는 경우 — 태그는 "fl2va, ref2va"처럼
// 한 칸에 여러 개를 적어도 된다) 맞는 것으로 본다. 이름과 태그 어디에도 이 family의 계열 표시가 없으면 'unknown'.
function wizardVariantInfo(variants){
  const name = String(wizard.checkpoint || '');
  const base = name.split(/[\\/]/).pop();
  const entry = Object.values(modelRegistry.items || {}).find(e =>
    (e.kind === 'diffusion_models' || e.kind === 'checkpoints') && (e.filename === name || String(e.filename).split(/[\\/]/).pop() === base));
  const tags = ((entry && entry.tags) || []).flatMap(t => String(t).toLowerCase().split(/[\s,]+/)).filter(Boolean);
  const has = v => name.toLowerCase().includes(v.toLowerCase()) || tags.includes(v.toLowerCase());
  const known = variants.filter(has);
  return { has, unknown: variants.length > 0 && known.length === 0 };
}

function renderWizardTypeModal(){
  const body = document.getElementById('wizard-type-modal-body');
  const catalog = workflowTypesCache || {};
  const familyVariants = [...new Set((catalog.base || []).filter(t => t.family_id === wizard.familyId && t.checkpoint_match).map(t => t.checkpoint_match))];
  const variant = wizardVariantInfo(familyVariants);
  const currentFamily = wizard.familyId ? baseModelFamilies[wizard.familyId] : null;
  // base/post 항목에 family_id/applies_to_base가 있으면 그 조건에 맞을 때만 보인다.
  // family_id가 없는 범용 항목(txt2img/img2img)은 체크포인트 기반(SDXL식, CheckpointLoaderSimple로
  // 조립하는 workflow_builder.py) family에서만 보인다 — krea.2/MiniMax-H3처럼 diffusion_models
  // kind(UNETLoader 기반, 전용 빌더가 조립)인 family에는 안 맞는 유형이라 숨긴다.
  // Settings의 베이스 모델 세부 설정에서 유형을 직접 지정했으면 그 목록에 있는 것만 그 순서로 보인다.
  // 아래 호환 필터는 그대로 함께 걸어 깨진 조합은 만들지 않는다. null이면 자동(호환 필터만).
  const allowed = currentFamily && Array.isArray(currentFamily.workflow_types) ? currentFamily.workflow_types : null;
  const allow = list => !allowed ? list
    : list.filter(t => allowed.includes(t.id)).sort((a, b) => allowed.indexOf(a.id) - allowed.indexOf(b.id));
  const bases = allow(catalog.base || []).filter(t => {
    if(t.family_id && t.family_id !== wizard.familyId) return false;
    // checkpoint_match: 같은 family(base_model)에 실제로 다른 역할을 하는 파일이
    // 여러 개 있을 때(예: MiniMax-H3의 fl2va/ref2va UNet) family_id만으로는 못
    // 가른다 — 1단계에서 고른 파일명에 이 부분 문자열이 있어야만 보인다.
    // 파일 이름이나 등록부 태그로 계열을 알 수 없으면 막지 않고 전부 보여 준다(아래 안내 문구).
    if(t.checkpoint_match && !variant.unknown && !variant.has(t.checkpoint_match)) return false;
    if(t.family_id) return true;
    // 단, 서버가 unet_image(텍스트 인코더·VAE)를 붙여 준 family(예: Anima)는 범용 UNet 빌더로 돈다.
    // 부품이 모자란 family(unet_image_missing)는 숨기지 않고 비활성 줄로 보여 이유를 안내한다.
    return !currentFamily || currentFamily.kind !== 'diffusion_models' || !!currentFamily.unet_image || !!currentFamily.unet_image_missing;
  });
  const unetMissing = (currentFamily && currentFamily.kind === 'diffusion_models' && !currentFamily.unet_image && currentFamily.unet_image_missing) || null;
  // family_kind가 있는 후처리(openpose)는 그 kind의 family에서만 보인다 — UNet형(Anima·krea2·영상)에서는 숨긴다.
  const posts = allow(catalog.post || []).filter(t => (!t.applies_to_base || t.applies_to_base.includes(wizard.base))
    && postFitsFamily(t, currentFamily));
  // preset 유형(ControlNet/IPAdapter)은 이 family용 프리셋이 없으면 골라봤자 실행할
  // 워크플로우가 없으므로 아예 목록에서 숨긴다(LoRA 호환성 필터링과 같은 원칙).
  const presets = allow(catalog.preset || []).filter(t => presetExists(wizard.familyId, t.id));
  const nodeTypes = (comfyObjectInfoCache && comfyObjectInfoCache.connected) ? comfyObjectInfoCache.node_types : null;

  const baseRows = bases.map(t => {
    const selected = wizard.base === t.id;
    // krea2_t2i: 프롬프트를 LLM으로 보강할지 on/off. minimax_h3_r2v: 참조 이미지/비디오/오디오를
    // 몇 개씩 쓸지(그래프 구조 자체가 바뀌므로 빌드 타임에 정함 — server/workflow_builder_minimax_h3.py
    // 참고, 이미지 0~9/비디오 0~3/오디오 0~3) + 비디오 참조 방식(표준 VHS_LoadVideo 기본, 파드에
    // 그 커스텀 노드가 없으면 순정 LoadVideo로 바꿀 수 있음).
    const vhsMissing = t.id === 'minimax_h3_r2v' && nodeTypes && !nodeTypes.includes('VHS_LoadVideo');
    const extra = !selected ? '' : t.id === 'krea2_t2i'
      ? `<label class="wizard-pick-row-strength" style="display:inline-flex;align-items:center;gap:4px;font-size:13px;">
           <input type="checkbox" class="wizard-krea2-refine-input" ${wizard.refinePrompt ? 'checked' : ''}> 프롬프트 자동 보강(LLM)
         </label>`
      : t.id === 'minimax_h3_r2v'
      ? `<div class="wizard-pick-row-block">
           <label>이미지<input type="number" class="option-input wizard-refimage-count-input" value="${wizard.refImageCount}" min="0" max="9" step="1" title="참조 이미지 개수(0~9)"></label>
           <label>비디오<input type="number" class="option-input wizard-refvideo-count-input" value="${wizard.refVideoCount}" min="0" max="3" step="1" title="참조 비디오 개수(0~3)"></label>
           <label>오디오<input type="number" class="option-input wizard-refaudio-count-input" value="${wizard.refAudioCount}" min="0" max="3" step="1" title="참조 오디오 개수(0~3)"></label>
           ${wizard.refVideoCount > 0 ? `<select class="option-input wizard-refvideo-mode-input">
             <option value="vhs"${wizard.videoRefMode === 'vhs' ? ' selected' : ''}>비디오 참조: 표준 방식(VHS_LoadVideo)${vhsMissing ? ' — 설치 필요' : ''}</option>
             <option value="native"${wizard.videoRefMode === 'native' ? ' selected' : ''}>비디오 참조: 순정 방식(LoadVideo)</option>
           </select>` : ''}
         </div>`
      : t.id === 'minimax_h3_i2v'
      ? `<div class="wizard-pick-row-block">
           <label><input type="checkbox" class="wizard-i2v-first-frame-input" ${wizard.useFirstFrame ? 'checked' : ''}> 첫 프레임으로 사용</label>
           <label><input type="checkbox" class="wizard-i2v-last-frame-input" ${wizard.useLastFrame ? 'checked' : ''}> 마지막 프레임으로 사용</label>
         </div>`
      : t.id === 'minimax_h3_t2v'
      ? `<div class="wizard-pick-row-block">
           <label>너비<input type="number" class="option-input wizard-t2v-width-input" value="${wizard.t2vWidth}" min="64" step="16" title="영상 너비(참조 이미지가 없어 직접 정함)"></label>
           <label>높이<input type="number" class="option-input wizard-t2v-height-input" value="${wizard.t2vHeight}" min="64" step="16" title="영상 높이(참조 이미지가 없어 직접 정함)"></label>
         </div>`
      : '';
    const partsMissing = unetMissing && !t.family_id;
    return `
    <div class="wizard-pick-row${selected ? ' selected' : ''}${partsMissing ? ' disabled' : ''}" data-group="base" data-type-id="${escapeHtml(t.id)}">
      <div class="wizard-pick-row-label">${escapeHtml(t.label)}</div>
      ${extra}
    </div>`;
  }).join('');

  const postRows = posts.map(t => {
    const checked = !!wizard.post[t.id];
    const missingNode = t.requires_node && nodeTypes && !nodeTypes.includes(t.requires_node);
    const badge = missingNode ? `<span class="wizard-pick-row-badge"><svg class="ico"><use href="#i-triangle-alert"/></svg> 설치 필요 (${escapeHtml(t.requires_node)})</span>` : '';
    // Hires Fix를 켰으면 배율(scale_by)을 바로 그 줄에서 입력받는다 — 서버(workflow_builder.py)는
    // 이미 이 값을 받는데 마법사가 항상 기본값(1.5)만 보내고 있었다.
    const scaleInput = (t.id === 'hires_fix' && checked)
      ? `<input type="number" class="option-input wizard-pick-row-strength wizard-hires-scale-input" value="${wizard.post.hiresScale}" min="1" max="4" step="0.1" title="배율(scale_by)">`
      : '';
    // OpenPose는 이 베이스 모델에 연결된 ControlNet(controlnets.openpose, 1순위가 맨 앞)을 고른다. 없으면 비활성+안내.
    const cnNames = t.id === 'openpose' ? openPoseControlNets(currentFamily) : null;
    const cnMissing = cnNames && !cnNames.length;
    const cnExtra = !cnNames ? '' : cnMissing
      ? `<span class="wizard-pick-row-hint">모델 탭에서 ControlNet을 이 베이스 모델에 연결해 주세요</span>`
      : checked ? `<select class="option-input wizard-cn-name-input" title="ControlNet 모델">${cnNames.map(n =>
          `<option value="${escapeHtml(n)}"${n === wizard.post.cnName ? ' selected' : ''}>${escapeHtml(n)}</option>`).join('')}</select>` : '';
    return `
      <div class="wizard-pick-row${checked ? ' selected' : ''}${wizard.base && !cnMissing ? '' : ' disabled'}" data-group="post" data-type-id="${escapeHtml(t.id)}">
        <div class="wizard-pick-row-label">${escapeHtml(t.label)}</div>
        ${badge}${scaleInput}${cnExtra}
      </div>`;
  }).join('');

  const pres = catalog.pre || [];
  const preSection = pres.length === 0 ? '' : `
    <div class="wizard-pick-section-title">전처리 <span class="wizard-pick-row-hint">— 프롬프트를 생성 전에 다듬어요. 모드(자연어/Danbooru)는 고른 모델 계열로 자동 정해져요</span></div>
    <div class="wizard-pick-list">${pres.map(t => `
      <div class="wizard-pick-row${wizard.post[t.id] ? ' selected' : ''}" data-group="pre" data-type-id="${escapeHtml(t.id)}">
        <div class="wizard-pick-row-label">${escapeHtml(t.label)}</div>
        <span class="wizard-pick-row-hint">${wizardEnhanceMode() === 'danbooru' ? 'Danbooru 태그' : '자연어'}</span>
      </div>`).join('')}</div>`;

  const presetSection = presets.length === 0 ? '' : `
    <div class="wizard-pick-section-title">ControlNet / IPAdapter <span class="wizard-pick-row-hint">— 베이스/후처리와 동시에 쓸 수 없어요(family별로 미리 만들어둔 워크플로우를 그대로 씀)</span></div>
    <div class="wizard-pick-list">${presets.map(t => `
      <div class="wizard-pick-row${wizard.preset === t.id ? ' selected' : ''}" data-group="preset" data-type-id="${escapeHtml(t.id)}">
        <div class="wizard-pick-row-label">${escapeHtml(t.label)}</div>
      </div>`).join('')}</div>`;

  const variantHint = variant.unknown
    ? `<div class="email-hint">고른 모델이 ${familyVariants.join('/')} 중 어느 계열인지 파일 이름으로 알 수 없어서 모든 유형을 보여 줘요. "모델" 탭에서 이 모델의 태그에 ${familyVariants.join(' 또는 ')}을(를) 적어 두면 맞는 유형만 보여요.</div>`
    : '';
  const partNames = { clip: '텍스트 인코더', vae: 'VAE' };
  const unetMissingHint = unetMissing
    ? `<div class="email-hint">${unetMissing.map(k => partNames[k] || k).join('/')}가 정해지지 않았어요 — "모델" 탭에서 그 파일의 베이스 모델을 ${escapeHtml(currentFamily.label || wizard.familyId)}(으)로 지정해 주세요.</div>`
    : '';
  body.innerHTML = `
    ${variantHint}
    ${unetMissingHint}
    <div class="wizard-pick-section-title">베이스 (하나 선택)</div>
    <div class="wizard-pick-list">${baseRows}</div>
    <div class="wizard-pick-section-title">후처리 <span class="wizard-pick-row-hint">— 여러 개 함께 켤 수 있어요(베이스를 먼저 고르세요)</span></div>
    <div class="wizard-pick-list">${postRows}</div>
    ${preSection}
    ${presetSection}
  `;
}

document.getElementById('wizard-type-modal-body').addEventListener('click', (e) => {
  // 배율/체크박스/참조 개수/비디오 참조 방식 같은 인라인 입력칸 클릭은 그 줄의 선택 토글이 아니다.
  if(e.target.classList.contains('wizard-hires-scale-input')
     || e.target.classList.contains('wizard-cn-name-input')
     || e.target.classList.contains('wizard-krea2-refine-input')
     || e.target.classList.contains('wizard-refimage-count-input')
     || e.target.classList.contains('wizard-refvideo-count-input')
     || e.target.classList.contains('wizard-refaudio-count-input')
     || e.target.classList.contains('wizard-refvideo-mode-input')
     || e.target.classList.contains('wizard-i2v-first-frame-input')
     || e.target.classList.contains('wizard-i2v-last-frame-input')
     || e.target.classList.contains('wizard-t2v-width-input')
     || e.target.classList.contains('wizard-t2v-height-input')) return;
  const row = e.target.closest('.wizard-pick-row');
  if(!row || row.classList.contains('disabled')) return;
  const group = row.dataset.group;
  const typeId = row.dataset.typeId;

  if(group === 'base'){
    if(wizard.base !== typeId){
      wizard.loras = [];
      wizard.batchMode = null;
      wizard.refinePrompt = true;
      wizard.refImageCount = 2;
      wizard.refVideoCount = 0;
      wizard.refAudioCount = 0;
      wizard.videoRefMode = 'vhs';
      wizard.useFirstFrame = true;
      wizard.useLastFrame = false;
      wizard.t2vWidth = 704;
      wizard.t2vHeight = 1280;
    }
    wizard.base = typeId;
    wizard.preset = null; // 베이스를 고르면 프리셋(배타적 그룹)은 해제
  }else if(group === 'post'){
    if(!wizard.base) return; // 베이스가 없으면 후처리도 의미 없음
    wizard.post[typeId] = !wizard.post[typeId];
  }else if(group === 'pre'){
    wizard.post[typeId] = !wizard.post[typeId];
  }else if(group === 'preset'){
    if(wizard.preset !== typeId){
      wizard.loras = [];
      wizard.batchMode = null;
    }
    wizard.preset = typeId;
    wizard.base = null; // 프리셋을 고르면 베이스/후처리(배타적 그룹)는 해제
    const keepEnhance = wizard.post.prompt_enhance; // 전처리는 프리셋과도 함께 쓴다
    wizard.post = freshWizardPost();
    wizard.post.prompt_enhance = keepEnhance;
  }
  wizardUpdateStepButtons();
  renderWizardTypeModal();
});

function handleWizardTypeModalInput(e){
  let needsRerender = false;
  if(e.target.classList.contains('wizard-hires-scale-input')){
    wizard.post.hiresScale = parseFloat(e.target.value) || 1.5;
  }else if(e.target.classList.contains('wizard-cn-name-input')){
    wizard.post.cnName = e.target.value;
  }else if(e.target.classList.contains('wizard-krea2-refine-input')){
    wizard.refinePrompt = e.target.checked;
  }else if(e.target.classList.contains('wizard-refimage-count-input')){
    wizard.refImageCount = Math.min(9, Math.max(0, parseInt(e.target.value, 10) || 0));
  }else if(e.target.classList.contains('wizard-refvideo-count-input')){
    wizard.refVideoCount = Math.min(3, Math.max(0, parseInt(e.target.value, 10) || 0));
    needsRerender = true; // 0 <-> 양수 전환에 따라 "비디오 참조 방식" select가 나타나거나 사라져야 함
  }else if(e.target.classList.contains('wizard-refaudio-count-input')){
    wizard.refAudioCount = Math.min(3, Math.max(0, parseInt(e.target.value, 10) || 0));
  }else if(e.target.classList.contains('wizard-refvideo-mode-input')){
    wizard.videoRefMode = e.target.value === 'native' ? 'native' : 'vhs';
  }else if(e.target.classList.contains('wizard-i2v-first-frame-input')){
    wizard.useFirstFrame = e.target.checked;
  }else if(e.target.classList.contains('wizard-i2v-last-frame-input')){
    wizard.useLastFrame = e.target.checked;
  }else if(e.target.classList.contains('wizard-t2v-width-input')){
    wizard.t2vWidth = Math.max(64, parseInt(e.target.value, 10) || 704);
  }else if(e.target.classList.contains('wizard-t2v-height-input')){
    wizard.t2vHeight = Math.max(64, parseInt(e.target.value, 10) || 1280);
  }else{
    return;
  }
  wizardUpdateStepButtons();
  if(needsRerender) renderWizardTypeModal();
}
document.getElementById('wizard-type-modal-body').addEventListener('input', handleWizardTypeModalInput);
// <select>는 브라우저에 따라 "input" 대신 "change"만 쏘는 경우가 있어(비디오 참조 방식
// 드롭다운), 같은 핸들러를 둘 다에 건다.
document.getElementById('wizard-type-modal-body').addEventListener('change', handleWizardTypeModalInput);

document.getElementById('wizard-step-type').addEventListener('click', openWizardTypeModal);

// ---- 3단계: LoRA ----
// 베이스(txt2img/img2img) 조합은 workflow_builder.py가 고른 LoRA를 전부 체인으로
// 구워 넣을 수 있어서 여러 개를 골라도 된다. preset(ControlNet/IPAdapter)은
// 관리자가 미리 만들어둔 워크플로우 구조를 그대로 쓰고, 거기 있는 LoRA 로더
// 하나에만 런타임으로 덮어쓸 수 있으므로(app.py의 apply_lora와 같은 제약) 한
// 개만 고를 수 있다.
async function openWizardLoraModal(){
  if(!wizardTypeChosen()) return;
  const body = document.getElementById('wizard-lora-modal-body');
  body.innerHTML = '<div class="comfy-model-empty">불러오는 중…</div>';
  openWizardModal('wizard-lora-modal');
  await Promise.all([
    fetchComfyObjectInfo(false).catch(() => { /* 아래에서 연결 안 됨으로 안내 */ }),
    fetchModelInventory(false),
  ]);
  renderWizardLoraModal();
}

// LoRA 목록의 설치 현황 배지 — 켜진 파드 중 어디에 있나. 없으면 경고(고를 수는 있다: 대기 칸에서 받으면 됨).
function loraPodsBadge(name){
  const pods = podsWithModel('loras', name);
  if(pods === null) return '';
  return pods.length
    ? `<span class="model-pods-badge ok" title="설치된 워커: ${escapeHtml(pods.join(', '))}">${ico('circle-check')} ${escapeHtml(pods.join(', '))}</span>`
    : `<span class="model-pods-badge warn" title="켜진 워커 어디에도 없어요 — 골라도 되지만, 작업이 대기 칸에서 '없는 모델 받기'를 기다려요">${ico('triangle-alert')} 켜진 워커에 없음</span>`;
}

// 마법사 LoRA 선택지 — 켜진 파드에 설치된 LoRA ∪ 모델 등록부 LoRA. 파드에 없는 것도 고를 수 있게 한다
// (대기 칸에서 '없는 모델 받기'로 받는다). 파일 이름(마지막 경로 조각)이 같으면 설치된 쪽 이름(하위 폴더 포함)을 남긴다.
function wizardLoraChoices(){
  const info = comfyObjectInfoCache;
  const installed = (info && info.connected && info.models && info.models.loras) || [];
  const catalog = (info && info.catalog && info.catalog.loras) || [];
  const baseName = name => String(name).split(/[\\/]/).pop();
  const seen = new Set();
  const out = [];
  for(const name of [...installed, ...catalog]){
    const key = baseName(name);
    if(seen.has(key)) continue;
    seen.add(key);
    out.push(name);
  }
  return out;
}

function renderWizardLoraModal(){
  const body = document.getElementById('wizard-lora-modal-body');
  const isPreset = wizardIsPreset();
  const all = wizardLoraChoices();
  if(all.length === 0){
    body.innerHTML = '<div class="comfy-model-empty">고를 수 있는 LoRA가 없어요 — 워커의 파드를 켜거나 "모델" 탭에 LoRA를 등록하세요.</div>';
    return;
  }
  // 등록부에 베이스 모델을 안 적은 LoRA는 모든 베이스 모델과 호환되는 것으로 취급한다 — 나머지는 소속 베이스 모델 중
  // 하나가 지금 고른 것과 같을 때만 보여준다(비호환 LoRA는 아예 숨김).
  const compatible = all.filter(name => {
    const entry = getLoraEntry(name);
    return !entry.baseIds.length || entry.baseIds.includes(wizard.familyId);
  });
  const hint = isPreset
    ? '<div class="email-hint">이 워크플로우 유형은 프리셋에 이미 배선된 LoRA 로더 하나에만 덮어쓸 수 있어서, 여기서는 하나만 고를 수 있어요.</div>'
    : '<div class="email-hint">여러 개를 고를 수 있어요. 강도는 고른 줄에 나타나는 입력칸에서 바꾸세요(기본 1).</div>';
  if(compatible.length === 0){
    body.innerHTML = hint + '<div class="comfy-model-empty">이 베이스 모델과 호환되는 LoRA가 없어요("모델" 탭에서 LoRA의 베이스 모델을 맞추거나 비워두면 보여요).</div>';
    return;
  }
  body.innerHTML = hint + `<div class="wizard-pick-list">${compatible.map(name => {
    const picked = wizard.loras.find(l => l.name === name);
    return `
      <div class="wizard-pick-row${picked ? ' selected' : ''}" data-lora-name="${escapeHtml(name)}">
        <div class="wizard-pick-row-label">${escapeHtml(name)}${loraPodsBadge(name)}</div>
        ${picked ? `<input type="number" class="option-input wizard-pick-row-strength wizard-lora-strength-input" data-lora-name="${escapeHtml(name)}" value="${picked.strength}" step="0.05">` : ''}
      </div>`;
  }).join('')}</div>`;
}

document.getElementById('wizard-lora-modal-body').addEventListener('click', (e) => {
  if(e.target.classList.contains('wizard-lora-strength-input')) return; // 강도 입력칸 클릭은 선택 토글이 아님
  const row = e.target.closest('.wizard-pick-row');
  if(!row) return;
  const name = row.dataset.loraName;
  const isPreset = wizardIsPreset();
  const existingIndex = wizard.loras.findIndex(l => l.name === name);
  if(existingIndex >= 0){
    wizard.loras.splice(existingIndex, 1);
  }else if(isPreset){
    wizard.loras = [{ name, strength: 1 }]; // preset 모드는 한 개만 — 새로 고르면 이전 선택을 대체
  }else{
    wizard.loras.push({ name, strength: 1 });
  }
  wizardUpdateStepButtons();
  renderWizardLoraModal();
});

document.getElementById('wizard-lora-modal-body').addEventListener('input', (e) => {
  if(!e.target.classList.contains('wizard-lora-strength-input')) return;
  const entry = wizard.loras.find(l => l.name === e.target.dataset.loraName);
  if(entry) entry.strength = parseFloat(e.target.value) || 1;
  wizardUpdateStepButtons(); // 강도를 바꾼 것도 "적용된 것과 달라짐"으로 쳐서 다시 적용 배너가 뜨게 한다
});

document.getElementById('wizard-step-lora').addEventListener('click', openWizardLoraModal);

// ---- 4단계: 실행 방식(시드 반복 / CSV 순회) ----
function openWizardBatchModeModal(){
  if(!wizardTypeChosen()) return;
  const body = document.getElementById('wizard-batchmode-modal-body');
  body.innerHTML = `<div class="wizard-pick-list">
    <div class="wizard-pick-row${wizard.batchMode === 'seed' ? ' selected' : ''}" data-mode="seed">
      <div class="wizard-pick-row-label">시드 반복<span class="wizard-pick-row-hint">프롬프트 하나로 시드만 바꿔가며 여러 장 생성</span></div>
    </div>
    <div class="wizard-pick-row${wizard.batchMode === 'csv' ? ' selected' : ''}" data-mode="csv">
      <div class="wizard-pick-row-label">CSV 순회<span class="wizard-pick-row-hint">CSV 파일의 행마다 다른 프롬프트/설정으로 생성</span></div>
    </div>
  </div>`;
  openWizardModal('wizard-batchmode-modal');
}

document.getElementById('wizard-batchmode-modal-body').addEventListener('click', (e) => {
  const row = e.target.closest('.wizard-pick-row');
  if(!row) return;
  wizard.batchMode = row.dataset.mode;
  wizardUpdateStepButtons();
  closeWizardModal('wizard-batchmode-modal');
});

document.getElementById('wizard-step-batchmode').addEventListener('click', openWizardBatchModeModal);

// ---- "적용" — 아래 템플릿 선택/옵션 폼/워크플로우 슬롯을 채운다 ----
// goToNewJobDetails()가 "다음"(또는 "세부 설정" 탭)을 눌렀을 때, 마법사가 마지막 적용
// 이후로 바뀌었으면(wizardStale()) 이 함수를 불러 지금 상태 기준으로 다시 만든다 — 그
// 말고는 절대 자동으로 안 불린다. renderOptionFields가 옵션 폼을 통째로 다시 그려서 이미
// 입력해둔 프롬프트/시드수 등을 지워버리므로, 자동으로 재실행하면 방금 쓴 값이 조용히
// 날아갈 수 있어서다 — 그래서 "다음"을 명시적으로 눌러야만 일어난다.
// 성공하면 true, 실패하면(#wizard-error에 안내를 남기고) false를 돌려준다 — 호출부가
// 이 값을 보고 세부 설정 탭으로 넘어갈지 말지, wizardAppliedSignature를 갱신할지 정한다.
async function wizardApply(){
  const errorEl = document.getElementById('wizard-error');
  errorEl.textContent = '';
  if(!wizard.familyId || !wizard.checkpoint || !wizardTypeChosen() || !wizard.batchMode){
    errorEl.textContent = '1~4단계를 모두 고른 뒤 적용하세요.';
    return false;
  }
  const templateIds = wizardTemplateIds();
  if(!templateIds){
    errorEl.textContent = '이 조합에 쓸 템플릿 정보를 찾지 못했어요.';
    return false;
  }
  if(Object.keys(templatesById).length === 0) await fetchTemplates();
  const templateId = templateIds[wizard.batchMode];
  const template = templatesById[templateId];
  if(!template){
    errorEl.textContent = `템플릿 '${templateId}'이(가) 서버에 등록돼 있지 않아요.`;
    return false;
  }

  const isPreset = wizardIsPreset();
  // architecture가 없으면(txt2img/img2img) 기본 SDXL 빌더(workflow_builder.py)로 간다 —
  // krea2_t2i/minimax_h3_i2v/minimax_h3_r2v는 각각 전용 빌더로 분기한다(app.py 참고).
  // 범용 txt2img/img2img도 UNet+CLIP+VAE family(unet_image)면 workflow_builder_unet.py로 보낸다.
  const unetImage = (baseModelFamilies[wizard.familyId] || {}).unet_image;
  const architecture = (wizardBaseMeta(wizard.base) || {}).architecture || (unetImage ? 'unet' : 'sdxl');
  let workflow;
  try{
    if(isPreset){
      const res = await fetch(`/api/workflow-presets/${encodeURIComponent(wizard.familyId)}/${encodeURIComponent(wizard.preset)}`);
      if(!res.ok) throw new Error('이 조합의 프리셋 워크플로우를 찾을 수 없어요 — "모델" 탭에서 먼저 올려주세요.');
      workflow = await res.json();
    }else{
      const spec = {
        base: wizard.base,
        architecture,
        checkpoint: wizard.checkpoint,
        // 진짜 프롬프트는 적용 뒤 아래 옵션 폼(시드 반복이면 main_prompt 필드, CSV
        // 순회면 CSV 파일)에서 입력한다 — 여기는 build_workflow()가 요구하는
        // 자리표시자일 뿐, 실행 시점에 템플릿의 MAIN_PROMPT/CSV 프롬프트 컬럼이
        // 덮어쓴다(비워두면 이 자리표시자 그대로 나갈 수 있으니 주의하라고 아래에서 안내).
        positive: '(prompt)',
        loras: wizard.loras.map(l => ({ name: l.name, strength_model: l.strength, strength_clip: l.strength })),
      };
      if(architecture === 'sdxl' || architecture === 'unet'){
        if(architecture === 'unet'){ spec.clip = unetImage.clip; spec.vae = unetImage.vae; }
        // 베이스 모델의 스윗 포인트(Settings 탭) — 빈 칸은 빌더 기본값
        const sweet = (baseModelFamilies[wizard.familyId] || {}).sweet || {};
        for(const k of ['cfg', 'steps', 'sampler_name', 'negative']) if(sweet[k] != null && sweet[k] !== '') spec[k] = sweet[k];
        if(wizardPostApplies('hires_fix')) spec.hires_fix = { enabled: true, scale_by: wizard.post.hiresScale };
        // 얼굴 프롬프트·시드는 실행 시점에 템플릿이 넣는다(face_prompt/face_negative_prompt 옵션).
        // 수치는 이 베이스 모델에 저장된 기본값("셋팅" 탭)을 쓴다 — 못 불러오면 빌더 기본값.
        if(wizardPostApplies('face_detailer') || wizard.base === 'face_detailer'){
          spec.face_detailer = await faceDetailerSpec(wizard.familyId, architecture);
          if(wizard.base !== 'face_detailer') spec.face_detailer.enabled = true;
        }
        if(wizardPostApplies('usdu')) spec.usdu = { enabled: true, upscale_by: 2.0 };
        // 포즈 이미지는 실행 시점에 템플릿이 넣는다(pose_image 옵션). 수치는 이 베이스 모델의 셋팅 기본값.
        if(wizardPostApplies('openpose') && postFitsFamily({ family_kind: 'checkpoints' }, baseModelFamilies[wizard.familyId])){
          spec.controlnet = await controlNetSpec(wizard.familyId,
            wizard.post.cnName || openPoseControlNets(baseModelFamilies[wizard.familyId])[0]);
        }
      }else if(architecture === 'krea2'){
        spec.refine_prompt = wizard.refinePrompt;
      }else if(architecture === 'minimax_h3_i2v'){
        // minimax_h3_t2v와 minimax_h3_i2v(fl2v)는 같은 빌더를 쓴다 — t2v는 이미지를
        // 하나도 안 쓰고 대신 너비/높이를 직접 정한다(server/workflow_builder_minimax_h3.py 참고).
        if(wizard.base === 'minimax_h3_t2v'){
          spec.use_first_frame = false;
          spec.use_last_frame = false;
          spec.width = wizard.t2vWidth;
          spec.height = wizard.t2vHeight;
        }else{
          spec.use_first_frame = wizard.useFirstFrame;
          spec.use_last_frame = wizard.useLastFrame;
        }
      }else if(architecture === 'minimax_h3_r2v'){
        spec.ref_image_count = wizard.refImageCount;
        spec.ref_video_count = wizard.refVideoCount;
        spec.ref_audio_count = wizard.refAudioCount;
        spec.video_ref_mode = wizard.videoRefMode;
      }
      const res = await fetch(`/api/build-workflow${podQuery()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(spec),
      });
      const data = await res.json().catch(() => ({}));
      if(!res.ok) throw new Error(data.detail || '워크플로우를 만들지 못했어요.');
      workflow = data.workflow;
      // 없는 모델은 막지 않는다 — 작업이 대기 칸에서 그 모델을 받은 뒤 시작한다.
      const missing = Array.isArray(data.missing_models) ? data.missing_models : [];
      if(missing.length){
        flashNotice(`이 워커에 없는 모델: ${missing.map(m => `${m.label} ${m.value}`).join(', ')} — 대기 칸에서 받아요(워커의 자동 설치가 켜져 있으면 알아서 받아요).`);
      }
    }
  }catch(e){
    errorEl.textContent = e.message || '워크플로우를 준비하지 못했어요.';
    return false;
  }

  resetForm();
  templateSelect.value = templateId;
  // 이 템플릿 그래프에 실제로 있는 참조 슬롯만 옵션 폼에 그려지도록(isWizardRefSlotHidden
  // 참고) — architecture가 다르면 그냥 null로 둬서 모든 슬롯이 보이게 한다(그런 슬롯
  // 이름을 안 쓰는 템플릿이라 어차피 영향 없음).
  wizardRefCounts = architecture === 'minimax_h3_r2v'
    ? { image: wizard.refImageCount, video: wizard.refVideoCount, audio: wizard.refAudioCount }
    : architecture === 'minimax_h3_i2v'
    ? (wizard.base === 'minimax_h3_t2v'
        ? { useFirstFrame: false, useLastFrame: false }
        : { useFirstFrame: wizard.useFirstFrame, useLastFrame: wizard.useLastFrame })
    : null;
  await onTemplateChange();
  // 마법사가 템플릿을 정했으니, 직접 고르는 펼침 토글은 접어 둔다(둘이 동시에 열려 있으면 헷갈림).
  const manualToggle = document.getElementById('manual-template-toggle');
  if(manualToggle) manualToggle.open = false;

  // builder 조합(txt2img/img2img + 후처리)은 체크포인트/LoRA를 이미 워크플로우에
  // 구워 넣었으므로 런타임 override 옵션(checkpoint/lora_name)은 비워둔다(중복
  // 적용 방지, "비워두면 워크플로우 값 그대로" 규칙과 일치). preset(ControlNet/
  // IPAdapter)은 프리셋 워크플로우를 그대로 쓰되, 같은 family의 다른 체크포인트로
  // 바꾸거나(호환 아키텍처이므로 안전) LoRA 하나를 얹고 싶을 때가 있어 그 두
  // 옵션만 채워준다.
  if(isPreset){
    const ckptEl = optionsFields.querySelector('[data-name="checkpoint"]');
    if(ckptEl) ckptEl.value = wizard.checkpoint;
    if(wizard.loras.length > 0){
      const loraNameEl = optionsFields.querySelector('[data-name="lora_name"]');
      const loraStrengthEl = optionsFields.querySelector('[data-name="lora_strength"]');
      if(loraNameEl) loraNameEl.value = wizard.loras[0].name;
      if(loraStrengthEl) loraStrengthEl.value = wizard.loras[0].strength;
    }
  }

  // 라이트박스 "Face Detailer"로 열었으면 그 이미지를 참조 이미지로 채운다.
  if(wizardPendingInputImage && wizard.base === 'face_detailer'){
    const inputEl = optionsFields.querySelector('[data-name="input_image"]');
    if(inputEl) inputEl.value = wizardPendingInputImage;
  }
  // Library "이 포즈로 생성"으로 열었으면 포즈 이미지 칸을 채우고(크기 제안까지), danbooru prompt를 메인 프롬프트 끝에 붙인다.
  // 순차 생성(NS-42)이면 포즈 칸 대신 "포즈 N개" 안내를 두고, 제출 직전에 pose_sequence를 채운다.
  // Library Position(NS-43)은 OpenPose 없이 같은 자리에서 danbooru prompt만 붙인다(순차면 Position N개 안내).
  const pendingPosition = wizardPendingPose && wizardPendingPose.position;
  if(wizardPendingPose && (pendingPosition || workflowHasOpenPose(workflow))){
    const poseEl = pendingPosition ? null : optionsFields.querySelector('[data-name="pose_image"]');
    const raw = optionsFields.querySelector('.enhance-raw');
    if(wizardPendingPose.sequence){
      if(pendingPosition) applyPoseSequenceForm(wizardPendingPose.sequence, null, raw, '프롬프트', positionSequenceValue);
      else applyPoseSequenceForm(wizardPendingPose.sequence, poseEl, raw);
    }else{
      if(poseEl){ poseEl.value = wizardPendingPose.name; suggestPoseSize(poseEl.value); }
      const tags = wizardPendingPose.prompt;
      const setTags = on => {
        if(!raw || !tags) return;
        const v = raw.value.trim();
        if(on && !v.includes(tags)) raw.value = [v, tags].filter(Boolean).join(', ');
        else if(!on && v.endsWith(tags)) raw.value = v.slice(0, -tags.length).replace(/,\s*$/, '');
        else return;
        raw.dispatchEvent(new Event('input'));
      };
      setTags(true);
      if(tags) addPoseTagSwitch(raw, setTags);
    }
  }

  setLoraTriggerField(wizard.loras.map(l => l.name));
  setQualityPromptField((baseModelFamilies[wizard.familyId] || {}).label);
  lastWizardModels = { checkpoint: wizard.checkpoint, loras: wizard.loras.map(l => l.name) };

  const builtIn = ['sdxl', 'unet'].includes(architecture);
  const typeSlug = isPreset ? wizard.preset : [wizard.base, architecture === 'unet' && 'unet', builtIn && wizardPostApplies('hires_fix') && 'hires', builtIn && wizardPostApplies('face_detailer') && 'fd', builtIn && wizardPostApplies('usdu') && 'usdu', workflowHasOpenPose(workflow) && 'cn'].filter(Boolean).join('_');
  setWorkflowFile(new File([JSON.stringify(workflow)], `wizard_${typeSlug}_workflow.json`, { type: 'application/json' }));

  document.getElementById('load-notice').textContent =
    '마법사 설정을 적용했어요 — 아래에서 프롬프트/세부 옵션을 확인하고(CSV 순회라면 CSV 파일도 첨부), 준비되면 "추가"를 눌러 큐에 등록하세요.';
  return true;
}

wizardUpdateStepButtons();

// ---- 업로드한 워크플로우가 이 서버에서 돌아갈 수 있는지 검사 ----
// 다른 ComfyUI 설치본에서 만든 워크플로우는 여기 없는 커스텀 노드를 쓰거나 없는
// 체크포인트/LoRA를 가리키기 쉬운데, 지금은 배치가 한참 돌다 실패해야 알 수 있다.
// 파일을 고르는 즉시 서버에 물어보고 결과만 보여준다(등록을 막지는 않음 — 검사가
// 틀릴 수도 있고, ComfyUI가 잠깐 꺼져 있을 수도 있으니 판단은 사용자 몫).
let workflowCheckToken = 0;

function renderWorkflowCheck(tone, text){
  const el = document.getElementById('workflow-check');
  el.hidden = false;
  el.className = `workflow-check ${tone}`;
  const toneIcon = tone === 'ok' ? 'circle-check' : tone === 'warn' ? 'triangle-alert' : null;
  el.innerHTML = (toneIcon ? ico(toneIcon) + ' ' : '') + escapeHtml(text);
}

function availableOnText(v){
  const pods = (v.available_on || []).map(p => p.name);
  let text = pods.length ? ` → ${pods.join(', ')} 워커에는 있어요` : '';
  if(v.registry && v.registry.download_url) text += ' · 받을 주소가 모델 탭에 등록돼 있어요';
  return text;
}

// 작업이 등록될 때 서버가 그 작업이 갈 파드에 모델/노드가 있는지 미리 본 결과(job.preflight)를 사람 말로.
function preflightLines(pf){
  if(!pf) return [];
  const lines = [];
  if((pf.missing_nodes || []).length) lines.push(`없는 노드: ${summarizeList(pf.missing_nodes)}`);
  (pf.missing_values || []).forEach(v => lines.push(`${v.field}="${v.value}"${availableOnText(v)}`));
  return lines;
}

function preflightCount(pf){
  return pf ? (pf.missing_nodes || []).length + (pf.missing_values || []).length : 0;
}

function summarizeList(items, limit = 5){
  const shown = items.slice(0, limit);
  const rest = items.length - shown.length;
  return shown.join(', ') + (rest > 0 ? ` 외 ${rest}개` : '');
}

// "세부 설정" 탭(또는 하단 "다음" 버튼)은 "워크플로우가 붙어 있다"/"마법사 4단계를
// 다 골랐다" 또는 "워크플로우가 필요 없는 템플릿을 직접 골랐다"(셸 명령 등) 중 하나일
// 때만 눌린다(newJobReadyToAdvance) — 그 전엔 넘어가도 보여줄 내용이 없다. 누르면
// goToNewJobDetails()가 필요할 때만(wizardStale()) 지금 마법사 상태로 다시 만든 다음
// 넘어간다 — 그래서 "새 작업" 탭에서 뭘 바꿔도 넘어가기 전까지는 그 상태 그대로
// 머물러 확인/조정할 수 있고, 넘어간 폼이 항상 최신 상태와 일치한다.

function newJobReadyToAdvance(){
  const template = templatesById[templateSelect.value];
  const needsWorkflow = !(template && template.requires_workflow === false);
  if(!needsWorkflow) return true;   // 셸 명령/Claude 글쓰기처럼 워크플로우 개념이 없는 템플릿
  return wizardComplete() || !!selectedFiles.workflow;
}

function updateNewJobTabs(){ renderNewJobTabs(); }

function renderNewJobTabs(){
  const selectBtn = document.getElementById('new-job-tab-select');
  const detailsBtn = document.getElementById('new-job-tab-details');
  const nextBtn = document.getElementById('new-job-next-btn');
  const ready = newJobReadyToAdvance();
  selectBtn.classList.toggle('active', newJobActiveTab === 'select');
  detailsBtn.classList.toggle('active', newJobActiveTab === 'details');
  detailsBtn.disabled = !ready;
  nextBtn.disabled = !ready;
  document.getElementById('new-job-select-panel').style.display = newJobActiveTab === 'select' ? '' : 'none';
  document.getElementById('new-job-details-panel').style.display = newJobActiveTab === 'details' ? '' : 'none';
}

// 빠른 실행창 "(새로 만들기)"(NS-52-3)로 연 마법사면 참 — "다음"에서 세부 설정 대신 빠른 실행창으로 돌아간다.
let quickRunReturn = false;

// "다음" 버튼과 "세부 설정" 탭이 똑같이 이 함수로 넘어간다 — 그래야 탭을 직접 눌러
// 옛 폼을 보는 우회로가 안 생긴다.
async function goToNewJobDetails(){
  if(!newJobReadyToAdvance()) return;
  if(wizardStale()){
    const ok = await wizardApply();
    if(!ok) return;   // #wizard-error에 이미 안내가 떴다 — 탭을 넘기지 않는다
    wizardAppliedSignature = JSON.stringify(wizard);
  }
  if(quickRunReturn && selectedFiles.workflow){
    quickRunReturn = false;
    closeNewJobModal();
    try{ await cmpUseNewWorkflow(selectedFiles.workflow); }
    catch(e){ document.getElementById('wizard-error').textContent = '만든 워크플로우를 읽지 못했어요'; openNewJobModal(); }
    return;
  }
  newJobActiveTab = 'details';
  renderNewJobTabs();
  document.getElementById('new-job-tabs').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

document.getElementById('new-job-tab-select').addEventListener('click', () => {
  newJobActiveTab = 'select';
  renderNewJobTabs();
});
document.getElementById('new-job-tab-details').addEventListener('click', goToNewJobDetails);
document.getElementById('new-job-next-btn').addEventListener('click', goToNewJobDetails);

// 라이트박스 "Face Detailer" 버튼 — 입력 이미지 풀에 둔 사본(stored)을 참조 이미지로 기억하고, 마법사를
// Face Detailer 유형·시드 반복으로 채운 채 1단계(베이스 모델) 고르기를 연다. 이미지 하나라 시드 반복이 맞다.
// 참조 이미지 칸은 "다음"을 눌러 wizardApply가 폼을 그릴 때 채워진다.
let wizardPendingInputImage = null;
async function startFaceDetailerWizard(stored){
  document.getElementById('load-error').textContent = '';
  openNewJobModal();
  resetWizardAndWorkflow();
  resetForm();
  wizardPendingInputImage = stored;
  wizardPendingPose = null;
  wizard.base = 'face_detailer';
  wizard.batchMode = 'seed';
  try{ await fetchWorkflowTypes(); }catch(e){ /* 유형 이름 없이도 진행한다 */ }
  newJobActiveTab = 'select';
  wizardUpdateStepButtons();
  document.getElementById('load-notice').textContent =
    `'${stored}'을(를) Face Detailer 참조 이미지로 골랐어요. 베이스 모델을 고르고 "다음"을 누르세요.`;
  await openWizardFamilyModal();
}
document.getElementById('wizard-reset-btn').addEventListener('click', () => { wizardPendingInputImage = null; wizardPendingPose = null; });

// Library "이 포즈로 생성" — 입력 이미지 풀에 둔 사본(stored)과 게시물의 danbooru prompt를 기억하고, 마법사를
// txt2img + OpenPose·시드 반복으로 채운 채 1단계(베이스 모델)를 연다. 포즈 칸·프롬프트는 wizardApply가 채운다.
let wizardPendingPose = null;
let wizardNextPoseSequence = null;   // startOpenPoseSequenceWizard가 넘기는 순차 목록(한 번 쓰고 비운다)
async function startOpenPoseWizard(stored, prompt){
  const sequence = wizardNextPoseSequence;
  wizardNextPoseSequence = null;
  document.getElementById('load-error').textContent = '';
  openNewJobModal();
  resetWizardAndWorkflow();
  resetForm();
  wizardPendingInputImage = null;
  wizardPendingPose = sequence ? { sequence } : { name: stored, prompt: (prompt || '').trim() };
  wizard.base = 'txt2img';
  wizard.post.openpose = true;
  wizard.batchMode = 'seed';
  try{ await fetchWorkflowTypes(); }catch(e){ /* 유형 이름 없이도 진행한다 */ }
  newJobActiveTab = 'select';
  wizardUpdateStepButtons();
  document.getElementById('load-notice').textContent = sequence
    ? `포즈 ${sequence.length}개를 목록 순서대로 생성해요. 베이스 모델을 고르고 "다음"을 누르세요.`
    : `'${stored}'을(를) 포즈 이미지로 골랐어요. 베이스 모델을 고르고 "다음"을 누르세요.`;
  await openWizardFamilyModal();
}

// Library "순차 생성"(NS-42) — 일괄 복사 API 응답(items, 목록 순서)을 기억하고 같은 OpenPose 마법사를 연다.
// 작업은 하나이고, seed_batch가 포즈마다 "포즈마다 생성 장수"만큼 돈다.
function startOpenPoseSequenceWizard(items){
  wizardNextPoseSequence = items;
  return startOpenPoseWizard(items[0].name, '');
}

// Library "프롬프트"(NS-43, NS-51부터 Pose·Position 공통) — 이미지는 넘기지 않고 danbooru prompt만 메인 프롬프트 끝에
// 붙이는 txt2img·시드 반복. wizardPendingPose.position은 원본 탭이 아니라 "프롬프트만" 방식의 표지다.
// sequence(장마다 한 항목)가 있으면 순차 실행 — 작업 하나에서 seed_batch가 항목마다 돈다. 크기는 마법사 기본값.
async function startPositionWizard(prompt, sequence = null){
  document.getElementById('load-error').textContent = '';
  openNewJobModal();
  resetWizardAndWorkflow();
  resetForm();
  wizardPendingInputImage = null;
  wizardPendingPose = sequence ? { position: true, sequence } : { position: true, prompt: (prompt || '').trim() };
  wizard.base = 'txt2img';
  wizard.post.openpose = false;
  wizard.batchMode = 'seed';
  try{ await fetchWorkflowTypes(); }catch(e){ /* 유형 이름 없이도 진행한다 */ }
  newJobActiveTab = 'select';
  wizardUpdateStepButtons();
  document.getElementById('load-notice').textContent = sequence
    ? `프롬프트 ${sequence.length}개를 목록 순서대로 생성해요. 베이스 모델을 고르고 "다음"을 누르세요.`
    : 'danbooru prompt를 메인 프롬프트에 붙여 생성해요. 베이스 모델을 고르고 "다음"을 누르세요.';
  await openWizardFamilyModal();
}
function startPositionSequenceWizard(items){ return startPositionWizard('', items); }

// "포즈 태그 자동 붙이기" 스위치(기본 켬) — 메인 프롬프트 칸 아래에 둔다. 누를 때마다 onChange(켜짐 여부).
function addPoseTagSwitch(raw, onChange){
  const label = document.createElement('label');
  label.className = 'pose-tag-toggle';
  label.innerHTML = '<button type="button" class="pod-switch" id="pose-tag-switch" role="switch" aria-checked="true" aria-label="포즈 태그 자동 붙이기"></button> 포즈 태그 자동 붙이기';
  const btn = label.querySelector('button');
  btn.addEventListener('click', () => {
    const on = btn.getAttribute('aria-checked') !== 'true';
    btn.setAttribute('aria-checked', String(on));
    onChange(on);
  });
  ((raw && raw.closest('.field')) || optionsFields).appendChild(label);
  return btn;
}

// 순차 모드 폼 — 포즈 칸을 숨기고, 시드 개수 칸을 "포즈마다 생성 장수"(1)로 바꾸고, 총 장수를 보여 준다.
// Position 순차(NS-43)는 noun='Position', toValue=positionSequenceValue로 같은 폼을 쓴다.
function applyPoseSequenceForm(items, poseEl, raw, noun = '포즈', toValue = poseSequenceValue){
  const poseField = poseEl && poseEl.closest('.field');
  if(poseField) poseField.hidden = true;
  const countEl = optionsFields.querySelector('[data-name="seed_count"]');
  if(countEl){
    countEl.value = '1';
    const label = countEl.closest('.field')?.querySelector('.field-label');
    if(label) label.textContent = `${noun}마다 생성 장수`;   // 포즈마다 생성 장수 / Position마다 생성 장수
  }
  const hint = document.createElement('div');
  hint.className = 'pose-sequence-hint';
  hint.id = 'pose-sequence-hint';
  const renderHint = () => {
    const per = Math.max(1, parseInt(countEl && countEl.value, 10) || 1);
    hint.innerHTML = `${noun} ${items.length}개 · 목록 순서대로 · <b>총 ${items.length * per}장</b>`;
  };
  renderHint();
  if(countEl) countEl.addEventListener('input', renderHint);
  if(poseField) poseField.after(hint); else optionsFields.prepend(hint);
  const tagSwitch = addPoseTagSwitch(raw, () => {});
  const seqEl = optionsFields.querySelector('[data-name="pose_sequence"]');
  danbooruPreSubmitHooks.push(() => {
    if(!seqEl) return;
    // width/height를 사용자가 직접 적었으면(자동 제안값이 아니면) 모든 포즈에 그 크기를 쓴다.
    const wEl = optionsFields.querySelector('[data-name="width"]');
    const hEl = optionsFields.querySelector('[data-name="height"]');
    const typed = el => el && el.value.trim() && el.dataset.poseAuto !== el.value.trim();
    const fixed = typed(wEl) && typed(hEl) ? { width: Number(wEl.value), height: Number(hEl.value) } : null;
    seqEl.value = toValue(items, tagSwitch.getAttribute('aria-checked') === 'true', fixed);   // poseSequenceValue(items, …)
  });
}

// 베이스 모델별 Face Detailer 기본값(GET /api/face-detailer-defaults)으로 spec.face_detailer를 만든다.
async function faceDetailerSpec(familyId, architecture){
  if(!familyId) return {};
  try{
    const res = await fetch(`/api/face-detailer-defaults/${encodeURIComponent(familyId)}?architecture=${encodeURIComponent(architecture)}`);
    return res.ok ? { ...(await res.json()).values } : {};
  }catch(e){ return {}; }
}

// 워크플로우(API 형식)에 FaceDetailer 노드가 있는지 — 있을 때만 얼굴 프롬프트 칸을 보인다.
function workflowHasFaceDetailer(workflow){
  if(!workflow || typeof workflow !== 'object') return false;
  return Object.values(workflow).some(n => n && typeof n === 'object' && n.class_type === 'FaceDetailer');
}

// 옵션 폼 컨테이너에 클래스만 붙인다 — 템플릿을 바꿔 칸을 다시 그려도 CSS(05-jobs.css)가 그대로 숨긴다.
function setFaceDetailerFieldsVisible(visible){
  optionsFields.classList.toggle('has-face-detailer', !!visible);
}

// family_kind가 있는 유형(openpose — "checkpoints")은 그 kind의 family에서만 맞는다. family를 안 골랐으면 막지 않는다.
function postFitsFamily(t, family){
  return !t.family_kind || !family || family.kind === t.family_kind;
}

// 이 베이스 모델에 연결된 OpenPose ControlNet 파일(서버가 셋팅 저장값 → openpose_pre → 알파벳순으로 정렬).
function openPoseControlNets(family){
  return ((family && family.controlnets) || {}).openpose || [];
}

// 베이스 모델별 OpenPose 기본값(GET /api/controlnet-defaults)으로 spec.controlnet을 만든다 — 평평한 값을
// 빌더 모양(ControlNet 수치 + dw)으로 나눈다. 고른 ControlNet 파일이 저장값보다 우선한다.
async function controlNetSpec(familyId, controlNetName){
  if(!controlNetName) throw new Error('이 베이스 모델에 연결된 OpenPose ControlNet이 없어요 — 모델 탭에서 연결해 주세요.');
  let values = {};
  try{
    const res = await fetch(`/api/controlnet-defaults/${encodeURIComponent(familyId)}`);
    if(res.ok) values = (await res.json()).values || {};
  }catch(e){ /* 빌더 기본값을 쓴다 */ }
  const { strength, start_percent, end_percent, control_net_name, ...dw } = values;
  const spec = { enabled: true, type: 'openpose', control_net_name: controlNetName, dw };
  for(const [k, v] of Object.entries({ strength, start_percent, end_percent })) if(v != null) spec[k] = v;
  return spec;
}

// 워크플로우에 DWPreprocessor가 있는지 — 있을 때만 포즈 이미지 칸과 OpenPose 패널을 보인다.
function workflowHasOpenPose(workflow){
  if(!workflow || typeof workflow !== 'object') return false;
  return Object.values(workflow).some(n => n && typeof n === 'object' && n.class_type === 'DWPreprocessor');
}
function setOpenPoseFieldsVisible(visible){
  optionsFields.classList.toggle('has-openpose', !!visible);
}

async function checkWorkflowCompatibility(file){
  const el = document.getElementById('workflow-check');
  const token = ++workflowCheckToken;
  updateNewJobTabs();
  setFaceDetailerFieldsVisible(false);
  renderFaceDetailerPanel(null);
  setOpenPoseFieldsVisible(false);
  renderOpenPosePanel(null);
  if(!file){
    el.hidden = true;
    el.textContent = '';
    return;
  }
  renderWorkflowCheck('unknown', '이 서버와 맞는지 확인하는 중…');
  let text;
  try{
    text = await file.text();
  }catch(e){
    if(token === workflowCheckToken) renderWorkflowCheck('warn', '파일을 읽지 못했어요.');
    return;
  }
  try{
    const parsed = JSON.parse(text);
    if(token === workflowCheckToken){
      setFaceDetailerFieldsVisible(workflowHasFaceDetailer(parsed));
      renderFaceDetailerPanel(parsed);
      setOpenPoseFieldsVisible(workflowHasOpenPose(parsed));
      renderOpenPosePanel(parsed);
    }
  }catch(e){
    if(token === workflowCheckToken) renderWorkflowCheck('warn', 'JSON 형식이 아니에요 — ComfyUI에서 "API 형식으로 저장"한 파일인지 확인해 주세요.');
    return;
  }
  let data;
  try{
    const res = await fetch(`/api/validate-workflow${podQuery()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    });
    data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '확인하지 못했어요.');
  }catch(e){
    if(token === workflowCheckToken) renderWorkflowCheck('unknown', `이 서버와 맞는지 확인하지 못했어요 — ${e.message}`);
    return;
  }
  if(token !== workflowCheckToken) return; // 그 사이 다른 파일을 골랐으면 버린다
  if(data.auto){
    // 파드를 아직 안 정한 건 잘못된 상태가 아니다(스케줄러가 나중에 배정) — 굳이
    // 안내를 띄우지 않고 조용히 넘어간다.
    el.hidden = true;
    el.textContent = '';
    return;
  }
  if(!data.connected){
    renderWorkflowCheck('unknown', 'ComfyUI에 연결되지 않아 이 워크플로우가 돌아갈지 확인하지 못했어요.');
    return;
  }
  if(data.ok){
    renderWorkflowCheck('ok', `이 서버에서 돌아갈 수 있어요 (노드 ${data.checked_nodes}개 확인)`);
    return;
  }
  const lines = [];
  if(data.missing_nodes.length > 0){
    lines.push(`이 서버에 없는 노드: ${summarizeList(data.missing_nodes)}`);
  }
  if(data.missing_values.length > 0){
    const labels = data.missing_values.map(v => `${v.field}="${v.value}"${availableOnText(v)}`);
    lines.push(`이 서버에 없는 모델/설정값: ${summarizeList(labels)}`);
  }
  lines.push('그대로 큐에 넣을 수는 있지만 실행 중 실패할 가능성이 높아요.');
  renderWorkflowCheck('warn', lines.join('\n'));
}

const EDITOR_KIND_LABEL = { workflow: '워크플로우', csv: 'CSV' };

let editingJobId = null;
let editingKind = 'workflow';
const workflowModal = document.getElementById('workflow-modal');
const workflowEditor = document.getElementById('workflow-editor');
const workflowEditorError = document.getElementById('workflow-editor-error');
const workflowSaveBtn = document.getElementById('workflow-save-btn');
const workflowModalTitle = document.getElementById('workflow-modal-title');

function computeLineCol(text, pos){
  const upto = text.slice(0, pos);
  const lines = upto.split('\n');
  return { line: lines.length, col: lines[lines.length - 1].length + 1 };
}

function validateWorkflowEditor(){
  const text = workflowEditor.value;
  try{
    JSON.parse(text);
    workflowEditor.classList.remove('invalid');
    workflowEditorError.textContent = '';
    workflowSaveBtn.disabled = false;
    return true;
  }catch(e){
    workflowEditor.classList.add('invalid');
    const lineColMatch = e.message.match(/line (\d+) column (\d+)/);
    const posMatch = e.message.match(/position (\d+)/);
    let where = '';
    if(lineColMatch){
      where = `${lineColMatch[1]}번째 줄, ${lineColMatch[2]}번째 열`;
    } else if(posMatch){
      const { line, col } = computeLineCol(text, Number(posMatch[1]));
      where = `${line}번째 줄, ${col}번째 열`;
    }
    workflowEditorError.textContent = where ? `${where} 근처에 JSON 문법 오류가 있어요.` : 'JSON 문법 오류가 있어요.';
    workflowSaveBtn.disabled = true;
    return false;
  }
}

async function openAttachmentEditor(jobId, kind){
  editingJobId = jobId;
  editingKind = kind;
  workflowModalTitle.textContent = `Edit ${kind === 'workflow' ? 'workflow' : 'CSV'} · ${jobId}`;
  workflowEditor.value = '불러오는 중…';
  workflowEditor.classList.remove('invalid');
  workflowEditorError.textContent = '';
  workflowSaveBtn.disabled = true;
  workflowModal.style.display = 'flex';

  try{
    const res = await fetch(`/api/jobs/${jobId}/${kind}`);
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      workflowEditor.value = '';
      workflowEditorError.textContent = data.detail || `${EDITOR_KIND_LABEL[kind]}를 불러오지 못했어요.`;
      return;
    }
    workflowEditor.value = await res.text();
    if(kind === 'workflow') validateWorkflowEditor();
    else workflowSaveBtn.disabled = false;
  }catch(e){
    workflowEditor.value = '';
    workflowEditorError.textContent = `${EDITOR_KIND_LABEL[kind]}를 불러오지 못했어요.`;
  }
}

function closeWorkflowEditor(){
  workflowModal.style.display = 'none';
  editingJobId = null;
}

workflowEditor.addEventListener('input', () => {
  if(editingKind === 'workflow') validateWorkflowEditor();
});
document.getElementById('workflow-modal-close').addEventListener('click', closeWorkflowEditor);
document.getElementById('workflow-cancel-btn').addEventListener('click', closeWorkflowEditor);
workflowModal.addEventListener('click', (e) => { if(e.target === workflowModal) closeWorkflowEditor(); });
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && workflowModal.style.display !== 'none') closeWorkflowEditor();
});

workflowSaveBtn.addEventListener('click', async () => {
  if(!editingJobId) return;
  if(editingKind === 'workflow' && !validateWorkflowEditor()) return;
  workflowSaveBtn.disabled = true;
  try{
    const res = await fetch(`/api/jobs/${editingJobId}/${editingKind}`, {
      method: 'PUT',
      headers: { 'Content-Type': editingKind === 'workflow' ? 'application/json' : 'text/csv' },
      body: workflowEditor.value,
    });
    if(!res.ok){
      const data = await res.json().catch(() => ({}));
      workflowEditorError.textContent = data.detail || '저장에 실패했어요.';
      workflowSaveBtn.disabled = false;
      return;
    }
    closeWorkflowEditor();
    fetchJobs();
  }catch(e){
    workflowEditorError.textContent = '저장에 실패했어요.';
    workflowSaveBtn.disabled = false;
  }
});

