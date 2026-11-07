"""Render the weekly research workflow inside the main app tab."""
import copy
import os
from datetime import datetime

import streamlit as st

from weekly_food.ai import Writer
from weekly_food.core import DEFAULT_QUERIES, KST, Window
from weekly_food.pipeline import create_drafts, research, zip_bytes


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
    st.write("최근 일주일의 공개 음식 소식을 모아 주제를 비교하고, 출처를 붙인 정보성 초안을 만듭니다.")
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
        top = st.number_input("원고 개수", min_value=1, max_value=20, value=5, key="weekly_top")
        demo = st.checkbox("가상 자료로 기능 시연", key="weekly_demo")

    if st.button("주간 자료 수집하고 원고 만들기", type="primary", key="weekly_run"):
        st.session_state.pop("weekly_report", None)
        try:
            writer = None
            if not demo and provider != "근거 정리 (AI 없이)":
                writer = Writer(provider.lower(), ai_key, model)
            with st.status("자료 수집 및 원고 작성 중", expanded=True) as status:
                current = st.empty()
                report = research(
                    Window.make(int(days), end_date), [q.strip() for q in query_text.splitlines() if q.strip()],
                    {"NAVER_CLIENT_ID": naver_id.strip(), "NAVER_CLIENT_SECRET": naver_secret.strip(),
                     "YOUTUBE_API_KEY": youtube_key.strip()},
                    [source_options[s] for s in selected], int(limit), int(minimum),
                    [q.strip() for q in focus_text.splitlines() if q.strip()] or None,
                    writer=writer, demo=demo, progress=current.write,
                )
                create_drafts(report, int(top), writer, progress=current.write)
                st.session_state.weekly_report = copy.deepcopy(report)
                current.empty()
                status.update(label="조사 결과 저장됨" if report["topics"] else "결과 부족 · 수집 상태를 확인하세요", state="complete")
        except ValueError as exc:
            st.error(str(exc))

    report = st.session_state.get("weekly_report")
    if report:
        st.subheader("저장된 조사 결과")
        st.caption(f"{report['window']['label']} · 실행: {report['generated_at']} · 검색어: {', '.join(report['queries'])}")
        if report["demo"]:
            st.warning("가상 자료를 사용한 데모입니다. 실제 맛집·음식 트렌드가 아닙니다.")
        cols = st.columns(3)
        cols[0].metric("기간 내 자료", len(report["sources"]))
        cols[1].metric("기준 충족 주제", len(report["topics"]))
        cols[2].metric("생성 원고", len(report["drafts"]))
        for warning in report["warnings"]:
            st.warning(warning)
        with st.expander("관심도 산정 방법 및 수집 상태", expanded=not bool(report["topics"])):
            st.write(report["method"])
            st.caption("headline: 유사 제목 · keyword: 반복 음식명 · entity: AI 추출 후 근거 문자열 대조 · configured: 직접 지정한 주제")
            st.caption(f"AI 이름 분류는 플랫폼별 최신 자료 최대 120건, 원고별 AI 입력은 최신 자료 최대 12건입니다. 이번 분류 입력: {report['ai_sample_count']}건")
            st.dataframe(report["source_status"], use_container_width=True)
        if report["topics"]:
            st.dataframe([{"주제": t["name"], "분류": t["category"], "표본 관심도": t["score"],
                           "자료": len(t["source_ids"]), "발행자": t["publisher_count"], "묶음": t["grouping"]}
                          for t in report["topics"]], use_container_width=True)
        for i, draft in enumerate(report["drafts"], 1):
            with st.expander(f"{i}. {draft['topic']} · {draft['mode']}", expanded=i == 1):
                st.text_area("원고", draft["text"], height=500, key=f"weekly_preview_{report['generated_at']}_{i}", disabled=True)
                st.download_button("원고 TXT 저장", draft["text"], f"주간_원고_{i:02d}.txt", key=f"weekly_txt_{i}")
                st.caption("발행 전 확인: " + " / ".join(draft["review"]))
        with st.expander("전체 수집 자료·원문 링크"):
            st.dataframe([{"제목": s["title"], "출처": s["publisher"], "유형": s["kind"],
                           "게시일": s["published_at"], "URL": s["url"]} for s in report["sources"]],
                         use_container_width=True)
        st.download_button("원고·순위·출처·검토표 ZIP 저장", zip_bytes(report), "weekly_food.zip", "application/zip", key="weekly_zip")
