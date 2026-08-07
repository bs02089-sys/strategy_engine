#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==================================================================
 setup_fvg_cron.py — FVG 봇 로컬 크론 설정 자동화
==================================================================
미국 장중(09:30~16:00 ET)에만 `fvg_signal_bot.py --once`를 자동 실행한다.
기존 크론 항목은 보존하고, 이 스크립트가 설치하는 항목에만
'# fvg-local' 마커를 달아 식별한다 (반복 설치 시 중복 방지).

설치되는 크론 (KST, Asia/Seoul 기준 — 장중 포함 넉넉한 창):
  - 매분(기본) KST 22:00~05:59, 월~토 (FVG_POLL_MINUTES로 간격 조절 가능)
    * 미국 장중 ET 09:30~16:00 = KST 22:30~05:00(여름·EDT) /
      23:30~06:00(겨울·EST) — 시간 창 22~05시(22:00~05:59)가 두 계절 모두
      커버하므로 DST 적용 여부와 무관하게 KST 22:30/23:30에 자동 시작된다.
      (Vixie cron은 CRON_TZ 미지원 — 넉넉한 KST 창 + 래퍼의 ET 재확인이 유일한 방법)
    * 요일 1-6(월~토): KST 금요일 저녁~토요일 새벽 = ET 금요일 장중이라
      KST 토요일 실행이 있어야 ET 금요일 오후가 커버된다. 결과적으로
      **ET 기준 월~금만** 실행되며, 래퍼가 ET 요일/시간(09:30~15:59)을
      2차 재확인해 장중 밖 실행은 스킵한다.
  - 래퍼: fvg_local_cron.sh (가상환경 실행 + .env 로드 + 알림 상태 git 동기화 +
    fvg_local.log 기록) — fvg_alerts.json(중복 알림 방지 상태, git 추적 대상)을
    원격과 공유해 GitHub Actions 백업(fvg_signal.yml)과 교차 중복을 차단한다.

사용법:
  python3 setup_fvg_cron.py --dry-run    # 설치될 크론 라인만 출력 (미설치)
  python3 setup_fvg_cron.py              # 실제 설치
  python3 setup_fvg_cron.py --list       # 현재 크론 전체 목록
  python3 setup_fvg_cron.py --remove     # 이 스크립트가 설치한 항목만 제거

환경변수:
  FVG_POLL_MINUTES   폴링 간격(분) — 60의 약수 (기본 1 = 매분)
==================================================================
"""
import os
import subprocess
import sys

MARKER = "# fvg-local"
REPO_DIR = "/home/bs020/projects/strategy_engine"
WRAPPER = f"{REPO_DIR}/fvg_local_cron.sh"


def _minute_expr(poll_minutes: int) -> str:
  """폴링 간격 → cron 분 표현 (1 = 매분 '*', 그 외 '*/N')."""
  return "*" if poll_minutes == 1 else f"*/{poll_minutes}"


# KST 장중 포함 창: 22:00~05:59 (22,23,0,1,2,3,4,5시), 월~토(1-6)
# 봇 내부 세션 필터가 09:30~15:59 ET를 정확히 재검증하므로 창은 넉넉히.
def build_cron_line(poll_minutes: int) -> str:
  minute = _minute_expr(poll_minutes)
  return f"{minute} 22-23,0-5 * * 1-6 {WRAPPER} {MARKER}"


def get_crontab():
  """현재 crontab 내용(문자열).

  - "no crontab for <user>"(정상): 빈 문자열 반환
  - 그 외 오류(데몬 문제 등): SystemExit — 기존 크론을 덮어쓰지 않도록 중단
  """
  proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
  if proc.returncode == 0:
    return proc.stdout
  if "no crontab" in proc.stderr.lower():
    return ""
  raise SystemExit(f"❌ crontab 조회 실패: {proc.stderr.strip()}")


def set_crontab(content):
  """crontab 재설정 (표준입력으로 전달)."""
  proc = subprocess.run(
      ["crontab", "-"], input=content, capture_output=True, text=True
  )
  if proc.returncode != 0:
    raise SystemExit(f"❌ crontab 설정 실패: {proc.stderr.strip()}")


def main():
  dry_run = "--dry-run" in sys.argv
  show_list = "--list" in sys.argv
  remove_mode = "--remove" in sys.argv

  try:
    poll_minutes = int(os.getenv("FVG_POLL_MINUTES", "1"))
  except ValueError:
    raise SystemExit("❌ FVG_POLL_MINUTES는 정수여야 합니다 (예: 1, 2, 5, 10, 15).")
  if not (1 <= poll_minutes <= 60 and 60 % poll_minutes == 0):
    raise SystemExit("❌ FVG_POLL_MINUTES는 60의 약수여야 합니다 (예: 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30).")

  cron_line = build_cron_line(poll_minutes)
  current = get_crontab()
  lines = [ln for ln in current.splitlines() if ln.strip()]

  if show_list:
    print("📋 현재 crontab:")
    print("\n".join(lines) if lines else "  (비어 있음)")
    return

  if dry_run:
    print("🔍 [DRY RUN] 설치될 크론 라인 (KST 기준 — 미국 장중 매분 포함 창):")
    print(f"  {cron_line}")
    print("\n  DST 대응 매핑 (한국시간 KST 기준):")
    print("    - 여름(EDT): ET 09:30~15:59 = KST 22:30~04:59 → 시간 창 22~05시가 커버")
    print("    - 겨울(EST): ET 09:30~15:59 = KST 23:30~05:59 → 시간 창 22~05시가 커버")
    print("    - 요일 1-6(KST): KST 금~토 새벽 = ET 금요일 장중 → ET 기준 월~금만 실행")
    print("      (래퍼가 ET 요일/시간 09:30~15:59를 2차 재확인 — 장중 밖은 스킵)")
    print("\n위 내용이 맞다면 `python3 setup_fvg_cron.py`로 설치하세요.")
    return

  if remove_mode:
    kept = [ln for ln in lines if MARKER not in ln]
    if len(kept) == len(lines):
      print("ℹ️ 제거할 fvg-local 크론 항목이 없습니다.")
      return
    set_crontab("\n".join(kept) + ("\n" if kept else ""))
    print(f"✅ fvg-local 크론 {len(lines) - len(kept)}개 제거 완료.")
    return

  # ── 설치: 기존 항목 보존 + MARKER 중복 제거 후 신규 추가 ────────
  kept = [ln for ln in lines if MARKER not in ln]
  new_lines = kept + [cron_line]
  set_crontab("\n".join(new_lines) + "\n")
  print("✅ 로컬 크론 설치 완료 (KST 기준 — 미국 장중 매분 실행, 봇이 세션을 재검증):")
  print(f"  {cron_line}")
  print("확인: python3 setup_fvg_cron.py --list")


if __name__ == "__main__":
  main()
