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
  (ATH 대비 MDD 구간 매수 + 매수가 대비 스윙 목표 수익률 매도) 재구현. 📋 **운영 루틴은 README
  '스윙 알리미 운영 루틴' + NAMU_SWING_SETUP.md 참고** (매수 체결 시 LOTS에 계좌 매수가/수량 기록 →
  나무증권 매수감시/매도감시는 📡/🚀 임박 시점에 등록, 30일 창 — 체결은 나무증권 앱, 알림은 스윙 알리미).
  OneSignal 푸시는
  **전체 구독자(Subscribed Users = 내 기기) 대상**으로 발송(2026-08-12 단독 사용 전환 — 매도 신호 푸시는
  계좌별 **사이클당 1회**로 변경, 2026-08-13). 매도 신호를 보낸 계좌는 리셋(전 계좌 매도 완료 자동 리셋/수동
  --reset) 전까지 재발송하지 않는다.
  설정은 `swing_config.json`(사용자 소유 — 공용: 티커/구간/목표/푸시 설정), 상태는 `swing_state.json`
  (봇 전용 — ZONE_ALERTS/매도 플래그)로 분리 — 봇이 상태 파일만 커밋하므로 git 충돌로 알림 상태가
  유실되지 않는다.
  🔒 **개인 포지션 분리 (2026-08-10)**: 실제 매수가/보유수량은 공용 설정에 두지 않고
  `swing_personal.json`(사용자 소유, 봇 미기입)에만 기록한다. 세븐 스플릿 7개 계좌는 `LOTS`
  (계좌별 BUY_PRICE/SHARES) 구조로 개별 추적 — `_PERSONAL` 마커가 붙은 포지션의 매도 정보는
  **Discord 브리핑에서만** 숨긴다 ('매도 미설정' 표시 — 지인 노출 차단). **대시보드는 사용자 전용**이라
  계좌별 매수/매도 예정가와 매도 상태를 서버가 그대로 그려 넣고(2026-08-12 서버 렌더링 전환 — 2026-09-12
  매도 칩/알람 카운트도 개인 포함으로 정정), 매도 임박(🚀)/도달(🚨) 푸시도 전체 구독자(= 내 기기)에게
  발송한다. 콘솔에서는 🔒 개인 라벨로 계좌별 매도 목표를 확인한다. 매도 푸시는 태그 없이 서버 LOTS 매도
  목표로 전체 구독자에 발송된다 (2026-08-12 단독 사용 전환 — 태그 필터 제거).
  🚀 **매도 목표 임박 푸시 (2026-08-15)**: 나무증권 매도감시를 '목표 임박 시점'에 등록하는 루틴
  (NAMU_SWING_SETUP.md ②)의 신호가 되도록, 개인 포지션(_PERSONAL)의 매도 목표 임박(목표까지
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
  📐 **표시 기준도 라이브 세션으로 통일 (2026-09-22)**: 라이브 표시 중에는 가격뿐 아니라
  **하락률·남은 %p·전일 종가 비교 기준**도 라이브 세션에 맞춘다 — 하락률은 `_display_dd()`
  (라이브면 라이브 가격 기준, 아니면 `dd_pct`) 하나를 앱·콘솔·Discord 브리핑이 함께 쓰고,
  '전일 종가'는 `get_prior_close()`(확정 종가의 앞 세션)가 아니라 **라이브 세션의 직전 세션 =
  엔진 확정 종가**를 쓴다 (오버레이가 `prior_close`/`prior_close_date` 를 교체). 수정 전에는
  라이브 `$76.35` 옆에 종가 기준 `-17.5%`·`전일 종가 $71.38(09-17)` 이 찍혀 같은 화면이 스스로
  모순됐다. 대시보드 헤더는 `업데이트 … KST` + 표시 가격의 **종가 날짜**(`st['as_of']`,
  `_close_date()` 로 MM-DD 만 통과)를 적는다 — 생성 시각을 '종가 기준' 옆에 쓰지 말 것.
  ⚠️ 위 문단과 마찬가지로 **알림 판정은 종가 기준** 이다 (표시만 라이브) — `_display_dd` 를
  `detect_alerts`/래더 `hit`/매도 판정에 쓰지 말 것.
  🕒 **확정 종가 조회 — 일봉 미확정 시 분봉 폴백 (2026-09-22, LOC·스윙 공용 함수)**:
  `get_prev_close()` 는 yfinance 일봉이 아직 확정되지 않았을 때(마지막 봉 Close=NaN/미수록)
  마지막 **유효** 종가를 쓰면 하루 낡은 값을 '최신 종가'로 돌려준다 — 09-21 일봉 Close=NaN 으로
  09-18 종가 $72.64 를 현재가로 읽어 하락률과 LOC 매수가 기준이 어긋났다 (실제 09-21 종가 $78.92).
  이때 `info.previousClose` 는 **같은 낡은 값**이라 구제되지 않으므로, `_intraday_last_close()`
  (정규장 1분봉 마지막 종가 — prepost=False 라 애프터마켓 미포함)를 info 폴백보다 **먼저**
  시도한다 (휴장일 오탐 시엔 None → 기존 info 폴백 유지). 스윙의 `get_prior_close()` 도 같은
  함정이 있다 — `dropna()` 로 as_of 세션 행을 지우면 '전일 종가'가 한 세션 더 밀리므로
  (09-21 기준 09-17 $71.38), 날짜 검색은 dropna 전 이력으로 하고 직전 종가는 as_of **앞쪽**
  유효 종가에서 고른다. 두 함수 모두 LOC 브리핑(매수가 기준)과 공용이라 되돌리면 주문 가격까지 어긋난다.
  🔄 **사이클 자동 리셋 (2026-08-11)**: LOTS의 전 계좌가 매도 목표(`SWING_TARGET_PCT` — 현재 +25%)에 도달하면
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
  🛡 **대시보드에 매도감시 점검 블록을 다시 넣지 말 것 (2026-09-12 추가 → 당일 사용자 요청으로 삭제)**:
  카드에 계좌별 '등록 필요 시점'(지금 등록/매도 신호/보류)을 나열하는 블록을 넣었다가 "단순한 앱"을
  위해 삭제했다. 감시 점검은 **수동 체크리스트**(NAMU_SWING_SETUP.md)와 🚀 매도 임박 푸시·칩으로
  수행한다 (계좌별 매수/매도 예정가 행은 그대로 유지).
  🏦 **나무증권 이중 장치 — 체결 자동화 (2026-08-12)**: 스윙 알리미(푸시 알림 담당)와 나무증권
  앱(자동 체결 담당)을 병행한다. 나무증권은 해외주식(TQQQ) 목표가 푸시 알림(시세알림)이 미지원이므로
  **알림은 스윙 알리미, 체결은 나무증권 '해외주식 시세포착주문'**으로 역할을 나눈다. 감시 등록 기준:
  ① 매수감시 = 감시 조건 **하락**, 감시 가격 = 매수 구간 가격(예: 2차 -18% $71.36 — 3% 래더 전환 후),
  **등록 시점 = 📡 매수 구간 임박 푸시 확인 시** (30일 창이 구간 도달 직전과 겹치도록 — 매도감시 ②와 동일 원칙), 지정가+0틱, (계좌별 상세 가격/수량표는 `NAMU_SWING_SETUP.md` 참고)
  수량은 나무증권 '매수가능수량' 안내(현재가 기준)를 따름($500 예산 안전 — 목표가 기준 7주도 되지만
  체결가 변동 감안 6주 권장), 감시시간 **정규장만**(전략 판정이 정규장 종가 기준이라 알림·체결 시점 일치),
  감시기간 **30일**(만료 시 자동 해제 → 미체결이면 재등록 필요). ② 매도감시 = 감시 조건 **상승**, 감시 가격 = 매도 예정가(매수가 × SWING_TARGET_PCT, 예: 1번 $73.49×1.25=$91.86 — 나무증권 절사 표시도 $91.86, 절사/반올림 1센트 차이는 무시. 2026-08-31 +40%→+25% 전환으로 **+40% 기준으로 등록해 둔 감시($102.88)는 해제·재등록 필요**). **매도감시 등록은 매수 직후가 아니라 목표 임박(🚀, 대시보드, 목표까지 5%p 이내) 시점** — 30일 감시가 목표 도달(평균 8.3개월) 전에 만료되는 것 방지 (백테스트: 매수 후 30일 내 도달 7%뿐, 2026-08-15 점검). ③ 감시기간 30일 만료 후 재등록 — 매수는 🔻/📡 푸시, 매도는 🚀 임박(대시보드)이 신호 → 임박 시 등록, 미도달 만료 시 재임박 때 재등록, 도달 시 📈 푸시 → 수동 매도가 최종 폴백.  운영 루틴: **매수 체결 → 사용자가 봇에게 알림 → 사용자가(채팅에서 봇 도움 하에)
  `swing_personal.json`(LOTS) 기록 → 매도감시 등록(목표 임박 시)** — 엔진(swing_alerter.py)은 이 파일을 읽기만
  하고 절대 쓰지 않는다(기존 개인 포지션 분리 원칙 유지). 폰앱/웹앱 입력란은 표시용이므로 값 관리는
  `swing_personal.json` 한 곳뿐이며, 나무증권 감시(체결)와 스윙 알리미(알림)가 서로를 백업한다.
  🔒 코드 입력칸은 마스킹(password) 표시 — 눈 아이콘 토글로 잠시 확인 (어깨 너머 노출 방지, 2026-08-12).
  ⚠️ **봇은 `swing_dashboard.html`을 main에 커밋하지 않는다** — 생성 파일(헤더 시각 등)이 봇/사용자
  양쪽에서 재생성되어 git pull 충돌을 반복하므로, 워크플로우가 생성한 신선한 사본을 `gh-pages`에만
  배포한다 (`swing_alerter.yml` Sync 단계의 cp 참고 — 미추적 파일이라 `git checkout --` 금지, pathspec 오류).
  **아래 '스윙 봇(swing)' 제거 항목과 무관한 별개 기능**이며 혼동하지 말 것.
  📐 **백테스트 가격 기준 정렬 (2026-09-12)**: `swing_split_backtest.py` 가 배당 조정 종가
  (`auto_adjust=True`)로 ATH·구간가를 계산해 실전(원시 High ATH + 확정 종가)과 ATH 가 1.22%
  어긋났다 — 래더 전체 시프트, 10년 일별 트리거 판정 불일치 1.6~3.2% 일 (백테스트가 매수를 덜 잡음).
  `fetch_ohlc`(원시 Close/High)로 정렬해 실전과 1:1 로 맞췄다 (배당 현금 미반영 → 수익률은 보수적).
  재검증(10년·$500×7·수수료 0.1%): 3% 스텝 +342.8% > 5% +300.3% (구간 전환 유지) · +52% MDD -39.9%
  절벽 재현(+51% +385.1%/-31.9%) → 절벽 위 (+40%·+50%) 는 여전히 미채택. 목표는 config 값(현재 +25%)을
  `--target` 기본값이 swing_config.json 에서 읽어온다 (하드코딩 제거 — 2026-09-12).
  10년 재측정: **+25% 가 Sharpe 최적(0.85)**, 평균보유 109일·매도 86회 (+40% 는 +342.8%·Sharpe 0.79·
  보유 194일·매도 56회) — 2026-08-31 전환 근거가 새 기준에서도 유지된다. 구 기준 절대 수치
  (+304.6%/+371.5%/-43.7%)는 재현되지 않는다 (swing_config.json _NOTE 에 경위 기록).
  `loc_vs_swing_backtest.py` 도 같은 정렬 적용 — 스윙 ATH = 원시 High 누적 최고, 가격 계열은
  두 전략이 공통으로 **원시 종가/고가**(배당 미반영 — LOC_DCA_strategy `--backtest` 의 배당 조정
  종가와는 다름). 그 결과 최근 5년 기본 비교는 스윙 우위(+104.8% vs +103.7%)에서 **거의 동률
  (LOC +94.1% vs 스윙 +91.3%)** 로 뒤집혔다 — 결론이 기준 민감하다는 증거이므로 5년 우열로
  전략을 바꾸지 말 것 (10년은 LOC 우위 +2536% vs +1081%, 롤링 14윈도우는 9:5).
  🔻 **스윙 래더 크래시 재검증 (2026-09-12, 목표는 실전값 +25%)**: 크래시 직전 고점에서 시작하는
  5년 윈도우 5개(2018-10·2020-02 코로나·2021-11·2021-12·2022-08)에서 현행 **7계좌 × -15% 시작 ×
  3% 스텝이 평균 1위** (평균 +136%·최악 +99%; 2% +129%·4% +126%·5% +122%·10% +101%; 계좌 수는 7이
  1위 — 3계좌 +121%·최악 +63%, 5계좌 +130%·+84%, 10계좌 +123%·+97%) → 세븐 스플릿·3% 스텝 유지 타당.
  첫 구간 -18% 가 평균 동률(+136%)·최악 +111%로 근소 우세하나 2018·2020에선 -10~-15%가 우세해
  변경 근거로는 약함. 목표 수익률은 크래시 윈도우에서 +40%(평균 +143%)가 +25%(+136%)보다 높지만
  10년 Sharpe·보유일(위 항목)에서 +25%가 우세 — **회전율과 수익률의 맞교환**(2026-08-31 결정 유지).
  ⚠️ 모델 차이: `loc_vs_swing_backtest --sweep-zones`(무매도 축적)에서 깊은 래더가 좋아 보이는 것은
  실전 모델(매도 후 재매수)이 아님 — 래더 형상 판단은 `swing_split_backtest.py` 기준으로 할 것.
- **현재 전략 규칙 (2026-08-16 단일 논리 재구성 · 2026-08-17 20→5분할 전환)**: **순수 LOC 지정가 5분할 DCA**
  하나만 사용한다 — LOC 매수가 = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER), 사용자가 정규장에서 이 가격으로
  LOC 지정가 주문 (마감가 체결 — 장 마감가 ≤ 지정가일 때만 체결, 판정은 종가 기준,
  2026-08-17 수정 · 차수당 $10,000 코드 상수 × 최대 5차, 분할 수는 `LOC_DCA` 블록 설정 —
  백테스트 기본값용, 예산은 사용자 엑셀 단일 소스 — config에 BUY_AMOUNT 없음, 2026-08-17).
  전고점 표시 = 원시 고가(High, 미조정)·전체 이력 기준 — 전고점 계산은 스윙 알리미와
  **공용 함수** `LOC_DCA_strategy.get_all_time_high` 하나만 쓴다 (2026-09-12 수정·중복 제거;
  기존 '252일 종가 최고'는 차트의 실제 전고점보다 낮게 표시됨. 매수 판정(종가 기준)은 그대로).
  전일 종가(매수가 기준)도 **공용 함수** `LOC_DCA_strategy.get_prev_close` 하나만 쓴다 — 일봉이 미확정이면
  정규장 분봉 종가로 폴백해 하루 낡은 종가를 쓰지 않는다 (2026-09-22, 위 스윙 항목 참조 —
  이 폴백을 걷어내면 LOC 매수가가 실제 전일 종가 기준으로 계산되지 않는다). `--signal` 의
  `load_data()` 도 실시간 모드(`end` 미지정)에서 같은 폴백으로 마지막 확정 종가 행을 붙인다 —
  수정 전에는 기준일이 09-18·LOC $66.99 로 하루 낡았다(실제 09-21·$68.17). 백테스트는 `end` 고정
  재현성을 지키기 위해 제외한다 (10년 결과 +1317.0%/MDD -81.7% 재현 확인).
  5분할 채택 근거:
  `loc_vs_swing_backtest.py --sweep-splits` (2026-09-12 재측정 — 가격 계열 원시 종가/고가로 통일) —
  LOC 추천 국면(강세장)에서 분할 수가 적을수록
  평균 수익률이 높고(1분할 +404% > 5분할 +389% > 20분할 +298%), MDD는 분할 수와 무관(1~10분할 -75.8% 동일),
  실용 균형 5~10분할 중 5분할은 평균·크래시 윈도우 모두 5~10 구간 내 최고. 20분할은 고점에서 4개월 만에
  소진되는 약점이 드러나 폐기 (하락장 최적은 52분할이지 20분할이 아님). ⚠️ **체결 추적은 봇이 하지 않는다
  (2026-08-16)**: 체결 여부는 증권앱 확인 + **엑셀 컬러 표시**로 관리하며, 분할 예산/회차는 엑셀이
  단일 소스 — 브리핑은 LOC 매수가 하나만 제공한다 (자동 카운터는 실제 주문 여부를 모르므로 폐기).
  **매도 규칙 없음** — 순수 적립 전용.
  🔻 **주문 채널 = LS증권 (2026-09-12 기록)**: LOC 주문은 **LS증권**에서 TQQQ **LOC(장마감 지정가)**로
  접수한다 — 마감가 ≤ 지정가일 때 종가 체결이라 엔진 판정과 같은 규칙이고, 일반 지정가로 걸면 장중
  터치로 체결돼 판정(종가 기준)과 어긋난다. 스윙(나무증권 7계좌 × $500)과 **별개 장치·별개 자금**. 봇은 주문을 넣지 않는다 — 알림·가격만 제공. 운영 루틴은 README
  'LOC 5분할 운영 루틴 (LS증권)' 참고.
  ⚠️ 위 삭제는 **`LOC_DCA_strategy.py` 내부 로직 한정**이다 — MA 레짐 필터·RSI+볼륨·ATH_DCA 비상 모드·
  STAGE5·회복 재진입·실시간 모니터(`--ath-monitor`)는 전부 삭제(아래 제거 목록). 단 `MarketStageSystem.py`
  의 5단계 트래커(STAGE5 포함)는 **별개 기능으로 운영 중**이므로 이 문장을 근거로 지우지 말 것 (2026-09-20).
