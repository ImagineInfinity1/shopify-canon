import os
import re
import requests
import json
import logging
import base64
import html
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


# Collection URL handles are always read from Shopify. Deriving one from the
# title looks right and is silently wrong whenever a merchant renamed a
# collection after creating it, which produced 404 links inside descriptions.


_GENERIC_COLLECTION_TOKENS = {
    "wall", "art", "arts", "print", "prints", "poster", "posters", "decor",
    "home", "artwork", "artworks", "collection", "collections", "the", "and",
    "for", "with", "new", "all", "best", "sellers", "seller", "shop", "store",
    "arrivals", "sale", "featured", "products", "product", "gift", "gifts",
    "piece", "pieces", "picture", "pictures", "design", "designs", "style",
    "styles", "unframed", "framed", "paper", "canvas",
}


# Concrete colour words (>=4 chars). Mirrors the app-layer set: lets the matcher
# recognise colour-only collection names so a single incidental palette colour
# can't force-assign a whole-artwork colour-scheme collection.
_COLOR_TOKENS = {
    "black", "white", "grey", "gray", "blue", "green", "yellow", "orange",
    "purple", "pink", "brown", "beige", "cream", "gold", "silver", "teal",
    "navy", "maroon", "violet", "indigo", "turquoise", "ivory", "charcoal",
    "bronze", "copper", "coral", "mint", "lavender", "burgundy", "mustard",
    "olive", "peach", "salmon", "magenta", "cyan", "aqua", "sepia", "taupe",
    "khaki", "crimson", "scarlet", "azure", "emerald", "amber", "ruby",
    "sapphire",
}


def _collection_keyword_tokens(*values):
    tokens = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            tokens |= _collection_keyword_tokens(*value)
            continue
        for tok in re.findall(r'[a-z0-9]+', str(value or '').lower()):
            if len(tok) >= 4 and tok not in _GENERIC_COLLECTION_TOKENS:
                tokens.add(tok)
    return tokens


_NEUTRAL_COLOR_TOKENS = {"black", "white", "grey", "gray"}


def _colour_only_collection_ok(name, palette_colors):
    """False if *name* is a colour-only collection whose scheme the palette does
    not satisfy. Mirrors the app-layer check: neutral-only schemes ("Black and
    White") require the palette to be entirely neutral; chromatic colour
    collections ("Blue") require that colour to be present. Concept /
    colour+concept collections always pass."""
    ctoks = _collection_keyword_tokens(name)
    if not ctoks:
        return True
    color_in_name = ctoks & _COLOR_TOKENS
    if not color_in_name or color_in_name != ctoks:
        return True
    if not palette_colors:
        return False
    if color_in_name <= _NEUTRAL_COLOR_TOKENS:
        return palette_colors.issubset(color_in_name | _NEUTRAL_COLOR_TOKENS)
    return bool(color_in_name & palette_colors)


def _auto_match_collections_local(metadata, available_collections, max_n=4):
    """Keyword-overlap match of a product to real store collections. Mirrors the
    app-layer matcher so the publish chokepoint can categorise a product even if
    upstream resolution was skipped. Only adds collections sharing meaningful
    tokens, so nothing irrelevant is force-assigned."""
    if not available_collections:
        return []
    mf = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    product_tokens = _collection_keyword_tokens(
        metadata.get('title'), metadata.get('product_type'), metadata.get('tags'),
        mf.get('theme'), mf.get('subject'), mf.get('art_style'), mf.get('art_movement'),
        mf.get('palette'), mf.get('composition'), mf.get('color'), mf.get('room'),
        mf.get('mood'), metadata.get('custom_label_0'), metadata.get('custom_label_3'),
    )
    if not product_tokens:
        return []
    palette_colors = _collection_keyword_tokens(mf.get('palette'), mf.get('color')) & _COLOR_TOKENS
    scored = []
    for name in available_collections:
        clean = str(name).strip()
        if not clean:
            continue
        overlap = _collection_keyword_tokens(clean) & product_tokens
        if not overlap:
            continue
        if not _colour_only_collection_ok(clean, palette_colors):
            continue
        scored.append((len(overlap), clean))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return [name for _, name in scored[:max_n]]


def _slugify_collection_title(title):
    """Slug Shopify would generate from a title, used only to RECOGNISE links
    that were previously written from a slugified title instead of the real
    handle. Never used to build a new link."""
    return re.sub(r"[^a-z0-9]+", "-", str(title or "").casefold()).strip("-")


def build_collection_link_repair_map(handle_map):
    """{wrong handle: real handle} for links written from a slugified title.

    ``handle_map`` is {collection title: real handle}. A collection titled
    "Animal Prints | Animal Art" living at /collections/animal-prints produced
    /collections/animal-prints-animal-art in older descriptions.
    """
    repair = {}
    for title, handle in (handle_map or {}).items():
        real = str(handle or "").strip()
        if not real:
            continue
        slug = _slugify_collection_title(title)
        if slug and slug != real:
            repair[slug] = real
    return repair


def repair_internal_collection_links(description_html, handle_map, repair_map=None):
    """Point every /collections/ link in a description at a real collection.

    Returns (fixed_html, fixed_links, unresolved_handles). Only the href value
    changes - link text, wording and the rest of the description are untouched.
    A link that cannot be resolved to a real collection is left exactly as it is
    and reported, never rewritten to a guess.
    """
    html_text = description_html or ""
    if not html_text or "/collections/" not in html_text:
        return html_text, [], []

    real_handles = {str(h).strip() for h in (handle_map or {}).values() if str(h or "").strip()}
    repair_map = repair_map if repair_map is not None else build_collection_link_repair_map(handle_map)
    title_to_handle = {
        " ".join(str(t).split()).casefold(): str(h).strip()
        for t, h in (handle_map or {}).items() if str(h or "").strip()
    }
    # Each title half is also a valid way to name the collection in link text.
    segment_to_handle = {}
    for title, handle in (handle_map or {}).items():
        for segment in re.split(r"[|" + chr(0x2013) + chr(0x2014) + r"/>+]| - ", str(title or "")):
            key = " ".join(segment.split()).casefold()
            if len(key) >= 4:
                segment_to_handle.setdefault(key, str(handle).strip())

    fixed = []
    unresolved = []

    def _replace(match):
        handle = match.group("handle")
        text = re.sub(r"<[^>]+>", "", match.group("text") or "")
        text_key = " ".join(html.unescape(text).split()).casefold()
        if handle in real_handles:
            return match.group(0)
        target = (
            repair_map.get(handle)
            or title_to_handle.get(text_key)
            or segment_to_handle.get(text_key)
        )
        if not target:
            unresolved.append(handle)
            return match.group(0)
        fixed.append({"from": handle, "to": target, "text": text.strip()})
        return match.group(0).replace(
            'href="/collections/%s"' % handle,
            'href="/collections/%s"' % target,
            1,
        )

    pattern = re.compile(
        r'<a[^>]*href="/collections/(?P<handle>[^"#?]+)"[^>]*>(?P<text>.*?)</a>',
        re.I | re.S,
    )
    fixed_html = pattern.sub(_replace, html_text)
    return fixed_html, fixed, unresolved


def _append_collection_links(description_html, collections, handle_map=None, max_links=2):
    """Append one internal-linking sentence pointing at the product's collections.

    Runs at publish time using the verified, store-matched collections and the
    real handles Shopify reports, so every link resolves. A collection whose
    handle is unknown is skipped rather than linked to a guessed URL. Idempotent:
    it will not append a second time if the marker sentence is already present.
    """
    if not description_html:
        return description_html
    if "Discover more designs in our" in description_html:
        return description_html
    handle_map = handle_map or {}
    titles = []
    seen = set()
    for title in collections or []:
        clean = " ".join(str(title or "").split())
        handle = str(handle_map.get(clean) or "").strip()
        key = handle.casefold()
        if clean and handle and key not in seen:
            seen.add(key)
            titles.append((clean, handle))
        if len(titles) >= max_links:
            break
    if not titles:
        return description_html
    links = [f'<a href="/collections/{h}">{html.escape(n)}</a>' for n, h in titles]
    if len(links) == 1:
        phrase = f"Discover more designs in our {links[0]} collection."
    else:
        phrase = f"Discover more designs in our {links[0]} and {links[1]} collections."
    return f"{description_html}<p>{phrase}</p>"


def _format_google_color(value):
    """Format a palette/colour value for the Google Shopping ``color`` attribute:
    up to 3 primary colours, '/'-separated (Google's required format). Returns an
    empty string if there is no usable colour."""
    parts = []
    seen = set()
    for chunk in re.split(r'[,/;&]|\band\b', str(value or ''), flags=re.I):
        name = " ".join(chunk.split()).strip(" .")
        if not name:
            continue
        key = name.lower()
        if key in seen or key in {"multi-color", "multicolor", "multi color", "various", "mixed"}:
            continue
        seen.add(key)
        parts.append(name[:1].upper() + name[1:])
        if len(parts) >= 3:
            break
    return "/".join(parts)

# ---------------------------------------------------------------------------
# Shopify API rate-limit semaphore
# Limits concurrent Shopify API calls across all threads to prevent 429 errors
# when multiple listings are being created simultaneously.
# ---------------------------------------------------------------------------
_shopify_api_semaphore = threading.Semaphore(2)

# ---------------------------------------------------------------------------
# Per-instance TTL cache for store configuration that is identical across every
# listing in a batch (collections, publication/channel IDs, market catalogs).
# Re-fetching these on every listing was a big chunk of the per-listing Shopify
# work; caching them for a few minutes makes the create path much leaner (closer
# to the WooCommerce sibling) without changing the resulting product. A short TTL
# means newly-added collections/channels still appear within minutes.
# ---------------------------------------------------------------------------
_store_config_cache = {}
_store_config_cache_lock = threading.Lock()
STORE_CONFIG_TTL_SECONDS = 600  # 10 minutes

SEO_PRODUCT_METAFIELD_DEFINITIONS = {
    "subject": ("Subject", "Visible product subject for filtering and SEO QA"),
    "room": ("Room", "Best-fit room or use case"),
    "mood": ("Mood", "Primary visual mood"),
    "palette": ("Palette", "Image-grounded color palette"),
    "audience": ("Audience", "Likely buyer or audience"),
    "occasion": ("Occasion", "Gift or use occasion"),
    "season": ("Season", "Seasonal relevance"),
    "composition": ("Composition", "Visible layout or composition"),
    "display_suggestion": ("Display suggestion", "Suggested placement or styling use"),
    "material": ("Material", "Product/display material"),
    "art_movement": ("Art movement", "Visual art movement"),
    "art_style": ("Art style", "Visual art style"),
    "artwork_authenticity": ("Artwork authenticity", "Original/reproduction status"),
    "orientation": ("Orientation", "Image/product orientation"),
}

CONTENT_PRODUCT_METAFIELD_DEFINITIONS = {
    "short_description": (
        "Short description",
        "Product-specific summary displayed above the price",
        "multi_line_text_field",
    ),
    "product_faq": (
        "Product FAQ",
        "Recurring product questions and factual answers",
        "json",
    ),
}

SEARCH_BOOST_GENERIC_TERMS = {
    "art", "wall art", "poster", "posters", "print", "prints", "home decor",
    "decor", "paper", "unframed", "new", "general", "reproduction",
    "reproduction print", "contemporary", "modern", "year-round", "year round",
    "high quality", "high-quality", "museum quality", "premium quality",
    "minimalist", "illustrative", "graphic design",
    "travel poster", "city wall art", "destination print", "modern art",
    "home decor", "wall decor",
}

BROAD_DISCOVERY_TERMS = SEARCH_BOOST_GENERIC_TERMS | {
    "travel", "destination", "city", "city poster", "country", "map",
    "nature", "nature wall art", "landscape", "landscape print",
    "sports", "sport", "athletic", "fitness", "room", "living room",
    "bedroom", "office", "study", "kitchen", "gallery wall",
}

DISCOVERY_ANCHOR_NOISE_WORDS = {
    "wall", "art", "poster", "posters", "print", "prints", "decor", "decoration",
    "home", "room", "style", "image", "picture", "design", "illustration",
    "modern", "contemporary", "vintage", "retro", "traditional", "classic",
    "illustrative", "graphic", "minimalist", "abstract", "typography",
    "travel", "destination", "city", "country", "landmark", "location",
    "new", "quality", "premium", "unframed", "paper", "portrait", "landscape",
    "blue", "green", "red", "black", "white", "cream", "beige", "orange",
    "yellow", "pink", "brown", "gold", "grey", "gray", "multi", "color",
}


def _discovery_concrete_words(value):
    return [
        word
        for word in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(word) > 2 and word not in DISCOVERY_ANCHOR_NOISE_WORDS
    ]


def _is_concrete_discovery_term(value):
    term = " ".join(str(value or "").lower().split()).strip(" ,.;:")
    return bool(term and term not in BROAD_DISCOVERY_TERMS and _discovery_concrete_words(term))


def _sanitize_search_boost_terms(terms, max_terms=8):
    """Return Shopify Search & Discovery query terms in a conservative shape."""
    if isinstance(terms, str):
        try:
            parsed = json.loads(terms)
            terms = parsed if isinstance(parsed, list) else terms
        except json.JSONDecodeError:
            pass
    if isinstance(terms, str):
        iterable = re.split(r"[,|;/]+", terms)
    elif isinstance(terms, (list, tuple, set)):
        iterable = terms
    else:
        iterable = []

    cleaned = []
    seen = set()
    for raw in iterable:
        for part in re.split(r"[,|;/]+", str(raw or "")):
            term = part.lower()
            term = term.replace("&", " and ")
            term = re.sub(r"[^a-z0-9&' -]+", " ", term)
            term = term.replace("&", " and ")
            term = re.sub(r"\s+", " ", term).strip(" -'")
            if not term or term in SEARCH_BOOST_GENERIC_TERMS:
                continue
            if len(term) < 3 or len(term) > 48:
                continue
            words = term.split()
            if len(words) > 6:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(term)
            if len(cleaned) >= max_terms:
                return cleaned
    return cleaned


def _product_search_phrase_candidates(metadata, max_terms=10):
    metadata = metadata or {}
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}

    def clean(value):
        text = re.sub(r"\s+", " ", str(value or "").lower()).strip(" ,.;:|-")
        text = text.replace("&", " and ")
        text = re.sub(r"[^a-z0-9' -]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def parts(value):
        return [clean(part) for part in re.split(r"[,|;/]+|\s+and\s+", str(value or ""), flags=re.I) if clean(part)]

    def with_suffix(value, suffix):
        value = clean(value)
        suffix = clean(suffix)
        if not value:
            return ""
        if value == suffix or value.endswith(f" {suffix}"):
            return value
        if suffix == "wall art" and value.endswith((" print", " poster", " decor")):
            return value
        return f"{value} {suffix}".strip()

    title_parts = parts(str(metadata.get("title") or "").replace("|", ","))
    title = title_parts[0] if title_parts else ""
    subject_parts = parts(metafields.get("subject") or metadata.get("custom_label_3"))
    theme_parts = parts(metafields.get("theme"))
    style = clean(metafields.get("art_style") or metadata.get("custom_label_0"))
    candidates = []

    def compact_subject(value):
        words = [
            word
            for word in re.findall(r"[a-z0-9'-]+", clean(value))
            if word not in {
                "and", "with", "the", "a", "an", "of", "in", "on", "for",
                "wall", "art", "poster", "print", "decor", "decorative",
            }
        ]
        return " ".join(words[:3]).strip()

    def combine_phrase(prefix, core, suffix):
        prefix = clean(prefix)
        core = clean(core)
        if not prefix or not core:
            return ""
        prefix_words = prefix.split()
        core_words = core.split()
        while prefix_words and core_words and prefix_words[-1] == core_words[0]:
            core_words = core_words[1:]
        phrase = " ".join(prefix_words + core_words).strip()
        return with_suffix(phrase, suffix)

    if title:
        candidates.extend([title, with_suffix(title, "print"), with_suffix(title, "wall art")])
    for title_part in title_parts[1:4]:
        candidates.extend([title_part, with_suffix(title_part, "print"), with_suffix(title_part, "wall art")])
    for subject in subject_parts[:3]:
        subject_core = compact_subject(subject)
        candidates.extend([
            with_suffix(subject, "wall art"),
            with_suffix(subject, "print"),
            with_suffix(subject, "poster"),
            subject,
        ])
        if subject_core and subject_core != subject:
            candidates.extend([
                with_suffix(subject_core, "wall art"),
                with_suffix(subject_core, "print"),
            ])
        for theme in theme_parts[:2]:
            theme_clean = clean(theme)
            if theme_clean and subject_core:
                candidates.extend([
                    combine_phrase(theme_clean, subject_core, "print"),
                    combine_phrase(theme_clean, subject_core, "wall art"),
                ])
        if style and subject_core and style not in SEARCH_BOOST_GENERIC_TERMS:
            candidates.append(combine_phrase(style, subject_core, "print"))
    return _sanitize_search_boost_terms(candidates, max_terms=max_terms)


def _store_config_cached(key, ttl, loader):
    """Return cached value for *key*, or call *loader()* and cache it.

    The cached value is only stored when it is truthy, so a failed lookup
    (empty list / None) is not cached and will be retried on the next listing.
    """
    now = time.monotonic()
    with _store_config_cache_lock:
        entry = _store_config_cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]
    value = loader()
    if value:
        with _store_config_cache_lock:
            _store_config_cache[key] = (now, value)
    return value


def _retry_with_backoff(func, max_retries=3, base_delay=1.0, context="API call"):
    """Execute *func* with retry + exponential backoff.

    *func* must be a callable that returns a `requests.Response` object.
    Retries on:
      - HTTP 429 (Too Many Requests) – honours Retry-After header
      - HTTP 5xx (server errors)
      - Network / timeout exceptions

    Returns the successful Response, or None after all retries exhausted.
    """
    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            with _shopify_api_semaphore:
                response = func()

            # --- Success ---
            if response.status_code in (200, 201):
                return response

            # --- Rate limited ---
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", base_delay * attempt))
                logger.warning(
                    f"⏳ {context}: 429 Too Many Requests (attempt {attempt}/{max_retries}). "
                    f"Retrying after {retry_after}s…"
                )
                time.sleep(retry_after)
                continue

            # --- Server error ---
            if response.status_code >= 500:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"⏳ {context}: HTTP {response.status_code} (attempt {attempt}/{max_retries}). "
                    f"Retrying after {delay}s…"
                )
                time.sleep(delay)
                continue

            # --- Other non-success (4xx etc.) – don't retry ---
            return response

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exception = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                f"⏳ {context}: {type(exc).__name__} (attempt {attempt}/{max_retries}). "
                f"Retrying after {delay}s…"
            )
            time.sleep(delay)

    # All retries exhausted
    if last_exception:
        logger.error(f"❌ {context}: All {max_retries} retries exhausted. Last error: {last_exception}")
    else:
        logger.error(f"❌ {context}: All {max_retries} retries exhausted.")
    return None


def format_description_html(description):
    """Convert a product description into clean Shopify-compatible HTML.

    If the description already contains <p> tags, strip any stray bullet/list
    markup and return it.  Otherwise, split on double-newlines to create
    paragraphs wrapped in <p> tags.  Plain \\n within a paragraph become <br>.
    """
    if not description or not description.strip():
        return "<p></p>"

    text = description.strip()

    # Strip markdown-style bullet characters (* - •) at the start of lines
    text = re.sub(r'(?m)^[\s]*[*\-\u2022]\s+', '', text)

    # If AI already returned HTML <p> tags, trust that structure
    if '<p>' in text.lower():
        return text

    # Otherwise build paragraphs from double-newline splits
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if not paragraphs:
        return f"<p>{text}</p>"

    html_parts = []
    for p in paragraphs:
        # Convert single newlines inside a paragraph to <br>
        p = p.replace('\n', '<br>')
        html_parts.append(f"<p>{p}</p>")
    return ''.join(html_parts)


# ---------------------------------------------------------------------------
# Multi-tenant helpers: resolve shop domain + token from current user's shop
# ---------------------------------------------------------------------------
def _resolve_credentials(shop_domain=None, access_token=None):
    """Resolve Shopify credentials.

    If explicit values are passed, use them.  Otherwise, try to get them from
    the current user's active Shop record via shop_helpers.
    Returns (domain, token) – both strings or raises ValueError.
    """
    if shop_domain and access_token:
        return shop_domain, access_token

    try:
        from shop_helpers import get_shop_credentials
        sd, at = get_shop_credentials()
        if sd and at:
            return sd, at
    except Exception:
        pass

    raise ValueError(
        "No Shopify store connected. Please connect your store via Settings > Shopify."
    )


DEFAULT_ADMIN_API_VERSION = os.environ.get("SHOPIFY_ADMIN_API_VERSION", "2025-07")
ANALYTICS_ADMIN_API_VERSION = os.environ.get("SHOPIFY_ANALYTICS_API_VERSION", "2025-10")


def _graphql_url(shop_domain, api_version=None):
    version = str(api_version or DEFAULT_ADMIN_API_VERSION).strip()
    return f"https://{shop_domain}/admin/api/{version}/graphql.json"


def _trim_text(value, max_chars):
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    # Never cut mid-word: trim at the last word boundary within the limit. If
    # there is no space at all, keep the whole token rather than mangling it.
    cutoff = text.rfind(" ", 0, max_chars)
    if cutoff <= 0:
        return text
    return text[:cutoff].rstrip(" |,.;:-")


def get_graphql_headers(access_token=None, shop_domain=None):
    """Get headers for GraphQL requests.

    If access_token is not provided, resolves from current user's shop.
    """
    if not access_token:
        _, access_token = _resolve_credentials(shop_domain, access_token)
    return {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': access_token
    }


