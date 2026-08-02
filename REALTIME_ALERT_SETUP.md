# 🚨 ATH DCA 실시간 알림 설정 가이드 (cron-job.org + Finnhub + Discord)

GitHub Actions의 `schedule` 크론은 **best-effort(최선 노력)** 방식이라 피크 시간대에
수 분~수 시간 지연되거나 드물게 누락될 수 있습니다. 그래서 장중 트리거(TRIGGER_1/2/3)
발생을 **1~5분 수준의 실시간**으로 받으려면, GitHub 외부의 정확한 스케줄러가 워크플로우를
"당겨서 실행(pull)"하게 만듭니다.

> 🔗 **관련 문서**: [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) — ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거

```
cron-job.org (정확한 N분 알람)
   │ POST /repos/{owner}/{repo}/dispatches   (event_type: ath-dca-monitor)
   ▼
GitHub Actions (repository_dispatch 트리거 — schedule 지연 없음)
   │ python3 DCA_MA_strategy.py --ath-monitor
   ▼
Finnhub /quote (실시간 가격)  ←── FINNHUB_API_KEY (선택, 없으면 yfinance 종가)
   ▼
check_ath_dca_signals(alerts_only=True) → 🚨 트리거 / 📡 임박만, 중복 제거
   ▼
Discord 웹훅 (기존 DISCORD_WEBHOOK / DISCORD_USER_ID 사용)
```

---

## 1. 필요한 것

| 항목 | 상태 | 용도 |
|---|---|---|
| GitHub 저장소 | 있음 | 워크플로우 실행 |
| GitHub PAT (Personal Access Token) | **새로 발급 필요** | cron-job.org가 GitHub API 호출용 |
| Discord 웹훅 + 사용자 ID | 이미 사용 중 | 알림 전송 (`DISCORD_WEBHOOK`, `DISCORD_USER_ID`) |
| Finnhub API 키 | 있다고 하셨음 | 실시간 가격 조회 (선택 — 없어도 동작) |

---

## 2. GitHub 시크릿 등록

저장소 **Settings → Secrets and variables → Actions** 에서:

| 시크릿 이름 | 값 |
|---|---|
| `DISCORD_WEBHOOK` | (이미 등록되어 있다면 유지) |
| `DISCORD_USER_ID` | (이미 등록되어 있다면 유지) |
| `FINNHUB_API_KEY` | Finnhub 무료 키 (60회/분 제한 — 충분) |

`FINNHUB_API_KEY`를 등록하지 않으면 yfinance 마지막 종가로 폴링하므로
장중 급락 감지가 다소 늦어질 수 있습니다. **등록을 권장합니다.**

---

## 3. GitHub PAT 발급

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens** → **Generate new token**
2. Repository access: 이 저장소만 선택
3. Permissions → **Contents: Read and write** 체크
4. Generate → 토큰 복사 (**이후 다시 볼 수 없음**)

> 참고: `repository_dispatch` API를 호출하려면 Contents 권한이 필요합니다.
> 클래식 토큰이면 `repo` 스코프로도 가능합니다.

---

## 4. cron-job.org 설정 (무료, 1분 간격 지원)

### 방법 A — 자동화 스크립트 (권장)

`setup_cronjob_org.py`가 아래 수동 절차를 대신 수행합니다.
cron-job.org 콘솔 **Settings**에서 발급한 **API 키**가 필요합니다.

```bash
# 1) 환경변수 설정 (.env 파일도 지원)
export CRONJOB_ORG_API_KEY=xxx   # cron-job.org 콘솔 Settings → API key
export GITHUB_PAT=xxx            # GitHub PAT (Contents: Read and write)
export GITHUB_OWNER=bs02089-sys
export GITHUB_REPO=strategy_engine

# 2) 미리보기 (실제 생성 전)
python3 setup_cronjob_org.py --dry-run

# 3) 실제 생성 (장중 10분 간격 기본값)
python3 setup_cronjob_org.py

# 4) 기존 잡 확인
export CRONJOB_ORG_API_KEY=xxx
python3 setup_cronjob_org.py --list

# 5) 테스트 — 워크플로우 1회 실제 실행 확인 (GitHub Actions 탭에서 확인)
python3 setup_cronjob_org.py --test-dispatch
```

선택 환경변수: `GITHUB_EVENT_TYPE`(기본 `ath-dca-monitor`),
`POLL_MINUTES`(기본 `10`), `UTC_HOURS_START`(기본 `13`),
`UTC_HOURS_END`(기본 `21`), `JOB_TITLE`.

**간격/시간 변경 (기존 잡 갱신):** 생성 후 `POLL_MINUTES`·`UTC_HOURS_*`를
바꾸려면 새로 생성하지 말고 아래로 기존 잡의 스케줄만 갱신합니다
(PAT·제목·URL·헤더 완전 보존, 삭제/재생성 불필요):

