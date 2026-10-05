"""실행 템플릿의 Face Detailer 주입 검사(NS-34-5) — 모의 ComfyUI에 run_once를 돌려 보낸 payload를 본다.

실행: python tests/test_face_detailer_templates.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import workflow_builder  # noqa: E402

SENT = []


class FakeComfy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/prompt":
            SENT.append(json.loads(body)["prompt"])
            self._reply({"prompt_id": f"p{len(SENT)}"})
        else:  # /upload/image
            self._reply({"name": "uploaded.png", "subfolder": "", "type": "input"})

    def do_GET(self):
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[1]
            self._reply({pid: {"status": {"status_str": "success"}, "outputs": {}}})
        else:
            self._reply({"queue_running": [], "queue_pending": []})


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "templates" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def by_title(wf, title):
    return next(n for n in wf.values() if n["_meta"]["title"] == title)


def fd(wf):
    return next(n for n in wf.values() if n["class_type"] == "FaceDetailer")


def main():
    server = HTTPServer(("127.0.0.1", 0), FakeComfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    tmp = tempfile.mkdtemp()
    os.environ.update(POLL_INTERVAL_SEC="0", NIGHTSHIFT_INPUT_IMAGES_DIR=tmp)
    (Path(tmp) / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    post = workflow_builder.build_workflow({"checkpoint": "a.safetensors", "positive": "built", "negative": "built neg",
                                            "face_detailer": {"enabled": True}})
    only_fd = workflow_builder.build_workflow({"checkpoint": "a.safetensors", "base": "face_detailer",
                                              "positive": "built", "negative": "built neg"})

    # 1) seed_batch: 얼굴 프롬프트가 비면 메인 긍정·부정을 쓰고, 시드는 KSampler와 같다.
    for k in ("FACE_PROMPT", "FACE_NEGATIVE_PROMPT"):
        os.environ.pop(k, None)
    seed_batch = load("seed_batch")
    seed_batch.run_once(post, url, 4242, 1, "a girl", "ugly", "", "")
    wf = SENT[-1]
    assert fd(wf)["inputs"]["seed"] == 4242 == by_title(wf, "KSampler")["inputs"]["seed"]
    assert by_title(wf, "Face Detailer 긍정")["inputs"]["text"] == "a girl"
    assert by_title(wf, "Face Detailer 부정")["inputs"]["text"] == "ugly"
    assert by_title(wf, "main_prompt")["inputs"]["text"] == "a girl"

    # 얼굴 프롬프트를 채우면 그 값이 들어간다.
    os.environ.update(FACE_PROMPT="detailed face", FACE_NEGATIVE_PROMPT="blurry face")
    seed_batch.run_once(post, url, 7, 2, "a girl", "ugly", "", "")
    wf = SENT[-1]
    assert by_title(wf, "Face Detailer 긍정")["inputs"]["text"] == "detailed face"
    assert by_title(wf, "Face Detailer 부정")["inputs"]["text"] == "blurry face"
    assert fd(wf)["inputs"]["seed"] == 7

    # 2) csv_batch: 얼굴 부정이 비면 그 행의 negative_prompt 값을 쓴다(행마다 다르게).
    os.environ.update(FACE_PROMPT="detailed face", FACE_NEGATIVE_PROMPT="")
    csv_batch = load("csv_batch")
    for i, neg in enumerate(("row1 neg", "row2 neg"), start=1):
        csv_batch.run_once(post, url, {"main_prompt": f"row{i}", "negative_prompt": neg}, "t", str(100 + i), i)
        wf = SENT[-1]
        assert fd(wf)["inputs"]["seed"] == 100 + i
        assert by_title(wf, "Face Detailer 부정")["inputs"]["text"] == neg
        assert by_title(wf, "Face Detailer 긍정")["inputs"]["text"] == "detailed face"
    # 행에 부정이 없으면 워크플로우의 메인 부정 노드 값을 쓴다.
    csv_batch.run_once(post, url, {"main_prompt": "row3"}, "t", "5", 3)
    assert by_title(SENT[-1], "Face Detailer 부정")["inputs"]["text"] == "built neg"

    # 3) input_image_csv_batch + face_detailer 유형(메인 프롬프트 노드 없음): 행 값으로 대체된다.
    os.environ.update(FACE_PROMPT="", FACE_NEGATIVE_PROMPT="")
    iicsv = load("input_image_csv_batch")
    iicsv.run_once(only_fd, url, {"main_prompt": "csv face", "negative_prompt": "csv neg"}, "t", "9", 1,
                   Path(tmp) / "face.png")
    wf = SENT[-1]
    assert fd(wf)["inputs"]["seed"] == 9
    assert by_title(wf, "Face Detailer 긍정")["inputs"]["text"] == "csv face"
    assert by_title(wf, "Face Detailer 부정")["inputs"]["text"] == "csv neg"

    # 4) input_image_batch: 시드·얼굴 긍정 주입, 부정은 비면 빌드 값 유지.
    os.environ.update(FACE_PROMPT="ib face")
    iib = load("input_image_batch")
    iib.run_once(only_fd, url, 31, Path(tmp) / "face.png", 1, None, tmp, "")
    wf = SENT[-1]
    assert fd(wf)["inputs"]["seed"] == 31
    assert by_title(wf, "Face Detailer 긍정")["inputs"]["text"] == "ib face"
    assert by_title(wf, "Face Detailer 부정")["inputs"]["text"] == "built neg"

    # 5) manifest: 네 템플릿에 얼굴 프롬프트 옵션이 있다.
    manifest = json.loads((ROOT / "templates" / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        if item["id"] in ("seed_batch", "csv_batch", "input_image_batch", "input_image_csv_batch"):
            names = {o["name"] for o in item["options"]}
            assert {"face_prompt", "face_negative_prompt"} <= names, item["id"]

    server.shutdown()
    print("ok — test_face_detailer_templates")


if __name__ == "__main__":
    main()
