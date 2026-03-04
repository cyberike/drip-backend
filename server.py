#!/usr/bin/env python3
"""
Drip Donations — FastAPI Backend Server
========================================
Stripe App backend that intercepts payment_intent.succeeded webhooks and
automatically donates a configurable percentage (1–10%) of each payment to
verified 501(c)(3) charities.

Architecture:
  - Stripe OAuth 2.0 for merchant onboarding
  - SQLite for persistence (swap for Postgres in production)
  - Platform fee: 2% of the donation amount (not the payment amount)
  - Multi-charity allocation splits per merchant

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

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
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
DRIP_BASE_URL: str = os.getenv("DRIP_BASE_URL", "http://localhost:8000")

if not STRIPE_SECRET_KEY:
    logger.warning("STRIPE_SECRET_KEY is not set — Stripe calls will fail")

stripe.api_key = STRIPE_SECRET_KEY

PLATFORM_FEE_PCT: float = 0.02  # 2% of the donation amount

DB_PATH = os.getenv("DB_PATH", "drip.db")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row_factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist and seed demo data."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS merchants (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            stripe_account_id   TEXT    UNIQUE NOT NULL,
            access_token        TEXT    NOT NULL,
            refresh_token       TEXT,
            webhook_endpoint_id TEXT,
            donation_pct        REAL    NOT NULL DEFAULT 3.0,
            auto_donate         INTEGER NOT NULL DEFAULT 1,
            installed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS charities (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            ein           TEXT    UNIQUE NOT NULL,
            category      TEXT,
            website       TEXT,
            verified      INTEGER NOT NULL DEFAULT 0,
            total_donated REAL    NOT NULL DEFAULT 0.0,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id     INTEGER NOT NULL REFERENCES merchants(id),
            payment_id      TEXT    NOT NULL,
            customer        TEXT,
            amount          REAL    NOT NULL,
            donation_pct    REAL    NOT NULL,
            donation_amount REAL    NOT NULL,
            platform_fee    REAL    NOT NULL,
            charity_id      INTEGER REFERENCES charities(id),
            charity_name    TEXT,
            status          TEXT    NOT NULL DEFAULT 'pending',
            date            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS charity_allocations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL REFERENCES merchants(id),
            charity_id  INTEGER NOT NULL REFERENCES charities(id),
            pct_share   REAL    NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            UNIQUE(merchant_id, charity_id)
        );

        CREATE TABLE IF NOT EXISTS webhook_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            payload     TEXT NOT NULL,
            processed   INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    _seed_demo_data(conn)
    logger.info("Database initialised at %s", DB_PATH)


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    """Insert verified demo charities if the table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM charities").fetchone()[0]
    if count > 0:
        return

    demo_charities = [
        ("American Red Cross",           "53-0196605", "Humanitarian",     "https://www.redcross.org",        1),
        ("Doctors Without Borders",      "13-3433452", "Healthcare",       "https://www.doctorswithoutborders.org", 1),
        ("World Wildlife Fund",          "52-1693387", "Environment",      "https://www.worldwildlife.org",   1),
        ("Feed America",                 "36-3673599", "Hunger Relief",    "https://www.feedingamerica.org",  1),
        ("St. Jude Children's Research", "62-0646012", "Healthcare",       "https://www.stjude.org",          1),
        ("UNICEF USA",                   "13-1760110", "Children",         "https://www.unicefusa.org",       1),
        ("The Nature Conservancy",       "53-0242652", "Environment",      "https://www.nature.org",          1),
        ("Habitat for Humanity",         "91-1914868", "Housing",          "https://www.habitat.org",         1),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO charities (name, ein, category, website, verified) VALUES (?,?,?,?,?)",
        demo_charities,
    )
    conn.commit()
    logger.info("Seeded %d demo charities", len(demo_charities))


# Module-level DB connection (SQLite is used single-process here)
_db: Optional[sqlite3.Connection] = None


def db() -> sqlite3.Connection:
    """Return the module-level DB connection, creating it if necessary."""
    global _db
    if _db is None:
        _db = get_db()
        init_db(_db)
    return _db


