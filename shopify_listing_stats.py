"""Read-only Shopify catalogue performance helpers.

The listing stats page deliberately lives outside the bulk editor mutation
path. It reads products plus ShopifyQL reports and never updates Shopify.
"""

from urllib.parse import unquote, urlsplit

import time
from shopify_graphql import ANALYTICS_ADMIN_API_VERSION, execute_graphql_query


VALID_RANGES = {"30d", "90d", "365d", "all"}


def normalize_range(value):
    return value if value in VALID_RANGES else "90d"


def shopifyql_period(value):
    value = normalize_range(value)
    if value == "30d":
        return "SINCE startOfDay(-30d) UNTIL today"
    if value == "90d":
        return "SINCE startOfDay(-90d) UNTIL today"
    if value == "365d":
        return "SINCE startOfDay(-365d) UNTIL today"
    return "SINCE 2000-01-01 UNTIL today"


def numeric_shopify_id(value):
    raw = str(value or "")
    return raw.rsplit("/", 1)[-1] if raw else ""


def product_handle_from_path(value):
    path = unquote(urlsplit(str(value or "")).path).rstrip("/")
    parts = path.split("/")
    if len(parts) >= 3 and parts[-2].lower() == "products" and parts[-1]:
        return parts[-1].lower()
    return None


def _number(value):
    try:
        number = float(value or 0)
        return number if number == number else 0
    except (TypeError, ValueError):
        return 0


def merge_listing_stats(products, sales_rows, traffic_rows):
    """Join ShopifyQL rows to the current product catalogue."""
    sales_by_id = {}
    for row in sales_rows:
        product_id = numeric_shopify_id(row.get("product_id"))
        if not product_id:
            continue
        current = sales_by_id.setdefault(product_id, {"orders": 0, "units_sold": 0, "net_sales": 0})
        # One product can have several rows if its title changed in the period.
        current["orders"] += _number(row.get("orders"))
        current["units_sold"] += _number(row.get("net_items_sold"))
        current["net_sales"] += _number(row.get("net_sales"))

    visits_by_handle = {}
    for row in traffic_rows:
        handle = product_handle_from_path(row.get("landing_page_path"))
        if handle:
            visits_by_handle[handle] = visits_by_handle.get(handle, 0) + _number(row.get("sessions"))

    merged = []
    for product in products:
        row = dict(product)
        sale = sales_by_id.get(str(product.get("product_id")), {})
        visits = visits_by_handle.get(str(product.get("handle") or "").lower(), 0)
        orders = sale.get("orders", 0)
        row.update({
            "visits": visits,
            "orders": orders,
            "units_sold": sale.get("units_sold", 0),
            "net_sales": sale.get("net_sales", 0),
            "conversion_rate": orders / visits if visits else None,
        })
        merged.append(row)
    return merged


CATALOGUE_QUERY = """
query ListingStatsCatalogue($first: Int!, $after: String) {
  shop { currencyCode }
  products(first: $first, after: $after, sortKey: TITLE) {
    nodes {
      id
      legacyResourceId
      title
      handle
      status
      createdAt
      updatedAt
      totalInventory
      onlineStoreUrl
      featuredMedia {
        ... on MediaImage { image { url } }
      }
      priceRangeV2 {
        minVariantPrice { amount }
        maxVariantPrice { amount }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


ANALYTICS_QUERY = """
query ListingStatsAnalytics($reportQuery: String!) {
  report: shopifyqlQuery(query: $reportQuery) {
    tableData { rows }
    parseErrors
  }
}
"""


def load_catalogue(shop_domain, access_token):
    products = []
    after = None
    currency = "GBP"
    store_handle = shop_domain.removesuffix(".myshopify.com")
    while True:
        result = execute_graphql_query(
            CATALOGUE_QUERY,
            {"first": 250, "after": after},
            shop_domain=shop_domain,
            access_token=access_token,
        )
        data = (result or {}).get("data") or {}
        connection = data.get("products")
        if not connection:
            raise RuntimeError("Shopify did not return the product catalogue")
        currency = (data.get("shop") or {}).get("currencyCode") or currency
        for node in connection.get("nodes") or []:
            numeric_id = numeric_shopify_id(node.get("legacyResourceId") or node.get("id"))
            price_range = node.get("priceRangeV2") or {}
            featured = node.get("featuredMedia") or {}
            image = featured.get("image") or {}
            products.append({
                "product_id": numeric_id,
                "gid": node.get("id") or "",
                "title": node.get("title") or "Untitled product",
                "handle": node.get("handle") or "",
                "status": str(node.get("status") or "").lower(),
                "created_at": node.get("createdAt") or "",
                "updated_at": node.get("updatedAt") or "",
                "inventory": node.get("totalInventory"),
                "image_url": image.get("url") or "",
                "storefront_url": node.get("onlineStoreUrl") or "",
                "admin_url": f"https://admin.shopify.com/store/{store_handle}/products/{numeric_id}",
                "price_min": (price_range.get("minVariantPrice") or {}).get("amount"),
                "price_max": (price_range.get("maxVariantPrice") or {}).get("amount"),
            })
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            raise RuntimeError("Shopify catalogue pagination stopped without a cursor")
    return currency, products


def load_analytics(shop_domain, access_token, selected_range):
    period = shopifyql_period(selected_range)

    def load_report(query_builder):
        rows = []
        for offset in range(0, 25000, 1000):
            report_query = query_builder(offset)
            last_error = None
            for attempt, delay in enumerate((0, 15, 30, 60), start=1):
                if delay:
                    time.sleep(delay)
                try:
                    result = execute_graphql_query(
                        ANALYTICS_QUERY,
                        {"reportQuery": report_query},
                        shop_domain=shop_domain,
                        access_token=access_token,
                        api_version=ANALYTICS_ADMIN_API_VERSION,
                        raise_errors=True,
                    )
                    report = ((result or {}).get("data") or {}).get("report") or {}
                    parse_errors = report.get("parseErrors") or []
                    if parse_errors:
                        messages = [
                            error.get("message", str(error)) if isinstance(error, dict) else str(error)
                            for error in parse_errors
                        ]
                        raise RuntimeError("Shopify analytics query failed: " + "; ".join(messages))
                    page = ((report.get("tableData") or {}).get("rows") or [])
                    rows.extend(page)
                    if len(page) < 1000:
                        return rows
                    time.sleep(2)
                    break
                except RuntimeError as exc:
                    last_error = exc
                    if "rate limit" not in str(exc).lower() or attempt == 4:
                        raise
            else:
                raise last_error or RuntimeError("Shopify analytics did not complete")
        return rows

    sales_rows = load_report(lambda offset: (
        "FROM sales SHOW orders, net_items_sold, net_sales "
        f"GROUP BY product_id, product_title {period} "
        f"ORDER BY net_items_sold DESC LIMIT 1000 OFFSET {offset}"
    ))
    traffic_rows = load_report(lambda offset: (
        "FROM sessions SHOW sessions WHERE human_or_bot_session = 'human' "
        f"GROUP BY landing_page_path {period} "
        f"ORDER BY sessions DESC LIMIT 1000 OFFSET {offset}"
    ))
    return sales_rows, traffic_rows


def load_listing_stats(shop_domain, access_token, selected_range, include_analytics=True):
    currency, products = load_catalogue(shop_domain, access_token)
    if not include_analytics:
        listings = [dict(product, visits=None, orders=None, units_sold=None, net_sales=None, conversion_rate=None) for product in products]
        return currency, listings
    sales_rows, traffic_rows = load_analytics(shop_domain, access_token, selected_range)
    return currency, merge_listing_stats(products, sales_rows, traffic_rows)
