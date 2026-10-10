# AGENTS.md — AI 에이전트 공통 지침 (strategy_engine)

> 이 저장소에서 코드를 작성하는 모든 AI 코딩 에이전트(Copilot · Codex · Cursor 등)가
> 자동 감지하는 **공통 표준** 지침입니다. GitHub Copilot은 `.github/copilot-instructions.md`와
> 함께 이 파일을 읽습니다.

## 코딩 철학 — Lazy Senior Dev (Ponytail)

You are a lazy senior developer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:

1. Does this need to be built at all? (YAGNI)
2. Does the standard library already do this? Use it.
3. Does a native platform feature cover it? Use it.
4. Does an already-installed dependency solve it? Use it.
5. Can this be one line? Make it one line.
6. Only then: write the minimum code that works.

Rules:

- No abstractions that weren't explicitly requested.
- No new dependency if it can be avoided.
- No boilerplate nobody asked for.
- Deletion over addition. Boring over clever. Fewest files possible.
- Question complex requests: "Do you actually need X, or does Y cover it?"
- Mark intentional simplifications with a `ponytail:` comment.

Not lazy about: input validation at trust boundaries, error handling that prevents data loss, security, accessibility, anything explicitly requested.

## 프로젝트 컨벤션 (strategy_engine)

### 현재 아키텍처
- **단일 파일 엔진**: `LOC_DCA_strategy.py` — 실전 브리핑 + 백테스트(`--backtest`) + 신호(`--signal`)를 모두 담당.
- **설정 단일 소스**: `portfolio_config.json` (포지션/시그마/LOC 분할 상태). 설정값은 코드에 하드코딩하지 않고 여기에서 읽는다.
- **현재 전략 규칙 (2026-08-16 단일 논리 재구성 · 2026-08-17 20→5분할 전환)**: **순수 LOC 지정가 5분할 DCA**
  하나만 사용한다 — LOC 매수가 = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER), 사용자가 정규장에서 이 가격으로
  LOC 지정가 주문 (마감가 체결 — 장 마감가 ≤ 지정가일 때만 체결, 판정은 종가 기준,
  2026-08-17 수정 · 차수당 $10,000 코드 상수 × 최대 5차, 분할 수는 `LOC_DCA` 블록 설정 —
  백테스트 기본값용, 예산은 사용자 엑셀 단일 소스 — config에 BUY_AMOUNT 없음, 2026-08-17).
  전고점 표시 = 원시 고가(High, 미조정)·전체 이력 기준 — 전고점 계산은
  **공용 함수** `LOC_DCA_strategy.get_all_time_high` 하나만 쓴다 (2026-09-12 수정·중복 제거;
  기존 '252일 종가 최고'는 차트의 실제 전고점보다 낮게 표시됨. 매수 판정(종가 기준)은 그대로).
  전일 종가(매수가 기준)도 **공용 함수** `LOC_DCA_strategy.get_prev_close` 하나만 쓴다 — 일봉이 미확정이면
  정규장 분봉 종가로 폴백해 하루 낡은 종가를 쓰지 않는다 (2026-09-22 —
  이 폴백을 걷어내면 LOC 매수가가 실제 전일 종가 기준으로 계산되지 않는다). `--signal` 의
  `load_data()` 도 실시간 모드(`end` 미지정)에서 같은 폴백으로 마지막 확정 종가 행을 붙인다 —
  수정 전에는 기준일이 09-18·LOC $66.99 로 하루 낡았다(실제 09-21·$68.17). 백테스트는 `end` 고정
  재현성을 지키기 위해 제외한다 (10년 결과 +1317.0%/MDD -81.7% 재현 확인).
  5분할 채택 근거:
  `loc_sweep_backtest.py` (2026-09-12 재측정 — 가격 계열 원시 종가로 통일) —
  LOC 추천 국면(강세장)에서 분할 수가 적을수록
  평균 수익률이 높고(1분할 +404% > 5분할 +389% > 20분할 +298%), MDD는 분할 수와 무관(1~10분할 -75.8% 동일),
  실용 균형 5~10분할 중 5분할은 평균·크래시 윈도우 모두 5~10 구간 내 최고. 20분할은 고점에서 4개월 만에
  소진되는 약점이 드러나 폐기 (하락장 최적은 52분할이지 20분할이 아님). ⚠️ **체결 추적은 봇이 하지 않는다
  (2026-08-16)**: 체결 여부는 증권앱 확인 + **엑셀 컬러 표시**로 관리하며, 분할 예산/회차는 엑셀이
  단일 소스 — 브리핑은 LOC 매수가 하나만 제공한다 (자동 카운터는 실제 주문 여부를 모르므로 폐기).
  **매도 규칙 없음** — 순수 적립 전용.
  🔻 **주문 채널 = LS증권 (2026-09-12 기록)**: LOC 주문은 **LS증권**에서 TQQQ **LOC(장마감 지정가)**로
  접수한다 — 마감가 ≤ 지정가일 때 종가 체결이라 엔진 판정과 같은 규칙이고, 일반 지정가로 걸면 장중
  터치로 체결돼 판정(종가 기준)과 어긋난다. 봇은 주문을 넣지 않는다 — 알림·가격만 제공. 운영 루틴은 README
  'LOC 5분할 운영 루틴 (LS증권)' 참고.
  ⚠️ 위 삭제는 **`LOC_DCA_strategy.py` 내부 로직 한정**이다 — MA 레짐 필터·RSI+볼륨·ATH_DCA 비상 모드·
  STAGE5·회복 재진입·실시간 모니터(`--ath-monitor`)는 전부 삭제(아래 제거 목록).
