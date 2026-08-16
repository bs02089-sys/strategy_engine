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
- **스윙 알리미**: `swing_alerter.py` (2026-08-08 신규) — 유튜브 'TQQQ 스윙 투자 전략' 구글 스프레드시트
  (ATH 대비 MDD 구간 매수 + 매수가 대비 스윙 목표 수익률 매도) 재구현. OneSignal 푸시는
  **전체 구독자(Subscribed Users = 내 기기) 대상**으로 발송(2026-08-12 단독 사용 전환 — 매도 신호 푸시는
  계좌별 **사이클당 1회**로 변경, 2026-08-13). 매도 신호를 보낸 계좌는 리셋(전 계좌 매도 완료 자동 리셋/수동
  --reset) 전까지 재발송하지 않는다.
  설정은 `swing_config.json`(사용자 소유 — 공용: 티커/구간/목표/푸시 설정), 상태는 `swing_state.json`
  (봇 전용 — ZONE_ALERTS/매도 플래그)로 분리 — 봇이 상태 파일만 커밋하므로 git 충돌로 알림 상태가
  유실되지 않는다.
  🔒 **개인 포지션 분리 (2026-08-10)**: 실제 매수가/보유수량은 공용 설정에 두지 않고
  `swing_personal.json`(사용자 소유, 봇 미기입)에만 기록한다. 세븐 스플릿 7개 계좌는 `LOTS`
  (계좌별 BUY_PRICE/SHARES) 구조로 개별 추적 — `_PERSONAL` 마커가 붙은 포지션은
  Discord 브리핑/대시보드/전역 푸시 등 공용 알림에서 매도 정보가 노출되지 않는다 (매도 미설정으로 표시),
  콘솔에서만 🔒 개인 라벨로 계좌별 매도 목표를 확인한다. 매도 푸시는 태그 없이 서버 LOTS 매도
  목표로 전체 구독자에 발송된다 (2026-08-12 단독 사용 전환 — 태그 필터 제거).
  🚀 **매도 목표 임박 푸시 (2026-08-15)**: 나무증권 매도감시를 '목표 임박 시점'에 등록하는 루틴
  (NAMYU_SWING_SETUP.md ②)의 신호가 되도록, 개인 포지션(_PERSONAL)의 매도 목표 임박(목표까지
  IMMINENT_GAP_PCT 이내)도 `send_sell_imminent_pushes`로 전체 구독자(= 내 기기)에 발송한다 —
  매도 신호 푸시와 동일 패턴 (계좌별 사이클당 1회, SELL_IMMINENT_PUSH_LAST_AT 상태로 중복 방지,
  도달 gap=0 은 제외 — 도달은 신호 푸시가 담당).
  ⚠️ **OneSignal 푸시 — 단독 사용 전환 (2026-08-10 제거 → 2026-08-12 재허용)**: 2026-08-10에 지인
  노출 차단 목적으로 전역 푸시를 제거했으나, 2026-08-12 지인 미구독 확인(카카오톡)으로 **이 앱은
  사용자 본인 전용**으로 운영한다. 이에 따라 `send_user_sell_pushes`/`send_zone_pushes`/모닝
  리마인더는 **전체 구독자(`Subscribed Users`) = 내 기기** 대상으로 발송한다 (태그 필터/Liquid 제거
  — 태그 미등록 기기도 수신, 'All included players are not subscribed' 0명 누락 방지).
  ⚠️ 지인이 새로 구독하면 본인 매수 정보가 노출될 수 있음을 인지할 것.
  📣 **매수 구간 푸시 (2026-08-11 → 2026-08-12 단독 전환)**: 매수 구간 도달(🔻)/임박(📡)은 Discord뿐
  아니라 전체 구독자(= 내 기기)에게 `send_zone_pushes`로 발송한다 — `swing_zone_{TICKER}` 태그 필터는
  단독 사용 전환으로 제거(2026-08-12). 매수 구간은 ATH(공개 정보) 기준이라 개인 정보 노출이 없다.
  중복 방지는 `ZONE_ALERTS` 상태(detect_alerts)가 담당 — 신규 이벤트만 푸시하고,
  발송 실패 시 `ZONE_PUSH_PENDING` 대기 큐(당일 한정)에 보관해 다음 폴링에서 재시도, 하루 지난 대기분은 폐기한다.
  알림은 Discord, 실시간은 cron-job.org `swing-monitor` 디스패치, 모바일 대시보드는
  `--serve`/`swing_dashboard.html` + GitHub Pages(`gh-pages` 브랜치 자동 배포).
  ⚠️ **장중 실시간 표시 (2026-08-11)**: 앱/대시보드의 **현재가 표시만** 장중에
  yfinance 실시간(15분 지연) 기준으로 오버레이한다 — `compute_ticker(live=True)`가
  `_get_live_price()`(fast_info → 1분봉 폴백, 미국 정규장 09:30~16:00 ET 판정)로
  표시 가격/as_of/등락률만 갱신한다. **알림 판정(매수 구간 도달/임박/매도)은 항상 확정
  종가 기준 유지** — `detect_alerts`는 `close_price`를 사용하며, 실시간 값으로 알림
  시점을 흔들지 말 것. `swing-monitor` 디스패치도 대시보드를 재생성·gh-pages 재배포하므로
  스마트폰 앱이 장중 갱신된다 (배포 가드: 대시보드 생성 실패 시 배포 생략).
  🔄 **사이클 자동 리셋 (2026-08-11)**: LOTS의 전 계좌가 매도 목표(+40%)에 도달하면
  수동 `--reset` 없이 알림 상태(ZONE_ALERTS/SELL 플래그/ATH_CYCLE_BASE)를 자동 초기화한다
  (`auto_cycle_reset()` — `CYCLE_RESET_DONE` 플래그로 중복 방지, 매도 미도달 상태가 되면 자동
  재무장). 신고가 갱신(+1%) 리셋과 별개 동작이며, 봇은 여전히 `swing_personal.json`(사용자 소유)을
  절대 쓰지 않는다 — 매도 후 LOTS 정리/재기록은 사용자 몫이다. 전 계좌 매도 시 수동 --reset 은 불필요.
  📌 **매수/매도 예정가 — 서버 렌더링 단일 소스 (2026-08-12)**: 계좌별 매수 예정가/매도 예정가는
  앱 대시보드를 생성할 때 `swing_personal.json`(LOTS 실제 매수가 × SWING_TARGET_PCT)을 읽어 서버가
  직접 그려 넣는다 — 폰/웹이 OneSignal 상태와 무관하게 항상 같은 값을 표시한다. 예상 수익률도
  `SWING_TARGET_PCT`(swing_config.json) 단일 소스. OneSignal 태그 동기화(swing_buy_/swing_sell_
  태그, login/getTags/addTag)는 409/중복 사용자 문제로 전면 제거 — 태그를 다시 추가하지 말 것.
  앱 헤더의 '동기화 코드' UI는 남아 있으나 OneSignal 외부 ID 병합(사용자 통합) 용도일 뿐 값
  동기화가 아니다.
  🏦 **나무증권 이중 장치 — 체결 자동화 (2026-08-12)**: 스윙 알리미(푸시 알림 담당)와 나무증권
  앱(자동 체결 담당)을 병행한다. 나무증권은 해외주식(TQQQ) 목표가 푸시 알림(시세알림)이 미지원이므로
  **알림은 스윙 알리미, 체결은 나무증권 '해외주식 시세포착주문'**으로 역할을 나눈다. 감시 등록 기준:
  ① 매수감시 = 감시 조건 **하락**, 감시 가격 = 매수 구간 가격(예: 2차 -18% $71.36 — 3% 래더 전환 후),
  **등록 시점 = 📡 매수 구간 임박 푸시 확인 시** (30일 창이 구간 도달 직전과 겹치도록 — 매도감시 ②와 동일 원칙), 지정가+0틱, (계좌별 상세 가격/수량표는 `NAMYU_SWING_SETUP.md` 참고)
  수량은 나무증권 '매수가능수량' 안내(현재가 기준)를 따름($500 예산 안전 — 목표가 기준 7주도 되지만
  체결가 변동 감안 6주 권장), 감시시간 **정규장만**(전략 판정이 정규장 종가 기준이라 알림·체결 시점 일치),
  감시기간 **30일**(만료 시 자동 해제 → 미체결이면 재등록 필요). ② 매도감시 = 감시 조건 **상승**, 감시 가격 = 매도 예정가(매수가 × SWING_TARGET_PCT, 예: $73.49×1.4=$102.89 — 나무증권은 절사로 $102.88 표시, 1센트 차이는 반올림 규칙일 뿐 무시). **매도감시 등록은 매수 직후가 아니라 목표 임박(🚀, 대시보드, 목표까지 5%p 이내) 시점** — 30일 감시가 목표 도달(평균 8.3개월) 전에 만료되는 것 방지 (백테스트: 매수 후 30일 내 도달 7%뿐, 2026-08-15 점검). ③ 감시기간 30일 만료 후 재등록 — 매수는 🔻/📡 푸시, 매도는 🚀 임박(대시보드)이 신호 → 임박 시 등록, 미도달 만료 시 재임박 때 재등록, 도달 시 📈 푸시 → 수동 매도가 최종 폴백.  운영 루틴: **매수 체결 → 사용자가 봇에게 알림 → 사용자가(채팅에서 봇 도움 하에)
  `swing_personal.json`(LOTS) 기록 → 매도감시 등록(목표 임박 시)** — 엔진(swing_alerter.py)은 이 파일을 읽기만
  하고 절대 쓰지 않는다(기존 개인 포지션 분리 원칙 유지). 폰앱/웹앱 입력란은 표시용이므로 값 관리는
  `swing_personal.json` 한 곳뿐이며, 나무증권 감시(체결)와 스윙 알리미(알림)가 서로를 백업한다.
  🔒 코드 입력칸은 마스킹(password) 표시 — 눈 아이콘 토글로 잠시 확인 (어깨 너머 노출 방지, 2026-08-12).
  ⚠️ **봇은 `swing_dashboard.html`을 main에 커밋하지 않는다** — 생성 파일(헤더 시각 등)이 봇/사용자
  양쪽에서 재생성되어 git pull 충돌을 반복하므로, 워크플로우가 생성한 신선한 사본을 `gh-pages`에만
  배포한다 (`swing_alerter.yml` Sync 단계의 cp 참고 — 미추적 파일이라 `git checkout --` 금지, pathspec 오류).
  **아래 '스윙 봇(swing)' 제거 항목과 무관한 별개 기능**이며 혼동하지 말 것.
