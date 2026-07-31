#!/usr/bin/env python3
"""
─────────────────────────────────────────────────────────────
cron-job.org 실시간 ATH DCA 알림 — 설정 자동화 스크립트
─────────────────────────────────────────────────────────────
REALTIME_ALERT_SETUP.md의 수동 설정 절차를 자동화합니다.
cron-job.org REST API(https://api.cron-job.org)를 사용해 GitHub
`repository_dispatch`(event_type: ath-dca-monitor)를 호출하는 크론잡을
만듭니다. 이 워크플로우는 github.com/.../dispatches 엔드포인트를 호출해
GitHub Actions의 `sigma_dca_manager.yml`(--ath-monitor 분기)를 실행시킵니다.

사용 환경변수 (.env 파일 지원 — python-dotenv):
  CRONJOB_ORG_API_KEY   cron-job.org 콘솔 Settings에서 발급한 API 키
  GITHUB_PAT            GitHub PAT (Contents: Read and write)
  GITHUB_OWNER          저장소 소유자 (예: bs02089-sys)
  GITHUB_REPO           저장소 이름 (예: strategy_engine)

선택 환경변수 (기본값 사용 가능):
  GITHUB_EVENT_TYPE     기본값: ath-dca-monitor
  POLL_MINUTES          기본값: 10  (장중 폴링 간격, 5/10/15 권장)
  UTC_HOURS_START       기본값: 13  (UTC, 장중 포함 09:00~17:00 ET 근처)
  UTC_HOURS_END         기본값: 21  (UTC)
  JOB_TITLE             기본값: "ATH DCA realtime monitor"

사용법:
  python setup_cronjob_org.py              # 실제 생성
  python setup_cronjob_org.py --dry-run    # 페이로드만 출력 (API 미호출, 시크릿 마스킹)
  python setup_cronjob_org.py --list       # 기존 잡 목록 조회
  python setup_cronjob_org.py --test-dispatch  # 테스트 dispatch 1회 발사 (워크플로우 실행)
"""
import base64
import copy
import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 없으면 환경변수만 사용


CRONJOB_API_BASE = "https://api.cron-job.org"
GITHUB_API_BASE = "https://api.github.com"
WORKFLOW_PATH = ".github/workflows/sigma_dca_manager.yml"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _github_dispatches_url(owner: str, repo: str) -> str:
    return f"{GITHUB_API_BASE}/repos/{owner}/{repo}/dispatches"


def _github_headers(pat: str) -> dict:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
    }


def _cronjob_headers(cronjob_api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {cronjob_api_key}",
        "Content-Type": "application/json",
    }


def _build_schedule(poll_minutes: int, hours_start: int, hours_end: int) -> dict:
    """장중(월~금, UTC hours_start~hours_end) poll_minutes 분 간격 스케줄."""
    return {
        "timezone": "UTC",
        "expiresAt": 0,
        "hours": list(range(hours_start, hours_end + 1)),
        "mdays": [-1],              # 매일 (요일 필터는 wdays로)
        "minutes": list(range(0, 60, poll_minutes)),
        "months": [-1],             # 매월
        "wdays": [1, 2, 3, 4, 5],   # 월~금 (0=일)
    }


