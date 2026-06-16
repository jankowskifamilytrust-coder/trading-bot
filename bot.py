import os
import atexit
import math
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger

from config import (
    TOP_N, STABLECOINS, MAX_POSITIONS, LEVERAGE, INTERVAL_MINUTES,
    SLIPPAGE, SETTLE_SECONDS, MAX_ORACLE_GAP_PCT,
    RISK_PER_TRADE_PCT, MAX_PORTFOLIO_RISK_PCT, MIN_NOTIONAL_USD, STOP_ATR_MULT,
    MAX_NOTIONAL_PCT, MIN_NOTIONAL_PCT,
    TRADE_LOG, EQUITY_LOG, TRAILING_STOP_LOG, LOC_LOG,
    VOLUME_RANK_LOG, VOLUME_RANK_TTL_HOURS, START_EQUITY_LOG,
    SUPERTREND_PERIOD, SUPERTREND_MULT,
    ADX_PERIOD, ADX_THRESHOLD,
    RSI_PERIOD, RSI_LONG_THRESHOLD, RSI_SHORT_THRESHOLD, RSI_LOOKBACK,
    EMA_PERIOD, EMA_BAND_PCT,
    FUNDING_LONG_MAX, FUNDING_SHORT_MIN, ADX_DECAY_EXIT,
    VOLUME_CONFIRM_RATIO, STRUCT_STOP_BUFFER,
)
from notify import send_telegram
from signals import (
    compute_daily_vol, compute_atr,
    compute_funding, compute_supertrend, compute_adx, compute_rsi, compute_ema,
    compute_volume_ratio, compute_struct_stops,
)
from exchange import (
    mainnet_info, exchange as hl_exchange,
    get_testnet_coins, get_testnet_price_map, get_testnet_book,
    place_alo_limit, cancel_order, get_open_positions, get_equity,
)

load_dotenv()

# ─── Utilities ────────────────────────────────────────────────────────────────

def load_json(filepath, default):
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(filepath, data):
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filepath)

def init_files():
    os.makedirs("data", exist_ok=True)
    for filepath, default in [
        (TRADE_LOG, []), (EQUITY_LOG, []), (TRAILING_STOP_LOG, {}),
        (LOC_LOG, {}), (VOLUME_RANK_LOG, {}),
    ]:
        try:
            with open(filepath, "r") as f:
                json.load(f)
        except Exception:
            save_json(filepath, default)
            logger.info(f"Created {filepath}")

def sleep_until_next_hour():
    now = datetime.now()
    next_run = (INTERVAL_MINUTES - (now.minute % INTERVAL_MINUTES)) * 60 - now.second
    next_time = datetime.fromtimestamp(time.time() + next_run).strftime('%H:%M:%S')
    logger.info(f"Sleeping {next_run//60}m {next_run%60}s — next cycle at {next_time}")
    time.sleep(next_run)

# ─── Dynamic Symbol Selection ─────────────────────────────────────────────────

def build_volume_ranking():
    """
    Fetch 30-day daily candles for all testnet-tradeable non-stablecoin symbols
    and compute average daily dollar volume (v × close). Pre-filters to top-50
    by 24h vol to bound the number of API calls. Results cached in VOLUME_RANK_LOG.
    Returns [[symbol, avg_vol_usd], ...] sorted descending.
    """
    logger.info("Building 30-day avg volume ranking (runs once per day)...")
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - 30 * 24 * 60 * 60 * 1000

    meta_ctxs     = mainnet_info.meta_and_asset_ctxs()
    universe      = meta_ctxs[0]['universe']
    ctxs          = meta_ctxs[1]
    testnet_coins = get_testnet_coins()

    # Candidate pool: testnet-tradeable non-stablecoins, pre-sorted by 24h vol
    candidates = []
    for i, asset in enumerate(universe):
        name = asset['name']
        if name.upper() in STABLECOINS or i >= len(ctxs):
            continue
        if testnet_coins and name not in testnet_coins:
            continue
        candidates.append((name, float(ctxs[i].get('dayNtlVlm', 0))))
    candidates.sort(key=lambda x: x[1], reverse=True)
    candidates = candidates[:50]

    def fetch_30d(sym):
        try:
            candles = mainnet_info.candles_snapshot(sym, "1d", start_ms, end_ms)
            if not candles:
                return sym, 0.0
            avg = sum(float(c['v']) * float(c['c']) for c in candles) / len(candles)
            return sym, avg
        except Exception as e:
            logger.warning(f"Volume rank: {sym} failed — {e}")
            return sym, 0.0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_30d, sym): sym for sym, _ in candidates}
        results = [f.result() for f in as_completed(futures)]

    results.sort(key=lambda x: x[1], reverse=True)
    if all(vol == 0.0 for _, vol in results):
        logger.error("Volume ranking: all symbols returned 0 volume — possible mass API failure")
        send_telegram("⚠️ <b>Volume ranking failed</b>\nAll symbols returned 0 volume — API may be down")
    ranking = [[sym, vol] for sym, vol in results]
    save_json(VOLUME_RANK_LOG, {"computed_at": datetime.now().isoformat(), "ranking": ranking})
    logger.info(f"30-day ranking built. Top 10: {[r[0] for r in ranking[:10]]}")
    return ranking


def get_top_symbols(top_n=TOP_N, extra_symbols=None):
    extra_symbols = extra_symbols or []
    try:
        # Load cached ranking or rebuild if missing / stale
        cached  = load_json(VOLUME_RANK_LOG, {})
        ranking = None
        if cached.get('computed_at') and cached.get('ranking'):
            age_h = (datetime.now() - datetime.fromisoformat(cached['computed_at'])).total_seconds() / 3600
            if age_h < VOLUME_RANK_TTL_HOURS:
                ranking = cached['ranking']

        if ranking is None:
            ranking = build_volume_ranking()

        top = [sym for sym, _ in ranking[:top_n]]

        for sym in extra_symbols:
            if sym not in top:
                top.append(sym)
                logger.info(f"  Keeping {sym} (open position)")

        logger.info(f"Top {top_n} testnet perps by 30-day avg $ volume:")
        for rank, (sym, vol) in enumerate(ranking[:top_n], 1):
            logger.info(f"  #{rank} {sym}: ${vol:,.0f}/day")

        return top
    except Exception as e:
        logger.error(f"Failed to rank symbols: {e}")
        return list(set(["BTC", "ETH"] + extra_symbols))

# ─── Market Data ──────────────────────────────────────────────────────────────

