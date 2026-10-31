"""
네이버 블로그 후기 원고 작성기 (Gemini 무료 API 버전)
- 레퍼런스(PDF/이미지)의 문체를 참고
- 방문 정보 입력 + 이미지 폴더 분석
- 네이버 SEO 형식 원고 + 이미지 배치 + 공정위 문구 자동 삽입

실행:  streamlit run app_gemini.py
"""
import base64
import html
import io
import json
import os
import re
import zipfile
import requests
from datetime import datetime
from pathlib import Path

import time

from google import genai
from google.genai import types
import streamlit as st
from PIL import Image, ImageOps, ImageSequence

try:  # 아이폰 HEIC 사진 지원 (선택)
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

def is_deployment_environment() -> bool:
    """배포 환경 여부를 판별합니다. 필요하면 BLOG_APP_ENV로 명시적으로 덮어쓸 수 있습니다."""
    forced = os.environ.get("BLOG_APP_ENV", "").strip().lower()
    if forced in {"cloud", "deploy", "deployed", "production"}:
        return True
    if forced in {"local", "development", "dev"}:
        return False

    cloud_flags = (
        os.environ.get("IS_STREAMLIT_CLOUD", ""),
        os.environ.get("STREAMLIT_CLOUD", ""),
        os.environ.get("STREAMLIT_RUNTIME_ENV", ""),
    )
    if any(value.strip().lower() in {"1", "true", "yes", "cloud", "production"} for value in cloud_flags):
        return True

    # Streamlit Community Cloud의 일반적인 앱 소스 경로
    try:
        app_path = Path(__file__).resolve()
        if Path("/mount/src").exists() and Path("/mount/src") in app_path.parents:
            return True
    except Exception:
        pass
    return False


def get_default_folder() -> str:
    """이미지 폴더 기본값: 환경변수 → Streamlit Secrets → 프로젝트 내 images 폴더."""
    configured = os.environ.get("BLOG_IMAGE_FOLDER", "").strip()
    if not configured:
        try:
            configured = str(st.secrets.get("BLOG_IMAGE_FOLDER", "")).strip()
        except Exception:
            configured = ""
    if configured:
        return configured
    bundled_images = Path(__file__).resolve().parent / "images"
    return str(bundled_images) if bundled_images.is_dir() else ""

DEFAULT_MODEL = "gemini-3.1-flash-lite"
PROVIDER_MODELS = {
    "Gemini": "gemini-3.1-flash-lite",
    "Groq": "openai/gpt-oss-20b",
}
PROVIDER_URLS = {
    "Groq": "https://api.groq.com/openai/v1/chat/completions",
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}

DISCLOSURE = {
    "내돈내산": "※ 본 포스팅은 업체로부터 어떠한 대가나 제공 없이, 직접 비용을 지불하고 방문하여 작성한 후기입니다.",
    "기자단": "※ 본 포스팅은 기자단 활동의 일환으로 업체로부터 무료 식사(서비스)를 제공받아 작성한 후기입니다.",
    "체험단": "※ 본 포스팅은 체험단 활동의 일환으로 업체로부터 무료 식사(서비스)를 제공받아 솔직하게 작성한 후기입니다.",
}

# ──────────────────────────── 이미지 처리 ────────────────────────────


def crop_to_ratio(img: Image.Image, ratio: str) -> Image.Image:
    """선택한 비율에 맞춰 중앙 기준으로 자릅니다. 원본 유지 시 자르지 않습니다."""
    ratios = {"1:1 정사각형": 1.0, "4:3 가로": 4 / 3, "3:4 세로": 3 / 4, "4:5 세로": 4 / 5, "16:9 가로": 16 / 9}
    target = ratios.get(ratio)
    if not target:
        return img
    w, h = img.size
    current = w / h
    if current > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img


def to_jpeg(src, max_side: int, quality: int = 85, crop_ratio: str = "원본 유지") -> bytes:
    """경로 또는 bytes를 받아 첫 프레임을 JPEG로 변환합니다(API 분석용)."""
    img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = crop_to_ratio(img, crop_ratio)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def process_gif(src, max_side: int = 860, crop_ratio: str = "원본 유지") -> bytes:
    """애니메이션 GIF의 모든 프레임에 크롭/리사이즈를 적용하면서 움직임을 보존합니다."""
    im = Image.open(src)
    frames = []
    durations = []
    loop = im.info.get("loop", 0)
    try:
        for frame in ImageSequence.Iterator(im):
            fr = frame.convert("RGBA")
            fr = crop_to_ratio(fr, crop_ratio)
            fr.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            frames.append(fr.copy())
            durations.append(frame.info.get("duration", im.info.get("duration", 100)))
    finally:
        im.close()
    if not frames:
        raise ValueError("GIF 프레임을 읽을 수 없습니다.")
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=loop, disposal=2, optimize=True,
    )
    return buf.getvalue()


def process_output_image(path: Path, crop_ratio: str = "원본 유지"):
    """내보내기용 이미지 bytes, MIME, 확장자를 반환합니다. GIF는 애니메이션을 유지합니다."""
    if path.suffix.lower() == ".gif":
        return process_gif(path, 720, crop_ratio), "image/gif", ".gif"
    return to_jpeg(path, 760, quality=78, crop_ratio=crop_ratio), "image/jpeg", ".jpg"


