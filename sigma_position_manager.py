import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# 인코딩 설정 (한글 깨짐 방지)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)


# ===================================================================
# 설정 및 장부 로드/저장
# ===================================================================
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        sys.exit(1)


def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [],
            "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []
        }


def save_config(cfg):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")


# ===================================================================
# ledger → config.json 자동 포지션 동기화
# ===================================================================
def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    for key, buy_key, sell_key in [
        ("LONG",  "SOXL_LONG_BUY",  "SOXL_LONG_SELL"),
        ("SHORT", "SOXL_SHORT_BUY", "SOXL_SHORT_SELL"),
    ]:
        buys = ledger.get(buy_key, [])
        sells = ledger.get(sell_key, [])

        total_buy_qty = sum(item.get("qty", 0) for item in buys)
        total_sell_qty = sum(item.get("qty", 0) for item in sells)
        hold_qty = max(total_buy_qty - total_sell_qty, 0)

        total_buy_amt = sum(item.get("total_amount", 0) for item in buys)
        avg_price = round(total_buy_amt / total_buy_qty, 4) if total_buy_qty > 0 else 0.0

        pos[f"CURRENT_CASTS_{key}"] = len(buys)
        pos[f"TOTAL_SHARES_{key}"] = hold_qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price

    save_config(cfg)


# ===================================================================
# 🔄 yfinance 데이터 기반 반기 롤링 로그수익률 자동 업데이트 엔진
# ===================================================================
def auto_update_rolling_sigma(cfg):
    vix_cfg = cfg.setdefault("VIX_CONFIG", {}).setdefault("LONG", {})
    last_update_str = vix_cfg.get("LAST_SIGMA_UPDATE", "1970-01-01")
    
    try:
        last_update = datetime.strptime(last_update_str, "%Y-%m-%d").date()
    except ValueError:
        last_update = datetime.date(1970, 1, 1)
        
    today = datetime.now(ZoneInfo("America/New_York")).date()
    
    # 마지막 자동 업데이트 이후 180일(반기)이 경과했거나 값이 없으면 리프레시 실행
    if (today - last_update).days >= 180 or "FIXED_SIGMA" not in vix_cfg:
        print("\n🔄 [반기 롤링] 마지막 업데이트 이후 180일이 경과하여 -1.5시그마를 자동 갱신합니다.")
        try:
            import yfinance as yf
            soxl = yf.Ticker("SOXL")
            df = soxl.history(period="18m", auto_adjust=False)
            
            if len(df) < 253:
                print("⚠️ 데이터 부족으로 시그마 자동 갱신을 건너뜁니다.")
                return
                
            df_recent = df.tail(253).copy()
            
            df_recent['Log_Return'] = np.log(df_recent['Close'] / df_recent['Close'].shift(1))
            df_recent = df_recent.dropna()
            
            log_avg = df_recent['Log_Return'].mean()
            log_stdev = df_recent['Log_Return'].std()
            
            calculated_sigma = -(log_avg - 1.5 * log_stdev)
            
            vix_cfg["FIXED_SIGMA"] = round(float(calculated_sigma), 4)
            vix_cfg["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            save_config(cfg)
            
            print(f"✅ -1.5시그마 자동 설정 완료: {calculated_sigma*100:.2f}% (기준일: {today})\n")
        except Exception as e:
            print(f"❌ yfinance 기반 시그마 자동 연산 실패: {e}")


# ===================================================================
# 미국 시장 모드 및 서머타임 판단 (zoneinfo 탑재로 서머타임 자동 추적)
# ===================================================================
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    is_dst = now_ny.dst() != timedelta(0)

    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} {'(EDT/서머타임 적용됨)' if is_dst else '(EST/표준시 적용됨)'}")

    if 9.5 <= hour < 16.0:
        return "장중", now_ny
    else:
        return "장전", now_ny


# ===================================================================
# 시세 데이터 수집 (야후 파이낸스)
# ===================================================================
def get_market_data(mode):
    try:
        import yfinance as yf
        soxl = yf.Ticker("SOXL")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 1:
            return None, None, None

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())

        prev_close = float(hist['Close'].iloc[-2] if is_today and len(hist) > 1 else hist['Close'].iloc[-1])

        if mode == "장전":
            current_open = prev_close
        else:
            current_open = float(getattr(soxl.fast_info, 'open', None) or 
                               (hist['Open'].iloc[-1] if is_today else prev_close))

        current_price = float(soxl.fast_info.last_price)

        return float(prev_close), float(current_open), float(current_price)
    except Exception as e:
        print(f"❌ 시세 조회 실패: {e}")
        return None, None, None


