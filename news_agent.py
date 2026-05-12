import os
import requests
import xml.etree.ElementTree as ET
import google.genai as genai

# 환경 변수 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 키워드 한글 매핑 (AI 실패 시 비상용)
KEYWORDS_MAP = {
    "Semiconductor": "반도체",
    "AI Infrastructure": "AI 인프라",
    "NVIDIA": "엔비디아",
    "CapEx": "설비투자(CapEx)"
}

def fetch_latest_news():
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for en_kw, ko_kw in KEYWORDS_MAP.items():
        rss_url = f"https://news.google.com/rss/search?q={en_kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n### 📂 섹터: {ko_kw}\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    title = item.find('title').text
                    all_news_text += f"- {title}\n"
        except: continue
    return all_news_text

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    report = None
    try:
        # [해결책] v1 정식 API 설정 + 모델 풀네임(models/...) 사용
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={'api_version': 'v1'}
        )
        
        # 모델명을 'models/gemini-1.5-flash'로 명시적으로 지정
        response = client.models.generate_content(
            model="models/gemini-1.5-flash",
            contents=f"너는 금융 전문 번역가야. 아래 뉴스 제목들을 반드시 한국어로 번역해서 요약해줘. 영어는 절대 쓰지 마:\n\n{news_content}"
        )
        report = response.text
    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")

    # 최종 결과 전송
    if report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{report}"
    else:
        # 404 에러 시 이 메시지가 출력되며, 뉴스 제목은 영어로 나옵니다.
        final_message = f"⚠️ **AI 요약 실패 (뉴스 원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    requests.post(DISCORD_WEBHOOK, json={"content": f"{mention}{final_message}"}, timeout=15)

if __name__ == "__main__":
    main()