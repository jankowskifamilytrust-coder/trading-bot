"""
Equity/price recorder.

Samples account equity and HYPE price on a fixed interval and appends each
reading to equity_curve.json. bot.py reads that file to render the performance
dashboard. Trades are logged separately (trades.json) by your strategy when it
places orders — this file just seeds it as an empty list if missing.

    python logger.py                 # log HYPE every 10s
    python logger.py --coin BTC      # log a different coin
    python logger.py --interval 30   # sample every 30s

Runs until Ctrl+C. Safe to stop and restart — it appends to the existing curve.
"""

import sys
import json
import time
import datetime
import os
from env import TradingEnv

CURVE_FILE = "equity_curve.json"
TRADES_FILE = "trades.json"


def parse_arg(flag, default, cast):
    if flag in sys.argv:
        return cast(sys.argv[sys.argv.index(flag) + 1])
    return default


def load_curve():
    if os.path.exists(CURVE_FILE):
        try:
            with open(CURVE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"[warn] {CURVE_FILE} was corrupt — starting fresh")
    return []


def seed_trades():
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "w") as f:
            json.dump([], f)
        print(f"[logger] created empty {TRADES_FILE}")


def main():
    coin = parse_arg("--coin", "HYPE", str)
    interval = parse_arg("--interval", 10, int)

    env = TradingEnv(testnet=True)
    seed_trades()
    curve = load_curve()
    print(f"[logger] logging {coin} every {interval}s -> {CURVE_FILE}")
    print(f"[logger] {len(curve)} existing samples. Ctrl+C to stop.")

    try:
        while True:
            point = {
                "timestamp": datetime.datetime.now().isoformat(),
                "equity": env.equity(),
                "price": env.price(coin),
            }
            curve.append(point)
            # Write the whole curve each tick (simple + crash-safe enough at this scale)
            with open(CURVE_FILE, "w") as f:
                json.dump(curve, f, indent=2)
            print(f"  {point['timestamp'][11:19]}  equity=${point['equity']:.4f}  {coin}=${point['price']:.4f}  (n={len(curve)})")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[logger] stopped. {len(curve)} samples saved to {CURVE_FILE}.")


if __name__ == "__main__":
    main()
