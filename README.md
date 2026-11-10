# CafeTalks · 주간 게시글 도우미

카페에 올릴 **금주의 신상 / 게임 추천 / 신작 애니 / 유머** 후보를 매일 자동으로 모아서,
웹페이지에서 고르고 복사만 하면 카페에 바로 붙여 넣을 수 있게 해 주는 도구예요.

## 쓰는 법
1. 웹페이지 열기: `https://yub815.github.io/CafeTalks/`
2. 탭(신상 · 게임 추천 · 신작 애니 · 유머)을 골라 후보 확인
   - 신상/애니/게임: 넣을 것 체크 → 제목줄·코멘트 수정 → **체크한 것으로 글 만들기**
   - 유머: 짤 하나마다 **이 짤로 글 만들기** (글 한 편)
   - 마음에 안 드는 후보는 **숨기기**
3. 완성 화면에서 **제목 복사** → 카페 제목, **전체 복사** → 카페 본문 Ctrl+V
   - 사진이 안 붙으면 **사진 한 번에 받기**(01, 02… 순서대로 zip)

## 자동 수집
- 매일 오전 6시에 GitHub Actions가 수집해요 (`.github/workflows/collect.yml`).
- 바로 수집하고 싶으면: 저장소 **Actions** 탭 → **자동 수집** → **Run workflow**.

## 설정 바꾸기
`config.json`을 GitHub에서 바로 수정하면 다음 수집부터 반영돼요.
- `news_queries`: 뉴스 검색어
- `include_words` / `exclude_words`: 신상에 엉뚱한 제품이 섞이면 단어 추가
- `humor_exclude_words`: 유머에서 뺄 논란 소지 단어
- `humor_sources`: 사이트별 켜기/끄기
- `게임 추천` → `steam`: 스팀 검색 조건, `steam_release_days`: 며칠 이내 출시작만

## 구성
- `collector.py`: 사이트별 수집 (디시 실베, 루리웹 유머, 더쿠 HOT, Bing 뉴스, 스팀, 치지직 방송)
- `build_site.py`: 수집 → `docs/data/items.json` + `docs/img/` 로 내보내기
- `docs/index.html`: 고르기·글 만들기 웹페이지
- `app.py`: PC에서 직접 돌리는 버전 (`python app.py`)
