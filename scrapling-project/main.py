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


# --------------------------------------------------------------------------- #
# 1) 적응형 스크래핑
# --------------------------------------------------------------------------- #
def run_adaptive(url: str = QUOTES_URL) -> dict[str, Any]:
    """fingerprint 저장 → selector 파손 → adaptive 재탐색 흐름을 보여준다."""
    from scrapling.fetchers import Fetcher  # curl_cffi 가 필요하므로 지연 임포트

    # 이 Fetcher 로 만든 페이지에서 adaptive 기능을 전역으로 켠다.
    Fetcher.configure(adaptive=True, storage_args=adaptive_storage_args())

    page = Fetcher.get(url, stealthy_headers=True)
    print(f"[adaptive] GET {url} -> HTTP {page.status}")

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

    return {
        "url": url,
        "saved_count": len(quotes),
        "broken_selector_matches": len(broken),
        "recovered_count": len(recovered),
        "fingerprint_stored": has_fingerprint,
        "first_item_preserved": preserved,
        "quotes": quotes,
    }


# --------------------------------------------------------------------------- #
# 2) StealthyFetcher (안티봇 우회)
# --------------------------------------------------------------------------- #
def run_stealthy(url: str = CLOUDFLARE_DEMO_URL) -> dict[str, Any]:
    """StealthyFetcher 로 Cloudflare 챌린지를 우회해 페이지를 가져온다."""
    from scrapling.fetchers import StealthyFetcher  # 브라우저 스택이 필요하므로 지연 임포트

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
    except Exception as exc:  # 브라우저 미설치 등
        print(f"[stealthy] 실패: {exc}")
        print("[stealthy] 브라우저가 없다면 먼저 `scrapling install` 을 실행하세요.")
        return {"url": url, "ok": False, "error": str(exc)}

    title = page.css("title::text").get()
    links = page.css("#padded_content a::attr(href)").getall()
    print(f"[stealthy] status={page.status} title={title!r} 링크 {len(links)}개")

    return {
        "url": url,
        "ok": True,
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
    matched_name = (hit.css("::text").get() or "").strip() if hit is not None else ""
    print(f"[static] find_by_text('USB-C Hub') -> {matched_name!r}")

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
        help="대상 URL. 생략하면 모드별 예시 사이트를 사용한다.",
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
