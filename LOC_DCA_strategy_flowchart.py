#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════
  DCA LOC Strategy — 전체 시스템 플로우차트
══════════════════════════════════════════════════════════════════════
  파일: LOC_DCA_strategy.py
  최종 업데이트: 2026-08-17
  전략: 순수 LOC 지정가 5분할 DCA (단일 논리 — 2026-08-17 20→5분할 전환)
    - MA 레짐 필터 / RSI+볼륨 / ATH_DCA 비상 모드 / STAGE5 / 회복 재진입
      / 실시간 모니터(--ath-monitor) 전부 삭제 (2026-08-16)
══════════════════════════════════════════════════════════════════════

[목차]
  1. 시스템 개요
  2. 전체 실행 흐름도
  3. 함수 호출 관계도
  4. 데이터 파일 의존성
  5. LOC 5분할 규칙 (단일 논리)
  6. GitHub Actions 워크플로우
  7. 파일 구성도
══════════════════════════════════════════════════════════════════════
"""

# =============================================================================
# 1. 시스템 개요
# =============================================================================
"""
📌 DCA LOC Strategy는 매일 정해진 시간에 GitHub Actions에서 실행되어,
   portfolio_config.json에 설정된 포지션(TQQQ)의 LOC 매수 목표가를 계산하고,
   **당일 저가 ≤ LOC → 5분할 중 1차 체결**을 감지해 디스코드로 종합 브리핑을
   전송합니다. 로직을 섞지 않고 **하나의 논리**(LOC 지정가 5분할 적립)만 사용합니다.

🔗 연동 시스템:
  - bear_market_signals.py → signal_report.json (시장 리스크 점수 — 참고용)
  - MarketStageSystem.py → market_state.json (바닥 단계 — 매수 트리거로 미사용)
  - yfinance → 종가/저가/변동성 데이터 (15분 지연)
"""

# =============================================================================
# 2. 전체 실행 흐름도
# =============================================================================
"""
┌──────────────────────────────────────────────────────────────────────────────┐
│                   🚀 LOC_DCA_strategy.py (메인 진입점)                       │
│                    (GitHub Actions: 매일 23:30 UTC 통합 브리핑)              │
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
│  reset_matured_rotation_positions(cfg, today)                                │
│  └─ 각 포지션의 INVEST_TYPE 확인                                             │
│     ├─ "ROTATION_3M" → check_rotation_exit_signal() → 만료일 도달?          │
│     │  ├─ YES → Sigma 재계산 + START_DATE 초기화                             │
│     │  └─ NO  → 다음 포지션                                                 │
│     └─ 기타 (LONG_YEAR / END_DEC) → 건너뜀                                  │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [3] Sigma 신선도 체크 (90일 기준)                                           │
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
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [4] LOC 매수가 계산 ⭐ 단일 논리 ⭐                                            │
│  calculate_loc_price(ticker, prev_close, cfg)                                │
│  └─ loc = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER)                             │
│     └─ 사용자가 정규장에서 이 가격으로 LOC 지정가 주문                      │
│        (체결 추적은 봇이 안 함 — 증권앱 + 엑셀이 단일 소스, 2026-08-16)     │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [5] 디스코드 브리핑 생성 ← _build_briefing_lines(now_ny, cfg)               │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  제목: "🌙 U.S. Market LOC Portfolio Briefing (YYYY-MM-DD HH:MM EST)" │ │
│  │  ├─ 📊 Market Risk Score: X / 14 (from signal_report.json)             │ │
│  │  ├─ 🎯 [국면 판정] … → LOC_DCA/스윙 유리 (2026-08-17 추가)            │ │
│  │  │     선행(고점 경고) a/6 · 확인(하락 진행) b/8                     │ │
│  │  └─ ─── 40 ───                                                        │ │
│  │                                                                        │ │
│  │  ▼ 각 포지션 반복 (TQQQ)                                             │ │
│  │  ┌──────────────────────────────────────────────────────────────────┐ │ │
│  │  │  [5-a] 전일 종가 조회                                          │ │ │
│  │  │  get_prev_close(ticker) ← yfinance 1mo (3회 재시도)            │ │ │
│  │  │  ├─ 장 마감+15분 후 (16:15 NY) → 오늘 최종 종가                │ │ │
│  │  │  ├─ 장 중 → 전일 종가                                          │ │ │
│  │  │  └─ 실패 시 → yfinance info API fallback                       │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [5-b] 전고점 대비 하락률 (참고 표시)                          │ │ │
│  │  │  format_drawdown_line(ticker, prev_close, lookback_days)       │ │ │
│  │  │  ├─ get_period_ath() → N일 최고가 (Close 기준)                 │ │ │
│  │  │  └─ calculate_drawdown_and_recovery()                          │ │ │
│  │  │     → "전고점 $XX 기준 하락률 -XX% / 회복필요 XX%"             │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [5-c] LOC 매수가 (_loc_action_line)                           │ │ │
│  │  │  calculate_loc_price(ticker, prev_close, cfg)                  │ │ │
│  │  │  └─ "🎯 [Action] LOC Buy: $XX" (분할 회차/예산 표시 없음 —     │ │ │
│  │  │     사용자 엑셀이 단일 소스, 2026-08-16)                       │ │ │
│  │  ├──────────────────────────────────────────────────────────────────┤ │ │
│  │  │  [5-d] Rotation 만료 체크                                      │ │ │
│  │  │  check_rotation_exit_signal(pos_cfg, today)                    │ │ │
│  │  │  └─ 만료 시 "🔴 D+XX Rotation Maturity" 경고                   │ │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [6] 설정 저장 + 디스코드 전송                                                │
│  save_portfolio(cfg) → portfolio_config.json                                 │
│  _send_discord(webhook, user_id, title, content)                             │
│  ├─ Embed 메시지 생성 (제목 + 설명 + 타임스탬프 + 색상)                       │
│  ├─ @유저 멘션 포함                                                          │
│  ├─ 내용 4096자 제한 / 제목 256자 제한                                       │
│  └─ Discord Webhook API 호출                                                 │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  [7] 월간 핑 (매월 1일만)                                                    │
│  send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)                     │
│  ├─ now_ny.day == 1? AND LAST_MONTHLY_PING != this month?                   │
│  ├─ YES → "📅 Monthly Operation Ping" 전송                                  │
│  └─ LAST_MONTHLY_PING 업데이트 → save_portfolio()                           │
└──────────────────────────────────────────────────────────────────────────────┘

        ┌───────────┐
        │   ✅ 완료   │
        └───────────┘