- **달러 알리미 (dollar_alerter.py, 2026-08-17 신규)**: 박성현 『매직 스플릿』의 달러 매매 아이디어를
  swing_alerter.py 와 같은 알림 앱 구조로 재구현 — 데이터는 yfinance `USDKRW=X` (주식 아님에도 환율 티커
  `=X` 접미사로 일봉/실시간 지원, 2003년부터 존재). 전략은 **dollar_split_backtest.py 백테스트 검증값**:
  매수 신호 = 전일 종가 대비 -0.3%~-0.5% 하락 (`BUY_DROP_PCT`/`BUY_BAND_PCT`), 익절 신호 = 매수가 대비
  +0.5% (`SELL_TARGET_PCT`) — 백테스트(2004~2026, 스프레드 왕복 0.1% 반영) 결과: '상승 매수/하락 매도'
  해석은 CAGR 음수(패배), **'하락 매수/익절 매도' 해석만** CAGR +4.1% vs 바이앤홀드 +0.8%·MDD -17.2%로
  우위. ⚠️ **'1년 97% 수익률'은 어떤 해석으로도 재현 불가** (최고 연도 +12.8%) — 수수료 0% 가정의
  회당 +0.5%가 복리로 과대표시된 것으로 판단. 실전 체결은 **은행 영업시간(평일 09~16시 KST)에만**
  가능하므로 신호 판정도 그 시간대의 실시간 가격 기준 (swing 의 종가 기준 원칙과 다름). 설정
  `dollar_config.json`(공용)/상태 `dollar_state.json`(봇 전용 — 매수 신호는 당일 한정 자동 리셋, 익절은
  계좌별 사이클당 1회)/개인 포지션 `dollar_personal.json`(사용자 소유, 봇 읽기만) 3파일 분리. 대시보드는
  JS 없이 meta refresh(300초) — `dollar_dashboard.html`은 main 에 커밋하지 않고 gh-pages 의 **dollar.html**
  로 배포 (swing 의 index.html 과 충돌 방지). cron-job.org 잡은 `GITHUB_EVENT_TYPE=dollar-monitor` +
  `UTC_HOURS_START=0 UTC_HOURS_END=7`(은행 영업시간)로 생성 (setup_cronjob_org.py 는 env 기반이라 수정 불필요).
