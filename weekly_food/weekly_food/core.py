"""Pure data, date filtering, deduplication, grouping, and ranking logic."""
from __future__ import annotations

import hashlib
import html
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DEFAULT_QUERIES = [
    "맛집 방송", "유튜브 맛집", "SNS 맛집", "인스타 디저트",
    "디저트 유행", "음식 트렌드", "식재료 인기", "빵집 카페 화제",
]
CATEGORIES = ("맛집", "디저트", "음식", "재료")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", value or ""))).strip()


def normalized(value: str) -> str:
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", value).casefold())


def canonical_url(url: str) -> str:
    parts = urlsplit(html.unescape(url.strip()))
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("유효한 공개 HTTP(S) 링크가 아닙니다.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(sorted(query)), ""))


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime  # exclusive

    @classmethod
    def make(cls, days: int = 7, end_date: date = None, now: datetime = None):
        now = (now or datetime.now(KST)).astimezone(KST)
        if not 1 <= days <= 31:
            raise ValueError("수집 기간은 1~31일이어야 합니다.")
        last = end_date or now.date()
        if last > now.date():
            raise ValueError("종료일은 오늘 이후로 지정할 수 없습니다.")
        start = datetime.combine(last - timedelta(days=days - 1), time.min, KST)
        end = min(datetime.combine(last + timedelta(days=1), time.min, KST), now)
        return cls(start, end)

    @property
    def label(self):
        return f"{self.start:%Y.%m.%d} ~ {self.end - timedelta(microseconds=1):%Y.%m.%d} (한국 시간)"

    def contains(self, value: datetime) -> bool:
        return value.tzinfo is not None and self.start <= value < self.end


@dataclass
class Evidence:
    title: str
    url: str
    published_at: str
    kind: str
    publisher: str
    publisher_id: str
    summary: str = ""
    queries: list[str] = field(default_factory=list)
    views: int = 0
    date_precision: str = "time"
    id: str = ""

    def __post_init__(self):
        self.title = clean(self.title)[:300]
        self.summary = clean(self.summary)[:20000 if self.kind in {"recipe_source", "venue_source"} else 600]
        self.url = canonical_url(self.url)
        self.id = "S" + hashlib.sha256(self.url.encode()).hexdigest()[:12]
        self.views = max(0, int(self.views))
        if self.published.tzinfo is None:
            raise ValueError("게시일에 시간대가 필요합니다.")
        if not self.publisher_id:
            raise ValueError("발행자 식별자가 필요합니다.")
        if self.kind == "news":
            self.publisher_id = self.publisher_id.lower().removeprefix("www.")

    @property
    def published(self):
        return datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))

    def to_dict(self):
        return asdict(self)


@dataclass
class Topic:
    name: str
    category: str
    source_ids: list[str]
    grouping: str = "headline"  # headline | keyword | entity | configured
    score: float = 0
    score_parts: dict = field(default_factory=dict)
    publisher_count: int = 0

    def to_dict(self):
        return asdict(self)


def headline(title: str) -> str:
    # Strip a publisher suffix, but retain geographic/branch qualifiers.
    return re.sub(r"\s+[\-–|]\s+[^|]+$", "", title).strip()


def deduplicate(items: list[Evidence], window: Window) -> list[Evidence]:
    urls, titles, result = {}, {}, []
    for item in sorted(items, key=lambda s: s.published):
        try:
            if not window.contains(item.published) or not item.title:
                continue
        except (ValueError, TypeError):
            continue
        title_key = normalized(headline(item.title))
        # Matching syndicated headlines are one observation, not independent votes.
        previous = urls.get(item.url) or titles.get(title_key)
        if previous:
            previous.queries = sorted(set(previous.queries + item.queries))
            previous.views = max(previous.views, item.views)
            continue
        urls[item.url] = titles[title_key] = item
        result.append(item)
    return result


STOP_WORDS = set("맛집 방송 유튜브 SNS 인스타 인기 화제 최근 이번 주 주간 디저트 음식 식재료 트렌드 추천 소개 서울 전국 맛있는 맛있는녀석들 오늘 새로운 공개 기자 단독 뉴스 사진 영상 카페 관심 핫한 방문 후기 곳 소식 있는 이 한 더 그 등 수 왜 대박 빵집".casefold().split())


def tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[가-힣a-zA-Z0-9]{2,}", headline(title).casefold())
            if w not in STOP_WORDS}


def guess_category(text: str) -> str:
    if re.search(r"식재료|원재료|재배|수확|채소|과일|품종", text):
        return "재료"
    if re.search(r"디저트|케이크|쿠키|초콜릿|베이커리|빵집|도넛|빙수|카페", text):
        return "디저트"
    return "맛집" if re.search(r"맛집|식당|가게|셰프|쉐프|매장", text) else "음식"