def _build_job_payload(cfg: dict) -> dict:
    """cron-job.org PUT /jobs 페이로드 생성."""
    return {
        "job": {
            "enabled": True,
            "title": cfg["job_title"],
            "url": cfg["dispatches_url"],
            "requestMethod": 1,             # 1 = POST
            "requestTimeout": 30,           # 초 (무료 플랜 최대 30초 — GitHub dispatches는 1초 내 응답)
            "saveResponses": True,
            "extendedData": {
                "headers": {
                    "Authorization": f"Bearer {cfg['github_pat']}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"event_type": cfg["event_type"]}),
            },
            "schedule": cfg["schedule"],
        }
    }


def _redact_secrets(payload: dict) -> dict:
    """시크릿(Authorization 헤더)을 마스킹한 사본 반환 (dry-run 출력용)."""
    redacted = copy.deepcopy(payload)
    for key in redacted.get("job", {}).get("extendedData", {}).get("headers", {}):
        redacted["job"]["extendedData"]["headers"][key] = "***"
    return redacted


def verify_github(owner: str, repo: str, pat: str, event_type: str) -> tuple[bool, str]:
    """부작용 없는 GitHub 검증: PAT 인증 + 워크플로우 트리거 존재 확인.

    dispatch를 실제로 발사하지 않습니다(발사는 --test-dispatch 전용).
    """
    headers = _github_headers(pat)

    # 1) 저장소 접근 + PAT 권한 확인
    resp = requests.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}", headers=headers, timeout=15)
    if resp.status_code == 404:
        return False, f"❌ 저장소를 찾을 수 없거나 접근 권한 없음 (404): {owner}/{repo}"
    if resp.status_code in (401, 403):
        return False, f"❌ GitHub PAT 인증 실패 ({resp.status_code}) — Contents: read/write 권한 확인"
    if resp.status_code != 200:
        return False, f"❌ GitHub API 오류 ({resp.status_code}): {resp.text[:200]}"

    # 2) 워크플로우 파일에 repository_dispatch 트리거 존재 확인
    wf_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{WORKFLOW_PATH}"
    wf_resp = requests.get(wf_url, headers=headers, timeout=15)
    if wf_resp.status_code != 200:
        return False, f"❌ {WORKFLOW_PATH} 파일을 확인할 수 없습니다 ({wf_resp.status_code}) — 최신 코드 푸시 필요"
    try:
        content = base64.b64decode(wf_resp.json().get("content", "")).decode("utf-8", errors="ignore")
    except Exception:
        content = ""
    if "repository_dispatch" not in content or event_type not in content:
        return False, f"❌ 워크플로우에 repository_dispatch({event_type}) 트리거가 없습니다 — 최신 코드 푸시 필요"

    return True, f"✅ GitHub PAT·워크플로우 검증 성공 ({owner}/{repo})"


def test_dispatch(owner: str, repo: str, pat: str, event_type: str) -> None:
    """테스트용 dispatch 1회 실제 발사 (워크플로우 실행됨)."""
    headers = _github_headers(pat)
    url = _github_dispatches_url(owner, repo)
    resp = requests.post(url, headers=headers, json={"event_type": event_type}, timeout=15)
    if resp.status_code == 204:
        print(f"✅ 테스트 dispatch 전송 완료 → GitHub Actions에서 '{event_type}' 실행을 확인하세요.")
    else:
        raise SystemExit(f"❌ 테스트 dispatch 실패 ({resp.status_code}): {resp.text[:300]}")