def get_symbol_data(symbol, max_retries=3, retry_delay=5, asset_ctxs=None, mids=None):
    for attempt in range(1, max_retries + 1):
        try:
            _mids = mids if mids is not None else mainnet_info.all_mids()
            if symbol not in _mids:
                logger.warning(f"{symbol} not found on Hyperliquid")
                return None

            price = float(_mids[symbol])
            end_time = int(time.time() * 1000)
            start_time = end_time - (48 * 60 * 60 * 1000)
            candles = mainnet_info.candles_snapshot(symbol, "1h", start_time, end_time)

            # Drop any partially-formed bar — all signals must be computed on the
            # most recent *fully closed* 60-min bar only.
            interval_ms = INTERVAL_MINUTES * 60 * 1000
            if candles:
                try:
                    age_ms = end_time - int(candles[-1]['t'])
                    if age_ms < interval_ms:
                        candles = candles[:-1]
                        logger.debug(
                            f"{symbol}: forming bar dropped ({age_ms//60000}min old); "
                            f"closed bar t={candles[-1]['t'] if candles else 'n/a'}"
                        )
                    else:
                        logger.debug(
                            f"{symbol}: last bar confirmed closed (age {age_ms//60000}min), t={candles[-1]['t']}"
                        )
                except (KeyError, TypeError):
                    pass  # no 't' field — can't verify, proceed
            if not candles:
                logger.warning(f"{symbol}: no closed bars after dropping forming bar — skipping")
                return None

            universe = asset_ctxs[0]['universe'] if asset_ctxs else None
            if universe is None:
                try:
                    universe = mainnet_info.meta()['universe']
                except Exception:
                    universe = []
            asset_info = next((a for a in universe if a['name'] == symbol), None)
            sz_decimals = int(asset_info['szDecimals']) if asset_info else 3

            # Daily candles — shared by Supertrend, ADX, and ATR sizing
            daily_candles = None
            try:
                d_end = int(time.time() * 1000)
                d_start = d_end - (90 * 24 * 60 * 60 * 1000)
                daily_candles = mainnet_info.candles_snapshot(symbol, "1d", d_start, d_end)
                if daily_candles:
                    try:
                        if d_end - int(daily_candles[-1]['t']) < 24 * 60 * 60 * 1000:
                            daily_candles = daily_candles[:-1]
                    except (KeyError, TypeError):
                        pass
                supertrend = compute_supertrend(daily_candles)
                adx = compute_adx(daily_candles)
            except Exception as e:
                logger.warning(f"{symbol}: daily candle fetch failed — Supertrend/ADX neutral: {e}")
                supertrend = {"direction": "neutral", "value": 0.0, "changed": False}
                adx = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}

            # 4h candles — intermediate timeframe filter
            try:
                h4_end = int(time.time() * 1000)
                h4_start = h4_end - (30 * 24 * 60 * 60 * 1000)
                candles_4h = mainnet_info.candles_snapshot(symbol, "4h", h4_start, h4_end)
                # Drop forming 4h bar
                if candles_4h:
                    try:
                        if h4_end - int(candles_4h[-1]['t']) < 4 * 60 * 60 * 1000:
                            candles_4h = candles_4h[:-1]
                    except (KeyError, TypeError):
                        pass
                rsi_4h = compute_rsi(candles_4h, period=RSI_PERIOD, lookback=3)
                ema_4h_now  = compute_ema(candles_4h, period=EMA_PERIOD)
                ema_4h_prev = compute_ema(candles_4h[:-3], period=EMA_PERIOD) \
                              if len(candles_4h) > EMA_PERIOD + 3 else None
                if ema_4h_now and ema_4h_prev:
                    ema_4h_slope = "up" if ema_4h_now > ema_4h_prev else "down"
                else:
                    ema_4h_slope = "unknown"
            except Exception as e:
                logger.warning(f"{symbol}: 4h candle fetch failed: {e}")
                rsi_4h = {"rsi": 50.0, "prev_rsi": 50.0, "min_recent": 50.0, "max_recent": 50.0}
                ema_4h_slope = "unknown"

            return {
                "symbol": symbol, "price": price,
                "tn_price": price,
                "sz_decimals": sz_decimals,
                "daily_vol": compute_daily_vol(candles),
                "atr": compute_atr(daily_candles) if daily_candles else None,
                "funding_data": compute_funding(symbol, asset_ctxs),
                "supertrend": supertrend,
                "adx": adx,
                "rsi_60": compute_rsi(candles, period=RSI_PERIOD, lookback=RSI_LOOKBACK),
                "ema20_60": compute_ema(candles, period=EMA_PERIOD),
                "vol_ratio_60": compute_volume_ratio(candles, lookback=10),
                "struct_stops": compute_struct_stops(daily_candles, lookback=20) if daily_candles
                               else compute_struct_stops(candles, lookback=48),
                "rsi_4h": rsi_4h,
                "ema_4h_slope": ema_4h_slope,
            }
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"{symbol} attempt {attempt}/{max_retries} failed: {e} — retrying in {retry_delay}s")
                time.sleep(retry_delay)
            else:
                logger.error(f"{symbol} failed after {max_retries} attempts — skipping")
                return None

def get_all_market_data(symbols, open_position_syms=None):
    open_position_syms = open_position_syms or set()
    logger.info("Fetching market data + orderflow for selected symbols (parallel)...")
    asset_ctxs = None
    try:
        asset_ctxs = mainnet_info.meta_and_asset_ctxs()
    except Exception as e:
        logger.warning(f"Could not pre-fetch asset contexts — funding data unavailable this cycle: {e}")
    mids = None
    try:
        mids = mainnet_info.all_mids()
    except Exception as e:
        logger.warning(f"Could not pre-fetch mid prices — will fetch per-symbol: {e}")
    all_data = {}
    failed = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_symbol_data, sym, 3, 5, asset_ctxs, mids): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                data = future.result()
                if data:
                    all_data[sym] = data
                else:
                    failed.append(sym)
            except Exception as e:
                logger.error(f"{sym} fetch error: {e}")
                failed.append(sym)

    tn_prices = get_testnet_price_map()
    wide_gap = []
    for sym in list(all_data.keys()):
        info_px = tn_prices.get(sym)
        if info_px and info_px['gap_pct'] > MAX_ORACLE_GAP_PCT:
            if sym in open_position_syms:
                all_data[sym]['tn_price'] = info_px['price']
                logger.info(f"  {sym} has wide oracle gap ({info_px['gap_pct']:.1f}%) but is held — keeping for management")
                continue
            wide_gap.append(f"{sym} ({info_px['gap_pct']:.1f}%)")
            del all_data[sym]
            continue
        if not info_px:
            logger.warning(f"  {sym}: testnet price unavailable — using mainnet price, oracle gap check skipped")
        all_data[sym]['tn_price'] = info_px['price'] if info_px else all_data[sym]['price']

    if wide_gap:
        logger.info(f"Skipping wide oracle-gap coins (won't fill cleanly on testnet): {', '.join(wide_gap)}")

    for sym, data in all_data.items():
        vol_str = f"{data['daily_vol']*100:.1f}%" if data['daily_vol'] else "n/a"
        st = data['supertrend']
        adx = data['adx']
        st_tag  = f"ST={st['direction'].upper()}" + (" [FLIP]" if st['changed'] else "")
        adx_tag = f"ADX={adx['adx']:.1f}" + (" ✓" if adx['trending'] else " ✗chop")
        rsi_d = data.get('rsi_60', {})
        ema_v = data.get('ema20_60')
        rsi_tag = f"RSI={rsi_d.get('rsi', 0):.1f}(min{rsi_d.get('min_recent',0):.1f}/max{rsi_d.get('max_recent',0):.1f})"
        ema_tag = f"EMA20=${ema_v:.4f}" if ema_v else "EMA20=n/a"
        rsi4_val = data.get('rsi_4h', {}).get('rsi', 0)
        slope_tag = data.get('ema_4h_slope', '?')
        vol_r = data.get('vol_ratio_60', 1.0)
        logger.info(
            f"  {sym}: testnet ${data['tn_price']:.4f} | DailyVol={vol_str} | "
            f"Funding={data['funding_data']['funding']}% | {st_tag} | {adx_tag} | "
            f"{rsi_tag} | {ema_tag} | 4hRSI={rsi4_val:.1f} slope={slope_tag} | VolRatio={vol_r:.2f}"
        )

    if failed:
        logger.warning(f"Symbols skipped this cycle: {', '.join(failed)}")
    return all_data

