#!/usr/bin/env python3
"""
Bar-by-bar backtest for the Hyperliquid LOC strategy.
Runs on 1h candles; daily/4h signals computed from closed bars only (no lookahead).
"""

import sys, time, math, json, bisect
from datetime import datetime

sys.path.insert(0, '/Users/marekjankowski/trading-bot')

from exchange import mainnet_info
from signals import (
    compute_supertrend, compute_adx, compute_atr,
    compute_rsi, compute_ema, compute_ema_slope, compute_volume_ratio,
)
from config import (
    STABLECOINS, TOP_N,
    ADX_THRESHOLD, ADX_DECAY_EXIT, STOP_ATR_MULT,
    SUPERTREND_PERIOD, SUPERTREND_MULT,
    RSI_LONG_THRESHOLD, RSI_SHORT_THRESHOLD, RSI_LOOKBACK,
    EMA_BAND_PCT, EMA_PERIOD, VOLUME_CONFIRM_RATIO,
    RISK_PER_TRADE_PCT, MAX_PORTFOLIO_RISK_PCT, MAX_POSITIONS,
    MAX_NOTIONAL_PCT, MIN_NOTIONAL_USD, MIN_NOTIONAL_PCT,
    FUNDING_LONG_MAX, FUNDING_SHORT_MIN,
)

INITIAL_EQUITY = 10000.0
LOOKBACK_DAYS  = 5 * 365 + 90  # 5 years of daily data + warmup buffer
WARMUP_DAYS    = 90            # skip first N days so indicators are seeded
LEVERAGE       = 2             # cosmetic — used only for log display

# Experimental: force-close a position if funding turns this adverse while
# held. Disabled by default (thresholds outside any real funding rate).
FUNDING_EXIT_LONG_MAX  = 1.0
FUNDING_EXIT_SHORT_MIN = -1.0


# ── Utilities ─────────────────────────────────────────────────────────────────

def ts(ms):
    return datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d %H:%M')

def closed_before(candle_list, cutoff_ms):
    """Candles whose open timestamp < cutoff_ms (i.e., they closed before cutoff)."""
    return [c for c in candle_list if int(c['t']) < cutoff_ms]


# ── Data Fetching ─────────────────────────────────────────────────────────────

def get_top_symbols(n=TOP_N):
    print("Ranking symbols by 30-day avg daily volume...")
    end   = int(time.time() * 1000)
    start = end - 30 * 24 * 60 * 60 * 1000

    meta  = mainnet_info.meta_and_asset_ctxs()
    universe, ctxs = meta[0]['universe'], meta[1]
    candidates = [
        (a['name'], float(ctxs[i].get('dayNtlVlm', 0)))
        for i, a in enumerate(universe)
        if a['name'].upper() not in STABLECOINS and i < len(ctxs)
    ]

    candidates.sort(key=lambda x: x[1], reverse=True)
    results = []
    for sym, _ in candidates[:30]:
        try:
            c = mainnet_info.candles_snapshot(sym, "1d", start, end)
            avg = sum(float(x['v']) * float(x['c']) for x in c) / len(c) if c else 0
            results.append((sym, avg))
        except Exception:
            results.append((sym, 0.0))
    results.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in results[:n]]
    print(f"  Top {n}: {top}")
    return top


def fetch_chunked(sym, interval, start_ms, end_ms, chunk_hours=4800, retries=4, pause=3.0):
    """
    Fetch candles in chunks to work around the ~5000-bar API cap.
    chunk_hours: size of each request window (4800h = 200 days for 1h bars).
    Returns deduplicated candles sorted by timestamp ascending.
    """
    chunk_ms = chunk_hours * 60 * 60 * 1000
    all_candles = []
    window_start = start_ms
    while window_start < end_ms:
        window_end = min(window_start + chunk_ms, end_ms)
        for attempt in range(retries):
            try:
                chunk = mainnet_info.candles_snapshot(sym, interval, window_start, window_end)
                all_candles.extend(chunk)
                break
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(pause * (attempt + 1))
                else:
                    print(f"\n    [{sym} {interval}] chunk {window_start} failed: {e}")
        window_start = window_end
        time.sleep(0.3)  # be gentle with the API between chunks
    # Deduplicate and sort
    seen = set()
    result = []
    for c in all_candles:
        t = int(c['t'])
        if t not in seen:
            seen.add(t)
            result.append(c)
    result.sort(key=lambda c: int(c['t']))
    return result


