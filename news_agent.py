import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai

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
                all_news_text += f"\n[Sector: {kw}]\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # [수정 포인트] 출력 언어를 '한국어'로 강력하게 고정하는 설정
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                "당신은 전문 번역가이자 시장 분석가입니다. 아래의 영문 뉴스들을 읽고 반드시 '한국어'로만 요약하여 리포트를 작성하세요.",
                f"분석할 뉴스 내용:\n{context}",
                "조건: 영문은 가급적 한국어로 번역하고, 핵심 내용 위주로 불렛 포인트를 사용해 한국어로 답변하세요."
            ]
        )
        return response.text
    except Exception as e:
        print(f"AI 분석 실패: {e}")
        return None

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    analysis_report = analyze_market_with_gemini(news_content)
    
    if analysis_report:
        # 혹시 모를 영어 출력을 방지하기 위해 제목도 명확히 한글화
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트 (KOREAN)**\n\n{analysis_report}"
    else:
        final_message = f"⚠️ **AI 요약 실패 (수집된 원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()