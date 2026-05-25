{
  "__GUIDE__": "★ 매매 체결 시 해당 종목의 티커(Key) 내부에 모드(LONG/SHORT)와 액션(BUY/SELL)에 맞는 템플릿 내용물만 복사하여 최신 데이터로 '덮어쓰기' 하세요.",
  
  "LONG_MODE_TEMPLATES": {
    "BUY_LONG_TEMPLATE": {
      "date": "2026-05-25",
      "mode": "LONG",
      "action": "BUY",
      "buy_target": 42.50,
      "qty": 15,
      "current_casts": 1
    },

    "SELL_LONG_TEMPLATE": {
      "date": "2026-08-12",
      "mode": "LONG",
      "action": "SELL",
      "target_price": 55.00,
      "qty": 15
    }
  },

  "SHORT_MODE_TEMPLATES": {
    "BUY_SHORT_TEMPLATE": {
      "date": "2026-05-26",
      "mode": "SHORT",
      "action": "BUY",
      "buy_target": 175.00,
      "qty": 10
    },
    
    "SELL_SHORT_TEMPLATE": {
      "date": "2026-05-28",
      "mode": "SHORT",
      "action": "SELL",
      "target_price": 188.50,
      "qty": 10
    }
  },

  "⚠️_IMPORTANT_JSON_RULES": {
    "RULE_1": "실제 장부(ledger.json)는 대괄호([]) 대신 중괄호({})로 시작하고 닫아야 합니다.",
    "RULE_2": "종목 추가 시 ' \"티커명\": { 템플릿내용 } ' 형태로 작성하며, 종목과 종목 사이에는 반드시 쉼표(,)가 들어가야 합니다.",
    "RULE_3": "동일 종목의 새로운 매매가 체결되면, 줄을 새로 추가하지 말고 기존 티커 내부의 값들만 새 데이터로 슥 '덮어쓰기' 하세요."
  }
}