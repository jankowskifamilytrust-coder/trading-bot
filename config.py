TOP_N = 30
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

MAX_POSITIONS = 3              # standard-dex cap (kept for back-references); see MAX_POSITIONS_PER_DEX
LEVERAGE = 2
INTERVAL_MINUTES = 240   # 4h cycle — entry triggers computed on closed 4h bars (matches backtest)
SLIPPAGE = 0.05          # max slippage for market stop-out orders — large intentionally for guaranteed fills
SETTLE_SECONDS = 3

# Per-dex position caps. HIP-3 (xyz) uses an isolated clearinghouse, so each dex
# is its own sub-portfolio. Standard keeps the backtested cap of 3; xyz capped at 1.
MAX_POSITIONS_PER_DEX = {"": 3, "xyz": 1}

RISK_PER_TRADE_PCT   = 0.025   # fraction of equity risked per stop-out
MAX_PORTFOLIO_RISK_PCT = 0.075  # standard-dex heat cap (kept for back-references); see *_PER_DEX
# Per-dex heat cap = that dex's position cap × per-trade risk, evaluated against that dex's equity.
MAX_PORTFOLIO_RISK_PCT_PER_DEX = {d: n * RISK_PER_TRADE_PCT for d, n in MAX_POSITIONS_PER_DEX.items()}
MIN_NOTIONAL_USD = 20
MAX_NOTIONAL_PCT = 0.30   # equity-relative ceiling — 30% of equity per trade
MIN_NOTIONAL_PCT = 0.02   # equity-relative floor (skip rather than inflate)

STOP_ATR_MULT = 3.0
ATR_PERIOD = 14

SUPERTREND_PERIOD = 14
SUPERTREND_MULT = 3.0

ADX_PERIOD = 14
ADX_THRESHOLD = 30

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
START_EQUITY_LOG      = "data/start_equity.json"
ADVISOR_LOG           = "data/advisor_log.jsonl"
VOLUME_RANK_TTL_HOURS = 24

FUNDING_LONG_MAX     = 0.018   # % — skip long if funding above this (aligned to exit threshold)
FUNDING_SHORT_MIN    = -0.018  # % — skip short if funding below this (aligned to exit threshold)
FUNDING_EXIT_THRESHOLD = 0.018 # % — close a held position if funding turns this adverse
                               #     (long: funding > +threshold; short: funding < −threshold)
ADX_DECAY_EXIT       = 20      # close if ADX drops below this while holding
VOLUME_CONFIRM_RATIO = 0.7     # hook bar must have ≥70% of 10-bar avg volume
STRUCT_STOP_BUFFER   = 0.3     # ATR buffer past swing low/high for structural stop
