#!/usr/bin/env python3
"""차단 감지 로직(main.detect_block)을 fixture 로 검증한다.

실제 차단은 상대 서버가 결정하므로 마음대로 재현할 수 없다. 그래서 차단 페이지를
fixture 로 넣어 잡아내는지 확인하고, 동시에 **오탐도 검사한다** — 정상 페이지 본문에
우연히 들어 있는 문자열을 마커로 쓰면 멀쩡한 응답을 차단으로 판정해 버린다.

실행:
    python scripts/check_block_detection.py     # scrapling-project 루트에서
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import detect_block  # noqa: E402  (sys.path 설정 후 임포트)

# Cloudflare 인터스티셜. Scrapling 내부 검출기가 보는 신호와 같은 형태로 만든다.
INTERSTITIAL_HTML = """<html><head><title>Just a moment...</title>
<script>window._cf_chl_opt = {cType: 'managed', cvId: '3'};</script>
</head><body><div id="cf-challenge-running">Checking your browser</div></body></html>"""

# 본문에 Turnstile 위젯 스크립트가 직접 박힌 페이지 (Scrapling 검출기의 'embedded').
EMBEDDED_TURNSTILE_HTML = """<html><head><title>Demo</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
</head><body><div class="cf-turnstile"></div></body></html>"""

NORMAL_QUOTES_HTML = """<html><head><title>Quotes to Scrape</title></head>
<body><div class="quote"><span class="text">“A quote.”</span></div></body></html>"""

# 2026-09-21 실측: 성공한 nopecha 페이지(200, 실제 콘텐츠)에도 아래 문자열들이 들어 있었다.
# 이걸 차단으로 판정하면 정상 응답을 오판하므로, 마커에서 제외했음을 회귀 테스트로 고정한다.
SUCCESSFUL_PAGE_WITH_TRAPS = """<html><head><title>NopeCHA - CAPTCHA Demo</title></head>
<body><a href="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=abc">__cf_chl_tk</a>
<p>This page uses turnstile v3 and interactive challenges.</p></body></html>"""

# (설명, 상태코드, 본문, 차단으로 보여야 하는가)
CASES = (
    ("인터스티셜 200", 200, INTERSTITIAL_HTML, True),
    ("임베드 Turnstile 200", 200, EMBEDDED_TURNSTILE_HTML, True),
    ("HTTP 403", 403, NORMAL_QUOTES_HTML, True),
    ("HTTP 429", 429, "", True),
    ("정상 페이지 200", 200, NORMAL_QUOTES_HTML, False),
    ("오탐 함정(__cf_chl·turnstile 포함 성공 페이지)", 200, SUCCESSFUL_PAGE_WITH_TRAPS, False),
)


def main() -> int:
    failures = []
    for label, status, html, should_block in CASES:
        reason = detect_block(status, html)
        blocked = reason is not None
        verdict = "차단" if blocked else "정상"
        print(f"[fixture] {label:48} status={status} -> {verdict}" + (f" ({reason})" if reason else ""))
        if blocked != should_block:
            expected = "차단" if should_block else "정상"
            failures.append(f"{label}: {expected} 으로 판정되어야 하는데 {verdict} 으로 나왔습니다")

    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    if failures:
        return 1

    print(f"PASS: 차단 감지 fixture {len(CASES)}건 (차단 4 · 정상 2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
