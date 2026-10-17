# strategy_engine

미국 지수 레버리지(**SOXL · TQQQ**)를 대상으로 **2σ LOC 매수 도구**와 **시장 약세 조기경보**를 돌리는 저장소입니다.

> ⚠️ **2026-10-11 — LOC DCA 엔진 삭제**: `LOC_DCA_strategy.py`(실전 브리핑·백테스트·`--signal`) ·
> `LOC_DCA_strategy_flowchart.py` · `portfolio_config.json` · `STRATEGY_RULES.md` ·
> `test_price_basis.py` · `.github/workflows/loc_dca_strategy.yml` · `sigma_history.csv` 를 삭제했습니다.
> 현재 운영 도구는 아래 **2개**뿐입니다 (복원: [AGENTS.md](AGENTS.md) '제거된 기능' 참고).

---

## 📋 목차

- [개요](#-개요)
- [2σ LOC 도구](#-2σ-loc-도구)
- [LOC 주문 규칙 (LS증권)](#-loc-주문-규칙-ls증권)
- [약세 조기경보](#-약세-조기경보)
- [파일 구성](#-파일-구성)
- [설치 및 실행](#-설치-및-실행)
- [GitHub Actions 자동화](#-github-actions-자동화)
- [참고 사항](#-참고-사항)
- [AI 에이전트 영어 공부법](#-ai-에이전트-영어-공부법-english_study)

---

## 📌 개요

1. **2σ LOC 도구** (`soxl_2sigma_alert.py`) — 매일 미국 장 마감 후 SOXL·TQQQ의 **20일 롤링 2σ** 기준
   LOC 목표가와 발동 빈도를 **Discord** 로 알림
2. **약세 조기경보** (`bear_market_signals.py`) — 7개 지표를 점수화해 **시장 국면(강세/약세)** 을 Discord 로 알림

> 봇은 **주문을 넣지 않습니다** — 알림·가격만 제공하고 실제 체결은 사용자 수동 매매입니다.

---

## 🎯 2σ LOC 도구

### 지표 정의 (단일 소스: `soxl_tqqq_2sigma_count.py` 의 `_hits()`)

| 항목 | 값 | 설명 |
|------|-----|------|
| `rolling_window` | 20 거래일 | 일간 수익률의 표준편차 σ 를 이 창으로 계산 |
| `sigma_multiplier` | 2.0 | σ 에 곱하는 배수 |
| **LOC 목표가** | 당일 종가 × (1 − 2σ) | 다음 세션에 이 가격으로 LOC 주문 |
| **발동(체결) 조건** | 당일 종가 ≤ 전일 종가 × (1 − 2 × 전일까지의 σ) | 수익률로 쓰면 `당일 수익률 < −2σ` 와 동일 |

> 두 기준을 구분합니다 — **알림 판정**(당일 σ 포함, `soxl_2sigma_alert.py` 가 찍는 신호)과
> **체결 기준**(전일까지의 σ, 주문을 미리 걸어둘 때 실제 체결되는 날). 아래 발동 횟수는 **체결 기준**입니다.

🕒 다운로드 창의 **끝**은 `soxl_tqqq_2sigma_count.download_end()` 공용 함수 하나만 씁니다 = ET 정규장
마감(16:00) 기준 **마지막으로 마감된 세션 + 1일**(yfinance `end`는 배타적). `datetime.now()`를 그대로 쓰면
GHA 러너(UTC)는 그날 마감된 세션이 잘려 **한 세션 낡은 값**을 찍고(로컬 실행과 하루 어긋남), KST 로컬은
장중에 **진행 중인 봉**을 마감가로 집습니다 (2026-10-16 수정).

### 매일 알림 예시 (`soxl_2sigma_alert.py`)

```
[SOXL 장마감 및 LOC 목표가 안내]
기준 세션: 2026-10-09 마감
전일 종가: $139.76 (-1.94%)
2σ 기준값: 14.25% (20일 롤링 2.0배)
🎯 다음 세션 LOC 목표가: $119.84
📊 예상 발동: 연 10회(장기 중앙값) · 최근 1년 실제 8회 → 연간 예산 ÷ 10
상태: 일반 장세 (참고용)
```

- 마지막 `📊` 줄은 **매 실행 시 전체 이력(2010년~)으로 다시 계산**됩니다 — 1년 뒤에도 자동 갱신되며 별도 작업이 필요 없습니다.
- ⚠️ 세션 표기는 **주문 기준**입니다 — `전일 종가` 는 알림 직전에 마감된 세션(기준 세션)의 종가이고,
  그것이 **다음 세션**에 걸 LOC 주문의 기준가입니다(σ·수익률도 같은 기준 세션 값). 위 지표 정의 표의
  `당일 종가` 와 **같은 세션**을 가리킵니다 — 용어만 주문 시점 기준으로 바뀐 것입니다.
- 발동 빈도 계산은 `soxl_tqqq_2sigma_count.frequency_stats()` **공용 함수 하나만** 씁니다(알림·집계 도구 동일).
- 통계 계산이 실패해도 **2σ 알림은 그대로 발송**됩니다(해당 줄만 생략).
- ⚠️ **마지막 마감 세션 봉이 미완성이면(야후가 일봉 `Close` 만 NaN 으로 줄 때) 알림을 보내지 않습니다** —
  `dropna()` 로 결측 봉을 지우고 **직전 세션 값을 다시 알리면** 어제와 같은 메시지·하루 낡은 LOC 목표가가
  나갑니다 (2026-10-17 실측: 10-16 일봉에 Open/High/Low/Volume 만 있고 Close 가 NaN → 10-15 값이
  '기준 세션'으로 두 번 발송). 대신 사유·마지막 확정 세션을 담은 `2σ 알림 생략 — 판정 불가` 가 갑니다
  (휴장일은 봉 자체가 없어 해당 없음 · 마지막 세션 **봉이 아예 없는** 경우는 휴장일과 구분할 수 없습니다).
- 🔁 **결측·전송 실패 재시도** — 결측이거나 **전송이 실패하면** 곱바로 포기하지 않고 1시간 간격으로 다시
  시도합니다(최장 2시간). 결측은 그 종목을 **재판정**하고(야후 일봉이 늦게 채워지는 경우), **전송 실패는
  이미 만든 메시지를 그대로 재전송**합니다 — 재판정이 없어 같은 세션이 두 번 알림되지 않고, 재전송분에는
  `🔁 전송 재시도` 표시가 붙습니다(타임아웃 뒤 실제로는 도착했을 수 있어 중복 주문 방지). 재시도 사이의
  재조회는 **같은 프로세스에서 새 HTTP 요청**으로 나갑니다(yfinance 가격 히스토리는 응답 캐시를 쓰지 않음).
  끝까지 결측이면 **종목당 판정 불가 안내 1건**, 알림을 끝내 전달하지 못하면 **종료코드 1**(워크플로우 실패
  → GitHub 알림 메일 = 수신 경로 2차 신호)입니다.

  ```bash
  # 기본 = 재시도 없음(수동 실행이 2시간 매달리지 않도록). CI 는 재시도를 켠다
  python3 soxl_2sigma_alert.py                                # 1회
  python3 soxl_2sigma_alert.py --retry-wait 3600 --retry-attempts 2   # 결측·전송 실패 시 1시간 간격 2회
  python3 soxl_2sigma_alert.py --ping                         # 웹훅 생존 확인(수동)
  ```

- 📡 **매월 1일 웹훅 PING** — 디스코드를 **수신 전용**으로 쓰면 웹훅이 무효화돼도 알 수 없습니다
  (알림이 없는 게 정상인 날도 많아서). 그래서 매월 1일 09:00 KST 에 점검 메시지 1건을 보냅니다(시세
  조회 없음). 전송에 실패하면 종료코드 1 → 워크플로우 실패 → **GitHub 알림 메일이 2차 신호**입니다.

### 발동 빈도와 자금 분배 (`soxl_tqqq_2sigma_count.py`)

```bash
python3 soxl_tqqq_2sigma_count.py        # 기본: 설정 count_years(1년)
python3 soxl_tqqq_2sigma_count.py 5      # 5년 창으로 연환산 (민감도 확인)
```

- 전체 이력 1년 창의 **중앙값**을 나눗셈의 **분모**로 씁니다 → **SOXL 10 · TQQQ 11** (2026-10-11 실측)
- **연간 예산 ÷ 분모 = 회당 금액** (SOXL·TQQQ 예산은 각각 따로 나눔)

| 종목 | 1년 발동 중앙값 | (p25 / p75 / 최대) | 최근 1년 실제 | 평균 |
|------|------------------|--------------------|----------------|------|
| SOXL | **10회** | 8 / 12 / 16 | 8회 | 9.7회 |
| TQQQ | **11회** | 8 / 13 / 17 | 3회 | 10.4회 |

- 중앙값은 사실상 고정입니다 — **하루 0.3% 확률로만 ±1 변동**(1년 전 대비 변화 0, 10년 전 대비 +1 / −2).
- ⚠️ **최근 1년 실적만 보면 흔들립니다** (최근 1년 TQQQ 3회 = 분포 하위 5%) — 그래서 분모는 실적이 아니라 중앙값을 씁니다.
- 조용한 해엔 예산이 남고, 발동이 몰리는 해(최대 15~17회)엔 뒤쪽 회차를 건너뜁니다 (감수하는 트레이드오프).
- SOXL 발동의 약 60%는 TQQQ 와 **같은 날**입니다(같은 급락 이벤트) — 예산은 종목별로 따로 잡습니다.

### 설정 — `soxl_tqqq_config.json`

| 키 | 값 | 설명 |
|----|-----|------|
| `tickers` | `["SOXL", "TQQQ"]` | 대상 종목 |
| `lookback_days` | 60 | 알림이 yfinance 에서 **받아오는 기간**(달력일) — σ 창이 아님. 60일 ≈ 40거래일이라 20거래일 창에 2배 버퍼 (늘려도 판정은 동일) |
| `rolling_window` | 20 | σ 계산 창(거래일) — **지표 정의는 이것뿐** |
| `sigma_multiplier` | 2.0 | σ 배수 |
| `count_years` | 1 | 빈도 집계 창(년) — 인자로 덮어쓸 수 있음 |

---

## 🔻 LOC 주문 규칙 (LS증권)

1. **LOC(장마감 지정가) 유형으로 주문** — 마감가가 지정가 이하일 때 **종가로 체결**됩니다.
   ⚠️ 일반 지정가로 걸면 장중 터치로 체결돼 판정(종가 기준)과 어긋납니다.
2. 주문 가격은 알림의 `🎯 다음 세션 LOC 목표가` — 매일 다시 계산되므로 **다음 알림의 새 가격으로 재주문**합니다(고정 래더 아님).
3. 미체결 LOC 주문은 당일 소멸합니다. 급락이 이어지면 며칠 연속 체결될 수 있으니 **쓴 회차를 먼저 기록**하세요.
4. **체결 추적은 봇이 하지 않습니다** — 증권앱 확인 + 사용자 **엑셀 기록이 단일 소스**입니다.

---

## 🌊 약세 조기경보

`bear_market_signals.py` — 7개 지표를 **선행 그룹(고점 경고 0~6점)** 과 **확인 그룹(하락 진행 0~8점)** 으로
나눠 점수화하고, 시장 국면(강세/약세)을 Discord 로 알립니다.

- 선행 = 금리 커브 · Fed 정책 사이클 · 밸류에이션(CAPE) / 확인 = Breadth · 신용 스프레드 · 경제활동(USPHCI YoY) · Sahm · 모멘텀
- 데이터 = FRED CSV · yfinance · multpl.com(CAPE, 3단계 폴백 + 차단 판정)
- 산출물 = `signal_report.json` (리스크 점수 · `data_ok` · `degraded_signals`) · CAPE 캐시 = `cape_cache.json`(둘 다 커밋 대상)
- 결측은 점수 0('정상')과 구분해 **판정 불가**로 표시합니다 (`data_ok=False`)

> 📌 **국면 판정 규칙의 단일 출처는 `bear_market_signals.assess_regime()` 의 docstring** 입니다 — 여기나 다른 문서에 중복 서술하지 마세요.

---

## 📁 파일 구성

| 파일 | 설명 |
|------|------|
| **soxl_2sigma_alert.py** | 📌 2σ LOC 알림 — 장 마감 후 Discord 발송 (GHA 자동화) |
| **soxl_tqqq_2sigma_count.py** | 발동 빈도 집계 + 장기 중앙값(분모) 계산 — `frequency_stats()` 공용 |
| **soxl_tqqq_config.json** | 2σ 도구 설정 (알림 · 집계 공용) |
| **bear_market_signals.py** | 약세장 신호 분석 시스템 |
| **signal_report.json** / **cape_cache.json** | 약세 신호 산출물 · CAPE 폴백 캐시 (자동 생성·커밋) |
| **requirements.txt** | Python 의존성 (버전 고정) |
| **pyrightconfig.json** | 타입 검사 설정 (VS Code Pylance) |
| **AGENTS.md** | AI 에이전트 공통 지침 (아키텍처·제거 이력) |
| **english_study/** | 🧑‍🎓 AI 에이전트 영어 공부법 (자세한 매뉴얼은 `english_study/README.md`) |
| **scrapling-project/** | Scrapling 웹 스크래핑 예시 (자체 README·별도 venv) |

### 제거된 파일 (재도입 금지 — 상세는 [AGENTS.md](AGENTS.md))

| 파일 | 제거 이유 |
|------|-----------|
| ~~LOC_DCA_strategy.py · LOC_DCA_strategy_flowchart.py · portfolio_config.json · STRATEGY_RULES.md · test_price_basis.py · loc_dca_strategy.yml · sigma_history.csv~~ | LOC DCA 엔진 삭제 (2026-10-11) — 2σ LOC 도구로 대체 |
| ~~swing_alerter.py · swing_config.json · swing_dashboard.html · swing_split_backtest.py · NAMU_SWING_SETUP.md 등~~ | 스윙 투자 알리미 삭제 (2026-10-05 — cron-job.org `swing-monitor` 잡은 콘솔에서 수동 삭제) |
| ~~dollar_alerter.py · dollar_config.json · dollar_dashboard.html · dollar_split_backtest.py~~ | 달러 알리미 삭제 (2026-08-31 — `dollar-monitor` 잡 수동 삭제) |
| ~~MarketStageSystem.py · market_state.json · market_stage_tracker.yml~~ | 판정 결과가 트리거로 미사용 (2026-09-29) |
| ~~fvg_signal_bot.py · fvg_bot_backtest.py · fvg_bot_eval.py · fvg_signal.yml 등~~ | FVG 봇 삭제 (2026-08-08 — cron-job.org 잡 수동 삭제) |
| ~~openprice_trading.py · openprice_bot.yml~~ | 시가봉 박스 단타 삭제 (2026-08-06) |
| ~~DUAL_MODE_SUMMARY.md · TRIGGER_OPTIMIZATION_SUMMARY.md · REALTIME_ALERT_SETUP.md~~ | 듀얼 모드/ATH_DCA 삭제 (2026-08-16) |

---

## 🔧 설치 및 실행

```bash
# 의존성
pip install -r requirements.txt

# 2σ 알림 1회 실행 (Discord 전송 — DISCORD_WEBHOOK_URL 필요)
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 soxl_2sigma_alert.py

# 발동 빈도 · 분모 확인
python3 soxl_tqqq_2sigma_count.py        # 최근 1년
python3 soxl_tqqq_2sigma_count.py 5      # 5년 연환산

# 약세 조기경보 (Discord 전송 — DISCORD_WEBHOOK_URL 필요)
python3 bear_market_signals.py
```

---

## 🤖 GitHub Actions 자동화

| 워크플로우 | 트리거 | 설명 |
|------------|--------|------|
| `soxl_2sigma_alert.yml` | 평일 22:30 UTC (07:30 KST) <br>매월 1일 00:00 UTC (09:00 KST) | SOXL·TQQQ 2σ 알림(결측 시 1시간 간격 2회 재시도) + 매월 1일 웹훅 PING |
| `bear_market_signals.yml` | 평일 23:00 UTC | 시장 국면 리포트 + CAPE 캐시 커밋 |
| `scrapling_docker.yml` | 수동 | Scrapling 브라우저 fetcher 이미지 빌드 |

### 환경 변수 (GitHub Secrets)

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK` | Discord Webhook 주소 |
| `DISCORD_USER_ID` | Discord 사용자 ID (멘션용) |

---

## 📝 참고 사항

- **시간 기준**: 시장 판정은 `America/New_York`, 알림 스케줄은 UTC (22:30 UTC = 07:30 KST)
- **가격 기준**: yfinance 종가(15분 지연) — 판정은 모두 **종가 기준**(LOC 체결 규칙과 동일)
- **캐시**: `cape_cache.json` 은 7일 초과 시 `data_ok=False` (낡은 값 조용히 사용 금지)

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

## 📄 라이선스

본 프로젝트는 독점 소프트웨어입니다. 모든 권리 보유.
