"""
Shopify OAuth 2.0 flow for multi-tenant store connections.

Routes:
  /shopify/connect   – Initiates OAuth; redirects merchant to Shopify authorisation screen
  /shopify/callback  – Handles the callback; exchanges code for access token; saves Shop row
  /shopify/disconnect – Removes the connected shop for the current user
"""

import os
import hmac
import hashlib
import logging
import requests
from urllib.parse import urlencode
import json
import time

from flask import redirect, request, session, flash, url_for, jsonify
from flask_login import login_required, current_user

from extensions import db
from models import Shop

logger = logging.getLogger(__name__)

# Scopes the app requests from the merchant's Shopify store
SHOPIFY_SCOPES = ",".join([
    "read_products",
    "write_products",
    # Read-only analytics used by the Listing Stats spreadsheet.
    "read_reports",
    "read_product_listings",
    "write_product_listings",
    "read_inventory",
    "write_inventory",
    "read_publications",
    "write_publications",
    "read_markets",
])


def _get_api_key():
    # Render values are occasionally pasted with a trailing newline. Shopify
    # credentials never contain surrounding whitespace, so normalise it here.
    return os.environ.get("SHOPIFY_API_KEY", "").strip()


def _get_api_secret():
    return os.environ.get("SHOPIFY_API_SECRET", "").strip()


def _get_app_url():
    """Return the public base URL of this app (no trailing slash)."""
    return (os.environ.get("APP_URL") or "http://localhost:5000").rstrip("/")


def _normalise_oauth_params(query_params):
    """Preserve repeated OAuth parameters instead of flattening MultiDicts."""
    if hasattr(query_params, "lists"):
        pairs = query_params.lists()
    else:
        pairs = query_params.items()

    normalised = {}
    for key, raw_value in pairs:
        if isinstance(raw_value, (list, tuple)):
            values = list(raw_value)
            normalised[key] = values if key.endswith("[]") else (values[0] if values else "")
        else:
            normalised[key] = raw_value
    return normalised


def _verify_hmac(query_params) -> bool:
    """Verify the HMAC signature Shopify sends on the callback."""
    secret = _get_api_secret()
    if not secret:
        return False

    query_params = _normalise_oauth_params(query_params)
    received_hmac = str(query_params.get("hmac", ""))
    try:
        # Reject stale callbacks as well as invalid signatures.
        if int(query_params.get("timestamp", 0)) < time.time() - (24 * 60 * 60):
            return False
    except (TypeError, ValueError):
        return False

    # Shopify OAuth signatures do not use urllib.urlencode. In particular,
    # urlencode changes padding in the base64 host parameter from = to %3D,
    # producing a different digest. This mirrors Shopify's Python client.
    encoded_pairs = []
    for raw_key, raw_value in query_params.items():
        if raw_key == "hmac":
            continue
        key = str(raw_key)
        value = raw_value
        if key.endswith("[]"):
            key = key[:-2]
            values = value if isinstance(value, (list, tuple)) else [value]
            value = json.dumps([str(item) for item in values])
        elif isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        key = key.replace("%", "%25").replace("=", "%3D")
        value = str(value).replace("%", "%25")
        encoded_pairs.append(f"{key}={value}".replace("&", "%26"))
    message = "&".join(sorted(encoded_pairs))

    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(digest, received_hmac)


