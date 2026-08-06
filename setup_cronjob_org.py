#!/usr/bin/env python3
"""
─────────────────────────────────────────────────────────────
cron-job.org 실시간 ATH DCA 알림 — 설정 자동화 스크립트
─────────────────────────────────────────────────────────────
REALTIME_ALERT_SETUP.md의 수동 설정 절차를 자동화합니다.
cron-job.org REST API(https://api.cron-job.org)를 사용해 GitHub
`repository_dispatch`(event_type: ath-dca-monitor)를 호출하는 크론잡을
만듭니다. 이 워크플로우는 github.com/.../dispatches 엔드포인트를 호출해
GitHub Actions의 `dca_ma_strategy.yml`(--ath-monitor 분기)를 실행시킵니다.

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
  python setup_cronjob_org.py              # ATH DCA 실시간 모니터 잡 생성 (기본)
  python setup_cronjob_org.py --swing      # 스윙 봇 잡 생성 (미국 장중 15분 폴링, 월~금)
  python setup_cronjob_org.py --dry-run    # 페이로드만 출력 (API 미호출, 시크릿 마스킹)
  python setup_cronjob_org.py --list       # 기존 잡 목록 조회
  python setup_cronjob_org.py --test-dispatch  # 테스트 dispatch 1회 발사 (워크플로우 실행)
  python setup_cronjob_org.py --update-pat # 크론잡에 저장된 GITHUB_PAT를 새 토큰으로 갱신
  python setup_cronjob_org.py --update-schedule  # 크론잡 폴링 간격 갱신 (POLL_MINUTES/UTC_HOURS 반영)

스윙 봇 잡 (--swing):
  - 이벤트: repository_dispatch(event_type: swing-bot) → swing_bot.yml 실행
  - 스케줄: 미국 장중 15분 간격 폴링 (기본 UTC 13~22시, 월~금) — 신호는 완성
    4h봉 기준이므로 봉이 닫히는 순간(첫 봉 13:30 ET / 장 마감 봉 16:00 ET)을
    놓치지 않고 15분 이내 감지해 실시간 알림한다. 상태 머신(swing_state.json)이
    중복 BUY/SELL 알림을 차단하므로 폴링 빈도와 무관하게 스팸이 없다.
  - 선택 환경변수: SWING_POLL_MINUTES(기본 15), SWING_UTC_HOURS_START(기본 13),
    SWING_UTC_HOURS_END(기본 22) — 변경 후 `--swing --update-schedule`로 반영
  - 관련 스크립트: setup_swing_cron.py (로컬 크론 — 평가 전용)
"""
import base64
import copy
import datetime as _dt
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
WORKFLOW_PATH = ".github/workflows/dca_ma_strategy.yml"

# 스윙 봇 잡 전용 설정
SWING_WORKFLOW_PATH = ".github/workflows/swing_bot.yml"
SWING_EVENT_TYPE = "swing-bot"
SWING_JOB_TITLE = "Swing Bot 4h bar monitor"

# cron-job.org jobDetails에 포함된 응답 전용(읽기 전용) 필드 — PATCH 시 제거해야 400을 피한다
READONLY_JOB_FIELDS = (
    "jobId",
    "lastStatus",
    "lastDuration",
    "lastExecution",
    "nextExecution",
    "sslCertExpiry",
    "someFailed",
)

# cron-job.org lastStatus 코드 → 사람이 읽는 상태 문구 (공식 문서 기준)
# 0=미실행, 1=OK(성공), 2~9=실패 사유별
LAST_STATUS_TEXT = {
    0: "미실행",
    1: "성공(OK)",
    2: "실패(DNS 오류)",
    3: "실패(호스트 연결 불가)",
    4: "실패(HTTP 오류 4xx/5xx)",
    5: "실패(타임아웃)",
    6: "실패(응답 데이터 과다)",
    7: "실패(잘못된 URL)",
    8: "실패(내부 오류)",
    9: "실패(알 수 없는 이유)",
}


def _fmt_epoch(ts) -> str:
    """epoch 초 → 'YYYY-MM-DD HH:MM' (시스템 로컬 시간대 — 사용자 PC 기준 KST) 문자열. 없으면 '-'."""
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _strip_readonly_fields(job_body: dict) -> None:
    """jobDetails의 읽기 전용 필드를 제거 (in-place)."""
    for _readonly in READONLY_JOB_FIELDS:
        job_body.pop(_readonly, None)


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


