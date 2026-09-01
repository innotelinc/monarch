"""
billing-api - Stripe webhook -> Authentik "paid users" provisioning.

Receives Stripe webhook events, records them in Postgres and mirrors the
payment state into Authentik:

  * checkout.session.completed   -> ensure the Authentik user exists
                                    (created from email/username if needed)
                                    and add them to the paid_users group
  * customer.subscription.updated-> refresh status / current period end;
                                    canceled/unpaid/incomplete_expired also
                                    disable the Authentik user (access off)
  * customer.subscription.deleted-> disable the Authentik user (access off,
                                    matching the subscription platform)
  * invoice.payment_succeeded    -> subscription back to active, user in group
  * invoice.payment_failed       -> subscription marked past_due

Signature verification uses `stripe.Webhook.construct_event`, so the Stripe
signature header is mandatory.

Configuration comes from environment variables (see docker-compose.yml).

Run:  uvicorn main:app --host 0.0.0.0 --port 8001
"""

import asyncio
import os
import re
import time

import httpx
import psycopg
import stripe
from fastapi import FastAPI, Header, HTTPException, Request

# ---------------------------------------------------------------------------
# Configuration (env)
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
# Prefer the dedicated endpoint secret; fall back to the shared platform secret.
STRIPE_WEBHOOK_SECRET = (
    os.environ.get("BILLING_WEBHOOK_SECRET", "").strip()
    or os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
)
BILLING_API_KEY = os.environ.get("BILLING_API_KEY", "").strip()
BILLING_BASE_URL = os.environ.get("BILLING_BASE_URL", "http://localhost:8001")

AUTHENTIK_BASE_URL = os.environ.get("AUTHENTIK_BASE_URL", "").strip().rstrip("/")
AUTHENTIK_BOOTSTRAP_TOKEN = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "").strip()
AUTHENTIK_DEFAULT_GROUP = os.environ.get("AUTHENTIK_DEFAULT_GROUP", "paid_users")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://billing:BillingDB!2026@billing-postgres:5432/billing",
)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="ARR Billing API", version="1.0.0")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id                   SERIAL PRIMARY KEY,
    stripe_customer_id   TEXT UNIQUE NOT NULL,
    email                TEXT,
    username             TEXT,
    authentik_user_id    TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                    SERIAL PRIMARY KEY,
    stripe_subscription_id TEXT UNIQUE NOT NULL,
    stripe_customer_id    TEXT NOT NULL REFERENCES customers(stripe_customer_id),
    price_id              TEXT,
    status                TEXT,
    current_period_end    TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def db_init() -> None:
    """Try to create the schema at startup; do not crash if the DB is not up yet."""
    deadline = time.time() + 60
    last_error = None
    while time.time() < deadline:
        try:
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA)
            return
        except Exception as exc:  # noqa: BLE001 - startup resilience
            last_error = exc
            time.sleep(5)
    print(f"[billing-api] WARNING: database not reachable at startup: {last_error}")


def db() -> psycopg.Connection:
    """Open a connection (autocommit) - Postgres is the source of truth."""
    conn = psycopg.connect(DATABASE_URL, connect_timeout=5)
    conn.autocommit = True
    return conn


def record_event(event_id: str, event_type: str) -> bool:
    """Insert the event id for dedupe. Returns False if it was seen already."""
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO webhook_events (event_id, type) VALUES (%s, %s) "
                    "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    (event_id, event_type),
                )
                return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 - never fail the webhook on a DB hiccup
        print(f"[billing-api] WARNING: could not record event {event_id}: {exc}")
        return True


def upsert_customer(customer_id: str, email: str | None, username: str | None,
                    authentik_user_id: str | None) -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (stripe_customer_id, email, username, authentik_user_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (stripe_customer_id) DO UPDATE SET
                    email = COALESCE(EXCLUDED.email, customers.email),
                    username = COALESCE(EXCLUDED.username, customers.username),
                    authentik_user_id = COALESCE(EXCLUDED.authentik_user_id,
                                                 customers.authentik_user_id)
                """,
                (customer_id, email, username, authentik_user_id),
            )


def set_authentik_user_for_customer(customer_id: str, user_id: str | None) -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE customers SET authentik_user_id = %s WHERE stripe_customer_id = %s",
                (user_id, customer_id),
            )


def get_customer_authentik_user(customer_id: str) -> str | None:
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT authentik_user_id FROM customers WHERE stripe_customer_id = %s",
                    (customer_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        print(f"[billing-api] WARNING: could not read customer {customer_id}: {exc}")
        return None


def upsert_subscription(subscription_id: str, customer_id: str, price_id: str | None,
                        status: str | None, current_period_end: int | None) -> None:
    period = None
    if current_period_end:
        period = psycopg.Timestamp.fromtimestamp(float(current_period_end))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscriptions
                    (stripe_subscription_id, stripe_customer_id, price_id, status,
                     current_period_end)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                    price_id = COALESCE(EXCLUDED.price_id, subscriptions.price_id),
                    status = COALESCE(EXCLUDED.status, subscriptions.status),
                    current_period_end = COALESCE(EXCLUDED.current_period_end,
                                                  subscriptions.current_period_end),
                    updated_at = now()
                """,
                (subscription_id, customer_id, price_id, status, period),
            )


