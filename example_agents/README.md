# Example Agents

These scripts trade against the local simulator API at `http://127.0.0.1:8000/api`.

Run the simulator from the repo root:

```bash
python3 main.py
```

Run one agent:

```bash
python3 example_agents/adaptive_edge_maker.py AdaptiveEdgeMaker
python3 example_agents/raider_core.py RaiderCore
```

Run a local competition:

```bash
python3 example_agents/competition_runner.py
python3 example_agents/competition_runner.py all
```

`adaptive_edge_maker.py` uses only current public market/account state: top-of-book, visible depth, current volatility, cash, and inventory. It does not inspect future prices or simulator internals.
