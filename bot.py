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
    TRADE_LOG, EQUITY_LOG, SUPERTREND_PERIOD, SUPERTREND_MULT,
)
from notify import send_telegram
from signals import compute_daily_vol, compute_atr, compute_cvd, compute_obi, compute_vpin, compute_oi, compute_supertrend
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
    for filepath, default in [(TRADE_LOG, []), (EQUITY_LOG, [])]:
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

            # Daily candles for Supertrend (90 days → enough for ATR period 14)
            try:
                d_end = int(time.time() * 1000)
                d_start = d_end - (90 * 24 * 60 * 60 * 1000)
                daily_candles = mainnet_info.candles_snapshot(symbol, "1d", d_start, d_end)
                supertrend = compute_supertrend(daily_candles)
            except Exception as e:
                logger.warning(f"{symbol}: daily candle fetch failed — Supertrend neutral: {e}")
                supertrend = {"direction": "neutral", "value": 0.0, "changed": False}

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
        st_tag = f"ST={st['direction'].upper()}" + (" [FLIP]" if st['changed'] else "")
        logger.info(
            f"  {sym}: testnet ${data['tn_price']:.4f} | DailyVol={vol_str} | "
            f"OI=${data['oi']['oi_usd']:,.0f} | CVD={data['cvd']['cvd_trend']} | "
            f"OBI={data['obi']['obi']} | Funding={data['oi']['funding']}% | {st_tag}"
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

# ─── ATR Stop-Loss ────────────────────────────────────────────────────────────

def check_stops(positions, all_data, equity):
    closed_any = False
    for sym, p in list(positions.items()):
        data = all_data.get(sym)
        if not data:
            logger.warning(f"Stop check: no market data for {sym} — skipping")
            continue
        atr = data.get('atr')
        price = data.get('tn_price') or data.get('price')
        entry = p['entry']; side = p['side']
        if not atr or not price or not entry:
            continue

        stop_distance = STOP_ATR_MULT * atr
        if side == "LONG":
            stop_price = entry - stop_distance
            breached = price <= stop_price
        else:
            stop_price = entry + stop_distance
            breached = price >= stop_price

        if breached:
            logger.warning(
                f"🛑 STOP HIT {sym} {side}: entry ${entry:.4f}, now ${price:.4f}, "
                f"stop ${stop_price:.4f} ({STOP_ATR_MULT}×ATR={stop_distance:.4f})"
            )
            send_telegram(
                f"🛑 <b>STOP-LOSS {sym} {side}</b>\n"
                f"Entry: ${entry:.4f} → Now: ${price:.4f}\n"
                f"Stop: ${stop_price:.4f} ({STOP_ATR_MULT}×ATR) — market close"
            )
            if close_position_market(sym, all_data, equity, f"ATR stop ({STOP_ATR_MULT}×ATR) breached"):
                closed_any = True
                time.sleep(1)
    return closed_any

# ─── Claude Decision ──────────────────────────────────────────────────────────

def ask_claude(all_data, equity, positions):
    market_summary = ""
    for symbol, data in all_data.items():
        pos = positions.get(symbol)
        pos_str = f"OPEN {pos['side']} {abs(pos['size'])} @ ${pos['entry']:.4f}" if pos else "no position"
        vol_str = f"{data['daily_vol']*100:.1f}%" if data.get('daily_vol') else "n/a"
        st = data['supertrend']
        st_flip = " ← TREND JUST FLIPPED" if st['changed'] else ""
        market_summary += f"""
{symbol} (${data['price']:.4f}) [{pos_str}] | 24h Vol: ${data['oi']['day_volume']:,.0f} | Daily vol: {vol_str}:
  Supertrend (daily, ATR {SUPERTREND_PERIOD}, ×{SUPERTREND_MULT}): {st['direction'].upper()} @ {st['value']:.4f}{st_flip}
  Candles (last 10h):
{data['candle_summary']}
  Orderflow:
    CVD: {data['cvd']['cvd']} | Trend: {data['cvd']['cvd_trend']} | {data['cvd']['divergence']}
    OBI: {data['obi']['obi']} | {data['obi']['signal']} | Bids: {data['obi']['bid_vol']} / Asks: {data['obi']['ask_vol']}
    VPIN: {data['vpin']['vpin']} | {data['vpin']['signal']}
  Open Interest:
    OI: ${data['oi']['oi_usd']:,.0f} ({data['oi']['oi_tokens']} tokens)
    Volume change 4h: {data['oi']['vol_change_pct']:+.1f}% — {data['oi']['oi_signal']}
    Funding rate: {data['oi']['funding']}% — {data['oi']['funding_signal']}
"""

    pos_summary = "\n".join([
        f"  {sym}: {p['side']} {abs(p['size'])} @ ${p['entry']:.4f}"
        for sym, p in positions.items()
    ]) if positions else "  No open positions"

    prompt = f"""
You are a professional crypto trading assistant with deep orderflow analysis expertise.
You trade BOTH directions — long and short — on Hyperliquid testnet perps.
You monitor the most liquid perps by 24h dollar volume (stablecoins excluded).

Account equity: ${equity:.2f}
Leverage: {LEVERAGE}x
Position sizing is VOLATILITY-TARGETED automatically (you do not set size):
each position targets {VOL_TARGET_PCT*100:.0f}% of equity in daily risk, capped at ${MAX_NOTIONAL_USD} notional.
Orders are placed as POST-ONLY maker limits, so an entry may not fill if price moves away — that's expected.
Max concurrent positions: {MAX_POSITIONS}
Current open positions: {len(positions)}/{MAX_POSITIONS}
An automatic {STOP_ATR_MULT}×ATR stop-loss (market order) protects every position.

Open positions:
{pos_summary}

Market data + orderflow signals:
{market_summary}

Signal interpretation guide:
- Supertrend (daily) is the PRIMARY TREND BIAS — it is enforced as a HARD FILTER:
    BULLISH Supertrend → only OPEN_LONG is allowed (OPEN_SHORT will be blocked).
    BEARISH Supertrend → only OPEN_SHORT is allowed (OPEN_LONG will be blocked).
    CLOSE and FLIP are never blocked by Supertrend.
    A Supertrend FLIP on the latest daily candle is a strong directional signal.
- CVD rising = buyers in control (bullish). Falling = sellers (bearish).
- CVD divergence: price up + CVD down = bearish reversal risk. Price down + CVD up = bullish reversal.
- OBI >+0.3 = bullish (bid heavy), <-0.3 = bearish (ask heavy), near 0 = neutral.
- VPIN high (>0.4) = informed traders active, expect big directional move soon.
- OI rising + price rising = strong bullish conviction (new longs).
- OI rising + price falling = strong bearish conviction (new shorts).
- OI falling + price rising = weak rally, short covering — fade candidate.
- High positive funding = crowded longs, squeeze/reversal down risk → favors SHORT.
- High negative funding = crowded shorts, squeeze up risk → favors LONG.

LONG setup (OPEN_LONG): Supertrend BULLISH + CVD rising + OBI bullish + OI rising + funding neutral/negative
SHORT setup (OPEN_SHORT): Supertrend BEARISH + CVD falling + OBI bearish + OI rising + funding neutral/positive
Strong reversal where you already hold the wrong side → FLIP

Available actions:
- OPEN_LONG  — open a new long (only if no position in this symbol, positions < {MAX_POSITIONS})
- OPEN_SHORT — open a new short (only if no position in this symbol, positions < {MAX_POSITIONS})
- CLOSE      — close an existing position in this symbol (lock profit or cut loss)
- FLIP       — close current position AND open opposite direction (only on strong reversal signals)
- HOLD       — do nothing

Rules:
- Be willing to SHORT as readily as LONG — markets fall too.
- Only OPEN if current positions < {MAX_POSITIONS}
- CLOSE or FLIP only apply to symbols you already hold.
- FLIP is aggressive — only use it on clear 3+ signal reversals against your current position.
- Prefer higher 24h volume when signals are otherwise equal.

Pick the SINGLE best action across all symbols.

Respond in this exact format:
SYMBOL: which asset (or NONE)
ACTION: OPEN_LONG or OPEN_SHORT or CLOSE or FLIP or HOLD
SIZE: 0 (sizing is automatic — always put 0)
CVD_SIGNAL: bullish or bearish or neutral
OBI_SIGNAL: bullish or bearish or neutral
OI_SIGNAL: bullish or bearish or neutral
CONFLUENCE: score out of 4 (e.g. 3/4)
REASON: one sentence explaining the directional orderflow confluence
"""

    message = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ─── Decision Routing ─────────────────────────────────────────────────────────

def _parse_decision(text):
    fields = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields

def execute_decision(decision_text, all_data, equity, positions):
    f = _parse_decision(decision_text)
    symbol     = f.get("SYMBOL", "")
    action     = f.get("ACTION", "").upper()
    cvd_signal = f.get("CVD_SIGNAL", "")
    obi_signal = f.get("OBI_SIGNAL", "")
    oi_signal  = f.get("OI_SIGNAL", "")
    confluence = f.get("CONFLUENCE", "")
    reason     = f.get("REASON", "")

    logger.info(f"Symbol: {symbol} | Action: {action} | Confluence: {confluence}")
    logger.info(f"CVD: {cvd_signal} | OBI: {obi_signal} | OI: {oi_signal}")
    logger.info(f"Reason: {reason}")

    held = positions.get(symbol)

    if action == "HOLD" or symbol in ["NONE", ""]:
        logger.info("HOLD — no action taken")
        log_trade("HOLD", symbol, 0, 0, reason, equity, cvd_signal, obi_signal, oi_signal, confluence)
        return False

    if symbol not in all_data:
        logger.error(f"Symbol {symbol} not in market data (may have been skipped for wide oracle gap)")
        return False

    if action in ("OPEN_LONG", "OPEN_SHORT"):
        if held:
            logger.warning(f"{symbol} already has a {held['side']} position — ignoring {action}")
            return False
        if len(positions) >= MAX_POSITIONS:
            logger.warning(f"Max positions ({MAX_POSITIONS}) reached — skipping")
            log_trade("SKIPPED", symbol, 0, 0, reason, equity, cvd_signal, obi_signal, oi_signal, confluence)
            return False
        st_dir = all_data[symbol].get('supertrend', {}).get('direction', 'neutral')
        if st_dir == 'bearish' and action == "OPEN_LONG":
            logger.info(f"{symbol} OPEN_LONG blocked — daily Supertrend is BEARISH")
            return False
        if st_dir == 'bullish' and action == "OPEN_SHORT":
            logger.info(f"{symbol} OPEN_SHORT blocked — daily Supertrend is BULLISH")
            return False
        return open_position(symbol, action == "OPEN_LONG", all_data, equity,
                             cvd_signal, obi_signal, oi_signal, confluence, reason)

    if action == "CLOSE":
        if not held:
            logger.warning(f"{symbol} has no open position to CLOSE")
            return False
        return close_position_maker(symbol, all_data, equity, reason,
                                    cvd_signal, obi_signal, oi_signal, confluence)

    if action == "FLIP":
        if not held:
            logger.warning(f"{symbol} has no position to FLIP — treating as fresh open")
            is_buy = cvd_signal == "bullish"
            if len(positions) < MAX_POSITIONS:
                return open_position(symbol, is_buy, all_data, equity,
                                     cvd_signal, obi_signal, oi_signal, confluence, reason)
            return False
        current_side = held['side']
        logger.info(f"FLIP {symbol}: market-closing {current_side} then maker-opening opposite")
        if close_position_market(symbol, all_data, equity, f"FLIP close: {reason}",
                                 cvd_signal, obi_signal, oi_signal, confluence):
            time.sleep(SETTLE_SECONDS)
            new_is_buy = current_side == "SHORT"
            return open_position(symbol, new_is_buy, all_data, equity,
                                 cvd_signal, obi_signal, oi_signal, confluence, f"FLIP open: {reason}")
        return False

    logger.warning(f"Unknown action: {action} — no trade")
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
    logger.info(f"Entries: POST-ONLY maker (passive → 1 aggressive reprice, else skip)")
    logger.info(f"Discretionary closes: maker w/ market fallback | Stops & flips: market")
    logger.info(f"Signals: CVD + OBI + VPIN + OI + Funding (from MAINNET)")
    logger.info(f"Sizing: VOL-TARGETED {VOL_TARGET_PCT*100:.0f}% daily risk, cap ${MAX_NOTIONAL_USD} notional")
    logger.info(f"Stop-loss: {STOP_ATR_MULT}×ATR | Maker wait: {MAKER_WAIT_SECONDS}s | Gap skip: >{MAX_ORACLE_GAP_PCT}%")
    logger.info(f"Leverage: {LEVERAGE}x | Max positions: {MAX_POSITIONS}")
    logger.info(f"Data: MAINNET | Trading: TESTNET | Interval: {INTERVAL_MINUTES}min (clock-aligned)")

    init_files()
    get_testnet_coins()
    send_telegram(
        "🤖 <b>Trading Bot Started</b> (maker orders)\n"
        f"Top {TOP_N} liquid perps | {LEVERAGE}x | Max {MAX_POSITIONS}\n"
        f"Post-only entries | Vol-targeted {VOL_TARGET_PCT*100:.0f}% | {STOP_ATR_MULT}×ATR stop\n"
        "Data: MAINNET | Trading: TESTNET"
    )

    while True:
        try:
            logger.info("--- New cycle ---")

            positions = get_open_positions()
            symbols = get_top_symbols(TOP_N, extra_symbols=list(positions.keys()))

            all_data = get_all_market_data(symbols, open_position_syms=set(positions.keys()))
            equity = get_equity()

            logger.info(f"Equity: ${equity:.2f} | Open positions: {len(positions)}/{MAX_POSITIONS}")

            log_equity(equity, all_data, positions)

            if positions:
                stopped = check_stops(positions, all_data, equity)
                if stopped:
                    time.sleep(SETTLE_SECONDS)
                    positions = get_open_positions()
                    equity = get_equity()

            logger.info("Asking Claude to analyze orderflow (long & short)...")
            decision = ask_claude(all_data, equity, positions)
            logger.info(f"Claude responded:\n{decision}")

            traded = execute_decision(decision, all_data, equity, positions)

            if traded:
                logger.info(f"Trade executed — waiting {SETTLE_SECONDS}s for settlement before summary")
                time.sleep(SETTLE_SECONDS)

            positions = get_open_positions()
            equity = get_equity()
            print_summary(equity, positions, all_data)

            sleep_until_next_hour()

        except Exception as e:
            logger.error(f"Error: {e}")
            send_telegram(f"⚠️ <b>Bot Error</b>\n{str(e)[:300]}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
