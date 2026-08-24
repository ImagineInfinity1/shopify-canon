"""
Helpers for resolving the current user's active Shopify shop.

get_current_shop() is the single source of truth for "which shop's credentials
should this request use?"  Every Shopify API call should go through this.
"""

import logging
import os
from flask import session
from flask_login import current_user

from extensions import db
from models import Shop

logger = logging.getLogger(__name__)


def _normalize_shop_domain(domain):
    """Ensure domain is in the form store.myshopify.com."""
    if not domain or not isinstance(domain, str):
        return None
    domain = domain.strip().replace("https://", "").replace("http://", "").split("/")[0]
    if not domain.endswith(".myshopify.com"):
        domain = domain + ".myshopify.com"
    return domain.lower()


def get_current_shop():
    """Return the active Shop object for the logged-in user, or None.

    Resolution order:
      1. session['current_shop_id'] — explicitly selected shop
      2. First active shop owned by current_user (auto-select if only one)
      3. Env-based Custom app: SHOP_DOMAIN + SHOPIFY_ACCESS_TOKEN (find or create Shop for user)

    If a shop is found via (2) or (3) it is written back to session for speed.
    """
    if not current_user or not current_user.is_authenticated:
        return None

    # 1. Try session
    shop_id = session.get("current_shop_id")
    if shop_id:
        shop = Shop.query.filter_by(id=shop_id, user_id=current_user.id, is_active=True).first()
        if shop:
            return shop
        # Stale session value — clear it
        session.pop("current_shop_id", None)

    # 2. Auto-select the user's first (or only) active shop
    shop = Shop.query.filter_by(user_id=current_user.id, is_active=True).first()
    if shop:
        session["current_shop_id"] = shop.id
        return shop

    # 3. Env-based Custom app (single store): no OAuth required
    env_domain = os.environ.get("SHOP_DOMAIN", "").strip()
    env_token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
    if env_domain and env_token:
        domain = _normalize_shop_domain(env_domain)
        if domain:
            # shop_domain is unique globally; find by domain (any user) or create for this user
            shop = Shop.query.filter_by(shop_domain=domain).first()
            if shop:
                shop.user_id = current_user.id
                shop.access_token = env_token
                shop.scope = shop.scope or "custom_app"
                shop.is_active = True
                db.session.commit()
            else:
                shop = Shop(
                    user_id=current_user.id,
                    shop_domain=domain,
                    access_token=env_token,
                    scope="custom_app",
                    shop_name=None,
                    is_active=True,
                )
                db.session.add(shop)
                db.session.commit()
            session["current_shop_id"] = shop.id
            logger.info("Using env-based Custom app shop for user %s: %s", current_user.id, domain)
            return shop

    return None


def get_shop_credentials():
    """Return (shop_domain, access_token) for the current shop, or (None, None)."""
    shop = get_current_shop()
    if shop:
        return shop.shop_domain, shop.access_token
    return None, None


def require_shop(f):
    """Decorator for routes that require an active Shopify shop connection.

    Returns a JSON error if no shop is connected (useful for API endpoints).
    """
    from functools import wraps
    from flask import jsonify

    @wraps(f)
    def decorated(*args, **kwargs):
        shop = get_current_shop()
        if not shop:
            return jsonify({
                "error": "No Shopify store connected. Please connect your store first.",
                "no_shop": True,
            }), 400
        return f(*args, **kwargs)
    return decorated
