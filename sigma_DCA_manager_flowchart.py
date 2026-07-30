#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════
  Sigma DCA Manager — 전체 시스템 플로우차트
══════════════════════════════════════════════════════════════════════
  파일: sigma_DCA_manager.py
  최종 업데이트: 2026-07-30
  듀얼 모드: LOC (일반) / ATH DCA (비상)
══════════════════════════════════════════════════════════════════════

[목차]
  1. 시스템 개요
  2. 전체 실행 흐름도
  3. 함수 호출 관계도
  4. 데이터 파일 의존성
  5. RSI + Volume Zone 설정 (12년 백테스트 검증)
  6. 전고점 청산 신호 설정
  7. GitHub Actions 워크플로우
  8. 파일 구성도
══════════════════════════════════════════════════════════════════════
"""

# =============================================================================
# 1. 시스템 개요
# =============================================================================
"""
📌 Sigma DCA Manager는 매일 정해진 시간에 GitHub Actions에서 실행되어,
   portfolio_config.json에 설정된 포지션(TQQQ, SOXL)의 LOC 매수 목표가를
   계산하고, RSI+거래량 복합 신호, 전고점 청산 신호, ATH 하락분할 DCA를
   평가하여 디스코드로 종합 브리핑을 전송하는 자동화 시스템입니다.

🔗 연동 시스템:
  - MarketStageSystem.py → market_state.json (바닥 단계 정보)
  - bear_market_signals.py → signal_report.json (시장 리스크 점수)