def init_oauth_routes(app):
    """Register Shopify OAuth routes on the Flask app."""

    @app.route("/shopify/connect")
    @login_required
    def shopify_connect():
        """Step 1: Redirect the user to Shopify's OAuth authorisation page."""
        shop_domain = request.args.get("shop", "").strip()
        if not shop_domain:
            flash("Please enter your Shopify store domain.", "error")
            return redirect(url_for("account"))

        # Normalise: ensure it ends with .myshopify.com
        if not shop_domain.endswith(".myshopify.com"):
            # Accept "my-store" or "my-store.myshopify.com"
            shop_domain = shop_domain.replace("https://", "").replace("http://", "").split("/")[0]
            if not shop_domain.endswith(".myshopify.com"):
                shop_domain = shop_domain + ".myshopify.com"

        api_key = _get_api_key()
        if not api_key:
            flash("Shopify API key is not configured. Please contact the administrator.", "error")
            return redirect(url_for("account"))

        # Store a nonce in session for CSRF protection
        import secrets
        nonce = secrets.token_urlsafe(16)
        session["shopify_oauth_nonce"] = nonce
        session["shopify_oauth_shop"] = shop_domain

        redirect_uri = _get_app_url() + "/shopify/callback"
        params = urlencode({
            "client_id": api_key,
            "scope": SHOPIFY_SCOPES,
            "redirect_uri": redirect_uri,
            "state": nonce,
        })

        auth_url = f"https://{shop_domain}/admin/oauth/authorize?{params}"
        logger.info(
            "Redirecting user %s to Shopify OAuth for %s (api_key_configured=%s callback=%s)",
            current_user.id,
            shop_domain,
            bool(api_key),
            redirect_uri,
        )
        return redirect(auth_url)

    @app.route("/shopify/callback")
    @login_required
    def shopify_callback():
        """Step 2: Handle Shopify's OAuth callback — exchange code for access token."""
        # Verify HMAC
        if not _verify_hmac(request.args):
            logger.warning(
                "Shopify OAuth callback HMAC verification failed for shop=%s keys=%s "
                "api_key_configured=%s secret_configured=%s secret_fingerprint=%s "
                "callback=%s/shopify/callback",
                request.args.get("shop", "unknown"),
                ",".join(sorted(request.args.keys())),
                bool(_get_api_key()),
                bool(_get_api_secret()),
                hashlib.sha256(_get_api_secret().encode("utf-8")).hexdigest()[:8]
                if _get_api_secret() else "missing",
                _get_app_url(),
            )
            flash("Authentication failed: invalid signature.", "error")
            return redirect(url_for("account"))

        # Verify state/nonce
        state = request.args.get("state", "")
        expected_nonce = session.pop("shopify_oauth_nonce", None)
        if not expected_nonce or state != expected_nonce:
            logger.warning("Shopify OAuth callback nonce mismatch")
            flash("Authentication failed: invalid state.", "error")
            return redirect(url_for("account"))

        shop_domain = request.args.get("shop", "")
        code = request.args.get("code", "")

        if not shop_domain or not code:
            flash("Authentication failed: missing parameters.", "error")
            return redirect(url_for("account"))

        # Exchange the temporary code for a permanent access token
        token_url = f"https://{shop_domain}/admin/oauth/access_token"
        payload = {
            "client_id": _get_api_key(),
            "client_secret": _get_api_secret(),
            "code": code,
        }

        try:
            resp = requests.post(token_url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Shopify token exchange failed: {e}")
            flash("Failed to connect to Shopify. Please try again.", "error")
            return redirect(url_for("account"))

        access_token = data.get("access_token")
        scope = data.get("scope", "")

        if not access_token:
            logger.error(f"No access_token in Shopify response: {data}")
            flash("Failed to obtain access token from Shopify.", "error")
            return redirect(url_for("account"))

        # Fetch the shop name for display purposes
        shop_name = _fetch_shop_name(shop_domain, access_token)

        # Upsert the Shop record
        existing = Shop.query.filter_by(shop_domain=shop_domain).first()
        if existing:
            # Store was previously connected — update token and reassign to current user
            existing.access_token = access_token
            existing.scope = scope
            existing.user_id = current_user.id
            existing.is_active = True
            if shop_name:
                existing.shop_name = shop_name
            shop = existing
        else:
            shop = Shop(
                user_id=current_user.id,
                shop_domain=shop_domain,
                access_token=access_token,
                scope=scope,
                shop_name=shop_name,
                is_active=True,
            )
            db.session.add(shop)

        db.session.commit()

        # Subscribe to product and collection webhooks so the cached catalogue
        # updates itself. A failure here must not block the connection: the app
        # still works on manual refresh, and Settings has a Register button.
        try:
            from shopify_webhooks import callback_url_for, ensure_webhooks

            callback_url = callback_url_for(_get_app_url())
            if callback_url.startswith("https://"):
                summary = ensure_webhooks(callback_url, shop_domain, access_token)
                logger.info("Webhook registration for %s: %s", shop_domain, summary)
            else:
                logger.info("Skipping webhook registration: %s is not https.", callback_url)
        except Exception as webhook_error:
            logger.warning("Webhook registration failed for %s: %s", shop_domain, webhook_error)

        # Set as active shop in session
        session["current_shop_id"] = shop.id
        logger.info(f"User {current_user.id} connected shop {shop_domain} (id={shop.id})")
        flash(f"Successfully connected to {shop_name or shop_domain}!", "success")
        return redirect(url_for("index"))

    @app.route("/shopify/disconnect", methods=["POST"])
    @login_required
    def shopify_disconnect():
        """Remove the connected Shopify store for the current user."""
        shop_id = request.form.get("shop_id") or session.get("current_shop_id")
        if shop_id:
            shop = Shop.query.filter_by(id=shop_id, user_id=current_user.id).first()
            if shop:
                shop_domain = shop.shop_domain
                db.session.delete(shop)
                db.session.commit()
                session.pop("current_shop_id", None)
                logger.info(f"User {current_user.id} disconnected shop {shop_domain}")
                flash(f"Disconnected from {shop_domain}.", "info")
        return redirect(url_for("account"))

    @app.route("/shopify/switch", methods=["POST"])
    @login_required
    def shopify_switch():
        """Switch the active shop (if a user has multiple)."""
        shop_id = request.form.get("shop_id")
        if shop_id:
            shop = Shop.query.filter_by(id=shop_id, user_id=current_user.id, is_active=True).first()
            if shop:
                session["current_shop_id"] = shop.id
                flash(f"Switched to {shop.shop_name or shop.shop_domain}.", "success")
        return redirect(url_for("index"))


def _fetch_shop_name(shop_domain, access_token):
    """Fetch the store name from Shopify's shop API."""
    try:
        url = f"https://{shop_domain}/admin/api/2025-07/shop.json"
        headers = {"X-Shopify-Access-Token": access_token}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok:
            return resp.json().get("shop", {}).get("name", "")
    except Exception as e:
        logger.warning(f"Could not fetch shop name for {shop_domain}: {e}")
    return None
