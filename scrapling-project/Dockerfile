# ============================================================
# Playwright 공식 이미지 기반 실행 환경
# ============================================================
# 목적: 로컬(WSL 등)에서 libnspr4 / libnss3 / libasound 부재로 실패하던
#       stealthy 모드가, 브라우저 시스템 라이브러리가 갖춰진 환경에서는
#       추가 작업 없이 동작함을 보인다.
#
# 그래서 이 이미지에는 scripts/setup_browser_libs.sh 우회책을 넣지 않는다.
# (main.py 의 .browser-libs 자동 감지도 아무것도 찾지 못하고 그냥 넘어간다)
#
# 빌드:
#   docker build -t scrapling-demo .
# 실행:
#   docker run --rm scrapling-demo                          # stealthy (기본)
#   docker run --rm scrapling-demo python main.py -m static  # 오프라인 모드
#   docker run --rm scrapling-demo python main.py -m all
# ============================================================

# playwright 와 patchright 는 같은 chromium revision(1243)을 사용한다.
# v1.63.0 이미지에는 해당 브라우저와 모든 시스템 라이브러리가 이미 포함되어 있다.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 브라우저 바이너리 보장.
# 공식 이미지의 /ms-playwright 를 그대로 재사용하므로 보통은 재다운로드 없이 통과한다.
# (requirements 로 이미지보다 새로운 playwright 가 설치된 경우에만 내려받는다)
RUN python -m patchright install chromium

COPY main.py ./

# 기본은 stealthy 모드. 인자를 주면 덮어쓸 수 있다.
CMD ["python", "main.py", "-m", "stealthy"]
