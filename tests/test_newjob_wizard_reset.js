// 실제 초기화 함수와 두 버튼 연결을 외부 API 없이 검사한다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
const models = source('05-models.js');
const form = source('02-job-move-newjob-form.js');
const refs = source('03-newjob-refs-inputs.js');
const wizardSource = source('06-wizard.js');
const ids = new Map();
const el = id => {
  if(!ids.has(id)) ids.set(id, {textContent: 'old error', remove(){this.removed = true;},
    addEventListener(type, fn){this.handler = fn;}});
  return ids.get(id);
};
let optionRenders = 0, tabRenders = 0;
const context = vm.createContext({
  document: {getElementById: el},
  wizard: {familyId: 'old', loras: ['old'], batchMode: 'csv'}, wizardAppliedSignature: 'old',
  wizardPendingInputImage: 'old', wizardPendingPose: 'old', wizardNextPoseSequence: ['old'],
  quickRunReturn: true, newJobActiveTab: 'details', lastWizardModels: ['old'],
  selectedFiles: {workflow: 'old', videoWorkflow: 'old', csv: 'old'},
  templateSelect: {selectedIndex: 1, options: [{value: 'first'}, {value: 'second'}]},
  freshWizardPost: () => ({enabled: false}), wizardUpdateStepButtons(){},
  setLoraTriggerField(value){context.loraTriggers = value;},
  setQualityPromptField(value){context.qualityPrompt = value;},
  setWorkflowFile(value){context.selectedFiles.workflow = value;},
  setVideoWorkflowFile(value){context.selectedFiles.videoWorkflow = value;},
  setCsvFile(value){context.selectedFiles.csv = value;},
  async onTemplateChange(){await Promise.resolve(); optionRenders++; context.options = {seed: 0};},
  renderNewJobTabs(){tabRenders++; assert.equal(context.newJobActiveTab, 'select');},
});
const run = code => vm.runInContext(code, context);
run(models.slice(models.indexOf('function resetWizardAndWorkflow()')));
run(form.slice(form.indexOf('function resetForm()'), form.indexOf('const templateSelect')));
const resetStart = wizardSource.indexOf('async function resetNewJobWizard()');
run(wizardSource.slice(resetStart, wizardSource.indexOf('// Library "이 포즈로 생성"', resetStart)));
const binding = "document.getElementById('reset-form-btn').addEventListener('click', resetNewJobWizard);";
assert.ok(refs.includes(binding)); run(binding);
assert.equal((models + wizardSource).match(/getElementById\('wizard-reset-btn'\)\.addEventListener/g).length, 1);
async function main(){
  for(const id of ['wizard-reset-btn', 'reset-form-btn']){
    context.newJobActiveTab = 'details'; context.quickRunReturn = true;
    context.wizardPendingPose = 'old'; context.wizardPendingInputImage = 'old';
    context.wizardNextPoseSequence = ['old']; context.templateSelect.selectedIndex = 1;
    context.selectedFiles = {workflow: 'old', videoWorkflow: 'old', csv: 'old'};
    await el(id).handler();
    assert.equal(context.wizard.familyId, null); assert.equal(context.wizard.checkpoint, null);
    assert.equal(context.wizard.base, null); assert.equal(context.wizard.preset, null);
    assert.equal(context.wizard.loras.length, 0); assert.equal(context.wizard.batchMode, null);
    assert.equal(context.wizard.refinePrompt, true); assert.equal(context.wizard.refImageCount, 2);
    assert.equal(context.wizard.refVideoCount, 0); assert.equal(context.wizard.refAudioCount, 0);
    assert.equal(context.wizard.videoRefMode, 'vhs'); assert.equal(context.wizard.useFirstFrame, true);
    assert.equal(context.wizard.useLastFrame, false); assert.equal(context.wizard.t2vWidth, 704);
    assert.equal(context.wizard.t2vHeight, 1280); assert.equal(context.wizardAppliedSignature, null);
    assert.deepEqual(context.selectedFiles, {workflow: null, videoWorkflow: null, csv: null});
    assert.equal(context.lastWizardModels, null); assert.equal(context.loraTriggers.length, 0);
    assert.equal(context.qualityPrompt, null); assert.equal(context.quickRunReturn, false);
    assert.equal(context.wizardPendingInputImage, null); assert.equal(context.wizardPendingPose, null);
    assert.equal(context.wizardNextPoseSequence, null); assert.equal(context.newJobActiveTab, 'select');
    assert.equal(context.templateSelect.selectedIndex, 0); assert.equal(context.options.seed, 0);
    assert.ok(el('library-selection-preview').removed);
    for(const error of ['load-error', 'load-notice', 'upload-error', 'wizard-error']) assert.equal(el(error).textContent, '');
  }
  assert.equal(optionRenders, 2); assert.equal(tabRenders, 2);
  console.log('두 버튼의 전체 초기화와 select 탭 이동 검사 통과');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
