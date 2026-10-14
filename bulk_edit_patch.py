"""Pure transformations for reviewed Shopify edits (no credentials or I/O)."""
import copy
import json


VARIANT_FIELDS = {
    'variant_sku': 'sku', 'variant_barcode': 'barcode', 'variant_price': 'price',
    'variant_compare_at_price': 'compare_at_price', 'variant_taxable': 'taxable',
    'variant_inventory_policy': 'inventory_policy', 'variant_weight': 'weight',
    'variant_weight_unit': 'weight_unit', 'variant_inventory_quantity': 'inventory_quantity',
    'variant_inventory_location_id': 'inventory_location_id',
    'variant_apply_inventory': 'apply_inventory',
}
PRODUCT_FIELDS = {
    'title', 'handle', 'status', 'body_html', 'tags', 'vendor', 'product_type',
    'product_category', 'category_gid', 'seo_title', 'seo_description',
    'collections', 'image_alt_text', 'google_product_category', 'gender', 'age_group',
    'condition', 'custom_product', 'category_attribute_picks',
    'metafield_color', 'metafield_theme', 'metafield_frame_style', 'metafield_condition',
    'metafield_decoration_material', 'metafield_artwork_frame_material',
} | {f'custom_label_{i}' for i in range(5)}


def rows(value, name):
    if value is None or value == '':
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{name} must contain valid JSON') from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f'{name} must be an array of objects')
    return copy.deepcopy(value)


def equivalent(left, right):
    if left is None:
        left = ''
    if right is None:
        right = ''
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return left == right
    return str(left) == str(right)


def build_product_patch(proposed, original, only_fields=None):
    """Drop untouched fields; enforce column scope before building mutations."""
    product_id = proposed.get('product_id') or proposed.get('id')
    if not product_id or product_id != (original.get('id') or original.get('product_id')):
        raise ValueError('The proposal must match its original Shopify product ID')
    scope = set(only_fields or proposed.get('_only_fields') or [])
    if 'product_category' in scope:
        scope.add('category_gid')
    allowed = lambda key: not scope or key in scope
    baseline = dict(original)
    baseline['product_type'] = original.get('product_type', original.get('type', ''))
    for identifier, mf in (original.get('metafields') or {}).items():
        namespace, _, key = identifier.partition('.')
        baseline[f'mf__{namespace}__{key}'] = (mf or {}).get('value', '')
    patch = {'product_id': product_id}
    for key, value in proposed.items():
        if (key in PRODUCT_FIELDS or key.startswith('mf__')) and allowed(key):
            if not equivalent(value, baseline.get(key, '')):
                patch[key] = copy.deepcopy(value)
                if key.startswith('mf__'):
                    type_key = key.replace('mf__', 'mf_type__', 1)
                    patch[type_key] = proposed.get(type_key, 'single_line_text_field')

    variant_scope = scope.intersection(VARIANT_FIELDS)
    if not scope or 'variants_json' in scope or variant_scope:
        previous = rows(original.get('variants'), 'Original variants')
        variants = rows(proposed.get('variants_json', previous), 'Variants')
        if variants and previous:
            for top_key, nested_key in VARIANT_FIELDS.items():
                if top_key in proposed and allowed(top_key):
                    before = original.get(top_key, previous[0].get(nested_key))
                    if not equivalent(proposed[top_key], before):
                        variants[0][nested_key] = proposed[top_key]
        by_id = {v.get('id'): v for v in previous if v.get('id')}
        changed = []
        for variant in variants:
            if not variant.get('id') and variant.get('variant_id'):
                variant['id'] = variant['variant_id']
            if variant.get('id') and variant['id'] not in by_id:
                raise ValueError('Variant does not belong to the original product; refresh and review again')
            old = by_id.get(variant.get('id'))
            if old is None:
                if scope and 'variants_json' not in scope:
                    continue
                changed.append(variant)
                continue
            fields = set(variant) - {'id', 'title', 'image_url', 'image_alt_text'}
            if scope and 'variants_json' not in scope:
                fields &= {VARIANT_FIELDS[f] for f in variant_scope}
            delta = {key: variant[key] for key in fields if not equivalent(variant[key], old.get(key))}
            # Location/quantity alone must never turn an inventory write on.
            inventory_keys = {'inventory_quantity', 'inventory_location_id', 'apply_inventory'}
            if inventory_keys.intersection(delta):
                enabled = str(variant.get('apply_inventory', '')).lower() in ('true', '1', 'yes', 'on')
                for key in inventory_keys:
                    delta.pop(key, None)
                if enabled:
                    for key in inventory_keys | {'inventory_item_id'}:
                        if key in variant:
                            delta[key] = variant[key]
            if 'weight' in delta or 'weight_unit' in delta:
                delta['weight'] = variant.get('weight', old.get('weight'))
                delta['weight_unit'] = variant.get('weight_unit', old.get('weight_unit'))
            if delta:
                changed.append({'id': variant['id'], **delta})
        mode = proposed.get('variant_mode', 'merge')
        if mode == 'replace' and (not scope or 'variants_json' in scope):
            patch['variant_mode'] = mode
            patch['original_variant_ids'] = list(by_id)
            # Replacement needs retained IDs even when their values are unchanged.
            included = {v.get('id') for v in changed}
            changed.extend({'id': v['id']} for v in variants if v.get('id') and v['id'] not in included)
        if changed or patch.get('variant_mode') == 'replace':
            patch['variants_json'] = json.dumps(changed)

    previous_media = rows(original.get('media'), 'Original media')
    if not scope or 'media_json' in scope or 'image_alt_text' in scope:
        media = rows(proposed.get('media_json', previous_media), 'Media')
        if 'image_alt_text' in patch:
            target = next((m for m in previous_media if m.get('url') == original.get('image_url')), None)
            if target is None:
                raise ValueError('Refresh the product before editing its main image alt text')
            media = [dict(m, alt=patch['image_alt_text']) if m.get('id') == target.get('id') else m for m in media]
        by_id = {m.get('id'): m for m in previous_media}
        if any(not m.get('id') or m['id'] not in by_id for m in media):
            raise ValueError('Media edits must target existing media on the original product')
        changes = [{'id': m['id'], 'alt': m.get('alt', '')} for m in media
                   if m.get('id') in by_id and not equivalent(m.get('alt'), by_id[m['id']].get('alt'))]
        if scope and 'media_json' not in scope:
            target_ids = {m.get('id') for m in previous_media if m.get('url') == original.get('image_url')}
            changes = [m for m in changes if m['id'] in target_ids]
        if changes:
            patch['media_json'] = json.dumps(changes)
        patch.pop('image_alt_text', None)
    if allowed('price_lists_json') and 'price_lists_json' in proposed:
        prices = rows(proposed['price_lists_json'], 'Market prices')
        if prices != rows(original.get('price_lists_json'), 'Original market prices'):
            patch['price_lists_json'] = json.dumps(prices)
    return patch
