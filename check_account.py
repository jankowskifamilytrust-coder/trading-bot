import os
import json
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
info = Info(constants.TESTNET_API_URL, skip_ws=True)

print("=" * 50)
print("Wallet address:", wallet.address)
print("=" * 50)

state = info.user_state(wallet.address)

print("\nAccount value:", state.get('marginSummary', {}).get('accountValue'))
print("\nOpen positions:")
positions = state.get('assetPositions', [])
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
spot = info.spot_user_state(wallet.address)
for b in spot.get('balances', []):
    print(f"  {b.get('coin')}: total={b.get('total')} hold={b.get('hold')}")
print("=" * 50)

print("\n" + "=" * 50)
print("TESTNET TRADEABLE PERPS:")
testnet_meta = info.meta()
names = [a['name'] for a in testnet_meta['universe']]
print(names)
print("=" * 50)

print("\n" + "=" * 50)
print("TESTNET CONTEXT FOR ETH:")
ctxs_data = info.meta_and_asset_ctxs()
universe = ctxs_data[0]['universe']
ctxs = ctxs_data[1]
for i, a in enumerate(universe):
    if a['name'] == 'ETH':
        print(f"ETH found at index {i}")
        print(json.dumps(ctxs[i], indent=2))
        break
print("\nTESTNET all_mids ETH:")
mids = info.all_mids()
print("ETH mid:", mids.get('ETH'))
print("=" * 50)

os._exit(0)