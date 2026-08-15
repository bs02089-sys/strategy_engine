# 📋 듀얼 모드 시스템 — 전체 구조 요약

> ⚠️ **2026-08-02 통합**: 본 문서의 과거 변경 기록은 당시 `sigma_DCA_manager.py` 기준입니다.
> 현재는 `DCA_MA_strategy.py`(완결판 — 실전 엔진 + 백테스트 + 신호)로 통합되었습니다.

> 🔗 **관련 문서**: [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) — ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거

---

## 1️⃣ 두 가지 모드

| 모드 | 이모지 | 설명 | 매수 방식 |
|:---:|:-----:|:----|:---------|
| **LOC** | 📗 | **일반 모드** — 평상시 기본 전략 | Sigma 기반 LOC 분할 매수 |
| **ATH_DCA** | 🚨 | **비상 모드** — 급락 시 자동 전환 | 3차 ATH 하락분할 DCA 매수 (3차 = Stage 5 바닥) |

---

## 2️⃣ 모드 전환 조건

```
LOC ─────────────────────────────────▶ ATH_DCA
  (자동) ATH DD >= TRIGGER_1 도달 시
  TQQQ: -35%

ATH_DCA ──────────────────────────────▶ LOC
  (자동) 비상 모드 종료 — 아래 조건이 모두 충족 시 (RECOVERY_REENTRY.ENABLED=true)
    ① 잔여 분할 1개 이상 (2차/3차 예비금 보존)
    ② ATH_DCA_ENTERED_ON(크래시 진입일)부터 MIN_DAYS(30) 영업일 경과
    ③ DD ≤ DD_RATIO(0.5) × TRIGGER_1   (TQQQ 17.5%)
    ④ MA20 > MA60 (불리시 정렬, MA_CONFIRM=true)
  (수동) 사용자가 STRATEGY_MODE = "LOC"로 변경해도 동작

  ⚠️ 비상 모드 종료 시 ATH_DCA_USED_SPLITS는 보존 → 재급락 시 2차/3차 이어서 발동
  ⚠️ TQQQ는 진입 2026-03-27 기준 30영업일 경과 완료 → 대기 표시 없음 (종료 검사 활성)
```

### 🧪 비상 모드 종료 실효성 검증 (백테스트, 2026-08-02)

백테스트로 크래시→회복 사이클 구간을 검증했습니다.

| 구간 (--end-date) | 윈도우 | 결과 |
|:---|:---|:---|
| **2020-08-31** (COVID 크래시 포함) | 2020-02-14 → 2020-08-28 | 🟢 **TQQQ: +136.17% vs 현행 +131.50% (+4.67%p)** — 종료 1회 (2020-06-23, D+81), LOC 예산 $3,333 → 예산 기반 LOC 2회 추가 매수 |
| 2020-12-31 | 2020-06-16 → 2020-12-30 | 동일 — 종료 1회 발생(2020-11-05)했으나 종료 시점 잔고 $0 (드라이 파우더 소진) |
| 2025-03-31 | 2024-09-16 → 2025-03-28 | 동일 — 크래시 발동 후 회복 전환 미충족 |
| 2023-06-30 | 2022-12-13 → 2023-06-29 | 동일 — 윈도우가 2022 바닥 이후라 크래시 미발동 |

> 💡 **결론**: 비상 모드 종료는 **크래시 후 잔여 현금(예비금)이 남아있을 때** 실제 수익 개선을
> 창출합니다 (2020 COVID: TQQQ +4.67%p, Sharpe 2.17 vs 2.14, MDD 동일 -39.62%).
> 반면 크래시 모드 진입 전 LOC가 예비금을 다 소진하면(드라이 파우더 소진) 종료 효과가
> 없으므로, **크래시 진입 시점의 예비금 보존**이 실효성의 핵심입니다.

---

## 3️⃣ 종목별 설정

| 항목 | TQQQ |
|:----|:----:|
| **기본 모드** | LOC |
| **현재 STRATEGY_MODE** ⚠️스냅샷 | **ATH_DCA** (진입 2026-03-27) |