- **약세 조기경보**: `bear_market_signals.py` (리포트 `signal_report.json` · CAPE 캐시 `cape_cache.json` ·
  WF `bear_market_signals.yml`(리포트와 함께 `cape_cache.json` 도 커밋 — 러너가 일회성이라 커밋해야
  폴백 캐시가 낡지 않는다), 평일 23:00 UTC) — 7개 지표를 **선행 그룹(고점 경고, 0~6점)** 과
  **확인 그룹(하락 진행, 0~8점)** 으로 나눠 점수화하고, 두 합으로 시장 국면을 판정해 시장 국면(강세/약세)과 LOC 투입 시 주의 여부를
  Discord 로 알린다. 선행 = 금리 커브 · Fed 정책 사이클 · 밸류에이션(CAPE),
  확인 = Breadth · 신용 스프레드 · 경제활동(USPHCI YoY)·Sahm · 모멘텀. 데이터 = FRED CSV · yfinance ·
  multpl.com(CAPE).
  🔻 **LEI 동결 발견·교체 (2026-09-20)**: 이 신호는 원래 `USSLIND`(Philly Fed 선행지수)를 썼는데,
  그 시리즈가 **2020-02 에 중단**돼 값이 1.72 로 6년 넘게 동결된 채 `LEI contraction (+1)` 이
  **구조적으로 발동 불가**였다 (옆 관측: `fredgraph.csv` 는 요청 시작일을 무시하고 과거 이력만
  돌려주므로 중단된 시리즈도 '정상 데이터'처럼 보인다). 확인 그룹 8점 중 1점이 영구 0에 고정돼
  낙관 쪽으로 편향됐다 — ⚠️ **`data_ok` 로는 안 잡힌다** (NaN 이 아니라 '유효하지만 낡은' 값).
  살아있는 동행지수 `USPHCI` 의 **YoY < 0** 으로 교체. 실측 검증: 1990-91·2001·2008·2020 침체
  구간 포착 · 플래그 발생률 8.2% · 최근 31개월 오경보 0회 (동행지수라 침체 '진행 중'에 반응 =
  확인 그룹 목적과 부합). **신선도 가드**(`ACTIVITY_MAX_AGE_DAYS=120`, 월간+발표지연 고려)를 넣어
  같은 동결이 재발하면 점수 대신 '판정 불가'로 뜨다. FRED 시리즈를 새로 쓸 때는 **중단 여부를
  먼저 확인**할 것.
  📌 **국면 판정 규칙의 단일 출처는 `assess_regime()` 의 docstring** — 헤더나 다른 문서에 중복 서술하지 말 것
  (과거 이중 관리로 두 곳 설명이 어긋난 적 있음).
  🔁 **NaN 조용한 위장 금지 (2026-09-20 수정)**: `validate_yf_data` 는 심볼 공통 **완성 행만** 반환한다
  (미완성 마지막 행 = NaN 제거). 수정 전에는 `.iloc[-1]` 이 NaN 을 집어 모든 비교(<, >)가 False 가 되고
  '정상(+0)' 으로 위장됐다 — Market Breadth(RSP/SPY)·Momentum(섹터 슬라이스)이 `+nan%` 로 +0 보고,
  최근 16회 리포트 중 14회 발생(국면이 confirm 0 → '고점 + 강세장 지속(LOC 유리)' 쪽으로 기울어 있었다).
  🧹 **CAPE 수집 = 차단 판정 + 3단계 폴백 (2026-09-24)**: multpl.com 조회는 **200 이라고 성공이 아니다**
  (Cloudflare 챌린지는 예외를 안 던지고 200 챌린지 페이지를 그대로 돌려준다) — `detect_block`(상태코드 +
  본문 마커, ⚠️ `__cf_chl` 은 정상 페이지에도 있어 마커로 쓰면 오탐) + 값 범위 가드(5~100) 후
  ① requests → ② StealthyFetcher(우회) → ③ 캐시 폴백, **내려갈 때마다 사유를 detail 에 남긴다**
  (조용한 폴백 금지 — 캐시 신선도 7일 초과면 `data_ok=False`). ②는 **선택 의존성**이고 **미설치가
  의도된 결정**이다: multpl 은 현재 차단 중이 아니며(2026-09-24 실측 41.28), 진짜 위험(낡은 값의 조용한
  사용)은 ①③ 만으로 이미 막혔다. 설치 비용은 엔진 venv 의 `curl_cffi` 범프(yfinance HTTP 계층,
  scrapling[fetchers] 는 >=0.16.1 요구 — requirements 는 0.16.0 고정) + CI 런당 ~2분 → **리포트에 실제
  🚫 차단 문구가 뜨면 그때** `pip install "scrapling[fetchers]"` + `patchright install chromium` 으로 ②를 살린다.
  🧩 **결측 = 판정 불가 분리 (2026-09-20 전수 점검 후 추가)**: 점수 0 은 '정상'과 '판정 불가'가
  같은 값이라, `SignalResult.data_ok` 플래그로 둘을 구분한다 — `except` 분기와 결측 가드
  (`_require_finite()` = 결측이면 예외 / 인라인 `math.isfinite` 검사 = '판정 불가' 문구)가
  `data_ok=False` 를 세우고, `assess_regime()` 이 note 에 결측 신호 개수와
  「점수 과소집계 가능」 경고를 덧붙인다(점수 0 이 낙관 쪽이라 결측은 LOC 유리 방향으로 편향된다).
  리포트는 콘솔·`signal_report.json`(`data_ok`·`degraded_signals`)·LOC 브리핑(`get_market_regime`)까지
  같은 값을 전달한다 — 구버전 리포트는 `data_ok` 기본 True 로 호환.
  ⚠️ **검증된 범위 (2026-09-20)**: 결측 가드 총 **9곳** — `_require_finite()` 6곳(금리 커브 ·
  Breadth DD · 스프레드 · 경제활동(USPHCI) · Sahm · 모멘텀 200D) + 인라인 유한성 검사 3곳(CAPE ·
  Breadth 비율 · 모멘텀 섹터). FRED(`fred_series`)·yfinance(`validate_yf_data`) 상류에서 결측이
  걸러지는 것을 실제 확인했고, 결측 주입·동결 시리즈·이력 부족 주입으로 금리 커브·신용 스프레드·
  경제활동/Sahm·Breadth·모멘텀의 `data_ok=False` 를 확인했다.
  새 신호를 추가하면 **같은 가드를 먼저 달고** 결측 주입으로 `data_ok=False` 를 확인할 것
  (상류 dropna 에만 의존 금지 — 그 보호가 빠지면 같은 버그가 그대로 재발한다).
