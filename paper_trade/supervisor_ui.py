from __future__ import annotations

import argparse
import json
import mimetypes
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PAPER_ROOT / "supervisor_static"
REPORTS_ROOT = PAPER_ROOT / "reports"
STATUS_FILE = REPORTS_ROOT / "status.json"
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORTS = range(8780, 8785)


def default_status() -> dict[str, Any]:
    return {
        "phase": "not_started",
        "message": "Supervisor has not written a status yet.",
        "day": None,
        "symbols": [],
        "agents": [],
        "session_index": None,
        "latest_session_report": None,
        "latest_daily_report": None,
        "summary": {"aggregate": [], "symbol_rankings": []},
        "updated_at": None,
    }


def read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_report_path(value: str) -> Path:
    decoded = unquote(value)
    candidate = Path(decoded)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    resolved.relative_to(REPORTS_ROOT.resolve())
    if resolved.suffix != ".json":
        raise ValueError("only JSON reports can be loaded")
    return resolved


def report_listing() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not REPORTS_ROOT.exists():
        return rows
    for path in REPORTS_ROOT.rglob("*.json"):
        if path == STATUS_FILE:
            continue
        kind = "full_day" if "full_day" in path.parts else "30_min"
        stat = path.stat()
        rows.append(
            {
                "kind": kind,
                "name": path.stem,
                "path": str(path.relative_to(ROOT)),
                "updated_at": stat.st_mtime,
            }
        )
    rows.sort(key=lambda row: float(row["updated_at"]), reverse=True)
    return rows


def fetch_bridge_json(url: str, timeout: float = 0.6) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None


def live_session_payload(port_values: list[str] | None = None) -> dict[str, Any]:
    ports: list[int] = []
    for value in port_values or []:
        try:
            ports.append(int(value))
        except ValueError:
            continue
    if not ports:
        ports = list(DEFAULT_BRIDGE_PORTS)

    status = read_json(STATUS_FILE) if STATUS_FILE.exists() else default_status()
    for port in ports:
        api_url = f"http://{DEFAULT_BRIDGE_HOST}:{port}/api"
        health = fetch_bridge_json(f"{api_url}/health")
        if not health:
            continue
        state = fetch_bridge_json(f"{api_url}/state", timeout=1.0)
        accounts_payload = fetch_bridge_json(f"{api_url}/accounts")
        return {
            "connected": True,
            "api_url": api_url,
            "port": port,
            "health": health,
            "state": state,
            "accounts": (accounts_payload or {}).get("accounts", []),
            "status": status,
            "updated_at": time.time(),
        }

    return {
        "connected": False,
        "api_url": None,
        "port": None,
        "health": None,
        "state": None,
        "accounts": [],
        "status": status,
        "updated_at": time.time(),
    }


class SupervisorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                self._json(read_json(STATUS_FILE) if STATUS_FILE.exists() else default_status())
                return
            if parsed.path == "/api/reports":
                self._json({"reports": report_listing()})
                return
            if parsed.path == "/api/live-session":
                params = parse_qs(parsed.query)
                self._json(live_session_payload(params.get("port")))
                return
            if parsed.path == "/api/report":
                params = parse_qs(parsed.query)
                values = params.get("path", [])
                if not values:
                    self._error(400, "missing report path")
                    return
                path = safe_report_path(values[0])
                self._json(read_json(path))
                return
            if parsed.path == "/":
                self._file(STATIC_ROOT / "index.html")
                return
            static_path = (STATIC_ROOT / parsed.path.lstrip("/")).resolve()
            static_path.relative_to(STATIC_ROOT.resolve())
            self._file(static_path)
        except FileNotFoundError:
            self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        data = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the daily supervisor dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), SupervisorHandler)
    print(f"Supervisor dashboard: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