> ⚠️ 위 "현재 STRATEGY_MODE" 행은 **2026-07-31 기준 스냅샷**입니다. 실제 모드는
> `portfolio_config.json`의 `STRATEGY_MODE`가 소스이며, 비상 모드 종료로 LOC 복귀 시
> 자동 갱신됩니다.
| **LOC 매수** | Sigma × 1.1 |
| **ATH_DCA 1차** | **-35%** |
| **ATH_DCA 2차** | -50% |
| **ATH_DCA 3차** | **Stage 5 바닥 감지** |
| **MA 레짐 필터** | **MA20** (하향 돌파 → 전량 청산 / 상향 돌파 → 재매수 50%) |

---

### MA 레짐 필터 (Moving-Average Regime Filter — 백테스트 검증 반영)

기존 듀얼 모드에 **종가 × 이동평균(MA) 크로스 레짐 필터**를 얹어 MDD를 낮추는 설계입니다.
`DCA_MA_strategy.py` 백테스트 검증(TQQQ MA20 +2,138.5% / -41.2%)을
반영해 실전에 통합되었습니다.

| 구분 | 동작 |
|:----|:----|
| **MA 하향 돌파** (종가 < MA) | 📉 **전량 청산 + 매수 금지** — LOC/RSI 매수 신호 생략, 현금 대기 |
| **MA 상향 돌파** (종가 > MA) | 💰 **재매수**(`REENTRY=lump`, 50%) / 🔄 **DCA 재개**(`REENTRY=dca_reset`) |
| **ATH_DCA 비상 모드** | 🚫 **필터 OFF** — 분할 매수 진행 중에는 개입하지 않음 (레짐 상태만 "참고" 표시) |
| **비상 모드 종료 → LOC 복귀** | MA 필터 **재활성** |

- 크로스 신호는 레짐 전환 시 **1회만** 발송 — `MA_FILTER_STATE`(`{regime, since}`) 영속화로 중복 알림 없음
- 설정 변경(MA 일수/재진입 방식) 감지 시 `MA_FILTER_CONFIG_FINGERPRINT`로 상태 리셋
- 설정: `MA_FILTER` 블록 (`ENABLED` / `MA_DAYS` / `REENTRY` / `REENTRY_PCT`)

---

## 4️⃣ 실행 흐름 (매일 실행 시)

```
┌─────────────────────────────────────────────┐
│  ① 초기화 (설정 로드)                         │
│     └─ portfolio_config.json (단일 파일)      │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ② Sigma 갱신 (90일 경과 시)                 │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ③ 듀얼 모드 전환 평가                       │
│  ├─ DD ≥ TRIGGER_1? → ATH_DCA로 자동 전환    │
│  └─ ATH_DCA 상태?                            │
│     ├─ 비상 모드 종료 신호? → LOC로 자동 복귀  │
│     │   (잔여분할+30일+DD회복+MA20>MA60)      │
│     └─ 아니면 유지 (2차/3차 대기 + ⏳ 대기표시)│
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ③-2 MA 레짐 필터 평가 (LOC 모드만)         │
│  ├─ MA 하향 돌파 → 전량 청산 + 매수 금지      │
│  ├─ MA 상향 돌파 → 전액 재매수/DCA 재개 신호  │
│  └─ ATH_DCA 모드 → OFF (상태만 추적)         │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ④ Discord 브리핑 생성 (모드별)              │
│  ├─ 📗 LOC 모드: LOC 목표가 + RSI 신호 + MA 레짐 │
│  └─ 🚨 ATH_DCA 모드: LOC 중단 + ATH DCA 집중 │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ⑤ ATH DCA 신호 확인                         │
│  ├─ 1~2차: ATH DD % 기반 (PCT 타입)          │
│  ├─ 3차: Stage 5 바닥 감지 (STAGE5 타입)     │
│  ├─ 사용된 분할 자동 기록                     │
│  └─ 사이클 완료 시 ATH 기록 + 사이클 재시작 대기      │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│  ⑥ 설정 저장 + Discord 전송                  │
└─────────────────────────────────────────────┘
```

---

## 5️⃣ 사용자가 해야 할 일

