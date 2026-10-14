"""Group-preserving Shopify CSV editing, independent of Flask and live APIs."""
import csv
import io
import json
from decimal import Decimal, InvalidOperation

ALIASES = {
    'Handle': 'URL handle', 'Body (HTML)': 'Description', 'Product Category': 'Product category',
    'SEO Title': 'SEO title', 'SEO Description': 'SEO description',
    'Image Src': 'Product image URL', 'Image Position': 'Image position',
    'Image Alt Text': 'Image alt text', 'Variant Image': 'Variant image URL',
    'Variant SKU': 'SKU', 'Variant Barcode': 'Barcodes', 'Variant Price': 'Price',
    'Variant Compare At Price': 'Compare-at price', 'Variant Inventory Qty': 'Inventory quantity',
    'Variant Inventory Policy': 'Continue selling when out of stock',
    'Variant Grams': 'Weight value (grams)', 'Variant Weight Unit': 'Weight unit for display',
    'Variant Taxable': 'Charge tax', 'Variant Requires Shipping': 'Requires shipping',
    **{f'Option{i} Name': f'Option{i} name' for i in range(1, 4)},
    **{f'Option{i} Value': f'Option{i} value' for i in range(1, 4)},
}
VARIANT_COLUMNS = {
    'variant_sku': 'Variant SKU', 'variant_barcode': 'Variant Barcode',
    'variant_price': 'Variant Price', 'variant_compare_at_price': 'Variant Compare At Price',
    'variant_inventory_quantity': 'Variant Inventory Qty', 'variant_inventory_policy': 'Variant Inventory Policy',
    'variant_taxable': 'Variant Taxable', 'variant_weight': 'Variant Grams',
    'variant_weight_unit': 'Variant Weight Unit',
}


def canonical_row(row):
    result = dict(row)
    for old, new in ALIASES.items():
        if old not in result and new in result:
            result[old] = result[new]
    return result