def shot_time(path: Path) -> float:
    """촬영 시각(EXIF) → 없으면 파일 수정 시각. 방문 순서대로 정렬하기 위함."""
    try:
        exif = Image.open(path).getexif()
        v = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if v:
            return datetime.strptime(str(v), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return path.stat().st_mtime


def load_images(folder: str, crop_ratio: str = "원본 유지"):
    p = Path(folder).expanduser()
    if not p.is_dir():
        return []
    files = [f for f in p.iterdir() if f.suffix.lower() in IMG_EXT and not f.name.startswith(".")]
    files.sort(key=lambda f: (shot_time(f), f.name))
    out = []
    for i, f in enumerate(files, 1):
        try:
            out_bytes, out_mime, out_ext = process_output_image(f, crop_ratio)
            out.append(
                {
                    "id": f"IMG_{i:02d}",
                    "name": f.name,
                    "api": to_jpeg(f, 1280, quality=76),  # 분석용(첫 프레임, 용량 최적화)
                    "out": out_bytes,  # 내보내기용; GIF 애니메이션 유지
                    "out_mime": out_mime,
                    "out_ext": out_ext,
                }
            )
        except Exception as e:
            st.warning(f"읽을 수 없는 파일 건너뜀: {f.name} ({e})")
    return out


def load_uploaded_images(uploaded_files, crop_ratio: str = "원본 유지"):
    """Streamlit 업로드 이미지도 로컬 폴더 이미지와 같은 규격으로 처리한다."""
    out = []
    for i, uploaded in enumerate(uploaded_files or [], 1):
        try:
            raw = uploaded.getvalue()
            suffix = Path(uploaded.name).suffix.lower()
            if suffix == ".gif":
                output = process_gif(io.BytesIO(raw), 720, crop_ratio)
                mime, ext = "image/gif", ".gif"
            else:
                output = to_jpeg(io.BytesIO(raw), 760, quality=78, crop_ratio=crop_ratio)
                mime, ext = "image/jpeg", ".jpg"
            api_image = to_jpeg(io.BytesIO(raw), 1280, quality=76)
            out.append({"id": f"IMG_{i:02d}", "name": uploaded.name, "api": api_image,
                        "out": output, "out_mime": mime, "out_ext": ext})
        except Exception as e:
            st.warning(f"읽을 수 없는 업로드 파일 건너뜀: {uploaded.name} ({e})")
    return out


def img_block(jpeg: bytes):
    return types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")


# ──────────────────────────── Gemini 호출 ────────────────────────────


# 진행 상황 표시용 콜백 (UI에서 교체됨)
LOG = lambda m: None
TICK = lambda n: None
WAIT = lambda sec: None

RATE_LIMIT_MARKERS = ("429", "RESOURCE_EXHAUSTED", "rate limit", "quota", "too many requests")
SERVER_MARKERS = ("503", "UNAVAILABLE", "500", "INTERNAL", "502", "504", "overloaded")
TIMEOUT_MARKERS = ("timed out", "timeout", "read operation timed out", "deadline exceeded")


def classify_api_error(exc):
    msg = str(exc)
    upper = msg.upper()
    if any(x.upper() in upper for x in RATE_LIMIT_MARKERS):
        return "무료 사용량/요청 한도(429)"
    if any(x.upper() in upper for x in SERVER_MARKERS):
        return "Gemini 서버 일시 오류(5xx)"
    if any(x.upper() in upper for x in TIMEOUT_MARKERS):
        return "응답 대기 시간 초과(timeout)"
    if "403" in msg or "PERMISSION_DENIED" in upper:
        return "API 키 권한/모델 접근 오류(403)"
    if "400" in msg or "INVALID_ARGUMENT" in upper:
        return "요청 형식/입력 크기 오류(400)"
    return "분류되지 않은 API 오류"


def _part_to_openai_blocks(part):
    """google-genai Part를 OpenAI 호환 멀티모달 메시지로 변환한다."""
    inline = getattr(part, "inline_data", None)
    if not inline:
        return [{"type": "text", "text": str(part)}]
    data = inline.data
    if isinstance(data, str):
        raw = base64.b64decode(data)
    else:
        raw = bytes(data)
    mime = getattr(inline, "mime_type", None) or "application/octet-stream"
    if mime.startswith("image/"):
        b64 = base64.b64encode(raw).decode("ascii")
        return [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
    if mime == "application/pdf":
        # OpenAI 호환 API의 PDF 지원은 제공자마다 달라 PDF 페이지를 이미지로 변환한다.
        try:
            import fitz
            doc = fitz.open(stream=raw, filetype="pdf")
            blocks = []
            for page in list(doc)[:12]:
                pix = page.get_pixmap(matrix=fitz.Matrix(1.3, 1.3), alpha=False)
                b64 = base64.b64encode(pix.tobytes("jpeg")).decode("ascii")
                blocks.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            doc.close()
            if blocks:
                return blocks
        except Exception as e:
            raise RuntimeError("PDF 레퍼런스를 이미지로 변환하지 못했습니다. PyMuPDF 설치 여부를 확인하거나 PDF를 이미지로 저장해 첨부해 주세요.") from e
        raise RuntimeError("PDF 레퍼런스에서 페이지를 읽지 못했습니다.")
    raise RuntimeError(f"대체 AI가 지원하지 않는 첨부 형식입니다: {mime}")


def _openai_compatible_call(provider, api_key, model, system, content, max_tokens, label):
    """Groq Chat Completions 호출. GPT-OSS 텍스트 모델에 맞춰 content를 항상 문자열로 보낸다."""
    if provider != "Groq":
        raise RuntimeError(f"지원하지 않는 AI 제공자입니다: {provider}")

    # Gemini의 Part/inline_data, 문자열, 리스트가 섞여 들어와도 최종 user content는 반드시 str.
    text_parts = []
    items = content if isinstance(content, (list, tuple)) else [content]
    for item in items:
        if isinstance(item, str):
            text_parts.append(item)
        elif hasattr(item, "inline_data"):
            inline = getattr(item, "inline_data", None)
            mime = getattr(inline, "mime_type", None) or "첨부파일"
            text_parts.append(
                f"[첨부파일 생략: {mime}. 현재 선택된 Groq GPT-OSS 모델은 텍스트 전용이므로 이미지/PDF 내용을 분석하지 않습니다.]"
            )
        elif isinstance(item, dict):
            # 혹시 이전 멀티모달 형식이 넘어오더라도 dict/list 자체를 API에 전달하지 않는다.
            if item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            else:
                text_parts.append("[멀티모달 첨부 항목 생략: Groq 텍스트 모델 미지원]")
        else:
            text_parts.append(str(item))

    user_content = "\n\n".join(part for part in text_parts if part).strip()
    if not isinstance(user_content, str):  # 최종 방어: API 오류 재발 방지
        user_content = str(user_content)

    headers = {"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"}
    payload = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": str(system)},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    # Groq의 오류를 보내기 전에 앱 내부 요청 형식을 검증한다.
    if not isinstance(payload["messages"][1]["content"], str):
        raise RuntimeError("Groq 요청 생성 오류: messages[1].content가 문자열이 아닙니다.")

    try:
        response = requests.post(PROVIDER_URLS[provider], headers=headers, json=payload, timeout=(20, 240))
    except requests.RequestException as e:
        raise RuntimeError(f"{provider} 연결 오류: {e}") from e
    if response.status_code >= 400:
        detail = response.text.replace("\n", " ")[:700]
        if response.status_code in (401, 403):
            hint = "Groq API 키 인증/권한 오류입니다. console.groq.com/keys에서 발급한 키인지 확인해 주세요."
        elif response.status_code == 400:
            hint = "Groq 요청 형식 또는 모델 지원 문제입니다. 기본 모델 ID(openai/gpt-oss-20b)로 먼저 테스트해 주세요."
        elif response.status_code == 429:
            hint = "무료 요청 한도 또는 분당 사용량 제한일 수 있습니다. 잠시 후 재시도해 주세요."
        elif response.status_code in (500, 502, 503, 504):
            hint = "Groq 서버가 일시적으로 불안정합니다. 잠시 후 재시도해 주세요."
        else:
            hint = "Groq 모델 ID와 API 요청을 확인해 주세요."
        raise RuntimeError(f"Groq API 오류 HTTP {response.status_code}. {hint} 상세: {detail}")
    try:
        data = response.json()
        result = data["choices"][0]["message"]["content"]
        if isinstance(result, list):
            result = "".join(x.get("text", "") for x in result if isinstance(x, dict))
        return str(result or "").strip()
    except Exception as e:
        raise RuntimeError(f"Groq 응답을 해석하지 못했습니다: {response.text[:400]}") from e


def ask(client, model, system, content, max_tokens=16000, label="응답") -> str:
    """선택된 AI 제공자로 호출하고, 일시 오류는 짧게 재시도한다."""
    provider = client.get("provider", "Gemini")
    api_key = client.get("api_key", "")
    last = None
    for attempt in range(3):
        t0 = time.time()
        try:
            LOG(f"🔄 {label} 요청 중... ({provider}, 시도 {attempt + 1}/3)")
            if provider == "Gemini":
                gclient = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=240_000))
                text = ""
                for ch in gclient.models.generate_content_stream(
                    model=model,
                    contents=content,
                    config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens),
                ):
                    if ch.text:
                        text += ch.text
                        TICK(len(text))
            else:
                text = _openai_compatible_call(provider, api_key, model, system, content, max_tokens, label)
                TICK(len(text))
            if not text:
                raise RuntimeError(f"{provider}가 빈 응답을 반환했습니다.")
            LOG(f"✅ {label} 완료 ({time.time() - t0:.0f}초, {len(text):,}자)")
            return text
        except Exception as e:
            last = e
            msg = str(e).replace("\n", " ")[:500]
            # 잘못된 키/요청 형식은 재시도해도 해결되지 않으므로 즉시 안내한다.
            if attempt >= 2 or any(code in msg for code in ("HTTP 400", "HTTP 401", "HTTP 403", "지원하지 않는 첨부", "PDF 레퍼런스")):
                LOG(f"❌ {label} 실패: {msg}")
                raise RuntimeError(msg) from e
            wait = (3, 8)[min(attempt, 1)]
            LOG(f"⚠️ {provider} 호출 실패. {wait}초 후 재시도합니다. 상세: {msg}")
            for sec in range(wait, 0, -1):
                WAIT(sec)
                time.sleep(1)
    raise last


ANALYZE_SYSTEM = "너는 음식점/카페/장소 후기 블로그를 위한 사진 분석 보조자다. 보이는 것만 정확히 기술하고 추측으로 지어내지 않는다."

