from loguru import logger
from config import ATR_PERIOD, SUPERTREND_PERIOD, SUPERTREND_MULT, ADX_PERIOD, ADX_THRESHOLD


def compute_daily_vol(candles):
    try:
        closes = [float(c['c']) for c in candles]
        if len(closes) < 6:
            return None
        returns = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                returns.append((closes[i] - closes[i-1]) / closes[i-1])
        if len(returns) < 5:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        hourly_vol = variance ** 0.5
        return hourly_vol * (24 ** 0.5)
    except Exception:
        return None


def compute_atr(candles, period=ATR_PERIOD):
    try:
        if len(candles) < period + 1:
            period = max(2, len(candles) - 1)
        trs = []
        for i in range(1, len(candles)):
            h = float(candles[i]['h'])
            l = float(candles[i]['l'])
            prev_c = float(candles[i-1]['c'])
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
        if not trs:
            return None
        recent = trs[-period:]
        return sum(recent) / len(recent)
    except Exception:
        return None


def compute_cvd(candles):
    cvd = 0.0
    cvd_series = []
    for c in candles:
        o = float(c['o']); cl = float(c['c']); v = float(c['v'])
        h = float(c['h']); l = float(c['l'])
        rng = h - l + 1e-9
        delta = v * ((cl - o) / rng) if cl >= o else -v * ((o - cl) / rng)
        cvd += delta
        cvd_series.append(cvd)

    cvd_trend = "rising" if len(cvd_series) >= 5 and cvd_series[-1] > cvd_series[-5] else "falling"

    divergence = "unknown"
    if len(candles) >= 5:
        price_change = float(candles[-1]['c']) - float(candles[-5]['c'])
        cvd_change = cvd_series[-1] - cvd_series[-5] if len(cvd_series) >= 5 else 0
        if price_change > 0 and cvd_change < 0:
            divergence = "bearish divergence (price up, CVD down)"
        elif price_change < 0 and cvd_change > 0:
            divergence = "bullish divergence (price down, CVD up)"
        else:
            divergence = "no divergence"

    return {"cvd": round(cvd, 2), "cvd_trend": cvd_trend, "divergence": divergence}


def compute_obi(l2_data):
    try:
        bids = l2_data['levels'][0][:10]
        asks = l2_data['levels'][1][:10]
        def sz(level):
            return float(level['sz']) if isinstance(level, dict) else float(level[1])
        bid_vol = sum(sz(b) for b in bids)
        ask_vol = sum(sz(a) for a in asks)
        total = bid_vol + ask_vol
        if total == 0:
            return {"obi": 0.0, "signal": "neutral", "bid_vol": 0, "ask_vol": 0}
        obi = (bid_vol - ask_vol) / total
        signal = "bullish (bid heavy)" if obi > 0.3 else "bearish (ask heavy)" if obi < -0.3 else "neutral"
        return {"obi": round(obi, 3), "signal": signal, "bid_vol": round(bid_vol, 2), "ask_vol": round(ask_vol, 2)}
    except Exception:
        return {"obi": 0.0, "signal": "unknown", "bid_vol": 0, "ask_vol": 0}


def compute_vpin(candles, bucket_size=10):
    try:
        buy_vols, sell_vols = [], []
        for c in candles:
            h, l, cl, v = float(c['h']), float(c['l']), float(c['c']), float(c['v'])
            rng = h - l + 1e-9
            buy_vols.append(v * ((cl - l) / rng))
            sell_vols.append(v * ((h - cl) / rng))
        if len(buy_vols) < bucket_size:
            return {"vpin": 0.5, "signal": "insufficient data"}
        vpins = []
        for i in range(len(buy_vols) - bucket_size + 1):
            b = sum(buy_vols[i:i+bucket_size])
            s = sum(sell_vols[i:i+bucket_size])
            vpins.append(abs(b - s) / (b + s + 1e-9))
        vpin = round(vpins[-1], 3)
        signal = ("high (informed trading — expect volatility)" if vpin > 0.4
                  else "moderate" if vpin > 0.25 else "low (retail flow)")
        return {"vpin": vpin, "signal": signal}
    except Exception:
        return {"vpin": 0.5, "signal": "unknown"}


