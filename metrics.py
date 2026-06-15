import os
import time
from datetime import datetime
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
info = Info(constants.TESTNET_API_URL, skip_ws=True)
addr = wallet.address

def f(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def line():
    print("-" * 52)

print("=" * 52)
print("  TRADING PERFORMANCE REPORT")
print(f"  Wallet: {addr}")
print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 52)

# ─── Pull fills ───────────────────────────────────────────────────────────────
try:
    fills = info.user_fills(addr)
except Exception as e:
    print(f"Could not fetch fills: {e}")
    os._exit(1)

if not fills:
    print("\nNo fills found yet — no trades to report.")
    os._exit(0)

# ─── Per-fill aggregation ─────────────────────────────────────────────────────
total_fees = 0.0
maker_fills = 0
taker_fills = 0
opens = 0
closes = 0
realized_pnls = []   # closedPnl per closing fill (non-zero)
per_coin = {}        # coin -> {pnl, fees, trades}

for fl in fills:
    coin = fl.get('coin', '?')
    fee = f(fl.get('fee', 0))
    pnl = f(fl.get('closedPnl', 0))
    crossed = fl.get('crossed', True)   # True = taker, False = maker
    direction = fl.get('dir', '')

    total_fees += fee
    if crossed:
        taker_fills += 1
    else:
        maker_fills += 1

    if direction.startswith("Open"):
        opens += 1
    elif direction.startswith("Close") or direction in ("Sell", "Buy"):
        closes += 1

    c = per_coin.setdefault(coin, {"pnl": 0.0, "fees": 0.0, "fills": 0})
    c["pnl"] += pnl
    c["fees"] += fee
    c["fills"] += 1

    # A non-zero closedPnl marks a realizing (closing) fill — the round-trip result
    if pnl != 0:
        realized_pnls.append(pnl)

# ─── Win/loss stats from closing fills ────────────────────────────────────────
wins = [p for p in realized_pnls if p > 0]
losses = [p for p in realized_pnls if p < 0]
n_closed = len(realized_pnls)

gross_profit = sum(wins)
gross_loss = abs(sum(losses))
total_realized = sum(realized_pnls)

win_rate = (len(wins) / n_closed * 100) if n_closed else 0
avg_win = (gross_profit / len(wins)) if wins else 0
avg_loss = (gross_loss / len(losses)) if losses else 0
profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else float('inf')

# ─── Funding (separate ledger, not in fills) ──────────────────────────────────
funding_total = None
try:
    end = int(time.time() * 1000)
    start = end - (90 * 24 * 60 * 60 * 1000)  # last 90 days
    funding_events = info.user_funding_history(addr, start, end)
    funding_total = 0.0
    for ev in funding_events:
        d = ev.get('delta', ev)
        funding_total += f(d.get('usdc', 0))
except Exception as e:
    funding_total = None  # mark unavailable rather than show a wrong number

# ─── Report ───────────────────────────────────────────────────────────────────
print(f"\nTotal fills:        {len(fills)}")
print(f"  Opens:            {opens}")
print(f"  Closes:           {closes}")
print(f"  Closed trades:    {n_closed} (with realized P&L)")
line()

print("WIN / LOSS")
print(f"  Win rate:         {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
print(f"  Avg win:          +${avg_win:.2f}")
print(f"  Avg loss:         -${avg_loss:.2f}")
if win_loss_ratio == float('inf'):
    print(f"  Win/loss ratio:   ∞ (no losses yet)")
else:
    print(f"  Win/loss ratio:   {win_loss_ratio:.2f}x")
if profit_factor == float('inf'):
    print(f"  Profit factor:    ∞ (no losses yet)")
else:
    print(f"  Profit factor:    {profit_factor:.2f}  (>1 = profitable)")
line()

print("MAKER vs TAKER")
total_ex = maker_fills + taker_fills
maker_pct = (maker_fills / total_ex * 100) if total_ex else 0
print(f"  Maker fills:      {maker_fills} ({maker_pct:.0f}%)")
print(f"  Taker fills:      {taker_fills} ({100-maker_pct:.0f}%)")
print(f"  (higher maker % = lower fees)")
line()

print("P&L BREAKDOWN")
rsign = "+" if total_realized >= 0 else ""
print(f"  Gross realized:   {rsign}${total_realized:.2f}  (before fees/funding)")
print(f"  Fees paid:        -${total_fees:.2f}")
if funding_total is None:
    print(f"  Funding:          (unavailable on this endpoint)")
    net = total_realized - total_fees
    print(f"  Net (after fees): {'+' if net>=0 else ''}${net:.2f}")
else:
    fsign = "+" if funding_total >= 0 else ""
    print(f"  Funding:          {fsign}${funding_total:.2f}  ({'received' if funding_total>=0 else 'paid'})")
    net = total_realized - total_fees + funding_total
    print(f"  Net (all-in):     {'+' if net>=0 else ''}${net:.2f}")
line()

print("PER-COIN")
for coin, d in sorted(per_coin.items(), key=lambda x: x[1]['pnl'], reverse=True):
    psign = "+" if d['pnl'] >= 0 else ""
    print(f"  {coin:<8} P&L {psign}${d['pnl']:.2f} | fees -${d['fees']:.2f} | {d['fills']} fills")
print("=" * 52)

# Fees as % of realized — the cost-efficiency number
if total_realized != 0:
    fee_drag = total_fees / abs(total_realized) * 100
    print(f"\nFee drag: fees are {fee_drag:.1f}% of gross realized P&L")
print("\nNote: 'closed trades' counts fills that realized P&L. Win rate")
print("reflects those closing fills, which is how Hyperliquid books P&L.")

os._exit(0)