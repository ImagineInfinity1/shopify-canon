"""
Shopify Category Metafields - Auto-fill category-specific attributes.

After product creation, this module:
1. Queries the taxonomy for what attributes the product category has
2. Enables standard metaobject definitions for each attribute type
3. Queries metaobject entries to get available values + their store-specific GIDs
4. Converts AI choices from the main metadata response into metaobject GIDs
5. Sets metafields on the product using metafieldsSet mutation
"""

import json
import logging
import re
import time

from shopify_graphql import execute_graphql_query

logger = logging.getLogger(__name__)

# --- Attribute name -> metafield key mapping ---
ATTRIBUTE_KEY_MAP = {
    "Color": "color-pattern",
    "Pattern": "color-pattern",
    "Material": "material",
    "Fabric": "fabric",
    "Art movement": "art-movement",
    "Art style": "art-style",
    "Artwork authenticity": "artwork-authenticity",
    "Frame style": "frame-style",
    "Orientation": "orientation",
    "Theme": "theme",
    "Target Gender": "target-gender",
    "Age Group": "age-group",
    "Size": "size",
}

POSTER_ATTRIBUTE_FALLBACKS = [
    {"name": "Color", "values": []},
    {"name": "Material", "values": []},
    {"name": "Art movement", "values": []},
    {"name": "Art style", "values": []},
    {"name": "Artwork authenticity", "values": []},
    {"name": "Frame style", "values": []},
    {"name": "Orientation", "values": []},
    {"name": "Theme", "values": []},
]

# --- Caches ---
_category_attributes_cache = {}  # {category_gid: {data: [...], fetched_at: float}}
_metaobject_values_cache = {}    # {type: {data: {...}, fetched_at: float}}
_enabled_definitions = set()     # tracks enabled metaobject types this app run

CACHE_TTL = 86400  # 24 hours


def _cache_valid(cache_entry):
    """Check if a cache entry is still valid (within TTL)."""
    if not cache_entry:
        return False
    return (time.time() - cache_entry.get('fetched_at', 0)) < CACHE_TTL