- **현재 전략 규칙 (2026-08-16 단일 논리 재구성)**: **순수 LOC 지정가 20분할 DCA** 하나만 사용한다 —
  LOC 매수가 = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER), 사용자가 정규장에서 이 가격으로 LOC 지정가 주문
  ($2,500 × 최대 20차, `LOC_DCA` 블록 설정 — 백테스트 기본값용). ⚠️ **체결 추적은 봇이 하지 않는다
  (2026-08-16)**: 체결 여부는 증권앱 확인 + **엑셀 컬러 표시**로 관리하며, 분할 예산/회차는 엑셀이
  단일 소스 — 브리핑은 LOC 매수가 하나만 제공한다 (자동 카운터는 실제 주문 여부를 모르므로 폐기).
  **매도 규칙 없음** — 순수 적립 전용.
  MA 레짐 필터·RSI+볼륨·ATH_DCA 비상 모드·STAGE5·회복 재진입·실시간 모니터(`--ath-monitor`)는 전부 삭제(아래 제거 목록).
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
- **MA 레짐 필터 / RSI+볼륨 / ATH_DCA 듀얼 모드 (2026-08-16)**: 로직을 섞는 방식은 효율이 낮고
  오버피팅 문제가 있다는 판단(사용자)으로 **순수 LOC 20분할 DCA 단일 논리로 재구성**하며 전부 삭제.
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

