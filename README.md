# DCA LOC Strategy

미국 주식 시장 **Sigma 기반 LOC 매수 목표가 자동 계산** 및 **디스코드 브리핑 자동 발송** 시스템입니다.

> ⚠️ **2026-08-16 단일 논리 재구성**: 이동평균선(MA 레짐 필터/MA 정렬) · RSI+거래량 ·
> ATH 하락분할 DCA(비상 모드) · STAGE5 · 회복 재진입 · 실시간 모니터(`--ath-monitor`)를
> **전부 삭제**하고, **순수 LOC 지정가 5분할 매수** 하나로 통일했습니다. (상세: [STRATEGY_RULES.md](STRATEGY_RULES.md))
> 2026-08-17 — 분할 수 20→**5로 전환** (백테스트 `--sweep-splits` 결론: 강세장=LOC 추천 국면에서 5~10분할이 평균 수익률 최고)

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
- [AI 에이전트 영어 공부법](#-ai-에이전트-영어-공부법-english_study)
- [라이선스](#-라이선스)

---

## 📌 개요

**DCA LOC Strategy**는 매일 미국 장 마감 후 정해진 시간에 자동 실행되어:
1. 포트폴리오에 등록된 티커(TQQQ)의 변동성을 계산/갱신
2. Sigma 기반 LOC 매수 목표가 산출
3. 정규장 **LOC 지정가 주문 신호** — 체결 추적은 증권앱 + 엑셀에서 관리 (봇 미추적)
4. 로테이션 포지션 만기 관리
5. 종합 브리핑을 **Discord**로 전송

---

## 🚀 주요 기능

### 1️⃣ Sigma(LOC) 목표가 계산
- **EWMA** 또는 **역사적 표준편차** 방식의 변동성 계산
- `ENTRY_MULTIPLIER` × σ 만큼 하락한 가격을 LOC 매수 목표가로 설정
- 설정된 LOOKBACK_DAYS 기준으로 변동성 자동 갱신 (90일 주기, 또는 설정 변경 시 즉시 갱신)
- Sigma 갱신 이력은 `sigma_history.csv`에 기록

### 2️⃣ 순수 LOC 지정가 5분할 DCA (단일 논리 — 2026-08-16, 분할 20→5 전환 2026-08-17)
- **LOC 매수가** = 전일 종가 × (1 − σ × `ENTRY_MULTIPLIER`) — 유일한 매수 논리
- 정규장에서 이 가격으로 **LOC 지정가 주문** → 체결 여부는 증권앱 + 엑셀로 관리 ($10,000 × 최대 **5차** — 적립 전용, 매도 없음)
- ⚠️ **체결 추적은 봇이 하지 않음 (2026-08-16)** — 분할 예산/회차는 사용자 **엑셀이 단일 소스** (봇은 실제 주문 여부를 알 수 없어 자동 카운터가 부정확)
- 브리핑은 **LOC 매수가 하나만** 제공 (`🎯 [Action] LOC Buy: $X`)

### 3️⃣ 포지션 유형별 전략

| 유형 | 전략 |
|------|------|
| **LONG_YEAR** | 기계적 LOC 전략 — 무조건 매수 신호 활성 (TQQQ) |
| **ROTATION_3M** | 기계적 LOC 전략 + 만기 초기화 (MA 신호 제거 — 2026-08-16) |
| **END_DEC** | 기계적 LOC 전략 (MA 신호 제거 — 2026-08-16) |

### 4️⃣ Discord 브리핑
- 매일 정해진 시간에 Discord Webhook으로 종합 브리핑 전송
- 각 티커별: 현재가, Sigma, LOC 목표가, 전고점 대비 하락률/회복률, 매수 신호
  (전고점 = **원시 고가(High, 미조정)·전체 이력** 기준 — 공용 함수
  `LOC_DCA_strategy.get_all_time_high` 로 계산 (Google Finance `high52` 와 동일), 2026-09-12 통합)
- 매월 1일 월간 작동 확인 Ping 전송

### 9️⃣ 로테이션 포지션 자동 초기화
- ROTATION_3M 포지션: 설정된 영업일(기본 63일) 경과 후 자동 초기화 + Sigma 재계산

---

## 🏗 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│              GitHub Actions (스케줄러)                           │
│  야간 실행: 매일 23:30 UTC — 통합 브리핑 1건 (월~금)         │
└─────────────────┬───────────────────────────────────────────────┘
                  │ 실행
┌─────────────────▼───────────────────────────────────────────────┐
│                  LOC_DCA_strategy.py                            │
│                                                                  │
│  1. portfolio_config.json 불러오기                                │
│  2. Sigma 갱신 (오래되었거나 설정 변경 시)                       │
│  3. 전일 종가 및 LOC 목표가 계산 (티커별)                        │
│  4. LOC 매수가 계산 — 정규장 지정가 주문 신호                   │
│  5. 로테이션 만기 확인                                           │
│  6. 브리핑 작성 → Discord 전송 (LOC 5분할 상태 포함)            │
│  7. 월간 Ping (매월 1일)                                        │
└──────┬──────────────┬──────────────┬──────────────┐
       │              │              │              │
       ▼              ▼              ▼              ▼
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│yfinance    │ │Discord     │ │signal_repo-│ │LOC_DCA_    │
│(종가/변동성)│ │Webhook     │ │rt.json     │ │USED_SPLITS │
└────────────┘ │(브리핑     │ │(리스크 점수│ │(체결 이력) │
              └────────────┘ │)           │ └────────────┘
                             └────────────┘
```

---

## 📁 파일 구성

| 파일 | 설명 |
|------|------|
| **LOC_DCA_strategy.py** | 📌 **통합 완결판** — 실전 엔진(순수 LOC 5분할 매수/Discord 브리핑) + 백테스트 + `--signal` 실시간 신호 |
| **LOC_DCA_strategy_flowchart.py** | 시스템 전체 플로우차트 문서 |
| **loc_sweep_backtest.py** | 🆕 LOC 분할 수 스윕 백테스트 — '1년 매수 + 홀딩' 구조에서 몇 차 분할이 최적인지 검증 |
| ~~swing_alerter.py · swing_config.json · swing_personal.json · swing_state.json · swing_dashboard.html · swing_split_backtest.py · loc_vs_swing_backtest.py · NAMU_SWING_SETUP.md · setup_cronjob_org.py · sw.js · OneSignalSDKWorker.js · tsconfig.json · tsconfig.dashboard.json · onesignal.d.ts · check_dashboard_js.py · package.json~~ | (제거됨 — 스윙 투자 알리미 삭제, 2026-10-05 · cron-job.org `swing-monitor` 잡은 콘솔에서 수동 삭제) |

| ~~MarketStageSystem.py · market_state.json · market_stage_tracker.yml~~ | (제거됨 — 판정 결과가 매수/매도 트리거로 미사용, 2026-09-29) |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **portfolio_config.json** | 📌 **포트폴리오 설정** — 포지션, Sigma, LOC 분할 파라미터 |
| ~~TRIGGER_OPTIMIZATION_SUMMARY.md~~ | (제거됨 — ATH_DCA 전략 삭제, 2026-08-16) |
| ~~DUAL_MODE_SUMMARY.md~~ | (제거됨 — 듀얼 모드 삭제, 2026-08-16) |
| ~~REALTIME_ALERT_SETUP.md~~ | (제거됨 — 실시간 ATH DCA 모니터 삭제, 2026-08-16) |
| ~~dollar_alerter.py · dollar_config.json · dollar_personal.json · dollar_state.json · dollar_dashboard.html · dollar_split_backtest.py~~ | (제거됨 — 달러 알리미 삭제, 2026-08-31 · cron-job.org `dollar-monitor` 잡은 콘솔에서 수동 삭제) |
| ~~MarketStage_config.json~~ | (제거됨 — portfolio_config.json으로 통합) |
| **sigma_history.csv** | Sigma 갱신 이력 (런타임 자동 생성 — 추적 제외) |
| **signal_report.json** | 시장 리스크 점수 (자동 생성) |
| **requirements.txt** | Python 의존성 패키지 목록 |
| **pyrightconfig.json** | Python 타입 검사 설정 (VSCode Pylance) |
| **english_study/** | 🧑‍🎓 **AI 에이전트 영어 공부법** — 여행 회화 덱 + 복습 CLI (자세한 매뉴얼은 `english_study/README.md`) |

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
            "ENTRY_MULTIPLIER": 1.43,
            "VOL_METHOD": "EWMA",
            "EWMA_LAMBDA": 0.94,
            "DAILY_SIGMA": 0.043,
            "LAST_SIGMA_UPDATE": "2026-08-15",
            "START_DATE": "2026-07-25",
            "INVEST_TYPE": "LONG_YEAR",
            "ALLOCATION_PCT": 10,
            "LOC_DCA": {
                "SPLITS": 5
            }
        }
    },
    "STRATEGY": { "CYCLE_YEARS": 2, "BUY_DURATION_DAYS": 252, "HOLD_DURATION_DAYS": 252 }
}
```

> 참고: `LOC_DCA` 블록(SPLITS)은 **백테스트 기본값**(분할 수)용입니다. 차수당 금액($10,000)은
> 코드 상수이며 config에 없습니다 — 실전 체결 추적·분할 예산은 봇이 하지 않으며
> **사용자 엑셀이 단일 소스**입니다 (2026-08-16).

#### 포지션 설정 항목

| 항목 | 설명 |
|------|------|
| `LOOKBACK_DAYS` | 변동성 계산 기간 (기본 252 = 1년) |
| `ENTRY_MULTIPLIER` | LOC 목표가 승수 — σ × 승수 만큼 하락한 가격이 매수 목표 |
| `VOL_METHOD` | 변동성 계산 방식: `EWMA` (기본) 또는 `STD` |
| `EWMA_LAMBDA` | EWMA 감쇠 계수 (기본 0.94) |
| `INVEST_TYPE` | 투자 유형: `LONG_YEAR` / `ROTATION_3M` / `END_DEC` |
| `ALLOCATION_PCT` | 포트폴리오 내 비중 |
| `LOC_DCA` | LOC 분할 수 (`SPLITS`=5) — 백테스트 기본값 (차수당 금액 $10,000은 코드 상수) |
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
python3 LOC_DCA_strategy.py

# 특정 함수만 테스트
python3 -c "
from LOC_DCA_strategy import get_prev_close, calculate_loc_price
import json
with open('portfolio_config.json') as f:
    cfg = json.load(f)

close, date = get_prev_close('TQQQ')
print(f'TQQQ 종가: \${close} ({date})')

loc = calculate_loc_price('TQQQ', close, cfg)
print(f'TQQQ LOC 목표가: \${loc}')
"
```

### 백테스트 실행 (순수 LOC 5분할)

```bash
python3 LOC_DCA_strategy.py --backtest                    # TQQQ (LOC 5분할)
python3 LOC_DCA_strategy.py --backtest --fee 0.001        # 수수료 0.1% 반영
```

상세 사용법(신호 모드 포함): [LOC 5분할 전략](#loc-5분할-전략-loc_dca_strategypy)

### 플로우차트 문서 보기

```bash
python3 LOC_DCA_strategy_flowchart.py
```

---

## 🤖 GitHub Actions 자동화

### `loc_dca_strategy.yml` — 정기 브리핑 + LOC 5분할 신호

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:30 (월~금) | 장 마감 후 **통합 브리핑 1건** 발송 (LOC 5분할 신호는 브리핑에 통합) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

> - 23:30 UTC 실행 시 `LOC_DCA_strategy.py`(통합 브리핑) 1건만 Discord로 발송합니다. LOC 실행 액션(▶)이 티커 블록에 포함되며, `--signal`은 콘솔 로그 확인용으로만 실행됩니다.
> - 신호 메시지: 종가·날짜 · LOC 매수가 · 오늘 LOC 도달 여부 · 액션을 한 번에 전송.

### LOC 5분할 운영 루틴 (LS증권 — 2026-09-12)

> LS증권은 알림을 보내지 않습니다 — **알림·가격 = LOC 브리핑(Discord 09:00 KST), 체결 = LS증권 앱 수동**.

1. **09:00 KST — 브리핑 확인**: Discord LOC 브리핑의 `🎯 [Action] LOC Buy: $X`
   (= 전일 종가 × (1 − σ × `ENTRY_MULTIPLIER`), 현재 σ 0.043·승수 1.43 → **전일 종가 −6.15%**)
2. **그날 밤 정규장 — LS증권에서 TQQQ LOC(장마감 지정가) 주문을 $X로 접수**
   - LOC = **마감가 ≤ 지정가일 때 종가로 체결** (지정가보다 싸게 끝나면 종가로 유리 체결) — 엔진 판정과 동일 규칙
   - ⚠️ 일반 지정가로 걸면 장중 터치 시 즉시 체결되어 판정(마감가 기준)과 어긋난다 → **LOC 유형 사용**
   - 접수 마감이 정규장 마감 전(증권사별 10~30분 전) — 09:00 브리핑이면 여유 충분, LS증권 앱 안내 시각 기준
3. **미체결 시**: LOC 주문은 당일 소멸 → 다음 브리핑의 **새 가격**으로 재주문 (가격은 매일 다시 계산 — 고정 래더 아님)
4. **체결 확인**: 다음 날 증권앱 확인 → **엑셀에 컬러 표시로 기록** (차수 관리 = 엑셀 단일 소스, 봇 미추적)
5. **총 5차까지**: 하루 최대 1차. 급락장에선 며칠 연속 체결될 수 있으니 '쓴 차수' 표시를 먼저 갱신 (중복 매수 방지)

- 차수당 금액은 코드 상수 $10,000(백테스트 기본값) — **실제 예산/잔여 차수는 사용자 엑셀 단일 소스**, 브리핑에 회차 표시 없음
- σ는 90일마다 자동 재계산(`sigma_history.csv`) — 다음 갱신 2026-11-13경 (σ가 바뀌면 LOC 하락폭도 바뀜)
- 도달 빈도(백테스트, 종가 기준 σ×1.43): **연 ~22회** — 대략 2~3주에 1차, 5차를 채우는 데 평균 2~3개월(편차 큼)
- ⚠️ `--signal`은 **이미 끝난 세션**의 LOC(그 전일 종가 기준)를 보여준다 — 주문에 쓸 값은 **브리핑**뿐
  (yfinance 일봉이 아직 확정되지 않았으면 정규장 분봉 폴백으로 **최신 확정 세션**을 쓴다 — 2026-09-22.
  수정 전에는 마지막 봉 Close=NaN 때문에 `--signal` 이 하루 낡은 세션·낡은 LOC($66.99 vs $68.17)를 보여줬다)

### `bear_market_signals.yml` — 약세장 신호

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:00 (월~금) | 시장 리스크 평가 |

### 환경 변수 (GitHub Secrets)

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK` | Discord Webhook 주소 |
| `DISCORD_USER_ID` | Discord 사용자 ID (멘션용) |

---

## 📊 백테스트

백테스트는 **`LOC_DCA_strategy.py`** 하나로 수행합니다 — **순수 LOC 5분할 DCA**(승수 1.43,
매수 $10,000×5, MA 필터 없음)를 검증하고, `--signal`로 실시간 신호도 확인합니다
(상세: [LOC 5분할 전략](#loc-5분할-전략-loc_dca_strategypy)).

### 사용 기술
- 일간 로그수익률 기반 변동성(σ) 계산
- EWMA(λ=0.94) 가중치 적용
- LOC 목표가: `전일종가 × (1 - σ × 승수)`
- 매수 조건: 당일 종가(마감가) ≤ LOC 목표가 (최대 5차 — 적립 전용, 매도 없음) —
  LOC 지정가는 장 마감가가 지정가 이하일 때만 체결 (2026-08-17 수정)

### LOC 5분할 전략 (`LOC_DCA_strategy.py`)

실전 엔진 + 백테스트/신호를 통합한 **완결판 단일 파일**입니다. 티커별 기본 설정:

| 티커 | 기본 설정 | 10년 결과 | 용도 |
|------|-----------|-----------|------|
| TQQQ | LOC 5분할 ($10,000×5, 승수 1.43) | +1,317.0% / MDD **-81.7%** (2026-08-17 종가 기준 재측정) | 순수 적립 — 매도 규칙 없음 |

```bash
# 백테스트
python3 LOC_DCA_strategy.py --backtest                # TQQQ (LOC 5분할)
python3 LOC_DCA_strategy.py --backtest --fee 0.001    # 수수료 0.1% 반영

# 실시간 신호 (장 마감 후) — --discord로 Discord 발송 (GitHub Actions 자동화)
python3 LOC_DCA_strategy.py --signal
python3 LOC_DCA_strategy.py --signal --discord       # TQQQ 신호를 Discord로
python3 LOC_DCA_strategy.py --signal --discord --all  # 전 종목 단일 메시지 (수동 확인용 — 워크플로우는 브리핑 1건만 발송)
```

### LOC 분할 수 스윕 (`loc_sweep_backtest.py`)

'1년 매수 + 4년 홀딩' 구조에서 순수 LOC 5분할의 분할 수가 최적인지 검증합니다 — 5년 윈도우의
1년차에만 시그마 LOC 트리거로 최대 N 회 매수(회당 $50,000/N), 이후 홀딩합니다.

```bash
python3 loc_sweep_backtest.py                              # 기본: TQQQ, 1,5,10,20,52분할
python3 loc_sweep_backtest.py --buy-years 2                # 2년차까지 매수 후 홀딩
python3 loc_sweep_backtest.py --sweep-counts 1,5,10,20,52   # 검사할 분할 수 목록
```

- 모델: 매도 없음 · 총 예산 $50,000 · 가격은 **원시 종가(미조정)** — 실전 판정(확정 종가)과 동일 계열
  (`LOC_DCA_strategy.py --backtest` 는 배당 조정 종가를 쓰므로 절대 수치가 다름)
- ⚠️ 결과: 분할 수가 적을수록 평균 수익률이 높다 — 1분할(≈일시 매수≈B&H) +404% >
  **현행 5분할 +389%** > 10분할 +383% > 20분할 +298% > 52분할 +125% (1년차 트리거 부족으로 유휴).
  MDD 는 1~10분할 동일(-75.8%) — 분할 수는 MDD와 무관하고 '강세장에서 늦은 투입'이 수익률 차이의 원인.
  최근 5년(크래시 윈도우)만 보면 반대로 52분할이 최고(+97.8%) — 윈도우 의존성. → **2026-08-17 실전 20→5분할 채택**
- ⚠️ 수치는 **2026-09-12 재측정**(LOC 판정을 마감가(종가) 기준으로 수정 + 가격 계열을 실전과 동일한
  원시 종가로 통일) — 구 배당 조정 기준 수치는 무효

### 실전 반영 — 순수 LOC 5분할 (2026-08-16 단일 논리 · 2026-08-17 20→5분할 전환)

MA/RSI/ATH_DCA 등 로직을 섞던 방식을 버리고 **하나의 논리**로 재구성했습니다
(알림 신호 방식 — 실제 주문 자동 실행은 없음):

- **LOC 매수가** = 전일 종가 × (1 − σ × 승수) — 당일 종가(마감가) ≤ LOC → **1차 체결**
  (LOC 지정가 — 장 마감가 ≤ 지정가일 때만 체결, 2026-08-17 수정)
- $10,000 × **최대 5차** — 5차 소진 시 매수 중단 (적립 전용, 2026-08-17 전환)
- **체결 추적 없음 (2026-08-16)** — 정규장 LOC(마감가 체결) 지정가 주문 후 체결 여부는 증권앱 확인 + 엑셀 기록 (봇 미추적)
- 일일 브리핑은 `• 🎯 [Action] LOC Buy:` 라인 하나로 **LOC 매수가만** 안내 (분할 예산/회차 표시 없음)
- **매도 규칙 없음** — 순수 적립 (매도 신호 자체가 발생하지 않음)

---

## 🧑‍🎓 AI 에이전트 영어 공부법 (english_study)

유튜브의 챗봇 기반 영어 공부법과 달리, **코딩 에이전트**(대화 + 코드 실행)를 활용한
여행 회화 공부 시스템입니다. 역할극은 에이전트와 채팅으로, 반복 학습은 스크립트가 담당합니다.

- **덱**: `english_study/phrases.json` — 공항/호텔/식당/카페/교통/길 찾기/쇼핑/응급/스몰토크 9개 상황 58개 표현
- **루틴 순서**: `learn`(표현 학습) → 역할극(활용) → `quiz`(확인) → `review`(간격 반복)
- **학습**: `python3 study.py learn [상황]` — 역할극 전에 상황별 표현을 영어+뜻+팁으로 먼저 읽기
- **복습**: `python3 study.py review` — Leitner 간격 반복 (1→3→7→14→30일, 틀리면 리셋)
- **퀴즈**: `python3 study.py quiz [상황]` — 한국어 → 영어 드릴, 틀린 카드는 복습 큐에 자동 등록
- **진척**: `progress.json` (개인 데이터 — gitignore 대상, 자동 생성)

상세 매뉴얼(하루 루틴 · 역할극 규칙 · 프롬프트 모음): [english_study/README.md](english_study/README.md)

---

## 🔗 연동 시스템

### bear_market_signals.py (독립 실행)
- 약세장 신호를 분석하여 `signal_report.json`에 리스크 점수 기록
- 시장 리스크 점수(0~14)를 브리핑에 포함

### cron-job.org (외부 스케줄러)
- 스윙 알리미 실시간 잡("Swing alerter realtime monitor")은 코드 삭제로 사라지지 않으므로
  **cron-job.org 콘솔에서 수동 삭제 필요** (2026-10-05).
- 기존 ATH DCA 실시간 잡("ATH DCA realtime monitor")도 콘솔에서 수동 삭제 필요 (2026-08-16 — `--ath-monitor` 삭제)

---

## 📝 참고 사항

- **NYSE 휴장일**: `pandas_market_calendars` 라이브러리로 자동 계산
- **시간 기준**: 모든 시간은 `America/New_York` 기준
- **yfinance 캐시 전략**: 서로 다른 period 파라미터로 호출하여 캐시 충돌 방지
- **정산 버퍼**: 장 마감 후 15분 버퍼 — 미정산 데이터 사용 방지
- **Sigma 갱신 주기**: 90일(약 63거래일) 또는 설정(VOL_METHOD/EWMA_LAMBDA) 변경 시

---

## 📄 관련 문서

| 문서 | 설명 |
|------|------|
| [STRATEGY_RULES.md](STRATEGY_RULES.md) | 전략 규칙 (순수 LOC 5분할 DCA) |
| [LOC_DCA_strategy_flowchart.py](LOC_DCA_strategy_flowchart.py) | 시스템 전체 플로우차트 |

---

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
