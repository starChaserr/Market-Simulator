# Market Simulator

A dependency-light synthetic exchange simulator for testing order handling, limit-order-book behavior, accounts, P/L, fees, risk checks, and market scenarios.

The simulator runs a single-symbol market with internal participants and an API surface for external strategies. It is not a market predictor and does not connect to live broker or exchange data.

## Features

- Central limit order book with price-time priority
- Market, limit, stop, and stop-limit orders
- `gtc`, `ioc`, and `fok` time-in-force support
- Post-only limit order rejection
- Order cancel and order status APIs
- API user accounts with cash, inventory, realized P/L, unrealized P/L, fees, equity, and drawdown
- Maker/taker fee simulation
- Risk checks for max order size, max position, shorting, and buying power
- Open-order buying-power and position reservation
- API rate limits with per-second and per-minute request buckets
- Institutional, high-frequency market-making, random, and background-liquidity agents
- Volatility clustering, order-flow impact, finite latent liquidity, queue cancellation, and stress-sensitive depth
- Scenario modes for calm, trending, high volatility, flash crash, liquidity drought, mean reversion, and news shocks
- Live chaos control that changes volatility and liquidity without resetting agent accounts
- Deterministic runs with `--seed`
- Browser UI for charting, depth, trade tape, API accounts, manual orders, and regime resets
- JSON REST API and Server-Sent Events stream

## Quick Start

```bash
python3 main.py
```

The default market clock advances once per second with small scheduling jitter. Override it with `--tick-interval` when running accelerated tests.

The API listens on:

```text
http://127.0.0.1:8000
```

Open the simulator UI at:

```text
http://127.0.0.1:8000/
```

## Scenarios

```bash
python3 main.py --scenario calm
python3 main.py --scenario high_volatility
python3 main.py --scenario trending_up
python3 main.py --scenario trending_down
python3 main.py --scenario flash_crash
python3 main.py --scenario liquidity_drought
python3 main.py --scenario mean_reverting
python3 main.py --scenario news_shock
```

Reproducible run:

```bash
python3 main.py --scenario flash_crash --seed 42
```

Optional JSON config override:

```bash
python3 main.py --config config.local.json
```

## API Examples

Health:

```bash
curl -sS http://127.0.0.1:8000/api/health
```

Create or fetch an account:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/accounts \
  -H 'Content-Type: application/json' \
  -d '{"user":"MyStrategy","starting_cash":1000000}'
```

Submit an order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/order \
  -H 'Content-Type: application/json' \
  -d '{"side":"buy","quantity":1000,"order_type":"market","user":"MyStrategy"}'
```

List orders:

```bash
curl -sS 'http://127.0.0.1:8000/api/orders?user=MyStrategy'
```

Cancel an order:

```bash
curl -sS -X DELETE 'http://127.0.0.1:8000/api/orders/ord_xxxxx?user=MyStrategy'
```

Account state:

```bash
curl -sS 'http://127.0.0.1:8000/api/account?user=MyStrategy'
```

Full simulator state:

```bash
curl -sS http://127.0.0.1:8000/api/state
```

Change regime and reset:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/regime \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"news_shock","seed":7331}'
```

Change live chaos without resetting:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/chaos \
  -H 'Content-Type: application/json' \
  -d '{"level":75,"source":"operator"}'
```

Run the guarded deterministic chaos operator:

```bash
python3 chaos_controller.py --seed 7331
```

Live stream:

```bash
curl -N http://127.0.0.1:8000/api/stream
```

## Testing

```bash
python3 -m unittest discover -s tests
```

## Project Layout

```text
main.py                  API server and route handling
chaos_controller.py      Guarded live market-chaos operator
example_agents/          Optional API agents for testing the simulator
market_sim/config.py     Defaults and scenario definitions
market_sim/engine.py     Matching engine, order lifecycle, accounts, P/L
market_sim/simulation.py Background simulation loop and internal agent behavior
static/                  Browser simulator UI
tests/                   Simulator unit tests
```

## License

MIT. See `LICENSE`.
