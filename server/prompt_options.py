"""프롬프트 안 옵션을 해석하고 CSV 행에 적용한다."""

import csv
import io
import math
import re


_OPTION = re.compile(r"(?<!\\)--([a-zA-Z][a-zA-Z0-9_-]*)\b")
_ALIASES = {"r": "repeat", "aspect": "ar", "ckpt": "model"}
_KNOWN = {"ar", "res", "seed", "no", "repeat", "lora", "model", "steps", "cfg",
          "sampler", "scheduler", "denoise", "detail", "nodetail", "upscale", "frames", "fps", "dur"}
_INTEGER = re.compile(r"[0-9]+\Z")
_CHOICE = re.compile(r"(?<!\\)\{([^{}]*,[^{}]*)\}")

# 안내 API와 파서가 공유하는 옵션 목록이다. 파서의 이름·별칭과 함께 갱신한다.
OPTION_SPECS = (
    {"name": "ar", "aliases": ["aspect"], "format": "가로:세로", "example": "--ar 2:3", "description": "가로세로 비율을 지정해요."},
    {"name": "res", "aliases": [], "format": "기준 크기 또는 가로x세로", "example": "--res 1024", "description": "기준 크기나 정확한 가로·세로 크기를 지정해요."},
    {"name": "seed", "aliases": [], "format": "0 이상의 정수 또는 범위", "example": "--seed 1..4", "description": "시드나 최대 64개의 시드 범위를 지정해요."},
    {"name": "no", "aliases": [], "format": "텍스트", "example": "--no blurry", "description": "네거티브 프롬프트 뒤에 덧붙여요."},
    {"name": "repeat", "aliases": ["r"], "format": "1~64", "example": "--r 4", "description": "반복 장수를 지정해요."},
    {"name": "lora", "aliases": [], "format": "모델명:강도", "example": "--lora detail:0.8", "description": "LoRA를 추가해요. 여러 번 쓸 수 있어요."},
    {"name": "model", "aliases": ["ckpt"], "format": "모델명", "example": "--model anime", "description": "체크포인트 모델을 지정해요."},
    {"name": "steps", "aliases": [], "format": "1 이상의 정수", "example": "--steps 28", "description": "샘플링 단계를 지정해요."},
    {"name": "cfg", "aliases": [], "format": "0보다 큰 수", "example": "--cfg 7.5", "description": "CFG 값을 지정해요."},
    {"name": "sampler", "aliases": [], "format": "이름", "example": "--sampler euler", "description": "샘플러를 지정해요."},
    {"name": "scheduler", "aliases": [], "format": "이름", "example": "--scheduler normal", "description": "스케줄러를 지정해요."},
    {"name": "denoise", "aliases": [], "format": "0~1", "example": "--denoise 0.7", "description": "디노이즈 강도를 지정해요."},
    {"name": "detail", "aliases": [], "format": "값 없음", "example": "--detail", "description": "Face Detailer를 사용해요."},
    {"name": "nodetail", "aliases": [], "format": "값 없음", "example": "--nodetail", "description": "Face Detailer를 우회해요."},
    {"name": "upscale", "aliases": [], "format": "1~4", "example": "--upscale 2", "description": "기존 업스케일 노드의 배율을 지정해요."},
    {"name": "frames", "aliases": [], "format": "1 이상의 정수", "example": "--frames 81", "description": "영상 프레임 수를 지정해요."},
    {"name": "fps", "aliases": [], "format": "1 이상의 정수", "example": "--fps 16", "description": "영상 프레임레이트를 지정해요."},
    {"name": "dur", "aliases": [], "format": "초(s)", "example": "--dur 5s", "description": "영상 길이를 초로 지정해요. 기본 16fps예요."},
)


