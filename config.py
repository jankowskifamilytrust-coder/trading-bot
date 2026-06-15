TOP_N = 10
PINNED = ["BTC", "ETH", "SOL", "HYPE"]
STABLECOINS = {
    # USD-pegged
    "USDC", "USDC0", "USDT", "USDT0", "USDHL",
    "USD", "USDB", "USDX", "USDD", "USDE", "USDM", "USDY",
    "DAI", "FRAX", "LUSD", "GUSD", "TUSD", "FDUSD",
    "PYUSD", "BUSD", "HUSD", "MIM", "DOLA", "GHO", "CRVUSD",
    "SUSD", "NUSD", "EUSD", "BEAN",
    "USR",
    # Euro / other fiat-pegged
    "EURS", "EURC", "AGEUR", "EURT",
}

MAX_POSITIONS = 3
LEVERAGE = 2
INTERVAL_MINUTES = 60
SLIPPAGE = 0.05
SETTLE_SECONDS = 3
MAX_ORACLE_GAP_PCT = 3.0
MAKER_WAIT_SECONDS = 30

VOL_TARGET_PCT = 0.02
MAX_NOTIONAL_USD = 200
MIN_NOTIONAL_USD = 20

STOP_ATR_MULT = 3.0
ATR_PERIOD = 14

SUPERTREND_PERIOD = 14
SUPERTREND_MULT = 3.0

ADX_PERIOD = 14
ADX_THRESHOLD = 25

RSI_PERIOD = 14
RSI_LONG_THRESHOLD = 40   # longs: wait for RSI dip below this
RSI_SHORT_THRESHOLD = 60  # shorts: wait for RSI spike above this
RSI_LOOKBACK = 5          # bars to look back for a dip/spike

EMA_PERIOD = 20
EMA_BAND_PCT = 0.02       # price within ±2% of EMA counts as "near"

TRADE_LOG         = "data/trades.json"
EQUITY_LOG        = "data/equity_curve.json"
TRAILING_STOP_LOG = "data/trailing_peaks.json"
