"""Render the weekly research workflow inside the main app tab."""
import copy
import base64
import os
from datetime import datetime

import streamlit as st

from weekly_food.ai import Writer
from weekly_food.core import DEFAULT_QUERIES, KST, Window
from weekly_food.pipeline import draft_text, research, zip_bytes
from weekly_food.detail import build_selected_posts
from weekly_food.guides import GUIDE_TYPES


def setting(name, default=""):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default)).strip()
    except (FileNotFoundError, KeyError):
        return default


def render_weekly_food():
    st.subheader("🍽️ 주간 푸드 트렌드 원고 작성기")
    st.write("① 주간 화제 찾기 → ② 주제 여러 개 선택 → ③ 추가 조사·원고·이미지 만들기")
    st.caption("SNS 반응은 공개 뉴스·블로그에 언급된 범위로 수집합니다. 인스타그램·틱톡 내부 게시물 전체를 직접 수집하지 않습니다.")

    with st.expander("트렌드 수집 설정", expanded=True):
        provider_default = 1 if setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY") else 2 if setting("GROQ_API_KEY") else 0
        provider = st.selectbox("원고 작성 방식", ["근거 정리 (AI 없이)", "Gemini", "Groq"], index=provider_default, key="weekly_provider")
        ai_key, model = "", ""
        if provider in {"Gemini", "Groq"}:
            env_name = provider.upper() + "_API_KEY"
            key_default = setting(env_name) or (setting("GOOGLE_API_KEY") if provider == "Gemini" else "")
            ai_key = st.text_input(f"{provider} API 키", value=key_default, type="password", key=f"weekly_{env_name}")
            default_model = "gemini-3.1-flash-lite" if provider == "Gemini" else ""
            model = st.text_input("모델 ID", value=setting(provider.upper() + "_MODEL", default_model), key=f"weekly_model_{provider}")
        with st.container(border=True):
            st.caption("네이버·유튜브 API 키 (선택)")
            naver_modes = {"NAVER API HUB (신규 발급)": "hub", "기존 개발자센터 (이전 발급)": "legacy"}
            naver_mode = st.selectbox(
                "네이버 키 발급처", list(naver_modes),
                index=1 if setting("NAVER_API_MODE", "hub").lower() == "legacy" else 0,
                key="weekly_naver_mode",
            )
            st.caption("2026년 7월 31일부터 검색 API 신규 발급은 NAVER API HUB에서 진행합니다.")
            st.markdown("[네이버 클라우드 콘솔](https://console.ncloud.com/) · "
                        "[검색 API 키 발급 안내](https://api.ncloud-docs.com/docs/naver-api-hub-overview)")
            naver_id = st.text_input("NAVER_CLIENT_ID", value=setting("NAVER_CLIENT_ID"), type="password", key="weekly_naver_id")
            naver_secret = st.text_input("NAVER_CLIENT_SECRET", value=setting("NAVER_CLIENT_SECRET"), type="password", key="weekly_naver_secret")
            youtube_key = st.text_input("YOUTUBE_API_KEY", value=setting("YOUTUBE_API_KEY"), type="password", key="weekly_youtube_key")
        st.caption("키가 없는 API는 수집 상태에 ‘제외’로 기록합니다. 공개 뉴스 RSS는 키 없이 사용할 수 있습니다.")

    left, right = st.columns([2, 1])
    with left:
        query_text = st.text_area("검색어 (한 줄에 하나)", "\n".join(DEFAULT_QUERIES), height=200, key="weekly_queries")
        focus_text = st.text_area("비교할 음식·가게명 (선택, 한 줄에 하나)", key="weekly_focus",
                                  help="비워두면 AI는 구체적인 이름을 추출하고, AI가 없으면 반복 등장한 음식명 또는 비슷한 제목을 묶습니다. 특정 가게는 지점·지역까지 확인하세요.")
    with right:
        today = datetime.now(KST).date()
        end_date = st.date_input("종료일 (한국 시간)", value=today, max_value=today, key="weekly_end")
        days = st.number_input("기간 (일)", min_value=1, max_value=31, value=7, key="weekly_days")
        source_options = {"공개 뉴스 RSS": "rss", "네이버 뉴스": "news", "네이버 블로그": "blog", "유튜브": "youtube"}
        selected = st.multiselect("수집원", list(source_options), default=list(source_options), key="weekly_sources")
        minimum = st.number_input("최소 자료·발행자 수", min_value=1, max_value=10, value=2, key="weekly_minimum")
        limit = st.number_input("검색어·수집원별 상한", min_value=1, max_value=50, value=20, key="weekly_limit")
        top = st.number_input("기본 선택 주제 수", min_value=1, max_value=10, value=3, key="weekly_top")
        demo = st.checkbox("가상 자료로 기능 시연", key="weekly_demo")

    if st.button("1. 이번 주 핫한 주제 찾기", type="primary", key="weekly_run"):
        st.session_state.pop("weekly_report", None)
        st.session_state.pop("weekly_posts", None)
        try:
            writer = None
            if not demo and provider != "근거 정리 (AI 없이)":
                writer = Writer(provider.lower(), ai_key, model)
            with st.status("주간 화제 수집 중", expanded=True) as status:
                current = st.empty()
                report = research(
                    Window.make(int(days), end_date), [q.strip() for q in query_text.splitlines() if q.strip()],
                    {"NAVER_CLIENT_ID": naver_id.strip(), "NAVER_CLIENT_SECRET": naver_secret.strip(),
                     "YOUTUBE_API_KEY": youtube_key.strip(), "NAVER_API_MODE": naver_modes[naver_mode]},
                    [source_options[s] for s in selected], int(limit), int(minimum),
                    [q.strip() for q in focus_text.splitlines() if q.strip()] or None,
                    writer=writer, demo=demo, progress=current.write,
                )
                st.session_state.weekly_report = copy.deepcopy(report)
                current.empty()
                status.update(label="조사 결과 저장됨" if report["topics"] else "결과 부족 · 수집 상태를 확인하세요", state="complete")
        except ValueError as exc:
            st.error(str(exc))

    report = st.session_state.get("weekly_report")
    if not report:
        return
    st.subheader("2. 주제를 골라 추가 조사하기")
    st.caption(f"{report['window']['label']} · 실행: {report['generated_at']} · 검색어: {', '.join(report['queries'])}")
    if report["demo"]:
        st.warning("가상 자료를 사용한 데모입니다. 실제 맛집·음식 트렌드가 아닙니다. 데모에서는 추가 검색·이미지 API를 호출하지 않습니다.")
    posts = st.session_state.get("weekly_posts", report)
    cols = st.columns(3)
    cols[0].metric("기간 내 자료", len(report["sources"]))
    cols[1].metric("기준 충족 주제", len(report["topics"]))
    draft_metric = cols[2].empty()
    draft_metric.metric("생성 원고", len(posts["drafts"]))
    for warning in dict.fromkeys(report["warnings"] + posts["warnings"]):
        st.warning(warning)
    with st.expander("관심도 산정 방법 및 수집 상태", expanded=not bool(report["topics"])):
        st.write(report["method"])
        st.caption("headline: 유사 제목 · keyword: 반복 음식명 · entity: AI 추출 후 근거 문자열 대조 · configured: 직접 지정한 주제")
        st.caption(f"AI 이름 분류 입력: {report['ai_sample_count']}건 (플랫폼별 최신 자료 최대 120건)")
        st.dataframe(report["source_status"], use_container_width=True)
    if report["topics"]:
        st.dataframe([{"주제": t["name"], "분류": t["category"], "표본 관심도": t["score"],
                       "자료": len(t["source_ids"]), "발행자": t["publisher_count"], "묶음": t["grouping"]}
                      for t in report["topics"]], use_container_width=True)
        stamp = report["generated_at"]
        chosen = st.multiselect(
            "원고로 만들 주제를 선택하세요 (최대 10개)", list(range(len(report["topics"]))),
            default=list(range(min(int(top), len(report["topics"])))),
            format_func=lambda i: f"{i + 1}. {report['topics'][i]['name']} · {report['topics'][i]['score']:.1f}점",
            key=f"weekly_selected_{stamp}",
        )
        detail_days = st.number_input("추가 조사 기간 (일)", 1, 90, 30, key="weekly_detail_days")
        st.caption("선택한 글 유형에 맞춰 레시피·계량·공정 또는 지점·영업시간·예약·주차를 다시 조사합니다. 기간은 주간 조사 종료 시점 기준이며, 백과사전 배경 자료의 확인일은 별도로 표시합니다.")
        modes = {"공개 사진 가져오기 (라이선스 확인)": "commons", "AI 이미지 생성": "ai", "이미지 없음": "none"}
        image_mode = st.selectbox("이미지 준비 방식", list(modes), key="weekly_image_mode")
        image_count = st.number_input("주제별 이미지 수", 1, 3, 1, key="weekly_image_count")
        image_key, image_model = "", "gemini-3.1-flash-image"
        if modes[image_mode] == "ai":
            image_key = st.text_input("이미지 생성용 Gemini API 키", type="password",
                                      value=(ai_key if provider == "Gemini" else "") or setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY"),
                                      key="weekly_image_key")
            image_model = st.text_input("이미지 생성 모델 ID", value=setting("GEMINI_IMAGE_MODEL", image_model), key="weekly_image_model")
            st.caption("이미지 모델은 텍스트 모델과 다릅니다. 계정의 이미지 생성 권한·요금·할당량이 적용됩니다. 생성 결과는 AI 참고 이미지로 표시합니다.")
        elif modes[image_mode] == "commons":
            st.caption("Wikimedia Commons에서 CC BY·CC BY-SA·CC0·퍼블릭 도메인 라이선스가 확인된 사진만 받습니다. 해당 매장의 실제 사진으로 표시하지 않습니다.")
        topic_options = {}
        if chosen:
            with st.expander("주제별 포스팅 유형·기준자료·이미지 설정", expanded=True):
                for index in chosen[:10]:
                    name = report["topics"][index]["name"]
                    guide_label = st.selectbox(f"{name} · 포스팅 유형", list(GUIDE_TYPES),
                                               index=1 if report["topics"][index]["category"] == "맛집" else 0,
                                               key=f"weekly_guide_type_{stamp}_{index}")
                    reference_url = st.text_input(f"{name} · 기준 레시피/매장 안내 출처 URL (선택)",
                                                  key=f"weekly_reference_url_{stamp}_{index}")
                    reference_text = st.text_area(f"{name} · 기준 레시피 또는 매장 안내 원문 (선택)",
                                                  key=f"weekly_reference_text_{stamp}_{index}",
                                                  help="검색 요약에 없는 세부 정보는 원문으로 보강하세요. 레시피는 하나의 원문에 분량·계량·과정·온도·시간을 함께 넣으세요. URL만 입력하면 원문을 자동으로 읽지는 않습니다.",
                                                  placeholder="재료: ...\n단계별 만드는 방법: ...\n또는 영업시간: ...\n예약: ...\n주차: ...")
                    search = st.text_input(f"{name} · 추가 검색어 (지점·지역 포함 가능)", value=name,
                                           key=f"weekly_detail_query_{stamp}_{index}")
                    subject = st.text_input(f"{name} · 이미지 검색어 또는 그릴 음식", value=name,
                                            key=f"weekly_image_subject_{stamp}_{index}",
                                            help="공개 사진은 영문 음식명으로도 검색해 보세요. 맛집 주제라면 대표 음식명을 입력하세요.")
                    topic_options[index] = {"search": search, "image": subject, "guide_type": GUIDE_TYPES[guide_label],
                                            "reference_url": reference_url, "reference_text": reference_text}
        if st.button("3. 선택 주제 추가 조사하고 원고·이미지 만들기", type="primary", key="weekly_build"):
            try:
                writer = Writer(provider.lower(), ai_key, model) if provider != "근거 정리 (AI 없이)" and not report["demo"] else None
                with st.status("선택 주제 조사·원고·이미지 준비 중", expanded=True) as status:
                    current = st.empty()
                    generated = build_selected_posts(
                        report, chosen,
                        credentials={"NAVER_CLIENT_ID": naver_id.strip(), "NAVER_CLIENT_SECRET": naver_secret.strip(),
                                     "YOUTUBE_API_KEY": youtube_key.strip(), "NAVER_API_MODE": naver_modes[naver_mode]},
                        sources=[source_options[s] for s in selected], writer=writer,
                        detail_days=int(detail_days), limit=int(limit), image_mode=modes[image_mode], image_count=int(image_count),
                        image_key=image_key, image_model=image_model, topic_options=topic_options, progress=current.write,
                    )
                    st.session_state.weekly_posts = generated
                    posts = generated
                    draft_metric.metric("생성 원고", len(posts["drafts"]))
                    current.empty()
                    status.update(label=f"초안 {len(posts['drafts'])}개 · 이미지 {len(posts.get('images', []))}장 (항목별 확인 상태를 검토하세요)", state="complete")
                for warning in posts["warnings"]:
                    st.warning(warning)
            except ValueError as exc:
                st.error(str(exc))
    if posts["drafts"]:
        st.subheader("완성된 정보성 초안")
        st.caption("생성 주제: " + ", ".join(d["topic"] for d in posts["drafts"]))
        with st.expander("추가 조사·이미지 처리 상태"):
            st.dataframe(posts.get("detail_status", []), use_container_width=True)
        for i, draft in enumerate(posts["drafts"], 1):
            with st.expander(f"{i}. {draft['topic']} · {draft['mode']}", expanded=i == 1):
                text = draft_text(posts, draft)
                st.text_area("원고", text, height=500, key=f"weekly_preview_{stamp}_{i}_{hash(text)}", disabled=True)
                for image in posts.get("images", []):
                    if image["topic"] == draft["topic"]:
                        st.image(base64.b64decode(image["data_base64"]), caption=image["caption"])
                        st.caption(image["attribution"])
                st.download_button("원고 TXT 저장", text, f"주간_원고_{i:02d}.txt", key=f"weekly_txt_{i}")
                if (draft.get("guide") or {}).get("missing"):
                    st.warning("자료 보완 필요: " + " / ".join(draft["guide"]["missing"]))
                st.caption("발행 전 확인: " + " / ".join(draft["review"]))
    with st.expander("전체 수집 자료·원문 링크"):
        st.dataframe([{"제목": s["title"], "출처": s["publisher"], "유형": s["kind"],
                       "날짜": s["published_at"], "날짜 종류": s["date_precision"], "URL": s["url"]} for s in posts["sources"]],
                     use_container_width=True)
    st.download_button("원고·이미지·출처 ZIP 저장", zip_bytes(posts), "weekly_food.zip", "application/zip", key="weekly_zip")
