#!/bin/bash
SCENARIOS=("calm" "high_volatility" "trending_up" "trending_down" "flash_crash" "liquidity_drought" "mean_reverting" "news_shock")

for scenario in "${SCENARIOS[@]}"; do
    echo "Starting scenario: $scenario"
    python3 main.py --scenario $scenario > /dev/null 2>&1 &
    SERVER_PID=$!
    sleep 5
    
    python3 example_agents/raider_core.py RaiderCore > /dev/null 2>&1 &
    RAIDER_PID=$!
    
    python3 example_agents/adaptive_edge_maker.py AdaptiveEdgeMaker > /dev/null 2>&1 &
    ADAPTIVE_PID=$!
    
    python3 example_agents/auto_trader.py AutoTrader > /dev/null 2>&1 &
    AUTO_PID=$!
    
    # Wait for RaiderCore to finish 300 orders or 3 minutes
    for i in {1..180}; do
        ORDERS=$(curl -s "http://127.0.0.1:8000/api/account?user=RaiderCore" | grep -o '"orders":[0-9]*' | cut -d: -f2)
        ORDERS=${ORDERS:-0}
        if [ "$ORDERS" -ge 300 ]; then
            echo "RaiderCore finished $ORDERS orders"
            break
        fi
        sleep 1
    done
    
    echo "Results for $scenario:"
    curl -s http://127.0.0.1:8000/api/accounts
    
    kill $RAIDER_PID $ADAPTIVE_PID $AUTO_PID $SERVER_PID
    sleep 2
    pkill -9 -f main.py
    pkill -9 -f raider_core.py
    pkill -9 -f adaptive_edge_maker.py
    pkill -9 -f auto_trader.py
done
