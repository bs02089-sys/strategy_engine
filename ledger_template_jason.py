{
  "__GUIDE__": "★ 매매 체결 시 해당 모드(LONG/SHORT)와 액션(BUY/SELL)에 맞는 템플릿을 복사하여 ledger.json에 붙여넣으세요.",
  
  "📈_LONG_MODE_TEMPLATES": {
    "BUY_LONG_TEMPLATE": {
      "date": "2026-05-25",
      "ticker": "SOXL",
      "mode": "LONG",
      "action": "BUY",
      "target_price": 42.50,
      "qty": 15,
      "current_casts": 1,
      "vix_status": "VIX 상태 기록 (예: ⚠️ 주의)",
      "time_guard": "타임가드 상태 기록 (예: 🟢 해제)",
      "note": "지정가 예약 매수 체결 완료"
    },
    "SELL_LONG_TEMPLATE": {
      "date": "2026-08-12",
      "ticker": "SOXL",
      "mode": "LONG",
      "action": "SELL",
      "target_price": 55.00,
      "qty": 15,
      "note": "목표가 도달 수동 익절"
    }
  },

  "⚡_SHORT_MODE_TEMPLATES": {
    "BUY_SHORT_TEMPLATE": {
      "date": "2026-05-26",
      "ticker": "TSLA",
      "mode": "SHORT",
      "action": "BUY",
      "target_price": 175.00,
      "qty": 10,
      "vix_status": "VIX 상태 기록 (예: ✨ 안정)",
      "note": "단기 타격 시그널 타점 진입"
    },
    "SELL_SHORT_TEMPLATE": {
      "date": "2026-05-28",
      "ticker": "TSLA",
      "mode": "SHORT",
      "action": "SELL",
      "target_price": 188.50,
      "qty": 10,
      "profit_rate": 7.7,
      "note": "숏 모드 지정가 익절 완료"
    }
  },

  "⚠️_IMPORTANT_JSON_RULES": {
    "RULE_1": "대괄호([) 바로 다음 줄은 무조건 두 칸 띄우고(스페이스 2번 또는 Tab) 중괄호({)를 입력할 것 (정렬 유지)",
    "RULE_2": "새로운 데이터를 아래에 연속으로 추가할 때는 기존 중괄호 닫히는 곳 뒤에 반드시 쉼표(,)를 찍을 것",
    "RULE_3": "맨 마지막 데이터의 중괄호 뒤에는 쉼표(,)를 찍지 않으며, 맨 마지막 줄 대괄호(])는 맨 앞으로 바짝 붙여 장부를 닫을 것"
  }
}