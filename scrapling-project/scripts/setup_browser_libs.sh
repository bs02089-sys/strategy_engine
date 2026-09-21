#!/usr/bin/env bash
# ============================================================
# 브라우저용 시스템 라이브러리를 root 권한 없이 프로젝트 안에 내려받는다.
# ============================================================
# StealthyFetcher / DynamicFetcher 는 Chromium 을 띄우므로 libnspr4, libnss3,
# libasound2 같은 시스템 라이브러리가 필요하다. 보통은 아래 한 줄로 끝난다.
#
#     sudo playwright install-deps chromium
#
# 그런데 컨테이너나 WSL 처럼 root 권한이 없는 환경에서는 apt 설치가 막힌다.
# 이 스크립트는 .deb 를 프로젝트 폴더에 내려받아 풀어 두고, main.py 가
# 실행 시 LD_LIBRARY_PATH 에 자동으로 추가하도록 한다. 시스템은 건드리지 않는다.
#
# 사용법:
#     bash scripts/setup_browser_libs.sh
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="$PROJECT_DIR/.browser-libs"

for cmd in apt-get dpkg-deb; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "오류: '$cmd' 명령을 찾을 수 없습니다. Debian/Ubuntu 계열에서만 동작합니다." >&2
        echo "     다른 배포판이라면 'sudo playwright install-deps chromium' 을 사용하세요." >&2
        exit 1
    fi
done

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> 대상 경로: $LIB_DIR"
echo "==> 임시 경로: $WORK_DIR"

cd "$WORK_DIR"

# libnspr4 / libnss3 는 필수. libasound2 는 오디오 장치가 없는 환경에서도 요구된다.
# Ubuntu 24.04+ 는 libasound2t64 로 이름이 바뀌었으므로 둘 다 시도한다.
REQUIRED=(libnspr4 libnss3)
ASOUND_CANDIDATES=(libasound2t64 libasound2)

echo "==> 패키지 다운로드 (root 권한 불필요)"
for pkg in "${REQUIRED[@]}"; do
    echo "  - $pkg"
    apt-get download "$pkg"
done

for pkg in "${ASOUND_CANDIDATES[@]}"; do
    if apt-get download "$pkg" 2>/dev/null; then
        echo "  - $pkg"
        break
    fi
done

if ! ls ./*.deb >/dev/null 2>&1; then
    echo "오류: 내려받은 패키지가 없습니다. 네트워크와 apt 소스를 확인하세요." >&2
    exit 1
fi

echo "==> 압축 해제"
rm -rf "$LIB_DIR"
mkdir -p "$LIB_DIR"
for deb in ./*.deb; do
    echo "  - $(basename "$deb")"
    dpkg-deb -x "$deb" "$LIB_DIR"
done

# 라이브러리는 보통 usr/lib/<multiarch-triple>/ 에 들어간다.
# (dpkg --print-architecture 는 'amd64' 를 돌려주지만 디렉터리 이름은
#  'x86_64-linux-gnu' 이므로 triple 이름을 직접 찾는다.)
TARGET_DIR=""
for candidate in "$LIB_DIR"/usr/lib/*-linux-gnu; do
    if [ -d "$candidate" ]; then
        TARGET_DIR="$candidate"
        break
    fi
done
if [ -z "$TARGET_DIR" ]; then
    TARGET_DIR="$LIB_DIR/usr/lib"
fi

echo "==> 결과: $TARGET_DIR"
ls "$TARGET_DIR"/*.so* 2>/dev/null | xargs -n1 basename | sed 's/^/    /'

echo
echo "완료. 이제 './.venv/bin/python main.py -m stealthy' 가 동작해야 합니다."
echo "(main.py 가 $LIB_DIR 를 자동으로 감지해 LD_LIBRARY_PATH 에 추가합니다)"
