# Drip Donations — Stripe App UI Extension Setup

This guide walks you through building and uploading the Stripe App UI extension
so the Settings and Dashboard views appear in the Stripe Dashboard.

---

## Prerequisites

- [Stripe CLI](https://stripe.com/docs/stripe-cli) installed and logged in
- Stripe Apps CLI plugin: `stripe plugin install apps`
- Node.js >= 18
- Your Drip backend running at `https://dripfinancial.org`

---

## Step 1: Install Dependencies

```bash
cd drip-backend
npm install
```

---

## Step 2: Preview Locally

Start the local dev server to test your views in the Stripe Dashboard:

```bash
stripe apps start
```

Then open:
- **Settings view**: https://dashboard.stripe.com/apps/settings-preview
- **Dashboard drawer**: Open any page in Stripe Dashboard, click the Apps icon

---

## Step 3: Upload to Stripe

When the views look good, upload the app:

```bash
stripe apps upload
```

This bundles the TypeScript/React code and hosts it on Stripe's CDN.

---

## Step 4: Set App Version Live

After uploading:

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
  - Saves directly to your Drip backend API
  - Donations auto-split equally among selected charities

### Dashboard View (`DashboardView.tsx`)
- **Viewport**: `stripe.dashboard.drawer.default` + `stripe.dashboard.payment.detail`
- **Features**:
  - Shows total donated, transactions today, active charities
  - Donation breakdown by category
  - Active/Paused status badge
  - Link to full Drip dashboard

---

## How It Connects to the Backend

The UI extension makes HTTP requests to `https://dripfinancial.org/api/*`:

| Action | Endpoint | Method |
|--------|----------|--------|
| Load charities | `/api/charities?verified=true` | GET |
| Load settings | `/api/settings` | GET |
| Save settings | `/api/settings` | PUT |
| Load allocations | `/api/allocations` | GET |
| Save allocations | `/api/allocations` | POST |
| Load stats | `/api/stats` | GET |

The `Stripe-Account` header identifies which merchant's data to read/write.

---

## Content Security Policy

The `stripe-app.json` manifest includes a CSP that allows connections to
`https://dripfinancial.org`. If you change your backend URL, update the
`content_security_policy.connect-src` field in the manifest.

---

## Troubleshooting

- **"No settings created"**: Upload the app with `stripe apps upload`
- **Settings not saving**: Check that `https://dripfinancial.org` is accessible
  and the CORS middleware allows the Stripe Dashboard origin
- **Charities not loading**: Verify the `/api/charities` endpoint returns data
- **Preview not working**: Run `stripe apps start` and check the terminal for errors