ANALYZE_PROMPT = """위 사진들을 각각 분석해서 JSON 배열만 출력해라(설명/코드블록 금지).
각 원소: {"id": "IMG_01", "category": "외관|입구|내부|메뉴판|음식|음료|사이드|테이블세팅|먹는장면|영수증|기타",
"description": "눈에 보이는 구체적 내용(색, 재료, 양, 플레이팅, 분위기, 간판/메뉴판의 읽히는 글자)",
"menu_guess": "메뉴명이 읽히거나 확실할 때만, 아니면 빈 문자열",
"caption": "블로그 사진 아래 들어갈 한 줄 캡션 후보",
"usable": true/false  // 심하게 흐리거나, 얼굴·번호판·영수증 카드번호 등 개인정보가 두드러지면 false
}"""


def analyze_images(client, model, imgs):
    """이미지를 작은 묶음으로 분석하고, 묶음마다 session_state에 즉시 저장한다."""
    results = dict(st.session_state.get("analysis", {}))
    if client.get("provider") == "Groq":
        # 현재 기본 Groq GPT-OSS 텍스트 모델에는 이미지 내용을 보내지 않는다.
        # 이미지 내용을 본 것처럼 추측하지 않고 파일명만 보존한다.
        for im in imgs:
            results.setdefault(im["id"], {
                "id": im["id"], "category": "기타",
                "description": f"이미지 분석 생략(Groq 텍스트 모델): {im['name']}",
                "menu_guess": "", "caption": "", "usable": True,
            })
        st.session_state.analysis = results.copy()
        LOG("ℹ️ Groq 텍스트 모델은 이미지 분석을 지원하지 않아 사진 내용 분석은 건너뛰고 원고를 작성합니다.")
        return results
    step = 4  # 무료 API에서 한 요청의 이미지 처리 부담을 낮춤
    pending = [im for im in imgs if im["id"] not in results]
    n_chunks = (len(pending) + step - 1) // step
    for i in range(0, len(pending), step):
        chunk = pending[i : i + step]
        content = []
        for im in chunk:
            content.append(f"[{im['id']}] 파일명: {im['name']}")
            content.append(img_block(im["api"]))
        content.append(ANALYZE_PROMPT)
        label = f"이미지 분석 {i // step + 1}/{n_chunks} (사진 {chunk[0]['id'][4:]}~{chunk[-1]['id'][4:]}번)"
        text = ask(client, model, ANALYZE_SYSTEM, content, max_tokens=6000, label=label)
        try:
            match = re.search(r"\[.*\]", text, re.S)
            if not match:
                raise ValueError("JSON 배열을 찾지 못했습니다")
            arr = json.loads(match.group(0))
            for a in arr:
                if isinstance(a, dict) and a.get("id"):
                    results[a["id"]] = a
            # 일부 누락된 사진은 재요청 대상이 되지 않도록 기본값 저장
            for im in chunk:
                results.setdefault(im["id"], {"id": im["id"], "category": "기타", "description": "분석 결과에 포함되지 않음", "usable": True})
        except Exception as e:
            raise RuntimeError(f"{label} 응답을 JSON으로 해석하지 못했습니다: {e}. 응답 일부: {text[:250]}") from e
        st.session_state.analysis = results.copy()  # 성공한 묶음은 즉시 보존
        LOG(f"💾 분석 저장 완료: {len(results)}/{len(imgs)}장")
    return results


DEFAULT_USER_STYLE_PROFILE = """[기본 문체: 사용자가 제공한 블로그 원고와 블로그 캡처 이미지 기반]

핵심 톤
- 실제로 다녀온 사람이 친구에게 후기 풀어주듯 편하고 솔직한 한국어를 사용한다. 정보 전달은 하되 광고 문구처럼 반듯하게 정리하지 않는다.
- 말투는 구어체와 일상적인 블로그체를 섞는다. '좋았다', '괜찮았다', '기억에 남았다'처럼 담백한 표현과 'ㅋㅋ', 'ㅎㅎ', '~', 'ㅠ', '그잡채', 'ㄹㅇ', '맛도리', '갈김' 같은 표현을 상황에 맞게 가끔 쓴다. 모든 문장에 유행어나 웃음 표현을 억지로 넣지 않는다.
- '양심발언 합니다.', '굳이 먼 곳으로 캠핑 갈 필요가 있을까', '이거 쓰다 보니 또 먹고프네,,,'처럼 혼잣말·괄호·자화자찬·가벼운 농담을 자연스럽게 섞는다. 표현을 그대로 반복하지 말고 새 상황에 맞게 창작한다.
- 감탄은 구체적인 장면에 붙인다. 과장된 극찬을 연속으로 쓰지 않고, 실제로 인상적이었던 점과 무난했던 점을 구분한다.

문장과 문단
- 짧은 문장 한두 줄을 단독으로 두기도 하고, 설명이 필요한 부분은 두 문장 정도로 풀어 쓴다. 문단 길이를 모두 똑같이 맞추지 않는다.
- 모바일에서 읽기 좋게 문장 사이에 줄바꿈을 자주 사용한다. 한 문단에 정보를 빽빽하게 몰아넣지 않는다.
- 설명 문장 뒤에 짧은 감상이나 농담을 붙이는 리듬을 활용한다. 문장 끝을 매번 '~습니다'로 통일하지 않는다. '~다', '~했다', '~좋았다', '~괜찮아 보였다', '~인 듯', '~라는 것!!', '~추천합니다' 등을 섞는다.
- 쉼표 두 개나 말줄임표를 가끔 사용하되 남발하지 않는다. 느낌표와 괄호도 글의 흐름에 맞을 때만 쓴다.

전개 방식
- 도입은 방문 계기나 일상적인 상황에서 시작한다. 예: 바쁜 현생, 친구들과 갈 곳 찾기, 근처에서 식사할 곳 찾기 등. 사용자가 제공하지 않은 상황은 지어내지 않는다.
- 외관/위치, 내부 분위기, 메뉴·이용 과정, 실제 먹거나 이용한 경험, 기억에 남은 점, 이용 팁, 총평 순으로 자연스럽게 흘러갈 수 있다. 매번 똑같은 목차를 기계적으로 강제하지 말고 자료가 있는 부분만 살린다.
- 사진 마커 전후에는 해당 사진과 연결되는 짧은 설명이나 감상을 둔다. 사진마다 '사진을 보니', '다음으로' 같은 똑같은 연결 문구를 반복하지 않는다.
- 메뉴 설명은 객관적인 특징과 개인적인 감상을 함께 쓴다. 맛·식감·양·분위기 등은 제공된 메모나 사진에서 확인 가능한 범위만 쓴다.
- '이용팁'에는 예약, 주차, 주문, 웨이팅처럼 실제로 도움이 되는 내용을 넣고, 마지막 '총평'은 앞 내용을 복사하지 말고 누구에게 어울릴지와 기억에 남은 점을 짧게 정리한다.
- 해시태그는 본문과 관련된 지역·업종·상호·방문 목적을 중심으로 작성한다. 같은 키워드를 과하게 반복하지 않는다.

첨부된 블로그 화면 캡처에서 보이는 구성 특징
- 대표 사진/썸네일에 핵심 주제를 짧고 크게 넣고, 본문은 사진을 충분히 배치하는 이미지 중심 구성이다.
- 서론에서 방문 계기와 장소에 대한 첫인상을 짧게 풀고, 본론에서는 외관·내부·시설/분위기·이용 과정·메뉴/서비스 경험을 사진 순서에 맞춰 소개한다.
- 사진 사이에는 사진에 실제로 담긴 내용을 설명하는 짧은 문단을 둔다. 한두 문장짜리 짧은 감상과 조금 더 긴 설명을 섞어 모바일에서 읽기 편하게 만든다.
- 음식점 글은 메뉴판 사진을 음식 사진보다 먼저 배치한다. 메뉴판/가격표 사진이 있으면 메뉴 소개 직전에 먼저 보여주고, 그다음 전체 상차림·메뉴별 근접 사진 순서로 구성한다. 메뉴판 사진이 없으면 메뉴판을 본 것처럼 쓰지 않는다.
- 결론은 반드시 '이용팁'과 '총평'을 별도 소제목으로 나눈다. 이용팁에는 확인된 주차·예약·웨이팅·주문·위치 정보를, 총평에는 실제 경험을 바탕으로 추천 대상과 아쉬운 점을 간결하게 적는다.
- 글 끝부분에는 주소·영업시간 등 이용 정보와 지도/위치 정보가 모일 수 있다. 입력 정보가 없으면 임의로 만들지 않는다.
- 캡처에 보이는 스티커/장식 이미지는 실제 이미지 폴더에 존재하는 경우에만 사진 마커로 배치한다. 장식 이미지를 새로 지어내거나 음식 사진으로 대체하지 않는다.

피해야 할 것
- 전형적인 AI 문구: '이번 포스팅에서는', '전반적으로 만족스러운 경험이었습니다', '특별한 시간을 보낼 수 있었습니다', '강력 추천드립니다', '완벽한 선택' 등.
- 지나치게 정돈된 홍보 문구, 보도자료 말투, 모든 문단이 결론-근거 구조인 글, 불필요한 소제목 남발.
- 제공되지 않은 경험·감정·가격·웨이팅·직원 응대·맛 표현을 지어내는 것.
- 유행어와 이모지를 모든 문단에 넣거나, 예시 원고의 문장을 그대로 재사용하는 것.

기본 작성 체크리스트
1. 첫 부분에서 방문 계기나 일상적인 상황이 자연스럽게 드러나는가?
2. 문단 길이와 줄바꿈이 획일적이지 않은가?
3. 정보 전달 뒤에 개인적인 감상이나 가벼운 말투가 자연스럽게 섞였는가?
4. 광고 같은 표현과 AI식 결론을 제거했는가?
5. 사진 마커의 순서와 개수를 그대로 유지했는가?
6. 사용자가 제공하지 않은 사실을 추가하지 않았는가?
"""

