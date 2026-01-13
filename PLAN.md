# Bitcoin Momentum Trader - Handover Plan

## Project Overview
We are building an automated trading bot (`examples/btc_momentum_trader.py`) that trades "Bitcoin Up or Down" markets on Polymarket based on price momentum. The bot monitors active markets, checks for price deviations indicating momentum, and executes trades.

## Current Status (2026-01-13)
- **Script**: `examples/btc_momentum_trader.py`
- **Functionality**:
  - Automatically finds "Bitcoin Up or Down" 15min markets (1h filtering removed).
  - Configurable parameters (Bet size, Momemtum thresholds).
  - Dry Run mode (Default) & Live mode (`--live`).
- **Recent Fixes**:
  - **Market Discovery**: Fixed an issue where active markets were hidden by hundreds of future markets in the API response.
    - Solution: Implemented deep pagination (scan up to 2000 markets) with `order="endDate", ascending=False` to traverse from future to present markets properly.

## Key Files
- `examples/btc_momentum_trader.py`: The main trading script.
- `examples/bitcoin_up_or_down_history.py`: Backtesting script used to validate the strategy logic against historical data.
- `dr_manhattan/exchanges/polymarket.py`: wrapper for polymarket interaction.

## Pending Tasks for Next Agent

### 1. Live Trading Verification
- The script has been heavily tested in **Dry Run** mode.
- **Action**: Run with `--live --amount 5.0` (small amount) to verify:
  - Order creation/submission works (`exchange.create_order`).
  - Position tracking updates correctly.
  - Balances are handled properly.

### 2. Optimization
- **API Efficiency**: The current pagination (Scanning 20 pages) is expensive and slow.
  - **Goal**: Reduce API calls. Potential solution: Binary search for the active time window or smarter filtering if API allows.
  - **Rate Limits**: Monitor for 429 errors.

### 3. Feature Enhancements
- **Dynamic Thresholds**: Move `MIN_PROB` / `MAX_PROB` to command-line arguments (currently constants).
- **Position Exit**: Implement logic to sell positions before market close if profit targets are met.

## How to Run
```bash
# Debug mode (Recommended for first run)
uv run python -u examples/btc_momentum_trader.py --debug

# Live mode
uv run python -u examples/btc_momentum_trader.py --live --amount 10

## Backtest (15m)
- **Real 15m data (preferred):**  
  `uv run python examples/bitcoin_up_or_down_history.py --limit 200 --min-close 5 --pattern 15min --save-graph examples/backtest_results_15m.png`  
  _Current status (2026-01-13): Polymarket API returns **0** matching 15m “Bitcoin Up or Down” markets, so this command will print “No matching markets found.”_
- **Smoke-test with sample data:**  
  `uv run python examples/bitcoin_up_or_down_history.py --test --save-graph examples/backtest_results_test.png`  
  Generates tables in stdout and saves a graph at `examples/backtest_results_test.png`, confirming the pipeline works while waiting for fresh 15m market data.
```
