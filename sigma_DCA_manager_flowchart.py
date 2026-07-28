#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════
  Sigma DCA Manager — 전체 시스템 플로우차트
══════════════════════════════════════════════════════════════════════
  파일: sigma_DCA_manager.py
  최종 업데이트: 2026-07-28
══════════════════════════════════════════════════════════════════════

[목차]
  1. 시스템 개요
  2. 전체 실행 흐름도
  3. 함수 호출 관계도
  4. 데이터 파일 의존성
  5. 최종 Zone 설정
  6. GitHub Actions 워크플로우
  7. 파일 구성도
══════════════════════════════════════════════════════════════════════
"""

# =============================================================================
# 1. 시스템 개요
# =============================================================================
"""
📌 Sigma DCA Manager는 매일 정해진 시간에 GitHub Actions에서 실행되어,
   portfolio_config.json에 설정된 포지션(TQQQ, SOXL)의 LOC(Limit Order
   Conditional) 매수 목표가를 계산하고, RSI+Volume 복합 신호를 평가하여
   디스코드로 브리핑을 전송하는 자동화 시스템입니다.

🔗 연동 시스템:
  - MarketStageSystem.py → market_state.json (바닥 단계 정보)
  - bear_market_signals.py  → signal_report.json (시장 리스크 점수)
"""

# =============================================================================
# 2. 전체 실행 흐름도
# =============================================================================
"""
┌──────────────────────────────────────────────────────────────────────────────┐
│                         🚀 execute_dual_tactical_trader()                    │
│                    (GitHub Actions가 매일 자동 실행)                          │
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
│  └─ 각 포지션의 INVEST_TYPE == "ROTATION_3M" 확인                            │
│     ├─ YES → check_rotation_exit_signal() → 만료일 도달?                     │
│     │  ├─ YES → Sigma 재계산 + START_DATE 리셋                               │
│     │  └─ NO  → 다음 포지션                                                 │
│     └─ NO  → 다음 포지션                                                     │
│  └─ 결과 메시지를 console에 출력 (GitHub Actions 로그)                       │
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
│  [4] 디스코드 브리핑 생성 ← _build_briefing_lines(now_ny, cfg)              │
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
│  │  │  └─ ROTATION_3M → MA20/MA60 crossover 분석                       │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-b] 전일 종가 조회                                            │ │ │
│  │  │  get_prev_close(ticker) ← yfinance 1mo 데이터 (3회 재시도)        │ │ │
│  │  │  ├─ 장 마감 후 (16:15 NY) → 오늘 종가                           │ │ │
│  │  │  ├─ 장 중 → 전일 종가                                            │ │ │
│  │  │  └─ 실패 시 → yfinance info API fallback                         │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-c] 전고점 대비 하락률                                         │ │ │
│  │  │  format_drawdown_line(ticker, prev_close, 252)                   │ │ │
│  │  │  ├─ get_period_high() → 252일 최고가                             │ │ │
│  │  │  └─ calculate_drawdown_and_recovery()                           │ │ │
│  │  │     → "전고점 $XX 기준 하락률 -XX% / 회복필요 XX%"               │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-d] LOC 목표가 계산                                           │ │ │
│  │  │  calculate_loc_price(ticker, prev_close, cfg)                    │ │ │
│  │  │  ├─ DAILY_SIGMA × ENTRY_MULTIPLIER × 전일종가                    │ │ │
│  │  │  ├─ calculate_final_loc() → Risk Discount (비활성화)             │ │ │
│  │  │  └─ "🎯 LOC Buy: $XX.XX"                                        │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-e] Rotation 만료 체크                                        │ │ │
│  │  │  check_rotation_exit_signal()                                    │ │ │
│  │  │  └─ 만료 시 "🔴 D+XX Rotation Maturity" 경고                     │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-f] All-In 신호 (Stage 5)                                     │ │ │
│  │  │  _format_all_in_line(ticker)                                     │ │ │
│  │  │  ├─ get_bottom_stage() → market_state.json                       │ │ │
│  │  │  └─ "🔥 Stage 5 All-In → XX% lump-sum buy"                      │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [4-g] RSI + Volume 복합 신호 ⭐                                 │ │ │
│  │  │  _check_rsi_volume_signal(ticker)                                │ │ │
│  │  │  ├─ yfinance 6mo 데이터 다운로드                                  │ │ │
│  │  │  ├─ RSI 계산 (SOXL: 14일 / TQQQ: 21일)                          │ │ │
│  │  │  ├─ 20일 볼륨 이동평균 계산                                       │ │ │
│  │  │  ├─ Zone 1 검사 (RSI + Volume 조건)                              │ │ │
│  │  │  ├─ Zone 2 검사 (RSI + Volume 조건)                              │ │ │
│  │  │  └─ 결과: 🔥🔥🔥 두 구역 / 🔥 한 구역 / ⏸️ 대기                   │ │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [5] 디스코드 전송                                                            │
│  _send_discord(webhook, user_id, title, content)                             │
│  ├─ Embed 메시지 생성 (제목 + 설명 + 타임스탬프 + 색상)                       │
│  ├─ @유저 멘션 포함                                                          │
│  ├─ 내용 4096자 제한 / 제목 256자 제한                                       │
│  └─ Discord Webhook API 호출                                                 │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [6] 월간 핑 (매월 1일만)                                                    │
│  send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)                     │
│  ├─ now_ny.day == 1? AND LAST_MONTHLY_PING != this month?                   │
│  ├─ YES → "📅 Monthly Operation Ping" 전송                                  │
│  └─ LAST_MONTHLY_PING 업데이트 → save_portfolio()                           │
└──────────────────────────────────────────────────────────────────────────────┘

        ┌───────────┐
        │   ✅ 완료   │
        └───────────┘
