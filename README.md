# 네이버 블로그 후기 원고 작성기

Streamlit 기반 블로그 후기 초안 작성 도구입니다. Gemini 또는 Groq 중 AI 제공자를 선택할 수 있고, 이미지 업로드·분석(지원 모델에 한함)·원고 생성·ZIP 내보내기를 지원합니다.

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 `http://localhost:8501`을 엽니다.

## API 키 설정

사이드바에서 키를 직접 입력하거나 환경변수로 설정할 수 있습니다.

- `GEMINI_API_KEY`
- `GROQ_API_KEY`

Streamlit Community Cloud에서는 앱의 **Settings → Secrets**에 다음과 같이 입력합니다.

```toml
GEMINI_API_KEY = "발급받은 키"
GROQ_API_KEY = "발급받은 키"
```

키가 포함된 `.env` 및 `.streamlit/secrets.toml`은 GitHub에 올리지 마세요.

## 이미지 폴더 설정 (DEFAULT_FOLDER 대체)

개인 PC 경로를 코드에 고정하지 않도록 기본 이미지 폴더를 다음 순서로 결정합니다.

1. 환경변수 `BLOG_IMAGE_FOLDER`
2. Streamlit Community Cloud의 App → Settings → Secrets에 지정한 `BLOG_IMAGE_FOLDER`
3. 프로젝트 루트의 `images/` 폴더(존재하는 경우)
4. 모두 없으면 빈 경로로 시작하며, 화면에서 사진을 직접 업로드할 수 있습니다.

Secrets 설정 예시:

```toml
BLOG_IMAGE_FOLDER = "./images"
```

**Streamlit Community Cloud 주의:** 배포 앱은 사용자의 개인 PC 폴더에 접근할 수 없습니다. 고정 사진을 앱과 함께 배포하려면 저장소에 `images/` 폴더를 추가하고, 매번 다른 사진을 쓰려면 앱 화면의 사진 업로드를 사용하세요. Cloud 인스턴스의 로컬 파일 변경은 재시작·재배포 시 유지된다고 보장되지 않습니다.

## 사진 처리 최적화

- AI 분석용 정지 이미지: 긴 변 최대 1,280px, JPEG 품질 76
- ZIP/미리보기용 일반 이미지: 긴 변 최대 760px, JPEG 품질 78
- GIF: 애니메이션 프레임 유지, 긴 변 최대 720px, 최적화 저장
- 클라우드 배포 환경에서는 로컬 폴더 경로 대신 화면의 사진 업로드 기능을 사용합니다.

원본보다 파일 크기가 줄어드는 정도는 사진의 해상도·형식·내용에 따라 다릅니다. GIF는 프레임 수가 많으면 여전히 클 수 있습니다.

## AI 제공자 참고

Groq의 기본 GPT-OSS 모델은 텍스트 전용으로 처리합니다. 사진은 ZIP에 포함되지만 사진 내용의 AI 분석은 건너뜁니다. 이미지 분석이 필요하면 Gemini를 선택하세요. Groq 연결 테스트는 API 키 인증뿐 아니라 선택한 모델로 실제 텍스트 생성 요청까지 확인합니다.

## 배포 관련 중요 안내

이 프로젝트는 **Streamlit 앱**입니다. GitHub 저장소에는 올릴 수 있지만, Vercel은 Streamlit의 상시 실행 서버를 그대로 호스팅하는 플랫폼이 아니므로 이 프로젝트를 Vercel에 바로 연결해 정상 배포할 수 없습니다.

현재 코드를 유지하려면 GitHub 저장소를 **Streamlit Community Cloud**에 연결하는 방식이 가장 간단합니다. Vercel을 꼭 사용하려면 화면과 서버 구조를 Next.js 기반으로 재구성하는 별도 마이그레이션이 필요합니다.

## 로컬 실행
python3 -m streamlit run app.py