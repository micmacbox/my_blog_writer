"""
네이버 블로그 후기 원고 작성기 (Gemini 무료 API 버전)
- 레퍼런스(PDF/이미지)의 문체를 참고
- 방문 정보 입력 + 이미지 폴더 분석
- 네이버 SEO 형식 원고 + 이미지 배치

실행:  streamlit run app_gemini.py
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import zipfile
import requests
try:
    import cv2
    import numpy as np
except Exception as exc:
    raise RuntimeError(
        "얼굴 모자이크에 필요한 OpenCV를 불러오지 못했습니다. "
        "requirements.txt 기준으로 패키지를 다시 설치한 뒤 앱을 재배포해 주세요."
    ) from exc

if not hasattr(cv2, "FaceDetectorYN"):
    raise RuntimeError(
        "잘못된 cv2 모듈이 로드되었습니다(FaceDetectorYN 없음). "
        "opencv-python-headless 설치 환경을 정리하고 앱을 재배포해 주세요."
    )
from datetime import datetime
from pathlib import Path

import time

from google import genai
from google.genai import types
import streamlit as st
from password_gate import require_password
from weekly_food.ui import render_weekly_food
from face_mosaic import FACE_PROCESSING_VERSION, mosaic_faces
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

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
PROVIDER_MODEL_OPTIONS = {
    "Gemini": ["gemma-4-31b-it", "gemini-3.1-flash-lite", "gemini-3.6-flash"],
    "Groq": [],
}
PROVIDER_URLS = {
    "Groq": "https://api.groq.com/openai/v1/chat/completions",
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}

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


IMAGE_SIGNATURE = "https://blog.naver.com/fizz_report"
def signature_font(size: int):
    """서명용 기본 산세리프 글꼴을 우선 사용합니다."""
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttf",
    )
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def add_image_signature(image: Image.Image) -> Image.Image:
    """이미지 오른쪽 아래에 작은 반투명 서명을 넣습니다."""
    image = image.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    font = signature_font(max(12, min(image.size) // 45))
    bbox = draw.textbbox((0, 0), IMAGE_SIGNATURE, font=font)
    pad = max(5, min(image.size) // 100)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = max(pad, image.width - width - pad * 2)
    y = max(pad, image.height - height - pad * 2)
    draw.rectangle((x - pad, y - pad, x + width + pad, y + height + pad), fill=(0, 0, 0, 125))
    draw.text((x - bbox[0], y - bbox[1]), IMAGE_SIGNATURE, font=font, fill=(255, 255, 255, 235))
    return image


def process_gif(src, max_side: int = 860, crop_ratio: str = "원본 유지") -> bytes:
    """애니메이션 GIF의 모든 프레임에 크롭/리사이즈를 적용하면서 움직임을 보존합니다."""
    im = Image.open(src)
    frames = []
    durations = []
    loop = im.info.get("loop", 0)
    try:
        for frame in ImageSequence.Iterator(im):
            fr = frame.convert("RGB")
            fr = crop_to_ratio(fr, crop_ratio)
            fr.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            frames.append(add_image_signature(mosaic_faces(fr.copy())))
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
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = crop_to_ratio(image, crop_ratio)
    image.thumbnail((760, 760), Image.Resampling.LANCZOS)
    image = add_image_signature(mosaic_faces(image))
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=78, optimize=True)
    return buf.getvalue(), "image/jpeg", ".jpg"


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
                image = Image.open(io.BytesIO(raw))
                image = ImageOps.exif_transpose(image).convert("RGB")
                image = crop_to_ratio(image, crop_ratio)
                image.thumbnail((760, 760), Image.Resampling.LANCZOS)
                image = add_image_signature(mosaic_faces(image))
                output_buf = io.BytesIO()
                image.convert("RGB").save(output_buf, "JPEG", quality=78, optimize=True)
                output = output_buf.getvalue()
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
    """Groq Chat Completions 호출. 텍스트 모델에 맞춰 content를 항상 문자열로 보낸다."""
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
                f"[첨부파일 생략: {mime}. 현재 선택된 Groq 모델은 텍스트 전용이므로 이미지/PDF 내용을 분석하지 않습니다.]"
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
            hint = "Groq 요청 형식 또는 모델 지원 문제입니다. 입력한 모델 ID가 Groq에서 제공되는지 확인해 주세요."
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
"usable": true/false  // 사진 자체가 심하게 흐리거나 원고에 쓸 수 없을 때만 false. 얼굴이 보인다는 이유로 false로 표시하지 않는다. 얼굴은 업로드 처리 단계에서 모자이크된다.
}"""


def analyze_images(client, model, imgs):
    """이미지를 작은 묶음으로 분석하고, 묶음마다 session_state에 즉시 저장한다."""
    results = dict(st.session_state.get("analysis", {}))
    if client.get("provider") == "Groq":
        # Groq 제공자는 텍스트 모델만 사용하므로 이미지 내용을 보내지 않는다.
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


