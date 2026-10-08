"""Library 생성 방식 고르기(NS-51) — 17c-library.js의 모달 구간을 모의 DOM·fetch로 node에서 실행해 검사한다.
- 두 탭 × 프롬프트/Openpose CN × 단일/순차의 API 호출과 마법사 전달값
- 닫기(취소)·중복 클릭·요청 뒤 탭 전환·복사 실패
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "static" / "js" / "17c-library.js").read_text(encoding="utf-8")
START = SRC.index("// 생성 방식 고르기(NS-51)")
END = SRC.index("// ---- 아티클 갤러리")
BLOCK = SRC[START:END]

HARNESS = r"""
const calls = [];
const handlers = {};
const mkBtn = t => ({ dataset: { transfer: t }, addEventListener: (k, f) => { handlers[t] = f; }, focus(){} });
const btns = [mkBtn('prompt'), mkBtn('openpose')];
const modal = { style: { display: 'none' }, querySelectorAll: () => btns, querySelector: () => btns[0],
  addEventListener: (k, f) => { handlers.overlay = f; } };
const closeBtn = { addEventListener: (k, f) => { handlers.close = f; } };
const document = {
  activeElement: null,
  getElementById: id => id === 'library-transfer-modal' ? modal : closeBtn,
  addEventListener: (k, f) => { handlers.key = f; },
};
let libraryTab = 'pose';
let libraryPoses = [
  { id: 7, name: 'A', danbooru_prompt: 'tag a', images: [{ id: 3 }, { id: 4 }] },
  { id: 8, name: 'B', danbooru_prompt: 'tag b', images: [{ id: 5 }] },
];
const librarySelected = new Set();
let lastError = '';
const setLibraryError = m => { lastError = m; };
const libraryLightbox = { style: { display: 'none' } };
const closeLibraryLightbox = () => calls.push(['closeLightbox']);
const startPositionWizard = p => calls.push(['prompt', p]);
const startOpenPoseWizard = (n, p) => calls.push(['openpose', n, p]);
const startPositionSequenceWizard = items => calls.push(['promptSeq', items]);
const startOpenPoseSequenceWizard = items => calls.push(['openposeSeq', items]);
let failNext = false;
const fetch = async (url, opt) => {
  calls.push(['fetch', url, opt && opt.body ? JSON.parse(opt.body) : null]);
  if(failNext){ failNext = false; return { ok: false, status: 404, json: async () => ({ detail: '없어요' }) }; }
  if(url.endsWith('/positions/to-input')) return { ok: true, json: async () => [
    { position_id: 7, position_name: 'A', image_id: 3, name: 'library_position_3.png', danbooru_prompt: 'tag a', sdxl_width: 1024, sdxl_height: 1024 }] };
  if(url.endsWith('/poses/to-input')) return { ok: true, json: async () => [{ pose_id: 7, pose_name: 'A', name: 'library_pose_3.png' }] };
  return { ok: true, json: async () => ({ name: 'copied.png', danbooru_prompt: 'tag a' }) };
};
__BLOCK__
const tick = () => new Promise(r => setTimeout(r, 0));
(async () => {
  const out = {};
  const run = async (name, fn) => { calls.length = 0; lastError = ''; await fn(); await tick(); await tick(); out[name] = { calls: JSON.parse(JSON.stringify(calls)), error: lastError, open: modal.style.display }; };
  await run('pose_single_prompt', async () => { libraryTab = 'pose'; generateFromPose(7, 3); handlers.prompt(); });
  await run('pose_single_openpose', async () => { libraryTab = 'pose'; generateFromPose(7, 3); handlers.openpose(); });
  await run('position_single_prompt', async () => { libraryTab = 'position'; generateFromPose(7, 3); handlers.prompt(); });
  await run('position_single_openpose', async () => { libraryTab = 'position'; generateFromPose(7, 4); handlers.openpose(); });
  await run('pose_seq_prompt', async () => { libraryTab = 'pose'; generatePoseSequence(); handlers.prompt(); });
  await run('pose_seq_openpose', async () => { libraryTab = 'pose'; generatePoseSequence(); handlers.openpose(); });
  await run('position_seq_prompt', async () => { libraryTab = 'position'; generatePoseSequence(); handlers.prompt(); });
  await run('position_seq_openpose', async () => { libraryTab = 'position'; generatePoseSequence(); handlers.openpose(); });
  await run('cancel', async () => { generateFromPose(7, 3); out.openedBeforeCancel = modal.style.display; handlers.close(); handlers.openpose(); handlers.prompt(); });
  await run('escape', async () => { generateFromPose(7, 3); handlers.key({ key: 'Escape' }); handlers.prompt(); });
  await run('double_click', async () => { libraryTab = 'pose'; generateFromPose(7, 3); handlers.openpose(); handlers.openpose(); });
  await run('tab_switch', async () => { libraryTab = 'position'; generateFromPose(7, 3); libraryTab = 'pose'; handlers.openpose(); });
  await run('fail', async () => { libraryTab = 'position'; failNext = true; generatePoseSequence(); handlers.openpose(); });
  console.log(JSON.stringify(out));
})();
"""


def main():
    node = shutil.which("node")
    if not node:
        print("node 없음 — 건너뜀")
        return
    r = subprocess.run([node, "-e", HARNESS.replace("__BLOCK__", BLOCK)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    c = lambda k: o[k]["calls"]

    assert c("pose_single_prompt") == [["prompt", "tag a"]], c("pose_single_prompt")
    assert c("pose_single_openpose") == [["fetch", "/api/library/poses/7/images/3/to-input", None],
                                         ["openpose", "copied.png", "tag a"]]
    assert c("position_single_prompt") == [["prompt", "tag a"]]
    assert c("position_single_openpose")[0][1] == "/api/library/positions/7/images/4/to-input"
    assert c("position_single_openpose")[1][0] == "openpose"

    seq = c("pose_seq_prompt")
    assert len(seq) == 1 and seq[0][0] == "promptSeq", seq   # 프롬프트만은 복사 없음
    assert [it["position_id"] for it in seq[0][1]] == [7, 7, 8]   # 장마다 한 항목, 목록 순서
    assert c("position_seq_prompt") == seq

    assert c("pose_seq_openpose")[0] == ["fetch", "/api/library/poses/to-input", {"pose_ids": [7, 8]}]
    assert c("pose_seq_openpose")[1][0] == "openposeSeq"
    pos = c("position_seq_openpose")
    assert pos[0] == ["fetch", "/api/library/positions/to-input", {"position_ids": [7, 8]}]
    item = pos[1][1][0]
    assert pos[1][0] == "openposeSeq" and item["pose_id"] == 7 and item["pose_name"] == "A"
    assert item["name"] == "library_position_3.png" and item["sdxl_width"] == 1024

    assert o["openedBeforeCancel"] == "flex"
    assert c("cancel") == [] and o["cancel"]["open"] == "none"
    assert c("escape") == []
    assert len([x for x in c("double_click") if x[0] == "fetch"]) == 1
    assert c("tab_switch")[0][1].startswith("/api/library/positions/")   # 요청 시점의 탭을 쓴다
    assert o["fail"]["error"] == "없어요" and [x[0] for x in c("fail")] == ["fetch"]
    print("ok")


if __name__ == "__main__":
    sys.exit(main())
