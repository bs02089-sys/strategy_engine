from datetime import datetime
import unicodedata
import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf


# ====================== NYSE 거래일 캘린더 ======================

def get_nyse_trading_days(start_date, n_days):
    """NYSE 공휴일을 반영한 n번째 거래일 반환"""
    end_estimate = pd.Timestamp(start_date) + pd.offsets.BDay(n_days + 20)
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=start_date, end_date=end_estimate.date())
    future_days = [d for d in schedule.index.date if d > start_date]
    while len(future_days) < n_days:
        end_estimate += pd.offsets.BDay(10)
        schedule = nyse.schedule(start_date=start_date, end_date=end_estimate.date())
        future_days = [d for d in schedule.index.date if d > start_date]
    return future_days[n_days - 1]


# ====================== 출력 정렬 유틸 ======================

def str_width(s):
    """한글 등 전각 문자를 너비 2로 계산"""
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in str(s))


def pad_to_width(s, width, align='left'):
    """실제 문자 너비 기준으로 패딩"""
    s = str(s)
    padding = max(0, width - str_width(s))
    if align == 'right':
        return ' ' * padding + s
    elif align == 'center':
        left = padding // 2
        return ' ' * left + s + ' ' * (padding - left)
    return s + ' ' * padding


def print_aligned_table(result_df):
    """한글 너비를 고려한 정렬 출력"""
    headers = ['종목', '시나리오', '현재가', '목표가', '필요 상승률',
               '예상 거래일', '예상 도달일', '평균 일수익률', '연환산 변동성']
    aligns  = ['left', 'left', 'right', 'right', 'right',
               'right', 'left', 'right', 'right']

    rows = []
    for (ticker, scenario), row in result_df.iterrows():
        rows.append((
            ticker, scenario,
            row['현재가'], row['목표가'], row['필요 상승률'],
            row['예상 거래일'], row['예상 도달일'],
            row['평균 일수익률'], row['연환산 변동성']
        ))

    col_widths = [str_width(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], str_width(str(val)))

    sep = '=' * (sum(col_widths) + 2 * (len(headers) - 1))

    print(sep)
    print('  '.join(pad_to_width(h, col_widths[i], 'center') for i, h in enumerate(headers)))
    print(sep)

    prev_ticker = None
    for row in rows:
        ticker = row[0]
        display_ticker = ticker if ticker != prev_ticker else ''
        prev_ticker = ticker
        cells = [display_ticker] + [str(v) for v in row[1:]]
        print('  '.join(pad_to_width(cells[i], col_widths[i], aligns[i]) for i in range(len(cells))))

    print(sep)


# ====================== 분석 함수 ======================

def calculate_target_dates(tickers, target_prices):
    """보수적 목표가 도달 예측 (NYSE 공휴일 반영)"""

    df = yf.download(tickers, period="2mo", progress=False)
    df = df.swaplevel(axis=1).sort_index(axis=1)

    today = datetime.today().date()
    results = []

    for ticker in tickers:
        ticker_df = df[ticker].dropna()
        recent_20 = ticker_df.tail(20)

        if len(recent_20) < 20:
            print(f"⚠️ {ticker} 데이터 부족")
            continue

        current_price = recent_20["Close"].iloc[-1]
        target_price = target_prices.get(ticker)

        if not target_price or target_price <= current_price:
            continue

        returns = recent_20["Close"].pct_change().dropna()
        avg_daily_return = returns.mean()
        daily_vol = returns.std()
        annualized_vol = daily_vol * np.sqrt(252)

        required_return = (target_price / current_price) - 1

        scenarios = {
            "Base": avg_daily_return,
            "Conservative": max(avg_daily_return - 0.3 * daily_vol, avg_daily_return * 0.4)
        }

        weekday_dict = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}

        for scenario_name, daily_r in scenarios.items():
            if required_return <= 0:
                days = 0
                date_str = "오늘 (이미 달성)"
            elif daily_r <= 0:
                days = "추정 불가"
                date_str = "변동성 과다"
            else:
                days = int(np.ceil(np.log(1 + required_return) / np.log(1 + daily_r)))
                days = max(1, days)

                target_date = get_nyse_trading_days(today, days)
                date_str = f"{target_date.strftime('%Y-%m-%d')} ({weekday_dict[target_date.weekday()]})"

            results.append({
                "종목": ticker,
                "현재가": round(current_price, 2),
                "목표가": target_price,
                "필요 상승률": f"{required_return:+.1%}",
                "시나리오": scenario_name,
                "예상 거래일": days,
                "예상 도달일": date_str,
                "평균 일수익률": f"{avg_daily_return:+.3%}",
                "연환산 변동성": f"{annualized_vol:.1%}"
            })

    final_columns = [
        "종목", "현재가", "목표가", "필요 상승률",
        "시나리오", "예상 거래일", "예상 도달일",
        "평균 일수익률", "연환산 변동성"
    ]

    result_df = pd.DataFrame(results).reindex(columns=final_columns)
    return result_df.set_index(["종목", "시나리오"])


# ====================== 실행 영역 ======================
if __name__ == "__main__":
    target_info = {
        "TSLA": 450.0,
        "IONQ": 100.0,
        "SOXL": 200.0
    }

    tickers_list = list(target_info.keys())

    print("📅 보수적 목표가 도달 예측 (NYSE 공휴일 반영)")
    print("   → Conservative를 가장 현실적인 참고값으로 추천합니다.")

    analysis_result = calculate_target_dates(tickers_list, target_info)

    print("\n[분석 결과]")
    print_aligned_table(analysis_result)

    print("\n💡 해석 가이드:")
    print("   • Base         : 최근 20일 평균 추세")
    print("   • Conservative : 변동성 고려 보수적 예측 (추천)")
    print("   • 예상 도달일은 NYSE 공휴일 및 주말 제외 실제 거래일 기준입니다.")