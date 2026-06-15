"""
Trading environment for Hyperliquid.

A thin, safe wrapper around the Hyperliquid SDK that gives you everything a
strategy needs: market data, account state, and order execution. Strategy logic
lives elsewhere — this just exposes clean building blocks.

Defaults to TESTNET. Switching to mainnet requires an explicit, deliberate flag.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants


@dataclass
class Position:
    coin: str
    size: float          # signed: positive = long, negative = short
    entry_px: float
    unrealized_pnl: float

    @property
    def side(self) -> str:
        return "LONG" if self.size > 0 else "SHORT"


class TradingEnv:
    def __init__(self, testnet: bool = True):
        load_dotenv()
        private_key = os.getenv("PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("PRIVATE_KEY not found in .env")

        self.testnet = testnet
        self.wallet = Account.from_key(private_key)
        self.address = self.wallet.address

        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.info = Info(base_url)
        self.exchange = Exchange(self.wallet, base_url)

        net = "TESTNET" if testnet else "\033[91mMAINNET (REAL FUNDS)\033[0m"
        print(f"[env] Connected to {net} as {self.address}")

    # ----- Market data -----

    def price(self, coin: str) -> float:
        """Current mid price for a coin (e.g. 'HYPE', 'BTC', 'ETH')."""
        return float(self.info.all_mids()[coin])

    def all_prices(self) -> dict:
        """All mid prices keyed by coin."""
        return {k: float(v) for k, v in self.info.all_mids().items()}

    # ----- Account state -----

    def equity(self) -> float:
        """Total account value in USD."""
        state = self.info.user_state(self.address)
        return float(state["marginSummary"]["accountValue"])

    def margin_used(self) -> float:
        state = self.info.user_state(self.address)
        return float(state["marginSummary"]["totalMarginUsed"])

    def positions(self) -> list[Position]:
        """All open positions."""
        state = self.info.user_state(self.address)
        out = []
        for p in state.get("assetPositions", []):
            pos = p["position"]
            out.append(
                Position(
                    coin=pos["coin"],
                    size=float(pos["szi"]),
                    entry_px=float(pos["entryPx"]) if pos["entryPx"] else 0.0,
                    unrealized_pnl=float(pos["unrealizedPnl"]),
                )
            )
        return out

    def position(self, coin: str) -> Position | None:
        """The open position for a single coin, or None."""
        for p in self.positions():
            if p.coin == coin:
                return p
        return None

    # ----- Order execution -----

    def market_buy(self, coin: str, size: float, slippage: float = 0.01):
        """Market buy `size` units of `coin`. slippage is a fraction (0.01 = 1%)."""
        print(f"[order] MARKET BUY {size} {coin}")
        return self.exchange.market_open(coin, True, size, None, slippage)

    def market_sell(self, coin: str, size: float, slippage: float = 0.01):
        """Market sell `size` units of `coin`."""
        print(f"[order] MARKET SELL {size} {coin}")
        return self.exchange.market_open(coin, False, size, None, slippage)

    def limit_order(self, coin: str, is_buy: bool, size: float, price: float):
        """Place a resting limit (GTC) order."""
        side = "BUY" if is_buy else "SELL"
        print(f"[order] LIMIT {side} {size} {coin} @ {price}")
        return self.exchange.order(
            coin, is_buy, size, price, {"limit": {"tif": "Gtc"}}
        )

    def close(self, coin: str, slippage: float = 0.01):
        """Market-close the entire open position for a coin."""
        print(f"[order] CLOSE {coin}")
        return self.exchange.market_close(coin, None, None, slippage)

    def cancel_all(self, coin: str):
        """Cancel all open (resting) orders for a coin."""
        open_orders = self.info.open_orders(self.address)
        results = []
        for o in open_orders:
            if o["coin"] == coin:
                results.append(self.exchange.cancel(coin, o["oid"]))
        print(f"[order] canceled {len(results)} order(s) for {coin}")
        return results


if __name__ == "__main__":
    # Smoke test — read-only, places no orders.
    env = TradingEnv(testnet=True)
    print(f"Equity      : ${env.equity():,.4f}")
    print(f"Margin used : ${env.margin_used():,.4f}")
    print(f"HYPE price  : ${env.price('HYPE'):,.4f}")
    positions = env.positions()
    if positions:
        print("Positions:")
        for p in positions:
            print(f"  {p.coin:<6} {p.side:<5} size={p.size:<10} entry={p.entry_px:<10} uPnL={p.unrealized_pnl:+.4f}")
    else:
        print("Positions   : none")
