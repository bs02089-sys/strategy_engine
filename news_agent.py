import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from google.genai import types

# 환경 변수 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
raw_keywords = os.getenv("NEWS_KEYWORDS", "Semiconductor, AI Infrastructure, NVIDIA, CapEx")
KEYWORDS = [k.strip() for k in raw_keywords.split(",")]

def fetch_latest_news():
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    for kw in KEYWORDS:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n[섹터: {kw}]\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        # [핵심] v1 정식 버전 API를 사용하도록 강제 설정
        client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})
        
        # 모델 명칭은 가장 기본인 gemini-1.5-flash 사용
        response = client.models.generate_content(
            model="gemini-1.5-flash", 
            contents=f"명령: 아래 영문 뉴스들을 한국어로 요약해줘. 반드시 한국어만 사용해.\n\n내용:\n{context}"
        )
        return response.text
    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")
        return None

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    analysis_report = analyze_market_with_gemini(news_content)
    
    if analysis_report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        # 404가 또 나더라도 원문은 확실히 나오게 처리
        final_message = f"⚠️ **AI 분석 실패 (원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()