"""

# =============================================================================
# 3. 함수 호출 관계도
# =============================================================================
"""
execute_dual_tactical_trader()
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
├── _build_briefing_lines()
│   ├── get_market_score()                     ← signal_report.json
│   │
│   │  ── [각 포지션 반복] ──
│   │
│   ├── check_macro_and_technical_signals()    ← yfinance API
│   ├── get_prev_close()                       ← yfinance API
│   │   └── _most_recent_trading_day()
│   ├── format_position_meta()
│   │   └── business_days_elapsed()
│   ├── format_drawdown_line()
│   │   ├── get_period_high()                  ← yfinance API
│   │   └── calculate_drawdown_and_recovery()
│   ├── check_rotation_exit_signal()
│   ├── _format_loc_action_line()
│   │   └── calculate_loc_price()
│   │       ├── _calculate_loc_from_sigma()
│   │       └── get_realtime_sigma()
│   │           └── _fetch_closes_for_lookback()
│   ├── _format_all_in_line()                  ← market_state.json
│   │   ├── get_bottom_stage()
│   │   └── get_all_in_percent()               ← MarketStage_config.json
│   │
│   └── ⭐ _check_rsi_volume_signal() ★NEW★   ← yfinance API
│       ├── _calculate_rsi()     (SOXL: 14일 / TQQQ: 21일)
│       ├── _TICKER_ZONES lookup (SOXL / TQQQ)
│       └── Zone 1 + Zone 2 평가
│
├── _send_discord()                           → Discord Webhook
└── send_monthly_ping_if_due()
    └── save_portfolio()
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
 │   └── ROTATION_EXIT_DAYS (for ROTATION_3M)
 └── LAST_MONTHLY_PING

 MarketStage_config.json  (읽기 전용, MarketStageSystem.py 소유)
 └── TICKERS → SOXL / TQQQ
     └── ALL_IN_PERCENT

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
 ├── 252d+ 데이터 → get_period_high() / get_realtime_sigma()
 └── 6mo 데이터 → _check_rsi_volume_signal() (RSI + Volume)

 Discord Webhook (외부 출력)
 └── Embed 메시지 → 디스코드 채널
