# 프롬프트 옵션 안내에 필요한 파서 명세를 로그인한 회원에게 제공한다.

@app.get("/api/prompt-options/spec")
def prompt_options_spec(request: Request):
    me(request)
    return {"options": prompt_options.OPTION_SPECS}
