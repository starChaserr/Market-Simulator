import subprocess, time, sys, json, urllib.request
from pathlib import Path

API_URL = "http://127.0.0.1:8780/api"
AGENTS = [
    ("raider_core.py", "RaiderMock"),
    ("adaptive_edge_maker.py", "AdaptiveMock"),
    ("apex_maker_v2.py", "ApexMock"),
]

def call_api(path):
    try:
        with urllib.request.urlopen(f"{API_URL}{path}") as r:
            return json.loads(r.read().decode())
    except: return None

def main():
    print(">>> Starting Mock Paper Trade Match (2 minutes)")
    agent_procs = []
    for script, name in AGENTS:
        cmd = [sys.executable, f"example_agents/{script}", name, "--url", API_URL, "--starting-cash", "10000", "--interval", "0.2"]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        agent_procs.append(p)
        print(f"  Started {name}")

    time.sleep(120)
    
    print("\n>>> Final Results:")
    print("| Agent | P/L | Orders | Fills | Equity |")
    print("| :--- | :--- | :--- | :--- | :--- |")
    accounts = call_api("/accounts") or {"accounts": []}
    for acc in sorted(accounts["accounts"], key=lambda x: x.get("profit_loss", 0), reverse=True):
        print(f"| {acc['owner']} | {acc.get('profit_loss', 0):.2f} | {acc.get('orders', 0)} | {acc.get('fills', 0)} | {acc.get('equity', 0):.2f} |")

    for p in agent_procs: p.kill()
    print("\nMatch complete.")

if __name__ == "__main__": main()
