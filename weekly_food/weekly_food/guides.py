"""Practical recipe and restaurant guides with field-level evidence and missing facts."""
from __future__ import annotations

import re
from .ai import AIError
from .core import normalized

GUIDE_TYPES = {"레시피·베이킹 만들기": "recipe", "식당 방문 가이드": "restaurant"}
FIELDS = {
    "recipe": {
        "overview": "어떤 음식인가요?", "yield": "분량·준비 시간", "ingredients": "재료와 정확한 계량",
        "tools": "준비 도구", "steps": "단계별 만드는 방법", "baking": "발효·굽기 온도와 시간",
        "tips": "실패를 줄이는 포인트", "storage": "보관·다시 데우기",
    },
    "restaurant": {
        "overview": "어떤 곳인가요?", "identity": "매장·지점 확인", "menu": "대표 메뉴와 가격",
        "directions": "주소·가는 길", "hours": "영업시간·휴무·브레이크타임",
        "reservation": "예약·웨이팅 꿀팁", "parking": "주차 위치·요금·지원 조건", "tips": "방문 전 체크",
    },
}
RECIPE_CORE = {"yield", "ingredients", "steps", "baking"}


def resolve_type(category, requested=None):
    mode = requested or ("restaurant" if category == "맛집" else "recipe")
    if mode not in FIELDS:
        raise ValueError("글 유형은 recipe 또는 restaurant이어야 합니다.")
    return mode


def detail_queries(term, mode):
    suffixes = (["", "레시피 재료 계량", "만들기 발효 굽기 온도 시간", "실패 원인 보관"]
                if mode == "recipe" else
                ["", "공식 지점 주소 가는길", "영업시간 휴무 브레이크타임", "예약 오픈 시간 웨이팅", "주차 요금 지원"])
    return [(term + " " + suffix).strip() for suffix in suffixes]


def validate_guide(data, sources, mode, base_id=None):
    """Reject invented citations, unsupported numbers, and mixed base recipes."""
    known = {s.id: s for s in sources}
    fields = {key: [] for key in FIELDS[mode]}
    issues = []
    if mode == "recipe" and base_id not in known:
        base_id = None
    rows = data.get("facts", [])
    if not isinstance(rows, list):
        raise AIError("실용 정보 응답의 형식이 올바르지 않습니다.")
    for row in rows[:70]:
        if not isinstance(row, dict):
            continue
        field, text, sid, quote = (row.get(k) for k in ("field", "text", "source_id", "quote"))
        if not isinstance(field, str) or field not in fields or not isinstance(sid, str) or sid not in known or not isinstance(text, str) or not isinstance(quote, str):
            continue
        if not 2 <= len(text) <= 700 or not 4 <= len(quote) <= 1000:
            continue
        source = known[sid]
        if not normalized(quote) or normalized(quote) not in normalized(source.summary):
            issues.append(f"{FIELDS[mode][field]}: 근거 원문과 대조되지 않는 문장을 제외했습니다.")
            continue
        if re.search(r"https?://|제가|다녀|내돈내산|먹어\s?보|전국\s*1위", text):
            continue
        # Numeric expressions retain their attached units, not merely a digit anywhere in a document.
        numbers = re.findall(r"\d+(?:[.,:]\d+)*(?:\s*(?:kg|mg|ml|g|L|℃|°C|도|분|시간|원|개|인분|일|시|대|층|번))?", text, re.I)
        if any(normalized(n) not in normalized(quote) for n in numbers):
            issues.append(f"{FIELDS[mode][field]}: 근거에 없는 수치·단위를 제외했습니다.")
            continue
        if mode == "recipe" and field in RECIPE_CORE and sid != base_id:
            issues.append(f"{FIELDS[mode][field]}: 기준 레시피 외 자료의 계량·공정을 섞지 않았습니다.")
            continue
        fields[field].append({"text": text.strip(), "source_id": sid, "quote": quote.strip()})
    # Conflicting operational facts must be reviewed rather than silently merged.
    for field in ({"identity", "hours", "reservation", "parking"} if mode == "restaurant" else set()):
        rows = fields[field]
        publishers = {row["source_id"] for row in rows}
        if len(publishers) > 1 and len({normalized(r["text"]) for r in rows}) > 1:
            issues.append(f"{FIELDS[mode][field]}: 여러 출처의 안내가 달라 원문 대조가 필요합니다.")
            fields[field] = []
    missing = [FIELDS[mode][key] for key, values in fields.items() if not values]
    return {"type": mode, "fields": fields, "missing": missing, "issues": list(dict.fromkeys(issues)),
            "base_source_id": base_id, "complete": not missing}


