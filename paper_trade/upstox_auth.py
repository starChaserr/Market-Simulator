from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PAPER_ROOT = Path(__file__).resolve().parent
DEFAULT_TOKEN_FILE = PAPER_ROOT / ".upstox_token"
DEFAULT_AUTH_BASE_URL = "https://api.upstox.com"
DEFAULT_TIMEZONE = "Asia/Kolkata"


class UpstoxAuthError(RuntimeError):
    """Raised when the Upstox access-token bootstrap flow cannot proceed."""


def metadata_path_for(token_file: Path) -> Path:
    return token_file.with_suffix(token_file.suffix + ".json")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_epoch_ms(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw > 10_000_000_000:
        raw /= 1000.0
    return dt.datetime.fromtimestamp(raw, tz=dt.UTC)


def _token_expiry_from_metadata(payload: dict[str, Any]) -> dt.datetime | None:
    for key in ("expires_at", "expiry", "authorization_expiry"):
        parsed = _parse_epoch_ms(payload.get(key))
        if parsed:
            return parsed
    value = payload.get("expires_at_iso")
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value).astimezone(dt.UTC)
        except ValueError:
            return None
    return None


def _default_expiry_from_file_mtime(token_file: Path, timezone: str) -> dt.datetime | None:
    try:
        modified = dt.datetime.fromtimestamp(token_file.stat().st_mtime, tz=ZoneInfo(timezone))
    except OSError:
        return None
    expiry = modified.replace(hour=3, minute=30, second=0, microsecond=0)
    if modified >= expiry:
        expiry += dt.timedelta(days=1)
    return expiry.astimezone(dt.UTC)


