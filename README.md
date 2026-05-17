# Market Simulator

A dependency-light synthetic exchange sandbox for testing automated trading and algo-trading systems.

The project runs a live single-symbol limit order book, internal market participants, REST APIs, Server-Sent Events, and a browser dashboard. It is meant for testing order handling, P/L accounting, strategy plumbing, and market-simulation behavior. It is not a market predictor.

## Features

- Central limit order book with price-time priority
- Market, limit, stop, and stop-limit orders
- `gtc`, `ioc`, and `fok` time-in-force support
- Post-only limit order rejection
- Order cancel and order status endpoints
- API user accounts with starting capital, cash, inventory, realized P/L, unrealized P/L, fees, equity, and drawdown
- Maker/taker fee simulation
- Risk checks for max order size, max position, shorting, and buying power
- Institutional, high-frequency market-making, random, and background-liquidity agents
- Agent action tracking for buy, sell, buy/sell quoting, and hold states
- Randomized internal agent counts and action intervals
- Scenario modes for calm, trending, high volatility, flash crash, liquidity drought, mean reversion, and news shocks
- Long-run stability guards for stale quotes, reference-price drift, and large-order latent liquidity
- Deterministic runs with `--seed`
- REST API, OpenAPI spec, and Server-Sent Events stream
- Live dashboard with candlesticks, volume, order book depth, trade tape, API user P/L, and internal agent activity
- Privacy-friendly display-currency auto-selection from browser locale/timezone

## Quick Start

```bash
python3 main.py
```

Open:

```text
http://127.0.0.1:8000
```

OpenAPI:

```text
http://127.0.0.1:8000/openapi.json
```

## Docker

```bash
docker compose up --build
```

Then open:

```text
http://127.0.0.1:8000
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

## API Usage

Create or fetch an account:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/accounts \
  -H 'Content-Type: application/json' \
  -d '{"user":"MyStrategy","starting_cash":1000000}'
```

Add funds to an API account:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/accounts/fund \
  -H 'Content-Type: application/json' \
  -d '{"user":"MyStrategy","amount":250000}'
```

Submit a market order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/order \
  -H 'Content-Type: application/json' \
  -d '{"side":"buy","quantity":1000,"order_type":"market","user":"MyStrategy"}'
```

Submit a post-only limit order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/order \
  -H 'Content-Type: application/json' \
  -d '{"side":"buy","quantity":500,"order_type":"limit","price":99.5,"post_only":true,"user":"MyStrategy"}'
```

Submit an IOC order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/order \
  -H 'Content-Type: application/json' \
  -d '{"side":"sell","quantity":500,"order_type":"limit","price":101,"time_in_force":"ioc","user":"MyStrategy"}'
```

Submit a stop order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/order \
  -H 'Content-Type: application/json' \
  -d '{"side":"sell","quantity":200,"order_type":"stop","stop_price":98,"user":"MyStrategy"}'
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

Resolve display currency:

```bash
curl -sS 'http://127.0.0.1:8000/api/currency?locale=en-IN&timezone=Asia/Kolkata'
```

Live stream:

```bash
curl -N http://127.0.0.1:8000/api/stream
```

## API User Names

Orders can identify the strategy/model with any of these JSON fields:

- `user`
- `user_name`
- `username`
- `api_user`
- `client`
- `client_id`
- `model`
- `owner`
- `name`

Or headers:

- `X-API-User`
- `X-Client-Name`
- `X-Model-Name`

## Currency Display

The simulator keeps one synthetic price series internally. The dashboard formats that synthetic quote currency based on the browser's locale and time zone, without requesting precise location or calling an external IP-geolocation service. The header currency selector lets users override the detected currency, and that choice is saved in browser local storage.

API clients can use `/api/currency` or send `X-Client-Locale`, `X-Client-Timezone`, or `X-Currency` headers if they want the same preference logic.

## Testing

```bash
python3 -m unittest discover -s tests
```

## Project Layout

```text
main.py                  HTTP server and route handling
example_agents/          Standalone API trading agents and competition runner
market_sim/config.py     Defaults and scenario definitions
market_sim/engine.py     Matching engine, order lifecycle, accounts, P/L
market_sim/simulation.py Background simulation loop and agent behavior
static/                  Dashboard and OpenAPI spec
tests/                   Unit tests
```

## License

MIT. See `LICENSE`.
