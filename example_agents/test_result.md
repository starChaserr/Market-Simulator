# Market Simulation Test Results

Run Date: 2026-05-18 05:32:08
Starting Cash: 10,000
Order Limit: 1,000

## Scenario: calm

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 3.45 |
| AdaptiveEdgeMaker | -52.88 |
| AutoTrader | 122.18 |

**WINNER: AutoTrader**

## Scenario: high_volatility

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 2449.28 |
| AdaptiveEdgeMaker | 4614.43 |
| AutoTrader | 1726.17 |

**WINNER: AdaptiveEdgeMaker**

## Scenario: trending_up

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 806.07 |
| AdaptiveEdgeMaker | 730.36 |
| AutoTrader | 394.38 |

**WINNER: RaiderCore_v3**

## Scenario: trending_down

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 3640.07 |
| AdaptiveEdgeMaker | 4101.49 |
| AutoTrader | 2046.95 |

**WINNER: AdaptiveEdgeMaker**

## Scenario: flash_crash

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 9533.94 |
| AdaptiveEdgeMaker | 5196.41 |
| AutoTrader | 5719.41 |

**WINNER: RaiderCore_v3**

## Scenario: liquidity_drought

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 386.11 |
| AdaptiveEdgeMaker | -1819.96 |
| AutoTrader | -156.32 |

**WINNER: RaiderCore_v3**

## Scenario: mean_reverting

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 117.35 |
| AdaptiveEdgeMaker | -102.46 |
| AutoTrader | 179.10 |

**WINNER: AutoTrader**

## Scenario: news_shock

| Agent | P/L |
| :--- | :--- |
| RaiderCore_v3 | 2166.26 |
| AdaptiveEdgeMaker | 885.31 |
| AutoTrader | -657.52 |

**WINNER: RaiderCore_v3**

## Final Summary

| Scenario | Winner | P/L |
| :--- | :--- | :--- |
| calm | AutoTrader | 122.18 |
| high_volatility | AdaptiveEdgeMaker | 4614.43 |
| trending_up | RaiderCore_v3 | 806.07 |
| trending_down | AdaptiveEdgeMaker | 4101.49 |
| flash_crash | RaiderCore_v3 | 9533.94 |
| liquidity_drought | RaiderCore_v3 | 386.11 |
| mean_reverting | AutoTrader | 179.10 |
| news_shock | RaiderCore_v3 | 2166.26 |
