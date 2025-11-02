def live_prices(tickers):
    """
    Fetch live prices for given tickers.
    Returns a dictionary with ticker symbols as keys and prices as values.
    """
    import yfinance as yf
    
    live_map = {}
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            price = info.get('currentPrice') or info.get('regularMarketPrice')
            live_map[ticker] = price
        except Exception as e:
            print(f"⚠️ {ticker} 실시간 가격 조회 실패: {e}")
            live_map[ticker] = None
    
    return live_map