def fetch_candles(symbols):
    # Entry signals now run on 4h bars (~2.3 years available vs ~6 months for 1h).
    # Daily bars provide ST / ADX / ATR (strategic filter, up to 5 years available).
    print("Fetching candles (daily / 4h) with chunked requests...")
    end   = int(time.time() * 1000)
    start = end - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    daily, h4 = {}, {}
    for sym in symbols:
        print(f"  {sym} ...", end='', flush=True)
        try:
            daily[sym] = mainnet_info.candles_snapshot(sym, "1d", start, end)
            h4[sym]    = fetch_chunked(sym, "4h", start, end, chunk_hours=4800*4)
            print(f" {len(daily[sym])}d / {len(h4[sym])} 4h")
        except Exception as e:
            print(f" FAILED: {e}")
            daily[sym] = h4[sym] = []
    return daily, h4


def fetch_funding_chunked(sym, start_ms, end_ms, chunk_hours=480, retries=4, pause=3.0):
    """funding_history caps at 500 rows/request — chunk in ~20-day windows."""
    chunk_ms = chunk_hours * 60 * 60 * 1000
    all_events = []
    window_start = start_ms
    while window_start < end_ms:
        window_end = min(window_start + chunk_ms, end_ms)
        for attempt in range(retries):
            try:
                chunk = mainnet_info.funding_history(sym, window_start, window_end)
                all_events.extend(chunk)
                break
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(pause * (attempt + 1))
                else:
                    print(f"\n    [{sym} funding] chunk {window_start} failed: {e}")
        window_start = window_end
        time.sleep(0.2)
    seen = set()
    result = []
    for e in all_events:
        t = int(e['time'])
        if t not in seen:
            seen.add(t)
            result.append(e)
    result.sort(key=lambda e: int(e['time']))
    return result


def fetch_funding(symbols, start_ms, end_ms):
    print("Fetching funding rate history...")
    funding = {}
    for sym in symbols:
        print(f"  {sym} ...", end='', flush=True)
        try:
            events = fetch_funding_chunked(sym, start_ms, end_ms)
            funding[sym] = events
            print(f" {len(events)} hourly events")
        except Exception as e:
            print(f" FAILED: {e}")
            funding[sym] = []
    return funding


def _funding_arrays(funding, symbols):
    times, rates = {}, {}
    for sym in symbols:
        events = funding.get(sym, [])
        times[sym]  = [int(e['time']) for e in events]
        rates[sym]  = [float(e['fundingRate']) for e in events]
    return times, rates


def _funding_window_sum(times, rates, sym, start_t, end_t):
    t = times.get(sym)
    if not t:
        return 0.0
    lo = bisect.bisect_left(t, start_t)
    hi = bisect.bisect_left(t, end_t)
    return sum(rates[sym][lo:hi])


def _latest_funding_pct(times, rates, sym, bar_t):
    """Most recent realized funding rate strictly before bar_t, as a percentage."""
    t = times.get(sym)
    if not t:
        return 0.0
    idx = bisect.bisect_left(t, bar_t) - 1
    if idx < 0:
        return 0.0
    return rates[sym][idx] * 100


# ── Signal Computation ────────────────────────────────────────────────────────