def _build_swing_schedule(poll_minutes: int, hours_start: int, hours_end: int) -> dict:
    """스윙 봇 전용 스케줄: 미국 장중 poll_minutes 분 간격 폴링, 월~금.

    신호는 완성 4h봉 기준이라 봉이 닫히는 순간(첫 봉 13:30 ET / 장 마감 봉
    16:00 ET)을 놓치지 않도록 장중 내내 자주 실행한다. 기본 UTC 13~22시는
    미국 장중(09:30~16:00 ET)을 여름/겨울 모두 포함하며, 상태 머신이 중복
    알림을 차단하므로 BUY/SELL Discord 발송은 신호 전환 시에만 일어난다.
    """
    return _build_schedule(poll_minutes, hours_start, hours_end)


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


def verify_github(owner: str, repo: str, pat: str, event_type: str,
                  workflow_path: str = WORKFLOW_PATH) -> tuple[bool, str]:
    """부작용 없는 GitHub 검증: PAT 인증 + 워크플로우 트리거 존재 확인.

    dispatch를 실제로 발사하지 않습니다(발사는 --test-dispatch 전용).
    workflow_path: 검증할 워크플로우 파일 (기본 dca_ma_strategy.yml,
    --swing 모드에선 swing_bot.yml).
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
    wf_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{workflow_path}"
    wf_resp = requests.get(wf_url, headers=headers, timeout=15)
    if wf_resp.status_code != 200:
        return False, f"❌ {workflow_path} 파일을 확인할 수 없습니다 ({wf_resp.status_code}) — 최신 코드 푸시 필요"
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
        status_code = j.get("lastStatus")
        status_txt = LAST_STATUS_TEXT.get(status_code, f"알 수 없음({status_code})")
        print(
            f"  - [{j.get('jobId')}] {j.get('title', '(제목 없음)')} "
            f"enabled={j.get('enabled')} → {j.get('url', '')}"
        )
        print(
            f"      마지막 실행: {_fmt_epoch(j.get('lastExecution'))} "
            f"| 상태: {status_txt} "
            f"| 소요: {j.get('lastDuration', '-')}ms"
        )
        print(f"      다음 실행: {_fmt_epoch(j.get('nextExecution'))}")


def find_existing_job(jobs: list[dict], dispatches_url: str, job_title: str) -> int | None:
    """목록 응답에서 URL+제목이 일치하는 잡의 jobId 반환 (없으면 None).

    cron-job.org의 GET /jobs 응답은 평면 구조(jobId/url/title이 최상위)라
    j["job"] 중첩 없이 직접 접근한다.
    """
    for j in jobs:
        if j.get("url") == dispatches_url and j.get("title") == job_title:
            return j.get("jobId")
    return None


def get_job(cronjob_api_key: str, job_id: int) -> dict:
    """cron-job.org 단일 잡 상세 조회 (GET /jobs/{jobId}).

    목록(GET /jobs) 응답에는 extendedData(헤더/바디)가 포함되지 않으므로,
    --update-pat처럼 기존 헤더를 보존한 채 갱신하려면 반드시 상세 조회가 필요.
    응답의 jobDetails 객체를 반환한다.
    """
    resp = requests.get(
        f"{CRONJOB_API_BASE}/jobs/{job_id}",
        headers=_cronjob_headers(cronjob_api_key),
        timeout=15,
    )
    if resp.status_code != 200:
        raise SystemExit(f"❌ 크론잡 상세 조회 실패 ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("jobDetails", {})


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


def update_job(cronjob_api_key: str, job_id: int, job_body: dict) -> None:
    """cron-job.org 기존 잡 갱신 (PATCH /jobs/{jobId}).

    cron-job.org API에서 기존 잡 갱신은 PUT이 아니라 **PATCH**만 허용한다
    (PUT /jobs는 신규 생성 전용 — PUT /jobs/{jobId}는 404를 반환).
    --update-pat 모드에서 사용 — 기존 잡의 job 바디를 그대로 받아
    Authorization 헤더만 새 PAT로 교체한 뒤 갱신합니다. 스케줄/제목/URL/
    타임아웃 등 다른 설정은 완전히 보존됩니다.
    """
    resp = requests.patch(
        f"{CRONJOB_API_BASE}/jobs/{job_id}",
        headers=_cronjob_headers(cronjob_api_key),
        json={"job": job_body},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SystemExit(f"❌ 크론잡 갱신 실패 ({resp.status_code}): {resp.text[:400]}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    show_list = "--list" in sys.argv
    test_mode = "--test-dispatch" in sys.argv
    update_pat_mode = "--update-pat" in sys.argv
    update_schedule_mode = "--update-schedule" in sys.argv
    swing_mode = "--swing" in sys.argv
    mode_flag = " --swing" if swing_mode else ""

    owner = _env("GITHUB_OWNER", "<owner>")
    repo = _env("GITHUB_REPO", "<repo>")
    pat = _env("GITHUB_PAT", "<pat>")
    cronjob_key = _env("CRONJOB_ORG_API_KEY", "<key>")

    if swing_mode:
        # ── 스윙 봇 잡 전용 설정 (미국 장중 15분 폴링) ────────────────
        event_type = SWING_EVENT_TYPE
        job_title = SWING_JOB_TITLE
        workflow_path = SWING_WORKFLOW_PATH
        poll_minutes = int(_env("SWING_POLL_MINUTES", "15"))
        hours_start = int(_env("SWING_UTC_HOURS_START", "13"))
        hours_end = int(_env("SWING_UTC_HOURS_END", "22"))
        schedule = _build_swing_schedule(poll_minutes, hours_start, hours_end)
        job_desc = f"스윙 봇 실시간 모니터 (장중 매 {poll_minutes}분, UTC {hours_start}~{hours_end}시)"
    else:
        # ── ATH DCA 실시간 모니터 잡 (장중 N분 폴링) ──────────────────
        event_type = _env("GITHUB_EVENT_TYPE", "ath-dca-monitor")
        poll_minutes = int(_env("POLL_MINUTES", "10"))
        hours_start = int(_env("UTC_HOURS_START", "13"))
        hours_end = int(_env("UTC_HOURS_END", "21"))
        job_title = _env("JOB_TITLE", "ATH DCA realtime monitor")
        workflow_path = WORKFLOW_PATH
        schedule = _build_schedule(poll_minutes, hours_start, hours_end)
        job_desc = f"장중 매 {poll_minutes}분 ATH DCA 실시간 알림"

    if not (5 <= poll_minutes <= 60 and 60 % poll_minutes == 0):
        raise SystemExit("❌ POLL_MINUTES/SWING_POLL_MINUTES는 60의 약수여야 합니다 (예: 5, 6, 10, 12, 15, 20, 30).")
    if hours_start > hours_end:
        raise SystemExit("❌ UTC_HOURS_START/SWING_UTC_HOURS_START는 END보다 작거나 같아야 합니다.")

    if test_mode:
        for key in ("GITHUB_PAT", "GITHUB_OWNER", "GITHUB_REPO"):
            if not _env(key):
                raise SystemExit(f"❌ 환경변수 {key}가 설정되지 않았습니다.")
        test_dispatch(owner, repo, pat, event_type)
        return

    if update_pat_mode:
        # ── 크론잡에 저장된 GITHUB_PAT 갱신 ──────────────────────
        for key in ("CRONJOB_ORG_API_KEY", "GITHUB_PAT", "GITHUB_OWNER", "GITHUB_REPO"):
            if not _env(key):
                raise SystemExit(f"❌ 환경변수 {key}가 설정되지 않았습니다.")

        # 1) 새 PAT가 유효한지 먼저 검증 (죽은 토큰을 크론잡에 기록하지 않도록)
        ok, msg = verify_github(owner, repo, pat, event_type, workflow_path)
        print(msg)
        if not ok:
            raise SystemExit("❌ 새 GitHub PAT 검증 실패 — 크론잡을 갱신하지 않았습니다.")

        # 2) 기존 잡 찾기 (URL+제목 매칭) — 목록은 평면 구조(jobId/url/title 최상위)
        dispatches_url = _github_dispatches_url(owner, repo)
        existing = list_jobs(cronjob_key)
        job_id = find_existing_job(existing, dispatches_url, job_title)
        if job_id is None:
            raise SystemExit(
                f"❌ 갱신할 크론잡을 찾을 수 없습니다: {dispatches_url} "
                f"(제목: {job_title}). 먼저 `python setup_cronjob_org.py{mode_flag}`로 생성하세요."
            )

        # 3) 목록 응답에는 extendedData(헤더)가 없으므로 단일 잡 상세를
        #    조회해 기존 job 바디를 그대로 가져온 뒤 Authorization만 교체한다.
        #    → 스케줄/타임아웃/제목/URL/saveResponses 완전 보존
        job_body = get_job(cronjob_key, job_id)
        if not job_body:
            raise SystemExit("❌ 크론잡 상세 조회 결과가 비어 있습니다 — 갱신을 중단합니다.")
        # jobDetails에는 응답 전용(읽기 전용) 필드가 포함되어 있으므로
        # PATCH 전에 제거 — 포함된 채 보내면 cron-job.org가 400으로 거부 가능
        _strip_readonly_fields(job_body)
        headers = job_body.setdefault("extendedData", {}).setdefault("headers", {})
        headers["Authorization"] = f"Bearer {pat}"
        update_job(cronjob_key, job_id, job_body)
        print(f"✅ 크론잡(jobId={job_id})의 GITHUB_PAT를 새 토큰으로 갱신했습니다.")
        print("   테스트: python setup_cronjob_org.py --test-dispatch")
        return

    if update_schedule_mode:
        # ── 크론잡 폴링 간격(스케줄) 갱신 ──────────────────────────
        # PAT는 불필요 (GitHub API 호출 없음) — 크론잡 위치 확인에
        # 필요한 GITHUB_OWNER/REPO + CRONJOB_ORG_API_KEY만 요구한다.
        for key in ("CRONJOB_ORG_API_KEY", "GITHUB_OWNER", "GITHUB_REPO"):
            if not _env(key):
                raise SystemExit(f"❌ 환경변수 {key}가 설정되지 않았습니다.")

        # 1) 기존 잡 찾기 (URL+제목 매칭)
        dispatches_url = _github_dispatches_url(owner, repo)
        existing = list_jobs(cronjob_key)
        job_id = find_existing_job(existing, dispatches_url, job_title)
        if job_id is None:
            raise SystemExit(
                f"❌ 갱신할 크론잡을 찾을 수 없습니다: {dispatches_url} "
                f"(제목: {job_title}). 먼저 `python setup_cronjob_org.py{mode_flag}`로 생성하세요."
            )

        # 2) 기존 job 바디를 그대로 가져와 schedule만 교체한다
        #    → Authorization 헤더/제목/URL/타임아웃/saveResponses 완전 보존
        job_body = get_job(cronjob_key, job_id)
        if not job_body:
            raise SystemExit("❌ 크론잡 상세 조회 결과가 비어 있습니다 — 갱신을 중단합니다.")
        _strip_readonly_fields(job_body)
        job_body["schedule"] = schedule
        update_job(cronjob_key, job_id, job_body)
        print(f"✅ 크론잡(jobId={job_id})의 스케줄을 갱신했습니다: {job_desc}")
        print("   테스트: python3 setup_cronjob_org.py --test-dispatch")
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
        "schedule": schedule,
    }
    payload = _build_job_payload(cfg)

    if dry_run:
        print(f"🔍 [DRY RUN] 생성될 크론잡 페이로드 ({job_desc}, 시크릿 *** 마스킹):")
        print(json.dumps(_redact_secrets(payload), indent=2, ensure_ascii=False))
        print(f"\n위 내용이 맞다면 `python setup_cronjob_org.py{mode_flag}`로 실행하세요.")
        return

    if show_list:
        if not _env("CRONJOB_ORG_API_KEY"):
            raise SystemExit("❌ 환경변수 CRONJOB_ORG_API_KEY가 설정되지 않았습니다.")
        print_jobs(list_jobs(cronjob_key))
        return

    # ── 실제 생성: 검증 → 중복 확인 → 생성 ────────────────────────
    ok, msg = verify_github(owner, repo, pat, event_type, workflow_path)
    print(msg)
    if not ok:
        raise SystemExit("❌ GitHub 검증 실패 — 크론잡을 생성하지 않았습니다.")

    existing = list_jobs(cronjob_key)
    dup_id = find_existing_job(existing, cfg["dispatches_url"], job_title)
    if dup_id is not None:
        print(f"⚠️ 동일한 크론잡이 이미 존재합니다 (jobId={dup_id}). 생성하지 않았습니다.")
        print("   스케줄 변경 시: `python setup_cronjob_org.py --update-schedule`")
        mode_hint = " --swing" if swing_mode else " (POLL_MINUTES/UTC_HOURS 반영) "
        print(f"   {mode_hint}— 또는 잡을 삭제 후 재생성.")
        print_jobs(existing)
        return

    print("✅ cron-job.org API 키 인증 성공.")
    job_id = create_job(cronjob_key, payload)
    print(f"🎉 크론잡 생성 완료! jobId = {job_id} ({job_desc})")
    print("\n✅ 설정 완료! 이벤트가 GitHub Actions 워크플로우를 실행합니다.")
    print("   테스트: python setup_cronjob_org.py --test-dispatch")


if __name__ == "__main__":
    main()
