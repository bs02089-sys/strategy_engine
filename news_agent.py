import os
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
                all_news_text += f"\n[섹터: {kw}]\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    try:
        # [핵심 처방] v1 정식 API 버전을 사용하도록 강제 설정
        # 이 설정이 v1beta로 인한 404 에러를 해결하는 가장 강력한 방법입니다.
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={'api_version': 'v1'}
        )
        
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=f"당신은 테크 분석가입니다. 아래 뉴스를 읽고 반드시 '한국어'로만 요약해 주세요:\n\n{news_content}"
        )
        report = response.text
    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")
        report = None

    if report:
        msg = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{report}"
    else:
        msg = f"⚠️ **AI 분석 실패 (원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    requests.post(DISCORD_WEBHOOK, json={"content": f"{mention}{msg}"}, timeout=15)

if __name__ == "__main__":
    main()