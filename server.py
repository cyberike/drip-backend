#!/usr/bin/env python3
"""
Drip Donations — FastAPI Backend Server  (v2.0 — Production Hardened)
=====================================================================
Stripe App backend that intercepts payment_intent.succeeded webhooks and
automatically donates a configurable percentage (1–10%) of each payment to
verified 501(c)(3) charities.

v2.0 changes:
  - PostgreSQL instead of SQLite (multi-container safe)
  - Stripe App SDK signature verification (fetchStripeSignature / absec_ secret)
  - Per-merchant webhook signing secrets stored in DB
  - Multi-charity split transaction model (donation_splits table)
  - Webhook idempotency guard (event_id dedup + rate limiter)

Architecture:
  - Stripe OAuth 2.0 for merchant onboarding
  - PostgreSQL via DATABASE_URL (Railway auto-provisions)
  - Platform fee: 2% of the donation amount (not the payment amount)
  - Multi-charity allocation splits per merchant

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import stripe
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Environment & Logging
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("drip")

STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_CLIENT_ID: str = os.getenv("STRIPE_CLIENT_ID", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # fallback global
STRIPE_APP_SECRET: str = os.getenv("STRIPE_APP_SECRET", "")  # absec_... signing secret
DRIP_BASE_URL: str = os.getenv("DRIP_BASE_URL", "http://localhost:8000")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

if not STRIPE_SECRET_KEY:
    logger.warning("STRIPE_SECRET_KEY is not set — Stripe calls will fail")
if not STRIPE_APP_SECRET:
    logger.warning("STRIPE_APP_SECRET is not set — API signature verification disabled (insecure)")
if not DATABASE_URL:
    logger.warning("DATABASE_URL is not set — database connection will fail")

stripe.api_key = STRIPE_SECRET_KEY

PLATFORM_FEE_PCT: float = 0.02  # 2% of the donation amount

# ---------------------------------------------------------------------------
# Database — PostgreSQL
# ---------------------------------------------------------------------------

def get_db() -> psycopg2.extensions.connection:
    """Return a new PostgreSQL connection with dict cursor factory."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def init_db(conn) -> None:
    """Create tables if they don't exist and seed demo data."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchants (
                id                  SERIAL PRIMARY KEY,
                stripe_account_id   TEXT    UNIQUE NOT NULL,
                access_token        TEXT    NOT NULL,
                refresh_token       TEXT,
                webhook_endpoint_id TEXT,
                webhook_secret      TEXT,
                donation_pct        REAL    NOT NULL DEFAULT 3.0,
                auto_donate         BOOLEAN NOT NULL DEFAULT TRUE,
                installed_at        TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS charities (
                id            SERIAL PRIMARY KEY,
                name          TEXT    NOT NULL,
                ein           TEXT    UNIQUE NOT NULL,
                category      TEXT,
                website       TEXT,
                verified      BOOLEAN NOT NULL DEFAULT FALSE,
                total_donated REAL    NOT NULL DEFAULT 0.0,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id              SERIAL PRIMARY KEY,
                merchant_id     INTEGER NOT NULL REFERENCES merchants(id),
                payment_id      TEXT    NOT NULL,
                event_id        TEXT    UNIQUE NOT NULL,
                customer        TEXT,
                amount          REAL    NOT NULL,
                donation_pct    REAL    NOT NULL,
                donation_amount REAL    NOT NULL,
                platform_fee    REAL    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                date            TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS donation_splits (
                id              SERIAL PRIMARY KEY,
                transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                charity_id      INTEGER NOT NULL REFERENCES charities(id),
                charity_name    TEXT    NOT NULL,
                pct_share       REAL    NOT NULL,
                split_amount    REAL    NOT NULL,
                split_fee       REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS charity_allocations (
                id          SERIAL PRIMARY KEY,
                merchant_id INTEGER NOT NULL REFERENCES merchants(id),
                charity_id  INTEGER NOT NULL REFERENCES charities(id),
                pct_share   REAL    NOT NULL,
                active      BOOLEAN NOT NULL DEFAULT TRUE,
                UNIQUE(merchant_id, charity_id)
            );

            CREATE TABLE IF NOT EXISTS webhook_events (
                id          SERIAL PRIMARY KEY,
                event_id    TEXT    UNIQUE NOT NULL,
                event_type  TEXT    NOT NULL,
                payload     TEXT    NOT NULL,
                processed   BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_merchant
                ON transactions(merchant_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_event
                ON transactions(event_id);
            CREATE INDEX IF NOT EXISTS idx_donation_splits_tx
                ON donation_splits(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_charity_alloc_merchant
                ON charity_allocations(merchant_id);
            CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id
                ON webhook_events(event_id);
        """)
    conn.commit()
    _seed_demo_data(conn)
    logger.info("PostgreSQL database initialised")


