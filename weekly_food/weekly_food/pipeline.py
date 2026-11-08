"""Research pipeline and portable Markdown/text/JSON export."""
from __future__ import annotations

import io
import base64
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .ai import AIError
from .guides import make_guide, render_guide
from .core import Evidence, KST, deduplicate, food_related, group_candidates, rank_topics
from .sources import collect

METHOD = (
    "수집 표본 안의 관심도이며 전국 인기·맛 평가·검색량 순위가 아닙니다. "
    "발행자 수 35점(log2, 10곳에서 상한), 출처 유형 수 25점(뉴스/블로그/유튜브 3종에서 상한), "
    "최신성 25점(경과일/3 지수감쇠 평균), 유튜브 반응 15점(log10, 누적조회/최소 1일, 일 100만에서 상한)의 합입니다. "
    "조회수는 수집 시점의 누적값으로 주간 증가량이 아닙니다. "
    "과거 날짜를 지정하면 당시 조회수를 복원할 수 없어 유튜브 반응 점수는 0점입니다. "
    "같은 URL·동일 제목은 하나로 줄이고, 같은 발행자의 여러 글은 발행자 수에서 중복 제외합니다. "
    "전재·광고·중복 계정을 완전히 판별할 수 없으며 검색 결과 상한과 검색어에 영향을 받습니다."
)


def balanced_sample(items, limit=120):
    """Don't let the first query or platform exhaust the AI context budget."""
    buckets = {}
    for source in sorted(items, key=lambda s: s.published, reverse=True):
        buckets.setdefault(source.kind, []).append(source)
    result = []
    while buckets and len(result) < limit:
        for kind in list(buckets):
            result.append(buckets[kind].pop(0))
            if not buckets[kind]:
                del buckets[kind]
            if len(result) == limit:
                break
    return result


def demo_sources(window):
    examples = [
        ("news", "example.com", "가상 소금크림빵, 디저트 소식에 등장"),
        ("blog", "sample-blog", "가상 소금크림빵 관련 공개 블로그 정보"),
        ("youtube", "sample-channel", "가상 소금크림빵 소개 영상"),
        ("news", "example.org", "가상 들깨면, 음식 소식에 등장"),
        ("blog", "sample-blog-two", "가상 들깨면 관련 공개 블로그 정보"),
    ]
    return [Evidence(title, f"https://example.com/demo/{i}",
                     (window.end - timedelta(hours=i + 1)).isoformat(), kind, publisher,
                     publisher, summary="실제 정보가 아닌 기능 시연용 가상 자료입니다.",
                     views=12000 if kind == "youtube" else 0)
            for i, (kind, publisher, title) in enumerate(examples)]


