"""
영상 편집(자르기 / 이어 붙이기) — ffmpeg로 새 영상을 만든다. 원본은 건드리지 않는다.

ComfyUI 파드는 클립 하나씩만 만들어 주므로(여러 클립을 잇는 기능이 코어에 없다), 이어 붙이기와 자르기는 서버가
결과물 폴더의 영상 파일을 ffmpeg로 처리한다. 폰에서도 갤러리에서 고르기만 하면 되는 가벼운 편집이 목적이다.

ffmpeg는 환경변수 NIGHTSHIFT_FFMPEG → PATH → imageio-ffmpeg 패키지가 들고 있는 바이너리 순으로 찾는다.

이어 붙이기:
  - 모든 클립의 코덱/해상도/프레임률/오디오 구성이 같으면 다시 인코딩하지 않고(concat demuxer, -c copy) 이어 붙인다 —
    같은 워크플로우가 만든 클립들은 보통 여기 해당하고 화질 손실이 없다.
  - 다르면 첫 클립의 해상도/프레임률에 맞춰(비율 유지, 여백은 검정) 다시 인코딩한다. 오디오가 있는 클립과 없는 클립이
    섞여 있으면 없는 쪽에 무음을 채운다.
자르기: 시작/끝 시각으로 정확히 자르려고 다시 인코딩한다.

작업은 스레드에서 돌고 진행률(0~1)을 폴링으로 볼 수 있다. 서버가 죽으면 작업 기록도 사라지지만 만들다 만 파일은
.part로 남지 않게 임시 이름으로 만든 뒤 끝나면 이름을 바꾼다.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

MAX_INPUTS = 30
MAX_JOBS_KEPT = 60
_slots = threading.Semaphore(int(os.environ.get("NIGHTSHIFT_EDIT_CONCURRENCY", "2")))
_jobs: dict[str, dict] = {}
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


class EditError(Exception):
    pass


def find_ffmpeg() -> str | None:
    env = (os.environ.get("NIGHTSHIFT_FFMPEG") or "").strip()
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+[^:]*:\s*Video:\s*([A-Za-z0-9_]+)[^\n]*?,\s*(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+[^:]*:\s*Audio:\s*([A-Za-z0-9_]+)[^\n]*?,\s*(\d+)\s*Hz")


def probe(path: Path) -> dict:
    """ffmpeg -i의 출력에서 길이/코덱/해상도/프레임률/오디오 여부를 읽는다(ffprobe가 없어도 되게)."""
    ff = find_ffmpeg()
    if ff is None:
        raise EditError("ffmpeg를 찾을 수 없어요.")
    res = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    text = res.stderr
    d = _DUR_RE.search(text)
    v = _VIDEO_RE.search(text)
    if not v:
        raise EditError(f"영상 정보를 읽지 못했어요: {path.name}")
    a = _AUDIO_RE.search(text)
    fps_m = _FPS_RE.search(text[v.start():v.start() + 400])
    if d:
        duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    else:
        # 스트리밍으로 만든 파일(예: ComfyUI의 일부 mp4)은 컨테이너에 길이가 없다("Duration: N/A") —
        # 복사만 하는 빠른 패스로 끝까지 읽어서 마지막 time= 값을 길이로 쓴다.
        duration = _measure_duration(ff, path)
    return {
        "duration": duration,
        "codec": v.group(1), "width": int(v.group(2)), "height": int(v.group(3)),
        "fps": float(fps_m.group(1)) if fps_m else 0.0,
        "audio": a is not None, "audio_codec": a.group(1) if a else None,
        "sample_rate": int(a.group(2)) if a else None,
    }


def _measure_duration(ff: str, path: Path) -> float:
    res = subprocess.run([ff, "-hide_banner", "-i", str(path), "-c", "copy", "-f", "null", "-"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=300)
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    if not times:
        raise EditError(f"영상 길이를 알 수 없어요: {path.name}")
    h, m, sec = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _same_streams(infos: list[dict]) -> bool:
    first = infos[0]
    for i in infos[1:]:
        if (i["codec"], i["width"], i["height"], i["audio"], i["audio_codec"], i["sample_rate"]) != (
                first["codec"], first["width"], first["height"], first["audio"], first["audio_codec"], first["sample_rate"]):
            return False
        if abs(i["fps"] - first["fps"]) > 0.01:
            return False
    return first["codec"] in ("h264", "hevc", "vp9", "av1", "mpeg4")


def _even(n: int) -> int:
    return n - (n % 2)


def build_concat_command(ff: str, paths: list[Path], infos: list[dict], out: Path, list_file: Path | None) -> tuple[list[str], str]:
    """(명령, 방식) — 방식은 'copy'(재인코딩 없음) 또는 'reencode'."""
    if _same_streams(infos) and list_file is not None:
        lines = []
        for p in paths:
            escaped = str(p).replace("\\", "/").replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ([ff, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
                 "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)], "copy")
    first = infos[0]
    w, h = _even(first["width"]), _even(first["height"])
    fps = first["fps"] if first["fps"] > 0 else 24
    any_audio = any(i["audio"] for i in infos)
    cmd = [ff, "-hide_banner", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    silent_index = {}
    if any_audio:
        for idx, info in enumerate(infos):
            if not info["audio"]:
                silent_index[idx] = len(paths) + len(silent_index)
                cmd += ["-f", "lavfi", "-t", f"{info['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    parts = []
    for idx in range(len(paths)):
        parts.append(f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps:g},format=yuv420p[v{idx}]")
        if any_audio:
            src = f"[{silent_index[idx]}:a]" if idx in silent_index else f"[{idx}:a]"
            parts.append(f"{src}aresample=48000,aformat=channel_layouts=stereo[a{idx}]")
    joined = "".join(f"[v{i}]" + (f"[a{i}]" if any_audio else "") for i in range(len(paths)))
    parts.append(f"{joined}concat=n={len(paths)}:v=1:a={1 if any_audio else 0}[v]" + ("[a]" if any_audio else ""))
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]"]
    if any_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(out)]
    return cmd, "reencode"


def build_trim_command(ff: str, src: Path, info: dict, start: float, end: float, out: Path) -> list[str]:
    cmd = [ff, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
           "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if info["audio"] else ["-an"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "op", "status", "progress", "error", "output", "method", "duration", "started_at", "finished_at")}



def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def owner_of(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        return job["owner_id"] if job else None


def cancel(job_id: str) -> bool:
    with _lock:
        proc = _procs.get(job_id)
        job = _jobs.get(job_id)
        if job is None:
            return False
        job["cancel"] = True
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return True


def _prune() -> None:
    finished = sorted((j for j in _jobs.values() if j["status"] in ("done", "error", "cancelled")),
                      key=lambda j: j["started_at"])
    for j in finished[:-MAX_JOBS_KEPT]:
        _jobs.pop(j["id"], None)


def start(op: str, sources: list[Path], out_path: Path, owner_id, *, start_s: float | None = None,
          end_s: float | None = None, on_done=None, out_rel: str | None = None, post=None) -> str:
    """편집 작업을 스레드에서 시작하고 id를 돌려준다. 입력 검증(존재/개수)은 부르는 쪽이 이미 했다.
    out_rel은 결과 파일의 화면용 이름(출력 폴더 기준 상대 경로), post는 파일이 다 만들어진 뒤 "완료"로 표시하기 전에
    부르는 함수(색인 등록처럼 완료를 본 쪽이 곧바로 그 파일을 찾을 수 있어야 하는 후처리)."""
    ff = find_ffmpeg()
    if ff is None:
        raise EditError("ffmpeg를 찾을 수 없어요 — 서버에 ffmpeg를 설치하거나 NIGHTSHIFT_FFMPEG를 지정하세요.")
    if op == "concat" and not 2 <= len(sources) <= MAX_INPUTS:
        raise EditError(f"이어 붙일 영상은 2~{MAX_INPUTS}개여야 해요.")
    if op == "trim" and len(sources) != 1:
        raise EditError("자르기는 영상 하나만 고를 수 있어요.")
    job = {"id": uuid.uuid4().hex[:10], "op": op, "status": "queued", "progress": 0.0, "error": None,
           "output": None, "method": None, "duration": None, "started_at": time.time(), "finished_at": None,
           "owner_id": owner_id, "cancel": False}
    with _lock:
        _jobs[job["id"]] = job
        _prune()
    threading.Thread(target=_run, args=(job, ff, op, sources, out_path, start_s, end_s, on_done, out_rel, post), daemon=True).start()
    return job["id"]


def _run(job, ff, op, sources, out_path, start_s, end_s, on_done, out_rel, post):
    tmp_dir = Path(tempfile.mkdtemp(prefix="ns_edit_"))
    tmp_out = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)
    try:
        with _slots:
            if job["cancel"]:
                job["status"] = "cancelled"
                return
            job["status"] = "running"
            infos = [probe(p) for p in sources]
            if op == "trim":
                info = infos[0]
                start = max(0.0, float(start_s or 0.0))
                end = float(end_s) if end_s is not None else info["duration"]
                end = min(end, info["duration"])
                if end - start < 0.1:
                    raise EditError("끝 시각이 시작 시각보다 0.1초 이상 뒤여야 해요.")
                total = end - start
                cmd = build_trim_command(ff, sources[0], info, start, end, tmp_out)
                job["method"] = "reencode"
            else:
                total = sum(i["duration"] for i in infos)
                cmd, job["method"] = build_concat_command(ff, sources, infos, tmp_out, tmp_dir / "list.txt")
            job["duration"] = total
            out_path.parent.mkdir(parents=True, exist_ok=True)
            err_file = tmp_dir / "ffmpeg.err"
            with open(err_file, "wb") as errf:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, encoding="utf-8", errors="replace")
                with _lock:
                    _procs[job["id"]] = proc
                for line in proc.stdout:
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            done_s = float(line.split("=", 1)[1]) / 1_000_000
                            job["progress"] = max(0.0, min(0.99, done_s / total)) if total > 0 else 0.0
                        except ValueError:
                            pass
                code = proc.wait()
            if job["cancel"]:
                job["status"] = "cancelled"
                return
            if code != 0 or not tmp_out.exists():
                tail = err_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-4:]
                raise EditError("ffmpeg가 실패했어요: " + " / ".join(tail))
            os.replace(tmp_out, out_path)
            if post is not None:
                try:
                    post(out_path)
                except Exception:
                    pass
            job["progress"] = 1.0
            job["output"] = out_rel or out_path.name
            job["status"] = "done"
    except EditError as e:
        job["status"] = "error"
        job["error"] = str(e)
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"편집에 실패했어요: {e}"
    finally:
        with _lock:
            _procs.pop(job["id"], None)
        job["finished_at"] = time.time()
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if on_done is not None:
            try:
                on_done(job)
            except Exception:
                pass
