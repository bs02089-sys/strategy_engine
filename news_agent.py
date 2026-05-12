import os
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

# 1. .env 파일의 환경 변수를 로드합니다.
load_dotenv()

# 2. 환경 변수 로드
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 키워드 한글 매핑
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
                    title = item.find('title').text or ""
                    all_news_text += f"- {title}\n"
        except Exception as e:
            print(f"뉴스 수집 에러 ({en_kw}): {e}")
            continue
    return all_news_text

def main():
    # 뉴스 수집
    news_content = fetch_latest_news()
    if not news_content.strip():
        print("수집된 뉴스가 없습니다.")
        return

    final_message = f"📢 **오늘의 글로벌 시장 뉴스**\n\n{news_content}"

    # 디스코드 전송 (2000자 제한 처리)
    if DISCORD_WEBHOOK:
        mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
        full_message = f"{mention}{final_message}"
        
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