# BITU_SOXL_sigma_alert

📉 ** BITU_SOXL 1시그마/2시그마 기반 매수 신호 알림 시스템**

이 프로젝트는 `yfinance` 데이터를 활용하여 BITU, SOXL 종목의 **1σ와 2σ 변동성 기준 매수 신호**를 체크하고, 결과를 **콘솔 출력 및 디스코드 알림(Webhook)**으로 전송합니다.

## 🚀 주요 기능
- BITU, SOXL 종목의 최근 1년 1σ와 2σ 계산
- 1σ, 2σ 기반 매수 조건 충족 여부 체크
- 최근 1년 거래 횟수 집계
- 결과를 콘솔과 디스코드 알림으로 전송

## 📦 설치 방법

1. 저장소 클론
   ```bash
   git clone https://github.com/your-username/your-repo.git
   cd your-repo

2. 패키지 설치
   pip install -r requirements.txt

3. 환경 변수 설정
   루트 디렉토리에 .env 파일을 생성하고 아래 내용을 추가:
   DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxxx/yyyy

4. 실행 방법
   python BITU_SOXL_sigma_alert.py
