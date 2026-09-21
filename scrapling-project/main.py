#!/usr/bin/env python3
"""Scrapling 웹 스크래핑 예시.

세 가지 모드를 제공한다.

  adaptive (기본) : 적응형 스크래핑. 요소의 고유 속성(fingerprint)을 저장해 두었다가
                    사이트 개편으로 selector 가 깨져도 요소를 다시 찾아낸다.
  stealthy        : StealthyFetcher 로 Cloudflare 보호 페이지를 우회한다.
                    (실행 전 `scrapling install` 로 브라우저 설치 필요)
  static          : 네트워크 없이 내장 HTML 로 파서와 적응형 기능을 확인한다.

사용 예:
    python main.py                        # adaptive 모드 (기본)
    python main.py -m static              # 네트워크 없는 오프라인 확인
    python main.py -m stealthy            # Cloudflare 우회 예시
    python main.py -m all                 # 전체 실행
    python main.py -m adaptive -u <URL>   # 대상 URL 지정
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from scrapling.parser import Selector

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

# 적응형 데이터 저장소.
# Scrapling 기본값은 site-packages 안(scrapling/elements_storage.db)이라 추적/백업이 어렵다.
# 프로젝트 폴더로 옮겨 실행 결과와 함께 관리한다.
ADAPTIVE_DB = OUTPUT_DIR / "adaptive_storage.db"

QUOTES_URL = "https://quotes.toscrape.com/"
CLOUDFLARE_DEMO_URL = "https://nopecha.com/demo/cloudflare"

# 데모용 사이트(quotes.toscrape.com)에서 quote 카드를 가리키는 selector.
QUOTES_SELECTOR = ".quote"
# 사이트 개편으로 위 selector 가 깨진 상황을 흉내 내기 위한, 일부러 존재하지 않는 selector.
BROKEN_SELECTOR = ".quote--v2"
# 적응형 저장소에서 이 요소를 찾아내기 위한 식별자.
QUOTES_IDENTIFIER = "quotes"

SAMPLE_HTML = """
<html><body>
  <div class="catalog">
    <article class="product" data-id="p1">
      <h3 class="name">Mechanical Keyboard</h3>
      <span class="price">$89.00</span>
    </article>
    <article class="product" data-id="p2">
      <h3 class="name">Noise Cancelling Headphones</h3>
      <span class="price">$249.00</span>
    </article>
    <article class="product" data-id="p3">
      <h3 class="name">USB-C Hub</h3>
      <span class="price">$39.00</span>
    </article>
  </div>
