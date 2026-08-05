#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==================================================================
 setup_swing_cron.py — 스윙 봇/평가 로컬 크론 설정 자동화
==================================================================
GitHub Actions와 병행해 로컬에서도 스윙 봇과 성과 평가를 자동 실행한다.
기존 크론 항목은 보존하고, 이 스크립트가 설치하는 항목에만
'# swing-local' 주석을 달아 식별한다 (반복 설치 시 중복 방지).

설치되는 크론 (KST, Asia/Seoul 기준):
  - 봇: cron-job.org(swing-bot dispatch)가 담당 — 로컬 크론에서는 제외
    * 4h봉 신호 감지는 클라우드(GHA)에서 실행되어 이 PC가 꺼져 있어도 동작
    * 설정: python3 setup_cronjob_org.py --swing
  - 평가: 매일 08:00 (전날 신호 기준 성과 스냅샷 저장 + Discord 전송)
  - 래퍼: swing_local_cron.sh eval (실행 전 git pull로 GHA 상태 동기화)

사용법:
  python3 setup_swing_cron.py --dry-run    # 설치될 크론 라인만 출력 (미설치)
  python3 setup_swing_cron.py              # 실제 설치
  python3 setup_swing_cron.py --list       # 현재 크론 전체 목록
  python3 setup_swing_cron.py --remove     # 이 스크립트가 설치한 항목만 제거
==================================================================
"""
import subprocess
import sys

MARKER = "# swing-local"
REPO_DIR = "/home/bs020/projects/strategy_engine"
WRAPPER = f"{REPO_DIR}/swing_local_cron.sh"

# 봇 신호 감지는 cron-job.org → GitHub Actions(repository_dispatch: swing-bot)가 담당 —
# 로컬 크론은 성과 평가(매일 08:00 KST)만 설치한다.
CRON_EVAL = (
    f"0 8 * * * {WRAPPER} eval {MARKER}"
)
CRON_LINES = [CRON_EVAL]


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

  current = get_crontab()
  lines = [ln for ln in current.splitlines() if ln.strip()]

  if show_list:
    print("📋 현재 crontab:")
    print("\n".join(lines) if lines else "  (비어 있음)")
    return

  if dry_run:
    print("🔍 [DRY RUN] 설치될 크론 라인 (KST 기준):")
    for ln in CRON_LINES:
      print(f"  {ln}")
    print("\n위 내용이 맞다면 `python3 setup_swing_cron.py`로 설치하세요.")
    return

  if remove_mode:
    kept = [ln for ln in lines if MARKER not in ln]
    if len(kept) == len(lines):
      print("ℹ️ 제거할 swing-local 크론 항목이 없습니다.")
      return
    set_crontab("\n".join(kept) + ("\n" if kept else ""))
    print(f"✅ swing-local 크론 {len(lines) - len(kept)}개 제거 완료.")
    return

  # ── 설치: 기존 항목 보존 + MARKER 중복 제거 후 신규 추가 ────────
  kept = [ln for ln in lines if MARKER not in ln]
  new_lines = kept + CRON_LINES
  set_crontab("\n".join(new_lines) + "\n")
  print("✅ 로컬 크론 설치 완료 (KST 기준):")
  print(f"  {CRON_EVAL}")
  print("확인: python3 setup_swing_cron.py --list")


if __name__ == "__main__":
  main()
