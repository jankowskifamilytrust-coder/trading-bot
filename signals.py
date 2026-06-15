from loguru import logger
from config import ATR_PERIOD


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
