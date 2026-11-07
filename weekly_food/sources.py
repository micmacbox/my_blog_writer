"""Public RSS/search metadata only; no login, full-article or photo scraping."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit

import requests

from .core import Evidence, KST, Window, clean


class SourceError(RuntimeError):
    pass


class Http:
    """Bounded requests. Error strings deliberately omit URLs, keys, and bodies."""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "WeeklyFoodWriter/1.0"

    def get(self, url, **kwargs):
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=(5, 25), **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(1 + attempt)
                        continue
                if not response.ok:
                    raise SourceError(f"HTTP {response.status_code}: 인증·할당량·서비스 상태를 확인하세요.")
                return response
            except requests.RequestException:
                if attempt == 2:
                    raise SourceError("네트워크 요청에 실패했습니다.") from None
                time.sleep(1 + attempt)
        raise SourceError("수집 요청에 실패했습니다.")

    def json(self, url, **kwargs):
        try:
            data = self.get(url, **kwargs).json()
            if not isinstance(data, dict):
                raise ValueError
            if "items" in data and (not isinstance(data["items"], list) or
                                    any(not isinstance(row, dict) for row in data["items"])):
                raise ValueError
            return data
        except ValueError:
            raise SourceError("수집 서버의 JSON 응답 형식이 올바르지 않습니다.") from None


def rss_items(xml: bytes, query: str) -> list[Evidence]:
    if len(xml) > 5_000_000 or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise SourceError("허용하지 않는 RSS 형식 또는 크기입니다.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError("RSS XML을 읽을 수 없습니다.") from None
    result = []
    for node in root.findall("./channel/item"):
        try:
            publisher = node.find("source")
            publisher_url = publisher.get("url", "") if publisher is not None else ""
            url = node.findtext("link", "")
            result.append(Evidence(
                title=node.findtext("title", ""), url=url,
                published_at=parsedate_to_datetime(node.findtext("pubDate", "")).isoformat(),
                kind="news", publisher=clean(publisher.text if publisher is not None else "RSS"),
                publisher_id=urlsplit(publisher_url or url).hostname or "unknown-rss",
                # Google News RSS description repeats the headline; do not call it article text.
                summary="", queries=[query],
            ))
        except (ValueError, TypeError, OverflowError):
            continue
    return result


def google_news(http: Http, query: str, window: Window, limit: int) -> list[Evidence]:
    # This public RSS interface has no guaranteed API SLA; it is a replaceable source.
    # Search a slightly wider range; exact KST bounds are enforced locally.
    after = window.start - timedelta(days=1)
    before = window.end + timedelta(days=1)
    dated = f"{query} after:{after:%Y-%m-%d} before:{before:%Y-%m-%d}"
    url = "https://news.google.com/rss/search?" + urlencode({
        "q": dated, "hl": "ko", "gl": "KR", "ceid": "KR:ko",
    })
    items = rss_items(http.get(url).content, query)
    return sorted((s for s in items if window.contains(s.published)),
                  key=lambda s: s.published, reverse=True)[:limit]


def naver_search(http: Http, query: str, window: Window, kind: str,
                 client_id: str, secret: str, limit: int, mode: str = "hub") -> list[Evidence]:
    params = {
        "query": query, "sort": "date", "display": limit, "start": 1,
    }
    if mode == "hub":
        url = f"https://naverapihub.apigw.ntruss.com/search/v1/{kind}"
        headers = {"X-NCP-APIGW-API-KEY-ID": client_id, "X-NCP-APIGW-API-KEY": secret}
        params["format"] = "json"
    elif mode == "legacy":
        url = f"https://openapi.naver.com/v1/search/{kind}.json"
        headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": secret}
    else:
        raise ValueError("네이버 API 방식은 hub 또는 legacy여야 합니다.")
    data = http.json(url, params=params, headers=headers)
    if "error" in data or "errorCode" in data or "items" not in data:
        raise SourceError("네이버 응답 오류: 선택한 API 방식·키 발급처·검색 API 사용 설정을 확인하세요.")
    result = []
    for item in data.get("items", []):
        try:
            if kind == "blog":
                published = datetime.strptime(item["postdate"], "%Y%m%d").replace(tzinfo=KST)
                url = item["link"]
                publisher = clean(item.get("bloggername", "블로그"))
                # Blogger URL identifies authors independently of the shared blog host.
                author_url = item.get("bloggerlink") or url
                publisher_id = author_url.split("?", 1)[0].rstrip("/")
            else:
                published = parsedate_to_datetime(item["pubDate"])
                url = item.get("originallink") or item["link"]
                publisher = urlsplit(url).hostname or "뉴스"
                publisher_id = publisher
            if window.contains(published):
                result.append(Evidence(
                    title=item["title"], url=url, published_at=published.isoformat(),
                    kind=kind, publisher=publisher, publisher_id=publisher_id,
                    summary=item.get("description", ""), queries=[query],
                    date_precision="day" if kind == "blog" else "time",
                ))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return result


def youtube_search(http: Http, query: str, window: Window, key: str, limit: int):
    def utc(dt):
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    data = http.json("https://www.googleapis.com/youtube/v3/search", params={
        "key": key, "part": "snippet", "type": "video", "q": query,
        "publishedAfter": utc(window.start), "publishedBefore": utc(window.end),
        "order": "viewCount", "regionCode": "KR", "relevanceLanguage": "ko",
        "maxResults": min(limit, 50),
    })
    ids = [item["id"]["videoId"] for item in data.get("items", [])
           if item.get("id", {}).get("videoId")]
    if not ids:
        return []
    details = http.json("https://www.googleapis.com/youtube/v3/videos", params={
        "key": key, "part": "snippet,statistics", "id": ",".join(ids),
    })
    result = []
    for item in details.get("items", []):
        try:
            snippet = item["snippet"]
            result.append(Evidence(
                title=snippet["title"], url="https://www.youtube.com/watch?v=" + item["id"],
                published_at=snippet["publishedAt"], kind="youtube",
                publisher=snippet["channelTitle"], publisher_id=snippet["channelId"],
                summary=snippet.get("description", ""), queries=[query],
                views=int(item.get("statistics", {}).get("viewCount", 0)),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return result


def collect(queries, window, credentials, sources, limit=20, progress=None):
    http, items, status = Http(), [], []
    naver_mode = credentials.get("NAVER_API_MODE", "hub").strip().lower() or "hub"
    if naver_mode not in {"hub", "legacy"}:
        raise ValueError("NAVER_API_MODE는 hub 또는 legacy여야 합니다.")
    if (credentials.get("NAVER_CLIENT_ID") and not credentials.get("NAVER_CLIENT_SECRET")) or (
            credentials.get("NAVER_CLIENT_SECRET") and not credentials.get("NAVER_CLIENT_ID")):
        status.append({"source": "naver", "state": "skipped", "message": "네이버 ID와 Secret이 모두 필요합니다."})
    for source in sources:
        ready = source == "rss" or (
            source in {"news", "blog"} and credentials.get("NAVER_CLIENT_ID") and
            credentials.get("NAVER_CLIENT_SECRET")) or (
            source == "youtube" and credentials.get("YOUTUBE_API_KEY"))
        if not ready:
            status.append({"source": source, "state": "skipped", "message": "API 키가 없어 제외했습니다."})
            continue
        for query in queries:
            if progress:
                progress(f"{source} · {query}")
            try:
                if source == "rss":
                    batch = google_news(http, query, window, limit)
                elif source == "youtube":
                    batch = youtube_search(http, query, window, credentials["YOUTUBE_API_KEY"], limit)
                else:
                    batch = naver_search(http, query, window, source,
                                         credentials["NAVER_CLIENT_ID"], credentials["NAVER_CLIENT_SECRET"], limit,
                                         mode=naver_mode)
                items.extend(batch)
                entry = {"source": source, "query": query, "state": "ok", "count": len(batch)}
                if source in {"news", "blog"}:
                    entry["api_mode"] = naver_mode
                status.append(entry)
            except SourceError as exc:
                entry = {"source": source, "query": query, "state": "error", "message": str(exc)}
                if source in {"news", "blog"}:
                    entry["api_mode"] = naver_mode
                status.append(entry)
                # Avoid retrying invalid credentials/quota for every remaining query.
                break
    return items, status
