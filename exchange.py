import os
import time
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from loguru import logger

from config import MAKER_WAIT_SECONDS, SLIPPAGE

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
mainnet_info = Info(constants.MAINNET_API_URL, skip_ws=True)
testnet_info = Info(constants.TESTNET_API_URL, skip_ws=True)
exchange = Exchange(wallet, constants.TESTNET_API_URL)

_TESTNET_COINS = None
_TESTNET_COINS_TS = 0.0
_TESTNET_COINS_TTL = 24 * 3600


def get_testnet_coins():
    global _TESTNET_COINS, _TESTNET_COINS_TS
    if _TESTNET_COINS is None or time.time() - _TESTNET_COINS_TS > _TESTNET_COINS_TTL:
        try:
            meta = testnet_info.meta()
            _TESTNET_COINS = {a['name'] for a in meta['universe']}
            _TESTNET_COINS_TS = time.time()
            logger.info(f"Testnet supports {len(_TESTNET_COINS)} tradeable perps")
        except Exception as e:
            logger.error(f"Could not fetch testnet coins: {e}")
            if _TESTNET_COINS is None:
                _TESTNET_COINS = set()
    return _TESTNET_COINS


def get_testnet_price_map():
    price_map = {}
    try:
        ctxs_data = testnet_info.meta_and_asset_ctxs()
        universe = ctxs_data[0]['universe']
        ctxs = ctxs_data[1]
        for i, a in enumerate(universe):
            if i < len(ctxs):
                ctx = ctxs[i]
                mid = ctx.get('midPx') or ctx.get('markPx')
                oracle = ctx.get('oraclePx')
                if mid and oracle:
                    mid = float(mid); oracle = float(oracle)
                    gap_pct = abs(mid - oracle) / oracle * 100 if oracle else 0
                    price_map[a['name']] = {"price": mid, "oracle": oracle, "gap_pct": gap_pct}
    except Exception as e:
        logger.warning(f"Could not fetch testnet price map: {e}")
    return price_map


def get_testnet_book(symbol):
    """Return (best_bid, best_ask, tick, decimals) from the testnet L2 book."""
    try:
        l2 = testnet_info.l2_snapshot(symbol)
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


def get_open_positions():
    positions = {}
    try:
        state = testnet_info.user_state(wallet.address)
        for p in state.get('assetPositions', []):
            pos = p.get('position', {})
            coin = pos.get('coin')
            size = float(pos.get('szi', 0))
            entry = float(pos.get('entryPx', 0))
            if size != 0:
                positions[coin] = {
                    "size": size, "entry": entry,
                    "side": "LONG" if size > 0 else "SHORT"
                }
    except Exception as e:
        logger.error(f"Position check error: {e}")
    return positions


def get_equity():
    try:
        user_state = testnet_info.user_state(wallet.address)
        return float(user_state['marginSummary']['accountValue'])
    except Exception as e:
        logger.error(f"Equity read error: {e}")
        return 0.0


def wait_until(symbol, want_open, seconds):
    """Poll positions until symbol is open (want_open=True) or closed (False), or timeout."""
    waited = 0
    step = 10
    while waited < seconds:
        time.sleep(min(step, seconds - waited))
        waited += step
        is_open = symbol in get_open_positions()
        if is_open == want_open:
            return True
    return (symbol in get_open_positions()) == want_open