📌 CLI 서브모드:
  - --backtest       : 순수 LOC 5분할 백테스트 (MA 필터 없음 — 단일 전략)
  - --signal         : 실시간 신호 (종가/LOC/오늘 LOC 도달 여부) — 콘솔용
  - --signal --discord [--all] : Discord 발송 (전 종목 단일 메시지)
"""

# =============================================================================
# 3. 함수 호출 관계도
# =============================================================================
"""
[메인 실행 흐름]
LOC_DCA_strategy.py (직접 실행)
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
│
├── _build_briefing_lines()                    ← 브리핑 생성
│   ├── get_market_score()                     ← signal_report.json
│   │
│   │  ── [각 포지션 반복] ──
│   │
│   ├── get_prev_close()                       ← yfinance API
│   │   └── _most_recent_trading_day()
│   ├── format_position_meta()
│   │   └── business_days_elapsed()
│   ├── format_drawdown_line()                 전고점 하락률 (참고)
│   │   ├── get_period_ath()                   ← yfinance API
│   │   └── calculate_drawdown_and_recovery()
│   ├── _loc_action_line()
│   │   └── calculate_loc_price()
│   │       ├── _calculate_loc_from_sigma()
│   │       └── get_realtime_sigma()
│   │           └── _fetch_closes_for_lookback()
│   └── check_rotation_exit_signal()
│
├── _send_discord()                            → Discord Webhook
│
└── send_monthly_ping_if_due()
    └── save_portfolio()

[CLI 서브모드]
--backtest:
  main() → load_data(ticker, end=TEST_END)     ← yfinance API
        → backtest(df, entry_multiplier, buy_amount, max_buys, fee_rate)
            ├── _calculate_volatility_from_closes()  (룩백 252일 EWMA)
            └── _calculate_loc_from_sigma()           (매수 판정)

--signal:
  main() → _resolve_signal(ticker, opts)
        → current_signal(ticker, entry_multiplier)
            ├── load_data(ticker)              ← yfinance API
            └── _calculate_loc_from_sigma()