STYLE_ANALYZE_SYSTEM = """너는 제공된 블로그 레퍼런스의 문체를 분석하는 편집자다. 주제나 장소 정보가 아니라 글쓰기 습관을 분석한다. 레퍼런스에 없는 특징을 지어내지 말고, 근거가 부족한 항목은 '자료에서 뚜렷하지 않음'이라고 적는다. 원문을 길게 복제하지 말고 대표 예시는 각각 1문장 이내로 짧게 발췌한다.
다음 항목을 한국어로 구체적으로 작성한다:
1. 전체적인 말투와 독자에게 말을 거는 방식
2. 도입부를 시작하는 패턴
3. 문장 길이와 리듬, 연결어·종결어미 습관
4. 문단 길이와 줄바꿈 패턴
5. 정보와 개인 경험을 배치하는 순서
6. 감탄사·괄호·물결표·이모지·구어체 사용 습관
7. 실제 사람 글처럼 느껴지는 세부 특징
8. 피해야 할 문체(레퍼런스에 없는 전형적인 AI 문구 포함)
9. 서로 다른 레퍼런스에 공통으로 나타나는 특징과 글마다 다른 특징
10. 대표 문장 예시 3개 이내(각 1문장 이내, 스타일 설명용)
마지막에 '작성 시 체크리스트'를 5~8개 항목으로 작성한다. 표면적인 키워드나 주제 내용을 문체 특징으로 혼동하지 않는다."""

STYLE_REVIEW_SYSTEM = """너는 블로그 원고의 문체 일치도를 검수하는 전문 편집자다.
주어진 레퍼런스 문체 프로필과 대표 예시를 기준으로 초안을 검토하고, 문체가 어긋난 부분만 고쳐 전체 원고를 다시 출력한다.
- 레퍼런스의 말투, 문장 리듬, 문단 길이, 도입 방식, 정보 전개를 우선한다.
- 흔한 AI식 문구, 과도하게 매끈한 광고체, 불필요한 요약/총평을 추가하지 않는다.
- 사실, 메뉴, 가격, 주소, 방문 경험을 새로 만들거나 바꾸지 않는다.
- 제목/본문/태그/키워드 구분자와 모든 [[IMG_XX]] 이미지 마커를 정확히 보존한다. 마커를 삭제·중복·재배치하지 않는다.
- 초안의 내용과 길이를 불필요하게 바꾸지 않고 문체만 보정한다.
- 아래 형식으로만 출력한다:
===TITLE===
...
===BODY===
...
===TAGS===
...
===KEYWORDS===
..."""


def analyze_reference_style(client, model, ref_blocks):
    content = list(ref_blocks)
    content.append("첨부된 레퍼런스들을 함께 분석해, 여러 글에서 반복되는 문체 특징과 개별 글의 차이를 구분한 재사용 가능한 문체 프로필을 작성해라. 위 STYLE_ANALYZE_SYSTEM의 항목을 빠짐없이 포함해라.")
    return ask(client, model, STYLE_ANALYZE_SYSTEM, content, max_tokens=7000, label="레퍼런스 문체 분석")


def review_style(client, model, draft, profile):
    if not profile or not profile.strip():
        return draft
    content = [f"[문체 프로필 및 대표 예시]\n{profile}\n\n[검수할 초안]\n{draft}\n\n문체 프로필을 기준으로 문체만 보정한 전체 원고를 출력해라."]
    return ask(client, model, STYLE_REVIEW_SYSTEM, content, max_tokens=16000, label="문체 검수·보정")


