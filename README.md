# SOXL_sigma_alert

📉 **SOXL 1시그마/2시그마 기반 매수 신호 알림 시스템**
이 프로젝트는 `MASSIVE` 데이터를 활용하여 SOXL 종목의 **변동성 기반 매수 신호**를 체크하고, 결과를 **콘솔 출력 및 디스코드 알림(Webhook)**으로 전송합니다. 또한 낚시 횟수와 프리마켓 체결 기록을 JSON 파일(`config.json`)로 관리하며, 깃허브 Actions 자동화를 통해 매일 장 마감 후 자동 실행됩니다.

## 🚀 주요 기능
- SOXL 종목의 최근 **40일 또는 1년 시그마** 계산 (불마켓/베어마켓 상황에 따라 적용)
- 1σ, 2σ 기반 매수 조건 충족 여부 체크
- 낚시 횟수 및 프리마켓 체결 기록을 JSON 파일로 관리
- 결과를 콘솔과 디스코드 알림(Webhook)으로 전송
- 깃허브 Actions 자동화로 매일 장 마감 후 자동 실행 및 `config.json` 업데이트

## 📦 설치 및 실행 방법
저장소를 클론한 뒤 필요한 패키지를 설치하고 환경 변수를 설정하면 바로 실행할 수 있습니다:

```bash
# 저장소 클론
git clone https://github.com/your-username/your-repo.git
cd your-repo

# 패키지 설치
pip install -r requirements.txt

# 환경 변수 설정 (.env 파일 생성)
echo "DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxxx/yyyy".env
echo "MASSIVE_API_KEY=your_api_key_here".env

# 실행
python SOXL_sigma_alert.py