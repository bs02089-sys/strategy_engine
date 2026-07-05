import os
import requests
from groq import Groq

def get_market_analysis():
    # 1. 환경변수에서 API 키와 웹훅 URL 불러오기
    # 깃허브 시크릿에 등록한 이름과 동일하게 설정하세요
    api_key = os.environ.get("GROQ_API_KEY")
    webhook_url = os.environ.get("DISCORD_WEBHOOK")

    if not api_key or not webhook_url:
        print("에러: API 키 또는 웹훅 URL이 설정되지 않았습니다.")
        return

    # 2. Groq 클라이언트 초기화
    client = Groq(api_key=api_key)

    # 3. 분석 요청
    try:
        response = client.chat.completions.create(
            messages=[{
                "role": "user", 
                "content": "SOXX, AIPO, QNDX, NVDX, SOXL, IONQ, TSLA와 관련되는 시장의 주요 이슈를 분석하고, 투자자가 참고할 만한 핵심 내용을 3줄 요약해줘."
            }],
            model="llama-3.3-70b-versatile"
        )
        analysis_text = response.choices[0].message.content
        
        # 4. 디스코드 전송
        payload = {"content": f"### 💡 시장 분석 리포트\n{analysis_text}"}
        requests.post(webhook_url, json=payload)
        print("성공: 뉴스 분석이 디스코드에 전송되었습니다.")
        
    except Exception as e:
        print(f"에러 발생: {e}")

if __name__ == "__main__":
    get_market_analysis()