// 불러온 워크플로우 읽기: 그래프·template_id·저장 옵션을 마법사 상태로 바꾸고, 못 읽는 구조는 읽기 전용 요약으로 보여 준다
function workflowNodes(workflow){
  if(!workflow || typeof workflow !== 'object') return [];
  return Object.values(workflow).filter(n => n && typeof n === 'object' && n.class_type);
}

function workflowNodeTitle(node){ return (node._meta && node._meta.title) || ''; }

// 실제로 쓰인 모델·LoRA. 저장 옵션의 checkpoint는 체크포인트 로더에만 적용되고(seed_batch.py),
// lora_name·lora_strength는 첫 LoRA 로더에 덮어써지므로 그 값으로 보여 준다.
function workflowModelInfo(workflow, options){
  const saved = options || {};
  const nodes = workflowNodes(workflow);
  const override = String(saved.checkpoint || '').trim();
  const checkpoints = nodes.filter(n => n.class_type === 'CheckpointLoaderSimple')
    .map(n => override || (n.inputs || {}).ckpt_name);
  const unets = nodes.filter(n => n.class_type === 'UNETLoader').map(n => (n.inputs || {}).unet_name);
  const models = [...checkpoints, ...unets].filter(name => name && typeof name === 'string');
  const loras = nodes
    .filter(n => n.class_type === 'LoraLoader' || n.class_type === 'LoraLoaderModelOnly')
    .map(n => ({
      name: (n.inputs || {}).lora_name,
      strength: typeof (n.inputs || {}).strength_model === 'number' ? n.inputs.strength_model : 1,
    }))
    .filter(l => l.name && typeof l.name === 'string');
  if(loras.length){
    const name = String(saved.lora_name || '').trim();
    const strength = parseFloat(saved.lora_strength);
    if(name) loras[0].name = name;
    if(Number.isFinite(strength)) loras[0].strength = strength;
  }
  return { models, loras };
}

function templateModeOf(type, templateId){
  const entry = Object.entries((type && type.template_ids) || {}).find(([, id]) => id === templateId);
  return entry ? entry[0] : null;
}

// krea.2·MiniMax-H3처럼 전용 빌더가 있는 family의 유형. 모르는 모양이면 null.
function familyBaseState(nodes, familyBases){
  const has = classType => nodes.some(n => n.class_type === classType);
  const titled = pattern => nodes.filter(n => pattern.test(workflowNodeTitle(n)));
  const pick = (id, extra) => familyBases.some(t => t.id === id) ? { base: id, ...extra } : null;
  const krea = familyBases.find(t => t.architecture === 'krea2');
  if(krea) return { base: krea.id, refinePrompt: has('TextGenerate') };

  const refVideos = titled(/^ref_video_\d+$/);
  const refImageCount = titled(/^ref_image_\d+$/).length;
  const refAudioCount = titled(/^ref_audio_\d+$/).length;
  if(has('MiniMaxH3ReferenceToVideo') || refImageCount + refVideos.length + refAudioCount > 0){
    return pick('minimax_h3_r2v', {
      refImageCount, refVideoCount: refVideos.length, refAudioCount,
      videoRefMode: refVideos.some(n => n.class_type === 'LoadVideo') ? 'native' : 'vhs',
    });
  }
  const useFirstFrame = titled(/^first_frame_image$/).length > 0;
  const useLastFrame = titled(/^last_frame_image$/).length > 0;
  if(useFirstFrame || useLastFrame) return pick('minimax_h3_i2v', { useFirstFrame, useLastFrame });
  if(!has('MiniMaxH3ImageToVideo')) return null;
  const size = title => {
    const node = nodes.find(n => n.class_type === 'PrimitiveInt' && workflowNodeTitle(n) === title);
    return node && (node.inputs || {}).value;
  };
  const state = { useFirstFrame: false, useLastFrame: false };
  if(typeof size('width') === 'number') state.t2vWidth = size('width');
  if(typeof size('height') === 'number') state.t2vHeight = size('height');
  return pick('minimax_h3_t2v', state);
}

