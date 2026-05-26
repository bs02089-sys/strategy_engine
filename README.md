# 📈 VIX Sigma Position Manager

**VIX 지수 + 로그 정규분포 시그마 기반 미국 주식 자동 매매 알림 및 자동 정산 마스터 가이드**

정규장 개시 후 VIX 공포 지수와 90일 로그수익률 표준편차(Daily Sigma)를 실시간으로 분석하여 최적 매수 타점을 계산하고 Discord로 알림을 전송하는 자동화 봇입니다. 

특히, 실전 체결 팩트를 ledger.json에만 기록하면 가중평균 평단가, 누적 수량, 올해 집행 횟수 등을 파이썬 매매 엔진이 연산하여 config.json에 자동으로 100% 반영하는 "단일 장부 자동 정산 시스템" 구축이 완료되었습니다.

---

## 🗂️ 프로젝트 파일 시스템 구성도

| 파일명 | 역할 | 직접 편집 여부 |
| :--- | :--- | :--- |
| sigma_position_manager.py | 실전 매매 분석 및 알림 엔진 | ❌ 절대 금지 (엔진 코어) |
| SOXL_VIX_SIGMA_BACKTEST.py | 4년 통계 기반 배수 최적화 | ✅ 범위 설정 시에만 편집 |
| optimize_strategy.py | Hermes AI 월간 복기 엔진 | 필요 시에만 편집 |
| config.json | 시스템 설정 및 포지션 저장소 | ❌ 수동 편집 절대 금지 |
| ledger.json | 실전 체결 팩트 기록 장부 | ✅ 체결 시 수동 입력      |
| `README.md` | 본 마스터 운용 가이드 및 전략 관제탑 | ❌ 수동 편집 금지(시스템 변경 시에만) |

> ⚠️ [경고] config.json은 직접 편집하지 마십시오!
> 새로 구축된 자동화 정산 레이어에 의해 ledger.json에 기록된 실전 팩트와 백테스트/AI 엔진 결과가 연산되어 config.json을 자동으로 100% 갱신합니다.

---

## ⚙️ 주요 기능

| 기능 | 설명 |
| :--- | :--- |
| VIX 3구간 분류 | 안정 / 공포 / 극단적 공포에 따라 매수 배수 자동 전환 |
| 갭 하락 구간별 보정 | 시가 갭 하락 폭에 따라 타점을 시가 가까이 자동 조정 |
| 시간 가드 | 직전 매수일 기준 최소 대기일(5일 or 14일) 이후에만 매수 허용 (타점 도달 시 강제 해제) |
| LONG / SHORT 이중 모드 | 종목별로 장기 적립(LONG) 또는 단기 타격(SHORT) 전략 독립 운용 |
| 3단계 분할 매도 계획 | SHORT 모드 보유 종목에 대해 +0.85σ / +1.95σ / +2.40σ 자동 계획 수립 |
| Discord 알림 전송 | 분석 결과를 Discord Webhook으로 자동 전송 |
| 단일 장부 자동 정산 | ledger.json 기록 시 평단가 및 수량을 소수점 4자리까지 자동 계산 후 config.json 동기화 |
| Git 자동 동기화 | 파라미터 최적화 및 복기 완료 시 자동 커밋 & 푸시 (GitHub Actions 지원) |

---

## 🧮 핵심 알고리즘

### 매수 타점 계산 (LONG 모드)

타점 = 기준가 * exp(-Daily_Sigma * VIX_배수)

- 기준가 : 장중이면 당일 시가, 장전이면 전일 종가
- 일간 변동성 (Daily_Sigma) : 최근 90일간의 로그수익률 표준편차 (ddof=1)
- VIX 배수 : config.json에서 로드되는 시장 국면별 동적 가중치

### VIX 3구간 배수 (기본값, 월간/연간 백테스트로 자동 갱신)

| VIX 구간 | 배수 | 장세 |
| :--- | :--- | :--- |
| 20 미만 | 0.85x | ✨ 평시 안정 |
| 20 ~ 30 | 1.95x | 🔴 공포 |
| 30 이상 | 2.40x | 🔴🔴 극단적 공포 |

### 갭 하락 구간별 배수 보정 (LONG 모드)

| 갭 하락 폭 | 적용 배수 |
| :--- | :--- |
| 0% ~ -3% | 원래 배수 유지 |
| -3% ~ -5% | 0.45 |
| -5% ~ -7% | 0.25 |
| -7% ~ -10% | 0.10 |
| -10% 초과 | 0.0 (시가 바로 매수) |

