import time
import datetime

from env import TradingEnv

env = TradingEnv(testnet=True)

POLL_INTERVAL = 5  # seconds
history = []

def clear():
    print("\033[H\033[J", end="")

def fetch_state():
    positions = env.positions()
    total_upnl = sum(p.unrealized_pnl for p in positions)
    return {
        "equity": env.equity(),
        "margin_used": env.margin_used(),
        "unrealized_pnl": total_upnl,
        "positions": positions,
        "mids": env.all_prices(),
        "timestamp": datetime.datetime.now(),
    }

def format_pnl(val):
    sign = "+" if val >= 0 else ""
    color = "\033[92m" if val >= 0 else "\033[91m"
    reset = "\033[0m"
    return f"{color}{sign}{val:.4f}{reset}"

def render(state, start_equity):
    clear()
    now = state["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    equity = state["equity"]
    pnl_since_start = equity - start_equity
    upnl = state["unrealized_pnl"]
    margin = state["margin_used"]

    print(f"\033[1m=== Hyperliquid Testnet — Real-Time Equity Tracker ===\033[0m")
    print(f"  Wallet : {env.address}")
    print(f"  Time   : {now}  (refresh every {POLL_INTERVAL}s)\n")

    print(f"  {'Equity':<22} ${equity:>14.4f}")
    print(f"  {'Session P&L':<22} {format_pnl(pnl_since_start):>21}")
    print(f"  {'Unrealized P&L':<22} {format_pnl(upnl):>21}")
    print(f"  {'Margin Used':<22} ${margin:>14.4f}")

    positions = state["positions"]
    if positions:
        print(f"\n  {'─'*52}")
        print(f"  {'Asset':<8} {'Side':<6} {'Size':>10} {'Entry':>12} {'Mark':>12} {'uPnL':>10}")
        print(f"  {'─'*52}")
        for p in positions:
            mark = float(state["mids"].get(p.coin, 0))
            print(f"  {p.coin:<8} {p.side:<6} {abs(p.size):>10.4f} {p.entry_px:>12.4f} {mark:>12.4f} {format_pnl(p.unrealized_pnl):>17}")
    else:
        print(f"\n  No open positions.")

    # Mini equity history (last 10 readings)
    if len(history) > 1:
        print(f"\n  {'─'*52}")
        print(f"  Recent equity (last {min(len(history), 10)} samples):")
        for h in history[-10:]:
            ts = h["timestamp"].strftime("%H:%M:%S")
            bar_val = h["equity"] - start_equity
            bar = "▲" if bar_val >= 0 else "▼"
            color = "\033[92m" if bar_val >= 0 else "\033[91m"
            reset = "\033[0m"
            print(f"    {ts}  ${h['equity']:.4f}  {color}{bar} {bar_val:+.4f}{reset}")

    print(f"\n  Press Ctrl+C to stop.")

def main():
    print("Connecting to Hyperliquid testnet…")
    state = fetch_state()
    start_equity = state["equity"]
    print(f"Starting equity: ${start_equity:.4f}")
    time.sleep(1)

    try:
        while True:
            state = fetch_state()
            history.append(state)
            render(state, start_equity)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n\nTracker stopped.")
        if history:
            final = history[-1]["equity"]
            print(f"Session P&L: {format_pnl(final - start_equity)}")

if __name__ == "__main__":
    main()