- **신호 시스템**: 브리핑의 ▶ 실행 액션 라인은 신호이며 실제 체결은 사용자 수동 매매 — 엔진은 주문을 자동 실행하지 않는다.

### 제거된 기능 — 재도입 금지
- **전고점 50% 청산 (peak sell)**: 2026-08-03 제거. 백테스트 전용으로만 존재했고 실전 엔진에서는 실행되지 않았다.
  관련 코드(`check_peak_sell_signal` 계열, `SELL_PCT`, `_SELL_*`)와 문서 언급(STRATEGY_RULES · README · 플로우차트)은
  모두 삭제됨. 다시 추가하거나 문서에 언급하지 말 것.
- **시가봉 박스 단타 봇 (openprice)**: 2026-08-06 제거. 유튜브 '시초가 단타매매' 영상 로직 기반
  (`openprice_trading.py` + `openprice_bot.yml` + `setup_cronjob_org.py --openprice` 모드).
  백테스트 결과 2시간 내 1:1 목표 도달률 3~4%·미청산(EXP) 70%로 사용자가 실전 채택을 포기해
  관련 파일·README 문서를 모두 삭제함. 다시 추가하거나 문서에 언급하지 말 것.
- **스윙 봇 (swing)**: 2026-08-07 제거. 4시간봉 3중 EMA + 변동성 수축 전략(`swing_bot.py`)과
  성과 평가(`swing_bot_eval.py`)·백테스트(`swing_bot_backtest.py`)·TP 분기 재평가(`swing_tp_review.py`),
  GHA 워크플로우 3개(`swing_bot.yml`/`swing_eval.yml`/`swing_tp_review.yml`), 로컬 크론(평가),
  cron-job.org 잡을 모두 삭제 — 당시 FVG 봇과의 백테스트 비교 후 알림 빈도·MDD 측면에서
  FVG 유지로 결정했으나, 이후 2026-08-08 FVG 봇 자체도 제거됨(아래). 다시 추가하거나 문서에 언급하지 말 것.