def execute_graphql_query(query, variables=None, shop_domain=None, access_token=None,
                          api_version=None, raise_errors=False):
    """Execute a GraphQL query against Shopify.

    shop_domain / access_token are optional — auto-resolved from current shop.
    """
    try:
        domain, token = _resolve_credentials(shop_domain, access_token)
        url = _graphql_url(domain, api_version=api_version)

        payload = {'query': query}
        if variables:
            payload['variables'] = variables
        
        # Log query type (extract mutation/query name) at INFO level
        query_name = 'unknown'
        for line in query.strip().split('\n'):
            line = line.strip()
            if line.startswith('mutation ') or line.startswith('query '):
                query_name = line.split('(')[0].split('{')[0].strip()
                break
        logger.info(f"GraphQL: {query_name} -> {url}")
        
        headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': token
        }

        response = _retry_with_backoff(
            lambda: requests.post(url, headers=headers, json=payload, timeout=30),
            max_retries=3,
            base_delay=1.0,
            context=f"GraphQL {query_name}",
        )

        if response is None:
            logger.error(f"GraphQL request failed after all retries for {query_name}")
            if raise_errors:
                raise RuntimeError(f"Shopify GraphQL request failed after retries for {query_name}")
            return None

        if response.status_code != 200:
            logger.error(f"GraphQL request failed: {response.status_code} for {query_name}")
            try:
                logger.error(f"Response: {response.text[:500]}")
            except:
                pass
            if raise_errors:
                raise RuntimeError(f"Shopify GraphQL HTTP {response.status_code}: {response.text[:500]}")
            return None

        result = response.json()

        # Check for GraphQL-level errors (different from userErrors)
        if 'errors' in result:
            logger.error(f"GraphQL errors in {query_name}:")
            for error in result['errors']:
                logger.error(f"  {error.get('message', 'Unknown error')}")
            if raise_errors:
                messages = "; ".join(error.get('message', 'Unknown error') for error in result['errors'])
                raise RuntimeError(f"Shopify GraphQL error: {messages}")
            return None

        logger.info(f"GraphQL: {query_name} -> OK")
        return result
    except Exception as e:
        logger.error(f"Unexpected error in GraphQL request: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(traceback.format_exc())
        if raise_errors:
            raise
        return None

def get_collections(shop_domain=None, access_token=None):
    """Get all collections from store using GraphQL"""
    try:
        domain, token = _resolve_credentials(shop_domain, access_token)
    except ValueError:
        logger.error("No Shopify credentials available - cannot fetch collections. Returning empty list.")
        return []
    
    # Reuse the cached, paginated title -> GID map. One fetch per batch instead
    # of one per listing, and stores with 250+ collections are fully covered.
    collection_map = _get_collection_title_id_map(shop_domain=domain, access_token=token)
    if not collection_map:
        logger.warning("Failed to fetch collections from Shopify - check your access token and connection")
        return []

    collections = list(collection_map.keys())
    logger.info(f"Retrieved {len(collections)} collections via GraphQL")
    return collections


def rank_collection_catalogue_item(collection):
    """Attach a transparent, zero-AI merchandising score to a collection page."""
    title = " ".join(str(collection.get('title') or '').split())
    handle = str(collection.get('handle') or '').strip()
    description = " ".join(str(collection.get('description') or '').split())
    seo_title = " ".join(str(collection.get('seo_title') or '').split())
    seo_description = " ".join(str(collection.get('seo_description') or '').split())
    image_url = str(collection.get('image_url') or '').strip()
    image_alt = " ".join(str(collection.get('image_alt') or '').split())
    product_count = int(collection.get('product_count') or 0)
    score = 0
    issues = []

    if title:
        score += 8
        if 10 <= len(title) <= 70:
            score += 7
        else:
            issues.append('Title length should be 10-70 characters')
    else:
        issues.append('Missing collection title')

    description_words = len(description.split())
    if description:
        score += 10
        if description_words >= 80:
            score += 15
        else:
            issues.append('Collection description is under 80 words')
    else:
        issues.append('Missing collection description')

    if seo_title:
        score += 8
        if 20 <= len(seo_title) <= 70:
            score += 7
        else:
            issues.append('SEO title length should be 20-70 characters')
    else:
        issues.append('Missing custom SEO title')

    if seo_description:
        score += 8
        if 90 <= len(seo_description) <= 160:
            score += 7
        else:
            issues.append('SEO description length should be 90-160 characters')
    else:
        issues.append('Missing SEO description')

    if image_url:
        score += 10
    else:
        issues.append('Missing collection image')
    if image_alt:
        score += 10
    else:
        issues.append('Missing collection image alt text')
    if handle:
        score += 5
    else:
        issues.append('Missing collection handle')
    if product_count > 0:
        score += 5
    else:
        issues.append('Collection has no products')

    collection['quality_score'] = max(0, min(100, score))
    collection['quality_band'] = 'good' if score >= 80 else ('review' if score >= 55 else 'poor')
    collection['quality_issues'] = issues
    collection['recommended_action'] = issues[0] if issues else 'No immediate content issue'
    collection['description_word_count'] = description_words
    return collection


_CONNECTION_TOPUP_QUERY = """
query connectionTopUp($id: ID!, $after: String) {
  node(id: $id) {
    ... on %(typename)s {
      %(field)s(first: 100, after: $after%(extra)s) {
        pageInfo { hasNextPage endCursor }
        nodes { %(fields)s }
      }
    }
  }
}
"""


def fetch_remaining_connection_nodes(gid, typename, field, node_fields, after,
                                     extra_args='', shop_domain=None, access_token=None,
                                     max_pages=20):
    """Return every node after `after` for one connection on one object.

    Shopify only returns the first page of a nested connection (metafields,
    publications). Anything past that page used to be invisible, so a product
    with 30 metafields looked like it had 20. This walks the rest of the pages
    and returns what the first page missed.
    """
    if not gid or not after:
        return []
    query = _CONNECTION_TOPUP_QUERY % {
        'typename': typename,
        'field': field,
        'extra': extra_args,
        'fields': node_fields,
    }
    collected = []
    cursor = after
    for _ in range(max_pages):
        result = execute_graphql_query(
            query, {'id': gid, 'after': cursor},
            shop_domain=shop_domain, access_token=access_token,
        )
        connection = (((result or {}).get('data') or {}).get('node') or {}).get(field) or {}
        nodes = connection.get('nodes') or []
        collected.extend(nodes)
        page_info = connection.get('pageInfo') or {}
        if not page_info.get('hasNextPage') or not nodes:
            break
        cursor = page_info.get('endCursor')
        if not cursor:
            break
    else:
        logger.warning('Connection %s.%s on %s still had more pages after %d requests',
                       typename, field, gid, max_pages)
    return collected


def list_collection_catalogue(shop_domain=None, access_token=None):
    """Fetch every Shopify collection page for cached, deterministic auditing."""
    domain, token = _resolve_credentials(shop_domain, access_token)
    query = """
    query listCollectionCatalogue($first: Int!, $after: String) {
      collections(first: $first, after: $after, sortKey: UPDATED_AT, reverse: true) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          title
          handle
          description
          descriptionHtml
          updatedAt
          sortOrder
          templateSuffix
          productsCount { count precision }
          availablePublicationsCount { count precision }
          resourcePublicationsCount(onlyPublished: false) { count precision }
          resourcePublicationsV2(first: 20, onlyPublished: false) {
            pageInfo { hasNextPage endCursor }
            nodes {
              isPublished
              publishDate
              publication { id name autoPublish }
            }
          }
          unpublishedPublications(first: 20) {
            pageInfo { hasNextPage endCursor }
            nodes { id name autoPublish }
          }
          ruleSet {
            appliedDisjunctively
            rules { column relation condition }
          }
          seo { title description }
          image { id url altText width height }
          metafields(first: 20) {
            pageInfo { hasNextPage endCursor }
            nodes { id namespace key type value }
          }
        }
      }
    }
    """
    after = None
    collections = []
    while True:
        result = None
        for attempt in range(1, 7):
            try:
                result = execute_graphql_query(
                    query,
                    variables={'first': 10, 'after': after},
                    shop_domain=domain,
                    access_token=token,
                    raise_errors=True,
                )
                break
            except RuntimeError as exc:
                rate_limited = 'throttled' in str(exc).lower() or 'rate limit' in str(exc).lower()
                if not rate_limited or attempt >= 6:
                    raise
                delay = min(20, 2 ** attempt)
                logger.warning("Collection audit throttled; retrying in %ss", delay)
                time.sleep(delay)
        connection = ((result or {}).get('data') or {}).get('collections') or {}
        for node in connection.get('nodes') or []:
            count_data = node.get('productsCount') or {}
            available_publications = node.get('availablePublicationsCount') or {}
            publication_count = node.get('resourcePublicationsCount') or {}
            image = node.get('image') or {}
            seo = node.get('seo') or {}
            rule_set = node.get('ruleSet') or None
            publication_connection = node.get('resourcePublicationsV2') or {}
            unpublished_connection = node.get('unpublishedPublications') or {}
            publications = []
            for publication_node in publication_connection.get('nodes') or []:
                publication = publication_node.get('publication') or {}
                publications.append({
                    'id': publication.get('id'),
                    'name': publication.get('name') or '',
                    'auto_publish': bool(publication.get('autoPublish')),
                    'is_published': bool(publication_node.get('isPublished')),
                    'publish_date': publication_node.get('publishDate'),
                })
            unpublished_publications = [{
                'id': publication.get('id'),
                'name': publication.get('name') or '',
                'auto_publish': bool(publication.get('autoPublish')),
            } for publication in (unpublished_connection.get('nodes') or [])]
            # Follow the rest of any connection Shopify truncated, so a
            # collection with more than 20 metafields or channels is read in full.
            collection_gid = node.get('id')
            publication_page = publication_connection.get('pageInfo') or {}
            if publication_page.get('hasNextPage'):
                for publication_node in fetch_remaining_connection_nodes(
                    collection_gid, 'Collection', 'resourcePublicationsV2',
                    'isPublished publishDate publication { id name autoPublish }',
                    publication_page.get('endCursor'), extra_args=', onlyPublished: false',
                    shop_domain=domain, access_token=token,
                ):
                    publication = publication_node.get('publication') or {}
                    publications.append({
                        'id': publication.get('id'),
                        'name': publication.get('name') or '',
                        'auto_publish': bool(publication.get('autoPublish')),
                        'is_published': bool(publication_node.get('isPublished')),
                        'publish_date': publication_node.get('publishDate'),
                    })
            unpublished_page = unpublished_connection.get('pageInfo') or {}
            if unpublished_page.get('hasNextPage'):
                unpublished_publications.extend({
                    'id': publication.get('id'),
                    'name': publication.get('name') or '',
                    'auto_publish': bool(publication.get('autoPublish')),
                } for publication in fetch_remaining_connection_nodes(
                    collection_gid, 'Collection', 'unpublishedPublications',
                    'id name autoPublish', unpublished_page.get('endCursor'),
                    shop_domain=domain, access_token=token,
                ))

            metafield_nodes = list((node.get('metafields') or {}).get('nodes') or [])
            metafield_page = (node.get('metafields') or {}).get('pageInfo') or {}
            if metafield_page.get('hasNextPage'):
                metafield_nodes.extend(fetch_remaining_connection_nodes(
                    collection_gid, 'Collection', 'metafields',
                    'id namespace key type value', metafield_page.get('endCursor'),
                    shop_domain=domain, access_token=token,
                ))
            metafields = {}
            for metafield in metafield_nodes:
                key = f"{metafield.get('namespace')}.{metafield.get('key')}"
                metafields[key] = {
                    'id': metafield.get('id'),
                    'namespace': metafield.get('namespace'),
                    'key': metafield.get('key'),
                    'type': metafield.get('type'),
                    'value': metafield.get('value'),
                }
            numeric_id = str(node.get('id') or '').rsplit('/', 1)[-1]
            item = {
                'id': node.get('id'),
                'numeric_id': numeric_id,
                'title': node.get('title') or '',
                'handle': node.get('handle') or '',
                'description': node.get('description') or '',
                'description_html': node.get('descriptionHtml') or '',
                'seo_title': seo.get('title') or '',
                'seo_description': seo.get('description') or '',
                'image_url': image.get('url') or '',
                'image_id': image.get('id') or '',
                'image_alt': image.get('altText') or '',
                'image_width': image.get('width'),
                'image_height': image.get('height'),
                'product_count': int(count_data.get('count') or 0),
                'product_count_precision': count_data.get('precision'),
                'available_publication_count': int(available_publications.get('count') or 0),
                'publication_count': int(publication_count.get('count') or 0),
                'publications': publications,
                'publications_truncated': False,
                'unpublished_publications': unpublished_publications,
                'unpublished_publications_truncated': False,
                'rule_set': rule_set,
                'collection_type': 'smart' if rule_set else 'custom',
                'sort_order': node.get('sortOrder') or '',
                'template_suffix': node.get('templateSuffix') or '',
                'updated_at': node.get('updatedAt') or '',
                'metafields': metafields,
                'metafields_truncated': bool(((node.get('metafields') or {}).get('pageInfo') or {}).get('hasNextPage')),
                'admin_url': f"https://admin.shopify.com/store/{domain.split('.')[0]}/collections/{numeric_id}",
                'shop_url': f"https://{domain}/collections/{node.get('handle') or ''}",
            }
            collections.append(rank_collection_catalogue_item(item))
        page_info = connection.get('pageInfo') or {}
        if not page_info.get('hasNextPage'):
            break
        throttle = (((result or {}).get('extensions') or {}).get('cost') or {}).get('throttleStatus') or {}
        available = float(throttle.get('currentlyAvailable') or 0)
        restore_rate = float(throttle.get('restoreRate') or 0)
        requested = float((((result or {}).get('extensions') or {}).get('cost') or {}).get('requestedQueryCost') or 0)
        if restore_rate > 0 and requested > available:
            time.sleep(min(20, ((requested - available) / restore_rate) + 0.25))
        after = page_info.get('endCursor')
        if not after:
            break
    logger.info("Retrieved %s collection pages for catalogue audit", len(collections))
    return collections


def create_smart_collection(collection, shop_domain=None, access_token=None):
    """Create one automated collection driven by tag rules.

    `collection` needs a title and a list of tags. Everything else - the
    description, SEO text, handle and image - is optional and only sent when
    supplied, so a partly filled proposal never overwrites a field with blank.
    """
    title = str((collection or {}).get('title') or '').strip()
    tags = [str(tag).strip() for tag in (collection or {}).get('tags') or [] if str(tag).strip()]
    if not title:
        return {'success': False, 'error': 'A collection needs a title.'}
    if not tags:
        return {'success': False, 'error': 'A collection with no tag rule would be empty; refusing to create it.'}

    collection_input = {
        'title': title,
        'ruleSet': {
            # Any one of the tags is enough to belong, which is how a group of
            # related tags becomes a single page.
            'appliedDisjunctively': True,
            'rules': [{'column': 'TAG', 'relation': 'EQUALS', 'condition': tag} for tag in tags],
        },
    }
    if collection.get('handle'):
        collection_input['handle'] = str(collection['handle']).strip()
    if collection.get('description_html'):
        collection_input['descriptionHtml'] = collection['description_html']
    seo = {}
    if collection.get('seo_title'):
        seo['title'] = collection['seo_title']
    if collection.get('seo_description'):
        seo['description'] = collection['seo_description']
    if seo:
        collection_input['seo'] = seo
    if collection.get('image_url'):
        collection_input['image'] = {
            'src': collection['image_url'],
            'altText': collection.get('image_alt') or title,
        }

    mutation = """
    mutation createSmartCollection($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection {
          id title handle
          ruleSet { appliedDisjunctively rules { column relation condition } }
        }
        userErrors { field message }
      }
    }
    """
    result = execute_graphql_query(
        mutation, {'input': collection_input}, shop_domain=shop_domain,
        access_token=access_token, raise_errors=True,
    )
    payload = ((result or {}).get('data') or {}).get('collectionCreate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        return {'success': False, 'error': '; '.join(error.get('message', 'Unknown error') for error in errors)}
    created = payload.get('collection') or {}
    if not created.get('id'):
        return {'success': False, 'error': 'Shopify did not return a collection.'}
    return {'success': True, 'collection': created}


def publish_collection_to_online_store(collection_id, publication_id, shop_domain=None, access_token=None):
    """Make a collection visible on the storefront."""
    if not collection_id or not publication_id:
        return {'success': False, 'error': 'Missing collection or publication id'}
    mutation = """
    mutation publishCollection($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    result = execute_graphql_query(
        mutation, {'id': collection_id, 'input': [{'publicationId': publication_id}]},
        shop_domain=shop_domain, access_token=access_token, raise_errors=True,
    )
    payload = ((result or {}).get('data') or {}).get('publishablePublish') or {}
    errors = payload.get('userErrors') or []
    if errors:
        return {'success': False, 'error': '; '.join(error.get('message', 'Unknown error') for error in errors)}
    return {'success': True}


def get_online_store_publication_id(shop_domain=None, access_token=None):
    """Return the Online Store sales channel id, or an empty string."""
    query = """
    query onlineStorePublication {
      publications(first: 25) { nodes { id name } }
    }
    """
    result = execute_graphql_query(
        query, {}, shop_domain=shop_domain, access_token=access_token, raise_errors=True)
    nodes = (((result or {}).get('data') or {}).get('publications') or {}).get('nodes') or []
    for node in nodes:
        if str(node.get('name') or '').strip().lower() == 'online store':
            return node.get('id') or ''
    return ''


def update_collection_rules(collection_id, rule_set, shop_domain=None, access_token=None):
    """Rewrite the conditions of an automated collection.

    Renaming a tag would quietly empty any smart collection built on the old
    tag, so the rule has to move with it. The whole rule set is sent because
    Shopify replaces it wholesale.
    """
    if not collection_id:
        return {'success': False, 'error': 'Missing Shopify collection id'}
    rules = []
    for rule in (rule_set or {}).get('rules') or []:
        column = str((rule or {}).get('column') or '').strip()
        relation = str((rule or {}).get('relation') or '').strip()
        condition = (rule or {}).get('condition')
        if not column or not relation or condition is None:
            return {'success': False, 'error': 'Incomplete collection rule; refusing to write a partial rule set.'}
        rules.append({'column': column, 'relation': relation, 'condition': str(condition)})
    if not rules:
        return {'success': False, 'error': 'No rules supplied; refusing to empty an automated collection.'}

    mutation = """
    mutation updateCollectionRules($input: CollectionInput!) {
      collectionUpdate(input: $input) {
        collection { id title ruleSet { appliedDisjunctively rules { column relation condition } } }
        userErrors { field message }
      }
    }
    """
    variables = {'input': {
        'id': collection_id,
        'ruleSet': {
            'appliedDisjunctively': bool((rule_set or {}).get('appliedDisjunctively')),
            'rules': rules,
        },
    }}
    result = execute_graphql_query(
        mutation, variables, shop_domain=shop_domain,
        access_token=access_token, raise_errors=True,
    )
    payload = ((result or {}).get('data') or {}).get('collectionUpdate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        return {'success': False, 'error': '; '.join(error.get('message', 'Unknown error') for error in errors)}
    return {'success': True, 'collection': payload.get('collection') or {}}


def get_collection_updated_at(collection_id, shop_domain=None, access_token=None):
    query = """
    query collectionEditVersion($id: ID!) {
      collection(id: $id) { id updatedAt }
    }
    """
    result = execute_graphql_query(
        query, {'id': collection_id}, shop_domain=shop_domain,
        access_token=access_token, raise_errors=True,
    )
    collection = ((result or {}).get('data') or {}).get('collection') or {}
    return collection.get('updatedAt')


def update_collection_metadata(collection_id, changes, shop_domain=None, access_token=None):
    """Update approved collection page fields; callers own review and confirmation."""
    if not collection_id:
        return {'success': False, 'error': 'Missing Shopify collection id'}
    changes = changes or {}
    collection_input = {'id': collection_id}
    direct_fields = {
        'title': 'title',
        'handle': 'handle',
        'description_html': 'descriptionHtml',
        'sort_order': 'sortOrder',
        'template_suffix': 'templateSuffix',
    }
    for source_key, api_key in direct_fields.items():
        if source_key in changes:
            collection_input[api_key] = changes.get(source_key)
    if 'handle' in changes:
        collection_input['redirectNewHandle'] = True
    if 'seo_title' in changes or 'seo_description' in changes:
        collection_input['seo'] = {}
        if 'seo_title' in changes:
            collection_input['seo']['title'] = changes.get('seo_title') or ''
        if 'seo_description' in changes:
            collection_input['seo']['description'] = changes.get('seo_description') or ''
    if 'image_alt' in changes and changes.get('image_id'):
        collection_input['image'] = {
            'id': changes.get('image_id'),
            'altText': changes.get('image_alt') or '',
        }
    mutation = """
    mutation updateCollectionMetadata($input: CollectionInput!) {
      collectionUpdate(input: $input) {
        collection {
          id title handle descriptionHtml updatedAt sortOrder templateSuffix
          seo { title description }
          image { id url altText }
        }
        userErrors { field message }
      }
    }
    """
    result = execute_graphql_query(
        mutation, {'input': collection_input}, shop_domain=shop_domain,
        access_token=access_token, raise_errors=True,
    )
    payload = ((result or {}).get('data') or {}).get('collectionUpdate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        return {'success': False, 'error': '; '.join(error.get('message', 'Unknown error') for error in errors)}

    metafields = []
    for metafield in changes.get('metafields') or []:
        if not isinstance(metafield, dict) or metafield.get('value') is None:
            continue
        item = {
            'ownerId': collection_id,
            'namespace': metafield.get('namespace'),
            'key': metafield.get('key'),
            'type': metafield.get('type') or 'single_line_text_field',
            'value': str(metafield.get('value')),
        }
        if item['namespace'] and item['key']:
            metafields.append(item)
    if metafields:
        metafield_mutation = """
        mutation setCollectionMetafields($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id namespace key type value }
            userErrors { field message code }
          }
        }
        """
        metafield_result = execute_graphql_query(
            metafield_mutation, {'metafields': metafields}, shop_domain=shop_domain,
            access_token=access_token, raise_errors=True,
        )
        metafield_payload = ((metafield_result or {}).get('data') or {}).get('metafieldsSet') or {}
        metafield_errors = metafield_payload.get('userErrors') or []
        if metafield_errors:
            return {
                'success': False,
                'partial': True,
                'collection': payload.get('collection'),
                'error': 'Collection fields updated, but metafields failed: ' + '; '.join(
                    error.get('message', 'Unknown error') for error in metafield_errors
                ),
            }
    return {'success': True, 'collection': payload.get('collection')}


def list_products_for_seo_enhancement(
    limit=250,
    query_filter="",
    shop_domain=None,
    access_token=None,
    after_cursor=None,
    sort_key="UPDATED_AT",
    reverse=True,
    include_page_info=False,
):
    """Fetch live Shopify products for the bulk SEO enhancement workflow.

    This uses cursor pagination so the UI can pull hundreds or thousands of
    products without requiring a Shopify CSV export first.
    """
    try:
        limit = max(1, min(int(limit or 250), 10000))
    except (TypeError, ValueError):
        limit = 250

    page_size = min(250, limit)
    products = []
    after = after_cursor or None
    allowed_sort_keys = {
        "CREATED_AT", "UPDATED_AT", "TITLE", "ID", "PRODUCT_TYPE", "VENDOR", "INVENTORY_TOTAL", "BEST_SELLING"
    }
    sort_key = str(sort_key or "UPDATED_AT").upper()
    if sort_key not in allowed_sort_keys:
        sort_key = "UPDATED_AT"
    reverse = bool(reverse)
    last_page_info = {}

    query = """
    query listProductsForSeoEnhancement($first: Int!, $after: String, $query: String, $sortKey: ProductSortKeys, $reverse: Boolean) {
      products(first: $first, after: $after, query: $query, sortKey: $sortKey, reverse: $reverse) {
        pageInfo {
          hasNextPage
          endCursor
        }
        edges {
          node {
            id
            title
            handle
            createdAt
            updatedAt
            descriptionHtml
            vendor
            productType
            tags
            status
            category {
              id
              fullName
            }
            collections(first: 50) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                title
              }
            }
            seo {
              title
              description
            }
            media(first: 10) {
              pageInfo { hasNextPage endCursor }
              nodes {
                ... on MediaImage {
                  id
                  alt
                  image {
                    url
                    altText
                  }
                }
              }
            }
            featuredMedia {
              ... on MediaImage {
                alt
                image {
                  url
                  altText
                }
              }
            }
            variants(first: 50) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                title
                sku
                barcode
                price
                compareAtPrice
                inventoryQuantity
                inventoryPolicy
                taxable
                selectedOptions {
                  name
                  value
                }
                image {
                  url
                  altText
                }
                inventoryItem {
                  id
                  tracked
                  unitCost {
                    amount
                    currencyCode
                  }
                  measurement {
                    weight {
                      value
                      unit
                    }
                  }
                }
              }
            }
            metafields(first: 50) {
              pageInfo { hasNextPage endCursor }
              nodes {
                namespace
                key
                type
                value
              }
            }
          }
        }
      }
    }
    """

    while len(products) < limit:
        variables = {
            "first": min(page_size, limit - len(products)),
            "after": after,
            "query": query_filter or None,
            "sortKey": sort_key,
            "reverse": reverse,
        }
        result = execute_graphql_query(query, variables, shop_domain=shop_domain, access_token=access_token)
        if not result or not result.get("data"):
            break

        products_data = result["data"].get("products") or {}
        for edge in products_data.get("edges", []):
            node = edge.get("node") or {}
            featured = node.get("featuredMedia") or {}
            image = featured.get("image") or {}
            image_url = image.get("url") or ""
            media_records = list((node.get("media") or {}).get("nodes", []))
            media_page = (node.get("media") or {}).get("pageInfo") or {}
            if media_page.get("hasNextPage"):
                media_records.extend(fetch_remaining_connection_nodes(
                    node.get("id"), 'Product', 'media',
                    '... on MediaImage { id alt image { url altText } }',
                    media_page.get("endCursor"),
                    shop_domain=shop_domain, access_token=access_token,
                ))
            media_nodes = []
            for media in media_records:
                media_image = media.get("image") or {}
                media_nodes.append({
                    "id": media.get("id") or "",
                    "url": media_image.get("url") or "",
                    "alt": media.get("alt") or media_image.get("altText") or "",
                })
            collection_records = list((node.get("collections") or {}).get("nodes", []))
            collection_page = (node.get("collections") or {}).get("pageInfo") or {}
            if collection_page.get("hasNextPage"):
                collection_records.extend(fetch_remaining_connection_nodes(
                    node.get("id"), 'Product', 'collections', 'id title',
                    collection_page.get("endCursor"),
                    shop_domain=shop_domain, access_token=access_token,
                ))
            collection_titles = [c.get("title") or "" for c in collection_records if c.get("title")]

            variant_records = list((node.get("variants") or {}).get("nodes", []))
            variant_page = (node.get("variants") or {}).get("pageInfo") or {}
            if variant_page.get("hasNextPage"):
                # A variant the app cannot see is a variant a bulk edit could
                # drop, so every page is read before anything is offered.
                variant_records.extend(fetch_remaining_connection_nodes(
                    node.get("id"), 'Product', 'variants',
                    'id title sku barcode price compareAtPrice inventoryQuantity inventoryPolicy '
                    'taxable selectedOptions { name value } image { url altText } '
                    'inventoryItem { id tracked unitCost { amount currencyCode } '
                    'measurement { weight { value unit } } }',
                    variant_page.get("endCursor"),
                    shop_domain=shop_domain, access_token=access_token,
                ))
            variants = []
            for variant in variant_records:
                inventory_item = variant.get("inventoryItem") or {}
                measurement = inventory_item.get("measurement") or {}
                weight = measurement.get("weight") or {}
                variant_image = variant.get("image") or {}
                options = variant.get("selectedOptions") or []
                variants.append({
                    "id": variant.get("id") or "",
                    "title": variant.get("title") or "",
                    "sku": variant.get("sku") or "",
                    "barcode": variant.get("barcode") or "",
                    "price": str(variant.get("price") or ""),
                    "compare_at_price": str(variant.get("compareAtPrice") or ""),
                    "inventory_quantity": variant.get("inventoryQuantity"),
                    "inventory_policy": variant.get("inventoryPolicy") or "",
                    "taxable": variant.get("taxable"),
                    "option1_name": (options[0] or {}).get("name") if len(options) > 0 else "",
                    "option1_value": (options[0] or {}).get("value") if len(options) > 0 else "",
                    "option2_name": (options[1] or {}).get("name") if len(options) > 1 else "",
                    "option2_value": (options[1] or {}).get("value") if len(options) > 1 else "",
                    "option3_name": (options[2] or {}).get("name") if len(options) > 2 else "",
                    "option3_value": (options[2] or {}).get("value") if len(options) > 2 else "",
                    "image_url": variant_image.get("url") or "",
                    "image_alt_text": variant_image.get("altText") or "",
                    "inventory_item_id": inventory_item.get("id") or "",
                    "inventory_tracked": inventory_item.get("tracked"),
                    "unit_cost": str((inventory_item.get("unitCost") or {}).get("amount") or ""),
                    "unit_cost_currency": (inventory_item.get("unitCost") or {}).get("currencyCode") or "",
                    "weight": weight.get("value"),
                    "weight_unit": weight.get("unit") or "",
                })
            metafield_nodes = list((node.get("metafields") or {}).get("nodes", []))
            metafield_page = (node.get("metafields") or {}).get("pageInfo") or {}
            if metafield_page.get("hasNextPage"):
                # More than one page of metafields: read the rest rather than
                # silently reporting a product as having fewer than it has.
                metafield_nodes.extend(fetch_remaining_connection_nodes(
                    node.get("id"), 'Product', 'metafields',
                    'namespace key type value', metafield_page.get("endCursor"),
                    shop_domain=shop_domain, access_token=access_token,
                ))
            metafields = {}
            for mf in metafield_nodes:
                namespace = mf.get("namespace") or ""
                key = mf.get("key") or ""
                if namespace and key:
                    metafields[f"{namespace}.{key}"] = {
                        "namespace": namespace,
                        "key": key,
                        "type": mf.get("type") or "single_line_text_field",
                        "value": mf.get("value") or "",
                    }

            def metafield_value(namespace, key):
                return (metafields.get(f"{namespace}.{key}") or {}).get("value") or ""

            products.append({
                "id": node.get("id") or "",
                "handle": node.get("handle") or "",
                "created_at": node.get("createdAt") or "",
                "updated_at": node.get("updatedAt") or "",
                "title": node.get("title") or "",
                "body_html": node.get("descriptionHtml") or "",
                "vendor": node.get("vendor") or "",
                "type": node.get("productType") or "",
                "tags": ", ".join(node.get("tags") or []),
                "status": node.get("status") or "",
                "category_gid": (node.get("category") or {}).get("id") or "",
                "product_category": (node.get("category") or {}).get("fullName") or "",
                "collections": ", ".join(collection_titles),
                "image_url": image_url,
                "image_alt_text": featured.get("alt") or image.get("altText") or "",
                "media": media_nodes,
                "media_count": len(media_nodes),
                "variants": variants,
                "variant_count": len(variants),
                "variant_sku": (variants[0] or {}).get("sku", "") if variants else "",
                "variant_id": (variants[0] or {}).get("id", "") if variants else "",
                "variant_barcode": (variants[0] or {}).get("barcode", "") if variants else "",
                "variant_price": (variants[0] or {}).get("price", "") if variants else "",
                "variant_compare_at_price": (variants[0] or {}).get("compare_at_price", "") if variants else "",
                "variant_inventory_quantity": (variants[0] or {}).get("inventory_quantity", "") if variants else "",
                "variant_inventory_item_id": (variants[0] or {}).get("inventory_item_id", "") if variants else "",
                "variant_inventory_policy": (variants[0] or {}).get("inventory_policy", "") if variants else "",
                "variant_taxable": (variants[0] or {}).get("taxable", "") if variants else "",
                "variant_weight": (variants[0] or {}).get("weight", "") if variants else "",
                "variant_weight_unit": (variants[0] or {}).get("weight_unit", "") if variants else "",
                "variant_inventory_tracked": (variants[0] or {}).get("inventory_tracked", "") if variants else "",
                "variant_unit_cost": (variants[0] or {}).get("unit_cost", "") if variants else "",
                "variant_unit_cost_currency": (variants[0] or {}).get("unit_cost_currency", "") if variants else "",
                "variant_options": " / ".join(
                    str((variants[0] or {}).get(key) or '') for key in ('option1_value', 'option2_value', 'option3_value')
                    if variants and (variants[0] or {}).get(key)
                ),
                "seo_title": (node.get("seo") or {}).get("title") or "",
                "seo_description": (node.get("seo") or {}).get("description") or "",
                "google_product_category": metafield_value("mm-google-shopping", "google_product_category"),
                "gender": metafield_value("mm-google-shopping", "gender"),
                "age_group": metafield_value("mm-google-shopping", "age_group"),
                "condition": metafield_value("mm-google-shopping", "condition"),
                "custom_product": metafield_value("mm-google-shopping", "custom_product"),
                "custom_label_0": metafield_value("mm-google-shopping", "custom_label_0"),
                "custom_label_1": metafield_value("mm-google-shopping", "custom_label_1"),
                "custom_label_2": metafield_value("mm-google-shopping", "custom_label_2"),
                "custom_label_3": metafield_value("mm-google-shopping", "custom_label_3"),
                "custom_label_4": metafield_value("mm-google-shopping", "custom_label_4"),
                "metafield_color": metafield_value("custom", "color"),
                "metafield_theme": metafield_value("custom", "theme"),
                "metafield_frame_style": metafield_value("custom", "frame_style"),
                "metafield_condition": metafield_value("custom", "condition"),
                "metafield_decoration_material": metafield_value("custom", "decoration_material"),
                "metafield_artwork_frame_material": metafield_value("custom", "artwork_frame_material"),
                "metafields": metafields,
                "source": "shopify_api",
            })

        page_info = products_data.get("pageInfo") or {}
        last_page_info = page_info
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break

    logger.info("Fetched %s Shopify products for SEO enhancement", len(products))
    if include_page_info:
        return {
            "products": products,
            "page_info": {
                "has_next_page": bool(last_page_info.get("hasNextPage")),
                "end_cursor": last_page_info.get("endCursor") or "",
            },
            "query": query_filter or "",
            "sort_key": sort_key,
            "reverse": reverse,
        }
    return products

def start_product_catalogue_bulk_operation(query_filter="", sort_key="UPDATED_AT", reverse=True,
                                           shop_domain=None, access_token=None):
    """Ask Shopify to build a large product catalogue export asynchronously."""
    allowed_sort_keys = {
        "CREATED_AT", "UPDATED_AT", "TITLE", "ID", "PRODUCT_TYPE",
        "VENDOR", "INVENTORY_TOTAL", "BEST_SELLING",
    }
    sort_key = str(sort_key or "UPDATED_AT").upper()
    if sort_key not in allowed_sort_keys:
        sort_key = "UPDATED_AT"
    filter_literal = json.dumps(str(query_filter or ""))
    reverse_literal = "true" if bool(reverse) else "false"
    bulk_query = """
    {
      products(query: %s, sortKey: %s, reverse: %s) {
        edges {
          node {
            __typename
            id
            title
            handle
            createdAt
            updatedAt
            descriptionHtml
            vendor
            productType
            tags
            status
            category { id fullName }
            seo { title description }
            featuredMedia {
              ... on MediaImage {
                alt
                image { url altText }
              }
            }
            collections {
              edges { node { __typename id title } }
            }
            media {
              edges {
                node {
                  __typename
                  ... on MediaImage {
                    id
                    alt
                    image { url altText }
                  }
                }
              }
            }
            variants {
              edges {
                node {
                  __typename
                  id
                  title
                  sku
                  barcode
                  price
                  compareAtPrice
                  inventoryQuantity
                  inventoryPolicy
                  taxable
                  selectedOptions { name value }
                  image { url altText }
                  inventoryItem {
                    id
                    tracked
                    unitCost { amount currencyCode }
                    measurement { weight { value unit } }
                  }
                }
              }
            }
            metafields {
              edges { node { __typename namespace key type value } }
            }
          }
        }
      }
    }
    """ % (filter_literal, sort_key, reverse_literal)
    mutation = """
    mutation startProductCatalogueBulkExport($query: String!) {
      bulkOperationRunQuery(query: $query) {
        bulkOperation { id status }
        userErrors { field message }
      }
    }
    """
    result = execute_graphql_query(
        mutation,
        {"query": bulk_query},
        shop_domain=shop_domain,
        access_token=access_token,
        raise_errors=True,
    )
    payload = ((result or {}).get("data") or {}).get("bulkOperationRunQuery") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError("; ".join(error.get("message", "Unknown bulk export error") for error in errors))
    operation = payload.get("bulkOperation") or {}
    if not operation.get("id"):
        raise RuntimeError("Shopify did not create a bulk catalogue operation")
    return operation


def get_product_catalogue_bulk_operation(operation_id, shop_domain=None, access_token=None):
    query = """
    query productCatalogueBulkExportStatus($id: ID!) {
      node(id: $id) {
        ... on BulkOperation {
          id
          status
          errorCode
          objectCount
          fileSize
          url
          partialDataUrl
        }
      }
    }
    """
    result = execute_graphql_query(
        query,
        {"id": operation_id},
        shop_domain=shop_domain,
        access_token=access_token,
        raise_errors=True,
    )
    operation = ((result or {}).get("data") or {}).get("node")
    if not operation:
        raise RuntimeError("Shopify bulk catalogue operation was not found")
    return operation


def _bulk_product_edit_row(node, children):
    media_nodes = []
    variants = []
    metafields = {}
    collections = []
    for child in children:
        typename = child.get("__typename")
        if typename == "Collection":
            if child.get("title"):
                collections.append(child.get("title"))
        elif typename == "MediaImage":
            image = child.get("image") or {}
            media_nodes.append({
                "id": child.get("id") or "",
                "url": image.get("url") or "",
                "alt": child.get("alt") or image.get("altText") or "",
            })
        elif typename == "ProductVariant":
            inventory_item = child.get("inventoryItem") or {}
            weight = ((inventory_item.get("measurement") or {}).get("weight") or {})
            variant_image = child.get("image") or {}
            options = child.get("selectedOptions") or []
            variants.append({
                "id": child.get("id") or "",
                "title": child.get("title") or "",
                "sku": child.get("sku") or "",
                "barcode": child.get("barcode") or "",
                "price": str(child.get("price") or ""),
                "compare_at_price": str(child.get("compareAtPrice") or ""),
                "inventory_quantity": child.get("inventoryQuantity"),
                "inventory_policy": child.get("inventoryPolicy") or "",
                "taxable": child.get("taxable"),
                "option1_name": (options[0] or {}).get("name") if len(options) > 0 else "",
                "option1_value": (options[0] or {}).get("value") if len(options) > 0 else "",
                "option2_name": (options[1] or {}).get("name") if len(options) > 1 else "",
                "option2_value": (options[1] or {}).get("value") if len(options) > 1 else "",
                "option3_name": (options[2] or {}).get("name") if len(options) > 2 else "",
                "option3_value": (options[2] or {}).get("value") if len(options) > 2 else "",
                "image_url": variant_image.get("url") or "",
                "image_alt_text": variant_image.get("altText") or "",
                "inventory_item_id": inventory_item.get("id") or "",
                "inventory_tracked": inventory_item.get("tracked"),
                "unit_cost": str((inventory_item.get("unitCost") or {}).get("amount") or ""),
                "unit_cost_currency": (inventory_item.get("unitCost") or {}).get("currencyCode") or "",
                "weight": weight.get("value"),
                "weight_unit": weight.get("unit") or "",
            })
        elif typename == "Metafield":
            namespace = child.get("namespace") or ""
            key = child.get("key") or ""
            if namespace and key:
                metafields[f"{namespace}.{key}"] = {
                    "namespace": namespace,
                    "key": key,
                    "type": child.get("type") or "single_line_text_field",
                    "value": child.get("value") or "",
                }

    def mf(namespace, key):
        return (metafields.get(f"{namespace}.{key}") or {}).get("value") or ""

    featured = node.get("featuredMedia") or {}
    image = featured.get("image") or {}
    first_variant = variants[0] if variants else {}
    return {
        "id": node.get("id") or "", "handle": node.get("handle") or "",
        "created_at": node.get("createdAt") or "", "updated_at": node.get("updatedAt") or "",
        "title": node.get("title") or "", "body_html": node.get("descriptionHtml") or "",
        "vendor": node.get("vendor") or "", "type": node.get("productType") or "",
        "tags": ", ".join(node.get("tags") or []), "status": node.get("status") or "",
        "category_gid": (node.get("category") or {}).get("id") or "",
        "product_category": (node.get("category") or {}).get("fullName") or "",
        "collections": ", ".join(collections), "image_url": image.get("url") or "",
        "image_alt_text": featured.get("alt") or image.get("altText") or "",
        "media": media_nodes, "media_count": len(media_nodes),
        "variants": variants, "variant_count": len(variants),
        "variant_sku": first_variant.get("sku", ""), "variant_id": first_variant.get("id", ""),
        "variant_barcode": first_variant.get("barcode", ""), "variant_price": first_variant.get("price", ""),
        "variant_compare_at_price": first_variant.get("compare_at_price", ""),
        "variant_inventory_quantity": first_variant.get("inventory_quantity", ""),
        "variant_inventory_item_id": first_variant.get("inventory_item_id", ""),
        "variant_inventory_policy": first_variant.get("inventory_policy", ""),
        "variant_taxable": first_variant.get("taxable", ""), "variant_weight": first_variant.get("weight", ""),
        "variant_weight_unit": first_variant.get("weight_unit", ""),
        "variant_inventory_tracked": first_variant.get("inventory_tracked", ""),
        "variant_unit_cost": first_variant.get("unit_cost", ""),
        "variant_unit_cost_currency": first_variant.get("unit_cost_currency", ""),
        "variant_options": " / ".join(
            str(first_variant.get(key) or '') for key in ('option1_value', 'option2_value', 'option3_value')
            if first_variant.get(key)
        ),
        "seo_title": (node.get("seo") or {}).get("title") or "",
        "seo_description": (node.get("seo") or {}).get("description") or "",
        "google_product_category": mf("mm-google-shopping", "google_product_category"),
        "gender": mf("mm-google-shopping", "gender"), "age_group": mf("mm-google-shopping", "age_group"),
        "condition": mf("mm-google-shopping", "condition"), "custom_product": mf("mm-google-shopping", "custom_product"),
        "custom_label_0": mf("mm-google-shopping", "custom_label_0"),
        "custom_label_1": mf("mm-google-shopping", "custom_label_1"),
        "custom_label_2": mf("mm-google-shopping", "custom_label_2"),
        "custom_label_3": mf("mm-google-shopping", "custom_label_3"),
        "custom_label_4": mf("mm-google-shopping", "custom_label_4"),
        "metafield_color": mf("custom", "color"), "metafield_theme": mf("custom", "theme"),
        "metafield_frame_style": mf("custom", "frame_style"),
        "metafield_condition": mf("custom", "condition"),
        "metafield_decoration_material": mf("custom", "decoration_material"),
        "metafield_artwork_frame_material": mf("custom", "artwork_frame_material"),
        "metafields": metafields, "source": "shopify_api",
    }


def stream_product_catalogue_bulk_result(result_url, output_path):
    """Stream Shopify JSONL into an indexed JSONL file of normalized products."""
    response = requests.get(result_url, stream=True, timeout=(15, 180))
    response.raise_for_status()
    offsets = []
    current_product = None

    def write_product(handle, product):
        if not product:
            return
        row = _bulk_product_edit_row(product["node"], product["children"])
        encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        offsets.append(handle.tell())
        handle.write(encoded)

    try:
        with open(output_path, "wb") as output:
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                record = json.loads(raw_line)
                if record.get("__typename") == "Product" and not record.get("__parentId"):
                    write_product(output, current_product)
                    current_product = {"node": record, "children": []}
                    continue
                if current_product and record.get("__parentId") == current_product["node"].get("id"):
                    current_product["children"].append(record)
            write_product(output, current_product)
    finally:
        response.close()

    logger.info("Normalized %s Shopify products from bulk catalogue export", len(offsets))
    return offsets


def get_product_metafield_definitions(limit=250, shop_domain=None, access_token=None):
    """Return product metafield definitions configured in the connected shop."""
    try:
        limit = max(1, min(int(limit or 250), 250))
    except (TypeError, ValueError):
        limit = 250

    query = """
    query getProductMetafieldDefinitions($first: Int!) {
      metafieldDefinitions(first: $first, ownerType: PRODUCT) {
        nodes {
          id
          namespace
          key
          name
          type {
            name
          }
        }
      }
    }
    """
    result = execute_graphql_query(query, {"first": limit}, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get("data"):
        return []

    definitions = []
    for node in result["data"].get("metafieldDefinitions", {}).get("nodes", []):
        definitions.append({
            "id": node.get("id") or "",
            "namespace": node.get("namespace") or "",
            "key": node.get("key") or "",
            "name": node.get("name") or "",
            "type": (node.get("type") or {}).get("name") or "single_line_text_field",
        })
    return definitions


def get_product_metafield_definition_type(namespace, key, shop_domain=None, access_token=None):
    """Return the configured Shopify metafield type for a product definition."""
    namespace = str(namespace or "").strip()
    key = str(key or "").strip()
    if not namespace or not key:
        return ""
    try:
        for definition in get_product_metafield_definitions(limit=250, shop_domain=shop_domain, access_token=access_token):
            if definition.get("namespace") == namespace and definition.get("key") == key:
                return definition.get("type") or ""
    except Exception as exc:
        logger.warning("Could not inspect metafield definition %s.%s: %s", namespace, key, exc)
    return ""


def ensure_product_metafield_definitions(keys, shop_domain=None, access_token=None):
    """Best-effort creation of PRODUCT metafield definitions for custom SEO fields.

    Shopify will accept ad hoc metafields without definitions, but definitions
    make them visible/manageable in Admin and more likely to appear in exports
    and filtering tools.
    """
    wanted = []
    for key in keys or []:
        normalized = str(key or "").strip().lower().replace(" ", "_").replace("-", "_")
        if normalized in SEO_PRODUCT_METAFIELD_DEFINITIONS or normalized in CONTENT_PRODUCT_METAFIELD_DEFINITIONS:
            wanted.append(normalized)
    wanted = sorted(set(wanted))
    if not wanted:
        return

    try:
        shop_key, _ = _resolve_credentials(shop_domain, access_token)
    except ValueError:
        return

    cache_key = ("ensured_product_metafield_definitions", shop_key, tuple(wanted))
    with _store_config_cache_lock:
        entry = _store_config_cache.get(cache_key)
        if entry and (time.monotonic() - entry[0]) < STORE_CONFIG_TTL_SECONDS:
            return

    try:
        existing = {
            (item.get("namespace"), item.get("key"))
            for item in get_product_metafield_definitions(shop_domain=shop_domain, access_token=access_token)
        }
    except Exception as exc:
        logger.warning("Could not load product metafield definitions: %s", exc)
        return

    mutation = """
    mutation createProductMetafieldDefinition($definition: MetafieldDefinitionInput!) {
      metafieldDefinitionCreate(definition: $definition) {
        createdDefinition {
          id
          namespace
          key
          name
        }
        userErrors {
          field
          message
          code
        }
      }
    }
    """

    for key in wanted:
        if ("custom", key) in existing:
            continue
        if key in CONTENT_PRODUCT_METAFIELD_DEFINITIONS:
            name, description, definition_type = CONTENT_PRODUCT_METAFIELD_DEFINITIONS[key]
        else:
            name, description = SEO_PRODUCT_METAFIELD_DEFINITIONS[key]
            definition_type = "single_line_text_field"
        variables = {
            "definition": {
                "name": name,
                "namespace": "custom",
                "key": key,
                "type": definition_type,
                "ownerType": "PRODUCT",
                "description": description,
            }
        }
        result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)
        errors = ((result or {}).get("data") or {}).get("metafieldDefinitionCreate", {}).get("userErrors") or []
        if errors:
            joined = "; ".join(str(err.get("message") or err) for err in errors)
            if "taken" not in joined.lower() and "already" not in joined.lower():
                logger.warning("Could not create metafield definition custom.%s: %s", key, joined)
        else:
            logger.info("Ensured product metafield definition custom.%s", key)

    with _store_config_cache_lock:
        _store_config_cache[cache_key] = (time.monotonic(), True)


def get_inventory_locations(limit=50, shop_domain=None, access_token=None):
    """Return active Shopify inventory locations for location-aware stock edits."""
    try:
        limit = max(1, min(int(limit or 50), 250))
    except (TypeError, ValueError):
        limit = 50

    query = """
    query getBulkEditorInventoryLocations($first: Int!) {
      locations(first: $first) {
        nodes {
          id
          name
          isActive
          shipsInventory
          address {
            city
            country
          }
        }
      }
    }
    """
    result = execute_graphql_query(query, {"first": limit}, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get("data"):
        return []

    locations = []
    for node in result["data"].get("locations", {}).get("nodes", []):
        address = node.get("address") or {}
        locations.append({
            "id": node.get("id") or "",
            "name": node.get("name") or "",
            "active": bool(node.get("isActive")),
            "ships_inventory": bool(node.get("shipsInventory")),
            "city": address.get("city") or "",
            "country": address.get("country") or "",
        })
    return locations


def get_price_lists(limit=50, shop_domain=None, access_token=None):
    """Return Shopify price lists used by market/catalog pricing."""
    try:
        limit = max(1, min(int(limit or 50), 250))
    except (TypeError, ValueError):
        limit = 50

    query = """
    query getBulkEditorPriceLists($first: Int!) {
      priceLists(first: $first) {
        nodes {
          id
          name
          currency
          fixedPricesCount
          catalog {
            id
            title
          }
        }
      }
    }
    """
    result = execute_graphql_query(query, {"first": limit}, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get("data"):
        return []

    price_lists = []
    for node in result["data"].get("priceLists", {}).get("nodes", []):
        catalog = node.get("catalog") or {}
        price_lists.append({
            "id": node.get("id") or "",
            "name": node.get("name") or "",
            "currency": node.get("currency") or "",
            "fixed_prices_count": node.get("fixedPricesCount") or 0,
            "catalog_id": catalog.get("id") or "",
            "catalog_title": catalog.get("title") or "",
        })
    return price_lists


def search_products_for_references(search="", limit=20, shop_domain=None, access_token=None):
    """Return lightweight products for product-reference metafield selectors."""
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20

    query = """
    query searchProductsForReferences($first: Int!, $query: String) {
      products(first: $first, query: $query) {
        nodes {
          id
          title
          handle
          featuredMedia {
            ... on MediaImage {
              image {
                url
              }
            }
          }
        }
      }
    }
    """
    result = execute_graphql_query(
        query,
        {"first": limit, "query": search or None},
        shop_domain=shop_domain,
        access_token=access_token,
    )
    if not result or not result.get("data"):
        return []

    products = []
    for node in result["data"].get("products", {}).get("nodes", []):
        image = ((node.get("featuredMedia") or {}).get("image") or {})
        products.append({
            "id": node.get("id") or "",
            "title": node.get("title") or "",
            "handle": node.get("handle") or "",
            "image_url": image.get("url") or "",
        })
    return products


def recommend_products_for_discovery(
    metadata,
    current_product_id="",
    limit_related=4,
    limit_complementary=4,
    shop_domain=None,
    access_token=None,
):
    """Pick real Shopify product IDs for Search & Discovery references.

    The AI provides search terms/tags; this function turns those into valid
    Shopify Product GIDs by searching the connected store and scoring matches.
    """
    metadata = metadata or {}

    def _list(value):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    def _boost_terms_from_metadata(metadata, max_terms=8):
        metadata = metadata or {}
        metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
        raw_values = []
        raw_values.extend(_list(metadata.get("tags"))[:10])
        raw_values.extend(_list(metadata.get("collections"))[:6])
        raw_values.extend([
            metadata.get("title"),
            metafields.get("subject") or metadata.get("custom_label_3"),
            metafields.get("theme"),
            metafields.get("art_style") or metadata.get("custom_label_0"),
            metafields.get("room") or metadata.get("custom_label_1"),
            metafields.get("mood"),
        ])
        terms = []
        for value in raw_values:
            for part in re.split(r"[,|;/]+", str(value or "")):
                term = " ".join(part.lower().split()).strip(" .")
                if term and len(term) > 2 and term not in SEARCH_BOOST_GENERIC_TERMS and term not in terms:
                    terms.append(term)
        return _sanitize_search_boost_terms(terms, max_terms=max_terms)

    def _parts(value):
        bits = []
        for part in re.split(r"[,|;/]+|\s+&\s+|\s+and\s+", str(value or ""), flags=re.I):
            part = " ".join(part.split()).strip()
            if part:
                bits.append(part)
        return bits

    def _tokens(value):
        stopwords = {
            "wall", "art", "poster", "print", "prints", "home", "decor",
            "modern", "style", "with", "from", "this", "that", "the", "and",
            "for", "your", "room", "image", "green", "blue", "red", "black",
            "white", "cream", "beige", "orange", "yellow", "pink", "brown",
        }
        words = []
        for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", str(value or "")):
            clean = word.strip("'-").lower()
            if clean and clean not in stopwords:
                words.append(clean)
        return words

    def _add_signal(signals, value, weight=1):
        text = " ".join(str(value or "").split()).strip(" ,.;:")
        if not text:
            return
        key = text.lower()
        if not _is_concrete_discovery_term(key):
            return
        if len(key) < 3:
            return
        signals[key] = max(signals.get(key, 0), weight)

    tags = _list(metadata.get("tags"))[:12]
    collections = _list(metadata.get("collections"))[:8]
    title = str(metadata.get("title") or "").strip()
    product_type = str(metadata.get("product_type") or "").strip()
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    theme = str(metafields.get("theme") or "").strip()
    subject = str(metafields.get("subject") or metadata.get("custom_label_3") or "").strip()
    room = str(metafields.get("room") or metadata.get("custom_label_1") or "").strip()
    palette = str(metafields.get("palette") or metafields.get("color") or metadata.get("custom_label_2") or "").strip()
    art_style = str(metafields.get("art_style") or metadata.get("custom_label_0") or "").strip()
    art_movement = str(metafields.get("art_movement") or "").strip()
    mood = str(metafields.get("mood") or "").strip()

    signals = {}
    for value in _product_search_phrase_candidates(metadata, max_terms=10):
        _add_signal(signals, value, 8)
    for value in (subject,):
        for part in _parts(value):
            _add_signal(signals, part, 7)
    for phrase in _parts(title):
        _add_signal(signals, phrase, 6)
    for value in (theme, art_style, art_movement):
        for part in _parts(value):
            _add_signal(signals, part, 4)
    search_terms = [
        term for term, _weight in sorted(signals.items(), key=lambda item: (-item[1], len(item[0])))
    ]
    # Cap the number of Shopify queries this makes. Each term = one synchronous
    # GraphQL call; on the free Render tier the old 16-call burst (plus ~15 more
    # calls in create_product_with_graphql) was killing the instance mid-listing.
    # Keep the cap modest but use richer weighted terms.
    MAX_DISCOVERY_QUERIES = 7
    search_terms = search_terms[:MAX_DISCOVERY_QUERIES]
    # Stop early once we have a healthy candidate pool to score from.
    enough_candidates = max(12, (limit_related + limit_complementary) * 2)

    query = """
    query recommendProductsForDiscovery($first: Int!, $query: String) {
      products(first: $first, query: $query) {
        nodes {
          id
          title
          handle
          productType
          tags
          collections(first: 10) {
            nodes {
              title
            }
          }
        }
      }
    }
    """

    candidates = {}
    for term in search_terms:
        if not term:
            continue
        try:
            result = execute_graphql_query(
                query,
                {"first": 10, "query": term},
                shop_domain=shop_domain,
                access_token=access_token,
            )
        except Exception as search_error:
            logger.warning("Discovery product search failed for term %r: %s", term, search_error)
            continue
        for node in ((result or {}).get("data") or {}).get("products", {}).get("nodes", []):
            product_id = node.get("id") or ""
            if not product_id or product_id == current_product_id:
                continue
            current = candidates.setdefault(product_id, {**node, "_hits": 0})
            current["_hits"] += 1
            current.setdefault("_matched_terms", set()).add(term)
        # Early exit — we already have enough distinct products to score.
        if len(candidates) >= enough_candidates:
            break

    tag_set = {tag.lower() for tag in tags}
    collection_set = {collection.lower() for collection in collections}
    title_words = {word.lower() for word in title.replace("|", " ").split() if len(word) > 3}
    strong_terms = {term for term, weight in signals.items() if weight >= 4}
    high_intent_terms = {term for term, weight in signals.items() if weight >= 7}
    all_terms = set(signals.keys())
    anchor_terms = {
        term
        for term in _product_search_phrase_candidates(metadata, max_terms=12)
        if _is_concrete_discovery_term(term)
    }
    for source in (subject, theme):
        for part in _parts(source):
            clean_part = part.lower()
            if len(clean_part) > 3 and _is_concrete_discovery_term(clean_part):
                anchor_terms.add(clean_part)
    concrete_anchor_words = {
        word
        for term in anchor_terms
        for word in _discovery_concrete_words(term)
    }
    ambiguous_negative_contexts = {
        "pool": ("pool table", "billiard", "billiards", "snooker", "cue", "eight ball", "8 ball"),
    }

    scored = []
    def _high_intent_match(term, haystack):
        for word, negatives in ambiguous_negative_contexts.items():
            if word in re.findall(r"[a-z0-9]+", term) and any(negative in haystack for negative in negatives):
                return False
        if term in haystack:
            return True
        noise = {"wall", "art", "poster", "posters", "print", "prints", "decor"}
        words = [word for word in re.findall(r"[a-z0-9]+", term) if word not in noise]
        if len(words) < 2:
            return False
        hits = sum(1 for word in words if word in haystack)
        return hits >= max(2, len(words) - 1)

    def _anchor_match(haystack):
        if any(negative in haystack for negatives in ambiguous_negative_contexts.values() for negative in negatives):
            return False
        haystack_words = set(re.findall(r"[a-z0-9]+", haystack))
        concrete_hits = concrete_anchor_words & haystack_words
        return bool(concrete_hits) and any(_high_intent_match(term, haystack) for term in anchor_terms)

    for product_id, candidate in candidates.items():
        candidate_tags = {str(tag).lower() for tag in candidate.get("tags") or []}
        candidate_collections = {
            str(collection.get("title") or "").lower()
            for collection in ((candidate.get("collections") or {}).get("nodes") or [])
        }
        candidate_title_words = {
            word.lower()
            for word in str(candidate.get("title") or "").replace("|", " ").split()
            if len(word) > 3
        }
        haystack = " ".join(
            [
                str(candidate.get("title") or ""),
                str(candidate.get("handle") or ""),
                " ".join(candidate.get("tags") or []),
                " ".join(candidate_collections),
                str(candidate.get("productType") or ""),
            ]
        ).lower()
        matched_terms = candidate.get("_matched_terms") or set()
        score = int(candidate.get("_hits") or 0)
        score += sum(signals.get(term, 0) for term in matched_terms)
        score += len(tag_set & candidate_tags) * 4
        score += len(collection_set & candidate_collections) * 3
        score += len(title_words & candidate_title_words) * 2
        score += sum(3 for term in strong_terms if term in haystack)
        score += sum(1 for term in all_terms if len(term) > 4 and term in haystack)
        if product_type and product_type.lower() == str(candidate.get("productType") or "").lower():
            score += 2
        related_score = score
        complementary_score = score
        if subject and subject.lower() in haystack:
            related_score += 6
        if theme and theme.lower() in haystack:
            related_score += 4
            complementary_score += 3
        if room and room.lower() in haystack:
            complementary_score += 4
        if palette:
            complementary_score += sum(2 for color in _parts(palette) if color.lower() in haystack)
        high_intent_match = any(_high_intent_match(term, haystack) for term in high_intent_terms)
        anchor_match = _anchor_match(haystack)
        scored.append((score, related_score, complementary_score, high_intent_match, anchor_match, candidate))

    related_sorted = sorted(scored, key=lambda item: item[1], reverse=True)
    complementary_sorted = sorted(scored, key=lambda item: item[2], reverse=True)
    ai_discovery = None
    try:
        from gemini_utils import curate_listing_discovery_with_ai
        ai_discovery = curate_listing_discovery_with_ai(
            metadata,
            candidate_products=[candidate for *_scores, candidate in scored],
            max_related=limit_related,
            max_complementary=limit_complementary,
        )
    except Exception as ai_discovery_error:
        logger.warning("AI discovery curation unavailable: %s", ai_discovery_error)

    related = [
        candidate["id"]
        for _score, related_score, _comp_score, high_intent_match, anchor_match, candidate in related_sorted
        if related_score >= 12 and high_intent_match and anchor_match
    ][:limit_related]
    complementary = [
        candidate["id"]
        for _score, _related_score, comp_score, high_intent_match, anchor_match, candidate in complementary_sorted
        if comp_score >= 12 and high_intent_match and anchor_match and candidate["id"] not in related
    ][:limit_complementary]
    skipped = [
        str(candidate.get("title") or candidate.get("handle") or candidate.get("id"))
        for _score, related_score, _comp_score, high_intent_match, anchor_match, candidate in related_sorted[:6]
        if not (related_score >= 12 and high_intent_match and anchor_match)
    ]
    if skipped:
        logger.info("Discovery skipped weak related candidates: %s", skipped)

    boosts = []
    for value in _product_search_phrase_candidates(metadata, max_terms=10) + search_terms + tags + collections + [subject, theme, art_style, room]:
        for part in str(value).replace("|", ",").split(","):
            term = part.strip().lower()
            if term and len(term) > 2 and term not in SEARCH_BOOST_GENERIC_TERMS and term not in boosts:
                boosts.append(term)
    for term in _boost_terms_from_metadata(metadata):
        if term not in boosts:
            boosts.append(term)
    boosts = _sanitize_search_boost_terms(boosts, max_terms=8)
    if ai_discovery is not None:
        ai_related = [
            product_id
            for product_id in (ai_discovery.get("related_products") or [])
            if product_id in candidates
        ][:limit_related]
        ai_complementary = [
            product_id
            for product_id in (ai_discovery.get("complementary_products") or [])
            if product_id in candidates and product_id not in ai_related
        ][:limit_complementary]
        ai_boosts = _sanitize_search_boost_terms(ai_discovery.get("search_boosts") or [], max_terms=8)
        related = ai_related
        complementary = ai_complementary
        if ai_boosts:
            boosts = ai_boosts
        logger.info(
            "AI discovery curation applied: related=%s complementary=%s boosts=%s",
            len(related),
            len(complementary),
            boosts,
        )
    return {
        "related_products": related,
        "complementary_products": complementary[:limit_complementary],
        "search_boosts": boosts,
    }


def _parse_json_payload(value, expected_type=list):
    if value is None or value == "":
        return None
    if isinstance(value, expected_type):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, expected_type) else None
    return None


def _json_payload_error(value, label, expected_type=list):
    if value in (None, "") or isinstance(value, expected_type):
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return f"{label} contains invalid JSON: {exc.msg}"
        if not isinstance(parsed, expected_type):
            return f"{label} must be a JSON {expected_type.__name__}"
    return ""


def _normalize_shopify_weight_unit(value):
    normalized = str(value or "").strip().upper().replace(" ", "_")
    aliases = {
        "G": "GRAMS", "GRAM": "GRAMS", "GRAMS": "GRAMS",
        "KG": "KILOGRAMS", "KILOGRAM": "KILOGRAMS", "KILOGRAMS": "KILOGRAMS",
        "OZ": "OUNCES", "OUNCE": "OUNCES", "OUNCES": "OUNCES",
        "LB": "POUNDS", "LBS": "POUNDS", "POUND": "POUNDS", "POUNDS": "POUNDS",
    }
    return aliases.get(normalized)


def _variant_update_input_from_data(data):
    variant_id = data.get("id") or data.get("variant_id")
    if not variant_id:
        return None

    variant_input = {"id": variant_id}
    if data.get("price") not in (None, ""):
        variant_input["price"] = str(data["price"])
    compare_price = data.get("compare_at_price", data.get("compareAtPrice"))
    if compare_price not in (None, ""):
        variant_input["compareAtPrice"] = str(compare_price)
    if data.get("barcode") not in (None, ""):
        variant_input["barcode"] = str(data["barcode"])
    inventory_policy = data.get("inventory_policy", data.get("inventoryPolicy"))
    if inventory_policy not in (None, ""):
        variant_input["inventoryPolicy"] = str(inventory_policy).upper()
    if data.get("taxable") not in (None, ""):
        variant_input["taxable"] = str(data["taxable"]).strip().lower() in ("1", "true", "yes", "y", "on")
    inventory_item = {}
    if data.get("sku") not in (None, ""):
        inventory_item["sku"] = str(data["sku"])

    raw_weight = data.get("weight")
    raw_weight_unit = data.get("weight_unit", data.get("weightUnit"))
    if raw_weight not in (None, "") or raw_weight_unit not in (None, ""):
        unit = _normalize_shopify_weight_unit(raw_weight_unit)
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            raise ValueError("Variant weight must be a number when a weight unit is supplied")
        if weight < 0:
            raise ValueError("Variant weight cannot be negative")
        if not unit:
            raise ValueError("Weight unit must be grams, kilograms, ounces, or pounds")
        inventory_item["measurement"] = {"weight": {"value": weight, "unit": unit}}

    if inventory_item:
        variant_input["inventoryItem"] = inventory_item

    return variant_input if len(variant_input) > 1 else None


def _set_inventory_quantity(inventory_item_id, location_id, quantity, shop_domain=None, access_token=None):
    try:
        quantity = int(float(str(quantity).strip()))
    except (TypeError, ValueError):
        return {"success": False, "error": "Inventory quantity must be a number"}

    mutation = """
    mutation setBulkEditorInventory($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        inventoryAdjustmentGroup {
          createdAt
        }
        userErrors {
          field
          message
          code
        }
      }
    }
    """
    variables = {
        "input": {
            "name": "available",
            "reason": "correction",
            "ignoreCompareQuantity": True,
            "referenceDocumentUri": "gid://listing-cannon/BulkEdit/shopify-api",
            "quantities": [{
                "inventoryItemId": inventory_item_id,
                "locationId": location_id,
                "quantity": quantity,
                "compareQuantity": None,
            }],
        }
    }
    result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)
    errors = ((result or {}).get("data") or {}).get("inventorySetQuantities", {}).get("userErrors") or []
    if errors:
        return {"success": False, "error": "; ".join(e.get("message", "Unknown error") for e in errors)}
    return {"success": True}


def _update_product_media_alt(product_id, media_updates, shop_domain=None, access_token=None):
    media_input = []
    for item in media_updates or []:
        media_id = item.get("id")
        if not media_id:
            continue
        if item.get("alt") is None:
            continue
        media_input.append({"id": media_id, "alt": str(item.get("alt") or "")})

    if not media_input:
        return {"success": True, "updated": 0}

    mutation = """
    mutation updateBulkEditorMediaAlt($productId: ID!, $media: [UpdateMediaInput!]!) {
      productUpdateMedia(productId: $productId, media: $media) {
        media {
          alt
        }
        mediaUserErrors {
          field
          message
          code
        }
      }
    }
    """
    result = execute_graphql_query(
        mutation,
        {"productId": product_id, "media": media_input},
        shop_domain=shop_domain,
        access_token=access_token,
    )
    errors = ((result or {}).get("data") or {}).get("productUpdateMedia", {}).get("mediaUserErrors") or []
    if errors:
        return {"success": False, "error": "; ".join(e.get("message", "Unknown error") for e in errors)}
    return {"success": True, "updated": len(media_input)}


def assign_media_to_product_variants(product_id, media_id, variant_ids, shop_domain=None, access_token=None):
    """Attach an existing product media item to selected product variants."""
    if not product_id:
        return {"success": False, "error": "Missing product id"}
    if not media_id:
        return {"success": False, "error": "Missing product media id"}

    variant_media = []
    for variant_id in variant_ids or []:
        if not variant_id:
            continue
        variant_media.append({
            "variantId": variant_id,
            "mediaIds": [media_id],
        })

    if not variant_media:
        return {"success": False, "error": "No variants selected"}

    mutation = """
    mutation appendMainMediaToVariants($productId: ID!, $variantMedia: [ProductVariantAppendMediaInput!]!) {
      productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
        product {
          id
        }
        productVariants {
          id
        }
        userErrors {
          field
          message
          code
        }
      }
    }
    """
    result = execute_graphql_query(
        mutation,
        {"productId": product_id, "variantMedia": variant_media},
        shop_domain=shop_domain,
        access_token=access_token,
    )
    payload = ((result or {}).get("data") or {}).get("productVariantAppendMedia") or {}
    errors = payload.get("userErrors") or []
    if errors:
        return {"success": False, "error": "; ".join(e.get("message", "Unknown error") for e in errors)}
    return {
        "success": True,
        "variants_updated": len(payload.get("productVariants") or variant_media),
    }


def _update_price_list_prices(price_list_updates, shop_domain=None, access_token=None):
    updated = 0
    for group in price_list_updates or []:
        price_list_id = group.get("price_list_id") or group.get("priceListId")
        prices = []
        for price in group.get("prices") or []:
            variant_id = price.get("variant_id") or price.get("variantId")
            amount = price.get("price")
            currency = price.get("currency_code") or price.get("currencyCode") or group.get("currency")
            if not price_list_id or not variant_id or amount in (None, "") or not currency:
                continue
            entry = {
                "variantId": variant_id,
                "price": {
                    "amount": str(amount),
                    "currencyCode": str(currency).upper(),
                },
            }
            compare_amount = price.get("compare_at_price") or price.get("compareAtPrice")
            if compare_amount not in (None, ""):
                entry["compareAtPrice"] = {
                    "amount": str(compare_amount),
                    "currencyCode": str(currency).upper(),
                }
            prices.append(entry)

        if not prices:
            continue

        mutation = """
        mutation updateBulkEditorPriceList($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
          priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
            prices {
              price {
                amount
                currencyCode
              }
            }
            userErrors {
              field
              code
              message
            }
          }
        }
        """
        result = execute_graphql_query(
            mutation,
            {"priceListId": price_list_id, "prices": prices},
            shop_domain=shop_domain,
            access_token=access_token,
        )
        errors = ((result or {}).get("data") or {}).get("priceListFixedPricesAdd", {}).get("userErrors") or []
        if errors:
            return {"success": False, "error": "; ".join(e.get("message", "Unknown error") for e in errors)}
        updated += len(prices)

    return {"success": True, "updated": updated}


def update_product_seo_metadata(product_id, enhanced_data, shop_domain=None, access_token=None):
    """Apply AI-enhanced product fields and metafields to an existing Shopify product."""
    if not product_id:
        return {"success": False, "error": "Missing Shopify product id"}

    product_input = {
        "id": product_id,
    }
    if enhanced_data.get("handle"):
        product_input["handle"] = str(enhanced_data["handle"]).strip()
    if enhanced_data.get("status"):
        product_input["status"] = str(enhanced_data["status"]).strip().upper()

    if enhanced_data.get("title"):
        product_input["title"] = enhanced_data["title"]
    if enhanced_data.get("body_html"):
        product_input["descriptionHtml"] = enhanced_data["body_html"]
    if enhanced_data.get("tags"):
        if isinstance(enhanced_data["tags"], list):
            product_input["tags"] = enhanced_data["tags"]
        else:
            product_input["tags"] = [t.strip() for t in str(enhanced_data["tags"]).split(",") if t.strip()]
    if enhanced_data.get("vendor"):
        product_input["vendor"] = enhanced_data["vendor"]
    if enhanced_data.get("product_type"):
        product_input["productType"] = enhanced_data["product_type"]
    if enhanced_data.get("category_gid"):
        product_input["category"] = enhanced_data["category_gid"]
    if enhanced_data.get("seo_title") or enhanced_data.get("seo_description"):
        product_input["seo"] = {}
        if enhanced_data.get("seo_title"):
            product_input["seo"]["title"] = enhanced_data["seo_title"]
        if enhanced_data.get("seo_description"):
            product_input["seo"]["description"] = enhanced_data["seo_description"]

    mutation = """
    mutation updateProductSeoMetadata($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product {
          id
          title
          handle
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    result = execute_graphql_query(mutation, {"product": product_input}, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get("data"):
        return {"success": False, "error": "Shopify productUpdate failed"}

    update_result = result["data"].get("productUpdate") or {}
    errors = update_result.get("userErrors") or []
    if errors:
        return {"success": False, "error": "; ".join(e.get("message", "Unknown error") for e in errors)}

    metafields = []
    metafield_key_map = {
        "metafield_color": ("custom", "color"),
        "metafield_theme": ("custom", "theme"),
        "metafield_frame_style": ("custom", "frame_style"),
        "metafield_condition": ("custom", "condition"),
        "metafield_decoration_material": ("custom", "decoration_material"),
        "metafield_artwork_frame_material": ("custom", "artwork_frame_material"),
        "google_product_category": ("mm-google-shopping", "google_product_category"),
        "gender": ("mm-google-shopping", "gender"),
        "age_group": ("mm-google-shopping", "age_group"),
        "condition": ("mm-google-shopping", "condition"),
        "custom_product": ("mm-google-shopping", "custom_product"),
        "custom_label_0": ("mm-google-shopping", "custom_label_0"),
        "custom_label_1": ("mm-google-shopping", "custom_label_1"),
        "custom_label_2": ("mm-google-shopping", "custom_label_2"),
        "custom_label_3": ("mm-google-shopping", "custom_label_3"),
        "custom_label_4": ("mm-google-shopping", "custom_label_4"),
    }
    for data_key, (namespace, key) in metafield_key_map.items():
        value = enhanced_data.get(data_key)
        if value is None or str(value).strip() == "":
            continue
        metafield_type = "single_line_text_field"
        metafield_value = str(value)
        if namespace == "mm-google-shopping" and key == "custom_product":
            metafield_type = "boolean"
            metafield_value = "true" if str(value).strip().lower() in ("1", "true", "yes", "y", "on") else "false"
        metafields.append({
            "ownerId": product_id,
            "namespace": namespace,
            "key": key,
            "type": metafield_type,
            "value": metafield_value,
        })

    # Google Shopping colour attribute (max 3 primary colours, '/'-separated)
    # derived from the product palette/colour. Size is a per-variant attribute
    # mapped automatically from the "Size" option, so it is not set here.
    _google_color = _format_google_color(
        enhanced_data.get("metafield_palette")
        or enhanced_data.get("metafield_color")
        or enhanced_data.get("color")
    )
    if _google_color:
        metafields.append({
            "ownerId": product_id,
            "namespace": "mm-google-shopping",
            "key": "color",
            "type": "single_line_text_field",
            "value": _google_color,
        })

    for data_key, value in enhanced_data.items():
        if not data_key.startswith("mf__") or data_key.startswith("mf_type__"):
            continue
        if value is None or str(value).strip() == "":
            continue
        parts = data_key.split("__", 2)
        if len(parts) != 3:
            continue
        _, namespace, key = parts
        metafield_type = enhanced_data.get(f"mf_type__{namespace}__{key}") or "single_line_text_field"
        metafields.append({
            "ownerId": product_id,
            "namespace": namespace,
            "key": key,
            "type": metafield_type,
            "value": str(value),
        })

    if metafields:
        deduped = {}
        for metafield in metafields:
            deduped[(metafield["namespace"], metafield["key"])] = metafield
        metafields = list(deduped.values())
        mf_mutation = """
        mutation setProductMetafields($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields {
              id
              namespace
              key
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        mf_result = execute_graphql_query(mf_mutation, {"metafields": metafields}, shop_domain=shop_domain, access_token=access_token)
        mf_errors = ((mf_result or {}).get("data") or {}).get("metafieldsSet", {}).get("userErrors") or []
        if mf_errors:
            return {
                "success": False,
                "error": "Product updated, but metafields failed: " + "; ".join(e.get("message", "Unknown error") for e in mf_errors),
            }

    for key, label in (
        ("variants_json", "All Variants JSON"),
        ("media_json", "All Media Alt Text JSON"),
        ("price_lists_json", "Market Price Lists JSON"),
    ):
        payload_error = _json_payload_error(enhanced_data.get(key), label, expected_type=list)
        if payload_error:
            return {"success": False, "error": f"Product updated, but advanced data was not applied: {payload_error}"}

    variants_updated = 0
    variants_created = 0
    variant_updates = []
    variant_creates = []
    variants_payload = _parse_json_payload(enhanced_data.get("variants_json"), expected_type=list)
    if variants_payload:
        for variant in variants_payload:
            if variant.get("id") or variant.get("variant_id"):
                try:
                    variant_input = _variant_update_input_from_data(variant)
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                if variant_input:
                    variant_updates.append(variant_input)
            else:
                variant_creates.append(variant)
    elif enhanced_data.get("variant_id"):
        try:
            variant_input = _variant_update_input_from_data({
                "id": enhanced_data.get("variant_id"),
                "sku": enhanced_data.get("variant_sku"),
                "barcode": enhanced_data.get("variant_barcode"),
                "price": enhanced_data.get("variant_price"),
                "compare_at_price": enhanced_data.get("variant_compare_at_price"),
                "inventory_policy": enhanced_data.get("variant_inventory_policy"),
                "taxable": enhanced_data.get("variant_taxable"),
                "weight": enhanced_data.get("variant_weight"),
                "weight_unit": enhanced_data.get("variant_weight_unit"),
            })
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if variant_input:
            variant_updates.append(variant_input)

    if variant_creates:
        sku_seed = (
            enhanced_data.get("variant_sku")
            or enhanced_data.get("handle")
            or enhanced_data.get("title")
            or "POSTER"
        )
        create_ok = create_variants_graphql(
            product_id,
            variant_creates,
            shop_domain=shop_domain,
            access_token=access_token,
            sku_base=str(sku_seed).upper().replace("-", "")[:24],
        )
        if not create_ok:
            return {"success": False, "error": "Product updated, but variant creation failed"}
        variants_created = len(variant_creates)

    if variant_updates:
            variant_mutation = """
            mutation updateBulkEditorVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
              productVariantsBulkUpdate(productId: $productId, variants: $variants) {
                productVariants {
                  id
                }
                userErrors {
                  field
                  message
                }
              }
            }
            """
            variant_result = execute_graphql_query(
                variant_mutation,
                {"productId": product_id, "variants": variant_updates},
                shop_domain=shop_domain,
                access_token=access_token,
            )
            variant_errors = ((variant_result or {}).get("data") or {}).get("productVariantsBulkUpdate", {}).get("userErrors") or []
            if variant_errors:
                return {
                    "success": False,
                    "error": "Product updated, but variant update failed: " + "; ".join(e.get("message", "Unknown error") for e in variant_errors),
                }
            variants_updated = len(((variant_result or {}).get("data") or {}).get("productVariantsBulkUpdate", {}).get("productVariants") or [])

    variants_deleted = 0
    variant_mode = str(enhanced_data.get("variant_mode") or "merge").strip().lower()
    if variant_mode not in ("merge", "append", "replace"):
        return {"success": False, "error": "Variant mode must be merge, append, or replace"}
    if variant_mode == "replace":
        original_ids = enhanced_data.get("original_variant_ids") or []
        if isinstance(original_ids, str):
            try:
                original_ids = json.loads(original_ids)
            except (TypeError, ValueError, json.JSONDecodeError):
                original_ids = []
        retained_ids = {
            variant.get("id") or variant.get("variant_id")
            for variant in (variants_payload or [])
            if variant.get("id") or variant.get("variant_id")
        }
        delete_ids = [variant_id for variant_id in original_ids if variant_id and variant_id not in retained_ids]
        if not variants_payload:
            return {"success": False, "error": "Variant replacement requires at least one proposed variant"}
        if not retained_ids and not variants_created:
            return {"success": False, "error": "Variant replacement did not create or retain a replacement variant"}
        if delete_ids:
            delete_mutation = """
            mutation deleteReplacedProductVariants($productId: ID!, $variantIds: [ID!]!) {
              productVariantsBulkDelete(productId: $productId, variantsIds: $variantIds) {
                product { id }
                userErrors { field message }
              }
            }
            """
            delete_result = execute_graphql_query(
                delete_mutation,
                {"productId": product_id, "variantIds": delete_ids},
                shop_domain=shop_domain,
                access_token=access_token,
            )
            delete_errors = (
                ((delete_result or {}).get("data") or {})
                .get("productVariantsBulkDelete", {})
                .get("userErrors") or []
            )
            if delete_errors:
                return {
                    "success": False,
                    "error": "Replacement variants were prepared, but old variants could not be removed: " +
                             "; ".join(error.get("message", "Unknown error") for error in delete_errors),
                }
            variants_deleted = len(delete_ids)

    inventory_updated = 0
    inventory_errors = []
    variants_for_inventory = variants_payload or []
    if not variants_for_inventory and enhanced_data.get("variant_inventory_item_id"):
        variants_for_inventory = [{
            "inventory_item_id": enhanced_data.get("variant_inventory_item_id"),
            "inventory_location_id": enhanced_data.get("variant_inventory_location_id"),
            "inventory_quantity": enhanced_data.get("variant_inventory_quantity"),
            "apply_inventory": enhanced_data.get("variant_apply_inventory"),
        }]
    for variant in variants_for_inventory:
        apply_inventory = str(variant.get("apply_inventory") or "").strip().lower() in ("1", "true", "yes", "y", "on")
        if not apply_inventory:
            continue
        inventory_item_id = variant.get("inventory_item_id") or variant.get("inventoryItemId")
        location_id = variant.get("inventory_location_id") or variant.get("location_id") or enhanced_data.get("variant_inventory_location_id")
        quantity = variant.get("inventory_quantity")
        if not inventory_item_id or not location_id or quantity in (None, ""):
            continue
        inv_result = _set_inventory_quantity(inventory_item_id, location_id, quantity, shop_domain=shop_domain, access_token=access_token)
        if inv_result.get("success"):
            inventory_updated += 1
        else:
            inventory_errors.append(inv_result.get("error") or "Unknown inventory error")
    if inventory_errors:
        return {"success": False, "error": "Product updated, but inventory failed: " + "; ".join(inventory_errors)}

    media_payload = _parse_json_payload(enhanced_data.get("media_json"), expected_type=list)
    media_result = _update_product_media_alt(product_id, media_payload, shop_domain=shop_domain, access_token=access_token) if media_payload else {"success": True, "updated": 0}
    if not media_result.get("success"):
        return {"success": False, "error": "Product updated, but media alt text failed: " + (media_result.get("error") or "Unknown error")}

    price_payload = _parse_json_payload(enhanced_data.get("price_lists_json"), expected_type=list)
    price_result = _update_price_list_prices(price_payload, shop_domain=shop_domain, access_token=access_token) if price_payload else {"success": True, "updated": 0}
    if not price_result.get("success"):
        return {"success": False, "error": "Product updated, but market price list update failed: " + (price_result.get("error") or "Unknown error")}

    return {
        "success": True,
        "product": update_result.get("product") or {},
        "metafields_updated": len(metafields),
        "variants_deleted": variants_deleted,
        "variants_updated": variants_updated,
        "variants_created": variants_created,
        "inventory_updated": inventory_updated,
        "media_updated": media_result.get("updated", 0),
        "price_list_prices_updated": price_result.get("updated", 0),
    }

def get_sales_channels(shop_domain=None, access_token=None):
    """Get all publishing channels and catalogs (markets) using GraphQL"""
    query = """
    query getSalesChannels {
        publications(first: 50) {
            edges {
                node {
                    id
                    name
                    supportsFuturePublishing
                    app {
                        id
                        title
                    }
                }
            }
        }
        markets(first: 50) {
            edges {
                node {
                    id
                    name
                    primary
                    enabled
                }
            }
        }
    }
    """
    
    result = execute_graphql_query(query, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get('data'):
        logger.warning("No data from GraphQL, returning empty structure")
        return {
            'publications': {'edges': []},
            'markets': {'edges': []}
        }
    
    data = result['data']
    
    sales_channels = []
    for edge in data['publications']['edges']:
        node = edge['node']
        sales_channels.append({
            'id': node['id'],
            'name': node['name'],
            'supports_future_publishing': node['supportsFuturePublishing'],
            'app_title': node['app']['title'] if node['app'] else None
        })
    
    markets = []
    for edge in data['markets']['edges']:
        node = edge['node']
        markets.append({
            'id': node['id'],
            'name': node['name'],
            'primary': node['primary'],
            'enabled': node['enabled']
        })
    
    logger.info(f"Retrieved {len(sales_channels)} publishing channels and {len(markets)} catalogs via GraphQL")
    
    # Return data in the same format as the GraphQL response so frontend can parse it correctly
    return {
        'publications': {
            'edges': [{'node': {
                'id': channel['id'],
                'name': channel['name'],
                'supportsFuturePublishing': channel['supports_future_publishing'],
                'app': {'title': channel['app_title']} if channel['app_title'] else None
            }} for channel in sales_channels]
        },
        'markets': {
            'edges': [{'node': {
                'id': market['id'],
                'name': market['name'],
                'primary': market['primary'],
                'enabled': market['enabled']
            }} for market in markets]
        }
    }


def get_catalog_publication_ids_for_markets(market_ids, shop_domain=None, access_token=None):
    """Get the catalog publication IDs for specific markets.
    
    Each market in Shopify has an associated MarketCatalog with its own Publication.
    To make a product available in a market, you must publish to that catalog's publication.
    
    Args:
        market_ids: List of market GIDs (e.g., ['gid://shopify/Market/123'])
    
    Returns:
        List of publication GIDs for the market catalogs
    """
    if not market_ids:
        return []

    _sd_key, _ = _resolve_credentials(shop_domain, access_token)
    cache_key = ("market_catalogs", _sd_key, tuple(sorted(market_ids)))
    return _store_config_cached(
        cache_key,
        STORE_CONFIG_TTL_SECONDS,
        lambda: _fetch_catalog_publication_ids_for_markets(market_ids, shop_domain, access_token),
    )


def _fetch_catalog_publication_ids_for_markets(market_ids, shop_domain=None, access_token=None):
    # Query catalogs and find ones linked to the requested markets
    query = """
    query getCatalogs {
        catalogs(first: 50) {
            edges {
                node {
                    id
                    title
                    ... on MarketCatalog {
                        markets(first: 10) {
                            edges {
                                node {
                                    id
                                    name
                                }
                            }
                        }
                        publication {
                            id
                        }
                    }
                }
            }
        }
    }
    """
    
    result = execute_graphql_query(query, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get('data'):
        logger.warning("Failed to fetch catalogs for market publishing")
        return []
    
    catalog_publication_ids = []
    market_ids_set = set(market_ids)
    
    for edge in result['data']['catalogs']['edges']:
        node = edge['node']
        catalog_markets = node.get('markets', {}).get('edges', [])
        publication = node.get('publication')
        
        if not publication:
            continue
        
        # Check if this catalog is linked to any of our target markets
        for market_edge in catalog_markets:
            market_id = market_edge['node']['id']
            market_name = market_edge['node']['name']
            if market_id in market_ids_set:
                pub_id = publication['id']
                catalog_publication_ids.append(pub_id)
                logger.info(f"📦 Found catalog publication for market '{market_name}': {pub_id}")
                break  # Each catalog only needs to be added once
    
    logger.info(f"📦 Resolved {len(catalog_publication_ids)} catalog publication IDs for {len(market_ids)} selected markets")
    return catalog_publication_ids

def get_all_publication_ids(shop_domain=None, access_token=None):
    """Get ALL available publication IDs for auto-publishing products (cached per instance)."""
    _sd_key, _ = _resolve_credentials(shop_domain, access_token)
    return _store_config_cached(
        ("all_publications", _sd_key),
        STORE_CONFIG_TTL_SECONDS,
        lambda: _fetch_all_publication_ids(shop_domain, access_token),
    )


def _fetch_all_publication_ids(shop_domain=None, access_token=None):
    query = """
    query getAllPublications {
        publications(first: 50) {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """
    
    result = execute_graphql_query(query, shop_domain=shop_domain, access_token=access_token)
    if not result or not result.get('data'):
        logger.error("Failed to fetch publications for auto-publishing")
        return []
    
    publication_ids = []
    for edge in result['data']['publications']['edges']:
        pub_id = edge['node']['id']
        pub_name = edge['node']['name']
        publication_ids.append(pub_id)
        logger.info(f"📡 Found publication: {pub_name} ({pub_id})")
    
    logger.warning(f"📡 AUTO-PUBLISH: Found {len(publication_ids)} total publications")
    return publication_ids

def _ensure_product_faq_metafield(product_gid, faq_items, shop_domain=None, access_token=None):
    """Write and read back the product FAQ metafield.

    Product creation can report success even when an individual metafield was
    rejected. Reading the value back makes the publishing result explicit and
    retries the isolated content metafield once when needed.
    """
    expected = json.dumps(faq_items, ensure_ascii=False, separators=(",", ":"))
    query = """
    query verifyProductFaq($id: ID!) {
      product(id: $id) {
        metafield(namespace: "custom", key: "product_faq") {
          id
          type
          value
        }
      }
    }
    """

    def read_matches():
        result = execute_graphql_query(
            query, {"id": product_gid}, shop_domain=shop_domain, access_token=access_token
        )
        metafield = (((result or {}).get("data") or {}).get("product") or {}).get("metafield")
        if not metafield:
            return False
        try:
            return json.loads(metafield.get("value") or "null") == faq_items
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

    if read_matches():
        return True

    mutation = """
    mutation ensureProductFaq($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields { id namespace key type }
        userErrors { field message }
      }
    }
    """
    result = execute_graphql_query(
        mutation,
        {"metafields": [{
            "ownerId": product_gid,
            "namespace": "custom",
            "key": "product_faq",
            "type": "json",
            "value": expected,
        }]},
        shop_domain=shop_domain,
        access_token=access_token,
    )
    errors = (
        (((result or {}).get("data") or {}).get("metafieldsSet") or {}).get("userErrors")
        or []
    )
    if errors:
        logger.error("Product FAQ metafield retry was rejected: %s", errors)
        return False
    return read_matches()


def create_product_with_graphql(metadata, filename, publishing_settings=None, image_paths=None,
                                shop_domain=None, access_token=None, use_main_image_per_variant=True):
    """Create a complete product with variants, tags, collections, and publishing using GraphQL.
    When use_main_image_per_variant is True, the first product image is assigned to all variant thumbnails."""
    
    # No fallback identity. A product must never be published under a title
    # derived from a filename or a generic placeholder - that is how junk
    # listings reach the storefront unnoticed. Missing AI copy fails the job.
    title = " ".join(str(metadata.get('title') or '').split())
    if not title:
        raise RuntimeError(
            f"Refusing to create a product for {filename}: the AI returned no title. "
            "Nothing was published."
        )
    if not str(metadata.get('description') or '').strip():
        raise RuntimeError(
            f"Refusing to create a product for {filename}: the AI returned no description. "
            "Nothing was published."
        )
    
    logger.info("Using AI-generated title for %s: '%s'", filename, title)
    description = metadata['description']
    tags = metadata.get('tags', [])
    collections = metadata.get('collections', [])
    if isinstance(collections, str):
        collections = [c.strip() for c in collections.split(',') if c.strip()]
    # Drop colour-only collections the palette doesn't support (catches AI picks
    # that arrive already-resolved, e.g. a colourful print mis-filed as B&W).
    _mf = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    _palette_colors = _collection_keyword_tokens(_mf.get('palette'), _mf.get('color')) & _COLOR_TOKENS
    collections = [c for c in collections if _colour_only_collection_ok(c, _palette_colors)]
    metadata['collections'] = collections
    # Safety net: every live publish funnels through here, so if no collections
    # arrived from upstream resolution, fetch the store's collections and
    # keyword-match now. Guarantees the product is categorised and the body can
    # link to real collections, regardless of which publish path ran.
    if not collections and not metadata.get('_collections_disabled'):
        try:
            _available = get_collections(shop_domain=shop_domain, access_token=access_token)
            collections = _auto_match_collections_local(metadata, _available)
            metadata['collections'] = collections
            logger.warning(
                "create_product: no upstream collections; store has %d, auto-matched=%s",
                len(_available or []), collections,
            )
        except Exception as _collection_error:
            logger.warning("create_product: collection safety-net failed: %s", _collection_error)
    # Append an internal-linking sentence using the verified store collections
    # and their real Shopify handles. Improves internal linking / crawl depth
    # without hardcoding any subject.
    try:
        _handle_map = _get_collection_title_handle_map(
            shop_domain=shop_domain, access_token=access_token)
    except Exception as _handle_error:
        logger.warning("Could not load collection handles for internal links: %s", _handle_error)
        _handle_map = {}
    description = _append_collection_links(description, collections, _handle_map)
    product_type = metadata.get('product_type', 'Poster')
    vendor = metadata.get('vendor', 'Samila Home')
    handle = metadata.get('seo_url_handle', filename.lower().replace(' ', '-').replace('.', ''))
    
    # Get category taxonomy ID - prefer direct GID from manual selection
    category_id = metadata.get('category_gid', '')
    if not category_id:
        category = metadata.get('category', '')
        category_id = get_category_taxonomy_id(category)
    
    logger.warning(f"🚀 CREATING PRODUCT VIA GRAPHQL:")
    logger.warning(f"   📝 Title: '{title}'")
    logger.warning(f"   🏷️ Tags: {tags}")
    logger.warning(f"   📁 Collections: {collections}")
    logger.warning(f"   📡 Publishing: {publishing_settings}")
    logger.warning(f"   🗂️ Metafields Available: {bool(metadata.get('metafields'))}")
    if metadata.get('metafields'):
        logger.warning(f"   🗂️ Metafields Data: {list(metadata['metafields'].keys())}")
    logger.warning(f"   📋 Full metadata keys: {list(metadata.keys())}")
    
    # Resolve variants before productCreate so Shopify receives a real Size
    # option instead of retaining its default Title option.
    configured_variants = []
    if metadata:
        configured_variants = metadata.get('variants') or metadata.get('variants_data') or []
    configured_size_values = []
    for variant in configured_variants:
        if not isinstance(variant, dict):
            continue
        value = str(variant.get('title') or variant.get('size') or variant.get('name') or '').strip()
        if value and value not in configured_size_values:
            configured_size_values.append(value)

    # Step 1: Create the base product with CORRECT 2024 GraphQL schema
    # FIXED: Use 'input' parameter name, not 'product'
    create_mutation = """
    mutation productCreate($input: ProductInput!) {
        productCreate(input: $input) {
            product {
                id
                title
                handle
                status
                productType
                tags
                category {
                    id
                    fullName
                }
                seo {
                    description
                    title
                }
                metafields(first: 25) {
                    nodes {
                        namespace
                        key
                        value
                    }
                }
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    
    # SEO configuration
    seo_config = {}
    seo_title_value = _trim_text(metadata.get('seo_title') or title, 70)
    if metadata.get('meta_description') or seo_title_value:
        seo_config = {}
        if metadata.get('meta_description'):
            seo_config['description'] = metadata['meta_description']
        if seo_title_value:
            seo_config['title'] = seo_title_value
    
    # Prepare metafields from AI-generated metadata
    metafields_input = []
    if metadata.get('metafields'):
        metafields_data = metadata['metafields']
        logger.info(f"🎯 PREPARING METAFIELDS: {metafields_data}")
        
        try:
            ensure_product_metafield_definitions(
                metafields_data.keys(),
                shop_domain=shop_domain,
                access_token=access_token,
            )
        except Exception as definition_error:
            logger.warning("Metafield definition ensure failed (non-fatal): %s", definition_error)

        for key, value in metafields_data.items():
            if value and str(value).strip():  # Only add non-empty values
                metafield = {
                    'namespace': 'custom',  # Use 'custom' namespace as per Shopify best practices
                    'key': key.lower().replace(' ', '_').replace('-', '_'),  # Ensure valid key format
                    'type': 'single_line_text_field',
                    'value': str(value).strip()
                }
                metafields_input.append(metafield)
                logger.warning(f"🎯 METAFIELD ADDED: {key} = {value} (namespace: custom, key: {metafield['key']})")

    short_description = str(metadata.get('short_description') or '').strip()
    if not short_description:
        plain_description = re.sub(r'<[^>]+>', ' ', str(description or ''))
        plain_description = re.sub(r'\s+', ' ', plain_description).strip()
        sentences = re.split(r'(?<=[.!?])\s+', plain_description)
        selected = []
        for sentence in sentences:
            candidate = ' '.join(selected + [sentence]).strip()
            if len(candidate.split()) > 80:
                break
            if sentence.strip():
                selected.append(sentence.strip())
            if len(candidate.split()) >= 50:
                break
        short_description = ' '.join(selected).strip()
    if short_description:
        metafields_input.append({
            'namespace': 'custom',
            'key': 'short_description',
            'type': 'multi_line_text_field',
            'value': short_description,
        })

    faq_items = metadata.get('faq') or []
    if isinstance(faq_items, str):
        try:
            faq_items = json.loads(faq_items)
        except (TypeError, ValueError):
            faq_items = []
    if isinstance(faq_items, list):
        faq_items = [
            {'question': str(item.get('question') or '').strip(), 'answer': str(item.get('answer') or '').strip()}
            for item in faq_items[:10]
            if isinstance(item, dict) and str(item.get('question') or '').strip() and str(item.get('answer') or '').strip()
        ]
    else:
        faq_items = []
    if faq_items:
        metafields_input.append({
            'namespace': 'custom',
            'key': 'product_faq',
            'type': 'json',
            'value': json.dumps(faq_items, ensure_ascii=False),
        })

    try:
        ensure_product_metafield_definitions(
            ['short_description', 'product_faq'],
            shop_domain=shop_domain,
            access_token=access_token,
        )
    except Exception as definition_error:
        logger.warning("Content metafield definition ensure failed (non-fatal): %s", definition_error)
    
    # ------------------------------------------------------------------
    # Google Shopping metafields (namespace: mm-google-shopping)
    # These populate the "Google Shopping / ..." columns in Shopify exports
    # and feed the Google Sales Channel / Merchant Center.
    # ------------------------------------------------------------------
    # Google Shopping colour: primary colours (max 3, '/'-separated) from the
    # product palette. Size is intentionally NOT set here — it is a per-variant
    # attribute the Google channel maps automatically from the "Size" option.
    _gs_mf = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    _google_color = _format_google_color(
        _gs_mf.get('palette') or _gs_mf.get('color') or metadata.get('custom_label_2')
    )
    google_shopping_fields = {
        'google_product_category': metadata.get('google_product_category', ''),
        'gender': metadata.get('gender', ''),
        'age_group': metadata.get('age_group', ''),
        'condition': metadata.get('condition', ''),
        'color': _google_color,
        'custom_product': metadata.get('custom_product', ''),
        'custom_label_0': metadata.get('custom_label_0', ''),
        'custom_label_1': metadata.get('custom_label_1', ''),
        'custom_label_2': metadata.get('custom_label_2', ''),
        'custom_label_3': metadata.get('custom_label_3', ''),
        'custom_label_4': metadata.get('custom_label_4', ''),
    }
    
    # Fields that Shopify defines as boolean type in mm-google-shopping
    boolean_metafields = {'custom_product'}
    
    for key, value in google_shopping_fields.items():
        # Normalize booleans / mixed types to a clean string
        if isinstance(value, bool):
            str_value = 'true' if value else 'false'
        else:
            str_value = str(value).strip() if value else ''
        
        if str_value:
            # Determine the correct metafield type
            if key in boolean_metafields:
                # Shopify expects boolean type for custom_product
                # Normalize to lowercase 'true'/'false'
                bool_val = str_value.lower() in ('true', '1', 'yes')
                metafield = {
                    'namespace': 'mm-google-shopping',
                    'key': key,
                    'type': 'boolean',
                    'value': 'true' if bool_val else 'false'
                }
            else:
                metafield = {
                    'namespace': 'mm-google-shopping',
                    'key': key,
                    'type': 'single_line_text_field',
                    'value': str_value
                }
            metafields_input.append(metafield)
            logger.warning(f"🎯 GOOGLE SHOPPING METAFIELD ADDED: mm-google-shopping.{key} = {metafield['value']} (type: {metafield['type']})")
    
    logger.warning(f"📊 TOTAL METAFIELDS PREPARED: {len(metafields_input)} (custom + google-shopping)")
    
    discovery_recommendations = metadata.get("discovery_recommendations") or {}
    search_boosts = _sanitize_search_boost_terms(discovery_recommendations.get("search_boosts") or [], max_terms=8)
    discovery_metafields_input = []
    discovery_metafields = {
        ("shopify--discovery--product_recommendation", "related_products", "list.product_reference"): discovery_recommendations.get("related_products") or [],
        ("shopify--discovery--product_recommendation", "complementary_products", "list.product_reference"): discovery_recommendations.get("complementary_products") or [],
        ("shopify--discovery--product_search_boost", "queries", "list.single_line_text_field"): search_boosts,
    }
    for (namespace, key, metafield_type), value in discovery_metafields.items():
        if not value:
            continue
        discovery_metafields_input.append({
            "namespace": namespace,
            "key": key,
            "type": metafield_type,
            "value": json.dumps(value),
        })
        logger.warning("Discovery metafield added: %s.%s = %s", namespace, key, value)
    if discovery_recommendations.get("related_products") or discovery_recommendations.get("complementary_products"):
        discovery_metafields_input.append({
            "namespace": "shopify--discovery--product_recommendation",
            "key": "related_products_display",
            # Shopify's standard definition is a choice field. This shop accepts
            # "ahead" or "only manual"; "true" is rejected by productCreate.
            "type": "single_line_text_field",
            "value": "ahead",
        })

    # Compute base SKU for variants (manual pattern or auto from title)
    import time
    _unique_id = int(time.time() * 1000)
    sku_manual = publishing_settings and publishing_settings.get('sku_manual', False)
    manual_sku = (publishing_settings and (publishing_settings.get('manual_sku') or '').strip()) or ''
    if sku_manual and manual_sku:
        base_sku = f"{manual_sku}-{_unique_id}"
        logger.info(f"Using manual SKU pattern: {base_sku}")
    else:
        sku_base = title or f'POSTER-{_unique_id}'
        base_sku = ''.join(c.upper() if c.isalnum() else '-' for c in sku_base)[:20] + f'-{_unique_id}'
        logger.info(f"Using auto-generated SKU: {base_sku}")
    
    # Validate and sanitize input data
    # Remove None values and empty strings
    if not title or not title.strip():
        logger.error("Product title is required and cannot be empty")
        return None
    
    # Sanitize handle - remove invalid characters
    import re
    if handle:
        # Shopify handles: lowercase, alphanumeric, hyphens only
        handle = re.sub(r'[^a-z0-9\-]', '-', handle.lower())
        handle = re.sub(r'-+', '-', handle)  # Replace multiple hyphens with single
        handle = handle.strip('-')  # Remove leading/trailing hyphens
        if not handle:
            # Generate a safe handle from title
            handle = re.sub(r'[^a-z0-9\-]', '-', title.lower())[:50]
            handle = re.sub(r'-+', '-', handle).strip('-')
    
    # Filter out empty tags
    if tags:
        tags = [tag for tag in tags if tag and str(tag).strip()]
    
    product_input = {
        'title': title.strip(),
        'descriptionHtml': format_description_html(description),
        'productType': product_type if product_type else 'Poster',
        'vendor': vendor if vendor else 'Samila Home',
        'status': 'ACTIVE',
        'handle': handle
    }

    if configured_size_values:
        product_input['productOptions'] = [{
            'name': 'Size',
            'values': [{'name': value} for value in configured_size_values],
        }]
    
    # Only add tags if we have valid ones
    if tags and len(tags) > 0:
        product_input['tags'] = tags
    
    # Only add category if we have a valid category_id
    if category_id:
        product_input['category'] = category_id
    
    # Add metafields to product input if we have any
    if metafields_input:
        product_input['metafields'] = metafields_input
        logger.warning(f"🚀 SENDING {len(metafields_input)} METAFIELDS TO SHOPIFY:")
        for mf in metafields_input:
            logger.warning(f"   - {mf['key']}: {mf['value'][:50]}..." if len(str(mf['value'])) > 50 else f"   - {mf['key']}: {mf['value']}")
    else:
        logger.warning(f"⚠️ NO METAFIELDS TO SEND - metadata.metafields = {metadata.get('metafields')}")
    
    if seo_config:
        product_input['seo'] = seo_config
    
    # FIXED: Use 'input' key to match the mutation parameter name
    variables = {'input': product_input}
    post_create_metafields_input = []
    
    logger.info(f"Sending product to Shopify: title='{product_input['title']}', metafields={len(product_input.get('metafields', []))}, tags={len(product_input.get('tags', []))}")
    
    # Resolve shop credentials once for the entire product creation flow
    try:
        _sd, _at = _resolve_credentials(shop_domain, access_token)
    except ValueError as e:
        logger.error(f"Cannot create product: {e}")
        return None

    result = execute_graphql_query(create_mutation, variables, shop_domain=_sd, access_token=_at)
    
    # Check for GraphQL errors first
    if not result:
        logger.error("Product creation failed: No result from GraphQL query")
        logger.error("This usually means the request failed or timed out")
        return None
    
    # Log response summary (not full response — too large for production)
    logger.info(f"Product creation response received, checking for errors...")
    
    # Check for GraphQL-level errors
    if 'errors' in result:
        logger.error(f"GraphQL errors in product creation:")
        for error in result['errors']:
            logger.error(f"  Error: {error.get('message', 'Unknown error')}")
            if 'locations' in error:
                logger.error(f"  Location: {error['locations']}")
            if 'path' in error:
                logger.error(f"  Path: {error['path']}")
        return None
    
    # Check for userErrors in the mutation response
    product_create_data = result.get('data', {}).get('productCreate', {})
    if product_create_data.get('userErrors'):
        user_errors = product_create_data['userErrors']
        logger.error(f"Product creation userErrors:")
        for error in user_errors:
            field = error.get('field', ['unknown'])
            message = error.get('message', 'unknown error')
            # field can be an array
            if isinstance(field, list):
                field_str = ' > '.join(field)
            else:
                field_str = str(field)
            logger.error(f"  - Field: {field_str}, Message: {message}")
        if product_input.get('metafields'):
            logger.warning("Retrying product creation without initial metafields; metafields will be written after creation")
            post_create_metafields_input = product_input.get('metafields') or []
            retry_input = dict(product_input)
            retry_input.pop('metafields', None)
            result = execute_graphql_query(create_mutation, {'input': retry_input}, shop_domain=_sd, access_token=_at)
            if not result or 'errors' in result:
                logger.error("Product creation retry without metafields failed: %s", result)
                return None
            product_create_data = result.get('data', {}).get('productCreate', {})
            retry_errors = product_create_data.get('userErrors') or []
            if retry_errors:
                logger.error("Product creation retry userErrors: %s", retry_errors)
                return None
        else:
            return None
    
    # Check if product was created
    if not product_create_data.get('product'):
        logger.error(f"Product creation failed: No product in response")
        logger.error(f"Response structure: {list(result.keys())}")
        if 'data' in result:
            logger.error(f"Data keys: {list(result['data'].keys())}")
            if 'productCreate' in result['data']:
                logger.error(f"productCreate keys: {list(result['data']['productCreate'].keys())}")
        return None
    
    product = result['data']['productCreate']['product']
    product_gid = product['id']
    
    logger.warning(f"✅ PRODUCT CREATED: {product_gid}")
    logger.warning(f"✅ FINAL PRODUCT TITLE: {product.get('title', 'NONE')}")
    logger.warning(f"✅ TAGS APPLIED: {product.get('tags', 'NONE')}")
    
    if post_create_metafields_input:
        try:
            owner_metafields = [
                {
                    "ownerId": product_gid,
                    "namespace": metafield["namespace"],
                    "key": metafield["key"],
                    "type": metafield["type"],
                    "value": metafield["value"],
                }
                for metafield in post_create_metafields_input
            ]
            post_create_mutation = """
            mutation setPostCreateMetafields($metafields: [MetafieldsSetInput!]!) {
              metafieldsSet(metafields: $metafields) {
                metafields {
                  id
                  namespace
                  key
                }
                userErrors {
                  field
                  message
                }
              }
            }
            """
            post_create_result = execute_graphql_query(
                post_create_mutation,
                {"metafields": owner_metafields},
                shop_domain=_sd,
                access_token=_at,
            )
            post_create_errors = (
                ((post_create_result or {}).get("data") or {})
                .get("metafieldsSet", {})
                .get("userErrors")
                or []
            )
            if post_create_errors:
                logger.warning("Post-create product metafields rejected: %s", post_create_errors)
            else:
                logger.info("Post-create product metafields set: %s", len(owner_metafields))
        except Exception as post_create_error:
            logger.warning("Post-create product metafield write failed (non-fatal): %s", post_create_error)

    if faq_items:
        try:
            returned_faq = next(
                (
                    node.get('value')
                    for node in ((product.get('metafields') or {}).get('nodes') or [])
                    if node.get('namespace') == 'custom' and node.get('key') == 'product_faq'
                ),
                None,
            )
            try:
                faq_verified = json.loads(returned_faq) == faq_items if returned_faq else False
            except (TypeError, ValueError, json.JSONDecodeError):
                faq_verified = False
            if not faq_verified:
                faq_verified = _ensure_product_faq_metafield(
                    product_gid,
                    faq_items,
                    shop_domain=_sd,
                    access_token=_at,
                )
        except Exception as faq_verify_error:
            faq_verified = False
            logger.error("Product FAQ metafield verification failed: %s", faq_verify_error)
        product["productFaqMetafieldVerified"] = faq_verified
        if faq_verified:
            logger.info("Product FAQ metafield verified after Shopify readback")
        else:
            logger.error(
                "Product was created, but custom.product_faq could not be verified: %s",
                product_gid,
            )

    if discovery_metafields_input:
        discovery_mutation = """
        mutation setDiscoveryMetafields($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields {
              id
              namespace
              key
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        discovery_successes = 0
        for metafield in discovery_metafields_input:
            owner_metafield = {
                "ownerId": product_gid,
                "namespace": metafield["namespace"],
                "key": metafield["key"],
                "type": metafield["type"],
                "value": metafield["value"],
            }
            attempts = [owner_metafield]
            if owner_metafield["key"] == "related_products_display":
                attempts.append({
                    **owner_metafield,
                    "type": "boolean",
                    "value": "true",
                })
            if owner_metafield["namespace"] == "shopify--discovery--product_search_boost" and owner_metafield["key"] == "queries":
                try:
                    terms = json.loads(owner_metafield["value"])
                except (TypeError, json.JSONDecodeError):
                    terms = []
                terms = _sanitize_search_boost_terms(terms, max_terms=8)
                if isinstance(terms, list) and terms:
                    configured_type = get_product_metafield_definition_type(
                        owner_metafield["namespace"],
                        owner_metafield["key"],
                        shop_domain=_sd,
                        access_token=_at,
                    )
                    if configured_type and configured_type != owner_metafield["type"]:
                        configured_value = (
                            "; ".join(str(term) for term in terms if str(term).strip())
                            if configured_type == "single_line_text_field"
                            else json.dumps(terms)
                        )
                        attempts.insert(0, {
                            **owner_metafield,
                            "type": configured_type,
                            "value": configured_value,
                        })
                    attempts.append({
                        **owner_metafield,
                        "type": "json",
                        "value": json.dumps(terms),
                    })
                    attempts.append({
                        **owner_metafield,
                        "type": "single_line_text_field",
                        "value": "; ".join(str(term) for term in terms if str(term).strip()),
                    })
            for attempt in attempts:
                try:
                    discovery_result = execute_graphql_query(
                        discovery_mutation,
                        {"metafields": [attempt]},
                        shop_domain=_sd,
                        access_token=_at,
                    )
                    discovery_errors = (
                        ((discovery_result or {}).get("data") or {})
                        .get("metafieldsSet", {})
                        .get("userErrors")
                        or []
                    )
                    if discovery_errors:
                        logger.warning(
                            "Discovery metafield rejected after product creation: %s.%s type=%s value=%s errors=%s",
                            attempt["namespace"],
                            attempt["key"],
                            attempt["type"],
                            attempt["value"],
                            discovery_errors,
                        )
                        continue
                    logger.info(
                        "Discovery metafield set after product creation: %s.%s",
                        attempt["namespace"],
                        attempt["key"],
                    )
                    discovery_successes += 1
                    break
                except Exception as discovery_error:
                    logger.warning(
                        "Discovery metafield write failed after product creation: %s.%s type=%s error=%s",
                        attempt["namespace"],
                        attempt["key"],
                        attempt["type"],
                        discovery_error,
                    )
            else:
                logger.warning(
                    "Discovery metafield could not be set after all attempts: %s.%s",
                    owner_metafield["namespace"],
                    owner_metafield["key"],
                )
        logger.info("Discovery metafields set after product creation: %s/%s", discovery_successes, len(discovery_metafields_input))

    # Log metafields creation result
    if product.get('metafields', {}).get('nodes'):
        metafields = product['metafields']['nodes']
        logger.warning(f"✅ METAFIELDS CREATED SUCCESSFULLY: {len(metafields)}")
        for mf in metafields:
            logger.warning(f"   ✅ {mf['key']}: {mf['value']}")
    else:
        logger.warning(f"❌ NO METAFIELDS RETURNED FROM SHOPIFY - Check if they were sent correctly")
        logger.warning(f"❌ GraphQL Response metafields section: {product.get('metafields', 'MISSING')}")
    
    # Step 2: Add product to collections. Size options were created atomically
    # with the product so variant rows and sales-channel feeds expose size.
    if collections:
        collection_success = add_to_collections_graphql(product_gid, collections, shop_domain=_sd, access_token=_at)
        logger.info(f"✅ Collections result: {collection_success}")
    
    # Step 4: Create variants - only if user provided them
    user_variants = configured_variants or None
    logger.warning(f"🎯 VARIANT EXTRACTION START - metadata keys: {list(metadata.keys()) if metadata else 'NO METADATA'}")
    
    if metadata:
        # Check multiple possible keys where variants might be stored
        variants_from_variants = metadata.get('variants')
        variants_from_variants_data = metadata.get('variants_data')
        
        logger.warning(f"🎯 VARIANT DATA SEARCH:")
        logger.warning(f"   - metadata.get('variants'): {variants_from_variants}")
        logger.warning(f"   - metadata.get('variants_data'): {variants_from_variants_data}")
        logger.warning(f"   - variants type: {type(variants_from_variants)}")
        logger.warning(f"   - variants_data type: {type(variants_from_variants_data)}")
        
        # Use whichever has data
        if variants_from_variants and len(variants_from_variants) > 0:
            user_variants = variants_from_variants
            logger.warning(f"🎯 USING variants key: {len(user_variants)} variants")
        elif variants_from_variants_data and len(variants_from_variants_data) > 0:
            user_variants = variants_from_variants_data
            logger.warning(f"🎯 USING variants_data key: {len(user_variants)} variants")
        else:
            logger.info(f"ℹ️ No variants found in metadata - product will use Shopify's default single variant")
    else:
        logger.warning(f"⚠️ METADATA IS NONE - product will use Shopify's default single variant")
    
    # Only create custom variants if user provided them
    if user_variants and len(user_variants) > 0:
        logger.warning(f"🎯 CREATING {len(user_variants)} USER-DEFINED VARIANTS")
        default_inventory_policy = (publishing_settings or {}).get("inventory_policy")
        if default_inventory_policy:
            for variant in user_variants:
                if isinstance(variant, dict) and not variant.get("inventory_policy"):
                    variant["inventory_policy"] = default_inventory_policy
        variants_success = create_variants_graphql(product_gid, user_variants, shop_domain=_sd, access_token=_at, sku_base=base_sku)
        logger.info(f"✅ Variants result: {variants_success}")
    else:
        logger.info(f"ℹ️ No custom variants to create - using Shopify's default single variant")
        variants_success = True  # No variants is valid
        # Set SKU on the default single variant
        set_variants_sku_graphql(product_gid, [base_sku], shop_domain=_sd, access_token=_at)
    
    # Step 5: Add images to product if provided
    # Handle both single image_path (backward compatibility) and list of image_paths
    image_path_list = []
    if image_paths:
        if isinstance(image_paths, list):
            image_path_list = image_paths
        elif isinstance(image_paths, str):
            image_path_list = [image_paths]  # Single image for backward compatibility
    
    if image_path_list:
        logger.warning(f"🎯🔥 STEP 5: ADDING {len(image_path_list)} IMAGE(S) TO PRODUCT")
        logger.warning(f"🎯🔥 METADATA HAS TITLE: {bool(metadata.get('title'))}")
        logger.warning(f"🎯🔥 AI TITLE IS: '{metadata.get('title', 'NONE')}'")
        logger.warning(f"🎯🔥 SEO FILENAME: '{metadata.get('seo_filename', 'NONE')}'")
        logger.warning(f"🎯🔥 ALT TEXT: '{metadata.get('alt_text', 'NONE')}'")
        
        # Get SEO data from metadata for image optimization
        seo_filename_base = metadata.get('seo_filename', '')
        ai_alt_text = metadata.get('alt_text', '')
        product_title_for_seo = title  # Use the resolved product title
        
        # Upload all images — track result per index so we can retry failures
        image_results = [None] * len(image_path_list)

        def _contextual_alt_text(i):
            base_alt = (ai_alt_text or product_title_for_seo or "").strip()
            base_alt = base_alt.rstrip(" .,:;")
            context_by_index = [
                "",
                "in an alternate framed mockup",
                "in a styled room mockup",
                "in an additional product view",
            ]
            context = context_by_index[i] if i < len(context_by_index) else "in an additional product view"
            base_alt = re.sub(r'\s+', ' ', (base_alt or product_title_for_seo or "")).strip().rstrip(" .,:;")
            if i == 0 or not context:
                return _trim_text(base_alt, 125) if len(base_alt) > 125 else base_alt
            # Reserve room for the FULL context phrase so it is never half-clipped.
            suffix = f", {context}"
            room_for_base = 125 - len(suffix)
            if room_for_base < 20:
                # No sensible room for both — keep a complete base, drop the context.
                return _trim_text(base_alt, 125) if len(base_alt) > 125 else base_alt
            fitted_base = base_alt if len(base_alt) <= room_for_base else _trim_text(base_alt, room_for_base)
            fitted_base = fitted_base.rstrip(" .,:;")
            return f"{fitted_base}{suffix}"

        def _build_image_data(i, img_path):
            """Helper to build the image_data dict for index *i*."""
            if seo_filename_base:
                _seo = f"{seo_filename_base}-{i+1}" if len(image_path_list) > 1 else seo_filename_base
            else:
                _seo = None
            _alt = _contextual_alt_text(i)
            return {
                'image_path': img_path,
                'alt_text': _alt,
                'seo_filename': _seo,
                'product_title': product_title_for_seo
            }

        if os.environ.get('SHOPIFY_STAGED_IMAGE_UPLOADS', 'true').lower() in {'1', 'true', 'yes', 'on'}:
            image_results = add_product_images_staged_graphql(
                product_gid,
                [_build_image_data(i, path) for i, path in enumerate(image_path_list)],
                shop_domain=_sd,
                access_token=_at,
            )
        else:
            image_results = [None] * len(image_path_list)

        # Upload images SEQUENTIALLY. Concurrent uploads spawned nested threads
        # that each base64-encode ~1MB + do TLS at the same time; on the 0.1-CPU
        # free tier that thread/CPU burst (on top of the direct-runner and create
        # executor threads) starved the gunicorn worker so it couldn't answer
        # status/health checks, and Render restarted the box (~13 min). One image
        # at a time keeps the create step lean, matching the WP worker's profile.
        def _upload_one(i, img_path):
            image_data = _build_image_data(i, img_path)
            logger.info(f"🎯 SEO Image {i+1}: filename='{image_data['seo_filename']}', alt='{image_data['alt_text'][:50]}...'")
            return add_product_image_graphql(product_gid, image_data, shop_domain=_sd, access_token=_at)

        for i, p in enumerate(image_path_list):
            if image_results[i] is not None:
                continue
            if not os.path.exists(p):
                logger.warning(f"⚠️ Image file not found: {p}")
                continue
            image_id = _upload_one(i, p)
            if image_id:
                image_results[i] = image_id
                logger.info(f"✅ Image {i+1}/{len(image_path_list)} uploaded successfully: {image_id}")
            else:
                logger.warning(f"⚠️ Failed to upload image {i+1}/{len(image_path_list)}")

        # --- Retry any failed images once more before proceeding ---
        failed_indices = [i for i, r in enumerate(image_results) if r is None and os.path.exists(image_path_list[i])]
        if failed_indices:
            logger.warning(f"⚠️ {len(failed_indices)} image(s) failed on first pass — retrying…")
            for i in failed_indices:
                image_data = _build_image_data(i, image_path_list[i])
                logger.warning(f"🔄 RETRY image {i+1}/{len(image_path_list)}: {image_path_list[i]}")
                image_id = add_product_image_graphql(product_gid, image_data, shop_domain=_sd, access_token=_at)
                if image_id:
                    image_results[i] = image_id
                    logger.info(f"✅ RETRY image {i+1} succeeded: {image_id}")
                else:
                    logger.error(f"❌ RETRY image {i+1} failed again — skipping")

        # Collect successful IDs (preserving order)
        uploaded_image_ids = [r for r in image_results if r is not None]
        final_failed = len(image_path_list) - len(uploaded_image_ids)
        if final_failed > 0:
            logger.error(
                f"❌ {final_failed} image(s) still missing after retry. "
                f"Proceeding with {len(uploaded_image_ids)}/{len(image_path_list)} images."
            )

        # Assign first image to all variants only when user opted in (Ready Framed / CSV behaviour)
        if use_main_image_per_variant and uploaded_image_ids:
            first_image_id = uploaded_image_ids[0]
            if str(first_image_id).startswith('gid://'):
                assign_success = assign_media_to_all_variants_graphql(
                    product_gid, first_image_id, shop_domain=_sd, access_token=_at
                )
            else:
                assign_success = assign_image_to_all_variants(product_gid, first_image_id, shop_domain=_sd, access_token=_at)
            if assign_success:
                logger.info(f"✅ Assigned first image to all product variants")
            else:
                logger.warning(f"⚠️ Failed to assign image to variants")
        elif not use_main_image_per_variant and uploaded_image_ids:
            logger.info(f"Variant images: skipped (user chose not to apply main image to variants)")
    else:
        logger.warning(f"No images provided for product")
    
    # Step 6: Publish to selected publishing channels AND catalog markets
    logger.info(f"=== PUBLISHING CHECK ===")
    logger.info(f"  publishing_settings: {publishing_settings}")
    
    try:
        selected_channels = []
        selected_markets = []
        
        # Get selected channels and markets from publishing_settings
        if publishing_settings:
            selected_channels = publishing_settings.get('selected_channels', [])
            selected_markets = publishing_settings.get('selected_markets', [])
        
        logger.info(f"  selected publishing channels: {len(selected_channels) if isinstance(selected_channels, list) else 'N/A'}")
        logger.info(f"  selected catalogs (markets): {len(selected_markets) if isinstance(selected_markets, list) else 'N/A'}")
        
        # Build the full list of publication IDs to publish to
        all_publication_ids = []
        
        # Add directly selected publishing channels (these are already publication IDs)
        if selected_channels and isinstance(selected_channels, list):
            all_publication_ids.extend(selected_channels)
            logger.info(f"📡 Adding {len(selected_channels)} publishing channel(s)")
        
        if publishing_settings and publishing_settings.get('auto_publish_all_channels'):
            try:
                auto_pub_ids = get_all_publication_ids(shop_domain=_sd, access_token=_at)
                if auto_pub_ids:
                    all_publication_ids.extend(auto_pub_ids)
                    logger.info(f"Auto-publish flag added {len(auto_pub_ids)} publication(s)")
            except Exception as auto_publish_error:
                logger.error(f"Error auto-fetching publications: {auto_publish_error}")

        # Resolve market/catalog IDs to their catalog publication IDs
        if selected_markets and isinstance(selected_markets, list) and len(selected_markets) > 0:
            try:
                catalog_pub_ids = get_catalog_publication_ids_for_markets(selected_markets, shop_domain=_sd, access_token=_at)
                if catalog_pub_ids:
                    all_publication_ids.extend(catalog_pub_ids)
                    logger.info(f"📦 Adding {len(catalog_pub_ids)} catalog publication(s) for {len(selected_markets)} market(s)")
                else:
                    logger.warning(f"📦 Could not resolve catalog publications for selected markets")
            except Exception as catalog_error:
                logger.error(f"📦 Error resolving catalog publications: {catalog_error}")
        
        # Deduplicate publication IDs
        all_publication_ids = list(set(all_publication_ids))
        
        # FALLBACK: Auto-fetch all channels if no publication IDs were resolved
        # This handles both: (a) publishing_settings is None, and (b) publishing_settings
        # has empty lists (which happens when the frontend failed to load channels on page init)
        if not all_publication_ids:
            logger.warning(f"📡 NO PUBLICATION IDS RESOLVED - Auto-fetching ALL available channels")
            logger.warning(f"📡 (publishing_settings was: {publishing_settings})")
            try:
                all_channels = get_all_publication_ids(shop_domain=_sd, access_token=_at)
                if all_channels:
                    all_publication_ids = all_channels
                    logger.warning(f"📡 AUTO-FETCHED {len(all_publication_ids)} channels for publishing")
                else:
                    logger.error(f"📡 Failed to auto-fetch channels")
            except Exception as fetch_error:
                logger.error(f"📡 Error fetching channels: {fetch_error}")
        
        if all_publication_ids and len(all_publication_ids) > 0:
            logger.info(f"Publishing to {len(all_publication_ids)} total publication(s)")
            try:
                publish_success = publish_to_channels_graphql(product_gid, all_publication_ids, shop_domain=_sd, access_token=_at)
                if publish_success:
                    logger.info(f"Publishing result: SUCCESS")
                else:
                    logger.warning(f"Publishing result: FAILED - Check logs above for details")
                    logger.warning("Product was created but publishing failed - product is still available in Shopify admin")
            except Exception as publish_error:
                logger.error(f"CRITICAL EXCEPTION during publishing: {str(publish_error)}")
                import traceback
                logger.error(f"Publishing traceback: {traceback.format_exc()}")
                logger.warning("Product was created but publishing had an exception - product is still available in Shopify admin")
        else:
            if publishing_settings:
                logger.info(f"PUBLISHING: User chose not to publish to any channels or catalogs")
            else:
                logger.warning(f"PUBLISHING SKIPPED - No channels available")
    except Exception as e:
        logger.error(f"CRITICAL: Error in publishing step: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.warning("Product was created but publishing step had an error - product is still available in Shopify admin")
    
    # Build return data - include admin URL
    numeric_id = product_gid.split('/')[-1]
    
    return {
        'id': numeric_id,
        'gid': product_gid,
        'title': product['title'],
        'handle': product['handle'],
        'tags': product.get('tags', ''),
        'faq_metafield_verified': product.get('productFaqMetafieldVerified'),
        'status': 'success',
        'admin_url': f"https://{_sd}/admin/products/{numeric_id}"
    }

def get_category_taxonomy_id(category_name):
    """Map AI-generated category to official Shopify taxonomy ID"""
    # Strip "Manual: " prefix so manual category from form can be resolved
    if category_name and isinstance(category_name, str):
        category_name = category_name.strip()
        if category_name.lower().startswith('manual:'):
            category_name = category_name[7:].strip()
    
    if not category_name:
        return ''  # No category provided, let Shopify default or caller resolve

    category_lower = category_name.lower()

    # Official Shopify taxonomy mappings (common shorthand -> GID)
    category_mappings = {
        'home & garden > decor > artwork > posters, prints, & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters, prints, & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters in poster, prints & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'poster, prints & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'poster': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'print': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'prints': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'visual art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'wall art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'home & garden > decor > artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
    }

    # Find matching category (exact first, then partial)
    if category_lower in category_mappings:
        return category_mappings[category_lower]
    for key, taxonomy_id in category_mappings.items():
        if key in category_lower:
            return taxonomy_id

    # No match in hardcoded mappings - return empty so caller can try taxonomy lookup
    logger.warning(f"Category '{category_name}' not in hardcoded mappings - returning empty for taxonomy lookup")
    return ''

def get_store_primary_location(shop_domain=None, access_token=None):
    """Get the primary location ID from the store (simplified query without name field)"""
    try:
        cache_shop, _ = _resolve_credentials(shop_domain, access_token)
    except ValueError:
        cache_shop = str(shop_domain or '')
    cache_key = ('primary_location', cache_shop)
    now = time.monotonic()
    with _store_config_cache_lock:
        cached = _store_config_cache.get(cache_key)
        if cached and (now - cached[0]) < STORE_CONFIG_TTL_SECONDS:
            return cached[1]

    def _remember(location_id):
        with _store_config_cache_lock:
            _store_config_cache[cache_key] = (time.monotonic(), location_id)
        return location_id

    query = """
    {
        locations(first: 5) {
            edges {
                node {
                    id
                    isActive
                    isPrimary
                }
            }
        }
    }
    """
    
    result = execute_graphql_query(query, {}, shop_domain=shop_domain, access_token=access_token)
    
    if result and result.get('data', {}).get('locations'):
        locations = result['data']['locations']['edges']
        
        # First try to find primary location
        for location in locations:
            node = location['node']
            if node.get('isPrimary') and node.get('isActive'):
                logger.info(f"✅ Found primary location ID: {node['id']}")
                return _remember(node['id'])
        
        # Fallback to first active location
        for location in locations:
            node = location['node']
            if node.get('isActive'):
                logger.info(f"✅ Using first active location ID: {node['id']}")
                return _remember(node['id'])
    
    logger.error("❌ No active locations found")
    return None

def add_product_option_graphql(product_gid, option_name):
    """Add a product option (like Size) to an existing product and return option details"""
    mutation = """
    mutation productOptionsCreate($productId: ID!, $options: [OptionCreateInput!]!) {
        productOptionsCreate(productId: $productId, options: $options) {
            product {
                id
                options {
                    id
                    name
                    values
                }
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    
    variables = {
        'productId': product_gid,
        'options': [{'name': option_name}]
    }
    
    result = execute_graphql_query(mutation, variables)
    
    if result and result.get('data', {}).get('productOptionsCreate', {}).get('product'):
        product = result['data']['productOptionsCreate']['product']
        options = product['options']
        logger.info(f"✅ Added option '{option_name}' to product. Total options: {len(options)}")
        
        # Return the created option details for referencing in variants
        for option in options:
            if option['name'] == option_name:
                logger.info(f"✅ Found created option: ID={option['id']}, Name={option['name']}")
                return option
        return True
    else:
        if result:
            errors = result.get('data', {}).get('productOptionsCreate', {}).get('userErrors', [])
            logger.error(f"Failed to add option '{option_name}': {errors}")
        return False

def _load_collection_directory(shop_domain=None, access_token=None):
    """Return {'ids': {title: gid}, 'handles': {title: handle}}, cached per instance.

    The full collection list is identical across every listing in a batch, so
    fetching it every time was pure waste. Cached with a short TTL.
    """
    _sd_key, _ = _resolve_credentials(shop_domain, access_token)

    def _load():
        query = """
        query getCollectionsByName($first: Int!, $after: String) {
            collections(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        title
                        handle
                    }
                }
            }
        }
        """
        mapping = {'ids': {}, 'handles': {}}
        cursor = None
        # Paginate: stores with more than 250 collections used to lose every
        # collection past the first page, so those assignments silently failed.
        for _page in range(20):
            result = execute_graphql_query(
                query, {'first': 250, 'after': cursor},
                shop_domain=shop_domain, access_token=access_token,
            )
            if not result or not result.get('data'):
                return mapping or None
            connection = result['data'].get('collections') or {}
            for edge in connection.get('edges') or []:
                node = edge.get('node') or {}
                if node.get('title'):
                    mapping['ids'][node['title']] = node['id']
                    if node.get('handle'):
                        mapping['handles'][node['title']] = node['handle']
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        return mapping if mapping['ids'] else None

    return _store_config_cached(("collection_map", _sd_key), STORE_CONFIG_TTL_SECONDS, _load)


def _get_collection_title_id_map(shop_domain=None, access_token=None):
    """{collection title: collection GID} for the store."""
    entry = _load_collection_directory(shop_domain=shop_domain, access_token=access_token)
    return (entry or {}).get('ids') or {}


def _get_collection_title_handle_map(shop_domain=None, access_token=None):
    """{collection title: real URL handle} for the store.

    Handles must come from Shopify, never be guessed from the title: a store
    titled "Animal Prints | Animal Art" can live at /collections/animal-prints,
    so a slugified title produces a dead link in every description.
    """
    entry = _load_collection_directory(shop_domain=shop_domain, access_token=access_token)
    return (entry or {}).get('handles') or {}


def add_to_collections_graphql(product_gid, collection_names, shop_domain=None, access_token=None):
    """Add product to collections using GraphQL"""

    # Collection title -> GID map (cached per instance — identical across listings)
    collection_map = _get_collection_title_id_map(shop_domain=shop_domain, access_token=access_token)
    if not collection_map:
        return False

    # Find matching collections. Match exactly first, then case- and
    # punctuation-insensitively, so "japanese art" / "Japanese Art!" still hit
    # the real collection instead of being dropped.
    def _norm(value):
        return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())

    normalised_map = {}
    for title, gid in collection_map.items():
        normalised_map.setdefault(_norm(title), (title, gid))

    collection_ids = []
    seen_ids = set()
    for name in collection_names:
        match = None
        if name in collection_map:
            match = (name, collection_map[name])
        else:
            match = normalised_map.get(_norm(name))
        if match and match[1] not in seen_ids:
            seen_ids.add(match[1])
            collection_ids.append(match[1])
            logger.info(f"Found collection '{name}' -> '{match[0]}': {match[1]}")
        elif not match:
            logger.warning(f"Collection '{name}' not found in store")
    
    if not collection_ids:
        logger.warning("No valid collections found to add product to")
        return False
    
    # Add product to each collection
    success_count = 0
    for collection_id in collection_ids:
        mutation = """
        mutation collectionAddProducts($id: ID!, $productIds: [ID!]!) {
            collectionAddProducts(id: $id, productIds: $productIds) {
                collection {
                    id
                    title
                }
                userErrors {
                    field
                    message
                }
            }
        }
        """
        
        variables = {
            'id': collection_id,
            'productIds': [product_gid]
        }
        
        result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)
        if result and result.get('data', {}).get('collectionAddProducts', {}).get('collection'):
            success_count += 1
            collection_title = result['data']['collectionAddProducts']['collection']['title']
            logger.info(f"✅ Added product to collection: {collection_title}")
        else:
            logger.error(f"Failed to add product to collection: {collection_id}")
    
    return success_count > 0

def set_variants_sku_graphql(product_gid, sku_list, shop_domain=None, access_token=None):
    """Set SKU on existing product variants via productVariantsBulkUpdate (e.g. default single variant)."""
    if not product_gid or not sku_list:
        return True
    try:
        _sd, _at = _resolve_credentials(shop_domain, access_token)
    except ValueError:
        return False
    query = """
    query getProductVariants($id: ID!) {
        product(id: $id) {
            id
            variants(first: 100) {
                nodes { id }
            }
        }
    }
    """
    r = execute_graphql_query(query, {'id': product_gid}, shop_domain=_sd, access_token=_at)
    if not r or not r.get('data', {}).get('product', {}).get('variants', {}).get('nodes'):
        logger.warning("set_variants_sku: could not load product variants")
        return False
    nodes = r['data']['product']['variants']['nodes']
    if len(sku_list) < len(nodes):
        logger.warning(f"set_variants_sku: only {len(sku_list)} SKUs for {len(nodes)} variants, padding")
        base = sku_list[-1] if sku_list else "SKU"
        sku_list = list(sku_list) + [f"{base}-V{i+2}" for i in range(len(nodes) - len(sku_list))]
    mutation = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id }
            userErrors { field message }
        }
    }
    """
    variants_input = [{'id': n['id'], 'inventoryItem': {'sku': sku_list[i]}} for i, n in enumerate(nodes)]
    result = execute_graphql_query(mutation, {'productId': product_gid, 'variants': variants_input}, shop_domain=_sd, access_token=_at)
    if result and result.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors'):
        errs = result['data']['productVariantsBulkUpdate']['userErrors']
        if errs:
            logger.error(f"set_variants_sku userErrors: {errs}")
            return False
    logger.info(f"Set SKU on {len(nodes)} variant(s)")
    return True

def update_variant_skus_graphql(product_gid, variant_skus, shop_domain=None, access_token=None):
    """Set an exact SKU on named variants of one product.

    variant_skus is a list of {'id': variant_gid, 'sku': new_sku}. Unlike
    set_variants_sku_graphql this never invents or pads a SKU: only the
    variants passed in are touched, with the exact value given.
    """
    variant_skus = [v for v in (variant_skus or []) if v.get('id') and v.get('sku')]
    if not product_gid or not variant_skus:
        return {'success': False, 'error': 'No variants to update.'}
    try:
        _sd, _at = _resolve_credentials(shop_domain, access_token)
    except ValueError as exc:
        return {'success': False, 'error': str(exc)}
    mutation = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id }
            userErrors { field message }
        }
    }
    """
    variants_input = [
        {'id': item['id'], 'inventoryItem': {'sku': str(item['sku'])}}
        for item in variant_skus
    ]
    result = execute_graphql_query(
        mutation, {'productId': product_gid, 'variants': variants_input},
        shop_domain=_sd, access_token=_at,
    )
    if not result:
        return {'success': False, 'error': 'No response from Shopify.'}
    if result.get('errors'):
        return {'success': False, 'error': str(result['errors'])}
    payload = (result.get('data') or {}).get('productVariantsBulkUpdate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        return {'success': False, 'error': '; '.join(
            str(e.get('message') or e) for e in errors
        )}
    return {'success': True, 'updated': len(payload.get('productVariants') or [])}


def create_variants_graphql(product_gid, user_variants_data=None, shop_domain=None, access_token=None, sku_base=None):
    """Create product variants using GraphQL with proper bulk creation. Optional sku_base applies SKU pattern (e.g. PATTERN-V1, PATTERN-V2)."""
    
    logger.warning(f"🎯 CREATE_VARIANTS_GRAPHQL CALLED:")
    logger.warning(f"   - product_gid: {product_gid}")
    logger.warning(f"   - user_variants_data: {user_variants_data}")
    logger.warning(f"   - user_variants_data type: {type(user_variants_data)}")
    logger.warning(f"   - user_variants_data length: {len(user_variants_data) if user_variants_data else 0}")
    
    # Only create variants if user provided them - NO FALLBACK DEFAULTS
    # If no variants provided, Shopify will use its default single "Default Title" variant
    if not user_variants_data or len(user_variants_data) == 0:
        logger.info(f"ℹ️ No user variants provided - skipping variant creation")
        logger.info(f"ℹ️ Product will use Shopify's default single variant")
        return True  # Return success - no variants to create is valid
    
    variants_data = user_variants_data
    logger.warning(f"✅ USING USER-PROVIDED VARIANTS: {len(variants_data)} variants")
    for i, v in enumerate(variants_data):
        logger.warning(f"   Variant {i+1}: {v}")
    
    # Use productVariantsBulkCreate with correct 2024 schema
    mutation = """
    mutation productVariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!, $strategy: ProductVariantsBulkCreateStrategy!) {
        productVariantsBulkCreate(productId: $productId, variants: $variants, strategy: $strategy) {
            product {
                id
                title
            }
            productVariants {
                id
                title
                price
                selectedOptions {
                    name
                    value
                }
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    
    # Get store location for inventory management
    location_id = get_store_primary_location(shop_domain=shop_domain, access_token=access_token)
    
    # Prepare variants for bulk creation with correct 2024 schema
    variant_inputs = []
    for i, variant in enumerate(variants_data):
        # Ensure price is a string (Shopify Money type expects string format like "6.99")
        price_value = variant.get('price', '0')
        if not isinstance(price_value, str):
            price_value = str(price_value)
        
        # Get variant title - support both 'title' key and legacy 'size' key
        variant_title = variant.get('title') or variant.get('size') or variant.get('name') or 'Default'
        
        logger.info(f"📦 Processing variant: title='{variant_title}', price='{price_value}'")
        
        variant_input = {
            'price': price_value,
            'optionValues': [
                {
                    'name': variant_title,
                    'optionName': 'Size'
                }
            ]
        }
        inventory_policy = variant.get('inventory_policy') or variant.get('inventoryPolicy')
        if inventory_policy:
            variant_input['inventoryPolicy'] = str(inventory_policy).upper()
        
        # Apply SKU pattern (manual or auto) so it appears on the product page
        if sku_base:
            variant_sku = f"{sku_base}-V{i+1}" if len(variants_data) > 1 else sku_base
            variant_input['inventoryItem'] = {'sku': variant_sku, 'requiresShipping': True}
            logger.info(f"✅ Setting variant SKU: {variant_sku}")
        
        raw_weight = variant.get('weight_grams', variant.get('grams', variant.get('weight')))
        try:
            weight_grams = float(raw_weight) if raw_weight not in (None, '') else 0.0
        except (TypeError, ValueError):
            weight_grams = 0.0
        if weight_grams > 0:
            item = variant_input.setdefault('inventoryItem', {'requiresShipping': True})
            item['measurement'] = {'weight': {'value': weight_grams, 'unit': 'GRAMS'}}

        # Add inventory if location found - support both 'inventory_quantity' and legacy 'inventory' keys
        inventory_qty = variant.get('inventory_quantity') or variant.get('inventory') or 999
        if location_id and inventory_qty:
            # Ensure inventory quantity is an integer
            if not isinstance(inventory_qty, int):
                inventory_qty = int(inventory_qty) if inventory_qty else 999
            variant_input['inventoryQuantities'] = [
                {
                    'locationId': location_id,
                    'availableQuantity': inventory_qty
                }
            ]
            logger.info(f"✅ Setting inventory: {variant_title} = {inventory_qty} units")
        
        variant_inputs.append(variant_input)
    
    variables = {
        'productId': product_gid,
        'variants': variant_inputs,
        'strategy': 'REMOVE_STANDALONE_VARIANT'  # Key missing field!
    }
    
    result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)
    
    if result and result.get('data', {}).get('productVariantsBulkCreate', {}).get('productVariants'):
        created_variants = result['data']['productVariantsBulkCreate']['productVariants']
        logger.info(f"✅ Created {len(created_variants)} variants successfully")
        for variant in created_variants:
            logger.info(f"✅ Created variant: {variant['title']} - ${variant['price']}")
        return True
    else:
        if result:
            errors = result.get('data', {}).get('productVariantsBulkCreate', {}).get('userErrors', [])
            logger.error(f"Failed to create variants: {errors}")
            logger.error(f"Full result: {result}")
        else:
            logger.error("No result from GraphQL query")
        return False

def publish_to_channels_graphql(product_gid, selected_channels, shop_domain=None, access_token=None):
    """Publish product to selected publication IDs using publishablePublish mutation.
    
    This handles both publishing channels (Online Store, POS, etc.) and catalog publications
    (market catalogs for EU, UK, International, etc.) - they all use publication IDs.
    """
    
    # Validate inputs
    if not product_gid:
        logger.error("Cannot publish: product_gid is required")
        return False
    
    if not selected_channels:
        logger.warning("No publications selected for publishing - skipping")
        return False
    
    if not isinstance(selected_channels, list):
        logger.error(f"selected_channels must be a list, got {type(selected_channels)}")
        return False
    
    if len(selected_channels) == 0:
        logger.warning("Empty publications list - skipping publishing")
        return False

    selected_channels = list(dict.fromkeys(str(channel).strip() for channel in selected_channels if str(channel).strip()))
    bulk_mutation = """
    mutation publishToChannels($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable { availablePublicationsCount { count } }
        userErrors { field message }
      }
    }
    """
    try:
        bulk_result = execute_graphql_query(
            bulk_mutation,
            {'id': product_gid, 'input': [{'publicationId': channel_id} for channel_id in selected_channels]},
            shop_domain=shop_domain,
            access_token=access_token,
        )
        bulk_payload = (((bulk_result or {}).get('data') or {}).get('publishablePublish') or {})
        if bulk_payload.get('publishable') and not bulk_payload.get('userErrors'):
            logger.info("Published product to %s channel(s) in one mutation", len(selected_channels))
            return True
        logger.warning("Bulk publication returned errors; falling back to individual publication calls: %s", bulk_payload.get('userErrors'))
    except Exception as bulk_error:
        logger.warning("Bulk publication failed; falling back to individual calls: %s", bulk_error)
    
    logger.info(f"STARTING PUBLISHING - Product: {product_gid}, Publications: {selected_channels}")
    logger.info(f"Number of publications to publish to: {len(selected_channels)}")
    
    success_count = 0
    for channel_id in selected_channels:
        if not channel_id or not str(channel_id).strip():
            logger.warning(f"Skipping empty channel ID: {channel_id}")
            continue
        mutation = """
        mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
            publishablePublish(id: $id, input: $input) {
                publishable {
                    availablePublicationsCount {
                        count
                    }
                }
                userErrors {
                    field
                    message
                }
            }
        }
        """
        
        variables = {
            'id': product_gid,
            'input': [
                {
                    'publicationId': channel_id
                }
            ]
        }
        
        logger.info(f"Publishing product {product_gid} to channel {channel_id}")
        logger.info(f"Mutation variables: {variables}")
        
        try:
            result = execute_graphql_query(mutation, variables, shop_domain=shop_domain, access_token=access_token)
            
            # Log result summary (not full response)
            
            if not result:
                logger.error(f"CRITICAL: Failed to publish to channel {channel_id}: No result from GraphQL query")
                logger.error(f"This usually means execute_graphql_query returned None")
                continue
            
            # Check for GraphQL-level errors
            if 'errors' in result:
                logger.error(f"CRITICAL: GraphQL errors publishing to {channel_id}:")
                for error in result['errors']:
                    logger.error(f"  Error: {error.get('message', 'Unknown error')}")
                    if 'locations' in error:
                        logger.error(f"  Location: {error['locations']}")
                continue
            
            # Check for publishablePublish in response
            if result.get('data', {}).get('publishablePublish'):
                publish_result = result['data']['publishablePublish']
            
                # Check for userErrors first
                if publish_result.get('userErrors'):
                    errors = publish_result['userErrors']
                    logger.error(f"CRITICAL: Publishing userErrors for {channel_id}:")
                    for error in errors:
                        field = error.get('field', ['unknown'])
                        message = error.get('message', 'unknown error')
                        if isinstance(field, list):
                            field_str = ' > '.join(field)
                        else:
                            field_str = str(field)
                        logger.error(f"  - Field: {field_str}, Message: {message}")
                    continue
                
                # Check if publishable object exists (indicates success)
                if publish_result.get('publishable'):
                    success_count += 1
                    logger.info(f"SUCCESS: Published to channel: {channel_id}")
                else:
                    logger.error(f"CRITICAL: No publishable object returned for {channel_id}")
                    logger.error(f"Response structure: {list(publish_result.keys())}")
                    logger.error(f"Full publish_result: {publish_result}")
            else:
                logger.error(f"CRITICAL: Failed to publish to channel: {channel_id} - Invalid response structure")
                logger.error(f"Response keys: {list(result.keys())}")
                if 'data' in result:
                    logger.error(f"Data keys: {list(result['data'].keys())}")
                logger.error(f"Full result: {result}")
        except Exception as e:
            logger.error(f"Exception publishing to channel {channel_id}: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            continue
    
    logger.info(f"PUBLISHING COMPLETE - Success count: {success_count}/{len(selected_channels)}")
    if success_count == 0 and len(selected_channels) > 0:
        logger.warning("WARNING: Failed to publish to any channels, but this won't fail product creation")
    return success_count > 0

def add_product_images_staged_graphql(product_gid, images_data, shop_domain=None, access_token=None):
    """Stage product images directly with Shopify and attach them in one mutation.

    Returns a positional list of MediaImage GIDs. Any failure returns an all-None
    list so the existing REST uploader can take over without losing a listing.
    """
    results = [None] * len(images_data)
    valid = [
        (index, data)
        for index, data in enumerate(images_data)
        if data.get('image_path') and os.path.exists(data['image_path'])
    ]
    if not valid:
        return results
    mutation = """
    mutation stagedProductImages($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets { url resourceUrl parameters { name value } }
        userErrors { field message }
      }
    }
    """
    inputs = []
    prepared = []
    for index, data in valid:
        path = data['image_path']
        extension = os.path.splitext(path)[1].lower()
        mime = 'image/png' if extension == '.png' else 'image/jpeg'
        filename = data.get('seo_filename') or os.path.basename(path)
        if not os.path.splitext(filename)[1]:
            filename += '.png' if mime == 'image/png' else '.jpg'
        inputs.append({
            'filename': os.path.basename(filename),
            'mimeType': mime,
            'httpMethod': 'POST',
            'resource': 'PRODUCT_IMAGE',
        })
        prepared.append((index, data, mime, os.path.basename(filename)))
    try:
        response = execute_graphql_query(
            mutation, {'input': inputs}, shop_domain=shop_domain, access_token=access_token
        )
        payload = (((response or {}).get('data') or {}).get('stagedUploadsCreate') or {})
        targets = payload.get('stagedTargets') or []
        if payload.get('userErrors') or len(targets) != len(prepared):
            logger.warning("Staged image target creation failed: %s", payload.get('userErrors'))
            return results

        def _upload(position):
            index, data, mime, filename = prepared[position]
            target = targets[position]
            fields = {item['name']: item['value'] for item in target.get('parameters') or []}
            with open(data['image_path'], 'rb') as image_file:
                upload = requests.post(
                    target['url'],
                    data=fields,
                    files={'file': (filename, image_file, mime)},
                    timeout=120,
                )
            upload.raise_for_status()
            return position, index, target['resourceUrl']

        uploaded = []
        workers = min(2, len(prepared))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_upload, position) for position in range(len(prepared))]
            for future in as_completed(futures):
                uploaded.append(future.result())
        uploaded.sort()
        create_media = """
        mutation attachProductImages($productId: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $productId, media: $media) {
            media { id alt status }
            mediaUserErrors { field message }
          }
        }
        """
        media_input = []
        for position, index, resource_url in uploaded:
            media_input.append({
                'mediaContentType': 'IMAGE',
                'originalSource': resource_url,
                'alt': str(prepared[position][1].get('alt_text') or '')[:125],
            })
        attached = execute_graphql_query(
            create_media,
            {'productId': product_gid, 'media': media_input},
            shop_domain=shop_domain,
            access_token=access_token,
        )
        attach_payload = (((attached or {}).get('data') or {}).get('productCreateMedia') or {})
        media_nodes = attach_payload.get('media') or []
        if attach_payload.get('mediaUserErrors') or len(media_nodes) != len(uploaded):
            logger.warning("Staged images could not be attached: %s", attach_payload.get('mediaUserErrors'))
            return results
        for uploaded_item, media_node in zip(uploaded, media_nodes):
            results[uploaded_item[1]] = media_node.get('id')
        logger.info("Staged and attached %s product image(s) with two Shopify mutations", len(media_nodes))
        return results
    except Exception as staged_error:
        logger.warning("Staged image upload failed; using legacy uploader: %s", staged_error)
        return results


def add_product_image_graphql(product_gid, image_data, shop_domain=None, access_token=None):
    """Add image to product using REST API with base64 encoding (works on localhost and domain)"""
    
    try:
        # Read image file and encode as base64
        image_path = image_data.get('image_path')
        if not image_path or not os.path.exists(image_path):
            logger.error(f"Image file not found: {image_path}")
            return None
        
        with open(image_path, 'rb') as f:
            image_bytes = f.read()
        
        # Check image size (Shopify limit is 20MB)
        max_size = 20 * 1024 * 1024  # 20MB
        if len(image_bytes) > max_size:
            logger.error(f"Image too large: {len(image_bytes)} bytes (max {max_size})")
            return None
        
        # Encode to base64
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        # SEO-OPTIMIZED FILENAME: Use seo_filename from AI metadata if available
        original_filename = os.path.basename(image_path)
        seo_filename = image_data.get('seo_filename')
        product_title = image_data.get('product_title', '')
        
        # Determine the best filename for SEO
        if seo_filename:
            # Use AI-generated SEO filename
            # Ensure it has proper extension
            original_ext = os.path.splitext(original_filename)[1] or '.jpg'
            if not seo_filename.endswith(original_ext):
                filename = f"{seo_filename}{original_ext}"
            else:
                filename = seo_filename
            logger.info(f"🎯 SEO: Using AI-generated filename: {filename}")
        elif product_title:
            # Generate SEO filename from product title
            import re
            clean_name = re.sub(r'[^a-zA-Z0-9\s\-]', '', product_title.lower())
            clean_name = re.sub(r'\s+', '-', clean_name.strip())
            clean_name = re.sub(r'-+', '-', clean_name)[:60]  # Max 60 chars
            original_ext = os.path.splitext(original_filename)[1] or '.jpg'
            filename = f"{clean_name}{original_ext}"
            logger.info(f"🎯 SEO: Generated filename from title: {filename}")
        else:
            filename = original_filename
            logger.warning(f"⚠️ SEO: No SEO filename available, using original: {filename}")
        
        # SEO-OPTIMIZED ALT TEXT: Use AI-generated alt text or create from title
        alt_text = image_data.get('alt_text')
        if not alt_text and product_title:
            # Generate SEO alt text from product title (max 125 chars, word-safe)
            alt_text = _trim_text(product_title, 125)
            logger.info(f"🎯 SEO: Generated alt text from title: {alt_text}")
        elif not alt_text:
            alt_text = f'Product image - {filename}'
            logger.warning(f"⚠️ SEO: No alt text available, using fallback: {alt_text}")
        
        # Extract numeric product ID from GID
        product_id = product_gid.split('/')[-1]
        
        # Resolve credentials for REST API call
        _sd, _at = _resolve_credentials(shop_domain, access_token)
        
        # Use REST API endpoint (accepts base64 directly, works on localhost and domain)
        image_url = f"https://{_sd}/admin/api/2025-07/products/{product_id}/images.json"
        
        headers = get_graphql_headers(access_token=_at)  # Same headers work for REST API
        
        # REST API format for base64 image upload
        payload = {
            'image': {
                'attachment': image_base64,
                'filename': filename,
                'alt': alt_text
            }
        }
        
        logger.warning(f"🎯🖼️ ADDING IMAGE TO PRODUCT: {product_gid} (Product ID: {product_id})")
        logger.warning(f"🎯🖼️ IMAGE FILE: {image_path}")
        logger.warning(f"🎯🖼️ IMAGE SIZE: {len(image_bytes)} bytes")
        logger.warning(f"🎯🖼️ BASE64 SIZE: {len(image_base64)} chars")
        logger.warning(f"🎯🖼️ SEO FILENAME: {filename}")
        logger.warning(f"🎯🖼️ SEO ALT TEXT: {alt_text[:100]}..." if len(alt_text) > 100 else f"🎯🖼️ SEO ALT TEXT: {alt_text}")
        logger.warning(f"🎯🖼️ USING REST API: {image_url}")

        response = _retry_with_backoff(
            lambda: requests.post(image_url, headers=headers, json=payload, timeout=60),
            max_retries=3,
            base_delay=2.0,
            context=f"Image upload for product {product_id}",
        )

        if response is None:
            logger.error(f"CRITICAL: Image upload failed after all retries for product {product_id}")
            return None

        if response.status_code in [200, 201]:
            result = response.json()
            if 'image' in result:
                image_info = result['image']
                image_id = image_info.get('id')
                image_src = image_info.get('src')
                logger.info(f"✅ Added image to product via REST API: {image_id}")
                logger.info(f"✅ Image URL: {image_src}")
                return image_id  # Return actual image_id for variant assignment
            else:
                logger.error(f"❌ Invalid response structure: {result}")
        else:
            logger.error(f"CRITICAL: REST API image upload failed: {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")

        return None
        
    except Exception as e:
        logger.error(f"CRITICAL: Exception adding image to product: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None

def assign_media_to_all_variants_graphql(product_gid, media_id, shop_domain=None, access_token=None):
    query = """
    query productVariantIds($id: ID!) {
      product(id: $id) { variants(first: 250) { nodes { id } } }
    }
    """
    mutation = """
    mutation assignVariantMedia($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id }
        userErrors { field message }
      }
    }
    """
    try:
        found = execute_graphql_query(
            query, {'id': product_gid}, shop_domain=shop_domain, access_token=access_token
        )
        variants = (((found or {}).get('data') or {}).get('product') or {}).get('variants', {}).get('nodes') or []
        if not variants:
            return False
        updated = execute_graphql_query(
            mutation,
            {
                'productId': product_gid,
                'variants': [{'id': variant['id'], 'mediaId': media_id} for variant in variants],
            },
            shop_domain=shop_domain,
            access_token=access_token,
        )
        payload = (((updated or {}).get('data') or {}).get('productVariantsBulkUpdate') or {})
        if payload.get('userErrors'):
            logger.warning("Bulk variant media assignment failed: %s", payload['userErrors'])
            return False
        logger.info("Assigned primary image to %s variant(s) in one mutation", len(variants))
        return True
    except Exception as assign_error:
        logger.warning("Bulk variant media assignment failed: %s", assign_error)
        return False


def assign_image_to_all_variants(product_gid, image_id, shop_domain=None, access_token=None):
    """Assign an image to all product variants using REST API"""
    try:
        _sd, _at = _resolve_credentials(shop_domain, access_token)
        # Extract numeric product ID from GID
        product_id = product_gid.split('/')[-1]
        
        # Get all variants for this product using REST API (with retry)
        variants_url = f"https://{_sd}/admin/api/2025-07/products/{product_id}/variants.json"
        headers = get_graphql_headers(access_token=_at)  # Same headers work for REST API

        response = _retry_with_backoff(
            lambda: requests.get(variants_url, headers=headers, timeout=30),
            max_retries=3,
            base_delay=1.0,
            context=f"Fetch variants for product {product_id}",
        )

        if response is None or response.status_code != 200:
            status = response.status_code if response else "no response"
            logger.error(f"Failed to fetch variants: {status}")
            return False
        
        variants_data = response.json()
        variants = variants_data.get('variants', [])
        
        if not variants:
            logger.warning(f"No variants found for product {product_id}")
            return False
        
        logger.info(f"Found {len(variants)} variants to assign image to")
        
        # Update each variant to use the specified image
        success_count = 0
        for variant in variants:
            variant_id = variant['id']
            update_url = f"https://{_sd}/admin/api/2025-07/variants/{variant_id}.json"
            
            # Update variant with image_id
            payload = {
                'variant': {
                    'image_id': image_id
                }
            }
            
            update_response = _retry_with_backoff(
                lambda _url=update_url, _pl=payload: requests.put(_url, headers=headers, json=_pl, timeout=30),
                max_retries=3,
                base_delay=1.0,
                context=f"Assign image to variant {variant_id}",
            )

            if update_response and update_response.status_code in [200, 201]:
                success_count += 1
                logger.info(f"✅ Assigned image to variant {variant_id}")
            else:
                status = update_response.status_code if update_response else "no response"
                logger.warning(f"⚠️ Failed to assign image to variant {variant_id}: {status}")
        
        if success_count == len(variants):
            logger.info(f"✅ Successfully assigned image to all {len(variants)} variants")
            return True
        elif success_count > 0:
            logger.warning(f"⚠️ Assigned image to {success_count}/{len(variants)} variants")
            return True  # Partial success is still considered success
        else:
            logger.error(f"❌ Failed to assign image to any variants")
            return False
            
    except Exception as e:
        logger.error(f"Exception assigning image to variants: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    return False

def generate_image_url(image_path, metadata=None):
    """Generate a publicly accessible URL for the image that Shopify can access"""
    logger.warning(f"🎯🔥 GENERATE_IMAGE_URL CALLED: path={image_path}, has_metadata={bool(metadata)}")
    try:
        # Check if file exists
        if not os.path.exists(image_path):
            logger.error(f"Image file not found: {image_path}")
            return None
            
        # Get original filename for fallback
        original_filename = os.path.basename(image_path)
        
        # 🎯 CRITICAL FIX: Generate AI-based filename for Shopify
        if metadata:
            # Try to generate clean filename from AI metadata
            clean_filename = None
            
            # First try: Use seo_filename from AI
            if metadata.get('seo_filename'):
                clean_filename = metadata['seo_filename']
                logger.warning(f"🎯 USING AI SEO FILENAME: {clean_filename}")
                
            # Second try: Generate from AI title
            elif metadata.get('title'):
                ai_title = metadata['title']
                # Convert title to filename: lowercase, spaces to hyphens, remove special chars
                import re
                clean_filename = re.sub(r'[^a-zA-Z0-9\s\-]', '', ai_title.lower())
                clean_filename = re.sub(r'\s+', '-', clean_filename.strip())
                clean_filename = re.sub(r'-+', '-', clean_filename)  # Remove multiple hyphens
                logger.warning(f"🎯 GENERATED FILENAME FROM AI TITLE: '{ai_title}' -> '{clean_filename}'")
            
            # Add file extension if we have a clean filename
            if clean_filename:
                # Get extension from original file
                original_ext = os.path.splitext(original_filename)[1] or '.jpg'
                clean_filename = f"{clean_filename}{original_ext}"
                logger.warning(f"🎯 FINAL AI FILENAME: {clean_filename}")
                
                # 🎯 CRITICAL: Create a copy with the AI-generated filename so serve_file can find it
                import shutil
                target_dir = os.path.dirname(image_path)
                ai_filepath = os.path.join(target_dir, clean_filename)
                
                # Only create copy if it doesn't already exist
                if not os.path.exists(ai_filepath):
                    shutil.copy2(image_path, ai_filepath)
                    logger.warning(f"🎯 CREATED AI FILE COPY: {ai_filepath}")
                else:
                    logger.warning(f"🎯 AI FILE ALREADY EXISTS: {ai_filepath}")
                
                filename = clean_filename
            else:
                logger.warning(f"⚠️ NO AI FILENAME AVAILABLE - Using original: {original_filename}")
                filename = original_filename
        else:
            logger.warning(f"⚠️ NO METADATA PROVIDED - Using original filename: {original_filename}")
            filename = original_filename
        
        # Generate dynamic public URL using the serve_file endpoint  
        # Get the current domain from environment or use a default
        app_url = os.environ.get('APP_URL', 'http://localhost:5000')
        public_url = f"{app_url.rstrip('/')}/serve_file/{filename}"
        
        logger.warning(f"🎯 GENERATED PUBLIC URL: {public_url}")
        logger.warning(f"🎯 SHOPIFY WILL USE FILENAME: {filename}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to generate image URL for {image_path}: {str(e)}")
        return None
