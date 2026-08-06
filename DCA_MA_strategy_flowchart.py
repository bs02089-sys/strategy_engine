#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════
  DCA MA Strategy — 전체 시스템 플로우차트
══════════════════════════════════════════════════════════════════════
  파일: DCA_MA_strategy.py
  최종 업데이트: 2026-08-03
  듀얼 모드: LOC (일반) / ATH DCA (비상)
  비상 모드 종료: 시장 회복 감지 시 LOC 자동 복귀 (RECOVERY_REENTRY)
  실시간 알림: cron-job.org → repository_dispatch → --ath-monitor
══════════════════════════════════════════════════════════════════════

[목차]
  1. 시스템 개요
  2. 전체 실행 흐름도
  3. 함수 호출 관계도
  4. 데이터 파일 의존성
  5. RSI + Volume Zone 설정 (12년 백테스트 검증)
  6. GitHub Actions 워크플로우
  7. 파일 구성도
══════════════════════════════════════════════════════════════════════
"""

# =============================================================================
# 1. 시스템 개요
# =============================================================================
"""
📌 DCA MA Strategy는 매일 정해진 시간에 GitHub Actions에서 실행되어,
   portfolio_config.json에 설정된 포지션(TQQQ, SOXL)의 LOC 매수 목표가를
   계산하고, RSI+거래량 복합 신호, ATH 하락분할 DCA,
   비상 모드 종료(회복 감지)를 평가하여 디스코드로 종합 브리핑을 전송합니다.
   또한 cron-job.org가 발사하는 repository_dispatch로 장중 실시간 ATH DCA
   알림(--ath-monitor)을 전송합니다.

🔗 연동 시스템:
  - MarketStageSystem.py → market_state.json (바닥 단계 정보)
  - bear_market_signals.py → signal_report.json (시장 리스크 점수)
  - cron-job.org → repository_dispatch (실시간 알림 발사)
"""

# =============================================================================
# 2. 전체 실행 흐름도
# =============================================================================
"""
┌──────────────────────────────────────────────────────────────────────────────┐
│                   🚀 DCA_MA_strategy.py (메인 진입점)                       │
│   (GitHub Actions: 매일 23:30 UTC 통합 브리핑 + cron-job.org 실시간 dispatch) │
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
│  [4] 듀얼 모드 전환 평가 ⭐신규⭐                                               │
│                                                                              │
│  _evaluate_all_strategy_modes(cfg)                                           │
│  ├─ 각 포지션의 STRATEGY_MODE 확인                                           │
│  │                                                                          │
│  │  LOC → ATH_DCA 전환 조건:                                                 │
│  │  ├─ ATH_DCA.ENABLED == true                                              │
│  │  ├─ 현재 ATH DD >= TRIGGER_1 (TQQQ: -35%, SOXL: -60%)                   │
│  │  └─ 모드 전환: STRATEGY_MODE = "ATH_DCA"                                 │
│  │                                                                          │
│  │  ATH_DCA → LOC 전환 조건: (자동 비상 모드 종료 — RECOVERY_REENTRY)         │
│  │  _check_recovery_reentry() — 아래 4조건 모두 충족 시 자동 복귀:             │
│  │  ├─ ① 잔여 분할 1개 이상 (2차/3차 예비금 보존)                            │
│  │  ├─ ② 진입일(ATH_DCA_ENTERED_ON)부터 MIN_DAYS(30) 영업일 경과            │
│  │  ├─ ③ DD ≤ DD_RATIO(0.5) × TRIGGER_1   (TQQQ 17.5% / SOXL 30%)         │
│  │  └─ ④ MA20 > MA60 (불리시 정렬, MA_CONFIRM=true)                         │
│  │  └─ (사용자 수동 STRATEGY_MODE="LOC" 변경도 동작)                        │
│  │                                                                          │
│  ├─ 대기 중 표시: _recovery_wait_line() → ⏳ D+X/MIN_DAYS (하루 1회 dedup)   │
│  ├─ 임박 넛지: _recovery_nudge_line() → 🔔 남은 D-5/D-1 (1회 dedup)         │
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
│  │  └─ 🚨 ATH_DCA 모드: LOC 참고가 + ATH DCA 브리핑                      │ │
│  │  📌 신호 통합 (브리핑 1건 — MA 레짐 신호 포함):                        │ │
│  │  ├─ ▶ 실행 액션 라인 (_ma_action_line) — MA 레짐 기반                  │ │
│  │  └─ 📡 다음 비상 트리거 (_next_trigger_line) — 가격($) 포함            │ │
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
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [6] ATH 하락분할 DCA 신호 확인 ⭐신규⭐                                    │
│                                                                              │
│  check_ath_dca_signals(alerts_only=False)                                   │
│  └─ 각 포지션 ATH_DCA.ENABLED 확인                                           │
│     ├─ 비활성화 → 다음 포지션                                                │
│     ├─ 활성화 → yfinance 1년 데이터 다운로드                                 │
│     │  ├─ ATH 계산 (Close 기준, expanding max)                              │
│     │  ├─ 현재 하락률(DD) 계산 (yfinance 종가 기준)                          │
│     │  └─ 각 TRIGGER_N 평가 (1차/2차/3차...)                                 │
│     │     ├─ DD ≥ 임계값 → 🚨 매수 신호! + ATH_DCA_USED_SPLITS 기록         │
│     │     ├─ DD < 임계값 but 5%p 이내 → 📡 임박 알림 (가격 $ 포함)          │
│     │     │   (alerts_only: ATH_DCA_IMMINENT_SENT로 갭 1.0%p 좁힘 시만 재알림) │
│     │     └─ 기타 → 건너뜀                                                   │
│     │  └─ 상태 라인: "다음(N차): 추가 X.X%p 하락 시 ($목표가)" (트리거 가격 표시)
│     ├─ 전체 분할 완료 시 ATH_DCA_CYCLE_ATH 기록                              │
│     └─ 신규 ATH > CYCLE_ATH × 1.01 → 🔄 사이클 재시작 준비 (사용 분할 초기화)       │
│                                                                              │
│  ⭐실시간 모드 (--ath-monitor):                                              │
│  run_ath_dca_monitor()                                                       │
│  ├─ yfinance 종가 기준 (Finnhub 키 불필요 — 2026-08 키 로직 제거)           │
│  ├─ check_ath_dca_signals(alerts_only=True) → 🚨/📡만, 상태 줄 생략           │
│  └─ 트리거/임박만 Discord 전송 (스팸 방지 dedup)                              │
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
│  (실시간 모드는 title="🚨 ATH DCA Realtime Alert"로 전송)                     │
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
   - "LOC" (📗 일반 모드):     20분할 Sigma 기반 LOC 매수 진행
   - "ATH_DCA" (🚨 비상 모드):  3분할 ATH 하락분할 DCA 매수 (LOC 중단)
   - ATH_DCA → LOC: 비상 모드 종료 (RECOVERY_REENTRY 4조건 자동 복귀)

