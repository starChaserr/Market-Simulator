# Upstox Paper Trade Bridge

This folder runs the existing example agents against live Upstox market data while keeping every trade local and simulated. It does not place real Upstox orders.

## Token

Use an Upstox access token from your app login flow:

```bash
export UPSTOX_ACCESS_TOKEN="paste-token-here"
```

Or keep it in an ignored local file:

```bash
printf "paste-token-here" > paper_trade/.upstox_token
```

For unattended remote runs, configure Upstox's notifier URL to point at this server and let the launcher store the token automatically after you approve the request in Upstox:

```bash
export UPSTOX_CLIENT_ID="your-api-key"
export UPSTOX_CLIENT_SECRET="your-api-secret"
PAPER_UPSTOX_WEBHOOK=1 PAPER_UPSTOX_WEBHOOK_HOST=0.0.0.0 ./launch_daily_supervisor.sh
```

The webhook listens on `/upstox/token` by default and writes `paper_trade/.upstox_token` plus `paper_trade/.upstox_token.json`. The supervisor checks token freshness before live sessions and, when credentials are configured, requests a fresh token shortly before market open. Upstox still requires user approval for token generation; the remote server handles storage after approval.

You can also exchange a one-use auth code manually:

```bash
python3 -m paper_trade.upstox_auth exchange \
  --code "paste-auth-code" \
  --client-id "$UPSTOX_CLIENT_ID" \
  --client-secret "$UPSTOX_CLIENT_SECRET" \
  --redirect-uri "$UPSTOX_REDIRECT_URI"
```

## Run One Paper Market

Start the bridge for one symbol:

```bash
python3 -m paper_trade.server --token-file paper_trade/.upstox_token --symbol RELIANCE --port 8780 --refresh 1.0
```

Then point any compatible agent at it:

```bash
python3 example_agents/adaptive_edge_maker.py AdaptivePaper --url http://127.0.0.1:8780/api --starting-cash 100000 --interval 0.5
python3 example_agents/raider_core.py RaiderPaper --url http://127.0.0.1:8780/api --starting-cash 100000 --interval 0.5
python3 example_agents/apex_maker_v5.py ApexPaper --url http://127.0.0.1:8780/api --starting-cash 100000 --target-loop-ms 500
```

Check standings:

```bash
curl -sS http://127.0.0.1:8780/api/accounts
```

## Run A Match

This starts the bridge, launches agents, waits, prints standings, and shuts everything down:

```bash
python3 -m paper_trade.run_live_match \
  --token-file paper_trade/.upstox_token \
  --symbols RELIANCE,HDFCBANK,INFY \
  --agents raider_core,adaptive_edge_maker,apex_maker \
  --duration 900 \
  --keep-results
```

`auto_trader.py` is not included by default because it has a hard-coded API URL. To test it unchanged, run the paper bridge on port `8000`.

## Auto Live Optimize With Gemini

This waits for the live NSE cash-market window, runs a basket test across every symbol in `symbols.json`, then asks Gemini CLI to improve the target bot from the full multi-symbol result:

```bash
python3 -m paper_trade.auto_live_optimize \
  --token-file paper_trade/.upstox_token \
  --symbols all \
  --agents raider_core,adaptive_edge_maker,apex_maker \
  --target-agents adaptive_edge_maker \
  --duration 600
```

The automation intentionally refuses one-symbol runs unless you pass `--allow-single-symbol`, because that makes it too easy to overfit a bot to one ticker. Market hours default to `09:15`-`15:30` Asia/Kolkata and can be overridden with `--market-open`, `--market-close`, and `--timezone`.

Gemini is invoked as `gemini -p "<prompt>"` by default. Add extra Gemini CLI flags with repeated `--gemini-arg`, or disable the optimizer pass with `--skip-gemini` for a scheduler smoke test. The prompt tells Gemini not to use token files, hardcoded symbols, or look-ahead data.

## Daily 30-Minute Supervisor

For continuous live supervision, run:

```bash
./launch_daily_supervisor.sh
```

By default this waits for the NSE cash-market window every weekday, runs 30-minute paper sessions, then asks:

- Codex CLI to improve `example_agents/adaptive_edge_maker.py`
- Gemini CLI to improve `example_agents/raider_core.py`

The launcher defaults to API-safe `split` symbol mode with a 3-second quote refresh. That divides the 30-minute budget across configured symbols and avoids multiple bridge servers hammering Upstox at the same time. Use `--symbol-mode parallel` only if your Upstox quota can handle one live quote poller per symbol.

The supervisor runs old and upgraded agent versions in the same live paper sessions. After each session it records live performance in `paper_trade/agent_registry.json` and deactivates losing challenger versions after repeated negative evaluations. Baseline agents stay protected as seed versions. Tune this with:

```bash
./launch_daily_supervisor.sh \
  --prune-min-evaluations 2 \
  --prune-loss-threshold 0 \
  --prune-keep-top-per-source 3
```

You can tune the API load without editing code:

```bash
PAPER_SYMBOL_MODE=split PAPER_REFRESH_SECONDS=5.0 PAPER_RATE_LIMIT_COOLDOWN=300 ./launch_daily_supervisor.sh
```

If Upstox returns `UDAPI10005` / HTTP 429, the supervisor writes a `rate_limited` status, cools down, and retries while the market is still open.

The launcher starts the dashboard at:

```text
http://127.0.0.1:8790
```

Pass supervisor flags through the launcher:

```bash
./launch_daily_supervisor.sh --max-days 1 --dry-run-supervisors
```

Reports are written in both JSON and Markdown:

- `paper_trade/reports/30_min/YYYYMMDD/session_NN.json`
- `paper_trade/reports/30_min/YYYYMMDD/session_NN.md`
- `paper_trade/reports/full_day/YYYYMMDD.json`
- `paper_trade/reports/full_day/YYYYMMDD.md`
- `paper_trade/reports/status.json`

At market close the supervisor aggregates all session data, writes the full-day report, and runs one more end-of-day improvement pass. Use `--max-days 1` for one trading day, or keep the default `--max-days 0` to continue every day until stopped.

Useful dry run:

```bash
python3 -m paper_trade.daily_supervisor \
  --symbols all \
  --max-days 1 \
  --dry-run-supervisors \
  --skip-verify
```

## Symbols

The default watchlist is in `paper_trade/symbols.json`:

- RELIANCE
- HDFCBANK
- INFY
- TCS
- ICICIBANK

You can add another symbol by adding its Upstox `instrument_key`, then start the server with `--symbol YOUR_SYMBOL`. You can also bypass the watchlist:

```bash
python3 -m paper_trade.server --token-file paper_trade/.upstox_token --symbol CUSTOM --instrument-key 'NSE_EQ|INE002A01018'
```

## Agent API

The bridge implements the simulator-style routes the agents already use:

- `GET /api/state`
- `GET /api/account?user=...`
- `GET /api/accounts`
- `GET /api/orders?user=...`
- `POST /api/accounts`
- `POST /api/accounts/fund`
- `POST /api/order`
- `POST /api/buy`
- `POST /api/sell`
- `POST /api/cancel`
- `DELETE /api/orders/{order_id}?user=...`

Live quotes refresh on the configured interval. Agents can submit orders whenever they want, but fills are simulated against the latest quote/depth snapshot.
