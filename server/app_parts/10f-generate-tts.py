# 서비스 연동 낭독 음성 POST /api/generate/tts — 작가가 미리 만들어 두는 charchat 낭독(Qwen3-TTS, 성우 하나).
# 구간 목록을 작업 정보 파일에 적고 tts_batch 템플릿 작업을 만든다. 워크플로우는 ComfyUI 기본 노드 하나뿐이다 — 모델 파일이 필요 없어
# 아무 이미지 등급 워커에나 배정된다(자동 켜기·회원 키 규칙은 다른 생성 작업과 같다). 결과 MP3는 data/tts/<작업>/에 남고,
# 연동 앱이 GET으로 받아 간 뒤 DELETE로 지운다.

TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
TTS_LANGUAGES = {"Korean", "English", "Chinese", "Japanese"}
TTS_SPEAKER_RE = re.compile(r"^[A-Za-z][A-Za-z_]{0,39}$")
TTS_ITEM_ID_RE = re.compile(r"^[a-f0-9]{64}$")
TTS_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
TTS_MAX_ITEMS, TTS_MAX_TEXT, TTS_MAX_CHARS = 2000, 400, 60000


def tts_job_dir(job_id: str) -> Path:
    if not TTS_JOB_ID_RE.match(job_id or ""):
        raise HTTPException(404, "없는 작업이에요.")
    return data_dir("tts") / job_id


def _tts_job(request: Request, job_id: str) -> dict:
    user = me(request)
    with lock:
        job = dict(jobs.get(job_id) or {})
    if not job or job.get("deleted") or job.get("template_id") != "tts_batch":
        raise HTTPException(404, "없는 작업이에요.")
    if not auth.can_access(user, job.get("owner_id")):
        raise HTTPException(403, "이 작업을 볼 수 없어요.")
    return job


@app.post("/api/generate/tts")
async def generate_tts(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    speaker, language = body.get("speaker"), body.get("language", "Korean")
    if body.get("model", TTS_MODEL) != TTS_MODEL:
        raise HTTPException(400, "지원하지 않는 낭독 모델이에요.")
    if not isinstance(speaker, str) or not TTS_SPEAKER_RE.match(speaker) or language not in TTS_LANGUAGES:
        raise HTTPException(400, "성우·언어가 올바르지 않아요.")
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= TTS_MAX_ITEMS:
        raise HTTPException(400, f"구간은 1~{TTS_MAX_ITEMS}개여야 해요.")
    clean, seen = [], set()
    for item in items:
        item_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(item_id, str) or not TTS_ITEM_ID_RE.match(item_id) or item_id in seen:
            raise HTTPException(400, "구간 id가 올바르지 않아요.")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= TTS_MAX_TEXT:
            raise HTTPException(400, f"구간 글은 1~{TTS_MAX_TEXT}자여야 해요.")
        seen.add(item_id)
        clean.append({"id": item_id, "text": text.strip()})
    if sum(len(item["text"]) for item in clean) > TTS_MAX_CHARS:
        raise HTTPException(400, f"한 번에 {TTS_MAX_CHARS}자까지 맡길 수 있어요.")
    name = "tts_" + uuid.uuid4().hex[:12]
    manifest = {"name": name, "model": TTS_MODEL, "speaker": speaker, "language": language, "items": clean, "batch": 8}
    manifest_path = JOBS_DIR / f"{name}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    workflow = {"1": {"inputs": {"width": 64, "height": 64, "batch_size": 1}, "class_type": "EmptyLatentImage",
                      "_meta": {"title": "낭독 작업 자리(실행하지 않음)"}}}
    job = await create_job(resolve_template("tts_batch"), json.dumps(workflow).encode(), f"{name}.json", None, None,
                           {"tts_manifest": str(manifest_path.resolve())}, user=user, parse_inline_options=False)
    return {"job_id": job["id"], "status": job["status"], "items": len(clean)}


@app.get("/api/generate/tts/{job_id}")
def tts_job_status(job_id: str, request: Request):
    """작업 상태와, 끝났으면 받은 구간 목록(id·길이·크기)."""
    job = _tts_job(request, job_id)
    clips = []
    result = tts_job_dir(job_id) / "result.json"
    if job.get("status") == "done" and result.is_file():
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        clips = [{"id": key, "duration": value.get("duration"), "size": value.get("size")}
                 for key, value in data.items() if TTS_ITEM_ID_RE.match(key) and (tts_job_dir(job_id) / f"{key}.mp3").is_file()]
    return {"status": job.get("status"), "progress": job.get("progress"), "clips": clips}


@app.get("/api/generate/tts/{job_id}/{clip}")
def tts_job_clip(job_id: str, clip: str, request: Request):
    _tts_job(request, job_id)
    if not clip.endswith(".mp3") or not TTS_ITEM_ID_RE.match(clip[:-4]):
        raise HTTPException(404, "없는 구간이에요.")
    path = tts_job_dir(job_id) / clip
    if not path.is_file():
        raise HTTPException(404, "없는 구간이에요.")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/generate/tts/{job_id}")
def tts_job_delete(job_id: str, request: Request):
    """연동 앱이 다 받아 간 뒤 결과 폴더를 지운다(작업 기록은 남는다)."""
    _tts_job(request, job_id)
    shutil.rmtree(tts_job_dir(job_id), ignore_errors=True)
    return {"ok": True}
