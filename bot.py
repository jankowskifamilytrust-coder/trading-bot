import os
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger

from config import (
    TOP_N, PINNED, STABLECOINS, MAX_POSITIONS, LEVERAGE, INTERVAL_MINUTES,
    SLIPPAGE, SETTLE_SECONDS, MAX_ORACLE_GAP_PCT, MAKER_WAIT_SECONDS,
    RISK_PER_TRADE_PCT, MAX_PORTFOLIO_RISK_PCT, MAX_NOTIONAL_USD, MIN_NOTIONAL_USD, STOP_ATR_MULT,
    TRADE_LOG, EQUITY_LOG, TRAILING_STOP_LOG, SUPERTREND_PERIOD, SUPERTREND_MULT,
    ADX_PERIOD, ADX_THRESHOLD,
    RSI_PERIOD, RSI_LONG_THRESHOLD, RSI_SHORT_THRESHOLD, RSI_LOOKBACK,
    EMA_PERIOD, EMA_BAND_PCT,
    FUNDING_LONG_MAX, FUNDING_SHORT_MIN, ADX_CVD_BOOST, ADX_DECAY_EXIT,
    VOLUME_CONFIRM_RATIO, MAX_HOLD_HOURS, STRUCT_STOP_BUFFER,
)
from notify import send_telegram
from signals import (
    compute_daily_vol, compute_atr, compute_cvd,
    compute_oi, compute_supertrend, compute_adx, compute_rsi, compute_ema,
    compute_volume_ratio, compute_struct_stops,
)
from exchange import (
    mainnet_info, exchange as hl_exchange,
    get_testnet_coins, get_testnet_price_map, get_testnet_book,
    place_alo_limit, cancel_order, get_open_positions, get_equity, wait_until,
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
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

def init_files():
    os.makedirs("data", exist_ok=True)
    for filepath, default in [(TRADE_LOG, []), (EQUITY_LOG, []), (TRAILING_STOP_LOG, {})]:
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

def get_top_symbols(top_n=TOP_N, extra_symbols=None):
    extra_symbols = extra_symbols or []
    try:
        meta_and_ctxs = mainnet_info.meta_and_asset_ctxs()
        universe = meta_and_ctxs[0]['universe']
        ctxs = meta_and_ctxs[1]

        markets = []
        for i, asset in enumerate(universe):
            if i >= len(ctxs):
                continue
            name = asset['name']
            if name.upper() in STABLECOINS:
                continue
            ctx = ctxs[i]
            day_volume = float(ctx.get('dayNtlVlm', 0))
            markets.append((name, day_volume))

        markets.sort(key=lambda x: x[1], reverse=True)

        testnet_coins = get_testnet_coins()
        if testnet_coins:
            tradeable = [(n, v) for n, v in markets if n in testnet_coins]
            skipped = [n for n, v in markets[:top_n] if n not in testnet_coins]
            if skipped:
                logger.info(f"  Skipping (not on testnet): {', '.join(skipped)}")
            markets = tradeable

        top = [name for name, vol in markets[:top_n]]

        for sym in PINNED:
            if sym not in top:
                if testnet_coins and sym not in testnet_coins:
                    logger.info(f"  Pin {sym} not on testnet — skipping")
                    continue
                top.append(sym)
                logger.info(f"  Pinning {sym} (always included)")

        for sym in extra_symbols:
            if sym not in top:
                top.append(sym)
                logger.info(f"  Keeping {sym} (open position, outside top {top_n})")

        logger.info(f"Top {top_n} testnet-tradeable perps by 24h dollar volume (stablecoins excluded):")
        for rank, (name, vol) in enumerate(markets[:top_n], 1):
            pin_tag = " [PINNED]" if name in PINNED else ""
            logger.info(f"  #{rank} {name}: ${vol:,.0f}{pin_tag}")

        return top
    except Exception as e:
        logger.error(f"Failed to rank symbols: {e}")
        fallback = ["BTC", "ETH", "SOL", "HYPE"]
        return list(set(fallback + extra_symbols))

# ─── Market Data ──────────────────────────────────────────────────────────────

def get_symbol_data(symbol, max_retries=3, retry_delay=5):
    for attempt in range(1, max_retries + 1):
        try:
            mids = mainnet_info.all_mids()
            if symbol not in mids:
                logger.warning(f"{symbol} not found on Hyperliquid")
                return None

            price = float(mids[symbol])
            end_time = int(time.time() * 1000)
            start_time = end_time - (48 * 60 * 60 * 1000)
            candles = mainnet_info.candles_snapshot(symbol, "1h", start_time, end_time)

            candle_summary = "\n".join([
                f"  open={c['o']} high={c['h']} low={c['l']} close={c['c']} volume={c['v']}"
                for c in candles[-10:]
            ])

            meta = mainnet_info.meta()
            asset_info = next((a for a in meta['universe'] if a['name'] == symbol), None)
            sz_decimals = int(asset_info['szDecimals']) if asset_info else 3

            # Daily candles — shared by Supertrend and ADX (one fetch, two indicators)
            try:
                d_end = int(time.time() * 1000)
                d_start = d_end - (90 * 24 * 60 * 60 * 1000)
                daily_candles = mainnet_info.candles_snapshot(symbol, "1d", d_start, d_end)
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
                "candle_summary": candle_summary,
                "sz_decimals": sz_decimals,
                "daily_vol": compute_daily_vol(candles),
                "atr": compute_atr(candles),
                "cvd": compute_cvd(candles),
                "oi": compute_oi(symbol, candles, price, mainnet_info),
                "supertrend": supertrend,
                "adx": adx,
                "rsi_60": compute_rsi(candles, period=RSI_PERIOD, lookback=RSI_LOOKBACK),
                "ema20_60": compute_ema(candles, period=EMA_PERIOD),
                "vol_ratio_60": compute_volume_ratio(candles, lookback=10),
                "struct_stops": compute_struct_stops(candles, lookback=RSI_LOOKBACK),
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
    all_data = {}
    failed = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_symbol_data, sym, 3, 5): sym for sym in symbols}
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
            f"OI=${data['oi']['oi_usd']:,.0f} | CVD={data['cvd']['cvd_trend']} | "
            f"Funding={data['oi']['funding']}% | {st_tag} | {adx_tag} | "
            f"{rsi_tag} | {ema_tag} | 4hRSI={rsi4_val:.1f} slope={slope_tag} | VolRatio={vol_r:.2f}"
        )

    if failed:
        logger.warning(f"Symbols skipped this cycle: {', '.join(failed)}")
    return all_data