- **시장 단계 트래커**: `MarketStageSystem.py` (상태 `market_state.json` · WF `market_stage_tracker.yml`,
  평일 23:14 UTC) — `portfolio_config.json` 의 `POSITIONS` 티커마다 **하단/상단 5단계**를 판정해
  Discord 로 보고하고 상태 파일만 커밋한다(2026-09-20 아키텍처에 추가 — 이전 누락으로 삭제 오해 위험이 있었다).
  하단 = 매도세 소진(5봉 하락·횡보 + 변동폭 임계 이하) → 재테스트(저점 근접 + 거래량 감소) →
  트랩(전일 저점 이탈 후 회복) → 추세 전환(20일 고점 돌파 + 거래량 1.4배) → 5단계(🔥 최종 매수: 거래량 1.75배 + MA 정배열),
  상단 = 과열(RSI 60 상회 후 하회) → 다이버전스(신고가 + MACD 데드크로스) → 밴드 트랩(볼린저 상단 이탈 후 복귀) →
  분산(거래량 1.4배 + 상승 정체) → 5단계(🔻 최종 매도: 하락 + 거래량 1.75배 + MA 하락 정렬).
  지표 = RSI(14)·MACD(12/26/9)·볼린저(20, 2σ)·거래량 MA20(전일 기준)·MA5/20/60 정렬, 데이터 = yfinance 일봉 6개월.
  ⚠️ 고갈(exhaustion) 임계는 **TQQQ 0.16 / 그 외 0.10** — 티커별 분기라 config 가 아니라 코드에 있다.
  ⚠️ 단계는 **증가만** 하고 5단계 진입 후 `STAGE5_RESET_DAYS`(30일)이 지나면 0으로 자동 리셋된다 —
  리셋이 없으면 5단계가 영구 고정되므로 이 리셋을 지우지 말 것. 상태는 원자적 쓰기(temp → move)로 저장한다.