# ─── Position Sizing ──────────────────────────────────────────────────────────

def compute_notional(symbol, all_data, equity):
    """
    ATR-based sizing: risk exactly RISK_PER_TRADE_PCT of equity per stop-out.
    Returns (None, None) to signal skip — callers must handle this.

    Cap  : equity×MAX_NOTIONAL_PCT (10%) — scales with account, no static ceiling.
    Floor: skip rather than inflate — preserves the 1% guarantee.
    """
    atr   = all_data[symbol].get('atr')
    price = all_data[symbol].get('tn_price') or all_data[symbol].get('price', 0)
    if not atr or not price or atr <= 0:
        logger.warning(f"{symbol}: no ATR data — skipping")
        return None, None

    dollar_risk   = equity * RISK_PER_TRADE_PCT
    stop_distance = STOP_ATR_MULT * atr
    size_tokens   = dollar_risk / stop_distance
    notional      = size_tokens * price

    if dollar_risk < 1.0:
        logger.warning(
            f"{symbol}: dollar risk ${dollar_risk:.2f} < $1.00 — skipping (equity too small for fees)"
        )
        return None, None

    notional = min(notional, equity * MAX_NOTIONAL_PCT)

    floor = max(equity * MIN_NOTIONAL_PCT, MIN_NOTIONAL_USD)
    if notional < floor:
        logger.warning(
            f"{symbol}: notional ${notional:.2f} < floor ${floor:.2f} "
            f"(ATR too large — skip rather than inflate)"
        )
        return None, None

    logger.info(
        f"ATR sizing {symbol}: ATR=${atr:.4f} | stop=${stop_distance:.4f} | "
        f"risk ${dollar_risk:.2f} → {size_tokens:.4f} tok → ${notional:.2f} notional"
    )
    return notional, atr

# ─── Entry (post-only maker) ──────────────────────────────────────────────────


def _finalize_open(symbol, direction, is_buy, notional_usd, atr_val,
                   confluence, reason, equity, sym_data=None):
    pos = None
    for _ in range(3):
        pos = get_open_positions().get(symbol)
        if pos:
            break
        time.sleep(2)
    fill_px = pos['entry'] if pos else 0
    fill_sz = abs(pos['size']) if pos else 0

    if not fill_px:
        logger.warning(f"⚠️ OPEN {direction} {symbol}: position not found after retries — check exchange manually")
        send_telegram(
            f"⚠️ <b>{direction} {symbol}: fill not confirmed</b>\n"
            f"Position not found after 3 retries — check exchange manually"
        )
        return False

    logger.success(f"✅ OPEN {direction} {symbol}: {fill_sz} @ ${fill_px:.4f} (MAKER) | "
                   f"Notional: ${notional_usd:.2f} | Confluence: {confluence}")

    # Compute structural stop from swing low/high over the RSI lookback window
    struct_stop = None
    if sym_data:
        struct_atr = sym_data.get('atr')
        sw_low, sw_high = sym_data.get('struct_stops', (None, None))
        if struct_atr:
            if is_buy and sw_low and sw_low < fill_px:
                struct_stop = sw_low - STRUCT_STOP_BUFFER * struct_atr
            elif not is_buy and sw_high and sw_high > fill_px:
                struct_stop = sw_high + STRUCT_STOP_BUFFER * struct_atr
    init_peak(symbol, fill_px, struct_stop=struct_stop, atr_val=atr_val)
    action_label = "BUY" if is_buy else "SELL"
    log_trade(action_label, symbol, fill_sz, fill_px, reason, equity, confluence)
    emoji = "🟢" if is_buy else "🟠"
    stop_dist = f"${STOP_ATR_MULT * atr_val:.4f}" if atr_val else "n/a"
    dollar_risk = equity * RISK_PER_TRADE_PCT if equity else 0
    send_telegram(
        f"{emoji} <b>OPEN {direction}</b> (maker)\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Fill: ${fill_px:.4f}\n"
        f"Size: {fill_sz} (${notional_usd:.0f} notional)\n"
        f"Stop dist: {stop_dist} | Risk: ${dollar_risk:.2f} ({RISK_PER_TRADE_PCT*100:.0f}% equity)\n"
        f"Confluence: {confluence}\n"
        f"Reason: {reason}"
    )
    return True

# ─── Exits ────────────────────────────────────────────────────────────────────

def close_position_market(symbol, all_data, equity, reason, confluence=""):
    """Guaranteed-fill market close. Used for all automatic exits."""
    exec_price = all_data.get(symbol, {}).get('tn_price') or all_data.get(symbol, {}).get('price', 0)
    pre_size = abs((get_open_positions().get(symbol) or {}).get('size', 0))
    try:
        if exec_price:
            result = hl_exchange.market_close(symbol, None, exec_price, SLIPPAGE)
        else:
            result = hl_exchange.market_close(symbol)
        logger.info(f"Market close result: {result}")
        statuses = result.get('response', {}).get('data', {}).get('statuses', [])
        filled = any('filled' in s for s in statuses)
        err = next((s['error'] for s in statuses if 'error' in s), "")
        fill_px = exec_price
        for s in statuses:
            if 'filled' in s:
                fill_px = float(s['filled'].get('avgPx', exec_price))
        if filled or symbol not in get_open_positions():
            logger.success(f"✅ CLOSE {symbol} @ ${fill_px:.4f} (market)")
            log_trade("CLOSE", symbol, pre_size, fill_px, reason, equity,
                      confluence)
            send_telegram(f"🔴 <b>CLOSE</b> (market)\nSymbol: <b>{symbol}</b>\nPrice: ${fill_px:.4f}\nReason: {reason}")
            return True
        logger.error(f"❌ CLOSE {symbol} REJECTED: {err}")
        send_telegram(f"⚠️ <b>Close {symbol} REJECTED</b>\n{err}")
        return False
    except Exception as e:
        logger.error(f"Market close {symbol} failed: {e}")
        send_telegram(f"⚠️ <b>Close {symbol} FAILED</b>\n{str(e)[:200]}")
        return False

# ─── Chandelier (Trailing ATR) Stop ──────────────────────────────────────────