# ===================================================================
# 자금 소진율 체크 → 검증된 21회 쿼터 변수 정밀 연동 엔진
# ===================================================================
def check_burn_rate_and_adjust_loc(base_loc_price, cfg, mode):
    if mode != "장중":
        return base_loc_price

    pos = cfg["POSITIONS"]["SOXL"]
    total_cap = pos.get("TOTAL_CAPITAL_LONG", 0)
    if total_cap <= 0:
        return base_loc_price

    ledger = load_ledger()
    executed = sum(item.get("total_amount", 0) for item in ledger.get("SOXL_LONG_BUY", []))

    quota_long = pos.get("ANNUAL_QUOTA_LONG", 21)       
    current_casts = pos.get("CURRENT_CASTS_LONG", 4)     
    
    burn_rate = executed / total_cap
    expected_rate = current_casts / quota_long          
    
    print(f"💰 LONG 소진율: {burn_rate*100:.1f}% (이론상 권장 소진율: {expected_rate*100:.1f}%)")

    if burn_rate > expected_rate * 1.35:
        adj = 0.99
        print(f"⚠️ 소진 빠름 → LOC 가격 제한선을 {adj:.1%} 낮춤 (더 싸게 사도록 조절)")
    elif burn_rate < expected_rate * 0.65:
        adj = 1.008
        print(f"📈 소진 느림 → LOC 가격 제한선을 {adj:.1%} 높임 (체결 확률 상승 조절)")
    else:
        adj = 1.0
        print("📊 소진 속도 정상범위 안착")

    return round(base_loc_price * adj, 2)