"""

# =============================================================================
# 4. 데이터 파일 의존성
# =============================================================================
"""
📂 프로젝트 파일 구조 및 데이터 흐름:

 portfolio_config.json  (읽기/쓰기 — 단일 설정 파일)
 ├── DISCORD_WEBHOOK, DISCORD_USER_ID
 ├── POSITIONS → TQQQ
 │   ├── LOOKBACK_DAYS, ENTRY_MULTIPLIER
 │   ├── VOL_METHOD (EWMA / STD), EWMA_LAMBDA
 │   ├── DAILY_SIGMA (← refresh_sigma_if_stale)
 │   ├── LAST_SIGMA_UPDATE, LAST_SIGMA_METHOD, LAST_EWMA_LAMBDA
 │   ├── ALLOCATION_PCT, INVEST_TYPE, START_DATE
 │   ├── LOC_DCA ⭐ (SPLITS=5, BUY_AMOUNT=10000 — 백테스트 기본값)
 │   └── LAST_MONTHLY_PING
 ⚠️ 체결 추적/분할 예산은 봇이 저장하지 않음 (사용자 엑셀이 단일 소스 — 2026-08-16)

 portfolio_config.json  (읽기 전용, MarketStageSystem.py가 공유 — 키 목록만 사용)

 market_state.json  (읽기 전용, MarketStageSystem.py가 작성 — DCA 트리거로 미사용)
 signal_report.json  (읽기 전용, bear_market_signals.py가 작성 — 리스크 점수)
 sigma_history.csv  (쓰기 전용, Sigma 업데이트 로그)

 yfinance API (외부 데이터)
 ├── 1mo 데이터 → get_prev_close() (최종 종가)
 ├── 252d+ 데이터 → get_period_ath() / get_realtime_sigma() / recompute_sigma_for_ticker()
 └── 백테스트/신호 → load_data() (Close + Low)

 Discord Webhook (외부 출력)
 └── Embed 메시지 → 디스코드 채널
"""

# =============================================================================
# 5. LOC 5분할 규칙 (단일 논리)
# =============================================================================
"""
╔══════════════════════════════════════════════════════════════════════╗
║                    순수 LOC 지정가 5분할 DCA                       ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   매수가 기준:                                                        ║
║     σ = EWMA(λ=0.94, 룩백 252일) 일일 변동성                         ║
║     LOC 매수가 = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER)             ║
║                                                                      ║
║   체결 규칙:                                                          ║
║     정규장에서 LOC 가격으로 지정가 주문 ($10,000 × 최대 5차)        ║
║     체결 여부는 증권앱 확인 + 엑셀 컬러 표시 (봇 미추적 — 2026-08-16)║
║     분할 예산/회차는 엑셀이 단일 소스                                 ║
║                                                                      ║
║   매도 규칙: 없음 (순수 적립 전용)                                    ║
║                                                                      ║
║   삭제된 로직 (2026-08-16):                                           ║
║     MA 레짐 필터 · RSI+볼륨 · ATH_DCA 비상 모드 · STAGE5 ·           ║
║     회복 재진입(RECOVERY_REENTRY) · 실시간 모니터(--ath-monitor)     ║
║     → 로직을 섞는 방식은 효율이 낮고 오버피팅 문제 → 단일 논리 채택   ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# =============================================================================
# 6. GitHub Actions 워크플로우
# =============================================================================
"""
📄 .github/workflows/loc_dca_strategy.yml (DCA LOC Strategy)

name: DCA LOC Strategy
on:
  schedule:
    - cron: '30 23 * * 1-5'   # 월~금 23:30 UTC = 19:30 ET (장 마감 후 통합 브리핑)
  workflow_dispatch:           # 수동 실행 지원

jobs:
  run-dca-loc:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
      DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}
    steps:
      - uses: actions/checkout@v4
      - name: Sync with latest remote
        run: git pull --rebase
      - uses: actions/setup-python@v5
        with: {python-version: '3.14'}
      - run: pip install -r requirements.txt
      - name: TypeScript strict typecheck
        run: |
          npm ci --silent
          npm run typecheck
      - name: Run Daily Briefing (통합 메시지 — LOC 5분할 신호 포함)
        run: |
          set -o pipefail
          python LOC_DCA_strategy.py 2>&1 | tee sigma_log.txt
          python LOC_DCA_strategy.py --signal --all 2>&1 | tee -a sigma_log.txt
      - name: Sync and Notify
        if: always()
        run: |
          git config --global user.name "DCA Bot"
          git config --global user.email "bot@example.com"
          [ -f "portfolio_config.json" ] && git add portfolio_config.json
          git commit -m "update: dca-state $(date +'%Y-%m-%d')" || echo "No changes to commit"
          git pull --rebase -X theirs || true
          git push

📄 .github/workflows/bear_market_signals.yml (Bear Market Signals Engine)
  - cron '0 23 * * 1-5' (23:00 UTC) — signal_report.json 갱신

📄 .github/workflows/tracker.yml (Market Stage Tracker)
  - cron '14 23 * * 1-5' (23:14 UTC) — market_state.json 갱신 (DCA 미사용)

📄 .github/workflows/swing_alerter.yml (스윙 알리미)
  - cron '0 0 * * 1-5' (00:00 UTC) + repository_dispatch(swing-monitor)
  - setup_cronjob_org.py는 이제 스윙 전용 (ATH DCA 실시간 모니터 삭제 — 2026-08-16)


📌 워크플로우 실행 순서 (23:00~23:30 UTC, 월~금):
  1. 23:00 UTC — bear_market_signals.yml   (시장 리스크 평가)
  2. 23:14 UTC — tracker.yml               (시장 단계 추적 — DCA와 독립)
  3. 23:30 UTC — loc_dca_strategy.yml       (통합 브리핑 1건 — LOC 5분할 신호 포함)


📌 실행 로그 예시 (GitHub Actions Console):

  🔍 Starting price lookup for TQQQ...
  ✅ TQQQ yfinance success: $76.79 (08-14)
  📊 TQQQ LOC 대기: 마지막 세션(08-14) 저가 $75.94 > LOC $73.50 ...
  ✅ Discord briefing sent successfully.


📌 디스코드 출력 예시 (통합 브리핑 1건):

  🌙 U.S. Market LOC Portfolio Briefing (2026-08-14 19:30 EDT)
  📊 Market Risk Score: 6 / 14
  🎯 [국면 판정] 고점 + 강세장 지속 → LOC_DCA 매수 조건 유리
  • 선행(고점 경고) 6/6 · 확인(하락 진행) 0/8
  ────────────────────────────────────────

  🔹 TQQQ (Close: $76.79 | 08-14 | LONG_YEAR / D+15)
  • 📈 전고점: $87.02 (2026-06-02) 기준 하락률 -11.76% / 회복 필요 13.32%
  • 🎯 [Action] LOC Buy: **$73.16**
"""

