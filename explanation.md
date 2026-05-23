# Market Simulator Explanation

This project is a self-contained market simulator with a Python HTTP API, a live background simulation loop, a browser UI, and open-source project scaffolding. It models one synthetic symbol, `SIM`, using a limit order book and several classes of agents that submit orders into the same market as external API users.

The implementation avoids external dependencies. Run it with:

```bash
python3 main.py
```

Then open: `http://127.0.0.1:8000`

## Main Files

- `main.py`: starts the HTTP server, serves the UI, and exposes JSON API endpoints.
- `market_sim/config.py`: default settings and scenario presets.
- `market_sim/engine.py`: contains the order book, matching engine, trade execution, market data snapshots, and account tracking.
- `market_sim/simulation.py`: contains the background simulation loop and the agent strategies.
- `static/index.html`: dashboard layout.
- `static/styles.css`: dashboard styling.
- `static/app.js`: UI polling, chart drawing, order submission, and table rendering.
- `static/openapi.json`: OpenAPI specification.
- `market_sim/currency.py`: privacy-friendly currency inference from locale/timezone.
- `tests/`: unit tests for matching, order lifecycle, and accounting.

## Market Model

The simulator uses a single-symbol central limit order book:

- Buy limit orders rest on the bid side.
- Sell limit orders rest on the ask side.
- Market orders immediately consume available opposite-side liquidity.
- Crossing limit orders execute immediately up to their limit price.
- Resting limit orders are sorted by price priority, then time priority.
- `gtc`, `ioc`, and `fok` time-in-force rules are supported.
- Post-only limit orders are rejected if they would immediately cross.
- Stop and stop-limit orders rest as pending triggers until the last traded price crosses the stop price.

For example, a buy limit order at `100.25` can match any resting sell order priced at `100.25` or lower. A sell limit order at `99.80` can match any resting buy order priced at `99.80` or higher.

Each trade updates:

- last traded price
- per-tick open, high, low, and close candle values
- session high and low
- total volume
- recent trade tape
- buyer and seller inventory
- buyer and seller cash
- per-agent filled volume
- marked-to-market P/L for each API user
- maker/taker fees
- realized and unrealized P/L

## Large Market Orders And Slippage

The API is designed to accept any positive quantity. Real order books can run out of visible depth, so the simulator includes a "latent liquidity" model for market orders. If a market order consumes all visible opposite-side orders and still has remaining quantity, the engine fills the rest against generated external liquidity at worse prices.

This creates slippage:

- A large buy order lifts visible asks first, then receives increasingly higher latent prices.
- A large sell order hits visible bids first, then receives increasingly lower latent prices.
- The slippage formula increases with order size and recent volatility.

This lets you submit very large manual orders while still making the price impact visible.

## Price Movement

The market does not use a random line chart. Price emerges from trades in the order book:

1. Agents submit market and limit orders.
2. Orders match against the book.
3. Trades update the last price.
4. Background liquidity replenishes depth around a drifting fundamental value.
5. Occasional news shocks move the fundamental value.
6. Agent behavior reacts to inventory, spread, recent momentum, and execution goals.

The dashboard plots candle data plus reference lines:

- candle OHLC: trade prices when trades happen, otherwise a mark price from the bounded book mid
- `mark`: the bounded mid/last reference used for no-trade candles
- `last_trade`: the latest executed trade price
- `fundamental`: the simulated fair value that background liquidity and some agents reference

The last traded price can deviate from the fundamental price when order flow is aggressive, liquidity is thin, or large orders cause impact.

## Long-Run Stability Controls

The first version could eventually lose realistic patterns in long runs because agents priced off the raw top-of-book mid price. If stale orders sat far away from the fundamental value, the raw mid became a bad fair-value estimate. HFT and random limit orders then placed more quotes around that distorted level, while background liquidity still used part of the last traded price. After enough time, the book could form two separated liquidity pools and the chart could appear to jump between two repeated prices.

The simulator now has guardrails for that:

- `reference_mid_price` clamps the raw mid around the simulated fundamental value before agents use it as fair value.
- Internal stale resting orders expire when their limit price moves too far outside the reference band.
- Latent liquidity prices are bounded around the fundamental so very large market orders still have slippage without permanently pulling the market to an extreme.
- Background depth is anchored mostly to the fundamental price, with only a small weight from bounded last price.
- No-trade candles use a mark price instead of repeating a stale last trade forever.

These controls are intentionally configurable in `market_sim/config.py` so open-source users can loosen the bands for stress testing or tighten them for stable benchmark runs.

## Agent Types

### Institutional Traders

Institutional traders simulate large parent orders broken into smaller child orders. Their behavior resembles VWAP/TWAP execution:

- They choose a large parent buy or sell order.
- They execute it gradually instead of all at once.
- They use a mix of limit orders and market orders.
- They become more aggressive based on urgency.
- They track remaining parent quantity in their agent metadata.
- They can buy, sell, or hold on any eligible tick depending on parent order state, urgency, inventory, and timing.

