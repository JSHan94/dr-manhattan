"""
Bitcoin Up or Down Backtesting

Backtest betting strategies on closed Polymarket "Bitcoin Up or Down" markets.
Analyzes win rate and expected value based on entry price thresholds.

Usage:
    uv run python examples/bitcoin_up_or_down_history.py
    uv run python examples/bitcoin_up_or_down_history.py --limit 100 --save-graph results.png
"""

import argparse
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dr_manhattan import Polymarket


# =============================================================================
# Configuration
# =============================================================================

# Market filter pattern (configurable)
# Default: 15-minute intervals starting at :00, :15, :30, :45
MARKET_PATTERNS = {
    "15min": re.compile(
        r"(\d{1,2}):?(00|15|30|45)\s*[AP]M\s*-\s*\d{1,2}:?(15|30|45|00)\s*[AP]M",
        re.IGNORECASE,
    ),
    "any": None,  # Match any Bitcoin Up or Down market
}

# Valid 15-minute interval pairs: start -> end minutes
VALID_15MIN_PAIRS = [(0, 15), (15, 30), (30, 45), (45, 0)]


# =============================================================================
# Market Filtering
# =============================================================================

def is_valid_market(question: str, pattern_name: str = "15min") -> bool:
    """Check if market matches the configured pattern."""
    if not question or "bitcoin up or down" not in question.lower():
        return False
    
    pattern = MARKET_PATTERNS.get(pattern_name)
    if pattern is None:
        return True  # Match any Bitcoin Up or Down market
    
    match = pattern.search(question)
    if not match:
        return False
    
    # For 15min pattern, verify the interval
    if pattern_name == "15min":
        start_min = int(match.group(2))
        end_min = int(match.group(3))
        return (start_min, end_min) in VALID_15MIN_PAIRS
    
    return True


def fetch_closed_markets(
    exchange: Polymarket,
    limit: int,
    min_minutes_since_close: int,
    pattern_name: str = "15min",
) -> List[Any]:
    """Fetch closed Bitcoin Up or Down markets matching the pattern using Gamma API."""
    import requests
    import json
    from dr_manhattan.models.market import Market
    
    print(f"Fetching closed markets (pattern: {pattern_name})...")
    
    # 102175 is the tag ID for "Crypto > 1h" which contains most Up/Down markets
    # We fetch a larger batch to filter relevant ones
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "tag_id": "102175", 
        "limit": str(max(limit * 5, 200)),
        "closed": "true"
    }
    
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        raw_data = resp.json()
    except Exception as e:
        print(f"! Error fetching from Gamma API: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_minutes_since_close)
    
    filtered_markets = []
    
    for item in raw_data:
        question = item.get("question", "")
        
        # Must be "Bitcoin Up or Down"
        if "bitcoin up or down" not in question.lower():
            continue
            
        # Parse close time
        close_time_str = item.get("end_date_iso") or item.get("endDate")
        if not close_time_str:
            continue
            
        try:
            # Handle ISO format with potential Z or offset
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            if close_time.tzinfo is None:
                close_time = close_time.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
            
        # Check if closed long enough
        if close_time > cutoff:
            continue
            
        if not is_valid_market(question, pattern_name):
            continue
            
        # Parse Outcomes
        outcomes = item.get("outcomes", "[\"Up\", \"Down\"]")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = ["Up", "Down"]
                
        # Parse Token IDs
        token_ids = item.get("clobTokenIds") or item.get("clob_token_ids")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except:
                token_ids = []
        
        market = Market(
            id=item.get("id"),
            question=question,
            outcomes=outcomes,
            close_time=close_time,
            # Move non-standard fields to metadata
            metadata={
                "clobTokenIds": token_ids,
                "closed": item.get("closed", True),
                "active": item.get("active", False),
                "market_slug": item.get("market_slug", ""),
                "category": item.get("category", "")
            },
            volume=float(item.get("volume", 0) or 0),
            liquidity=float(item.get("liquidity", 0) or 0),
            prices={}, # Gamma API doesn't allow easy price fetching in list
            tick_size=0.1, # Default
            description=item.get("description", ""),
        )
        filtered_markets.append(market)
    
    # Sort by close time (newest first)
    filtered_markets.sort(key=lambda m: m.close_time or datetime.min, reverse=True)
    
    print(f"Found {len(filtered_markets[:limit])} matching markets")
    return filtered_markets[:limit]


# =============================================================================
# Data Collection
# =============================================================================

def ensure_token_ids(exchange: Polymarket, market) -> bool:
    """Ensure market has token IDs. Returns False if failed."""
    token_ids = market.metadata.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = [token_ids]
    if token_ids:
        return True
    
    try:
        token_ids = exchange.fetch_token_ids(market.id)
        market.metadata["clobTokenIds"] = token_ids
        return True
    except Exception:
        return False


def fetch_price_history_direct(
    market,
    token_id: str,
    fidelity: int = 5,
    minutes_before_close: int = 60,
) -> List[Dict[str, Any]]:
    """
    Fetch price history directly from CLOB API with time range.
    
    Uses startTs/endTs parameters for complete data.
    Returns list of {t: timestamp, p: price} dicts.
    """
    import requests
    
    ct = market.close_time
    if not ct:
        return []
    
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc)
    
    end_ts = int(ct.timestamp())
    start_ts = end_ts - (minutes_before_close * 60)
    
    url = (
        f"https://clob.polymarket.com/prices-history"
        f"?startTs={start_ts}&market={token_id}&fidelity={fidelity}&endTs={end_ts}"
    )
    
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except Exception as e:
        print(f"    ! API error: {e}")
        return []