def group_rows(rows):
    groups = {}
    handle = ''
    for row in rows:
        handle = str(canonical_row(row).get('Handle') or '').strip() or handle
        if not handle:
            raise ValueError('The first CSV row must contain a product handle')
        groups.setdefault(handle, []).append(dict(row))
    return groups


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def transform_csv(data, column_map=None):
    column_map = DEFAULT_COLUMN_MAP if column_map is None else column_map
    original = data.get('original_csv') or {}
    headers = list(original.get('headers') or [])
    rows = original.get('rows') or []
    if not headers or not rows:
        raise ValueError('Invalid CSV data')
    groups = group_rows(rows)
    handles = list(groups)
    edits = {}
    for item in data.get('enhancements') or []:
        enhanced = item.get('enhanced') or {}
        index = item.get('originalIndex')
        try:
            index = int(index) if index is not None else -1
        except (TypeError, ValueError):
            index = -1
        handle = handles[index] if 0 <= index < len(handles) else enhanced.get('original_handle', enhanced.get('handle'))
        if handle not in groups or handle in edits:
            raise ValueError('Each enhancement must match a unique original CSV product')
        edits[handle] = enhanced

    def column(old):
        return old if old in headers else ALIASES.get(old) if ALIASES.get(old) in headers else old

    def put(row, old, value):
        key = column(old)
        if key not in headers:
            headers.append(key)
        if key == 'Continue selling when out of stock' and str(value).lower() in ('continue', 'deny'):
            value = str(value).lower() == 'continue'
        row[key] = text(value)

    replace = bool(data.get('variant_override_enabled'))
    definitions = data.get('variant_definitions') or []
    if replace:
        if not definitions:
            raise ValueError('Variant replacement requires at least one definition')
        names = set()
        for variant in definitions:
            name = str(variant.get('name') or '').strip()
            if not name or name in names:
                raise ValueError('Replacement variant names must be nonempty and unique')
            names.add(name)
            try:
                price = Decimal(str(variant.get('price', '')))
                quantity = Decimal(str(variant.get('quantity', 0)))
                if not price.is_finite() or price < 0 or not quantity.is_finite() or quantity != quantity.to_integral_value():
                    raise ValueError('Invalid variant price or quantity')
            except InvalidOperation as exc:
                raise ValueError('Variant price and quantity must be numeric') from exc

    output = []
    for original_handle, group in groups.items():
        enhanced = edits.get(original_handle)
        if enhanced is None:
            output.extend(group)
            continue
        first = dict(group[0])
        for key, value in (enhanced.get('source_columns') or {}).items():
            if key not in headers:
                headers.append(key)
            first[key] = text(value)
        # Source columns preserve arbitrary CSV edits; named fields override only
        # when explicitly present, including intentional blanks and zero values.
        mapped = {}
        for key, old in column_map:
            mapped.setdefault(key, []).append(old)
        for key, choices in mapped.items():
            if key not in enhanced:
                continue
            targets = [old for old in choices if column(old) in headers]
            if not targets:
                targets = [next((old for old in choices if 'product.metafields.custom.' in old), choices[0])]
            for old in targets:
                put(first, old, enhanced[key])
        for key, old in VARIANT_COLUMNS.items():
            if key in enhanced:
                put(first, old, enhanced[key])
        if 'status' in enhanced:
            put(first, 'Status', enhanced['status'])
        for key, value in enhanced.items():
            if key.startswith('mf__'):
                _, namespace, field = key.split('__', 2)
                identifier = f'product.metafields.{namespace}.{field}'
                existing = next((h for h in headers if h == identifier or f'({identifier})' in h), identifier)
                put(first, existing, value)
        next_handle = str(enhanced.get('handle', canonical_row(first).get('Handle', original_handle)) or '').strip()
        if not next_handle:
            raise ValueError('Product handles cannot be blank')
        put(first, 'Handle', next_handle)
        main_image = next((canonical_row(row).get('Image Src') for row in group if canonical_row(row).get('Image Src')), '')

        if replace:
            # Keep product-level values on the first row, but remove obsolete
            # option/variant values before generating exactly one replacement set.
            for key in list(first):
                old = next((a for a, b in ALIASES.items() if b == key), key)
                if old.startswith('Variant ') or old.startswith('Option'):
                    first[key] = ''
            rewritten = []
            for index, definition in enumerate(definitions):
                row = dict(first) if index == 0 else {}
                put(row, 'Handle', next_handle)
                put(row, 'Option1 Name', data.get('variant_option_name') or 'Size')
                put(row, 'Option1 Value', definition['name'])
                put(row, 'Variant Price', definition['price'])
                put(row, 'Variant Inventory Qty', definition.get('quantity', 0))
                put(row, 'Variant Inventory Policy', data.get('variant_inventory_policy', 'deny'))
                put(row, 'Variant Fulfillment Service', 'manual')
                put(row, 'Variant Requires Shipping', True)
                put(row, 'Variant Taxable', True)
                base_sku = enhanced.get('variant_sku', '')
                put(row, 'Variant SKU', definition.get('sku', f'{base_sku}-V{index + 1}' if base_sku and len(definitions) > 1 else base_sku))
                rewritten.append(row)
            # Preserve additional images as image-only rows instead of turning
            # old variant rows into another set of replacement variants.
            seen_images = {canonical_row(first).get('Image Src')}
            for previous in group:
                image = canonical_row(previous).get('Image Src')
                if not image or image in seen_images:
                    continue
                seen_images.add(image)
                row = {}
                put(row, 'Handle', next_handle)
                for name in ('Image Src', 'Image Position', 'Image Alt Text'):
                    value = canonical_row(previous).get(name)
                    if value is not None:
                        put(row, name, value)
                rewritten.append(row)
        else:
            rewritten = [first] + [dict(row) for row in group[1:]]
            for row in rewritten:
                put(row, 'Handle', next_handle)
        for row in rewritten:
            is_variant = bool(canonical_row(row).get('Option1 Value')) or row is rewritten[0]
            if is_variant and data.get('use_main_image_per_variant') and main_image:
                put(row, 'Variant Image', main_image)
        output.extend(rewritten)

    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(output)
    return stream.getvalue()


