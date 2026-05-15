"""billing_agent.py — Stripe subscription management for AutoPost SaaS.

Plans: trial | free | starter ($19) | pro ($49) | elite ($149)
Each plan enforces limits via PLAN_LIMITS.
Stripe keys come from .env — if missing, checkout returns a setup error.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------

PLAN_LIMITS: dict[str, dict[str, Any]] = {
    "trial": {
        "platforms":            {"telegram"},
        "slots_per_day":        1,
        "niches":               1,
        "video":                False,
        "avatar":               False,
        "heygen":               False,
        "viral_scout":          False,
        "competitor_accounts":  0,
        "ab_testing":           False,
        "ad_studio":            False,
        "web_learner":          False,
        "trial_days":           14,
    },
    "free": {
        "platforms":            {"telegram"},
        "slots_per_day":        1,
        "niches":               1,
        "video":                False,
        "avatar":               False,
        "heygen":               False,
        "viral_scout":          False,
        "competitor_accounts":  0,
        "ab_testing":           False,
        "ad_studio":            False,
        "web_learner":          False,
    },
    "starter": {
        "platforms":            {"telegram", "x"},
        "slots_per_day":        2,
        "niches":               1,
        "video":                True,
        "avatar":               False,
        "heygen":               False,
        "viral_scout":          "basic",
        "competitor_accounts":  0,
        "ab_testing":           False,
        "ad_studio":            False,
        "web_learner":          False,
    },
    "pro": {
        "platforms":            {"telegram", "x", "instagram", "tiktok", "youtube", "facebook"},
        "slots_per_day":        2,
        "niches":               3,
        "video":                True,
        "avatar":               True,
        "heygen":               False,
        "viral_scout":          "full",
        "competitor_accounts":  5,
        "ab_testing":           True,
        "ad_studio":            True,
        "web_learner":          True,
    },
    "elite": {
        "platforms":            {"telegram", "x", "instagram", "tiktok", "youtube", "facebook"},
        "slots_per_day":        99,
        "niches":               10,
        "video":                True,
        "avatar":               True,
        "heygen":               True,
        "viral_scout":          "full",
        "competitor_accounts":  999,
        "ab_testing":           True,
        "ad_studio":            True,
        "web_learner":          True,
    },
}

PLAN_PRICES = {
    "starter": {"monthly": 19,  "annual": 15},
    "pro":     {"monthly": 49,  "annual": 39},
    "elite":   {"monthly": 149, "annual": 119},
}

VALID_PLANS   = {"trial", "free", "starter", "pro", "elite"}
PAID_PLANS    = {"starter", "pro", "elite"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_plan_limits(plan: str) -> dict[str, Any]:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])


def stripe_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def is_plan_active(settings: dict) -> bool:
    """Return True if user's plan has not expired."""
    plan    = settings.get("plan", "trial")
    expires = settings.get("plan_expires_at", "")
    if not expires:
        return True
    try:
        exp = datetime.fromisoformat(expires)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < exp
    except Exception:
        return True


def get_billing_summary(user_id: int) -> dict[str, Any]:
    from src.dashboard.database import get_user_settings
    settings = get_user_settings(user_id)
    plan     = settings.get("plan", "trial")
    return {
        "plan":        plan,
        "expires_at":  settings.get("plan_expires_at", ""),
        "active":      is_plan_active(settings),
        "limits":      get_plan_limits(plan),
        "stripe_ok":   stripe_configured(),
        "customer_id": settings.get("stripe_customer_id", ""),
    }


def _price_ids() -> dict[str, str]:
    return {
        "starter_monthly": os.environ.get("STRIPE_PRICE_STARTER_MONTHLY", ""),
        "starter_annual":  os.environ.get("STRIPE_PRICE_STARTER_ANNUAL",  ""),
        "pro_monthly":     os.environ.get("STRIPE_PRICE_PRO_MONTHLY",     ""),
        "pro_annual":      os.environ.get("STRIPE_PRICE_PRO_ANNUAL",      ""),
        "elite_monthly":   os.environ.get("STRIPE_PRICE_ELITE_MONTHLY",   ""),
        "elite_annual":    os.environ.get("STRIPE_PRICE_ELITE_ANNUAL",    ""),
    }