// 범용 유형(txt2img/img2img/face_detailer)과 후처리. 베이스 패스를 못 알아보면 null.
function genericBaseState(workflow, nodes){
  const hasEmptyLatent = nodes.some(n => n.class_type === 'EmptyLatentImage');
  const hasInputImage = nodes.some(n => n.class_type === 'LoadImage' && workflowNodeTitle(n) === 'input_image');
  // KSampler 없이 입력 이미지가 FaceDetailer로만 가면 Face Detailer 유형(base="face_detailer")이다.
  const hasKSampler = nodes.some(n => n.class_type === 'KSampler');
  const hasFaceDetailer = workflowHasFaceDetailer(workflow);
  const base = hasEmptyLatent ? 'txt2img'
    : !hasInputImage ? null
    : (hasKSampler || !hasFaceDetailer) ? 'img2img' : 'face_detailer';
  if(!base) return null;

  const post = freshWizardPost();
  const upscaleNode = nodes.find(n => n.class_type === 'LatentUpscaleBy');
  if(nodes.some(n => n.class_type === 'KSampler' && workflowNodeTitle(n) === 'KSampler (hires-fix)')){
    post.hires_fix = true;
    if(upscaleNode && typeof (upscaleNode.inputs || {}).scale_by === 'number') post.hiresScale = upscaleNode.inputs.scale_by;
  }
  post.usdu = nodes.some(n => n.class_type === 'UltimateSDUpscaleNoUpscale');
  // base가 face_detailer면 FaceDetailer는 베이스 자체라 후처리로 켜지 않는다.
  post.face_detailer = hasFaceDetailer && base !== 'face_detailer';
  return { base, post };
}

// 워크플로우를 마법사 상태로 읽는다. 마법사가 다루지 않는 구조나 모르는 모델이면 null —
// 억지로 끼워 맞추면 오히려 헷갈린다. batchMode는 template_id를 모르면 null이다.
function wizardStateFromWorkflow(workflow, { families, catalog, templateId, options } = {}){
  const nodes = workflowNodes(workflow);
  const { models, loras } = workflowModelInfo(workflow, options);
  if(models.length !== 1) return null;
  const checkpoint = models[0];
  const match = Object.entries(families || {}).find(([, fam]) => (fam.checkpoints || []).includes(checkpoint));
  if(!match) return null;
  const [familyId, family] = match;
  const types = catalog || {};

  const clipNode = nodes.find(n => n.class_type === 'CLIPLoader');
  const clipName = clipNode && (clipNode.inputs || {}).clip_name;
  const state = {
    familyId, checkpoint, base: null, preset: null, post: freshWizardPost(), loras, batchMode: null,
    textEncoder: (family.text_encoders || []).includes(clipName) ? clipName : null,
  };

  const preset = (types.preset || []).find(t => templateModeOf(t, templateId));
  if(preset){
    state.preset = preset.id;
    state.batchMode = templateModeOf(preset, templateId);
    return state;
  }
  const familyBases = (types.base || []).filter(t => t.family_id === familyId);
  if(!familyBases.length && family.kind === 'diffusion_models' && !family.unet_image) return null;
  const typeState = familyBases.length ? familyBaseState(nodes, familyBases) : genericBaseState(workflow, nodes);
  if(!typeState) return null;
  Object.assign(state, typeState);
  state.batchMode = templateModeOf((types.base || []).find(t => t.id === state.base), templateId);
  return state;
}

// 고른 파드에 없는 family를 전체 등록 목록에서 찾았을 때 합친다 — 그래야 2단계 라벨이 깨지지 않는다.
function mergeBaseModelFamilies(current, all, familyId){
  const have = current[familyId], found = all[familyId];
  if(!have) return { ...current, [familyId]: found };
  const checkpoints = [...new Set([...(have.checkpoints || []), ...(found.checkpoints || [])])];
  return { ...current, [familyId]: { ...have, checkpoints } };
}

async function fetchAllBaseModelFamilies(){
  try{
    const res = await fetch('/api/base-model-families?pod_id=auto');
    return res.ok ? await res.json() : {};
  }catch(e){
    return {};
  }
}

