// 외부 API 없이 실제 입력 이벤트와 업로드 핸들러를 검사한다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { File } = require('node:buffer');
class Element {
  constructor(){ this.children = []; this.listeners = {}; this.style = {}; this.dataset = {}; this.value = ''; this.className = ''; this.textContent = ''; this.classList = {add(){}, remove(){}}; }
  focus(){ this.focused = true; }
  setSelectionRange(start, end){ this.selectionStart = start; this.selectionEnd = end; }
  appendChild(el){ this.children.push(el); return el; }
  addEventListener(type, fn){ (this.listeners[type] ||= []).push(fn); }
  async fire(type){ for(const fn of this.listeners[type] || []) await fn({target: this}); }
  dispatchEvent(e){ return this.fire(e.type); }
  all(){ return this.children.flatMap(c => [c, ...c.all()]); }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){ return this.all().filter(c => selector.startsWith('.') ? c.className.split(' ').includes(selector.slice(1)) : selector.includes('data-name') ? c.dataset.name === selector.match(/"([^"]+)"/)[1] : false); }
}
const ids = new Map();
const el = id => { assert.notEqual(id, 'quality-prompt-disabled'); if(!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
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
async function model(kind, filename){ context.family = {kind}; context.filename = filename; await run('setWizardQualityPrompt(family, filename)'); }
async function main(){
  items = ['a', 'b'].map(filename => ({kind: 'checkpoints', filename, sweet: {positive_prefix: filename + ' quality', prompt_tips: 'tips'}}));
  items.push({kind: 'diffusion_models', filename: 'u', sweet: {positive_prefix: 'unet quality'}});
  let wrap = prompt('body');
  await model('checkpoints', 'a');
  assert.equal(el('quality-prompt-input').value, 'a quality');
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'body');
  assert.equal(run("wizardPromptWithExtras('  seed, ', '', true)"), '  seed, ');
  const unclicked = new File(['main_prompt\nbody\n'], 'unclicked.csv');
  context.unclicked = unclicked;
  assert.equal(await (await run('csvWithWizardQuality(unclicked, "", templatesById.test)')).text(), 'main_prompt\nbody\n');
  await add(); await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'body, a quality');
  assert.equal(wrap.qualityPromptControl.get().focused, true);
  assert.equal(wrap.qualityPromptControl.get().selectionStart, 'body, a quality'.length);
  el('quality-prompt-input').value = 'edited'; await model('checkpoints', 'a'); assert.equal(el('quality-prompt-input').value, 'edited');
  await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'body, a quality, edited');
  wrap.qualityPromptControl.get().value = 'user body'; wrap.qualityPromptControl.sync();
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'user body');
  await model('checkpoints', 'b'); assert.equal(el('quality-prompt-input').value, 'b quality');
  assert.equal(wrap.qualityPromptControl.get().value, 'user body');
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'user body');
  await model('diffusion_models', 'u'); assert.equal(el('quality-prompt-input').value, 'unet quality');
  await model('checkpoints', 'missing'); assert.equal(el('quality-prompt-input').value, '');
  failed = true; await model('checkpoints', 'b'); assert.match(el('quality-prompt-hint').textContent, /직접 입력/); assert.match(el('quality-prompt-hint').textContent, /끝에/); failed = false;
  await model('checkpoints', 'b'); assert.equal(el('quality-prompt-input').value, 'b quality');
  await model('checkpoints', 'a'); wrap = prompt(); await add();
  wrap.qualityPromptControl.get().value += ', body'; await wrap.qualityPromptControl.get().fire('input');
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'a quality, body');
  await model('checkpoints', 'b'); assert.equal(wrap.qualityPromptControl.get().value, 'a quality, body');
  await model('checkpoints', 'a');
  wrap = prompt('middle a quality, body'); await add(); assert.equal(wrap.qualityPromptControl.get().value, 'middle a quality, body, a quality');
  wrap = prompt('raw body');
  await wrap.all().find(c => c.className === 'enhance-btn').fire('click');
  await wrap.all().find(c => c.textContent === '적용').fire('click');
  await add(); await submit(); assert.equal(submitted.values.get('main_prompt'), 'enhanced body, a quality');
  const result = wrap.qualityPromptControl.get(); result.value += ', edited'; await result.fire('input');
  el('lora-trigger-field').style.display = ''; el('lora-trigger-input').value = 'trigger';
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'trigger, enhanced body, a quality, edited');
  await wrap.all().find(c => c.textContent === '원문 유지').fire('click');
  await submit(); assert.equal(submitted.values.get('main_prompt'), 'trigger, raw body');
  const seeds = new Element(); seeds.className = 'option-input'; seeds.dataset.name = 'danbooru_seed_prompts'; optionsFields.appendChild(seeds);
  context.danbooruPreSubmitHooks.push(() => {seeds.value = JSON.stringify(['one', 'a quality, two', '']);});
  el('quality-prompt-input').value = 'not clicked';
  await submit(); assert.deepEqual(JSON.parse(submitted.values.get('danbooru_seed_prompts')), ['trigger, one, a quality', 'trigger, a quality, two, a quality', 'trigger, a quality']);
  for(const target of ['main_prompt', 'prompt']){
    context.templatesById.test.requires_csv = true;
    context.templatesById.test.csv_columns = [{name: target}, {name: 'quality_prompt'}];
    const originalText = target + ',quality_prompt\nbody,row quality\n"a quality, other",\n';
    const original = new File([originalText], 'test.csv');
    context.selectedFiles.csv = original;
    await submit(); context.csv = await submitted.values.get('csv').text();
    assert.equal(await original.text(), originalText);
    let rows = run('parseCsvText(csv).rows');
    assert.equal(rows[0][target], 'trigger, body, a quality'); assert.equal(rows[0].quality_prompt, 'row quality');
    assert.equal(rows[1][target], 'trigger, a quality, other, a quality');
    await run('openCsvEditor()'); assert.equal(run(`csvEditorRows[0].${target}`), 'body, a quality');
    run(`csvEditorRows[0].${target} = 'user csv'`);
    await el('csv-editor-save-btn').fire('click');
    await submit(); context.csv = await submitted.values.get('csv').text();
    assert.equal(run(`parseCsvText(csv).rows[0].${target}`), 'trigger, user csv');
    await run('openCsvEditor()'); assert.equal(run(`csvEditorRows[0].${target}`), 'user csv');
    await el('csv-editor-save-btn').fire('click'); await submit(); context.csv = await submitted.values.get('csv').text();
    assert.equal(run(`parseCsvText(csv).rows[0].${target}`), 'trigger, user csv');
    el('quality-prompt-input').value = 'new quality'; await add();
    await el('csv-editor-save-btn').fire('click'); await submit(); context.csv = await submitted.values.get('csv').text();
    assert.equal(run(`parseCsvText(csv).rows[0].${target}`), 'trigger, user csv, new quality');
    el('quality-prompt-input').value = 'a quality'; await add();
  }
  run('setQualityPromptField(null)'); await submit(); context.csv = await submitted.values.get('csv').text();
  assert.equal(run('wizardQuality.applied'), '');
  assert.deepEqual(JSON.parse(submitted.values.get('danbooru_seed_prompts')), ['trigger, one', 'trigger, a quality, two', '']);
  run('setQualityPromptField(null)'); assert.equal(el('quality-prompt-input').value, '');
  context.direct = new File(['prompt,quality_prompt\nbody,row quality\nother,\n'], 'direct.csv');
  const direct = await run('csvWithQualityPrompt(direct, "Q", "prefix")');
  context.csv = await direct.text();
  assert.equal(run('parseCsvText(csv).rows[0].prompt'), 'prefix, body');
  assert.equal(run('parseCsvText(csv).rows[1].prompt'), 'prefix, other, Q');
  console.log('Quality Prompt 클릭·순서·편집 보존·Enhance·CSV·시드 실제 제출 검사 통과');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