"""

# =============================================================================
# 2. 전체 실행 흐름도
# =============================================================================
"""
┌──────────────────────────────────────────────────────────────────────────────┐
│                   🚀 sigma_DCA_manager.py (메인 진입점)                       │
│            (GitHub Actions가 매일 23:24 UTC에 자동 실행)                       │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [1] 초기화                                                                  │
│  ├─ now_ny = 현재 시각 (America/New_York)                                    │
│  ├─ cfg ← load_portfolio() → portfolio_config.json                          │
│  ├─ webhook ← os.environ["DISCORD_WEBHOOK"] or cfg["DISCORD_WEBHOOK"]       │
│  └─ user_id ← os.environ["DISCORD_USER_ID"] or cfg["DISCORD_USER_ID"]       │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [2] 포지션 만료 체크 (Rotation 3M)                                          │
│                                                                              │
│  reset_matured_rotation_positions(cfg, today)                                │
│  └─ 각 포지션의 INVEST_TYPE 확인                                             │
│     ├─ "ROTATION_3M" → check_rotation_exit_signal() → 만료일 도달?          │
│     │  ├─ YES → Sigma 재계산 + START_DATE 초기화                             │
│     │  └─ NO  → 다음 포지션                                                 │
│     └─ 기타 (LONG_YEAR / END_DEC) → 건너뜀                                  │
│  └─ 결과 메시지를 console에 출력                                              │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [3] Sigma 신선도 체크 (90일 기준)                                           │
│                                                                              │
│  refresh_sigma_if_stale(cfg)                                                 │
│  └─ 각 포지션 확인:                                                          │
│     ├─ LAST_SIGMA_UPDATE가 90일 이상 지났는가?                               │
│     ├─ VOL_METHOD 또는 EWMA_LAMBDA가 변경되었는가?                           │
│     ├─ YES → recompute_sigma_for_ticker()                                    │
│     │  ├─ _fetch_closes_for_lookback() ← yfinance                           │
│     │  ├─ _calculate_volatility_from_closes()                                │
│     │  │  ├─ "EWMA" → _calculate_ewma_sigma_from_closes()                   │
│     │  │  └─ "STD"/"HISTORICAL" → _calculate_sigma_from_closes()            │
│     │  ├─ log_sigma_update() → sigma_history.csv                            │
│     │  └─ DAILY_SIGMA + LAST_SIGMA_UPDATE 업데이트                          │
│     └─ NO → 다음 포지션                                                     │
│  └─ 결과 메시지를 console에 출력                                              │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [4] 듀얼 모드 전환 평가 ⭐신규                                               │
│                                                                              │
│  _evaluate_all_strategy_modes(cfg)                                           │
│  ├─ 각 포지션의 STRATEGY_MODE 확인                                           │
│  │                                                                          │
│  │  LOC → ATH_DCA 전환 조건:                                                 │
│  │  ├─ ATH_DCA.ENABLED == true                                              │
│  │  ├─ 현재 ATH DD >= TRIGGER_1 (TQQQ: -35%, SOXL: -60%)                   │
│  │  └─ 모드 전환: STRATEGY_MODE = "ATH_DCA"                                 │
│  │                                                                          │
│  │  ATH_DCA → LOC 전환 조건: (사용자 수동)                                     │
│  │  ├─ 3분할 모두 사용 완료 (len(used) >= total_splits)                     │
│  │  ├─ 신규 ATH > CYCLE_ATH × 1.01 (회복 완료)                               │
│  │  └─ 모드 전환: STRATEGY_MODE = "LOC"                                     │
│  │                                                                          │
│  ├─ 모드 전환 발생 시 → console에 출력                                        │
│  └─ cfg에 변경 사항 기록 (save_portfolio()에서 저장)                           │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [5] 디스코드 브리핑 생성 (모드별) ← _build_briefing_lines(now_ny, cfg)              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  📌 듀얼 모드 표시: 각 포지션의 STRATEGY_MODE 표시                     │ │
│  │  ├─ 📗 LOC 모드: LOC 목표가 + RSI/볼륨 신호 표시                       │ │
│  │  └─ 🚨 ATH_DCA 모드: LOC 중단 표시 + ATH DCA 브리핑                   │ │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  제목: "🌙 U.S. Market LOC Portfolio Briefing (YYYY-MM-DD HH:MM EST)" │ │
│  │  ├─ 📊 Market Risk Score: X / 14 (from signal_report.json)             │ │
│  │  └─ ─── 40 ───                                                        │ │
│  │                                                                        │ │
│  │  ▼ 각 포지션 반복 (TQQQ → SOXL)                                        │ │
│  │                                                                        │ │
│  │  ┌──────────────────────────────────────────────────────────────────┐ │ │
│  │  │  [4-a] 기술적 신호 확인                                          │ │ │
│  │  │  check_macro_and_technical_signals(ticker, pos_cfg)              │ │ │
│  │  │  ├─ LONG_YEAR → Buy=True, Sell=False ("LOC mechanical")          │ │ │
│  │  │  ├─ ROTATION_3M → MA20/MA60 교차 분석 (120d 데이터)              │ │ │
│  │  │  └─ END_DEC → MA20/MA60 교차 분석 (120d 데이터)                  │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-b] 전일 종가 조회                                            │ │ │
│  │  │  get_prev_close(ticker) ← yfinance 1mo 데이터 (3회 재시도)        │ │ │
│  │  │  ├─ 장 마감+15분 후 (16:15 NY) → 오늘 최종 종가                  │ │ │
│  │  │  ├─ 장 중 → 전일 종가                                            │ │ │
│  │  │  └─ 실패 시 → yfinance info API fallback                         │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-c] 전고점 대비 하락률                                         │ │ │
│  │  │  format_drawdown_line(ticker, prev_close, lookback_days)         │ │ │
│  │  │  ├─ get_period_ath() → N일 최고가 (Close 기준, 표준 방법론)                    │ │ │
│  │  │  └─ calculate_drawdown_and_recovery()                             │ │ │
│  │  │     → "전고점 $XX 기준 하락률 -XX% / 회복필요 XX%"               │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-d] LOC 목표가 계산                                           │ │ │
│  │  │  calculate_loc_price(ticker, prev_close, cfg)                    │ │ │
│  │  │  ├─ DAILY_SIGMA × ENTRY_MULTIPLIER × 전일종가                    │ │ │
│  │  │  ├─ DAILY_SIGMA 없으면 → get_realtime_sigma() 실시간 계산        │ │ │
│  │  │  └─ "🎯 LOC Buy: $XX.XX"                                        │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-e] Rotation 만료 체크                                        │ │ │
│  │  │  check_rotation_exit_signal(pos_cfg, today)                      │ │ │
│  │  │  └─ 만료 시 "🔴 D+XX Rotation Maturity" 경고                     │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-f] (제거됨 — Stage 5는 ATH DCA 3차 트리거로 통합)           │ │ │
│  │  │  → check_ath_dca_signals() 내부의 STAGE5 타입 트리거로 처리      │ │ │
│  │  │  ├─ get_bottom_stage() → market_state.json (Stage 0~5)           │ │ │
│  │  │  └─ Stage 5 시 "🚨 3차 DCA 매수 신호! [Stage 5 Bottom Confirmed]"│ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-g] RSI + 거래량 복합 신호                                    │ │ │
│  │  │  _check_rsi_volume_signal(ticker)                                │ │ │
│  │  │  ├─ yfinance 6mo 데이터 다운로드                                  │ │ │
│  │  │  ├─ RSI 계산 (SOXL: 14일 / TQQQ: 21일)                          │ │ │
│  │  │  ├─ 20일 거래량 이동평균 계산                                     │ │ │
│  │  │  ├─ Zone 1 검사 (RSI + Volume 조건)                              │ │ │
│  │  │  ├─ Zone 2 검사 (RSI + Volume 조건)                              │ │ │
│  │  │  └─ 결과: 🔥🔥🔥 두 구역 / 🔥 한 구역 / ⏸️ 대기                  │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-h] 전고점 근접 50% 청산 신호 ⭐ (브리핑 내 별도 평가)         │ │ │
│  │  │  (브리핑 빌더에 직접 포함 — format_drawdown_line에서 처리)        │ │ │
│  │  │  ├─ get_period_ath() → 전고점 (Close 기준, LOOKBACK_DAYS)        │ │ │
│  │  │  └─ calculate_drawdown_and_recovery() → 하락률/회복률 표시        │ │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [6] ATH 하락분할 DCA 신호 확인 ⭐신규⭐                                    │
│                                                                              │
│  check_ath_dca_signals(cfg)                                                  │
│  └─ 각 포지션 ATH_DCA.ENABLED 확인                                           │
│     ├─ 비활성화 → 다음 포지션                                                │
│     ├─ 활성화 → yfinance 1년 데이터 다운로드                                 │
│     │  ├─ ATH 계산 (Close 기준, expanding max)                              │
│     │  ├─ 현재 하락률(DD) 계산                                               │
│     │  └─ 각 TRIGGER_N 평가 (1차/2차/3차...)                                 │
│     │     ├─ DD ≥ 임계값 → 🚨 매수 신호! + ATH_DCA_USED_SPLITS 기록         │
│     │     ├─ DD < 임계값 but 5%p 이내 → 📡 임박 알림                        │
│     │     └─ 기타 → 건너뜀                                                   │
│     ├─ 전체 분할 완료 시 ATH_DCA_CYCLE_ATH 기록                              │
│     └─ 신규 ATH > CYCLE_ATH × 1.01 → 🔄 재진입 준비 (사용 분할 초기화)       │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [7] 디스코드 전송                                                            │
│  _send_discord(webhook, user_id, title, content)                             │
│  ├─ Embed 메시지 생성 (제목 + 설명 + 타임스탬프 + 색상)                       │
│  ├─ @유저 멘션 포함                                                          │
│  ├─ 내용 4096자 제한 / 제목 256자 제한                                       │
│  └─ Discord Webhook API 호출                                                 │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [8] 월간 핑 (매월 1일만)                                                    │
│  send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)                     │
│  ├─ now_ny.day == 1? AND LAST_MONTHLY_PING != this month?                   │
│  ├─ YES → "📅 Monthly Operation Ping" 전송                                  │
│  └─ LAST_MONTHLY_PING 업데이트 → save_portfolio()                           │
└──────────────────────────────────────────────────────────────────────────────┘

        ┌───────────┐
        │   ✅ 완료   │
        └───────────┘