// 마법사 LoRA 목록 — 불러온 LoRA가 목록에 없어도 강도를 바꾸거나 해제할 수 있게 맨 앞에 넣는다.
function wizardLoraListNames(compatible, loras){
  return [...loras.map(l => l.name).filter(name => !compatible.includes(name)), ...compatible];
}

function workflowSummaryLines(workflow, options){
  const { models, loras } = workflowModelInfo(workflow, options);
  if(!models.length && !loras.length) return [];
  return [
    ...models.map(name => `모델: ${name}`),
    ...(loras.length ? loras.map(l => `LoRA: ${l.name} (강도 ${l.strength})`) : ['LoRA: 없음']),
    '마법사가 다루지 않는 구조라 여기서는 고칠 수 없어요',
  ];
}

function renderWorkflowLoadedSummary(lines){
  const el = document.getElementById('workflow-loaded-summary');
  el.hidden = !lines.length;
  el.textContent = lines.join('\n');
}

// ---- 불러온 작업 수정: 제자리 패치 또는 옵션을 보존한 재조립 ----
function wizardMarkLoaded(){
  wizardAppliedSignature = JSON.stringify(wizard);
  wizardLoadedState = JSON.parse(wizardAppliedSignature);
}

// 재조립이 필요한 값만 남긴 비교용 문자열. 꺼진 값과 없는 값은 같게 본다(후처리를 켰다 끄면 false 키가 남는다).
function wizardStructureKey(state){
  const { checkpoint, textEncoder, loras, post, ...rest } = state;
  const { hiresScale, ...flags } = post || {};
  return JSON.stringify(Object.entries({ ...rest, ...flags }).filter(([, value]) => value).sort(([a], [b]) => a.localeCompare(b)));
}

function wizardLorasChanged(before, after){
  const key = state => JSON.stringify((state.loras || []).map(l => [l.name, l.strength]));
  return key(before) !== key(after);
}

function wizardHiresChanged(before, after){
  return !!(after.post || {}).hires_fix && before.post.hiresScale !== after.post.hiresScale;
}

// 불러온 뒤 마법사가 어떻게 바뀌었나 — 'same'(그대로), 'patch'(제자리 패치), 'rebuild'(재조립).
function wizardLoadedChange(before, after){
  if(wizardStructureKey(before) !== wizardStructureKey(after)) return 'rebuild';
  const patched = before.checkpoint !== after.checkpoint || (before.textEncoder || null) !== (after.textEncoder || null)
    || wizardHiresChanged(before, after) || wizardLorasChanged(before, after);
  return patched ? 'patch' : 'same';
}

// 패치 뒤 런타임 덮어쓰기 칸을 비우므로, 칸에 값이 있으면 바뀌지 않았어도 워크플로우에 구워 넣는다.
function wizardPatchValues(before, after, { modelOnly, overrides }){
  const values = {};
  if(before.checkpoint !== after.checkpoint || overrides.checkpoint) values.checkpoint = after.checkpoint;
  if(after.textEncoder && before.textEncoder !== after.textEncoder) values.clip = after.textEncoder;
  if(wizardHiresChanged(before, after)) values.hires_scale = after.post.hiresScale;
  if(wizardLorasChanged(before, after) || overrides.lora){
    values.loras = after.loras.map(l => ({ name: l.name, strength: l.strength }));
    values.model_only = modelOnly;
  }
  return values;
}

// 제거된 LoRA의 트리거를 메인 프롬프트 앞에서 빼고, 손대지 못한 CSV 행에 대한 안내를 돌려준다.
function dropRemovedLoraTriggers(beforeLoras){
  const { removed, kept } = removedLoraTriggers(beforeLoras, wizard.loras, loraTriggerFor);
  if(!removed.length) return '';
  const control = qualityMainControl();
  if(control){
    const input = control.get();
    input.value = stripLoraTriggers(input.value, removed, kept);
    control.sync();
  }
  return selectedFiles.csv ? `CSV 행의 트리거 문구(${removed.join(', ')})는 그대로 뒀어요 — 필요하면 직접 지워 주세요.` : '';
}

