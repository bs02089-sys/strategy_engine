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
                all_news_text += f"\n**[{kw}]**\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    # 404 에러 방지를 위해 모델명을 가장 안정적인 'gemini-1.5-flash'로 설정
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        # 중요: 모델명에서 'models/'를 완전히 제외하고 입력
        response = client.models.generate_content(
            model="gemini-1.5-flash", 
            contents=f"다음 뉴스를 요약해줘: {context}"
        )
        return response.text
    except Exception as e:
        print(f"AI 분석 실패: {e}")
        return None # 실패 시 None 반환

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    # AI 분석 시도
    analysis_report = analyze_market_with_gemini(news_content)
    
    # 메시지 구성 (분석 성공 시 분석 내용, 실패 시 뉴스 목록 전송)
    if analysis_report:
        final_message = f"🔎 **오늘의 AI & 반도체 시장 리포트**\n\n{analysis_report}"
    else:
        # AI가 죽었을 때를 대비한 '간단한 메시지' 모드
        final_message = f"⚠️ **AI 분석 일시 중단 (뉴스 목록 대체)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()