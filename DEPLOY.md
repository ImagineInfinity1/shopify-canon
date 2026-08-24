# Deploying Listing Cannon to Render

## Prerequisites

1. A [Render](https://render.com) account
2. A [Shopify Partner](https://partners.shopify.com/) account with an app created
3. A [Google AI Studio](https://aistudio.google.com/) API key (for Gemini)

---

## Step 1: Create a Shopify App

1. Go to **Shopify Partners > Apps > Create app** (or use an existing app).
2. Set the **App URL** to your Render service URL (e.g. `https://listing-cannon.onrender.com`).
3. Set the **Allowed redirection URL** to `https://listing-cannon.onrender.com/shopify/callback`.
4. Note the **API key** and **API secret key** from the app credentials page.

The app requires these OAuth scopes (requested automatically during the connect flow):
- `read_products`, `write_products`
- `read_product_listings`, `write_product_listings`
- `read_inventory`, `write_inventory`
- `read_publications`, `write_publications`
- `read_markets`

---

## Step 2: Deploy to Render

### Option A: One-click with render.yaml

1. Push this repository to GitHub/GitLab.
2. In Render, click **New > Blueprint** and select your repo.
3. Render will read `render.yaml` and create:
   - A **Web Service** (`listing-cannon`)
   - A **PostgreSQL database** (`listing-cannon-db`)
4. Set the following environment variables in the Render dashboard:
   - `SHOPIFY_API_KEY` — from Step 1
   - `SHOPIFY_API_SECRET` — from Step 1
   - `GEMINI_API_KEY` — from Google AI Studio
   - `APP_URL` — your Render service URL (e.g. `https://listing-cannon.onrender.com`)

### Option B: Manual setup

1. Create a **PostgreSQL** database in Render.
2. Create a **Web Service**:
   - **Runtime**: Python
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn -c gunicorn.conf.py app:app`
3. Set environment variables as listed in `.env.example`.
4. Link the database: set `DATABASE_URL` to the Internal Database URL.

---

## Step 3: Create Staff Accounts

Public registration is **disabled by default** (`REGISTRATION_ENABLED=false`).

### Using the CLI script

SSH into the Render shell (or run locally with `DATABASE_URL` pointing to your Render database):

```bash
python create_user.py --username admin --email admin@company.com --password "your-strong-password"
```

### Enabling public registration (later)

Set `REGISTRATION_ENABLED=true` in the Render environment variables to allow anyone to sign up.

---

## Step 4: Enable Render Basic Auth (recommended for staff-only phase)

For an additional layer of protection during the staff-only phase:

1. Open your Web Service in the Render dashboard.
2. Go to **Settings** (or **Environment**).
3. Look for **Basic Authentication** / **Password Protection**.
4. Enable it and set a shared username/password.
5. Share these credentials with your staff.

This puts an HTTP Basic Auth prompt in front of the entire app, so nobody can even see the login page without the shared password.

---

## Step 5: Connect a Shopify Store

You can connect a store in one of two ways.

### Option 1: Custom app (single store, no OAuth)

If you use a **Custom app** created in your store (Settings → Apps and sales channels → Develop apps), you can connect by setting two environment variables on Render. No "Connect" button needed.

1. In Shopify Admin: **Settings → Apps and sales channels → Develop apps** → your app (or create one).
2. Configure **Admin API** scopes (e.g. `read_products`, `write_products`, `read_product_listings`, `write_product_listings`, and any others the app needs).
3. **Install app** (if not already), then open **API credentials** and **Reveal token once** — copy the token (starts with `shpat_`).
4. In the Render dashboard, add these environment variables to your Web Service:
   - `SHOP_DOMAIN` = `your-store.myshopify.com` (your store’s myshopify domain)
   - `SHOPIFY_ACCESS_TOKEN` = the token you copied
5. Redeploy (or let auto-deploy run). Log in to the app; the store will be connected automatically for your user.

You do **not** need `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, or the in-app Connect flow for this mode.

### Option 2: OAuth (Partners app, multi-tenant)

1. Log in to the app.
2. Go to **Account** (user menu dropdown).
3. Enter your Shopify store domain (e.g. `my-store.myshopify.com`) and click **Connect**.
4. You will be redirected to Shopify to authorise the app.
5. After authorisation, you will be redirected back and the store will be connected.

This requires a Shopify **Partners** app (Step 1 of this guide) and the OAuth env vars. Each user can connect their own store; credentials are stored per user in the database.

---

## Local Development

```bash
# 1. Copy environment file
cp .env.example .env
# Edit .env with your actual API keys

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run locally
python run_local.py
# App runs at http://localhost:5000

# 4. Create a test user
python create_user.py --username dev --email dev@test.com --password "password123"
```

For local Shopify OAuth testing, you may need to use a tunnel tool like ngrok to get a public URL, then set `APP_URL` to that URL.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `SHOPIFY_API_KEY` | Yes* | — | Shopify app API key (for OAuth; *not needed for Custom app mode) |
| `SHOPIFY_API_SECRET` | Yes* | — | Shopify app API secret (for OAuth; *not needed for Custom app mode) |
| `SESSION_SECRET` | Yes | auto-generated | Flask session secret |
| `APP_URL` | Yes* | `http://localhost:5000` | Public URL of the app (*for OAuth callback) |
| `SHOP_DOMAIN` | No | — | For Custom app mode: your store (e.g. `your-store.myshopify.com`) |
| `SHOPIFY_ACCESS_TOKEN` | No | — | For Custom app mode: Admin API token from Develop apps |
| `DATABASE_URL` | No | SQLite | PostgreSQL connection string |
| `REGISTRATION_ENABLED` | No | `false` | Enable public user registration |
