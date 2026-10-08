// 외부 API 없이 실제 입력 이벤트와 업로드 핸들러를 검사한다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { File } = require('node:buffer');
class Element {
  constructor(){ this.children = []; this.listeners = {}; this.style = {}; this.dataset = {}; this.value = ''; this.className = ''; this.textContent = ''; this.classList = {add(){}, remove(){}}; }
  appendChild(el){ this.children.push(el); return el; }
  addEventListener(type, fn){ (this.listeners[type] ||= []).push(fn); }
  async fire(type){ for(const fn of this.listeners[type] || []) await fn({target: this}); }
  dispatchEvent(e){ return this.fire(e.type); }
  all(){ return this.children.flatMap(c => [c, ...c.all()]); }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){ return this.all().filter(c => selector.startsWith('.') ? c.className.split(' ').includes(selector.slice(1)) : selector.includes('data-name') ? c.dataset.name === selector.match(/"([^"]+)"/)[1] : false); }
}
const ids = new Map();
const el = id => { if(!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
let items = [], failed = false, submitted;
const optionsFields = new Element();
const context = vm.createContext({console, File, Event, optionsFields, escapeHtml: String,
  document: {getElementById: el, querySelector: el, createElement: () => new Element(), addEventListener(){}},
  wizard: {post: {prompt_enhance: true}}, wizardEnhanceMode: () => 'danbooru',
  createCopyButton: () => new Element(), createSendToDanbooruButton: () => new Element(),
  currentFamilySupportsDanbooru: () => false, ico: () => '', danbooruPreSubmitHooks: [],
  templatesById: {test: {id: 'test', requires_workflow: false, options: []}},
  templateSelect: {value: 'test'}, selectedFiles: {csv: null}, uploadError: el('error'), lastWizardModels: null,
  formPodId: () => '', formProjectId: () => null, closeNewJobModal(){}, fetchJobs(){}, flashNotice(){},
  FormData: class { constructor(){this.values = new Map();} append(k,v){ this.values.set(k,v); } },
  fetch: async url => {
    if(url === '/api/models') return {ok: !failed, json: async () => ({items})};
    if(url === '/api/enhance-prompt') return {ok: true, json: async () => ({enhanced: 'enhanced body'})};
    assert.equal(url, '/api/upload'); submitted = context.lastForm;
    return {ok: true, json: async () => null};
  },
});
context.FormData = class {constructor(){this.values = new Map(); context.lastForm = this;} append(k,v){this.values.set(k,v);} };
context.setCsvFile = file => { context.selectedFiles.csv = file; };
const run = code => vm.runInContext(code, context);
const source = file => fs.readFileSync(require('node:path').join(__dirname, '../static/js/', file), 'utf8');
const tools = source('04-newjob-tools.js');
run(tools.slice(tools.indexOf('let csvEditorSchema'), tools.indexOf('async function fetchComfyStatus')));
run(tools.slice(tools.indexOf('function prependLoraTrigger'), tools.indexOf('async function fetchBaseModelFamilies')));
const form = source('02-job-move-newjob-form.js');
run(form.slice(form.indexOf('function buildPromptEnhanceControl'), form.indexOf('const ASPECT_RATIOS')));
const refs = source('03-newjob-refs-inputs.js');
run(refs.slice(refs.indexOf("document.getElementById('submit-btn').addEventListener"), refs.indexOf("document.getElementById('reset-form-btn')")));
function prompt(value = ''){
  optionsFields.children = [];
  context.opt = {name: 'main_prompt', default: value};
  const wrap = run('buildPromptEnhanceControl(opt)'); optionsFields.appendChild(wrap); return wrap;
}
const submit = () => el('submit-btn').fire('click');
const add = () => el('quality-prompt-add').fire('click');
async function disable(on){ el('quality-prompt-disabled').checked = on; await el('quality-prompt-disabled').fire('change'); }
async function model(kind, filename){ context.family = {kind}; context.filename = filename; await run('setWizardQualityPrompt(family, filename)'); }
async function main(){
  items = ['a', 'b'].map(filename => ({kind: 'checkpoints', filename, sweet: {positive_prefix: filename + ' quality', prompt_tips: 'tips'}}));
  items.push({kind: 'diffusion_models', filename: 'u', sweet: {positive_prefix: 'unet quality'}});
  let wrap = prompt('body');
  await model('checkpoints', 'a');
  assert.equal(el('quality-prompt-input').value, 'a quality');
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'body');
  await add(); await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'a quality, body');
  el('quality-prompt-input').value = 'edited'; await model('checkpoints', 'a'); assert.equal(el('quality-prompt-input').value, 'edited');
  await disable(true); assert.equal(wrap.qualityPromptControl.get().value, 'body'); assert.equal(el('quality-prompt-input').value, 'edited');
  await add(); await disable(false); await submit(); assert.equal(submitted.values.get('main_prompt'), 'body');
  await add(); wrap.qualityPromptControl.get().value = 'user edited, body'; wrap.qualityPromptControl.sync();
  await disable(true); assert.equal(wrap.qualityPromptControl.get().value, 'user edited, body');
  await model('checkpoints', 'b'); assert.equal(el('quality-prompt-input').value, 'b quality'); assert.equal(el('quality-prompt-disabled').checked, false);
  await model('diffusion_models', 'u'); assert.equal(el('quality-prompt-input').value, 'unet quality');
  await model('checkpoints', 'missing'); assert.equal(el('quality-prompt-input').value, ''); assert.equal(el('quality-prompt-field').style.display, '');
  failed = true; await model('checkpoints', 'b'); assert.equal(el('quality-prompt-input').value, ''); assert.match(el('quality-prompt-hint').textContent, /직접 입력/); failed = false;
  await model('checkpoints', 'b'); assert.equal(el('quality-prompt-input').value, 'b quality');
  await model('checkpoints', 'a'); wrap = prompt(); await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'a quality');
  wrap = prompt('middle a quality, body'); await add(); assert.equal(wrap.qualityPromptControl.get().value, 'a quality, middle a quality, body');
  wrap = prompt('raw body');
  await wrap.all().find(c => c.className === 'enhance-btn').fire('click');
  await wrap.all().find(c => c.textContent === '적용').fire('click');
  await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'a quality, enhanced body');
  const result = wrap.qualityPromptControl.get(); result.value += ', edited'; await result.fire('input');
  el('lora-trigger-field').style.display = ''; el('lora-trigger-input').value = 'trigger';
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'a quality, trigger, enhanced body, edited');
  const seeds = new Element(); seeds.className = 'option-input'; seeds.dataset.name = 'danbooru_seed_prompts'; optionsFields.appendChild(seeds);
  context.danbooruPreSubmitHooks.push(() => {seeds.value = JSON.stringify(['one', 'a quality, two', '']);});
  await submit(); assert.deepEqual(JSON.parse(submitted.values.get('danbooru_seed_prompts')), ['a quality, trigger, one', 'a quality, trigger, two', 'a quality, trigger']);
  for(const target of ['main_prompt', 'prompt']){
    context.templatesById.test.requires_csv = true;
    context.templatesById.test.csv_columns = [{name: target}, {name: 'quality_prompt'}];
    const original = new File([target + ',quality_prompt\nbody,row quality\n"a quality, other",\n'], 'test.csv');
    context.selectedFiles.csv = original;
    await submit(); const csv = await submitted.values.get('csv').text();
    assert.equal(await original.text(), target + ',quality_prompt\nbody,row quality\n"a quality, other",\n');
    context.csv = csv; const rows = run('parseCsvText(csv).rows');
    assert.equal(rows[0][target], 'a quality, trigger, body'); assert.equal(rows[0].quality_prompt, 'row quality');
    assert.equal(rows[1][target], 'a quality, trigger, other');
    await run('openCsvEditor()'); assert.equal(run(`csvEditorRows[0].${target}`), 'a quality, body');
    await el('csv-editor-save-btn').fire('click');
    await disable(true); await submit(); context.csv = await submitted.values.get('csv').text();
    assert.equal(run('parseCsvText(csv).rows[0]["' + target + '"]'), 'trigger, body');
    await disable(false); await add();
  }
  run('setQualityPromptField(null)'); assert.equal(run('wizardQuality.applied'), ''); assert.equal(el('quality-prompt-input').value, '');
  console.log('추천 모델값·편집·추가·비활성·Enhance·CSV 원본·시드별 실제 업로드 검사 통과');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
