import requests
from typing import Optional
import os
from dotenv import load_dotenv
import requests
from config.config import logger
import socket
import datetime


load_dotenv()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")


# ✅ 웹훅 주소 로딩 함수 (.env 기반)
def load_webhook() -> Optional[str]:
    webhook_url: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("❌ DISCORD_WEBHOOK_URL이 .env에 설정되지 않았습니다.")
    return webhook_url

# ✅ 디스코드 알림 전송
def send_discord_alert(
    ticker: str,
    price: Optional[float] = None,
    alert_type: Optional[str] = None,
    strategy_data: Optional[dict] = None,
    extra: Optional[str] = None,
    live_mode: bool = False,
    webhook_url: Optional[str] = None
) -> None:
    if not live_mode:
        return

    url = webhook_url or load_webhook()
    if not url:
        logger.error("❌ DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")
        return

    # 📦 메시지 구성
    content = extra or (
        f"📊 전략 알림: **{ticker}**\n"
        f"SL: {strategy_data.get('SL', 'N/A')}, TP: {strategy_data.get('TP', 'N/A')}\n"
        f"Live Mode: {live_mode}"
        if alert_type == "strategy" and strategy_data else
        f"💡 **{ticker}**가 2SD 매수 조건을 충족했습니다.\n"
        f"현재 가격: {price:.2f} → 전략가의 판단이 필요합니다 ⚔️📈"
    )

    payload = {"content": content}

    try:
        response = requests.post(url, json=payload)  # response: requests.Response
        if response.status_code != 204:
            logger.warning(f"⚠️ 디스코드 알림 실패 → {response.status_code}: {response.text}")
    except Exception as e:
        logger.warning(f"⚠️ 디스코드 요청 오류 → {type(e).__name__}: {e}")

# ✅ 생존 핑 전송
def send_ping(live_mode: bool = True) -> None:
    webhook_url = load_webhook()
    # Local heartbeat (always shown in console/log)
    now_utc = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    host = socket.gethostname()
    logger.info(f"📡 전략 시스템 Ping: 정상 작동 중 — {now_utc} — {host}")

    if not webhook_url:
        logger.warning("⚠️ Ping 웹훅 미설정: Discord에 전송되지 않았습니다.")
        return

    # Minimal payload to keep webhook active (timestamp + host)
    content = f"📡 전략 시스템 Ping: 정상 작동 중\n시간(UTC): {now_utc}\n호스트: {host}"
    send_discord_alert(
        ticker="webhook_ping",
        extra=content,
        live_mode=True,  # force send for heartbeat
        webhook_url=webhook_url
    )
    logger.info("✅ Discord Ping 메시지 전송 완료.")