- **약세 조기경보**: `bear_market_signals.py` (리포트 `signal_report.json` · CAPE 캐시 `cape_cache.json` ·
  WF `bear_market_signals.yml`(리포트와 함께 `cape_cache.json` 도 커밋 — 러너가 일회성이라 커밋해야
  폴백 캐시가 낡지 않는다), 평일 23:00 UTC) — 7개 지표를 **선행 그룹(고점 경고, 0~6점)** 과
  **확인 그룹(하락 진행, 0~8점)** 으로 나눠 점수화하고, 두 합으로 시장 국면을 판정해 **LOC_DCA / 스윙 중
  유리한 쪽**을 Discord 로 알린다. 선행 = 금리 커브 · Fed 정책 사이클 · 밸류에이션(CAPE),
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
  (참고: 2026-08-08 신규 추가된 `swing_alerter.py` 스윙 알리미는 이 스윙 봇과 무관한 별개 기능이며
  재도입 금지 대상이 아니다.)
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
  ⚠️ 이 삭제는 **LOC 엔진(`LOC_DCA_strategy.py`) 내부 한정** — `MarketStageSystem.py` 의 하단/상단 5단계
  트래커(STAGE5 포함)는 별개 기능으로 실운영 중이다 (2026-09-20 범위 명시 — 혼동 금지).
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
  (33.3%) 모델이라 현재 실전(순수 LOC 5분할 σ / 스윙 7계좌 3% 스텝)과 설계가 어긋난다.
  대상도 SOXL·PLTR 로 현재 전략(둘 다 TQQQ 전용) 밖이고, 코드·워크플로우·문서 어디에서도
  참조되지 않던 고아 파일이었다(마지막 변경 2026-08-31 — README 파일 표에 없던 유일한 .py).
  💡 리서치 결론만 남긴다 — **hard limit = 실측 최대낙폭 + 버퍼**: SOXL -93.0%(실측 -90.5%,
  2022-10) · PLTR -88.0%(실측 -84.6%, 2020 상장 이후) · 기본 -75.0%.
  레벨 예: SOXL strong_bull [-18,-30,-50] / extreme [-48,-62,-80],
  PLTR strong_bull [-12,-22,-35] / extreme [-35,-50,-65].
  복원: `git log --diff-filter=D -- mdd_optimizer.py` 로 삭제 커밋을 찾고
  `git checkout <삭제 커밋>^ -- mdd_optimizer.py mdd_config.json`.
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
- **가격 기준 회귀 테스트 (2026-09-22)**: 2026-09-22 에 난 가격 기준 버그(일봉 미확정 분봉 폴백 ·
  `get_prior_close` dropna 함정 · `--signal` 낡은 세션 · 라이브 표시 기준 · 헤더 종가 날짜 ·
  알림 판정 종가 기준)는 `test_price_basis.py` 에 고정해 두었다 —
  `python3 -m unittest -q test_price_basis` (네트워크 0회, yfinance 는 mock, 0.01초).
  가격 조회(`get_prev_close`/`get_prior_close`/`load_data`)나 표시 기준(`_display_dd`/`_close_date`),
  알림 판정을 건드렸으면 이 테스트를 먼저 통과시킬 것 (수정 전 코드에선 8/11 이 실패해 실제 회귀를 잡는다 —
  2026-09-22 확인). JS 게이트(`npm run typecheck`)와 별개로 동작하며 워크플로우에는 걸려 있지 않다.
- 커밋 메시지: `type: 한글 요약 — 상세` 형식 (예: `refactor: ...`, `feat: ...`, `docs: ...`).
- 언어: 사용자 소통·문서는 한국어, 코드 식별자는 영어.
