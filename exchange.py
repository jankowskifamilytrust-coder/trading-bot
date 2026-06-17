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

        # Tick: Hyperliquid L2 levels are price-aggregated and skip empty prices,
        # so the gap between the top two levels is often several ticks and would
        # over-estimate the increment. Use the SMALLEST nonzero gap across the
        # nearest levels as the best estimate of the true tick.
        def min_gap(levels):
            prices = sorted({px(l) for l in levels})
            gaps = [b - a for a, b in zip(prices, prices[1:]) if b - a > 0]
            return min(gaps) if gaps else 0.0

        candidate_gaps = [g for g in (min_gap(asks[:10]), min_gap(bids[:10])) if g > 0]
        tick = min(candidate_gaps) if candidate_gaps else abs(best_ask - best_bid)
        if tick <= 0:
            tick = abs(best_ask - best_bid)

        # Decimals (for display): take the max decimal places across nearby levels
        # rather than a single level whose trailing zeros may be trimmed.
        def dec_of(level):
            s = str(level['px']) if isinstance(level, dict) else str(level[0])
            return len(s.split('.')[1]) if '.' in s else 0
        decimals = max((dec_of(l) for l in (asks[:10] + bids[:10])), default=0)

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
                # avgPx/totalSz may be present-but-null; .get's default only applies to
                # a missing key, so float(None) would raise and mislabel a real fill as
                # an error. Fall back explicitly when the value is None.
                avg_px = f.get('avgPx')
                tot_sz = f.get('totalSz')
                fill_px = float(avg_px) if avg_px is not None else limit_px
                fill_sz = float(tot_sz) if tot_sz is not None else size_tokens
                return 'filled', f.get('oid'), fill_px, fill_sz, ''
            if 'error' in s:
                err_msg = s['error']
                if 'immediately' in err_msg.lower() or 'post only' in err_msg.lower():
                    return 'rejected', None, None, None, err_msg
                return 'error', None, None, None, err_msg
        return 'error', None, None, None, f"unexpected response: {result}"
    except Exception as e:
        return 'error', None, None, None, str(e)


_STOP_LIMIT_SLIP = 0.10  # market-trigger limit buffer past the trigger to guarantee fill

def _round_px(px, px_decimals):
    """Apply Hyperliquid's ≤5-significant-figure rule and, when known, the perp
    decimal-place rule (≤ 6 - szDecimals) so the price isn't rejected as invalid."""
    p = float(f"{px:.5g}")
    if px_decimals is not None:
        p = round(p, max(0, px_decimals))
    return p


def place_stop_market(symbol, is_buy, size_tokens, trigger_px, px_decimals=None, reduce_only=True):
    """Reduce-only stop-MARKET trigger order. `is_buy` is the side of the CLOSING order
    (buy to close a short, sell to close a long). Returns (oid_or_None, err).

    The order rests on the exchange and triggers a market close when price crosses
    trigger_px, so the stop is enforced continuously rather than only at each cycle.
    Never raises — the caller keeps the soft chandelier as a backstop.
    """
    try:
        tpx = _round_px(trigger_px, px_decimals)
        if tpx <= 0:
            return None, f"non-positive trigger px {trigger_px}"
        # Limit after trigger: set past the trigger in the fill direction so the
        # (market) stop always fills. Buy closes a short → limit above; sell → below.
        raw_limit = tpx * (1 + _STOP_LIMIT_SLIP) if is_buy else tpx * (1 - _STOP_LIMIT_SLIP)
        limit_px = _round_px(raw_limit, px_decimals)
        order_type = {"trigger": {"triggerPx": tpx, "isMarket": True, "tpsl": "sl"}}
        result = exchange.order(symbol, is_buy, size_tokens, limit_px, order_type,
                                reduce_only=reduce_only)
        statuses = result.get('response', {}).get('data', {}).get('statuses', [])
        for s in statuses:
            if 'resting' in s:
                return s['resting'].get('oid'), ''
            if 'error' in s:
                return None, s['error']
            if 'filled' in s:   # shouldn't fill immediately, but treat as placed
                return s['filled'].get('oid'), ''
        return None, f"unexpected response: {result}"
    except Exception as e:
        return None, str(e)


def cancel_order(symbol, oid):
    """Cancel a resting order. Returns True if acknowledged, False otherwise."""
    if oid is None:
        return False
    try:
        exchange.cancel(symbol, oid)
        logger.info(f"Cancelled resting order {oid} on {symbol}")
        return True
    except Exception as e:
        logger.warning(f"Cancel {symbol} oid {oid} failed (may have already filled): {e}")
        return False


_last_positions: dict = {}
_positions_loaded = False

def get_open_positions():
    """All open positions, flat dict keyed by symbol.

    Returns None on API failure — NOT stale data — so callers can fail closed
    (a silent stale/empty result is indistinguishable from a genuinely flat
    account and can leave a live position unmanaged or fabricate a phantom one).
    Use last_known_positions() explicitly for a degraded-mode fallback.
    """
    global _last_positions, _positions_loaded
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
        _positions_loaded = True
        return positions
    except Exception as e:
        logger.error(f"Position check error: {e} — read FAILED (returning None)")
        return None


def last_known_positions():
    """Most recent successfully-read positions, or None if never read this run.

    For degraded-mode (API-down) stop evaluation only — the data may be stale.
    """
    return _last_positions if _positions_loaded else None


def get_open_orders():
    """Live resting orders for the master account, or None on API failure."""
    try:
        return mainnet_info.open_orders(master)
    except Exception as e:
        logger.warning(f"Open-orders fetch failed: {e}")
        return None


def get_equity():
    """Account value — spot USDC balance (unified account mode).

    Returns None on API failure so callers can distinguish 'read failed' from a
    genuine zero balance and fail closed (e.g. keep the portfolio-heat gate from
    silently disabling itself when equity reads as 0 on a transient error).
    """
    try:
        spot = mainnet_info.spot_user_state(master)
        for b in spot.get('balances', []):
            if b.get('coin') == 'USDC':
                return float(b.get('total', 0))
        return 0.0
    except Exception as e:
        logger.error(f"Equity read error: {e}")
        return None