📌 듀얼 모드 시스템:
   각 포지션의 STRATEGY_MODE 필드에 따라:
   - "LOC" (📗 Normal):    20분할 Sigma 기반 LOC 매수 진행
   - "ATH_DCA" (🚨 Crash):  3분할 ATH 하락분할 DCA 매수 (LOC 중단)

📌 ATH DCA 체크([5])는 브리핑 빌더와 별도로 실행되며, 그 결과는
   별도 Discord 메시지로 전송됨 (또는 save_portfolio()로 상태만 저장).
"""

# =============================================================================
# 3. 함수 호출 관계도
# =============================================================================
"""
[메인 실행 흐름]
sigma_DCA_manager.py (직접 실행)
│
├── load_portfolio()                           ← portfolio_config.json
├── reset_matured_rotation_positions()
│   └── check_rotation_exit_signal()
│       └── business_days_elapsed()
│           └── _get_nyse_holidays()           ← pandas_market_calendars
├── refresh_sigma_if_stale()
│   └── recompute_sigma_for_ticker()
│       ├── _fetch_closes_for_lookback()       ← yfinance API
│       ├── _calculate_volatility_from_closes()
│       │   ├── _calculate_ewma_sigma_from_closes()
│       │   └── _calculate_sigma_from_closes()
│       │       └── _get_recent_log_returns()
│       └── log_sigma_update()                 → sigma_history.csv
├── save_portfolio()                           → portfolio_config.json
│
├── _evaluate_all_strategy_modes()             ← 듀얼 모드 평가
│   ├── LOC → ATH_DCA: ATH DD >= TRIGGER_1
│   └── ATH_DCA → LOC: 사용자 수동 전환 (STRATEGY_MODE="LOC")
│
├── _build_briefing_lines()                    ← 브리핑 생성 (모드별)
│   ├── get_market_score()                     ← signal_report.json
│   │
│   │  ── [각 포지션 반복] ──
│   │
│   ├── check_macro_and_technical_signals()    ← yfinance API
│   ├── get_prev_close()                       ← yfinance API
│   │   └── _most_recent_trading_day()
│   ├── format_position_meta()
│   │   └── business_days_elapsed()
│   ├── format_drawdown_line()                 ⭐ 전고점 하락률
│   │   ├── get_period_ath()                   ← yfinance API
│   │   └── calculate_drawdown_and_recovery()
│   ├── check_rotation_exit_signal()
│   ├── _format_loc_action_line()
│   │   └── calculate_loc_price()
│   │       ├── _calculate_loc_from_sigma()
│   │       └── get_realtime_sigma()
│   │           └── _fetch_closes_for_lookback()
│   ├── _format_all_in_line()                  ← market_state.json
│   │   ├── get_bottom_stage()
│   │   └── Stage 5 → ATH DCA 3차 트리거로 통합          ← portfolio_config.json
│   │
│   └── _check_rsi_volume_signal()             ← yfinance API
│       ├── _calculate_rsi()     (SOXL: 14일 / TQQQ: 21일)
│       ├── _TICKER_ZONES lookup (SOXL / TQQQ)
│       └── Zone 1 + Zone 2 평가
│
├── check_ath_dca_signals(cfg)                 ⭐신규⭐ ATH 하락분할 DCA
│   ├── _parse_ath_trigger()                   (-30% → 0.30)
│   ├── yfinance 1년 데이터 다운로드
│   ├── ATH (expanding max) 계산
│   ├── 각 TRIGGER_N 평가
│   └── ATH_DCA_USED_SPLITS / CYCLE_ATH 관리
│
├── _send_discord()                            → Discord Webhook
│
└── send_monthly_ping_if_due()
    └── save_portfolio()


