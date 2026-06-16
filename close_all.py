import os
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
# MAINNET emergency flatten across both dexes (standard "" + xyz HIP-3).
# perp_dexs lets market_close route xyz: positions to the right clearinghouse.
DEXES = ["", "xyz"]
exchange = Exchange(wallet, constants.MAINNET_API_URL, perp_dexs=DEXES)
info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=DEXES)  # skip_ws prevents the hang

print("Closing all positions (mainnet, std + xyz)...")
closed = 0
for dex in DEXES:
    state = info.user_state(wallet.address, dex=dex)
    for p in state.get('assetPositions', []):
        pos = p.get('position', {})
        coin = pos.get('coin')
        size = float(pos.get('szi', 0))
        if size != 0:
            try:
                result = exchange.market_close(coin)
                print(f"Closed {coin} (dex={dex or 'std'}): {result}")
                closed += 1
            except Exception as e:
                print(f"Failed to close {coin} (dex={dex or 'std'}): {e}")

if closed == 0:
    print("No open positions found")
else:
    print(f"Done — closed {closed} positions")

# Force clean exit — kills any lingering SDK background threads
os._exit(0)