Institutional flow creates persistent buying or selling pressure. This is useful for producing directional market movement without forcing the price directly.

### High Frequency Traders

High frequency traders simulate fast market-making and short-horizon momentum:

- They frequently cancel their previous quotes.
- They place fresh bid and ask limit orders around a fair price.
- They widen quotes when volatility rises.
- They skew quotes based on inventory.
- They may fire small market orders when recent momentum is strong.
- They can quote both sides, buy only, sell only, or hold when throttled or inventory-limited.

HFT agents add liquidity most of the time, but they can also amplify short-term moves.

The number of market-making HFT agents is randomized on startup and reset. Their action intervals are also randomized so they do not all quote at the same cadence.

### Random Traders

Random traders produce noise flow:

- They submit buys and sells randomly.
- Most of their orders are market orders.
- Some are limit orders away from the current mid price.
- Their order sizes follow an exponential distribution, so many trades are small and a few are larger.
- They can buy, sell, or hold based on their randomized activity rate.

Random traders make the tape less deterministic and prevent the market from becoming too clean.

The number of random traders is randomized on startup and reset. Each random trader also has its own randomized activity interval and average order size.

### Background Liquidity

The engine also creates background depth around the simulated fundamental price. This is not shown as an agent in the UI because it represents passive external liquidity rather than a strategic trader. It keeps the book populated and gives manual/API orders something to interact with.

## API Endpoints

The server runs on `127.0.0.1:8000` by default.

API orders can include a user or model name. The simulator accepts any of these JSON fields:

- `user`
- `user_name`
- `username`
- `api_user`
- `client`
- `client_id`
- `model`
- `owner`
- `name`

It also accepts these headers:

- `X-API-User`
- `X-Client-Name`
- `X-Model-Name`

If no name is provided, the order is attributed to `anonymous-api-user`.

### Get Full Market State

```http
GET /api/state
```

Returns:

- current price, bid, ask, spread, volume, volatility
- order book depth
- recent OHLC candle history
- recent trades
- agent accounts and metadata
- API users with cash, inventory, volume, average trade price, equity, and P/L
- open API-user orders
- recent event stream entries
- simulation running state

### Live Stream

```http
GET /api/stream
```

Returns a Server-Sent Events stream. Each message has event type `state` and contains the same style of payload as `/api/state`.

### Chart Refresh Cadence

Trading endpoints are independent from chart updates. Agents can submit orders whenever they want, subject to API rate limits, while OHLC chart/history sampling follows a broker-style refresh cadence.

```http
GET /api/chart-refresh
```

```http
POST /api/chart-refresh
Content-Type: application/json

{
  "chart_refresh_ms": 5000
}
```

The same values are also included in `/api/state` as `chart_refresh_interval` and `chart_refresh_ms`, so agents can align polling or UI assumptions with the current chart feed rate.

### Currency Preference

```http
GET /api/currency?locale=en-IN&timezone=Asia/Kolkata
```

Returns a display currency such as `INR`, `USD`, `EUR`, or `GBP`.

The dashboard uses this endpoint with the browser's locale and time zone. It does not request GPS permission and does not call an external IP-geolocation service. API clients can also use headers:

- `X-Client-Locale`
- `X-Client-Timezone`
- `X-Currency`

The UI also includes a currency selector in the header. If the automatic browser locale/time-zone detection resolves to the wrong currency, the user can override it, and the choice is persisted in browser local storage.

### API Rate Limits

All `/api/...` routes pass through a broker-style rolling-window limiter. Requests are bucketed by endpoint class plus API user/model when the request provides a user identity field or `X-API-User` style header. Anonymous requests, such as bare market-data polling, are bucketed by client IP address. Market-data, trading, account, and control endpoints use separate buckets so state polling cannot block order submission or chart-refresh setting changes.

Default limits:

```text
api_rate_limit_per_second = 25
api_rate_limit_per_minute = 900
```

When a client exceeds a bucket, the server returns `429 Too Many Requests` with `Retry-After` plus `X-RateLimit-*` headers. These defaults can be changed with a JSON config override.

### Accounts

Create or fetch an API account:

```http
POST /api/accounts
Content-Type: application/json

{
  "user": "GPT-Strategy-1",
  "starting_cash": 1000000
}
```

Get one account:

```http
GET /api/account?user=GPT-Strategy-1
```

Add funds to an API account:

```http
POST /api/accounts/fund
Content-Type: application/json

{
  "user": "GPT-Strategy-1",
  "amount": 250000
}
```

### Buy

```http
POST /api/buy
Content-Type: application/json

{
  "quantity": 1000,
  "order_type": "market",
  "user": "GPT-Strategy-1"
}
```

Limit buy:

```http
POST /api/buy
Content-Type: application/json

{
  "quantity": 1000,
  "order_type": "limit",
  "price": 99.75,
  "user": "GPT-Strategy-1"
}
```

### Sell