def _seed_demo_data(conn) -> None:
    """Insert verified demo charities if the table is empty."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM charities")
        count = cur.fetchone()["cnt"]
        if count > 0:
            return

        demo_charities = [
            ("American Red Cross",           "53-0196605", "Humanitarian",  "https://www.redcross.org",              True),
            ("Doctors Without Borders",      "13-3433452", "Healthcare",    "https://www.doctorswithoutborders.org", True),
            ("World Wildlife Fund",          "52-1693387", "Environment",   "https://www.worldwildlife.org",         True),
            ("Feed America",                 "36-3673599", "Hunger Relief", "https://www.feedingamerica.org",        True),
            ("St. Jude Children's Research", "62-0646012", "Healthcare",    "https://www.stjude.org",                True),
            ("UNICEF USA",                   "13-1760110", "Children",      "https://www.unicefusa.org",             True),
            ("The Nature Conservancy",       "53-0242652", "Environment",   "https://www.nature.org",                True),
            ("Habitat for Humanity",         "91-1914868", "Housing",       "https://www.habitat.org",               True),
        ]
        cur.executemany(
            "INSERT INTO charities (name, ein, category, website, verified) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ein) DO NOTHING",
            demo_charities,
        )
    conn.commit()
    logger.info("Seeded %d demo charities", len(demo_charities))


# Module-level connection pool (single connection reused per process)
_db: Optional[psycopg2.extensions.connection] = None


def db():
    """Return the module-level DB connection, creating/reconnecting if necessary."""
    global _db
    if _db is None or _db.closed:
        _db = get_db()
        init_db(_db)
    return _db


# ---------------------------------------------------------------------------
# Rate Limiter (in-memory, per-process)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple sliding-window rate limiter for webhook endpoints."""
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window
        # Purge old entries
        self._hits[key] = [t for t in self._hits[key] if t > window_start]
        if len(self._hits[key]) >= self.max_requests:
            return False
        self._hits[key].append(now)
        return True


webhook_limiter = RateLimiter(max_requests=120, window_seconds=60)


# ---------------------------------------------------------------------------
# App Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise database on startup, close on shutdown."""
    logger.info("Drip backend starting up…")
    db()  # trigger init
    yield
    if _db is not None and not _db.closed:
        _db.close()
    logger.info("Drip backend shut down.")


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Drip Donations API",
    description="Fintech middleware that donates a configurable % of payments to verified charities.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class CharityCreate(BaseModel):
    name:     str
    ein:      str
    category: Optional[str] = None
    website:  Optional[str] = None


class SettingsUpdate(BaseModel):
    donation_pct: Optional[float] = Field(None, ge=1.0, le=10.0)
    auto_donate:  Optional[bool]  = None


class AllocationItem(BaseModel):
    charity_id: int
    pct_share:  float = Field(..., gt=0, le=100)