def get_signals(sym, bar_t, daily, h4):
    """
    Return signal dict for sym at bar_t (ms), using only closed bars.
    Daily bars → ST / ADX / ATR / C0-slope.
    4h bars    → price, EMA, RSI, volume (C2–C5 entry conditions).
    """
    d_bars  = closed_before(daily.get(sym, []), bar_t)
    h4_bars = closed_before(h4.get(sym, []),    bar_t)

    if len(d_bars) < 32 or len(h4_bars) < EMA_PERIOD + RSI_LOOKBACK + 2:
        return None

    price = float(h4_bars[-1]['c'])

    # Daily — strategic signals.
    # Window matches the live bot's 90-day daily lookback (bot.py get_symbol_data):
    # Supertrend/ADX/ATR are recursively seeded, so the window length must match or
    # the backtest produces different flips/values than live would have on the same day.
    d_win    = d_bars[-90:]
    st       = compute_supertrend(d_win, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULT)
    adx_data = compute_adx(d_win)
    atr      = compute_atr(d_win)
    # C0: daily EMA slope — single seeded series, matches the live bot
    daily_slope = compute_ema_slope(d_win, period=EMA_PERIOD, lag=3)

    # 4h — entry conditions (C2–C5).
    # Window matches the live bot's 30-day 4h lookback (~180 bars) so EMA/RSI/volume
    # are seeded identically to live.
    h4_win    = h4_bars[-180:]
    rsi       = compute_rsi(h4_win, period=14, lookback=RSI_LOOKBACK)
    ema       = compute_ema(h4_win, period=EMA_PERIOD)
    vol_ratio = compute_volume_ratio(h4_win, lookback=10)

    return {
        "price": price, "atr": atr,
        "st": st, "adx": adx_data,
        "slope": daily_slope,
        "rsi": rsi, "ema": ema,
        "vol_ratio": vol_ratio,
        "daily_bar_t": int(d_win[-1]['t']),   # for daily-bar-gated ADX-decay confirmation
    }


# ── Entry Check ───────────────────────────────────────────────────────────────

def check_entry(sig, is_long):
    """Returns (passed, loc_price_or_None, reason_str).
    Entry conditions now evaluated on 4h bars (backtest mode).
    C0 uses daily EMA slope (one timeframe up from 4h, same role as 4h slope in the live bot).
    """
    if not sig or not sig["ema"] or not sig["atr"]:
        return False, None, "missing data"

    price     = sig["price"]
    ema       = sig["ema"]
    rsi       = sig["rsi"]
    slope     = sig["slope"]
    vol_ratio = sig["vol_ratio"]
    adx_data  = sig["adx"]
    adx_val   = adx_data.get("adx", 0.0)
    plus_di   = adx_data.get("plus_di", 0.0)
    minus_di  = adx_data.get("minus_di", 0.0)
    st_dir    = sig["st"].get("direction", "neutral")

    # C1a: Supertrend alignment
    if is_long  and st_dir != "bullish":  return False, None, "ST bearish"
    if not is_long and st_dir != "bearish": return False, None, "ST bullish"

    # C1b: ADX gate + DI confirmation
    if adx_val < ADX_THRESHOLD:            return False, None, f"ADX {adx_val:.1f}<{ADX_THRESHOLD}"
    if is_long  and plus_di  <= minus_di:  return False, None, "+DI≤-DI"
    if not is_long and minus_di <= plus_di: return False, None, "-DI≤+DI"

    # C0: daily EMA slope in trade direction
    c0 = slope == "up" if is_long else slope == "down"
    if not c0: return False, None, f"C0 daily slope={slope}"

    # C2: 4h RSI dip/spike within lookback
    if is_long:
        c2 = rsi["min_recent"] < RSI_LONG_THRESHOLD
    else:
        c2 = rsi["max_recent"] > RSI_SHORT_THRESHOLD
    if not c2: return False, None, "C2 no dip/spike"

    # C3: 4h price within EMA band (maker-side)
    if ema <= 0: return False, None, "no EMA"
    pct = (price - ema) / ema
    near = (0 <= pct <= EMA_BAND_PCT) if is_long else (-EMA_BAND_PCT <= pct <= 0)
    if not near: return False, None, f"C3 pct={pct*100:+.2f}%"

    # C4: 4h RSI hook back in trade direction
    c4 = rsi["rsi"] > rsi["prev_rsi"] if is_long else rsi["rsi"] < rsi["prev_rsi"]
    if not c4: return False, None, "C4 no hook"

    # C5: 4h volume confirmation
    if vol_ratio < VOLUME_CONFIRM_RATIO: return False, None, f"C5 vol={vol_ratio:.2f}"

    return True, ema, f"ADX={adx_val:.1f} ST={st_dir} pct={pct*100:+.2f}%"