```bash
# 15분 간격으로 변경 예시 (저장소가 공개라 10분 유지도 무방 — 아래 참고 사항 참조)
export POLL_MINUTES=15
export CRONJOB_ORG_API_KEY=xxx   # cron-job.org 콘솔 Settings → API key
export GITHUB_OWNER=bs02089-sys
export GITHUB_REPO=strategy_engine
python3 setup_cronjob_org.py --update-schedule
```

> `--update-schedule`은 GitHub PAT를 요구하지 않습니다 (GitHub API 호출 없음).
> PAT까지 함께 바꾸려면 `--update-pat`를 먼저 실행하세요.

---

### 방법 B — 수동 설정

1. [cron-job.org](https://cron-job.org) 가입 (무료)
2. **Create cronjob** → 아래처럼 입력:

| 필드 | 값 |
|---|---|
| Title | `ATH DCA realtime monitor` |
| URL | `https://api.github.com/repos/{owner}/{repo}/dispatches` |
| Request method | **POST** |
| Send as | JSON |
| Content-Type | `application/json` |
| Custom Header: `Authorization` | `Bearer {여기서 GitHub PAT}` |
| Custom Header: `Accept` | `application/vnd.github+json` |
| Payload(body) | `{"event_type":"ath-dca-monitor"}` |
| Execution schedule | `*/10 13-21 * * 1-5` (UTC) |

- `{owner}/{repo}` → 실제 저장소 경로로 교체 (예: `bs02089-sys/strategy_engine`)
- 실행 시간대는 **UTC 기준**: `13-21`시 = 뉴욕 장중(09:00~17:00 ET) 대략 포함.
  DST(서머타임)를 고려해 넉넉하게 잡았습니다.
- `*/10` = 10분 간격. 원하면 `*/5`(5분) 또는 `*/15`(15분)로 조정 가능.
- **비공개 저장소 Actions 분(minutes) 주의**: 무료 티어는 월 2,000분 제한.
  10분 간격 × 장중 8시간 × 22거래일 ≈ 1,056회/월, 각 회당 1~2분이면
  **2,000분을 초과할 수 있습니다**. 비공개 저장소라면 **`*/15`(15분, ≈700분/월)**
  간격을 권장하고, **공개 저장소(무제한)라면 5~10분도 가능합니다.**
  (2026-07-31 확인: 본 저장소는 **공개** — 10분 유지 또는 15분 전환 모두 문제 없음)

---

## 5. 동작 원리 (코드 측면)

자동화 스크립트가 만드는 크론잡 설정값 (cron-job.org REST API):

```json
{
  "job": {
    "enabled": true,
    "title": "ATH DCA realtime monitor",
    "url": "https://api.github.com/repos/{owner}/{repo}/dispatches",
    "requestMethod": 1,
    "extendedData": {
      "headers": {
        "Authorization": "Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
      },
      "body": "{\"event_type\":\"ath-dca-monitor\"}"
    },
    "schedule": { "timezone": "UTC", "hours": [13..21], "minutes": [0,10,..50], "wdays": [1,2,3,4,5], "mdays": [-1], "months": [-1] }
  }
}
```

- `.github/workflows/sigma_dca_manager.yml`
  - `repository_dispatch: types: [ath-dca-monitor]` 추가 → cron-job.org 호출 시 즉시 실행
  - `concurrency` 그룹으로 야간 브리핑과 실시간 폴링이 동시에 돌지 않게 직렬화
  - `FINNHUB_API_KEY` 시크릿을 환경변수로 주입
  - dispatch 이벤트면 `python3 DCA_MA_strategy.py --ath-monitor`, 아니면 기존 야간 브리핑

- `DCA_MA_strategy.py`
  - `check_ath_dca_signals(cfg, realtime_prices, alerts_only=True)`:
    - `realtime_prices`: Finnhub 실시간 가격으로 현재가 오버라이드 (ATH는 1년 일봉 유지)
    - `alerts_only=True`: 🚨 트리거/📡 임박(+/🔄 설정변경) 메시지만 생성, 상태/요약 줄 생략
    - 임박 메시지 중복 방지: `ATH_DCA_IMMINENT_SENT`에 (split → gap) 저장,
      **갭이 1.0%p 이상 좁혀질 때만** 다시 알림 → 폴링마다 스팸 방지
    - 트리거 발생 시 `ATH_DCA_USED_SPLITS`에 기록되어 중복 매수 신호 방지
  - `run_ath_dca_monitor()`: `--ath-monitor` 플래그로 진입, 전용 디스코드 전송
  - **MA 레짐 크로스 알림** (LOC 모드에서만 작동 — ATH_DCA 비상 모드 중에는 OFF):
    - `_check_ma_filter()`가 종가×MA 크로스를 평가, 레짐 전환 시 1회만 감지
    - MA **하향 돌파** → 🚨 전량 청산 + 매수 금지 / MA **상향 돌파** → 💰 전액 재매수(TQQQ) / 🔄 DCA 재개(SOXL)
    - `MA_FILTER_STATE`(`{regime, since}`) 영속화 → 크로스 중복 알림 없음
    - 실시간 모니터가 장중에 돌아도 인트라데이 가격으로 거짓 크로스가 나지 않도록
      미정산 당일 바 제외(`_drop_unsettled_today_bar()` — 종가 확정 후 반영)

---

## 6. 테스트 방법

### 로컬 테스트 (옵션)
```bash
export FINNHUB_API_KEY=xxxx
export DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
export DISCORD_USER_ID=123456789
python3 DCA_MA_strategy.py --ath-monitor
```
- 트리거/임박이 없으면 `✅ No ATH DCA trigger/imminent alerts this poll.`
- 시뮬레이션 확인: 임시로 `portfolio_config.json`의 `TRIGGER_1`을 현재가 대비 높게
  잡아(예: `-1%`) 한 번 실행해보면 🚨 메시지가 오는지 확인 가능. 확인 후 원복.

### cron-job.org 테스트
- cron-job.org 대시보드에서 **Run now** 버튼 → Actions 탭에서 실행 확인
- Discord에서 알림 수신 확인

---

## 7. 참고 사항

- GitHub Actions 스케줄은 60일간 저장소 활동이 없으면 자동 비활성화됩니다.
  실시간 폴링 자체가 커밋을 만들어내므로 자연히 해결됩니다.
- `FINNHUB_API_KEY`가 없으면 yfinance 종가 기준이라 장중 1~5분 지연이 아닌
  "다음 종가까지 대기"가 될 수 있습니다. 키를 넣는 게 핵심입니다.
- 트리거가 발동된 split은 `ATH_DCA_USED_SPLITS`에 저장되어 재발동되지 않으며,
  신규 ATH 갱신 시 사이클이 재시작되면 다시 알림이 갑니다.
- **임박(📡) 알림 중복 규칙**: 한 번 임박 알림을 보내면 갭이 직전 알림 대비
  **1.0%p 이상 좁혀질 때만** 다시 보냅니다. 가격이 5%p 구간에서 벗어났다가
  같은 갭으로 재진입하면 즉시 재알림되지 않을 수 있습니다 (스팸 방지 목적).
- **노이즈 커밋**: 폴링마다 `sigma_log.txt`가 갱신되고 임박 갭이 바뀌면
  `portfolio_config.json`도 커밋됩니다. 정상 동작이며 저장소 활동 유지에 도움이
  되지만 git 히스토리가 늘어납니다.
- **MA 레짐 필터**: 실시간 모니터는 ATH DCA 트리거/임박 알림뿐 아니라 **MA 레짐 크로스 알림**(TQQQ MA20 / SOXL MA250)도
  LOC 모드에서 발송합니다. 비상 모드 중에는 필터가 OFF라 "참고" 표시만 됩니다.
  백테스트 검증(TQQQ MA20 +2,138.5%/-41.2%, SOXL MA250 +265.2%/-34.8%)과 설정값은
  [README.md](README.md)의 "MA 레짐 필터" 섹션과 `portfolio_config.json`의 `MA_FILTER` 블록을 참고.
- **트리거 값 근거**: TQQQ `TRIGGER_2=-50%`, SOXL `TRIGGER_2=-70%` 등 트리거 후보값의
  최적화 근거(2016~2026 월말 스윕)는 [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) 참고.
- **비상 모드 종료 실효성 검증 (백테스트, 2026-08-02)**: 백테스트로
  크래시→회복 사이클을 검증한 결과, **2020 COVID 크래시 포함 구간에서
  TQQQ 비상 모드 종료가 +4.67%p 수익 우위**(+136.17% vs 현행 +131.50%, Sharpe 2.17 vs 2.14, MDD 동일 -39.62%)를
  냈습니다. 종료는 2020-06-23(D+81, DD 15.5% ≤ 0.5×T1 + MA20>MA60)에 1회 발동, 잔여 예비금 $3,333으로
  LOC 2회 추가 매수했습니다. 단, **종료 시점 잔여 현금이 $0(드라이 파우더 소진)이면 효과가 없어**
  크래시 진입 후 예비금 보존이 실효성의 핵심입니다. 상세는 [DUAL_MODE_SUMMARY.md](DUAL_MODE_SUMMARY.md) 참고.