[전고점 청산 신호 엔진 (브리핑 내 간접 참조)]
get_period_ath()                               ← yfinance API
  └── _fetch_closes_for_lookback()             (retry 로직 공유)

format_drawdown_line()
  ├── get_period_ath()
  └── calculate_drawdown_and_recovery()
      → "전고점 $XX 기준 하락률 XX% / 회복필요 XX%"

check_peak_sell_signal()                       ← 백테스트 전용 (backtest.py)
  ├── get_rolling_ath()
  ├── get_20day_return()
  └── get_sigma_spike_ratio()
      ├── _calculate_volatility_from_closes()  (short 20d)
      └── _calculate_volatility_from_closes()  (long 252d)

check_peak_sell_signal_with_cooldown()
  └── check_peak_sell_signal() + _COOLDOWN_DAYS (60거래일)
"""

# =============================================================================
# 4. 데이터 파일 의존성
# =============================================================================
"""
📂 프로젝트 파일 구조 및 데이터 흐름:

 portfolio_config.json  (읽기/쓰기)
 ├── DISCORD_WEBHOOK, DISCORD_USER_ID
 ├── POSITIONS → TQQQ / SOXL
 │   ├── LOOKBACK_DAYS, ENTRY_MULTIPLIER
 │   ├── VOL_METHOD (EWMA / STD), EWMA_LAMBDA
 │   ├── DAILY_SIGMA (← refresh_sigma_if_stale)
 │   ├── LAST_SIGMA_UPDATE, LAST_SIGMA_METHOD, LAST_EWMA_LAMBDA
 │   ├── ALLOCATION_PCT, INVEST_TYPE
 │   ├── START_DATE
 │   ├── ROTATION_EXIT_DAYS (for ROTATION_3M)
 │   ├── ATH_DCA ⭐신규
 │   │   ├── ENABLED (true/false)
 │   │   ├── SPLITS (분할 수)
 │   │   ├── TRIGGER_1 ~ TRIGGER_N (하락률 임계값)
 │   │   └── STRATEGY (설명)   │   ├── ATH_DCA_USED_SPLITS (사용된 분할 목록, 자동 관리)
   │   ├── ATH_DCA_CYCLE_ATH (사이클 완료 시점 ATH 기록)
   │   └── ATH_DCA_CONFIG_FINGERPRINT ⭐신규 (설정 변경 감지용 지문)
   │       → TRIGGER/SPLITS/STRATEGY 변경 시 자동 감지 → 분할 상태 초기화
 └── LAST_MONTHLY_PING

 portfolio_config.json  (읽기 전용, MarketStageSystem.py가 공유)
 └── POSITIONS → SOXL / TQQQ (키 목록을 ticker로 사용)

 market_state.json  (읽기 전용, MarketStageSystem.py가 작성)
 └── SOXL / TQQQ
     ├── bottom (0~5)           → All-In 트리거
     └── top (0~5)

 signal_report.json  (읽기 전용, bear_market_signals.py가 작성)
 └── total_score (0~14)         → Market Risk Score

 sigma_history.csv  (쓰기 전용, Sigma 업데이트 로그)
 └── Date, Ticker, Sigma

 yfinance API (외부 데이터)
 ├── 1mo 데이터 → get_prev_close() (전일 종가)
 ├── 120d 데이터 → check_macro_and_technical_signals() (MA20/MA60)
 ├── 252d+ 데이터 → get_period_ath() / get_realtime_sigma() / recompute_sigma()
 ├── 1y 데이터 → check_ath_dca_signals() (ATH 하락률)
 └── 6mo 데이터 → _check_rsi_volume_signal() (RSI + Volume)

 Discord Webhook (외부 출력)
 └── Embed 메시지 → 디스코드 채널