# ─── Position Sizing ──────────────────────────────────────────────────────────

def compute_notional(symbol, all_data, equity):
    """Size position so a stop-out costs exactly RISK_PER_TRADE_PCT of equity."""
    atr   = all_data[symbol].get('atr')
    price = all_data[symbol].get('tn_price') or all_data[symbol].get('price', 0)
    if atr and price and atr > 0:
        dollar_risk   = equity * RISK_PER_TRADE_PCT
        stop_distance = STOP_ATR_MULT * atr
        size_tokens   = dollar_risk / stop_distance
        notional      = size_tokens * price
        notional      = min(notional, MAX_NOTIONAL_USD)
        notional      = max(notional, MIN_NOTIONAL_USD)
        logger.info(
            f"ATR sizing {symbol}: ATR=${atr:.4f} | stop=${stop_distance:.4f} | "
            f"risk ${dollar_risk:.2f} → {size_tokens:.4f} tok → ${notional:.2f} notional"
        )
        return notional, atr
    logger.warning(f"{symbol}: no ATR data — using max notional ${MAX_NOTIONAL_USD:.2f}")
    return MAX_NOTIONAL_USD, None

# ─── Entry (post-only maker) ──────────────────────────────────────────────────

def open_position(symbol, is_buy, all_data, equity,
                  cvd_signal, oi_signal, confluence, reason):
    """
    Post-only maker entry. Two attempts: passive (at touch), then aggressive
    (one tick inside, toward mid). If neither fills, the trade is skipped.
    """
    sz_decimals = all_data[symbol].get('sz_decimals', 3)
    notional_usd, atr_val = compute_notional(symbol, all_data, equity)
    direction = "LONG" if is_buy else "SHORT"

    try:
        hl_exchange.update_leverage(LEVERAGE, symbol, is_cross=True)
    except Exception as e:
        logger.warning(f"Could not set leverage for {symbol}: {e}")

    for attempt in (1, 2):
        best_bid, best_ask, tick, decimals = get_testnet_book(symbol)
        if best_bid is None:
            logger.error(f"{symbol}: no book — cannot place maker order")
            send_telegram(f"⚠️ <b>{symbol} {direction} skipped</b>\nNo testnet order book")
            return False

        if attempt == 1:
            limit_px = best_bid if is_buy else best_ask
            tag = "passive"
        else:
            limit_px = (best_ask - tick) if is_buy else (best_bid + tick)
            tag = "aggressive"
        limit_px = round(limit_px, decimals)

        size_tokens = round(notional_usd / limit_px, sz_decimals)
        min_size = 10 ** (-sz_decimals)
        if size_tokens < min_size:
            logger.warning(f"{symbol}: size {size_tokens} below minimum {min_size} — skipping")
            return False

        logger.info(f"{symbol} {direction} maker {tag} attempt: post {size_tokens} @ ${limit_px:.{decimals}f} "
                    f"(${notional_usd:.0f} notional)")

        status, oid, fpx, fsz, err = place_alo_limit(symbol, is_buy, size_tokens, limit_px)

        sym_data = all_data.get(symbol, {})

        if status == 'filled':
            return _finalize_open(symbol, direction, is_buy, notional_usd, atr_val,
                                  cvd_signal, oi_signal, confluence, reason, equity,
                                  sym_data=sym_data)

        if status == 'rejected':
            logger.info(f"{symbol} {tag} ALO rejected (would cross / {err}) — trying next")
            continue

        if status == 'error':
            logger.error(f"{symbol} order error: {err}")
            send_telegram(f"⚠️ <b>{symbol} {direction} order error</b>\n{err[:200]}")
            return False

        logger.info(f"{symbol} resting (oid {oid}) — waiting up to {MAKER_WAIT_SECONDS}s for fill")
        filled = wait_until(symbol, want_open=True, seconds=MAKER_WAIT_SECONDS)
        if filled:
            return _finalize_open(symbol, direction, is_buy, notional_usd, atr_val,
                                  cvd_signal, oi_signal, confluence, reason, equity,
                                  sym_data=sym_data)
        cancel_order(symbol, oid)
        logger.info(f"{symbol} {tag} maker order unfilled in {MAKER_WAIT_SECONDS}s")

    logger.info(f"{symbol} {direction}: no maker fill after 2 attempts — skipping (no fee paid)")
    send_telegram(f"⏳ <b>{symbol} {direction} skipped</b>\nMaker order didn't fill (no taker fee paid)")
    return False