// 지금 워크플로우 파일을 기준으로 고치므로 Face Detailer·OpenPose 패널에서 고친 값이 남는다.
async function wizardPatchLoaded(){
  const errorEl = document.getElementById('wizard-error');
  errorEl.textContent = '';
  const file = selectedFiles.workflow;
  const cell = name => optionsFields.querySelector(`[data-name="${name}"]`);
  const filled = name => !!String((cell(name) || {}).value || '').trim();
  const architecture = (wizardBaseMeta(wizard.base) || {}).architecture || '';
  const values = wizardPatchValues(wizardLoadedState, wizard, {
    modelOnly: architecture === 'krea2' || architecture.startsWith('minimax_h3_'),
    overrides: { checkpoint: filled('checkpoint'), lora: filled('lora_name') || filled('lora_strength') },
  });
  let data;
  try{
    data = await fdPost('/api/workflow/apply-models', { workflow: JSON.parse(await file.text()), values });
  }catch(e){
    errorEl.textContent = e.message;
    return false;
  }
  setWorkflowFile(new File([JSON.stringify(data.workflow)], file.name, { type: 'application/json' }));
  for(const name of ['checkpoint', 'lora_name', 'lora_strength']) if(cell(name)) cell(name).value = '';
  const csvNote = dropRemovedLoraTriggers(wizardLoadedState.loras);
  setLoraTriggerField(wizard.loras.map(l => l.name));
  lastWizardModels = { checkpoint: wizard.checkpoint, loras: wizard.loras.map(l => l.name) };
  wizardMarkLoaded();
  document.getElementById('load-notice').textContent =
    ['고른 모델·LoRA를 워크플로우에 반영했어요 — 다른 설정은 그대로예요.', csvNote].filter(Boolean).join('\n');
  return true;
}

// checkpoint·lora_*는 재조립한 상태로 wizardApply가 새로 채우고, main_prompt는 입력칸째 따로 복원한다.
const WIZARD_REBUILT_OPTIONS = ['checkpoint', 'lora_name', 'lora_strength', 'main_prompt'];

function wizardOptionDraft(){
  const values = {};
  for(const el of optionsFields.querySelectorAll('.option-input')) values[el.dataset.name] = el.value;
  return {
    values, prompt: qualityMainControl()?.snapshot(),
    csv: selectedFiles.csv, videoWorkflow: selectedFiles.video_workflow,
  };
}

function restoreWizardOptions(values){
  for(const el of optionsFields.querySelectorAll('.option-input')){
    const name = el.dataset.name;
    if(!Object.prototype.hasOwnProperty.call(values, name) || WIZARD_REBUILT_OPTIONS.includes(name)) continue;
    if(name === 'pose_image'){
      setPoseImageValue(el, values[name]);
    }else if(el.tagName !== 'SELECT'){
      el.value = values[name];
    }else if(selectHasOptionValue(el, values[name])){
      el.value = values[name];
      el.dispatchEvent(new Event('change'));   // char_no 뒤의 asset_folder 목록을 다시 채운다
    }
  }
}

async function wizardApplyKeepingOptions(){
  const before = wizardLoadedState;
  const draft = wizardOptionDraft();
  if(!await wizardApply()) return false;
  restoreWizardOptions(draft.values);
  if(draft.prompt) qualityMainControl()?.restore(draft.prompt);
  const template = templatesById[templateSelect.value] || {};
  if(template.requires_csv && draft.csv) setCsvFile(draft.csv);
  if(template.optional_video_workflow && draft.videoWorkflow) setVideoWorkflowFile(draft.videoWorkflow);
  const csvNote = dropRemovedLoraTriggers(before.loras);
  wizardMarkLoaded();
  const notice = document.getElementById('load-notice');
  notice.textContent = [notice.textContent, '샘플러·Face Detailer 수치는 베이스 모델 기본값으로 다시 만들어졌어요.', csvNote].filter(Boolean).join('\n');
  return true;
}
