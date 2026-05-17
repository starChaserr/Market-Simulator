# Contributing

Thanks for helping improve Market Simulator.

## Development

Run the server:

```bash
python3 main.py
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run syntax checks:

```bash
python3 -m py_compile main.py market_sim/*.py
node --check static/app.js
```

## Pull Request Guidelines

- Keep changes focused and explain the market behavior being changed.
- Add or update tests for matching, risk, order lifecycle, or accounting changes.
- Do not add network services, databases, or package dependencies unless the benefit is clear.
- Keep the simulator explicit about being synthetic and non-predictive.
- Avoid adding example trading bots to this repo unless the project maintainers decide to keep examples in a separate package.

## Useful Areas

- More realistic queue-position and latency modeling
- More market regimes and scenario fixtures
- Better risk limits and margin modeling
- More complete OpenAPI schemas
- Dashboard usability improvements
- Performance tests for high-order-volume simulations
