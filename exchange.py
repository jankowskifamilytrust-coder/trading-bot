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

# MAINNET-only, dual-dex: the standard perp dex ("") and the xyz HIP-3 builder
# dex. Building Info/Exchange with perp_dexs=["", "xyz"] lets a single object
# serve candles for both dexes and auto-route order()/market_close()/
# update_leverage()/l2_snapshot() to the right dex off the "xyz:" name prefix.
# Note: all_mids() and user_state() are per-dex and take a dex= argument.
mainnet_info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=["", "xyz"])
exchange = Exchange(wallet, constants.MAINNET_API_URL, perp_dexs=["", "xyz"])

DEXES = ["", "xyz"]


def dex_of(sym):
    """Dex a symbol trades on, derived from its name prefix."""
    return "xyz" if sym.startswith("xyz:") else ""


def get_all_mids():
    """Merged mid-price map across both dexes. xyz keys keep the 'xyz:' prefix,
    so there is no key collision with standard symbols."""
    mids = {}
    for d in DEXES:
        try:
            mids.update(mainnet_info.all_mids(dex=d))
        except Exception as e:
            logger.warning(f"all_mids dex={d!r} failed: {e}")
    return mids


def meta_and_ctxs(dex=""):
    """Return [meta, asset_ctxs] for a dex. Standard uses the SDK helper; xyz
    uses a raw /info post (the SDK helper is standard-dex only)."""
    if dex == "":
        return mainnet_info.meta_and_asset_ctxs()
    return mainnet_info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex})


def get_book(symbol):
    """Return (best_bid, best_ask, tick, decimals) from the mainnet L2 book.
    l2_snapshot resolves xyz: symbols via the dual-dex name map."""
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
    """All open positions across both dexes, flat dict keyed by symbol (xyz: keys
    carry the prefix). Each entry tagged with its 'dex' for per-dex grouping."""
    global _last_positions
    positions = {}
    try:
        for d in DEXES:
            state = mainnet_info.user_state(wallet.address, dex=d)
            for p in state.get('assetPositions', []):
                pos = p.get('position', {})
                coin = pos.get('coin')
                size = float(pos.get('szi', 0))
                entry = float(pos.get('entryPx', 0))
                if size != 0:
                    positions[coin] = {
                        "size": size, "entry": entry,
                        "side": "LONG" if size > 0 else "SHORT",
                        "dex": d,
                    }
        _last_positions = positions
        return positions
    except Exception as e:
        logger.error(f"Position check error: {e} — returning last known positions")
        return _last_positions


def get_equity(dex=""):
    """Account value for a single dex (HIP-3 dexes have isolated clearinghouses)."""
    try:
        user_state = mainnet_info.user_state(wallet.address, dex=dex)
        return float(user_state['marginSummary']['accountValue'])
    except Exception as e:
        logger.error(f"Equity read error (dex={dex!r}): {e}")
        return 0.0


def get_equity_by_dex():
    """Per-dex equity map, e.g. {'': 1234.0, 'xyz': 56.0}."""
    return {d: get_equity(d) for d in DEXES}


