"""Second-stage research for only the topics explicitly selected in the UI."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

from .core import Evidence, KST, Window, deduplicate, food_related, normalized
from .media import generated_images, licensed_photos
from .pipeline import create_drafts
from .guides import resolve_type, detail_queries
from .sources import Http, SourceError, collect


def background_info(topic):
    """An exact Wikipedia article lead; retrieval date is never called a publication date."""
    data = Http().json("https://ko.wikipedia.org/w/api.php", params={
        "action": "query", "format": "json", "titles": topic, "redirects": 1,
        "prop": "extracts|info", "inprop": "url", "exintro": 1, "explaintext": 1, "exchars": 1200,
    })
    results = []
    for page in data.get("query", {}).get("pages", {}).values():
        if "missing" in page or not page.get("extract") or not page.get("fullurl"):
            continue
        results.append(Evidence(
            title=page["title"], url=page["fullurl"], published_at=datetime.now(KST).isoformat(),
            kind="reference", publisher="위키백과 (배경 설명)", publisher_id="ko.wikipedia.org",
            summary=page["extract"], date_precision="retrieved", queries=[topic],
        ))
    return results


def build_selected_posts(report, selected, credentials=None, sources=None, writer=None,
                         detail_days=30, limit=15, image_mode="commons", image_count=1,
                         image_key="", image_model="gemini-3.1-flash-image",
                         topic_options=None, progress=None):
    if not selected or len(selected) > 10 or len(set(selected)) != len(selected):
        raise ValueError("주제를 1~10개 선택하세요.")
    if any(type(i) is not int or not 0 <= i < len(report["topics"]) for i in selected):
        raise ValueError("선택한 주제가 조사 결과에 없습니다.")
    if not 1 <= detail_days <= 90 or not 1 <= limit <= 50:
        raise ValueError("추가 조사 기간은 1~90일, 결과 상한은 1~50이어야 합니다.")
    if image_mode not in {"none", "commons", "ai"} or not 1 <= image_count <= 3:
        raise ValueError("이미지 방식 또는 개수가 올바르지 않습니다.")
    if image_mode == "ai" and not report["demo"] and (not image_key.strip() or not image_model.strip()):
        raise ValueError("AI 이미지용 Gemini API 키와 이미지 모델을 설정하세요.")
    sources = sources if sources is not None else ["rss", "news", "blog", "youtube"]
    if not sources or set(sources) - {"rss", "news", "blog", "youtube"}:
        raise ValueError("추가 조사 수집원을 하나 이상 선택하세요.")
    options = topic_options or {}
    result = copy.deepcopy(report)
    result["drafts"], result["images"], result["detail_status"] = [], [], []
    result["selected_topic_indices"] = list(selected)
    weekly_end = datetime.fromisoformat(report["window"]["end_exclusive"])
    window = Window(weekly_end - timedelta(days=detail_days), weekly_end)
    result["detail_window"] = {"start": window.start.isoformat(), "end_exclusive": window.end.isoformat()}
    by_id = {row["id"]: Evidence(**row) for row in result["sources"]}
    selected_rows = []
    for index in selected:
        topic = copy.deepcopy(report["topics"][index])
        name = topic["name"]
        config = options.get(index, {})
        topic["guide_type"] = resolve_type(topic["category"], config.get("guide_type"))
        search_term = config.get("search", name).strip() or name
        if len(search_term) > 150:
            raise ValueError("추가 검색어는 150자 이하로 입력하세요.")
        if progress:
            progress(f"추가 조사 · {name}")
        background = []
        added = []
        if not report["demo"]:
            queries = detail_queries(search_term, topic["guide_type"])
            raw, status = collect(queries, window, credentials or {}, sources, limit, progress)
            result["detail_status"].extend({**s, "topic": name} for s in status)
            anchor = normalized(name)
            added = [s for s in deduplicate(raw, window) if food_related(s) and
                     (anchor in normalized(s.title) or anchor in normalized(s.summary))]
            try:
                background = background_info(name)
                result["detail_status"].append({"topic": name, "source": "wikipedia", "state": "ok", "count": len(background)})
            except SourceError as exc:
                result["detail_status"].append({"topic": name, "source": "wikipedia", "state": "error", "message": str(exc)})
        provided = []
        if config.get("reference_text", "").strip():
            if not config.get("reference_url", "").strip():
                raise ValueError(f"{name}: 참고 원문의 출처 URL을 입력하세요.")
            provided = [Evidence(title=name + " 사용자 제공 참고 원문", url=config["reference_url"].strip(),
                                 published_at=datetime.now(KST).isoformat(),
                                 kind="recipe_source" if topic["guide_type"] == "recipe" else "venue_source",
                                 publisher="사용자 제공 원문 (원문 대조 필요)", publisher_id="provided-reference",
                                 summary=config["reference_text"][:20000], date_precision="retrieved")]
        topic["base_source_id"] = provided[0].id if provided and topic["guide_type"] == "recipe" else config.get("base_source_id")
        original = [by_id[sid] for sid in topic["source_ids"]]
        combined = deduplicate(original + added, window)
        # Preserve the original weekly evidence even if detail_days is shorter.
        combined_by_id = {s.id: s for s in original + combined + background + provided}
        by_id.update(combined_by_id)
        topic["source_ids"] = list(combined_by_id)
        topic["weekly_source_count"] = len(original)
        topic["weekly_publisher_count"] = topic["publisher_count"]
        topic["detail_added_count"] = len(set(combined_by_id) - {s.id for s in original})
        if topic["detail_added_count"] == 0:
            result["warnings"].append(f"{name}: 새로 연결할 추가 자료가 없어 기존 주간 근거로 작성했습니다.")
        selected_rows.append(topic)
    result["sources"] = [s.to_dict() for s in by_id.values()]
    # Only selected rows enter the draft writer; preserve the original ranking in the result.
    all_topics = result["topics"]
    result["topics"] = selected_rows
    create_drafts(result, top=len(selected_rows), writer=writer, progress=progress)
    result["topics"] = all_topics
    result["selected_topics"] = selected_rows
    for index, draft in zip(selected, result["drafts"]):
        name = draft["topic"]
        if image_mode == "none" or report["demo"]:
            result["detail_status"].append({"topic": name, "source": "images", "state": "skipped",
                                           "message": "데모에서는 이미지를 요청하지 않습니다." if report["demo"] else "이미지 없음"})
            continue
        if progress:
            progress(f"이미지 {'생성' if image_mode == 'ai' else '검색'} · {name}")
        try:
            subject = options.get(index, {}).get("image", name).strip() or name
            if image_mode == "commons":
                assets = licensed_photos(subject, name, image_count)
            else:
                guide = draft.get("guide") or {}
                fields = guide.get("fields", {})
                scenes = [heading + ": " + " ".join(r["text"] for r in fields[key])
                          for key, heading in [("ingredients", "재료"), ("steps", "성형·만들기 과정"), ("baking", "완성")]
                          if fields.get(key)] if guide.get("type") == "recipe" else []
                assets = generated_images(name, subject, image_key, image_model, image_count, scenes=scenes)
            result["images"].extend(assets)
            result["detail_status"].append({"topic": name, "source": "images", "state": "ok", "count": len(assets)})
            if len(assets) < image_count:
                result["warnings"].append(f"{name}: 이미지 {image_count}장 중 {len(assets)}장만 확보했습니다.")
        except (SourceError, ValueError) as exc:
            result["warnings"].append(f"{name}: {exc}")
            result["detail_status"].append({"topic": name, "source": "images", "state": "error", "message": str(exc)})
    return result