def _finalize_open(symbol, direction, is_buy, notional_usd, atr_val,
                   cvd_signal, oi_signal, confluence, reason, equity, sym_data=None):
    pos = get_open_positions().get(symbol)
    fill_px = pos['entry'] if pos else 0
    fill_sz = abs(pos['size']) if pos else 0
    logger.success(f"✅ OPEN {direction} {symbol}: {fill_sz} @ ${fill_px:.4f} (MAKER) | "
                   f"Notional: ${notional_usd:.2f} | Confluence: {confluence}")

    # Compute structural stop from swing low/high over the RSI lookback window
    struct_stop = None
    if sym_data and fill_px:
        atr_val = sym_data.get('atr')
        sw_low, sw_high = sym_data.get('struct_stops', (None, None))
        if atr_val:
            if is_buy and sw_low and sw_low < fill_px:
                struct_stop = sw_low - STRUCT_STOP_BUFFER * atr_val
            elif not is_buy and sw_high and sw_high > fill_px:
                struct_stop = sw_high + STRUCT_STOP_BUFFER * atr_val
    if fill_px:
        init_peak(symbol, fill_px, struct_stop=struct_stop)
    action_label = "BUY" if is_buy else "SELL"
    log_trade(action_label, symbol, fill_sz, fill_px, reason, equity,
              cvd_signal, oi_signal, confluence)
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