> 💡 갭 하락이 클수록 이미 당일 가격 조정을 크게 받은 것이므로, 배수를 대폭 줄여 타점을 시가 근처로 강하게 끌어당깁니다.

### 시간 가드 (LONG 모드)

| 조건 | 최소 대기일 |
| :--- | :--- |
| 20일 변동성 <= 정상 기준 * 1.3 | 5일 |
| 20일 변동성 > 정상 기준 * 1.3 (고변동성) | 14일 |

> 💡 직전 매수일로부터 최소 대기일이 지나야 다음 매수가 허용됩니다. 단, 실시간 가격이 계산된 최적 타점에 도달할 경우 시간 가드를 즉시 강제 해제하고 매수를 집행합니다.

---

## 🛠️ 설치 및 환경 세팅

### 1. 패키지 설치
pip install yfinance pandas numpy requests pytz holidays

### 2. config.json 기본 뼈대 구성
{
  "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/...",
  "DISCORD_USER_ID": "your_user_id",
  "TICKERS": ["SOXL", "TSLA"],
  "POSITIONS": {
    "SOXL": {
      "MODE": "LONG",
      "TOTAL_SHARES": 34,
      "MY_AVG_PRICE": 165.11,
      "CURRENT_CASTS": 4,
      "ANNUAL_QUOTA": 24,
      "LAST_CAST_DATE": "2026-05-15"
    }
  },
  "VIX_CONFIG": {
    "LONG": {
      "LEVEL_LOW": 20.0,
      "LEVEL_HIGH": 30.0,
      "MULT_NORMAL": 0.85,
      "MULT_FEAR": 1.95,
      "MULT_EXTREME": 2.40
    }
  }
}

---

## 📅 타임라인별 운영 프로토콜 (Operating Protocols)

### 📅 매일 (정규장 개시 전후)
1. 밤 11시 30분 이후 sigma_position_manager.py를 실행합니다. (또는 스케줄러 자동화)
   * ※ 서머타임 해제 시에는 밤 11시 30분, 서머타임 적용 시에는 밤 10시 30분 개장 후 30분 경과 시점이 기준입니다.
2. Discord 채널로 전송된 당일 매수 예정가 및 퀀트 가이드 메시지를 확인합니다.
3. 시장 실시간 가격이 타점 이하로 진입 시, 증권사 앱을 통해 직접 매수를 집행합니다.
   * ※ 주의: 봇은 정량 데이터만 계산할 뿐, 최종 투자 집행은 운용자(사람)가 판단합니다.

### 🛒 매수 집행 직후 (★ 실전 운용 핵심)
매수가 체결되면 즉시 장부(ledger.json)에 체결 데이터를 입력합니다. 파이썬 엔진이 실행될 때 이 장부를 1순위로 읽고, 가중평균 평단가(MY_AVG_PRICE), 누적 수량(TOTAL_SHARES), 사용 날짜(LAST_CAST_DATE)를 자동으로 연산하여, config.json에 완벽하게 덮어씁니다. ("누워서 코풀기" 정산 레이어)

* ledger.json 기록 규격 (LONG 매수 예시)
{
  "SOXL": {
    "action": "BUY",
    "mode": "LONG",
    "buy_target": 158.23,
    "qty": 2,
    "current_casts": 5,
    "date": "2026-05-26"
  }
}

### 📅 매월 말일 (동적 롤링 재최적화)
LONG 모드 변동성은 '90일 롤링 수익률'을 추적하므로, 시장 변화에 뒤처지지 않기 위해 매월 마지막 주 금요일(디스코드 생존 핑 발송 시점) 백테스트 및 복기 파이프라인을 작동시킵니다.

* [STEP 1] 통계 기반 배수 최적화
    * 명령어 : python SOXL_VIX_SIGMA_BACKTEST.py
    * 설명 : 당월 시장 변동성을 반영한 최적 VIX 배수 조합 탐색
    * 조치 : 성과(수익률, MDD, Calmar) 확인 후 y 입력 시 config.json 자동 갱신
* [STEP 2] AI 월간 복기 및 튜닝
    * 명령어 : python optimize_strategy.py
    * 설명 : Hermes AI가 ledger.json을 정밀 분석 후 거시 국면 평가
    * 조치 : 결과 만족 시 y 입력 시 미세조정 값 반영 + GitHub 자동 동기화

> ⚠️ 원칙 : 과적합(Overfitting) 방지를 위해 AI 미세조정 시 한 달에 2개 이상의 변수 변경을 엄격히 금지합니다.