📌 비상 모드 종료 실효성 검증 (백테스트, 2026-08-02):
   (2020 COVID 크래시 포함 구간 백테스트)에서 TQQQ가 현행 대비 +4.67%p 우위
   (+136.17% vs +131.50%, Sharpe 2.17 vs 2.14, MDD 동일 -39.62%).
   단, 크래시 후 잔여 현금(예비금)이 남아있을 때만 효과가 있으므로
   예비금 보존이 실효성의 핵심. 상세는 DUAL_MODE_SUMMARY.md 참고.

📌 ATH DCA 체크([6])는 브리핑 빌더와 별도로 실행되지만, 그 상태 라인은
   통합 브리핑 하단 "📉 ATH Drawdown DCA Monitor" 섹션에 포함되어
   하나의 Discord 메시지로 전송됨 (트리거 도달 가격($) 포함).

📌 실시간 알림 (--ath-monitor): 야간 브리핑과 별도로 cron-job.org가
   repository_dispatch를 발사하면 워크플로우가 --ath-monitor 분기로 실행.
"""

# =============================================================================
# 3. 함수 호출 관계도
# =============================================================================
"""
[메인 실행 흐름]
DCA_MA_strategy.py (직접 실행)
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
│   └── ATH_DCA → LOC (자동 비상 모드 종료):
│       └── _check_recovery_reentry()  — ①잔여분할 ②30일 ③DD≤DD_RATIO×T1 ④MA20>MA60
│   ├── _recovery_wait_line()                  ⏳ 대기 모니터 (하루 1회 dedup)
│   └── _recovery_nudge_line()                 🔔 임박 넛지 (D-5/D-1, 1회 dedup)
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
│   ├── mode_line: 📗 일반 모드 (LOC) / 🚨 비상 모드 (ATH DCA)
│   │   └── 비상 모드 종료 발생 시 " | 🔄 **비상 모드 종료** (reason)" 추가
│   ├── get_bottom_stage()                     ← market_state.json
│   │   └── Stage 5 → ATH DCA 3차 트리거로 통합
│   ├── _check_ma_filter()                     ← MA 레짐 필터 (LOC 모드 활성)
│   │   ├── _fetch_ma_closes()                 ← yfinance API (auto_adjust=True)
│   │   │   └── _drop_unsettled_today_bar()    장중 미확정 바 제외 (거짓 크로스 방지)
│   │   ├── 레짐 판정 (종가 > MA: above / < MA: below)
│   │   ├── 크로스 1회 감지 (MA_FILTER_STATE 영속화)
│   │   └── ATH_DCA 비상 모드 → suspended (OFF — 상태만 추적)
│   ├── _ma_filter_lines()                     → 레짐/크로스 알림 라인 (🚨/💰/🔄)
│   ├── _ma_action_line()                      → ▶ 실행 액션 라인 (신호 통합)
│   │   └── crossed_down/below/above/crossed_up → 매도/현금유지/보유/재매수
│   └── _next_trigger_line()                   → 📡 다음 비상 트리거 (가격 $ 포함)
│       └── _ath_info()                        → ATH/MDD + next_trigger/next_price
│
├── check_ath_dca_signals(alerts_only=False)
│   ├── _parse_ath_trigger()                   (-30% → 0.30)
│   ├── yfinance 1년 데이터 다운로드
│   ├── ATH (expanding max) 계산
│   ├── 현재가 = yfinance 종가 (Finnhub 오버라이드 제거 — 2026-08)
│   ├── 각 TRIGGER_N 평가 (alerts_only: 📡 갭 1.0%p dedup)
│   └── ATH_DCA_USED_SPLITS / CYCLE_ATH 관리
│
├── _send_discord()                            → Discord Webhook
│
├── run_ath_dca_monitor()                      ⭐실시간 모드 (--ath-monitor)
│   ├── yfinance 종가 기준 (Finnhub 키 불필요 — 2026-08 키 로직 제거)
│   ├── check_ath_dca_signals(alerts_only=True)
│   ├── _check_ma_filter() → 크로스 알림      ← MA 레짐 크로스 (LOC 모드만, 1회 dedup)
│   └── 🚨/📡/🔄 알림만 Discord 전송
│
└── send_monthly_ping_if_due()
    └── save_portfolio()


