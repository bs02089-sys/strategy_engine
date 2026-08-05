#!/usr/bin/env bash
# ==================================================================
# swing_local_cron.sh — 로컬 크론 래퍼 (setup_swing_cron.py가 설치)
# ==================================================================
# 용도: GitHub Actions와 병행해 로컬에서도 스윙 봇/성과 평가를 실행.
#   - 실행 전 git pull: GHA가 커밋한 최신 swing_state.json / swing_signals.jsonl 동기화
#   - 모드: bot  = 신호 분석 (4h봉 마감 직후, 잠든 사이 신호 포착용)
#           eval = 성과 평가 (매일, swing_performance.json 저장 + Discord 전송)
#   - 로그: swing_local.log 에 기록 (커밋 대상 아님 — .gitignore 확인)
#   - git push는 하지 않음 (로컬 저장만 — 저장소 반영은 GHA 담당)
# ==================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

MODE="${1:-bot}"
PY=".venv/bin/python"
LOG="swing_local.log"

if [ ! -x "$PY" ]; then
  echo "[$(date '+%F %T')] 오류: $PY 없음 — 가상환경 확인 필요" >> "$LOG"
  exit 1
fi

# ── 1) 최신 상태 동기화 (GHA가 커밋한 상태/저널 수신) ─────────────
git pull --rebase -X theirs >/dev/null 2>&1 || {
  echo "[$(date '+%F %T')] 경고: git pull 실패 — 원격 상태를 못 받았습니다" >> "$LOG"
}

# ── 2) 실행 ──────────────────────────────────────────────────────
case "$MODE" in
  bot)
    "$PY" swing_bot.py >> "$LOG" 2>&1
    ;;
  eval)
    # --since all: 월별 스냅샷 키 일관성 (로컬 실행이 CI 스냅샷을 덮어쓰지 않게)
    "$PY" swing_bot_eval.py --save --since all --discord >> "$LOG" 2>&1
    ;;
  *)
    echo "[$(date '+%F %T')] 오류: 알 수 없는 모드 '$MODE' (bot|eval)" >> "$LOG"
    exit 1
    ;;
esac

# ── 3) 상태 파일은 변경돼도 저장만 (push 없음) ────────────────────
#    저장소 반영은 GitHub Actions 워크플로우가 담당한다.
echo "[$(date '+%F %T')] 완료: $MODE 실행 종료" >> "$LOG"