def compute_supertrend(candles, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULT):
    """
    Daily Supertrend bias filter.
    Returns direction 'bullish' (price above ST → long bias) or 'bearish' (short bias).
    'changed' is True when the trend flipped on the latest candle.
    """
    try:
        if len(candles) < period + 2:
            return {"direction": "neutral", "value": 0.0, "changed": False}

        n = len(candles)
        H = [float(c['h']) for c in candles]
        L = [float(c['l']) for c in candles]
        C = [float(c['c']) for c in candles]

        # True Range (index 0 unused; TR[i] = TR of candle[i])
        TR = [0.0] + [max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))
                      for i in range(1, n)]

        # ATR per bar — simple MA over `period` TR values
        ATR = [0.0] * n
        for i in range(1, n):
            start = max(1, i - period + 1)
            ATR[i] = sum(TR[start:i+1]) / (i - start + 1)

        # Bands and Supertrend (computed from index `period` onward)
        fu = [0.0] * n   # final upper band
        fl = [0.0] * n   # final lower band
        st = [0.0] * n   # supertrend value

        hl2 = (H[period] + L[period]) / 2
        fu[period] = hl2 + multiplier * ATR[period]
        fl[period] = hl2 - multiplier * ATR[period]
        st[period] = fl[period]  # start bullish

        for i in range(period + 1, n):
            hl2 = (H[i] + L[i]) / 2
            bu = hl2 + multiplier * ATR[i]
            bl = hl2 - multiplier * ATR[i]

            # Trailing bands: tighten upper, raise lower — using previous close
            fu[i] = bu if (bu < fu[i-1] or C[i-1] > fu[i-1]) else fu[i-1]
            fl[i] = bl if (bl > fl[i-1] or C[i-1] < fl[i-1]) else fl[i-1]

            # Flip logic: was bearish → flip bullish if close crosses above upper
            if st[i-1] == fu[i-1]:
                st[i] = fl[i] if C[i] > fu[i] else fu[i]
            else:
                st[i] = fu[i] if C[i] < fl[i] else fl[i]

        direction      = "bullish" if st[-1] == fl[-1] else "bearish"
        prev_direction = "bullish" if st[-2] == fl[-2] else "bearish"

        return {
            "direction": direction,
            "value": round(st[-1], 6),
            "changed": direction != prev_direction,
        }
    except Exception:
        return {"direction": "neutral", "value": 0.0, "changed": False}


def compute_adx(candles, period=ADX_PERIOD):
    """
    Wilder's ADX on the provided candles (use daily bars).
    Returns adx, +DI, -DI, and trending (ADX > ADX_THRESHOLD).
    Highest ADX among eligible symbols = strongest trend = priority pick.
    """
    try:
        if len(candles) < period * 2 + 1:
            return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}

        n = len(candles)
        H = [float(c['h']) for c in candles]
        L = [float(c['l']) for c in candles]
        C = [float(c['c']) for c in candles]

        tr_vals, pdm_vals, mdm_vals = [], [], []
        for i in range(1, n):
            tr = max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))
            up   = H[i] - H[i-1]
            down = L[i-1] - L[i]
            tr_vals.append(tr)
            pdm_vals.append(up   if up > down and up > 0   else 0.0)
            mdm_vals.append(down if down > up and down > 0 else 0.0)

        def wilder(values, p):
            """Wilder smoothing (RMA): SMA seed, then (prev*(p-1) + cur) / p."""
            if len(values) < p:
                return []
            result = [sum(values[:p]) / p]
            for v in values[p:]:
                result.append((result[-1] * (p - 1) + v) / p)
            return result

        atr_s = wilder(tr_vals,  period)
        pdm_s = wilder(pdm_vals, period)
        mdm_s = wilder(mdm_vals, period)

        dx_vals = []
        for i in range(len(atr_s)):
            if atr_s[i] == 0:
                dx_vals.append((0.0, 0.0, 0.0))
                continue
            pdi = 100.0 * pdm_s[i] / atr_s[i]
            mdi = 100.0 * mdm_s[i] / atr_s[i]
            denom = pdi + mdi
            dx  = 100.0 * abs(pdi - mdi) / denom if denom else 0.0
            dx_vals.append((dx, pdi, mdi))

        adx_s = wilder([d[0] for d in dx_vals], period)
        if not adx_s:
            return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}

        adx   = round(adx_s[-1], 2)
        pdi   = round(dx_vals[-1][1], 2)
        mdi   = round(dx_vals[-1][2], 2)

        return {"adx": adx, "plus_di": pdi, "minus_di": mdi, "trending": adx > ADX_THRESHOLD}
    except Exception:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}