### 문서 규율
- `STRATEGY_RULES.md`는 **순수 규칙만** — 백테스트 근거·성과 수치·미사용 기능 노트를 넣지 않는다.
- 기능/로직 제거 시 모든 문서(README · 플로우차트 · 요약 문서)에서 함께 정리한다.
- `LOC_DCA_strategy_flowchart.py`는 플로우차트 문서. (설계/분석 문서 DUAL_MODE_SUMMARY.md·TRIGGER_OPTIMIZATION_SUMMARY.md는
  2026-08-16 듀얼 모드/ATH_DCA 삭제로 함께 제거됨 — 재생성 금지)

### 검증 & 커밋
- 변경 후 `python3 -m py_compile <file>.py` 로 문법 확인, 가능하면 실제 실행(`--signal` / `--backtest`)으로 동작 확인.
  버그 검토는 코드 리뷰로 수행.
- **JS 변경 시 (2026-08-14)**: `sw.js`/`OneSignalSDKWorker.js`/`swing_alerter.py` 인라인 대시보드 JS를
  건드렸으면 반드시 `npm run typecheck` 로 strict 검사를 통과시킨다 (Python 의존성과 별개로
  Node + typescript 필요 — `npm ci` 후 실행). 대시보드 인라인 JS는 `check_dashboard_js.py` 가
  `swing_alerter.py` 의 `<script>` 상수를 `.typecheck/` 로 추출해 `tsconfig.dashboard.json` 이 검사한다
  (tsc 는 HTML 인라인 스크립트를 직접 읽지 못함). `.typecheck/` 는 추출물이라 커밋하지 않는다.
- **테스트 전략 (2026-08-14 검토 결론)**: pytest/Vitest/Zod 등 테스트·스키마 라이브러리를
  설치하지 않는다 (YAGNI). 이유: ① JS 는 서비스 워커 52줄 + 화면 표시용뿐이라 테스트할 로직이 없고
  (진짜 계산은 Python), ② Python 검증은 `py_compile` + 실제 실행으로 충분하며, ③ mock 기반 단위
  검증이 필요하면 표준 라이브러리 `unittest.mock` 으로 충분 (설치 0건 — ZONE_PUSH_PENDING 재시도 큐
  검증 사례 참고). **예외 — 테스트를 추가하는 때**: 알림 판정(`detect_alerts`/`build_ladder`) 등에서
  실제 버그가 재발하면 그 함수만 `unittest` 로 고정(회귀 테스트), 또는 계산 규칙 변경 시 변경 함수부터
  테스트 작성 후 수정.
- 커밋 메시지: `type: 한글 요약 — 상세` 형식 (예: `refactor: ...`, `feat: ...`, `docs: ...`).
- 언어: 사용자 소통·문서는 한국어, 코드 식별자는 영어.
