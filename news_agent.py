"""
글로벌 시장 뉴스 수집 및 AI 번역 에러 핸들링 봇 (config.json 통합 버전)
"""

import os
import sys
import json
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from pathlib import Path
from dotenv import load_dotenv

# 1. .env 파일의 환경 변수를 로드합니다. (Gemini API Key용)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. 통합 설정 파일(config.json) 로드 로직
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

# 3. config.json에서 실시간 설정 추출
DISCORD_WEBHOOK = config_data.get("DISCORD_WEBHOOK", "")
DISCORD_USER_ID = config_data.get("DISCORD_USER_ID", "")

# 💡 주말에 새로 합친 뉴스 설정을 안전하게 가져옵니다.
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
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 💡 config.json에 저장된 최신 키워드 리스트를 기반으로 루프를 돕니다.
    for kw in KEYWORDS:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n### 📂 검색 키워드: {kw}\n"
                items = root.findall(".//item")
                
                # 💡 config.json에서 가져온 MAX_NEWS_PER_KEYWORD 값으로 개수를 제한합니다.
                for item in items[:MAX_NEWS_PER_KEYWORD]:
                    title = item.find('title').text or ""
                    all_news_text += f"- {title}\n"
        except Exception as e:
            print(f"뉴스 수집 에러 ({kw}): {e}")
            continue
    return all_news_text


def main():
    # 뉴스 수집
    news_content = fetch_latest_news()
    if not news_content.strip():
        print("수집된 뉴스가 없습니다.")
        return

    report = None
    try:
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={'api_version': 'v1beta'}
        )
        
        response = client.models.generate_content(
            model="models/gemini-2.5-flash",
            contents=f"너는 금융 전문 번역가야. 아래 뉴스 제목들을 한국어로 번역해줘. 단, 회사명, 주식 티커, 고유명사는 영문 그대로 유지해. 섹터 구조는 그대로 유지해:\n\n{news_content}"
        )
        report = response.text

    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")

    # 최종 결과 전송 로직
    if report:
        final_message = f"📢 **오늘의 글로벌 시장 뉴스**\n\n{report}"
    else:
        final_message = f"⚠️ **AI 번역 실패 (뉴스 원문)**\n\n{news_content}"

    # 📌 매월 1일 하트비트/핑 기능 (계정 만료 방지)
    from datetime import datetime
    ping_prefix = ""
    if datetime.now().day == 1:
        ping_prefix = "📡 **[System Ping]** 디스코드 계정 활성화 유지 신호 송신 중 (정상 작동)\n\n"
        
    # 디스코드 전송 (2000자 제한 처리)
    if DISCORD_WEBHOOK:
        mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
        full_message = f"{mention}{ping_prefix}{final_message}"
        
        if len(full_message) > 2000:
            full_message = full_message[:1997] + "..."
        
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": full_message}, timeout=15)
            print("디스코드 메시지 전송 완료")
        except Exception as e:
            print(f"디스코드 전송 실패: {e}")
    else:
        print("디스코드 웹후크 URL이 설정되지 않았습니다.")


if __name__ == "__main__":
    main()