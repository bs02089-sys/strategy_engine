# Scrapling Web Scraping 예시

[Scrapling](https://github.com/D4Vinci/Scrapling) 을 사용한 웹 스크래핑 예시 프로젝트입니다.

Scrapling 은 **적응형(adaptive) 스크래핑** 프레임워크로, 요소의 고유 속성(fingerprint)을 저장해 두었다가
사이트 개편으로 CSS selector 가 깨져도 요소를 다시 찾아냅니다. 또한 `StealthyFetcher` 로
Cloudflare Turnstile/Interstitial 같은 안티봇 보호를 우회할 수 있습니다.

## 요구 사항

| 항목 | 버전 |
| --- | --- |
| Python | 3.10 이상 (검증 환경: 3.14.6) |
| Scrapling | 0.4.15 |
| OS | Linux / macOS / WSL (브라우저 모드는 Linux 계열에서 검증) |

> Scrapling 은 Python 3.10 미만을 지원하지 않습니다.

## 설치

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 브라우저 fetcher 사용 시 (stealthy 모드)

`static`, `adaptive` 모드는 브라우저 없이 바로 동작합니다.
`stealthy` 모드는 실제 Chromium 을 띄우므로 아래 두 단계가 추가로 필요합니다.

**1단계 — 브라우저 바이너리 내려받기**

```bash
scrapling install
```

`scrapling install` 은 브라우저 바이너리뿐 아니라 시스템 라이브러리(`playwright install-deps`)까지
설치하려 하므로 **root 권한이 필요**합니다. 권한이 없다면 브라우저 바이너리만 따로 받으세요.

```bash
python -m patchright install chromium
```

**2단계 — 브라우저용 시스템 라이브러리**

Chromium 은 `libnspr4`, `libnss3`, `libasound2` 같은 시스템 라이브러리를 요구합니다.
root 권한이 있다면 표준 명령으로 끝납니다.

```bash
sudo playwright install-deps chromium
```

컨테이너나 WSL 처럼 **root 권한이 없는 환경**이라면 이 프로젝트의 헬퍼 스크립트를 사용하세요.
`.deb` 패키지를 프로젝트 안(`.browser-libs/`)에 내려받아 풀어 두며, **시스템은 건드리지 않습니다.**

```bash
bash scripts/setup_browser_libs.sh
```

`main.py` 는 `.browser-libs/` 가 있으면 실행 시 자동으로 `LD_LIBRARY_PATH` 에 추가합니다.
폴더가 없으면 아무것도 하지 않으므로, 라이브러리가 이미 갖춰진 환경에서는 동작에 영향이 없습니다.

### Docker 로 실행 (브라우저 의존성 문제 회피)

`Dockerfile` 은 `python:3.14-slim-bookworm` 을 베이스로 씁니다. 브라우저 바이너리와
시스템 라이브러리는 `patchright install --with-deps chromium` 한 줄이 처리하므로
베이스 이미지가 브라우저 의존성을 제공하지 않아도 됩니다.

```bash
docker build -t scrapling-demo .

docker run --rm scrapling-demo                             # stealthy (기본)
docker run --rm scrapling-demo python main.py -m static    # 오프라인 모드
docker run --rm scrapling-demo python main.py -m all       # 전체 실행
```

이미지 크기는 약 **1.49GB** 입니다. 처음에는 공식 Playwright 이미지
(`mcr.microsoft.com/playwright/python:v1.63.0-noble`)를 썼는데, firefox 와 webkit 및 그들의
의존성까지 포함해 2.74GB 였습니다. 이 프로젝트는 chromium 하나만 필요하므로 slim 베이스에
필요한 것만 설치하도록 바꿔 **45% 줄였습니다.**

이 이미지에는 `scripts/setup_browser_libs.sh` 우회책이 **들어가지 않습니다.**
`main.py` 의 `.browser-libs` 자동 감지도 아무것도 찾지 못하고 그냥 넘어갑니다.

## 실행 방법

```bash
python main.py                     # adaptive 모드 (기본)
python main.py -m static           # 네트워크 없이 오프라인 확인
python main.py -m stealthy         # Cloudflare 우회 예시
python main.py -m all              # 전체 실행
python main.py -m adaptive -u <URL>  # 대상 URL 지정
```

모든 실행 결과는 `output/results.json` 에 저장됩니다.

## 프로젝트 구조

```
.
├── main.py                      # 예시 스크립트 (adaptive / stealthy / static 모드)
├── requirements.txt             # 의존성 (scrapling[fetchers] 포함)
├── Dockerfile                   # python:3.14-slim + chromium (1.49GB) 실행 환경
├── .dockerignore
├── README.md
├── scripts/
│   ├── setup_browser_libs.sh    # [root 불필요] 브라우저용 시스템 라이브러리 로컬 설치
│   └── check_block_detection.py # 차단 감지 로직 fixture 검증 (차단 4 · 정상 2)
├── .browser-libs/               # 위 스크립트가 내려받는 라이브러리 (git 추적 제외)
└── output/                      # 실행 산출물 (git 추적 제외)
    ├── results.json
    └── adaptive_storage.db
```

## 모드별 설명

### 1. `adaptive` — 적응형 스크래핑 (기본)

[quotes.toscrape.com](https://quotes.toscrape.com/) 을 대상으로 적응형 스크래핑의 두 단계를 보여줍니다.

1. **Save 단계** — 정상 selector(`.quote`)로 요소를 찾으면서 `auto_save=True` 로 고유 속성을 저장합니다.
2. **Match 단계** — 사이트 개편으로 selector 가 깨진 상황(`.quote--v2`, 매칭 0개)을 가정한 뒤,
   `adaptive=True` 로 저장해 둔 속성과 가장 유사한 요소를 되찾습니다.

```python
from scrapling.fetchers import Fetcher

Fetcher.configure(adaptive=True, storage_args={"storage_file": "output/adaptive_storage.db"})

page = Fetcher.get("https://quotes.toscrape.com/", stealthy_headers=True)

# 1) 저장
page.css(".quote", auto_save=True, identifier="quotes")

# 2) selector 가 깨진 뒤 재탐색
page.css(".quote--v2", adaptive=True, identifier="quotes")
```

실행 확인된 출력:

```
[adaptive] GET https://quotes.toscrape.com/ -> HTTP 200
[adaptive] 저장 단계: '.quote' -> 10개 저장
[adaptive] 개편 후 '.quote--v2' -> 0개 (0개여야 정상)
[adaptive] adaptive=True 재탐색 -> 10개 복구
[adaptive] 복구된 첫 항목이 저장 시점과 동일함을 확인
```

### 2. `stealthy` — 안티봇 우회

`StealthyFetcher` 로 Cloudflare 데모 페이지를 가져옵니다.

```python
from scrapling.fetchers import StealthyFetcher

page = StealthyFetcher.fetch(
    "https://nopecha.com/demo/cloudflare",
    headless=True,
    network_idle=True,
    solve_cloudflare=True,  # Turnstile/Interstitial 자동 해결
    timeout=90_000,         # Cloudflare 해결에는 60초 이상 권장 (밀리초)
)
```

주요 인자:

| 인자 | 설명 |
| --- | --- |
| `headless` | 창을 띄우지 않고 실행 |
| `network_idle` | 네트워크가 500ms 이상 조용해질 때까지 대기 |
| `solve_cloudflare` | Cloudflare 챌린지를 자동으로 해결 |
| `block_webrtc` | 프록시 사용 시 로컬 IP 유출 방지 |
| `hide_canvas` | 캔버스 지문 채집 방지 |
| `timeout` | 밀리초 단위. 기본 30,000 |

실행 확인된 출력 (실제 Cloudflare Turnstile 챌린지를 우회함):

```
INFO: The turnstile version discovered is "interactive"
INFO: Cloudflare captcha is solved
INFO: Fetched (200) <GET https://nopecha.com/demo>
[stealthy] status=200 title='NopeCHA - CAPTCHA Demo' 링크 19개
```

두 fetcher 모드(`adaptive`·`stealthy`)는 실패해도 예외를 잡아 **원인을 분류해** 안내하고,
나머지 모드는 계속 진행합니다 — 한 모드가 죽어도 결과 JSON 은 남으므로 `-m all` 도 중단되지 않습니다.
(분류 기준과 실측 메시지는 아래 '문제 해결' 참고.)

#### 차단 감지 — 200 이라고 우회 성공이 아니다

Scrapling 은 Cloudflare 해결에 **실패해도 예외를 던지지 않습니다.** 로그만
`Failed to solve the Cloudflare challenge ... returning the page as is` 로 남기고
**챌린지 페이지를 그대로 돌려줍니다.** 그래서 상태코드만 보면 실패를 성공으로 읽게 됩니다.
`main.py` 는 상태코드와 본문을 함께 보고 판정합니다 (`detect_block`).

| 신호 | 판정 |
| --- | --- |
| `401/403/407/429/444/500/502/503/504` | `HTTP <코드>` |
| 본문에 `cType: '` — Scrapling 내부 검출기와 같은 신호 | 챌린지 페이지 |
| 본문에 `challenges.cloudflare.com/turnstile` | 임베드 위젯 |
| 본문/제목에 `Just a moment` | 인터스티셜 |

차단으로 판정되면 결과 JSON 에 `ok=false` · `blocked=true` · `block_reason` 이 남습니다.

⚠️ **`__cf_chl` 을 마커로 쓰면 안 됩니다.** 2026-09-21 실측에서 **성공한 nopecha 페이지
(200, 실제 콘텐츠)** 본문에도 그 문자열이 들어 있었습니다(스크립트 URL).
`scripts/check_block_detection.py` 가 이 판단을 fixture 로 고정합니다 — 차단 4건과 함께
**정상 페이지 2건(오탐 함정 포함)** 을 검사해 오탐도 막습니다.

```bash
python scripts/check_block_detection.py
```

```
[fixture] 인터스티셜 200                                        status=200 -> 차단 (Cloudflare 챌린지 페이지 (본문에 "cType: '"))
[fixture] 임베드 Turnstile 200                                status=200 -> 차단 (Cloudflare 챌린지 페이지 (본문에 'challenges.cloudflare.com/turnstile'))
[fixture] HTTP 403                                         status=403 -> 차단 (HTTP 403)
[fixture] HTTP 429                                         status=429 -> 차단 (HTTP 429)
[fixture] 정상 페이지 200                                       status=200 -> 정상
[fixture] 오탐 함정(__cf_chl·turnstile 포함 성공 페이지)              status=200 -> 정상
PASS: 차단 감지 fixture 6건 (차단 4 · 정상 2)
```

참고: **인터스티셜은 보통 403 이라 상태코드로도 잡히지만, 200 으로 오는 챌린지 페이지는
상태코드로 구분되지 않습니다.** 또 임베드 Turnstile 데모는 해결 여부가 같은 200 페이지
안에서 결정되어 본문만으로는 구분되지 않으므로, 상태코드 검사도 함께 유지합니다.

### 3. `static` — 오프라인 확인

네트워크나 브라우저 없이 내장 HTML 로 파서와 적응형 기능을 확인합니다.
설치 직후 동작 여부를 점검할 때 유용합니다.

```bash
python main.py -m static
```

실행 확인된 출력:

```
[static] css('.product') -> 3개
[static] xpath data-id -> ['p1', 'p2', 'p3']
[static] find_by_text('USB-C Hub') -> 'USB-C Hub'
[static] 깨진 selector 로 adaptive 재탐색 -> 1개 복구
```

⚠️ `find_by_text` 는 **못 찾았을 때 `None` 이 아니라 빈 결과를 돌려줍니다.** 2026-09-21 실측으로
확인했으며(`Selector` → 찾음 / 빈 `Selectors` → 못 찾음), `is not None` 으로 검사하면 못 찾음을
절대 잡지 못합니다. 그래서 `bool()` 로 판정하고 로그에는 `(찾지 못함)` 을 덧붙입니다.

## 적응형 스크래핑 동작 원리

Scrapling 은 요소의 **태그명·텍스트·속성·형제 요소·경로**, 그리고 부모의 태그명·속성·텍스트를
고유 속성으로 저장합니다. 사이트가 개편되면 현재 페이지의 모든 요소와 저장된 속성을 비교해
유사도가 가장 높은 요소를 반환합니다.

- 저장소는 기본적으로 SQLite 를 사용합니다.
- Scrapling 기본 저장 경로는 `site-packages/scrapling/elements_storage.db` 입니다.
  이 프로젝트는 `storage_args` 로 **`output/adaptive_storage.db`** 에 저장하도록 지정해
  실행 산출물과 함께 관리합니다.
- 저장은 도메인 단위로 구분되므로, 저장할 때와 찾을 때의 도메인이 같아야 합니다.
  도메인이 바뀌었다면 `adaptive_domain` 인자로 동일 사이트임을 알려줄 수 있습니다.
- 하나의 selector 로 여러 요소를 저장하더라도 **첫 번째 요소의 속성만 저장**됩니다.
  (`static` 모드에서 복구 결과가 1개인 이유입니다.)

## 문제 해결

`stealthy` 모드가 실패하면 스크립트가 예외 문자열을 보고 원인을 분류해 안내합니다.
결과 JSON 에는 `ok=false` · `failure_kind` · `error` 가 남습니다. 분류 기준은 아래 실측값입니다.

| 예외 메시지의 식별자 | 해당 모드 | `failure_kind` | 안내 |
| --- | --- | --- | --- |
| `Executable doesn't exist at <경로>` | stealthy | 브라우저 바이너리 없음 | `scrapling install` 또는 `python -m patchright install chromium` |
| `error while loading shared libraries: libnspr4.so` | stealthy | 시스템 라이브러리 없음 | `sudo playwright install-deps chromium` 또는 `bash scripts/setup_browser_libs.sh` |
| `net::ERR_*` (예: `net::ERR_NAME_NOT_RESOLVED`) | stealthy | 네트워크/대상 문제 | **브라우저 설치와 무관** — 대상 URL 과 네트워크 확인 |
| `Timeout <n>ms exceeded` | stealthy | 응답 지연/차단 | `timeout` 을 60초 이상으로 |
| `DNSError: ... curl: (6) Could not resolve host` (`curl: (N)` 계열) | adaptive | 네트워크/대상 문제 | **브라우저를 쓰지 않는 모드** — 대상 URL 과 네트워크 확인 |
| `HTTP 404` · `HTTP 403` 등 4xx/5xx 응답 | adaptive | `HTTP <코드>` | 받은 페이지가 대상이 아님 — **selector 문제가 아니므로** URL 확인 |

⚠️ **시스템 라이브러리 부재는 Playwright 예외의 첫 줄에 원인이 안 드러납니다.** 2026-09-21 실측에서
첫 줄은 `BrowserType.launch_persistent_context: Target page, context or browser has been closed` 였고,
`libnspr4.so` 오류는 브라우저 로그 줄에만 있었습니다. 그래서 첫 줄이 아니라 **식별자가 들어 있는 줄**을
찾아 보여주며(`(pid=..)[err]` 접두사는 떼어냅니다), 어느 식별자에도 안 걸리면 원인을 단정하지 않고
`원인 미분류` 로 표시합니다.

**`Executable doesn't exist at .../chrome`**
브라우저 바이너리가 없습니다. `scrapling install` 또는 `python -m patchright install chromium` 을 실행하세요.

**`error while loading shared libraries: libnspr4.so` (또는 libnss3, libasound)**
브라우저 실행에 필요한 시스템 라이브러리가 없는 경우입니다.

```bash
sudo playwright install-deps chromium          # root 권한이 있는 경우
bash scripts/setup_browser_libs.sh             # root 권한이 없는 경우 (시스템 미변경)
```

`scrapling install` 이 `playwright install-deps` 단계에서 실패하는 것도 같은 원인입니다.
브라우저 바이너리는 이미 받아졌을 수 있으니, 위 2단계만 따로 처리하면 됩니다.
Docker 라면 `mcr.microsoft.com/playwright` 이미지를 쓰면 이 문제를 피할 수 있습니다.

**Cloudflare 챌린지가 계속 반복되는 경우**
`timeout` 을 60초 이상으로 두세요. 챌린지 종류에 따라 한 번 더 시도하는 로그가 정상적으로 출력됩니다.
`Timeout <n>ms exceeded` 가 이 경우이고, 챌린지 페이지가 200 으로 돌아오면 `차단 감지` 로 판정됩니다.

**`net::ERR_...` (네트워크 오류)**
브라우저는 정상적으로 떴고 대상 주소에 닿지 못한 경우입니다. 브라우저 설치 문제가 아니므로
`scrapling install` 을 다시 돌릴 필요가 없습니다 — URL 과 네트워크 연결을 확인하세요.

**적응형 재탐색이 아무것도 찾지 못하는 경우**
먼저 `auto_save=True` 로 저장이 되었는지 확인하세요.

```python
if page.retrieve("quotes") is None:
    print("저장된 fingerprint 가 없습니다")
```

도메인이 바뀌었다면 `identifier` 와 `adaptive_domain` 을 함께 맞춰 주세요.

## 검증 상태

| 항목 | 상태 |
| --- | --- |
| `static` 모드 | ✅ 실행 확인 (로컬 + Docker CI) |
| `adaptive` 모드 | ✅ 실행 확인 (10개 저장 → selector 파손 → 10개 복구) |
| `stealthy` 모드 (브라우저) | ✅ 실행 확인 — 실제 Cloudflare Turnstile 챌린지 우회 성공 (HTTP 200, `blocked=false`) |
| `scripts/setup_browser_libs.sh` | ✅ 새로 실행하여 확인 (root 권한 불필요) |
| `scripts/check_block_detection.py` | ✅ fixture 6건 (차단 4 · 정상 2) — 200 챌린지 페이지를 성공으로 오인하지 않음 |
| `Dockerfile` (slim, 1.49GB) | ✅ **Docker 실기 검증 완료** — 컨테이너 안에서 static 파서·adaptive 재탐색·stealthy Cloudflare 우회 모두 성공 |

### Dockerfile 검증 방식

개발 환경에 컨테이너 런타임이 없어 로컬에서는 빌드할 수 없으므로,
GitHub Actions 러너에서 실제로 빌드·실행해 검증합니다
(`.github/workflows/scrapling_docker.yml`, `scrapling-project/**` 변경 시 자동 실행).

워크플로우가 확인하는 것:

1. 이미지가 빌드되는가 — `scrapling-demo:latest 1.49GB`
2. 로컬 우회책이 이미지에 없는가 — `OK: /app/.browser-libs 없음`
3. 차단 감지가 200 챌린지 페이지를 성공으로 오인하지 않는가 — fixture 6건(차단 4 · 정상 2)
4. static 모드가 그대로 동작하는가 — 상품 3개 · xpath `['p1','p2','p3']` · `find_by_text` 일치 · 재탐색 1개 복구
5. adaptive 모드가 깨진 selector 를 재탐색으로 복구하는가 — 10개 저장 → 0개 매칭 → 10개 복구
6. stealthy 모드가 실제로 Cloudflare 를 통과하는가 — `status=200` (`blocked` 필드도 함께 검사)

종료코드는 신뢰하지 않습니다. `main.py` 는 모드가 실패해도 exit 0 으로 끝나고
`ok=false` / 0건을 남기므로, 워크플로우는 모드별 `results.json` 을 읽어 값을 검사합니다.
실패 원인도 즉시 드러납니다 — 차단은 `::error::차단 감지: <이유> — 우회 실패`,
`adaptive`·`stealthy` 실패는 `::error::<모드> 모드 실패(<원인 종류>): <이유>` 로 보고됩니다.
(`static` · 차단 감지 fixture 는 네트워크 없이 돌아갑니다.)

### 빌드 캐시를 쓴 이유

구성마다 **다른 러너**에서 재어 왜곡을 없앤 측정값입니다.

| 구성 | 빌드 스텝 소요 |
| --- | --- |
| 기본 빌드 | 52초 |
| 캐시 적중 | 29초 (+ buildx 준비 3초) |
| 캐시 미적중 | 135초 |

실제 런에서도 재확인했습니다 — 현재 워크플로우의 로그는 빌드 레이어가 `#8~#11 CACHED`,
빌드 스텝 29초, 이미지 `1.49GB` 입니다.

이미지가 작아지면서 `load: true` 로 로컬 데몬에 옮기는 비용이 줄어 캐시가 **이득으로 뒤집혔습니다.**
2.74GB 시절에는 그 비용이 55초라 캐시가 오히려 손해여서 도입했다가 되돌렸습니다.
`Dockerfile` 의 마지막이 `COPY main.py` 라서 코드·문서만 바꾸면 무거운 레이어(pip 설치,
chromium 설치)는 그대로 캐시되므로 대부분의 실행이 캐시 적중입니다.

## 참고 링크

- Scrapling GitHub: <https://github.com/D4Vinci/Scrapling>
- 문서: <https://scrapling.readthedocs.io/>
- 적응형 스크래핑: <https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html>
- StealthyFetcher: <https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html>

## 주의

웹 스크래핑 시 대상 사이트의 `robots.txt`, 이용약관, 그리고 관련 법규를 반드시 확인하세요.
예시에서 사용하는 두 사이트는 스크래핑 연습 및 안티봇 데모 용도로 공개된 사이트입니다.
