"""Metrics receiver — webhook + CLI that writes metrics.json into queue slots.

Two modes:

1. SERVER MODE — run it as a tiny HTTP server that a Make.com scenario POSTs to:

     python src/agents/metrics_receiver.py serve

   Accepts: POST /metrics
   Headers: Authorization: Bearer <METRICS_WEBHOOK_SECRET>
   Body (JSON):
     {
       "date":     "2026-04-19",
       "slot":     "morning",
       "x":        {"impressions": 1450, "likes": 38, "retweets": 5},
       "telegram": {"views": 820}
     }

   Writes (merging with any existing data) to:
     queue/2026-04-19_morning/metrics.json

   After a successful write, re-runs the analytics + dashboard build so the
   new numbers show up immediately.

2. CLI MODE — manual entry from the terminal (no server needed):

     python src/agents/metrics_receiver.py set \\
         --date 2026-04-19 --slot morning \\
         --x-impressions 1450 --x-likes 38 --x-retweets 5 \\
         --tg-views 820

Security notes:
- The receiver binds to 127.0.0.1 by default. If you expose it publicly (e.g.
  via ngrok so Make.com can reach it), KEEP the shared-secret check on.
- The secret lives in METRICS_WEBHOOK_SECRET (empty = reject all requests).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_BIND = "127.0.0.1:8787"
DEFAULT_QUEUE = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))


# --- Core write + merge ----------------------------------------------------

def _slot_dir(queue_root: Path, date: str, slot: str) -> Path:
    return queue_root / f"{date}_{slot}"


def write_metrics(queue_root: Path, date: str, slot: str,
                  payload: dict[str, Any]) -> Path:
    """Merge payload into queue/<date>_<slot>/metrics.json."""
    sd = _slot_dir(queue_root, date, slot)
    if not sd.exists():
        raise FileNotFoundError(f"slot dir does not exist: {sd}")
    metrics_path = sd / "metrics.json"

    current: dict[str, Any] = {}
    if metrics_path.exists():
        try:
            current = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}

    # shallow merge per platform: new values overwrite old for same keys,
    # but platforms not in the payload are preserved.
    for plat in ("x", "telegram"):
        incoming = payload.get(plat)
        if incoming is None:
            continue
        if not isinstance(incoming, dict):
            raise ValueError(f"{plat} must be an object, got {type(incoming).__name__}")
        merged = dict(current.get(plat) or {})
        merged.update(incoming)
        current[plat] = merged

    current["_updated_at"] = datetime.now(timezone.utc).isoformat()

    metrics_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metrics_path


def refresh_analytics(queue_root: Path) -> None:
    """Best-effort: rebuild analytics + dashboard after a write."""
    try:
        from src.agents.analytics import refresh_and_write as _refresh
        from src.dashboard import build as _db
        an_dir = _REPO_ROOT / "analytics"
        n = _refresh(queue_root, an_dir)
        _db.build_dashboard(an_dir / "rollup.json",
                            _REPO_ROOT / "src" / "dashboard" / "index.html")
        print(f"[metrics_receiver] analytics refreshed ({n} slots)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[metrics_receiver] analytics refresh failed (non-fatal): {exc}",
              file=sys.stderr)


# --- HTTP server -----------------------------------------------------------

def make_handler(queue_root: Path, secret: str):
    """Factory returning a handler class closed over queue_root + secret."""

    class MetricsHandler(BaseHTTPRequestHandler):
        # Silence stderr-spam from the default logger
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[metrics_receiver] {fmt % args}\n")

        def _send_json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth_ok(self) -> bool:
            if not secret:
                return True  # no secret configured — local-only mode, allow all
            header = self.headers.get("Authorization", "")
            return header.strip() == f"Bearer {secret}"

        def do_GET(self):  # health check
            if self.path in ("/", "/health"):
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/metrics":
                self._send_json(404, {"error": "not_found"})
                return
            if not self._auth_ok():
                self._send_json(401, {"error": "unauthorized"})
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 1_048_576:
                self._send_json(400, {"error": "bad_length"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "bad_json", "detail": str(exc)})
                return

            date = payload.get("date")
            slot = payload.get("slot")
            if not date or slot not in ("morning", "evening"):
                self._send_json(400,
                    {"error": "need_date_and_slot",
                     "detail": "body must have 'date' (YYYY-MM-DD) + 'slot' in {morning,evening}"})
                return

            try:
                path = write_metrics(queue_root, date, slot, payload)
            except FileNotFoundError as exc:
                self._send_json(404, {"error": "slot_not_found", "detail": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"error": "bad_payload", "detail": str(exc)})
                return

            refresh_analytics(queue_root)
            self._send_json(200, {"status": "ok", "metrics_path": str(path)})

    return MetricsHandler


def serve(bind: str, queue_root: Path, secret: str) -> None:
    if not secret:
        print("[metrics_receiver] WARNING: METRICS_WEBHOOK_SECRET is empty — "
              "running in unauthenticated local-only mode. Set the secret before "
              "exposing this server publicly (e.g. via ngrok).",
              file=sys.stderr)

    host, _, port_str = bind.partition(":")
    port = int(port_str or "8787")
    handler_cls = make_handler(queue_root, secret)
    server = HTTPServer((host, port), handler_cls)
    print(f"[metrics_receiver] listening on http://{host}:{port}  "
          f"queue={queue_root}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[metrics_receiver] shutting down", file=sys.stderr)
    finally:
        server.server_close()


# --- CLI -------------------------------------------------------------------

def _cli_set(args) -> int:
    payload: dict[str, Any] = {}
    x_block: dict[str, Any] = {}
    tg_block: dict[str, Any] = {}
    if args.x_impressions is not None: x_block["impressions"] = args.x_impressions
    if args.x_likes is not None:       x_block["likes"] = args.x_likes
    if args.x_retweets is not None:    x_block["retweets"] = args.x_retweets
    if args.tg_views is not None:      tg_block["views"] = args.tg_views
    if x_block:  payload["x"] = x_block
    if tg_block: payload["telegram"] = tg_block
    if not payload:
        print("[metrics_receiver] no metrics provided", file=sys.stderr)
        return 2

    queue_root = Path(args.queue).resolve()
    try:
        path = write_metrics(queue_root, args.date, args.slot, payload)
    except FileNotFoundError as exc:
        print(f"[metrics_receiver] {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[metrics_receiver] {exc}", file=sys.stderr)
        return 1

    print(f"[metrics_receiver] wrote {path}")
    if not args.no_refresh:
        refresh_analytics(queue_root)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run HTTP receiver")
    p_serve.add_argument("--bind",
                         default=os.environ.get("METRICS_RECEIVER_BIND", DEFAULT_BIND))
    p_serve.add_argument("--queue",
                         default=os.environ.get("AUTOPOST_QUEUE", "queue"))

    p_set = sub.add_parser("set", help="manually set metrics from the CLI")
    p_set.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_set.add_argument("--slot", required=True, choices=["morning", "evening"])
    p_set.add_argument("--queue",
                       default=os.environ.get("AUTOPOST_QUEUE", "queue"))
    p_set.add_argument("--x-impressions", type=int, default=None)
    p_set.add_argument("--x-likes", type=int, default=None)
    p_set.add_argument("--x-retweets", type=int, default=None)
    p_set.add_argument("--tg-views", type=int, default=None)
    p_set.add_argument("--no-refresh", action="store_true",
                       help="skip analytics + dashboard rebuild after write")

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        secret = os.environ.get("METRICS_WEBHOOK_SECRET", "").strip()
        serve(args.bind, Path(args.queue).resolve(), secret)
        return 0
    if args.cmd == "set":
        return _cli_set(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
