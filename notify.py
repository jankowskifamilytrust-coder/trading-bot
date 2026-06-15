import os
import requests
from loguru import logger


def send_telegram(message):
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram not configured — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