- **Finnhub API 키 로직**: 2026-08-06 제거. 무료 티어가 `/stock/candle`(봉) 데이터를 지원하지
  않아(403) 스윙 봇은 yfinance 전환, ATH DCA 실시간 모니터도 Finnhub `/quote` 오버라이드
  (`_fetch_finnhub_quote`/`realtime_prices` 파라미터)와 `FINNHUB_API_KEY` 시크릿 참조를 전면 삭제.
  이유: 키가 채팅·git 이력에 노출된 데다 삭제된 시크릿 참조 시 워크플로우가 실패하므로.
  모든 가격 판정은 yfinance(15분 지연) 기준. 다시 추가하거나 시크릿 참조를 부활시키지 말 것.
- **MA 레짐 필터 / RSI+볼륨 / ATH_DCA 듀얼 모드 (2026-08-16)**: 로직을 섞는 방식은 효율이 낮고
  오버피팅 문제가 있다는 판단(사용자)으로 **순수 LOC DCA 단일 논리로 재구성**하며 전부 삭제
  (당시 20분할 채택 → 2026-08-17 5분할로 전환, 재구성 자체는 유지).
  관련 코드(`check_ath_dca_signals`/`_check_ma_filter`/`_check_rsi_volume_signal`/`_check_recovery_reentry`/
  `_evaluate_strategy_mode`/`run_ath_dca_monitor` 계열, `STRATEGY_MODE`/`ATH_DCA`/`MA_FILTER`/`RECOVERY_REENTRY`
  설정 블록, `--ath-monitor` CLI)와 문서(DUAL_MODE_SUMMARY.md·TRIGGER_OPTIMIZATION_SUMMARY.md·
  REALTIME_ALERT_SETUP.md 삭제, README/STRATEGY_RULES/플로우차트/AGENTS.md 정리)를 모두 정리함.
  ⚠️ **cron-job.org 원격 ATH DCA 잡("ATH DCA realtime monitor")은 콘솔에서 수동 삭제 필요** —
  `--ath-monitor` 분기 삭제로 코드만으로는 사라지지 않는다 (FVG 원격 잡과 동일 케이스).
  다시 추가하거나 문서에 언급하지 말 것.
