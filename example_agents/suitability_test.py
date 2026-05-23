from __future__ import annotations
import json, subprocess, sys, time, urllib.request, socket, os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = Path(__file__).resolve().parent
API_URL = "http://127.0.0.1:8000/api"
RESULT_FILE = AGENT_DIR / "market_suitability_results.md"
SCENARIOS = ["calm", "high_volatility", "trending_up", "trending_down", "flash_crash", "liquidity_drought", "mean_reverting", "news_shock"]
AGENTS = [
    ("raider_core.py", "RaiderCore", ["--url", "http://127.0.0.1:8000/api"]),
    ("adaptive_edge_maker.py", "AdaptiveEdgeMaker", []),
    ("auto_trader.py", "AutoTrader", []),
    ("apex_maker_v2.py", "ApexMaker", []),
    ("omnitrader.py", "OmniTrader", []),
]
TEST_DURATION = 60 

def call_api(path):
    try:
        with urllib.request.urlopen(f"{API_URL}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except: return None

def kill_all():
    for p in ["main.py", "raider_core.py", "adaptive_edge_maker.py", "auto_trader.py", "apex_maker_v2.py", "omnitrader.py"]:
        subprocess.run(["pkill", "-9", "-f", p], stderr=subprocess.DEVNULL)
    time.sleep(2)

def run_scenario(scenario):
    print(f"\n>>> TESTING SUITABILITY: {scenario}")
    kill_all()
    server = subprocess.Popen([sys.executable, "main.py", "--scenario", scenario], cwd=ROOT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if call_api("/state"): break
        time.sleep(1)
    else:
        print("Simulator failed"); server.kill(); return {}
    
    procs = []
    for script, name, args in AGENTS:
        p = subprocess.Popen([sys.executable, str(AGENT_DIR / script), name] + args, cwd=ROOT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append((p, name))
        print(f"  Started {name}")
    
    time.sleep(TEST_DURATION)
    
    results = {}
    for _, name in procs:
        acc = call_api(f"/account?user={name}")
        if acc:
            results[name] = {
                "pl": acc.get("profit_loss", 0),
                "drawdown": acc.get("max_drawdown", 0),
                "orders": acc.get("orders", 0)
            }
    
    for p, _ in procs: p.kill()
    server.kill(); time.sleep(2)
    return results

def main():
    with open(RESULT_FILE, "w") as f:
        f.write("# Market Suitability Test Results (including ApexMaker)\n\nGoal: Identify the best market type for each agent.\n\n")
    
    all_res = {}
    for s in SCENARIOS:
        res = run_scenario(s)
        all_res[s] = res
        with open(RESULT_FILE, "a") as f:
            f.write(f"## Market: {s}\n\n| Agent | P/L | Max Drawdown | Orders |\n| :--- | :--- | :--- | :--- |\n")
            for n, d in res.items():
                f.write(f"| {n} | {d['pl']:.2f} | {d['drawdown']:.2f} | {d['orders']} |\n")
            if res: f.write(f"\n**Best Performer (Profit): {max(res, key=lambda k: res[k]['pl'])}**\n\n")

    with open(RESULT_FILE, "a") as f:
        f.write("## Strategy Best Use Case Analysis\n\n")
        for _, name, _ in AGENTS:
            # Find best market for this agent across all scenarios where it participated
            valid_scenarios = [s for s in SCENARIOS if s in all_res and name in all_res[s]]
            if not valid_scenarios:
                continue
            agent_best_market = max(valid_scenarios, key=lambda s: all_res[s][name]['pl'])
            f.write(f"### {name}\n- **Best Market:** {agent_best_market}\n- **Max Profit achieved:** {all_res[agent_best_market][name]['pl']:.2f}\n\n")

    print(f"Results saved to {RESULT_FILE}")

if __name__ == '__main__': main()
