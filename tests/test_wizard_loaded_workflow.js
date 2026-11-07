// 불러온 워크플로우를 마법사 상태로 읽는 순수 함수와 loadJobSettings 연결을 검사한다(NS-122-2).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
const wizardSource = source('06-wizard.js');
const refs = source('03-newjob-refs-inputs.js');
const fnOf = (src, name) => src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n}\\r?\\n`))[0];

const context = vm.createContext({
  freshWizardPost: () => ({ hires_fix: false, face_detailer: false, usdu: false, hiresScale: 1.5, prompt_enhance: false }),
});
vm.runInContext(fnOf(wizardSource, 'workflowHasFaceDetailer') + source('06b-wizard-loaded.js'), context);
const { wizardStateFromWorkflow, workflowSummaryLines, workflowModelInfo, wizardLoraListNames, mergeBaseModelFamilies } = context;
// vm 안에서 만든 객체도 deepEqual로 비교할 수 있게 평범한 값으로 바꾼다.
const plain = value => JSON.parse(JSON.stringify(value));

const catalog = {
  base: [
    { id: 'txt2img', template_ids: { seed: 'seed_batch', csv: 'csv_batch' } },
    { id: 'img2img', template_ids: { seed: 'input_image_batch', csv: 'input_image_csv_batch' } },
    { id: 'face_detailer', template_ids: { seed: 'input_image_batch', csv: 'input_image_csv_batch' } },
    { id: 'krea2_t2i', family_id: 'krea.2', architecture: 'krea2', template_ids: { seed: 'seed_batch', csv: 'csv_batch' } },
    { id: 'minimax_h3_t2v', family_id: 'minimax-h3', architecture: 'minimax_h3_i2v', template_ids: { seed: 'minimax_h3_i2v_batch', csv: 'minimax_h3_i2v_csv_batch' } },
    { id: 'minimax_h3_i2v', family_id: 'minimax-h3', architecture: 'minimax_h3_i2v', template_ids: { seed: 'minimax_h3_i2v_batch', csv: 'minimax_h3_i2v_csv_batch' } },
    { id: 'minimax_h3_r2v', family_id: 'minimax-h3', architecture: 'minimax_h3_r2v', template_ids: { seed: 'minimax_h3_r2v_batch', csv: 'minimax_h3_r2v_csv_batch' } },
  ],
  preset: [{ id: 'depth_cn', template_ids: { seed: 'depth_batch', csv: 'depth_csv_batch' } }],
};
const families = {
  illustrious: { label: 'Illustrious', kind: 'checkpoints', checkpoints: ['ill.safetensors', 'ill2.safetensors'] },
  'krea.2': { label: 'krea.2', kind: 'diffusion_models', checkpoints: ['krea2.safetensors'], text_encoders: ['krea_te.safetensors', 'krea_te2.safetensors'] },
  anima: { label: 'Anima', kind: 'diffusion_models', checkpoints: ['anima.safetensors'], text_encoders: ['qwen.safetensors'], unet_image: { clip: 'qwen.safetensors', vae: 'v.safetensors' } },
  'minimax-h3': { label: 'MiniMax-H3', kind: 'diffusion_models', checkpoints: ['fl2va.safetensors', 'ref2va.safetensors'], text_encoders: [] },
  wan: { label: 'WAN', kind: 'diffusion_models', checkpoints: ['wan_high.safetensors', 'wan_low.safetensors'], text_encoders: [] },
};
const node = (class_type, inputs, title = '') => ({ class_type, inputs, _meta: { title } });
const graph = (...nodes) => Object.fromEntries(nodes.map((n, i) => [String(i + 1), n]));
const read = (workflow, extra = {}) => {
  const state = wizardStateFromWorkflow(workflow, { families, catalog, ...extra });
  return state && plain(state);
};

const unet = name => node('UNETLoader', { unet_name: name, weight_dtype: 'default' });
const onlyLora = (name, strength) => node('LoraLoaderModelOnly', { lora_name: name, strength_model: strength, model: ['1', 0] });
const krea = refine => graph(
  unet('krea2.safetensors'), node('CLIPLoader', { clip_name: 'krea_te2.safetensors', type: 'krea2' }),
  onlyLora('style.safetensors', 0.7), onlyLora('detail.safetensors', 1.2),
  ...(refine ? [node('TextGenerate', {})] : []), node('EmptyLatentImage', { width: 1024, height: 1024 }),
);

test('krea.2: UNet·LoraLoaderModelOnly·텍스트 인코더·refinePrompt·실행 방식', () => {
  const state = read(krea(true), { templateId: 'csv_batch' });
  assert.equal(state.familyId, 'krea.2');
  assert.equal(state.checkpoint, 'krea2.safetensors');
  assert.equal(state.base, 'krea2_t2i');
  assert.equal(state.textEncoder, 'krea_te2.safetensors');
  assert.equal(state.refinePrompt, true);
  assert.equal(state.batchMode, 'csv');
  assert.deepEqual(state.loras, [{ name: 'style.safetensors', strength: 0.7 }, { name: 'detail.safetensors', strength: 1.2 }]);
  assert.equal(read(krea(false)).refinePrompt, false);
});

test('Anima: 범용 txt2img이고 family에 없는 텍스트 인코더는 비운다', () => {
  const workflow = graph(unet('anima.safetensors'), node('CLIPLoader', { clip_name: 'qwen.safetensors' }),
    node('LoraLoader', { lora_name: 'a.safetensors', strength_model: 0.5, strength_clip: 0.5 }), node('EmptyLatentImage', {}));
  const state = read(workflow, { templateId: 'seed_batch' });
  assert.equal(state.familyId, 'anima');
  assert.equal(state.base, 'txt2img');
  assert.equal(state.textEncoder, 'qwen.safetensors');
  assert.equal(state.batchMode, 'seed');
  workflow['2'].inputs.clip_name = 'other.safetensors';
  assert.equal(read(workflow).textEncoder, null);
});

test('체크포인트형: 후처리, img2img, Face Detailer 유형', () => {
  const ckpt = node('CheckpointLoaderSimple', { ckpt_name: 'ill.safetensors' });
  const state = read(graph(ckpt, node('EmptyLatentImage', {}), node('KSampler', {}), node('KSampler', {}, 'KSampler (hires-fix)'),
    node('LatentUpscaleBy', { scale_by: 2 }), node('FaceDetailer', {}), node('UltimateSDUpscaleNoUpscale', {})), { templateId: 'seed_batch' });
  assert.equal(state.base, 'txt2img');
  assert.deepEqual(state.post, { hires_fix: true, face_detailer: true, usdu: true, hiresScale: 2, prompt_enhance: false });
  const input = node('LoadImage', { image: '' }, 'input_image');
  assert.equal(read(graph(ckpt, input, node('KSampler', {})), { templateId: 'input_image_csv_batch' }).base, 'img2img');
  const detailer = read(graph(ckpt, input, node('FaceDetailer', {})));
  assert.equal(detailer.base, 'face_detailer');
  assert.equal(detailer.post.face_detailer, false);
});

test('preset은 template_id로 찾는다', () => {
  const state = read(graph(node('CheckpointLoaderSimple', { ckpt_name: 'ill.safetensors' }), node('EmptyLatentImage', {})), { templateId: 'depth_csv_batch' });
  assert.equal(state.preset, 'depth_cn');
  assert.equal(state.base, null);
  assert.equal(state.batchMode, 'csv');
});

test('MiniMax: r2v 칸 수, i2v 프레임, t2v 크기', () => {
  const r2v = read(graph(unet('ref2va.safetensors'), node('LoadImage', {}, 'ref_image_1'), node('LoadImage', {}, 'ref_image_2'),
    node('LoadImage', {}, 'ref_image_3'), node('LoadVideo', {}, 'ref_video_1'), node('GetVideoComponents', {}, 'ref_video_1_components'),
    node('LoadAudio', {}, 'ref_audio_1'), node('MiniMaxH3ReferenceToVideo', {})), { templateId: 'minimax_h3_r2v_csv_batch' });
  assert.equal(r2v.base, 'minimax_h3_r2v');
  assert.deepEqual([r2v.refImageCount, r2v.refVideoCount, r2v.refAudioCount], [3, 1, 1]);
  assert.equal(r2v.videoRefMode, 'native');
  assert.equal(r2v.batchMode, 'csv');
  const i2v = read(graph(unet('fl2va.safetensors'), node('LoadImage', {}, 'last_frame_image'), node('MiniMaxH3ImageToVideo', {})));
  assert.equal(i2v.base, 'minimax_h3_i2v');
  assert.deepEqual([i2v.useFirstFrame, i2v.useLastFrame], [false, true]);
  const t2v = read(graph(unet('fl2va.safetensors'), node('PrimitiveInt', { value: 512 }, 'width'),
    node('PrimitiveInt', { value: 900 }, 'height'), node('MiniMaxH3ImageToVideo', {})));
  assert.equal(t2v.base, 'minimax_h3_t2v');
  assert.deepEqual([t2v.useFirstFrame, t2v.useLastFrame, t2v.t2vWidth, t2v.t2vHeight], [false, false, 512, 900]);
});

test('저장 옵션 덮어쓰기는 실제로 적용되는 곳에만 표시한다', () => {
  const ckptGraph = graph(node('CheckpointLoaderSimple', { ckpt_name: 'ill.safetensors' }),
    node('LoraLoader', { lora_name: 'old.safetensors', strength_model: 1 }), node('LoraLoader', { lora_name: 'second.safetensors', strength_model: 0.4 }),
    node('EmptyLatentImage', {}));
  const options = { checkpoint: 'ill2.safetensors', lora_name: 'new.safetensors', lora_strength: '0.65' };
  const state = read(ckptGraph, { options, templateId: 'seed_batch' });
  assert.equal(state.checkpoint, 'ill2.safetensors');
  assert.deepEqual(state.loras, [{ name: 'new.safetensors', strength: 0.65 }, { name: 'second.safetensors', strength: 0.4 }]);
  // UNet 로더에는 checkpoint 옵션이 적용되지 않는다.
  assert.equal(read(krea(true), { options: { checkpoint: 'ill.safetensors' } }).checkpoint, 'krea2.safetensors');
  // 빈 옵션과 LoRA 없는 워크플로우는 그대로다.
  assert.equal(read(ckptGraph, { options: { checkpoint: '', lora_name: '', lora_strength: '' } }).loras[0].strength, 1);
  assert.equal(plain(workflowModelInfo(graph(unet('krea2.safetensors')), { lora_name: 'x' })).loras.length, 0);
});

test('모르는 구조는 null이고 요약에 모델·LoRA·강도·이유가 나온다', () => {
  const wan = graph(unet('wan_high.safetensors'), unet('wan_low.safetensors'), onlyLora('w.safetensors', 0.8));
  assert.equal(read(wan), null);
  assert.equal(read(graph(unet('wan_high.safetensors'), node('EmptyLatentImage', {}))), null);   // 전용 빌더도 unet_image도 없는 family
  assert.equal(read(graph(node('CheckpointLoaderSimple', { ckpt_name: 'unknown.safetensors' }), node('EmptyLatentImage', {}))), null);
  assert.equal(read(graph(node('CheckpointLoaderSimple', { ckpt_name: 'ill.safetensors' }))), null);
  assert.equal(read(graph(unet('fl2va.safetensors'))), null);
  assert.equal(read(null), null);
  assert.deepEqual(plain(workflowSummaryLines(wan)), [
    '모델: wan_high.safetensors', '모델: wan_low.safetensors', 'LoRA: w.safetensors (강도 0.8)',
    '마법사가 다루지 않는 구조라 여기서는 고칠 수 없어요',
  ]);
  assert.deepEqual(plain(workflowSummaryLines({})), []);
});

test('실행 방식은 template_id를 모르면 null이다', () => {
  assert.equal(read(krea(true), { templateId: 'img2video_i2v_csv_batch' }).batchMode, null);
  assert.equal(read(krea(true)).batchMode, null);
});

test('다른 워커의 family는 합치고, 목록에 없는 LoRA는 맨 앞에 넣는다', () => {
  const current = { a: { label: 'A', checkpoints: ['x'] } };
  const all = { a: { label: 'A', checkpoints: ['x', 'y'] }, b: { label: 'B', checkpoints: ['z'] } };
  assert.deepEqual(plain(mergeBaseModelFamilies(current, all, 'b')).b, all.b);
  assert.deepEqual(plain(mergeBaseModelFamilies(current, all, 'a')).a.checkpoints, ['x', 'y']);
  assert.deepEqual(plain(wizardLoraListNames(['a', 'b'], [{ name: 'b' }, { name: 'gone' }])), ['gone', 'a', 'b']);
});

// ---- NS-122-3: 수정 시 패치·옵션 보존 재조립 ----
const tools = source('04-newjob-tools.js');
const element = (name, value, extra = {}) => ({ dataset: { name }, value, tagName: 'INPUT', ...extra });

// 실제 goToNewJobDetails와 패치·재조립 함수를 모의 화면에서 돌린다.
function editHarness({ state, file = '{"1":{"class_type":"FaceDetailer"}}', options = [], template = { requires_csv: true }, apply, fail, csv = null }){
  const dom = { 'load-notice': { textContent: '' }, 'wizard-error': { textContent: '' }, 'new-job-tabs': { scrollIntoView(){} } };
  const calls = { patch: [], apply: 0, files: [], csv: [], triggers: [], synced: 0 };
  const promptInput = { value: 'old, body' };
  const control = {
    get: () => promptInput, sync: () => { calls.synced++; },
    snapshot: () => ({ raw: promptInput.value }), restore: draft => { promptInput.value = draft.raw; },
  };
  const harness = vm.createContext({
    JSON, Object, String, File: class { constructor(parts, name){ this.body = parts[0]; this.name = name; } text(){ return Promise.resolve(this.body); } },
    Event: class { constructor(type){ this.type = type; } },
    document: { getElementById: id => dom[id] },
    optionsFields: { querySelectorAll: () => options, querySelector: selector => options.find(el => selector.includes(`"${el.dataset.name}"`)) || null },
    templateSelect: { value: 'next' }, templatesById: { next: template },
    selectedFiles: { workflow: { name: 'job.json', text: () => Promise.resolve(file) }, csv },
    wizard: JSON.parse(JSON.stringify(state)), wizardLoadedState: null, wizardAppliedSignature: null, lastWizardModels: null,
    wizardQuality: { key: '' }, quickRunReturn: false, newJobActiveTab: 'select',
    newJobReadyToAdvance: () => true, renderNewJobTabs(){},
    wizardStale: () => harness.wizardAppliedSignature !== JSON.stringify(harness.wizard) && !!harness.wizard.batchMode,
    wizardBaseMeta: () => ({ architecture: 'krea2' }), qualityMainControl: () => control,
    loraTriggerFor: name => ({ 'old.safetensors': 'old', 'twin.safetensors': 'same', 'twin2.safetensors': 'same' })[name] || '',
    setWorkflowFile: f => { harness.selectedFiles.workflow = f; calls.files.push(f); },
    setCsvFile: f => { calls.csv.push(f); }, setVideoWorkflowFile(){},
    setLoraTriggerField: names => { calls.triggers.push(names); },
    selectHasOptionValue: (el, value) => el.choices.includes(value), setPoseImageValue(el, value){ el.value = value; },
    fdPost: async (url, body) => {
      calls.patch.push(body);
      if(fail) throw new Error(fail);
      return { workflow: { ...body.workflow, patched: true } };
    },
    wizardApply: async () => {
      calls.apply++;
      dom['load-notice'].textContent = '마법사 설정을 적용했어요';
      for(const el of options) el.value = el.dataset.name === 'checkpoint' ? 'rebuilt.safetensors' : '';
      promptInput.value = '';
      return apply !== false;
    },
  });
  vm.runInContext(fnOf(tools, 'removedLoraTriggers') + fnOf(tools, 'stripLoraTriggers') + source('06b-wizard-loaded.js')
    + `async ${fnOf(wizardSource, 'goToNewJobDetails')}`, harness);
  harness.wizardMarkLoaded();
  return { harness, dom, calls, promptInput };
}
const loadedState = {
  familyId: 'krea.2', checkpoint: 'a.safetensors', textEncoder: 'te1', base: 'krea2_t2i', preset: null, batchMode: 'seed',
  post: { hires_fix: true, hiresScale: 1.5, usdu: false }, loras: [{ name: 'old.safetensors', strength: 1 }], refinePrompt: true,
};
const edited = change => {
  const after = JSON.parse(JSON.stringify(loadedState));
  change(after);
  return context.wizardLoadedChange(loadedState, after);
};

test('분기 판정: 모델·인코더·Hires 배율·LoRA만 바뀌면 패치, 구조가 바뀌면 재조립', () => {
  assert.equal(edited(() => {}), 'same');
  assert.equal(edited(s => { s.post.usdu = false; s.post.openpose = false; }), 'same');
  assert.equal(edited(s => { s.post.hires_fix = false; s.post.hiresScale = 2; }), 'rebuild');
  for(const change of [s => { s.checkpoint = 'b'; }, s => { s.textEncoder = 'te2'; }, s => { s.post.hiresScale = 2; },
    s => { s.loras[0].strength = 0.5; }, s => { s.loras = []; }]){
    assert.equal(edited(change), 'patch');
  }
  for(const change of [s => { s.base = 'txt2img'; }, s => { s.post.usdu = true; }, s => { s.batchMode = 'csv'; },
    s => { s.familyId = 'anima'; }, s => { s.preset = 'depth_cn'; }, s => { s.refinePrompt = false; }]){
    assert.equal(edited(change), 'rebuild');
  }
});

test('패치 값은 바뀐 항목만 보내고, 비워질 덮어쓰기 칸의 값은 구워 넣는다', () => {
  const after = JSON.parse(JSON.stringify(loadedState));
  after.loras[0].strength = 0.5;
  const none = { checkpoint: false, lora: false };
  assert.deepEqual(plain(context.wizardPatchValues(loadedState, after, { modelOnly: true, overrides: none })),
    { loras: [{ name: 'old.safetensors', strength: 0.5 }], model_only: true });
  assert.deepEqual(plain(context.wizardPatchValues(loadedState, loadedState, { modelOnly: false, overrides: { checkpoint: true, lora: true } })),
    { checkpoint: 'a.safetensors', loras: [{ name: 'old.safetensors', strength: 1 }], model_only: false });
  after.post.hiresScale = 2; after.textEncoder = 'te2';
  const values = context.wizardPatchValues(loadedState, after, { modelOnly: false, overrides: none });
  assert.equal(values.hires_scale, 2);
  assert.equal(values.clip, 'te2');
});

test('트리거 제거: 맨 앞 묶음에서 빠진 LoRA 것만 빼고 같은 트리거를 쓰는 LoRA가 남으면 유지한다', () => {
  const trigger = name => ({ a: 'alpha', b: 'beta', c: 'alpha' })[name] || '';
  vm.runInContext(fnOf(tools, 'removedLoraTriggers') + fnOf(tools, 'stripLoraTriggers'), context);
  const { removedLoraTriggers, stripLoraTriggers } = context;
  assert.deepEqual(plain(removedLoraTriggers([{ name: 'a' }, { name: 'b' }], [{ name: 'b' }], trigger)), { removed: ['alpha'], kept: ['beta'] });
  assert.deepEqual(plain(removedLoraTriggers([{ name: 'a' }, { name: 'c' }], [{ name: 'c' }], trigger)).removed, []);
  assert.equal(stripLoraTriggers('alpha, 1girl', ['alpha']), '1girl');
  assert.equal(stripLoraTriggers('Alpha, beta, 1girl', ['beta'], ['alpha']), 'Alpha, 1girl');
  assert.equal(stripLoraTriggers('alpha', ['alpha']), '');
  assert.equal(stripLoraTriggers('1girl, alpha', ['alpha']), '1girl, alpha');
  assert.equal(stripLoraTriggers('alphabet, 1girl', ['alpha']), 'alphabet, 1girl');
});

test('다음: 패치면 재조립 없이 파일명을 지키고 덮어쓰기 칸·트리거·스냅샷을 갱신한다', async () => {
  const cells = [element('checkpoint', 'override.safetensors'), element('lora_name', 'x'), element('lora_strength', '0.3'), element('seed_count', '7')];
  const { harness, dom, calls, promptInput } = editHarness({ state: { ...loadedState, batchMode: null }, options: cells, csv: { name: 'rows.csv' } });
  harness.wizard.loras = [];
  await harness.goToNewJobDetails();
  assert.equal(calls.apply, 0);
  assert.equal(calls.patch[0].values.checkpoint, 'a.safetensors');
  assert.deepEqual(plain(calls.patch[0].values.loras), []);
  assert.equal(calls.files[0].name, 'job.json');
  assert.equal(JSON.parse(calls.files[0].body).patched, true);
  assert.deepEqual(cells.map(el => el.value), ['', '', '', '7']);
  assert.equal(promptInput.value, 'body');
  assert.deepEqual(plain(calls.triggers), [[]]);
  assert.equal(plain(harness.lastWizardModels).checkpoint, 'a.safetensors');
  assert.equal(harness.wizardAppliedSignature, JSON.stringify(harness.wizard));
  assert.deepEqual(plain(harness.wizardLoadedState.loras), []);
  assert.equal(harness.newJobActiveTab, 'details');
  assert.match(dom['load-notice'].textContent, /CSV 행의 트리거 문구\(old\)/);
});

test('다음: 패치가 거절되면 이유를 띄우고 탭을 넘기지 않는다', async () => {
  const { harness, dom, calls } = editHarness({ state: loadedState, fail: 'LoRA 체인이 일자가 아니라 고칠 수 없어요' });
  harness.wizard.checkpoint = 'b.safetensors';
  await harness.goToNewJobDetails();
  assert.equal(dom['wizard-error'].textContent, 'LoRA 체인이 일자가 아니라 고칠 수 없어요');
  assert.equal(harness.newJobActiveTab, 'select');
  assert.equal(calls.files.length, 0);
  assert.equal(harness.wizardLoadedState.checkpoint, 'a.safetensors');
});

test('다음: 재조립은 이름이 같은 옵션·프롬프트·CSV를 복원하고 런타임 칸은 새 값을 둔다', async () => {
  const csv = { name: 'rows.csv' };
  const select = element('sampler', 'euler', { tagName: 'SELECT', choices: ['euler'], dispatchEvent(){} });
  const gone = element('style', 'removed-choice', { tagName: 'SELECT', choices: ['kept'], dispatchEvent(){} });
  const cells = [element('checkpoint', 'old.safetensors'), element('seed_count', '7'), element('pose_image', 'p.png'), select, gone];
  const { harness, dom, calls, promptInput } = editHarness({ state: loadedState, options: cells, csv });
  harness.wizard.base = 'txt2img';
  await harness.goToNewJobDetails();
  assert.equal(calls.apply, 1);
  assert.equal(calls.patch.length, 0);
  assert.deepEqual(cells.map(el => el.value), ['rebuilt.safetensors', '7', 'p.png', 'euler', '']);
  assert.equal(promptInput.value, 'old, body');
  assert.deepEqual(calls.csv, [csv]);
  assert.match(dom['load-notice'].textContent, /적용했어요\n샘플러·Face Detailer 수치는 베이스 모델 기본값으로/);
  assert.equal(harness.wizardLoadedState.base, 'txt2img');
  assert.equal(harness.newJobActiveTab, 'details');
});

test('다음: 새 템플릿이 CSV를 안 쓰면 버리고, 재조립 실패면 넘어가지 않으며, 변경이 없으면 아무것도 안 부른다', async () => {
  const noCsv = editHarness({ state: loadedState, template: {}, csv: { name: 'rows.csv' } });
  noCsv.harness.wizard.batchMode = 'csv';
  await noCsv.harness.goToNewJobDetails();
  assert.equal(noCsv.calls.csv.length, 0);

  const failed = editHarness({ state: loadedState, apply: false });
  failed.harness.wizard.base = 'txt2img';
  await failed.harness.goToNewJobDetails();
  assert.equal(failed.harness.newJobActiveTab, 'select');

  const same = editHarness({ state: loadedState });
  same.harness.wizard.post.openpose = false;
  await same.harness.goToNewJobDetails();
  assert.equal(same.calls.apply + same.calls.patch.length, 0);
  assert.equal(same.harness.newJobActiveTab, 'details');
});

test('loadJobSettings가 마법사를 비우고 역채움 뒤에 안내를 쓴다', () => {
  const body = fnOf(refs, 'loadJobSettings');
  const order = ['resetWizardAndWorkflow()', 'resetForm()', 'tryPopulateWizardFromWorkflow(loadedWorkflow', 'setLoraTriggerField(', 'loadNotice.textContent = msg'];
  const positions = order.map(text => body.indexOf(text));
  assert.ok(positions.every(p => p >= 0), positions.join());
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});