# ---------------------------------------------------------------------------
# Authentik helpers
# ---------------------------------------------------------------------------


def ak_headers() -> dict:
    return {
        "Authorization": f"Bearer {AUTHENTIK_BOOTSTRAP_TOKEN}",
        "Accept": "application/json",
    }


def ak_request(method: str, path: str, *, json_body: dict | None = None,
               params: dict | None = None) -> httpx.Response:
    """Authentik API call; raises on transport errors, returns the response."""
    if not AUTHENTIK_BASE_URL or not AUTHENTIK_BOOTSTRAP_TOKEN:
        raise HTTPException(status_code=503, detail="Authentik not configured")
    r = httpx.request(method, f"{AUTHENTIK_BASE_URL}/api/v3{path}",
                      json=json_body, params=params,
                      headers=ak_headers(), timeout=15)
    return r


def ak_find_user(pk: int | None = None, email: str | None = None,
                 username: str | None = None) -> dict | None:
    params = {}
    if pk is not None:
        params["id"] = pk
    if email:
        params["email"] = email
    if username:
        params["username"] = username
    r = ak_request("GET", "/core/users/", params=params)
    if r.status_code == 200:
        results = r.json().get("results", [])
        return results[0] if results else None
    return None


def ak_ensure_user(email: str | None, username: str) -> dict:
    """Find by username or email; create the Authentik user if missing."""
    existing = ak_find_user(username=username)
    if not existing and email:
        existing = ak_find_user(email=email)
    if existing:
        return existing
    body = {
        "username": username,
        "name": username,
        "email": email or "",
        "is_active": True,
    }
    r = ak_request("POST", "/core/users/", json_body=body)
    if r.status_code not in (201, 200):
        raise HTTPException(status_code=502,
                            detail=f"Authentik user creation failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def ak_group(name: str) -> dict:
    r = ak_request("GET", "/core/groups/", params={"name": name})
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0]
    body = {"name": name}
    r = ak_request("POST", "/core/groups/", json_body=body)
    if r.status_code not in (201, 200):
        raise HTTPException(status_code=502,
                            detail=f"Authentik group creation failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def ak_group_add_user(group_id: int, user_id: int) -> None:
    r = ak_request("POST", f"/core/groups/{group_id}/add_user/",
                   json_body={"pk": [user_id]})
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=502,
                            detail=f"Authentik group add failed ({r.status_code}): {r.text[:300]}")


def ak_set_user_active(user_id: int, active: bool) -> None:
    r = ak_request("PATCH", f"/core/users/{user_id}/", json_body={"is_active": active})
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=502,
                            detail=f"Authentik user update failed ({r.status_code}): {r.text[:300]}")


def provision_paid_user(email: str | None, username: str) -> str:
    """Ensure the user exists in Authentik and is a member of paid_users."""
    if not AUTHENTIK_DEFAULT_GROUP:
        return ""
    user = ak_ensure_user(email=email, username=username)
    group = ak_group(AUTHENTIK_DEFAULT_GROUP)
    ak_group_add_user(int(group["pk"]), int(user["pk"]))
    return str(user["pk"])


def revoke_paid_user(authentik_user_id: str | None) -> None:
    if not authentik_user_id:
        return
    try:
        ak_set_user_active(int(authentik_user_id), False)
    except HTTPException as exc:
        print(f"[billing-api] WARNING: could not disable Authentik user {authentik_user_id}: {exc.detail}")


def safe_username(raw: str | None, email: str | None) -> str:
    candidate = raw or ""
    if not candidate and email:
        candidate = email.split("@")[0]
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "", candidate).strip("._-")
    return candidate[:150] or "user"


def subscription_price_id(sub: dict) -> str | None:
    """Extract the price id from a Subscription object's first line item."""
    items = sub.get("items") or {}
    data = items.get("data") or []
    if not data:
        return None
    price = data[0].get("price") or {}
    return price.get("id")


# Subscription statuses that mean the subscriber has lost access and should be
# disabled in Authentik. past_due is deliberately NOT here: Stripe retries the
# payment during the grace period, so the user keeps access until it ends.
REVOKED_STATUSES = ("canceled", "unpaid", "incomplete_expired")
ACTIVE_STATUSES = ("active", "trialing")


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------