"""

# =============================================================================
# 5. RSI + Volume Zone 설정 (12년 백테스트 검증)
# =============================================================================
"""
╔══════════════════════════════════════════════════════════════════════╗
║                    RSI + Volume Zone 설정                           ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   SOXL ──────────────────────────────────────────────────────────   ║
║     RSI 기간: 14일                                                   ║
║     Zone 1: RSI  25~34  |  거래량 0.3~0.7× MA20 (저RSI 저볼륨)     ║
║     Zone 2: RSI  34~40  |  거래량 0.4~0.9× MA20 (중간RSI 중볼륨)   ║
║     → Sharpe 2.62 | 승률 71% | 12년 백테스트                        ║
║                                                                      ║
║   TQQQ ──────────────────────────────────────────────────────────   ║
║     RSI 기간: 21일                                                   ║
║     Zone 1: RSI  25~35  |  거래량 0.3~0.7× MA20 (저RSI 저볼륨)     ║
║     Zone 2: RSI  35~50  |  거래량 0.4~1.0× MA20 (중간RSI 중볼륨)   ║
║     → Sharpe 1.30 | 승률 67% | 12년 백테스트                        ║
║                                                                      ║
║   Zone 경계 처리:                                                     ║
║     Zone 1: [min ≤ RSI ≤ max]  (양쪽 포함)                           ║
║     Zone 2: (min < RSI ≤ max]  (하한 미포함)                         ║
║     → RSI = 경계값 → Zone 1에만 포함 (중복/갭 없음)                  ║
║                                                                      ║
║   신호 레벨:                                                         ║
║     🔥🔥🔥 두 구역 동시 충족 → "적극 매수 추천!"                     ║
║     🔥     한 구역 충족     → "매수 신호 발생!"                      ║
║     ⏸️     조건 미충족      → "대기"                                 ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# =============================================================================
# 6. 전고점 청산 신호 설정
# =============================================================================
"""
╔══════════════════════════════════════════════════════════════════════╗
║                 전고점 근접 50% 청산 신호 설정                       ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   조건 1: 현재가 > 전고점(ATH) × 90%                                 ║
║     → get_rolling_ath(prices) — Close 기준 (표준 방법론)          ║
║     → ATH 비율 = 현재가 / ATH * 100                                 ║
║                                                                      ║
║   조건 2: 20일 상승률 40% 이상                                       ║
║     → get_20day_return(closes)                                       ║
║     → (close[-1] / close[-21] - 1) ≥ 0.40                           ║
║                                                                      ║
║   조건 3: 단기 Sigma(20d) / 장기 Sigma(252d) 비율                    ║
║     → get_sigma_spike_ratio()                                        ║
║     → 현재 비활성화 (SOXL 특성상 0.0으로 설정 = 항상 통과)          ║
║                                                                      ║
║   매도 실행: 50% 포지션 청산 (백테스트 전용)                         ║
║   쿨다운: 60거래일 (약 3개월) 동안 재매도 금지                       ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# =============================================================================
# 7. GitHub Actions 워크플로우
# =============================================================================
"""
📄 .github/workflows/sigma_dca_manager.yml (Sigma DCA Manager Engine)