def parse_prompt_options(text):
    """(옵션을 뺀 문장, 해석한 값, 오류 목록)을 반환한다."""
    text = str(text or "")
    matches = list(_OPTION.finditer(text))
    options, errors, spans = {}, [], []
    for index, match in enumerate(matches):
        name = _ALIASES.get(match.group(1).lower(), match.group(1).lower())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip().rstrip(",").strip()
        spans.append((match.start(), end))
        if name not in _KNOWN:
            errors.append(f"--{match.group(1)}: 모르는 옵션이에요 (글자로 쓰려면 \\--를 사용해요).")
            continue
        if name in {"detail", "nodetail"}:
            if value:
                errors.append(f"--{name}: 값 없이 사용해요.")
            else:
                options["detail"] = name == "detail"
            continue
        if not value:
            errors.append(f"--{name}: 값이 필요해요.")
            continue
        try:
            if name == "ar":
                parts = value.split(":")
                if len(parts) != 2 or not all(_INTEGER.fullmatch(p) and int(p) > 0 for p in parts):
                    raise ValueError
                parsed = (int(parts[0]), int(parts[1]))
            elif name == "res":
                if _INTEGER.fullmatch(value) and int(value) > 0:
                    parsed = int(value)
                else:
                    parts = re.split("[xX×]", value)
                    if len(parts) != 2 or not all(_INTEGER.fullmatch(p) and int(p) > 0 for p in parts):
                        raise ValueError
                    parsed = (int(parts[0]), int(parts[1]))
            elif name == "seed" and ".." in value:
                bounds = value.split("..")
                if len(bounds) != 2 or not all(_INTEGER.fullmatch(part) for part in bounds):
                    raise ValueError
                first, last = map(int, bounds)
                if last < first or last - first + 1 > 64:
                    raise ValueError
                options.pop("seed", None)
                options["seed_range"] = (first, last)
                continue
            elif name in {"seed", "repeat", "steps", "frames", "fps"}:
                if not _INTEGER.fullmatch(value) or (name != "seed" and int(value) < 1):
                    raise ValueError
                parsed = int(value)
                if name == "repeat" and parsed > 64:
                    raise ValueError
            elif name in {"cfg", "denoise", "upscale"}:
                parsed = float(value)
                if not math.isfinite(parsed) or (name == "cfg" and parsed <= 0) or (name == "denoise" and not 0 <= parsed <= 1) or (name == "upscale" and not 1 <= parsed <= 4):
                    raise ValueError
            elif name == "dur":
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", value):
                    raise ValueError
                parsed = float(value[:-1])
                if not math.isfinite(parsed) or parsed <= 0:
                    raise ValueError
            elif name == "lora":
                model, sep, strength = value.rpartition(":")
                parsed = (model.strip(), float(strength)) if sep else None
                if not parsed or not parsed[0] or not math.isfinite(parsed[1]):
                    raise ValueError
                options.setdefault("loras", []).append(parsed)
                continue
            elif name in {"sampler", "scheduler"}:
                if not re.fullmatch(r"[A-Za-z0-9_+.-]+", value):
                    raise ValueError
                parsed = value
            else:
                parsed = value
            if name == "seed":
                options.pop("seed_range", None)
            options[name] = parsed
        except ValueError:
            errors.append(f"--{name}: 값 '{value}'이 올바르지 않아요.")
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    if "seed_range" in options and "repeat" in options:
        errors.append("시드 범위와 --r는 같이 쓸 수 없어요.")
    return text.replace("\\--", "--").strip(" ,\t\r\n"), options, errors


def expand_choices(text):
    """이스케이프한 중괄호는 보존하고 선택지를 최대 64줄로 펼친다."""
    values = [text]
    while True:
        expanded = []
        found = False
        for value in values:
            match = _CHOICE.search(value)
            if not match:
                expanded.append(value)
                continue
            found = True
            choices = [choice.strip() for choice in match.group(1).split(",")]
            if not all(choices) or len(expanded) + len(choices) > 64:
                raise ValueError("프롬프트 경우의 수는 최대 64줄이에요.")
            expanded.extend(value[:match.start()] + choice + value[match.end():] for choice in choices)
        values = expanded
        if len(values) > 64:
            raise ValueError("프롬프트 경우의 수는 최대 64줄이에요.")
        if not found:
            return [value.replace("\\{", "{") for value in values]


def _round_multiple(value, multiple):
    return max(multiple, int(value / multiple) * multiple)


def resolution(options, base=1024):
    """res가 기준 넓이를 뜻할 때 ar 비율을 적용한다."""
    res = options.get("res", base)
    if isinstance(res, tuple):
        return res
    ratio = options.get("ar", (1, 1))
    width = res * math.sqrt(ratio[0] / ratio[1])
    height = res * math.sqrt(ratio[1] / ratio[0])
    return _round_multiple(width, 64), _round_multiple(height, 64)


def validate_workflow_options(workflow, options):
    """존재가 필요한 노드는 작업 등록 시점에 확인한다."""
    nodes = [node for node in workflow.values() if isinstance(node, dict)]
    if options.get("detail") is True and not any(node.get("class_type") == "FaceDetailer" for node in nodes):
        raise ValueError("이 워크플로우에는 Face Detailer가 없어요.")
    if "upscale" in options and not any(node.get("class_type") in ("LatentUpscaleBy", "ImageScaleBy") for node in nodes):
        raise ValueError("이 워크플로우에는 업스케일 노드가 없어요.")