def research(window, queries, credentials=None, sources=None, limit=20,
             min_sources=2, focus=None, writer=None, demo=False, progress=None):
    if not queries or len(queries) > 30 or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("검색어는 1~30개를 입력하세요.")
    if not 1 <= limit <= 50 or not 1 <= min_sources <= 10:
        raise ValueError("검색어별 수집 상한은 1~50, 최소 출처 수는 1~10이어야 합니다.")
    sources = list(dict.fromkeys(sources if sources is not None else ["rss", "news", "blog", "youtube"]))
    if not sources or set(sources) - {"rss", "news", "blog", "youtube"}:
        raise ValueError("수집원은 rss, news, blog, youtube 중에서 선택하세요.")
    warnings = []
    if demo:
        raw, status = demo_sources(window), [{"source": "demo", "state": "ok", "count": 5}]
        focus = ["가상 소금크림빵", "가상 들깨면"]
        writer = None  # Demo must never spend API quota or send fabricated data to an API.
    else:
        raw, status = collect(queries, window, credentials or {}, sources, limit, progress)
    deduped = deduplicate(raw, window)
    items = [s for s in deduped if food_related(s)]
    topics = group_candidates(items, focus)
    ai_sample_count = 0
    if writer and items and not focus:
        sample = balanced_sample(items)
        ai_sample_count = len(sample)
        if progress:
            progress(f"AI로 음식명·가게명 분류 중 ({len(sample)}건)")
        try:
            discovered = writer.discover(sample)
            if discovered:
                # Keep unassigned headlines as candidates instead of silently dropping them.
                assigned = {sid for topic in discovered for sid in topic.source_ids}
                topics = discovered + group_candidates([s for s in items if s.id not in assigned])
            else:
                warnings.append("AI가 근거 있는 주제명을 찾지 못해 음식 키워드·제목 묶음을 사용했습니다.")
        except AIError as exc:
            warnings.append(str(exc) + " 음식 키워드·제목 묶음을 사용했습니다.")
    ranked = rank_topics(topics, items, window, min_sources)
    if not items:
        warnings.append("해당 기간의 자료가 없습니다. 수집 상태·키·검색어를 확인하세요.")
    elif not ranked:
        warnings.append("최소 독립 발행자/자료 수를 충족하는 주제가 없습니다. 검색어·관심 주제를 좁히거나 최소 출처 수를 조정하세요.")
    return {
        "generated_at": datetime.now(KST).isoformat(),
        "window": {"start": window.start.isoformat(), "end_exclusive": window.end.isoformat(), "label": window.label},
        "demo": demo, "queries": queries, "min_sources": min_sources,
        "collection_counts": {"raw": len(raw), "deduplicated_in_window": len(deduped),
                              "irrelevant_removed": len(deduped) - len(items)},
        "method": METHOD, "ai_sample_count": ai_sample_count,
        "source_status": status, "warnings": warnings,
        "sources": [s.to_dict() for s in items], "topics": [t.to_dict() for t in ranked],
        "drafts": [],
    }


def source_footer(sources):
    lines = ["## 출처", "", "뉴스·영상 날짜는 게시일이며 실제 방송일·방문일과 다를 수 있어요. 배경 자료는 확인일을 따로 표시했어요.", ""]
    for source in sources:
        published = source.published.astimezone(KST).strftime("%Y-%m-%d")
        if source.date_precision == "retrieved":
            published = "배경 자료 확인일 " + published + " (게시일 아님)"
        views = f" · 수집 시 누적 {source.views:,}회" if source.kind == "youtube" else ""
        title = source.title.replace("[", "(").replace("]", ")")
        lines.append(f"- [{source.id}] [{title}]({source.url}) · {source.publisher} · {published}{views}")
        if source.kind == "reference" and "wikipedia.org" in source.url:
            lines.append("  위키백과 기여자 · 배경 발췌/요약 · CC BY-SA 4.0: https://creativecommons.org/licenses/by-sa/4.0/")
    return "\n".join(lines)