def close_position_market(symbol, all_data, equity, reason,
                          cvd_signal="", oi_signal="", confluence=""):
    """Guaranteed-fill market close. Used for all automatic exits."""
    exec_price = all_data.get(symbol, {}).get('tn_price') or all_data.get(symbol, {}).get('price', 0)
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
            clear_peak(symbol)
            log_trade("CLOSE", symbol, 0, fill_px, reason, equity,
                      cvd_signal, oi_signal, confluence)
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
            raw[sym] = {"peak": float(val), "opened_at": None, "struct_stop": None}
            migrated = True
    if migrated:
        save_json(TRAILING_STOP_LOG, raw)
    return raw

def _save_peaks(peaks):
    save_json(TRAILING_STOP_LOG, peaks)

def init_peak(symbol, entry_price, struct_stop=None):
    peaks = _load_peaks()
    peaks[symbol] = {
        "peak": entry_price,
        "opened_at": datetime.now().isoformat(),
        "struct_stop": struct_stop,
    }
    _save_peaks(peaks)
    extra = f" | struct stop ${struct_stop:.4f}" if struct_stop is not None else ""
    logger.info(f"Chandelier {symbol}: peak initialised at ${entry_price:.4f}{extra}")

def clear_peak(symbol):
    peaks = _load_peaks()
    if symbol in peaks:
        del peaks[symbol]
        _save_peaks(peaks)