def apply_csv_options(csv_bytes, base=1024, expand_repeat=False, allow_video=False):
    """CSV의 프롬프트 열마다 옵션을 적용하고 실제 덮어쓴 값을 기록한다."""
    source = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(source))
    if not reader.fieldnames:
        return csv_bytes, []
    rows, records, changed = [], [], False
    for line, row in enumerate(reader, 2):
        combined = {}
        for column in ("trigger_prompt", "quality_prompt", "main_prompt", "prompt", "end_prompt", "user_prompt", "video_prompt"):
            if column not in row or not row[column]:
                continue
            clean, options, errors = parse_prompt_options(row[column])
            if errors:
                raise ValueError(f"{line}번째 줄 " + "; ".join(errors))
            if options or "\\--" in row[column]:
                row[column] = clean
                changed = True
            if options:
                combined.update({k: v for k, v in options.items() if k != "loras"})
                if "loras" in options:
                    combined.setdefault("loras", []).extend(options["loras"])
        if any(key in combined for key in ("frames", "fps", "dur")) and not allow_video:
            raise ValueError(f"{line}번째 줄 영상 옵션은 영상 템플릿에서만 쓸 수 있어요.")
        if "seed_range" in combined and "repeat" in combined:
            raise ValueError(f"{line}번째 줄 시드 범위와 --r는 같이 쓸 수 없어요.")
        if combined:
            changed = True
            applied = {}
            for key, column in (("seed", "seed"), ("model", "checkpoint"), ("steps", "steps"), ("cfg", "cfg"), ("repeat", "seeds"),
                                ("sampler", "sampler"), ("scheduler", "scheduler"), ("denoise", "denoise"),
                                ("detail", "detail"), ("upscale", "upscale"), ("fps", "fps")):
                if key in combined:
                    row[column] = ("true" if combined[key] else "false") if key == "detail" else str(combined[key])
                    applied[column] = combined[key]
            if "loras" in combined:
                row["loras"] = ";".join(f"{name}:{strength:g}" for name, strength in combined["loras"])
                applied["loras"] = row["loras"]
            if "no" in combined:
                row["negative_prompt"] = ", ".join(filter(None, ((row.get("negative_prompt") or "").strip(), combined["no"])))
                applied["negative_prompt"] = row["negative_prompt"]
            if "ar" in combined or "res" in combined:
                width, height = resolution(combined, base)
                row.update(width=str(width), height=str(height))
                applied.update(width=width, height=height)
            if "frames" in combined or "dur" in combined:
                frames = combined.get("frames") or max(1, round(combined["dur"] * combined.get("fps", 16)))
                row["frames"] = str(frames)
                applied["frames"] = frames
            if "seed_range" in combined:
                applied["seed_range"] = combined["seed_range"]
            records.append({"line": line, **applied})
        variants = [dict(row)]
        for column in ("trigger_prompt", "quality_prompt", "main_prompt", "prompt", "end_prompt", "user_prompt", "video_prompt"):
            if column not in row or not row[column]:
                continue
            next_variants = []
            for variant in variants:
                for value in expand_choices(variant[column]):
                    next_variants.append({**variant, column: value})
            variants = next_variants
            if len(variants) > 64:
                raise ValueError(f"{line}번째 줄 프롬프트 경우의 수는 최대 64줄이에요.")
        if variants != [row]:
            changed = True
        seeds = range(combined["seed_range"][0], combined["seed_range"][1] + 1) if "seed_range" in combined else range(combined.get("repeat", 1) if expand_repeat else 1)
        if len(variants) * len(seeds) > 64:
            raise ValueError(f"{line}번째 줄 프롬프트 경우의 수는 최대 64줄이에요.")
        for variant in variants:
            for index, seed in enumerate(seeds):
                repeated = dict(variant)
                if "seed_range" in combined:
                    repeated["seed"] = str(seed)
                elif expand_repeat and combined.get("seed") is not None:
                    repeated["seed"] = str(combined["seed"] + index)
                rows.append(repeated)
    if not changed:
        return csv_bytes, []
    fields = list(reader.fieldnames)
    fields += [key for key in ("seed", "width", "height", "negative_prompt", "checkpoint", "loras", "steps", "cfg", "seeds",
                               "sampler", "scheduler", "denoise", "detail", "upscale", "frames", "fps") if key not in fields and any(key in row for row in rows)]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8"), records
