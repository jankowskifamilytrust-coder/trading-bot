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

RISK_PER_TRADE_PCT   = 0.01   # fraction of equity risked per stop-out
MAX_PORTFOLIO_RISK_PCT = 0.03  # total open risk cap before new entries are blocked
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

TRADE_LOG             = "data/trades.json"
EQUITY_LOG            = "data/equity_curve.json"
TRAILING_STOP_LOG     = "data/trailing_peaks.json"
LOC_LOG               = "data/pending_loc.json"
VOLUME_RANK_LOG       = "data/volume_ranking.json"
VOLUME_RANK_TTL_HOURS = 24

FUNDING_LONG_MAX     = 0.05    # % — skip long if funding above this
FUNDING_SHORT_MIN    = -0.05   # % — skip short if funding below this
ADX_DECAY_EXIT       = 20      # close if ADX drops below this while holding
VOLUME_CONFIRM_RATIO = 0.7     # hook bar must have ≥70% of 10-bar avg volume
STRUCT_STOP_BUFFER   = 0.3     # ATR buffer past swing low/high for structural stop