def check_stops(positions, all_data, equity):
    """
    Four exit triggers, evaluated in order per position:

    1. Supertrend flip — daily ST flips against position direction → market close.
    2. ADX decay — ADX drops below ADX_DECAY_EXIT → trend is dead, close.
    3. Time exit — held > MAX_HOLD_HOURS without reaching break-even → close.
    4. Chandelier trailing stop with break-even lock + structural floor.
       stop = peak ± STOP_ATR_MULT × ATR; at break-even the peak is floored
       so the stop never retreats below entry. The structural stop (swing
       low/high ± STRUCT_STOP_BUFFER × ATR) acts as the minimum stop floor
       in the early part of the trade before the chandelier catches up.
    """
    peaks = _load_peaks()
    peaks_changed = False
    closed_any = False

    for sym, p in list(positions.items()):
        data = all_data.get(sym)
        if not data:
            logger.warning(f"Stop check: no market data for {sym} — skipping")
            continue
        atr   = data.get('atr')
        price = data.get('tn_price') or data.get('price')
        entry = p['entry']
        side  = p['side']
        if not atr or not price or not entry:
            continue

        # ── 1. Supertrend flip exit ───────────────────────────────────────────
        st = data.get('supertrend', {})
        if st.get('changed'):
            st_dir = st.get('direction', 'neutral')
            flip_against = (side == "LONG" and st_dir == "bearish") or \
                           (side == "SHORT" and st_dir == "bullish")
            if flip_against:
                logger.warning(
                    f"🔄 ST FLIP EXIT {sym} {side}: daily Supertrend just flipped "
                    f"to {st_dir.upper()} — closing immediately"
                )
                send_telegram(
                    f"🔄 <b>SUPERTREND FLIP EXIT {sym} {side}</b>\n"
                    f"Daily ST flipped to {st_dir.upper()} — market close"
                )
                if close_position_market(sym, all_data, equity,
                                         f"Supertrend flipped to {st_dir}"):
                    clear_peak(sym)
                    peaks.pop(sym, None)
                    closed_any = True
                    time.sleep(1)
                continue

        # ── 2. ADX decay exit ─────────────────────────────────────────────────
        adx_val = data.get('adx', {}).get('adx', 0.0)
        if adx_val < ADX_DECAY_EXIT:
            logger.warning(
                f"📉 ADX DECAY EXIT {sym} {side}: ADX={adx_val:.1f} < {ADX_DECAY_EXIT} — trend gone"
            )
            send_telegram(
                f"📉 <b>ADX DECAY EXIT {sym} {side}</b>\n"
                f"ADX={adx_val:.1f} dropped below {ADX_DECAY_EXIT} — trend exhausted"
            )
            if close_position_market(sym, all_data, equity,
                                     f"ADX decay ({adx_val:.1f} < {ADX_DECAY_EXIT})"):
                clear_peak(sym)
                peaks.pop(sym, None)
                closed_any = True
                time.sleep(1)
            continue

        # Ensure peak entry exists in new dict format
        if sym not in peaks:
            peaks[sym] = {"peak": entry, "opened_at": None, "struct_stop": None}
            peaks_changed = True

        peak_data     = peaks[sym]
        peak          = peak_data["peak"]
        opened_at     = peak_data.get("opened_at")
        struct_stop   = peak_data.get("struct_stop")
        stop_distance = STOP_ATR_MULT * atr

        # ── 3. Time exit (no break-even after MAX_HOLD_HOURS) ─────────────────
        if opened_at:
            try:
                hours_held = (datetime.now() - datetime.fromisoformat(opened_at)).total_seconds() / 3600
                be_reached = (side == "LONG"  and peak >= entry + stop_distance) or \
                             (side == "SHORT" and peak <= entry - stop_distance)
                if hours_held > MAX_HOLD_HOURS and not be_reached:
                    logger.warning(
                        f"⏱ TIME EXIT {sym} {side}: held {hours_held:.1f}h without break-even"
                    )
                    send_telegram(
                        f"⏱ <b>TIME EXIT {sym} {side}</b>\n"
                        f"Held {hours_held:.1f}h without reaching break-even"
                    )
                    if close_position_market(sym, all_data, equity,
                                             f"Time exit ({hours_held:.1f}h, no break-even)"):
                        clear_peak(sym)
                        peaks.pop(sym, None)
                        closed_any = True
                        time.sleep(1)
                    continue
            except Exception as e:
                logger.debug(f"Time exit check for {sym} failed: {e}")

        # ── 4. Chandelier stop + structural floor ─────────────────────────────
        if side == "LONG":
            new_peak = max(peak, price)
            if price - entry >= atr:
                new_peak = max(new_peak, entry + stop_distance)
            chandelier_stop = new_peak - stop_distance
            if struct_stop is not None and struct_stop < entry:
                chandelier_stop = max(chandelier_stop, struct_stop)
            stop_price = chandelier_stop
            breached   = price <= stop_price
        else:
            new_peak = min(peak, price)
            if entry - price >= atr:
                new_peak = min(new_peak, entry - stop_distance)
            chandelier_stop = new_peak + stop_distance
            if struct_stop is not None and struct_stop > entry:
                chandelier_stop = min(chandelier_stop, struct_stop)
            stop_price = chandelier_stop
            breached   = price >= stop_price

        if new_peak != peak:
            peaks[sym]["peak"] = new_peak
            peaks_changed = True

        be_active  = (side == "LONG"  and new_peak >= entry + stop_distance) or \
                     (side == "SHORT" and new_peak <= entry - stop_distance)
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
                clear_peak(sym)
                peaks.pop(sym, None)
                closed_any = True
                time.sleep(1)

    if peaks_changed:
        _save_peaks(peaks)
    return closed_any

# (Claude exit management removed — exits handled automatically by check_stops)


# ─── Rule-Based Entry Selection ───────────────────────────────────────────────

