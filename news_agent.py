"""
글로벌 시장 뉴스 수집 및 AI 번역 에러 핸들링 봇 (V2.2: 리팩토링 버전)

주요 변경사항 (V2.1 → V2.2):
- [버그 수정] Groq 모델 `mixtral-8x7b-32768` → `openai/gpt-oss-120b` (전자는 Groq가
  이미 폐기(deprecate)한 모델이라 API 호출 시 에러 발생)
- [버그 수정] `python-dotenv`가 없는 환경(GitHub Actions)에서도 죽지 않도록
  optional import로 방어
- [구조 개선] 설정 로드 / 뉴스 수집 / AI 요약(Ollama·Groq) / 디스코드 전송을
  각각 독립 함수 + 명확한 책임으로 분리
- [구조 개선] 프롬프트 템플릿 중복 제거 (Ollama/Groq 공용 함수로 통합)
- [안정성] 타입 힌트 추가, config 누락 키에 대한 기본값 처리 강화
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

# pyrefly: ignore [missing-import]
import trafilatura

# pyrefly: ignore [missing-import]
from groq import Groq

# 🛡️ python-dotenv는 로컬 개발 편의용 선택 의존성.
#    GitHub Actions 등 CI 환경에는 없을 수 있으므로 없어도 죽지 않게 방어.
try:
    # pyrefly: ignore [missing-import]
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 🛡️ 올라마(로컬 LLM)도 선택 의존성. 없으면 자동으로 Groq API로 폴백.
try:
    # pyrefly: ignore [missing-import]
    import ollama  # type: ignore
    HAS_OLLAMA = True
except ImportError:
    ollama = None
    HAS_OLLAMA = False


# ============================================================
# 설정 로드
# ============================================================

CONFIG_PATH = Path("config.json")

DEFAULT_KEYWORDS = [
    "AI Infrastructure",
    "semiconductor stock",
    "NVDA",
    "NVDX ETF",
    "SOXX ETF",
    "SOXL ETF",
    "TSLA stock",
    "IONQ stock",
]

# 현재(2026년 기준) Groq에서 지원하는 모델. mixtral-8x7b-32768은 폐기되어 사용 불가.
GROQ_MODEL = "openai/gpt-oss-120b"

PROMPT_TEMPLATE = (
    "너는 월스트리트 출신의 전문 금융 애널리스트이자 번역가야. 내가 준 기사 세트(제목과 내용)를 "
    "바탕으로 리포트를 작성해줘.\n\n"
    "🚨 [작성 규칙]\n"
    "1. 단순 의문문이나 낚시성 제목에 낚이지 말고, 제공된 '내용'을 파악해서 실질적인 정보와 "
    "'팩트(Fact)' 중심의 결과물로 보정해줘.\n"
    "2. 각 기사는 반드시 아래의 형식을 똑같이 유지해서 한 줄 한 줄 깔끔하게 출력해줘.\n"
    "3. 제목은 절대 번역하지 말고 기사에 주어진 영문 원제 그대로 출력해줘.\n"
    "4. 핵심 팩트(내용 요약)는 반드시 한글로 작성해줘. 단, 회사명, 주식 티커(NVDA, SOXL, TSLA 등), "
    "고유명사는 번역하지 말고 영문 그대로 유지해줘.\n\n"
    "📝 [출력 포맷 예시]\n"
    "- **{{영문 원제 그대로}}**\n"
    "  └ 💡 **핵심 팩트:** 기사 본문 내용을 기반으로 한 알맹이 있는 실질적 내용 한 줄 요약 (한글)\n\n"
    "이제 아래의 뉴스 데이터를 가지고 규칙과 포맷에 맞춰 작업해줘:\n\n{news_content}"
)


class Config:
    """config.json + 환경 변수를 통합 관리."""

    def __init__(self, path: Path):
        if not path.exists():
            print("❌ 에러: config.json 파일이 존재하지 않습니다. 파일을 먼저 생성해 주세요.")
            sys.exit(1)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"❌ 에러: config.json 파일을 읽는 도중 오류가 발생했습니다: {e}")
            sys.exit(1)

        self._data = data
        news_settings = data.get("news_settings", {})

        self.discord_webhook = (
            os.environ.get("DISCORD_WEBHOOK", "").strip()
            or data.get("DISCORD_WEBHOOK", "").strip()
        )
        self.discord_user_id = (
            os.environ.get("DISCORD_USER_ID", "").strip()
            or data.get("DISCORD_USER_ID", "").strip()
        )
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "").strip() or data.get(
            "GROQ_API_KEY", ""
        )
        self.keywords: list[str] = news_settings.get("KEYWORDS", DEFAULT_KEYWORDS)
        self.max_news_per_keyword: int = news_settings.get("MAX_NEWS_PER_KEYWORD", 5)
        # 기사 원문에서 추출할 본문 길이 상한
        self.max_content_chars: int = news_settings.get("MAX_CONTENT_CHARS", 500)
        # 원문 접속 타임아웃(초)
        self.article_fetch_timeout: int = news_settings.get("ARTICLE_FETCH_TIMEOUT", 8)
        # 이 길이 미만으로 추출되면 유료 구독/접근 차단으로 간주하고 폴백
        self.min_content_chars: int = news_settings.get("MIN_CONTENT_CHARS", 200)
        # 본문 병렬 fetch에 사용할 스레드 수
        self.article_fetch_workers: int = news_settings.get("ARTICLE_FETCH_WORKERS", 8)


# ============================================================
# 뉴스 수집
# ============================================================

def fetch_article_text(
    url: str, timeout: int, min_chars: int, headers: dict
) -> Optional[str]:
    """기사 원문 URL을 방문해 본문 텍스트를 추출합니다.

    유료 구독 벽, 로그인 요구, 접근 차단 등으로 실제 본문을 가져오지 못하면
    추출 결과가 매우 짧아지므로(쿠키 배너/안내문 정도), min_chars 미만이면
    실패로 간주하고 None을 반환합니다. 즉 언론사를 수동으로 화이트/블랙리스트
    관리할 필요 없이 자동으로 걸러집니다.
    """
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        extracted = trafilatura.extract(
            resp.text, include_comments=False, include_tables=False
        )
        if not extracted or len(extracted.strip()) < min_chars:
            return None
        return extracted.strip()
    except Exception:
        return None


def fetch_latest_news(
    keywords: list[str],
    max_per_keyword: int,
    max_content_chars: int = 500,
    article_fetch_timeout: int = 8,
    min_content_chars: int = 200,
    max_workers: int = 8,
) -> str:
    """Google News RSS에서 제목을 수집하고, 원문 링크를 병렬로 따라가 실제 본문을 추출합니다."""
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1단계: RSS 파싱 (가볍고 빠르므로 순차 처리)
    # 각 항목: {keyword, idx, title, link, fallback_text}
    parsed_items: list[dict] = []

    for kw in keywords:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            resp.raise_for_status()

            # 구글 RSS 인코딩 이슈를 피하기 위해 utf-8 바이트로 통일 후 파싱
            root = ET.fromstring(resp.text.encode("utf-8"))
            items = root.findall(".//item")

            for idx, item in enumerate(items[:max_per_keyword], 1):
                title_el = item.find("title")
                title = title_el.text if title_el is not None and title_el.text else "No Title"

                description_el = item.find("description")
                description_raw = (
                    description_el.text
                    if description_el is not None and description_el.text
                    else ""
                )
                desc_unescaped = html.unescape(description_raw)
                clean_text = re.sub(r"<[^>]*>", "", desc_unescaped).strip()

                if "This article appeared in" in clean_text:
                    clean_text = clean_text.split("This article appeared in")[0].strip()

                link_el = item.find("link")
                link = link_el.text if link_el is not None and link_el.text else None

                parsed_items.append(
                    {
                        "keyword": kw,
                        "idx": idx,
                        "title": title,
                        "link": link,
                        "clean_text": clean_text,
                    }
                )
        except Exception as e:
            print(f"❌ 뉴스 수집 에러 ({kw}): {e}")
            continue

    # 2단계: 기사 원문 본문을 병렬로 fetch (네트워크 대기가 병목이므로 스레드풀 사용)
    article_texts: dict[int, Optional[str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_pos = {
            executor.submit(
                fetch_article_text, entry["link"], article_fetch_timeout, min_content_chars, headers
            ): pos
            for pos, entry in enumerate(parsed_items)
            if entry["link"]
        }
        for future in as_completed(future_to_pos):
            pos = future_to_pos[future]
            try:
                article_texts[pos] = future.result()
            except Exception:
                article_texts[pos] = None

    # 3단계: 원래 순서(키워드 → idx)대로 최종 텍스트 조립
    all_news_text = ""
    current_kw = None
    for pos, entry in enumerate(parsed_items):
        if entry["keyword"] != current_kw:
            current_kw = entry["keyword"]
            all_news_text += f"\n### 📂 검색 키워드: {current_kw}\n"

        article_text = article_texts.get(pos)
        clean_text = entry["clean_text"]
        title = entry["title"]

        # 1순위: 병렬로 fetch한 원문 본문
        if article_text:
            truncated = len(article_text) > max_content_chars
            desc_text = article_text[:max_content_chars]
            if truncated:
                desc_text += "..."
        # 2순위: RSS description 폴백. 구글 RSS description은 흔히
        # "제목 + 언론사명"만 담고 있어 제목보다 살짝 긴 정도로는
        # 실질적인 추가 정보가 아니므로, 제목보다 충분히 길 때만 채택.
        elif clean_text and len(clean_text) - len(title) >= 30:
            desc_text = clean_text[:max_content_chars]
        else:
            desc_text = "본문 요약 없음 (유료 구독 또는 접근 차단 기사로 추정)"

        all_news_text += f"기사 {entry['idx']}.\n"
        all_news_text += f"- 제목: {title}\n"
        all_news_text += f"- 내용: {desc_text}\n\n"

    return all_news_text


# ============================================================
# AI 요약 (Ollama → Groq 순서로 시도)
# ============================================================

def _extract_ollama_text(response) -> Optional[str]:
    """ollama.chat() 응답 shape이 버전마다 달라 방어적으로 텍스트를 추출."""
    if isinstance(response, dict):
        msg = response.get("message") or response
        if isinstance(msg, dict):
            return msg.get("content") or msg.get("text")
        return str(msg)

    if hasattr(response, "message"):
        msg = getattr(response, "message")
        if isinstance(msg, dict):
            return msg.get("content") or msg.get("text")
        if hasattr(msg, "content"):
            return getattr(msg, "content")
    if hasattr(response, "content"):
        return getattr(response, "content")
    if hasattr(response, "text"):
        return getattr(response, "text")
    return str(response)


def summarize_with_ollama(news_content: str) -> Optional[str]:
    if not HAS_OLLAMA or not ollama or not callable(getattr(ollama, "chat", None)):
        return None

    print("ℹ️ 로컬 AI 엔진(Ollama) 감지: Llama3 모델로 뉴스 팩트 요약을 시작합니다.")
    try:
        prompt = PROMPT_TEMPLATE.format(news_content=news_content)
        response = ollama.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
        report = _extract_ollama_text(response)
        if not report:
            raise ValueError("Ollama 응답에서 텍스트를 추출하지 못했습니다.")
        return report
    except Exception as e:
        print(f"❌ 로컬 Ollama 구동 실패: {e}. Groq API 백업 엔진 전환을 시도합니다.")
        return None


def summarize_with_groq(news_content: str, api_key: str) -> Optional[str]:
    if not api_key:
        print("❌ 에러: AI 처리를 위한 자격 증명(Ollama 또는 GROQ_API_KEY)이 없습니다.")
        return None

    print("ℹ️ 클라우드 AI 엔진(Groq) 구동: 팩트 요약 번역을 시작합니다.")
    try:
        client = Groq(api_key=api_key)
        prompt = PROMPT_TEMPLATE.format(news_content=news_content)

        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional financial analyst and translator "
                    "specializing in Wall Street news.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2048,
        )
        return response.choices[0].message.content if response.choices else None
    except Exception as e:
        print(f"❌ Groq 클라우드 AI 분석 실패: {e}")
        return None


# ============================================================
# 디스코드 전송
# ============================================================

def send_to_discord(webhook_url: str, user_id: str, message_body: str) -> None:
    """디스코드 2000자 제한을 우회하여 안전하게 분할 전송합니다."""
    header = f"<@{user_id}>\n" if user_id else ""
    max_len = 1900

    if len(header + message_body) <= 2000:
        chunks = [message_body]
    else:
        chunks, current_chunk = [], ""
        for line in message_body.split("\n"):
            if len(current_chunk + line + "\n") > max_len:
                chunks.append(current_chunk)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            chunks.append(current_chunk)

    for i, chunk in enumerate(chunks):
        payload = (
            {"content": f"{header}{chunk}"}
            if i == 0
            else {"content": f"🔄 **이어서 계속...**\n\n{chunk}"}
        )
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                print(f"✅ 디스코드 메시지 전송 완료 (파트 {i + 1}/{len(chunks)})")
            else:
                print(f"❌ 디스코드 전송 실패 (상태 코드: {resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"❌ 디스코드 전송 중 네트워크 예외 발생: {e}")


# ============================================================
# 메인
# ============================================================

def main() -> None:
    config = Config(CONFIG_PATH)

    news_content = fetch_latest_news(
        config.keywords,
        config.max_news_per_keyword,
        max_content_chars=config.max_content_chars,
        article_fetch_timeout=config.article_fetch_timeout,
        min_content_chars=config.min_content_chars,
        max_workers=config.article_fetch_workers,
    )
    if not news_content.strip():
        print("ℹ️ 수집된 뉴스가 없습니다.")
        return

    report = summarize_with_ollama(news_content)
    ai_mode_notice = "🤖 **[Ollama AI 뉴스 팩트 브리핑]**\n\n" if report else ""

    if not report:
        report = summarize_with_groq(news_content, config.groq_api_key)
        ai_mode_notice = "✨ **[Groq AI 뉴스 팩트 브리핑]**\n\n" if report else ""

    if report:
        final_message = f"{ai_mode_notice}{report}"
    else:
        final_message = f"⚠️ **AI 번역 전원 실패 (뉴스 원문 출력)**\n\n{news_content}"

    if config.discord_webhook:
        send_to_discord(config.discord_webhook, config.discord_user_id, final_message)
    else:
        print("\n" + "=" * 60)
        print("ℹ️  로컬 편집기 환경 감지: 디스코드 웹훅이 없어 콘솔창에 결과를 출력합니다.")
        print("=" * 60)
        print(final_message)
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
