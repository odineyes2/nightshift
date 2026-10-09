"""NS-41-3: 마법사·세부설정의 OpenPose 화면 검사.

- 고친 JS 문법(node --check)
- postFitsFamily: family_kind "checkpoints" 유형은 UNet형(diffusion_models) family에서 숨는다
- workflowHasOpenPose: DWPreprocessor가 있을 때만 참 → pose_image 칸·OpenPose 패널이 보인다
- poseSizeFor: 포즈 비율에 맞춘 64 배수 크기(면적 약 1024², 512~2048)
- pose_image 전용 홀더·업로드·갤러리·라이브러리 선택의 실제 DOM 이벤트 검사
node가 없으면 JS 실행 검사는 건너뛴다.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
FILES = [JS / f for f in ("02-job-move-newjob-form.js", "03-newjob-refs-inputs.js", "04-newjob-tools.js",
                          "06-wizard.js", "17b-settings.js")]
CSS = ROOT / "static" / "css" / "05-jobs.css"
HTML = ROOT / "static" / "index.html"


def func(src: str, name: str) -> str:
    m = re.search(r"function " + name + r"\([\s\S]*?\n}\n", src)
    assert m, f"{name}가 없다"
    return m.group(0)


def main():
    wizard = (JS / "06-wizard.js").read_text(encoding="utf-8")
    tools = (JS / "04-newjob-tools.js").read_text(encoding="utf-8")
    refs = (JS / "03-newjob-refs-inputs.js").read_text(encoding="utf-8")
    assert "postFitsFamily(t, currentFamily)" in wizard
    assert "setOpenPoseFieldsVisible(workflowHasOpenPose(parsed))" in wizard
    assert "spec.controlnet = await controlNetSpec(" in wizard
    assert "opt.name === 'pose_image'" in refs
    assert "'/api/controlnet/apply'" in tools and "/api/controlnet-defaults/" in tools
    html = HTML.read_text(encoding="utf-8")
    assert 'id="op-panel"' in html and 'id="op-edit-grid"' in html and 'id="op-setting-grid"' in html
    css = CSS.read_text(encoding="utf-8")
    assert '#options-fields:not(.has-openpose) .field:has([data-name="pose_image"])' in css

    node = shutil.which("node")
    if not node:
        print("node 없음 — JS 실행 검사 건너뜀")
        print("ok")
        return
    for f in FILES:
        subprocess.run([node, "--check", str(f)], check=True)
    script = "\n".join([func(wizard, "postFitsFamily"), func(wizard, "workflowHasOpenPose"),
                        func(tools, "poseSizeFor")]) + "\n" + """
