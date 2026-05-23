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
      "note": "분할 매도 계획에 따른 익절 완료"
    }
  }
}