def compute_oi(symbol, candles, price, info):
    try:
        asset_contexts = info.meta_and_asset_ctxs()
        meta = asset_contexts[0]['universe']
        ctxs = asset_contexts[1]
        symbol_idx = next((i for i, a in enumerate(meta) if a['name'] == symbol), None)
        if symbol_idx is None or symbol_idx >= len(ctxs):
            return {"oi_usd": 0, "oi_tokens": 0, "vol_change_pct": 0,
                    "oi_vol_ratio": 0, "oi_signal": "unavailable",
                    "funding": 0, "funding_signal": "unavailable", "day_volume": 0}
        ctx = ctxs[symbol_idx]
        oi_tokens = float(ctx.get('openInterest', 0))
        funding = float(ctx.get('funding', 0)) * 100
        day_volume = float(ctx.get('dayNtlVlm', 0))
        oi_usd = round(oi_tokens * price, 2)

        if len(candles) >= 8:
            recent_vol = sum(float(c['v']) for c in candles[-4:])
            prev_vol = sum(float(c['v']) for c in candles[-8:-4])
            vol_change_pct = ((recent_vol - prev_vol) / (prev_vol + 1e-9)) * 100
        else:
            vol_change_pct = 0

        last_vol = float(candles[-1]['v']) if candles else 1
        oi_vol_ratio = round(oi_tokens / (last_vol + 1e-9), 2)

        if vol_change_pct > 15:
            oi_signal = "rising fast — strong conviction, new money entering"
        elif vol_change_pct > 5:
            oi_signal = "rising — trend has momentum"
        elif vol_change_pct < -15:
            oi_signal = "falling fast — positions unwinding, trend weakening"
        elif vol_change_pct < -5:
            oi_signal = "falling — losing momentum"
        else:
            oi_signal = "stable — no strong conviction either way"

        if funding > 0.05:
            funding_signal = "high positive (longs paying — crowded long, potential squeeze)"
        elif funding > 0.01:
            funding_signal = "positive (mild long bias)"
        elif funding < -0.05:
            funding_signal = "high negative (shorts paying — crowded short, potential squeeze)"
        elif funding < -0.01:
            funding_signal = "negative (mild short bias)"
        else:
            funding_signal = "neutral"

        return {
            "oi_usd": oi_usd, "oi_tokens": round(oi_tokens, 2),
            "vol_change_pct": round(vol_change_pct, 2), "oi_vol_ratio": oi_vol_ratio,
            "oi_signal": oi_signal, "funding": round(funding, 4),
            "funding_signal": funding_signal, "day_volume": day_volume
        }
    except Exception as e:
        logger.error(f"OI fetch error for {symbol}: {e}")
        return {"oi_usd": 0, "oi_tokens": 0, "vol_change_pct": 0,
                "oi_vol_ratio": 0, "oi_signal": "unavailable",
                "funding": 0, "funding_signal": "unavailable", "day_volume": 0}
