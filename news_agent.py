"""
📰 Stock News Agent
- Google News RSS로 키워드 기반 뉴스 수집
- Gemini API로 투자 시사점 분석
- Discord Webhook으로 전송
- 매일 오전 9시 / 오후 7시 자동 실행
"""

import feedparser
import google.generativeai as genai
import requests
import schedule
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from config import KEYWORDS, DISCORD_WEBHOOK, GEMINI_API_KEY, SCHEDULE_TIMES, MAX_NEWS_PER_KEYWORD

# ─────────────────────────────────────────
# 뉴스 수집
# ─────────────────────────────────────────

def fetch_news(keyword: str, max_results: int = MAX_NEWS_PER_KEYWORD) -> list[dict]:
    """Google News RSS에서 키워드 뉴스 수집"""
    encoded = quote(keyword)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    feed = feedparser.parse(url)
    news_list = []
    cutoff = datetime.now() - timedelta(hours=24)

    for entry in feed.entries[:max_results]:
        try:
            published = datetime(*entry.published_parsed[:6])
            if published < cutoff:
                continue
        except Exception:
            published = datetime.now()

        news_list.append({
            "title": entry.get("title", ""),
            "summary": entry.get("summary", "")[:500],
            "link": entry.get("link", ""),
            "published": published.strftime("%Y-%m-%d %H:%M"),
            "source": entry.get("source", {}).get("title", "Unknown"),
        })

    return news_list


# ─────────────────────────────────────────
# Gemini 분석
# ─────────────────────────────────────────

def analyze_with_gemini(keyword: str, news_list: list[dict]) -> str:
    """Gemini API로 뉴스 묶음 분석"""
    if not news_list:
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    news_text = "\n\n".join([
        f"[{i+1}] {n['title']}\n출처: {n['source']} | {n['published']}\n내용: {n['summary']}"
        for i, n in enumerate(news_list)
    ])

    prompt = f"""당신은 미국 주식 투자 전문 애널리스트입니다.
아래는 키워드 '{keyword}'에 관한 최신 뉴스입니다.

{news_text}

다음 형식으로 분석해주세요:

**📊 시장 분위기**: (긍정적/부정적/중립적 + 한 줄 이유)
**🔑 핵심 내용**: (3줄 이내 요약)
**💹 투자 시사점**: (이 키워드 관련 주식/섹터에 미치는 영향, 단기/중기 관점)
**⚠️ 주의사항**: (놓치면 안 될 리스크 또는 변수)

간결하고 날카롭게, 실제 투자 판단에 도움이 되도록 작성해주세요."""

    response = model.generate_content(prompt)
    return response.text


# ─────────────────────────────────────────
# Discord 전송
# ─────────────────────────────────────────

def send_to_discord(keyword: str, news_list: list[dict], analysis: str):
    """Discord Webhook으로 뉴스 + 분석 전송"""
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    session_label = "🌅 오전 브리핑" if datetime.now().hour < 12 else "🌆 오후 브리핑"

    header_embed = {
        "title": f"{session_label} | `{keyword}`",
        "description": f"📅 {now} 기준 최신 뉴스 **{len(news_list)}건** 분석 완료",
        "color": 0x00B4D8,
        "footer": {"text": "Stock News Agent · Powered by Gemini"},
    }

    news_fields = []
    for i, n in enumerate(news_list[:5]):
        news_fields.append({
            "name": f"{i+1}. {n['title'][:80]}",
            "value": f"📰 {n['source']} | {n['published']}\n[기사 보기]({n['link']})",
            "inline": False,
        })

    news_embed = {
        "title": "📋 수집된 뉴스",
        "color": 0x48CAE4,
        "fields": news_fields,
    }

    analysis_embed = {
        "title": "🤖 Gemini AI 투자 분석",
        "description": analysis[:4000] if analysis else "분석 결과 없음",
        "color": 0x0077B6,
    }

    payload = {
        "username": "📈 Stock News Agent",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2103/2103633.png",
        "embeds": [header_embed, news_embed, analysis_embed],
    }

    resp = requests.post(DISCORD_WEBHOOK, json=payload)
    if resp.status_code in (200, 204):
        print(f"  ✅ Discord 전송 완료: {keyword}")
    else:
        print(f"  ❌ Discord 전송 실패 ({resp.status_code}): {keyword}")


def send_separator_to_discord():
    """뉴스 묶음 사이 구분선"""
    requests.post(DISCORD_WEBHOOK, json={
        "username": "📈 Stock News Agent",
        "content": "─────────────────────────────",
    })


# ─────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────

def run_agent():
    """전체 뉴스 에이전트 실행"""
    print(f"\n{'='*50}")
    print(f"🚀 뉴스 에이전트 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    for keyword in KEYWORDS:
        print(f"\n🔍 키워드 처리 중: [{keyword}]")

        news_list = fetch_news(keyword)
        print(f"  📰 수집된 뉴스: {len(news_list)}건")

        if not news_list:
            print(f"  ⚠️  최근 24시간 내 뉴스 없음, 건너뜀")
            continue

        print(f"  🤖 Gemini 분석 중...")
        analysis = analyze_with_gemini(keyword, news_list)

        send_to_discord(keyword, news_list, analysis)
        send_separator_to_discord()

        time.sleep(2)

    print(f"\n✅ 모든 키워드 처리 완료\n")


# ─────────────────────────────────────────
# 스케줄러
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("📅 뉴스 에이전트 스케줄러 시작")
    print(f"⏰ 실행 시간: {', '.join(SCHEDULE_TIMES)}")
    print(f"🔑 키워드: {', '.join(KEYWORDS)}\n")

    for t in SCHEDULE_TIMES:
        schedule.every().day.at(t).do(run_agent)

    # 시작 시 즉시 한 번 실행하려면 아래 주석 해제
    # run_agent()

    while True:
        schedule.run_pending()
        time.sleep(30)