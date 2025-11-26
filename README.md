# USD/KRW 2σ Dollar Buy Alert

## 📖 프로젝트 소개
달러 매수 타이밍을 알려주는 간단한 봇입니다.  
매일 아침 실행하면 **"담아!"** 또는 **"대기"** 메시지를 귀엽게 알려줍니다.  

> "환율이 비쌀 때 미국 주식 사기 싫다 → 그럼 달러를 미리 싸게 담아놓자!"  
> 라는 한국 투자자들의 고민에서 출발한 프로젝트입니다.

---

## 🇰🇷 한국어 버전 (감성 100%)
- **달러담으러.py** → 한국어 감성 버전  
  - 매일 실행 시 "담아!" vs "대기" 알림 제공  
  - 개인용, 귀여운 알림 스타일

---

## 🌍 English / Production Version
- **[`dollar_buy_alert.py`](dollar_buy_alert.py)** → Production-ready version  
  - GitHub Actions + Discord webhook + `.env` 지원  
  - 매일 자동으로 디스코드 알림 전송  
  - **20일 이동평균 ± 2σ 밴드 하향 돌파 시 `@everyone` 알림**

---

## 📊 왜 2σ 밴드인가?
- 통계적으로 **평균 ± 2σ 범위**는 약 95%의 데이터가 포함되는 구간  
- 환율이 하단 밴드를 돌파하면 **과매도 구간 → 매수 기회**로 해석  
- 단기 변동성을 반영해 **정기 적립식 매수 전략**에 적합

---

## ⚙️ 실행 방법
1. `.env` 파일에 Discord Webhook URL 설정:
   ```env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx/yyyy
