# DCA LOC Strategy

미국 주식 시장 **Sigma 기반 LOC 매수 목표가 자동 계산** 및 **디스코드 브리핑 자동 발송** 시스템입니다.

> ⚠️ **2026-08-16 단일 논리 재구성**: 이동평균선(MA 레짐 필터/MA 정렬) · RSI+거래량 ·
> ATH 하락분할 DCA(비상 모드) · STAGE5 · 회복 재진입 · 실시간 모니터(`--ath-monitor`)를
> **전부 삭제**하고, **순수 LOC 지정가 20분할 매수** 하나로 통일했습니다. (상세: [STRATEGY_RULES.md](STRATEGY_RULES.md))

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

### 2️⃣ 순수 LOC 지정가 20분할 DCA (단일 논리 — 2026-08-16)
- **LOC 매수가** = 전일 종가 × (1 − σ × `ENTRY_MULTIPLIER`) — 유일한 매수 논리
- 정규장에서 이 가격으로 **LOC 지정가 주문** → 체결 여부는 증권앱 + 엑셀로 관리 ($2,500 × 최대 **20차** — 적립 전용, 매도 없음)
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
- 각 티커별: 현재가, Sigma, LOC 목표가, 전고점 대비 하락률/회복률, 매수/매도 신호
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
│  6. 브리핑 작성 → Discord 전송 (LOC 20분할 상태 포함)            │
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
| **LOC_DCA_strategy.py** | 📌 **통합 완결판** — 실전 엔진(순수 LOC 20분할 매수/Discord 브리핑) + 백테스트 + `--signal` 실시간 신호 |
| **LOC_DCA_strategy_flowchart.py** | 시스템 전체 플로우차트 문서 |
| **setup_cronjob_org.py** | cron-job.org 실시간 알림 설정 자동화 (생성/--list/--test-dispatch/--update-pat/--update-schedule) — 스윙 알리미(swing-monitor) 전용 |
| **swing_alerter.py** | 🆕 **스윙 투자 알리미** — MDD 구간 매수/매도 알림 + 모바일 대시보드 (유튜브 TQQQ 스윙 전략 재구현) |
| **swing_config.json** | 스윙 알리미 공용 설정 (사용자 소유 — MDD 구간/목표/포지션/푸시) |
| **swing_personal.json** | 🔒 스윙 알리미 **개인 포지션** (LOTS — 계좌별 BUY_PRICE/SHARES, 공용 알림에 노출 안 됨, 사용자 소유) |
| **swing_state.json** | 스윙 알리미 봇 상태 (ZONE_ALERTS/매도 플래그 — 봇 전용, 자동 관리) |
| **swing_dashboard.html** | 스윙 알리미 모바일 대시보드 (자동 생성) |
| **dollar_split_backtest.py** | 🆕 달러(USD/KRW) 매직 스플릿 전략 백테스트 — '97% 수익률' 주장 검증 (검증 결과: 세븐 스플릿 정통 해석만 바이앤홀드 우위) |
| **dollar_alerter.py** | 🆕 **달러 매직 스플릿 알리미** — 전일 종가 대비 -0.3% 하락 매수 신호 / 매수가 대비 +0.3% 익절 신호 + 모바일 대시보드 |
| **dollar_config.json** | 달러 알리미 공용 설정 (사용자 소유 — 매수/익절 파라미터/푸시) |
| **dollar_personal.json** | 🔒 달러 알리미 **개인 포지션** (LOTS — 계좌별 BUY_PRICE/SHARES, 사용자 소유) |
| **dollar_state.json** | 달러 알리미 봇 상태 (매수/익절 신호 플래그 — 봇 전용, 자동 관리) |
| **dollar_dashboard.html** | 달러 알리미 모바일 대시보드 (자동 생성) |
| **sw.js** | PWA 서비스 워커 — 통과형 fetch (설치형 앱용, strict 검사 대상) |
| **OneSignalSDKWorker.js** | OneSignal 웹 푸시 + PWA 통합 서비스 워커 (strict 검사 대상) |
| **tsconfig.json** | TypeScript strict 검사 설정 — `sw.js`/`OneSignalSDKWorker.js` (서비스 워커) |
| **tsconfig.dashboard.json** | TypeScript strict 검사 설정 — 대시보드 인라인 JS (DOM) |
| **onesignal.d.ts** | OneSignal 웹 푸시 SDK 전역 타입 스텁 (대시보드 JS 검사용) |
| **check_dashboard_js.py** | 대시보드 인라인 JS 추출 — `swing_alerter.py`의 `<script>` 블록을 `.typecheck/`로 분리 |
| **package.json** | `npm run typecheck` 스크립트 (typescript 의존성) |
| **MarketStageSystem.py** | 독립적인 시장 단계 시스템 — 바닥 단계 감지 |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **portfolio_config.json** | 📌 **포트폴리오 설정** — 포지션, Sigma, LOC 분할 파라미터 |
| ~~TRIGGER_OPTIMIZATION_SUMMARY.md~~ | (제거됨 — ATH_DCA 전략 삭제, 2026-08-16) |
| ~~DUAL_MODE_SUMMARY.md~~ | (제거됨 — 듀얼 모드 삭제, 2026-08-16) |
| ~~REALTIME_ALERT_SETUP.md~~ | (제거됨 — 실시간 ATH DCA 모니터 삭제, 2026-08-16) |
| ~~MarketStage_config.json~~ | (제거됨 — portfolio_config.json으로 통합) |
| **sigma_history.csv** | Sigma 갱신 이력 (런타임 자동 생성 — 추적 제외) |
| **market_state.json** | 시장 단계 상태 정보 (자동 생성) |
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
            "ENTRY_MULTIPLIER": 1.1,
            "VOL_METHOD": "EWMA",
            "EWMA_LAMBDA": 0.94,
            "DAILY_SIGMA": 0.043,
            "LAST_SIGMA_UPDATE": "2026-08-15",
            "START_DATE": "2026-07-25",
            "INVEST_TYPE": "LONG_YEAR",
            "ALLOCATION_PCT": 10,
            "LOC_DCA": {
                "SPLITS": 20,
                "BUY_AMOUNT": 2500
            }
        }
    },
    "STRATEGY": { "CYCLE_YEARS": 2, "BUY_DURATION_DAYS": 252, "HOLD_DURATION_DAYS": 252 }
}
```

> 참고: `LOC_DCA` 블록(SPLITS/BUY_AMOUNT)은 **백테스트 기본값**용입니다.
> 실전 체결 추적·분할 예산은 봇이 하지 않으며 **사용자 엑셀이 단일 소스**입니다 (2026-08-16).

#### 포지션 설정 항목

| 항목 | 설명 |
|------|------|
| `LOOKBACK_DAYS` | 변동성 계산 기간 (기본 252 = 1년) |
| `ENTRY_MULTIPLIER` | LOC 목표가 승수 — σ × 승수 만큼 하락한 가격이 매수 목표 |
| `VOL_METHOD` | 변동성 계산 방식: `EWMA` (기본) 또는 `STD` |
| `EWMA_LAMBDA` | EWMA 감쇠 계수 (기본 0.94) |
| `INVEST_TYPE` | 투자 유형: `LONG_YEAR` / `ROTATION_3M` / `END_DEC` |
| `ALLOCATION_PCT` | 포트폴리오 내 비중 |
| `LOC_DCA` | LOC 20분할 설정 (`SPLITS`=20, `BUY_AMOUNT`=2500) — 백테스트 기본값 |
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

> 참고: cron-job.org 실시간 알림(`setup_cronjob_org.py`)은 이제 **스윙 알리미 전용**입니다
> (ATH DCA 실시간 모니터 삭제 — 2026-08-16). 스윙 잡 생성은 아래
> [스윙 알리미 실시간 알림](#실시간-알림-cron-joborg) 섹션 참고.

### 백테스트 실행 (순수 LOC 20분할)

```bash
python3 LOC_DCA_strategy.py --backtest                    # TQQQ (LOC 20분할)
python3 LOC_DCA_strategy.py --backtest --fee 0.001        # 수수료 0.1% 반영
```

상세 사용법(신호 모드 포함): [LOC 20분할 전략](#loc-20분할-전략-loc_dca_strategypy)

### 플로우차트 문서 보기

```bash
python3 LOC_DCA_strategy_flowchart.py
```

---

## 🤖 GitHub Actions 자동화

### `loc_dca_strategy.yml` — 정기 브리핑 + LOC 20분할 신호

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 23:30 (월~금) | 장 마감 후 **통합 브리핑 1건** 발송 (LOC 20분할 신호는 브리핑에 통합) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

> - 23:30 UTC 실행 시 `LOC_DCA_strategy.py`(통합 브리핑) 1건만 Discord로 발송합니다. LOC 실행 액션(▶)이 티커 블록에 포함되며, `--signal`은 콘솔 로그 확인용으로만 실행됩니다.
> - 신호 메시지: 종가·날짜 · LOC 매수가 · 오늘 LOC 도달 여부 · 액션을 한 번에 전송.

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
| 예약 실행 | 매일 00:00 UTC = 09:00 KST (월~금) | 스윙 일일 브리핑 + 대시보드 갱신 (한국 아침 9시 고정) |
| repository_dispatch | 장중 N분 (cron-job.org) | `--monitor` 실시간 알림 (매수 구간 도달/임박/매도) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

### `dollar_alerter.yml` — 달러 매직 스플릿 알리미

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 00:00 UTC = 09:00 KST (월~금) | 달러 일일 브리핑 + 대시보드 갱신 (은행 영업 시작 전) |
| repository_dispatch | 장중 N분 (cron-job.org, `dollar-monitor`) | `--monitor` 실시간 신호 (매수/익절/임박 — 나무증권 달러 환전 시간 09:00~익일 02:00 KST 폴링) |
| 수동 실행 | 사용자 요청 시 | workflow_dispatch 수동 실행 |

> cron-job.org 잡 생성: `GITHUB_EVENT_TYPE=dollar-monitor JOB_TITLE="Dollar alerter realtime monitor" UTC_HOURS_START=0 UTC_HOURS_END=17 POLL_MINUTES=10 python setup_cronjob_org.py`
> (나무증권 환전 시간 09:00~익일 02:00 KST = UTC 00:00~17:00 — 점검 23:50~24:30 KST(14:50~15:30 UTC)는 코드 `_bank_hours_open` 게이트가 제외하므로 UTC_HOURS 는 넓게 잡는다, 2026-08-17 외환시장 연장 조사 반영)

### TypeScript strict 검사 게이트 (모든 워크플로우 공통)

모든 봇 워크플로우(`swing_alerter.yml`/`loc_dca_strategy.yml`/`bear_market_signals.yml`/`tracker.yml`)는
봇 실행 전에 **JS 수정 검사 게이트**를 통과해야 합니다 (2026-08-14):

```bash
npm ci --silent
npm run typecheck   # tsc strict + checkJs
```

- **대상**: `sw.js`/`OneSignalSDKWorker.js`(서비스 워커) + `swing_alerter.py`에 인라인된
  대시보드 JS (`check_dashboard_js.py`가 `.typecheck/`로 추출해 검사)
- **역할**: JS 수정이 배포(gh-pages) 전에 깨지지 않도록 하는 게이트 — 검사 실패 시
  해당 워크플로우 실행이 중단되어 잘못된 대시보드 배포를 막는다.

### 환경 변수 (GitHub Secrets)

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK` | Discord Webhook 주소 |
| `DISCORD_USER_ID` | Discord 사용자 ID (멘션용) |

