# ============================================================
# 슬림 베이스 이미지 (python:3.14-slim-bookworm)
# ============================================================
# 이전에는 mcr.microsoft.com/playwright/python 이미지를 썼다. 브라우저 시스템
# 라이브러리가 다 들어 있어 편했지만 2.74GB 로 컸다. 그 이미지는 firefox 와
# webkit, 그리고 그들의 의존성까지 포함하는데 이 프로젝트는 chromium 하나만 쓴다.
#
# 그래서 slim 베이스에 필요한 것만 설치한다. 브라우저 바이너리와 시스템
# 라이브러리는 `patchright install --with-deps chromium` 한 줄이 처리한다.
#
# 참고: 이 이미지에는 이 프로젝트의 로컬 우회책(.browser-libs)이 들어가지 않는다.
#
# 빌드:
#   docker build -t scrapling-demo .
# 실행:
#   docker run --rm scrapling-demo                            # stealthy (기본)
#   docker run --rm scrapling-demo python main.py -m static    # 오프라인 모드
#   docker run --rm scrapling-demo python main.py -m all
# ============================================================

# bookworm(Debian 12)을 쓰는 이유: Playwright/patchright 의 --with-deps 가
# 지원하는 배포판 목록에 들어 있다. (trixie 등 최신 배포판은 목록에 없을 수 있다)
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# chromium 바이너리 + 실행에 필요한 시스템 라이브러리를 함께 설치한다.
# --with-deps 가 apt 설치까지 처리하므로 slim 베이스에서도 동작한다.
# 같은 레이어에서 apt 캐시를 지워 이미지 크기를 줄인다.
RUN python -m patchright install --with-deps --no-progress chromium \
 && rm -rf /var/lib/apt/lists/*

COPY main.py ./

# 기본은 stealthy 모드. 인자를 주면 덮어쓸 수 있다.
CMD ["python", "main.py", "-m", "stealthy"]