def compute_notional(price, atr, equity):
    if not atr or not price or atr <= 0:
        return None, None
    dollar_risk   = equity * RISK_PER_TRADE_PCT
    if dollar_risk < 1.0:
        return None, None
    stop_distance = STOP_ATR_MULT * atr
    notional      = (dollar_risk / stop_distance) * price
    notional      = min(notional, equity * MAX_NOTIONAL_PCT)
    floor         = max(equity * MIN_NOTIONAL_PCT, MIN_NOTIONAL_USD)
    if notional < floor:
        return None, None
    return notional, atr


# ── Main Backtest ─────────────────────────────────────────────────────────────

def run(symbols, daily, h4, funding=None):
    # Simulation runs on the 4h timeline — ~2.3 years of history available.
    funding_times, funding_rates = _funding_arrays(funding or {}, symbols)

    ref = symbols[0]
    all_4h_ts = sorted(int(c['t']) for c in h4.get(ref, []))
    if not all_4h_ts:
        print("No 4h bars for reference symbol — aborting")
        return

    warmup_ms     = WARMUP_DAYS * 24 * 60 * 60 * 1000
    backtest_bars = [t for t in all_4h_ts if t >= all_4h_ts[0] + warmup_ms]

    equity       = INITIAL_EQUITY
    positions    = {}   # sym → {side, entry_px, size, entry_atr, peak, adx_decay_count}
    pending_loc  = {}   # sym → {is_long, limit_px, notional, atr, placed_at}
    trades       = []
    equity_curve = [{"t": backtest_bars[0], "equity": equity}]

    print(f"\n{'='*60}")
    print(f"Backtest: {ts(backtest_bars[0])} → {ts(backtest_bars[-1])}")
    print(f"Symbols : {symbols}")
    print(f"Equity  : ${equity:.2f} | Bars: {len(backtest_bars)}")
    print(f"{'='*60}\n")

    for bar_idx, bar_t in enumerate(backtest_bars):
        if bar_idx % (6 * 30) == 0:  # progress every ~30 days (6 bars/day × 30)
            print(f"  {ts(bar_t)} | equity=${equity:.2f} | positions={list(positions.keys())}")

        # ── Resolve pending LOC orders ────────────────────────────────────
        for sym in list(pending_loc.keys()):
            loc = pending_loc.pop(sym)
            if sym in positions:
                continue  # already have a position

            # Check whether this 4h bar's range touched the limit price
            h4_here = [c for c in h4.get(sym, []) if int(c['t']) == bar_t]
            if not h4_here:
                continue
            bar = h4_here[0]
            lo, hi = float(bar['l']), float(bar['h'])
            touched = (loc['is_long'] and lo <= loc['limit_px']) or \
                      (not loc['is_long'] and hi >= loc['limit_px'])
            if touched and loc['notional'] and loc['atr']:
                fill_px = loc['limit_px']
                size    = loc['notional'] / fill_px
                side    = "LONG" if loc['is_long'] else "SHORT"
                positions[sym] = {
                    "side": side, "entry_px": fill_px, "size": size,
                    "entry_atr": loc['atr'], "peak": fill_px,
                    "adx_decay_count": 0, "decay_last_bar": None, "entry_t": bar_t,
                    "last_funding_t": bar_t,
                }
                trades.append({
                    "t": bar_t, "sym": sym, "action": f"OPEN_{side}",
                    "px": fill_px, "size": size, "notional": loc['notional'],
                    "equity": equity,
                })

        # ── Gather signals ─────────────────────────────────────────────────
        sigs = {sym: get_signals(sym, bar_t, daily, h4) for sym in symbols}

        # ── Check exits ────────────────────────────────────────────────────
        for sym, pos in list(positions.items()):
            sig = sigs.get(sym)
            if not sig:
                continue

            price     = sig["price"]
            atr       = sig["atr"] or pos["entry_atr"]
            side      = pos["side"]
            entry     = pos["entry_px"]
            peak      = pos["peak"]
            entry_atr = pos["entry_atr"] or atr
            adx_val   = sig["adx"].get("adx", 0.0)
            st_dir    = sig["st"].get("direction", "neutral")
            bar_day_t = sig.get("daily_bar_t")

            if funding is not None:
                rate_sum = _funding_window_sum(funding_times, funding_rates, sym, pos["last_funding_t"], bar_t)
                if rate_sum:
                    side_sign = 1 if side == "LONG" else -1
                    equity -= side_sign * rate_sum * abs(pos["size"]) * price
                pos["last_funding_t"] = bar_t

            def close_pos(reason):
                nonlocal equity
                pnl = (price - entry) * pos["size"] if side == "LONG" else (entry - price) * pos["size"]
                equity += pnl
                trades.append({
                    "t": bar_t, "sym": sym, "action": f"CLOSE_{side}",
                    "px": price, "size": pos["size"],
                    "entry_px": entry, "pnl": pnl,
                    "reason": reason, "equity": equity,
                    "bars_held": (bar_t - pos["entry_t"]) // (60 * 60 * 1000),
                })
                del positions[sym]

            # 0. Funding exit (adverse funding while held)
            if funding is not None:
                fr_pct = _latest_funding_pct(funding_times, funding_rates, sym, bar_t)
                if side == "LONG" and fr_pct > FUNDING_EXIT_LONG_MAX:
                    close_pos(f"funding exit {fr_pct:.3f}>{FUNDING_EXIT_LONG_MAX}")
                    continue
                if side == "SHORT" and fr_pct < FUNDING_EXIT_SHORT_MIN:
                    close_pos(f"funding exit {fr_pct:.3f}<{FUNDING_EXIT_SHORT_MIN}")
                    continue

            # 1. Supertrend exit
            st_against = (side == "LONG" and st_dir == "bearish") or \
                         (side == "SHORT" and st_dir == "bullish")
            if st_against:
                close_pos(f"ST flip {st_dir}")
                continue

            # 2. ADX decay — 2 consecutive DAILY bars below threshold. ADX is daily but the
            #    loop steps every 4h, so the counter must advance only on a NEW daily bar
            #    (daily_bar_t), matching the live bot — otherwise it confirms the same daily
            #    reading twice within one day.
            if adx_val == 0.0:
                pos["adx_decay_count"] = 0  # sentinel — reset
                pos["decay_last_bar"] = None
            elif adx_val < ADX_DECAY_EXIT:
                if bar_day_t is not None and bar_day_t == pos.get("decay_last_bar"):
                    pass  # same daily ADX reading already counted — hold
                else:
                    pos["decay_last_bar"] = bar_day_t
                    if pos["adx_decay_count"] >= 1:
                        close_pos(f"ADX decay {adx_val:.1f}<{ADX_DECAY_EXIT}")
                        continue
                    else:
                        pos["adx_decay_count"] = 1
                        # fall through to chandelier (daily bar 1 — don't exit yet)
            else:
                pos["adx_decay_count"] = 0
                pos["decay_last_bar"] = None

            # 3. Chandelier stop
            stop_dist   = STOP_ATR_MULT * atr
            be_dist     = STOP_ATR_MULT * entry_atr

            if side == "LONG":
                new_peak = max(peak, price)
                if price - entry >= entry_atr:
                    new_peak = max(new_peak, entry + be_dist)
                chandelier = new_peak - stop_dist
                be_active  = new_peak >= entry + be_dist
                if be_active:
                    chandelier = max(chandelier, entry)
                pos["peak"] = new_peak
                if price <= chandelier:
                    close_pos(f"chandelier {'BE' if be_active else 'trail'} stop @{chandelier:.4f}")
                    continue
            else:
                new_peak = min(peak, price)
                if entry - price >= entry_atr:
                    new_peak = min(new_peak, entry - be_dist)
                chandelier = new_peak + stop_dist
                be_active  = new_peak <= entry - be_dist
                if be_active:
                    chandelier = min(chandelier, entry)
                pos["peak"] = new_peak
                if price >= chandelier:
                    close_pos(f"chandelier {'BE' if be_active else 'trail'} stop @{chandelier:.4f}")
                    continue

        # ── Check entries ──────────────────────────────────────────────────
        occupied = set(positions.keys()) | set(pending_loc.keys())
        if len(occupied) < MAX_POSITIONS:
            # Heat check
            open_risk = sum(
                abs(p['size']) * (p['entry_atr'] or 0) * STOP_ATR_MULT
                for p in positions.values()
            )
            open_risk += (len(pending_loc) + 1) * RISK_PER_TRADE_PCT * equity
            heat = open_risk / equity if equity > 0 else 0

            if heat < MAX_PORTFOLIO_RISK_PCT:
                candidates = []
                for sym in symbols:
                    if sym in occupied:
                        continue
                    sig = sigs.get(sym)
                    if not sig:
                        continue
                    # Try long first (ST determines direction)
                    st_dir = sig["st"].get("direction", "neutral")
                    if st_dir == "neutral":
                        continue
                    is_long = (st_dir == "bullish")
                    if funding is not None:
                        fr_pct = _latest_funding_pct(funding_times, funding_rates, sym, bar_t)
                        if is_long and fr_pct > FUNDING_LONG_MAX:
                            continue
                        if not is_long and fr_pct < FUNDING_SHORT_MIN:
                            continue
                    passed, loc_px, reason = check_entry(sig, is_long)
                    if passed and loc_px:
                        adx_val = sig["adx"].get("adx", 0.0)
                        candidates.append((sym, is_long, adx_val, loc_px, sig["atr"], reason))

                if candidates:
                    candidates.sort(key=lambda x: x[2], reverse=True)
                    sym, is_long, adx_val, loc_px, atr, reason = candidates[0]
                    notional, atr_used = compute_notional(loc_px, atr, equity)
                    if notional and atr_used:
                        pending_loc[sym] = {
                            "is_long": is_long, "limit_px": loc_px,
                            "notional": notional, "atr": atr_used,
                        }

        # Equity snapshot (daily — 6 bars per day on 4h)
        if bar_idx % 6 == 0:
            # Mark-to-market open positions
            mtm = equity
            for sym, pos in positions.items():
                sig = sigs.get(sym)
                if sig:
                    px = sig["price"]
                    if pos["side"] == "LONG":
                        mtm += (px - pos["entry_px"]) * pos["size"]
                    else:
                        mtm += (pos["entry_px"] - px) * pos["size"]
            equity_curve.append({"t": bar_t, "equity": mtm})

    # Close any remaining positions at last price
    for sym, pos in list(positions.items()):
        sig = sigs.get(sym) if 'sigs' in dir() else None
        if sig:
            price = sig["price"]
        else:
            last = h4.get(sym, [])
            price = float(last[-1]['c']) if last else pos["entry_px"]
        if funding is not None:
            rate_sum = _funding_window_sum(funding_times, funding_rates, sym, pos["last_funding_t"], backtest_bars[-1])
            if rate_sum:
                side_sign = 1 if pos["side"] == "LONG" else -1
                equity -= side_sign * rate_sum * abs(pos["size"]) * price
        pnl = (price - pos["entry_px"]) * pos["size"] if pos["side"] == "LONG" else (pos["entry_px"] - price) * pos["size"]
        equity += pnl
        trades.append({
            "t": backtest_bars[-1], "sym": sym, "action": f"CLOSE_{pos['side']}",
            "px": price, "size": pos["size"], "entry_px": pos["entry_px"],
            "pnl": pnl, "reason": "end of backtest", "equity": equity,
            "bars_held": (backtest_bars[-1] - pos["entry_t"]) // (60 * 60 * 1000),
        })

    return trades, equity_curve, equity