DEFAULT_USER_STYLE_PROFILE = """[기본 문체: 사용자가 제공한 네이버 블로그 게시물 캡처 기반]

- 이번에 사용자가 정리해 준 문장 예시를 문체의 핵심 기준으로 삼는다. 이전의 '건조한 메모체' 해석보다 생동감 있고 친근한 실제 방문 후기체를 우선한다.
- 방문 계기와 검색한 이유, 방문 시간대·예약 여부·대기 상황처럼 제공된 정보를 도입에 자연스럽게 엮는다. 첫 문단은 독자가 방문 상황을 바로 떠올릴 수 있게 쓰고, 필요하면 가벼운 기대감이나 '리뷰 시작!' 같은 활기 있는 마무리를 더한다.
- 사진이 중심인 긴 글이며 외관·층별 안내·입구·내부·메뉴판·음식·먹는 장면 순으로 현장 경험을 따라간다. 사진 아래에는 보이는 장면을 짚고 개인 반응을 덧붙인다. 비슷한 음식 사진도 전체샷·근접샷·한입 장면에 맞춰 서로 다른 관찰을 쓴다.
- 메뉴 설명은 도우의 쫄깃함, 치즈와 소스의 조합, 허브 향처럼 구체적인 맛·향·온도·식감을 사용자가 준 경험과 이미지가 뒷받침하는 범위에서 생생하게 표현한다. 감각 표현은 구체적으로 쓰되 근거 없이 보태지 않는다.
- 친근한 해요체와 구어체를 중심으로 '~더라고요', '~했어요', '~죠' 등을 자연스럽게 섞고, 짧은 감탄이나 장난스러운 말끝·물결표·이모티콘은 어울리는 곳에 가끔 사용한다. 예시의 감탄 표현을 매번 그대로 반복하지 않는다.
- 서비스나 대기처럼 아쉬운 점도 실제 제공된 정보가 있으면 이유와 함께 솔직히 적고, 음식의 장점과 함께 균형 있게 정리한다. 불만을 숨기거나 반대로 과장하지 않는다.
- 캡처 이미지는 본문 글자가 작아 세밀하게 판독하기 어려우므로, 사용자가 직접 정리해 준 문장 예시와 화면에서 확인 가능한 구성만 참고한다. 읽히지 않는 문장이나 업체 정보를 추측해 따라 하지 않는다.

글쓴이의 목소리
- 직접 다녀온 사람이 방문 순서대로 사진을 넘기며 말을 건네듯 쓰는 1인칭 후기체다. 정돈된 안내문보다 현장감 있는 개인 경험을 살리고, 지나치게 건조하거나 보고서처럼 쓰지 않는다.
- 친근한 해요체·구어체를 중심으로 '~더라고요', '~했어요', '~죠'와 담백한 서술형을 자연스럽게 섞는다. 간결하고 정중한 문장이 필요한 이용팁·총평에서도 말투가 갑자기 딱딱해지지 않게 한다.
- 마침표는 생략하는 편이지만 문장마다 줄을 바꾼다. 물결표, 느낌표, '^^', 'ㅠㅠ' 같은 반응은 실제 상황과 감정이 입력에 있을 때 가끔 활용하고, 특정 유행 표현을 반복하지 않는다.
- 평가는 솔직하고 구체적으로 쓴다. 맛있으면 온도·식감·향·재료 조합 등 실제 느낀 점을 짚고, 서비스나 대기에서 아쉬움이 있었다면 사실에 근거해 장점과 함께 균형 있게 적는다.
- 사진을 보며 떠올린 짧은 농담이나 반응을 상황에 맞게 곁들여 생동감을 준다. 독자에게 말을 거는 표현과 가벼운 감탄도 과하지 않게 사용한다.
- 독자를 길게 부르지 않는다. 도입이나 총평에서 '~잖아요?', '~죠' 같은 짧은 공감 질문이나 권유를 한 번 정도 쓰는 정도다. '저는', '막상 가보니' 같은 1인칭 표현은 필요할 때만 가볍게 쓴다.
- 아쉬운 점이나 주의할 점은 입력 정보에 있을 때만 담담하게 한 줄로 말하고, 없으면 만들어내지 않는다. '무조건 추천'보다 어떤 상황의 사람에게 맞는지 구체적으로 권한다.

문장과 화면 리듬
- 한 줄에는 한 문장(또는 한 의미 덩어리)만 쓰고 바로 줄을 바꾼다. 한 줄은 대체로 15~45자이고, 길어지면 '~는데요/~라서/~지만' 뒤에서 끊어 다음 줄로 넘긴다. 쉼표로 이어 붙인 긴 문장은 쓰지 않는다.
- 사진 한 장 아래에는 대부분 1~3줄만 쓴다. 음식 사진은 첫 줄에 메뉴 이름을 짧게 쓰고 다음 줄에 먹은 반응을 붙이기도 한다. 사진이나 화제가 바뀔 때만 빈 줄을 두고, 한 문단에 여러 문장을 길게 붙이지 않는다.
- 주문 메뉴와 가격은 '✔메뉴명 가격'처럼 한 줄씩 짧게 나열한다.
- 도입은 3~5줄이다. 어떤 상황에서 왜 이곳을 찾았는지와 검색·예약·워크인·방문 시간 등 실제 정보를 먼저 말한다. 지역이나 방문 목적을 자연스럽게 엮고 '후기 시작!' 같은 활기 있는 마무리는 글 분위기에 맞을 때만 쓴다.
- 정보를 한 줄 던진 뒤 바로 짧은 반응을 붙인다. 정보만 나열하거나 반응만 이어 쓰지 않는다.
- 총평은 3~5줄이다. 맛과 서비스의 장단점을 솔직하게 함께 정리하고, 잘 맞을 방문자나 상황을 친근하게 권한다. 재방문 의사는 실제 판단이 입력된 경우에만 덧붙인다.
- 이용 팁은 한 줄에 정보 하나씩 편하게 알려준다. '~하면 편해요', '~가능하니 참고하세요'처럼 본문과 같은 친근한 말투를 유지한다.
- 소제목은 인용구처럼 짧고 눈에 띄는 말로 둔다. '메뉴판', '이용팁', '총평'처럼 필요한 정보 표지도 쓰되 매번 '서론/본론/결론'을 반복하지 않는다.
- 제목은 지역명이 있으면 대괄호 없이 '강남 훠궈 ...'처럼 지역명으로 시작하고, 검색 주제/메뉴·업체명·방문 후기 정보를 자연스럽게 잇는다. '[강남] 훠궈 ...'나 '[지역] ...'처럼 지역명을 대괄호로 감싸지 않는다. 지역 정보가 없으면 임의로 만들지 않는다.

원고 구성 순서
- 본문 도입/서론을 먼저 쓴 뒤 해시태그 한 줄을 배치하고, 바로 다음 줄에 [스티커: 내돈내산]을 둔다. 그 다음부터 사진과 경험을 이어간다.
- 이용 팁 제목은 반드시 정확히 '[인용구: 이용 팁]', 총평 제목은 반드시 정확히 '[인용구: 총평]'으로 쓴다. 대괄호 안 문구는 바꾸지 않는다.
- 본문 맨 아래에 [인용: 영업시간]을 쓰고, 바로 아래에 확인된 영업시간과 문의 전화번호를 배치한다. 예: '매일 10:30 - 22:00' 다음 줄에 '☎️문의 0507-1411-9922'. 확인되지 않은 값은 만들지 말고 '[확인 필요]'로 쓴다.
- 모든 본문 내용의 최하단에는 [지도: 업체명]을 둔다. 업체명은 입력된 업체명을 그대로 사용한다.

후기 전개와 정보
- 첫머리는 방문 계기나 첫인상을 짧고 편하게 꺼낸다. '오늘은 ~에 다녀왔습니다' 같은 정형적인 도입을 반복하지 말고, 사용자가 알려준 실제 상황에서 바로 시작한다.
- 대표 사진과 짧은 소개 뒤에 외관·찾아가는 길·내부 분위기·메뉴판·주문 메뉴·먹거나 이용한 과정·이용 팁·총평을 사진 흐름에 맞춰 배치한다. 업종에 맞지 않는 항목은 빼고, 모든 글을 똑같은 순서로 만들지 않는다.
- 메뉴판이나 가격표 사진이 있으면 주문 메뉴를 소개하기 전에 배치하고, 읽히는 메뉴명과 가격만 정확히 옮긴다. 음식·서비스의 맛, 질감, 변화, 양, 과정은 입력 정보나 이미지에서 뒷받침되는 만큼 구체적으로 쓴다.
- 맛집 콘텐츠의 사진 본론은 반드시 '전체적인 가게 내부 및 첫인상 → 메뉴판(실제 이미지가 있는 경우) → 본메뉴' 순서로 구성한다. 외관·입구 사진은 첫인상 구간에 포함한다. 메뉴판 사진들이 시작되는 첫 이미지 마커 바로 위에 '[스티커: 메뉴판]'을 한 번 넣는다. 메뉴판 이미지가 없으면 해당 구간과 메뉴판 스티커를 모두 생략한다. 본메뉴 뒤에 먹는 과정과 사이드·후식·마무리를 이어간다. 도입/해시태그/내돈내산 스티커와 하단 이용 팁·총평·영업정보·지도 구성은 이 사진 본론의 앞뒤에 유지한다.
- 사진 마커 앞뒤 문장은 사진에 보이는 내용과 이어지게 한다. 같은 연결 문구를 사진마다 반복하지 않는다. 사진 설명은 실제로 보이는 범위를 넘지 않는다.
- 예약, 주차, 웨이팅, 주문 방법, 운영시간처럼 재방문에 쓸 정보를 확인된 사실만 정리한다. 마지막 평가는 기억에 남은 점과 잘 맞을 방문자를 짧게 짚는다.
- 가격, 위치, 운영시간, 직원 응대, 개인 경험은 입력 자료에 없으면 만들어내지 않는다. 장식 스티커는 실제 파일이 있을 때만 배치한다.

피해야 할 것
- '이번 포스팅에서는', '전반적으로 만족스러운 경험이었습니다', '특별한 시간을 보낼 수 있었습니다', '완벽한 선택' 같은 전형적인 AI 문구.
- '~하였습니다', '~할 수 있었습니다'처럼 보고서에 가까운 문어체와, 한 줄이 길게 늘어지는 설명 문장.
- '입안 가득 퍼지는', '환상적인 조화' 같은 미사여구, 형용사를 겹쳐 쓰는 칭찬, 과장된 홍보체, 근거 없는 최상급.
- 문장마다 마침표를 찍는 것, 이모지나 감탄 부호를 여러 번 쓰는 것, 어색한 키워드 반복, 불필요하게 많은 소제목.
- 방문 동행자·상황·감정·맛·가격·웨이팅·서비스 경험을 지어내는 것.
- 캡처에 있는 문장이나 독특한 표현을 그대로 재사용하는 것.

작성 체크리스트
1. 첫 문단에서 실제 방문 계기나 목적이 드러나는가?
2. 한 줄에 한 문장씩 줄을 바꿨고, 사진 아래 코멘트가 1~3줄로 짧은가?
3. 사진 설명부터 도입·이용 팁·총평까지 말하듯 친근한 후기체가 이어지는가?
4. 짧은 문장도 각각 줄을 바꿨고, 감탄·물결표·이모티콘은 실제 상황에 맞게 가끔 사용했는가?
5. 좋은 점과 평범하거나 아쉬운 점을 사실에 맞게, 부풀리지 않고 적었는가?
6. 필요한 이용 정보와 누구에게 맞는지 담겼는가?
7. 사진 마커와 사진 속 사실을 정확히 보존했는가?
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
- 사용자가 직접 정리한 예시처럼 방문 상황과 사진을 따라가며 편하고 생생한 방문 후기를 살린다. 친근한 해요체·구어체를 중심으로 '~더라고요', '~했어요', '~죠' 등을 자연스럽게 섞고, 도입부터 이용 팁·총평까지 갑자기 정중한 안내문처럼 딱딱해지지 않게 한다. 종결어미를 규칙적으로 교대하거나 한 가지로 일괄 교정하지 않는다.
- 맛과 현장 반응은 구체적이고 솔직하게 표현한다. 실제로 입력된 감정과 경험에 맞는 짧은 감탄, 물결표, 이모티콘은 가끔 살리고, 과장이나 같은 반응의 반복만 다듬는다. 사진 아래 코멘트는 대체로 1~3줄로 유지하며, 형용사를 겹친 칭찬이나 미사여구는 고친다.
- 초안에 이미 자연스러운 구어체, 짧은 감상, 가벼운 여운이 있으면 유지한다. 문장을 과하게 다듬거나 안내문·보고서처럼 만들어 딱딱하게 바꾸지 않는다.
- 짧은 문장이어도 문장마다 줄을 바꾼다. 한 문장씩 줄을 나누고, 사진이나 화제가 바뀌거나 감상에 여운을 줄 때는 문단 사이에 빈 줄을 둔다.
- 실제 경험을 담은 가벼운 감탄이나 혼잣말은 어울릴 때 살리되, 새로운 경험이나 감정은 지어내지 않는다.
- 흔한 AI식 문구, 과도하게 매끈한 광고체, 불필요한 요약/총평을 추가하지 않는다.
- 사실, 메뉴, 가격, 주소, 방문 경험을 새로 만들거나 바꾸지 않는다.
- 제목/본문/태그/키워드 구분자와 모든 [[IMG_XX]] 이미지 마커를 정확히 보존한다. 마커를 삭제·중복·재배치하지 않는다.
- '[스티커: 메뉴판]'이 있으면 첫 메뉴판 이미지 바로 위 위치에 그대로 보존한다. 맛집의 내부·첫인상 → 메뉴판 → 본메뉴 구성도 문체 검수 중 변경하지 않는다.
- 초안의 내용과 길이를 불필요하게 바꾸지 않고 문체만 보정한다.
- 본문 구조를 도입/서론 → 해시태그 → [스티커: 내돈내산] → 나머지 본문과 사진 → '[인용구: 이용 팁]'과 확인된 이용 정보 → '[인용구: 총평]'과 총평 → [인용: 영업시간] → 영업시간 → 문의 번호 → [지도: 입력 업체명] 순서로 맞춘다. 두 인용구 제목의 문구는 반드시 고정한다. 해시태그는 ===TAGS===와 같게 유지한다.
- 영업시간이나 전화번호는 원고의 입력 정보·사진에 확인되는 값만 쓰고, 모르면 '[확인 필요]'로 표시한다. 지도 마커의 업체명은 사용자가 입력한 업체명을 그대로 쓴다.
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
- 직접 다녀온 사람이 사진을 넘기며 말을 건네듯 편하고 생생한 후기체로 쓴다. 방문 계기와 현장 반응, 음식 감상을 자연스럽게 살리고, 문어체 안내문이나 정리 보고서처럼 딱딱하게 쓰지 않는다.
- 사진 아래 코멘트는 친근한 해요체와 구어체를 중심으로 평서형을 자연스럽게 섞는다. 도입·이용 팁·총평도 예시처럼 편안하게 쓰고, 문어체로 딱딱하게 정리하지 않는다. 종결어미를 규칙적으로 교대하지 않는다.
- 문장 끝 마침표는 생략하는 편이며 짧은 문장도 문장마다 줄을 바꾼다. 물결표·감탄·이모티콘은 상황에 맞게 가끔 사용한다. 평가는 솔직하고 구체적으로 쓰며, 사진 아래 코멘트는 대체로 1~3줄로 쓴다.
- 짧은 문장도 문장마다 줄을 바꾼다. 한 문장씩 줄을 나누되, 사진이나 화제가 바뀌는 지점에는 빈 줄을 두어 읽기 편하게 한다.
- 정보는 분명히 전달하되 초안을 지나치게 매끈한 설명문으로 고치지 않는다. 사진을 보며 떠올린 짧은 반응과 감상은 사실에 맞게 살린다.
- 본문 도입/서론 직후에 해시태그 한 줄과 [스티커: 내돈내산]을 차례로 배치한다. 본문 태그는 ===TAGS===와 동일해야 한다.
- 이용 팁과 총평의 제목은 각각 '[인용구: 이용 팁]', '[인용구: 총평]'으로 고정한다. 대괄호 안 제목 문구를 임의로 요약하거나 바꾸지 않는다. 영업정보 부분은 [인용: 영업시간] 다음 줄에 확인된 영업시간과 문의 번호를 배치하고, 알 수 없는 값은 '[확인 필요]'라고 쓴다.
- 원고의 마지막 줄은 [지도: 입력된 업체명]이다. 업체명은 방문 정보에 입력된 값을 그대로 쓴다.
- 레퍼런스의 문장·내용을 그대로 복사하지 않는다. 스타일만 참고한다.

[네이버 SEO]
- 제목은 25~40자 안팎으로 작성하고, 사용자가 입력한 메인 키워드를 제목 앞쪽에 정확히 포함한다. 과장·낚시 금지.
- 메인 키워드가 입력되면 그 문구를 띄어쓰기·철자·순서까지 한 글자도 바꾸지 않는 '정확 일치 키워드'로 취급한다.
- 제목에 사용한 핵심 키워드 문구를 본문에 최소 5회 이상 정확히 동일한 형태로 노출한다. 첫 문단에 반드시 1회 넣고, 본문 중간과 후반에 문맥에 맞게 분산한다. 조사나 수식어를 붙이는 것은 가능하지만 키워드 자체의 띄어쓰기와 철자는 변경하지 않는다.
- 본문 작성 후 제목의 키워드 문구가 본문에 정확히 몇 회 등장하는지 자체 점검한다. 5회 미만이면 자연스러운 문장에 추가해 기준을 충족한다. 반복을 위해 어색한 문장을 만들거나 키워드만 나열하지 않는다.
- 제목에 지역명을 넣을 때 대괄호를 붙이지 않는다. 예: '[강남] 훠궈 무한리필 방문 후기'가 아니라 '강남 훠궈 무한리필 방문 후기'.
- 메인 키워드가 비어 있으면 업체명·지역·업종/메뉴에서 대표 키워드를 정하고, 제목에 넣은 정확한 문구를 본문에 최소 5회 동일하게 사용한다.
- 본문은 사진 수와 입력 정보에 맞춰 충분히 작성하되, 글자 수를 채우려고 같은 말을 반복하지 않는다.
- 네이버 에디터에 바로 붙여넣을 수 있도록 마크다운 제목 기호(#, **)를 사용하지 않는다. 소제목은 짧고 명확하게 사용한다.
- '서론 → 본론 → 결론'을 본문에 기계적으로 반복하지 않는다. 필요한 곳에 짧고 자연스러운 소제목을 두고 사진과 경험을 따라 전개한다. 이용 팁과 총평은 각각 '[인용구: 이용 팁]', '[인용구: 총평]'으로 고정해 쓴다.
- 최상급·단정 표현("무조건", "인생 맛집", 효능 주장)은 피하고 경험 기반으로 서술한다.
- 해시태그 8~12개를 생성해 본문 도입/서론 바로 뒤에 한 줄로 배치하고, ===TAGS===에도 동일하게 출력한다. 최종 출력 조립 과정에서 태그를 본문 끝에 다시 덧붙이지 않는다.
- 메인 키워드와 서브 키워드는 검색 의도에 맞게 사용하되, 키워드 목록을 나열하지 말고 실제 정보가 담긴 문장에 포함한다.
- 서브 키워드는 문맥에 맞을 때만 사용하고 억지로 모두 넣지 않는다. 동일 키워드를 한 문단에 몰아넣지 않는다.
- 사용자가 선택한 글 유형/검색 의도를 우선해 검색자가 궁금해할 정보(위치·가격·메뉴·과정·장단점·추천 대상)를 빠뜨리지 않는다.
- 확인되지 않은 검색량, 인기 순위, 방문자 수, 영업 정보, 주차 가능 여부를 추정하지 않는다. 검색 상위 노출이나 순위를 보장하는 표현을 쓰지 않는다.

[이미지 배치]
- 사진은 [[IMG_01]] 처럼 마커를 단독 한 줄로 넣는다. 사진 분석표의 모든 사진을 각각 정확히 한 번씩 사용한다. 얼굴이 보이는 사진도 얼굴 모자이크 후 사용할 수 있으므로 얼굴이 있다는 이유로 제외하지 않는다. 사진 자체가 심하게 흐리거나 원고에 쓸 수 없는 경우만 제외한다.
- 맛집 콘텐츠의 사진 본론 순서는 반드시 '전체적인 가게 내부 및 첫인상 → 메뉴판 이미지(있는 경우) → 본메뉴'로 맞춘다. 외관·입구는 첫인상 구간에 함께 둔다. 메뉴판 이미지들은 한 구간으로 모으고 첫 메뉴판 이미지 마커 바로 위에 '[스티커: 메뉴판]'을 정확히 한 번 넣는다. 메뉴판 이미지가 없으면 메뉴판 구간과 스티커는 만들지 않는다. 본메뉴 이후 먹는 과정·사이드·후식·마무리를 이어간다. 이 규칙은 맛집으로 지정했거나 업체/입력 내용상 음식점 후기인 경우 적용하며 다른 유형에 강제하지 않는다.
- 같은 종류 사진은 원칙적으로 연속 3장까지만 배치하되, 맛집의 메뉴판 사진은 음식 사진 사이로 흩어놓지 않고 메뉴판 구간에 모은다. 사진 직후에는 그 사진 내용과 맞는 1~3문장을 쓴다. 사진 설명에 없는 맛·재료·가격을 지어내지 않는다.
- 메뉴판 사진을 배치한 뒤에 메뉴명과 가격을 소개하고, 음식 사진에서는 비주얼·양·식감·맛 등 제공된 메모나 사진으로 뒷받침되는 부분만 서술한다.

[사실 관계]
- 주소·영업시간·전화·가격은 입력 정보나 사진에서 확인되는 것만 쓴다. 없으면 해당 항목은 "[확인 필요]"로 표시한다.
- 방문 인원에 맞게 서술하되(혼자 / 2명 / 3명 / 4명 / 5명 이상) 동행자의 이름·관계를 지어내지 않는다. 사용자가 준 '기억나는 점'은 반드시 반영한다.
- 공정위·협찬·기자단·체험단 표시 문구를 자동으로 만들거나 삽입하지 않는다.

[출력 형식 — 반드시 아래 구분자 사용]
===TITLE===
(제목)
===BODY===
(도입/서론)
#태그1 #태그2 ...
[스티커: 내돈내산]
(나머지 경험과 사진 마커)
[인용구: 이용 팁]
(확인된 실용 정보)
[인용구: 총평]
(솔직한 최종 감상과 잘 맞는 방문자)
[인용: 영업시간]
(확인된 영업시간)
☎️문의 (확인된 전화번호)
[지도: 입력된 업체명]
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
    section_ok = all(x in body for x in ["이용팁", "총평"])
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
        ("이용팁·총평 포함", section_ok, 15, "실용 정보와 최종 감상 구분 확인"),
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
- 추가로 기억나는 점: {form['memo'] or '(없음)'}

[사진 분석표 — 촬영 순서대로]
{json.dumps(list(usable.values()), ensure_ascii=False, indent=1)}"""
    content = list(ref_blocks)
    intro = "위 첨부는 문체를 참고할 레퍼런스 원문이다.\n\n" if ref_blocks else "(레퍼런스 없음 — 자연스러운 네이버 후기체로 작성)\n\n"
    profile_text = f"[사용자 지정 문체 프로필 — 반드시 우선 반영]\n{style_profile}\n\n" if style_profile and style_profile.strip() else "[문체 프로필 없음 — 첨부 레퍼런스의 문체를 직접 분석해 반영]\n\n"
    content.append(profile_text + intro + info + "\n\n위 정보로 원고를 작성해라. 딱딱한 안내문이나 요약 보고서처럼 쓰지 말고, 사용자가 제공한 예시처럼 방문 상황과 사진을 따라가며 말하듯 친근하고 생생한 후기체로 작성해라. 사진 아래는 대체로 1~3줄로 쓰고 짧은 문장도 문장마다 줄을 바꿔라. 마침표는 생략하는 편으로 하되 자연스러운 해요체·구어체, 실제 감정에 맞는 가벼운 감탄을 활용해라. 맛과 서비스의 장단점을 제공된 사실에 맞게 구체적으로 담아라.")
    if prev and feedback:
        content.append(f"[이전 초안]\n{prev}\n\n[수정 요청]\n{feedback}\n\n수정 요청을 반영해 전체 원고를 같은 형식으로 다시 출력해라.")
    return ask(client, model, WRITE_SYSTEM, content, label="원고 작성")