[전고점 하락률 표시 (브리핑)]
get_period_ath()                               ← yfinance API
  └── _fetch_closes_for_lookback()             (retry 로직 공유)

format_drawdown_line()
  ├── get_period_ath()
  └── calculate_drawdown_and_recovery()
      → "전고점 $XX 기준 하락률 XX% / 회복필요 XX%"

[MA 레짐 필터 엔진 (실전 반영 — 백테스트 검증)]
_check_ma_filter()
  ├── _fetch_ma_closes()                       ← yfinance API
  │   └── _drop_unsettled_today_bar()
  ├── MA_FILTER_STATE {regime, since} 영속화
  ├── crossed_down / crossed_up 1회 감지
  └── suspended (ATH_DCA 모드 OFF)

_ma_filter_lines()
  └── 🟢/🟡 레짐 상태 + 🚨 하향 돌파(전량 청산) / 💰 전액 재매수(TQQQ) / 🔄 DCA 재개(SOXL)
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
 │   ├── STRATEGY_MODE ⭐ (LOC / ATH_DCA — 자동 관리)
 │   ├── MA_FILTER ⭐신규 (TQQQ: MA20+lump, SOXL: MA250+dca_reset)
 │   │   ├── ENABLED, MA_DAYS, REENTRY, REENTRY_PCT
 │   │   ├── MA_FILTER_STATE {regime, since} (자동 관리)
 │   │   └── MA_FILTER_CONFIG_FINGERPRINT (설정 변경 감지)
 │   ├── ATH_DCA ⭐신규
 │   │   ├── ENABLED (true/false)
 │   │   ├── SPLITS (분할 수)
 │   │   ├── TRIGGER_1 ~ TRIGGER_N (하락률 임계값 / STAGE5)
 │   │   └── STRATEGY (설명)
 │   ├── ATH_DCA_USED_SPLITS (사용된 분할 목록, 자동 관리)
 │   ├── ATH_DCA_CYCLE_ATH (사이클 완료 시점 ATH 기록)
 │   ├── ATH_DCA_CONFIG_FINGERPRINT ⭐신규 (설정 변경 감지용 지문)
 │   │   → TRIGGER/SPLITS/STRATEGY 변경 시 자동 감지 → 분할 상태 초기화
 │   ├── ATH_DCA_ENTERED_ON (비상 모드 진입일 — 유지 클럭 기준)
 │   ├── ATH_DCA_WAIT_SENT / ATH_DCA_NUDGE_SENT (⏳/🔔 전송 dedup)
 │   ├── ATH_DCA_IMMINENT_SENT (📡 실시간 임박 갭 dedup)
 │   └── RECOVERY_REENTRY ⭐ (비상 모드 종료: ENABLED/DD_RATIO/MIN_DAYS/MA_CONFIRM)
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
 ├── 252d+ 데이터 → get_period_ath() / get_realtime_sigma() / recompute_sigma_for_ticker()
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
# 6. GitHub Actions 워크플로우
# =============================================================================
"""
📄 .github/workflows/dca_ma_strategy.yml (Sigma DCA Manager Engine)

name: Sigma DCA Manager Engine
on:
  schedule:
    - cron: '30 23 * * 1-5'   # 월~금 23:30 UTC = 19:30 ET (장 마감 후 통합 브리핑)
  repository_dispatch:
    types: [ath-dca-monitor]  # cron-job.org 실시간 알림 발사
  workflow_dispatch:           # 수동 실행 지원

concurrency:
  group: sigma-dca-manager     # 야간 브리핑 ↔ 실시간 폴링 직렬화
  cancel-in-progress: false

jobs:
  run-dca-manager:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
      DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.14'}
      - run: pip install -r requirements.txt
      - name: Realtime Monitor (repository_dispatch)
        if: github.event_name == 'repository_dispatch'
        run: |
          set -o pipefail
          python3 DCA_MA_strategy.py --ath-monitor 2>&1 | tee sigma_log.txt
      - name: Scheduled Briefing
        if: github.event_name != 'repository_dispatch'
        run: |
          set -o pipefail
          python3 DCA_MA_strategy.py 2>&1 | tee sigma_log.txt
      - name: Sync and Notify
        if: always()
        run: |
          git config --global user.name "DCA Bot"
          git config --global user.email "bot@example.com"
          # 상태 파일만 커밋 (로그/히스토리는 저장하지 않음)
          [ -f "portfolio_config.json" ] && git add portfolio_config.json
          git commit -m "update: dca-state $(date +'%Y-%m-%d')" || echo "No changes to commit"
          git pull --rebase || true
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
        with: {python-version: '3.14'}
      - run: pip install -r requirements.txt
      - name: Run Market Signals
        run: |
          set -o pipefail
          python bear_market_signals.py 2>&1 | tee bear_log.txt
      - name: Sync and Notify
        run: |
          # 상태 파일만 커밋 (로그는 저장하지 않음)
          [ -f "signal_report.json" ] && git add signal_report.json
          git commit -m "update: signals $(date +'%Y-%m-%d')" || echo "No changes to commit"
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
        with: {python-version: '3.14'}
      - run: pip install -r requirements.txt
      - name: Run Tracker
        run: |
          set -o pipefail
          python MarketStageSystem.py 2>&1 | tee market_log.txt
      - name: Commit and Push
        run: |
          git config --global user.name "Market Bot"
          git config --global user.email "bot@tracker.com"
          # 상태 파일만 커밋 (로그는 저장하지 않음)
          git add -f market_state.json
          git commit -m "Auto-update stage state" || echo "No changes"
          git pull --rebase
          git push


📌 워크플로우 실행 순서 (23:00~23:30 UTC, 월~금):
  1. 23:00 UTC — bear_market_signals.yml   (시장 리스크 평가)
  2. 23:14 UTC — tracker.yml               (시장 단계 추적)
  3. 23:30 UTC — dca_ma_strategy.yml      (통합 브리핑 1건 — MA 레짐 실행 액션·비상 트리거 포함)
  4. 장중 N분 — cron-job.org → repository_dispatch(ath-dca-monitor) → --ath-monitor 실시간 알림


📌 실행 로그 예시 (GitHub Actions Console):

  📊 Auto-updating Sigma for SOXL...
  📊 Calculating real-time Sigma for SOXL (Lookback: 252/EWMA)...
  ✅ SOXL Sigma calculation success: 0.0798 (method: EWMA)
  🔍 Starting price lookup for TQQQ...
  ✅ TQQQ yfinance success: $42.15 (07-29)
  🔍 Starting price lookup for SOXL...
  ✅ SOXL yfinance success: $12.34 (07-29)
  📊 ATH DCA check: TQQQ - 모든 분할 완료 (사이클 재시작 대기)
  ⚙️ Mode: LOC → (ATH DD >= 35% 시 자동 전환)
  📊 ATH DCA check: SOXL - 모든 분할 완료 (사이클 재시작 대기)
  ⚙️ Mode: LOC → (ATH DD >= 60% 시 자동 전환)
  ✅ Discord briefing sent successfully.


📌 디스코드 출력 예시 (2026-07-31 기준 — 통합 브리핑 1건):

  🌙 U.S. Market LOC Portfolio Briefing (2026-07-31 19:30 EDT)
  📊 Market Risk Score: 7 / 14
  ────────────────────────────────────────

  🔹 TQQQ (Close: $64.62 | 07-31 | LONG_YEAR / D+4)
  • 📈 전고점: $87.02 (2026-06-02) 기준 하락률 -25.74% / 회복 필요 34.66%
  • Mode: 🚨 비상 모드 (ATH DCA)
  • 📉 MA20 레짐 (참고): 🟡 MA 아래 — 비상 모드 중 MA 필터 OFF
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$62.10**
  ▶ 🟡 현금 유지 (MA20 아래 — 매수 금지, 재돌파 대기)
  • 📡 다음 비상 트리거: 2차(-50%)까지 -24.3%p ($43.51)

  🔹 SOXL (Close: $114.72 | 07-31 | LONG_YEAR / D+4)
  • 📈 전고점: $300.77 (2026-06-22) 기준 하락률 -61.86% / 회복 필요 162.18%
  • Mode: 🚨 비상 모드 (ATH DCA)
  • 📉 MA250 레짐 (참고): 🟢 MA 위 — 비상 모드 중 MA 필터 OFF
  • Signals: Buy[True] / Sell[False] | LOC mechanical strategy active
  • 🎯 [Action] LOC Buy: **$104.65**
  ▶ 🟢 보유 유지 (MA250 위 — LOC 분할매수 조건 확인)
  • 📡 다음 비상 트리거: 2차(-70%)까지 -8.1%p ($90.23)

  ────────────────────────────────────────
  📉 ATH Drawdown DCA Monitor
  📊 TQQQ ATH 2차 DCA
     • ATH: $87.02 | 실행: 1/3차 ✅
     • 다음(2차): 추가 +24.3%p 하락 시 ($43.51)
  📊 SOXL ATH 2차 DCA
     • ATH: $300.77 | 실행: 1/3차 ✅
     • 다음(2차): 추가 +8.1%p 하락 시 ($90.23)
"""

