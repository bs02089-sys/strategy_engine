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
├── README.md
├── scripts/
│   └── setup_browser_libs.sh    # [root 불필요] 브라우저용 시스템 라이브러리 로컬 설치
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

브라우저가 준비되지 않았거나 라이브러리가 없으면 스크립트는 예외를 잡아 원인과 해결 방법을
안내하고, 나머지 모드는 계속 진행합니다.

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

**적응형 재탐색이 아무것도 찾지 못하는 경우**
먼저 `auto_save=True` 로 저장이 되었는지 확인하세요.

```python
if page.retrieve("quotes") is None:
    print("저장된 fingerprint 가 없습니다")
```

도메인이 바뀌었다면 `identifier` 와 `adaptive_domain` 을 함께 맞춰 주세요.

## 참고 링크

- Scrapling GitHub: <https://github.com/D4Vinci/Scrapling>
- 문서: <https://scrapling.readthedocs.io/>
- 적응형 스크래핑: <https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html>
- StealthyFetcher: <https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html>

## 주의

웹 스크래핑 시 대상 사이트의 `robots.txt`, 이용약관, 그리고 관련 법규를 반드시 확인하세요.
예시에서 사용하는 두 사이트는 스크래핑 연습 및 안티봇 데모 용도로 공개된 사이트입니다.