- **FVG 봇 (fvg)**: 2026-08-08 제거. 유튜브 FVG/CHoCH 데이 트레이딩 전략 이식 봇(`fvg_signal_bot.py`)과
  백테스트(`fvg_bot_backtest.py`)·실전 평가(`fvg_bot_eval.py`)·로컬 크론(`setup_fvg_cron.py`/`fvg_local_cron.sh`),
  GHA 워크플로우(`fvg_signal.yml`/`fvg_eval.yml`), 나무증권 가이드(`FVG_NAMYU_SETUP.md`),
  cron-job.org 잡(`setup_cronjob_org.py --fvg`), `portfolio_config.json`의 `FVG` 섹션을 모두 삭제.
  이유: 사용자 주도 백테스트 검증에서 실전 HTF(15분) 기준 창 내 성과가 마이너스·본전으로 확인되어
  실전 채택을 포기. 단기 데이 트레이딩 전략은 수수료(왕복 0.14%)가 얇은 엣지를 초과하는 구조.
  ⚠️ cron-job.org 서버의 원격 FVG 잡("FVG Signal Bot poll (5m)")은 코드 삭제만으로 사라지지
  않으므로 콘솔에서 수동 삭제 필요 (setup_cronjob_org.py는 잡 삭제 기능 없음).
  다시 추가하거나 문서에 언급하지 말 것.
- **시그마 LOC 백테스트 모드 + 52주 중앙값 밴드 (2026-08-17 실험 후 제거)**:
  `dollar_split_backtest.py --sigma` 실험(당시 달러 알리미 백테스트 — 파일은 2026-08-31 함께 삭제) — 전일종가×(1−배수×σ) LOC 매수(1개월 롤링 σ) ×
  +3% 익절, 52주 고/저 중앙값 밴드(중앙값 미만에서만 매수, 상향 돌파 시 매수 중지). 결과:
  ① σ×1.1 은 트리거 도달 **연 83~90회** — '252일 중 20회' 가정과 4배 차이 (장중 저가 기준 —
  당시 엔진 판정. 2026-08-17 LOC 판정을 마감가(종가) 기준으로 수정하며 해당 수치는 무효:
  종가 기준 σ×1.1 ≈ 연 32회·σ×1.43 ≈ 연 22회로 20회 가정에 근접), ② +3%
  익절은 평균 보유 150~700일로 수년간 갇혀 CAGR 0.2~1% (바이앤홀드 +0.8%와 동급), ③ 중앙값 밴드는
  σ 전략의 MDD 를 -36→-25% 로 개선하지만 최적(σ×2.5+밴드)도 CAGR +2.9% 로 현재 실전 대비 열위,
  ④ 밴드를 현재 실전(0.3%/0.3%)에 얹으면 CAGR 반토막(+5.2→+3.1%) 대신 MDD 개선(-14.2→-11.9,
  최근 10년 -12.5→-4.1) — 수익 포기 대가. 전 구간(22.6년/10년) 동일 판정 → **실전 미채택**,
  백테스트 도구에서도 코드 제거. 다시 추가하거나 문서에 언급하지 말 것.
  (참고: 같은 날 발견한 yfinance 데이터 글리치 보정 — 2008-03-17 High=21,353 → max(Open,Close)
  — 은 fetch_ohlc 에 유지, 기존 실전 결과 영향 없음 확인.)
- **달러 알리미 (dollar)**: 2026-08-31 제거. 박성현 『매직 스플릿』의 달러 매매(USD/KRW)를
  swing_alerter.py 와 같은 알림 앱 구조로 재구현했던 기능 — `dollar_alerter.py`,
  `dollar_config.json`/`dollar_state.json`/`dollar_personal.json`, 대시보드 `dollar_dashboard.html`,
  백테스트 `dollar_split_backtest.py`, GHA 워크플로우 `dollar_alerter.yml`, cron-job.org `dollar-monitor`
  잡, gh-pages `dollar.html` 배포를 모두 삭제하고 문서(README/AGENTS/.gitignore) 잔재도 정리함.
  ⚠️ cron-job.org 콘솔의 원격 잡("Dollar alerter realtime monitor")은 코드 삭제만으로 사라지지 않으므로
  수동 삭제 필요 (FVG/ATH DCA 잡과 동일 케이스, setup_cronjob_org.py 는 잡 삭제 기능 없음).
  다시 추가하거나 문서에 언급하지 말 것.
