from __future__ import annotations
import json, subprocess, sys, time, urllib.request, socket, os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = Path(__file__).resolve().parent
API_URL = "http://127.0.0.1:8000/api"
RESULT_FILE = AGENT_DIR / "market_suitability_test.md"
SCENARIOS = ["calm", "high_volatility", "trending_up", "trending_down", "flash_crash", "liquidity_drought", "mean_reverting", "news_shock"]
AGENTS = [
    ("raider_core.py", "RaiderCore_v3.1", ["--model-path", str(ROOT_DIR / "llama-3-8b-instruct.gguf")]),
    ("adaptive_edge_maker.py", "AdaptiveEdgeMaker", []),
    ("apex_maker_v2.py", "ApexMaker_v2", []),
]

def call_api(path):
    try:
        with urllib.request.urlopen(f"{API_URL}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except: return None

def kill_all():
    for p in ["main.py", "raider_core.py", "adaptive_edge_maker.py", "auto_trader.py"]:
        subprocess.run(["pkill", "-9", "-f", p], stderr=subprocess.DEVNULL)
    time.sleep(2)

def run_scenario(scenario):
    print(f"\n>>> SCENARIO START: {scenario}")
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
    
    finished = set(); start = time.time()
    while len(finished) < len(AGENTS) and (time.time() - start) < 600:
        for p, name in procs:
            if name in finished: continue
            acc = call_api(f"/account?user={name}")
            if acc and acc.get("orders", 0) >= 500:
                finished.add(name)
        time.sleep(2)
    
    results = {
        name: {
            "pnl": (call_api(f"/account?user={name}") or {}).get("profit_loss", 0),
            "dd": (call_api(f"/account?user={name}") or {}).get("max_drawdown", 0)
        } for _, name in procs
    }
    for p, _ in procs: p.kill()
    server.kill(); time.sleep(2)
    print(f">>> SCENARIO END: {scenario} | Results: {results}")
    return results

def main():
    with open(RESULT_FILE, "w") as f:
        f.write("# Market Suitability Test Results\n\nRun Date: " + time.strftime('%Y-%m-%d %H:%M:%S') + "\nStarting Cash: 10,000\nOrder Limit: 500\n\n")
    all_res = {}
    for s in SCENARIOS:
        res = run_scenario(s)
        all_res[s] = res
        with open(RESULT_FILE, "a") as f:
            f.write(f"## Scenario: {s}\n\n| Agent | P/L | Max Drawdown |\n| :--- | :--- | :--- |\n")
            for n, d in res.items(): f.write(f"| {n} | {d['pnl']:.2f} | {d['dd']:.2f} |\n")
            if res: f.write(f"\n**WINNER (Profit): {max(res, key=lambda x: res[x]['pnl'])}**\n\n")
    with open(RESULT_FILE, "a") as f:
        f.write("## Final Summary\n\n| Scenario | Winner | P/L |\n| :--- | :--- | :--- |\n")
        for s, r in all_res.items():
            if r: 
                w = max(r, key=lambda x: r[x]['pnl'])
                f.write(f"| {s} | {w} | {r[w]['pnl']:.2f} |\n")
    print(f"Done: {RESULT_FILE}")

if __name__ == '__main__': main()