class AllocationsUpdate(BaseModel):
    allocations: list[AllocationItem]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_merchant(conn, stripe_account_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM merchants WHERE stripe_account_id = %s", (stripe_account_id,))
        return cur.fetchone()


def _get_or_create_demo_merchant(conn) -> dict:
    """
    Return the first merchant in the DB, or a synthetic demo record.
    Used by dashboard endpoints when no Stripe account header is provided.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM merchants LIMIT 1")
        row = cur.fetchone()
    if row:
        return row
    return {
        "id": 0,
        "stripe_account_id": "acct_demo",
        "donation_pct": 3.0,
        "auto_donate": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }


def _verify_stripe_app_signature(request: Request, body_bytes: bytes = b"") -> dict:
    """
    Verify the Stripe App SDK signature (fetchStripeSignature) on incoming
    requests from the UI extension.

    The signature is in the 'Stripe-Signature' header. The signed payload is
    JSON of {user_id, account_id} (the body sent by the frontend).

    Returns the verified payload dict with user_id and account_id.
    Raises HTTPException 401 if verification fails.
    """
    if not STRIPE_APP_SECRET:
        # Fallback: if no app secret is configured, trust Stripe-Account header
        # (for local dev / testing — logs a warning)
        acct = request.headers.get("Stripe-Account", "")
        logger.warning("STRIPE_APP_SECRET not set — falling back to unverified Stripe-Account header")
        return {"account_id": acct, "user_id": ""}

    sig = request.headers.get("Stripe-Signature", "")
    if not sig:
        raise HTTPException(status_code=401, detail="Missing Stripe-Signature header")

    try:
        # The payload to verify is the JSON body containing user_id + account_id
        # exactly as sent by the UI extension via fetchStripeSignature()
        stripe.WebhookSignature.verify_header(
            body_bytes.decode("utf-8") if body_bytes else "{}",
            sig,
            STRIPE_APP_SECRET,
        )
    except stripe.error.SignatureVerificationError as e:
        logger.warning("Stripe App signature verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse the verified body to extract account_id
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        payload = {}

    return payload


def _resolve_merchant_verified(request: Request, conn, body_bytes: bytes = b"") -> dict:
    """
    Resolve the calling merchant by verifying the Stripe App SDK signature
    and extracting the account_id from the signed payload.
    Falls back to Stripe-Account header for backwards compatibility during dev.
    """
    # Try signature verification first
    verified = _verify_stripe_app_signature(request, body_bytes)
    acct = verified.get("account_id", "") or request.headers.get("Stripe-Account", "")

    if acct:
        merchant = _get_merchant(conn, acct)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        return merchant
    return _get_or_create_demo_merchant(conn)


def _resolve_merchant(request: Request, conn) -> dict:
    """
    Resolve merchant from Stripe-Account header (GET requests where
    signature is in the header but no body). Verifies signature if present.
    """
    # For GET requests, check signature header against empty payload
    sig = request.headers.get("Stripe-Signature", "")
    acct = request.headers.get("Stripe-Account", "")

    if sig and STRIPE_APP_SECRET:
        try:
            # For GET requests, the signed payload is {user_id, account_id}
            # but typically the UI extension sends it as a query param or header
            # We verify what we can — the signature proves the request came from Stripe
            stripe.WebhookSignature.verify_header(
                json.dumps({"user_id": request.headers.get("Stripe-User-Id", ""), "account_id": acct}),
                sig,
                STRIPE_APP_SECRET,
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=401, detail="Invalid signature")

    if acct:
        merchant = _get_merchant(conn, acct)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        return merchant
    return _get_or_create_demo_merchant(conn)


def _refresh_access_token_if_needed(merchant: dict) -> str:
    """
    Placeholder for token refresh logic.
    Stripe access tokens for Standard OAuth don't expire, but Express tokens can.
    Returns a valid access token.
    """
    return merchant.get("access_token", STRIPE_SECRET_KEY)


# ---------------------------------------------------------------------------
# 1. OAuth Flow
# ---------------------------------------------------------------------------

@app.get("/oauth/connect", tags=["OAuth"])
def oauth_connect(request: Request):
    """
    Redirect the merchant to Stripe's OAuth authorisation page.
    Query params are forwarded so the dashboard can pass ?state=... for CSRF.
    """
    if not STRIPE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="STRIPE_CLIENT_ID is not configured")

    state = request.query_params.get("state", "")
    params = {
        "response_type": "code",
        "client_id": STRIPE_CLIENT_ID,
        "scope": "read_write",
        "redirect_uri": f"{DRIP_BASE_URL}/oauth/callback",
    }
    if state:
        params["state"] = state

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    auth_url = f"https://connect.stripe.com/oauth/authorize?{query_string}"
    logger.info("Redirecting to Stripe OAuth: %s", auth_url)
    return RedirectResponse(url=auth_url)


@app.get("/oauth/callback", tags=["OAuth"])
def oauth_callback(code: str = Query(...), state: Optional[str] = Query(None)):
    """
    Receive the auth code from Stripe, exchange it for tokens,
    persist the merchant record, and register a webhook endpoint.
    Stores the per-merchant webhook signing secret in the DB.
    """
    try:
        token_response = stripe.OAuth.token(
            grant_type="authorization_code",
            code=code,
        )
    except stripe.oauth_error.OAuthError as e:
        logger.error("OAuth token exchange failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unexpected error during OAuth callback: %s", e)
        raise HTTPException(status_code=500, detail="Token exchange failed")

    stripe_account_id: str = token_response.stripe_user_id
    access_token: str       = token_response.access_token
    refresh_token: str      = getattr(token_response, "refresh_token", None) or ""

    conn = db()

    # Upsert merchant
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO merchants (stripe_account_id, access_token, refresh_token)
            VALUES (%s, %s, %s)
            ON CONFLICT(stripe_account_id) DO UPDATE SET
                access_token  = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token
            RETURNING id
            """,
            (stripe_account_id, access_token, refresh_token),
        )
        merchant_id = cur.fetchone()["id"]
    conn.commit()

    # Register a webhook endpoint on the merchant's account
    # and store the per-merchant signing secret
    webhook_endpoint_id, webhook_secret = _register_webhook(stripe_account_id, access_token)
    if webhook_endpoint_id:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE merchants SET webhook_endpoint_id = %s, webhook_secret = %s WHERE stripe_account_id = %s",
                (webhook_endpoint_id, webhook_secret, stripe_account_id),
            )
        conn.commit()

    logger.info("Merchant %s connected (webhook: %s, secret stored: %s)",
                stripe_account_id, webhook_endpoint_id, bool(webhook_secret))

    # Redirect to the dashboard after successful install
    return RedirectResponse(url=f"{DRIP_BASE_URL}/dashboard?connected=1")