---

## 📊 백테스트

백테스트는 **`LOC_DCA_strategy.py`** 하나로 수행합니다 — **순수 LOC 20분할 DCA**(승수 1.1,
매수 $2,500×20, MA 필터 없음)를 검증하고, `--signal`로 실시간 신호도 확인합니다
(상세: [LOC 20분할 전략](#loc-20분할-전략-loc_dca_strategypy)).

### 사용 기술
- 일간 로그수익률 기반 변동성(σ) 계산
- EWMA(λ=0.94) 가중치 적용
- LOC 목표가: `전일종가 × (1 - σ × 승수)`
- 매수 조건: 당일 저가 ≤ LOC 목표가 (최대 20차 — 적립 전용, 매도 없음)

### LOC 20분할 전략 (`LOC_DCA_strategy.py`)

실전 엔진 + 백테스트/신호를 통합한 **완결판 단일 파일**입니다. 티커별 기본 설정:

| 티커 | 기본 설정 | 10년 결과 | 용도 |
|------|-----------|-----------|------|
| TQQQ | LOC 20분할 ($2,500×20, 승수 1.1) | +1,271.3% / MDD **-81.7%** (2026-08-16 재측정) | 순수 적립 — 매도 규칙 없음 |

```bash
# 백테스트
python3 LOC_DCA_strategy.py --backtest                # TQQQ (LOC 20분할)
python3 LOC_DCA_strategy.py --backtest --fee 0.001    # 수수료 0.1% 반영

# 실시간 신호 (장 마감 후) — --discord로 Discord 발송 (GitHub Actions 자동화)
python3 LOC_DCA_strategy.py --signal
python3 LOC_DCA_strategy.py --signal --discord       # TQQQ 신호를 Discord로
python3 LOC_DCA_strategy.py --signal --discord --all  # 전 종목 단일 메시지 (수동 확인용 — 워크플로우는 브리핑 1건만 발송)
```

### 실전 반영 — 순수 LOC 20분할 (2026-08-16)

MA/RSI/ATH_DCA 등 로직을 섞던 방식을 버리고 **하나의 논리**로 재구성했습니다
(알림 신호 방식 — 실제 주문 자동 실행은 없음):

- **LOC 매수가** = 전일 종가 × (1 − σ × 승수) — 당일 저가 ≤ LOC → **1차 체결**
- $2,500 × **최대 20차** — 20차 소진 시 매수 중단 (적립 전용)
- **체결 추적 없음 (2026-08-16)** — 정규장 지정가 주문 후 체결 여부는 증권앱 확인 + 엑셀 기록 (봇 미추적)
- 일일 브리핑은 `• 🎯 [Action] LOC Buy:` 라인 하나로 **LOC 매수가만** 안내 (분할 예산/회차 표시 없음)
- **매도 규칙 없음** — 순수 적립 (매도 신호 자체가 발생하지 않음)

---

## 📈 스윙 투자 알리미 (swing_alerter.py)

유튜브 **"TQQQ 스윙 투자 전략 / 스윙 투자 계산기&매수 매도 시점 알리미"** (구글
스프레드시트)의 로직을 자체 엔진으로 재구현한 도구입니다. 스마트폰에서 확인할 수 있는
모바일 대시보드와 Discord 알림을 함께 제공합니다.

### 전략 규칙 (스프레드시트 기준)

- **매수**: 역대 최고가(ATH) 대비 MDD 5% 단위 구간(-5% ~ -95%)에 현재가가 도달하면
  해당 구간이 '매수' 상태가 됩니다.
- **매도**: 실제 매수가(`BUY_PRICE`) 대비 스윙 목표 수익률(`SWING_TARGET_PCT`, 현재 +40%) 도달 시
  매도 알람 (예: 매수가 $100 → 목표 $140). 앱 대시보드의 기본 선택 수익률도 이 설정값을 읽습니다.
- **계산기**: `BUY_PRICE` × `SHARES` → 목표 매도 시 예상 수익금/수익률 자동 계산.

### 설정 (swing_config.json — 공용 / swing_personal.json — 개인 / swing_state.json — 봇 상태)

```json
{
    "ENABLED": true,
    "REFERENCE_HIGH": "ATH",
    "MDD_START_PCT": 5, "MDD_END_PCT": 95, "MDD_STEP_PCT": 5,
    "SWING_TARGET_PCT": 40,   # 앱 대시보드 기본 선택 수익률도 이 값을 읽음 (JS 하드코딩 없음)
    "IMMINENT_GAP_PCT": 5,
    "POSITIONS": {
        "TQQQ": {
            "ENABLED": true, "LABEL": "TQQQ (예시)"
        }
    }
}
```

📱 **나무증권 시세포착주문 감시 등록 가이드**: [NAMYU_SWING_SETUP.md](NAMYU_SWING_SETUP.md) —
매수/매도 감시 조건·가격·수량, 30일 만료 재등록 루틴 (3% 래더 기준).

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
- **OneSignal 푸시 — 단독 사용 전환 (2026-08-12)**: 지인 미구독 확인(카카오톡)으로 이 앱은
  사용자 본인 전용입니다. 매도/매수 구간 푸시는 **전체 구독자(Subscribed Users = 내 기기)**
  대상으로 발송됩니다 — 태그 필터 없이 동작하며, 태그 미등록 기기도 수신합니다.
- **매수 구간 푸시 (2026-08-11 → 2026-08-12 단독 전환)**: 매수 구간 도달(🔻)/임박(📡)은 Discord뿐
  아니라 **전체 구독자(= 내 기기)에게 푸시**로 발송됩니다. `swing_zone_{TICKER}` 태그 필터는
  단독 사용 전환으로 제거됐습니다. ATH(공개 정보) 기준이라 개인 정보 노출이 없습니다.
- `POSITIONS` 에 티커를 추가/수정하면 자유롭게 여러 종목을 모니터링합니다.
- 알림 플래그(`ZONE_ALERTS`, `SELL_ALARM_SENT`)는 엔진이 자동 관리하며 **`swing_state.json`** 에
  보관됩니다. 설정(사용자)과 상태(봇)가 별도 파일로 분리되어 있어 봇이 상태 파일만 커밋하므로
  git 충돌로 알림 상태가 유실되지 않습니다. **전 계좌 매도 완료 시엔 자동 리셋**되며(2026-08-11),
  부분 매도 후 즉시 초기화하려면 `python3 swing_alerter.py --reset TICKER` 를 수동 실행하세요.
- **신규 전고가 자동 리셋 (2026-08-10)**: 전고가가 직전 사이클 기준(`ATH_CYCLE_BASE`)보다
  **+1% 이상** 갱신되면 기록된 매수 구간 상태(`hit`/`imminent`)가 자동 초기화됩니다 —
  이전 사이클의 기록이 남아 새 하락 사이클의 구간 도달/임박 알림이 삼켜지는 문제를 방지하며,
  갱신 시 🆕 신규 전고가 알림이 1회 발송됩니다.
- **전 계좌 매도 완료 자동 리셋 (2026-08-11)**: `LOTS` 의 모든 계좌가 매도 목표(+40%)에 도달하면
  수동 `--reset` 없이 알림 상태(매수 구간/매도 플래그/`ATH_CYCLE_BASE`)가 자동 초기화됩니다 —
  새 포지션을 `swing_personal.json` 과 앱에 기록하면 다음 하락 사이클의 구간 도달/임박 알림이
  다시 울립니다. `CYCLE_RESET_DONE` 플래그로 중복 리셋을 방지하고, 매도 미도달 상태가 되면
  자동 재무장됩니다. ⚠️ 자동 리셋은 '목표 도달' 신호 기준이며 **봇은 `swing_personal.json`을
  건드리지 않으므로** 매도 후 기록 정리·재기록은 계속 사용자 수동 작업입니다.

#### 🧹 매도 후 정리 체크리스트 (사용자 수동 작업)

봇은 `swing_personal.json`(LOTS)과 앱 입력값을 절대 건드리지 않으므로, **매도한 계좌는 사용자가 직접 비웁니다.**

| 상황 | 스윙 퍼스널 (`swing_personal.json` LOTS) | 대시보드 (계좌별 매수/매도 예정가 행) |
|---|---|---|
| **일부 계좌만 매도** (예: 2번) | 매도한 계좌(2번)만 `BUY_PRICE`/`SHARES` → `null` | 다음 대시보드 갱신 때 자동 반영 (2번 행 비워짐) |
| **전 계좌 매도 완료** (사이클 종료) | 1~7번 전부 `BUY_PRICE`/`SHARES` → `null` | 다음 대시보드 갱신 때 자동 반영 (1~7행 전부 비워짐) |

- ⚠️ **보유 중인 계좌는 지우지 않는다** — 계속 매도 추적·푸시를 받아야 하므로.
  (지우면 "아직 안 팔았는데 알림이 안 온다"는 문제가 생김)
- **미입력(null) 처리**: `BUY_PRICE`/`SHARES`가 `null`인 계좌는 매수/매도 예정가가 비워져
  계산·푸시에서 제외됩니다. (앱 화면 입력은 표시용 — 실제 값은 이 파일이 단일 소스)
- 전 계좌 매도 완료 후엔 자동 리셋으로 새 사이클이 준비되므로 **별도 `--reset` 은 불필요**합니다.
- 재매수 시에는 그 계좌에 새 매수가/수량을 기록하면 됩니다 (기록 ↔ null 복원 반복).
- 첫 실행, `--reset`, 또는 **전 계좌 매도 자동 리셋 직후** 첫 모니터링에서는 **현재 도달된
  모든 매수 구간이 한 번에** 알림으로 옵니다 (현재 상태 스냅샷 — 수동 리셋과 동일 동작).
  이후에는 새로 도달하는 구간/임박/매도만 알립니다.
- 기준가는 기본 `ATH`(역대 최고가)이며 **배당 조정 종가(Adj Close) 기준**으로 계산해
  TradingView 등 조정가 차트와 일치합니다. (분할·배당 자동 반영 — 2026-08-10 변경)

### 매도 알람 — 서버 LOTS 기준 (대시보드 + 서버)

대시보드의 **매도 상태 칩**(🚨 매도 / 🚀 임박 / ⏳ 대기)과 상단 **"🚨 매도 알람 N"** 카운트는
**서버 LOTS(swing_personal.json 실제 매수가) 기준**으로 자동 판정됩니다. 예상 수익률은
`SWING_TARGET_PCT` 설정값(현재 40%)이 단일 소스입니다.

- **매수/매도 예정가는 서버 렌더링 단일 소스 (2026-08-12)**: 대시보드 생성 시 서버가
  `swing_personal.json`의 LOTS(계좌별 실제 매수가)를 읽어 계좌 1~7행에 매수 예정가로 그려 넣고,
  **매도 예정가 = 매수 예정가 × (1 + SWING_TARGET_PCT/100)** 를 함께 렌더링합니다 — 폰/웹이
  OneSignal 상태와 무관하게 **항상 같은 값**을 표시합니다 (기기 간 태그 동기화 제거).
- **값 변경 방법**: 편집기에서 `swing_personal.json`을 수정하면 다음 대시보드 갱신 때
  폰/웹 모두에 반영됩니다. 미입력 계좌는 매수/매도 예정가 모두 비워 두고 (현재가 자동
  표시 없음), 입력한 계좌만 계산/저장/푸시에 반영됩니다 (오발송 방지).
- **판정**: 현재가 ≥ 매도 예정가 → **🚨 매도** (빨강) / 목표까지 `IMMINENT_GAP_PCT`(기본 5%p) 이내 →
  **🚀 임박** (호박) / 그 외 → **⏳ 대기** — 입력된 계좌 중 하나라도 도달하면 칩에 표시.
- **서버 설정 불필요**: 사용자가 별도로 값을 입력하지 않아도 `swing_personal.json`의 LOTS가
  있으면 매도 알람이 항상 동작합니다.
- **매도 푸시 — 단독 사용 전환 (2026-08-12)**: 서버 모니터가 현재가를 확인해 **매도 목표
  (서버 LOTS 매수가 × 목표 수익률)에 도달한 계좌를 전체 구독자(= 내 기기)에게** 푸시합니다
  (계좌별 사이클당 1회 — 2026-08-13: 매도 신호가 전달된 계좌는 리셋 전까지 재발송하지 않음).
  태그 필터/Liquid 개인화는 제거되어 앱에서 매수 예정가를 입력하지 않아도 푸시가 동작합니다.
- **매도 목표 임박 푸시 (2026-08-15)**: 개인 포지션의 매도 목표 임박(🚀, 목표까지 `IMMINENT_GAP_PCT`
  이내)도 전체 구독자(= 내 기기)에게 푸시됩니다 — 나무증권 매도감시를 **임박 시점에 등록하는 루틴
  (NAMYU_SWING_SETUP.md ②)의 신호**로 활용 (계좌별 사이클당 1회 — 매도 신호 푸시와 동일 패턴).
- **푸시 수신 조건**: 앱에서 🔔 알림을 구독하기만 하면 수신됩니다 (태그 등록 불필요 — 2026-08-12
  단독 사용 전환).
- **전역 푸시: 단독 사용 전환 (2026-08-12)** — 지인 노출 차단을 위해 제거했던(2026-08-10) 전체
  구독자 푸시를, 지인 미구독 확인으로 **본인 전용 재개**했습니다. 푸시는 태그 필터 없이
  전체 구독자(= 내 기기)로 발송됩니다.
- **매수 구간 푸시 (2026-08-11 → 2026-08-12 단독 전환)**: 매수 구간 도달(🔻)/임박(📡) 발생 시
  전체 구독자(= 내 기기)에게 푸시가 발송됩니다 (별도 설정 불필요 — `swing_zone_{TICKER}` 태그
  필터는 단독 사용 전환으로 제거). 개인 정보가 없는 공개 신호이며, 중복 방지는 서버 `ZONE_ALERTS`
  상태가 담당하고 발송 실패 시 다음 폴링에서 재시도합니다 (하루 지난 대기분은 폐기 — 스테일 발송 방지).
- **저장**: 계좌별 매수 예정가/매도 예정가는 서버가 렌더링하므로 기기별 저장이 필요 없습니다
  (OneSignal 태그 동기화 제거 — 2026-08-12, 409/중복 사용자 문제로 폐기).
- **동기화 코드 (2026-08-12 이후)**: 헤더의 **🔄 동기화 코드**는 값 동기화가 아니라
  OneSignal **외부 ID 사용자 병합** 용도입니다. 같은 코드를 두 기기에 입력하면 OneSignal
  사용자가 병합되지만, **매수/매도 예정가의 기기 간 일치는 `swing_personal.json` 서버
  렌더링이 담당**합니다. 코드 입력칸은 🔒 가려진 상태로 표시되며 (눈 아이콘으로 잠시
  확인), 코드는 화면·기기 localStorage·OneSignal 사용자 레코드에만 존재합니다
  (커밋/공용 알림 노출 없음).

#### 예시 (TQQQ 현재가 $74.47, SWING_TARGET_PCT 40%)

| 매수 예정가 (서버 LOTS) | 매도 예정가 (= 매수 × 1.4) | 현재가 기준 상태 |
|---|---|---|
| (미입력 — null) | — | — |
| $60.00 | $84.00 | ⏳ 대기 |
| $50.00 | $70.00 | 🚨 매도 (이미 도달) |

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
   (매일 00:00 UTC = 09:00 KST 자동 실행, 또는 Actions 탭에서 수동 실행 `workflow_dispatch`).
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

### MarketStageSystem.py (독립 실행)
- `portfolio_config.json`의 `POSITIONS` 키에서 티커 목록을 읽어 시장 바닥 단계(0~5) 감지
- `LOC_DCA_strategy`와 **설정 파일 공유** (`resolve_discord_config()` 공유)
- 감지된 바닥 단계는 `market_state.json`에 기록 — **DCA 엔진의 매수 트리거로는 사용하지 않음** (2026-08-16 이후)

### bear_market_signals.py (독립 실행)
- 약세장 신호를 분석하여 `signal_report.json`에 리스크 점수 기록
- 시장 리스크 점수(0~14)를 브리핑에 포함

### cron-job.org (외부 스케줄러 — 스윙 알리미 실시간 알림)
- GitHub Actions `schedule` 크론의 best-effort 지연을 우회하는 정확한 N분 알람
- `repository_dispatch`(event_type: `swing-monitor`)로 스윙 알리미 워크플로우 즉시 실행
- 설정 자동화: `setup_cronjob_org.py` — 상세: [스윙 실시간 알림](#실시간-알림-cron-joborg)
- ⚠️ 기존 ATH DCA 실시간 잡("ATH DCA realtime monitor")은 **cron-job.org 콘솔에서 수동 삭제 필요** (2026-08-16 — `--ath-monitor` 삭제)

---

## 📝 참고 사항

- **NYSE 휴장일**: `pandas_market_calendars` 라이브러리로 자동 계산
- **시간 기준**: 모든 시간은 `America/New_York` 기준
- **yfinance 캐시 전략**: 서로 다른 period 파라미터로 호출하여 캐시 충돌 방지
- **정산 버퍼**: 장 마감 후 15분 버퍼 — 미정산 데이터 사용 방지
- **Sigma 갱신 주기**: 90일(약 63거래일) 또는 설정(VOL_METHOD/EWMA_LAMBDA) 변경 시
- **실시간 알림 (스윙 전용)**: GitHub Actions 스케줄은 60일간 활동 없으면 자동 비활성화되지만,
  cron-job.org 폴링이 매일 커밋을 만들어내므로 자연히 유지됩니다.

---

## 📄 관련 문서

| 문서 | 설명 |
|------|------|
| [STRATEGY_RULES.md](STRATEGY_RULES.md) | 전략 규칙 (순수 LOC 20분할 DCA) |
| [LOC_DCA_strategy_flowchart.py](LOC_DCA_strategy_flowchart.py) | 시스템 전체 플로우차트 |
| [NAMYU_SWING_SETUP.md](NAMYU_SWING_SETUP.md) | 나무증권 시세포착주문 감시 등록 가이드 (스윙 알리미) |

---

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
