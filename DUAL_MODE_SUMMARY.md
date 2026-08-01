# 📋 듀얼 모드 시스템 — 전체 구조 요약

> 🔗 **관련 문서**: [TRIGGER_OPTIMIZATION_SUMMARY.md](TRIGGER_OPTIMIZATION_SUMMARY.md) — ATH_DCA 트리거 최적화 분석 · 바닥 분포 · 후보값 스윕 · 의사결정 근거

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
  TQQQ: -35%  /  SOXL: -60%

ATH_DCA ──────────────────────────────▶ LOC
  (자동) 비상 모드 종료 — 아래 조건이 모두 충족 시 (RECOVERY_REENTRY.ENABLED=true)
    ① 잔여 분할 1개 이상 (2차/3차 예비금 보존)
    ② ATH_DCA_ENTERED_ON(크래시 진입일)부터 MIN_DAYS(30) 영업일 경과
    ③ DD ≤ DD_RATIO(0.5) × TRIGGER_1   (TQQQ 17.5%, SOXL 30%)
    ④ MA20 > MA60 (불리시 정렬, MA_CONFIRM=true)
  (수동) 사용자가 STRATEGY_MODE = "LOC"로 변경해도 동작

  ⚠️ 비상 모드 종료 시 ATH_DCA_USED_SPLITS는 보존 → 재급락 시 2차/3차 이어서 발동
  ⚠️ 대기 클록 표시: SOXL ⏳ D+3/30 영업일 (진입 2026-07-28) — 브리핑/실시간 알림에 표시
  ⚠️ TQQQ는 진입 2026-03-27 기준 30영업일 경과 완료 → 대기 표시 없음 (종료 검사 활성)
```

---

## 3️⃣ 종목별 설정

| 항목 | TQQQ | SOXL |
|:----|:----:|:----:|
| **기본 모드** | LOC | LOC |
| **현재 STRATEGY_MODE** ⚠️스냅샷 | **ATH_DCA** (진입 2026-03-27) | **ATH_DCA** (진입 2026-07-28) |

> ⚠️ 위 "현재 STRATEGY_MODE" 행은 **2026-07-31 기준 스냅샷**입니다. 실제 모드는
> `portfolio_config.json`의 `STRATEGY_MODE`가 소스이며, 비상 모드 종료로 LOC 복귀 시
> 자동 갱신됩니다.
| **LOC 매수** | Sigma × 1.1 | Sigma × 1.1 |
| **ATH_DCA 1차** | **-35%** | **-60%** |
| **ATH_DCA 2차** | -50% | -70% |
| **ATH_DCA 3차** | **Stage 5 바닥 감지** | **Stage 5 바닥 감지** |

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
│  ④ Discord 브리핑 생성 (모드별)              │
│  ├─ 📗 LOC 모드: LOC 목표가 + RSI 신호 표시   │
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
| **ALL_IN_PERCENT** | SOXL=30%, TQQQ=50% (잔금 비중) | **제거됨** (3차는 단순 1/3 분할) |
| **Discord 설정** | 각 파일에서 중복 로드 | **`resolve_discord_config()`** 공유 함수 |
| **시장 단계 설정** | `MarketStage_config.json` → TICKERS | **`portfolio_config.json` → POSITIONS 키** |

### 트리거 진화 (SOXL 예시)

```
이전:  1차(-60%) → 2차(-70%) → 3차(-80%) →   + 별도 All-In(30%)
                    ↓
변경:  1차(-60%) → 2차(-70%) → 3차(Stage 5 바닥 감지)  ← 통합 완료
```

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
| **`sigma_DCA_manager.py`** | `_is_stage5_trigger()` 추가, `check_ath_dca_signals()`에 STAGE5 타입 + 실시간 모드(realtime_prices/alerts_only) 지원, `resolve_discord_config()` 추가, 비상 모드 종료(`_check_recovery_reentry`) + 대기/임박 모니터, `--ath-monitor` 진입점, 브리핑 Mode 라벨 한국어화 |
| **`MarketStageSystem.py`** | `portfolio_config.json` 읽도록 변경, Discord 설정 `resolve_discord_config()` 공유, `_load_config()` 제거 |
| **`portfolio_config.json`** | `TRIGGER_3: "STAGE5"`, `STRATEGY_MODE`, `RECOVERY_REENTRY`, `ATH_DCA_ENTERED_ON` + dedup 상태키(`WAIT_SENT`/`NUDGE_SENT`/`IMMINENT_SENT`) 추가 |
| **`.github/workflows/sigma_dca_manager.yml`** | `repository_dispatch(ath-dca-monitor)` 트리거 + `concurrency` 직렬화 + `FINNHUB_API_KEY` 시크릿 + `--ath-monitor` 분기 + `git pull --rebase` |
| **`sigma_DCA_manager_flowchart.py`** | Stage 5 통합 반영, 비상 모드 종료/실시간 모니터 흐름 반영, 함수/파일 참조 최신화 |
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
 └── POSITIONS → TQQQ / SOXL
     ├── Sigma 관련: LOOKBACK_DAYS, VOL_METHOD, DAILY_SIGMA 등
     ├── STRATEGY_MODE                    (LOC / ATH_DCA — 자동 관리)
     ├── ATH_DCA: ENABLED, SPLITS, TRIGGER_1~3, STRATEGY
     ├── ATH_DCA_USED_SPLITS              (자동 관리)
     ├── ATH_DCA_CYCLE_ATH                (사이클 완료 시 기록)
     ├── ATH_DCA_CONFIG_FINGERPRINT       (설정 변경 감지)
     ├── ATH_DCA_ENTERED_ON               (크래시 진입일, 비상 모드 종료 클럭 기준)
     ├── ATH_DCA_WAIT_SENT                (⏳ 대기 모니터 일일 전송 dedup)
     ├── ATH_DCA_NUDGE_SENT               (🔔 임박 넛지 전송 dedup)
     ├── ATH_DCA_IMMINENT_SENT            (📡 실시간 임박 갭 dedup — 1.0%p 좁힘 시 재알림)
     └── RECOVERY_REENTRY                 (비상 모드 종료: ENABLED/DD_RATIO/MIN_DAYS/MA_CONFIRM)

 market_state.json  (읽기 전용 — MarketStageSystem.py가 작성)
 └── SOXL / TQQQ
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
| **sigma_DCA_manager.py** | DCA 브리핑 + Discord 전송 | ✅ (직접 읽음) |
| **MarketStageSystem.py** | 시장 단계 감지 (bottom/top 0~5) | ✅ (공유 함수 사용) |
| **bear_market_signals.py** | 약세장 7대 신호 분석 | ❌ (독립 실행, signal_report.json만 출력) |
| **setup_cronjob_org.py** | cron-job.org 실시간 알림 설정 자동화 (생성/--list/--test-dispatch/--update-pat/--update-schedule) | ❌ (환경변수만 사용) |
| **cron-job.org (외부)** | 정확한 N분 알람 → `repository_dispatch` 발사 (현재 15분 간격, `POLL_MINUTES`로 조정) | ❌ (GitHub API 호출) |
| **Finnhub (외부)** | `/quote` 실시간 가격 (FINNHUB_API_KEY) | ❌ (REST API) |