```http
POST /api/sell
Content-Type: application/json

{
  "quantity": 750,
  "order_type": "market",
  "user": "Claude-Arb-Bot"
}
```

Limit sell:

```http
POST /api/sell
Content-Type: application/json

{
  "quantity": 750,
  "order_type": "limit",
  "price": 101.25,
  "user": "Claude-Arb-Bot"
}
```

### Generic Order Endpoint

```http
POST /api/order
Content-Type: application/json

{
  "side": "buy",
  "quantity": 500,
  "order_type": "market",
  "model": "Local-Llama-Trader"
}
```

Order requests support:

- `order_type`: `market`, `limit`, `stop`, `stop_limit`
- `time_in_force`: `gtc`, `ioc`, `fok`
- `post_only`: `true` or `false`
- `price`: required for `limit` and `stop_limit`
- `stop_price`: required for `stop` and `stop_limit`

### Orders And Cancels

List orders:

```http
GET /api/orders?user=GPT-Strategy-1
```

Cancel by endpoint:

```http
DELETE /api/orders/ord_abc123?user=GPT-Strategy-1
```

Cancel by JSON:

```http
POST /api/cancel
Content-Type: application/json

{
  "order_id": "ord_abc123",
  "user": "GPT-Strategy-1"
}
```

### API User Leaderboard

```http
GET /api/users
```

Returns API users ranked by marked-to-market P/L.

P/L is computed as:

```text
cash + inventory * current_last_price - initial_cash
```

API users start with their configured starting cash, and funding deposits increase both `cash` and `initial_cash`. That keeps deposits from being counted as trading P/L. Internal simulation agents also start with seed capital, so their P/L is measured relative to that seed capital.

### Pause Or Resume

```http
POST /api/simulation
Content-Type: application/json

{
  "running": false
}
```

### Reset

```http
POST /api/reset
```

This resets the order book, agents, trade history, price history, and simulation clock.

## Example Curl Commands

Buy 2,500 shares at market:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/buy \
  -H 'Content-Type: application/json' \
  -d '{"quantity":2500,"order_type":"market","user":"GPT-Strategy-1"}'
```

Sell 10,000 shares at market:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/sell \
  -H 'Content-Type: application/json' \
  -d '{"quantity":10000,"order_type":"market","user":"Claude-Arb-Bot"}'
```

Place a resting buy limit order:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/buy \
  -H 'Content-Type: application/json' \
  -d '{"quantity":500,"order_type":"limit","price":99.5,"client_id":"Research-Agent-A"}'
```

Fetch state:

```bash
curl -sS http://127.0.0.1:8000/api/state
```

Fetch API user P/L:

```bash
curl -sS http://127.0.0.1:8000/api/users
```

## Dashboard

The UI is a live operational dashboard:

- top metrics show last price, bid/ask, spread, volume, and volatility
- price canvas shows OHLC candles, volume bars, moving averages, current price, and fundamental value
- order ticket sends manual buy/sell requests to the API
- depth canvas shows bid and ask liquidity
- API user table ranks external users and models by P/L
- agent table shows internal agent inventory, volume, and P/L versus seed capital
- trade tape shows recent executions
- currency badge shows the display currency inferred from the browser locale/time zone

The UI polls `/api/state` every 650 milliseconds.

## Scenarios And Reproducibility

Run with a scenario:

```bash
python3 main.py --scenario flash_crash
```

Run with a deterministic seed:

```bash
python3 main.py --scenario trending_up --seed 42
```

Available built-in scenarios:

- `default`
- `calm`
- `high_volatility`
- `trending_up`
- `trending_down`
- `flash_crash`
- `liquidity_drought`
- `mean_reverting`
- `news_shock`

You can also pass a JSON config override:

```bash
python3 main.py --config config.local.json
```

## Open Source Support

The repository includes:

- `README.md`
- `LICENSE`
- `CONTRIBUTING.md`
- `Dockerfile`
- `docker-compose.yml`
- `.github/workflows/ci.yml`
- `static/openapi.json`
- unit tests under `tests/`

## What Makes It Market-Like

The simulator includes several real microstructure ideas:

- price-time priority in the limit order book
- market orders consuming visible liquidity
- slippage and price impact for large orders
- spread and depth changing over time
- passive market-making behavior
- inventory-sensitive quoting
- gradual institutional execution
- noisy retail-style order flow
- a drifting fundamental value
- occasional news-like shocks

It is still a simulation, not a predictive model. It does not model real exchange rules, auctions, halts, hidden order types, transaction fees, borrow constraints, cross-asset relationships, or real historical calibration. Its purpose is to create believable market dynamics for experimentation and visualization.

## Extending The Simulator

Useful next additions would be:

- multiple symbols
- per-agent risk limits
- transaction costs and maker/taker fees
- short-selling constraints
- historical replay mode
- WebSocket streaming instead of polling
- persistence for trades and snapshots
- configurable agent counts from an API endpoint
- more advanced institutional execution schedules
- market maker adverse-selection logic