WRITE_SYSTEM = """너는 네이버 블로그 맛집/장소 후기 전문 작가다. 첨부된 '레퍼런스 원문'의 문체를 분석해 같은 블로거가 쓴 것처럼 작성한다.

[문체 모방]
- 사용자 제공 문체 프로필이 있으면 일반적인 블로그 템플릿보다 프로필과 대표 문장 패턴을 우선한다.
- 종결어미, 말투, 이모지/이모티콘 빈도, 문장 길이, 문단 구분 방식, 소제목 스타일, 감탄사 습관을 따라 한다.
- 레퍼런스의 문장·내용을 그대로 복사하지 않는다. 스타일만 참고한다.

[네이버 SEO]
- 제목은 25~40자 안팎으로 작성하고, 사용자가 입력한 메인 키워드를 제목 앞쪽에 정확히 포함한다. 과장·낚시 금지.
- 메인 키워드가 입력되면 그 문구를 띄어쓰기·철자·순서까지 한 글자도 바꾸지 않는 '정확 일치 키워드'로 취급한다.
- 제목에 사용한 핵심 키워드 문구를 본문에 최소 5회 이상 정확히 동일한 형태로 노출한다. 첫 문단에 반드시 1회 넣고, 본문 중간과 후반에 문맥에 맞게 분산한다. 조사나 수식어를 붙이는 것은 가능하지만 키워드 자체의 띄어쓰기와 철자는 변경하지 않는다.
- 본문 작성 후 제목의 키워드 문구가 본문에 정확히 몇 회 등장하는지 자체 점검한다. 5회 미만이면 자연스러운 문장에 추가해 기준을 충족한다. 반복을 위해 어색한 문장을 만들거나 키워드만 나열하지 않는다.
- 메인 키워드가 비어 있으면 업체명·지역·업종/메뉴에서 대표 키워드를 정하고, 제목에 넣은 정확한 문구를 본문에 최소 5회 동일하게 사용한다.
- 본문은 사진 수와 입력 정보에 맞춰 충분히 작성하되, 글자 수를 채우려고 같은 말을 반복하지 않는다.
- 네이버 에디터에 바로 붙여넣을 수 있도록 마크다운 제목 기호(#, **)를 사용하지 않는다. 소제목은 짧고 명확하게 사용한다.
- 전체 구조는 반드시 '서론 → 본론 → 결론'으로 구성한다. 결론 안에서 '이용팁'과 '총평'을 각각 별도 소제목으로 분리한다.
- 최상급·단정 표현("무조건", "인생 맛집", 효능 주장)은 피하고 경험 기반으로 서술한다.
- 마지막에 해시태그 8~12개.
- 메인 키워드와 서브 키워드는 검색 의도에 맞게 사용하되, 키워드 목록을 나열하지 말고 실제 정보가 담긴 문장에 포함한다.
- 서브 키워드는 문맥에 맞을 때만 사용하고 억지로 모두 넣지 않는다. 동일 키워드를 한 문단에 몰아넣지 않는다.
- 사용자가 선택한 글 유형/검색 의도를 우선해 검색자가 궁금해할 정보(위치·가격·메뉴·과정·장단점·추천 대상)를 빠뜨리지 않는다.
- 확인되지 않은 검색량, 인기 순위, 방문자 수, 영업 정보, 주차 가능 여부를 추정하지 않는다. 검색 상위 노출이나 순위를 보장하는 표현을 쓰지 않는다.

[이미지 배치]
- 사진은 [[IMG_01]] 처럼 마커를 단독 한 줄로 넣는다. 분석표에서 usable=true인 사진은 가급적 모두, 각각 정확히 한 번만 사용한다.
- 흐름은 대체로 외관 → 내부/시설 → 메뉴판/가격표 → 음식 전체샷 → 음식별 근접샷 → 마무리 순으로 구성한다. 음식 사진보다 메뉴판 사진을 반드시 먼저 배치한다(메뉴판 사진이 실제로 제공된 경우).
- 같은 종류 사진은 연속 3장까지만 배치하고, 사진 직후에는 그 사진 내용과 맞는 1~3문장을 쓴다. 사진 설명에 없는 맛·재료·가격을 지어내지 않는다.
- 메뉴판 사진을 배치한 뒤에 메뉴명과 가격을 소개하고, 음식 사진에서는 비주얼·양·식감·맛 등 제공된 메모나 사진으로 뒷받침되는 부분만 서술한다.

[사실 관계]
- 주소·영업시간·전화·가격은 입력 정보나 사진에서 확인되는 것만 쓴다. 없으면 해당 항목은 "[확인 필요]"로 표시한다.
- 방문 인원에 맞게 서술하되(혼자 / 2명 / 3~4명 / 5명 이상) 동행자의 이름·관계를 지어내지 않는다. 사용자가 준 '기억나는 점'은 반드시 반영한다.
- 공정위 문구는 시스템이 자동으로 상단/하단에 넣으므로 본문에 쓰지 않는다. 단, 기자단/체험단이면 제공받은 입장임을 인지한 솔직한 톤을 유지하고 아쉬운 점도 한두 가지 담는다.

[출력 형식 — 반드시 아래 구분자 사용]
===TITLE===
(제목)
===BODY===
서론
(방문 계기와 첫인상)

본론
(외관·내부·메뉴판·음식/서비스 경험을 사진 순서에 맞춰 전개)

결론
이용팁
(확인된 실용 정보)

총평
(솔직한 최종 감상과 추천 대상)
(본문, 이미지 마커 포함)
===TAGS===
#태그1 #태그2 ...
===KEYWORDS===
(사용한 핵심 키워드 쉼표 구분)"""



def _clean_for_seo(text: str) -> str:
    return re.sub(r"\[\[IMG_\d+\]\]", " ", text or "")


def audit_blog_seo(result: dict, form: dict, images: dict | None = None) -> dict:
    """발행 전 구조·키워드 점검. 네이버 검색 순위 점수가 아닌 원고 작성 품질 체크다."""
    title = (result.get("title") or "").strip()
    body = _clean_for_seo(result.get("body") or "")
    tags = (result.get("tags") or "").strip()
    keyword = (form.get("keyword") or "").strip()
    paragraphs = [x.strip() for x in re.split(r"\n\s*\n", body) if x.strip() and not MARK.match(x.strip())]
    plain = re.sub(r"\s+", " ", body).strip()
    exact_count = body.count(keyword) if keyword else 0
    first_part = plain[:220]
    keyword_paras = sum(1 for para in paragraphs if keyword and keyword in para)
    title_ok = bool(title and (keyword in title if keyword else True))
    count_ok = exact_count >= 5 if keyword else True
    intro_ok = bool(keyword and keyword in first_part) if keyword else bool(plain)
    spread_ok = keyword_paras >= min(3, max(1, len(paragraphs))) if keyword else True
    section_ok = all(x in body for x in ["서론", "본론", "결론", "이용팁", "총평"])
    tag_count = len(re.findall(r"#[^\s#]+", tags))
    tag_ok = 5 <= tag_count <= 15
    length = len(re.sub(r"\s", "", plain))
    length_ok = length >= 700
    # 동일 문장 반복은 과도한 반복의 신호만 제공한다(문장 중복 자체가 검색 순위 요소라는 뜻은 아님).
    sentences = [x.strip() for x in re.split(r"[.!?。\n]+", plain) if len(x.strip()) >= 15]
    duplicates = len(sentences) - len(set(sentences))
    duplication_ok = duplicates == 0
    photo_ok = True
    if images:
        used = re.findall(r"\[\[(IMG_\d+)\]\]", result.get("body", ""))
        photo_ok = len(used) == len(set(used)) and all(x in images for x in used)
    checks = [
        ("제목에 메인 키워드 포함", title_ok, 10, "제목에 입력한 키워드가 정확히 들어갔는지 확인"),
        ("본문 정확 일치 키워드 5회 이상", count_ok, 15, f"현재 {exact_count}회" if keyword else "메인 키워드 미입력"),
        ("첫 부분에 메인 키워드 포함", intro_ok, 10, "도입부에서 주제가 바로 드러나는지 확인"),
        ("키워드 문단별 분산", spread_ok, 10, f"키워드가 포함된 문단 {keyword_paras}개" if keyword else "메인 키워드 미입력"),
        ("서론·본론·결론 및 이용팁·총평", section_ok, 15, "요청한 글 구조 확인"),
        ("해시태그 5~15개", tag_ok, 5, f"현재 {tag_count}개"),
        ("본문 정보량", length_ok, 10, f"공백 제외 약 {length:,}자 (700자 기준은 내부 점검용)"),
        ("동일 문장 반복 없음", duplication_ok, 10, f"중복 의심 문장 {duplicates}개"),
        ("사진 마커 중복·오류 없음", photo_ok, 5, "사진 배치 마커 확인"),
        ("실제 경험·구체 정보 보강", bool(form.get("memo") or form.get("menu") or form.get("reason")), 10, "입력한 경험 정보가 충분한지 직접 검토"),
    ]
    score = sum(weight for _, ok, weight, _ in checks if ok)
    return {"score": score, "checks": checks, "keyword_count": exact_count, "keyword_paragraphs": keyword_paras, "body_length": length, "title_length": len(title)}


def suggest_title_patterns(form: dict, current_title: str) -> list[str]:
    """메인 키워드를 보존하는 제목 초안. 실제 검색량·경쟁도 예측은 하지 않는다."""
    kw = (form.get("keyword") or "").strip()
    store = (form.get("store") or "").strip()
    region = (form.get("region") or "").strip()
    if not kw:
        return [current_title] if current_title else []
    suffixes = ["방문 후기", "솔직 후기", "메뉴와 분위기 정리", "위치와 이용팁까지"]
    candidates = [f"{kw} {store} {suffix}".strip() for suffix in suffixes[:2] if store]
    candidates += [f"{kw} {suffix}" for suffix in suffixes]
    if region and region not in kw:
        candidates.append(f"{kw} {region} 방문 후기")
    out=[]
    for title in [current_title] + candidates:
        title=re.sub(r"\s+", " ", title).strip()
        if title and title not in out:
            out.append(title)
    return out[:6]

def build_ref_blocks(files):
    blocks = []
    for f in files or []:
        raw = f.getvalue()
        if f.name.lower().endswith(".pdf"):
            blocks.append(types.Part.from_bytes(data=raw, mime_type="application/pdf"))
        else:
            blocks.append(img_block(to_jpeg(io.BytesIO(raw), 1568)))
    return blocks


