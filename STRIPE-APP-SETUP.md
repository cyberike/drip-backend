# Drip Donations — Stripe App Setup Guide (v2.0)

This guide walks you through deploying the production-hardened Drip backend
and uploading the Stripe App UI extension.

---

## What Changed in v2.0

| Issue | Fix |
|-------|-----|
| SQLite in production (data splits across containers) | PostgreSQL via `DATABASE_URL` |
| Spoofable `Stripe-Account` header auth | Stripe App SDK signature verification (`fetchStripeSignature` + `absec_` secret) |
| Global webhook secret (single point of compromise) | Per-merchant webhook signing secrets stored in DB |
| Single `charity_id` per transaction (can't track splits) | New `donation_splits` table — one row per charity per transaction |
| No idempotency on webhooks (duplicate processing) | Event ID dedup in `webhook_events` + `transactions` tables |
| No rate limiting | In-memory sliding-window rate limiter (120 req/min per IP) |

---

## Prerequisites

- [Stripe CLI](https://stripe.com/docs/stripe-cli) installed and logged in
- Stripe Apps CLI plugin: `stripe plugin install apps`
- Node.js >= 18
- PostgreSQL database (Railway auto-provisions one)
- Your Drip backend running at `https://dripfinancial.org`

---

## Step 1: Add PostgreSQL on Railway

1. Go to your Railway project dashboard
2. Click **"+ New"** → **"Database"** → **"PostgreSQL"**
3. Railway auto-creates the `DATABASE_URL` env var
4. Verify it's linked: go to your backend service → **Variables** tab → confirm `DATABASE_URL` is set

> The server auto-creates all tables on first boot. No manual migration needed.

---

## Step 2: Set New Environment Variables

Add these to your Railway service variables:

```
DATABASE_URL=postgresql://...   (auto-set by Railway if you linked the DB)
STRIPE_APP_SECRET=absec_...     (from Stripe Dashboard → Apps → Drip Donations → ⋯ → Signing secret)
```

Your existing vars stay the same:
```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_CLIENT_ID=ca_...
STRIPE_WEBHOOK_SECRET=whsec_...   (still used as global fallback)
DRIP_BASE_URL=https://dripfinancial.org
```

### How to get `STRIPE_APP_SECRET`:
1. Go to https://dashboard.stripe.com/apps
2. Click on "Drip Donations"
3. Click the **⋯** menu → **Signing secret**
4. Copy the `absec_...` value

---

## Step 3: Deploy Backend

```bash
cd drip-backend
git pull origin main
```

Railway will auto-deploy when it detects the push. Verify with:

```bash
curl https://dripfinancial.org/api/health
```

Expected response:
```json
{
  "status": "ok",
  "database": "ok",
  "database_engine": "postgresql",
  "stripe_configured": true,
  "app_secret_set": true
}
```

---

## Step 4: Install UI Extension Dependencies

```bash
cd drip-backend
npm install
```

---

## Step 5: Preview Locally

```bash
stripe apps start
```

Then open:
- **Settings view**: https://dashboard.stripe.com/apps/settings-preview
- **Dashboard drawer**: Open any page in Stripe Dashboard, click the Apps icon

---

## Step 6: Upload to Stripe

```bash
stripe apps upload
```

This bundles the TypeScript/React code and hosts it on Stripe's CDN.

---

## Step 7: Set App Version Live

1. Go to https://dashboard.stripe.com/apps
2. Find "Drip Donations"
3. Set the latest version as the live version

---

## What Each View Does

### Settings View (`AppSettings.tsx`)
- **Viewport**: `settings` (accessible from app settings page)
- **Features**:
  - Set donation percentage (1-10%)
  - Select up to 3 verified 501(c)(3) charities
  - Saves with `fetchStripeSignature()` authentication
  - Donations auto-split equally among selected charities

### Dashboard View (`DashboardView.tsx`)
- **Viewport**: `stripe.dashboard.drawer.default` + `stripe.dashboard.payment.detail`
- **Features**:
  - Shows total donated, transactions today, active charities
  - Donation breakdown by category (from `donation_splits` table)
  - Active/Paused status badge
  - Link to full Drip dashboard

---

## How Authentication Works

### UI Extension → Backend (API calls)
1. Frontend calls `fetchStripeSignature()` from `@stripe/ui-extension-sdk/utils`
2. Signature + `user_id` + `account_id` sent in request
3. Backend verifies with `stripe.WebhookSignature.verify_header(payload, sig, STRIPE_APP_SECRET)`
4. If `STRIPE_APP_SECRET` is not set, falls back to trusting `Stripe-Account` header (dev mode only)

### Webhooks (Stripe → Backend)
1. Each merchant gets a unique webhook signing secret during OAuth onboarding
2. Secret stored in `merchants.webhook_secret` column
3. Webhook handler looks up merchant by `account` field in the event
4. Verifies with per-merchant secret; falls back to global `STRIPE_WEBHOOK_SECRET`

---

## Database Schema (PostgreSQL)

| Table | Purpose |
|-------|---------|
| `merchants` | Connected Stripe accounts + tokens + per-merchant webhook secret |
| `charities` | Verified 501(c)(3) organizations |
| `transactions` | One row per payment event (linked by `event_id` for idempotency) |
| `donation_splits` | One row per charity per transaction (supports multi-charity splits) |
| `charity_allocations` | Merchant's chosen charities + percentage splits |
| `webhook_events` | Raw event log with `event_id` UNIQUE constraint for dedup |

---

## API Endpoints

| Action | Endpoint | Method | Auth |
|--------|----------|--------|------|
| Load charities | `/api/charities?verified=true` | GET | Public |
| Load settings | `/api/settings` | GET | Signature verified |
| Save settings | `/api/settings` | PUT | Signature verified |
| Load allocations | `/api/allocations` | GET | Signature verified |
| Save allocations | `/api/allocations` | POST | Signature verified |
| Load stats | `/api/stats` | GET | Signature verified |
| Tax report | `/api/tax-report?year=2026` | GET | Signature verified |
| Transactions | `/api/transactions` | GET | Signature verified |
| Health check | `/api/health` | GET | Public |

---

## Troubleshooting

- **"No settings created"**: Upload the app with `stripe apps upload` and set version live
- **Settings not saving**: Check that `STRIPE_APP_SECRET` is set on Railway
- **401 Invalid signature**: Verify the `absec_...` secret matches what's in the Stripe Dashboard
- **Database connection errors**: Check `DATABASE_URL` is set and PostgreSQL is running
- **Duplicate webhooks**: Normal — the idempotency guard silently skips duplicates
- **Charities not loading**: Verify the `/api/charities` endpoint returns data (seeded on first boot)