def _load_peaks():
    raw = load_json(TRAILING_STOP_LOG, {})
    # Migrate old flat format {sym: float} → {sym: {"peak": float, ...}}
    migrated = False
    for sym, val in raw.items():
        if isinstance(val, (int, float)):
            raw[sym] = {"peak": float(val), "struct_stop": None, "atr": None}
            migrated = True
        elif isinstance(val, dict) and 'atr' not in val:
            raw[sym].pop('opened_at', None)
            raw[sym]['atr'] = None
            migrated = True
    if migrated:
        save_json(TRAILING_STOP_LOG, raw)
    return raw

def _save_peaks(peaks):
    save_json(TRAILING_STOP_LOG, peaks)

def init_peak(symbol, entry_price, struct_stop=None, atr_val=None):
    peaks = _load_peaks()
    peaks[symbol] = {
        "peak": entry_price,
        "struct_stop": struct_stop,
        "atr": atr_val,
    }
    _save_peaks(peaks)
    extra = f" | struct stop ${struct_stop:.4f}" if struct_stop is not None else ""
    logger.info(f"Chandelier {symbol}: peak initialised at ${entry_price:.4f}{extra}")

def clear_peak(symbol):
    peaks = _load_peaks()
    if symbol in peaks:
        del peaks[symbol]
        _save_peaks(peaks)


# ─── Pending LOC Orders ───────────────────────────────────────────────────────

def _load_pending_loc():
    return load_json(LOC_LOG, {})

def _save_pending_loc(pending):
    save_json(LOC_LOG, pending)

def check_pending_loc(positions, all_data, equity):
    """
    Called at the start of each cycle. For each pending LOC order:
    - Position now open → order filled while we slept → finalize the trade.
    - Position still closed → order did not fill by bar close → cancel it.
    """
    pending = _load_pending_loc()
    if not pending:
        return

    to_remove = []
    for symbol, meta in list(pending.items()):
        oid       = meta['oid']
        direction = "LONG" if meta['is_buy'] else "SHORT"

        expected_side = "LONG" if meta['is_buy'] else "SHORT"
        if symbol in positions and positions[symbol]['side'] == expected_side:
            logger.info(f"{symbol} LOC {direction} filled while sleeping — finalizing")
            sym_data = all_data.get(symbol)
            if sym_data is None:
                logger.warning(f"{symbol}: not in all_data this cycle — fetching individually for LOC finalization")
                try:
                    fallback_ctxs = mainnet_info.meta_and_asset_ctxs()
                except Exception:
                    fallback_ctxs = None
                sym_data = get_symbol_data(symbol, asset_ctxs=fallback_ctxs) or {}
            _finalize_open(
                symbol, direction, meta['is_buy'],
                meta['notional_usd'], meta.get('atr_val'),
                meta['confluence'], meta['reason'],
                equity,
                sym_data=sym_data,
            )
        else:
            logger.info(f"{symbol} LOC {direction} unfilled at bar close — cancelling (oid {oid})")
            try:
                cancel_order(symbol, oid)
            except Exception as e:
                logger.warning(f"{symbol} LOC cancel failed: {e}")
            send_telegram(f"⌛ <b>{symbol} {direction} LOC expired</b>\nOrder at ${meta['limit_px']:.4f} cancelled (no fill)")
        to_remove.append(symbol)

    for symbol in to_remove:
        del pending[symbol]
    _save_pending_loc(pending)


def place_loc_order(symbol, is_long, all_data, equity, pb_reason=""):
    """
    Limit-on-close entry. Signals are computed on the just-closed bar; this
    places one post-only limit at the 60-min EMA (the pullback level) and
    returns immediately. The order rests on the exchange for up to one bar.
    check_pending_loc() resolves it next cycle: finalizes if filled, cancels if not.
    """
    data        = all_data[symbol]
    ema_val     = data.get('ema20_60')
    price       = data.get('tn_price') or data.get('price', 0)
    sz_decimals = data.get('sz_decimals', 3)
    direction   = "LONG" if is_long else "SHORT"
    adx_val     = data.get('adx', {}).get('adx', 0)
    st_dir      = data.get('supertrend', {}).get('direction', 'neutral')
    confluence  = f"ST={st_dir.upper()} ADX={adx_val:.1f} | {pb_reason}" if pb_reason else "LOC"

    if not ema_val:
        logger.warning(f"{symbol}: no EMA — cannot compute LOC price")
        return False

    notional_usd, atr_val = compute_notional(symbol, all_data, equity)
    if notional_usd is None:
        return False

    try:
        hl_exchange.update_leverage(LEVERAGE, symbol, is_cross=True)
    except Exception as e:
        logger.warning(f"Could not set leverage for {symbol}: {e}")

    best_bid, best_ask, tick, decimals = get_testnet_book(symbol)
    if best_bid is None:
        logger.error(f"{symbol}: no book — cannot place LOC order")
        return False

    # Snap directionally to the maker side (floor for buys, ceil for sells) so the
    # snap itself can't push the price through the book and trigger the crossing guard.
    # Then enforce Hyperliquid's ≤5 significant-figure rule via Python's %g formatter,
    # which handles sub-1 prices correctly (int(log10(x))+1 undercounts for x < 1).
    if tick > 0:
        snapped = (math.floor(ema_val / tick) if is_long else math.ceil(ema_val / tick)) * tick
    else:
        snapped = ema_val
    limit_px = float(f"{snapped:.5g}") if snapped > 0 else round(snapped, decimals)
    # Defence-in-depth: C3 tightening makes crossing unreachable in normal flow,
    # but guard remains for any edge case that slips through.
    if is_long and limit_px > best_bid:
        logger.info(
            f"{symbol} LONG LOC: EMA ${limit_px:.{decimals}f} > bid ${best_bid:.{decimals}f} "
            f"— price below EMA, post-only would cross — skipping"
        )
        return False
    if not is_long and limit_px < best_ask:
        logger.info(
            f"{symbol} SHORT LOC: EMA ${limit_px:.{decimals}f} < ask ${best_ask:.{decimals}f} "
            f"— price above EMA, post-only would cross — skipping"
        )
        return False
    size_tokens = round(notional_usd / limit_px, sz_decimals)
    min_size    = 10 ** (-sz_decimals)
    if size_tokens < min_size:
        logger.warning(f"{symbol}: LOC size {size_tokens} below min {min_size} — skipping")
        return False

    logger.info(
        f"{symbol} {direction} LOC: limit at EMA ${limit_px:.{decimals}f} "
        f"(market ${price:.{decimals}f}) | {size_tokens} tokens (${notional_usd:.0f} notional)"
    )

    status, oid, fpx, fsz, err = place_alo_limit(symbol, is_long, size_tokens, limit_px)

    if status == 'filled':
        logger.success(f"{symbol} LOC filled immediately at ${fpx:.4f}")
        return _finalize_open(
            symbol, direction, is_long, notional_usd, atr_val,
            confluence=confluence,
            reason=f"LOC at EMA ${limit_px:.4f} (ADX={adx_val:.1f})",
            equity=equity, sym_data=data,
        )

    if status == 'resting':
        pending = _load_pending_loc()
        pending[symbol] = {
            "oid":         oid,
            "is_buy":      is_long,
            "limit_px":    limit_px,
            "notional_usd": notional_usd,
            "atr_val":     atr_val,
            "confluence":  "7/7 LOC",
            "reason":      f"LOC at EMA ${limit_px:.4f} (ADX={adx_val:.1f})",
        }
        _save_pending_loc(pending)
        logger.info(
            f"{symbol} {direction} LOC resting @ ${limit_px:.{decimals}f} (oid {oid}) — "
            f"will cancel if unfilled next cycle"
        )
        send_telegram(
            f"⏳ <b>{symbol} {direction} LOC placed</b>\n"
            f"Limit: ${limit_px:.4f} (EMA) | Market: ${price:.4f}\n"
            f"Cancels if unfilled by next bar close"
        )
        return True

    logger.warning(f"{symbol} LOC {direction} could not rest: {status} — {err}")
    send_telegram(f"⚠️ <b>{symbol} {direction} LOC failed to rest</b>\n{status}: {err[:200]}")
    return False


