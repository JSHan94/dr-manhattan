import argparse
import time
import sys
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set

from dr_manhattan import Polymarket
from dr_manhattan.models.market import Market
from dr_manhattan.models.order import OrderSide

# Configuration
MIN_PROB = 0.52
MAX_PROB = 0.60
MIN_MINUTES_TO_CLOSE = 2
REFRESH_MARKETS_INTERVAL = 60
POLL_INTERVAL = 3

active_positions: Dict[str, Set[str]] = {}

def main():
    parser = argparse.ArgumentParser(
        description="Auto-trade Bitcoin Up/Down markets based on Momentum Strategy."
    )
    parser.add_argument(
        "--amount", type=float, default=5.0,
        help="Amount to bet per trade in USDC (default: 5.0).",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="ENABLE REAL TRADING. If not set, runs in DRY RUN mode.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show detailed debug logs.",
    )
    args = parser.parse_args()

    mode_str = "🚀 LIVE TRADING" if args.live else "🛡️ DRY RUN (Simulation)"
    print("=" * 60)
    print(f"BITCOIN MOMENTUM TRADER | {mode_str}")
    print(f"Strategy: Buy when Ask Price in [{MIN_PROB:.2f}, {MAX_PROB:.2f}]")
    print(f"Bet Size: {args.amount} USDC")
    print("=" * 60)

    if args.live:
        print("⚠️  WARNING: Real money will be used!")
        print("Press Ctrl+C to stop immediately.")
        time.sleep(3)

    # Initialize Exchange
    try:
        # Load credentials from .env automatically
        exchange = Polymarket()
        print(f"Connected to Polymarket.")
    except Exception as e:
        print(f"Failed to connect: {e}")
        print("Did you set POLYMARKET_PRIVATE_KEY / POLYMARKET_API_KEY in .env?")
        sys.exit(1)

    target_markets: List[Market] = []
    last_market_refresh = 0

    while True:
        try:
            now = time.time()
            if now - last_market_refresh > REFRESH_MARKETS_INTERVAL:
                target_markets = find_open_btc_markets(exchange, args)
                last_market_refresh = now
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monitoring {len(target_markets)} active markets...")

            if not target_markets:
                time.sleep(10)
                continue

            for market in target_markets:
                check_and_trade(exchange, market, args)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopping trader...")
            break
        except Exception as e:
            print(f"\n! Error in main loop: {e}")
            time.sleep(5)

def find_open_btc_markets(exchange: Polymarket, args) -> List[Market]:
    valid_markets = []
    now_utc = datetime.now(timezone.utc)
    limit = 100
    offset = 0
    max_pages = 10 # Check up to 1000 items
    
    # Regex
    time_pattern = re.compile(
            r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<start_t>\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(?P<end_t>\d{1,2}:\d{2}\s*[AP]M)',
            re.IGNORECASE
    )

    if args.debug:
        print(f"DEBUG: Searching active markets with pagination (deep search)...")

    try:
        for page in range(max_pages):
            raw_markets = exchange.search_markets(
                query="bitcoin up or down",
                limit=limit,
                offset=offset,
                closed=False
            )
            
            if not raw_markets:
                break
                
            batch_active_count = 0
            
            for m in raw_markets:
                if not m.close_time or m.close_time <= now_utc:
                    continue
                    
                if "bitcoin up or down" not in m.question.lower():
                    continue

                mins_left = (m.close_time - now_utc).total_seconds() / 60
                
                # Check active logic
                match = time_pattern.search(m.question)
                is_future = False
                if match:
                    try:
                        is_15m = "15" in m.question or re.search(r'\d{1,2}:\d{2}-\d{1,2}:\d{2}', m.question)
                        is_1h = "Candle" in m.question
                        
                        start_time = None
                        if is_1h:
                            start_time = m.close_time - timedelta(minutes=60)
                        elif match:
                            start_time = m.close_time - timedelta(minutes=20)
                        else:
                            # 1 hour fallback
                            start_time = m.close_time - timedelta(minutes=60)

                        if now_utc < start_time:
                             is_future = True
                    except:
                        pass
                
                if is_future:
                    continue

                if mins_left < MIN_MINUTES_TO_CLOSE:
                    if args.debug: print(f"SKIP (Ending): {m.question} ({mins_left:.1f}m left)")
                    continue

                if mins_left > 120:
                     if args.debug: print(f"SKIP (Too long): {m.question} ({mins_left:.1f}m left)")
                     continue

                if not any(vm.id == m.id for vm in valid_markets):
                    valid_markets.append(m)
                    batch_active_count += 1
            
            if args.debug and batch_active_count > 0:
                print(f"DEBUG: Page {page} (offset {offset}): Found {batch_active_count} active candidates.")
            
            offset += limit
            
        valid_markets.sort(key=lambda x: x.close_time)
        return valid_markets

    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []

def check_and_trade(exchange: Polymarket, market: Market, args):
    if market.id not in active_positions:
        active_positions[market.id] = set()

    if len(active_positions[market.id]) > 0:
        return

    token_ids = market.metadata.get("clobTokenIds") or market.metadata.get("clob_token_ids")
    if not token_ids or len(token_ids) < 2:
        try:
            token_ids = exchange.fetch_token_ids(market.id)
            if not token_ids:
                return
        except:
            return

    outcomes = market.outcomes
    if len(outcomes) < 2:
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking: {market.question}")
    for idx, outcome in enumerate(outcomes[:2]):
        token_id = token_ids[idx]
        
        try:
            # Fetch Orderbook to get Best Ask
            # Note: We need the CLOB token ID
            book = exchange.get_orderbook(token_id)
            
            if not book or not book.get("asks"):
                if args.debug:
                    print(f"   x {outcome}: No asks available")
                continue
                
            best_ask_price = float(book["asks"][0]["price"])
            prob_percent = best_ask_price * 100
            
            # Log status
            in_range = MIN_PROB <= best_ask_price <= MAX_PROB
            status_icon = "✅" if in_range else "gray"
            if in_range:
                status_msg = f"MATCH! ({MIN_PROB:.2f} <= {best_ask_price:.3f} <= {MAX_PROB:.2f})"
            else:
                status_msg = f"Skip ({prob_percent:.1f}%)"
                
            print(f"   > {outcome}: {best_ask_price:.3f} ({prob_percent:.1f}%) -> {status_msg}")

            if in_range:
                print(f"🎯 SIGNAL FOUND: {market.question}")
                print(f"   Outcome: {outcome} | Price: {best_ask_price:.3f} | Trend: {best_ask_price*100:.1f}% Prob")
                
                execute_trade(exchange, market, token_id, outcome, best_ask_price, args)
                break 

        except Exception as e:
            if args.debug:
                print(f"Error checking price for {market.id}: {e}")

def execute_trade(exchange: Polymarket, market: Market, token_id: str, outcome: str, price: float, args):
    if outcome in active_positions[market.id]:
        return

    cost = args.amount
    size = cost / price
    size = round(size, 2)
    
    if size < 0.1:
        print(f"   ! Trade size too small ({size}), skipping.")
        return

    if args.live:
        print(f"🚀 EXECUTING BUY: {size} shares of '{outcome}' @ {price:.3f}...")
        try:
            # Polymarket wrapper create_order signature:
            # create_order(market_id, outcome, side, price, size, params)
            resp = exchange.create_order(
                market_id=market.id,
                outcome=outcome,
                side=OrderSide.BUY,
                price=price,
                size=size,
                params={"token_id": token_id}
            )
            # Response is Order object
            print(f"✅ ORDER SENT: ID {resp.id}")
            active_positions[market.id].add(outcome)
            
        except Exception as e:
            print(f"❌ TRADE FAILED: {e}")
    else:
        print(f"🛡️ [DRY RUN] Would BUY: {size} shares of '{outcome}' @ {price:.3f} (Cost: ${cost:.2f})")
        active_positions[market.id].add(outcome)

if __name__ == "__main__":
    main()