- **스윙 투자 알리미 (swing alerter)**: 2026-10-05 제거. 유튜브 'TQQQ 스윙 투자 전략' 스프레드시트를
  재구현한 알림 앱(ATH 대비 MDD 구간 매수 + 목표 매도, OneSignal 푸시, 모바일 대시보드, 세븐 스플릿
  7계좌)을 `swing_alerter.py` · `swing_config.json`/`swing_state.json`/`swing_personal.json` ·
  `swing_dashboard.html` · `swing_split_backtest.py` · `loc_vs_swing_backtest.py`(→ `loc_sweep_backtest.py`
  로 LOC 전용 축소) · `NAMU_SWING_SETUP.md` · GHA `swing_alerter.yml` · gh-pages 대시보드와 함께 삭제.
  계좌 3번까지만 기록할 정도로 운용 피로가 컸고 TQQQ 가 하락하지 않아 기회 이익도 없었음(약 +13% 수익).
  데드코드가 된 JS/PWA 툴체인(`sw.js` · `OneSignalSDKWorker.js` · `onesignal.d.ts` ·
  `check_dashboard_js.py` · `tsconfig.json`/`tsconfig.dashboard.json` · `package.json`)과 cron-job.org
  swing-monitor 전용 `setup_cronjob_org.py` 도 함께 제거 — **이제 TypeScript/npm 검사 게이트가 없다.**
  ⚠️ **cron-job.org 콘솔의 원격 잡("Swing alerter realtime monitor")은 코드 삭제만으로 사라지지 않으므로
  수동 삭제 필요** (달러/FVG/ATH DCA 잡과 동일 케이스). `LOC_DCA_strategy.get_all_time_high`/`get_prev_close`
  는 LOC 전용으로 남으므로 건드리지 말 것. `bear_market_signals` 의 'LOC vs 스윙' 추천 문구도 함께
  제거(LOC 단일 전략 기준 note 로 정리). 다시 추가하거나 문서에 언급하지 말 것.

- **후지모토 시게루 '1:2:6 매매법' 기계적 검증 (2026-09-20 실험 후 제거)**: 유튜브 영상 전략
  검증용 임시 도구 `fujimoto_126_backtest.py` (커밋 없이 삭제 — git 이력에 없음).
  원전은 포지션 **사이징** 규칙(1000:2000:6000 = 11%:22%:67%, 매도도 같은 분할)이고 트리거가
  재량("좋아 보인다")이라 그대로는 검증 불가 — 기계적 트리거로 대체해 검증함
  (비중1 = RSI30 회복 / 비중2 = MACD 골든크로스 / 비중3 = 일목 구름대 돌파 후 재돌파·상승 전환,
  매도 없음, 예산 $10,000 × 1:2:6). 29종목 × 롤링 5년 6개월 간격 = 290윈도우 결과:
  B&H 대비 승률 31~41%(순차 37%·독립 31%) · 평균 초과 -24~-57%p · 종목별 평균 우위 2~4/29 ·
  평균 MDD -44.9% vs B&H -46.3% → **분할의 하락 방어 효과 없음** (3트랜치가 전 종목 발동 =
  투입률 100%라 그 뒤로는 B&H 와 같은 커브 — 위험은 그대로, 노출 기간만 짧음).
  원인: 세 지표가 모두 후행 '하락 후 반등' 신호(RSI 회복 → MACD → 구름대 재돌파 순)라 한 바닥에
  몰려 체결(SOXL 실측 3.5개월 내 $6.8~8.0 구간) = 사실상 'B&H 를 늦게 시작'. 1:2:6 비중 배분
  자체는 다 발동되면 성과에 영향이 없다(방향이 아니라 크기 규칙). 단일 시작일로만 보면 17~19/29
  로 이겨 보이는 **시작일 편향**도 재확인 → 판정은 반드시 롤링 기준으로 할 것.
  **실전 미채택** — 파일 삭제. 다시 만들거나 문서에 언급하지 말 것.