def generate(client, model, ref_blocks, form, analysis, style_profile="", prev=None, feedback=None):
    usable = {k: v for k, v in analysis.items()}
    info = f"""[방문 정보]
- 업체명: {form['store'] or '(미입력 — 사진/메뉴판에서 확인되면 사용)'}
- 지역: {form['region'] or '(미입력)'}
- 메인 키워드: {form['keyword'] or '(없음 — 지역+업종/메뉴로 자동 선정)'}
- 서브 키워드: {form.get('subkeywords') or '(미입력 — 억지로 만들지 말 것)'}
- 글 유형: {form.get('content_type', '자동 선택')}
- 검색 의도: {form.get('search_intent', '자동 선택')}
- 방문 인원: {form['party']}
- 주문 메뉴: {form['menu']}
- 방문 계기: {form['reason']}
- 공정위 표시 유형: {form['ftc']}
- 추가로 기억나는 점: {form['memo'] or '(없음)'}

[사진 분석표 — 촬영 순서대로]
{json.dumps(list(usable.values()), ensure_ascii=False, indent=1)}"""
    content = list(ref_blocks)
    intro = "위 첨부는 문체를 참고할 레퍼런스 원문이다.\n\n" if ref_blocks else "(레퍼런스 없음 — 자연스러운 네이버 후기체로 작성)\n\n"
    profile_text = f"[사용자 지정 문체 프로필 — 반드시 우선 반영]\n{style_profile}\n\n" if style_profile and style_profile.strip() else "[문체 프로필 없음 — 첨부 레퍼런스의 문체를 직접 분석해 반영]\n\n"
    content.append(profile_text + intro + info + "\n\n위 정보로 원고를 작성해라.")
    if prev and feedback:
        content.append(f"[이전 초안]\n{prev}\n\n[수정 요청]\n{feedback}\n\n수정 요청을 반영해 전체 원고를 같은 형식으로 다시 출력해라.")
    return ask(client, model, WRITE_SYSTEM, content, label="원고 작성")


def parse(text: str):
    def sec(name):
        m = re.search(rf"==={name}===\s*(.*?)(?====[A-Z]+===|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    return {"title": sec("TITLE"), "body": sec("BODY"), "tags": sec("TAGS"), "keywords": sec("KEYWORDS"), "raw": text}


# ──────────────────────────── 렌더링 / 내보내기 ────────────────────────────

MARK = re.compile(r"^\s*\[\[(IMG_\d+)\]\]\s*$")


def disclosure_text(form):
    t = DISCLOSURE[form["ftc"]]
    if form["ftc"] != "내돈내산" and form.get("provided"):
        t += f" (제공 내역: {form['provided']})"
    return t


def used_ids(body):
    return [m.group(1) for line in body.splitlines() if (m := MARK.match(line))]


def render_preview(res, imgs_by_id, form):
    st.markdown(f"### {res['title']}")
    st.caption(disclosure_text(form))
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            st.image(imgs_by_id[m.group(1)]["out"])
        elif line.strip():
            st.markdown(line.replace("~", r"\~"))
    st.markdown(res["tags"])
    st.caption(disclosure_text(form))


def placement_items(res, imgs_by_id):
    """미리보기 본문에 실제로 등장하는 순서대로 사진 번호와 파일명을 만든다."""
    items = []
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            im = imgs_by_id[m.group(1)]
            seq = len(items) + 1
            filename = f"{seq:02d}_{Path(im['name']).stem}{im.get('out_ext', '.jpg')}"
            items.append({"seq": seq, "iid": m.group(1), "image": im, "filename": filename})
    return items


def build_text(res, imgs_by_id, form):
    out = [res["title"], "", disclosure_text(form), ""]
    seq = 0
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m:
            im = imgs_by_id.get(m.group(1))
            if im:
                seq += 1
                filename = f"{seq:02d}_{Path(im['name']).stem}{im.get('out_ext', '.jpg')}"
                out.append(f"[사진 {seq:02d} : {filename}]")
            else:
                out.append(f"[사진 : 원본 이미지 확인 필요 - {m.group(1)}]")
        else:
            out.append(line)
    out += ["", res["tags"], "", disclosure_text(form)]
    return "\n".join(out)


def build_html(res, imgs_by_id, form):
    parts = [f"<h2>{html.escape(res['title'])}</h2>", f"<p><small>{html.escape(disclosure_text(form))}</small></p>"]
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            im = imgs_by_id[m.group(1)]
            b64 = base64.b64encode(im["out"]).decode()
            mime = im.get("out_mime", "image/jpeg")
            parts.append(f'<p style="text-align:center"><img src="data:{mime};base64,{b64}" style="max-width:100%"></p>')
        elif line.strip():
            parts.append(f"<p>{html.escape(line)}</p>")
    parts += [f"<p>{html.escape(res['tags'])}</p>", f"<p><small>{html.escape(disclosure_text(form))}</small></p>"]
    return "<html><head><meta charset='utf-8'></head><body style='max-width:860px;margin:auto;line-height:1.8'>" + "\n".join(parts) + "</body></html>"


def build_zip(res, imgs_by_id, form):
    """원고의 사진 번호와 ZIP 안 이미지 파일명이 미리보기 순서로 일치하도록 저장합니다."""
    buf = io.BytesIO()
    items = placement_items(res, imgs_by_id)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("원고.txt", build_text(res, imgs_by_id, form))
        z.writestr("원고.html", build_html(res, imgs_by_id, form))
        for item in items:
            z.writestr(f"사진/{item['filename']}", item["image"]["out"])
    return buf.getvalue()


def build_naver_zip(res, imgs_by_id, form):
    """미리보기 순서대로 사진과 원고를 묶습니다. 별도 배치순서표는 만들지 않습니다."""
    buf = io.BytesIO()
    items = placement_items(res, imgs_by_id)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("원고.txt", build_text(res, imgs_by_id, form))
        for item in items:
            z.writestr(f"사진/{item['filename']}", item["image"]["out"])
    return buf.getvalue()


def make_client(provider_name, key):
    # 붙여넣기 시 앞뒤 공백/개행이 섞여 인증이 실패하는 경우를 방지합니다.
    return {"provider": provider_name, "api_key": (key or "").strip()}


# ──────────────────────────── UI ────────────────────────────

st.set_page_config(page_title="네이버 블로그 후기 원고 작성기", page_icon="✍️", layout="wide")
st.title("✍️ 네이버 블로그 후기 원고 작성기")

with st.sidebar:
    st.header("설정")
    provider = st.selectbox(
        "AI 제공자",
        ["Gemini", "Groq"],
        index=0,
        key="ai_provider",
        help="Gemini 서버가 503 오류일 때 Groq로 전환할 수 있습니다. 제공자별 API 키가 필요합니다.",
    )
    key_env = {
        "Gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "Groq": ("GROQ_API_KEY",),
    }[provider]
    key_label = {"Gemini": "Gemini API Key", "Groq": "Groq API Key"}[provider]
    key_default = next((os.environ.get(k, "") for k in key_env if os.environ.get(k)), "")
    if not key_default:
        try:
            key_default = next((str(st.secrets[k]) for k in key_env if k in st.secrets), "")
        except Exception:
            key_default = ""
    api_key = st.text_input(key_label, value=key_default, type="password", key=f"api_key_{provider}").strip()
    model = st.text_input("모델 ID", value=PROVIDER_MODELS[provider], key=f"model_{provider}", help="기본 모델을 사용하거나 해당 제공자의 모델 ID로 변경할 수 있습니다.")
    if provider == "Groq":
        st.caption("Groq GPT-OSS 모델은 텍스트 전용으로 사용합니다. 사진은 ZIP에 포함되지만 사진 내용의 AI 분석은 생략됩니다. 이미지 분석은 Gemini를 선택해 주세요.")
        st.caption("Groq Console에서 발급한 Groq 전용 API 키를 입력하세요. 다른 AI 제공자/Gemini 키는 사용할 수 없습니다.")
        st.markdown("[Groq API 키 발급/관리 페이지 열기](https://console.groq.com/keys)")
        if st.button("Groq API 키 연결 테스트", key="test_groq_api_key"):
            if not api_key:
                st.error("Groq API 키를 먼저 입력해 주세요.")
            else:
                try:
                    check = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
                        json={"model": model.strip(), "messages": [{"role": "user", "content": "Reply with OK only."}], "max_tokens": 8, "stream": False},
                        timeout=(15, 45),
                    )
                    if check.status_code == 200:
                        st.success(f"Groq 연결 성공! 모델 '{model}'의 실제 텍스트 생성 요청까지 확인했습니다.")
                    elif check.status_code == 401:
                        st.error("Groq가 API 키를 거부했습니다(401). console.groq.com/keys에서 새 키를 발급해 주세요.")
                    else:
                        st.error(f"Groq 모델/API 테스트 실패: HTTP {check.status_code} — {check.text[:500]}")
                except requests.RequestException as e:
                    st.error(f"Groq 연결 테스트 중 네트워크 오류: {e}")
    # 로컬 실행에서만 폴더 경로 입력과 다시 읽기 버튼을 노출합니다.
    # 배포 환경에서는 해당 컨트롤을 숨기고 업로드 이미지 또는 저장소의 images 폴더를 사용합니다.
    deployment_mode = is_deployment_environment()
    folder = get_default_folder()
    if not deployment_mode:
        folder = st.text_input(
            "이미지 폴더 경로 (선택)",
            value=get_default_folder(),
            placeholder="예: ./images 또는 /Users/사용자명/Pictures/blog",
            help="로컬 PC의 이미지 폴더 경로를 입력할 수 있습니다.",
        )

    crop_ratio = st.selectbox("블로그 사진 크롭", ["원본 유지", "4:3 가로", "3:4 세로", "4:5 세로", "1:1 정사각형", "16:9 가로"], index=0,
                              help="원본 유지를 권장합니다. 비율을 선택하면 사진 중앙 기준으로 잘라 미리보기와 내보내기에 적용합니다. 가장자리의 피사체가 잘릴 수 있습니다.")
    if st.session_state.get("crop_ratio_prev", "원본 유지") != crop_ratio:
        st.session_state.pop("imgs", None)
        st.session_state.pop("analysis", None)
        st.session_state.crop_ratio_prev = crop_ratio
    if not deployment_mode and st.button("이미지 다시 읽기"):
        st.session_state.pop("imgs", None)
        st.session_state.pop("analysis", None)