def _register_webhook(stripe_account_id: str, access_token: str) -> tuple[Optional[str], Optional[str]]:
    """
    Create a webhook endpoint on the connected merchant's account.
    Returns (endpoint_id, signing_secret) — the secret is unique per merchant.
    """
    try:
        endpoint = stripe.WebhookEndpoint.create(
            url=f"{DRIP_BASE_URL}/webhooks/stripe",
            enabled_events=["payment_intent.succeeded"],
            stripe_account=stripe_account_id,
            api_key=access_token,
        )
        # endpoint.secret contains the per-merchant webhook signing secret
        return endpoint.id, endpoint.secret
    except stripe.error.StripeError as e:
        logger.error("Failed to register webhook for %s: %s", stripe_account_id, e)
        return None, None


# ---------------------------------------------------------------------------
# 2. Webhook Handler (idempotent + per-merchant secret verification)
# ---------------------------------------------------------------------------

@app.post("/webhooks/stripe", tags=["Webhooks"])
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Stripe webhook events.
    - Rate-limited (120 req/min per source IP)
    - Idempotent (duplicate event_id rejected)
    - Per-merchant webhook secret verification with global fallback
    """
    # Rate limiting by source IP
    client_ip = request.client.host if request.client else "unknown"
    if not webhook_limiter.is_allowed(client_ip):
        logger.warning("Rate limit exceeded for %s", client_ip)
        raise HTTPException(status_code=429, detail="Too many requests")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # First, try to parse the event to get the account ID for per-merchant secret lookup
    try:
        raw_event = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    event_id = raw_event.get("id", "")
    account_id = raw_event.get("account")  # connected account that triggered event

    conn = db()

    # --- Idempotency check: reject duplicate events ---
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM webhook_events WHERE event_id = %s", (event_id,))
        if cur.fetchone():
            logger.info("Duplicate webhook event %s — skipping", event_id)
            return {"status": "duplicate"}

    # --- Verify signature (per-merchant secret, then global fallback) ---
    webhook_secret = None
    if account_id:
        merchant = _get_merchant(conn, account_id)
        if merchant and merchant.get("webhook_secret"):
            webhook_secret = merchant["webhook_secret"]

    if not webhook_secret:
        webhook_secret = STRIPE_WEBHOOK_SECRET

    if not webhook_secret:
        logger.error("No webhook secret available — cannot verify signature")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        # If per-merchant secret failed, try global fallback once
        if webhook_secret != STRIPE_WEBHOOK_SECRET and STRIPE_WEBHOOK_SECRET:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
                logger.info("Per-merchant secret failed but global fallback succeeded for %s", account_id)
            except stripe.error.SignatureVerificationError:
                logger.warning("Webhook signature verification failed for event %s", event_id)
                raise HTTPException(status_code=400, detail="Invalid webhook signature")
        else:
            logger.warning("Webhook signature verification failed for event %s", event_id)
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception as e:
        logger.error("Webhook construction error: %s", e)
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    # --- Log raw event (with idempotency insert) ---
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                (event_id, event["type"], payload.decode()),
            )
        conn.commit()
    except Exception as e:
        logger.error("Failed to log webhook event: %s", e)
        conn.rollback()

    if event["type"] == "payment_intent.succeeded":
        background_tasks.add_task(_process_payment_intent, event)

    return {"status": "received"}


def _process_payment_intent(event: dict) -> None:
    """
    Background task: calculate donation, create transaction + donation_splits,
    update charity totals.
    The platform fee is 2% of the DONATION amount, not the payment amount.
    Uses the donation_splits table to properly record multi-charity splits.
    """
    conn = None
    try:
        conn = get_db()  # fresh connection for background task
        pi             = event["data"]["object"]
        payment_id     = pi["id"]
        event_id       = event["id"]
        account_id     = event.get("account")          # connected account
        amount_cents   = pi["amount"]                   # Stripe amounts are in cents
        amount_dollars = amount_cents / 100.0
        customer       = pi.get("customer") or pi.get("receipt_email") or "anonymous"

        # --- Idempotency: check if this event was already processed ---
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM transactions WHERE event_id = %s", (event_id,))
            if cur.fetchone():
                logger.info("Transaction for event %s already exists — skipping", event_id)
                return

        # Find merchant
        merchant = _get_merchant(conn, account_id) if account_id else None
        if not merchant:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM merchants LIMIT 1")
                merchant = cur.fetchone()

        if not merchant:
            logger.warning("No merchant found for account %s — skipping donation", account_id)
            return

        if not merchant.get("auto_donate"):
            logger.info("Auto-donate disabled for merchant %s — skipping", merchant["stripe_account_id"])
            return

        donation_pct    = float(merchant["donation_pct"])
        donation_amount = round(amount_dollars * (donation_pct / 100.0), 4)
        platform_fee    = round(donation_amount * PLATFORM_FEE_PCT, 4)

        # Resolve charity allocations for this merchant
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ca.pct_share, c.id AS charity_id, c.name AS charity_name
                FROM charity_allocations ca
                JOIN charities c ON c.id = ca.charity_id
                WHERE ca.merchant_id = %s AND ca.active = TRUE
                ORDER BY ca.pct_share DESC
                """,
                (merchant["id"],),
            )
            allocations = cur.fetchall()

        if not allocations:
            # Fallback: pick first verified charity
            with conn.cursor() as cur:
                cur.execute("SELECT id AS charity_id, name AS charity_name FROM charities WHERE verified = TRUE LIMIT 1")
                fallback = cur.fetchone()
            if fallback:
                allocations = [{"pct_share": 100.0, "charity_id": fallback["charity_id"], "charity_name": fallback["charity_name"]}]
            else:
                logger.warning("No charities configured — donation not logged")
                return

        # --- Insert single transaction row ---
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions
                    (merchant_id, payment_id, event_id, customer, amount, donation_pct,
                     donation_amount, platform_fee, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'completed')
                RETURNING id
                """,
                (
                    merchant["id"], payment_id, event_id, customer,
                    amount_dollars, donation_pct,
                    donation_amount, platform_fee,
                ),
            )
            tx_id = cur.fetchone()["id"]

        # --- Insert donation_splits for each charity allocation ---
        for alloc in allocations:
            pct_share    = alloc["pct_share"]
            charity_id   = alloc["charity_id"]
            charity_name = alloc["charity_name"]
            split_amount = round(donation_amount * (pct_share / 100.0), 4)
            split_fee    = round(platform_fee    * (pct_share / 100.0), 4)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO donation_splits
                        (transaction_id, charity_id, charity_name, pct_share, split_amount, split_fee)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (tx_id, charity_id, charity_name, pct_share, split_amount, split_fee),
                )

                # Accumulate total donated to charity
                cur.execute(
                    "UPDATE charities SET total_donated = total_donated + %s WHERE id = %s",
                    (split_amount, charity_id),
                )

        # Mark event as processed
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE webhook_events SET processed = TRUE WHERE event_id = %s",
                (event_id,),
            )

        conn.commit()

        logger.info(
            "Processed payment %s (event %s) — $%.2f → donation $%.4f (fee $%.4f) across %d charity/charities",
            payment_id, event_id, amount_dollars, donation_amount, platform_fee, len(allocations),
        )

    except Exception as e:
        logger.exception("Error processing payment_intent.succeeded: %s", e)
        if conn and not conn.closed:
            conn.rollback()
    finally:
        if conn and not conn.closed:
            conn.close()


# ---------------------------------------------------------------------------
# 3. API — Stats
# ---------------------------------------------------------------------------

@app.get("/api/stats", tags=["Dashboard"])
def get_stats(request: Request):
    """Dashboard KPIs: total donated, transactions today, active charities."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    today = datetime.now(timezone.utc).date().isoformat()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(donation_amount), 0) AS val FROM transactions WHERE merchant_id = %s",
            (mid,),
        )
        total_donated = cur.fetchone()["val"]

        cur.execute(
            "SELECT COUNT(*) AS val FROM transactions WHERE merchant_id = %s AND date::date = %s",
            (mid, today),
        )
        tx_today = cur.fetchone()["val"]

        cur.execute(
            "SELECT COUNT(DISTINCT charity_id) AS val FROM charity_allocations WHERE merchant_id = %s AND active = TRUE",
            (mid,),
        )
        active_charities = cur.fetchone()["val"]

        cur.execute(
            "SELECT COUNT(*) AS val FROM transactions WHERE merchant_id = %s", (mid,)
        )
        total_transactions = cur.fetchone()["val"]

        cur.execute(
            "SELECT COALESCE(SUM(platform_fee), 0) AS val FROM transactions WHERE merchant_id = %s",
            (mid,),
        )
        platform_fees = cur.fetchone()["val"]

        # Donation by category (top 5) — from donation_splits
        cur.execute(
            """
            SELECT c.category, COALESCE(SUM(ds.split_amount), 0) AS donated
            FROM donation_splits ds
            JOIN transactions t ON t.id = ds.transaction_id
            JOIN charities c ON c.id = ds.charity_id
            WHERE t.merchant_id = %s
            GROUP BY c.category
            ORDER BY donated DESC
            LIMIT 5
            """,
            (mid,),
        )
        by_category = cur.fetchall()

    return {
        "total_donated":      round(float(total_donated), 2),
        "transactions_today": tx_today,
        "active_charities":   active_charities,
        "total_transactions": total_transactions,
        "platform_fees":      round(float(platform_fees), 4),
        "donation_by_category": [{"category": r["category"], "donated": round(float(r["donated"]), 2)} for r in by_category],
    }