def create_drafts(report, top=5, writer=None, progress=None):
    from .core import Topic

    if not 1 <= top <= 20:
        raise ValueError("원고 개수는 1~20개여야 합니다.")
    by_id = {row["id"]: Evidence(**row) for row in report["sources"]}
    drafts = []
    for row in report["topics"][:top]:
        topic = Topic(**{key: value for key, value in row.items() if key in Topic.__dataclass_fields__})
        sources = [by_id[sid] for sid in topic.source_ids]
        # Send only the 12 most recent evidence records for each draft, but export all links.
        background = [s for s in sources if s.kind == "reference"][:2]
        sample = background + sorted((s for s in sources if s.kind != "reference"),
                                     key=lambda s: (bool(s.summary), s.published), reverse=True)[:12 - len(background)]
        mode = "근거 정리"
        title = (f"{topic.name}, 이번 주 공개 자료에서 살펴본 이야기" if topic.grouping != "headline"
                 else f"이번 주 음식 소식: {topic.name}")
        paragraphs = []
        if writer and not report["demo"] and not row.get("guide_type"):
            if progress:
                progress(f"정보성 원고 작성 중 · {topic.name}")
            try:
                paragraphs = writer.draft(topic, sample)
                mode = "AI 정보성 초안"
            except AIError as exc:
                report["warnings"].append(f"{topic.name}: {exc}")
        if not paragraphs:
            paragraphs = [{
                "text": (f"{s.publisher}에서 ‘{s.title}’의 배경을 살펴볼 수 있어요. " + s.summary[:200]
                         if s.kind == "reference" else
                         f"{s.published.astimezone(KST):%m월 %d일} {s.publisher}에서 "
                         f"‘{s.title}’이라는 제목으로 관련 소식을 전했어요."),
                "source_ids": [s.id],
            } for s in sample[:5]]
        lines = [title, "", f"자료 수집 기간: {report['window']['label']}", "",
                 "직접 방문한 후기가 아니라 공개 검색 자료를 모아 정리한 정보성 초안이에요.",
                 "검색 제목·요약·영상 설명과 연결된 배경 자료를 바탕으로 작성했으며 기사 전문이나 영상 자막을 확인한 글은 아니에요.", "",
                 f"## 이번 주에 눈여겨본 이유\n\n주간 수집 표본에서 {row.get('weekly_source_count', len(sources))}개 자료, "
                 f"{row.get('weekly_publisher_count', topic.publisher_count)}개 발행자에 걸쳐 관련 내용이 확인됐어요.\n"
                 "실제 이용객 수나 전국적인 인기 순위를 뜻하지는 않아요.", "",
                 "## 공개 자료에서 살펴본 내용", ""]
        if "detail_added_count" in row:
            lines[8:8] = [f"주제 선택 후 {row['detail_added_count']}개의 추가 자료를 연결했어요. 배경 자료의 확인일은 주간 인기도 근거와 구분했어요.", ""]
        for paragraph in paragraphs:
            refs = " ".join(f"[{sid}]" for sid in paragraph["source_ids"])
            lines.extend([paragraph["text"] + " " + refs, ""])
        if topic.category == "맛집":
            tip = "방문 전 동일한 매장·지점인지 확인하고, 공식 안내에서 메뉴·가격·영업시간·예약 조건을 다시 확인해 주세요."
        else:
            tip = "구매·이용 전 공식 판매처에서 구성·가격·판매 여부와 원재료·알레르기 표시를 확인해 주세요."
        lines.extend(["## 알아두면 좋은 점", "", tip, "",
                      "이 글의 정보만으로 맛·품질·건강 효과를 보장할 수는 없어요.", "",
                      source_footer(sources), "", "#주간푸드트렌드 #음식정보 #" + topic.category])
        if report["demo"]:
            lines[0:0] = ["[데모: 모든 상호·음식·링크는 테스트용 가상 자료입니다]", ""]
        guide = None
        if row.get("guide_type"):
            topic.guide_type = row["guide_type"]
            topic.base_source_id = row.get("base_source_id")
            try:
                guide = make_guide(topic, sources, writer if not report["demo"] else None)
            except AIError as exc:
                report["warnings"].append(f"{topic.name}: {exc}")
                guide = make_guide(topic, sources)
            title, practical_text = render_guide(topic.name, guide)
            lines = [practical_text, "", source_footer(sources)]
            if report["demo"]:
                lines.insert(0, "[데모: 가상 자료입니다]")
            mode = "실용 가이드 초안" if guide["complete"] else "자료 보완이 필요한 실용 가이드"
            report["warnings"].extend(f"{topic.name}: {issue}" for issue in guide["issues"])
        drafts.append({"guide": guide, "topic": topic.name, "title": title, "mode": mode, "category": topic.category,
                       "source_ids": list(topic.source_ids),
                       "text": "\n".join(lines),
                       "review": ["출처 원문 및 실제 방송·영상 내용 대조", "매장 지점·지역 일치 확인",
                                  "가격·영업시간·판매 여부는 별도 확인 필요", "AI 문장의 사실 관계·출처 해석 검토"]})
    report["drafts"] = drafts
    return report