def food_related(source: Evidence) -> bool:
    """Remove obvious metaphorical food mentions, without claiming perfect relevance."""
    title = source.title
    if re.search(r"디저트처럼|달콤한\s*변신|신곡|후속곡|타이틀곡|뮤직비디오|컴백|반도체\s*예산|택배맛집|노을\s*맛집|귀신\s*맛집", title):
        return False
    return True


FOOD_ENDINGS = (
    "크루아상|크로와상|마카롱|두쫀쿠|모찌|츄러스|도넛|초콜릿|케이크|쿠키|베이글|푸딩|빙수|젤라토|와플|라떼|빵|"
    "육개장|해장국|곰탕|칼국수|뼈국수|감자탕면|국밥|미역국|순댓국|순대국|오징어볶음|제육볶음|"
    "불고기|보쌈|냉면|우동|뇨끼|파스타|피자|치킨|떡볶이|떡국|라멘|김밥|비빔밥|한우|와규|대방어|고구마|감자|버터"
)
FOOD_PATTERN = re.compile(r"(?<![가-힣A-Za-z0-9])([가-힣A-Za-z0-9]{0,20}(?:" + FOOD_ENDINGS +
                          r"))(?=$|[\s\W]|[은는이가을를와과에의도서])")


def group_candidates(items: list[Evidence], focus: list[str] = None) -> list[Topic]:
    """With no AI, prefer explicitly repeated food nouns to corporate headline clusters."""
    if focus:
        return group_headlines(items, focus)
    observed = {}
    for source in items:
        for match in FOOD_PATTERN.finditer(headline(source.title)):
            name = match.group(1)
            if len(name) >= 2:
                observed.setdefault(normalized(name), name)
    candidates = []
    for key, name in observed.items():
        ids = [s.id for s in items if any(normalized(m.group(1)) == key
                                         for m in FOOD_PATTERN.finditer(headline(s.title)))]
        if len(ids) < 2:
            continue
        category = "디저트" if re.search(r"크루아상|크로와상|마카롱|두쫀쿠|모찌|츄러스|도넛|초콜릿|케이크|쿠키|베이글|푸딩|빙수|젤라토|와플|라떼|빵$", name) else "음식"
        if re.search(r"(?:한우|와규|대방어|고구마|감자|버터)$", name):
            category = "재료"
        candidates.append(Topic(name, category, sorted(set(ids)), "keyword"))
    return candidates or group_headlines(items)


def group_headlines(items: list[Evidence], focus: list[str] = None) -> list[Topic]:
    """Conservative fallback: similar headlines, never inferred restaurant names."""
    if focus:
        return [Topic(term, guess_category(term),
                      [s.id for s in items if normalized(term) in normalized(s.title)
                       or normalized(term) in normalized(s.summary)],
                      "configured") for term in dict.fromkeys(focus) if normalized(term)]
    groups: list[Topic] = []
    representative: list[set[str]] = []
    for source in sorted(items, key=lambda s: s.id):
        words = tokens(source.title)
        match = None
        for i, previous in enumerate(representative):
            intersection = len(words & previous)
            if intersection >= 2 and intersection / max(1, len(words | previous)) >= 0.45:
                match = i
                break
        if match is None:
            groups.append(Topic(headline(source.title), guess_category(source.title), [source.id]))
            representative.append(words)
        else:
            groups[match].source_ids.append(source.id)
    return groups


def rank_topics(topics: list[Topic], items: list[Evidence], window: Window,
                min_sources: int = 2) -> list[Topic]:
    by_id = {s.id: s for s in items}
    ranked = []
    for topic in topics:
        topic.source_ids = sorted(set(topic.source_ids) & by_id.keys())
        sources = [by_id[sid] for sid in topic.source_ids]
        publishers = {s.publisher_id for s in sources}
        if len(sources) < min_sources or len(publishers) < min_sources:
            continue
        freshness = sum(math.exp(-max(0, (window.end - s.published).total_seconds()) / 86400 / 3)
                        for s in sources) / len(sources)
        # Historical publication windows cannot reconstruct historical view counts.
        historical = (window.end - timedelta(microseconds=1)).astimezone(KST).date() < datetime.now(KST).date()
        daily_views = max((s.views / max(1, (window.end - s.published).total_seconds() / 86400)
                           for s in sources if s.kind == "youtube"), default=0)
        if historical:
            daily_views = 0
        parts = {
            "독립_발행자_수": 35 * min(1, math.log2(1 + len(publishers)) / math.log2(11)),
            "출처_유형_다양성": 25 * min(1, len({s.kind for s in sources}) / 3),
            "최신성": 25 * freshness,
            "유튜브_일평균_누적조회": 15 * min(1, math.log10(1 + daily_views) / 6),
        }
        topic.score_parts = {k: round(v, 2) for k, v in parts.items()}
        topic.score = round(sum(parts.values()), 2)
        topic.publisher_count = len(publishers)
        ranked.append(topic)
    return sorted(ranked, key=lambda t: (-t.score, t.name))