# ---------------------------------------------------------------------------
# 4. API — Charities
# ---------------------------------------------------------------------------

@app.get("/api/charities", tags=["Charities"])
def list_charities(
    category: Optional[str] = Query(None),
    verified: Optional[bool] = Query(None),
):
    """List all charities with optional filters."""
    conn   = db()
    query  = "SELECT * FROM charities WHERE TRUE"
    params: list = []

    if category:
        query  += " AND category = %s"
        params.append(category)
    if verified is not None:
        query  += " AND verified = %s"
        params.append(verified)

    query += " ORDER BY total_donated DESC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/charities", status_code=201, tags=["Charities"])
def create_charity(body: CharityCreate):
    """Add a new charity (unverified by default)."""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO charities (name, ein, category, website) VALUES (%s, %s, %s, %s) RETURNING *",
                (body.name, body.ein, body.category, body.website),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="A charity with this EIN already exists")


# ---------------------------------------------------------------------------
# 5. API — Transactions
# ---------------------------------------------------------------------------

@app.get("/api/transactions", tags=["Transactions"])
def list_transactions(
    request:    Request,
    page:       int            = Query(1, ge=1),
    page_size:  int            = Query(20, ge=1, le=100),
    charity_id: Optional[int]  = Query(None),
    status:     Optional[str]  = Query(None),
    date_from:  Optional[str]  = Query(None),
    date_to:    Optional[str]  = Query(None),
):
    """Paginated transaction log with optional filters. Includes donation splits."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]
    offset   = (page - 1) * page_size

    query  = "SELECT * FROM transactions WHERE merchant_id = %s"
    params: list = [mid]

    if charity_id:
        # Filter by charity via donation_splits
        query = """
            SELECT DISTINCT t.* FROM transactions t
            JOIN donation_splits ds ON ds.transaction_id = t.id
            WHERE t.merchant_id = %s AND ds.charity_id = %s
        """
        params = [mid, charity_id]
    if status:
        query  += " AND status = %s"
        params.append(status)
    if date_from:
        query  += " AND date::date >= %s"
        params.append(date_from)
    if date_to:
        query  += " AND date::date <= %s"
        params.append(date_to)

    with conn.cursor() as cur:
        # Count total
        cur.execute(f"SELECT COUNT(*) AS cnt FROM ({query}) AS sub", params)
        total = cur.fetchone()["cnt"]

        # Paginate
        query += " ORDER BY date DESC LIMIT %s OFFSET %s"
        params.extend([page_size, offset])
        cur.execute(query, params)
        rows = cur.fetchall()

        # For each transaction, attach its donation splits
        items = []
        for row in rows:
            item = dict(row)
            cur.execute(
                "SELECT charity_id, charity_name, pct_share, split_amount, split_fee FROM donation_splits WHERE transaction_id = %s",
                (row["id"],),
            )
            item["donation_splits"] = [dict(s) for s in cur.fetchall()]
            items.append(item)

    return {
        "total":      total,
        "page":       page,
        "page_size":  page_size,
        "pages":      (total + page_size - 1) // page_size,
        "items":      items,
    }


# ---------------------------------------------------------------------------
# 6. API — Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings", tags=["Settings"])
def get_settings(request: Request):
    """Get merchant donation settings."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    return {
        "donation_pct":         merchant.get("donation_pct", 3.0),
        "auto_donate":          bool(merchant.get("auto_donate", True)),
        "stripe_account_id":    merchant.get("stripe_account_id"),
        "webhook_endpoint_id":  merchant.get("webhook_endpoint_id"),
        "installed_at":         str(merchant.get("installed_at", "")),
    }


