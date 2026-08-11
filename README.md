# DCA MA Strategy

미국 주식 시장 **Sigma 기반 LOC 매수 목표가 자동 계산** 및 **디스코드 브리핑 자동 발송** 시스템입니다.

---

## 📋 목차

- [개요](#-개요)
- [주요 기능](#-주요-기능)
- [시스템 아키텍처](#-시스템-아키텍처)
- [파일 구성](#-파일-구성)
- [설치 및 설정](#-설치-및-설정)
- [설정 파일](#-설정-파일-단일-파일)
- [실행 방법](#-실행-방법)
- [GitHub Actions 자동화](#-github-actions-자동화)
- [백테스트](#-백테스트)
- [연동 시스템](#-연동-시스템)
- [관련 문서](#-관련-문서)
- [라이선스](#-라이선스)

---

## 📌 개요

**DCA MA Strategy**는 매일 미국 장 마감 후 정해진 시간에 자동 실행되어:
1. 포트폴리오에 등록된 티커(TQQQ, SOXL 등)의 변동성을 계산/갱신
2. Sigma 기반 LOC 매수 목표가 산출
3. RSI+거래량 복합 매수 신호 평가 (12년 백테스트 검증)
4. 듀얼 모드 (LOC 일반 / ATH DCA 비상) 자동 전환
5. ATH 하락분할 DCA 트리거 모니터링 (3차 분할, STAGE5 바닥 통합)
6. 비상 모드 종료 — 시장 회복 감지 시 일반 모드(LOC) 자동 복귀
7. MA 레짐 필터 — 종가×MA 크로스 기반 추세 필터 (LOC 모드: 전량 청산/재진입 신호)
8. 로테이션 포지션 만기 관리
9. 종합 브리핑을 **Discord**로 전송
10. 장중 실시간 ATH DCA 알림 (cron-job.org → GitHub Actions, `--ath-monitor`)

---

## 🚀 주요 기능

### 1️⃣ Sigma(LOC) 목표가 계산
- **EWMA** 또는 **역사적 표준편차** 방식의 변동성 계산
- `ENTRY_MULTIPLIER` × σ 만큼 하락한 가격을 LOC 매수 목표가로 설정
- 설정된 LOOKBACK_DAYS 기준으로 변동성 자동 갱신 (90일 주기, 또는 설정 변경 시 즉시 갱신)
- Sigma 갱신 이력은 `sigma_history.csv`에 기록

### 2️⃣ RSI + 거래량 복합 매수 신호 (12년 백테스트 검증)
- **SOXL**: RSI(14) — 구간1 RSI 25~34 거래량 0.3~0.7배 / 구간2 RSI 34~40 거래량 0.4~0.9배
  - 샤프비율 2.62 | 승률 71.4% | 평균 +21.56%
- **TQQQ**: RSI(21) — 구간1 RSI 25~35 거래량 0.3~0.7배 / 구간2 RSI 35~50 거래량 0.4~1.0배
  - 샤프비율 1.30 | 승률 67.3% | 평균 +7.48%
- 두 구간 동시 충족 시 **🔥🔥🔥 적극 매수 추천** 플래그 표시

### 3️⃣ ATH 하락분할 DCA
- ATH 대비 하락률에 따라 N분할 매수 트리거
- 설정 예시 (현재 값): TQQQ -35% / -50% / Stage 5 바닥, SOXL -60% / -70% / Stage 5 바닥 — 각각 1/3씩
- 전 사이클 완료 후 신규 ATH 갱신 시 **자동 초기화 및 사이클 재시작**
- 임박 알림 (목표 임계값 5%p 이내 접근 시)

### 4️⃣ 포지션 유형별 전략

| 유형 | 전략 |
|------|------|
| **LONG_YEAR** | 기계적 LOC 전략 — 무조건 매수 신호 활성 |
| **ROTATION_3M** | MA20/MA60 추세 기반 매수/매도 신호 + 만기 초기화 |
| **END_DEC** | MA20/MA60 추세 기반 매수/매도 신호 |

### 5️⃣ 듀얼 모드 전환 + ATH 하락분할 DCA (비상 모드)
- **LOC 모드** (📗): 평상시 Sigma 기반 LOC 20분할 매수
- **ATH DCA 모드** (🚨): ATH 하락률이 TRIGGER_1 도달 시 자동 전환 → 3차 분할 매수
  - 1차/2차: ATH 대비 설정된 % 하락 시 (TQQQ: -35%/-50%, SOXL: -60%/-70%)
  - **3차: MarketStageSystem의 Stage 5 바닥 감지 시 발동**
- MarketStageSystem.py가 `market_state.json`에 기록한 바닥 단계를 ATH DCA 3차 트리거로 활용
- 전 사이클(3차) 완료 후 신규 ATH 갱신 시 자동 초기화 및 사이클 재시작
- **비상 모드 종료** (🔄): 시장 회복 감지 시 자동으로 일반 모드(LOC) 복귀
  - 조건 4가지: 잔여 분할 보존 + 진입 후 30영업일 경과 + DD ≤ DD_RATIO×TRIGGER_1 + MA20 > MA60
  - 파라미터: `RECOVERY_REENTRY` 블록 (ENABLED / DD_RATIO / MIN_DAYS / MA_CONFIRM)
  - 브리핑에 ⏳ 대기 모니터(D+X/30)와 🔔 임박 넛지 알림 제공

### 6️⃣ MA 레짐 필터 (백테스트 검증 반영)

기존 전략에 **종가 × 이동평균(MA) 크로스 레짐 필터**를 얹어 MDD를 낮추는 설계입니다.
`DCA_MA_strategy.py`(TQQQ MA20 +2,138.5%/-41.2%, SOXL MA250 +265.2%/-34.8%)에서
검증한 설정을 실전 엔진(`DCA_MA_strategy.py`)에 반영했습니다.

| 모드 | MA 필터 동작 |
|------|-------------|
| **LOC (일반)** | 🟢 활성 — MA 하향 돌파 → **🚨 전량 청산 + 매수 금지** (LOC/RSI 매수 신호 생략) / MA 상향 돌파 → **💰 전액 재매수**(TQQQ) 또는 **🔄 DCA 재개**(SOXL) |
| **ATH_DCA (비상)** | OFF — 분할 매수 진행 중에는 개입하지 않음 (레짐 참고 표시만) |
| **비상 모드 종료 → LOC 복귀** | 리커버리 리엔트리가 복귀를 판정하면 **MA 필터 다시 활성** |

- 크로스 신호는 레짐 전환 시 **1회만** 발송 (상태 자동 영속화 → 중복 알림 없음)
- 실시간 모니터(`--ath-monitor`)에서도 크로스 알림 발송
- 설정: `MA_FILTER` 블록 (아래 [설정 파일](#-설정-파일-단일-파일) 참고)

### 7️⃣ 장중 실시간 ATH DCA 알림 (--ath-monitor)
- GitHub Actions `schedule` 크론은 best-effort라 피크 시간대에 수 분~수 시간 지연될 수 있음
- **cron-job.org**(정확한 N분 알람)가 `repository_dispatch` 이벤트를 발사 → 워크플로우가 `--ath-monitor` 분기로 즉시 실행
- yfinance 종가 기준으로 🚨 트리거 / 📡 임박(5%p)만 전송 (중복 제거: 갭이 1.0%p 이상 좁혀질 때만 재알림). Finnhub 키 의존 제거 (2026-08)
- 설정 자동화: `setup_cronjob_org.py` (생성 / --list / --test-dispatch / --update-pat / --update-schedule)
- 상세 가이드: `REALTIME_ALERT_SETUP.md`

### 8️⃣ Discord 브리핑
- 매일 정해진 시간에 Discord Webhook으로 종합 브리핑 전송
- 각 티커별: 현재가, Sigma, LOC 목표가, 전고점 대비 하락률/회복률, 매수/매도 신호
- 매월 1일 월간 작동 확인 Ping 전송

### 9️⃣ 로테이션 포지션 자동 초기화
- ROTATION_3M 포지션: 설정된 영업일(기본 63일) 경과 후 자동 초기화 + Sigma 재계산

---

## 🏗 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│              GitHub Actions (스케줄러 + 실시간)                  │
│  야간 실행: 매일 23:30 UTC — 통합 브리핑 1건 (월~금)         │
│  실시간 알림: cron-job.org → repository_dispatch (장중 N분)    │
└─────────────────┬───────────────────────────────────────────────┘
                  │ 실행
┌─────────────────▼───────────────────────────────────────────────┐
│                  DCA_MA_strategy.py                            │
│                                                                  │
│  1. portfolio_config.json 불러오기                                │
│  2. Sigma 갱신 (오래되었거나 설정 변경 시)                       │
│  3. 전일 종가 및 LOC 목표가 계산 (티커별)                        │
│  4. RSI+거래량 복합 신호 확인                                    │
│  5. ATH 하락분할 DCA 트리거 확인                                 │
│  6. 로테이션 만기 확인                                           │
│  7. 시장 바닥 단계 확인                                          │
│  8. 비상 모드 종료 평가 + 대기 모니터                             │
│  9. 브리핑 작성 → Discord 전송                                  │
│  10. 월간 Ping (매월 1일)                                        │
│  (--ath-monitor: yfinance 종가 → 🚨/📡 알림만 전송)            │
└──────┬──────────────┬──────────────┬──────────────┬──────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│yfinance    │ │Discord     │ │market_state│ │signal_repo-│
│(실시간     │ │Webhook     │ │.json       │ │rt.json     │
│시세/변동성)│ │(브리핑     │ │(단계 정보) │ │(리스크 점수│
└────────────┘ │전송)       │ └────────────┘ │)           │
              └────────────┘                └────────────┘
```

---

## 📁 파일 구성

| 파일 | 설명 |
|------|------|
| **DCA_MA_strategy.py** | 📌 **통합 완결판** — 실전 엔진(LOC 목표가/ATH DCA/MA 레짐 필터/Discord 브리핑/`--ath-monitor`) + 백테스트 + `--signal` 실시간 신호 |
| **DCA_MA_strategy_flowchart.py** | 시스템 전체 플로우차트 문서 |
| **setup_cronjob_org.py** | cron-job.org 실시간 알림 설정 자동화 (생성/--list/--test-dispatch/--update-pat/--update-schedule) |
| **swing_alerter.py** | 🆕 **스윙 투자 알리미** — MDD 구간 매수/매도 알림 + 모바일 대시보드 (유튜브 TQQQ 스윙 전략 재구현) |
| **swing_config.json** | 스윙 알리미 공용 설정 (사용자 소유 — MDD 구간/목표/포지션/푸시) |
| **swing_personal.json** | 🔒 스윙 알리미 **개인 포지션** (LOTS — 계좌별 BUY_PRICE/SHARES, 공용 알림에 노출 안 됨, 사용자 소유) |
| **swing_state.json** | 스윙 알리미 봇 상태 (ZONE_ALERTS/매도 플래그 — 봇 전용, 자동 관리) |
| **swing_dashboard.html** | 스윙 알리미 모바일 대시보드 (자동 생성) |
| **MarketStageSystem.py** | 독립적인 시장 단계 시스템 — 바닥 단계 감지 |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **portfolio_config.json** | 📌 **포트폴리오 설정** — 포지션, Sigma, DCA 파라미터, 모드 상태 |
| **TRIGGER_OPTIMIZATION_SUMMARY.md** | ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거 |
| **DUAL_MODE_SUMMARY.md** | 듀얼 모드(LOC ↔ ATH_DCA) 구조 요약 문서 |
| **REALTIME_ALERT_SETUP.md** | 실시간 ATH DCA 알림 설정 가이드 |
| ~~MarketStage_config.json~~ | (제거됨 — portfolio_config.json으로 통합) |
| **sigma_history.csv** | Sigma 갱신 이력 (런타임 자동 생성 — 추적 제외) |
| **market_state.json** | 시장 단계 상태 정보 (자동 생성) |
| **signal_report.json** | 시장 리스크 점수 (자동 생성) |
| **requirements.txt** | Python 의존성 패키지 목록 |

---

## 🔧 설치 및 설정

### 요구 사항
- Python 3.9+
- pip 패키지 매니저

### 설치

```bash
# 저장소 클론
git clone <저장소-주소>
cd strategy_engine

# 의존성 설치
pip install -r requirements.txt
```

### 의존성 패키지

```
requests              # Discord Webhook 전송
python-dotenv         # 환경 변수 관리 (선택)
trafilatura           # 웹 크롤링 (보조)
numpy                 # 수치 연산
yfinance              # Yahoo Finance 실시간 시세 조회
pandas_market_calendars  # NYSE 휴장일 계산
```

---

## ⚙️ 설정 파일 (단일 파일)

> **`portfolio_config.json`** 하나만 있으면 됩니다.
> `MarketStage_config.json`은 제거되어 `portfolio_config.json`으로 통합되었습니다.

### `portfolio_config.json`

```json
{
    "POSITIONS": {
        "TQQQ": {
            "LOOKBACK_DAYS": 252,
            "ENTRY_MULTIPLIER": 1.1,
            "VOL_METHOD": "EWMA",
            "EWMA_LAMBDA": 0.94,
            "DAILY_SIGMA": 0.0355,
            "START_DATE": "2026-07-25",
            "INVEST_TYPE": "LONG_YEAR",
            "ALLOCATION_PCT": 10,
            "STRATEGY_MODE": "ATH_DCA",
            "ATH_DCA": {
                "ENABLED": true,
                "SPLITS": 3,
                "TRIGGER_1": "-35%",
                "TRIGGER_2": "-50%",
                "TRIGGER_3": "STAGE5",
                "STRATEGY": "v2 crash-mode"
            },
            "ATH_DCA_USED_SPLITS": [1],
            "ATH_DCA_ENTERED_ON": "2026-03-27",
            "MA_FILTER": {
                "ENABLED": true,
                "MA_DAYS": 20,
                "REENTRY": "lump",
                "REENTRY_PCT": 1.0
            },
            "RECOVERY_REENTRY": {
                "ENABLED": true,
                "DD_RATIO": 0.5,
                "MIN_DAYS": 30,
                "MA_CONFIRM": true
            }
        }
    },
    "STRATEGY": { "CYCLE_YEARS": 2, "BUY_DURATION_DAYS": 252, "HOLD_DURATION_DAYS": 252 }
}
```

> 참고: `STRATEGY_MODE`는 `"LOC"`(일반) 또는 `"ATH_DCA"`(비상) 중 하나이며,
> 시스템이 자동으로 전환합니다. `ATH_DCA`의 `TRIGGER_3`는 `"STAGE5"`(시장 바닥 감지)가 기본입니다.

#### 포지션 설정 항목

| 항목 | 설명 |
|------|------|
| `LOOKBACK_DAYS` | 변동성 계산 기간 (기본 252 = 1년) |
| `ENTRY_MULTIPLIER` | LOC 목표가 승수 — σ × 승수 만큼 하락한 가격이 매수 목표 |
| `VOL_METHOD` | 변동성 계산 방식: `EWMA` (기본) 또는 `STD` |
| `EWMA_LAMBDA` | EWMA 감쇠 계수 (기본 0.94) |
| `INVEST_TYPE` | 투자 유형: `LONG_YEAR` / `ROTATION_3M` / `END_DEC` |
| `ALLOCATION_PCT` | 포트폴리오 내 비중 |
| `STRATEGY_MODE` | 현재 전략 모드: `LOC` (일반) / `ATH_DCA` (비상) — 자동 관리 |
| `ATH_DCA` | ATH 대비 하락분할 매수 설정 (`ENABLED`/`SPLITS`/`TRIGGER_1~3`) |
| `RECOVERY_REENTRY` | 비상 모드 종료 파라미터 (`ENABLED`/`DD_RATIO`/`MIN_DAYS`/`MA_CONFIRM`) |
| `MA_FILTER` | MA 레짐 필터 (`ENABLED`/`MA_DAYS`/`REENTRY`/`REENTRY_PCT`) — TQQQ: MA20+lump, SOXL: MA250+dca_reset |
| `ATH_DCA_ENTERED_ON` | 비상 모드 진입일 — 비상 모드 유지 클럭 기준 (자동 기록) |
| `ROTATION_EXIT_DAYS` | ROTATION_3M 만기 영업일 수 |

### LOC 목표가 계산식

```
LOC 목표가 = 전일종가 × (1 - sigma × ENTRY_MULTIPLIER)
```

- **sigma**: 일간 로그수익률의 (EWMA 또는 표준편차)
- **ENTRY_MULTIPLIER**: 목표가 조정 승수 (portfolio_config.json에서 설정)

---

## ▶️ 실행 방법

### 수동 실행

```bash
# LOC 브리핑 생성 및 Discord 전송 (기본 실행)
python3 DCA_MA_strategy.py

# 장중 실시간 ATH DCA 알림 (cron-job.org dispatch에서 호출)
python3 DCA_MA_strategy.py --ath-monitor

# 특정 함수만 테스트
python3 -c "
from DCA_MA_strategy import get_prev_close, calculate_loc_price
import json
with open('portfolio_config.json') as f:
    cfg = json.load(f)

close, date = get_prev_close('TQQQ')
print(f'TQQQ 종가: \${close} ({date})')

loc = calculate_loc_price('TQQQ', close, cfg)
print(f'TQQQ LOC 목표가: \${loc}')
"
```

### cron-job.org 설정 (실시간 알림)

```bash
# 환경변수 (.env 파일도 지원)
export CRONJOB_ORG_API_KEY=xxx   # cron-job.org 콘솔 Settings → API key
export GITHUB_PAT=xxx            # GitHub PAT (Contents: Read and write)
export GITHUB_OWNER=bs02089-sys
export GITHUB_REPO=strategy_engine

# ATH DCA 실시간 모니터 (장중 10분)
python3 setup_cronjob_org.py --dry-run          # 생성 전 미리보기
python3 setup_cronjob_org.py                    # 실제 생성 (장중 10분 간격 기본)

# 공통 관리
python3 setup_cronjob_org.py --list             # 등록된 잡 목록
python3 setup_cronjob_org.py --test-dispatch    # 테스트 dispatch 1회
python3 setup_cronjob_org.py --update-pat       # 크론잡에 저장된 PAT 갱신 (토큰 재발급 시)
python3 setup_cronjob_org.py --update-schedule  # 폴링 간격 갱신 (POLL_MINUTES/UTC_HOURS 반영)
```

### 백테스트 실행 (MA 레짐 전략)

```bash
python3 DCA_MA_strategy.py --backtest                    # TQQQ (MA20 + 올인 재진입)
python3 DCA_MA_strategy.py --backtest --ticker SOXL      # SOXL (MA250 + DCA 재개)
python3 DCA_MA_strategy.py --backtest --ticker SOXL --ma 30 --reentry dca_reset  # MDD 절감 대안
python3 DCA_MA_strategy.py --backtest --fee 0.001        # 수수료 0.1% 반영
```

상세 사용법(신호 모드 포함): [MA 레짐 전략](#ma-레짐-전략-dca_ma_strategypy)

### 플로우차트 문서 보기

```bash
python3 DCA_MA_strategy_flowchart.py
```

---

## 🤖 GitHub Actions 자동화

### `dca_ma_strategy.yml` — 통합: 정기 브리핑 + MA 레짐 신호 + 실시간 ATH DCA 알림

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:30 (월~금) | 장 마감 후 **통합 브리핑 1건** 발송 (MA 레짐 신호는 브리핑에 통합) |
| repository_dispatch | 장중 N분 (cron-job.org) | `--ath-monitor` 실시간 알림 (🚨/📡) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

> - 23:30 UTC 실행 시 `DCA_MA_strategy.py`(통합 브리핑) 1건만 Discord로 발송합니다. MA 레짐 실행 액션(▶)과 비상 트리거(📡)가 티커 블록에 포함되며, `--signal`은 콘솔 로그 확인용으로만 실행됩니다.
> - 신호 메시지: 종가·날짜 · LOC 매수가 · 레짐 상태 · 액션을 한 번에 전송.
> - `concurrency` 그룹으로 야간 실행과 실시간 폴링이 동시에 돌지 않게 직렬화됩니다.

### `bear_market_signals.yml` — 약세장 신호

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:00 (월~금) | 시장 리스크 평가 |

### `tracker.yml` — 시장 단계 추적

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:14 (월~금) | 바닥 단계 추적 |

### `swing_alerter.yml` — 스윙 투자 알리미

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:40 (월~금) | 장 마감 후 스윙 일일 브리핑 + 대시보드 갱신 |
| repository_dispatch | 장중 N분 (cron-job.org) | `--monitor` 실시간 알림 (매수 구간 도달/임박/매도) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

### 환경 변수 (GitHub Secrets)

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK` | Discord Webhook 주소 |
| `DISCORD_USER_ID` | Discord 사용자 ID (멘션용) |

---

## 📊 백테스트

백테스트는 **`DCA_MA_strategy.py`** 하나로 수행합니다 — 기존 시그마 DCA 엔진(승수 1.1,
매수 $2,500×20) + MA 레짐 필터를 티커별 기본 설정으로 검증하고,
`--signal`로 실시간 신호도 확인합니다 (상세: [MA 레짐 전략](#ma-레짐-전략-dca_ma_strategypy)).

### 사용 기술
- 일간 로그수익률 기반 변동성(σ) 계산
- EWMA(λ=0.94) 가중치 적용
- LOC 목표가: `전일종가 × (1 - σ × 승수)`
- 매수 조건: 당일 저가 ≤ LOC 목표가

> 📊 **ATH_DCA 트리거 최적화 분석** — 10년 치 월말 스윕 기반 TQQQ/SOXL 트리거 후보값 비교와
> 의사결정 근거는 [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) 참고.

> 🧪 **비상 모드 종료 실효성 검증 (2026-08-02)** — 2020 COVID 크래시 포함 구간에서
> TQQQ가 현행 대비 **+4.67%p**(+136.17% vs +131.50%) 우위를
> 기록했습니다. 단, 크래시 후 **잔여 현금(예비금)이 남아있을 때만** 효과가 있으므로 예비금 보존이
> 핵심입니다. 상세는 [DUAL_MODE_SUMMARY.md](DUAL_MODE_SUMMARY.md) 참고.

### TQQQ MA 교차 그리드 탐색 (탐색 완료 — 툴 정리됨)

유튜브 스타일 "단기/장기 MA 크로스" 전수 탐색(9,009개 조합)은 완료 후 파일을 정리했습니다.

> 📊 **검증 결과 (2026-08-02)**: 10년 구간 수익률 최고 조합은 **20일/22일**(+2,628%, MDD -58%),
> MDD 최소 조합은 **8일/73일**(MDD -37.5%, +952%), 저빈도 균형 추천 **7일/104일**(+1,842%, MDD -40.6%, 연 3.7회).
> 유튜버 기준 **6/107**은 수익률 82위/9,009·MDD 상위 35위로 전략이 유효함을 확인.
> 하이브리드(예비금 + 급락 분할매수)는 예비금 기회비용과 나이프 잡기 효과로 **수익률이 절반 수준으로
> 낮아지고 MDD만 개선**되므로, MDD 축소가 목표일 때만 적합합니다.

### 기존 전략 + MA 필터 오버레이 (탐색 완료 — 툴 정리됨)

기존 시그마 DCA 엔진(승수 1.1, 매수 $2,500×20)에 **MA 레짐 필터(청산형)** 를
얹는 일선 탐색은 완료 후 파일을 정리했습니다. 재진입 방식: `dca_reset`(DCA 재개) / `lump`(올인 재진입).

> 🎯 **검증 결과 (2026-08-02)**: 기준선(MA 필터 없음)은 +273.7% / MDD **-49.1%**.
> **MA 20일선 + 올인 재진입**이 최적으로 **+2,138.5% / MDD -41.2%** (MDD 7.9p 개선 + 수익 7.8배,
> Calmar 5.6→51.9). 전반/후반기 MDD도 각각 -26.6%/-33.7%로 안정적.
> 단, "기존 DCA 성격 유지"(dca_reset) 방식은 MDD를 줄이되(MA5~30: -1~-21%) 수익까지 같이
> 줄어듭니다(MA5~30: +9~33%). 즉 **MDD와 수익을 동시에 얻으려면 MA20 + 올인 재진입**이 유일한
> 답이며, 이는 사실상 "가격-20일선 크로스" 전략에 수렴합니다.

### MA 레짐 전략 (`DCA_MA_strategy.py`)

실전 엔진 + 백테스트/신호를 통합한 **완결판 단일 파일**입니다. MA 레짐 전략 티커별 기본 설정:

| 티커 | 기본 설정 | 10년 결과 | 용도 |
|------|-----------|-----------|------|
| TQQQ | MA20 + 올인 재진입 100% | +2,138.5% / MDD -41.2% | 수익·MDD 동시 개선 |
| SOXL | **MA250 + DCA 재개** | **+265.2% / MDD -34.8%** | 수익 3배 (기존과 MDD 동일) |
| SOXL (대안) | MA30 + DCA 재개 | +49.1% / MDD **-16.2%** | MDD 절감 우선 (전반 -14.8% / 후반 -17.2% 안정) |

```bash
# 백테스트
python3 DCA_MA_strategy.py --backtest                # TQQQ (MA20 lump)
python3 DCA_MA_strategy.py --backtest --ticker SOXL  # SOXL (MA30 dca_reset)
python3 DCA_MA_strategy.py --backtest --ticker SOXL --ma 250 --reentry dca_reset
python3 DCA_MA_strategy.py --backtest --fee 0.001    # 수수료 0.1% 반영

# 실시간 신호 (장 마감 후) — --discord로 Discord 발송 (GitHub Actions 자동화)
python3 DCA_MA_strategy.py --signal
python3 DCA_MA_strategy.py --signal --ticker SOXL
python3 DCA_MA_strategy.py --signal --discord       # TQQQ 신호를 Discord로
python3 DCA_MA_strategy.py --signal --discord --ticker SOXL
python3 DCA_MA_strategy.py --signal --discord --all  # TQQQ+SOXL 단일 메시지 (수동 확인용 — 워크플로우는 브리핑 1건만 발송)
```

> ⚠️ **SOXL에 TQQQ식 MA20 올인을 적용하면 MDD가 -84.7%로 폭증합니다.** SOXL은 변동성이
> 너무 커 짧은 20일선 타이밍이 휩쏘에 걸립니다. 티커별 특성에 맞는 설정을 사용하세요.

### 실전 반영 — `DCA_MA_strategy.py` MA 레짐 필터

`DCA_MA_strategy.py`에서 검증한 레짐 필터를 **실전 운용 엔진에 통합**했습니다
(2026-08-02 기준, 알림 신호 방식 — 실제 주문 자동 실행은 없음):

- **TQQQ (MA20 + lump)**: LOC 모드에서 종가가 MA20을 하향 돌파 → **🚨 전량 청산 + 매수 금지**,
  상향 돌파 → **💰 전액 재매수** 신호
- **SOXL (MA250 + dca_reset)**: LOC 모드에서 MA250 하향 돌파 → **🚨 전량 청산 + 매수 금지**,
  상향 돌파 → **🔄 DCA 재개** 신호
- **ATH_DCA 비상 모드 중에는 MA 필터 OFF** — 분할 매수 진행을 방해하지 않음 (레짐 참고 표시만)
- 비상 모드 종료(리커버리 리엔트리)로 LOC 복귀 후 **MA 필터 다시 활성**
- 레짐/크로스 상태는 `MA_FILTER_STATE`에 자동 기록 — 크로스 알림은 1회만 발송
- 일일 브리핑의 `• 📉 **MA{n} 레짐:**` 라인과 실시간 모니터(`--ath-monitor`)의 🚨/💰/🔄 크로스
  알림으로 확인 가능

---

## 📈 스윙 투자 알리미 (swing_alerter.py)

유튜브 **"TQQQ 스윙 투자 전략 / 스윙 투자 계산기&매수 매도 시점 알리미"** (구글
스프레드시트)의 로직을 자체 엔진으로 재구현한 도구입니다. 스마트폰에서 확인할 수 있는
모바일 대시보드와 Discord 알림을 함께 제공합니다.

### 전략 규칙 (스프레드시트 기준)

- **매수**: 역대 최고가(ATH) 대비 MDD 5% 단위 구간(-5% ~ -95%)에 현재가가 도달하면
  해당 구간이 '매수' 상태가 됩니다.
- **매도**: 실제 매수가(`BUY_PRICE`) 대비 스윙 목표 수익률(`SWING_TARGET_PCT`, 현재 +20%) 도달 시
  매도 알람 (예: 매수가 $100 → 목표 $120). 앱 대시보드의 기본 선택 수익률도 이 설정값을 읽습니다.
- **계산기**: `BUY_PRICE` × `SHARES` → 목표 매도 시 예상 수익금/수익률 자동 계산.

### 설정 (swing_config.json — 공용 / swing_personal.json — 개인 / swing_state.json — 봇 상태)

```json
{
    "ENABLED": true,
    "REFERENCE_HIGH": "ATH",
    "MDD_START_PCT": 5, "MDD_END_PCT": 95, "MDD_STEP_PCT": 5,
    "SWING_TARGET_PCT": 20,   # 앱 대시보드 기본 선택 수익률도 이 값을 읽음 (JS 하드코딩 없음)
    "IMMINENT_GAP_PCT": 5,
    "POSITIONS": {
        "TQQQ": {
            "ENABLED": true, "LABEL": "TQQQ (예시)"
        }
    }
}
```

**🔒 개인 포지션은 `swing_personal.json`에 분리해서 기록합니다** — 세븐 스플릿 7개 계좌는
`LOTS`(계좌별 로트) 구조로 각 계좌의 실제 매수가/보유수량을 개별 추적합니다 (2026-08-10):

```json
{
    "POSITIONS": {
        "TQQQ": {
            "LOTS": [
                { "ACCOUNT": 1, "BUY_PRICE": 73.97, "SHARES": 6 },
                { "ACCOUNT": 2, "BUY_PRICE": 70.00, "SHARES": 7 },
                { "ACCOUNT": 3, "BUY_PRICE": null, "SHARES": null },
                { "ACCOUNT": 4, "BUY_PRICE": null, "SHARES": null },
                { "ACCOUNT": 5, "BUY_PRICE": null, "SHARES": null },
                { "ACCOUNT": 6, "BUY_PRICE": null, "SHARES": null },
                { "ACCOUNT": 7, "BUY_PRICE": null, "SHARES": null }
            ]
        }
    }
}
```

- **주수(SHARES)는 정수로만 기록** — 나무증권 등 정수 주 단위 매수 대응. 소수 입력 시 내림(버림):
  $500 ÷ 매수가 = 6.76 → **6주** (반올림 금지 — 예산 초과로 실제 매수 불가).
- 계좌별 `BUY_PRICE`(실제 매수가) × `(1 + SWING_TARGET_PCT/100)` → **계좌별 매도 목표**가 자동 계산되고,
  콘솔에서 계좌별 상태(🚨 매도/🚀 임박/⏳ 대기)와 예상 손익을 확인할 수 있습니다. 미입력 계좌는 무시됩니다.
- 구형 단일 키(`BUY_PRICE`/`SHARES` 직접 입력)도 지원됩니다 (1번 계좌 로트로 승격 — 하위 호환).
- 실제 매수가·보유수량은 지인과 공유되는 Discord 브리핑/대시보드에 노출되지 않도록
  **공용 설정(`swing_config.json`)에서 분리**했습니다. `_PERSONAL` 마커가 붙은 포지션은
  공용 알림에서 "매도 미설정"으로 표시되고, 콘솔에서만 🔒 개인 라벨로 확인할 수 있습니다.
- **전역 OneSignal 푸시는 제거됨 (2026-08-10)**: `--monitor`가 신호 요약을 구독자 전체에게
  보내던 동작은 차단되었습니다. 개인 알림은 앱이 등록한 매도 예정가 태그(`swing_sell_{TICKER}`)
  기준 사용자별 푸시로만 발송됩니다.
- `POSITIONS` 에 티커를 추가/수정하면 자유롭게 여러 종목을 모니터링합니다.
- 알림 플래그(`ZONE_ALERTS`, `SELL_ALARM_SENT`)는 엔진이 자동 관리하며 **`swing_state.json`** 에
  보관됩니다. 설정(사용자)과 상태(봇)가 별도 파일로 분리되어 있어 봇이 상태 파일만 커밋하므로
  git 충돌로 알림 상태가 유실되지 않습니다. **전 계좌 매도 완료 시엔 자동 리셋**되며(2026-08-11),
  부분 매도 후 즉시 초기화하려면 `python3 swing_alerter.py --reset TICKER` 를 수동 실행하세요.
- **신규 전고가 자동 리셋 (2026-08-10)**: 전고가가 직전 사이클 기준(`ATH_CYCLE_BASE`)보다
  **+1% 이상** 갱신되면 기록된 매수 구간 상태(`hit`/`imminent`)가 자동 초기화됩니다 —
  이전 사이클의 기록이 남아 새 하락 사이클의 구간 도달/임박 알림이 삼켜지는 문제를 방지하며,
  갱신 시 🆕 신규 전고가 알림이 1회 발송됩니다.
- **전 계좌 매도 완료 자동 리셋 (2026-08-11)**: `LOTS` 의 모든 계좌가 매도 목표(+20%)에 도달하면
  수동 `--reset` 없이 알림 상태(매수 구간/매도 플래그/`ATH_CYCLE_BASE`)가 자동 초기화됩니다 —
  새 포지션을 `swing_personal.json` 과 앱에 기록하면 다음 하락 사이클의 구간 도달/임박 알림이
  다시 울립니다. `CYCLE_RESET_DONE` 플래그로 중복 리셋을 방지하고, 매도 미도달 상태가 되면
  자동 재무장됩니다. ⚠️ 자동 리셋은 '목표 도달' 신호 기준이며 **봇은 `swing_personal.json`을
  건드리지 않으므로** 매도 후 기록 정리·재기록은 계속 사용자 수동 작업입니다.

#### 🧹 매도 후 정리 체크리스트 (사용자 수동 작업)

봇은 `swing_personal.json`(LOTS)과 앱 입력값을 절대 건드리지 않으므로, **매도한 계좌는 사용자가 직접 비웁니다.**

| 상황 | 스윙 퍼스널 (`swing_personal.json` LOTS) | 폰앱 (계좌별 매수 예정가 행) |
|---|---|---|
| **일부 계좌만 매도** (예: 2번) | 매도한 계좌(2번)만 `BUY_PRICE`/`SHARES` → `null` | 매도한 계좌(2번) 행만 삭제(또는 0 입력) |
| **전 계좌 매도 완료** (사이클 종료) | 1~7번 전부 `BUY_PRICE`/`SHARES` → `null` | 1~7번 행 전부 삭제(또는 0 입력) |

- ⚠️ **보유 중인 계좌는 지우지 않는다** — 계속 매도 추적·푸시를 받아야 하므로.
  (지우면 "아직 안 팔았는데 알림이 안 온다"는 문제가 생김)
- 앱에서 **0 입력과 빈 칸(삭제)은 동일하게 "미입력" 처리**됩니다 — 저장·태그(푸시) 모두 제외.
- 전 계좌 매도 완료 후엔 자동 리셋으로 새 사이클이 준비되므로 **별도 `--reset` 은 불필요**합니다.
- 재매수 시에는 그 계좌에 새 매수가/수량을 기록하면 됩니다 (기록 ↔ null 복원 반복).
- 첫 실행, `--reset`, 또는 **전 계좌 매도 자동 리셋 직후** 첫 모니터링에서는 **현재 도달된
  모든 매수 구간이 한 번에** 알림으로 옵니다 (현재 상태 스냅샷 — 수동 리셋과 동일 동작).
  이후에는 새로 도달하는 구간/임박/매도만 알립니다.
- 기준가는 기본 `ATH`(역대 최고가)이며 **배당 조정 종가(Adj Close) 기준**으로 계산해
  TradingView 등 조정가 차트와 일치합니다. (분할·배당 자동 반영 — 2026-08-10 변경)

### 매도 알람 — 예상 수익률 기준 (앱 대시보드 + 서버)

대시보드의 **매도 상태 칩**(🚨 매도 / 🚀 임박 / ⏳ 대기)과 상단 **"🚨 매도 알람 N"** 카운트는
**각 사용자가 계좌별로 입력한 매수 예정가와 선택한 예상 수익률**(5/10/15/20%, 브라우저 localStorage) 기준으로
자동 판정됩니다. 서버 설정 없이도 사용자별로 동작하며, **기본 선택 수익률은 `SWING_TARGET_PCT`
설정값**(현재 20%)을 읽습니다 (이전엔 JS 하드코딩 10% — 2026-08-10 설정 기반으로 변경).

- **계좌별 매수 예정가 입력**: 세븐 스플릿에 맞춰 **계좌 1~7행**을 입력합니다 (각 $500).
  입력한 계좌만 계산/저장/푸시에 반영되고, 미입력 계좌는 무시됩니다 (오발송 방지).
- **매도 예정가 = 매수 예정가 × (1 + 예상 수익률/100)** — 계좌별 실시간 자동 계산.
- **판정**: 현재가 ≥ 매도 예정가 → **🚨 매도** (빨강) / 목표까지 `IMMINENT_GAP_PCT`(기본 5%p) 이내 →
  **🚀 임박** (호박) / 그 외 → **⏳ 대기** — 입력된 계좌 중 하나라도 도달하면 칩에 표시.
- **서버 설정 불필요**: owner가 `swing_config.json`에 `BUY_PRICE`를 기록하지 않아도 앱의 사용자별
  매도 알람은 항상 동작합니다.
- **사용자별 푸시 (OneSignal 태그)**: 계좌별 매도 예정가가 계산되면 구독에
  `swing_sell_{TICKER}_{ACCOUNT}` 태그로 등록됩니다. 서버 모니터가 현재가를 확인해 **자기 매도
  예정가 ≤ 현재가인 사용자에게만** 계좌별 푸시를 발송합니다 (메시지에 계좌 번호와 내 매도 예정가
  표시, 계좌별 1일 1회 중복 방지). 1번 계좌는 구형 단일 태그(`swing_sell_{TICKER}`)에도 동일하게
  기록되어 아직 새 앱을 열지 않은 기기의 기존 태그와 호환됩니다 (전환 갭 없음).
- **푸시 수신 조건**: 앱에서 🔔 알림을 구독하고, 앱을 한 번 이상 열어 계좌별 매도 예정가 태그가
  등록돼야 합니다 (태그 없이 구독만 한 사용자는 대상에서 제외).
- **전역 푸시: 제거됨 (2026-08-10)** — 기존에 `BUY_PRICE` 기준 공통 신호를 구독자 전체에게
  발송하던 동작은 지인 노출 문제로 차단했습니다. 푸시는 사용자별 태그 기준으로만 발송됩니다.
- **저장**: 계좌별 매수 예정가(`swing_buy_{TICKER}_{ACCOUNT}`)·예상 수익률(`swing_sell_{TICKER}`)은
  브라우저 localStorage에 기기별로 독립 저장되고, 매도 예정가가 계좌별 OneSignal 태그로 파생 등록됩니다.

#### 예시 (TQQQ 현재가 $74.47)

| 내 매수 예정가 | 예상 수익률 20% 매도 예정가 | 현재가 기준 상태 |
|---|---|---|
| (미입력 = 현재가 $74.47) | $89.36 | ⏳ 대기 |
| $60 | $72.00 | ⏳ 대기 |
| $55 | $66.00 | 🚨 매도 (이미 도달) |

### 실행 방법

```bash
python3 swing_alerter.py                    # 상태 출력 + 대시보드 HTML 저장
python3 swing_alerter.py --discord          # + Discord 일일 브리핑 발송
python3 swing_alerter.py --monitor          # 실시간 모니터 (변경분 알림만)
python3 swing_alerter.py --serve 8080       # 스마트폰 대시보드 서버 (같은 Wi-Fi)
python3 swing_alerter.py --reset TQQQ       # 알림 플래그 초기화 (전 계좌 매도 시 자동 — 특수 상황 수동 사용)
```

### 실시간 알림 (cron-job.org)

기존 `setup_cronjob_org.py` 로 스윙 전용 잡을 생성합니다:

```bash
export GITHUB_EVENT_TYPE=swing-monitor
export GITHUB_WORKFLOW_PATH=.github/workflows/swing_alerter.yml
export JOB_TITLE="Swing alerter realtime monitor"
python3 setup_cronjob_org.py   # CRONJOB_ORG_API_KEY/GITHUB_PAT/GITHUB_OWNER/GITHUB_REPO 필요
```

### 모바일 대시보드 GitHub Pages 배포 (스마트폰 어디서나 접속)

`swing_alerter.yml` 이 일일 실행 시 대시보드를 **`gh-pages` 브랜치에 `index.html` 로
자동 배포**합니다 (장중 실시간 폴링 dispatch 에서는 배포하지 않아 배포 횟수를 아낌).

**1회성 설정 (약 1분):**

1. 변경 사항을 push 한 뒤, 워크플로우가 `gh-pages` 브랜치를 만들 때까지 대기
   (매일 23:40 UTC 자동 실행, 또는 Actions 탭에서 수동 실행 `workflow_dispatch`).
2. 저장소 **Settings → Pages** 에서:
   - Source: **Deploy from a branch**
   - Branch: `gh-pages` / `/(root)` → **Save**
3. 배포 후 주소: **`https://bs02089-sys.github.io/strategy_engine/`**
   (`swing_config.json` 의 `PAGES_URL` 에 반영 — 대시보드 상단에 🌐 라이브 링크 표시)

> 참고: Pages 대시보드는 **매일 갱신되는 스냅샷**입니다 (장 마감 후 데이터). 장중
> 실시간 알림(매수 구간 도달/임박/매도)은 Discord 푸시가 담당하므로, Pages 는
> 스마트폰에서 상태 확인용으로 사용하세요.

### 스마트폰 홈 화면 추가 (앱처럼 사용)

대시보드는 PWA(웹앱)로 동작하므로 홈 화면에 **앱처럼 설치**할 수 있습니다
(`swing_manifest.webmanifest` + `swing_icon.png` — 배포 시 함께 업로드, 스탠드얼론 실행).

- **아이폰 (Safari)**: 대시보드 열기 → 하단 **공유 버튼** → **홈 화면에 추가**
- **안드로이드 (Chrome)**: 대시보드 열기 → 메뉴(⋮) → **앱 설치** 또는 **홈 화면에 추가**

설치 후에는 전용 아이콘으로 실행되고 브라우저 주소창 없이 전체 화면(스탠드얼론)으로 열립니다.

---

## 🔗 연동 시스템

### MarketStageSystem.py (portfolio_config.json 공유)
- `portfolio_config.json`의 `POSITIONS` 키에서 티커 목록을 읽어 시장 바닥 단계(0~5) 감지
- `DCA_MA_strategy`와 **설정 파일 공유** (`resolve_discord_config()` 공유)
- 감지된 바닥 단계(Stage 5)는 `market_state.json`에 기록 → **ATH DCA 3차 트리거로 사용**

### bear_market_signals.py (독립 실행)
- 약세장 신호를 분석하여 `signal_report.json`에 리스크 점수 기록
- 시장 리스크 점수(0~14)를 브리핑에 포함

### cron-job.org (외부 스케줄러 — 실시간 알림)
- GitHub Actions `schedule` 크론의 best-effort 지연을 우회하는 정확한 N분 알람
- `repository_dispatch`(event_type: `ath-dca-monitor`)로 워크플로우 즉시 실행
- 설정 자동화: `setup_cronjob_org.py` — 상세: `REALTIME_ALERT_SETUP.md`

---

## 📝 참고 사항

- **NYSE 휴장일**: `pandas_market_calendars` 라이브러리로 자동 계산
- **시간 기준**: 모든 시간은 `America/New_York` 기준
- **yfinance 캐시 전략**: 서로 다른 period 파라미터로 호출하여 캐시 충돌 방지
- **정산 버퍼**: 장 마감 후 15분 버퍼 — 미정산 데이터 사용 방지
- **Sigma 갱신 주기**: 90일(약 63거래일) 또는 설정(VOL_METHOD/EWMA_LAMBDA) 변경 시
- **실시간 알림**: GitHub Actions 스케줄은 60일간 활동 없으면 자동 비활성화되지만,
  cron-job.org 폴링이 매일 커밋을 만들어내므로 자연히 유지됩니다.

---

## 📄 관련 문서

| 문서 | 설명 |
|------|------|
| [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) | ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거 |
| [DUAL_MODE_SUMMARY.md](DUAL_MODE_SUMMARY.md) | 듀얼 모드(LOC ↔ ATH_DCA) 시스템 전체 구조 요약 |
| [REALTIME_ALERT_SETUP.md](REALTIME_ALERT_SETUP.md) | 실시간 ATH DCA 알림 설정 가이드 |

---

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
