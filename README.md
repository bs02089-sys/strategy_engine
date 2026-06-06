# 📈 Sigma DCA Position Manager

통계적 시그마를 활용해 AIQ · SOXX · SOXL의 LOC 매수 타점을 계산하고, Discord로 매일 브리핑을 전송하는 개인 운용 자동화 시스템입니다. 복잡한 외부 지표를 제거하고, '확률 분포 기반 저점 분할 매수'라는 본질에 집중합니다.

---

## 🗂️ 프로젝트 파일 구성

| 파일명 | 역할 | 편집 정책 |
| :--- | :--- | :--- |
| `sigma_position_manager.py` | LOC 타점 계산 및 Discord 브리핑 엔진 | 수정 금지 |
| `fx_alert.py` | USD/KRW 환율 모니터링 및 알림 | 별도 워크플로 실행 |
| `config.json` | 시스템 설정 및 매매 파라미터 | 시스템 자동 업데이트 — 직접 수정 

---

## 🎯 포트폴리오 구성 (3층 구조)

| 티커 | 설명 | 비중 | ENTRY_MULTIPLIER | DAILY_SIGMA | 갱신 주기 |
| :--- | :--- | :---: | :---: | :---: | :---: |
| `SOXL` | 반도체 3× 레버리지 ETF (수익 엔진) | 50% | 1.5 | 0.074649 | 365일 |
| `SOXX` | 반도체 ETF (안정 코어) | 30% | 1.3 | 0.02507 | 365일 |
| `AIQ` | AI·로보틱스 ETF (테마 확장) | 20% | 1.45 | 0.02653 | 90일 |

> DAILY_SIGMA는 종목별 LOOKBACK_DAYS 주기마다 yfinance 데이터로 자동 갱신됩니다.

---

## ⚙️ 핵심 운용 원칙

1. **매수 철학:** 전일 종가 대비 N 시그마 하락 지점을 LOC 주문 타점으로 삼습니다.
2. **기계적 실행:** 매일 장 개시 전 Discord 브리핑을 확인하고, LOC 예정가를 증권 앱에 등록 후 대기합니다.
3. **주간 거래 금지:** 정규장 LOC 매수만 실행합니다. 주간 거래는 일시적 과매도로 정규장 종가보다 불리한 경우가 많습니다.
4. **분할 매수:** 1회 신호당 1회분씩만 매수합니다. 몰빵은 금지입니다.
5. **시그마 갱신:** LOOKBACK_DAYS 경과 시 해당 기간 일봉 데이터로 DAILY_SIGMA를 자동 재산출합니다.
6. **단순 매매:** 익절 규칙 없이 저점 매수를 통해 반도체 슈퍼사이클 상승분을 온전히 향유합니다.

---

## 🧮 LOC 타점 계산 공식

```
LOC 타점 = 전일 종가 × exp(−ENTRY_MULTIPLIER × DAILY_SIGMA)
```

예시 (SOXL, 전일 종가 $182.54):
```
$182.54 × exp(−1.5 × 0.074649) ≈ $163.20
```

---

## 📅 정기 운용 프로토콜

### 매일 루틴 (GitHub Actions 자동)
- 장 개시 전 `sigma_position_manager.py` 자동 실행
- Discord 브리핑으로 각 종목의 전일 종가 · LOC 예정가 확인
- LOC 예정가를 증권 앱에 등록 후 대기 → 정규장에서 기계적 체결

### 월말 루틴 (GitHub Actions 자동)
- `SOXL_VIX_SIGMA_BACKTEST.py` — 백테스트로 전략 유효성 검증
- `optimize_strategy.py` (Hermes) — Gemini API 기반 AI 월간 전략 리뷰

---

## 🔧 config.json 주요 키

> ⚠️ 직접 수정 금지: 모든 파라미터 변경은 스크립트를 통해 이루어집니다.

| 키 | 설명 | 갱신 주기 |
| :--- | :--- | :--- |
| `POSITIONS.{ticker}.ENTRY_MULTIPLIER` | LOC 계산 배수 | 고정 |
| `POSITIONS.{ticker}.DAILY_SIGMA` | 일별 변동성 시그마 | LOOKBACK_DAYS마다 자동 |
| `POSITIONS.{ticker}.LAST_SIGMA_UPDATE` | 시그마 최근 갱신일 | LOOKBACK_DAYS마다 자동 |
| `POSITIONS.{ticker}.LOOKBACK_DAYS` | 시그마 갱신 주기 및 데이터 기간 | 고정 |
| `STRATEGY` | 전략 파라미터 (주기, 기간 등) | 참조용 |
| `LAST_MONTHLY_PING` | 월초 핑 중복 방지용 날짜 | 월 1회 자동 |
| `exchange_status` | USD/KRW 환율 상태 (fx_alert.py 관리) | fx_alert.py 자동 |
| `news_settings` | 뉴스 키워드 설정 | 수동 조정 가능 |

---

## 🔐 환경 변수 (GitHub Secrets)

| 변수명 | 설명 |
| :--- | :--- |
| `DISCORD_WEBHOOK` | Discord 웹훅 URL |
| `DISCORD_USER_ID` | Discord 멘션 대상 사용자 ID |
| `GEMINI_API_KEY` | Hermes(AI 리뷰) 전용 — 로컬 환경변수 |

---

## 📊 Discord 브리핑 예시

```
🌙 2026-06-06 07:00 EDT

🔹 AIQ
• 전일 종가: $62.50  |  LOC: $60.14

🔹 SOXX
• 전일 종가: $539.77  |  LOC: $522.46

🔹 SOXL
• 전일 종가: $182.54  |  LOC: $163.20
```

---

## ⚠️ 주의사항

- 본 시스템은 반도체 슈퍼사이클 장기 우상향을 전제로 수량을 늘려가는 적립식 전략입니다.
- SOXL은 3× 레버리지 상품으로 변동성 감쇄(volatility decay) 위험이 있습니다.
- 최종 매매 판단은 항상 본인이 합니다. 시스템은 전략을 기계적으로 실행하는 도구일 뿐입니다.

---

**운용 철학**: "시장의 소음은 제거하고, 오직 통계적 타점만을 따라간다."