# =============================================================================
# 7. 파일 구성도
# =============================================================================
"""
📁 strategy_engine/
│
├── 📄 DCA_MA_strategy.py              ★ 완결판 (실전 엔진 + 백테스트 + 실시간 신호)
├── 📄 DCA_MA_strategy_flowchart.py    ★ 본 문서
├── 📄 setup_cronjob_org.py              cron-job.org 실시간 알림 설정 자동화
├── 📄 MarketStageSystem.py              시장 단계 트래커
├── 📄 bear_market_signals.py            약세장 신호 분석
│
├── 📄 portfolio_config.json             ★ 포트폴리오 설정 (핵심 설정 파일)
├── 📄 (MarketStage_config.json — 제거됨, portfolio_config.json으로 통합)
├── 📄 market_state.json                 시장 상태 저장 (자동 생성)
├── 📄 signal_report.json                신호 리포트 (자동 생성)
├── 📄 sigma_history.csv                 Sigma 변경 이력 (런타임 자동 생성 — 추적 제외)
│
├── 📄 README.md                         시스템 문서
├── 📄 DUAL_MODE_SUMMARY.md              듀얼 모드 구조 요약 문서
├── 📄 TRIGGER_OPTIMIZATION_SUMMARY.md      ATH_DCA 트리거 최적화 분석
├── 📄 REALTIME_ALERT_SETUP.md           실시간 알림 설정 가이드
├── 📄 requirements.txt                  Python 의존성 목록
├── 📄 cape_cache.json                   CAPE 캐시 (bear_market_signals.py)
│
└── 📁 .github/workflows/
    ├── dca_ma_strategy.yml           ★ 통합 — 브리핑 + MA 신호 (23:30 UTC) + 실시간 dispatch
    ├── bear_market_signals.yml         신호 분석 자동 실행 (23:00 UTC)
    └── tracker.yml                     시장 단계 추적 자동 실행 (23:14 UTC)
"""

if __name__ == "__main__":
    print("=" * 78)
    print("  📖 DCA MA Strategy — System Flowchart")
    print("  Open this file in any text editor to view the flowchart.")
    print("=" * 78)
    print()
    print("  This is a DOCUMENTATION file (not executable).")
    print("  It contains the complete system architecture")
    print("  in ASCII diagrams and structured comments.")
    print()
    print("  📍 File: DCA_MA_strategy_flowchart.py")
    print("  📅 Last updated: 2026-08-03")
    print()
    print("  💡 Tip: Use 'cat' to view, or open in VS Code")
    print("  with collapsed sections for easy navigation.")
    print("=" * 78)
