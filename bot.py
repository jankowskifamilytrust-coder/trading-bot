import os
import time
import json
import anthropic
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger

from config import (
    TOP_N, PINNED, STABLECOINS, MAX_POSITIONS, LEVERAGE, INTERVAL_MINUTES,
    SLIPPAGE, SETTLE_SECONDS, MAX_ORACLE_GAP_PCT, MAKER_WAIT_SECONDS,
    VOL_TARGET_PCT, MAX_NOTIONAL_USD, MIN_NOTIONAL_USD, STOP_ATR_MULT,
    TRADE_LOG, EQUITY_LOG, TRAILING_STOP_LOG, SUPERTREND_PERIOD, SUPERTREND_MULT,
    ADX_PERIOD, ADX_THRESHOLD,
    RSI_PERIOD, RSI_LONG_THRESHOLD, RSI_SHORT_THRESHOLD, RSI_LOOKBACK,
    EMA_PERIOD, EMA_BAND_PCT,
)
from notify import send_telegram
from signals import (
    compute_daily_vol, compute_atr, compute_cvd, compute_obi, compute_vpin,
    compute_oi, compute_supertrend, compute_adx, compute_rsi, compute_ema,
)
from exchange import (
    mainnet_info, exchange as hl_exchange,
    get_testnet_coins, get_testnet_price_map, get_testnet_book,
    place_alo_limit, cancel_order, get_open_positions, get_equity, wait_until,
)

