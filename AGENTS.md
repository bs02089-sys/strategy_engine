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
- **단일 파일 엔진**: `DCA_MA_strategy.py` — 실전 브리핑 + 백테스트(`--backtest`) + 신호(`--signal`)를 모두 담당.
- **설정 단일 소스**: `portfolio_config.json` (포지션/시그마/모드 상태). 설정값은 코드에 하드코딩하지 않고 여기에서 읽는다.
- **스윙 알리미**: `swing_alerter.py` (2026-08-08 신규) — 유튜브 'TQQQ 스윙 투자 전략' 구글 스프레드시트
  (ATH 대비 MDD 구간 매수 + 매수가 대비 스윙 목표 수익률 매도) 재구현. 사용자별 매도 푸시는 앱이
  등록한 매도 예정가 태그(`swing_sell_{TICKER}`) 기준으로 OneSignal에 필터 발송(1일 1회). 설정/상태는 `swing_config.json` 단일 파일,
  알림은 Discord, 실시간은 cron-job.org `swing-monitor` 디스패치, 모바일 대시보드는
  `--serve`/`swing_dashboard.html` + GitHub Pages(`gh-pages` 브랜치 자동 배포).
  **아래 '스윙 봇(swing)' 제거 항목과 무관한 별개 기능**이며 혼동하지 말 것.
- **현재 전략 규칙**: LOC ↔ ATH_DCA 듀얼 모드 · MA 레짐 필터(LOC 모드 한정) · ATH 하락분할 DCA(3분할, 3차 = Stage 5 바닥) ·
  비상 모드 종료(RECOVERY_REENTRY: 미사용 분할 ≥1 + 30영업일 + DD ≤ TRIGGER_1×50% + MA20>MA60).
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
  (참고: 2026-08-08 신규 추가된 `swing_alerter.py` 스윙 알리미는 이 스윙 봇과 무관한 별개 기능이며
  재도입 금지 대상이 아니다.)
- **Finnhub API 키 로직**: 2026-08-06 제거. 무료 티어가 `/stock/candle`(봉) 데이터를 지원하지
  않아(403) 스윙 봇은 yfinance 전환, ATH DCA 실시간 모니터도 Finnhub `/quote` 오버라이드
  (`_fetch_finnhub_quote`/`realtime_prices` 파라미터)와 `FINNHUB_API_KEY` 시크릿 참조를 전면 삭제.
  이유: 키가 채팅·git 이력에 노출된 데다 삭제된 시크릿 참조 시 워크플로우가 실패하므로.
  모든 가격 판정은 yfinance(15분 지연) 기준. 다시 추가하거나 시크릿 참조를 부활시키지 말 것.
- **FVG 봇 (fvg)**: 2026-08-08 제거. 유튜브 FVG/CHoCH 데이 트레이딩 전략 이식 봇(`fvg_signal_bot.py`)과
  백테스트(`fvg_bot_backtest.py`)·실전 평가(`fvg_bot_eval.py`)·로컬 크론(`setup_fvg_cron.py`/`fvg_local_cron.sh`),
  GHA 워크플로우(`fvg_signal.yml`/`fvg_eval.yml`), 나무증권 가이드(`FVG_NAMYU_SETUP.md`),
  cron-job.org 잡(`setup_cronjob_org.py --fvg`), `portfolio_config.json`의 `FVG` 섹션을 모두 삭제.
  이유: 사용자 주도 백테스트 검증에서 실전 HTF(15분) 기준 창 내 성과가 마이너스·본전으로 확인되어
  실전 채택을 포기. 단기 데이 트레이딩 전략은 수수료(왕복 0.14%)가 얇은 엣지를 초과하는 구조.
  ⚠️ cron-job.org 서버의 원격 FVG 잡("FVG Signal Bot poll (5m)")은 코드 삭제만으로 사라지지
  않으므로 콘솔에서 수동 삭제 필요 (setup_cronjob_org.py는 잡 삭제 기능 없음).
  다시 추가하거나 문서에 언급하지 말 것.

### 문서 규율
- `STRATEGY_RULES.md`는 **순수 규칙만** — 백테스트 근거·성과 수치·미사용 기능 노트를 넣지 않는다.
- 기능/로직 제거 시 모든 문서(README · 플로우차트 · 요약 문서)에서 함께 정리한다.
- `DCA_MA_strategy_flowchart.py`는 플로우차트 문서, `DUAL_MODE_SUMMARY.md`·`TRIGGER_OPTIMIZATION_SUMMARY.md`는 설계/분석 문서.

### 검증 & 커밋
- 변경 후 `python3 -m py_compile <file>.py` 로 문법 확인, 가능하면 실제 실행(`--signal` / `--backtest`)으로 동작 확인.
  버그 검토는 코드 리뷰로 수행.
- 커밋 메시지: `type: 한글 요약 — 상세` 형식 (예: `refactor: ...`, `feat: ...`, `docs: ...`).
- 언어: 사용자 소통·문서는 한국어, 코드 식별자는 영어.