def _stripe():
    import stripe as _s
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set in .env")
    _s.api_key = key
    return _s


def _get_or_create_customer(s, user_id: int, email: str) -> str:
    from src.dashboard.database import get_user_settings, save_user_settings
    settings    = get_user_settings(user_id)
    customer_id = settings.get("stripe_customer_id", "").strip()
    if customer_id:
        return customer_id
    cust = s.Customer.create(email=email, metadata={"user_id": str(user_id)})
    settings["stripe_customer_id"] = cust["id"]
    save_user_settings(user_id, settings)
    return cust["id"]


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def create_checkout_session(
    user_id:     int,
    user_email:  str,
    plan:        str,
    billing:     str,
    success_url: str,
    cancel_url:  str,
) -> str:
    """Return Stripe Checkout URL for the requested plan."""
    if plan not in PAID_PLANS:
        raise ValueError(f"Plan '{plan}' is not a paid plan")
    if billing not in ("monthly", "annual"):
        raise ValueError("billing must be 'monthly' or 'annual'")

    prices    = _price_ids()
    price_key = f"{plan}_{billing}"
    price_id  = prices.get(price_key, "").strip()
    if not price_id:
        raise ValueError(
            f"Stripe price not configured: add STRIPE_PRICE_{plan.upper()}_{billing.upper()} to .env"
        )

    s           = _stripe()
    customer_id = _get_or_create_customer(s, user_id, user_email)

    session = s.checkout.Session.create(
        customer            = customer_id,
        payment_method_types= ["card"],
        mode                = "subscription",
        line_items          = [{"price": price_id, "quantity": 1}],
        success_url         = success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url          = cancel_url,
        metadata            = {"user_id": str(user_id), "plan": plan, "billing": billing},
        subscription_data   = {"metadata": {"user_id": str(user_id), "plan": plan}},
        allow_promotion_codes = True,
    )
    return session.url


def create_portal_session(user_id: int, return_url: str) -> str:
    """Return Stripe Customer Portal URL (manage/cancel subscription)."""
    from src.dashboard.database import get_user_settings
    s           = _stripe()
    settings    = get_user_settings(user_id)
    customer_id = settings.get("stripe_customer_id", "").strip()
    if not customer_id:
        raise ValueError("No Stripe customer linked to this account")
    session = s.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session.url


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

def handle_webhook(payload: bytes, sig_header: str) -> dict[str, str]:
    s              = _stripe()
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set in .env")

    try:
        event = s.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as exc:
        return {"status": "error", "msg": str(exc)}

    etype = event["type"]
    obj   = event["data"]["object"]

    if etype == "checkout.session.completed":
        _on_checkout_completed(obj)
    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        _on_subscription_updated(obj)
    elif etype == "customer.subscription.deleted":
        _on_subscription_deleted(obj)
    elif etype == "invoice.payment_failed":
        _on_payment_failed(obj)
    else:
        return {"status": "ignored", "msg": etype}

    return {"status": "ok", "msg": etype}


def _on_checkout_completed(session: dict) -> None:
    meta    = session.get("metadata", {})
    user_id = int(meta.get("user_id", 0) or 0)
    plan    = meta.get("plan", "")
    if user_id and plan:
        _activate_plan(user_id, plan)


def _on_subscription_updated(sub: dict) -> None:
    meta    = sub.get("metadata", {})
    user_id = int(meta.get("user_id", 0) or 0)
    plan    = meta.get("plan", "")
    status  = sub.get("status", "")
    if not user_id:
        return
    if status in ("active", "trialing"):
        if plan:
            _activate_plan(user_id, plan)
    elif status in ("canceled", "unpaid", "past_due"):
        _deactivate_plan(user_id)


def _on_subscription_deleted(sub: dict) -> None:
    meta    = sub.get("metadata", {})
    user_id = int(meta.get("user_id", 0) or 0)
    if user_id:
        _deactivate_plan(user_id)


def _on_payment_failed(invoice: dict) -> None:
    customer_id = invoice.get("customer", "")
    try:
        from src.dashboard.database import get_all_users, get_user_settings
        for u in get_all_users():
            s = get_user_settings(u["id"])
            if s.get("stripe_customer_id") == customer_id:
                _alert_payment_failed(u["email"], u["id"])
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Plan activation / deactivation
# ---------------------------------------------------------------------------