name: Sigma DCA Manager Engine
on:
  schedule:
    - cron: '24 23 * * 1-5'   # 월~금 23:24 UTC = 19:24 ET (장 마감 후)
  workflow_dispatch:           # 수동 실행 지원

jobs:
  run-dca-manager:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - run: pip install yfinance pandas requests numpy pytz pandas_market_calendars
      - run: python sigma_DCA_manager.py > sigma_log.txt 2>&1
      - name: Sync and Notify
        run: |
          git config --global user.name "DCA Bot"
          git config --global user.email "bot@example.com"
          git add sigma_log.txt portfolio_config.json sigma_history.csv
          git commit -m "update: dca-log $(date +'%Y-%m-%d')" || echo "No changes"
          git push


📄 .github/workflows/bear_market_signals.yml (Bear Market Signals Engine)

name: Bear Market Signals Engine
on:
  schedule:
    - cron: '0 23 * * 1-5'    # 월~금 23:00 UTC = 19:00 ET
  workflow_dispatch:

jobs:
  run-signals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - run: pip install yfinance pandas requests numpy pytz holidays
      - run: python bear_market_signals.py > bear_log.txt 2>&1
      - name: Sync and Notify
        run: |
          git add bear_log.txt signal_report.json bear_config.json
          git commit -m "update: signals $(date +'%Y-%m-%d')" || echo "No changes"
          git push


📄 .github/workflows/tracker.yml (Market Stage Tracker)

name: Market Stage Tracker
on:
  schedule:
    - cron: '14 23 * * 1-5'   # 월~금 23:14 UTC = 19:14 ET
  workflow_dispatch:

jobs:
  run-tracker:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - run: pip install pandas requests yfinance
      - run: python MarketStageSystem.py > market_log.txt 2>&1
      - name: Commit and Push
        run: |
          git config --global user.name "Market Bot"
          git config --global user.email "bot@tracker.com"
          git add -f market_log.txt market_state.json
          git commit -m "Auto-update logs and stage state" || echo "No changes"
          git pull --rebase
          git push


📌 워크플로우 실행 순서 (23:00~23:24 UTC, 월~금):
  1. 23:00 UTC — bear_market_signals.yml   (시장 리스크 평가)
  2. 23:14 UTC — tracker.yml               (시장 단계 추적)
  3. 23:24 UTC — sigma_dca_manager.yml      (LOC 브리핑)


📌 실행 로그 예시 (GitHub Actions Console):

  📊 Auto-updating Sigma for SOXL...
  📊 Calculating real-time Sigma for SOXL (Lookback: 252/EWMA)...
  ✅ SOXL Sigma calculation success: 0.0798 (method: EWMA)
  🔍 Starting price lookup for TQQQ...
  ✅ TQQQ yfinance success: $42.15 (07-29)
  🔍 Starting price lookup for SOXL...
  ✅ SOXL yfinance success: $12.34 (07-29)
  📊 ATH DCA check: TQQQ - 모든 분할 완료 (재진입 대기)
  ⚙️ Mode: LOC → (ATH DD >= 35% 시 자동 전환)
  📊 ATH DCA check: SOXL - 모든 분할 완료 (재진입 대기)
  ⚙️ Mode: LOC → (ATH DD >= 60% 시 자동 전환)
  ✅ Discord briefing sent successfully.


