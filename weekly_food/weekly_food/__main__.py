"""Usage: python -m weekly_food --demo or python -m weekly_food --help."""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .ai import Writer
from .core import DEFAULT_QUERIES, Window
from .pipeline import create_drafts, research, save_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="주간 음식 트렌드 수집 → 표본 관심도 비교 → 출처 포함 정보성 원고")
    parser.add_argument("--config", type=Path, help="검색어·관심 주제 JSON 설정 파일")
    parser.add_argument("--days", type=int, default=7, help="종료일 포함 1~31일 (기본 7)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="한국 시간 종료일 YYYY-MM-DD (기본 오늘)")
    parser.add_argument("--query", action="append", help="검색어. 여러 번 지정 가능, 설정 파일보다 우선")
    parser.add_argument("--focus", action="append", help="비교할 음식/가게명. 여러 번 지정 가능")
    parser.add_argument("--sources", nargs="+", choices=["rss", "news", "blog", "youtube"],
                        default=None, help="기본: 모든 수집원, 키 없는 API는 제외")
    parser.add_argument("--limit", type=int, default=20, help="검색어·수집원별 결과 상한 1~50 (페이지 추가 조회 없음)")
    parser.add_argument("--min-sources", type=int, default=2, help="주제별 최소 자료 수 및 독립 발행자 수")
    parser.add_argument("--top", type=int, default=5, help="상위 몇 개 원고를 만들지 (1~20)")
    parser.add_argument("--provider", choices=["auto", "none", "gemini", "groq"], default="auto")
    parser.add_argument("--model", help="AI 모델 ID (또는 GEMINI_MODEL/GROQ_MODEL)")
    parser.add_argument("--naver-api-mode", choices=["hub", "legacy"],
                        default=os.environ.get("NAVER_API_MODE", "hub").strip().lower() or "hub",
                        help="네이버 키 발급처: hub=신규 NAVER API HUB (기본), legacy=기존 개발자센터")
    parser.add_argument("--output", type=Path, default=Path("output/weekly_food"))
    parser.add_argument("--demo", action="store_true", help="네트워크/API 호출 없이 가상 자료로 시연")
    args = parser.parse_args(argv)
    try:
        config = {}
        if args.config:
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"queries", "focus", "sources"}:
                raise ValueError("설정 파일은 queries, focus, sources 필드만 사용할 수 있습니다. 키는 환경변수에 보관하세요.")
            for key, values in config.items():
                if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise ValueError(f"{key} 설정은 빈 값이 없는 문자열 목록이어야 합니다.")
        if not 1 <= args.top <= 20:
            raise ValueError("--top은 1~20이어야 합니다.")
        window = Window.make(args.days, args.end_date)
        credentials = {key: os.environ.get(key, "").strip() for key in (
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "YOUTUBE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY")}
        credentials["GEMINI_API_KEY"] = credentials["GEMINI_API_KEY"] or os.environ.get("GOOGLE_API_KEY", "").strip()
        credentials["NAVER_API_MODE"] = args.naver_api_mode
        provider = args.provider
        if provider == "auto":
            provider = ("gemini" if credentials["GEMINI_API_KEY"] else
                        "groq" if credentials["GROQ_API_KEY"] else "none")
        writer = None
        if provider != "none" and not args.demo:
            model = args.model or os.environ.get(provider.upper() + "_MODEL", "")
            if not model and provider == "gemini":
                model = "gemini-3.1-flash-lite"  # Same configurable default as the existing app.
            writer = Writer(provider, credentials[provider.upper() + "_API_KEY"], model)
        report = research(
            window, args.query or config.get("queries", DEFAULT_QUERIES), credentials,
            args.sources if args.sources is not None else config.get("sources"), args.limit,
            args.min_sources, args.focus or config.get("focus"), writer, args.demo,
            progress=lambda message: print(message, flush=True),
        )
        create_drafts(report, args.top, writer, progress=lambda message: print(message, flush=True))
        destination = save_report(report, args.output)
        print(f"\n기간: {window.label}\n자료 {len(report['sources'])}건 · 후보 {len(report['topics'])}개 · 원고 {len(report['drafts'])}개")
        print(f"저장: {destination}")
        for warning in report["warnings"]:
            print("확인: " + warning)
        if not report["topics"]:
            return 2  # Schedulers can distinguish no draft from a successful research run.
        return 0
    except (ValueError, OSError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