def _activate_plan(user_id: int, plan: str) -> None:
    from src.dashboard.database import get_user_settings, save_user_settings
    settings = get_user_settings(user_id)
    settings["plan"] = plan
    # Expires 35 days from now — renewed each billing cycle via webhook
    exp = (datetime.now(timezone.utc) + timedelta(days=35)).isoformat()
    settings["plan_expires_at"] = exp
    save_user_settings(user_id, settings)
    print(f"[billing] user {user_id} → plan={plan} active until {exp[:10]}")


def _deactivate_plan(user_id: int) -> None:
    from src.dashboard.database import get_user_settings, save_user_settings
    settings = get_user_settings(user_id)
    settings["plan"] = "free"
    settings["plan_expires_at"] = ""
    save_user_settings(user_id, settings)
    print(f"[billing] user {user_id} → downgraded to free")


# ---------------------------------------------------------------------------
# Admin: manual plan set (no Stripe)
# ---------------------------------------------------------------------------

def admin_set_plan(user_id: int, plan: str, days: int = 30) -> None:
    """Manually activate a plan (admin use, no Stripe payment)."""
    if plan not in VALID_PLANS:
        raise ValueError(f"Invalid plan: {plan}")
    if plan in PAID_PLANS:
        _activate_plan(user_id, plan)
        # Override expiry to exact days requested
        from src.dashboard.database import get_user_settings, save_user_settings
        settings = get_user_settings(user_id)
        exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        settings["plan_expires_at"] = exp
        save_user_settings(user_id, settings)
    else:
        _deactivate_plan(user_id) if plan == "free" else None
        if plan == "trial":
            from src.dashboard.database import get_user_settings, save_user_settings
            settings = get_user_settings(user_id)
            settings["plan"] = "trial"
            exp = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
            settings["plan_expires_at"] = exp
            save_user_settings(user_id, settings)


# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Crypto payments — USDC on Solana
# ---------------------------------------------------------------------------

MERCHANT_WALLET = "8znfTyUF6JBEfz9eyGJ2MpEJkVcSurggnVmMSmmt8EKR"
USDC_MINT       = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS   = 6
SOLANA_RPC      = "https://api.mainnet-beta.solana.com"

# USDC amounts per plan/billing (in micro-USDC: $1 = 1_000_000)
USDC_PRICES: dict[str, dict[str, int]] = {
    "starter": {"monthly": 19_000_000,  "annual": 180_000_000},
    "pro":     {"monthly": 49_000_000,  "annual": 468_000_000},
    "elite":   {"monthly": 149_000_000, "annual": 1_428_000_000},
}

# Human-readable USD prices (for display)
USDC_PRICES_USD: dict[str, dict[str, float]] = {
    "starter": {"monthly": 19,  "annual": 180},
    "pro":     {"monthly": 49,  "annual": 468},
    "elite":   {"monthly": 149, "annual": 1428},
}


