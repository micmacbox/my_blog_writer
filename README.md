# 독립 블로그 작성 도구

이 저장소는 코드·실행 환경·의존성·인증을 공유하지 않는 두 프로젝트를 보관합니다.
각 폴더를 그대로 다른 Git 저장소의 루트로 옮길 수 있습니다.

| 프로젝트 | 용도 | 실행 파일 | 설정 비밀번호 |
| --- | --- | --- | --- |
| [review_writer](review_writer/README.md) | 방문 후기·사진·썸네일·네이버 원고 ZIP | `review_writer/app.py` | `BLOG_APP_PASSWORD` |
| [weekly_food](weekly_food/README.md) | 주간 화제·추가 조사·레시피/식당 가이드·이미지 | `weekly_food/app.py` | `WEEKLY_FOOD_PASSWORD` |

## 원고 생성기

```bash
cd review_writer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# .streamlit/secrets.example.toml을 secrets.toml로 복사하고 비밀번호·API 키 설정
streamlit run app.py --server.port 8501
```

## Weekly Food

별도 터미널과 별도 가상환경에서 실행합니다.

```bash
cd weekly_food
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# .streamlit/secrets.example.toml을 secrets.toml로 복사하고 비밀번호·API 키 설정
streamlit run app.py --server.port 8502
```

CLI도 Weekly Food 프로젝트 안에서 실행합니다.

```bash
cd weekly_food
python3 -m weekly_food --demo
python3 -m unittest discover -s tests -v
```

## 저장소 분리와 배포

- `review_writer/` 또는 `weekly_food/`의 **내용 전체**를 새 저장소 루트로 복사하면 됩니다. 각 폴더에 실행 파일, 필요한 코드·자산, requirements.txt, README, 테스트, .gitignore, 설정 예제가 있습니다.
- 형제 프로젝트, 공용 Python 모듈, 공용 비밀번호·세션·secrets.toml, 상위 디렉터리의 자산에 의존하지 않습니다.
- 기존 통합 탭은 제거했습니다. 두 앱은 독립 서버/배포 주소로 사용하며 재시작과 배포도 각각 수행합니다.
- 현재 저장소에서 새로 배포할 때 각 프로젝트의 `app.py`를 실행 파일로 지정합니다. 해당 프로젝트의 requirements.txt로 의존성을 설치하세요. 별도 저장소로 분리한 뒤에는 루트 `app.py`와 `requirements.txt`를 사용하면 됩니다.
- 루트의 `app.py`, `requirements.txt`는 기존 원고 생성기 배포 경로를 위한 호환 진입점입니다. 원고 생성기만 실행하며 Weekly Food를 불러오지 않습니다. Weekly Food의 의존성은 별도로 설치해야 합니다.
- 실제 비밀번호·API 키는 예제 파일에 넣지 마세요. 각 배포의 Secrets에 각각 설정하세요. 현재 루트의 기존 Secrets는 이동하거나 복사하지 않았습니다.
- 기존 루트 `output/weekly_food/`의 생성 결과는 유지했습니다. 앞으로의 CLI 결과는 실행하는 Weekly Food 프로젝트의 `output/` 아래에 저장됩니다.

루트에서 분리 경계 검증:

```bash
python3 -m unittest discover -s tests -v
```