### 📅 매년 1월 초 (연간 거시 점검)
월간 루틴과 별개로 연초에는 백테스트 파일(SOXL_VIX_SIGMA_BACKTEST.py) 내부의 코드를 열어, 거시 원칙들을 추가 검토 및 수정합니다.
* CHECK 1 : SAFETY_BOUNDS 탐색 범위가 시대 흐름에 맞는지 재설정
* CHECK 2 : 홀딩 필터 및 타깃 매수 횟수 필터(170~240회) 전략 일치성 검증
* CHECK 3 : 증권사 우대 수수료 변동 여부 확인 후 FEES 값 최신화
> 💡 연간 점검은 백테스트 파일(SOXL_VIX_SIGMA_BACKTEST.py)만 수정하면 됩니다. config.json은 직접 편집하지 않습니다.

---

## ⚙️ 시스템 제어 규칙 및 가이드라인 (System Rules)

### 📊 수정이 필요한 상황별 작업 위치

| 상황 | 수정 파일 |
| :--- | :--- |
| 탐색 범위 변경 (MULT_NORMAL 등) | SOXL_VIX_SIGMA_BACKTEST.py |
| 수수료율 변경 (FEES) | SOXL_VIX_SIGMA_BACKTEST.py |
| 홀딩 기간 변경 (HOLD_DAYS) | SOXL_VIX_SIGMA_BACKTEST.py |
| 매수 횟수 필터 변경 (170~240회) | SOXL_VIX_SIGMA_BACKTEST.py |
| AI 프롬프트 수정 | optimize_strategy.py |
| AI 분석 저장 로직 수정 | optimize_strategy.py |
| Discord / Git 설정 | 환경변수 또는 GitHub Secrets |
| 실제 운용 설정값 확인 | config.json (읽기 전용으로 참고) |

### 📊 SAFETY_BOUNDS 핵심 설계 원칙
백테스트 파일 내의 파라미터 탐색 범위는 수학적/심리적 하방 붕괴를 막기 위해 아래의 엄격한 가이드라인에서만 움직여야 합니다.

* MULT_NORMAL : 0.65 ~ 0.85 (평시 시장 : VIX 20 미만)
* MULT_FEAR : 1.95 ~ 2.45 (공포 시장 : VIX 20 ~ 30)
* MULT_EXTREME : 2.50 ~ 2.75 (극단 공포 : VIX 30 이상)

> ⚠️ [필수 논리 고수]
> "극단 공포 배수 > 공포 배수" 원칙을 보장하기 위해 MULT_EXTREME 하한(2.50)은 반드시 MULT_FEAR 상한(2.45)보다 크게 설정해야 시스템 역전 현상이 발생하지 않습니다.
> (※ config.json의 초기 배수는 백테스트 최초 실행 시 자동으로 보정 범위로 갱신됩니다.)

### 🔒 환경 변수 및 보안 프로토콜
시스템 크래시 및 API 탈취 방지를 위해 시크릿 키는 절대 코드 파일에 하드코딩을 하지 않습니다.

* GEMINI_API_KEY : 로컬 PC 시스템 환경변수에 등록하여 사용
* DISCORD_WEBHOOK / USER_ID : GitHub 가상 서버 운용 시 GitHub Secrets에 등록

> ⚠️ [GitHub Actions 배포 주의사항]
> 가상 워크플로우(.yml) 환경에서 Git push 인증 시 외부 PAT(개인 토큰)를 URL에 직접 삽입하면, 인코딩 에러로 서버 push가 무한 실패할 수 있습니다. 안전을 위해 반드시 GitHub 표준 내장 토큰인 ${{ github.token }} 아키텍처를 고수하십시오.

* 표준 배포 스크립트 가이드
- uses: actions/checkout@v4
  with:
    token: ${{ github.token }}
- run: git remote set-url origin "https://github.com/${{ github.repository }}.git"
- run: git push

---

## 📡 Discord 알림 예시

=== 🎯 매매엔진 통합 리포트 (🚀 실시간 모드) ===
🎬 ✨ VIX 안정 (18.3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
● 종목 : SOXL [📈 LONG (장기 적립)] (보유량 : 34주)
● 전일 종가 : $173.20
● VIX 상태 : ✨ VIX 안정 ➔ 0.85배수 하방
🛒 [매수 예정가] : $164.85
-----------------------------------------
📊 일간 평균 변동성 : ±5.23%
💡 적용 배수     : 0.85x
⚙️ 타임 엔진     : 🟢 [시간 가드 해제] 자유 매수 가능
📊 집행 현황     : 4/24회
🍏 평단가         : $165.11
⏰ 통합 분석 관제탑 시각: 2026-05-26 23:35:00

---

## 📄 라이선스

BSU License