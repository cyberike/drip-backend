# Drip Donations — Backend Server

> Fintech middleware that intercepts Stripe payment webhooks and automatically donates a configurable percentage (1–10%) of each transaction to verified 501(c)(3) charities.

---

## What is Drip?

Drip is a Stripe App (OAuth 2.0) that sits between merchants and their Stripe accounts. After a merchant installs the app, every successful payment triggers a configurable charitable donation — without any changes to the merchant's checkout flow.

**Key numbers:**
- Merchants set their donation rate: **1%–10%** of each payment
- Drip takes a **2% platform fee** from the donation amount (not the payment)
- Merchants can split donations across multiple charities with configurable % allocations

**Example:** A $100 payment with a 5% donation rate → $5 donation. Drip keeps $0.10 (2% of $5). The charity receives $4.90.

---

## Architecture

```
Stripe Dashboard
      │  (OAuth install)
      ▼
/oauth/connect  ──►  Stripe OAuth Authorization Page
                            │
                            ▼ (auth code)
/oauth/callback  ──►  Exchange for access_token
                       Store merchant in SQLite
                       Register webhook on merchant account
                            │
Merchant's Stripe ──►  payment_intent.succeeded
                            │
/webhooks/stripe  ──►  Verify signature
                        Calculate donation
                        Log transaction
                        Update charity totals
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A Stripe account with Connect enabled
- A Stripe App created in the [Stripe Dashboard](https://dashboard.stripe.com/apps)

### 2. Install Dependencies

```bash
cd drip-backend
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

| Variable               | Description                                              |
|------------------------|----------------------------------------------------------|
| `STRIPE_SECRET_KEY`    | Your Stripe secret key (`sk_test_...` or `sk_live_...`) |
| `STRIPE_CLIENT_ID`     | Your Stripe Connect app's client ID (`ca_...`)          |
| `STRIPE_WEBHOOK_SECRET`| Signing secret from your webhook endpoint (`whsec_...`) |
| `DRIP_BASE_URL`        | The public URL of this server (`https://dripfinancial.org`)  |

### 4. Run the Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

The interactive API docs are available at `http://localhost:8000/docs`.

---

## OAuth Flow

1. **Merchant visits** `GET /oauth/connect`
   → Redirected to `https://connect.stripe.com/oauth/authorize?...`

2. **Merchant authorises** the Drip app in Stripe's UI.

3. **Stripe redirects** back to `GET /oauth/callback?code=ac_xxx`
   → Server exchanges the code for `access_token` + `refresh_token`
   → Merchant is stored in SQLite
   → A `payment_intent.succeeded` webhook is registered on their account
   → Merchant is redirected to `{DRIP_BASE_URL}/dashboard?connected=1`

4. **All subsequent payments** on the merchant's Stripe account trigger the webhook.

---

## API Endpoints

| Method | Path                    | Description                               |
|--------|-------------------------|-------------------------------------------|
| GET    | `/oauth/connect`        | Start OAuth flow                          |
| GET    | `/oauth/callback`       | OAuth callback (Stripe redirects here)    |
| POST   | `/webhooks/stripe`      | Stripe webhook receiver                   |
| GET    | `/api/health`           | Health check                              |
| GET    | `/api/stats`            | Dashboard KPIs                            |
| GET    | `/api/charities`        | List charities                            |
| POST   | `/api/charities`        | Add a charity                             |
| GET    | `/api/transactions`     | Paginated transaction log                 |
| GET    | `/api/settings`         | Get merchant settings                     |
| PUT    | `/api/settings`         | Update settings (donation %, auto-donate) |
| GET    | `/api/tax-report`       | Annual tax summary by charity             |
| GET    | `/api/allocations`      | Get charity allocation splits             |
| POST   | `/api/allocations`      | Set charity allocation splits             |

**Merchant identification:** Pass `Stripe-Account: acct_xxx` in the request header. If omitted, the first merchant in the DB is used (demo/single-tenant mode).

---

## Testing with Stripe CLI

### 1. Install Stripe CLI

```bash
brew install stripe/stripe-cli/stripe
stripe login
```

### 2. Forward Webhooks to Local Server

```bash
stripe listen \
  --forward-to localhost:8000/webhooks/stripe \
  --events payment_intent.succeeded
```

Copy the webhook signing secret printed by the CLI into your `.env`:
```
STRIPE_WEBHOOK_SECRET=whsec_...
```

### 3. Trigger a Test Payment

```bash
stripe trigger payment_intent.succeeded
```

You should see a new transaction appear in `GET /api/transactions`.

### 4. Test the OAuth Flow

With a Stripe Connect app configured:

```bash
open "http://localhost:8000/oauth/connect"
```

---

## Database

SQLite is used for development and single-server deployments. The DB file is `drip.db` in the working directory (configurable via `DB_PATH` env var).

**Tables:**
- `merchants` — Connected Stripe accounts and their settings
- `charities` — 501(c)(3) charity registry (8 demo charities seeded on first run)
- `transactions` — Donation ledger, one row per charity allocation per payment
- `charity_allocations` — Per-merchant charity split configuration
- `webhook_events` — Raw Stripe event log for debugging and idempotency

---

## Production Deployment

### Environment

- Set `DRIP_BASE_URL` to your real public domain (used in OAuth redirect URIs and webhook URLs)
- Use `sk_live_...` keys in production
- Never commit `.env` to version control

### Database

For production, replace SQLite with PostgreSQL:
1. Install `asyncpg` or `psycopg2`
2. Replace `sqlite3` calls with SQLAlchemy or an async ORM
3. Run migrations via Alembic

### HTTPS

The server must be behind a TLS-terminating reverse proxy (nginx, Caddy, or a managed load balancer). Stripe requires HTTPS for all webhook and OAuth endpoints.

### Running with Gunicorn (Production)

```bash
pip install gunicorn
gunicorn server:app \
  -w 4 \
  -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile -
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t drip-backend .
docker run -p 8000:8000 --env-file .env drip-backend
```

### Platform-as-a-Service

The server is stateless (except for the SQLite file). For PaaS deployments:

| Platform    | Notes                                            |
|-------------|--------------------------------------------------|
| Railway     | Set env vars in dashboard; attach a Postgres DB  |
| Render      | Use a persistent disk for SQLite or Postgres     |
| Fly.io      | Mount a volume for `drip.db`                     |
| AWS ECS     | Use RDS Postgres; deploy as a Fargate service    |

### Domain Configuration

`stripe-app.json` is pre-configured for `dripfinancial.org`:

```json
"allowed_redirect_uris": ["https://dripfinancial.org/oauth/callback"],
"post_install_action": { "url": "https://dripfinancial.org/onboarding" }
```

---

## Security Notes

- Webhook payloads are verified using `stripe.Webhook.construct_event()` before any processing
- Access tokens are stored in SQLite — use encrypted storage (AWS Secrets Manager, HashiCorp Vault) in production
- CORS is set to `allow_origins=["*"]` for development; restrict to your dashboard domain in production
- Rate limiting (e.g., slowapi) should be added to the OAuth and webhook endpoints in production
