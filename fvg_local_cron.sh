#!/usr/bin/env bash
# ==================================================================
# fvg_local_cron.sh — FVG 봇 로컬 크론 래퍼 (setup_fvg_cron.py가 설치)
# ==================================================================
# 용도: 미국 장중(09:30~16:00 ET)에 fvg_signal_bot.py --once 를 매분 실행.
#   - 래퍼에서 ET 장중을 1차 확인(TZ=America/New_York) → 장중 밖에는 파이썬을
#     실행하지 않아 yfinance 요청을 아낀다. 봇 내부 in_trading_session이
#     마지막 봉 기준으로 2차 재검증한다 (이중 방어).
#   - 알림 상태(fvg_alerts.json)를 git으로 원격 동기화 — GitHub Actions 백업과    # 같은 쿨다운 이력을 공유해 교차 중복 알림을 차단한다. commit/push는
    # best-effort — 실패해도 봇 실행은 계속된다.
#   - .env 파일이 있으면 DISCORD_WEBHOOK/DISCORD_USER_ID 등 자동 로드
#   - 로그: fvg_local.log 에 기록 (커밋 대상 아님 — .gitignore)
#   - 수동 실행: ./fvg_local_cron.sh
# ==================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PY=".venv/bin/python"
LOG="fvg_local.log"

if [ ! -x "$PY" ]; then
  echo "[$(date '+%F %T')] 오류: $PY 없음 — 가상환경 확인 필요" >> "$LOG"
  exit 1
fi

# ── 1) 미국 장중(09:30~15:59 ET, 월~금) 1차 확인 ────────────────
#    크론 창(KST 22~06시)은 DST와 무관하게 넉넉히 잡았으므로, 장중 밖 실행은
#    여기서 조용히 종료한다 (파이썬/yfinance 호출 없음). %u: 1=월..7=일
ET_HOUR=0; ET_MIN=0; ET_DOW=0
read -r ET_HOUR ET_MIN ET_DOW <<< "$(TZ='America/New_York' date '+%H %M %u')"
if [ "$ET_DOW" -ge 6 ] \
   || [ "$ET_HOUR" -lt 9 ] || [ "$ET_HOUR" -gt 15 ] \
   || { [ "$ET_HOUR" -eq 9 ] && [ "$ET_MIN" -lt 30 ]; }; then
  exit 0
fi

# ── 2) 알림 상태 동기화 (로컬 크론 ↔ GHA 교차 중복 방지) ──────────
#    fvg_alerts.json을 원격과 동기화해 두 배포가 같은 쿨다운 이력을 공유한다.
#    - 로컬 미커밋 변경이 있으면 pull을 건너뛴다 (충돌 위험 + 매분 경고 로그 스팸 방지)
#    - -X theirs: 충돌 시 원격 버전 유지 — 동시에 기록된 알림 항목이 버려져
#      재알림될 수 있으나, 쿨다운 창(1h) 내로 그쳐 실질 피해는 제한적 (best-effort)
if ! git status --porcelain | grep -q .; then
  if ! git pull --rebase -X theirs >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] 경고: git pull 실패 — 알림 상태를 못 받았습니다" >> "$LOG"
  fi
fi

# ── 3) .env 로드 (있으면) — Discord 웹훅 등 환경변수 주입 ────────
#    set -u 상태에서 소싱하면 .env의 미정의 변수 참조($VAR)로 중단될 수 있어
#    소싱 구간에서만 해제한다. 주석/빈 줄은 grep으로 걸러 소싱한다.
if [ -f .env ]; then
  set +u
  set -a
  # shellcheck disable=SC1091
  . <(grep -vE '^[[:space:]]*(#|$)' .env)
  set +a
  set -u
fi

# ── 4) 실행 ───────────────────────────────────────────────────────
"$PY" fvg_signal_bot.py --once >> "$LOG" 2>&1
echo "[$(date '+%F %T')] 완료 exit=$?" >> "$LOG"

# ── 5) 알림 상태 원격 동기화: fvg_alerts.json commit + push ───────
#    새 알림이 기록된 경우에만 커밋/푸시한다 (변경 없으면 no-op).
#    best-effort: 자격증명이 없으면 실패 로그만 남기고 크론은 계속된다.
if [ -e fvg_alerts.json ]; then
  git add -- fvg_alerts.json
  if ! git diff --cached --quiet; then
    git -c user.name="FVG Local" -c user.email="local@example.com" \
        commit -m "update: fvg-alerts $(date '+%F %T')" >> "$LOG" 2>&1 || \
      echo "[$(date '+%F %T')] 경고: commit 실패" >> "$LOG"
  fi
  git pull --rebase -X theirs >> "$LOG" 2>&1 || true
  git push >> "$LOG" 2>&1 || \
    echo "[$(date '+%F %T')] 경고: push 실패 — HTTPS 자격증명 설정 필요" >> "$LOG"
fi