def list_jobs(cronjob_api_key: str) -> list[dict]:
    """cron-job.org 잡 목록 조회. 성공 시 잡 목록 반환."""
    resp = requests.get(
        f"{CRONJOB_API_BASE}/jobs",
        headers=_cronjob_headers(cronjob_api_key),
        timeout=15,
    )
    if resp.status_code != 200:
        raise SystemExit(f"❌ cron-job.org API 키 인증 실패 ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("jobs", [])


def print_jobs(jobs: list[dict]) -> None:
    if not jobs:
        print("📭 등록된 크론잡이 없습니다.")
        return
    print(f"📋 등록된 크론잡 ({len(jobs)}개):")
    for j in jobs:
        job = j.get("job", {})
        print(
            f"  - [{j.get('jobId')}] {job.get('title', '(제목 없음)')} "
            f"enabled={job.get('enabled')} → {job.get('url', '')}"
        )


def find_existing_job(jobs: list[dict], dispatches_url: str, job_title: str) -> int | None:
    for j in jobs:
        job = j.get("job", {})
        if job.get("url") == dispatches_url and job.get("title") == job_title:
            return j.get("jobId")
    return None


def create_job(cronjob_api_key: str, payload: dict) -> int:
    resp = requests.put(
        f"{CRONJOB_API_BASE}/jobs",
        headers=_cronjob_headers(cronjob_api_key),
        json=payload,
        timeout=15,
    )
    if resp.status_code != 200:
        raise SystemExit(f"❌ 크론잡 생성 실패 ({resp.status_code}): {resp.text[:400]}")
    return resp.json().get("jobId")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    show_list = "--list" in sys.argv
    test_mode = "--test-dispatch" in sys.argv

    owner = _env("GITHUB_OWNER", "<owner>")
    repo = _env("GITHUB_REPO", "<repo>")
    pat = _env("GITHUB_PAT", "<pat>")
    cronjob_key = _env("CRONJOB_ORG_API_KEY", "<key>")
    event_type = _env("GITHUB_EVENT_TYPE", "ath-dca-monitor")
    poll_minutes = int(_env("POLL_MINUTES", "10"))
    hours_start = int(_env("UTC_HOURS_START", "13"))
    hours_end = int(_env("UTC_HOURS_END", "21"))
    job_title = _env("JOB_TITLE", "ATH DCA realtime monitor")

    if not (5 <= poll_minutes <= 60 and 60 % poll_minutes == 0):
        raise SystemExit("❌ POLL_MINUTES는 60의 약수여야 합니다 (예: 5, 6, 10, 12, 15, 20, 30).")
    if hours_start > hours_end:
        raise SystemExit("❌ UTC_HOURS_START는 UTC_HOURS_END보다 작거나 같아야 합니다.")

    if test_mode:
        for key in ("GITHUB_PAT", "GITHUB_OWNER", "GITHUB_REPO"):
            if not _env(key):
                raise SystemExit(f"❌ 환경변수 {key}가 설정되지 않았습니다.")
        test_dispatch(owner, repo, pat, event_type)
        return

    if not dry_run and not show_list:
        for key in ("CRONJOB_ORG_API_KEY", "GITHUB_PAT", "GITHUB_OWNER", "GITHUB_REPO"):
            if not _env(key):
                raise SystemExit(f"❌ 환경변수 {key}가 설정되지 않았습니다.")

    cfg = {
        "dispatches_url": _github_dispatches_url(owner, repo),
        "github_pat": pat,
        "event_type": event_type,
        "job_title": job_title,
        "schedule": _build_schedule(poll_minutes, hours_start, hours_end),
    }
    payload = _build_job_payload(cfg)

    if dry_run:
        print("🔍 [DRY RUN] 생성될 크론잡 페이로드 (시크릿은 ***로 마스킹):")
        print(json.dumps(_redact_secrets(payload), indent=2, ensure_ascii=False))
        print("\n위 내용이 맞다면 `python setup_cronjob_org.py`로 실행하세요.")
        return

    if show_list:
        if not _env("CRONJOB_ORG_API_KEY"):
            raise SystemExit("❌ 환경변수 CRONJOB_ORG_API_KEY가 설정되지 않았습니다.")
        print_jobs(list_jobs(cronjob_key))
        return

    # ── 실제 생성: 검증 → 중복 확인 → 생성 ────────────────────────
    ok, msg = verify_github(owner, repo, pat, event_type)
    print(msg)
    if not ok:
        raise SystemExit("❌ GitHub 검증 실패 — 크론잡을 생성하지 않았습니다.")

    existing = list_jobs(cronjob_key)
    dup_id = find_existing_job(existing, cfg["dispatches_url"], job_title)
    if dup_id is not None:
        print(f"⚠️ 동일한 크론잡이 이미 존재합니다 (jobId={dup_id}). 생성하지 않았습니다.")
        print("   간격/시간 변경 시: cron-job.org 콘솔에서 해당 잡을 수정하거나,")
        print("   먼저 삭제한 뒤 이 스크립트를 다시 실행하세요.")
        print_jobs(existing)
        return

    print("✅ cron-job.org API 키 인증 성공.")
    job_id = create_job(cronjob_key, payload)
    print(f"🎉 크론잡 생성 완료! jobId = {job_id}")
    print("\n✅ 설정 완료! 장중 매 N분마다 ATH DCA 실시간 알림이 실행됩니다.")
    print("   테스트: python setup_cronjob_org.py --test-dispatch")


if __name__ == "__main__":
    main()