def fetch_price_history(exchange: Polymarket, market, outcome: str) -> List[Any]:
    """Fetch price history for an outcome. Returns empty list on failure."""
    try:
        return exchange.fetch_price_history(
            market, outcome=outcome, interval="1m", fidelity=60, as_dataframe=False
        )
    except Exception:
        return []


def determine_winner(history: List[Any]) -> Optional[str]:
    """Determine winner based on final price. Returns None if unclear."""
    if not history:
        return None
    final_price = history[-1].price
    if final_price > 0.5:
        return "Up"
    elif final_price < 0.5:
        return "Down"
    return None


# =============================================================================
# Backtesting Logic
# =============================================================================

def collect_all_bets(
    exchange: Polymarket,
    markets: List[Any],
    use_direct_api: bool = True,
    fidelity: int = 5,
) -> List[Dict[str, Any]]:
    """Collect all betting opportunities with entry prices and outcomes."""
    all_bets = []
    
    for idx, market in enumerate(markets, start=1):
        print(f"  [{idx}/{len(markets)}] {market.question[:60]}...")
        
        # Ensure token IDs
        if not ensure_token_ids(exchange, market):
            print(f"    ! Failed to get token IDs, skipping")
            continue
        
        token_ids = market.metadata.get("clobTokenIds", [])
        if not token_ids or len(token_ids) < 2:
            print(f"    ! Insufficient token IDs: {token_ids}")
            continue
        
        # Find Up/Down outcomes and their token indices
        up_idx = down_idx = None
        for i, o in enumerate(market.outcomes):
            if o.lower() == "up":
                up_idx = i
            elif o.lower() == "down":
                down_idx = i
        
        if up_idx is None or down_idx is None:
            print(f"    ! Could not find Up/Down outcomes. Available: {market.outcomes}")
            continue
        
        # Fetch price history using direct API or library
        if use_direct_api:
            up_token = token_ids[up_idx]
            down_token = token_ids[down_idx]
            # print(f"    Fetching history for tokens: {up_token[:10]}... / {down_token[:10]}...")
            
            history_up_raw = fetch_price_history_direct(market, up_token, fidelity=fidelity)
            history_down_raw = fetch_price_history_direct(market, down_token, fidelity=fidelity)
            
            if not history_up_raw:
                print(f"    ! No price history (direct API), skipping. Token: {up_token}")
                continue
            
            # Determine winner from final price
            final_price = history_up_raw[-1]["p"]
            winner = "Up" if final_price > 0.5 else "Down" if final_price < 0.5 else None
            
            if not winner:
                print(f"    ! Could not determine winner, skipping")
                continue
            
            print(f"    Winner: {winner}, Points: {len(history_up_raw)}, Price range: {min(h['p'] for h in history_up_raw):.3f}-{max(h['p'] for h in history_up_raw):.3f}")
            
            # Collect bets from each time point
            for i, h in enumerate(history_up_raw):
                up_price = h["p"]
                down_price = history_down_raw[i]["p"] if i < len(history_down_raw) else (1.0 - up_price)
                
                # Calculate seconds to close
                end_ts = history_up_raw[-1]["t"]
                seconds_to_close = end_ts - h["t"]
                minutes_to_close = seconds_to_close / 60
                
                all_bets.append({
                    "market_id": market.id,
                    "outcome": "Up",
                    "entry_price": up_price,
                    "won": 1 if winner == "Up" else 0,
                    "profit": (1.0 - up_price) if winner == "Up" else -up_price,
                    "minutes_to_close": minutes_to_close,
                    "price_deviation": abs(up_price - 0.5),
                    "winner": winner,
                })
                
                all_bets.append({
                    "market_id": market.id,
                    "outcome": "Down",
                    "entry_price": down_price,
                    "won": 1 if winner == "Down" else 0,
                    "profit": (1.0 - down_price) if winner == "Down" else -down_price,
                    "minutes_to_close": minutes_to_close,
                    "price_deviation": abs(down_price - 0.5),
                    "winner": winner,
                })
        else:
            # Use library method (fallback)
            history_up = fetch_price_history(exchange, market, "Up")
            history_down = fetch_price_history(exchange, market, "Down")
            
            if not history_up:
                print(f"    ! No price history, skipping")
                continue
            
            winner = determine_winner(history_up)
            if not winner:
                print(f"    ! Could not determine winner, skipping")
                continue
            
            print(f"    Winner: {winner}, Points: {len(history_up)}")
            
            total_points = len(history_up)
            for i in range(total_points):
                up_price = history_up[i].price
                down_price = history_down[i].price if i < len(history_down) else 0.5
                minutes_to_close = total_points - 1 - i
                
                all_bets.append({
                    "market_id": market.id,
                    "outcome": "Up",
                    "entry_price": up_price,
                    "won": 1 if winner == "Up" else 0,
                    "profit": (1.0 - up_price) if winner == "Up" else -up_price,
                    "minutes_to_close": minutes_to_close,
                    "price_deviation": abs(up_price - 0.5),
                    "winner": winner,
                })
                
                all_bets.append({
                    "market_id": market.id,
                    "outcome": "Down",
                    "entry_price": down_price,
                    "won": 1 if winner == "Down" else 0,
                    "profit": (1.0 - down_price) if winner == "Down" else -down_price,
                    "minutes_to_close": minutes_to_close,
                    "price_deviation": abs(down_price - 0.5),
                    "winner": winner,
                })
    
    return all_bets