uploaded_images = st.file_uploader(
    "사진 업로드 (GitHub/클라우드 배포 환경에서는 이 방식 사용)",
    type=["jpg", "jpeg", "png", "webp", "heic", "heif", "gif"],
    accept_multiple_files=True,
    help="Streamlit Community Cloud에서는 이 업로드 방식을 권장합니다. 사진은 분석·내보내기 전에 크기를 줄이며 GIF는 움직임을 유지합니다.",
)

st.subheader("1. 작성 문체")
style_mode = st.radio(
    "어떤 문체로 작성할까요?",
    ["내 문체 (기본)", "첨부 레퍼런스 문체"],
    horizontal=True,
    key="style_mode",
    help="기본은 제공해 주신 블로그 원고의 말투를 사용합니다. 레퍼런스 문체를 선택하면 첨부한 글의 문체를 우선합니다."
)
refs = st.file_uploader("레퍼런스 원문 PDF 또는 이미지 (레퍼런스 문체 선택 시 사용, 여러 개 가능)", type=["pdf", "png", "jpg", "jpeg", "webp"], accept_multiple_files=True)

if "style_profile" not in st.session_state:
    st.session_state.style_profile = ""
if "style_profile_editor" not in st.session_state:
    st.session_state.style_profile_editor = st.session_state.style_profile

if style_mode == "내 문체 (기본)":
    st.info("기본 문체가 선택되어 있습니다. 제공해 주신 글의 편한 구어체와 짧은 문단에 더해, 사진 중심의 긴 후기 구성·서론/본론/결론·이용팁/총평 분리·메뉴판 선배치를 기본값으로 사용합니다. 첨부한 레퍼런스는 이번 원고의 문체에 반영하지 않습니다.")
    with st.expander("내 기본 문체 프로필 보기"):
        st.text_area("기본 문체 프로필", value=DEFAULT_USER_STYLE_PROFILE, height=260, disabled=True, key="default_style_profile_view")
    active_style_profile = DEFAULT_USER_STYLE_PROFILE
    style_review_enabled = st.checkbox("원고 생성 후 내 문체 기준으로 검수·보정", value=True, key="style_review_enabled")
else:
    style_col1, style_col2 = st.columns([1, 2])
    with style_col1:
        if st.button("🧠 레퍼런스 문체 분석"):
            if not api_key:
                st.error(f"먼저 {key_label}을 입력해 주세요.")
            elif not refs:
                st.error("문체를 분석할 PDF 또는 이미지 레퍼런스를 첨부해 주세요.")
            else:
                client = make_client(provider, api_key)
                with st.status("레퍼런스 문체를 분석하는 중...", expanded=True) as status:
                    live = st.empty()
                    bind_status(live) if "bind_status" in globals() else None
                    profile = analyze_reference_style(client, model, build_ref_blocks(refs))
                    st.session_state.style_profile = profile
                    st.session_state.style_profile_editor = profile
                    live.empty()
                    status.update(label="문체 프로필 생성 완료", state="complete")
                st.rerun()
    with style_col2:
        st.caption("원문 PDF/이미지를 첨부하고 문체 분석을 누르세요. 분석 결과는 직접 수정할 수 있고, 이후 레퍼런스 문체 모드에서 재사용됩니다.")
    if st.button("레퍼런스 문체 프로필 비우기"):
        st.session_state.style_profile = ""
        st.session_state.style_profile_editor = ""
        st.rerun()
    with st.expander("레퍼런스 문체 프로필 보기 / 수정", expanded=bool(st.session_state.get("style_profile"))):
        style_profile_editor = st.text_area("문체 프로필 및 대표 문장 예시", key="style_profile_editor", height=260,
            placeholder="레퍼런스 문체 분석 버튼을 누르면 말투, 문장 리듬, 문단 구성, 대표 문장 예시가 여기에 생성됩니다. 직접 입력하거나 수정할 수도 있습니다.")
        st.session_state.style_profile = style_profile_editor
    active_style_profile = st.session_state.get("style_profile_editor", st.session_state.get("style_profile", ""))
    style_review_enabled = st.checkbox("원고 생성 후 레퍼런스 문체 기준으로 검수·보정 실행 (API 호출 추가)", value=True, key="style_review_enabled")

st.subheader("2. 방문 정보")
c1, c2, c3 = st.columns(3)
store = c1.text_input("업체명 (선택)")
region = c2.text_input("지역 (선택)", placeholder="예: 성수동")
keyword = c3.text_input("메인 키워드 (선택)", placeholder="예: 성수 파스타 맛집", help="제목에 넣을 핵심 문구를 입력하세요. 입력한 문구는 띄어쓰기까지 동일하게 본문에 5회 이상 반영합니다.")
seo1, seo2, seo3 = st.columns([2, 2, 2])
subkeywords = seo1.text_input("서브 키워드 (선택)", placeholder="예: 성수동 데이트, 성수 파스타")
content_type = seo2.selectbox("글 유형", ["자동 선택", "맛집", "카페", "피부과·뷰티", "방탈출·체험", "제품 리뷰", "여행·숙소", "기타"])
search_intent = seo3.selectbox("검색 의도", ["자동 선택", "방문 전 정보 탐색", "가격·메뉴 비교", "실제 방문 후기", "예약·이용 방법", "제품 구매 전 비교"])

party = st.radio("방문 인원", ["혼자", "2명", "3~4명", "5명 이상"], horizontal=True)
menu = st.text_area("주문 메뉴", placeholder="예: 트러플 크림 파스타, 감바스, 아이스 아메리카노 2잔")
reason = st.text_input("방문 계기", placeholder="예: 인스타에서 보고 가보고 싶었음 / 친구 생일")
ftc = st.radio("공정위 표시 유형", ["내돈내산", "기자단", "체험단"], horizontal=True)
provided = st.text_input("제공 내역 (기자단/체험단일 때)", placeholder="예: 2인 식사 제공") if ftc != "내돈내산" else ""
memo = st.text_area("추가로 기억나는 점", placeholder="예: 웨이팅 20분, 직원이 친절, 주차는 불편, 파스타 면이 쫄깃")