def token_file_has_token(token_file: Path) -> bool:
    try:
        return bool(token_file.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def token_file_is_fresh(
    token_file: Path,
    metadata_file: Path | None = None,
    *,
    min_valid_seconds: float = 1800.0,
    timezone: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> bool:
    if not token_file_has_token(token_file):
        return False
    now = (now or _utc_now()).astimezone(dt.UTC)
    metadata_file = metadata_file or metadata_path_for(token_file)
    expiry: dt.datetime | None = None
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict):
        expiry = _token_expiry_from_metadata(metadata)
    expiry = expiry or _default_expiry_from_file_mtime(token_file, timezone)
    if expiry is None:
        return True
    return (expiry - now).total_seconds() > min_valid_seconds


def write_access_token(
    token_file: Path,
    payload: dict[str, Any],
    *,
    metadata_file: Path | None = None,
    source: str,
) -> dict[str, Any]:
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise UpstoxAuthError("token payload did not include access_token")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token + "\n", encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        pass

    expiry = _token_expiry_from_metadata(payload)
    metadata = {
        "source": source,
        "updated_at": _utc_now().isoformat(),
        "expires_at": str(payload.get("expires_at", "")),
        "expires_at_iso": expiry.isoformat() if expiry else None,
        "issued_at": str(payload.get("issued_at", "")),
        "client_id": payload.get("client_id"),
        "user_id": payload.get("user_id"),
        "message_type": payload.get("message_type"),
    }
    metadata_file = metadata_file or metadata_path_for(token_file)
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    try:
        metadata_file.chmod(0o600)
    except OSError:
        pass
    return metadata


def _request_json(
    url: str,
    *,
    method: str,
    body: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("accept", "application/json")
    request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstoxAuthError(f"Upstox auth HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise UpstoxAuthError(f"Upstox auth request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpstoxAuthError("Upstox auth returned invalid JSON") from exc


def request_access_token(
    *,
    client_id: str,
    client_secret: str,
    auth_base_url: str = DEFAULT_AUTH_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    quoted_client = urllib.parse.quote(client_id.strip(), safe="")
    url = f"{auth_base_url.rstrip('/')}/v3/login/auth/token/request/{quoted_client}"
    body = json.dumps({"client_secret": client_secret}).encode("utf-8")
    return _request_json(url, method="POST", body=body, timeout=timeout)


def exchange_auth_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    auth_base_url: str = DEFAULT_AUTH_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = f"{auth_base_url.rstrip('/')}/v2/login/authorization/token"
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    return _request_json(url, method="POST", body=body, content_type="application/x-www-form-urlencoded", timeout=timeout)


def ensure_access_token_file(
    *,
    token_file: Path,
    metadata_file: Path | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    request_if_stale: bool = True,
    wait_seconds: float = 900.0,
    min_valid_seconds: float = 1800.0,
    timezone: str = DEFAULT_TIMEZONE,
    auth_base_url: str = DEFAULT_AUTH_BASE_URL,
) -> dict[str, Any]:
    metadata_file = metadata_file or metadata_path_for(token_file)
    if token_file_is_fresh(token_file, metadata_file, min_valid_seconds=min_valid_seconds, timezone=timezone):
        return {"status": "fresh", "token_file": str(token_file), "requested": False}

    if not request_if_stale:
        raise UpstoxAuthError(f"Upstox token is missing or stale at {token_file}")
    if not client_id or not client_secret:
        raise UpstoxAuthError(
            "Upstox token is missing or stale, and UPSTOX_CLIENT_ID / UPSTOX_CLIENT_SECRET are not configured for token requests."
        )

    response = request_access_token(client_id=client_id, client_secret=client_secret, auth_base_url=auth_base_url)
    deadline = time.time() + max(wait_seconds, 0.0)
    while time.time() <= deadline:
        if token_file_is_fresh(token_file, metadata_file, min_valid_seconds=min_valid_seconds, timezone=timezone):
            return {"status": "fresh", "token_file": str(token_file), "requested": True, "request": response}
        time.sleep(2.0)
    raise UpstoxAuthError(
        f"Requested an Upstox token but no fresh token reached {token_file} within {wait_seconds:.0f}s. "
        "Approve the request in Upstox and make sure the notifier webhook is reachable."
    )


class TokenWebhookServer(HTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        token_file: Path,
        metadata_file: Path,
        webhook_path: str,
        webhook_secret: str | None,
    ) -> None:
        super().__init__(address, TokenWebhookHandler)
        self.token_file = token_file
        self.metadata_file = metadata_file
        self.webhook_path = webhook_path if webhook_path.startswith("/") else f"/{webhook_path}"
        self.webhook_secret = webhook_secret


class TokenWebhookHandler(BaseHTTPRequestHandler):
    server: TokenWebhookServer
    server_version = "UpstoxTokenWebhook/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"ok": True})
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.webhook_path:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if self.server.webhook_secret:
            query = urllib.parse.parse_qs(parsed.query)
            supplied = self.headers.get("X-Webhook-Secret") or (query.get("secret") or [""])[0]
            if supplied != self.server.webhook_secret:
                self._error(HTTPStatus.FORBIDDEN, "invalid webhook secret")
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            metadata = write_access_token(
                self.server.token_file,
                payload,
                metadata_file=self.server.metadata_file,
                source="upstox_webhook",
            )
        except Exception as exc:  # noqa: BLE001 - webhook must return clear JSON errors
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json({"ok": True, "expires_at": metadata.get("expires_at_iso")})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status=status)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Upstox access-token files for unattended paper trading.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
        subparser.add_argument("--metadata-file", default=None)

    status = subparsers.add_parser("status", help="Show whether the local token file is fresh.")
    add_common(status)
    status.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    status.add_argument("--min-valid-seconds", type=float, default=1800.0)

    request = subparsers.add_parser("request", help="Ask Upstox to send a token to the configured notifier webhook.")
    add_common(request)
    request.add_argument("--client-id", default=os.environ.get("UPSTOX_CLIENT_ID"))
    request.add_argument("--client-secret", default=os.environ.get("UPSTOX_CLIENT_SECRET"))
    request.add_argument("--auth-base-url", default=DEFAULT_AUTH_BASE_URL)
    request.add_argument("--wait", action=argparse.BooleanOptionalAction, default=False)
    request.add_argument("--wait-seconds", type=float, default=900.0)
    request.add_argument("--timezone", default=DEFAULT_TIMEZONE)

    exchange = subparsers.add_parser("exchange", help="Exchange a one-use auth code and store the returned access token.")
    add_common(exchange)
    exchange.add_argument("--code", required=True)
    exchange.add_argument("--client-id", default=os.environ.get("UPSTOX_CLIENT_ID"))
    exchange.add_argument("--client-secret", default=os.environ.get("UPSTOX_CLIENT_SECRET"))
    exchange.add_argument("--redirect-uri", default=os.environ.get("UPSTOX_REDIRECT_URI"))
    exchange.add_argument("--auth-base-url", default=DEFAULT_AUTH_BASE_URL)

    webhook = subparsers.add_parser("webhook", help="Run a small notifier webhook receiver that stores Upstox tokens.")
    add_common(webhook)
    webhook.add_argument("--host", default="127.0.0.1")
    webhook.add_argument("--port", type=int, default=8791)
    webhook.add_argument("--path", default="/upstox/token")
    webhook.add_argument("--webhook-secret", default=os.environ.get("UPSTOX_WEBHOOK_SECRET"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token_file = Path(args.token_file)
    metadata_file = Path(args.metadata_file) if args.metadata_file else metadata_path_for(token_file)
    if args.command == "status":
        fresh = token_file_is_fresh(
            token_file,
            metadata_file,
            min_valid_seconds=args.min_valid_seconds,
            timezone=args.timezone,
        )
        print(json.dumps({"token_file": str(token_file), "metadata_file": str(metadata_file), "fresh": fresh}, indent=2))
        return 0 if fresh else 1
    if args.command == "request":
        if not args.client_id or not args.client_secret:
            raise UpstoxAuthError("UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET are required")
        if args.wait:
            result = ensure_access_token_file(
                token_file=token_file,
                metadata_file=metadata_file,
                client_id=args.client_id,
                client_secret=args.client_secret,
                wait_seconds=args.wait_seconds,
                timezone=args.timezone,
                auth_base_url=args.auth_base_url,
            )
        else:
            response = request_access_token(
                client_id=args.client_id,
                client_secret=args.client_secret,
                auth_base_url=args.auth_base_url,
            )
            result = {"status": "requested", "request": response}
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "exchange":
        if not args.client_id or not args.client_secret or not args.redirect_uri:
            raise UpstoxAuthError("client id, client secret, and redirect URI are required")
        payload = exchange_auth_code(
            code=args.code,
            client_id=args.client_id,
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            auth_base_url=args.auth_base_url,
        )
        metadata = write_access_token(token_file, payload, metadata_file=metadata_file, source="auth_code_exchange")
        print(json.dumps({"token_file": str(token_file), "metadata_file": str(metadata_file), "expires_at": metadata.get("expires_at_iso")}, indent=2))
        return 0
    if args.command == "webhook":
        server = TokenWebhookServer((args.host, args.port), token_file, metadata_file, args.path, args.webhook_secret)
        print(f"Upstox token webhook listening on http://{args.host}:{args.port}{server.webhook_path}")
        server.serve_forever()
        return 0
    raise UpstoxAuthError(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