# ===================================================================
# 디스코드 전송 메신저
# ===================================================================
def send_discord(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return

    payload = {
        "content": f"<@{user_id}> " if user_id else "",
        "embeds": [{
            "title": title,
            "description": content,
            "color": 15158332 if "🚨" in title or "[반기 결산]" in title else 3447003,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(webhook_url, json=payload, timeout=15)
        if res.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")


# ===================================================================
# 🎯 [신규] 반기 말(6월/12월 마지막 영업일) 디스코드 브리핑 엔진
# ===================================================================
def check_and_send_semiannual_report(webhook_url, user_id):
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    # 장 마감 직후 시점(현지 시각 16시~17시 사이)에 딱 한 번 리포트 송신
    if now_ny.month in [6, 12] and now_ny.hour == 16:
        import yfinance as yf
        try:
            # 오늘이 이번 달의 마지막 거래일(영업일)인지 확인
            ticker = yf.Ticker("SOXL")
            df = ticker.history(period="1mo")
            last_trading_day = df.index[-1].date()
            
            if now_ny.date() == last_trading_day:
                title = f"[반기 결산] 데이터 롤링 및 시그마 최적화 보고"
                content = (
                    "대표님, 6개월(반기)이 경과하여 시스템의 핵심 축인 로그수익률 표준편차(σ)의 갱신 시점이 도래했습니다.\n\n"
                    "지난 반기 동안의 반도체 시장 변동성(숨소리)을 정확히 반영하기 위해 yfinance 엔진이 최근 252거래일의 "
                    "데이터를 기반으로 **[-1.5 시그마] 절댓값을 최신화**했습니다.\n\n"
                    "변해버린 시장의 체력에 맞추어 향후 6개월간 적용될 새로운 하방 그물망(LOC 제한선)의 기준점이 정밀 재조정되었으니, "
                    "`config.json` 파일의 `FIXED_SIGMA` 수치와 금일 터미널 로그를 확인하시고 최종 가동을 승인해 주시기 바랍니다.\n\n"
                    "* 통계 확률과 자연의 이치는 결코 흔들리지 않습니다. *"
                )
                print("📢 반기 마지막 영업일 마감 브리핑을 디스코드로 송신합니다.")
                send_discord(webhook_url, user_id, title, content)
        except Exception as e:
            print(f"⚠️ 반기 영업일 판별 중 오류 발생: {e}")


# ===================================================================
# 메인 실행 컨트롤러
# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()

    print("======================================================================")
    print(f"📡 sigma_position_manager.py [{mode} 모드]")
    print("======================================================================\n")

    cfg = load_config()
    
    # 1단계: 반기 주기 체크 후 필요한 경우 자동 롤링 시그마 연산 실행
    auto_update_rolling_sigma(cfg)
    cfg = load_config()                     
    
    # 2단계: 장부(ledger) 데이터를 기반으로 실시간 계좌 정보 동기화
    update_positions_from_ledger(cfg)
    cfg = load_config()                     

    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # 🎯 6월/12월 마지막 영업일 장 마감 리포트 조건 체크 실행
    check_and_send_semiannual_report(webhook_url, user_id)

    SIGMA_1_5 = vix_cfg.get("FIXED_SIGMA", 0.0832)
    TAKE_PROFIT_RATIO = vix_cfg.get("TAKE_PROFIT_RATIO", 0.30)
    last_update_date = vix_cfg.get("LAST_SIGMA_UPDATE", "미정")

    prev_close, current_open, current_price = get_market_data(mode)
    if prev_close is None:
        print("❌ 야후 파이낸스 시세 데이터를 가져올 수 없습니다.")
        return

    base_loc = prev_close * np.exp(-SIGMA_1_5)

    # 3단계: 소진율 추적에 의한 최종 LOC 체결 예정가 미세조정 반영
    final_loc = check_burn_rate_and_adjust_loc(base_loc, cfg, mode)

    price_label = "현재가" if mode == "장중" else "전일 종가"

    print(f"📌 {price_label}     : ${current_price:.2f}")
    print(f"📌 당일 시가     : ${current_open:.2f}")
    print(f"📌 적용 -1.5시그마 : {SIGMA_1_5*100:.2f}% (최근 자동갱신일: {last_update_date})")
    print(f"📌 LOC 예정가    : ${final_loc:.2f}\n")

    # ====================== Discord 브리핑 문자열 생성 ======================
    discord_lines = []
    any_triggered = False

    header = f"**{price_label}: ${current_price:.2f}**\n📍 당일 시가: **${current_open:.2f}** | 적용 시그마: {SIGMA_1_5*100:.2f}% (갱신일: {last_update_date})\n{now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
    discord_lines.append(header)

    shares_long = pos_cfg.get("TOTAL_SHARES_LONG", 0)
    avg_long = pos_cfg.get("MY_AVG_PRICE_LONG", 0)

    long_msg = "**🟢 [LONG] 매수 계좌**\n"
    if shares_long > 0 and avg_long > 0:
        ret = (current_price - avg_long) / avg_long
        long_msg += f"• {shares_long}주 / 평단 ${avg_long:.4f} / 수익률 {ret*100:+.2f}%\n"
        if ret >= TAKE_PROFIT_RATIO:
            long_msg += f"🚨 **[+30% 부분 익절] → {int(shares_long*0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음\n"

    if mode == "장중":
        long_msg += f"• LOC 매수 예정가: **${final_loc:.2f}**"

    discord_lines.append(long_msg)

    shares_short = pos_cfg.get("TOTAL_SHARES_SHORT", 0)
    avg_short = pos_cfg.get("MY_AVG_PRICE_SHORT", 0)

    short_msg = "**🔵 [SHORT] 매도 계좌**\n"
    if shares_short > 0 and avg_short > 0:
        ret = (current_price - avg_short) / avg_short
        tp_price = avg_short * (1 + TAKE_PROFIT_RATIO)
        short_msg += f"• {shares_short}주 / 평단 ${avg_short:.4f} / 수익률 {ret*100:+.2f}%\n"
        if ret >= TAKE_PROFIT_RATIO:
            short_msg += f"🚨 **[+30% 부분 익절] → {int(shares_short*0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
        if mode == "장중":
            short_msg += f"• 익절 지정가: **${tp_price:.2f}**"
    else:
        short_msg += "• 보유 물량 없음"

    discord_lines.append(short_msg)

    full_content = "\n\n".join(discord_lines)
    title = (f"🚨 [부분 익절 발동] {mode} 모드 - 50% 매도 권장"
             if any_triggered else
             f"[{mode} 모드 브리핑]")

    send_discord(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()