const assert = require('assert');
const op = { id: 'openpose', family_kind: 'checkpoints' };
assert.strictEqual(postFitsFamily(op, { kind: 'checkpoints' }), true);
assert.strictEqual(postFitsFamily(op, { kind: 'diffusion_models' }), false);
assert.strictEqual(postFitsFamily({ id: 'hires_fix' }, { kind: 'diffusion_models' }), true);
assert.strictEqual(workflowHasOpenPose({ 1: { class_type: 'DWPreprocessor' } }), true);
assert.strictEqual(workflowHasOpenPose({ 1: { class_type: 'KSampler' } }), false);
assert.strictEqual(workflowHasOpenPose(null), false);
assert.deepStrictEqual(poseSizeFor(1000, 1000), { width: 1024, height: 1024 });
assert.deepStrictEqual(poseSizeFor(768, 1024), { width: 896, height: 1152 });
assert.deepStrictEqual(poseSizeFor(1920, 1080), { width: 1344, height: 768 });
const s = poseSizeFor(100, 2000);
assert.ok(s.width === 512 && s.height === 2048, JSON.stringify(s));
"""
    subprocess.run([node, "-e", script], check=True)
    browser_checks(refs)
    print("ok")


def browser_checks(refs):
    """실제 DOM 이벤트와 모의 API 응답으로 포즈 제출값·표시·취소를 확인한다."""
    from playwright.sync_api import sync_playwright
    html = HTML.read_text(encoding='utf-8')
    modal = html[html.index('<div class="modal-overlay" id="pose-image-picker-modal"'):html.index('<div class="modal-overlay" id="new-job-modal"')]
    source = refs[refs.index('function setPoseImageValue'):refs.index('function buildOptionControl')]
    css = '\n'.join(p.read_text(encoding='utf-8') for p in sorted((ROOT / 'static/css').glob('*.css')))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content(f'<style>{css}</style>{modal}<div id="new-job-modal" style="display:flex"><form id="form" class="field" style="width:100%;max-width:640px;padding:16px"></form></div>')
            page.evaluate("""() => {
              window.__nightshiftMediaUrl = () => 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="80" height="120"><rect width="80" height="120" fill="gray"/></svg>';
              window.responses = []; window.calls = []; window.changes = 0;
              window.fetch = async (url, opts) => {
                calls.push([url, opts]); const next = responses.shift();
                if(next === 'delay') return new Promise(resolve => window.resolveFetch = resolve);
                if(next === 'error') throw new Error('모의 실패');
                return {ok: true, json: async () => next};
              };
              document.getElementById('form').addEventListener('change', () => changes++);
            }""")
            page.add_script_tag(content=source)
            page.evaluate("() => document.getElementById('form').appendChild(buildPoseImageControl({name:'pose_image'}))")
            value = lambda: page.locator('[data-name="pose_image"]').input_value()
            assert value() == ''
            assert page.locator('.pose-image-holder').inner_text() == '포즈 이미지를 선택해 주세요.'
            assert page.locator('.pose-image-actions button:visible').all_text_contents() == ['업로드', '갤러리에서 선택', '라이브러리에서 선택']
            page.evaluate("() => responses.push({name:'upload.png'})")
            page.locator('input[type=file]').set_input_files({'name':'pose.png', 'mimeType':'image/png', 'buffer':b'test'})
            page.wait_for_function("document.querySelector('[data-name=pose_image]').value === 'upload.png'")
            assert page.locator('.pose-image-holder img').count() == 1
            assert page.evaluate("() => Object.fromEntries([...document.querySelectorAll('.option-input')].map(i => [i.dataset.name,i.value])).pose_image") == 'upload.png'
            page.evaluate("() => responses.push({images:[{name:'gallery.png'}]}, {added:['copied.png']})")
            page.get_by_role('button', name='갤러리에서 선택', exact=True).click()
            page.get_by_role('button', name='gallery.png', exact=True).click()
            assert value() == 'copied.png'
            assert page.evaluate("JSON.parse(calls[calls.length-1][1].body).names[0]") == 'gallery.png'
            listing = {'items':[{'id':7,'name':'두 장','images':[{'id':11,'thumb_url':'/first'},{'id':12,'thumb_url':'/second'}]}]}
            page.evaluate('(listing) => responses.push(listing)', listing)
            page.get_by_role('button', name='라이브러리에서 선택', exact=True).click()
            assert page.locator('.pose-image-choice').count() == 2
            page.keyboard.press('Escape')
            assert value() == 'copied.png'
            assert page.get_by_role('button', name='라이브러리에서 선택', exact=True).evaluate('(b) => b === document.activeElement')
            page.evaluate("() => responses.push({items:[]})")
            page.get_by_role('button', name='라이브러리에서 선택', exact=True).click()
            assert page.locator('#pose-image-picker-grid').inner_text() == '선택할 이미지가 없어요.'
            page.keyboard.press('Escape')
            page.evaluate("() => responses.push('error')")
            page.get_by_role('button', name='갤러리에서 선택', exact=True).click()
            assert page.locator('#pose-image-picker-error').inner_text() == '모의 실패'
            page.keyboard.press('Escape')
            page.evaluate('(listing) => responses.push(listing, "error")', listing)
            page.get_by_role('button', name='라이브러리에서 선택', exact=True).click()
            page.get_by_role('button', name='두 장 · 2번째 이미지', exact=True).click()
            assert value() == 'copied.png'
            assert page.locator('#pose-image-picker-error').inner_text() == '모의 실패'
            page.evaluate("() => responses.push('delay')")
            page.get_by_role('button', name='두 장 · 2번째 이미지', exact=True).click()
            before = page.evaluate('calls.length')
            page.evaluate("() => document.querySelector('.pose-image-choice').click()")
            assert page.evaluate('calls.length') == before
            page.keyboard.press('Escape')
            page.evaluate("() => resolveFetch({ok:true,json:async()=>({name:'late.png'})})")
            assert value() == 'copied.png'
            page.evaluate('(listing) => responses.push(listing, {name:"library.png",danbooru_prompt:"ignored"})', listing)
            page.get_by_role('button', name='라이브러리에서 선택', exact=True).click()
            page.get_by_role('button', name='두 장 · 2번째 이미지', exact=True).click()
            assert value() == 'library.png'
            assert page.evaluate('calls[calls.length-1][0]') == '/api/library/poses/7/images/12/to-input'
            assert page.locator('.enhance-raw').count() == 0  # 선택창은 프롬프트 UI를 만들지 않는다
            page.get_by_role('button', name='선택 해제', exact=True).click()
            assert value() == '' and page.locator('.pose-image-holder img').count() == 0
            page.evaluate("() => setPoseImageValue(document.querySelector('[data-name=pose_image]'), 'restored.png')")
            assert value() == 'restored.png' and page.locator('.pose-image-holder img').count() == 1
            page.evaluate("() => responses.push('delay')")
            page.locator('input[type=file]').set_input_files({'name':'late.png', 'mimeType':'image/png', 'buffer':b'test'})
            page.evaluate("() => document.getElementById('new-job-modal').style.display = 'none'")
            page.evaluate("() => document.getElementById('new-job-modal').style.display = 'flex'")
            page.evaluate("() => resolveFetch({ok:true,json:async()=>({name:'closed.png'})})")
            assert value() == 'restored.png'
            assert page.evaluate('changes') >= 6
            # 저장된 select 타입 pose_image도 입력 파일명 그대로 복원한다.
            page.add_script_tag(content='async ' + func(refs, 'loadJobSettings'))
            page.evaluate("""async () => {
              window.optionsFields = document.getElementById('form');
              const template = {id:'pose',options:[{name:'pose_image',type:'select',choices:[]}]};
              window.templatesById = {pose:template}; window.templateSelect = {};
              window.resetForm = () => {}; window.onTemplateChange = async () => {};
              window.openNewJobModal = () => {};
              for(const id of ['load-error','load-notice']){ const e = document.createElement('div'); e.id=id; document.body.append(e); }
              await loadJobSettings({template_id:'pose',options:{pose_image:'saved.png'}}, {});
            }""")
            assert value() == 'saved.png' and page.locator('.pose-image-holder img').count() == 1
            tools = (JS / '04-newjob-tools.js').read_text(encoding='utf-8')
            page.add_script_tag(content='async ' + func(tools, 'suggestPoseSize'))
            page.evaluate("""async () => {
              window.inputImageSize = () => new Promise(resolve => window.resolveSize = resolve);
              const pending = suggestPoseSize('saved.png');
              setPoseImageValue(document.querySelector('[data-name=pose_image]'), 'new.png');
              resolveSize({w:80,h:120}); await pending;
              if(document.getElementById('pose-size-hint').textContent) throw Error('늦은 크기 제안');
            }""")
            # 일반 이미지·영상 시작/끝은 기존 공용 컨트롤로 보낸다.
            page.add_script_tag(content=func(refs, 'buildOptionControl'))
            page.evaluate("""() => {
              window.buildInputImageControl = opt => ({kind:'image',name:opt.name});
              window.buildInputImageOptionalControl = opt => ({kind:'optional',name:opt.name});
              window.buildInputVideoControl = opt => ({kind:'video',name:opt.name});
              for(const name of ['input_image','first_frame_image','last_frame_image']){
                const type = name === 'input_image' ? 'input_image' : 'input_image_optional';
                if(buildOptionControl({name,type}).name !== name) throw Error(name);
              }
              if(buildOptionControl({name:'video',type:'input_video'}).kind !== 'video') throw Error('video');
            }""")
            # 변경한 포즈 섹션과 선택창만 네 가지 크기·테마로 저장한다.
            shots = ROOT / '.ui-captures' / 'NS-67-1'
            shots.mkdir(parents=True, exist_ok=True)
            for theme in ('light','dark'):
                page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                for width in (1300,390):
                    page.set_viewport_size({'width':width,'height':850})
                    for state in ('empty','selected','picker'):
                        page.evaluate('(name) => setPoseImageValue(document.querySelector("[data-name=pose_image]"), name)', '' if state == 'empty' else 'restored.png')
                        if state == 'picker':
                            page.evaluate('(listing) => responses.push(listing)', listing)
                            page.get_by_role('button', name='라이브러리에서 선택', exact=True).click()
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        page.screenshot(path=str(shots / f'{state}-{theme}-{width}.png'))
                        if state == 'picker': page.keyboard.press('Escape')
        finally:
            browser.close()


if __name__ == "__main__":
    main()
    sys.exit(0)
