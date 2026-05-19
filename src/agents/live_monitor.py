"""live_monitor.py — Real-time health monitor with interactive Telegram bot.

Watches:
  - scheduler.log  (ERROR / CRASHED lines)
  - server_err.log (Python tracebacks)
  - publish_log.json files (failed platform posts)
  - systemd service status

On any issue, sends an instant Telegram alert with inline buttons.
Pressing a button triggers an auto-fix directly on the server.

Integrated into scheduler — starts automatically at boot.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent.parent

# Errors are re-reported every N seconds even if seen before (daily reset).
_DEDUP_TTL = 6 * 3600   # 6 hours


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tg_call(token: str, method: str, data: dict, timeout: int = 10) -> dict:
    url  = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(data).encode()
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, data=body), timeout=timeout
        )
        return json.loads(resp.read())
    except Exception:
        return {}


def _tg_get(token: str, method: str, params: dict, timeout: int = 10) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}?{urllib.parse.urlencode(params)}"
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# LiveMonitor
# ---------------------------------------------------------------------------

class LiveMonitor(threading.Thread):
    """Background daemon that watches logs and talks to the admin via Telegram."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="live-monitor")
        self._token:   str = ""
        self._chat_id: str = ""

        # pending_fixes: cb_data -> {action, msg_id, chat_id, original_text}
        self._pending: dict[str, dict] = {}
        self._in_progress: set[str] = set()

        # dedup: error_key -> timestamp when first sent
        self._sent: dict[str, float] = {}

        # last read position per log file
        self._log_pos: dict[str, int] = {}

        # Telegram update offset
        self._offset: int = 0

        self._lock = threading.Lock()

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        time.sleep(15)                          # let app fully boot
        self._sync_tg_offset()                  # skip old button presses
        self._reload_creds()

        # Callback polling thread
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="tg-poll").start()

        while True:
            try:
                self._reload_creds()
                if self._token and self._chat_id:
                    self._check_logs()
                    self._check_publish_logs()
                    self._purge_dedup()
            except Exception:
                pass
            time.sleep(30)

    # ── Credentials ────────────────────────────────────────────────────────

    def _reload_creds(self) -> None:
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = (os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
               or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
        if tok and cid:
            self._token   = tok
            self._chat_id = cid

    # ── Alert sending ───────────────────────────────────────────────────────

    def _send_alert(self, key: str, text: str,
                    fixes: list[dict] | None = None) -> None:
        """Send alert if not recently sent. fixes = [{label, action}, ...]"""
        now = time.time()
        with self._lock:
            last = self._sent.get(key, 0)
            if now - last < _DEDUP_TTL:
                return
            self._sent[key] = now

        markup = None
        cb_keys: list[str] = []
        if fixes:
            buttons: list[dict] = []
            for fix in fixes:
                cb = f"fix_{int(now)}_{len(self._pending)}"
                with self._lock:
                    self._pending[cb] = {
                        "action":        fix["action"],
                        "label":         fix["label"],
                        "original_text": text,
                        "msg_id":        None,
                        "chat_id":       self._chat_id,
                    }
                cb_keys.append(cb)
                buttons.append({"text": fix["label"], "callback_data": cb})
            buttons.append({"text": "❌ Ignoră", "callback_data": f"ign_{int(now)}"})
            markup = {"inline_keyboard": [buttons]}

        result = _tg_call(self._token, "sendMessage", {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "HTML",
            **({"reply_markup": json.dumps(markup)} if markup else {}),
        })

        msg_id = (result.get("result") or {}).get("message_id")
        if msg_id and cb_keys:
            with self._lock:
                for cb in cb_keys:
                    if cb in self._pending:
                        self._pending[cb]["msg_id"] = msg_id

    # ── Telegram callback polling ───────────────────────────────────────────

    def _sync_tg_offset(self) -> None:
        """Advance offset past any existing queued updates so stale buttons are ignored."""
        self._reload_creds()
        if not self._token:
            return
        data = _tg_get(self._token, "getUpdates", {"offset": -1, "limit": 1}, timeout=10)
        results = (data.get("result") or [])
        if results:
            self._offset = results[-1]["update_id"] + 1

    def _poll_loop(self) -> None:
        """Long-poll Telegram for callback_query updates (button presses)."""
        while True:
            if not self._token:
                time.sleep(10)
                continue
            try:
                data = _tg_get(self._token, "getUpdates", {
                    "offset":          self._offset,
                    "timeout":         30,
                    "allowed_updates": '["callback_query"]',
                }, timeout=40)

                for upd in (data.get("result") or []):
                    self._offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        threading.Thread(
                            target=self._handle_callback,
                            args=(upd["callback_query"],),
                            daemon=True,
                        ).start()
            except Exception:
                time.sleep(5)

    def _handle_callback(self, cb: dict) -> None:
        cb_id   = cb["id"]
        cb_data = cb.get("data", "")
        msg     = cb.get("message", {})
        msg_id  = msg.get("message_id")
        chat_id = msg.get("chat", {}).get("id", self._chat_id)

        if cb_data.startswith("ign_"):
            _tg_call(self._token, "answerCallbackQuery",
                     {"callback_query_id": cb_id, "text": "Ignorat ✓"})
            # Remove inline keyboard and mark message as ignored
            if msg_id:
                original_text = msg.get("text", "")
                _tg_call(self._token, "editMessageText", {
                    "chat_id":    chat_id,
                    "message_id": msg_id,
                    "text":       original_text + "\n\n❌ <i>Ignorat</i>",
                    "parse_mode": "HTML",
                })
            return

        if not cb_data.startswith("fix_"):
            _tg_call(self._token, "answerCallbackQuery",
                     {"callback_query_id": cb_id, "text": "Comandă necunoscută"})
            return

        with self._lock:
            fix = self._pending.get(cb_data)

        if not fix:
            _tg_call(self._token, "answerCallbackQuery",
                     {"callback_query_id": cb_id, "text": "Fix-ul nu mai e disponibil"})
            return

        if cb_data in self._in_progress:
            _tg_call(self._token, "answerCallbackQuery",
                     {"callback_query_id": cb_id, "text": "⏳ Deja în execuție..."})
            return

        self._in_progress.add(cb_data)
        _tg_call(self._token, "answerCallbackQuery",
                 {"callback_query_id": cb_id, "text": "⏳ Se rezolvă..."})

        result_text = self._execute_fix(fix["action"])

        # Edit the original message to show result
        final_msg = (f"{fix['original_text']}\n\n"
                     f"━━━━━━━━━━━━━━━━━\n"
                     f"🔧 <b>{fix['label']}</b>\n"
                     f"✅ {result_text}")
        _tg_call(self._token, "editMessageText", {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       final_msg[:4000],
            "parse_mode": "HTML",
        })

        with self._lock:
            self._pending.pop(cb_data, None)
        self._in_progress.discard(cb_data)

    # ── Fix executors ───────────────────────────────────────────────────────

    def _execute_fix(self, action: str) -> str:
        try:
            if action == "restart_service":
                r = subprocess.run(
                    ["systemctl", "restart", "autopost"],
                    capture_output=True, text=True, timeout=30,
                )
                return "Serviciu restartat ✓" if r.returncode == 0 else f"Eroare: {r.stderr[:120]}"

            if action == "clear_locks":
                locks = list(_REPO.glob("scheduler_*.lock"))
                for lock in locks:
                    lock.unlink(missing_ok=True)
                return f"{len(locks)} lock(uri) șterse ✓"

            if action.startswith("retry:"):
                # retry:user_id:slot_name:platform
                _, uid_s, slot, plat = action.split(":", 3)
                return self._fix_retry_publish(int(uid_s), slot, plat)

            if action.startswith("reset_perm:"):
                _, log_path_s = action.split(":", 1)
                return self._fix_reset_perm_failed(Path(log_path_s))

            return f"Acțiune necunoscută: {action}"

        except Exception as exc:
            return f"Eroare la fix: {str(exc)[:150]}"

    def _fix_retry_publish(self, user_id: int, slot_name: str, platform: str) -> str:
        try:
            import sys
            if str(_REPO) not in sys.path:
                sys.path.insert(0, str(_REPO))
            from src.dashboard.database import get_user_settings
            from src.agents.user_pipeline_manager import _build_user_env
            from src.agents.publisher import publish

            settings   = get_user_settings(user_id)
            queue_root = _REPO / "queue" / str(user_id)
            env        = _build_user_env(settings, user_id, queue_root)
            for k, v in env.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

            slot_dir = queue_root / slot_name
            if not slot_dir.exists():
                return f"Slot {slot_name} nu există"

            result     = publish(slot_dir, platforms={platform}, dry_run=False)
            plat_res   = result.get("results", {}).get(platform, {})
            status     = plat_res.get("status", "?")
            url        = plat_res.get("url", "")
            err        = plat_res.get("error", "")[:120]

            if status == "ok":
                return f"Publicat pe {platform.upper()} ✓ {url}"
            return f"Eșuat: {err}"
        except Exception as exc:
            return f"Eroare retry: {str(exc)[:120]}"

    def _fix_reset_perm_failed(self, log_path: Path) -> str:
        try:
            data    = json.loads(log_path.read_text(encoding="utf-8"))
            changed = 0
            for res in data.get("results", {}).values():
                if res.get("permanently_failed"):
                    res.pop("permanently_failed", None)
                    res.pop("permanently_failed_reason", None)
                    res["retry_count"] = 0
                    changed += 1
            log_path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                encoding="utf-8")
            return f"{changed} platformă(e) resetate ✓ — retry automat în ~15 min"
        except Exception as exc:
            return f"Eroare reset: {str(exc)[:120]}"

    # ── Log file watchers ───────────────────────────────────────────────────

    def _check_logs(self) -> None:
        for log_file in [_REPO / "scheduler.log", _REPO / "server_err.log"]:
            if not log_file.exists():
                continue
            try:
                size = log_file.stat().st_size
                # On first check, only look at the last 4 KB to avoid spamming
                # old errors from before this monitor started.
                last = self._log_pos.get(str(log_file),
                                         max(0, size - 4096))
                if size <= last:
                    self._log_pos[str(log_file)] = size
                    continue
                with log_file.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(last)
                    new_text = f.read()
                self._log_pos[str(log_file)] = size

                for line in new_text.splitlines():
                    if any(kw in line for kw in ("[ERROR]", "CRASHED", "Traceback",
                                                  "slot=morning -> error",
                                                  "slot=evening -> error")):
                        self._process_log_error(line.strip(), log_file.name)
            except Exception:
                pass

    def _process_log_error(self, line: str, source: str) -> None:
        key = f"log:{source}:{line[:80]}"
        ts  = datetime.now().strftime("%H:%M:%S")

        text = (f"🚨 <b>AutoPost — Eroare detectată</b>\n"
                f"📋 <b>Fișier:</b> {source}\n"
                f"⏰ <b>Ora:</b> {ts}\n"
                f"📝 <b>Linie:</b>\n<code>{line[:300]}</code>")

        fixes: list[dict] = []
        low = line.lower()
        if "crashed" in low or "slot" in low and "error" in low:
            fixes.append({"label": "🔄 Restart serviciu", "action": "restart_service"})
        if "lock" in low:
            fixes.append({"label": "🔓 Curăță lock-uri", "action": "clear_locks"})

        self._send_alert(key, text, fixes or None)

    # ── Publish log watcher ─────────────────────────────────────────────────

    def _check_publish_logs(self) -> None:
        queue_root = _REPO / "queue"
        if not queue_root.exists():
            return
        cutoff = time.time() - 3600   # only check logs from last hour
        try:
            for log_path in queue_root.glob("*/*/publish_log.json"):
                if log_path.stat().st_mtime < cutoff:
                    continue
                self._process_publish_log(log_path)
        except Exception:
            pass

    def _process_publish_log(self, log_path: Path) -> None:
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("dry_run"):
            return

        slot_name = log_path.parent.name
        try:
            user_id = int(log_path.parent.parent.name)
        except Exception:
            user_id = 0

        for plat, res in data.get("results", {}).items():
            if res.get("status") != "error":
                continue
            if res.get("skipped_duplicate"):
                continue

            err  = (res.get("error") or "eroare necunoscută")[:200]
            key  = f"pub:{slot_name}:{plat}:{err[:50]}"
            ts   = datetime.now().strftime("%H:%M:%S")
            perm = res.get("permanently_failed", False)

            text = (f"❌ <b>Postare eșuată</b>\n"
                    f"📌 <b>Slot:</b> <code>{slot_name}</code>\n"
                    f"📱 <b>Platformă:</b> <b>{plat.upper()}</b>\n"
                    f"⏰ <b>Ora:</b> {ts}\n"
                    f"🔁 <b>Încercări:</b> {res.get('retry_count', 0)}\n"
                    f"💬 <b>Eroare:</b>\n<code>{err}</code>")

            if perm:
                fixes = [{
                    "label":  f"🔄 Resetează & reîncearcă {plat.upper()}",
                    "action": f"reset_perm:{log_path}",
                }]
            else:
                fixes = [{
                    "label":  f"🔁 Reîncearcă acum {plat.upper()}",
                    "action": f"retry:{user_id}:{slot_name}:{plat}",
                }]

            self._send_alert(key, text, fixes)

    # ── Dedup cleanup ───────────────────────────────────────────────────────

    def _purge_dedup(self) -> None:
        cutoff = time.time() - _DEDUP_TTL
        with self._lock:
            stale = [k for k, t in self._sent.items() if t < cutoff]
            for k in stale:
                del self._sent[k]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_monitor: LiveMonitor | None = None
_monitor_lock = threading.Lock()


def start_live_monitor() -> None:
    """Start the live monitor daemon. Safe to call multiple times."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None and _monitor.is_alive():
            return
        _monitor = LiveMonitor()
        _monitor.start()
