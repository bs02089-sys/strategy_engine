# Sigma DCA Manager

미국 주식 시장 **Sigma 기반 LOC 매수 목표가 자동 계산** 및 **디스코드 브리핑 자동 발송** 시스템입니다.

---

## 📋 목차

- [개요](#-개요)
- [주요 기능](#-주요-기능)
- [시스템 아키텍처](#-시스템-아키텍처)
- [파일 구성](#-파일-구성)
- [설치 및 설정](#-설치-및-설정)
- [설정 파일](#-설정-파일)
- [실행 방법](#-실행-방법)
- [GitHub Actions 자동화](#-github-actions-자동화)
- [백테스트](#-백테스트)
- [연동 시스템](#-연동-시스템)
- [라이선스](#-라이선스)

---

## 📌 개요

**Sigma DCA Manager**는 매일 미국 장 마감 후 정해진 시간에 자동 실행되어:
1. 포트폴리오에 등록된 티커(TQQQ, SOXL 등)의 변동성을 계산/갱신
2. Sigma 기반 LOC 매수 목표가 산출
3. RSI+거래량 복합 매수 신호 평가 (12년 백테스트 검증)
4. 전고점 근접 50% 청산 신호 감지
5. 듀얼 모드 (LOC 일반 / ATH DCA 비상) 자동 전환
6. ATH 하락분할 DCA 트리거 모니터링 (3차 분할, STAGE5 바닥 통합)
7. 로테이션 포지션 만기 관리
8. 종합 브리핑을 **Discord**로 전송

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

### 3️⃣ 전고점 근접 50% 청산 신호
- 3가지 조건이 모두 충족되면 매도 신호 (50% 포지션 청산 권장):
  1. 현재가가 전고점의 90% 이상 도달
  2. 20일 상승률 40% 이상 (급등 확인)
  3. 단기/장기 Sigma 비율 조건 (SOXL 특성상 비활성화)
- 매도 후 **60거래일** 쿨다운 적용 (재매도 방지)

### 4️⃣ ATH 하락분할 DCA
- ATH 대비 하락률에 따라 N분할 매수 트리거
- 설정 예시: -15% / -20% / -60% 에 각각 1/3씩 매수
- 전 사이클 완료 후 신규 ATH 갱신 시 **자동 초기화 및 사이클 재시작**
- 임박 알림 (목표 임계값 5%p 이내 접근 시)

### 5️⃣ 포지션 유형별 전략

| 유형 | 전략 |
|------|------|
| **LONG_YEAR** | 기계적 LOC 전략 — 무조건 매수 신호 활성 |
| **ROTATION_3M** | MA20/MA60 추세 기반 매수/매도 신호 + 만기 초기화 |
| **END_DEC** | MA20/MA60 추세 기반 매수/매도 신호 |

### 6️⃣ 듀얼 모드 전환 + ATH 하락분할 DCA (비상 모드)
- **LOC 모드** (📗): 평상시 Sigma 기반 LOC 20분할 매수
- **ATH DCA 모드** (🚨): ATH 하락률이 TRIGGER_1 도달 시 자동 전환 → 3차 분할 매수
  - 1차/2차: ATH 대비 설정된 % 하락 시 (TQQQ: -35%/-50%, SOXL: -60%/-70%)
  - **3차: MarketStageSystem의 Stage 5 바닥 감지 시 발동**
- MarketStageSystem.py가 `market_state.json`에 기록한 바닥 단계를 ATH DCA 3차 트리거로 활용
- 전 사이클(3차) 완료 후 신규 ATH 갱신 시 자동 초기화 및 사이클 재시작

### 7️⃣ Discord 브리핑
- 매일 정해진 시간에 Discord Webhook으로 종합 브리핑 전송
- 각 티커별: 현재가, Sigma, LOC 목표가, 전고점 대비 하락률/회복률, 매수/매도 신호
- 매월 1일 월간 작동 확인 Ping 전송

### 8️⃣ 로테이션 포지션 자동 초기화
- ROTATION_3M 포지션: 설정된 영업일(기본 63일) 경과 후 자동 초기화 + Sigma 재계산

---

## 🏗 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                 GitHub Actions (스케줄러)                        │
│  매일 장 마감 후(18:30 UTC) / 매 30분 / 매월 1일               │
└─────────────────┬───────────────────────────────────────────────┘
                  │ 실행
┌─────────────────▼───────────────────────────────────────────────┐
│                  sigma_DCA_manager.py                            │
│                                                                  │
│  1. portfolio_config.json 불러오기                                │
│  2. Sigma 갱신 (오래되었거나 설정 변경 시)                       │
│  3. 전일 종가 및 LOC 목표가 계산 (티커별)                        │
│  4. RSI+거래량 복합 신호 확인                                    │
│  5. 전고점 청산 신호 확인                                        │
│  6. ATH 하락분할 DCA 트리거 확인                                 │
│  7. 로테이션 만기 확인                                           │
│  8. 시장 바닥 단계 확인                                          │
│  9. 브리핑 작성 → Discord 전송                                   │
│  10. 월간 Ping (매월 1일)                                        │
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
| **sigma_DCA_manager.py** | 📌 **메인 실행 파일** — LOC 목표가 계산, 신호 평가, Discord 브리핑 |
| **sigma_DCA_manager_flowchart.py** | 시스템 전체 플로우차트 문서 |
| **sigma_backtest.py** | 백테스트 엔진 — 단일 실행, 승수 스윕, 다중 기간 검증, 포트폴리오 최적화 |
| **optimize_ath_dca.py** | ATH 하락분할 DCA 트리거 최적화 도구 |
| **verify_split_strategy.py** | 분할 매수 전략 검증 도구 |
| **MarketStageSystem.py** | 독립적인 시장 단계 시스템 — 바닥 단계 감지 |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **portfolio_config.json** | 📌 **포트폴리오 설정** — 포지션, Sigma, DCA 파라미터 |
| ~~MarketStage_config.json~~ | (제거됨 — portfolio_config.json으로 통합) |
| **sigma_history.csv** | Sigma 갱신 이력 (자동 생성) |
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
    "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/...",
    "DISCORD_USER_ID": "",
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
            "ATH_DCA": {
                "ENABLED": true,
                "SPLITS": 3,
                "TRIGGER_1": "-10%",
                "TRIGGER_2": "-25%",
                "TRIGGER_3": "-30%",
                "STRATEGY": "v2 multi-cycle"
            },
            "ATH_DCA_USED_SPLITS": [1, 2, 3],
            "ATH_DCA_CYCLE_ATH": 87.02
        }
    },
    "STRATEGY": {
        "CYCLE_YEARS": 2,
        "BUY_DURATION_DAYS": 252,
        "HOLD_DURATION_DAYS": 252,
        "DEFAULT_LOOKBACK_DAYS": 365
    }
}
```

#### 포지션 설정 항목

| 항목 | 설명 |
|------|------|
| `LOOKBACK_DAYS` | 변동성 계산 기간 (기본 252 = 1년) |
| `ENTRY_MULTIPLIER` | LOC 목표가 승수 — σ × 승수 만큼 하락한 가격이 매수 목표 |
| `VOL_METHOD` | 변동성 계산 방식: `EWMA` (기본) 또는 `STD` |
| `EWMA_LAMBDA` | EWMA 감쇠 계수 (기본 0.94) |
| `INVEST_TYPE` | 투자 유형: `LONG_YEAR` / `ROTATION_3M` / `END_DEC` |
| `ALLOCATION_PCT` | 포트폴리오 내 비중 |
| `ATH_DCA` | ATH 대비 하락분할 매수 설정 |
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
python3 sigma_DCA_manager.py

# 특정 함수만 테스트
python3 -c "
from sigma_DCA_manager import get_prev_close, calculate_loc_price
import json
with open('portfolio_config.json') as f:
    cfg = json.load(f)

close, date = get_prev_close('TQQQ')
print(f'TQQQ 종가: \${close} ({date})')

loc = calculate_loc_price('TQQQ', close, cfg)
print(f'TQQQ LOC 목표가: \${loc}')
"
```

### 백테스트 실행

```bash
# 기본 단일 백테스트
python3 sigma_backtest.py

# 승수 스윕 최적화
python3 sigma_backtest.py --sweep

# 다중 기간 검증
python3 sigma_backtest.py --multi-sweep

# 포트폴리오 비중 최적화 (TQQQ/SOXL)
python3 sigma_backtest.py --portfolio-sweep
python3 sigma_backtest.py --multi-portfolio-sweep
```

### 플로우차트 문서 보기

```bash
python3 sigma_DCA_manager_flowchart.py
```

---

## 🤖 GitHub Actions 자동화

### `sigma_dca_manager.yml` — 정기 브리핑

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 14:30, 18:30, 21:30 | 장 마감 후 브리핑 |
| 수동 실행 | 사용자 요청 시 | 수동 실행 |

### `bear_market_signals.yml` — 약세장 신호

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 14:00 | 시장 리스크 평가 |

### `tracker.yml` — 시장 단계 추적

| 트리거 | 시간 (UTC) | 설명 |
|--------|------------|------|
| 예약 실행 | 매일 14:30 | 바닥 단계 추적 |

### 환경 변수 (GitHub Secrets)

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK` | Discord Webhook 주소 |
| `DISCORD_USER_ID` | Discord 사용자 ID (멘션용) |

---

## 📊 백테스트

`sigma_backtest.py`는 sigma_DCA_manager의 핵심 전략을 검증하는 백테스트 엔진입니다.

### 기능
- **단일 실행**: 설정된 ENTRY_MULTIPLIER로 1년 백테스트
- **승수 스윕**: 0.6~3.0 범위의 승수를 테스트하여 최적 승수 탐색
- **다중 기간 스윕**: 여러 시장 국면(강세/약세/회복)에 걸쳐 일관된 승수 검증
- **포트폴리오 스윕**: TQQQ/SOXL 비중 최적화 (10%~90%)
- **전고점 청산 비교**: DCA 단독 vs DCA+전고점50%청산 성능 비교
- **상세 리포트**: 샤프 비율, 최대 낙폭, 승률, 월별 수익률 등

### 사용 기술
- 일간 로그수익률 기반 변동성(σ) 계산
- EWMA(λ=0.94) 가중치 적용
- LOC 목표가: `전일종가 × (1 - σ × 승수)`
- 매수 조건: 당일 저가 ≤ LOC 목표가

---

## 🔗 연동 시스템

### MarketStageSystem.py (portfolio_config.json 공유)
- `portfolio_config.json`의 `POSITIONS` 키에서 티커 목록을 읽어 시장 바닥 단계(0~5) 감지
- `sigma_DCA_manager`와 **설정 파일 공유** (`resolve_discord_config()` 공유)
- 감지된 바닥 단계(Stage 5)는 `market_state.json`에 기록 → **ATH DCA 3차 트리거로 사용**

### bear_market_signals.py (독립 실행)
- 약세장 신호를 분석하여 `signal_report.json`에 리스크 점수 기록
- 시장 리스크 점수(0~14)를 브리핑에 포함

---

## 📝 참고 사항

- **NYSE 휴장일**: `pandas_market_calendars` 라이브러리로 자동 계산
- **시간 기준**: 모든 시간은 `America/New_York` 기준
- **yfinance 캐시 전략**: 서로 다른 period 파라미터로 호출하여 캐시 충돌 방지
- **정산 버퍼**: 장 마감 후 15분 버퍼 — 미정산 데이터 사용 방지
- **Sigma 갱신 주기**: 90일(약 63거래일) 또는 설정(VOL_METHOD/EWMA_LAMBDA) 변경 시

---

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