def backtest_threshold_strategy(
    all_bets: List[Dict[str, Any]],
    thresholds: List[float] = None,
) -> Dict[float, Dict[str, Any]]:
    """
    Backtest: Bet when price >= threshold.
    
    Returns results for each threshold level.
    """
    if thresholds is None:
        thresholds = [0.50, 0.505, 0.51, 0.52, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    
    results = {}
    
    for threshold in thresholds:
        bets = [b for b in all_bets if b["entry_price"] >= threshold]
        
        if not bets:
            results[threshold] = {
                "bet_count": 0, "win_count": 0, "win_rate": 0,
                "avg_ev": 0, "total_profit": 0, "avg_entry_price": 0,
            }
            continue
        
        bet_count = len(bets)
        win_count = sum(b["won"] for b in bets)
        total_profit = sum(b["profit"] for b in bets)
        avg_entry = sum(b["entry_price"] for b in bets) / bet_count
        
        results[threshold] = {
            "bet_count": bet_count,
            "win_count": win_count,
            "win_rate": win_count / bet_count,
            "avg_ev": total_profit / bet_count,
            "total_profit": total_profit,
            "avg_entry_price": avg_entry,
            "required_win_rate": avg_entry,
            "edge": (win_count / bet_count) - avg_entry,
        }
    
    return results


def backtest_price_buckets(
    all_bets: List[Dict[str, Any]],
    bucket_size: float = 0.005,  # 0.5%
    min_price: float = 0.50,
    max_price: float = 0.95,
) -> Dict[float, Dict[str, Any]]:
    """
    Backtest: Analyze each price bucket separately.
    
    Returns win rate and EV for each price range.
    """
    results = {}
    
    price = min_price
    while price < max_price:
        bucket_bets = [
            b for b in all_bets
            if price <= b["entry_price"] < price + bucket_size
        ]
        
        if bucket_bets:
            bet_count = len(bucket_bets)
            win_count = sum(b["won"] for b in bucket_bets)
            total_profit = sum(b["profit"] for b in bucket_bets)
            avg_entry = sum(b["entry_price"] for b in bucket_bets) / bet_count
            
            results[round(price * 100, 1)] = {
                "bet_count": bet_count,
                "win_count": win_count,
                "win_rate": win_count / bet_count,
                "avg_ev": total_profit / bet_count,
                "total_profit": total_profit,
                "avg_entry_price": avg_entry,
                "required_win_rate": avg_entry,
                "edge": (win_count / bet_count) - avg_entry,
            }
        
        price += bucket_size
    
    return results


def backtest_time_based(
    all_bets: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """
    Backtest: Analyze win rate and EV by minutes before close.
    
    Shows how entry timing affects profitability.
    """
    from collections import defaultdict
    
    # Group by minutes to close
    by_time = defaultdict(list)
    for bet in all_bets:
        mins = bet.get("minutes_to_close", 0)
        by_time[mins].append(bet)
    
    results = {}
    for mins in sorted(by_time.keys()):
        bets = by_time[mins]
        bet_count = len(bets)
        win_count = sum(b["won"] for b in bets)
        total_profit = sum(b["profit"] for b in bets)
        avg_entry = sum(b["entry_price"] for b in bets) / bet_count
        avg_deviation = sum(b.get("price_deviation", 0) for b in bets) / bet_count
        
        results[mins] = {
            "bet_count": bet_count,
            "win_count": win_count,
            "win_rate": win_count / bet_count,
            "avg_ev": total_profit / bet_count,
            "total_profit": total_profit,
            "avg_entry_price": avg_entry,
            "avg_deviation": avg_deviation,
        }
    
    return results


def backtest_follow_momentum(
    all_bets: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Backtest: Follow the momentum (bet on the side with higher price).
    
    Strategy: If Up price > 50%, bet Up. If Down price > 50%, bet Down.
    Analyzes at different deviation thresholds.
    """
    results = {}
    
    # Different deviation thresholds to test
    for dev_threshold in [0.0, 0.005, 0.01, 0.02, 0.05, 0.10, 0.15, 0.20]:
        # Filter bets where we bet on the favored side (price > 50%)
        favored_bets = [
            b for b in all_bets
            if (b["outcome"] == "Up" and b["entry_price"] > 0.5 + dev_threshold) or
               (b["outcome"] == "Down" and b["entry_price"] > 0.5 + dev_threshold)
        ]
        
        if not favored_bets:
            results[dev_threshold] = {
                "bet_count": 0, "win_count": 0, "win_rate": 0, "avg_ev": 0, "total_profit": 0,
            }
            continue
        
        bet_count = len(favored_bets)
        win_count = sum(b["won"] for b in favored_bets)
        total_profit = sum(b["profit"] for b in favored_bets)
        avg_entry = sum(b["entry_price"] for b in favored_bets) / bet_count
        
        results[dev_threshold] = {
            "bet_count": bet_count,
            "win_count": win_count,
            "win_rate": win_count / bet_count,
            "avg_ev": total_profit / bet_count,
            "total_profit": total_profit,
            "avg_entry_price": avg_entry,
            "required_win_rate": avg_entry,
            "edge": (win_count / bet_count) - avg_entry,
        }
    
    return results


def find_optimal_entry(
    all_bets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Find the optimal entry conditions based on backtested data."""
    
    # Analyze by time + deviation combination
    from collections import defaultdict
    
    combos = defaultdict(list)
    for bet in all_bets:
        mins = bet.get("minutes_to_close", 0)
        dev = bet.get("price_deviation", 0)
        
        # Only consider bets where we're betting on the favored side
        if (bet["outcome"] == "Up" and bet["entry_price"] > 0.5) or \
           (bet["outcome"] == "Down" and bet["entry_price"] > 0.5):
            # Bucket: (time_bucket, deviation_bucket)
            time_bucket = mins // 3  # 3-minute buckets
            dev_bucket = round(dev * 100, 1)  # 0.1% buckets
            combos[(time_bucket, dev_bucket)].append(bet)
    
    # Find best combo
    best_combo = None
    best_ev = float("-inf")
    
    for (time_b, dev_b), bets in combos.items():
        if len(bets) < 10:  # Minimum sample size
            continue
        
        avg_ev = sum(b["profit"] for b in bets) / len(bets)
        win_rate = sum(b["won"] for b in bets) / len(bets)
        
        if avg_ev > best_ev:
            best_ev = avg_ev
            best_combo = {
                "time_bucket": time_b,
                "deviation_bucket": dev_b,
                "minutes_range": f"{time_b*3}-{time_b*3+2}",
                "deviation_pct": dev_b,
                "bet_count": len(bets),
                "win_rate": win_rate,
                "avg_ev": avg_ev,
            }
    
    return best_combo


# =============================================================================
# Output
# =============================================================================

def print_threshold_results(results: Dict[float, Dict[str, Any]]) -> None:
    """Print threshold strategy backtest results."""
    print("\n" + "=" * 85)
    print("BACKTEST: Threshold Strategy (Bet when price >= X)")
    print("=" * 85)
    print(f"{'Threshold':>10} {'Bets':>10} {'Wins':>8} {'Win%':>8} {'Edge':>10} {'Avg EV':>12} {'Total P/L':>12}")
    print("-" * 85)
    
    for threshold in sorted(results.keys()):
        data = results[threshold]
        if data["bet_count"] == 0:
            print(f"{threshold*100:>9.1f}% {'N/A':>10} {'N/A':>8} {'N/A':>8} {'N/A':>10} {'N/A':>12} {'N/A':>12}")
        else:
            edge = data.get("edge", 0) * 100
            print(
                f"{threshold*100:>9.1f}% {data['bet_count']:>10} {data['win_count']:>8} "
                f"{data['win_rate']*100:>7.1f}% {edge:>+9.1f}% "
                f"{data['avg_ev']:>+11.4f} {data['total_profit']:>+11.2f}"
            )
    
    print("-" * 85)


def print_bucket_results(results: Dict[float, Dict[str, Any]]) -> None:
    """Print price bucket analysis results."""
    print("\n" + "=" * 90)
    print("BACKTEST: Price Bucket Analysis")
    print("=" * 90)
    print(f"{'Price Range':>14} {'Bets':>10} {'Wins':>8} {'Win%':>8} {'Required':>10} {'Edge':>10} {'Avg EV':>12}")
    print("-" * 90)
    
    for price_pct in sorted(results.keys()):
        data = results[price_pct]
        next_price = price_pct + 0.5
        edge = data.get("edge", 0) * 100
        
        print(
            f"{price_pct:>5.1f}-{next_price:<5.1f}%  {data['bet_count']:>10} {data['win_count']:>8} "
            f"{data['win_rate']*100:>7.1f}% {data['required_win_rate']*100:>9.1f}% "
            f"{edge:>+9.1f}% {data['avg_ev']:>+11.4f}"
        )
    
    print("-" * 90)


def print_time_based_results(results: Dict[int, Dict[str, Any]]) -> None:
    """Print time-based analysis results."""
    print("\n" + "=" * 85)
    print("BACKTEST: Entry Timing Analysis (by minutes to close)")
    print("=" * 85)
    print(f"{'Mins to Close':>14} {'Bets':>10} {'Win%':>8} {'Avg Dev':>10} {'Avg EV':>12} {'Total P/L':>12}")
    print("-" * 85)
    
    for mins in sorted(results.keys(), reverse=True):
        data = results[mins]
        print(
            f"T-{mins:>2} min       {data['bet_count']:>10} "
            f"{data['win_rate']*100:>7.1f}% {data['avg_deviation']*100:>9.2f}% "
            f"{data['avg_ev']:>+11.4f} {data['total_profit']:>+11.2f}"
        )
    
    print("-" * 85)


def print_momentum_results(results: Dict[float, Dict[str, Any]]) -> None:
    """Print momentum strategy results."""
    print("\n" + "=" * 90)
    print("BACKTEST: Momentum Strategy (bet on favored side when price > 50% + threshold)")
    print("=" * 90)
    print(f"{'Min Deviation':>14} {'Bets':>10} {'Wins':>8} {'Win%':>8} {'Edge':>10} {'Avg EV':>12} {'Total P/L':>12}")
    print("-" * 90)
    
    for threshold in sorted(results.keys()):
        data = results[threshold]
        if data["bet_count"] == 0:
            print(f">{threshold*100:>5.1f}%       {'N/A':>10} {'N/A':>8} {'N/A':>8} {'N/A':>10} {'N/A':>12} {'N/A':>12}")
        else:
            edge = data.get("edge", 0) * 100
            print(
                f">{threshold*100:>5.1f}%       {data['bet_count']:>10} {data['win_count']:>8} "
                f"{data['win_rate']*100:>7.1f}% {edge:>+9.1f}% "
                f"{data['avg_ev']:>+11.4f} {data['total_profit']:>+11.2f}"
            )
    
    print("-" * 90)


def print_optimal_entry(optimal: Dict[str, Any]) -> None:
    """Print optimal entry finding results."""
    print("\n" + "=" * 60)
    print("OPTIMAL ENTRY POINT")
    print("=" * 60)
    
    if optimal:
        print(f"  Minutes before close: {optimal['minutes_range']} min")
        print(f"  Price deviation:      >{optimal['deviation_pct']:.1f}%")
        print(f"  Sample size:          {optimal['bet_count']} bets")
        print(f"  Win rate:             {optimal['win_rate']*100:.1f}%")
        print(f"  Average EV:           {optimal['avg_ev']:+.4f}")
    else:
        print("  ! Not enough data to determine optimal entry")
    
    print("=" * 60)


def plot_results(bucket_results: Dict[float, Dict[str, Any]], output_path: str) -> None:
    """Generate matplotlib visualization of backtest results."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("! matplotlib not installed. Run: pip install matplotlib")
        return
    
    if not bucket_results:
        print("! No data to plot")
        return
    
    prices = sorted(bucket_results.keys())
    win_rates = [bucket_results[p]["win_rate"] * 100 for p in prices]
    avg_evs = [bucket_results[p]["avg_ev"] for p in prices]
    edges = [bucket_results[p].get("edge", 0) * 100 for p in prices]
    bet_counts = [bucket_results[p]["bet_count"] for p in prices]
    required_rates = [bucket_results[p]["required_win_rate"] * 100 for p in prices]
    
    fig, axes = plt.subplots(3, 1, figsize=(12, 10))
    fig.suptitle("Bitcoin Up or Down: Backtest Results", fontsize=14, fontweight='bold')
    
    # Win Rate
    ax1 = axes[0]
    ax1.bar(prices, win_rates, width=0.4, alpha=0.7, color='steelblue', label='Actual Win Rate')
    ax1.plot(prices, required_rates, 'r--', linewidth=2, label='Required (breakeven)')
    ax1.set_xlabel('Entry Price (%)')
    ax1.set_ylabel('Win Rate (%)')
    ax1.set_title('Win Rate by Entry Price')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    for i, (p, c) in enumerate(zip(prices, bet_counts)):
        ax1.annotate(f'n={c}', (p, win_rates[i]), xytext=(0, 5),
                    textcoords="offset points", ha='center', fontsize=7, alpha=0.7)
    
    # Expected Value
    ax2 = axes[1]
    colors = ['green' if ev > 0 else 'red' for ev in avg_evs]
    ax2.bar(prices, avg_evs, width=0.4, alpha=0.7, color=colors)
    ax2.axhline(y=0, color='black', linestyle='-', linewidth=1)
    ax2.set_xlabel('Entry Price (%)')
    ax2.set_ylabel('Avg EV (profit per bet)')
    ax2.set_title('Expected Value by Entry Price')
    ax2.grid(True, alpha=0.3)
    
    # Edge
    ax3 = axes[2]
    edge_colors = ['green' if e > 0 else 'red' for e in edges]
    ax3.bar(prices, edges, width=0.4, alpha=0.7, color=edge_colors)
    ax3.axhline(y=0, color='black', linestyle='-', linewidth=1)
    ax3.set_xlabel('Entry Price (%)')
    ax3.set_ylabel('Edge (%) = Win Rate - Required')
    ax3.set_title('Edge by Entry Price (Positive = Profitable)')
    ax3.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\nGraph saved to: {output_path}")
    plt.close()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Backtest betting strategies on Bitcoin Up or Down markets.'
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Number of markets to analyze (default: 50).",
    )
    parser.add_argument(
        "--min-close", type=int, default=5,
        help="Only include markets closed at least X minutes ago (default: 5).",
    )
    parser.add_argument(
        "--pattern", type=str, default="15min", choices=["15min", "any"],
        help="Market filter pattern: '15min' or 'any' (default: 15min).",
    )
    parser.add_argument(
        "--save-graph", type=str, default=None,
        help="Save graph to file (e.g., 'results.png').",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run with sample market data for testing.",
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("BITCOIN UP OR DOWN BACKTEST")
    print("=" * 60)
    
    if args.test:
        # Test mode: use sample market token
        print("\n[TEST MODE] Using sample market data...")
        all_bets = collect_sample_bets()
    else:
        exchange = Polymarket({"timeout": 30})
        
        # Fetch markets
        markets = fetch_closed_markets(
            exchange, args.limit, args.min_close, args.pattern
        )
        
        if not markets:
            print("No matching markets found.")
            print("\nTip: Run with --test flag to use sample data:")
            print("  uv run python examples/bitcoin_up_or_down_history.py --test")
            return
        
        # Collect all betting data
        print("\nCollecting price data from markets...")
        all_bets = collect_all_bets(exchange, markets)
    
    if not all_bets:
        print("No betting data collected.")
        return
    
    print(f"\nTotal betting opportunities: {len(all_bets)}")
    
    # Run backtests
    print("\nRunning backtests...")
    
    # 1. Threshold strategy
    threshold_results = backtest_threshold_strategy(all_bets)
    print_threshold_results(threshold_results)
    
    # 2. Price bucket analysis
    bucket_results = backtest_price_buckets(all_bets)
    print_bucket_results(bucket_results)
    
    # 3. Time-based analysis
    time_results = backtest_time_based(all_bets)
    print_time_based_results(time_results)
    
    # 4. Momentum strategy
    momentum_results = backtest_follow_momentum(all_bets)
    print_momentum_results(momentum_results)
    
    # 5. Find optimal entry
    optimal = find_optimal_entry(all_bets)
    print_optimal_entry(optimal)
    
    # Generate graph
    if args.save_graph:
        print("\nGenerating graph...")
        plot_results(bucket_results, args.save_graph)
    
    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)


def collect_sample_bets() -> List[Dict[str, Any]]:
    """Collect betting data from sample market for testing."""
    import requests
    
    # Sample market token (known working example)
    token_id = "87045024342651348796350372863203337800057585097171187262503683342084250795812"
    start_ts = 1768174283
    end_ts = 1768260683
    
    url = f"https://clob.polymarket.com/prices-history?startTs={start_ts}&market={token_id}&fidelity=5&endTs={end_ts}"
    
    print(f"  Fetching sample data from CLOB API...")
    resp = requests.get(url, timeout=30)
    history = resp.json().get("history", [])
    
    print(f"  Got {len(history)} data points")
    
    if not history:
        return []
    
    # Determine winner from final price
    final_price = history[-1]["p"]
    winner = "Up" if final_price > 0.5 else "Down"
    print(f"  Winner: {winner} (final Up price: {final_price:.4f})")
    
    # Collect bets
    all_bets = []
    end_time = history[-1]["t"]
    
    for h in history:
        up_price = h["p"]
        down_price = 1.0 - up_price
        
        seconds_to_close = end_time - h["t"]
        minutes_to_close = seconds_to_close / 60
        
        # Up bet
        all_bets.append({
            "market_id": "sample",
            "outcome": "Up",
            "entry_price": up_price,
            "won": 1 if winner == "Up" else 0,
            "profit": (1.0 - up_price) if winner == "Up" else -up_price,
            "minutes_to_close": minutes_to_close,
            "price_deviation": abs(up_price - 0.5),
            "winner": winner,
        })
        
        # Down bet
        all_bets.append({
            "market_id": "sample",
            "outcome": "Down",
            "entry_price": down_price,
            "won": 1 if winner == "Down" else 0,
            "profit": (1.0 - down_price) if winner == "Down" else -down_price,
            "minutes_to_close": minutes_to_close,
            "price_deviation": abs(down_price - 0.5),
            "winner": winner,
        })
    
    return all_bets


if __name__ == "__main__":
    main()