</body></html>
"""


# --------------------------------------------------------------------------- #
# 공통 헬퍼
# --------------------------------------------------------------------------- #
def extract_quotes(elements: Any) -> list[dict[str, Any]]:
    """선택된 quote 카드들에서 본문/저자/태그를 뽑아낸다."""
    quotes = []
    for element in elements:
        quotes.append(
            {
                "text": (element.css(".text::text").get() or "").strip(),
                "author": (element.css(".author::text").get() or "").strip(),
                "tags": element.css(".tag::text").getall(),
            }
        )
    return quotes


def save_json(filename: str, payload: Any) -> Path:
    """결과를 output/ 아래 JSON 으로 저장한다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def adaptive_storage_args() -> dict[str, str]:
    """적응형 저장소를 프로젝트 폴더로 지정하기 위한 인자."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return {"storage_file": str(ADAPTIVE_DB)}


def local_browser_libs_dir() -> Path | None:
    """`scripts/setup_browser_libs.sh` 가 내려받아 둔 라이브러리 폴더를 찾는다.

    root 권한 없이 브라우저를 띄우기 위한 장치로, 없으면 None 을 돌려준다.
    """
    base = BASE_DIR / ".browser-libs" / "usr" / "lib"
    if not base.is_dir():
        return None
    for candidate in sorted(base.glob("*-linux-gnu")):
        if candidate.is_dir():
            return candidate
    return base if any(base.glob("*.so*")) else None


def enable_local_browser_libs() -> Path | None:
    """로컬 라이브러리 폴더가 있으면 LD_LIBRARY_PATH 앞에 붙인다.

    브라우저는 자식 프로세스로 뜨기 때문에 여기서 환경변수를 설정하면 그대로 상속된다.
    폴더가 없으면 아무것도 하지 않으므로, 시스템 라이브러리가 갖춰진 환경에서는
    동작에 영향이 없다.
    """
    libs = local_browser_libs_dir()
    if libs is None:
        return None
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [str(libs), *(p for p in current.split(os.pathsep) if p)]
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(parts))
    return libs


# --------------------------------------------------------------------------- #
# 실패 원인 분류 — 두 fetcher 가 서로 다른 예외를 던진다
# --------------------------------------------------------------------------- #
# 예외 메시지의 **첫 줄만 보여주면 안 된다.** 2026-09-21 로컬 재현에서 확인한 두 가지:
#   · stealthy(브라우저) 시스템 라이브러리 부재 → 첫 줄은 "Target page, context or
#     browser has been closed" 라서 원인이 드러나지 않고, 원인은 브라우저 로그 줄에만 있다.
#   · adaptive(HTTP) 네트워크 실패 → curl_cffi 예외가 그대로 튀어올라(안 잡으면 트레이스백)
#     사용자가 볼 안내가 없다.
# 그래서 식별자가 들어 있는 줄을 찾아 이유로 쓰고, 원인 종류에 맞는 안내를 함께 돌려준다.
# 표기한 문자열은 모두 그 재현에서 확인한 것이다.
STEALTHY_FAILURE_SIGNATURES = (
    #   브라우저 바이너리 없음 : "Executable doesn't exist at <경로>"
    #   시스템 라이브러리 없음 : "[pid=..][err] <chrome>: error while loading shared libraries: libnspr4.so ..."
    #   네트워크/대상 문제    : "net::ERR_*" (예: ERR_NAME_NOT_RESOLVED) — 브라우저와 무관하다
    #   응답 지연/차단        : "Timeout <n>ms exceeded"
    (
        ("Executable doesn't exist",),
        "브라우저 바이너리 없음",
        (
            "브라우저 바이너리가 없습니다.",
            "  root 권한이 있으면 : scrapling install",
            "  root 권한이 없으면: python -m patchright install chromium",
        ),
    ),
    (
        ("shared libraries", "libnspr4"),
        "시스템 라이브러리 없음",
        (
            "브라우저 실행에 필요한 시스템 라이브러리가 없습니다.",
            "  root 권한이 있다면 : sudo playwright install-deps chromium",
            "  root 권한이 없다면: bash scripts/setup_browser_libs.sh",
        ),
    ),
    (
        ("net::ERR_",),
        "네트워크/대상 문제",
        (
            "브라우저는 정상 동작했고 네트워크 또는 대상 주소 문제입니다 (브라우저 설치와 무관).",
            "  대상 URL 과 네트워크 연결을 확인하세요.",
        ),
    ),
    (
        ("ms exceeded",),
        "응답 지연/차단",
        (
            "응답이 timeout 안에 오지 않았습니다. 차단 챌린지가 풀리지 않았을 수 있습니다.",
            "  timeout 을 60초 이상으로 두고 다시 시도해 보세요 (Cloudflare 권장).",
        ),
    ),
)

REQUEST_FAILURE_SIGNATURES = (
    # curl_cffi(HTTP fetcher) 실패는 curl 오류코드가 메시지에 들어온다.
    #   "Failed to perform, curl: (6) Could not resolve host: ..." (DNS)
    #   "curl: (7) Failed to connect" · "curl: (28) timed out" 등
    (
        ("curl: (", "Could not resolve host", "Failed to connect", "timed out"),
        "네트워크/대상 문제",
        (
            "대상 주소에 닿지 못했습니다 (브라우저를 쓰지 않는 HTTP 모드입니다).",
            "  대상 URL 과 네트워크 연결을 확인하세요.",
        ),
    ),
)


def classify_failure(detail: str, signatures: tuple) -> tuple[str, str, list[str]]:
    """예외 문자열을 (보여줄 이유, 원인 종류, 안내문) 으로 정리한다."""
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    for markers, kind, hints in signatures:
        reason = next((line for line in lines if any(m in line for m in markers)), None)
        if reason is None:
            continue
        # 브라우저 로그 접두사([pid=..][err])를 떼어 사람이 읽는 부분만 남긴다.
        return reason.split("][err] ", 1)[-1].strip(), kind, list(hints)
    return (
        (lines[0] if lines else detail.strip()),
        "원인 미분류",
        ["원인을 분류하지 못했습니다. 로그 전체를 확인하세요."],
    )


def classify_stealthy_failure(detail: str) -> tuple[str, str, list[str]]:
    """StealthyFetcher(브라우저) 실패를 분류한다."""
    return classify_failure(detail, STEALTHY_FAILURE_SIGNATURES)


def classify_request_failure(detail: str) -> tuple[str, str, list[str]]:
    """Fetcher(HTTP, curl_cffi) 실패를 분류한다."""
    return classify_failure(detail, REQUEST_FAILURE_SIGNATURES)


# --------------------------------------------------------------------------- #
# 1) 적응형 스크래핑
# --------------------------------------------------------------------------- #
def adaptive_failure(url: str, kind: str, error: str, **extra: Any) -> dict[str, Any]:
    """실패해도 CI 와 사용자가 읽을 수 있게 성공과 같은 키를 채운다 (0건 · ok=false)."""
    return {
        "url": url,
        "ok": False,
        "failure_kind": kind,
        "error": error,
        "saved_count": 0,
        "broken_selector_matches": 0,
        "recovered_count": 0,
        "fingerprint_stored": False,
        "first_item_preserved": False,
        **extra,
    }
def run_adaptive(url: str = QUOTES_URL) -> dict[str, Any]:
    """fingerprint 저장 → selector 파손 → adaptive 재탐색 흐름을 보여준다."""
    from scrapling.fetchers import Fetcher  # curl_cffi 가 필요하므로 지연 임포트

    # 이 Fetcher 로 만든 페이지에서 adaptive 기능을 전역으로 켠다.
    Fetcher.configure(adaptive=True, storage_args=adaptive_storage_args())

    try:
        page = Fetcher.get(url, stealthy_headers=True)
    except Exception as exc:  # 예외를 안 잡으면 트레이스백만 남고 결과 JSON 도 안 쓰인다
        reason, kind, hints = classify_request_failure(f"{type(exc).__name__}: {exc}")
        print(f"[adaptive] 실패({kind}): {reason}")
        for hint in hints:
            print(f"[adaptive] {hint}")
        return adaptive_failure(url, kind, reason)

    print(f"[adaptive] GET {url} -> HTTP {page.status}")

    # 페이지를 못 받은 상태(4xx/5xx)로 진행하면 '0개 저장'만 보고되어 selector 가 깨진 것처럼
    # 보인다. 원인은 상태코드이므로 그대로 알리고, selector 판정은 하지 않는다.
    if not 200 <= page.status < 300:
        reason = f"HTTP {page.status} — 대상 페이지를 받지 못했습니다 (selector 문제가 아님)"
        print(f"[adaptive] {reason}")
        return adaptive_failure(url, f"HTTP {page.status}", reason, status=page.status)

    # --- 1) Save 단계 -------------------------------------------------------
    # 정상 selector 로 요소를 찾으면서, 그 요소의 고유 속성을 저장소에 기록한다.
    saved = page.css(QUOTES_SELECTOR, auto_save=True, identifier=QUOTES_IDENTIFIER)
    quotes = extract_quotes(saved)
    print(f"[adaptive] 저장 단계: '{QUOTES_SELECTOR}' -> {len(quotes)}개 저장")

    # --- 2) Match 단계 ------------------------------------------------------
    # 사이트가 개편되어 기존 selector 가 더 이상 매칭되지 않는 상황을 가정한다.
    broken = page.css(BROKEN_SELECTOR)
    print(f"[adaptive] 개편 후 '{BROKEN_SELECTOR}' -> {len(broken)}개 (0개여야 정상)")

    # 같은 페이지에서 adaptive=True 로 넘기면 저장해 둔 속성과 가장 유사한 요소를 되찾는다.
    recovered_elements = page.css(
        BROKEN_SELECTOR, adaptive=True, identifier=QUOTES_IDENTIFIER
    )
    recovered = extract_quotes(recovered_elements)
    print(f"[adaptive] adaptive=True 재탐색 -> {len(recovered)}개 복구")

    # 저장된 fingerprint 를 직접 조회할 수도 있다.
    has_fingerprint = page.retrieve(QUOTES_IDENTIFIER) is not None
    preserved = bool(quotes) and quotes[0]["text"] == (recovered[0]["text"] if recovered else None)
    if preserved:
        print("[adaptive] 복구된 첫 항목이 저장 시점과 동일함을 확인")
    else:
        # 아무 말도 안 하면 로그만 보고는 성공처럼 보인다 (CI 는 JSON 을 검사한다).
        print("[adaptive] 복구된 첫 항목이 저장 시점과 다릅니다")

    return {
        "url": url,
        "ok": True,
        "saved_count": len(quotes),
        "broken_selector_matches": len(broken),
        "recovered_count": len(recovered),
        "fingerprint_stored": has_fingerprint,
        "first_item_preserved": preserved,
        "quotes": quotes,
    }


# --------------------------------------------------------------------------- #
# 2) StealthyFetcher (안티봇 우회) + 차단 감지
# --------------------------------------------------------------------------- #
# 200 이라고 우회 성공이 아니다. Scrapling 은 Cloudflare 해결에 실패하면 예외를 던지지 않고
# "Failed to solve the Cloudflare challenge ... returning the page as is" 로그만 남기고
# 챌린지 페이지를 그대로 돌려준다. 그래서 상태코드만 보면 실패를 성공으로 오인한다.
BLOCKED_STATUS_CODES = frozenset({401, 403, 407, 429, 444, 500, 502, 503, 504})

# 챌린지 페이지를 알아보는 본문 마커. Scrapling 내부 검출기(_browsers/_base.py 의
# _detect_cloudflare)가 쓰는 신호와 같은 것을 쓴다 (그 함수는 private 이라 필요한 만큼만 직접 본다).
#
# ⚠️ 2026-09-21 실측: 성공한 nopecha 페이지(200, 실제 콘텐츠)에도 '__cf_chl' 이 들어 있었다
# (스크립트 URL). 그래서 그 문자열은 마커로 쓰면 정상 응답을 차단으로 오판한다.
CHALLENGE_MARKERS = (
    "cType: '",  # Turnstile/Interstitial 챌린지 스크립트
    "challenges.cloudflare.com/turnstile",  # 본문에 직접 박힌 Turnstile 위젯
    "Just a moment",  # 인터스티셜 제목
)


def detect_block(status: int, html: str) -> str | None:
    """차단/챌린지 페이지로 보이면 이유를, 아니면 None 을 돌려준다.

    네트워크·브라우저 없이 확인할 수 있도록 순수 함수로 둔다
    (fixture 검증은 scripts/check_block_detection.py).
    """
    if status in BLOCKED_STATUS_CODES:
        return f"HTTP {status}"
    for marker in CHALLENGE_MARKERS:
        if marker in html:
            return f"Cloudflare 챌린지 페이지 (본문에 {marker!r})"
    return None


def run_stealthy(url: str = CLOUDFLARE_DEMO_URL) -> dict[str, Any]:
    """StealthyFetcher 로 Cloudflare 챌린지를 우회해 페이지를 가져온다."""
    from scrapling.fetchers import StealthyFetcher  # 브라우저 스택이 필요하므로 지연 임포트

    libs = enable_local_browser_libs()
    if libs is not None:
        print(f"[stealthy] 로컬 브라우저 라이브러리 사용: {libs}")
    print(f"[stealthy] StealthyFetcher.fetch({url}) — 브라우저 기동")

    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,          # 창을 띄우지 않는다
            network_idle=True,      # 네트워크가 500ms 이상 조용해질 때까지 대기
            solve_cloudflare=True,  # Turnstile/Interstitial 자동 해결
            block_webrtc=True,      # 프록시 사용 시 로컬 IP 유출 방지
            hide_canvas=True,       # 캔버스 지문 채집 방지
            timeout=90_000,         # Cloudflare 해결에는 60초 이상 권장 (밀리초 단위)
        )
    except Exception as exc:  # 브라우저 미설치 / 라이브러리 부족 / 네트워크 오류 등
        reason, kind, hints = classify_stealthy_failure(str(exc))
        print(f"[stealthy] 실패({kind}): {reason}")
        for hint in hints:
            print(f"[stealthy] {hint}")
        return {"url": url, "ok": False, "failure_kind": kind, "error": reason}

    title = page.css("title::text").get()
    links = page.css("#padded_content a::attr(href)").getall()
    print(f"[stealthy] status={page.status} title={title!r} 링크 {len(links)}개")

    blocked = detect_block(page.status, page.html_content)
    if blocked:
        print(f"[stealthy] 차단 감지: {blocked} — 우회 실패로 판정")
        return {
            "url": url,
            "ok": False,
            "blocked": True,
            "block_reason": blocked,
            "status": page.status,
            "title": title,
        }

    return {
        "url": url,
        "ok": True,
        "blocked": False,
        "status": page.status,
        "title": title,
        "sample_links": links[:10],
    }


# --------------------------------------------------------------------------- #
# 3) 오프라인 파서 데모
# --------------------------------------------------------------------------- #
def run_static() -> dict[str, Any]:
    """네트워크/브라우저 없이 내장 HTML 로 파서와 적응형 기능을 확인한다."""
    page = Selector(
        SAMPLE_HTML,
        url="example.com",
        adaptive=True,
        storage_args=adaptive_storage_args(),
    )

    products = page.css(".product")
    print(f"[static] css('.product') -> {len(products)}개")

    ids = page.xpath('//article[@class="product"]/@data-id').getall()
    print(f"[static] xpath data-id -> {ids}")

    hit = page.find_by_text("USB-C Hub", first_match=True)
    # ⚠️ 2026-09-21 실측: 못 찾았을 때 None 이 아니라 빈 결과(Selectors)가 온다.
    #    `is not None` 으로 검사하면 '못 찾음'을 절대 잡지 못한다 — bool() 로 본다.
    found = bool(hit)
    matched_name = (hit.css("::text").get() or "").strip() if found else ""
    # 못 찾았을 때 '' 만 찍으면 "찾았는데 비어 있음" 처럼 보인다.
    not_found = "" if found else " (찾지 못함)"
    print(f"[static] find_by_text('USB-C Hub') -> {matched_name!r}{not_found}")

    # 적응형: 정상 selector 로 저장한 뒤, 깨진 selector 로도 다시 찾아낸다.
    page.css(".product", auto_save=True, identifier="products")
    recovered = page.css(".catalog-item", adaptive=True, identifier="products")
    print(f"[static] 깨진 selector 로 adaptive 재탐색 -> {len(recovered)}개 복구")

    return {
        "product_count": len(products),
        "product_ids": ids,
        "text_search_match": matched_name,
        "adaptive_recovered": len(recovered),
        "products": [
            {
                "name": (item.css(".name::text").get() or "").strip(),
                "price": (item.css(".price::text").get() or "").strip(),
            }
            for item in products
        ],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scrapling 웹 스크래핑 예시 (적응형 스크래핑 / StealthyFetcher)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python main.py                    # adaptive 모드\n"
            "  python main.py -m static          # 오프라인 확인\n"
            "  python main.py -m all             # 전체 실행\n"
        ),
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=("adaptive", "stealthy", "static", "all"),
        default="adaptive",
        help="실행할 모드 (기본: adaptive)",
    )
    parser.add_argument(
        "-u",
        "--url",
        default=None,
        help="대상 URL. 생략하면 모드별 예시 사이트를 사용한다. static 모드는 내장 HTML 을 쓰므로 무시된다.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modes = ("adaptive", "stealthy", "static") if args.mode == "all" else (args.mode,)

    results: dict[str, Any] = {}
    for mode in modes:
        print(f"\n=== {mode} ===")
        if mode == "adaptive":
            results["adaptive"] = run_adaptive(args.url or QUOTES_URL)
        elif mode == "stealthy":
            results["stealthy"] = run_stealthy(args.url or CLOUDFLARE_DEMO_URL)
        else:
            results["static"] = run_static()

    path = save_json("results.json", results)
    print(f"\n결과 저장 완료: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
