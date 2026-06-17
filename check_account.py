import os
import json
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
master = os.getenv("MASTER_ADDRESS", wallet.address)
info = Info(constants.MAINNET_API_URL, skip_ws=True)

print("=" * 50)
print("API wallet:    ", wallet.address)
print("Master account:", master)
print("MAINNET")
print("=" * 50)

state = info.user_state(master)

print("\nAccount value:", state.get('marginSummary', {}).get('accountValue'))
print("\nOpen positions:")
positions = [p for p in state.get('assetPositions', [])
             if float(p.get('position', {}).get('szi', 0)) != 0]
if not positions:
    print("  NONE — account has no open positions")
for p in positions:
    pos = p.get('position', {})
    print(f"  {pos.get('coin')}: size={pos.get('szi')} entry={pos.get('entryPx')}")

print("\n" + "=" * 50)
print("FULL RAW STATE:")
print(json.dumps(state, indent=2))

print("\n" + "=" * 50)
print("SPOT BALANCES:")
spot = info.spot_user_state(master)
for b in spot.get('balances', []):
    print(f"  {b.get('coin')}: total={b.get('total')} hold={b.get('hold')}")
print("=" * 50)

print("\n" + "=" * 50)
print("MAINNET TRADEABLE PERPS:")
names = [a['name'] for a in info.meta()['universe']]
print(f"  {len(names)} perps")
print("=" * 50)

os._exit(0)