import os
import time
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from loguru import logger


load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
master = os.getenv("MASTER_ADDRESS", wallet.address)

# MAINNET, standard perp dex only.
mainnet_info = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=master)


def get_all_mids():
    """Mid-price map for the standard perp dex."""
    return mainnet_info.all_mids()


def get_book(symbol):
    """Return (best_bid, best_ask, tick, decimals) from the mainnet L2 book."""
    try:
        l2 = mainnet_info.l2_snapshot(symbol)
        bids = l2['levels'][0]
        asks = l2['levels'][1]

        def px(level):
            return float(level['px']) if isinstance(level, dict) else float(level[0])

        best_bid = px(bids[0])
        best_ask = px(asks[0])

        if len(asks) >= 2:
            tick = abs(px(asks[1]) - px(asks[0]))
        elif len(bids) >= 2:
            tick = abs(px(bids[0]) - px(bids[1]))
        else:
            tick = abs(best_ask - best_bid)
        if tick <= 0:
            tick = abs(best_ask - best_bid)

        s = str(asks[0]['px']) if isinstance(asks[0], dict) else str(asks[0][0])
        decimals = len(s.split('.')[1]) if '.' in s else 0

        return best_bid, best_ask, tick, decimals
    except Exception as e:
        logger.warning(f"Book fetch failed for {symbol}: {e}")
        return None, None, None, None


def place_alo_limit(symbol, is_buy, size_tokens, limit_px, reduce_only=False):
    """Post-only limit order. Returns (status, oid, fill_px, fill_sz, err)."""
    order_type = {"limit": {"tif": "Alo"}}
    try:
        result = exchange.order(symbol, is_buy, size_tokens, limit_px, order_type, reduce_only=reduce_only)
        statuses = result.get('response', {}).get('data', {}).get('statuses', [])
        for s in statuses:
            if 'resting' in s:
                return 'resting', s['resting'].get('oid'), None, None, ''
            if 'filled' in s:
                f = s['filled']
                return 'filled', f.get('oid'), float(f.get('avgPx', limit_px)), float(f.get('totalSz', size_tokens)), ''
            if 'error' in s:
                err_msg = s['error']
                if 'immediately' in err_msg.lower() or 'post only' in err_msg.lower():
                    return 'rejected', None, None, None, err_msg
                return 'error', None, None, None, err_msg
        return 'error', None, None, None, f"unexpected response: {result}"
    except Exception as e:
        return 'error', None, None, None, str(e)


def cancel_order(symbol, oid):
    if oid is None:
        return
    try:
        exchange.cancel(symbol, oid)
        logger.info(f"Cancelled resting order {oid} on {symbol}")
    except Exception as e:
        logger.warning(f"Cancel {symbol} oid {oid} failed (may have already filled): {e}")


_last_positions: dict = {}

def get_open_positions():
    """All open positions, flat dict keyed by symbol."""
    global _last_positions
    positions = {}
    try:
        state = mainnet_info.user_state(master)
        for p in state.get('assetPositions', []):
            pos = p.get('position', {})
            coin = pos.get('coin')
            size = float(pos.get('szi', 0))
            entry = float(pos.get('entryPx', 0))
            if size != 0:
                positions[coin] = {
                    "size": size, "entry": entry,
                    "side": "LONG" if size > 0 else "SHORT",
                }
        _last_positions = positions
        return positions
    except Exception as e:
        logger.error(f"Position check error: {e} — returning last known positions")
        return _last_positions


def get_equity():
    """Account value — spot USDC balance (unified account mode)."""
    try:
        spot = mainnet_info.spot_user_state(master)
        for b in spot.get('balances', []):
            if b.get('coin') == 'USDC':
                return float(b.get('total', 0))
        return 0.0
    except Exception as e:
        logger.error(f"Equity read error: {e}")
        return 0.0
