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
                all_news_text += f"\n[분석 섹터: {kw}]\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        # [핵심 수정] 시스템 인스트럭션을 통해 AI의 정체성을 '한국어 분석가'로 고정
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # 시스템 지침을 명확히 전달하는 구조
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            config=genai.types.GenerateContentConfig(
                system_instruction="당신은 영문 경제 뉴스를 한국어로 요약 전달하는 전문 번역가입니다. 어떤 경우에도 영어 원문을 그대로 출력하지 말고, 반드시 한국어로 친절하게 설명하세요."
            ),
            contents=f"다음 영문 뉴스들을 섹터별로 핵심만 뽑아서 한국어로 요약해줘:\n{context}"
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
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        final_message = f"⚠️ **AI 요약 실패 (원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()