form = dict(store=store, region=region, keyword=keyword, subkeywords=subkeywords, content_type=content_type, search_intent=search_intent, party=party, menu=menu, reason=reason, ftc=ftc, provided=provided, memo=memo)


def make_client(provider_name, key):
    # 붙여넣기 시 앞뒤 공백/개행이 섞여 인증이 실패하는 경우를 방지합니다.
    return {"provider": provider_name, "api_key": (key or "").strip()}
def bind_status(live):
    global LOG, TICK, WAIT
    LOG = lambda m: st.write(m)
    TICK = lambda n: live.caption(f"✍️ 응답 수신 중... {n:,}자")
    WAIT = lambda sec: live.caption(f"⏳ 재시도 대기 중... {sec}초 남음")


upload_signature = tuple((f.name, len(f.getvalue())) for f in (uploaded_images or []))
if st.session_state.get("upload_signature") != upload_signature:
    st.session_state.pop("imgs", None)
    st.session_state.pop("analysis", None)
    st.session_state.upload_signature = upload_signature

if st.button("원고 생성", type="primary"):
    if not api_key:
        st.error(f"{key_label}을 입력해 주세요.")
        st.stop()
    if not menu.strip():
        st.error("주문 메뉴를 입력해 주세요.")
        st.stop()
    client = make_client(provider, api_key)
    t_start = time.time()

    with st.status("원고 생성 중... (사진이 많으면 몇 분 걸릴 수 있어요. 아래 로그가 계속 올라오면 정상 진행 중입니다)", expanded=True) as status:
        live = st.empty()
        bind_status(live)

        if "imgs" not in st.session_state:
            LOG("📂 이미지 폴더를 읽고 크기를 줄이는 중...")
            if uploaded_images:
                st.session_state.imgs = load_uploaded_images(uploaded_images, crop_ratio)
            else:
                st.session_state.imgs = load_images(folder, crop_ratio) if folder.strip() else []
            LOG(f"📂 사진 {len(st.session_state.imgs)}장 불러옴")
        imgs = st.session_state.imgs
        if not imgs:
            st.warning(f"이미지를 찾지 못했습니다: {folder or '설정된 폴더 없음'}  (이미지 없이 텍스트만 작성합니다)")

        if imgs:
            cached_count = len(st.session_state.get("analysis", {}))
            if cached_count:
                LOG(f"♻️ 저장된 분석 결과 {cached_count}장 재사용")
            try:
                st.session_state.analysis = analyze_images(client, model, imgs)
            except Exception as e:
                st.error(f"이미지 분석이 중단됐습니다. 성공한 분석 결과는 저장되어 다음 실행에서 이어집니다.\n\n{e}")
                st.stop()
        analysis = st.session_state.get("analysis", {})

        style_profile = active_style_profile
        reference_blocks = build_ref_blocks(refs) if style_mode == "첨부 레퍼런스 문체" and refs else []
        if style_mode == "첨부 레퍼런스 문체" and not reference_blocks and not style_profile.strip():
            st.error("레퍼런스 문체를 사용하려면 원문 파일을 첨부하거나 저장된 문체 프로필을 준비해 주세요.")
            st.stop()
        raw = generate(client, model, reference_blocks, form, analysis, style_profile=style_profile)
        if style_profile.strip() and style_review_enabled:
            raw = review_style(client, model, raw, style_profile)
        live.empty()
        st.session_state.result = parse(raw)
        st.session_state.form = form
        st.session_state.refs_blocks = reference_blocks
        st.session_state.style_mode_used = style_mode
        st.session_state.style_profile_used = style_profile
        status.update(label=f"완료! (총 {time.time() - t_start:.0f}초)", state="complete", expanded=False)

res = st.session_state.get("result")
if res:
    imgs_by_id = {im["id"]: im for im in st.session_state.get("imgs", [])}
    f = st.session_state.form
    st.divider()
    st.subheader("3. 결과")

    left, right = st.columns([3, 2])
    with left:
        render_preview(res, imgs_by_id, f)
    with right:
        st.markdown("**사용한 키워드**")
        st.write(res["keywords"] or "-")
        body_chars = len(re.sub(r"\s|\[\[IMG_\d+\]\]", "", res["body"]))
        st.metric("본문 글자 수(공백 제외)", f"{body_chars:,}")
        used = used_ids(res["body"])
        st.metric("배치된 사진", f"{len(used)} / {len(imgs_by_id)}")
        seo_report = audit_blog_seo(res, f, imgs_by_id)
        st.divider()
        st.markdown("**네이버 SEO 발행 전 점검**")
        st.metric("원고 점검 점수", f"{seo_report['score']} / 100", help="네이버의 공식 검색 순위 점수가 아니라, 프로젝트 내부의 원고 품질 체크 점수입니다.")
        st.caption(f"메인 키워드 정확 일치: {seo_report['keyword_count']}회 · 키워드 포함 문단: {seo_report['keyword_paragraphs']}개 · 제목: {seo_report['title_length']}자")
        with st.expander("SEO 점검 항목 자세히 보기", expanded=True):
            for label, ok, weight, detail in seo_report["checks"]:
                st.write(("✅ " if ok else "⚠️ ") + f"{label} ({weight}점) — {detail}")
        with st.expander("제목 후보 보기"):
            for title_candidate in suggest_title_patterns(f, res.get("title", "")):
                st.write("• " + title_candidate)
        skipped = [k for k in imgs_by_id if k not in used]
        if skipped:
            st.caption("미사용: " + ", ".join(skipped))
        with st.expander("이미지 분석 결과"):
            st.json(st.session_state.get("analysis", {}))

        st.download_button("📦 네이버 업로드용 ZIP", build_naver_zip(res, imgs_by_id, f), "네이버_업로드용.zip", "application/zip", help="미리보기에서 보이는 순서대로 원고의 사진 번호와 ZIP 안 사진 파일명을 맞춰 담습니다.")
        st.download_button("📦 원고+이미지 ZIP (전체)", build_zip(res, imgs_by_id, f), "blog_draft.zip", "application/zip")
        st.download_button("📄 텍스트(.txt)", build_text(res, imgs_by_id, f), "원고.txt")
        st.download_button("🌐 HTML(.html)", build_html(res, imgs_by_id, f), "원고.html", "text/html")
        st.warning("미리보기 사진을 복사해 붙여넣으면 네이버 에디터의 사진 편집창이 열리지 않을 수 있습니다. ZIP을 풀고 원고.txt를 붙여넣은 뒤, [사진 NN] 위치마다 네이버 에디터의 사진 첨부 버튼으로 해당 번호의 사진을 직접 넣어주세요.")
        st.caption("선택한 크롭 비율은 미리보기와 ZIP 사진에 적용됩니다. 크롭은 중앙 기준 자동 자르기이므로 가장자리의 사람·음식·간판 등이 잘릴 수 있어요. 중요한 피사체가 가장자리에 있으면 '원본 유지'를 선택하세요.")

    st.subheader("수정 요청")
    fb = st.text_input("예: 도입부를 더 짧게, 파스타 설명을 더 자세히, 아쉬운 점 추가")
    if st.button("수정해서 다시 쓰기") and fb:
        client = make_client(provider, api_key)
        with st.status("수정 중...", expanded=True) as status:
            live = st.empty()
            bind_status(live)
            style_profile = st.session_state.get("style_profile_used", DEFAULT_USER_STYLE_PROFILE)
            raw = generate(client, model, st.session_state.refs_blocks, f, st.session_state.get("analysis", {}), style_profile=style_profile, prev=res["raw"], feedback=fb)
            if style_profile.strip() and st.session_state.get("style_review_enabled", True):
                raw = review_style(client, model, raw, style_profile)
            live.empty()
            status.update(label="수정 완료", state="complete")
        st.session_state.result = parse(raw)
        st.rerun()