def make_guide(topic, sources, writer=None):
    mode = resolve_type(topic.category, getattr(topic, "guide_type", None))
    base_id = getattr(topic, "base_source_id", None)
    data = {"facts": []}
    aliases = {"재료": "ingredients", "분량": "yield", "도구": "tools", "만드는 방법": "steps",
               "과정": "steps", "굽기": "baking", "온도": "baking", "보관": "storage", "팁": "tips",
               "매장": "identity", "메뉴": "menu", "주소": "directions", "가는 길": "directions",
               "영업시간": "hours", "휴무": "hours", "예약": "reservation", "주차": "parking", "소개": "overview"}
    aliases.update({heading: key for key, heading in FIELDS[mode].items()})
    for source in sources:
        if source.kind not in {"recipe_source", "venue_source"}:
            continue
        # Supplied labeled notes also work without an AI key; retain the literal evidence.
        for label, field in aliases.items():
            if field not in FIELDS[mode]:
                continue
            match = re.search(re.escape(label) + r"\s*[:：]\s*(.*?)(?=(?:" +
                              "|".join(re.escape(k) for k in aliases) + r")\s*[:：]|$)", source.summary)
            if match and len(match.group(1).strip()) >= 4:
                quote = match.group(1).strip()[:700]
                data["facts"].append({"field": field, "text": quote, "source_id": source.id, "quote": quote})
    if writer:
        data = writer.json(
            "네이버 블로그 실용 가이드를 작성합니다. 뉴스 제목을 나열하지 마세요. "
            "제공된 summary 원문에 명시된 사실만 필드별로 정리하세요. "
            "직접 방문·제작한 경험을 만들지 말고 친근한 ~요체와 짧은 모바일 문단을 사용하세요. "
            "한 항목에 한 사실을 쓰고, 근거 문구 quote를 summary에서 연속된 문자열 그대로 발췌하세요. "
            "가격·주소·영업시간·예약·주차는 동일 매장·지점인지 자료로 확인할 수 있을 때만 적으세요. "
            "만들기 유형은 base_source_id의 분량·계량·순서·온도·시간을 하나의 기준으로 유지하세요. "
            "기준 레시피가 없으면 구체적인 계량과 공정은 비워 두세요. 조리·보관·알레르기 정보를 추측하지 마세요. "
            "기준에 없는 대체 재료나 오븐 온도 변환도 만들지 마세요. 확인되지 않은 항목은 생략하세요. "
            '출력 형식: {"facts":[{"field":"허용된 필드 키", "text":"자연스럽게 풀어 쓴 사실", '
            '"source_id":"S...", "quote":"근거 원문 그대로"}]}',
            {"topic": topic.name, "type": mode, "fields": FIELDS[mode], "base_source_id": base_id,
             "sources": [{"id": s.id, "title": s.title, "summary": s.summary, "kind": s.kind} for s in sources]},
        )
    return validate_guide(data, sources, mode, base_id)


def render_guide(name, guide):
    recipe = guide["type"] == "recipe"
    title = f"{name} 만들기, 재료부터 과정·실패 방지 팁까지" if recipe else f"{name} 방문 가이드, 가는 길·영업시간·예약·주차 정리"
    lines = [title, "", "공개 자료를 바탕으로 정리한 정보성 글이에요. 직접 만들거나 방문한 후기는 아니에요.", ""]
    if recipe:
        lines += ["계량과 공정은 아래 기준 레시피에서 확인된 내용만 사용했어요.", ""]
        if guide["base_source_id"]:
            lines += [f"기준 레시피: [{guide['base_source_id']}]", ""]
    for key, heading in FIELDS[guide["type"]].items():
        lines.extend(["## " + heading, ""])
        rows = guide["fields"][key]
        if not rows:
            lines.extend(["[확인 필요] 해당 항목은 충분한 근거를 확보하지 못했어요.", ""])
        for i, row in enumerate(rows, 1):
            prefix = f"{i}. " if key == "steps" else ""
            lines.extend([prefix + row["text"] + f" [{row['source_id']}]", ""])
    if guide["missing"]:
        lines += ["## 아직 확인할 정보", "", " · ".join(guide["missing"]), ""]
    return title, "\n".join(lines)
