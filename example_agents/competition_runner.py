from __future__ import annotations
import json, subprocess, sys, time, urllib.request, socket, os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = Path(__file__).resolve().parent
API_URL = "http://127.0.0.1:8000/api"
RESULT_FILE = AGENT_DIR / "test_result.md"
SCENARIOS = ["calm", "high_volatility", "trending_up", "trending_down", "flash_crash", "liquidity_drought", "mean_reverting", "news_shock"]
AGENTS = [
    ("raider_core.py", "RaiderCore", ["--model-path", "/tmp/non_existent"]),
    ("adaptive_edge_maker.py", "AdaptiveEdgeMaker", []),
    ("auto_trader.py", "AutoTrader", []),
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
    print(f"\n>>> SCENARIO: {scenario}")
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
    
    finished = set(); start = time.time()
    while len(finished) < len(AGENTS) and (time.time() - start) < 600:
        for p, name in procs:
            if name in finished: continue
            acc = call_api(f"/account?user={name}")
            if acc and acc.get("orders", 0) >= 1000:
                print(f"  {name} finished"); finished.add(name)
        time.sleep(2)
    
    results = {name: (call_api(f"/account?user={name}") or {}).get("profit_loss", 0) for _, name in procs}
    for p, _ in procs: p.kill()
    server.kill(); time.sleep(2)
    return results

def main():
    with open(RESULT_FILE, "w") as f:
        f.write("# Market Simulation Test Results\n\nRun Date: " + time.strftime('%Y-%m-%d %H:%M:%S') + "\nStarting Cash: 10,000\nOrder Limit: 1,000\n\n")
    all_res = {}
    for s in SCENARIOS:
        res = run_scenario(s)
        all_res[s] = res
        with open(RESULT_FILE, "a") as f:
            f.write(f"## Scenario: {s}\n\n| Agent | P/L |\n| :--- | :--- |\n")
            for n, p in res.items(): f.write(f"| {n} | {p:.2f} |\n")
            if res: f.write(f"\n**WINNER: {max(res, key=res.get)}**\n\n")
    with open(RESULT_FILE, "a") as f:
        f.write("## Final Summary\n\n| Scenario | Winner | P/L |\n| :--- | :--- | :--- |\n")
        for s, r in all_res.items():
            if r: w = max(r, key=r.get); f.write(f"| {s} | {w} | {r[w]:.2f} |\n")
    print(f"Done: {RESULT_FILE}")

if __name__ == '__main__': main()