# ---------------------------------------------------------------------------
# App Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise database on startup, close on shutdown."""
    logger.info("Drip backend starting up…")
    db()  # trigger init
    yield
    if _db is not None:
        _db.close()
    logger.info("Drip backend shut down.")


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Drip Donations API",
    description="Fintech middleware that donates a configurable % of payments to verified charities.",
    version="1.0.0",
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

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _get_merchant(conn: sqlite3.Connection, stripe_account_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM merchants WHERE stripe_account_id = ?", (stripe_account_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def _get_or_create_demo_merchant(conn: sqlite3.Connection) -> dict:
    """
    Return the first merchant in the DB, or a synthetic demo record.
    Used by dashboard endpoints when no Stripe account header is provided.
    """
    row = conn.execute("SELECT * FROM merchants LIMIT 1").fetchone()
    if row:
        return _row_to_dict(row)
    return {
        "id": 0,
        "stripe_account_id": "acct_demo",
        "donation_pct": 3.0,
        "auto_donate": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }


def _resolve_merchant(request: Request, conn: sqlite3.Connection) -> dict:
    """
    Resolve the calling merchant from the Stripe-Account header (preferred)
    or fall back to the demo record. Returns the merchant dict.
    """
    acct = request.headers.get("Stripe-Account")
    if acct:
        merchant = _get_merchant(conn, acct)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        return merchant
    return _get_or_create_demo_merchant(conn)


def _refresh_access_token_if_needed(merchant: dict) -> str:
    """
    Placeholder for token refresh logic.
    In production, check token expiry and call stripe.OAuth.token()
    with grant_type='refresh_token' when expired.
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
    conn.execute(
        """
        INSERT INTO merchants (stripe_account_id, access_token, refresh_token)
        VALUES (?, ?, ?)
        ON CONFLICT(stripe_account_id) DO UPDATE SET
            access_token  = excluded.access_token,
            refresh_token = excluded.refresh_token
        """,
        (stripe_account_id, access_token, refresh_token),
    )
    conn.commit()

    # Register a webhook endpoint on the merchant's account
    webhook_endpoint_id = _register_webhook(stripe_account_id, access_token)
    if webhook_endpoint_id:
        conn.execute(
            "UPDATE merchants SET webhook_endpoint_id = ? WHERE stripe_account_id = ?",
            (webhook_endpoint_id, stripe_account_id),
        )
        conn.commit()

    logger.info("Merchant %s connected (webhook: %s)", stripe_account_id, webhook_endpoint_id)

    # Redirect to the dashboard after successful install
    return RedirectResponse(url=f"{DRIP_BASE_URL}/dashboard?connected=1")


def _register_webhook(stripe_account_id: str, access_token: str) -> Optional[str]:
    """Create a webhook endpoint on the connected merchant's account."""
    try:
        endpoint = stripe.WebhookEndpoint.create(
            url=f"{DRIP_BASE_URL}/webhooks/stripe",
            enabled_events=["payment_intent.succeeded"],
            stripe_account=stripe_account_id,
            api_key=access_token,
        )
        return endpoint.id
    except stripe.error.StripeError as e:
        logger.error("Failed to register webhook for %s: %s", stripe_account_id, e)
        return None


# ---------------------------------------------------------------------------
# 2. Webhook Handler
# ---------------------------------------------------------------------------

@app.post("/webhooks/stripe", tags=["Webhooks"])
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Stripe webhook events.
    Only payment_intent.succeeded is actionable; all events are logged.
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception as e:
        logger.error("Webhook construction error: %s", e)
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    conn = db()

    # Log raw event
    conn.execute(
        "INSERT INTO webhook_events (event_type, payload) VALUES (?, ?)",
        (event["type"], payload.decode()),
    )
    conn.commit()

    if event["type"] == "payment_intent.succeeded":
        background_tasks.add_task(_process_payment_intent, event, conn)

    return {"status": "received"}


def _process_payment_intent(event: dict, conn: sqlite3.Connection) -> None:
    """
    Background task: calculate donation, log transaction, update charity totals.
    The platform fee is 2% of the DONATION amount, not the payment amount.
    """
    try:
        pi             = event["data"]["object"]
        payment_id     = pi["id"]
        account_id     = event.get("account")
        amount_cents   = pi["amount"]
        amount_dollars = amount_cents / 100.0
        customer       = pi.get("customer") or pi.get("receipt_email") or "anonymous"

        # Find merchant
        merchant = _get_merchant(conn, account_id) if account_id else None
        if not merchant:
            row = conn.execute("SELECT * FROM merchants LIMIT 1").fetchone()
            merchant = _row_to_dict(row) if row else None

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
        allocations = conn.execute(
            """
            SELECT ca.pct_share, c.id, c.name
            FROM charity_allocations ca
            JOIN charities c ON c.id = ca.charity_id
            WHERE ca.merchant_id = ? AND ca.active = 1
            ORDER BY ca.pct_share DESC
            """,
            (merchant["id"],),
        ).fetchall()

        if not allocations:
            row = conn.execute(
                "SELECT id, name FROM charities WHERE verified = 1 LIMIT 1"
            ).fetchone()
            if row:
                allocations = [(100.0, row["id"], row["name"])]
            else:
                logger.warning("No charities configured — donation not logged")
                return

        # Insert one transaction row per charity allocation
        for alloc in allocations:
            pct_share, charity_id, charity_name = alloc[0], alloc[1], alloc[2]
            split_donation = round(donation_amount * (pct_share / 100.0), 4)
            split_fee      = round(platform_fee    * (pct_share / 100.0), 4)

            conn.execute(
                """
                INSERT INTO transactions
                    (merchant_id, payment_id, customer, amount, donation_pct,
                     donation_amount, platform_fee, charity_id, charity_name, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
                """,
                (
                    merchant["id"], payment_id, customer,
                    amount_dollars, donation_pct,
                    split_donation, split_fee,
                    charity_id, charity_name,
                ),
            )

            conn.execute(
                "UPDATE charities SET total_donated = total_donated + ? WHERE id = ?",
                (split_donation, charity_id),
            )

        conn.execute(
            "UPDATE webhook_events SET processed = 1 WHERE event_type = 'payment_intent.succeeded' AND payload LIKE ?",
            (f'%{payment_id}%',),
        )
        conn.commit()

        logger.info(
            "Processed payment %s — $%.2f → donation $%.4f (fee $%.4f) across %d charity/charities",
            payment_id, amount_dollars, donation_amount, platform_fee, len(allocations),
        )

    except Exception as e:
        logger.exception("Error processing payment_intent.succeeded: %s", e)


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

    total_donated = conn.execute(
        "SELECT COALESCE(SUM(donation_amount), 0) FROM transactions WHERE merchant_id = ?", (mid,)
    ).fetchone()[0]

    tx_today = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_id = ? AND date(date) = ?", (mid, today)
    ).fetchone()[0]

    active_charities = conn.execute(
        "SELECT COUNT(DISTINCT charity_id) FROM charity_allocations WHERE merchant_id = ? AND active = 1", (mid,)
    ).fetchone()[0]

    total_transactions = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_id = ?", (mid,)
    ).fetchone()[0]

    platform_fees = conn.execute(
        "SELECT COALESCE(SUM(platform_fee), 0) FROM transactions WHERE merchant_id = ?", (mid,)
    ).fetchone()[0]

    by_category = conn.execute(
        """
        SELECT c.category, COALESCE(SUM(t.donation_amount), 0) AS donated
        FROM transactions t
        JOIN charities c ON c.id = t.charity_id
        WHERE t.merchant_id = ?
        GROUP BY c.category
        ORDER BY donated DESC
        LIMIT 5
        """,
        (mid,),
    ).fetchall()

    return {
        "total_donated":      round(total_donated, 2),
        "transactions_today": tx_today,
        "active_charities":   active_charities,
        "total_transactions": total_transactions,
        "platform_fees":      round(platform_fees, 4),
        "donation_by_category": [{"category": r[0], "donated": round(r[1], 2)} for r in by_category],
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
    query  = "SELECT * FROM charities WHERE 1=1"
    params: list = []

    if category:
        query  += " AND category = ?"
        params.append(category)
    if verified is not None:
        query  += " AND verified = ?"
        params.append(1 if verified else 0)

    query += " ORDER BY total_donated DESC"
    rows   = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


@app.post("/api/charities", status_code=201, tags=["Charities"])
def create_charity(body: CharityCreate):
    """Add a new charity (unverified by default)."""
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO charities (name, ein, category, website) VALUES (?, ?, ?, ?)",
            (body.name, body.ein, body.category, body.website),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM charities WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)
    except sqlite3.IntegrityError:
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
    """Paginated transaction log with optional filters."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]
    offset   = (page - 1) * page_size

    query  = "SELECT * FROM transactions WHERE merchant_id = ?"
    params: list = [mid]

    if charity_id:
        query  += " AND charity_id = ?"
        params.append(charity_id)
    if status:
        query  += " AND status = ?"
        params.append(status)
    if date_from:
        query  += " AND date(date) >= ?"
        params.append(date_from)
    if date_to:
        query  += " AND date(date) <= ?"
        params.append(date_to)

    total = conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]
    query += " ORDER BY date DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])

    rows = conn.execute(query, params).fetchall()
    return {
        "total":      total,
        "page":       page,
        "page_size":  page_size,
        "pages":      (total + page_size - 1) // page_size,
        "items":      [_row_to_dict(r) for r in rows],
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
        "installed_at":         merchant.get("installed_at"),
    }


@app.put("/api/settings", tags=["Settings"])
def update_settings(request: Request, body: SettingsUpdate):
    """Update merchant donation settings."""
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    if mid == 0:
        raise HTTPException(status_code=400, detail="No merchant connected — complete OAuth first")

    updates: dict = {}
    if body.donation_pct is not None:
        updates["donation_pct"] = body.donation_pct
    if body.auto_donate is not None:
        updates["auto_donate"] = 1 if body.auto_donate else 0

    if not updates:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [mid]
    conn.execute(f"UPDATE merchants SET {set_clause} WHERE id = ?", values)
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
    """
    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    rows = conn.execute(
        """
        SELECT
            t.charity_id,
            t.charity_name,
            c.ein,
            c.category,
            c.website,
            COUNT(*)                   AS transaction_count,
            COALESCE(SUM(t.donation_amount), 0) AS total_donated,
            COALESCE(SUM(t.platform_fee),    0) AS total_fees
        FROM transactions t
        LEFT JOIN charities c ON c.id = t.charity_id
        WHERE t.merchant_id = ?
          AND strftime('%Y', t.date) = ?
        GROUP BY t.charity_id, t.charity_name
        ORDER BY total_donated DESC
        """,
        (mid, str(year)),
    ).fetchall()

    grand_total = sum(r["total_donated"] for r in rows)

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
                "total_donated":     round(r["total_donated"], 2),
                "total_fees":        round(r["total_fees"], 4),
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

    rows = conn.execute(
        """
        SELECT ca.id, ca.charity_id, c.name AS charity_name, c.ein, c.category,
               ca.pct_share, ca.active
        FROM charity_allocations ca
        JOIN charities c ON c.id = ca.charity_id
        WHERE ca.merchant_id = ?
        ORDER BY ca.pct_share DESC
        """,
        (mid,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@app.post("/api/allocations", status_code=200, tags=["Allocations"])
def set_allocations(request: Request, body: AllocationsUpdate):
    """
    Set multi-charity allocation splits.
    All pct_share values must sum to exactly 100.
    Replaces the existing allocations for the merchant.
    """
    total_pct = sum(a.pct_share for a in body.allocations)
    if abs(total_pct - 100.0) > 0.01:
        raise HTTPException(
            status_code=422,
            detail=f"Allocation percentages must total 100 (got {total_pct:.2f})",
        )

    conn     = db()
    merchant = _resolve_merchant(request, conn)
    mid      = merchant["id"]

    if mid == 0:
        raise HTTPException(status_code=400, detail="No merchant connected — complete OAuth first")

    for alloc in body.allocations:
        row = conn.execute("SELECT id FROM charities WHERE id = ?", (alloc.charity_id,)).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Charity id={alloc.charity_id} not found",
            )

    conn.execute("DELETE FROM charity_allocations WHERE merchant_id = ?", (mid,))
    conn.executemany(
        "INSERT INTO charity_allocations (merchant_id, charity_id, pct_share) VALUES (?, ?, ?)",
        [(mid, a.charity_id, a.pct_share) for a in body.allocations],
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
        db().execute("SELECT 1").fetchone()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    stripe_configured = bool(STRIPE_SECRET_KEY and STRIPE_CLIENT_ID)

    return {
        "status":             "ok" if db_status == "ok" and stripe_configured else "degraded",
        "database":           db_status,
        "stripe_configured":  stripe_configured,
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