def _sol_rpc(method: str, params: list) -> dict:
    import json, urllib.request as _ur
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = _ur.Request(SOLANA_RPC, data=payload, headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def verify_solana_usdc_tx(tx_sig: str, plan: str, billing: str) -> dict[str, Any]:
    """
    Verify a Solana transaction sent USDC to MERCHANT_WALLET.
    Returns {"ok": True, "amount_usdc": int, "amount_usd": float}
           or {"ok": False, "error": str}
    """
    tx_sig = tx_sig.strip()
    if not tx_sig or len(tx_sig) < 40:
        return {"ok": False, "error": "Invalid transaction signature format."}

    required = USDC_PRICES.get(plan, {}).get(billing, 0)
    if not required:
        return {"ok": False, "error": f"Unknown plan/billing: {plan}/{billing}"}

    # Check already used
    try:
        import sqlite3
        from pathlib import Path
        _db = Path(__file__).resolve().parent.parent / "dashboard" / "autopost.db"
        with sqlite3.connect(str(_db)) as conn:
            row = conn.execute(
                "SELECT user_id FROM crypto_payments WHERE tx_signature=?", (tx_sig,)
            ).fetchone()
            if row:
                return {"ok": False, "error": "This transaction was already used to activate a plan."}
    except Exception:
        pass  # DB check failure is non-blocking

    try:
        resp = _sol_rpc("getTransaction", [
            tx_sig,
            {"encoding": "jsonParsed", "commitment": "confirmed",
             "maxSupportedTransactionVersion": 0}
        ])
    except Exception as exc:
        return {"ok": False, "error": f"Could not reach Solana RPC: {exc}"}

    result = resp.get("result")
    if not result:
        return {"ok": False, "error": "Transaction not found. Wait for confirmation and try again."}

    if result.get("meta", {}).get("err"):
        return {"ok": False, "error": "Transaction failed on-chain."}

    # Find USDC received by MERCHANT_WALLET via token balance diff
    pre_balances  = {b["accountIndex"]: b for b in result.get("meta", {}).get("preTokenBalances",  [])}
    post_balances = {b["accountIndex"]: b for b in result.get("meta", {}).get("postTokenBalances", [])}

    received = 0
    for idx, pb in post_balances.items():
        if pb.get("mint") != USDC_MINT:
            continue
        if pb.get("owner", "") != MERCHANT_WALLET:
            continue
        pre_amt  = int((pre_balances.get(idx) or {}).get("uiTokenAmount", {}).get("amount", "0") or "0")
        post_amt = int(pb.get("uiTokenAmount", {}).get("amount", "0") or "0")
        delta = post_amt - pre_amt
        if delta > received:
            received = delta

    if received <= 0:
        return {
            "ok":    False,
            "error": f"No USDC transfer to {MERCHANT_WALLET[:8]}... found in this transaction. "
                     f"Make sure you sent USDC (not SOL) to the correct address.",
        }

    required_usd = required / 10 ** USDC_DECIMALS
    received_usd = received / 10 ** USDC_DECIMALS

    if received < required:
        return {
            "ok":    False,
            "error": f"Amount too low: received ${received_usd:.2f} USDC, required ${required_usd:.2f} USDC.",
        }

    return {"ok": True, "amount_usdc": received, "amount_usd": received_usd}


def activate_crypto_plan(user_id: int, tx_sig: str, plan: str, billing: str) -> dict[str, Any]:
    """Verify tx on-chain and activate plan for user. Returns status dict."""
    result = verify_solana_usdc_tx(tx_sig, plan, billing)
    if not result["ok"]:
        return {"status": "error", "error": result["error"]}

    # Record tx in DB (prevents reuse)
    try:
        import sqlite3
        from pathlib import Path
        _db = Path(__file__).resolve().parent.parent / "dashboard" / "autopost.db"
        with sqlite3.connect(str(_db)) as conn:
            conn.execute(
                "INSERT INTO crypto_payments (user_id, tx_signature, plan, billing, amount_usdc) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, tx_sig.strip(), plan, billing, result["amount_usdc"])
            )
    except Exception as exc:
        # If already recorded by another request, treat as duplicate
        if "UNIQUE" in str(exc).upper():
            return {"status": "error", "error": "This transaction was already used."}
        return {"status": "error", "error": f"DB error: {exc}"}

    # Activate plan
    _activate_plan(user_id, plan)

    # Alert admin
    _alert_crypto_payment(user_id, plan, billing, result["amount_usd"], tx_sig)

    return {
        "status":     "ok",
        "plan":       plan,
        "billing":    billing,
        "amount_usd": result["amount_usd"],
    }


def _alert_crypto_payment(user_id: int, plan: str, billing: str, amount: float, tx_sig: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.request as _ur, urllib.parse as _up
        text = (f"✅ Crypto Payment Received\n"
                f"👤 User {user_id} → {plan.upper()} {billing}\n"
                f"💰 ${amount:.2f} USDC on Solana\n"
                f"🔗 {tx_sig[:20]}...")
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = _up.urlencode({"chat_id": chat_id, "text": text}).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=8)
    except Exception:
        pass


def _alert_payment_failed(email: str, user_id: int) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.request as _ur, urllib.parse as _up
        text = (f"⚠️ Stripe Payment FAILED\n"
                f"👤 {email} (user {user_id})\n"
                f"Subscription may be cancelled soon.")
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = _up.urlencode({"chat_id": chat_id, "text": text}).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=8)
    except Exception:
        pass
