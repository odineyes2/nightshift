# 프롬프트 옵션 안내에 필요한 파서 명세를 로그인한 회원에게 제공한다.

@app.get("/api/prompt-options/spec")
def prompt_options_spec(request: Request):
    me(request)
    return {"options": prompt_options.OPTION_SPECS}


@app.post("/api/prompt-options/parse")
async def prompt_options_parse(request: Request):
    me(request)
    body = await read_json_object(request, allow_empty=False)
    if not isinstance(body.get("text"), str):
        raise HTTPException(400, "text는 문자열이어야 해요.")
    clean_text, options, errors = prompt_options.parse_prompt_options(body["text"])
    size = prompt_options.resolution(options) if "ar" in options or "res" in options else None
    return {"clean_text": clean_text, "options": options, "errors": errors, "resolution": size}
