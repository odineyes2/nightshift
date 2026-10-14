"""실제 CSS·패널 렌더러·마법사 적용 흐름의 브라우저 가시성 회귀 검사.

서버/GPU 대신 로컬 요청을 모의 응답으로 대체한다. 브라우저 부재는 실패한다.
--capture는 바뀐 새 작업 세부 화면만 낮·밤 × 1300·390으로 저장한다.
"""
import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_ui import isolated_environment
from capture_ui_support import child_environment


def function(source, name):
    match = re.search(r'(?:async )?function ' + name + r'\([\s\S]*?\n}\n', source)
    assert match, name
    return match.group(0)


def fixture():
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', html)
    html = re.sub(r'<link\b[^>]*>', '', html)
    css = '\n'.join(p.read_text(encoding='utf-8') for p in sorted((ROOT / 'static/css').glob('*.css')))
    tools = (ROOT / 'static/js/04-newjob-tools.js').read_text(encoding='utf-8')
    wizard = (ROOT / 'static/js/06-wizard.js').read_text(encoding='utf-8')
    form = (ROOT / 'static/js/02-job-move-newjob-form.js').read_text(encoding='utf-8')
    # 화면과 관계없는 모델 목록·옵션 폼 준비만 대체한다. 생성/파일 교체/판정은 실제 함수를 쓴다.
    script = """
const optionsFields = document.getElementById('options-fields');
const templateSelect = document.getElementById('template-select');
templateSelect.innerHTML = '<option value="test">test</option>';
const templatesById = {test: {requires_workflow: true}};
const selectedFiles = {};
const baseModelFamilies = {test: {kind: 'checkpoints', controlnets: {openpose: ['test-cn']}}};
let wizard = {familyId:'test', checkpoint:'test.safetensors', base:'txt2img', batchMode:'seed',
  loras:[], post:{face_detailer:false, openpose:false, cnName:'test-cn'}};
let workflowCheckToken = 0, wizardRefCounts, lastWizardModels, wizardAppliedSignature;
let wizardPendingInputImage = null, wizardPendingPose = null, newJobActiveTab = 'select';
let quickRunReturn = false;
const wizardQuality = {key:null};
const wizardTypeChosen = () => true, wizardComplete = () => true;
const wizardTemplateIds = () => ({seed:'test'}), wizardIsPreset = () => false;
const wizardBaseMeta = () => ({}), wizardPostApplies = k => !!wizard.post[k];
const wizardStale = () => wizardAppliedSignature !== JSON.stringify(wizard);
const podQuery = () => '';
const setLoraTriggerField = () => {}, setQualityPromptField = () => {};
const setWizardQualityPrompt = async () => {}, onTemplateChange = async () => {};
const setVideoWorkflowFile = () => {}, setCsvFile = () => {};
const renderWorkflowCheck = (tone, text) => {
  document.getElementById('workflow-check').textContent = text;
};
const opEditGrid = document.getElementById('op-edit-grid');
let fdArchitecture;
"""
    # 실제 패널 입력 생성 코드도 로드한다.
    script += tools[tools.index('const FD_FIELDS'):tools.index('function fdRead')]
    op_start = tools.index('const OP_FIELDS')
    script += tools[op_start:tools.index('// checkWorkflowCompatibility', op_start)].replace(
        "const opEditGrid = document.getElementById('op-edit-grid');", '')
    for name in ('fdFill', 'renderFaceDetailerPanel', 'renderOpenPosePanel'):
        script += function(tools, name)
    for name in ('setupSlot', 'resetForm'):
        script += function(form, name)
    for name in ('workflowHasFaceDetailer', 'setFaceDetailerFieldsVisible', 'workflowHasOpenPose',
                 'setOpenPoseFieldsVisible', 'checkWorkflowCompatibility', 'postFitsFamily',
                 'openPoseControlNets', 'faceDetailerSpec', 'controlNetSpec', 'wizardTextEncoder', 'wizardApply',
                 'newJobReadyToAdvance', 'updateNewJobTabs', 'renderNewJobTabs', 'goToNewJobDetails'):
        script += function(wizard, name)
    script += """
const setWorkflowFile = setupSlot('workflow-slot','workflow-input','workflow','.json',checkWorkflowCompatibility);
document.getElementById('new-job-modal').style.display = 'flex';
document.getElementById('new-job-next-btn').onclick = goToNewJobDetails;
renderNewJobTabs();
"""
    return html.replace('</head>', '<style>' + css + '</style></head>'), script