def process_event(event: dict) -> dict:
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        customer = obj.get("customer")
        email = obj.get("customer_details", {}).get("email") or None
        meta = obj.get("metadata") or {}
        username = safe_username(meta.get("username") or meta.get("jellyfin_username"), email)
        # Note: line_items are not expanded on webhook payloads, so the price
        # is best taken from the configured STRIPE_PRICE_ID or checkout metadata.
        price_id = meta.get("price_id") or os.environ.get("STRIPE_PRICE_ID") or None
        upsert_customer(customer, email, username, None)
        user_id = provision_paid_user(email, username)
        set_authentik_user_for_customer(customer, user_id)
        sub = obj.get("subscription")
        if sub:
            # Period/status get corrected by the follow-up
            # customer.subscription.created/updated events.
            upsert_subscription(sub, customer, price_id, "active", None)
        return {"action": "provisioned", "user": username}

    if etype == "customer.subscription.updated":
        customer = obj.get("customer")
        status = obj.get("status")
        upsert_subscription(obj.get("id"), customer,
                            subscription_price_id(obj), status,
                            obj.get("current_period_end"))
        user_id = get_customer_authentik_user(customer)
        if user_id:
            if status in ACTIVE_STATUSES:
                ak_set_user_active(int(user_id), True)
            elif status in REVOKED_STATUSES:
                revoke_paid_user(user_id)
        return {"action": "updated", "status": status}

    if etype == "customer.subscription.deleted":
        customer = obj.get("customer")
        upsert_subscription(obj.get("id"), customer, None, "canceled", None)
        user_id = get_customer_authentik_user(customer)
        if user_id:
            revoke_paid_user(user_id)
        return {"action": "revoked"}

    if etype == "invoice.payment_succeeded":
        customer = obj.get("customer")
        sub = obj.get("subscription")
        try:
            user_id = None
            if sub:
                upsert_subscription(sub, customer, None, "active",
                                    obj.get("period_end"))
                user_id = get_customer_authentik_user(customer)
            # First payment may arrive before checkout.session.completed: build
            # the user from the invoice's billing details as a fallback.
            if not user_id:
                email = obj.get("customer_email") or (obj.get("customer_details") or {}).get("email") or None
                username = safe_username(meta_username(obj), email)
                user_id = provision_paid_user(email, username)
                set_authentik_user_for_customer(customer, user_id)
            return {"action": "paid", "status": "active"}
        except Exception as exc:  # noqa: BLE001
            print(f"[billing-api] WARNING: invoice.payment_succeeded handling failed: {exc}")
            return {"action": "recorded"}

    if etype == "invoice.payment_failed":
        customer = obj.get("customer")
        sub = obj.get("subscription")
        if sub:
            upsert_subscription(sub, customer, None, "past_due", None)
        return {"action": "past_due"}

    return {"action": "ignored", "type": etype}


def meta_username(obj: dict) -> str | None:
    meta = obj.get("metadata") or {}
    return meta.get("username") or meta.get("jellyfin_username")


@app.post("/api/webhook")
async def webhook(request: Request, stripe_signature: str = Header(default="")) -> dict:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503,
                            detail="STRIPE_WEBHOOK_SECRET is not configured (.env)")
    raw = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload=raw, sig_header=stripe_signature, secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as exc:  # noqa: BLE001 - invalid signature
        raise HTTPException(status_code=400, detail=f"Invalid signature: {exc}") from exc

    event_id = event.get("id")
    event_type = event.get("type", "")
    if not record_event(event_id, event_type):
        return {"received": True, "duplicate": True}

    try:
        result = await asyncio.to_thread(process_event, event)
    except HTTPException as exc:
        print(f"[billing-api] ERROR: event {event_id} ({event_type}) failed: {exc.detail}")
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    print(f"[billing-api] processed {event_type} {event_id}: {result}")
    return {"received": True, "result": result}


@app.get("/")
def root() -> dict:
    return {"service": "billing-api", "status": "ok", "docs": "/docs"}


@app.get("/health")
def health() -> dict:
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok", "database": "ok" if db_ok else "unavailable"}


@app.get("/api/config")
def config(x_api_key: str = Header(default="")) -> dict:
    if BILLING_API_KEY and x_api_key != BILLING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return {
        "billing_base_url": BILLING_BASE_URL,
        "authentik_url": AUTHENTIK_BASE_URL,
        "authentik_group": AUTHENTIK_DEFAULT_GROUP,
        "stripe_configured": bool(STRIPE_SECRET_KEY),
        "stripe_webhook_configured": bool(STRIPE_WEBHOOK_SECRET),
        "webhook_secret_source": (
            "BILLING_WEBHOOK_SECRET"
            if os.environ.get("BILLING_WEBHOOK_SECRET", "").strip()
            else "STRIPE_WEBHOOK_SECRET (shared)"
        ),
    }


db_init()