- **장세 기반 MDD 3차 분할 최적화 (mdd_optimizer)**: 2026-09-20 제거. `mdd_optimizer.py` +
  `mdd_config.json` — ATH 대비 MDD·변동성(20일 연율)·추세점수(MA5/20/60)로 장세를 판정해
  (strong_bull / normal / deep_correction / extreme) 종목별 **3차 매수 레벨(-MDD)** 과 목표가를
  콘솔에 출력하는 단발 도구. **백테스트 없이 설정 파일에 손으로 튜닝한 레벨**이고 3분할 균등
  (33.3%) 모델이라 당시 실전(순수 LOC 5분할 σ · 스윙 7계좌 3% 스텝)과 설계가 어긋난다.
  대상도 SOXL·PLTR 로 현재 전략(둘 다 TQQQ 전용) 밖이고, 코드·워크플로우·문서 어디에서도
  참조되지 않던 고아 파일이었다(마지막 변경 2026-08-31 — README 파일 표에 없던 유일한 .py).
  💡 리서치 결론만 남긴다 — **hard limit = 실측 최대낙폭 + 버퍼**: SOXL -93.0%(실측 -90.5%,
  2022-10) · PLTR -88.0%(실측 -84.6%, 2020 상장 이후) · 기본 -75.0%.
  레벨 예: SOXL strong_bull [-18,-30,-50] / extreme [-48,-62,-80],
  PLTR strong_bull [-12,-22,-35] / extreme [-35,-50,-65].
  복원: `git log --diff-filter=D -- mdd_optimizer.py` 로 삭제 커밋을 찾고
  `git checkout <삭제 커밋>^ -- mdd_optimizer.py mdd_config.json`.
  다시 추가하거나 문서에 언급하지 말 것.
- **시장 단계 트래커 (market_stage)**: 2026-09-29 제거. `MarketStageSystem.py`(하단/상단 5단계 트래커 +
  Discord 리포트), 상태 `market_state.json`, GHA 워크플로우 `market_stage_tracker.yml`(평일 23:14 UTC),
  회귀 테스트 `test_market_stage.py` 를 모두 삭제하고 문서(README/AGENTS/STRATEGY_RULES/플로우차트/
  .gitignore) 잔재도 정리함.
  이유: 판정 결과가 **매수/매도 트리거로 쓰이지 않아**(2026-08-16 이후 DCA 미사용) 실익이 없었고,
  바닥 라벨은 지수/레버리지 ETF 밖에서 부호가 뒤집혀(NVDA·AAPL 기대초과 음수) 이식성도 없었다.
  ⚠️ **워크플로우를 함께 지워야 한다** — 스크립트만 지우면 평일 23:14 UTC 런이 매번 실패한다.
  cron-job.org 원격 잡은 없으므로(순수 GHA schedule) 콘솔 정리 대상은 없다.
  💡 삭제 시점까지의 리서치 결론(재검토 시 참고 — 재도입 근거 아님):
  ① 단계는 증가만 하는 래칫이라 만료(리셋)가 유일한 해제 경로였고, 기산일은 `stage_entered_date`
     하나로 충분했다(별도 무장일 필드 불필요, 같은 날 재실행에도 멱등).
  ② 하단 1단계를 '낙폭 ≤ -10% + RSI(14) < 35' 로 교체 — 발동률 4.8%·이후 20일 +6.06pp.
     상단은 교체 불가(고점+과매수 후보가 모멘텀 자산에서 부호 반전 — SOXL +6.92pp).
  ③ 하단 래더에서 실제 우위가 있던 단계는 1단계(+1.63pp)·4단계(+2.80pp, 최대낙폭 최소)였고,
     2단계는 매수 단계가 아니라 **위험 구간**(이후 20일 최대낙폭 -18.66%, 기준 -8.42%),
     5단계는 근거가 약했다(진입 3회뿐).
  ④ 하단 2단계의 '거래량 감소' 요구를 완화해도 V자 바닥(2020-03 COVID)은 잡히지 않는다 —
     저점 근접일의 거래량이 1.61~2.00배였고, 거래량 조건을 완전히 빼야만 진행했다.
  복원: `git log --diff-filter=D -- MarketStageSystem.py` 로 삭제 커밋을 찾고
  `git checkout <삭제 커밋>^ -- MarketStageSystem.py market_state.json test_market_stage.py
  .github/workflows/market_stage_tracker.yml` (파일 4개를 함께 되돌려야 워크플로우가 죽지 않는다).
  다시 추가하거나 문서에 언급하지 말 것.