| 시점 | 할 일 |
|:----|:------|
| **평소** | 아무것도 안 해도 됨 ✅ |
| **급락장 진입** | 아무것도 안 해도 됨 (자동 전환) ✅ |
| **비상 모드 중** | 아무것도 안 해도 됨 (Stage 5까지 자동 감지) ✅ |
| **시장 회복 후** | 아무것도 안 해도 됨 — **비상 모드 종료가 자동으로 LOC 복귀** ✅ |
| **추가 자금 생김** | LOC 가격 확인 후 직접 매수 ✋ |
| **비상 모드 종료 파라미터 변경** | `RECOVERY_REENTRY` 블록 (ENABLED/DD_RATIO/MIN_DAYS/MA_CONFIRM) |
| **토큰 재발급(실시간 알림 깨질 때)** | `export GITHUB_PAT=...` + `python3 setup_cronjob_org.py --update-pat` |

---

## 6️⃣ Stage 5 통합: 이전 vs 이후

| 항목 | 이전 | 변경 후 |
|:-----|:----|:--------|
| **설정 파일** | `portfolio_config.json` + `MarketStage_config.json` | **`portfolio_config.json` 단일 파일** |
| **Stage 5 역할** | 독립 All-In 메시지 (`_format_all_in_line()`) | **ATH DCA 3차 트리거** (`check_ath_dca_signals()` 통합) |
| **ALL_IN_PERCENT** | TQQQ=50% (잔금 비중) | **제거됨** (3차는 단순 1/3 분할) |
| **Discord 설정** | 각 파일에서 중복 로드 | **`resolve_discord_config()`** 공유 함수 |
| **시장 단계 설정** | `MarketStage_config.json` → TICKERS | **`portfolio_config.json` → POSITIONS 키** |

---

## 7️⃣ 변경된 파일 목록

### 추가된 파일
| 파일 | 설명 |
|:-----|:------|
| **`setup_cronjob_org.py`** | cron-job.org 실시간 알림 설정 자동화 (생성/--list/--test-dispatch/--update-pat/--update-schedule) |
| **`REALTIME_ALERT_SETUP.md`** | 실시간 ATH DCA 알림 설정 가이드 |
| **`TRIGGER_OPTIMIZATION_SUMMARY.md`** | ATH_DCA 트리거 최적화 분석 — 바닥 분포 · 후보값 스윕 · 의사결정 근거 (2026-08-01) |

### 수정된 파일
| 파일 | 변경 내용 |
|:-----|:---------|
| **`DCA_MA_strategy.py`** | `_is_stage5_trigger()` 추가, `check_ath_dca_signals()`에 STAGE5 타입 + 알림 전용(alerts_only) 모드 지원, `resolve_discord_config()` 추가, 비상 모드 종료(`_check_recovery_reentry`) + 대기/임박 모니터, `--ath-monitor` 진입점, 브리핑 Mode 라벨 한국어화. Finnhub 실시간 가격 오버라이드(realtime_prices)는 2026-08 키 유출 방지 차원에서 제거 |
| **`MarketStageSystem.py`** | `portfolio_config.json` 읽도록 변경, Discord 설정 `resolve_discord_config()` 공유, `_load_config()` 제거 |
| **`portfolio_config.json`** | `TRIGGER_3: "STAGE5"`, `STRATEGY_MODE`, `RECOVERY_REENTRY`, `ATH_DCA_ENTERED_ON` + dedup 상태키(`WAIT_SENT`/`NUDGE_SENT`/`IMMINENT_SENT`) 추가 |
| **`.github/workflows/dca_ma_strategy.yml`** | `repository_dispatch(ath-dca-monitor)` 트리거 + `concurrency` 직렬화 + `--ath-monitor` 분기 + `git pull --rebase` |
| **`DCA_MA_strategy_flowchart.py`** | Stage 5 통합 반영, 비상 모드 종료/실시간 모니터 흐름 반영, 함수/파일 참조 최신화 |
| **`README.md`** | 단일 설정 파일 명시, 듀얼 모드/실시간 알림 설명 업데이트, 워크플로우 크론 표 최신화, 설정 예시·목차 앵커 정리, `--update-schedule` 플래그 반영 |
| **`setup_cronjob_org.py`** | `--update-schedule` 추가 (기존 잡의 폴링 간격만 PATCH 갱신, PAT 불필요) + `READONLY_JOB_FIELDS`/`_strip_readonly_fields()` 헬퍼 추출로 `--update-pat`과 DRY |
| **`REALTIME_ALERT_SETUP.md`** | `python` → `python3` 표기 통일 + `--update-schedule` 절차 추가 |
| **용어 통일 (전 파일)** | "회복 재진입" → **비상 모드 종료** / ATH 사이클 "재진입" → **사이클 재시작** / 브리핑 Mode 라벨 한국어화 — 사용자 표시 용어 전면 정리 |
| **`.github/workflows/bear_market_signals.yml`** | `bear_config.json` 참조 제거 (존재하지 않는 파일) |

