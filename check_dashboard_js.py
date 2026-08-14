#!/usr/bin/env python3
"""대시보드 인라인 JS strict 검사용 추출 스크립트.

swing_alerter.py 의 Python 문자열 상수(_AUTO_RELOAD_JS/_PUSH_SDK/_PLAN_JS)에
담긴 <script> 블록을 떼어내 .typecheck/ 아래 .js 파일로 저장한다.
tsc 는 HTML 인라인 스크립트를 직접 읽지 못하므로, 검사 전에 이 스크립트로
추출한 뒤 tsconfig.dashboard.json(tsc -p)로 검사한다. (npm run typecheck 에 포함)
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "swing_alerter.py"
OUT_DIR = ROOT / ".typecheck"

# (Python 상수명, 추출 후 파일명) — 대시보드 HTML에 들어가는 <script> 블록 전부
BLOCKS = [
    ("_AUTO_RELOAD_JS", "auto_reload.js"),
    ("_PUSH_SDK", "push_sdk.js"),
    ("_PLAN_JS", "plan_js.js"),
]

_PATTERN = re.compile(r'"""(.*?)"""', re.S)


def extract(name: str) -> str:
    """상수 문자열에서 <script> … </script> 안쪽 JS 코드만 추출."""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(rf'^{name} = """(.*?)"""', src, re.S | re.M)
    if not m:
        raise SystemExit(f"❌ {name} 상수를 swing_alerter.py 에서 찾지 못함")
    body = m.group(1)
    # 모든 <script …> / </script> 태그와 앞뒤 공백 제거
    # (_PUSH_SDK 는 CDN <script src> 태그 + 코드 블록 <script> 2개로 구성)
    inner = re.sub(r"<script[^>]*>|</script>", "", body)
    return inner.strip()


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for name, fname in BLOCKS:
        js = extract(name)
        (OUT_DIR / fname).write_text(js, encoding="utf-8")
        print(f"✅ {fname} ({name}, {len(js)}자)")


if __name__ == "__main__":
    main()
