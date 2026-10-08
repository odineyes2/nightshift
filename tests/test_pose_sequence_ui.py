"""NS-42-3: Library 순차 생성·포즈 태그 스위치·포즈 k/N 표시 화면 검사.

- index.html에 전체 선택·순차 생성 버튼, 카드에 체크 버튼, 마법사에 role=switch 태그 스위치가 있다
- poseSequenceValue(node로 실행): 꺼짐이면 tags가 비고, 고정 크기면 모든 항목에 같은 크기, 아니면 포즈별 SDXL 크기
- 작업·워커 카드가 progress.label을 보여 준다
- 고친 JS 문법(node --check, node가 없으면 건너뜀)
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
FILES = ["17c-library.js", "06-wizard.js", "04-newjob-tools.js", "03-newjob-refs-inputs.js", "01-core-jobs.js", "08-pods.js"]


def read(name):
    return (JS / name).read_text(encoding="utf-8")


def main():
    for needle in ['id="library-select-all-btn"', 'id="library-sequence-btn"']:
        assert needle in HTML, needle
    lib = read("17c-library.js")
    for needle in ['class="library-check-btn"', "'/api/library/poses/to-input'", "startOpenPoseSequenceWizard(items)",
                   "!librarySelected.size || librarySelected.has(p.id)"]:
        assert needle in lib, needle
    wiz = read("06-wizard.js")
    for needle in ['role="switch" aria-checked="true"', "포즈 태그 자동 붙이기", "포즈마다 생성 장수",
                   "function startOpenPoseSequenceWizard", "toValue(items"]:
        assert needle in wiz, needle
    assert "opt.name === 'pose_sequence'" in read("03-newjob-refs-inputs.js")
    assert "progress.label" in read("01-core-jobs.js")
    assert "prog.label" in read("08-pods.js")

    node = shutil.which("node")
    if not node:
        print("node 없음 — JS 실행 검사 건너뜀")
        print("ok")
        return
    for name in FILES:
        r = subprocess.run([node, "--check", str(JS / name)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)

    src = read("04-newjob-tools.js")
    fn = re.search(r"function poseSequenceValue\(.*?\n}\n", src, re.S).group(0)
    items = [
        {"name": "library_pose_1_1.png", "pose_id": 1, "pose_name": "a", "danbooru_prompt": " standing ", "sdxl_width": 832, "sdxl_height": 1216},
        {"name": "library_pose_2_5.png", "pose_id": 2, "pose_name": "b", "danbooru_prompt": "sitting", "sdxl_width": 1216, "sdxl_height": 832},
    ]
    script = fn + f"""
const items = {json.dumps(items)};
console.log(JSON.stringify([
  JSON.parse(poseSequenceValue(items, true, null)),
  JSON.parse(poseSequenceValue(items, false, {{width: 1024, height: 1024}})),
]));"""
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    auto, fixed = json.loads(r.stdout)
    assert [p["image"] for p in auto] == ["library_pose_1_1.png", "library_pose_2_5.png"]
    assert [p["tags"] for p in auto] == ["standing", "sitting"]
    assert [(p["width"], p["height"]) for p in auto] == [(832, 1216), (1216, 832)]
    assert [p["tags"] for p in fixed] == ["", ""]
    assert all((p["width"], p["height"]) == (1024, 1024) for p in fixed)
    assert auto[1]["pose_id"] == 2 and auto[1]["pose_name"] == "b"
    # 실제 순차 폼의 초기값·태그 토글·공통 프롬프트 편집·제출 훅을 함께 실행한다.
    form_fn = re.search(r"function applyPoseSequenceForm\(.*?\n}\n", wiz, re.S).group(0)
    script = fn + form_fn + r'''
const mk = value => ({value, dataset:{}, listeners:{},
  addEventListener(k,f){this.listeners[k]=f;}, closest(){return {querySelector(){return {};}};}});
const count = mk('1'), raw = mk('common'), seq = mk(''), width = mk(''), height = mk('');
const fields = {seed_count:count, pose_sequence:seq, width, height};
const optionsFields = {querySelector(s){return fields[s.match(/data-name="(.*?)"/)[1]];}, prepend(){}};
const document = {createElement(){return {appendChild(){}};}};
const wizardPendingPose = {};
const danbooruPreSubmitHooks = [];
let onChange, shown, switchState = 'true';
const addPoseTagSwitch = (r, f) => {onChange=f; return {
 getAttribute(){return switchState;}, setAttribute(k,v){switchState=v;}};};
const renderLibrarySelection = (hint, common, on) => {shown={common,on};};
const items = [{name:'copy.png', source_kind:'position', article_id:7, source_image_id:3,
 generation_mode:'openpose', danbooru_prompt:'standing'}];
applyPoseSequenceForm(items, null, raw);
const initial = JSON.parse(seq.value);
switchState='false'; onChange();
const off = JSON.parse(seq.value);
applyPoseSequenceForm(items, null, raw);
if(switchState !== 'false' || JSON.parse(seq.value)[0].tags !== '') throw Error('reapply lost tag switch');
raw.value='edited'; raw.listeners.input();
switchState='true'; onChange();
danbooruPreSubmitHooks.forEach(f=>f());
console.log(JSON.stringify({initial,off,final:JSON.parse(seq.value),shown,pending:wizardPendingPose}));
'''
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    state = json.loads(r.stdout)
    assert state["initial"][0]["tags"] == "standing"
    assert state["off"][0]["tags"] == ""
    assert state["final"][0]["source_kind"] == "position"
    assert state["final"][0]["source_image_id"] == 3
    assert state["shown"] == {"common": "edited", "on": True}
    assert state["pending"]["tagsOn"] is True
    print("ok")


if __name__ == "__main__":
    main()
    sys.exit(0)