def get_category_attributes(category_gid, shop_domain=None, access_token=None):
    """
    Query the taxonomy for a category's choice-list attributes and their allowed values.

    Returns: [{name: "Color", values: ["Black", "Blue", ...]}, ...]
    """
    # Check cache
    if category_gid in _category_attributes_cache and _cache_valid(_category_attributes_cache[category_gid]):
        logger.info(f"Using cached category attributes for {category_gid}")
        return _category_attributes_cache[category_gid]['data']

    query = """
    query getTaxonomyCategory($id: ID!) {
        node(id: $id) {
            ... on TaxonomyCategory {
                id
                name
                attributes(first: 250) {
                    pageInfo { hasNextPage }
                    nodes {
                        ... on TaxonomyChoiceListAttribute {
                            id
                            name
                            values(first: 250) {
                                pageInfo { hasNextPage endCursor }
                                nodes {
                                    id
                                    name
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    variables = {"id": category_gid}
    result = execute_graphql_query(query, variables, shop_domain=shop_domain, access_token=access_token)

    if not result:
        logger.warning(f"Failed to query taxonomy for category {category_gid}")
        return []

    if 'errors' in result:
        logger.warning(f"GraphQL errors querying taxonomy: {result['errors']}")
        return []

    category_data = result.get('data', {}).get('node')
    if not category_data:
        logger.warning(f"No taxonomy category found for {category_gid}")
        return []

    if (category_data.get('attributes', {}).get('pageInfo') or {}).get('hasNextPage'):
        logger.warning(
            "Category %s has more than 250 attributes; some were not read.", category_gid
        )

    attributes = []
    for node in category_data.get('attributes', {}).get('nodes', []):
        name = node.get('name')
        values_connection = node.get('values') or {}
        values_nodes = list(values_connection.get('nodes') or [])
        # An allowed value the app never sees is a value the AI can never pick,
        # which is how a metafield ends up blank. Read every page.
        page_info = values_connection.get('pageInfo') or {}
        if page_info.get('hasNextPage') and node.get('id'):
            from shopify_graphql import fetch_remaining_connection_nodes

            values_nodes.extend(fetch_remaining_connection_nodes(
                node['id'], 'TaxonomyChoiceListAttribute', 'values', 'id name',
                page_info.get('endCursor'),
                shop_domain=shop_domain, access_token=access_token,
            ))
        if name and values_nodes:
            value_names = [v['name'] for v in values_nodes if v.get('name')]
            attributes.append({
                'name': name,
                'values': value_names
            })

    # Cache the result
    _category_attributes_cache[category_gid] = {
        'data': attributes,
        'fetched_at': time.time()
    }

    logger.info(f"Found {len(attributes)} attributes for category {category_gid}")
    for attr in attributes:
        logger.info(f"  - {attr['name']}: {len(attr['values'])} values")

    return attributes


def _get_metafield_key(attribute_name):
    """Map attribute name to metafield key."""
    if attribute_name in ATTRIBUTE_KEY_MAP:
        return ATTRIBUTE_KEY_MAP[attribute_name]
    return attribute_name.lower().replace(" ", "-")


def _get_metaobject_type(metafield_key):
    """Get the metaobject type from a metafield key."""
    return f"shopify--{metafield_key}"


def _is_poster_category(category_gid):
    return bool(category_gid and str(category_gid).startswith("gid://shopify/TaxonomyCategory/hg-3-4"))


def enable_metaobject_definition(metaobject_type, shop_domain=None, access_token=None):
    """
    Enable a standard metaobject definition in the store (idempotent).
    Tracks already-enabled types to avoid repeated calls.

    Returns True if enabled (or already enabled), False on failure.
    """
    cache_key = (shop_domain or "", metaobject_type)
    if cache_key in _enabled_definitions:
        return True

    mutation = """
    mutation enableStandardMetaobject($type: String!) {
        standardMetaobjectDefinitionEnable(type: $type) {
            metaobjectDefinition {
                id
                type
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    variables = {"type": metaobject_type}
    result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)

    if not result:
        logger.warning(f"Failed to enable metaobject definition: {metaobject_type}")
        return False

    if 'errors' in result:
        # Check if it's an "already enabled" error - that's fine
        error_msgs = [e.get('message', '') for e in result.get('errors', [])]
        already_enabled = any('already' in msg.lower() for msg in error_msgs)
        if already_enabled:
            logger.info(f"Metaobject definition already enabled: {metaobject_type}")
            _enabled_definitions.add(cache_key)
            return True
        logger.warning(f"GraphQL errors enabling metaobject: {result['errors']}")
        return False

    mutation_data = result.get('data', {}).get('standardMetaobjectDefinitionEnable', {})
    user_errors = mutation_data.get('userErrors', [])
    if user_errors:
        # "already enabled" may come as a userError too
        error_msgs = [e.get('message', '') for e in user_errors]
        already_enabled = any('already' in msg.lower() for msg in error_msgs)
        if already_enabled:
            logger.info(f"Metaobject definition already enabled: {metaobject_type}")
            _enabled_definitions.add(cache_key)
            return True
        logger.warning(f"User errors enabling metaobject {metaobject_type}: {user_errors}")
        return False

    _enabled_definitions.add(cache_key)
    logger.info(f"Enabled metaobject definition: {metaobject_type}")
    return True


def get_metaobject_values(metaobject_type, shop_domain=None, access_token=None):
    """
    Query all metaobject entries for a type to build a name->GID map.

    Returns: {"Black": "gid://shopify/Metaobject/123", "Blue": "gid://shopify/Metaobject/456", ...}
    """
    # Check cache
    cache_key = (shop_domain or "", metaobject_type)
    if cache_key in _metaobject_values_cache and _cache_valid(_metaobject_values_cache[cache_key]):
        logger.info(f"Using cached metaobject values for {metaobject_type}")
        return _metaobject_values_cache[cache_key]['data']

    query = """
    query getMetaobjects($type: String!, $first: Int!) {
        metaobjects(type: $type, first: $first) {
            nodes {
                id
                displayName
            }
        }
    }
    """
    variables = {"type": metaobject_type, "first": 250}
    result = execute_graphql_query(query, variables, shop_domain=shop_domain, access_token=access_token)

    if not result:
        logger.warning(f"Failed to query metaobjects for type: {metaobject_type}")
        return {}

    if 'errors' in result:
        logger.warning(f"GraphQL errors querying metaobjects: {result['errors']}")
        return {}

    nodes = result.get('data', {}).get('metaobjects', {}).get('nodes', [])
    values_map = {}
    for node in nodes:
        display_name = node.get('displayName', '')
        gid = node.get('id', '')
        if display_name and gid:
            values_map[display_name] = gid

    # Cache the result
    _metaobject_values_cache[cache_key] = {
        'data': values_map,
        'fetched_at': time.time()
    }

    logger.info(f"Found {len(values_map)} metaobject entries for {metaobject_type}")
    return values_map


def _prepare_category_attribute_context(category_gid, shop_domain=None, access_token=None):
    """
    Build the deterministic category-attribute context used by both the AI prompt
    and the post-create metafieldsSet mutation.
    """
    attributes = get_category_attributes(category_gid, shop_domain=shop_domain, access_token=access_token)
    if not attributes:
        if _is_poster_category(category_gid):
            logger.info("Using poster category attribute fallback for %s", category_gid)
            attributes = POSTER_ATTRIBUTE_FALLBACKS
        else:
            return None

    key_to_attributes = {}
    for attr in attributes:
        mf_key = _get_metafield_key(attr['name'])
        key_to_attributes.setdefault(mf_key, []).append(attr)

    key_to_metaobject_values = {}
    for mf_key in key_to_attributes:
        mo_type = _get_metaobject_type(mf_key)

        if not enable_metaobject_definition(mo_type, shop_domain=shop_domain, access_token=access_token):
            logger.warning(f"Could not enable metaobject definition {mo_type}, skipping")
            continue

        values_map = get_metaobject_values(mo_type, shop_domain=shop_domain, access_token=access_token)
        if not values_map:
            logger.warning(f"No metaobject entries found for {mo_type}, skipping")
            continue

        key_to_metaobject_values[mf_key] = values_map

    if not key_to_metaobject_values:
        return None

    attributes_with_options = {}
    for mf_key, values_map in key_to_metaobject_values.items():
        attr_names = [a['name'] for a in key_to_attributes[mf_key]]
        available_options = list(values_map.keys())
        for attr_name in attr_names:
            attributes_with_options[attr_name] = available_options

    return {
        'attributes': attributes,
        'key_to_attributes': key_to_attributes,
        'key_to_metaobject_values': key_to_metaobject_values,
        'attributes_with_options': attributes_with_options,
    }


def get_category_attribute_options(category_gid, shop_domain=None, access_token=None):
    """
    Return {attribute_name: [allowed display values]} for prompt injection.
    This performs the Shopify taxonomy/metaobject lookup before the single AI
    image-analysis call.
    """
    try:
        context = _prepare_category_attribute_context(
            category_gid,
            shop_domain=shop_domain,
            access_token=access_token,
        )
        if not context:
            return {}
        return context['attributes_with_options']
    except Exception as exc:
        logger.warning(f"Could not prepare category attribute options for {category_gid}: {exc}")
        return {}


def _coerce_pick_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    # AI and generic metafields often return "Green, Beige, Brown" or
    # "Nature; Landscape". Split these into individual choices so Shopify's
    # exact metaobject values can be matched independently.
    return [part.strip() for part in re.split(r"[,;\n/]+", text) if part.strip()]


def _match_pick_to_gid(value, values_map):
    """Return the GID for an exact or case-insensitive allowed value match."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in values_map:
        return values_map[text]
    text_folded = text.casefold()
    for allowed, gid in values_map.items():
        if allowed.casefold() == text_folded:
            return gid

    aliases = {
        # Art movement: stores expose different broad labels, so map the common
        # broad answers onto whichever one this store actually allows. Nothing
        # is set unless the alias exists in the store's allowed values.
        "contemporary": ["Contemporary art", "Contemporary", "Modernism", "Modern art", "Modern"],
        "contemporary art": ["Contemporary art", "Contemporary", "Modernism"],
        "modern": ["Modernism", "Modern art", "Modern", "Contemporary art", "Contemporary"],
        "modernism": ["Modernism", "Modern art", "Modern", "Contemporary art", "Contemporary"],
        "illustrative": ["Illustration", "Contemporary art", "Contemporary", "Modernism"],
        "vertical": ["Portrait"],
        "portrait": ["Vertical"],
        "reproduction": ["Reproduction/Print", "Print", "Reproduction"],
        "print": ["Reproduction/Print", "Reproduction"],
        "paper": ["Paper", "Fine art paper", "Matte paper"],
        "unframed": ["Unframed", "No frame", "Frameless"],
        "multi-color": ["Multicolor", "Multi-color", "Multi color"],
        "multi color": ["Multicolor", "Multi-color", "Multi color"],
    }
    for alias in aliases.get(text_folded, []):
        if alias in values_map:
            return values_map[alias]
        alias_folded = alias.casefold()
        for allowed, gid in values_map.items():
            if allowed.casefold() == alias_folded:
                return gid

    # Last resort: allow a contained-word match for descriptive AI values such as
    # "Japanese Illustrative" when Shopify's allowed value is "Illustrative".
    for allowed, gid in values_map.items():
        allowed_folded = allowed.casefold()
        if len(allowed_folded) >= 4 and (
            allowed_folded in text_folded or text_folded in allowed_folded
        ):
            return gid
    return None


def _normalise_attribute_name(attr_name, attributes_with_options):
    if attr_name in attributes_with_options:
        return attr_name
    attr_folded = str(attr_name).strip().casefold()
    for allowed_attr in attributes_with_options:
        if allowed_attr.casefold() == attr_folded:
            return allowed_attr
    return None


def build_category_metafield_entries(category_gid, category_attribute_picks, shop_domain=None, access_token=None):
    """
    Convert validated AI category_attribute_picks into metafieldsSet entries.

    category_attribute_picks format:
        {"Color": ["Blue", "White"], "Material": ["Paper"]}
    """
    if not isinstance(category_attribute_picks, dict) or not category_attribute_picks:
        return []

    context = _prepare_category_attribute_context(
        category_gid,
        shop_domain=shop_domain,
        access_token=access_token,
    )
    if not context:
        return []

    attributes_with_options = context['attributes_with_options']
    key_to_metaobject_values = context['key_to_metaobject_values']

    metafield_gids = {}
    matched_debug = {}
    for attr_name, picked_values in category_attribute_picks.items():
        matched_attr_name = _normalise_attribute_name(attr_name, attributes_with_options)
        if not matched_attr_name:
            logger.warning(f"AI returned unknown category attribute '{attr_name}', skipping")
            continue

        mf_key = _get_metafield_key(matched_attr_name)
        values_map = key_to_metaobject_values.get(mf_key, {})
        if not values_map:
            logger.warning(f"No metaobject values available for category attribute '{matched_attr_name}', skipping")
            continue

        for picked_value in _coerce_pick_list(picked_values):
            gid = _match_pick_to_gid(picked_value, values_map)
            if gid:
                metafield_gids.setdefault(mf_key, [])
                if gid not in metafield_gids[mf_key]:
                    metafield_gids[mf_key].append(gid)
                    matched_debug.setdefault(mf_key, []).append(str(picked_value))
            else:
                sample = ", ".join(list(values_map.keys())[:20])
                logger.warning(
                    "No allowed metaobject value matched AI pick '%s' for %s. Allowed sample: %s",
                    picked_value,
                    matched_attr_name,
                    sample,
                )

    if matched_debug:
        logger.warning("Matched Shopify category attributes: %s", matched_debug)

    metafield_entries = []
    for mf_key, gids in metafield_gids.items():
        if gids:
            metafield_entries.append({
                'key': mf_key,
                'value': json.dumps(gids),
            })

    return metafield_entries


def set_product_category_metafields(product_gid, metafield_entries, shop_domain=None, access_token=None):
    """
    Set category metafields on a product using metafieldsSet mutation.

    metafield_entries: list of dicts with keys: key, value (JSON-encoded list of GIDs)
    """
    if not metafield_entries:
        logger.info("No metafield entries to set")
        return True

    metafields = []
    for entry in metafield_entries:
        metafields.append({
            "ownerId": product_gid,
            "namespace": "shopify",
            "key": entry['key'],
            "type": "list.metaobject_reference",
            "value": entry['value']  # Already JSON-encoded list of GIDs
        })

    mutation = """
    mutation setMetafields($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields {
                key
                value
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    variables = {"metafields": metafields}
    result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)

    if not result:
        logger.error("Failed to set category metafields")
        return False

    if 'errors' in result:
        logger.error(f"GraphQL errors setting metafields: {result['errors']}")
        return False

    mutation_data = result.get('data', {}).get('metafieldsSet', {})
    user_errors = mutation_data.get('userErrors', [])
    if user_errors:
        logger.error(f"User errors setting metafields: {user_errors}")
        return False

    set_fields = mutation_data.get('metafields', [])
    logger.info(f"Successfully set {len(set_fields)} category metafields on product")
    return True


def _legacy_enrich_product_with_category_metafields(product_gid, category_gid, image_path):
    """
    Main orchestrator: enrich a product with category-specific metafield values.

    Called after product creation. Best-effort — logs errors but never raises.

    Args:
        product_gid: Full GraphQL product ID (gid://shopify/Product/xxx)
        category_gid: Taxonomy category GID (gid://shopify/TaxonomyCategory/xxx)
        image_path: Path to product image (str or list — uses first if list)
    """
    try:
        # If image_path is a list, use the first one
        if isinstance(image_path, list):
            image_path = image_path[0] if image_path else None

        if not image_path:
            logger.warning("No image path available for category metafield enrichment")
            return

        logger.info(f"Starting category metafield enrichment for product {product_gid}")

        # Step 1: Get category attributes
        attributes = get_category_attributes(category_gid)
        if not attributes:
            logger.info(f"No choice-list attributes found for category {category_gid}, skipping enrichment")
            return

        # Step 2: For each attribute, determine key/type, enable definition, get values
        # Group attributes by metafield key (e.g., Color and Pattern both map to color-pattern)
        key_to_attributes = {}  # metafield_key -> [attribute dicts]
        for attr in attributes:
            mf_key = _get_metafield_key(attr['name'])
            if mf_key not in key_to_attributes:
                key_to_attributes[mf_key] = []
            key_to_attributes[mf_key].append(attr)

        # Enable metaobject definitions and get available values
        key_to_metaobject_values = {}  # metafield_key -> {name: gid}
        for mf_key in key_to_attributes:
            mo_type = _get_metaobject_type(mf_key)

            # Enable the definition (idempotent)
            if not enable_metaobject_definition(mo_type):
                logger.warning(f"Could not enable metaobject definition {mo_type}, skipping")
                continue

            # Get available values
            values_map = get_metaobject_values(mo_type)
            if not values_map:
                logger.warning(f"No metaobject entries found for {mo_type}, skipping")
                continue

            key_to_metaobject_values[mf_key] = values_map

        if not key_to_metaobject_values:
            logger.info("No metaobject values available for any attribute, skipping enrichment")
            return

        # Step 3: Build attributes_with_options for Gemini
        # Use the metaobject display names as the option list (these are what actually exist in the store)
        attributes_with_options = {}
        for mf_key, values_map in key_to_metaobject_values.items():
            # Find the original attribute names for this key
            attr_names = [a['name'] for a in key_to_attributes[mf_key]]
            available_options = list(values_map.keys())
            for attr_name in attr_names:
                attributes_with_options[attr_name] = available_options

        logger.info(f"Asking Gemini to pick values for {len(attributes_with_options)} attributes")

        # Step 4: Ask Gemini to pick values
        from gemini_utils import pick_category_attribute_values
        gemini_picks = pick_category_attribute_values(image_path, attributes_with_options)

        if not gemini_picks:
            logger.warning("Gemini returned no picks for category attributes")
            return

        logger.info(f"Gemini picked values: {gemini_picks}")

        # Step 5: Map Gemini picks to Metaobject GIDs, grouped by metafield key
        metafield_gids = {}  # metafield_key -> [gid, gid, ...]
        for attr_name, picked_values in gemini_picks.items():
            if not isinstance(picked_values, list):
                picked_values = [picked_values]

            mf_key = _get_metafield_key(attr_name)
            values_map = key_to_metaobject_values.get(mf_key, {})

            if mf_key not in metafield_gids:
                metafield_gids[mf_key] = []

            for value in picked_values:
                gid = values_map.get(value)
                if gid:
                    if gid not in metafield_gids[mf_key]:  # Avoid duplicates
                        metafield_gids[mf_key].append(gid)
                else:
                    logger.warning(f"No metaobject GID found for '{value}' in {mf_key}")

        if not metafield_gids:
            logger.warning("No valid metaobject GIDs found from Gemini picks")
            return

        # Step 6: Build metafield entries and set them
        metafield_entries = []
        for mf_key, gids in metafield_gids.items():
            if gids:
                metafield_entries.append({
                    'key': mf_key,
                    'value': json.dumps(gids)
                })

        success = set_product_category_metafields(product_gid, metafield_entries)
        if success:
            logger.info(f"Set {len(metafield_entries)} category metafields on product {product_gid}")
        else:
            logger.warning(f"Failed to set category metafields on product {product_gid}")

    except Exception as e:
        logger.warning(f"Category metafield enrichment failed (non-fatal): {e}")


def enrich_product_with_category_metafields(
    product_gid,
    category_gid,
    image_path=None,
    category_attribute_picks=None,
    shop_domain=None,
    access_token=None,
    allow_ai_fallback=False,
):
    """
    Enrich a product with category-specific metafield values.

    The normal path uses category_attribute_picks from the main metadata AI
    response. That keeps the listing workflow to one AI image analysis. The
    legacy image-based picker is available only when allow_ai_fallback=True.
    """
    try:
        if isinstance(image_path, list):
            image_path = image_path[0] if image_path else None

        logger.info(f"Starting category metafield enrichment for product {product_gid}")

        if not category_attribute_picks and allow_ai_fallback:
            if not image_path:
                logger.warning("No image path available for legacy category attribute picking")
                return
            context = _prepare_category_attribute_context(
                category_gid,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            if not context:
                logger.info(f"No choice-list attributes found for category {category_gid}, skipping enrichment")
                return
            from gemini_utils import pick_category_attribute_values
            category_attribute_picks = pick_category_attribute_values(image_path, context['attributes_with_options'])

        if not category_attribute_picks:
            logger.info("No category_attribute_picks supplied; skipping category metafields to avoid a second AI image analysis")
            return

        logger.info(f"Using category attribute picks from main metadata response: {category_attribute_picks}")
        metafield_entries = build_category_metafield_entries(
            category_gid,
            category_attribute_picks,
            shop_domain=shop_domain,
            access_token=access_token,
        )
        if not metafield_entries:
            logger.info("No valid category metafield entries could be built from AI picks")
            return

        success = set_product_category_metafields(
            product_gid,
            metafield_entries,
            shop_domain=shop_domain,
            access_token=access_token,
        )
        if success:
            logger.info(f"Set {len(metafield_entries)} category metafields on product {product_gid}")
        else:
            logger.warning(f"Failed to set category metafields on product {product_gid}")

    except Exception as e:
        logger.warning(f"Category metafield enrichment failed (non-fatal): {e}")
