import os
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()
wallet = Account.from_key(os.getenv("PRIVATE_KEY"))
exchange = Exchange(wallet, constants.TESTNET_API_URL)
info = Info(constants.TESTNET_API_URL, skip_ws=True)  # skip_ws prevents the hang

print("Closing all positions...")
state = info.user_state(wallet.address)
closed = 0
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

# Force clean exit — kills any lingering SDK background threads
os._exit(0)