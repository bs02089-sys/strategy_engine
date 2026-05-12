import os
import requests
import xml.etree.ElementTree as ET
import google.genai as genai

# 환경 변수 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 키워드 매핑
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
        # [핵심 변경] v1 정식 API와 최신 모델 ID를 사용하여 404 원천 차단
        client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})
        
        response = client.models.generate_content(
            model="gemini-1.5-flash", # 혹은 "gemini-1.5-flash-002"
            contents=f"너는 투자 뉴스 번역가야. 아래의 섹터별 뉴스 제목들을 하나도 빠짐없이 '한국어'로 번역해서 요약해줘. 영어 원문은 출력하지 마.\n\n{news_content}"
        )
        report = response.text
    except Exception as e:
        # 에러 발생 시 로그 출력
        print(f"AI 분석 실패 상세: {e}")

    # 최종 메시지 구성
    if report:
        # AI 분석 성공 시: 요약된 한글 내용 출력
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{report}"
    else:
        # AI 분석 실패 시: 한글 섹터명 + 영어 제목 출력 (현재 상태)
        final_message = f"⚠️ **AI 요약 실패 (뉴스 원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()