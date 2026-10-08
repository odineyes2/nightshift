// 실제 모델 렌더·위임 이벤트·저장/재조회 경로를 임시 DOM과 서버로 검사한다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
const element = id => {
  if(!elements.has(id)) elements.set(id, {textContent: '', listeners: {},
    addEventListener(type, fn){ (this.listeners[type] ||= []).push(fn); }});
  return elements.get(id);
};
let admin = true, stored = [], requests = [];
const context = vm.createContext({console, document: {getElementById: element, addEventListener(){}},
  isAdminUser: () => admin, flashNotice(){},
  escapeHtml: v => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c])),
  fetch: async (url, options) => {
    assert.equal(url, '/api/models');
    if(options){
      const payload = JSON.parse(options.body);
      assert.equal(options.method, 'PUT');
      requests.push(payload);
      stored = [{...payload}];
      return {ok: true, json: async () => ({entry: stored[0]})};
    }
    return {ok: true, json: async () => ({items: stored})};
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/05-models.js'), 'utf8'), context);
const run = code => vm.runInContext(code, context);
run('renderModelRegistry = () => {};');
function seed(kind, sweet = {}){
  stored = [{kind, filename: 'test.safetensors', base_models: [], notes: '', tags: [],
    trigger_keyword: '', page_url: '', download_url: '', sweet}];
  context.entry = stored[0];
  run('modelRegistry.items = {[modelKey(entry.kind, entry.filename)]: entry}; modelEditDraft = modelDraftFromEntry(entry.kind, entry.filename);');
}
const saveButton = {disabled: true}, hint = {hidden: true};
const editor = {querySelector: selector => selector === '.model-save-btn' ? saveButton : hint};
function event(field, value, type = 'change'){
  const target = {dataset: {field}, value, matches: () => true,
    closest: selector => selector === '.model-editor' ? editor : target};
  for(const fn of element('lora-tab-list').listeners[type]) fn({target});
}
function options(html, field){
  const select = html.match(new RegExp(`<select[^>]*data-field="sweet\\.${field}"[^>]*>([\\s\\S]*?)</select>`));
  assert.ok(select, field);
  return [...select[1].matchAll(/<option value="([^"]*)"( selected)?>(.*?)<\/option>/g)];
}
async function main(){
  const samplers = ['er_sde', 'euler_ancestral', 'dpmpp_2m_sde_gpu', 'euler', 'dpmpp_2m',
    'dpmpp_2m_sde', 'dpmpp_sde', 'dpmpp_sde_gpu', 'dpmpp_2s_ancestral', 'heun', 'ddim', 'lcm', 'uni_pc'];
  const schedulers = ['simple', 'beta57', 'normal', 'karras', 'beta', 'exponential', 'sgm_uniform',
    'ddim_uniform', 'linear_quadratic', 'kl_optimal'];
  for(const kind of ['checkpoints', 'diffusion_models']){
    seed(kind);
    const html = run("modelSweetHtml(modelEditDraft, '')");
    for(const [field, values] of [['sampler_name', samplers], ['scheduler', schedulers]]){
      assert.deepEqual(options(html, field).map(o => o[1]), ['', ...values]);
      assert.equal(options(html, field).filter(o => o[2]).length, 1);
      for(const value of values){
        event(`sweet.${field}`, value);
        assert.equal(run(`modelEditDraft.sweet.${field}`), value);
        assert.equal(saveButton.disabled, false);
        if(field === 'scheduler') assert.equal(hint.hidden, value !== 'beta57');
        await run('saveModelDraft()');
        assert.equal(requests.at(-1).sweet[field], value);
        await run('fetchModelRegistry()');
        run('modelEditDraft = modelDraftFromEntry(entry.kind, entry.filename)');
        assert.equal(run(`modelEditDraft.sweet.${field}`), value);
        assert.equal(run('modelDraftDirty()'), false);
        assert.equal(options(run("modelSweetHtml(modelEditDraft, '')"), field).find(o => o[2])[1], value);
      }
    }
    event('sweet.sampler_name', ''); event('sweet.scheduler', '');
    await run('saveModelDraft()');
    assert.deepEqual(requests.at(-1).sweet, {});
  }
  for(const value of ['euler_a', 'custom_sampler']){
    seed('checkpoints', {sampler_name: value, scheduler: 'custom_scheduler'});
    const html = run("modelSweetHtml(modelEditDraft, '')");
    assert.equal(options(html, 'sampler_name').filter(o => o[1] === value).length, 1);
    assert.equal(options(html, 'sampler_name').find(o => o[2])[1], value);
    event('notes', '다른 필드 변경', 'input');
    await run('saveModelDraft()');
    assert.equal(requests.at(-1).sweet.sampler_name, value);
    assert.equal(requests.at(-1).sweet.scheduler, 'custom_scheduler');
  }
  seed('checkpoints', {scheduler: 'beta57', sampler_name: '\"><img src=x>&'});
  const html = run("modelSweetHtml(modelEditDraft, 'disabled')");
  assert.ok(html.includes('&quot;&gt;&lt;img src=x&gt;&amp;'));
  assert.ok(!html.includes('<img'));
  assert.match(html, /현재 저장값:/);
  assert.match(html, /data-model-beta57-hint>표준 ComfyUI에는 beta57/);
  assert.equal((html.match(/<select[^>]*disabled/g) || []).length, 2);
  admin = false;
  event('sweet.scheduler', 'karras');
  assert.equal(run('modelEditDraft.sweet.scheduler'), 'beta57');
  admin = true;
  seed('loras', {strength: 0.8, strength_min: 0.6, strength_max: 1, strength_clip: 0.9});
  const lora = run("modelSweetHtml(modelEditDraft, '')");
  assert.ok(!lora.includes('<select'));
  for(const field of ['strength', 'strength_min', 'strength_max', 'strength_clip']) assert.ok(lora.includes(`data-field="sweet.${field}"`));
  event('sweet.strength', '0.7', 'input');
  await run('saveModelDraft()');
  assert.deepEqual(requests.at(-1).sweet, {strength: 0.7, strength_min: 0.6, strength_max: 1, strength_clip: 0.9});
  console.log('모델 select 렌더·이벤트·저장·재조회 검사 통과');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
