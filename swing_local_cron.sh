#!/usr/bin/env bash
# ==================================================================
# swing_local_cron.sh — 로컬 크론 래퍼 (setup_swing_cron.py가 설치)
# ==================================================================
# 용도: 성과 평가를 로컬에서 매일 실행 (봇 신호 감지는 cron-job.org →
#       GitHub Actions가 담당하므로 이 래퍼는 평가 전용).
#   - 실행 전 git pull: GHA가 커밋한 최신 상태/저널 동기화
#   - 실행: swing_bot_eval.py --save --since all --discord
#   - 실행 후 commit + push: 상태/저널/성과 파일을 원격과 완전 동기화
#     (best-effort — 자격증명이 없으면 실패 로그만 남기고 계속)
#   - 로그: swing_local.log 에 기록 (커밋 대상 아님 — .gitignore 확인)
#   - 수동 실행: ./swing_local_cron.sh
# ==================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PY=".venv/bin/python"
LOG="swing_local.log"

if [ ! -x "$PY" ]; then
  echo "[$(date '+%F %T')] 오류: $PY 없음 — 가상환경 확인 필요" >> "$LOG"
  exit 1
fi

# ── 1) 최신 상태 동기화 (GHA가 커밋한 상태/저널 수신) ─────────────
#    주의: 로컬에 미커밋 변경이 있으면 pull이 거부된다 — 평가는 계속
#    실행되지만 원격 저널을 못 받을 수 있다. 상태가 중요하면 로컬 변경을
#    먼저 커밋할 것.
if ! git pull --rebase -X theirs >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain)" ]; then
    echo "[$(date '+%F %T')] 경고: git pull 실패 — 로컬 미커밋 변경이 있습니다" >> "$LOG"
    echo "  (원격 상태 동기화를 위해 로컬 변경을 커밋하세요. 평가는 기존 저널로 계속 실행)" >> "$LOG"
  else
    echo "[$(date '+%F %T')] 경고: git pull 실패 — 원격 상태를 못 받았습니다" >> "$LOG"
  fi
fi

# ── 2) 성과 평가 실행 ─────────────────────────────────────────────
# --since all: 월별 스냅샷 키 일관성 (로컬 실행이 CI 스냅샷을 덮어쓰지 않게)
"$PY" swing_bot_eval.py --save --since all --discord >> "$LOG" 2>&1

# ── 3) 원격 완전 동기화: 상태/저널/성과 파일 commit + push ────────
#    GHA 워크플로우가 커밋하는 파일과 동일하게만 취급한다 (소스 코드 미포함).
#    best-effort: HTTPS 자격증명이 없으면 push 실패 로그만 남기고 크론은 계속.
#    주의: 로컬에 미커밋 변경이 있으면 pull이 거부된 상태라 push도 실패할 수
#    있다 — 완전 동기화를 원하면 로컬 변경을 먼저 커밋할 것.
#    커밋 신원은 -c로 일회성 적용 (로컬 .git/config를 영구히 덮어쓰지 않게)
#    주의: 존재하지 않는 파일을 git add에 넣으면 pathspec 오류로 add 전체가
#    중단된다 → 추적 중(삭제 포함)이거나 실제 존재하는 파일만 add.
for f in swing_state.json swing_signals.jsonl swing_performance.json; do
  if git ls-files --error-unmatch -- "$f" >/dev/null 2>&1 || [ -e "$f" ]; then
    git add -- "$f"
  fi
done

if ! git diff --cached --quiet; then
  git -c user.name="Swing Local" -c user.email="local@example.com" \
      commit -m "update: swing-local $(date '+%F %T')" >> "$LOG" 2>&1 || \
    echo "[$(date '+%F %T')] 경고: commit 실패" >> "$LOG"
fi

# -X theirs: 충돌 시 원격(사람) 버전 유지 — 스테일 로컬 상태는 버리고 다음 실행에서 재생성
if ! git pull --rebase -X theirs >> "$LOG" 2>&1; then
  echo "[$(date '+%F %T')] 경고: pull 실패 — 로컬 변경/네트워크 확인" >> "$LOG"
fi
if ! git push >> "$LOG" 2>&1; then
  echo "[$(date '+%F %T')] 경고: push 실패 — HTTPS 자격증명 설정 필요 (credential helper)" >> "$LOG"
fi

echo "[$(date '+%F %T')] 완료: eval 실행 종료" >> "$LOG"