"""

# =============================================================================
# 5. 최종 Zone 설정 (2026-07-28 기준, 12년 백테스트 검증)
# =============================================================================
"""
╔══════════════════════════════════════════════════════════════════════╗
║                    RSI + Volume Zone 설정                           ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   SOXL ──────────────────────────────────────────────────────────   ║
║     RSI 기간: 14일                                                   ║
║     Zone 1: RSI  25~34  |  Volume 0.3~0.7× MA20  (저RSI 저볼륨)     ║
║     Zone 2: RSI  34~40  |  Volume 0.4~0.9× MA20  (중간RSI 중볼륨)   ║
║     → Sharpe 2.62 | 승률 71% | 12년 백테스트                        ║
║                                                                      ║
║   TQQQ ──────────────────────────────────────────────────────────   ║
║     RSI 기간: 21일                                                   ║
║     Zone 1: RSI  25~35  |  Volume 0.3~0.7× MA20  (저RSI 저볼륨)     ║
║     Zone 2: RSI  35~50  |  Volume 0.4~1.0× MA20  (중간RSI 중볼륨)   ║
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
# 6. GitHub Actions 워크플로우
# =============================================================================
"""
📄 .github/workflows/sigma_dca_manager.yml

name: Sigma DCA Manager
on:
  schedule:
    - cron: '0 20 * * 1-5'    # 월~금, 20:00 UTC = 16:00 EST / 15:00 EDT
  workflow_dispatch:           # 수동 실행 지원

jobs:
  run-dca:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'
      - run: pip install -r requirements.txt
      - run: python sigma_DCA_manager.py
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
          DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}

📌 실행 로그 예시 (GitHub Actions Console):

  📊 Calculating real-time Sigma for SOXL (Lookback: 252/EWMA)...
  ✅ SOXL Sigma calculation success: 0.0798 (method: EWMA)
  🔍 Starting price lookup for TQQQ...
  ✅ TQQQ yfinance success: $42.15 (07-27)
  🔍 Starting price lookup for SOXL...
  ✅ SOXL yfinance success: $12.34 (07-27)
  ✅ Discord briefing sent successfully.

📌 디스코드 출력 예시:

  🌙 U.S. Market LOC Portfolio Briefing (2026-07-28 16:00 EST)
  📊 Market Risk Score: 5 / 14
  ────────────────────────────────────────

  🔹 TQQQ (Close: $42.15 | 07-27 | D+45)
  • 📈 전고점: $75.23 (2026-02-18) 기준 하락률 -43.97% / 회복 필요 상승률 78.48%
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$36.50**

  📡 TQQQ RSI+Volume: ⏸️ 대기 (조건 미충족)
     └ RSI: 41.7 | Vol: 1.11× 20일 평균
     └ [RSI 25~35 Vol 0.3~0.7×] | [RSI 35~50 Vol 0.4~1.0×]

  🔹 SOXL (Close: $12.34 | 07-27 | D+45)
  • 📈 전고점: $45.67 (2026-03-15) 기준 하락률 -72.97% / 회복 필요 상승률 270.10%
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$10.52**

  📡 SOXL RSI+Volume: ⏸️ 대기 (조건 미충족)
     └ RSI: 39.1 | Vol: 1.49× 20일 평균
     └ [RSI 25~34 Vol 0.3~0.7×] | [RSI 34~40 Vol 0.4~0.9×]
"""

# =============================================================================
# 7. 파일 구성도
# =============================================================================
"""
📁 strategy_engine/
│
├── 📄 sigma_DCA_manager.py          ★ 메인 실행 파일
├── 📄 sigma_backtest.py             백테스트 엔진
├── 📄 MarketStageSystem.py          시장 단계 트래커
├── 📄 bear_market_signals.py        베어마켓 신호 분석
│
├── 📄 portfolio_config.json         포트폴리오 설정
├── 📄 MarketStage_config.json       시장 단계 설정
├── 📄 market_state.json             시장 상태 저장
├── 📄 signal_report.json            신호 리포트
├── 📄 sigma_history.csv             시그마 변경 이력
│
├── 📄 requirements.txt              Python 의존성
├── 📄 verify_soxl_rsi_strategy.py   유튜브 전략 검증
├── 📄 soxl_optimal_condition.py     SOXL 최적 조건 탐색
├── 📄 tqqq_rsi_analysis.py          TQQQ RSI 분석
├── 📄 rsi_period_optimizer.py       RSI 기간 최적화
├── 📄 tqqq_rsi21_optimize.py        TQQQ RSI21 최적화
├── 📄 soxl_zone_tuning.py           SOXL Zone 튜닝
├── 📄 sigma_DCA_manager_flowchart.py  ★ 본 문서
│
└── 📁 .github/workflows/
    ├── sigma_dca_manager.yml        ★ DCA 자동 실행
    ├── bear_market_signals.yml      신호 분석 자동 실행
    └── tracker.yml                  시장 단계 트래킹
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
    print(f"  📅 Date: 2026-07-28")
    print()
    print("  💡 Tip: Use 'cat' to view, or open in VS Code")
    print("  with collapsed sections for easy navigation.")
    print("=" * 78)