def check_stops(positions, all_data, equity):
    """
    Three exit triggers, evaluated in order per position:

    1. Supertrend flip — daily ST flips against position direction → market close.
    2. ADX decay — 2 consecutive bars below ADX_DECAY_EXIT → trend is dead, close.
    3. Chandelier trailing stop with break-even lock + structural floor.
       stop = peak ± STOP_ATR_MULT × ATR; once the peak has moved STOP_ATR_MULT×entryATR
       in our favour the stop is floored at entry (BE lock uses the stored entry ATR so
       an ATR expansion cannot push the stop back below entry). The structural stop
       (swing low/high ± STRUCT_STOP_BUFFER × ATR) acts as the minimum stop floor
       in the early part of the trade before the chandelier catches up.
    """
    peaks = _load_peaks()
    peaks_changed = False
    closed_any = False

    for sym, p in list(positions.items()):
        data = all_data.get(sym)
        if not data:
            logger.warning(f"Stop check: {sym} missing from market data — attempting individual fetch")
            send_telegram(f"⚠️ <b>{sym}: market data missing</b>\nStop checks skipped — retrying fetch individually")
            try:
                fallback_ctxs = mainnet_info.meta_and_asset_ctxs()
            except Exception:
                fallback_ctxs = None
            data = get_symbol_data(sym, asset_ctxs=fallback_ctxs)
            if not data:
                logger.error(f"Stop check: fallback fetch failed for {sym} — position unmanaged this cycle")
                continue
            all_data[sym] = data
        atr   = data.get('atr')
        price = data.get('tn_price') or data.get('price')
        entry = p['entry']
        side  = p['side']
        if not atr or not price or not entry:
            continue

        # Ensure peak entry exists before exit checks (ADX counter needs it)
        if sym not in peaks:
            peaks[sym] = {"peak": entry, "struct_stop": None, "atr": None}
            peaks_changed = True
        peak_data = peaks[sym]

        # ── 1. Supertrend exit — close whenever ST is against position ───────
        st = data.get('supertrend', {})
        st_dir = st.get('direction', 'neutral')
        st_against = (side == "LONG" and st_dir == "bearish") or \
                     (side == "SHORT" and st_dir == "bullish")
        if st_against:
            logger.warning(
                f"🔄 ST EXIT {sym} {side}: Supertrend is {st_dir.upper()} — closing"
            )
            send_telegram(
                f"🔄 <b>SUPERTREND EXIT {sym} {side}</b>\n"
                f"ST direction is {st_dir.upper()} — market close"
            )
            if close_position_market(sym, all_data, equity,
                                     f"Supertrend {st_dir}"):
                peaks.pop(sym, None)
                peaks_changed = True
                closed_any = True
                time.sleep(1)
            continue

        # ── 2. ADX decay exit — requires 2 consecutive bars below threshold ──
        adx_val = data.get('adx', {}).get('adx', 0.0)
        if adx_val == 0.0:
            # Sentinel value — compute_adx returned its failure default.
            # Treat as missing data: skip (don't count, don't close) and reset any
            # in-progress decay counter so stale bad data can't accumulate toward exit.
            if peak_data.get('adx_decay_count', 0):
                peak_data['adx_decay_count'] = 0
                peaks_changed = True
            logger.warning(f"Stop check: ADX=0 for {sym} (sentinel) — skipping decay check")
        elif adx_val < ADX_DECAY_EXIT:
            decay_hits = peak_data.get('adx_decay_count', 0)
            if decay_hits < 1:
                peak_data['adx_decay_count'] = 1
                peaks_changed = True
                logger.warning(
                    f"📉 ADX DECLINING {sym} {side}: ADX={adx_val:.1f} < {ADX_DECAY_EXIT} "
                    f"— confirming next bar before exit"
                )
                continue
            logger.warning(
                f"📉 ADX DECAY EXIT {sym} {side}: ADX={adx_val:.1f} < {ADX_DECAY_EXIT} — trend gone"
            )
            send_telegram(
                f"📉 <b>ADX DECAY EXIT {sym} {side}</b>\n"
                f"ADX={adx_val:.1f} dropped below {ADX_DECAY_EXIT} — trend exhausted"
            )
            if close_position_market(sym, all_data, equity,
                                     f"ADX decay ({adx_val:.1f} < {ADX_DECAY_EXIT})"):
                peaks.pop(sym, None)
                peaks_changed = True
                closed_any = True
                time.sleep(1)
            continue
        elif peak_data.get('adx_decay_count', 0):
            peak_data['adx_decay_count'] = 0
            peaks_changed = True

        peak          = peak_data["peak"]
        struct_stop   = peak_data.get("struct_stop")
        entry_atr     = peak_data.get("atr") or atr
        stop_distance = STOP_ATR_MULT * atr
        be_stop_dist  = STOP_ATR_MULT * entry_atr   # uses stored entry ATR so expansion can't break BE

        # ── 4. Chandelier stop + structural floor ─────────────────────────────
        if side == "LONG":
            new_peak = max(peak, price)
            if price - entry >= entry_atr:
                # Floor peak at the BE anchor using entry ATR for the threshold so both
                # the trigger and the activation check use the same distance. Note: stored
                # peak may exceed the actual price high-water-mark while BE is active —
                # it is a stop anchor, not a true MFE tracker.
                new_peak = max(new_peak, entry + be_stop_dist)
            chandelier_stop = new_peak - stop_distance
            if struct_stop is not None and struct_stop < entry:
                chandelier_stop = max(chandelier_stop, struct_stop)
            be_active = new_peak >= entry + be_stop_dist
            if be_active:
                chandelier_stop = max(chandelier_stop, entry)
            stop_price = chandelier_stop
            breached   = price <= stop_price
        else:
            new_peak = min(peak, price)
            if entry - price >= entry_atr:
                new_peak = min(new_peak, entry - be_stop_dist)
            chandelier_stop = new_peak + stop_distance
            if struct_stop is not None and struct_stop > entry:
                chandelier_stop = min(chandelier_stop, struct_stop)
            be_active = new_peak <= entry - be_stop_dist
            if be_active:
                chandelier_stop = min(chandelier_stop, entry)
            stop_price = chandelier_stop
            breached   = price >= stop_price

        if new_peak != peak:
            peaks[sym]["peak"] = new_peak
            peaks_changed = True

        be_tag     = " [BE]" if be_active else ""
        struct_tag = f" | struct=${struct_stop:.4f}" if struct_stop else ""
        logger.debug(
            f"Chandelier {sym} {side}: entry=${entry:.4f} peak=${new_peak:.4f} "
            f"stop=${stop_price:.4f}{be_tag}{struct_tag} | now=${price:.4f}"
        )

        if breached:
            stop_label = "break-even stop" if be_active else f"chandelier ({STOP_ATR_MULT}×ATR)"
            logger.warning(
                f"🛑 {stop_label.upper()} {sym} {side}: "
                f"peak ${new_peak:.4f} → stop ${stop_price:.4f} | now ${price:.4f}"
            )
            send_telegram(
                f"🛑 <b>{stop_label.upper()} {sym} {side}</b>\n"
                f"Entry: ${entry:.4f} | Peak: ${new_peak:.4f}\n"
                f"Stop: ${stop_price:.4f} | Now: ${price:.4f}"
            )
            if close_position_market(sym, all_data, equity,
                                     f"{stop_label} at ${stop_price:.4f}"):
                peaks.pop(sym, None)
                peaks_changed = True
                closed_any = True
                time.sleep(1)

    if peaks_changed:
        _save_peaks(peaks)
    return closed_any

