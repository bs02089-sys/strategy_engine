import json

# 1. 제이슨에 들어갈 데이터를 파이썬 딕셔너리로 정의 (오타 방지)
config_data = {
    "DISCORD_WEBHOOK": "https: //discord.com/api/webhooks/1499609112122626150/A_EMqtNHG3GGXs8kgERjXYc-WiJJ3ZC0_rXCvicDCuxucREWafwaZsJrBoo6zOElJ8KN",
    "DISCORD_USER_ID": "1431474369196064868",
    "TICKERS": ["SOXL", "TSLA"],
    "POSITIONS": {
        "SOXL": {
            "MODE": "LONG",
            "TOTAL_SHARES": 34,
            "MY_AVG_PRICE": 165.1124,
            "CURRENT_CASTS": 4,
            "ANNUAL_QUOTA": 20,
            "LAST_CAST_DATE": "2026-05-15"
        },
        "TSLA": {
            "MODE": "SHORT",
            "TOTAL_SHARES": 10,
            "MY_AVG_PRICE": 321.20
        }
    }
}

# 2. 파이썬이 직접 시스템 규칙에 맞게 config.json 파일로 추출
file_name = "config.json"
with open(file_name, "w", encoding="utf-8") as f:
    # indent=2를 주어 컴퓨터가 자동으로 2칸 들여쓰기 정렬을 수행합니다.
    json.dump(config_data, f, ensure_ascii=False, indent=2)

print(f"✅ {file_name} 파일이 문법 에러 없이 완벽하게 자동 생성되었습니다!")