@app.put("/api/settings", tags=["Settings"])
async def update_settings(request: Request, body: SettingsUpdate):
    """Update merchant donation settings (signature-verified)."""
    conn = db()

    # For PUT/POST, verify signature against the request body
    body_bytes = await request.body()
    merchant = _resolve_merchant_verified(request, conn, body_bytes)
    mid = merchant["id"]

    if mid == 0:
        raise HTTPException(status_code=400, detail="No merchant connected — complete OAuth first")

    updates: dict = {}
    if body.donation_pct is not None:
        updates["donation_pct"] = body.donation_pct
    if body.auto_donate is not None:
        updates["auto_donate"] = body.auto_donate

    if not updates:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values     = list(updates.values()) + [mid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE merchants SET {set_clause} WHERE id = %s", values)
    conn.commit()

    return get_settings(request)


# ---------------------------------------------------------------------------
# 7. API — Tax Report
# ---------------------------------------------------------------------------

@app.get("/api/tax-report", tags=["Reports"])
def tax_report(request: Request, year: int = Query(datetime.now().year)):
    """
    Annual tax summary grouped by charity.
    Returns total donated per 501(c)(3) for the given calendar year.
    Uses donation_splits for accurate multi-charity reporting.
    """
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ds.charity_id,
                ds.charity_name,
                c.ein,
                c.category,
                c.website,
                COUNT(DISTINCT t.id)                    AS transaction_count,
                COALESCE(SUM(ds.split_amount), 0)       AS total_donated,
                COALESCE(SUM(ds.split_fee), 0)          AS total_fees
            FROM donation_splits ds
            JOIN transactions t ON t.id = ds.transaction_id
            LEFT JOIN charities c ON c.id = ds.charity_id
            WHERE t.merchant_id = %s
              AND EXTRACT(YEAR FROM t.date) = %s
            GROUP BY ds.charity_id, ds.charity_name, c.ein, c.category, c.website
            ORDER BY total_donated DESC
            """,
            (mid, year),
        )
        rows = cur.fetchall()

    grand_total = sum(float(r["total_donated"]) for r in rows)

    return {
        "year":        year,
        "grand_total": round(grand_total, 2),
        "charities":   [
            {
                "charity_id":        r["charity_id"],
                "charity_name":      r["charity_name"],
                "ein":               r["ein"],
                "category":          r["category"],
                "website":           r["website"],
                "transaction_count": r["transaction_count"],
                "total_donated":     round(float(r["total_donated"]), 2),
                "total_fees":        round(float(r["total_fees"]), 4),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 8. API — Allocations
# ---------------------------------------------------------------------------

@app.get("/api/allocations", tags=["Allocations"])
def get_allocations(request: Request):
    """Get the current charity allocation splits for the merchant."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ca.id, ca.charity_id, c.name AS charity_name, c.ein, c.category,
                   ca.pct_share, ca.active
            FROM charity_allocations ca
            JOIN charities c ON c.id = ca.charity_id
            WHERE ca.merchant_id = %s
            ORDER BY ca.pct_share DESC
            """,
            (mid,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/allocations", status_code=200, tags=["Allocations"])
async def set_allocations(request: Request, body: AllocationsUpdate):
    """
    Set multi-charity allocation splits (signature-verified).
    All pct_share values must sum to exactly 100.
    Replaces the existing allocations for the merchant.
    """
    total_pct = sum(a.pct_share for a in body.allocations)
    if abs(total_pct - 100.0) > 0.01:
        raise HTTPException(
            status_code=422,
            detail=f"Allocation percentages must total 100 (got {total_pct:.2f})",
        )

    conn = db()

    # Verify signature for write operations
    body_bytes = await request.body()
    merchant = _resolve_merchant_verified(request, conn, body_bytes)
    mid = merchant["id"]

    if mid == 0:
        raise HTTPException(status_code=400, detail="No merchant connected — complete OAuth first")

    # Validate all charity IDs exist
    with conn.cursor() as cur:
        for alloc in body.allocations:
            cur.execute("SELECT id FROM charities WHERE id = %s", (alloc.charity_id,))
            if not cur.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail=f"Charity id={alloc.charity_id} not found",
                )

        # Atomically replace allocations
        cur.execute("DELETE FROM charity_allocations WHERE merchant_id = %s", (mid,))
        for a in body.allocations:
            cur.execute(
                "INSERT INTO charity_allocations (merchant_id, charity_id, pct_share) VALUES (%s, %s, %s)",
                (mid, a.charity_id, a.pct_share),
            )
    conn.commit()

    return get_allocations(request)


# ---------------------------------------------------------------------------
# 9. Health Check
# ---------------------------------------------------------------------------

@app.get("/api/health", tags=["System"])
def health_check():
    """Health check — used by load balancers and deployment monitors."""
    try:
        conn = db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    stripe_configured = bool(STRIPE_SECRET_KEY and STRIPE_CLIENT_ID)

    return {
        "status":             "ok" if db_status == "ok" and stripe_configured else "degraded",
        "database":           db_status,
        "database_engine":    "postgresql",
        "stripe_configured":  stripe_configured,
        "app_secret_set":     bool(STRIPE_APP_SECRET),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
