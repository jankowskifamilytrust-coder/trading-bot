import os
import json
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# Emergency flatten. Stop the bot before running this so it can't re-place orders
# against the state we're about to clear.
load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
master = os.getenv("MASTER_ADDRESS", wallet.address)
# MAINNET emergency flatten. Sign with the API wallet, act on the master account.
exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=master)
info = Info(constants.MAINNET_API_URL, skip_ws=True)  # skip_ws prevents the hang

# 1. Cancel every resting order first — otherwise a resting LOC could fill into a
#    new position right after we flatten.
print("Cancelling resting orders...")
cancelled = 0
try:
    for o in info.open_orders(master) or []:
        coin, oid = o.get('coin'), o.get('oid')
        try:
            exchange.cancel(coin, oid)
            print(f"Cancelled {coin} oid {oid}")
            cancelled += 1
        except Exception as e:
            print(f"Failed to cancel {coin} oid {oid}: {e}")
except Exception as e:
    print(f"Could not fetch open orders: {e}")
print(f"Cancelled {cancelled} resting orders")

# 2. Close every open position.
print("Closing all positions (mainnet)...")
closed = 0
state = info.user_state(master)
for p in state.get('assetPositions', []):
    pos = p.get('position', {})
    coin = pos.get('coin')
    size = float(pos.get('szi', 0))
    if size != 0:
        try:
            result = exchange.market_close(coin)
            print(f"Closed {coin}: {result}")
            closed += 1
        except Exception as e:
            print(f"Failed to close {coin}: {e}")

if closed == 0:
    print("No open positions found")
else:
    print(f"Done — closed {closed} positions")

# 3. Clear bot state so a restart doesn't act on stale peaks / pending orders.
for path in ("data/trailing_peaks.json", "data/pending_loc.json"):
    try:
        if os.path.exists(path):
            with open(path, "w") as f:
                json.dump({}, f)
            print(f"Cleared {path}")
    except Exception as e:
        print(f"Failed to clear {path}: {e}")

# Force clean exit — kills any lingering SDK background threads
os._exit(0)