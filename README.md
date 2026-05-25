# 📈 VIX Sigma Position Manager

**VIX 지수 + 로그 정규분포 시그마 기반 미국 주식 자동 매매 알림 엔진**

정규장 개시 후 VIX 공포 지수와 90일 로그수익률 표준편차(Daily Sigma)를 실시간으로 분석하여,  
최적 매수 타점을 계산하고 Discord로 알림을 전송하는 자동화 봇입니다.

---

## 🗂️ 프로젝트 구조

```
strategy_engine/
├── sigma_position_manager.py   ← 핵심 매매 엔진 (본 파일)
├── config.json                 ← 전략 설정 파일 (수동 관리)
├── SOXL_VIX_SIGMA_BACKTEST.py  ← 백테스트 & 최적 배수 탐색
└── optimize_strategy.py        ← VIX 구간별 최적 배수 역산
```

---

## ⚙️ 주요 기능

| 기능 | 설명 |
|------|------|
| **VIX 3구간 분류** | 안정 / 공포 / 극단적 공포에 따라 매수 배수 자동 전환 |
| **갭 하락 구간별 보정** | 시가 갭 하락 폭에 따라 타점을 시가 가까이 자동 조정 |
| **시간 가드** | 직전 매수일 기준 최소 대기일(5일 or 14일) 이후에만 매수 허용 |
| **LONG / SHORT 이중 모드** | 종목별로 장기 적립(LONG) 또는 단기 타격(SHORT) 전략 독립 운용 |
| **3단계 분할 매도 계획** | SHORT 모드 보유 종목에 대해 +0.85σ / +1.95σ / +2.40σ 자동 계획 수립 |
| **Discord 알림 전송** | 분석 결과를 Discord Webhook으로 자동 전송 |
| **Git 자동 동기화** | config.json 변경 시 자동 커밋 & 푸시 (GitHub Actions 지원) |

---

## 🧮 핵심 알고리즘

### 매수 타점 계산 (LONG 모드)
```
타점 = 기준가 × exp(−Daily_Sigma × VIX_배수)
```
- **기준가** : 장중이면 당일 시가, 장전이면 전일 종가
- **Daily_Sigma** : 90일 로그수익률 표준편차 (ddof=1)
- **VIX_배수** : config.json의 `VIX_CONFIG`에서 로드

### VIX 3구간 배수 (기본값, config.json에서 변경 가능)

| VIX 구간 | 배수 | 장세 |
|----------|------|------|
| 20 미만 | 0.85x | ✨ 평시 안정 |
| 20 ~ 30 | 1.95x | 🔴 공포 |
| 30 이상 | 2.40x | 🔴🔴 극단적 공포 |

### 갭 하락 구간별 배수 보정 (LONG 모드)

| 갭 하락 폭 | 적용 배수 |
|-----------|----------|
| 0% ~ -3% | 원래 배수 유지 |
| -3% ~ -5% | 0.45 |
| -5% ~ -7% | 0.25 |
| -7% ~ -10% | 0.10 |
| -10% 초과 | 0.0 (시가 바로 아래) |

> 갭 하락이 클수록 이미 많이 내려온 것이므로 배수를 줄여 타점을 시가 가까이 당깁니다.

### 시간 가드 (LONG 모드)

| 조건 | 최소 대기일 |
|------|------------|
| 20일 변동성 ≤ 정상 기준 × 1.3 | 5일 |
| 20일 변동성 > 정상 기준 × 1.3 (고변동성) | 14일 |

> 직전 매수일(`LAST_CAST_DATE`)로부터 최소 대기일이 지나야 다음 매수 허용.  
> 단, 타점 도달 시 시간 가드를 강제 해제하고 즉시 집행.

---

## 🛠️ 설치 및 실행

### 1. 패키지 설치
```bash
pip install yfinance pandas numpy requests pytz holidays
```

### 2. config.json 설정
```json
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
    },
    "TSLA": {
      "MODE": "SHORT",
      "TOTAL_SHARES": 10,
      "MY_AVG_PRICE": 321.20
    }
  },
  "VIX_CONFIG": {
    "LONG": {
      "LEVEL_LOW": 20.0,
      "LEVEL_HIGH": 30.0,
      "MULT_NORMAL": 0.85,
      "MULT_FEAR": 1.95,
      "MULT_EXTREME": 2.40
    },
    "SHORT": {
      "LEVEL_LOW": 20.0,
      "LEVEL_HIGH": 30.0,
      "MULT_NORMAL": 0.85,
      "MULT_FEAR": 1.95,
      "MULT_EXTREME": 2.40
    }
  }
}
```

### 3. 실행
```bash
python sigma_position_manager.py
```

> **권장 실행 시각** : 한국 시간 기준 밤 **11시 30분 이후**  
> (미국 동부 시간 오전 9시 30분 개장 후 30분 경과 → 시가 확정 및 초반 변동성 안정)

---

## 📋 config.json 수동 관리 항목

매수 집행 후 아래 두 항목을 **반드시 수동으로 업데이트**하세요.  
봇은 타점 계산만 하며, 실제 매수 여부는 사람이 직접 판단합니다.

| 항목 | 설명 | 예시 |
|------|------|------|
| `LAST_CAST_DATE` | 가장 최근 매수 집행일 | `"2026-05-20"` |
| `CURRENT_CASTS` | 올해 누적 매수 횟수 | `5` |

---

## 📊 백테스트 & 배수 최적화

### 최적 배수 탐색
```bash
python SOXL_VIX_SIGMA_BACKTEST.py
```
- 4년치 SOXL 데이터 기반 Calmar Ratio 최적화
- 안전 범위 내에서 최적 배수 조합을 자동 탐색
- 결과를 `config.json`에 그대로 적용 가능

### VIX 구간별 황금 배수 역산
```bash
python vix_multiplier_optimizer.py
```
- 3년치 데이터 기반 구간별 최적 배수 역산
- 5거래일 청산 기준 승률 및 프로핏 팩터 산출

---

## 📡 Discord 알림 예시

```
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
⏰ 통합 분석 관제탑 시각: 2026-05-20 23:35:00
```

---

## ⚠️ 주의사항

- 본 코드는 **매수 알림 도구**이며, 증권사 API와 연동된 자동 주문 기능은 없습니다.
- 투자 판단 및 실제 매수는 **사용자 본인의 책임** 하에 이루어집니다.
- `config.json`에 Discord Webhook URL 등 민감 정보가 포함되므로 **`.gitignore`에 추가**하거나 GitHub Secrets로 관리하세요.

---

## 📄 라이선스

MIT License