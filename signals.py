from loguru import logger
from config import (
    ATR_PERIOD, SUPERTREND_PERIOD, SUPERTREND_MULT, ADX_PERIOD, ADX_THRESHOLD,
    RSI_PERIOD, RSI_LOOKBACK, EMA_PERIOD,
)


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


def _wilder(values, p):
    """Wilder smoothing (RMA): SMA seed then (prev*(p-1) + cur) / p."""
    if len(values) < p:
        return []
    result = [sum(values[:p]) / p]
    for v in values[p:]:
        result.append((result[-1] * (p - 1) + v) / p)
    return result


def compute_atr(candles, period=ATR_PERIOD):
    try:
        trs = []
        for i in range(1, len(candles)):
            h = float(candles[i]['h'])
            l = float(candles[i]['l'])
            prev_c = float(candles[i-1]['c'])
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
        if not trs:
            return None
        atr_w = _wilder(trs, period)
        return atr_w[-1] if atr_w else None
    except Exception:
        return None


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

        # True Range and Wilder RMA ATR (matches TradingView canonical Supertrend)
        tr_vals = [max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))
                   for i in range(1, n)]
        atr_w = _wilder(tr_vals, period)   # atr_w[j] = ATR for candle index (period + j)
        if not atr_w:
            return {"direction": "neutral", "value": 0.0, "changed": False}

        # Bands and Supertrend (computed from index `period` onward)
        fu = [0.0] * n   # final upper band
        fl = [0.0] * n   # final lower band
        st = [0.0] * n   # supertrend value

        hl2 = (H[period] + L[period]) / 2
        fu[period] = hl2 + multiplier * atr_w[0]
        fl[period] = hl2 - multiplier * atr_w[0]
        st[period] = fl[period]  # start bullish

        for i in range(period + 1, n):
            j = i - period
            if j >= len(atr_w):
                break
            hl2 = (H[i] + L[i]) / 2
            bu = hl2 + multiplier * atr_w[j]
            bl = hl2 - multiplier * atr_w[j]

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

        atr_s = _wilder(tr_vals,  period)
        pdm_s = _wilder(pdm_vals, period)
        mdm_s = _wilder(mdm_vals, period)

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

        adx_s = _wilder([d[0] for d in dx_vals], period)
        if not adx_s:
            return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}

        adx   = round(adx_s[-1], 2)
        pdi   = round(dx_vals[-1][1], 2)
        mdi   = round(dx_vals[-1][2], 2)

        return {"adx": adx, "plus_di": pdi, "minus_di": mdi, "trending": adx > ADX_THRESHOLD}
    except Exception:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}


def compute_rsi(candles, period=RSI_PERIOD, lookback=RSI_LOOKBACK):
    """
    Wilder's RSI on 60-min close prices.
    Returns current RSI, previous-bar RSI, and the min/max RSI over the last `lookback` bars
    so callers can detect a recent dip below / spike above a threshold.
    """
    try:
        closes = [float(c['c']) for c in candles]
        if len(closes) < period + 2:
            return {"rsi": 50.0, "prev_rsi": 50.0, "min_recent": 50.0, "max_recent": 50.0}

        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains   = [max(ch, 0.0) for ch in changes]
        losses  = [max(-ch, 0.0) for ch in changes]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_series = []
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
            rsi_series.append(100.0 - 100.0 / (1.0 + rs))

        if len(rsi_series) < 2:
            return {"rsi": 50.0, "prev_rsi": 50.0, "min_recent": 50.0, "max_recent": 50.0}

        recent = rsi_series[-lookback:] if len(rsi_series) >= lookback else rsi_series
        return {
            "rsi":        round(rsi_series[-1], 2),
            "prev_rsi":   round(rsi_series[-2], 2),
            "min_recent": round(min(recent), 2),
            "max_recent": round(max(recent), 2),
        }
    except Exception:
        return {"rsi": 50.0, "prev_rsi": 50.0, "min_recent": 50.0, "max_recent": 50.0}


def compute_ema_series(candles, period=EMA_PERIOD):
    """EMA of close prices as a full series (one value per bar from index period-1)."""
    try:
        closes = [float(c['c']) for c in candles]
        if len(closes) < period:
            return []
        k = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        series = [ema]
        for close in closes[period:]:
            ema = close * k + ema * (1.0 - k)
            series.append(ema)
        return series
    except Exception:
        return []


def compute_ema(candles, period=EMA_PERIOD):
    """Standard EMA of close prices seeded with SMA over the first `period` bars."""
    series = compute_ema_series(candles, period=period)
    return round(series[-1], 6) if series else None


def compute_ema_slope(candles, period=EMA_PERIOD, lag=3):
    """Direction of the EMA over the last `lag` bars: 'up' / 'down' / 'unknown'.

    Reads a single seeded EMA series and compares series[-1] to series[-1-lag].
    This is the true EMA `lag` bars ago — unlike comparing compute_ema(candles)
    to compute_ema(candles[:-lag]), which re-seeds from a different first bar and
    yields an only-approximate (and near flat EMA, sometimes wrong) slope.
    """
    series = compute_ema_series(candles, period=period)
    if len(series) < lag + 1:
        return "unknown"
    now, prev = series[-1], series[-1 - lag]
    if now == prev:
        return "unknown"
    return "up" if now > prev else "down"


def compute_volume_ratio(candles, lookback=10):
    """Ratio of current bar volume to the previous lookback-bar average."""
    try:
        vols = [float(c['v']) for c in candles]
        if len(vols) < lookback + 1:
            return 0.0
        avg = sum(vols[-(lookback + 1):-1]) / lookback
        return round(vols[-1] / avg, 3) if avg > 0 else 0.0
    except Exception:
        return 0.0


def compute_struct_stops(candles, lookback=5):
    """Swing low/high over the recent lookback bars for structural stop placement."""
    try:
        recent = candles[-lookback:]
        return min(float(c['l']) for c in recent), max(float(c['h']) for c in recent)
    except Exception:
        return None, None


def compute_funding(symbol, asset_ctxs):
    try:
        if asset_ctxs is None:
            return {"funding": 0, "day_volume": 0}
        meta = asset_ctxs[0]['universe']
        ctxs = asset_ctxs[1]
        symbol_idx = next((i for i, a in enumerate(meta) if a['name'] == symbol), None)
        if symbol_idx is None or symbol_idx >= len(ctxs):
            return {"funding": 0, "day_volume": 0}
        ctx = ctxs[symbol_idx]
        return {
            "funding":    round(float(ctx.get('funding', 0)) * 100, 4),
            "day_volume": float(ctx.get('dayNtlVlm', 0)),
        }
    except Exception as e:
        logger.error(f"Funding fetch error for {symbol}: {e}")
        return {"funding": 0, "day_volume": 0}