# =============================================================================
# 7. 파일 구성도
# =============================================================================
"""
📁 strategy_engine/
│
├── 📄 LOC_DCA_strategy.py              ★ 완결판 (실전 엔진 — 순수 LOC 5분할 + 백테스트 + 신호)
├── 📄 LOC_DCA_strategy_flowchart.py    ★ 본 문서
├── 📄 setup_cronjob_org.py              cron-job.org 실시간 알림 설정 자동화 (스윙 전용)
├── 📄 swing_alerter.py                  스윙 투자 알리미 (별도 전략 — LOC와 무관)
├── 📄 MarketStageSystem.py              시장 단계 트래커 (독립 — DCA 미사용)
├── 📄 bear_market_signals.py            약세장 신호 분석
│
├── 📄 portfolio_config.json             ★ 포트폴리오 설정 (핵심 설정 파일)
├── 📄 STRATEGY_RULES.md                 전략 규칙 (순수 LOC 5분할)
├── 📄 market_state.json                 시장 상태 저장 (자동 생성)
├── 📄 signal_report.json                신호 리포트 (자동 생성)
├── 📄 sigma_history.csv                 Sigma 변경 이력 (런타임 자동 생성 — 추적 제외)
│
├── 📄 README.md                         시스템 문서
├── 📄 requirements.txt                  Python 의존성 목록
├── 📄 cape_cache.json                   CAPE 캐시 (bear_market_signals.py)
│
└── 📁 .github/workflows/
    ├── loc_dca_strategy.yml           ★ 브리핑 + LOC 5분할 신호 (23:30 UTC)
    ├── bear_market_signals.yml         신호 분석 자동 실행 (23:00 UTC)
    ├── tracker.yml                     시장 단계 추적 자동 실행 (23:14 UTC)
    └── swing_alerter.yml               스윙 알리미 (00:00 UTC + 실시간 dispatch)

(2026-08-16 삭제: DUAL_MODE_SUMMARY.md · TRIGGER_OPTIMIZATION_SUMMARY.md ·
 REALTIME_ALERT_SETUP.md — 듀얼 모드/ATH_DCA 전략 삭제로 함께 제거)
"""

if __name__ == "__main__":
    print("=" * 78)
    print("  📖 DCA LOC Strategy — System Flowchart")
    print("  Open this file in any text editor to view the flowchart.")
    print("=" * 78)
    print()
    print("  This is a DOCUMENTATION file (not executable).")
    print("  It contains the complete system architecture")
    print("  in ASCII diagrams and structured comments.")
    print()
    print("  📍 File: LOC_DCA_strategy_flowchart.py")
    print("  📅 Last updated: 2026-08-16")
    print()
    print("  💡 Tip: Use 'cat' to view, or open in VS Code")
    print("  with collapsed sections for easy navigation.")
    print("=" * 78)