def main(capture=False):
    from playwright.sync_api import sync_playwright
    html, script = fixture()
    builds = []

    def respond(route):
        url = route.request.url
        if url == 'http://127.0.0.1/':
            route.fulfill(content_type='text/html', body=html)
        elif '/api/build-workflow' in url:
            spec = route.request.post_data_json
            builds.append(spec)
            workflow = {'1': {'class_type': 'KSampler'}}
            if spec.get('face_detailer', {}).get('enabled'):
                workflow['2'] = {'class_type': 'FaceDetailer', 'inputs': {'steps': 20}}
            if spec.get('controlnet', {}).get('enabled'):
                workflow['3'] = {'class_type': 'DWPreprocessor', 'inputs': {'resolution': 512}}
            route.fulfill(json={'workflow': workflow})
        elif '/api/validate-workflow' in url:
            route.fulfill(json={'auto': True})
        elif '/api/' in url:
            route.fulfill(json={'values': {}})
        else:
            route.abort()  # 외부 URL·서비스에는 연결하지 않는다.

    def panels(page, face, pose):
        for name, visible in [('fd-panel', face), ('op-panel', pose)]:
            locator = page.locator('#' + name)
            if visible:
                locator.wait_for(state='visible')
            else:
                page.wait_for_function('(id) => getComputedStyle(document.getElementById(id)).display === "none"', arg=name)
            assert locator.is_visible() == visible, name
            assert locator.evaluate('(el) => getComputedStyle(el).display') == ('flex' if visible else 'none')
            if visible:
                assert locator.evaluate('(el) => el.scrollWidth <= el.clientWidth'), name + ' 가로 넘침'
                assert locator.locator('input').evaluate_all('els => els.every(el => el.getBoundingClientRect().right <= el.closest("label").getBoundingClientRect().right + 1)'), name + ' 입력 겹침'

    output = ROOT / 'tests/shots/workflow-option-panels'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as directory:
        env = child_environment(Path(directory))
        with isolated_environment(env, Path(directory) / 'tmp'), sync_playwright() as pw:
            browser = pw.chromium.launch(env=env)
            try:
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        context = browser.new_context(viewport={'width': width, 'height': 850}, service_workers='block')
                        context.route('**/*', respond)
                        page = context.new_page()
                        page.goto('http://127.0.0.1/')
                        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                        page.add_script_tag(content=script)
                        # 초기 hidden이 .field에 덮이지 않는지 계산된 스타일로 확인한다.
                        for name in ('fd-panel', 'op-panel'):
                            assert page.locator('#' + name).evaluate('(el) => getComputedStyle(el).display') == 'none'
                        for face, pose in ((False, False), (True, False), (False, True), (True, True), (False, False)):
                            page.evaluate('([face,pose]) => {wizard.post.face_detailer=face; wizard.post.openpose=pose; newJobActiveTab="select"; renderNewJobTabs();}', [face, pose])
                            page.locator('#new-job-next-btn').click()
                            page.wait_for_function('newJobActiveTab === "details"')
                            panels(page, face, pose)
                            assert bool(builds[-1].get('face_detailer', {}).get('enabled')) == face
                            assert bool(builds[-1].get('controlnet', {}).get('enabled')) == pose
                            if capture and (face, pose) in ((False, False), (True, True)):
                                suffix = 'both' if face else 'none'
                                page.locator('#new-job-modal .modal-panel').screenshot(path=str(output / f'{theme}-{width}-{suffix}.png'))
                                if face:
                                    for name in ('fd-panel', 'op-panel'):
                                        page.locator('#' + name).screenshot(path=str(output / f'{theme}-{width}-{name}.png'))
                        for replacement in (None, '{"1":{"class_type":"KSampler"}}', '{broken'):
                            page.evaluate('() => setWorkflowFile(new File([JSON.stringify({1:{class_type:"FaceDetailer"},2:{class_type:"DWPreprocessor"}})],"both.json"))')
                            panels(page, True, True)
                            page.evaluate('(text) => setWorkflowFile(text === null ? null : new File([text],"replacement.json"))', replacement)
                            panels(page, False, False)
                        context.close()
            finally:
                browser.close()
    print('ok: 낮·밤 × 1300·390 / 네 조합·선택 해제·파일 제거·일반 교체·잘못된 JSON')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    main(parser.parse_args().capture)