# ── Results ───────────────────────────────────────────────────────────────────

def print_results(trades, equity_curve, final_equity):
    opens  = [t for t in trades if t['action'].startswith('OPEN')]
    closes = [t for t in trades if t['action'].startswith('CLOSE')]

    if not closes:
        print("\nNo completed trades.")
        return

    pnls       = [t['pnl'] for t in closes]
    winners    = [p for p in pnls if p > 0]
    losers     = [p for p in pnls if p <= 0]
    win_rate   = len(winners) / len(closes) * 100 if closes else 0
    avg_win    = sum(winners) / len(winners) if winners else 0
    avg_loss   = sum(losers)  / len(losers)  if losers  else 0
    profit_factor = abs(sum(winners) / sum(losers)) if sum(losers) != 0 else float('inf')

    # Max drawdown on equity curve
    eq_vals = [e['equity'] for e in equity_curve]
    peak = eq_vals[0]
    max_dd = 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    total_return = (final_equity - INITIAL_EQUITY) / INITIAL_EQUITY * 100

    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"Initial equity  : ${INITIAL_EQUITY:.2f}")
    print(f"Final equity    : ${final_equity:.2f}")
    print(f"Total return    : {total_return:+.1f}%")
    print(f"Max drawdown    : {max_dd:.1f}%")
    print(f"{'─'*60}")
    print(f"Total trades    : {len(closes)}")
    print(f"Win rate        : {win_rate:.1f}%  ({len(winners)}W / {len(losers)}L)")
    print(f"Avg win         : ${avg_win:.2f}")
    print(f"Avg loss        : ${avg_loss:.2f}")
    print(f"Profit factor   : {profit_factor:.2f}")
    print(f"{'─'*60}")

    # Per-symbol breakdown
    sym_stats = {}
    for t in closes:
        sym = t['sym']
        if sym not in sym_stats:
            sym_stats[sym] = {"trades": 0, "pnl": 0.0, "wins": 0}
        sym_stats[sym]["trades"] += 1
        sym_stats[sym]["pnl"]    += t['pnl']
        if t['pnl'] > 0:
            sym_stats[sym]["wins"] += 1

    print(f"{'Symbol':<10} {'Trades':>6} {'Win%':>6} {'P&L':>10}")
    print(f"{'─'*36}")
    for sym, s in sorted(sym_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = s['wins'] / s['trades'] * 100 if s['trades'] else 0
        print(f"  {sym:<8} {s['trades']:>6} {wr:>5.0f}%  ${s['pnl']:>8.2f}")

    # Exit reason breakdown
    reasons = {}
    for t in closes:
        r = t.get('reason', 'unknown').split(' ')[0]
        reasons[r] = reasons.get(r, 0) + 1
    print(f"\nExit breakdown:")
    for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {r:<20} {cnt:>4} ({cnt/len(closes)*100:.0f}%)")

    # Average holding time
    bars_held = [t.get('bars_held', 0) for t in closes if 'bars_held' in t]
    if bars_held:
        avg_hold_h = sum(bars_held) / len(bars_held)
        print(f"\nAvg holding time: {avg_hold_h:.0f}h ({avg_hold_h/24:.1f}d)")

    print(f"{'='*60}")

    # Save detailed results
    out = {
        "summary": {
            "initial_equity": INITIAL_EQUITY, "final_equity": final_equity,
            "total_return_pct": total_return, "max_drawdown_pct": max_dd,
            "total_trades": len(closes), "win_rate_pct": win_rate,
            "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": profit_factor,
        },
        "trades": trades,
        "equity_curve": equity_curve,
    }
    with open("data/backtest_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nFull results saved to data/backtest_results.json")


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    t0 = time.time()

    args = sys.argv[1:]
    no_funding = "--no-funding" in args
    args = [a for a in args if a not in ("--no-funding",)]
    n = int(args[0]) if args else TOP_N

    symbols = get_top_symbols(n)
    daily, h4 = fetch_candles(symbols)

    funding = None
    if not no_funding:
        end   = int(time.time() * 1000)
        start = end - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
        funding = fetch_funding(symbols, start, end)

    trades, equity_curve, final_equity = run(symbols, daily, h4, funding=funding)
    print_results(trades, equity_curve, final_equity)

    print(f"\nCompleted in {time.time() - t0:.0f}s")