# (Claude exit management removed — exits handled automatically by check_stops)


# ─── Rule-Based Entry Selection ───────────────────────────────────────────────

def select_entry(all_data, positions):
    """
    Evaluates all 7 entry conditions for every non-held symbol and returns
    (symbol, is_long) for the best qualifying setup (highest ADX), or (None, None).

    Conditions checked here:
      C1a  Daily Supertrend direction (bullish → long, bearish → short)
      C1b  Daily ADX > ADX_THRESHOLD + DI direction confirmation
      Funding gate — skip crowded-side entries

    Conditions delegated to _check_pullback_entry:
      C0   4h RSI and EMA slope in trade direction (intermediate timeframe filter)
      C2   60-min RSI dipped below RSI_LONG_THRESHOLD / spiked above RSI_SHORT_THRESHOLD
      C3   Price within ±EMA_BAND_PCT of 20-EMA on 60-min
      C4   RSI hook back in trend direction
      C5   Current bar volume ≥ VOLUME_CONFIRM_RATIO × 10-bar average
    """
    candidates = []
    for symbol, data in all_data.items():
        if symbol in positions:
            continue

        st_dir = data.get('supertrend', {}).get('direction', 'neutral')
        if st_dir == 'neutral':
            continue
        is_long = (st_dir == 'bullish')
        action  = "OPEN_LONG" if is_long else "OPEN_SHORT"

        # Funding gate — skip crowded-side entries
        funding = data.get('funding_data', {}).get('funding', 0.0)
        if is_long and funding > FUNDING_LONG_MAX:
            logger.debug(f"  {symbol}: funding {funding:.4f}% > {FUNDING_LONG_MAX}% — skip long (crowded)")
            continue
        if not is_long and funding < FUNDING_SHORT_MIN:
            logger.debug(f"  {symbol}: funding {funding:.4f}% < {FUNDING_SHORT_MIN}% — skip short (crowded)")
            continue

        # ADX gate + DI direction confirmation
        adx_data = data.get('adx', {})
        adx_val  = adx_data.get('adx', 0.0)
        if adx_val < ADX_THRESHOLD:
            logger.debug(f"  {symbol}: ADX {adx_val:.1f} < {ADX_THRESHOLD} — skip")
            continue
        plus_di  = adx_data.get('plus_di', 0.0)
        minus_di = adx_data.get('minus_di', 0.0)
        if is_long and plus_di <= minus_di:
            logger.debug(f"  {symbol}: +DI {plus_di:.1f} ≤ −DI {minus_di:.1f} — skip long (bearish DM)")
            continue
        if not is_long and minus_di <= plus_di:
            logger.debug(f"  {symbol}: −DI {minus_di:.1f} ≤ +DI {plus_di:.1f} — skip short (bullish DM)")
            continue

        passed, pb_reason = _check_pullback_entry(symbol, all_data, action)
        if not passed:
            if data.get('ema_4h_slope') == 'unknown':
                logger.info(f"  {symbol} {action}: C0 blocked — insufficient 4h history for slope")
            else:
                logger.debug(f"  {symbol} {action}: pullback not ready — {pb_reason}")
            continue

        logger.info(f"  {symbol} {action}: SETUP READY — ADX={adx_val:.1f} | {pb_reason}")
        candidates.append((symbol, is_long, adx_val, pb_reason))

    if not candidates:
        logger.info("No entry setup ready this cycle")
        return None, None, ""

    candidates.sort(key=lambda x: x[2], reverse=True)
    best_sym, best_is_long, best_adx, best_reason = candidates[0]
    direction = "LONG" if best_is_long else "SHORT"
    logger.info(
        f"Best entry: {best_sym} {direction} (ADX={best_adx:.1f}) "
        f"— {len(candidates)} setup(s) qualified"
    )
    return best_sym, best_is_long, best_reason

# ─── Entry Conditions ─────────────────────────────────────────────────────────