📌 디스코드 출력 예시 (2026-07-30 기준):

  🌙 U.S. Market LOC Portfolio Briefing (2026-07-30 16:00 EST)
  📊 Market Risk Score: 5 / 14
  ────────────────────────────────────────

  🔹 TQQQ (Close: $42.15 | 07-29 | LONG_YEAR / D+5)
  • 📈 전고점: $87.02 (2026-07-25) 기준 하락률 -51.56% / 회복 필요 상승률 106.43%
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$40.50**

  📡 TQQQ RSI+Volume: ⏸️ 대기 (조건 미충족)
     └ RSI: 41.7 | Vol: 1.11× 20일 평균
     └ [RSI 25~35 Vol 0.3~0.7×] | [RSI 35~50 Vol 0.4~1.0×]

  🚨 TQQQ ATH DCA 1차 매수 신호! 🔥 (LOC → ATH_DCA 모드 전환)
  ⚙️ Mode: LOC → ATH_DCA (자동 전환)
     • ATH: $87.02 | 현재 DD: -51.6% (임계: -10%)
     • 현재가: $42.15 | 목표가: $78.32 (이하)
     • 잔여: 2/3차

  🔹 SOXL (Close: $12.34 | 07-29 | LONG_YEAR / D+5)
  • 📈 전고점: $300.77 (2026-07-25) 기준 하락률 -95.90% / 회복 필요 상승률 2337.68%
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$10.52**

  📡 SOXL RSI+Volume: ⏸️ 대기 (조건 미충족)
     └ RSI: 39.1 | Vol: 1.49× 20일 평균
     └ [RSI 25~34 Vol 0.3~0.7×] | [RSI 34~40 Vol 0.4~0.9×]

  🚨 SOXL ATH DCA 1차 매수 신호! 🔥 (LOC → ATH_DCA 모드 전환)
  ⚙️ Mode: LOC → ATH_DCA (자동 전환)
     • ATH: $300.77 | 현재 DD: -95.9% (임계: -15%)
     • 현재가: $12.34 | 목표가: $255.65 (이하)
     • 잔여: 2/3차
"""

# =============================================================================
# 8. 파일 구성도
# =============================================================================
"""
📁 strategy_engine/
│
├── 📄 sigma_DCA_manager.py              ★ 메인 실행 파일 (LOC/Discord 브리핑)
├── 📄 sigma_DCA_manager_flowchart.py    ★ 본 문서
├── 📄 sigma_backtest.py                 백테스트 엔진 (승수 스윕/포트폴리오 최적화)
├── 📄 optimize_ath_dca.py               ATH DCA 트리거 최적화
├── 📄 verify_split_strategy.py         분할 매수 전략 검증
│
├── 📄 MarketStageSystem.py              시장 단계 트래커
├── 📄 bear_market_signals.py            약세장 신호 분석
│
├── 📄 portfolio_config.json             ★ 포트폴리오 설정 (핵심 설정 파일)
├── 📄 (MarketStage_config.json — 제거됨, portfolio_config.json으로 통합)
├── 📄 market_state.json                 시장 상태 저장 (자동 생성)
├── 📄 signal_report.json                신호 리포트 (자동 생성)
├── 📄 sigma_history.csv                 Sigma 변경 이력 (자동 생성)
│
├── 📄 README.md                         시스템 문서
├── 📄 requirements.txt                  Python 의존성 목록
├── 📄 cape_cache.json                   CAPE 캐시 (bear_market_signals.py)
│
└── 📁 .github/workflows/
    ├── sigma_dca_manager.yml           ★ DCA 자동 실행 (23:24 UTC)
    ├── bear_market_signals.yml         신호 분석 자동 실행 (23:00 UTC)
    └── tracker.yml                     시장 단계 추적 자동 실행 (23:14 UTC)
"""

if __name__ == "__main__":
    print("=" * 78)
    print("  📖 Sigma DCA Manager — System Flowchart")
    print("  Open this file in any text editor to view the flowchart.")
    print("=" * 78)
    print()
    print("  This is a DOCUMENTATION file (not executable).")
    print("  It contains the complete system architecture")
    print("  in ASCII diagrams and structured comments.")
    print()
    print("  📍 File: sigma_DCA_manager_flowchart.py")
    print("  📅 Last updated: 2026-07-30")
    print()
    print("  💡 Tip: Use 'cat' to view, or open in VS Code")
    print("  with collapsed sections for easy navigation.")
    print("=" * 78)