### 문서 규율
- `STRATEGY_RULES.md`는 **순수 규칙만** — 백테스트 근거·성과 수치·미사용 기능 노트를 넣지 않는다.
- 기능/로직 제거 시 모든 문서(README · 플로우차트 · 요약 문서)에서 함께 정리한다.
- `LOC_DCA_strategy_flowchart.py`는 플로우차트 문서. (설계/분석 문서 DUAL_MODE_SUMMARY.md·TRIGGER_OPTIMIZATION_SUMMARY.md는
  2026-08-16 듀얼 모드/ATH_DCA 삭제로 함께 제거됨 — 재생성 금지)

### 검증 & 커밋
- 변경 후 `python3 -m py_compile <file>.py` 로 문법 확인, 가능하면 실제 실행(`--signal` / `--backtest`)으로 동작 확인.
  버그 검토는 코드 리뷰로 수행.
- **테스트 전략 (2026-08-14 검토 결론)**: pytest/Zod 등 테스트·스키마 라이브러리를
  설치하지 않는다 (YAGNI). 이유: ① 계산은 Python 단일이므로 `py_compile` + 실제 실행 검증으로 충분하고,
  ② mock 기반 단위 검증이 필요하면 표준 라이브러리 `unittest.mock` 으로 충분하다 (설치 0건).
  **예외 — 테스트를 추가하는 때**: 국면 판정·가격 조회 등에서 실제 버그가 재발하면 그 함수만
  `unittest` 로 고정(회귀 테스트), 또는 계산 규칙 변경 시 변경 함수부터 테스트 작성 후 수정.
- **가격 기준 회귀 테스트 (2026-09-22)**: 2026-09-22 에 난 가격 기준 버그(일봉 미확정 분봉 폴백 ·
  `--signal` 낡은 세션)는 `test_price_basis.py` 에 고정해 두었다 —
  `python3 -m unittest -q test_price_basis` (네트워크 0회, yfinance 는 mock, 0.01초).
  가격 조회(`get_prev_close`/`load_data`)를 건드렸으면 이 테스트를 먼저 통과시킬 것
  (수정 전 코드에선 실제 회귀를 잡는다 — 2026-09-22 확인). 워크플로우에는 걸려 있지 않다.
- ⚠️ **`.loc[Hashable]` 함정 — 전고점 조회는 `.max()` (2026-10-10)**: `get_all_time_high` 의
  `float(highs.loc[peak_idx])` 는 **실행은 되지만 Pylance 가 오류로 표시**한다 — `idxmax()` 의
  반환형이 `Hashable` 인데 `.loc` 키 오버로드는 스칼라를 받지 않는다 (`No overloads for
  "__getitem__" match the provided arguments (reportCallIssue)`). 지금은 `float(highs.max())` 로
  최댓값을 얻는다 — `loc[idxmax()]` 와 같은 값이고 동률일 때 첫 위치 선택도 동일하다.
  ⚠️ `.loc[peak_idx]` 로 되돌리지 말 것 (되돌리면 그 줄에 Pylance 오류가 부활한다 — 코드 주석에도
  같은 경고가 있다). 이 오류는 **도구(타입 스텁 유무)에 따라 보이거나 안 보인다**: 스텁이 잡히는
  환경에선 파일 전체에서 그 줄 하나만 오류로 뜨고, 프로젝트 `pyrightconfig.json` 설정만으로는
  `pyright` 가 0 errors 를 낸다 — "pyright 통과" 만으로 이 줄이 안전하다고 판단하지 말 것.
- 커밋 메시지: `type: 한글 요약 — 상세` 형식 (예: `refactor: ...`, `feat: ...`, `docs: ...`).
- 언어: 사용자 소통·문서는 한국어, 코드 식별자는 영어.
