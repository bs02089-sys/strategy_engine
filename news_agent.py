"""
글로벌 시장 뉴스 수집 및 AI 번역 에러 핸들링 봇 (config.json 통합 및 버그 방어 버전)
"""

import os
import sys
import json
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# 1. 환경 변수 로드 (로컬 PC 구동 시 .env 파일 스캔용)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. 통합 설정 파일(config.json) 로드
CONFIG_PATH = Path("config.json")

if not CONFIG_PATH.exists():
    print("❌ 에러: config.json 파일이 존재하지 않습니다. 파일을 먼저 생성해 주세요.")
    sys.exit(1)

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config_data = json.load(f)
except Exception as e:
    print(f"❌ 에러: config.json 파일을 읽는 도중 오류가 발생했습니다: {e}")
    sys.exit(1)

# 3. config.json에서 실시간 설정 추출 및 안전화
# 💡 [.strip()] 주소 앞뒤의 보이지 않는 공백/줄바꿈을 완벽히 도려내어 'Invalid URL' 버그를 차단합니다.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip() or config_data.get("DISCORD_WEBHOOK", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip() or config_data.get("DISCORD_USER_ID", "").strip()

# 주말에 새로 합친 뉴스 설정을 안전하게 가져옵니다.
NEWS_SETTINGS = config_data.get("news_settings", {})
KEYWORDS = NEWS_SETTINGS.get("KEYWORDS", [
    "AI Infrastructure",
    "semiconductor stock",
    "NVDA",
    "SOXL ETF",
    "TSLA stock",
    "IONQ stock"
])
MAX_NEWS_PER_KEYWORD = NEWS_SETTINGS.get("MAX_NEWS_PER_KEYWORD", 5)


def fetch_latest_news():
    """Google News RSS를 통해 키워드별 최신 뉴스를 수집합니다."""
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for kw in KEYWORDS:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n### 📂 검색 키워드: {kw}\n"
                items = root.findall(".//item")
                
                for item in items[:MAX_NEWS_PER_KEYWORD]:
                    title = item.find('title').text or ""
                    all_news_text += f"- {title}\n"
        except Exception as e:
            print(f"❌ 뉴스 수집 에러 ({kw}): {e}")
            continue
    return all_news_text


def send_to_discord(webhook_url, user_id, message_body, ping_prefix=""):
    """디스코드 2000자 제한을 우회하여 안전하게 분할 전송합니다."""
    if not webhook_url:
        print("⚠️ 디스코드 웹후크 URL이 설정되지 않았습니다.")
        return

    # 멘션 및 시스템 핑 헤더 생성
    mention = f"<@{user_id}>\n" if user_id else ""
    header = f"{mention}{ping_prefix}"
    
    # 디스코드 안전 제한 길이 (여유를 두고 1900자로 설정)
    MAX_LEN = 1900
    
    # 메시지 본문 내용이 길 경우 분할 처리
    if len(header + message_body) <= 2000:
        chunks = [message_body]
    else:
        # 본문을 줄 단위로 쪼개어 분할
        lines = message_body.split('\n')
        chunks = []
        current_chunk = ""
        
        for line in lines:
            if len(current_chunk + line + '\n') > MAX_LEN:
                chunks.append(current_chunk)
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        if current_chunk:
            chunks.append(current_chunk)

    # 분할된 청크별 전송 진행
    for i, chunk in enumerate(chunks):
        # 첫 번째 메시지에만 멘션과 헤더를 포함시킵니다.
        if i == 0:
            payload = {"content": f"{header}{chunk}"}
        else:
            payload = {"content": f"🔄 **이어서 계속...**\n\n{chunk}"}
            
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if resp.status_code in [200, 204]:
                print(f"✅ 디스코드 메시지 전송 완료 (파트 {i+1}/{len(chunks)})")
            else:
                print(f"❌ 디스코드 전송 실패 (상태 코드: {resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"❌ 디스코드 전송 중 네트워크 예외 발생: {e}")


def main():
    # 💡 [하이브리드 환경을 위한 환경 변수 상호 해석 장치]
    # 깃허브 서버(Action) 환경일 때는 Secrets에 등록된 값을 우선 추적하고, 
    # 로컬 파워셸 환경일 때는 로컬 .env나 config.json에 등록된 API 키를 영리하게 보정합니다.
    global GEMINI_API_KEY
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = config_data.get("GEMINI_API_KEY")

    # 💡 [마법 구문 변형 안전장치]
    # 만약 로컬 PC에서 테스트나 개발 용도로 수동 실행할 때, 디스코드에 알림이 중복 전송되어 
    # 채널이 도배되는 현상을 방지하고 싶다면 아래의 마법 구문 주석(#)을 해제하고 사용하시면 됩니다.
    # if os.getenv("GITHUB_ACTIONS") != "true":
    #     print("ℹ️ 로컬 편집기 환경 감지: 디스코드 도배 방지를 위해 시뮬레이션 모드로 전환하거나 종료할 수 있습니다.")

    # 1. 뉴스 수집
    news_content = fetch_latest_news()
    if not news_content.strip():
        print("ℹ️ 수집된 뉴스가 없습니다.")
        return

    # 2. Gemini AI 번역 및 요약 진행
    if not GEMINI_API_KEY:
        print("❌ 에러: GEMINI_API_KEY가 환경 변수나 config.json에 설정되지 않았습니다.")
        return

    report = None
    try:
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={'api_version': 'v1beta'}
        )
        
        prompt = (
            f"너는 금융 전문 번역가야. 아래 뉴스 제목들을 한국어로 번역해줘. "
            f"단, 회사명, 주식 티커, 고유명사는 영문 그대로 유지해. 섹터 구조는 그대로 유지해:\n\n{news_content}"
        )
        
        response = client.models.generate_content(
            model="models/gemini-3.5-flash",
            contents=prompt,
        )
        report = response.text

    except Exception as e:
        print(f"❌ AI 분석 실패 상세: {e}")

    # 3. 메시지 타이틀 세팅
    if report:
        final_message = f"📢 **오늘의 글로벌 시장 뉴스**\n\n{report}"
    else:
        final_message = f"⚠️ **AI 번역 실패 (뉴스 원문)**\n\n{news_content}"

    # 4. 매월 1일 하트비트/핑 기능 (계정 만료 방지)
    ping_prefix = ""
    if datetime.now().day == 1:
        ping_prefix = "📡 **[System Ping]** 디스코드 계정 활성화 유지 신호 송신 중 (정상 작동)\n\n"
        
    # 5. 최종 안전 전송 실행
    send_to_discord(DISCORD_WEBHOOK, DISCORD_USER_ID, final_message, ping_prefix)


if __name__ == "__main__":
    main()