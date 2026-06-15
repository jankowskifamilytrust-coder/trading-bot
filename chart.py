import json
import time
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
from datetime import datetime

EQUITY_LOG = "equity_curve.json"
TRADE_LOG = "trades.json"
SYMBOLS = ["HYPE", "ZEC", "ONDO", "NEAR", "BTC", "SOL", "ETH"]
REFRESH_SECONDS = 60

# Dark theme
plt.style.use('dark_background')
COLORS = {
    "HYPE": "#00ff88",
    "BTC":  "#ff9900",
    "ETH":  "#6272a4",
    "SOL":  "#9945ff",
    "NEAR": "#00c1de",
    "ONDO": "#ff79c6",
    "ZEC":  "#f1fa8c"
}

def load_json(filepath, default):
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except:
        return default

def draw_chart():
    curve = load_json(EQUITY_LOG, [])
    trades = load_json(TRADE_LOG, [])

    if len(curve) < 2:
        print("Waiting for data — need at least 2 cycles...")
        return

    timestamps = [datetime.fromisoformat(p['timestamp']) for p in curve]
    equity = [p['equity'] for p in curve]

    # Build per-symbol price series
    price_series = {sym: [] for sym in SYMBOLS}
    for point in curve:
        prices = point.get('prices', {})
        for sym in SYMBOLS:
            price_series[sym].append(prices.get(sym, None))

    # Normalize prices to % change from first value
    norm_series = {}
    for sym in SYMBOLS:
        vals = price_series[sym]
        first = next((v for v in vals if v is not None), None)
        if first and first > 0:
            norm_series[sym] = [((v / first) - 1) * 100 if v else None for v in vals]

    # P&L
    start_equity = equity[0]
    current_equity = equity[-1]
    pnl = current_equity - start_equity
    pnl_pct = (pnl / start_equity * 100) if start_equity > 0 else 0
    pnl_color = '#00ff88' if pnl >= 0 else '#ff4444'

    real_trades = [t for t in trades if t['action'] not in ['HOLD', 'SKIPPED']]

    # Layout
    fig = plt.figure(figsize=(16, 10), facecolor='#0f0f1a')
    gs = gridspec.GridSpec(3, 1, height_ratios=[2, 2, 1], hspace=0.4)

    # ── Panel 1: Equity curve ──
    ax1 = fig.add_subplot(gs[0])
    ax1.set_facecolor('#1a1a2e')
    ax1.plot(timestamps, equity, color='#00ff88', linewidth=2, label='Equity')
    ax1.fill_between(timestamps, equity, alpha=0.1, color='#00ff88')

    # Mark trades
    for t in real_trades:
        try:
            ts = datetime.fromisoformat(t['timestamp'])
            color = '#00aaff' if t['action'] == 'BUY' else '#ff4444'
            label = f"{t['action']} {t.get('symbol','')}"
            ax1.axvline(x=ts, color=color, alpha=0.6, linestyle='--', linewidth=1)
            ax1.annotate(
                label,
                xy=(ts, min(equity)),
                fontsize=7,
                color=color,
                rotation=90,
                va='bottom'
            )
        except:
            pass

    ax1.set_title('Equity Curve', color='white', fontsize=11)
    ax1.set_ylabel('USDC', color='white')
    ax1.tick_params(colors='white')
    ax1.grid(alpha=0.15)
    ax1.annotate(
        f"P&L: ${pnl:.2f} ({pnl_pct:.2f}%)",
        xy=(0.02, 0.92), xycoords='axes fraction',
        fontsize=11, color=pnl_color, fontweight='bold'
    )
    ax1.annotate(
        f"Trades: {len(real_trades)} | Current: ${current_equity:.2f}",
        xy=(0.02, 0.80), xycoords='axes fraction',
        fontsize=9, color='#aaaaaa'
    )

    # ── Panel 2: Symbol performance (% change) ──
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor('#1a1a2e')
    for sym, vals in norm_series.items():
        clean_ts = [timestamps[i] for i, v in enumerate(vals) if v is not None]
        clean_vals = [v for v in vals if v is not None]
        if clean_vals:
            ax2.plot(clean_ts, clean_vals, linewidth=1.5,
                     label=sym, color=COLORS.get(sym, '#ffffff'))

    ax2.axhline(y=0, color='#444', linewidth=0.8, linestyle='--')
    ax2.set_title('Symbol Performance (% change since start)', color='white', fontsize=11)
    ax2.set_ylabel('% Change', color='white')
    ax2.tick_params(colors='white')
    ax2.legend(loc='upper left', fontsize=8, ncol=4,
               facecolor='#1a1a2e', labelcolor='white')
    ax2.grid(alpha=0.15)

    # ── Panel 3: Trade log table ──
    ax3 = fig.add_subplot(gs[2])
    ax3.set_facecolor('#1a1a2e')
    ax3.axis('off')

    recent_trades = real_trades[-8:] if len(real_trades) > 8 else real_trades
    if recent_trades:
        table_data = []
        for t in reversed(recent_trades):
            ts = datetime.fromisoformat(t['timestamp']).strftime('%m/%d %H:%M')
            table_data.append([
                ts,
                t.get('action', ''),
                t.get('symbol', ''),
                f"${t.get('size', 0):.4f}",
                f"${t.get('price', 0):.4f}",
                f"${t.get('equity', 0):.2f}"
            ])

        table = ax3.table(
            cellText=table_data,
            colLabels=['Time', 'Action', 'Symbol', 'Size', 'Price', 'Equity'],
            loc='center',
            cellLoc='center'
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.3)

        for (row, col), cell in table.get_celld().items():
            cell.set_facecolor('#1a1a2e')
            cell.set_edgecolor('#333')
            if row == 0:
                cell.set_facecolor('#2a2a4e')
                cell.set_text_props(color='white', fontweight='bold')
            else:
                action = table_data[row-1][1]
                cell.set_text_props(
                    color='#00aaff' if action == 'BUY' else '#ff4444' if action == 'SELL' else 'white'
                )

    ax3.set_title('Recent Trades', color='white', fontsize=11)

    # Format x axis
    for ax in [ax1, ax2]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=7)

    # Title
    fig.suptitle(
        f'Claude Trading Bot Dashboard — Updated {datetime.now().strftime("%H:%M:%S")}',
        fontsize=13, color='white', fontweight='bold', y=0.98
    )

    plt.savefig('dashboard.png', dpi=130, bbox_inches='tight', facecolor='#0f0f1a')
    plt.close()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard updated → dashboard.png")

def run_chart(refresh_seconds=60):
    print(f"Chart running — refreshing every {refresh_seconds} seconds")
    print("Dashboard saved to dashboard.png after each update")
    while True:
        draw_chart()
        time.sleep(refresh_seconds)

if __name__ == "__main__":
    run_chart(refresh_seconds=REFRESH_SECONDS)