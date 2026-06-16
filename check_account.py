import os
import json
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
# MAINNET, dual-dex: standard ("") + xyz HIP-3 (isolated clearinghouses).
DEXES = ["", "xyz"]
info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=DEXES)

print("=" * 50)
print("Wallet address:", wallet.address)
print("MAINNET (std + xyz dexes)")
print("=" * 50)

states = {}
for dex in DEXES:
    state = info.user_state(wallet.address, dex=dex)
    states[dex] = state
    label = dex or "std"
    print(f"\n[{label}] Account value:", state.get('marginSummary', {}).get('accountValue'))
    positions = [p for p in state.get('assetPositions', [])
                 if float(p.get('position', {}).get('szi', 0)) != 0]
    if not positions:
        print(f"  [{label}] NONE — no open positions")
    for p in positions:
        pos = p.get('position', {})
        print(f"  [{label}] {pos.get('coin')}: size={pos.get('szi')} entry={pos.get('entryPx')}")

print("\n" + "=" * 50)
print("FULL RAW STATE (per dex):")
print(json.dumps(states, indent=2))

print("\n" + "=" * 50)
print("SPOT BALANCES:")
spot = info.spot_user_state(wallet.address)
for b in spot.get('balances', []):
    print(f"  {b.get('coin')}: total={b.get('total')} hold={b.get('hold')}")
print("=" * 50)

print("\n" + "=" * 50)
print("MAINNET TRADEABLE PERPS (std + xyz):")
for dex in DEXES:
    meta = info.post("/info", {"type": "meta", "dex": dex}) if dex else info.meta()
    names = [a['name'] for a in meta['universe']]
    print(f"  [{dex or 'std'}] {len(names)} perps")
print("=" * 50)

os._exit(0)