DEFAULT_COLUMN_MAP = [('title', 'Title'),
 ('body_html', 'Body (HTML)'),
 ('tags', 'Tags'),
 ('product_category', 'Product Category'),
 ('product_type', 'Type'),
 ('seo_title', 'SEO Title'),
 ('seo_description', 'SEO Description'),
 ('vendor', 'Vendor'),
 ('collections', 'Collection'),
 ('google_product_category', 'Google Shopping / Google Product Category'),
 ('gender', 'Google Shopping / Gender'),
 ('age_group', 'Google Shopping / Age Group'),
 ('condition', 'Google Shopping / Condition'),
 ('custom_product', 'Google Shopping / Custom Product'),
 ('custom_label_0', 'Google Shopping / Custom Label 0'),
 ('custom_label_1', 'Google Shopping / Custom Label 1'),
 ('custom_label_2', 'Google Shopping / Custom Label 2'),
 ('custom_label_3', 'Google Shopping / Custom Label 3'),
 ('custom_label_4', 'Google Shopping / Custom Label 4'),
 ('metafield_color', 'Metafield: custom.color [single_line_text_field]'),
 ('metafield_theme', 'Metafield: custom.theme [single_line_text_field]'),
 ('metafield_frame_style', 'Metafield: custom.frame_style [single_line_text_field]'),
 ('metafield_condition', 'Metafield: custom.condition [single_line_text_field]'),
 ('metafield_decoration_material', 'Metafield: custom.decoration_material [single_line_text_field]'),
 ('metafield_artwork_frame_material', 'Metafield: custom.artwork_frame_material [single_line_text_field]'),
 ('custom_product', 'Google: Custom Product (product.metafields.mm-google-shopping.custom_product)'),
 ('metafield_color', 'Color (product.metafields.custom.color)'),
 ('metafield_frame_style', 'Frame Style (product.metafields.custom.frame_style)'),
 ('metafield_theme', 'Theme (product.metafields.custom.theme)'),
 ('metafield_art_movement', 'Art movement (product.metafields.shopify.art-movement)'),
 ('metafield_art_style', 'Art style (product.metafields.shopify.art-style)'),
 ('metafield_artwork_authenticity', 'Artwork authenticity (product.metafields.shopify.artwork-authenticity)'),
 ('metafield_color', 'Color (product.metafields.shopify.color-pattern)'),
 ('metafield_frame_style', 'Frame style (product.metafields.shopify.frame-style)'),
 ('metafield_material', 'Material (product.metafields.shopify.material)'),
 ('metafield_orientation', 'Orientation (product.metafields.shopify.orientation)'),
 ('metafield_theme', 'Theme (product.metafields.shopify.theme)'),
 ('seo_title', 'Metafield: global.title_tag [single_line_text_field]'),
 ('seo_description', 'Metafield: global.description_tag [multi_line_text_field]'),
 ('mf__shopify--discovery--product_recommendation__complementary_products',
  'Complementary products '
  '(product.metafields.shopify--discovery--product_recommendation.complementary_products)'),
 ('mf__shopify--discovery--product_recommendation__related_products',
  'Related products (product.metafields.shopify--discovery--product_recommendation.related_products)'),
 ('mf__shopify--discovery--product_recommendation__related_products_display',
  'Related products settings '
  '(product.metafields.shopify--discovery--product_recommendation.related_products_display)'),
 ('mf__shopify--discovery--product_search_boost__queries',
  'Search product boosts (product.metafields.shopify--discovery--product_search_boost.queries)')]
