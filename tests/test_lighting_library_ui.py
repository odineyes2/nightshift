"""Lighting 서브탭의 프롬프트 전용 생성·저장 대상·이동 차단을 모의 DOM/fetch로 검사한다."""
import shutil
import subprocess
from pathlib import Path
from test_library_transfer_ui import HARNESS, BLOCK, SRC

ROOT = Path(__file__).resolve().parents[1]


def function(name):
    start = SRC.index(f'function {name}(')
    if SRC[max(0, start - 6):start] == 'async ':
        start -= 6
    return SRC[start:SRC.index('\n}\n', start) + 3]


def main():
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    assert 'data-library-tab="lighting"' in html
    kinds = html.split('id="lfg-kind"')[1].split('</div>')[0]
    assert 'data-kind="lighting">Lighting</button>' in kinds
    header = SRC[SRC.index('const LIBRARY_API'):SRC.index('function setLibraryError')]
    # 기존 Pose/Position 모의 환경을 재사용하되 Lighting만 직접 호출한다.
    harness = HARNESS[:HARNESS.index('const tick =')].replace('__BLOCK__', BLOCK)
    script = harness + header + r'''
const assert = require('assert');
const elements = new Map();
const element = id => {
  if(!elements.has(id)) elements.set(id, {hidden:false, style:{}, textContent:'', innerHTML:'',
    setAttribute(){}, querySelector(){return element(id+'-label');}});
  return elements.get(id);
};
document.getElementById = element;
const libraryArticle = () => libraryPoses[0];
''' + function('poseGenerateButton') + function('libraryImageDeleteButton') + function('renderLibraryArticle') + function('moveLibraryPost') + r'''
const ico = () => '';
const escapeHtml = s => s;
(async () => {
  libraryTab = 'lighting';
  assert.equal(libraryApi(), '/api/library/lightings');
  assert.equal(libraryNoun(), 'Lighting');
  assert(poseGenerateButton('test',3).includes('이 Lighting으로 생성 (txt2img)'));
  await generateFromPose(7,3);
  assert.equal(modal.style.display, 'none');
  assert.deepEqual(calls[0].slice(0,2), ['prompt','tag a']);
  assert.equal(calls[0][2].source_kind, 'lighting');
  assert.equal(calls[0][2].generation_mode, 'prompt');
  calls.length = 0;
  librarySelected.add(8);
  await generatePoseSequence();
  assert.equal(modal.style.display, 'none');
  assert.equal(calls[0][0], 'promptSeq');
  assert.deepEqual(calls[0][1].map(i=>i.source_image_id), [5]);
  assert(calls[0][1].every(i=>i.source_kind==='lighting' && i.generation_mode==='prompt'));
  calls.length = 0;
  // 내부 실행 함수에 잘못된 모드를 넘겨도 Openpose 경로로 가지 않는다.
  await runLibrarySingle('lighting', libraryPoses[0], 4, 'openpose');
  await runLibrarySequence('lighting', libraryPoses, 'openpose');
  assert(calls.every(c=>c[0]==='prompt' || c[0]==='promptSeq'));
  renderLibraryArticle();
  assert.equal(element('library-article-move-btn').hidden, true);
  calls.length = 0;
  await moveLibraryPost();
  assert.equal(calls.length,0);
  for(const kind of ['pose','position']){
    libraryTab=kind; renderLibraryArticle();
    assert.equal(element('library-article-move-btn').hidden,false);
  }
  console.log('ok: Lighting prompt single/sequence, origin, no transfer/copy/move');
})();
'''
    node = shutil.which('node')
    assert node, 'node가 필요해요'
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    print(result.stdout.strip())


if __name__ == '__main__':
    main()
