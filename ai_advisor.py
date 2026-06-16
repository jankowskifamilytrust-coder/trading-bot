"""
Advisory-only AI verdict on entry signals — logs what the model would have
said next to the real (rule-based) decision, without blocking or sizing
trades. The bot's entries remain fully rule-based; this is purely a
side-channel to accumulate evidence on whether the verdict would have
helped, before ever wiring it into the decision path.
"""
import json
import os
from datetime import datetime

from anthropic import Anthropic
from loguru import logger

from config import ADVISOR_LOG

_client = None
_MODEL = "claude-haiku-4-5-20251001"


def _get_client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _build_prompt(symbol, is_long, pb_reason, data):
    direction = "LONG" if is_long else "SHORT"
    adx_data  = data.get('adx', {})
    return (
        f"A systematic trend-following strategy wants to open a {direction} "
        f"position on {symbol}.\n\n"
        "Signal context:\n"
        f"- Daily Supertrend direction: {data.get('supertrend', {}).get('direction', 'unknown')}\n"
        f"- Daily ADX: {adx_data.get('adx', 0):.1f} (+DI={adx_data.get('plus_di', 0):.1f} "
        f"-DI={adx_data.get('minus_di', 0):.1f})\n"
        f"- Funding rate: {data.get('funding_data', {}).get('funding', 0):.4f}%\n"
        f"- Current price: {data.get('tn_price') or data.get('price', 0)}\n"
        f"- 4h EMA: {data.get('ema_entry')}\n"
        f"- Daily EMA slope: {data.get('daily_slope', 'unknown')}\n"
        f"- Entry trigger: {pb_reason}\n\n"
        "Respond with strict JSON only, no other text: "
        '{"verdict": "YES" or "NO", "reasoning": "<one sentence>"}\n'
        "This is advisory-only logging — your verdict does not block the trade."
    )


def get_advisor_verdict(symbol, is_long, pb_reason, data):
    try:
        client = _get_client()
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": _build_prompt(symbol, is_long, pb_reason, data)}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        return parsed.get("verdict", "UNKNOWN"), parsed.get("reasoning", ""), None
    except Exception as e:
        return "ERROR", "", str(e)


def log_advisor_verdict(symbol, is_long, pb_reason, data):
    verdict, reasoning, error = get_advisor_verdict(symbol, is_long, pb_reason, data)
    entry = {
        "t": datetime.now().isoformat(),
        "symbol": symbol,
        "direction": "LONG" if is_long else "SHORT",
        "pb_reason": pb_reason,
        "verdict": verdict,
        "reasoning": reasoning,
        "error": error,
    }
    try:
        os.makedirs(os.path.dirname(ADVISOR_LOG), exist_ok=True)
        with open(ADVISOR_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Advisor log write failed: {e}")

    if error:
        logger.warning(f"AI advisor call failed for {symbol}: {error}")
    else:
        logger.info(f"AI advisor [{symbol} {entry['direction']}]: {verdict} — {reasoning}")
    return entry
