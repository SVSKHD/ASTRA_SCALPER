# BACKTEST COMMAND REFERENCE
# All commands from project root: ASTRA_HAWK_SCALPER_20265/

# ============================================================
# BASELINE (current live bot config)
# ============================================================

python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# CHANGE CAPITAL ONLY (lot auto-scales from sl-target)
# ============================================================

# $25,000 account
python backtest.py --capital 25000 --close-confirm --trend-filter --data-dir data --months 3

# $50,000 account (current live)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3

# $100,000 account
python backtest.py --capital 100000 --close-confirm --trend-filter --data-dir data --months 3

# $200,000 account
python backtest.py --capital 200000 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# CHANGE LOT SIZE via SL TARGET
# lot = sl_target / (sl_pips × 100)
# sl_pips=5: lot = sl_target / 500
# ============================================================

# 0.2 lot → sl_target=$100
python backtest.py --capital 50000 --sl-target 100 --tp-target 300 --sl-pips 5 --daily-loss 100 --daily-profit 300 --close-confirm --trend-filter --data-dir data --months 3

# 0.4 lot → sl_target=$200 (current live)
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 5 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# 0.6 lot → sl_target=$300
python backtest.py --capital 50000 --sl-target 300 --tp-target 900 --sl-pips 5 --daily-loss 300 --daily-profit 900 --close-confirm --trend-filter --data-dir data --months 3

# 0.8 lot → sl_target=$400
python backtest.py --capital 50000 --sl-target 400 --tp-target 1200 --sl-pips 5 --daily-loss 400 --daily-profit 1200 --close-confirm --trend-filter --data-dir data --months 3

# 1.0 lot → sl_target=$500
python backtest.py --capital 50000 --sl-target 500 --tp-target 1500 --sl-pips 5 --daily-loss 500 --daily-profit 1500 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# CHANGE SL PIPS (tighter/wider stop)
# ============================================================

# 3 pip SL → tighter stop, smaller lot
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 3 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# 5 pip SL (current live)
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 5 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# 8 pip SL → wider stop, fewer fakeouts
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 8 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# 10 pip SL → wide stop
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 10 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# CHANGE R:R RATIO
# tp_target = sl_target × RR
# ============================================================

# R:R 1:2 → tp=$400
python backtest.py --capital 50000 --sl-target 200 --tp-target 400 --sl-pips 5 --daily-loss 200 --daily-profit 400 --close-confirm --trend-filter --data-dir data --months 3

# R:R 1:3 (current live) → tp=$600
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 5 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3

# R:R 1:4 → tp=$800
python backtest.py --capital 50000 --sl-target 200 --tp-target 800 --sl-pips 5 --daily-loss 200 --daily-profit 800 --close-confirm --trend-filter --data-dir data --months 3

# R:R 1:5 → tp=$1000
python backtest.py --capital 50000 --sl-target 200 --tp-target 1000 --sl-pips 5 --daily-loss 200 --daily-profit 1000 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# MULTIPLE TRADES PER DAY
# daily-loss auto-scales to N × sl-target
# ============================================================

# 1 trade/day (current live — optimal)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3

# 2 trades/day
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --max-trades 2

# 3 trades/day
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --max-trades 3

# 4 trades/day
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --max-trades 4

# ============================================================
# MONTHS OF DATA
# ============================================================

# 1 month (April only)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 1

# 3 months (validated baseline)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3

# 6 months
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 6

# 12 months
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 12

# ============================================================
# PROP FIRM CHALLENGE CONFIGS
# ============================================================

# Funding Pips $50k challenge — current config
# Need +8% = +$4,000 | Daily DD 5% = $2,500 | Total DD 10% = $5,000
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3

# Funding Pips $100k challenge
# Need +8% = +$8,000 | Daily DD 5% = $5,000 | Total DD 10% = $10,000
python backtest.py --capital 100000 --sl-target 400 --tp-target 1200 --sl-pips 5 --daily-loss 400 --daily-profit 1200 --close-confirm --trend-filter --data-dir data --months 3

# Funding Pips $200k challenge
python backtest.py --capital 200000 --sl-target 800 --tp-target 2400 --sl-pips 5 --daily-loss 800 --daily-profit 2400 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# THRESHOLD VARIATIONS (research only — does not change bot)
# ============================================================

# T=18 (tighter breakout — more signals, lower quality)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --threshold-pips 18

# T=19 (marginal improvement — needs more data to confirm)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --threshold-pips 19

# T=20 (current live — validated optimal)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --threshold-pips 20

# T=25 (wider breakout — fewer signals, higher quality?)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --threshold-pips 25

# T=30 (very wide — few signals)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --threshold-pips 30

# ============================================================
# RESEARCH FLAGS (confirmed WORSE than baseline — do not deploy)
# ============================================================

# ATR dynamic threshold
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --dynamic-threshold atr --compare-fixed

# Breakeven stop at 8 pips
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --breakeven-stop 8

# Continuation bias (after SL, same direction next day)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --continuation-bias

# Time filter (blackout midnight/London/NY)
python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --time-filter

# ============================================================
# COMBINATION RUNS FOR COMPARISON
# ============================================================

# Compare 0.4 lot vs 0.8 lot on $100k
python backtest.py --capital 100000 --sl-target 200 --tp-target 600 --sl-pips 5 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3
python backtest.py --capital 100000 --sl-target 400 --tp-target 1200 --sl-pips 5 --daily-loss 400 --daily-profit 1200 --close-confirm --trend-filter --data-dir data --months 3

# Compare R:R 1:2 vs 1:3 vs 1:4
python backtest.py --capital 50000 --sl-target 200 --tp-target 400 --sl-pips 5 --daily-loss 200 --daily-profit 400 --close-confirm --trend-filter --data-dir data --months 3
python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --sl-pips 5 --daily-loss 200 --daily-profit 600 --close-confirm --trend-filter --data-dir data --months 3
python backtest.py --capital 50000 --sl-target 200 --tp-target 800 --sl-pips 5 --daily-loss 200 --daily-profit 800 --close-confirm --trend-filter --data-dir data --months 3

# ============================================================
# LOT / DOLLAR REFERENCE TABLE
# Formula: lot = sl_target / (sl_pips × 100)
# sl_pips=5 → lot = sl_target / 500
# ============================================================
#
# sl_target | lot  | tp (1:3) | daily_loss | daily_profit
# ----------|------|----------|------------|-------------
#   $100    | 0.2  |   $300   |   $100     |    $300
#   $200    | 0.4  |   $600   |   $200     |    $600   ← LIVE
#   $300    | 0.6  |   $900   |   $300     |    $900
#   $400    | 0.8  |  $1,200  |   $400     |   $1,200
#   $500    | 1.0  |  $1,500  |   $500     |   $1,500
#   $600    | 1.2  |  $1,800  |   $600     |   $1,800
#  $1,000   | 2.0  |  $3,000  |  $1,000    |   $3,000
#
# ============================================================
# ALWAYS PASS THESE FLAGS — they match live bot behaviour
# ============================================================
# --close-confirm   wait for bar close before entry
# --trend-filter    bar.open must be above/below start price
# --data-dir data   use real locked start prices from day files
# ============================================================
## python backtest.py --capital 50000 --close-confirm --trend-filter --data-dir data --months 3 --max-trades 4                                                                