def select_entry(all_data, positions):
    """
    Evaluates all 5 entry conditions for every non-held symbol and returns
    (symbol, is_long) for the best qualifying setup (highest ADX), or (None, None).

    Conditions:
      C1a  Daily Supertrend direction (bullish → long, bearish → short)
      C1b  Daily ADX > ADX_THRESHOLD
      C2   60-min RSI dipped below RSI_LONG_THRESHOLD / spiked above RSI_SHORT_THRESHOLD
      C3   Price within ±EMA_BAND_PCT of 20-EMA on 60-min
      C4   RSI hook back in trend direction
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
        funding = data.get('oi', {}).get('funding', 0.0)
        if is_long and funding > FUNDING_LONG_MAX:
            logger.debug(f"  {symbol}: funding {funding:.4f}% > {FUNDING_LONG_MAX}% — skip long (crowded)")
            continue
        if not is_long and funding < FUNDING_SHORT_MIN:
            logger.debug(f"  {symbol}: funding {funding:.4f}% < {FUNDING_SHORT_MIN}% — skip short (crowded)")
            continue

        # ADX gate — raise threshold when CVD disagrees with direction
        adx_data  = data.get('adx', {})
        adx_val   = adx_data.get('adx', 0.0)
        cvd_trend = data.get('cvd', {}).get('cvd_trend', 'neutral')
        cvd_agrees  = (is_long and cvd_trend == 'rising') or (not is_long and cvd_trend == 'falling')
        required_adx = ADX_THRESHOLD if cvd_agrees else ADX_CVD_BOOST
        if adx_val < required_adx:
            label = "agrees" if cvd_agrees else f"disagrees → need {ADX_CVD_BOOST}"
            logger.debug(f"  {symbol}: ADX {adx_val:.1f} < {required_adx} (CVD {label}) — skip")
            continue

        passed, pb_reason = _check_pullback_entry(symbol, all_data, action)
        if not passed:
            logger.debug(f"  {symbol} {action}: pullback not ready — {pb_reason}")
            continue

        logger.info(f"  {symbol} {action}: SETUP READY — ADX={adx_val:.1f} | {pb_reason}")
        candidates.append((symbol, is_long, adx_val))

    if not candidates:
        logger.info("No entry setup ready this cycle")
        return None, None

    candidates.sort(key=lambda x: x[2], reverse=True)
    best_sym, best_is_long, best_adx = candidates[0]
    direction = "LONG" if best_is_long else "SHORT"
    logger.info(
        f"Best entry: {best_sym} {direction} (ADX={best_adx:.1f}) "
        f"— {len(candidates)} setup(s) qualified"
    )
    return best_sym, best_is_long

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

    if ema_val and price:
        pct_from_ema = abs(price - ema_val) / ema_val
        near_ema = pct_from_ema <= EMA_BAND_PCT
        pct_str  = f"{(price - ema_val) / ema_val * 100:+.1f}%"
    else:
        near_ema = False
        pct_str  = "n/a"
    ema_str = f"${ema_val:.4f}" if ema_val else "n/a"

    c5 = vol_ratio >= VOLUME_CONFIRM_RATIO

    if action == "OPEN_LONG":
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

def log_trade(action, symbol, size, price, reason, equity,
              cvd_signal="", oi_signal="", confluence=""):
    trades = load_json(TRADE_LOG, [])
    trades.append({
        "timestamp": datetime.now().isoformat(),
        "action": action, "symbol": symbol, "size": size, "price": price,
        "reason": reason, "equity": equity,
        "cvd_signal": cvd_signal, "oi_signal": oi_signal,
        "confluence": confluence,
        "leverage": LEVERAGE, "notional": size * price
    })
    save_json(TRADE_LOG, trades)

def log_equity(equity, all_data, positions):
    curve = load_json(EQUITY_LOG, [])
    curve.append({
        "timestamp": datetime.now().isoformat(),
        "equity": equity,
        "prices": {sym: data['tn_price'] for sym, data in all_data.items()},
        "volume_24h": {sym: data['oi']['day_volume'] for sym, data in all_data.items()},
        "positions": {sym: {"side": p["side"], "size": p["size"], "entry": p["entry"]}
                      for sym, p in positions.items()}
    })
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

    start_equity = curve[0]['equity']
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
                funding = all_data.get(sym, {}).get('oi', {}).get('funding', 0)
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
    logger.info("=== Claude Long/Short Orderflow Bot Started (MAKER orders) ===")
    logger.info(f"Dynamic selection: TOP {TOP_N} testnet-tradeable perps by 24h dollar volume")
    logger.info(f"Pinned symbols: {', '.join(PINNED)}")
    logger.info(f"Entries: RULE-BASED (Supertrend + ADX + pullback) — post-only maker")
    logger.info(f"Exits: ST flip | ADX decay <{ADX_DECAY_EXIT} | time >{MAX_HOLD_HOURS}h | chandelier {STOP_ATR_MULT}×ATR + struct stop")
    logger.info(f"Sizing: ATR-BASED {RISK_PER_TRADE_PCT*100:.0f}% equity risk/trade | portfolio cap {MAX_PORTFOLIO_RISK_PCT*100:.0f}% | notional ${MIN_NOTIONAL_USD}–${MAX_NOTIONAL_USD}")
    logger.info(f"Stop: chandelier {STOP_ATR_MULT}×ATR trailing | Break-even lock at +1×ATR")
    logger.info(f"Leverage: {LEVERAGE}x | Max positions: {MAX_POSITIONS} | Gap skip: >{MAX_ORACLE_GAP_PCT}%")
    logger.info(f"Data: MAINNET | Trading: TESTNET | Interval: {INTERVAL_MINUTES}min (clock-aligned)")

    init_files()
    get_testnet_coins()
    send_telegram(
        "🤖 <b>Trading Bot Started</b>\n"
        f"Top {TOP_N} liquid perps | {LEVERAGE}x | Max {MAX_POSITIONS}\n"
        f"Entries: rule-based (ST + ADX + pullback) | Post-only maker\n"
        f"Exits: ST flip | ADX decay | time {MAX_HOLD_HOURS}h | chandelier {STOP_ATR_MULT}×ATR\n"
        "Data: MAINNET | Trading: TESTNET"
    )

    while True:
        try:
            logger.info("--- New cycle ---")

            positions = get_open_positions()
            symbols   = get_top_symbols(TOP_N, extra_symbols=list(positions.keys()))
            all_data  = get_all_market_data(symbols, open_position_syms=set(positions.keys()))
            equity    = get_equity()

            logger.info(f"Equity: ${equity:.2f} | Open positions: {len(positions)}/{MAX_POSITIONS}")
            log_equity(equity, all_data, positions)

            # ── Step 1: Automatic stops (chandelier + Supertrend flip) ────────
            if positions:
                stopped = check_stops(positions, all_data, equity)
                if stopped:
                    time.sleep(SETTLE_SECONDS)
                    positions = get_open_positions()
                    equity    = get_equity()

            # ── Step 2: Rule-based entry (only if slot available + heat OK) ────
            if len(positions) < MAX_POSITIONS:
                logger.info("Scanning for entry setups...")
                entry_sym, is_long = select_entry(all_data, positions)
                if entry_sym:
                    open_risk = sum(
                        abs(p['size']) * (all_data.get(sym, {}).get('atr') or 0) * STOP_ATR_MULT
                        for sym, p in positions.items()
                    )
                    heat_pct = open_risk / equity if equity > 0 else 0
                    logger.info(f"Portfolio heat: ${open_risk:.2f} = {heat_pct*100:.1f}% of equity")
                    if heat_pct >= MAX_PORTFOLIO_RISK_PCT:
                        logger.info(
                            f"Heat {heat_pct*100:.1f}% ≥ {MAX_PORTFOLIO_RISK_PCT*100:.0f}% cap — skipping {entry_sym}"
                        )
                        entry_sym = None
                    data       = all_data[entry_sym]
                    cvd_signal = data.get('cvd', {}).get('cvd_trend', 'neutral')
                    oi_signal  = data.get('oi', {}).get('oi_signal', 'stable')
                    adx_val    = data.get('adx', {}).get('adx', 0)
                    open_position(
                        entry_sym, is_long, all_data, equity,
                        cvd_signal, oi_signal,
                        confluence="7/7 rule-based",
                        reason=f"All 7 conditions met (ADX={adx_val:.1f})"
                    )
                    time.sleep(SETTLE_SECONDS)

            positions = get_open_positions()
            equity    = get_equity()
            print_summary(equity, positions, all_data)

            sleep_until_next_hour()

        except Exception as e:
            logger.error(f"Error: {e}")
            send_telegram(f"⚠️ <b>Bot Error</b>\n{str(e)[:300]}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
