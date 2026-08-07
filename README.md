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
| **fvg_signal_bot.py** | 📌 **FVG 반자동 매매 스캐너** — HTF(15분) 추세 필터 + 1분봉 CHoCH/FVG 진입 모델 + 구조 기반 손절, Discord 알림(멘션 3회), 파일 기반 중복 알림 방지, **청산(매도) 알림**(TP/손절/당일 마감 추적) |
| **fvg_bot_backtest.py** | FVG 전략 백테스트 — 5분봉 근사(1분봉은 yfinance 7일 한도), 당일 마감/overnight 모드, 승률·MDD·PF 지표 |
| **fvg_bot_eval.py** | FVG 봇 **실전 평가** — fvg_positions.json 기반 승률/평균익절·손절/PF/총수익/MDD/청산 사유(TP·SL·마감) 분포 리포트 (종목·기간 필터) |
| **setup_fvg_cron.py** | FVG 봇 로컬 크론 설정 자동화 (미국 장중 매분, 설치/--list/--remove/--dry-run) |
| **fvg_local_cron.sh** | 로컬 크론 래퍼 — ET 장중 확인 + git 알림 상태(fvg_alerts.json) 동기화 후 실행 |
| **MarketStageSystem.py** | 독립적인 시장 단계 시스템 — 바닥 단계 감지 |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **portfolio_config.json** | 📌 **포트폴리오 설정** — 포지션, Sigma, DCA 파라미터, 모드 상태 |
| **TRIGGER_OPTIMIZATION_SUMMARY.md** | ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거 |
| **DUAL_MODE_SUMMARY.md** | 듀얼 모드(LOC ↔ ATH_DCA) 구조 요약 문서 |
| **REALTIME_ALERT_SETUP.md** | 실시간 ATH DCA 알림 설정 가이드 |
| **FVG_NAMYU_SETUP.md** | FVG 봇 나무증권 무인 운영 가이드 — 시세포착주문(손절/익절) + MOC 마감 청산 설정법 |
| ~~MarketStage_config.json~~ | (제거됨 — portfolio_config.json으로 통합) |
| **sigma_history.csv** | Sigma 갱신 이력 (런타임 자동 생성 — 추적 제외) |
| **market_state.json** | 시장 단계 상태 정보 (자동 생성) |
| **signal_report.json** | 시장 리스크 점수 (자동 생성) |
| **fvg_alerts.json** | FVG 봇 알림 상태 (자동 생성 — 동일 FVG 중복 알림 방지, 로컬↔GHA 공유) |
| **fvg_positions.json** | FVG 봇 포지션 상태 (자동 생성 — 진입 기록 → TP/손절/당일 마감 청산 알림 추적, 로컬↔GHA 공유, CLOSED 45일 보존 → `fvg_bot_eval.py` 평가용) |
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

python3 setup_cronjob_org.py --dry-run          # 생성 전 미리보기
python3 setup_cronjob_org.py                    # 실제 생성 (장중 10분 간격 기본)
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

### `fvg_signal.yml` — FVG 신호 봇 (TQQQ/SOXL, 클라우드 백업)

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 장중 5분 폴링 (13:00~21:00, 월~금) | 로컬 크론(매분)의 클라우드 백업 — PC가 꺼져 있어도 실행 |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 (테스트) |

> - 1분봉 CHoCH → FVG 중간점 풀백 진입 모델 + HTF(15분) 추세 필터 (유튜브 Craig Percoo 전략).
> - 알림만 전송하며 실제 주문은 자동 실행하지 않습니다 (수동 매매). `DISCORD_USER_ID` 설정 시 멘션 3회.
> - 중복 알림은 `fvg_alerts.json` 파일 기반 쿨다운(1시간)으로 방지 — 로컬 크론과 git으로 상태 공유.
> - **청산(매도) 알림**: 진입 알림 시 포지션을 `fvg_positions.json`에 기록 → 이후 익절(TP) 도달·손절(SL) 도달·당일 마감 임박(ET 15:40) 시 매도 알림 자동 전송 (한 봉에 겹치면 손절 우선).
> - **무인(수면) 운용**: 나무증권 기준 주문 3건 — ①시세포착주문 신규편입(손실제한+이익실현 % 동시 등록, 매수 체결 순간 서버 감시 자동 시작 — 바이&셀은 손절 없음으로 제외) + ②지정가 매수(FVG 중간점) + ③MOC(마감 경매 자동 청산)로 자는 동안 청산 자동 처리, 이후 TP/손절/MOC 알림은 아침에 확인하는 기록용. 상세: `FVG_NAMYU_SETUP.md`
> - 백테스트: `fvg_bot_backtest.py` (5분봉 근사 — **당일 마감 운용이 핵심**, 야간 보유 시 MDD 급증).

### FVG 봇 배포 — 로컬 크론 (매분, 주력) + GitHub Actions (5분, 백업)

1분봉 전략은 신호가 분 단위로 생기고 사라지므로 **로컬 PC에서 매분 실행**이 주력이고,
PC가 꺼져 있어도 GitHub Actions 백업(`fvg_signal.yml`, 장중 5분 폴링)이 이어받습니다.
두 경로는 `fvg_alerts.json`(알림 쿨다운)·`fvg_positions.json`(포지션 추적)을 git으로 공유해 중복 알림을 차단합니다. 실제 매매는
알림을 받은 사용자가 직접 실행합니다 (자동 주문 없음).

```bash
python3 setup_fvg_cron.py --dry-run   # 설치될 크론 라인 미리보기
python3 setup_fvg_cron.py             # 설치 (미국 장중 매분, KST 22~06시 월~토)
python3 setup_fvg_cron.py --list      # 현재 crontab 목록
python3 setup_fvg_cron.py --remove    # 제거
```

> - 로컬 래퍼(`fvg_local_cron.sh`)가 ET 장중을 1차 확인 → 장중 밖엔 파이썬 실행 없음 (이중 방어).
> - GitHub Actions는 `secrets.DISCORD_WEBHOOK`/`DISCORD_USER_ID`로 알림을 보냅니다 (로컬 .env와 무관).
> - 장중 알림은 Discord 멘션 3회 포함 — 잠든 사이에도 모바일 알림이 울립니다.
> - **당일 마감 운용**: 진입 후 당일 15:55 ET까지 미해결 시 청산 권장 (백테스트 근거 — 야간 보유 시 MDD 급증).

### 실전 평가 (fvg_bot_eval.py)

진입 알림마다 `fvg_positions.json`에 자동 기록되는 실전 데이터를 읽어 백테스트와 동일한 양식으로
평가 리포트를 만듭니다 (CLOSED 포지션만 집계, 수수료/슬리피지 미반영).

```bash
python3 fvg_bot_eval.py                 # 전체 평가 리포트 (승률/평균익절·손절/PF/총수익/MDD)
python3 fvg_bot_eval.py --days 30       # 최근 30일 청산분만
python3 fvg_bot_eval.py --ticker TQQQ   # 특정 종목만
python3 fvg_bot_eval.py --path test.json  # 다른 포지션 파일로 테스트
```

> - 청산 사유 분포(TP 익절 / SL 손절 / DAY_CLOSE 당일 마감)와 종목별 통계 포함.
> - 미청산(OPEN) 포지션은 평가에서 제외하고 별도로 표시합니다.
> - 포지션은 **CLOSED 후 45일간 보존**되므로 한 달(20영업일) 단위 평가가 가능합니다.

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
| [FVG_NAMYU_SETUP.md](FVG_NAMYU_SETUP.md) | FVG 봇 나무증권 무인 운영 가이드 — 시세포착주문 + MOC 설정법 |

---

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