def _check_pullback_entry(symbol, all_data, action):
    """
    Validates pullback conditions C0–C5 for OPEN_LONG / OPEN_SHORT.
    C0: 4h RSI > 50 and 4h EMA slope is in trade direction (intermediate filter).
    C2: RSI dipped below RSI_LONG_THRESHOLD (long) or spiked above RSI_SHORT_THRESHOLD (short)
        within the last RSI_LOOKBACK bars.
    C3: Price is within EMA_BAND_PCT of the 20-EMA on 60-min.
    C4: RSI is now hooking back in the direction of the trade.
    C5: Current bar volume ≥ VOLUME_CONFIRM_RATIO × 10-bar average (volume confirmation).
    Returns (passed: bool, reason: str).
    """
    data      = all_data[symbol]
    rsi_data  = data.get('rsi_60', {})
    ema_val   = data.get('ema20_60')
    price     = data.get('tn_price') or data.get('price', 0)
    rsi_4h    = data.get('rsi_4h', {})
    slope     = data.get('ema_4h_slope', 'unknown')
    vol_ratio = data.get('vol_ratio_60', 1.0)

    rsi      = rsi_data.get('rsi', 50.0)
    prev_rsi = rsi_data.get('prev_rsi', 50.0)
    rsi_4h_v = rsi_4h.get('rsi', 50.0)

    pct_from_ema = None
    pct_str = "n/a"
    if ema_val and price:
        pct_from_ema = (price - ema_val) / ema_val   # signed: positive = above EMA
        pct_str = f"{pct_from_ema * 100:+.1f}%"
    ema_str = f"${ema_val:.4f}" if ema_val else "n/a"

    c5 = vol_ratio >= VOLUME_CONFIRM_RATIO

    if action == "OPEN_LONG":
        # Price still at/above EMA — limit at EMA rests below market as a maker order.
        # Prices below EMA are excluded: a buy limit at EMA would cross (price < EMA → taker).
        near_ema = pct_from_ema is not None and 0 <= pct_from_ema <= EMA_BAND_PCT
        c0 = (rsi_4h_v > 50) and (slope == "up")
        c2 = rsi_data.get('min_recent', 50.0) < RSI_LONG_THRESHOLD
        c3 = near_ema
        c4 = rsi > prev_rsi
        passed = c0 and c2 and c3 and c4 and c5
        reason = (
            f"C0(4hRSI={rsi_4h_v:.1f}>50,slope={slope})={'✓' if c0 else '✗'} "
            f"C2(dip<{RSI_LONG_THRESHOLD})={'✓' if c2 else '✗'}[min={rsi_data.get('min_recent',50):.1f}] "
            f"C3(near EMA {ema_str} {pct_str})={'✓' if c3 else '✗'} "
            f"C4(hook↑ {prev_rsi:.1f}→{rsi:.1f})={'✓' if c4 else '✗'} "
            f"C5(vol={vol_ratio:.2f}≥{VOLUME_CONFIRM_RATIO})={'✓' if c5 else '✗'}"
        )
    elif action == "OPEN_SHORT":
        # Price still at/below EMA — limit at EMA rests above market as a maker order.
        # Prices above EMA are excluded: a sell limit at EMA would cross (price > EMA → taker).
        near_ema = pct_from_ema is not None and -EMA_BAND_PCT <= pct_from_ema <= 0
        c0 = (rsi_4h_v < 50) and (slope == "down")
        c2 = rsi_data.get('max_recent', 50.0) > RSI_SHORT_THRESHOLD
        c3 = near_ema
        c4 = rsi < prev_rsi
        passed = c0 and c2 and c3 and c4 and c5
        reason = (
            f"C0(4hRSI={rsi_4h_v:.1f}<50,slope={slope})={'✓' if c0 else '✗'} "
            f"C2(spike>{RSI_SHORT_THRESHOLD})={'✓' if c2 else '✗'}[max={rsi_data.get('max_recent',50):.1f}] "
            f"C3(near EMA {ema_str} {pct_str})={'✓' if c3 else '✗'} "
            f"C4(hook↓ {prev_rsi:.1f}→{rsi:.1f})={'✓' if c4 else '✗'} "
            f"C5(vol={vol_ratio:.2f}≥{VOLUME_CONFIRM_RATIO})={'✓' if c5 else '✗'}"
        )
    else:
        return True, ""

    return passed, reason


# ─── Logging ──────────────────────────────────────────────────────────────────

def log_trade(action, symbol, size, price, reason, equity, confluence=""):
    trades = load_json(TRADE_LOG, [])
    trades.append({
        "timestamp": datetime.now().isoformat(),
        "action": action, "symbol": symbol, "size": size, "price": price,
        "reason": reason, "equity": equity,
        "confluence": confluence,
        "leverage": LEVERAGE, "notional": size * price
    })
    if len(trades) > 10000:
        trades = trades[-10000:]
    save_json(TRADE_LOG, trades)

def log_equity(equity, all_data, positions):
    curve = load_json(EQUITY_LOG, [])
    curve.append({
        "timestamp": datetime.now().isoformat(),
        "equity": equity,
        "prices": {sym: data['tn_price'] for sym, data in all_data.items()},
        "volume_24h": {sym: data.get('funding_data', {}).get('day_volume', 0) for sym, data in all_data.items()},
        "positions": {sym: {"side": p["side"], "size": p["size"], "entry": p["entry"]}
                      for sym, p in positions.items()}
    })
    if len(curve) > 8760:
        curve = curve[-8760:]
    save_json(EQUITY_LOG, curve)

# ─── Summary ──────────────────────────────────────────────────────────────────

def _pos_pnl(pos, current_price):
    size = abs(pos['size'])
    if pos['side'] == "LONG":
        return (current_price - pos['entry']) * size
    return (pos['entry'] - current_price) * size

def print_summary(equity, positions, all_data):
    curve = load_json(EQUITY_LOG, [])
    trades = load_json(TRADE_LOG, [])
    if not curve:
        return

    start_data = load_json(START_EQUITY_LOG, None)
    start_equity = start_data['equity'] if start_data else curve[0]['equity']
    real_trades = [t for t in trades if t['action'] in ['BUY', 'SELL']]

    unrealized_pnl = 0.0
    for sym, p in positions.items():
        current_price = all_data.get(sym, {}).get('tn_price', 0) or all_data.get(sym, {}).get('price', 0)
        if current_price and p['entry']:
            unrealized_pnl += _pos_pnl(p, current_price)

    realized_pnl = (equity - start_equity) - unrealized_pnl
    total_pnl = equity - start_equity
    total_pnl_pct = (total_pnl / start_equity * 100) if start_equity > 0 else 0

    rsign = "+" if realized_pnl >= 0 else ""
    usign = "+" if unrealized_pnl >= 0 else ""
    tsign = "+" if total_pnl >= 0 else ""

    logger.info("========== PORTFOLIO SUMMARY ==========")
    logger.info(f"Start equity:    ${start_equity:.2f}")
    logger.info(f"Current equity:  ${equity:.2f}")
    logger.info(f"Leverage:        {LEVERAGE}x")
    logger.info(f"Realized P&L:    {rsign}${realized_pnl:.2f}")
    logger.info(f"Unrealized P&L:  {usign}${unrealized_pnl:.2f}")
    logger.info(f"Total P&L:       {tsign}${total_pnl:.2f} ({tsign}{total_pnl_pct:.2f}%)")
    logger.info(f"Total opens:     {len(real_trades)}")
    logger.info("---------------------------------------")

    pos_lines_tg = ""
    if positions:
        logger.info("Open positions:")
        for sym, p in positions.items():
            current_price = all_data.get(sym, {}).get('tn_price', 0) or all_data.get(sym, {}).get('price', 0)
            entry = p['entry']; size = abs(p['size']); side = p['side']
            notional = size * current_price
            if current_price and entry:
                unreal_usd = _pos_pnl(p, current_price)
                unreal_pct = (unreal_usd / (size * entry)) * 100
                usign2 = "+" if unreal_usd >= 0 else ""
                funding = all_data.get(sym, {}).get('funding_data', {}).get('funding', 0)
                logger.info(
                    f"  {sym}: {side} {size} | Entry ${entry:.4f} → Now ${current_price:.4f} | "
                    f"Notional: ${notional:.2f} | "
                    f"Unrealized: {usign2}${unreal_usd:.2f} ({usign2}{unreal_pct:.2f}%) | "
                    f"Funding: {funding:.4f}%"
                )
                pemoji = "🟢" if unreal_usd >= 0 else "🔴"
                pos_lines_tg += f"{pemoji} {sym} {side}: {usign2}${unreal_usd:.2f} ({usign2}{unreal_pct:.1f}%)\n"
    else:
        logger.info("Open positions:  None")
        pos_lines_tg = "None\n"

    logger.info("---------------------------------------")
    open_longs = sum(1 for p in positions.values() if p['side'] == 'LONG')
    open_shorts = sum(1 for p in positions.values() if p['side'] == 'SHORT')
    total_opens = len(real_trades)
    total_closes = len([t for t in trades if t['action'] == 'CLOSE'])
    logger.info(f"Open now: L:{open_longs} S:{open_shorts} | All-time: opens={total_opens} closes={total_closes}")
    logger.info("=======================================")

    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    send_telegram(
        f"{pnl_emoji} <b>Hourly Summary</b>\n"
        f"Equity: ${equity:.2f}\n"
        f"Total P&L: {tsign}${total_pnl:.2f} ({tsign}{total_pnl_pct:.2f}%)\n"
        f"Realized: {rsign}${realized_pnl:.2f} | Unrealized: {usign}${unrealized_pnl:.2f}\n"
        f"Positions: {len(positions)}/{MAX_POSITIONS} (L:{open_longs} S:{open_shorts})\n"
        f"All-time: {total_opens} opens · {total_closes} closes\n"
        f"\n<b>Open:</b>\n{pos_lines_tg}"
    )