load_dotenv()
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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

            l2 = mainnet_info.l2_snapshot(symbol)

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

            return {
                "symbol": symbol, "price": price,
                "tn_price": price,
                "candle_summary": candle_summary,
                "sz_decimals": sz_decimals,
                "daily_vol": compute_daily_vol(candles),
                "atr": compute_atr(candles),
                "cvd": compute_cvd(candles),
                "obi": compute_obi(l2),
                "vpin": compute_vpin(candles),
                "oi": compute_oi(symbol, candles, price, mainnet_info),
                "supertrend": supertrend,
                "adx": adx,
                "rsi_60": compute_rsi(candles, period=RSI_PERIOD, lookback=RSI_LOOKBACK),
                "ema20_60": compute_ema(candles, period=EMA_PERIOD),
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
    logger.info("Fetching market data + orderflow for selected symbols...")
    all_data = {}
    failed = []
    for symbol in symbols:
        data = get_symbol_data(symbol, max_retries=3, retry_delay=5)
        if data:
            all_data[symbol] = data
        else:
            failed.append(symbol)
        time.sleep(0.5)

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
        logger.info(
            f"  {sym}: testnet ${data['tn_price']:.4f} | DailyVol={vol_str} | "
            f"OI=${data['oi']['oi_usd']:,.0f} | CVD={data['cvd']['cvd_trend']} | "
            f"OBI={data['obi']['obi']} | Funding={data['oi']['funding']}% | {st_tag} | {adx_tag} | "
            f"{rsi_tag} | {ema_tag}"
        )

    if failed:
        logger.warning(f"Symbols skipped this cycle: {', '.join(failed)}")
    return all_data

# ─── Position Sizing ──────────────────────────────────────────────────────────

def compute_notional(symbol, all_data, equity):
    daily_vol = all_data[symbol].get('daily_vol')
    if daily_vol and daily_vol > 0:
        target_risk_usd = equity * VOL_TARGET_PCT
        vol_notional = target_risk_usd / daily_vol
        notional = min(vol_notional, MAX_NOTIONAL_USD)
        notional = max(notional, MIN_NOTIONAL_USD)
        logger.info(
            f"Vol sizing {symbol}: daily_vol={daily_vol*100:.1f}% | target risk ${target_risk_usd:.2f} | "
            f"vol notional ${vol_notional:.2f} → final ${notional:.2f}"
        )
        return notional, daily_vol
    logger.warning(f"{symbol}: no vol data — using max notional ${MAX_NOTIONAL_USD:.2f}")
    return MAX_NOTIONAL_USD, None

# ─── Entry (post-only maker) ──────────────────────────────────────────────────

def open_position(symbol, is_buy, all_data, equity,
                  cvd_signal, obi_signal, oi_signal, confluence, reason):
    """
    Post-only maker entry. Two attempts: passive (at touch), then aggressive
    (one tick inside, toward mid). If neither fills, the trade is skipped.
    """
    sz_decimals = all_data[symbol].get('sz_decimals', 3)
    notional_usd, daily_vol = compute_notional(symbol, all_data, equity)
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

        if status == 'filled':
            return _finalize_open(symbol, direction, is_buy, notional_usd, daily_vol,
                                  cvd_signal, obi_signal, oi_signal, confluence, reason, equity)

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
            return _finalize_open(symbol, direction, is_buy, notional_usd, daily_vol,
                                  cvd_signal, obi_signal, oi_signal, confluence, reason, equity)
        cancel_order(symbol, oid)
        logger.info(f"{symbol} {tag} maker order unfilled in {MAKER_WAIT_SECONDS}s")

    logger.info(f"{symbol} {direction}: no maker fill after 2 attempts — skipping (no fee paid)")
    send_telegram(f"⏳ <b>{symbol} {direction} skipped</b>\nMaker order didn't fill (no taker fee paid)")
    return False

def _finalize_open(symbol, direction, is_buy, notional_usd, daily_vol,
                   cvd_signal, obi_signal, oi_signal, confluence, reason, equity):
    pos = get_open_positions().get(symbol)
    fill_px = pos['entry'] if pos else 0
    fill_sz = abs(pos['size']) if pos else 0
    logger.success(f"✅ OPEN {direction} {symbol}: {fill_sz} @ ${fill_px:.4f} (MAKER) | "
                   f"Notional: ${notional_usd:.2f} | Confluence: {confluence}")
    if fill_px:
        init_peak(symbol, fill_px)
    action_label = "BUY" if is_buy else "SELL"
    log_trade(action_label, symbol, fill_sz, fill_px, reason, equity,
              cvd_signal, obi_signal, oi_signal, confluence)
    emoji = "🟢" if is_buy else "🟠"
    vol_pct = f"{daily_vol*100:.1f}%" if daily_vol else "n/a"
    send_telegram(
        f"{emoji} <b>OPEN {direction}</b> (maker)\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Fill: ${fill_px:.4f}\n"
        f"Size: {fill_sz} (${notional_usd:.0f} notional)\n"
        f"Daily vol: {vol_pct} | Risk-targeted {VOL_TARGET_PCT*100:.0f}%\n"
        f"Confluence: {confluence}\n"
        f"Reason: {reason}"
    )
    return True

# ─── Exits ────────────────────────────────────────────────────────────────────

def close_position_market(symbol, all_data, equity, reason,
                          cvd_signal="", obi_signal="", oi_signal="", confluence=""):
    """Guaranteed-fill taker close. Used for stops, flips, and as maker-close fallback."""
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
            logger.success(f"✅ CLOSE {symbol} @ ${fill_px:.4f} (taker)")
            clear_peak(symbol)
            log_trade("CLOSE", symbol, 0, fill_px, reason, equity,
                      cvd_signal, obi_signal, oi_signal, confluence)
            send_telegram(f"🔴 <b>CLOSE</b> (market)\nSymbol: <b>{symbol}</b>\nPrice: ${fill_px:.4f}\nReason: {reason}")
            return True
        logger.error(f"❌ CLOSE {symbol} REJECTED: {err}")
        send_telegram(f"⚠️ <b>Close {symbol} REJECTED</b>\n{err}")
        return False
    except Exception as e:
        logger.error(f"Market close {symbol} failed: {e}")
        send_telegram(f"⚠️ <b>Close {symbol} FAILED</b>\n{str(e)[:200]}")
        return False

def _log_maker_close(symbol, fill_px, reason, equity, cvd_signal, obi_signal, oi_signal, confluence):
    logger.success(f"✅ CLOSE {symbol} @ ${fill_px:.4f} (MAKER)")
    clear_peak(symbol)
    log_trade("CLOSE", symbol, 0, fill_px, reason, equity, cvd_signal, obi_signal, oi_signal, confluence)
    send_telegram(f"🔴 <b>CLOSE</b> (maker)\nSymbol: <b>{symbol}</b>\nPrice: ${fill_px:.4f}\nReason: {reason}")

def close_position_maker(symbol, all_data, equity, reason,
                         cvd_signal="", obi_signal="", oi_signal="", confluence=""):
    """
    Discretionary close: try a post-only maker exit first, then FALL BACK to a
    market close if it doesn't fill — a close must always complete.
    """
    pos = get_open_positions().get(symbol)
    if not pos:
        logger.warning(f"{symbol}: no position to close")
        return False
    is_buy = pos['side'] == "SHORT"
    size_tokens = abs(pos['size'])

    best_bid, best_ask, tick, decimals = get_testnet_book(symbol)
    if best_bid is not None:
        limit_px = best_ask if not is_buy else best_bid
        limit_px = round(limit_px, decimals)
        logger.info(f"{symbol} maker close attempt: {size_tokens} @ ${limit_px:.{decimals}f}")
        status, oid, fpx, fsz, err = place_alo_limit(symbol, is_buy, size_tokens, limit_px, reduce_only=True)
        if status == 'resting':
            if wait_until(symbol, want_open=False, seconds=MAKER_WAIT_SECONDS):
                _log_maker_close(symbol, limit_px, reason, equity, cvd_signal, obi_signal, oi_signal, confluence)
                return True
            cancel_order(symbol, oid)
            logger.info(f"{symbol} maker close unfilled — falling back to market")
        elif status == 'filled':
            _log_maker_close(symbol, fpx, reason, equity, cvd_signal, obi_signal, oi_signal, confluence)
            return True
        else:
            logger.info(f"{symbol} maker close not resting ({status}: {err}) — falling back to market")

    return close_position_market(symbol, all_data, equity, f"{reason} (market fallback)",
                                 cvd_signal, obi_signal, oi_signal, confluence)

# ─── Chandelier (Trailing ATR) Stop ──────────────────────────────────────────

def _load_peaks():
    return load_json(TRAILING_STOP_LOG, {})

def _save_peaks(peaks):
    save_json(TRAILING_STOP_LOG, peaks)

def init_peak(symbol, entry_price):
    peaks = _load_peaks()
    peaks[symbol] = entry_price
    _save_peaks(peaks)
    logger.info(f"Chandelier {symbol}: peak initialised at ${entry_price:.4f}")

def clear_peak(symbol):
    peaks = _load_peaks()
    if symbol in peaks:
        del peaks[symbol]
        _save_peaks(peaks)


def check_stops(positions, all_data, equity):
    """
    Two exit triggers, evaluated in order:

    1. Supertrend flip exit — if the daily Supertrend flips against the
       position direction on this candle, close immediately (market order).
       Catches trend reversals before the chandelier can react.

    2. Chandelier (trailing ATR) stop with break-even lock —
       stop = peak ± STOP_ATR_MULT × ATR, peak ratchets in the profitable
       direction only. Once profit ≥ 1×ATR, the peak is floored so the stop
       never falls below entry (break-even lock).
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
                continue  # skip chandelier for this symbol

        # ── 2. Chandelier stop with break-even lock ───────────────────────────
        if sym not in peaks:
            peaks[sym] = entry
            peaks_changed = True

        peak          = peaks[sym]
        stop_distance = STOP_ATR_MULT * atr

        if side == "LONG":
            new_peak = max(peak, price)
            # Break-even: profit ≥ 1×ATR → floor peak so stop ≥ entry
            if price - entry >= atr:
                new_peak = max(new_peak, entry + stop_distance)
            stop_price = new_peak - stop_distance
            breached   = price <= stop_price
        else:
            new_peak = min(peak, price)
            if entry - price >= atr:
                new_peak = min(new_peak, entry - stop_distance)
            stop_price = new_peak + stop_distance
            breached   = price >= stop_price

        if new_peak != peak:
            peaks[sym] = new_peak
            peaks_changed = True

        be_active = (side == "LONG" and new_peak >= entry + stop_distance) or \
                    (side == "SHORT" and new_peak <= entry - stop_distance)
        be_tag = " [BE]" if be_active else ""
        logger.debug(
            f"Chandelier {sym} {side}: entry=${entry:.4f} peak=${new_peak:.4f} "
            f"stop=${stop_price:.4f}{be_tag} | now=${price:.4f}"
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

# ─── Claude Exit Management ───────────────────────────────────────────────────

def ask_claude_exits(positions, all_data, equity):
    """
    Claude's sole job: decide whether to CLOSE, FLIP, or HOLD each open position.
    It never sees the full symbol universe and cannot open new positions.
    Entries are handled by the rule-based select_entry().
    """
    pos_summary = ""
    for sym, p in positions.items():
        data  = all_data.get(sym, {})
        price = data.get('tn_price') or data.get('price', 0)
        entry = p['entry']
        size  = abs(p['size'])
        side  = p['side']
        unreal     = _pos_pnl(p, price) if price else 0
        unreal_pct = (unreal / (size * entry) * 100) if entry and size else 0
        sign   = "+" if unreal >= 0 else ""
        st     = data.get('supertrend', {})
        adx    = data.get('adx', {})
        rsi_d  = data.get('rsi_60', {})
        ema_v  = data.get('ema20_60')
        pct_ema = f"{(price - ema_v) / ema_v * 100:+.1f}%" if ema_v and price else "n/a"
        cvd    = data.get('cvd', {})
        obi    = data.get('obi', {})
        vpin   = data.get('vpin', {})
        oi     = data.get('oi', {})

        pos_summary += f"""
{sym} {side} {size} @ ${entry:.4f} | Now ${price:.4f} | P&L: {sign}${unreal:.2f} ({sign}{unreal_pct:.1f}%)
  Supertrend (daily): {st.get('direction','?').upper()} @ {st.get('value',0):.4f}{' ← JUST FLIPPED' if st.get('changed') else ''}
  ADX: {adx.get('adx',0):.1f} | +DI {adx.get('plus_di',0):.1f} / -DI {adx.get('minus_di',0):.1f}
  RSI(60m): {rsi_d.get('rsi',50):.1f} (prev {rsi_d.get('prev_rsi',50):.1f}) | Price vs EMA20: {pct_ema}
  CVD: {cvd.get('cvd_trend','?')} | {cvd.get('divergence','?')}
  OBI: {obi.get('obi',0)} | {obi.get('signal','?')}
  VPIN: {vpin.get('vpin',0)} | {vpin.get('signal','?')}
  OI: {oi.get('oi_signal','?')} | Funding: {oi.get('funding',0):.4f}% ({oi.get('funding_signal','?')})
"""

    prompt = f"""You are a professional crypto trading assistant managing open positions on Hyperliquid perps.

Your ONLY job: decide whether to CLOSE, FLIP, or HOLD each open position.
Entries are handled separately by a rule-based system — do NOT suggest opening new positions.

Account equity: ${equity:.2f} | Leverage: {LEVERAGE}x | Positions: {len(positions)}/{MAX_POSITIONS}
Chandelier trailing stop ({STOP_ATR_MULT}×ATR from peak) and Supertrend flip exit run automatically.

Open positions:
{pos_summary}

Exit signals to act on:
- CVD divergence against position (price up + CVD falling for a long) → CLOSE
- OBI flipping against position direction → CLOSE
- VPIN > 0.4 + divergence → informed traders moving against you → CLOSE
- OI falling while price holds → short-covering rally / weak trend → CLOSE
- Funding rate strongly against your direction (paying the crowd) → CLOSE or FLIP
- Daily Supertrend already flipped against your position → FLIP
- All signals still aligned with position → HOLD

FLIP only on 3+ signal reversal (Supertrend flip + CVD divergence + OBI flip).
Pick the SINGLE most urgent action, or HOLD if positions look healthy.

Respond in this exact format:
SYMBOL: asset symbol (or NONE)
ACTION: CLOSE or FLIP or HOLD
CVD_SIGNAL: bullish or bearish or neutral
OBI_SIGNAL: bullish or bearish or neutral
OI_SIGNAL: bullish or bearish or neutral
CONFLUENCE: score out of 4
REASON: one sentence
"""

    message = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


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

        adx_data = data.get('adx', {})
        if not adx_data.get('trending', False):
            logger.debug(f"  {symbol}: ADX {adx_data.get('adx',0):.1f} ≤ {ADX_THRESHOLD} — skip")
            continue

        passed, pb_reason = _check_pullback_entry(symbol, all_data, action)
        if not passed:
            logger.debug(f"  {symbol} {action}: pullback not ready — {pb_reason}")
            continue

        adx_val = adx_data.get('adx', 0.0)
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

# ─── Exit Routing ─────────────────────────────────────────────────────────────

def _check_pullback_entry(symbol, all_data, action):
    """
    Validates pullback conditions C2–C4 for OPEN_LONG / OPEN_SHORT.
    C2: RSI dipped below RSI_LONG_THRESHOLD (long) or spiked above RSI_SHORT_THRESHOLD (short)
        within the last RSI_LOOKBACK bars.
    C3: Price is within EMA_BAND_PCT of the 20-EMA on 60-min.
    C4: RSI is now hooking back in the direction of the trade (up for long, down for short).
    Returns (passed: bool, reason: str).
    """
    data     = all_data[symbol]
    rsi_data = data.get('rsi_60', {})
    ema_val  = data.get('ema20_60')
    price    = data.get('tn_price') or data.get('price', 0)

    rsi      = rsi_data.get('rsi', 50.0)
    prev_rsi = rsi_data.get('prev_rsi', 50.0)

    if ema_val and price:
        pct_from_ema = abs(price - ema_val) / ema_val
        near_ema = pct_from_ema <= EMA_BAND_PCT
        pct_str  = f"{(price - ema_val) / ema_val * 100:+.1f}%"
    else:
        near_ema = False
        pct_str  = "n/a"
    ema_str = f"${ema_val:.4f}" if ema_val else "n/a"

    if action == "OPEN_LONG":
        c2 = rsi_data.get('min_recent', 50.0) < RSI_LONG_THRESHOLD
        c3 = near_ema
        c4 = rsi > prev_rsi
        passed = c2 and c3 and c4
        reason = (
            f"C2(dip<{RSI_LONG_THRESHOLD})={'✓' if c2 else '✗'}[min={rsi_data.get('min_recent',50):.1f}] "
            f"C3(near EMA {ema_str} {pct_str})={'✓' if c3 else '✗'} "
            f"C4(hook↑ {prev_rsi:.1f}→{rsi:.1f})={'✓' if c4 else '✗'}"
        )
    elif action == "OPEN_SHORT":
        c2 = rsi_data.get('max_recent', 50.0) > RSI_SHORT_THRESHOLD
        c3 = near_ema
        c4 = rsi < prev_rsi
        passed = c2 and c3 and c4
        reason = (
            f"C2(spike>{RSI_SHORT_THRESHOLD})={'✓' if c2 else '✗'}[max={rsi_data.get('max_recent',50):.1f}] "
            f"C3(near EMA {ema_str} {pct_str})={'✓' if c3 else '✗'} "
            f"C4(hook↓ {prev_rsi:.1f}→{rsi:.1f})={'✓' if c4 else '✗'}"
        )
    else:
        return True, ""

    return passed, reason


def _parse_decision(text):
    fields = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields

def execute_exit(decision_text, all_data, equity, positions):
    """Routes Claude's exit decision (CLOSE / FLIP / HOLD only)."""
    f = _parse_decision(decision_text)
    symbol     = f.get("SYMBOL", "")
    action     = f.get("ACTION", "").upper()
    cvd_signal = f.get("CVD_SIGNAL", "")
    obi_signal = f.get("OBI_SIGNAL", "")
    oi_signal  = f.get("OI_SIGNAL", "")
    confluence = f.get("CONFLUENCE", "")
    reason     = f.get("REASON", "")

    logger.info(f"Claude exit: {symbol} {action} | {confluence} | {reason}")

    if action == "HOLD" or symbol in ["NONE", ""]:
        logger.info("Claude: HOLD — positions look healthy")
        return False

    if symbol not in all_data:
        logger.error(f"{symbol} not in market data")
        return False

    held = positions.get(symbol)

    if action == "CLOSE":
        if not held:
            logger.warning(f"{symbol} has no open position to CLOSE")
            return False
        return close_position_maker(symbol, all_data, equity, reason,
                                    cvd_signal, obi_signal, oi_signal, confluence)

    if action == "FLIP":
        if not held:
            logger.warning(f"{symbol} no position to FLIP")
            return False
        current_side = held['side']
        logger.info(f"FLIP {symbol}: market-closing {current_side} then maker-opening opposite")
        if close_position_market(symbol, all_data, equity, f"FLIP close: {reason}",
                                 cvd_signal, obi_signal, oi_signal, confluence):
            time.sleep(SETTLE_SECONDS)
            new_is_buy = current_side == "SHORT"
            return open_position(symbol, new_is_buy, all_data, equity,
                                 cvd_signal, obi_signal, oi_signal, confluence,
                                 f"FLIP open: {reason}")
        return False

    logger.warning(f"Unexpected action from Claude exit: {action}")
    return False

# ─── Logging ──────────────────────────────────────────────────────────────────

def log_trade(action, symbol, size, price, reason, equity,
              cvd_signal="", obi_signal="", oi_signal="", confluence=""):
    trades = load_json(TRADE_LOG, [])
    trades.append({
        "timestamp": datetime.now().isoformat(),
        "action": action, "symbol": symbol, "size": size, "price": price,
        "reason": reason, "equity": equity,
        "cvd_signal": cvd_signal, "obi_signal": obi_signal,
        "oi_signal": oi_signal, "confluence": confluence,
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
    logger.info(f"Exits: chandelier trailing stop + Supertrend flip (automatic) + Claude (discretionary)")
    logger.info(f"Sizing: VOL-TARGETED {VOL_TARGET_PCT*100:.0f}% daily risk, cap ${MAX_NOTIONAL_USD} notional")
    logger.info(f"Stop: chandelier {STOP_ATR_MULT}×ATR trailing | Break-even lock at +1×ATR")
    logger.info(f"Leverage: {LEVERAGE}x | Max positions: {MAX_POSITIONS} | Gap skip: >{MAX_ORACLE_GAP_PCT}%")
    logger.info(f"Data: MAINNET | Trading: TESTNET | Interval: {INTERVAL_MINUTES}min (clock-aligned)")

    init_files()
    get_testnet_coins()
    send_telegram(
        "🤖 <b>Trading Bot Started</b>\n"
        f"Top {TOP_N} liquid perps | {LEVERAGE}x | Max {MAX_POSITIONS}\n"
        f"Entries: rule-based (ST + ADX + pullback) | Post-only maker\n"
        f"Exits: chandelier {STOP_ATR_MULT}×ATR + ST flip + Claude\n"
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

            # ── Step 2: Claude exit management (only if holding positions) ────
            if positions:
                logger.info("Asking Claude to review open positions for exits...")
                exit_decision = ask_claude_exits(positions, all_data, equity)
                logger.info(f"Claude exit:\n{exit_decision}")
                exited = execute_exit(exit_decision, all_data, equity, positions)
                if exited:
                    time.sleep(SETTLE_SECONDS)
                    positions = get_open_positions()
                    equity    = get_equity()

            # ── Step 3: Rule-based entry (only if slot available) ─────────────
            if len(positions) < MAX_POSITIONS:
                logger.info("Scanning for entry setups...")
                entry_sym, is_long = select_entry(all_data, positions)
                if entry_sym:
                    data       = all_data[entry_sym]
                    cvd_signal = data.get('cvd', {}).get('cvd_trend', 'neutral')
                    obi_signal = data.get('obi', {}).get('signal', 'neutral')
                    oi_signal  = data.get('oi', {}).get('oi_signal', 'stable')
                    adx_val    = data.get('adx', {}).get('adx', 0)
                    open_position(
                        entry_sym, is_long, all_data, equity,
                        cvd_signal, obi_signal, oi_signal,
                        confluence="5/5 rule-based",
                        reason=f"All 5 conditions met (ADX={adx_val:.1f})"
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
