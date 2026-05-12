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
                all_news_text += f"\n[섹터: {kw}]\n" # '분석 섹터' 대신 '섹터'로 유지
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    try:
        client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})
        
        # [수정] 404 에러 해결을 위해 모델 경로를 'models/gemini-1.5-flash'로 풀네임 작성
        response = client.models.generate_content(
            model="models/gemini-1.5-flash", 
            contents=[
                "너는 글로벌 경제 뉴스 전문 요약가야. 아래 영문 뉴스들을 읽고 한국인 투자자들이 이해하기 쉽게 핵심만 한국어로 요약해줘.",
                "지침: 1. 영어는 한 마디도 쓰지 마. 2. 섹터별로 나눠서 요약해.",
                f"분석할 뉴스:\n{news_content}"
            ]
        )
        report = response.text
    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")
        report = None

    # 결과 전송 (분석 실패 시에도 원문은 한글 '섹터' 표기 유지)
    if report:
        msg = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{report}"
    else:
        msg = f"⚠️ **AI 분석 실패 (뉴스 원문 리스트)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    requests.post(DISCORD_WEBHOOK, json={"content": f"{mention}{msg}"}, timeout=15)

if __name__ == "__main__":
    main()