# ─── Main Loop ────────────────────────────────────────────────────────────────

def run_bot():
    lockfile = "/tmp/trading_bot.lock"
    try:
        with open(lockfile, 'x') as _lf:
            _lf.write(str(os.getpid()))
    except FileExistsError:
        old_pid = "unknown"
        try:
            with open(lockfile) as _lf:
                old_pid = _lf.read().strip()
        except Exception:
            pass
        alive = False
        if old_pid.isdigit():
            try:
                os.kill(int(old_pid), 0)
                alive = True
            except ProcessLookupError:
                pass          # process is dead — stale lock
            except PermissionError:
                alive = True  # exists but no signal permission
        if alive:
            logger.error(f"Lock file exists (PID {old_pid}) — another instance is running. Exiting.")
            raise SystemExit(1)
        logger.warning(f"Stale lockfile (PID {old_pid} dead) — removing and continuing")
        os.remove(lockfile)
        with open(lockfile, 'x') as _lf:
            _lf.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(lockfile) and os.remove(lockfile))

    logger.info("=== Claude Long/Short Orderflow Bot Started (MAKER orders) ===")
    logger.info(f"Dynamic selection: TOP {TOP_N} testnet-tradeable perps by 30-day avg daily dollar volume")
    logger.info(f"Entries: RULE-BASED (Supertrend + ADX + pullback) — post-only maker")
    logger.info(f"Exits: ST against position | ADX decay <{ADX_DECAY_EXIT} | chandelier {STOP_ATR_MULT}×ATR + struct stop")
    logger.info(f"Sizing: ATR-BASED {RISK_PER_TRADE_PCT*100:.0f}% equity risk/trade | portfolio cap {MAX_PORTFOLIO_RISK_PCT*100:.0f}% | notional floor ${MIN_NOTIONAL_USD} | cap {MAX_NOTIONAL_PCT*100:.0f}% equity")
    logger.info(f"Stop: chandelier {STOP_ATR_MULT}×ATR trailing | Break-even lock at +1×ATR")
    logger.info(f"Leverage: {LEVERAGE}x | Max positions: {MAX_POSITIONS} | Gap skip: >{MAX_ORACLE_GAP_PCT}%")
    logger.info(f"Data: MAINNET | Trading: TESTNET | Interval: {INTERVAL_MINUTES}min (clock-aligned)")

    init_files()
    get_testnet_coins()
    if not load_json(START_EQUITY_LOG, None):
        first_eq = get_equity()
        if first_eq > 0:
            save_json(START_EQUITY_LOG, {"equity": first_eq, "recorded_at": datetime.now().isoformat()})
            logger.info(f"Start equity recorded: ${first_eq:.2f}")
    send_telegram(
        "🤖 <b>Trading Bot Started</b>\n"
        f"Top {TOP_N} liquid perps | {LEVERAGE}x | Max {MAX_POSITIONS}\n"
        f"Entries: rule-based (ST + ADX + pullback) | Post-only maker\n"
        f"Exits: ST against position | ADX decay | chandelier {STOP_ATR_MULT}×ATR\n"
        "Data: MAINNET | Trading: TESTNET"
    )

    while True:
        try:
            logger.info("--- New cycle ---")

            positions = get_open_positions()
            symbols   = get_top_symbols(TOP_N, extra_symbols=list(positions.keys()))
            all_data  = get_all_market_data(symbols, open_position_syms=set(positions.keys()))
            equity = get_equity()
            if equity <= 0:
                equity = get_equity()   # one retry on transient API failure

            logger.info(f"Equity: ${equity:.2f} | Open positions: {len(positions)}/{MAX_POSITIONS}")

            # ── Step 0: Resolve pending LOC orders from last cycle ─────────────
            positions = get_open_positions()   # refresh — LOC may have filled during market data fetch
            check_pending_loc(positions, all_data, equity)

            if equity > 0:
                log_equity(equity, all_data, positions)

            # ── Step 1: Automatic stops ────────────────────────────────────────
            if positions:
                stopped = check_stops(positions, all_data, equity)
                if stopped:
                    time.sleep(SETTLE_SECONDS)
                    positions = get_open_positions()
            equity = get_equity()  # always refresh before entry decision

            # ── Step 2: LOC entry (only if slot available, no pending order, heat OK)
            pending_syms = set(_load_pending_loc().keys())
            if len(pending_syms) + len(positions) < MAX_POSITIONS:
                logger.info("Scanning for entry setups...")
                # Treat pending symbols as taken slots so select_entry skips them
                occupied = {**positions, **{sym: {"side": "PENDING"} for sym in pending_syms}}
                entry_sym, is_long, pb_reason = select_entry(all_data, occupied)
                if entry_sym:
                    peaks = _load_peaks()
                    # Open-position risk: conservative full 3×ATR even for BE-locked trades.
                    # Pending LOC risk: each resting order represents RISK_PER_TRADE_PCT.
                    # Prospective entry: +1 trade's worth so we can't step over the cap.
                    open_risk = sum(
                        abs(p['size']) * (peaks.get(sym, {}).get('atr') or all_data.get(sym, {}).get('atr') or 0) * STOP_ATR_MULT
                        for sym, p in positions.items()
                    )
                    open_risk += (len(pending_syms) + 1) * RISK_PER_TRADE_PCT * equity
                    heat_pct = open_risk / equity if equity > 0 else 0
                    logger.info(f"Portfolio heat: ${open_risk:.2f} = {heat_pct*100:.1f}% of equity")
                    if heat_pct >= MAX_PORTFOLIO_RISK_PCT:
                        logger.info(
                            f"Heat {heat_pct*100:.1f}% ≥ {MAX_PORTFOLIO_RISK_PCT*100:.0f}% cap — skipping {entry_sym}"
                        )
                    else:
                        place_loc_order(entry_sym, is_long, all_data, equity, pb_reason)

            positions = get_open_positions()
            equity    = get_equity()
            print_summary(equity, positions, all_data)

            sleep_until_next_hour()

        except Exception as e:
            logger.error(f"Error: {e}")
            send_telegram(f"⚠️ <b>Bot Error</b>\n{str(e)[:300]}")
            sleep_until_next_hour()

if __name__ == "__main__":
    run_bot()