def parse(text: str):
    def sec(name):
        m = re.search(rf"==={name}===\s*(.*?)(?====[A-Z]+===|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    title = sec("TITLE")
    body = sec("BODY")
    # 생성 모델이 제목을 본문 첫 줄에 중복 출력한 경우 본문에서는 제거합니다.
    if title and body.splitlines() and body.splitlines()[0].strip() == title.strip():
        body = "\n".join(body.splitlines()[1:]).lstrip()
    frame_quote = f"[프레임 인용구: {title}]" if title else ""
    if frame_quote and not body.startswith(frame_quote):
        body = f"{frame_quote}\n\n{body}".strip()
    return {"title": title, "body": body, "tags": sec("TAGS"), "keywords": sec("KEYWORDS"), "raw": text}


# ──────────────────────────── 렌더링 / 내보내기 ────────────────────────────

MARK = re.compile(r"^\s*\[\[(IMG_\d+)\]\]\s*$")
THUMBNAIL_WHITE_OVERLAY_OPACITY = 0.30


def used_ids(body):
    return [m.group(1) for line in body.splitlines() if (m := MARK.match(line))]


def render_preview(res, imgs_by_id, form):
    st.markdown(f"### {res['title']}")
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            st.image(imgs_by_id[m.group(1)]["out"])
        elif caption := re.fullmatch(r"\[이미지 캡션:\s*(.*?)\s*\]", line.strip()):
            st.caption(caption.group(1))
        elif line.strip():
            st.markdown(line.replace("~", r"\~"))


def make_white_overlay(image_bytes: bytes, opacity: float = THUMBNAIL_WHITE_OVERLAY_OPACITY) -> bytes:
    """이미지를 600×500으로 맞춘 뒤 지정한 투명도의 흰색 오버레이를 적용합니다."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        if getattr(source, "is_animated", False):
            source.seek(0)
        base = ImageOps.fit(source.convert("RGB"), (600, 500), method=Image.Resampling.LANCZOS).convert("RGBA")

    overlay = Image.new("RGBA", base.size, (255, 255, 255, round(255 * opacity)))
    canvas = Image.alpha_composite(base, overlay)
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def make_thumbnail(image_bytes: bytes, title: str) -> bytes:
    """사진에 흰색 70% 오버레이와 제목을 얹은 600×500 PNG를 만듭니다."""
    overlay_bytes = make_white_overlay(image_bytes, THUMBNAIL_WHITE_OVERLAY_OPACITY)
    with Image.open(io.BytesIO(overlay_bytes)) as image:
        canvas = image.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    font_paths = (
        Path(__file__).resolve().parent / "assets" / "Pretendard-Black.otf",
        Path(__file__).resolve().parent / "assets" / "NotoSansKR-VF.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    )
    font_path = next((path for path in font_paths if Path(path).is_file()), None)
    if font_path is None:
        raise RuntimeError("한글 글꼴을 찾을 수 없습니다. Noto Sans CJK 또는 나눔고딕 글꼴을 설치해 주세요.")

    def load_font(size):
        font = ImageFont.truetype(font_path, size=size)
        try:
            axes = font.get_variation_axes()
            if axes:
                font.set_variation_by_axes([700] + [axis["default"] for axis in axes[1:]])
        except (AttributeError, OSError, ValueError):
            pass
        return font

    def wrap_text(text, font, max_width):
        lines = []
        for paragraph in str(text).splitlines() or [str(text)]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            line = ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] <= max_width:
                    line = candidate
                    continue
                if line:
                    lines.append(line)
                # 공백 없는 긴 단어만 폭을 넘지 않게 글자 단위로 나눕니다.
                line = ""
                for char in word:
                    candidate = line + char
                    if line and draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] > max_width:
                        lines.append(line)
                        line = char
                    else:
                        line = candidate
            if line:
                lines.append(line)
        return lines or [""]

    title = str(title or "").strip()
    if not title:
        raise ValueError("블로그 제목이 비어 있어 썸네일 문구를 만들 수 없습니다.")
    max_width, max_height = 540, 330
    for size in range(54, 9, -2):
        font = load_font(size)
        lines = wrap_text(title, font, max_width)
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=3) for line in lines]
        heights = [box[3] - box[1] for box in boxes]
        gap = max(4, size // 7)
        total_height = sum(heights) + gap * (len(lines) - 1)
        if len(lines) <= 3 and total_height <= max_height and all((box[2] - box[0]) <= max_width for box in boxes):
            break
    else:
        font = load_font(10)
        lines = wrap_text(title, font, max_width)
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
        heights = [box[3] - box[1] for box in boxes]
        gap = 4
        total_height = sum(heights) + gap * (len(lines) - 1)
        if len(lines) > 3:
            raise ValueError("제목이 너무 길어 썸네일 문구를 3줄 안에 표시할 수 없습니다. 제목을 줄여 주세요.")

    y = (500 - total_height) / 2
    for line, box, height in zip(lines, boxes, heights):
        width = box[2] - box[0]
        draw.text(((600 - width) / 2 - box[0], y - box[1]), line, font=font,
                  fill=(255, 255, 255, 255), stroke_width=max(2, size // 14),
                  stroke_fill=(20, 20, 20, 255))
        y += height + gap

    # 예시의 크리에이터 라벨은 왼쪽 위로 옮기고, 서명은 모든 이미지와 같이 오른쪽 아래에 둡니다.
    label_font = load_font(20)
    label = "@치와바타"
    label_box = draw.textbbox((0, 0), label, font=label_font)
    label_pad_x, label_pad_y = 12, 7
    label_w, label_h = label_box[2] - label_box[0], label_box[3] - label_box[1]
    draw.rectangle((0, 0, label_w + label_pad_x * 2, label_h + label_pad_y * 2), fill=(255, 194, 45, 255))
    draw.text((label_pad_x - label_box[0], label_pad_y - label_box[1]), label,
              font=label_font, fill=(255, 255, 255, 255))
    canvas = add_image_signature(canvas)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def seo_image_filename(seq, image, form):
    """Use the main keyword and analyzed scene in a short, descriptive export name."""
    def compact(value, limit):
        value = re.sub(r"[^가-힣A-Za-z0-9]+", "", str(value or ""))
        return value[:limit]

    keyword = compact(form.get("keyword") or form.get("menu") or form.get("store") or "사진", 32)
    category = image.get("category", "")
    scene_by_category = {
        "외관": "외관", "입구": "입구", "내부": "내부", "메뉴판": "메뉴판",
        "음식": "클로즈업", "음료": "음료", "사이드": "사이드메뉴",
        "테이블세팅": "한상차림", "먹는장면": "먹는장면", "영수증": "영수증",
    }
    scene = scene_by_category.get(category, "사진")
    menu_guess = compact(image.get("menu_guess"), 16)
    if menu_guess and menu_guess not in keyword:
        scene = f"{menu_guess}{scene}"
    return f"{seq:02d}_{keyword}{scene}{image.get('out_ext', '.jpg')}"


def placement_items(res, imgs_by_id, form):
    """미리보기 본문에 실제로 등장하는 순서대로 사진 번호와 파일명을 만든다."""
    items = []
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            im = imgs_by_id[m.group(1)]
            seq = len(items) + 1
            filename = seo_image_filename(seq, im, form)
            items.append({"seq": seq, "iid": m.group(1), "image": im, "filename": filename})
    return items


def build_text(res, imgs_by_id, form):
    out = [res["title"], ""]
    items = iter(placement_items(res, imgs_by_id, form))
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m:
            if m.group(1) in imgs_by_id:
                item = next(items)
                out.append(f"[사진 {item['seq']:02d} : {item['filename']}]")
            else:
                out.append(f"[사진 : 원본 이미지 확인 필요 - {m.group(1)}]")
        else:
            out.append(line)
    return "\n".join(out)


def build_html(res, imgs_by_id, form):
    parts = [f"<h2>{html.escape(res['title'])}</h2>"]
    for line in res["body"].splitlines():
        m = MARK.match(line)
        if m and m.group(1) in imgs_by_id:
            im = imgs_by_id[m.group(1)]
            b64 = base64.b64encode(im["out"]).decode()
            mime = im.get("out_mime", "image/jpeg")
            parts.append(f'<p style="text-align:center"><img src="data:{mime};base64,{b64}" style="max-width:100%"></p>')
        elif line.strip():
            parts.append(f"<p>{html.escape(line)}</p>")
    return "<html><head><meta charset='utf-8'></head><body style='max-width:860px;margin:auto;line-height:1.8'>" + "\n".join(parts) + "</body></html>"


def build_zip(res, imgs_by_id, form):
    """원고의 사진 번호와 ZIP 안 이미지 파일명이 미리보기 순서로 일치하도록 저장합니다."""
    buf = io.BytesIO()
    items = placement_items(res, imgs_by_id, form)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("원고.txt", build_text(res, imgs_by_id, form))
        z.writestr("원고.html", build_html(res, imgs_by_id, form))
        for item in items:
            z.writestr(f"사진/{item['filename']}", item["image"]["out"])
        add_generation_thumbnail(z)
    return buf.getvalue()


def build_naver_zip(res, imgs_by_id, form):
    """미리보기 순서대로 사진과 원고를 묶습니다. 별도 배치순서표는 만들지 않습니다."""
    buf = io.BytesIO()
    items = placement_items(res, imgs_by_id, form)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("원고.txt", build_text(res, imgs_by_id, form))
        for item in items:
            z.writestr(f"사진/{item['filename']}", item["image"]["out"])
        add_generation_thumbnail(z)
    return buf.getvalue()


def add_generation_thumbnail(archive):
    artifacts = st.session_state.get("generation_thumbnail_artifacts") or {}
    for filename, content in artifacts.items():
        archive.writestr(f"사진/{filename}", content)


def attach_generation_thumbnail_marker(result, store_name=""):
    artifacts = st.session_state.get("generation_thumbnail_artifacts") or {}
    thumbnail_filename = next((name for name in artifacts if name.endswith("_썸네일.jpg")), None)
    if thumbnail_filename:
        marker = f"[사진 썸네일 : {thumbnail_filename}]"
        business_name = (store_name or "").strip() or "업체명"
        caption_marker = f"[이미지 캡션: {business_name} 메인이미지]"
        lines = [
            line for line in result["body"].splitlines()
            if line.strip() != marker
            and not re.fullmatch(r"\[이미지 캡션: .* 메인이미지\]", line.strip())
        ]
        frame_index = next((i for i, line in enumerate(lines) if line.strip().startswith("[프레임 인용구:")), None)
        if frame_index is None:
            lines[0:0] = [marker, caption_marker]
        else:
            lines[frame_index + 1:frame_index + 1] = [marker, caption_marker]
        result["body"] = "\n".join(lines).strip()
    return result


def make_client(provider_name, key):
    # 붙여넣기 시 앞뒤 공백/개행이 섞여 인증이 실패하는 경우를 방지합니다.
    return {"provider": provider_name, "api_key": (key or "").strip()}


# ──────────────────────────── UI ────────────────────────────

st.set_page_config(page_title="네이버 블로그 원고 작성기", page_icon="✍️", layout="wide")
require_password()
st.title("✍️ 네이버 블로그 원고 작성기")
review_tab, weekly_tab = st.tabs(["방문 후기 작성", "주간 푸드 트렌드"])

with weekly_tab:
    render_weekly_food()

with review_tab:
    if st.session_state.get("face_processing_version") != FACE_PROCESSING_VERSION:
        st.session_state.pop("imgs", None)
        st.session_state["face_processing_version"] = FACE_PROCESSING_VERSION
    st.subheader("✍️ 방문 후기 원고 작성기")

    with st.expander("방문 후기 작성 설정", expanded=True):
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
        model_choice = st.selectbox(
            "모델",
            [*PROVIDER_MODEL_OPTIONS[provider], "직접 입력"],
            index=(PROVIDER_MODEL_OPTIONS[provider].index(DEFAULT_MODEL)
                   if provider == "Gemini" and DEFAULT_MODEL in PROVIDER_MODEL_OPTIONS[provider]
                   else len(PROVIDER_MODEL_OPTIONS[provider])),
            key=f"model_choice_{provider}",
        )
        if model_choice == "직접 입력":
            model = st.text_input(
                "모델 ID 직접 입력",
                value=st.session_state.get(f"custom_model_{provider}", ""),
                key=f"custom_model_{provider}",
                placeholder="예: gemini-3.1-flash-lite",
            ).strip()
        else:
            model = model_choice
        if provider == "Groq":
            st.caption("Groq 모델은 텍스트 전용으로 처리합니다. 사진은 ZIP에 포함되지만 사진 내용의 AI 분석은 생략됩니다. 이미지 분석은 Gemini를 선택해 주세요.")
            st.caption("Groq Console에서 발급한 Groq 전용 API 키를 입력하세요. 다른 AI 제공자/Gemini 키는 사용할 수 없습니다.")
            st.markdown("[Groq API 키 발급/관리 페이지 열기](https://console.groq.com/keys)")
            if st.button("Groq API 키 연결 테스트", key="test_groq_api_key"):
                if not api_key:
                    st.error("Groq API 키를 먼저 입력해 주세요.")
                elif not model:
                    st.error("Groq에서 제공하는 모델 ID를 입력해 주세요.")
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

    def mark_image_upload_changed():
        st.session_state.image_upload_changed = True


    uploaded_images = st.file_uploader(
        "사진 업로드",
        type="image",
        accept_multiple_files=True,
        key="blog_image_uploader",
        on_change=mark_image_upload_changed,
        help="Android 갤러리에서 사진을 여러 장 선택하고 완료를 누르세요. 첨부된 파일명은 아래 목록에 표시됩니다.",
    )
    uploaded_images = list(uploaded_images or [])
    image_upload_changed = st.session_state.pop("image_upload_changed", False)
    if image_upload_changed or "upload_signature" not in st.session_state:
        with st.spinner("첨부된 사진을 불러오는 중..."):
            upload_signature = tuple(
                (f.name, len(raw := f.getvalue()), hashlib.sha256(raw).hexdigest())
                for f in uploaded_images
            )
    else:
        upload_signature = st.session_state.upload_signature
    if st.session_state.get("upload_signature") != upload_signature:
        st.session_state.pop("imgs", None)
        st.session_state.pop("analysis", None)
        st.session_state.upload_signature = upload_signature

    if uploaded_images:
        st.caption(f"선택된 사진: {len(uploaded_images)}장")
        for image_index, uploaded_image in enumerate(uploaded_images, 1):
            st.text(f"{image_index:02d}. {uploaded_image.name}")

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
        st.info("제공해 주신 네이버 블로그 캡처의 편한 후기체와 사진 중심의 모바일 문단 리듬을 기본 문체로 사용합니다. 외관·내부·메뉴·먹는 장면을 사진 흐름에 맞춰 소개하고, 사진마다 짧고 겹치지 않는 관찰과 감상을 붙입니다.")
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
                elif not model:
                    st.error("사용할 모델 ID를 입력해 주세요.")
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

    party = st.radio("방문 인원", ["혼자", "2명", "3명", "4명", "5명 이상"], horizontal=True)
    menu = st.text_area("주문 메뉴", placeholder="예: 트러플 크림 파스타, 감바스, 아이스 아메리카노 2잔")
    reason = st.text_input("방문 계기", placeholder="예: 인스타에서 보고 가보고 싶었음 / 친구 생일")
    memo = st.text_area("추가로 기억나는 점", placeholder="예: 웨이팅 20분, 직원이 친절, 주차는 불편, 파스타 면이 쫄깃")

    form = dict(store=store, region=region, keyword=keyword, subkeywords=subkeywords, content_type=content_type, search_intent=search_intent, party=party, menu=menu, reason=reason, memo=memo)

    st.subheader("원고 생성용 썸네일")
    st.caption("원고에 넣을 썸네일 문구와 이미지를 생성 전에 지정합니다. 결과 썸네일과 흰색 오버레이 배경은 원고 ZIP에 함께 담깁니다.")
    st.session_state.setdefault("generation_thumbnail_title", "")
    generation_thumbnail_title = st.text_input(
        "썸네일 문구",
        key="generation_thumbnail_title",
        placeholder="비워 두면 자동 생성된 원고 제목 사용",
        help="직접 입력하면 입력한 문구를 사용하고, 비워 두면 자동 생성된 원고 제목과 똑같이 만듭니다.",
    )
    generation_thumbnail_upload = st.file_uploader(
        "원고용 썸네일 이미지",
        type=["png", "jpg", "jpeg", "webp", "gif", "heic", "heif"],
        accept_multiple_files=False,
        key="generation_thumbnail_upload",
        help="업로드하지 않으면 원고 사진 중 선택한 이미지를 사용합니다.",
    )
    generation_thumbnail_images = st.session_state.get("imgs", [])
    generation_thumbnail_options = [im["id"] for im in generation_thumbnail_images]
    generation_thumbnail_id = None
    if generation_thumbnail_options and generation_thumbnail_upload is None:
        generation_thumbnail_id = st.selectbox(
            "또는 원고 사진에서 선택",
            generation_thumbnail_options,
            format_func=lambda image_id: next((im["name"] for im in generation_thumbnail_images if im["id"] == image_id), image_id),
            key="generation_thumbnail_image_id",
        )


    def make_client(provider_name, key):
        # 붙여넣기 시 앞뒤 공백/개행이 섞여 인증이 실패하는 경우를 방지합니다.
        return {"provider": provider_name, "api_key": (key or "").strip()}
    def bind_status(live):
        global LOG, TICK, WAIT
        LOG = lambda m: st.write(m)
        TICK = lambda n: live.caption(f"✍️ 응답 수신 중... {n:,}자")
        WAIT = lambda sec: live.caption(f"⏳ 재시도 대기 중... {sec}초 남음")


    if st.button("원고 생성", type="primary"):
        if not api_key:
            st.error(f"{key_label}을 입력해 주세요.")
            st.stop()
        if not model:
            st.error("사용할 모델 ID를 입력해 주세요.")
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
            result = parse(raw)
            thumbnail_source = None
            if generation_thumbnail_upload:
                thumbnail_source = generation_thumbnail_upload.getvalue()
            elif generation_thumbnail_id:
                thumbnail_source = next((im["out"] for im in generation_thumbnail_images if im["id"] == generation_thumbnail_id), None)
            if thumbnail_source:
                try:
                    thumb_title = generation_thumbnail_title.strip() or result["title"]
                    white_background = make_white_overlay(thumbnail_source, THUMBNAIL_WHITE_OVERLAY_OPACITY)
                    thumbnail_png = make_thumbnail(thumbnail_source, thumb_title)
                    # Naver marker uses .jpg; encode actual JPEG bytes to match that extension.
                    with Image.open(io.BytesIO(thumbnail_png)) as thumb_image:
                        jpeg_buffer = io.BytesIO()
                        thumb_image.convert("RGB").save(jpeg_buffer, format="JPEG", quality=94, optimize=True)
                    filename_base = re.sub(r"[^가-힣A-Za-z0-9]+", "", keyword or store or region or "블로그")[:32] or "블로그"
                    thumbnail_filename = f"{filename_base}_썸네일.jpg"
                    background_filename = f"{filename_base}_썸네일_흰배경30.png"
                    st.session_state.generation_thumbnail_artifacts = {
                        thumbnail_filename: jpeg_buffer.getvalue(),
                        background_filename: white_background,
                    }
                    attach_generation_thumbnail_marker(result, form.get("store", ""))
                except (OSError, ValueError, RuntimeError) as exc:
                    st.session_state.pop("generation_thumbnail_artifacts", None)
                    st.error(f"원고용 썸네일을 만들지 못했습니다: {exc}")
            else:
                st.session_state.pop("generation_thumbnail_artifacts", None)
            st.session_state.result = result
            st.session_state.form = form
            st.session_state.refs_blocks = reference_blocks
            st.session_state.style_mode_used = style_mode
            st.session_state.style_profile_used = style_profile
            status.update(label=f"완료! (총 {time.time() - t_start:.0f}초)", state="complete", expanded=False)

    res = st.session_state.get("result")
    st.divider()
    st.subheader("썸네일 만들기")
    st.caption("원고 생성과 별도로 썸네일을 만들 수 있습니다. 블로그 제목과 배경 이미지를 지정하세요.")
    thumbnail_title = st.text_input(
        "블로그 제목 (썸네일 문구)",
        value=res.get("title", "") if res else "",
        key="standalone_thumbnail_title",
    )
    thumbnail_upload = st.file_uploader(
        "썸네일용 이미지 업로드",
        type=["png", "jpg", "jpeg", "webp", "gif", "heic", "heif"],
        accept_multiple_files=False,
        key="thumbnail_background_upload",
        help="이미지를 업로드하면 썸네일과 흰색 오버레이 배경 이미지를 만듭니다.",
    )
    thumbnail_images = st.session_state.get("imgs", [])
    thumbnail_image_id = None
    if thumbnail_images and thumbnail_upload is None:
        thumbnail_image_ids = [im["id"] for im in thumbnail_images]
        thumbnail_image_id = st.selectbox(
            "또는 원고 첨부 사진에서 선택",
            thumbnail_image_ids,
            format_func=lambda image_id: next((im["name"] for im in thumbnail_images if im["id"] == image_id), image_id),
            key="thumbnail_image_id",
        )
    if thumbnail_upload or thumbnail_image_id:
        try:
            if thumbnail_upload:
                thumbnail_source = thumbnail_upload.getvalue()
            else:
                thumbnail_source = next(im["out"] for im in thumbnail_images if im["id"] == thumbnail_image_id)
            white_overlay = make_white_overlay(thumbnail_source, THUMBNAIL_WHITE_OVERLAY_OPACITY)
            thumbnail = make_thumbnail(thumbnail_source, thumbnail_title)
            st.image(thumbnail, caption="600×500 · Pretendard Black · 흰색 오버레이 30%")
            st.download_button("⬜ 흰색 오버레이 이미지 다운로드", white_overlay, "썸네일_배경_흰색30.png", "image/png", key="thumbnail_overlay_download")
            st.download_button("🖼️ 썸네일 PNG 다운로드", thumbnail, "블로그_썸네일.png", "image/png", key="thumbnail_download")
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(f"썸네일을 만들지 못했습니다: {exc}")
    else:
        st.caption("이미지를 업로드하거나 원고 첨부 사진을 선택하면 썸네일이 생성됩니다.")

    if res:
        analysis_by_id = st.session_state.get("analysis", {})
        imgs_by_id = {
            im["id"]: {**im, **analysis_by_id.get(im["id"], {})}
            for im in st.session_state.get("imgs", [])
        }
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

            st.download_button("📦 네이버 업로드용 ZIP", build_naver_zip(res, imgs_by_id, f), "네이버_업로드용.zip", "application/zip", help="미리보기 순서에 맞춰 사진 번호를 붙이고, 메인 키워드와 사진 장면을 반영한 파일명으로 저장합니다.")
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
            st.session_state.result = attach_generation_thumbnail_marker(parse(raw), f.get("store", ""))
            st.rerun()
