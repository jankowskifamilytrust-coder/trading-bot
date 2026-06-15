import datetime

import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.dates as mdates

from env import TradingEnv

POLL_INTERVAL = 5  # seconds

env = TradingEnv(testnet=True)

times = []
equities = []
start_equity = None


def fetch_equity():
    return env.equity()


# --- Plot setup ---
plt.style.use("dark_background")
fig, ax = plt.subplots(figsize=(11, 6))
fig.canvas.manager.set_window_title("Hyperliquid Testnet — Equity")

(line,) = ax.plot([], [], color="#4ea1ff", linewidth=2)
fill = None


def update(frame):
    global start_equity, fill

    try:
        eq = fetch_equity()
    except Exception as e:
        print(f"fetch error: {e}")
        return

    now = datetime.datetime.now()
    if start_equity is None:
        start_equity = eq

    times.append(now)
    equities.append(eq)

    line.set_data(times, equities)

    # Color the line by session performance
    pnl = eq - start_equity
    up = pnl >= 0
    color = "#3ddc84" if up else "#ff5c5c"
    line.set_color(color)

    # Refresh fill under the curve
    if fill is not None:
        fill.remove()
    fill = ax.fill_between(times, start_equity, equities, color=color, alpha=0.15)

    # Baseline (starting equity)
    ax.axhline(start_equity, color="#777", linestyle="--", linewidth=0.8)

    ax.relim()
    ax.autoscale_view()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()

    sign = "+" if pnl >= 0 else ""
    ax.set_title(
        f"Equity ${eq:,.4f}   Session P&L {sign}{pnl:,.4f}   ({len(equities)} samples)",
        color=color,
        fontsize=13,
    )
    ax.set_xlabel("Time")
    ax.set_ylabel("Account Value (USD)")
    ax.grid(True, alpha=0.15)


print("Connecting to Hyperliquid testnet… opening live graph window.")
print("Close the window or press Ctrl+C to stop.")

ani = animation.FuncAnimation(
    fig, update, interval=POLL_INTERVAL * 1000, cache_frame_data=False
)
plt.tight_layout()
plt.show()