### 삭제된 파일
| 파일 | 설명 |
|:-----|:------|
| **`MarketStage_config.json`** | `portfolio_config.json`으로 통합 완료 |

---

## 8️⃣ 데이터 파일 의존성

```
 portfolio_config.json  (읽기/쓰기 — 단일 설정 파일)
 ├── DISCORD_WEBHOOK, DISCORD_USER_ID     (← resolve_discord_config() 공유)
 └── POSITIONS → TQQQ
     ├── Sigma 관련: LOOKBACK_DAYS, VOL_METHOD, DAILY_SIGMA 등
     ├── STRATEGY_MODE                    (LOC / ATH_DCA — 자동 관리)
     ├── MA_FILTER: ENABLED, MA_DAYS, REENTRY, REENTRY_PCT   (TQQQ MA20/lump)
     ├── MA_FILTER_STATE                   (자동 관리 — {regime, since})
     ├── MA_FILTER_CONFIG_FINGERPRINT      (설정 변경 감지 — 상태 리셋)
     ├── ATH_DCA: ENABLED, SPLITS, TRIGGER_1~3, STRATEGY
     ├── ATH_DCA_USED_SPLITS              (자동 관리)
     ├── ATH_DCA_CYCLE_ATH                (사이클 완료 시 기록)
     ├── ATH_DCA_CONFIG_FINGERPRINT       (설정 변경 감지)
     ├── ATH_DCA_ENTERED_ON               (크래시 진입일, 비상 모드 유지 클럭 기준)
     ├── ATH_DCA_WAIT_SENT                (⏳ 대기 모니터 일일 전송 dedup)
     ├── ATH_DCA_NUDGE_SENT               (🔔 임박 넛지 전송 dedup)
     ├── ATH_DCA_IMMINENT_SENT            (📡 실시간 임박 갭 dedup — 1.0%p 좁힘 시 재알림)
     └── RECOVERY_REENTRY                 (비상 모드 종료: ENABLED/DD_RATIO/MIN_DAYS/MA_CONFIRM)

 market_state.json  (읽기 전용 — MarketStageSystem.py가 작성)
 └── TQQQ
     ├── bottom (0~5) → ATH DCA 3차 트리거
     └── top (0~5)

 signal_report.json  (읽기 전용 — bear_market_signals.py가 작성)
 └── total_score (0~14) → Market Risk Score

 sigma_history.csv  (쓰기 전용 — Sigma 업데이트 로그)
```

---

## 9️⃣ 연동 시스템

| 시스템 | 담당 | portfolio_config.json 사용 |
|:-------|:-----|:--------------------------:|
| **DCA_MA_strategy.py** | DCA 브리핑 + Discord 전송 | ✅ (직접 읽음) |
| **MarketStageSystem.py** | 시장 단계 감지 (bottom/top 0~5) | ✅ (공유 함수 사용) |
| **bear_market_signals.py** | 약세장 7대 신호 분석 | ❌ (독립 실행, signal_report.json만 출력) |
| **setup_cronjob_org.py** | cron-job.org 실시간 알림 설정 자동화 (생성/--list/--test-dispatch/--update-pat/--update-schedule) | ❌ (환경변수만 사용) |
| **cron-job.org (외부)** | 정확한 N분 알람 → `repository_dispatch` 발사 (현재 15분 간격, `POLL_MINUTES`로 조정) | ❌ (GitHub API 호출) |
| **yfinance (외부)** | 주가 데이터 (1시간봉/일봉, 15분 지연) | ✅ (실전·백테스트 공통) |