def draft_text(report, draft, markdown=False):
    text = draft["text"]
    mode = (draft.get("guide") or {}).get("type")
    targets = (["## 재료와 정확한 계량", "## 단계별 만드는 방법", "## 발효·굽기 온도와 시간"]
               if mode == "recipe" else ["## 대표 메뉴와 가격", "## 어떤 곳인가요?"])
    fallback = []
    for n, image in enumerate(i for i in report.get("images", []) if i["topic"] == draft["topic"]):
        marker = f"![참고 이미지](../{image['filename']})" if markdown else f"[사진 {image['number']:02d} : {image['filename']}]"
        block = "\n\n" + marker + "\n" + image["caption"] + "\n이미지 출처: " + image["attribution"] + "\n\n"
        target = targets[n % len(targets)]
        if mode and target in text:
            text = text.replace(target, target + block, 1)
        else:
            fallback.append(block)
    if fallback:
        text = text.replace("## 출처", "## 참고 이미지\n" + "".join(fallback) + "\n## 출처", 1)
    return text


def export_files(report):
    portable = {**report, "images": [{k: v for k, v in image.items() if k != "data_base64"}
                                    for image in report.get("images", [])]}
    files = {"report.json": json.dumps(portable, ensure_ascii=False, indent=2),
             "sources.json": json.dumps(report["sources"], ensure_ascii=False, indent=2)}
    lines = ["# 주간 푸드 트렌드 리서치", "", report["window"]["label"], "", METHOD, ""]
    if report["demo"]:
        lines.extend(["**데모: 테스트용 가상 자료이며 실제 트렌드가 아닙니다.**", ""])
    lines.extend(["| 순서 | 주제 | 분류 | 표본 관심도 | 자료 / 발행자 | 묶음 방식 |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for i, topic in enumerate(report["topics"], 1):
        name = topic["name"].replace("|", " ")
        lines.append(f"| {i} | {name} | {topic['category']} | {topic['score']:.2f} | "
                     f"{len(topic['source_ids'])} / {topic['publisher_count']} | {topic['grouping']} |")
    lines.extend(["", "## 수집 상태", ""])
    for status in report["source_status"] + report.get("detail_status", []):
        lines.append(f"- {status['source']} {status.get('query', '')}: {status['state']} "
                     f"{status.get('count', '')} {status.get('message', '')}")
    lines.extend(["", "## 확인할 사항", ""] + ["- " + w for w in report["warnings"]])
    files["rankings.md"] = "\n".join(lines)
    reviews = ["# 발행 전 확인", "", "초안은 자동 발행되지 않습니다. 검색 요약은 사실 확인을 대체하지 않습니다.", ""]
    for i, draft in enumerate(report["drafts"], 1):
        slug = re.sub(r"[^\w가-힣-]+", "_", draft["topic"]).strip("_")[:55] or "topic"
        files[f"drafts/{i:02d}_{slug}.md"] = draft_text(report, draft, markdown=True)
        files[f"drafts/{i:02d}_{slug}.txt"] = draft_text(report, draft)
        reviews.extend([f"## {draft['topic']} ({draft['mode']})", ""] +
                       ["- [ ] " + item for item in draft["review"]] + [""])
    files["review_checklist.md"] = "\n".join(reviews)
    credits = ["# 이미지 출처", "", "참고 이미지이며 실제 매장·제품의 촬영 사진으로 소개하지 마세요.", ""]
    for image in report.get("images", []):
        files[image["filename"]] = base64.b64decode(image["data_base64"])
        credits.extend([image["filename"], image["caption"], image["attribution"], ""])
    if report.get("images"):
        files["image_credits.md"] = "\n".join(credits)
    return files


def zip_bytes(report):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in export_files(report).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def save_report(report, output):
    # Unique run directory prevents overwriting a previous draft or mixing stale files.
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    import tempfile

    destination = Path(tempfile.mkdtemp(prefix=datetime.now(KST).strftime("%Y%m%d_%H%M%S_"), dir=root))
    for name, content in export_files(report).items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    (destination / "weekly_food.zip").write_bytes(zip_bytes(report))
    return destination
