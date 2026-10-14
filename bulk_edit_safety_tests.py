"""Offline regression tests: Shopify responses are mocked; app startup is avoided."""
import json
import unittest
from unittest.mock import Mock, patch

from bulk_edit_patch import build_product_patch
import shopify_graphql as g

PID = 'gid://shopify/Product/1'
VID = 'gid://shopify/ProductVariant/1'


class ProductPatchTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            'id': PID, 'title': 'Old', 'vendor': 'Vendor', 'tags': 'old',
            'image_url': 'main.jpg', 'image_alt_text': 'Old alt',
            'variants': [{'id': VID, 'price': '10', 'sku': 'ONE', 'weight': 2,
                          'weight_unit': 'KILOGRAMS', 'inventory_quantity': 12,
                          'inventory_item_id': 'item'}],
            'media': [{'id': 'media', 'url': 'main.jpg', 'alt': 'Old alt'}],
        }

    def test_title_edit_does_not_resave_unrelated_data(self):
        proposed = dict(self.original, product_id=PID, title='New',
                        variants_json=json.dumps(self.original['variants']),
                        media_json=json.dumps(self.original['media']), price_lists_json='[]')
        self.assertEqual(build_product_patch(proposed, self.original), {'product_id': PID, 'title': 'New'})

    def test_column_scope_rejects_ai_side_effects(self):
        proposed = {'product_id': PID, 'title': 'New', 'mf__custom__x': 'AI',
                    'variants_json': '[{"price":"999"}]', 'media_json': '[{"id":"media","alt":"AI"}]',
                    'price_lists_json': '[{"price_list_id":"p","prices":[]}]',
                    'category_attribute_picks': {'color': 'red'}}
        self.assertEqual(build_product_patch(proposed, self.original, ['title']), {'product_id': PID, 'title': 'New'})

    def test_changed_variant_does_not_resave_price(self):
        variant = dict(self.original['variants'][0], sku='TWO')
        result = build_product_patch({'product_id': PID, 'variants_json': json.dumps([variant])}, self.original)
        self.assertEqual(json.loads(result['variants_json']), [{'id': VID, 'sku': 'TWO'}])

    def test_weight_patch_preserves_unit(self):
        variant = dict(self.original['variants'][0], weight=3)
        result = build_product_patch({'product_id': PID, 'variants_json': json.dumps([variant])}, self.original)
        self.assertEqual(json.loads(result['variants_json']), [{'id': VID, 'weight': 3, 'weight_unit': 'KILOGRAMS'}])

    def test_inventory_requires_explicit_opt_in(self):
        variant = dict(self.original['variants'][0], inventory_quantity=999, inventory_location_id='loc', apply_inventory=False)
        result = build_product_patch({'product_id': PID, 'variants_json': json.dumps([variant])}, self.original)
        self.assertNotIn('variants_json', result)
        variant['apply_inventory'] = True
        result = build_product_patch({'product_id': PID, 'variants_json': json.dumps([variant])}, self.original)
        self.assertEqual(json.loads(result['variants_json'])[0]['inventory_item_id'], 'item')

    def test_main_alt_targets_featured_media(self):
        result = build_product_patch({'product_id': PID, 'image_alt_text': ''}, self.original)
        self.assertEqual(json.loads(result['media_json']), [{'id': 'media', 'alt': ''}])

    def test_identity_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            build_product_patch({'product_id': 'wrong', 'title': 'New'}, self.original)

    def test_replace_retains_unchanged_variant_ids(self):
        result = build_product_patch({'product_id': PID, 'variant_mode': 'replace',
                                     'variants_json': json.dumps(self.original['variants'])}, self.original)
        self.assertEqual(json.loads(result['variants_json']), [{'id': VID}])
        self.assertEqual(result['original_variant_ids'], [VID])

    def test_metafield_type_and_clear_are_preserved(self):
        original = dict(self.original, metafields={'custom.x': {'value': 'before'}})
        result = build_product_patch({'product_id': PID, 'mf__custom__x': '',
                                     'mf_type__custom__x': 'multi_line_text_field'}, original)
        self.assertEqual(result['mf__custom__x'], '')
        self.assertEqual(result['mf_type__custom__x'], 'multi_line_text_field')


class MutationTests(unittest.TestCase):
    @patch.object(g, 'execute_graphql_query', return_value=None)
    def test_missing_responses_fail(self, api):
        results = [g._set_inventory_quantity('i', 'l', 1),
                   g._update_product_media_alt(PID, [{'id': 'm', 'alt': 'new'}]),
                   g.assign_media_to_product_variants(PID, 'm', [VID]),
                   g._update_price_list_prices([{'price_list_id': 'p', 'currency': 'USD',
                                                 'prices': [{'variant_id': VID, 'price': '8'}]}]),
                   g.update_product_seo_metadata(PID, {'mf__custom__x': 'new'})]
        self.assertTrue(all(not result['success'] for result in results))

    @patch.object(g, 'execute_graphql_query')
    def test_invalid_advanced_data_prevents_product_write(self, api):
        for value in ('{bad', '{}', '[null]'):
            self.assertFalse(g.update_product_seo_metadata(PID, {'title': 'New', 'variants_json': value})['success'])
        api.assert_not_called()

    @patch.object(g, 'execute_graphql_query')
    def test_invalid_weight_prevents_product_write(self, api):
        self.assertFalse(g.update_product_seo_metadata(PID, {'title': 'New', 'variants_json':
                         json.dumps([{'id': VID, 'weight': 'NaN', 'weight_unit': 'GRAMS'}])})['success'])
        api.assert_not_called()

    @patch.object(g, 'execute_graphql_query')
    def test_metafields_are_chunked(self, api):
        api.side_effect = lambda q, v, **kw: {'data': {'metafieldsSet': {'metafields': v['metafields'], 'userErrors': []}}}
        result = g.update_product_seo_metadata(PID, {f'mf__custom__key{i}': 'value' for i in range(26)})
        self.assertTrue(result['success'])
        self.assertEqual([len(c.args[1]['metafields']) for c in api.call_args_list], [25, 1])

    @patch.object(g, 'execute_graphql_query')
    def test_variants_are_chunked(self, api):
        api.side_effect = lambda q, v, **kw: {'data': {'productVariantsBulkUpdate': {'productVariants': v['variants'], 'userErrors': []}}}
        result = g.update_product_seo_metadata(PID, {'variants_json': json.dumps([{'id': str(i), 'price': '1'} for i in range(251)])})
        self.assertTrue(result['success'])
        self.assertEqual(result['variants_updated'], 251)
        self.assertEqual([len(c.args[1]['variants']) for c in api.call_args_list], [250, 1])

    @patch.object(g, 'execute_graphql_query')
    def test_later_chunk_failure_is_not_success(self, api):
        api.side_effect = [{'data': {'metafieldsSet': {'metafields': [{'id': str(i)} for i in range(25)], 'userErrors': []}}}, None]
        result = g.update_product_seo_metadata(PID, {f'mf__custom__key{i}': 'v' for i in range(26)})
        self.assertFalse(result['success'])
        self.assertIn('2/2', result['error'])

    @patch.object(g, 'execute_graphql_query')
    def test_blank_product_fields_are_written(self, api):
        api.return_value = {'data': {'productUpdate': {'product': {'id': PID}, 'userErrors': []}}}
        result = g.update_product_seo_metadata(PID, {'tags': '', 'vendor': '', 'body_html': '', 'seo_title': ''})
        self.assertTrue(result['success'])
        self.assertEqual(api.call_args.args[1]['product'], {'id': PID, 'tags': [], 'vendor': '', 'descriptionHtml': '', 'seo': {'title': ''}})

    def test_variant_clear_values(self):
        value = g._variant_update_input_from_data({'id': VID, 'sku': '', 'barcode': '', 'compare_at_price': '', 'taxable': False})
        self.assertEqual(value, {'id': VID, 'inventoryItem': {'sku': ''}, 'barcode': '', 'compareAtPrice': None, 'taxable': False})

    def test_new_variant_preserves_explicit_values(self):
        value = g._variant_create_input_from_edit({'sku': 'MY-SKU', 'barcode': '123', 'price': '10',
            'compare_at_price': '20', 'taxable': False, 'weight': 2, 'weight_unit': 'KILOGRAMS',
            'inventory_quantity': 0, 'inventory_location_id': 'chosen',
            'option1_name': 'Size', 'option1_value': 'A4', 'option2_name': 'Color', 'option2_value': 'Blue'})
        self.assertEqual(value['inventoryItem']['sku'], 'MY-SKU')
        self.assertEqual(value['inventoryItem']['measurement']['weight'], {'value': 2, 'unit': 'KILOGRAMS'})
        self.assertEqual(value['inventoryQuantities'], [{'locationId': 'chosen', 'availableQuantity': 0}])
        self.assertEqual(len(value['optionValues']), 2)
        self.assertEqual(value['compareAtPrice'], '20')
        self.assertEqual(value['barcode'], '123')
        self.assertFalse(value['taxable'])

    @patch.object(g, 'execute_graphql_query')
    def test_new_variant_requires_reviewed_default_replacement(self, api):
        api.return_value = {'data': {'product': {'hasOnlyDefaultVariant': True,
            'options': [{'name': 'Title'}], 'variants': {'nodes': [{'id': VID}]}}}}
        result = g.update_product_seo_metadata(PID, {'title': 'New', 'variants_json': json.dumps([
            {'option1_name': 'Title', 'option1_value': 'Another', 'price': '5'}])})
        self.assertFalse(result['success'])
        self.assertIn('confirmation', result['error'])
        self.assertEqual(api.call_count, 1)
        self.assertIn('query bulkVariantOptions', api.call_args.args[0])

    @patch.object(g, 'execute_graphql_query')
    def test_explicit_variant_append_uses_reviewed_input(self, api):
        api.side_effect = [
            {'data': {'product': {'hasOnlyDefaultVariant': False, 'options': [{'name': 'Size'}]}}},
            {'data': {'productVariantsBulkCreate': {'productVariants': [{'id': 'new'}], 'userErrors': []}}},
        ]
        result = g.update_product_seo_metadata(PID, {'variants_json': json.dumps([
            {'sku': 'EXACT', 'price': '12', 'option1_name': 'Size', 'option1_value': 'A3'}])})
        self.assertTrue(result['success'])
        self.assertEqual(result['variants_created'], 1)
        self.assertEqual(api.call_args.args[1]['variants'][0]['inventoryItem']['sku'], 'EXACT')

    @patch.object(g, 'execute_graphql_query')
    def test_empty_success_payload_is_failure(self, api):
        api.return_value = {'data': {'productUpdate': {'product': {}, 'userErrors': []}}}
        self.assertFalse(g.update_product_seo_metadata(PID, {'title': 'New'})['success'])

    @patch.object(g, 'execute_graphql_query')
    def test_metafield_clear_uses_delete(self, api):
        api.return_value = {'data': {'metafieldsDelete': {'deletedMetafields': [
            {'ownerId': PID, 'namespace': 'custom', 'key': 'x'}], 'userErrors': []}}}
        self.assertTrue(g.update_product_seo_metadata(PID, {'mf__custom__x': ''})['success'])
        self.assertIn('metafieldsDelete', api.call_args.args[0])

    @patch.object(g, '_load_collection_directory', return_value={'ids': {'Old': 'old', 'New': 'new'}, 'smart_ids': []})
    @patch.object(g, 'execute_graphql_query')
    def test_manual_membership_changes_use_ids(self, api, directory):
        api.side_effect = [
            {'data': {'product': {'collections': {'nodes': [{'id': 'old', 'ruleSet': None}], 'pageInfo': {'hasNextPage': False}}}}},
            {'data': {'productUpdate': {'product': {'id': PID}, 'userErrors': []}}},
        ]
        self.assertTrue(g.update_product_seo_metadata(PID, {'collections': 'New'})['success'])
        self.assertEqual(api.call_args.args[1]['product'], {'id': PID, 'collectionsToJoin': ['new'], 'collectionsToLeave': ['old']})

    @patch.object(g, '_load_collection_directory', return_value={'ids': {'Smart': 'smart'}, 'smart_ids': ['smart']})
    @patch.object(g, 'execute_graphql_query')
    def test_smart_membership_change_fails_before_write(self, api, directory):
        api.return_value = {'data': {'product': {'collections': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}
        self.assertFalse(g.update_product_seo_metadata(PID, {'title': 'New', 'collections': 'Smart'})['success'])
        self.assertEqual(api.call_count, 1)

    @patch.object(g, 'execute_graphql_query', return_value=None)
    def test_failed_catalogue_read_raises(self, api):
        with self.assertRaises(RuntimeError):
            g.list_products_for_seo_enhancement(include_page_info=True)

    @patch.object(g.time, 'sleep')
    @patch.object(g, '_retry_with_backoff')
    def test_http_200_throttle_retries(self, retry, sleep):
        throttle = Mock(status_code=200)
        throttle.json.return_value = {'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED'}}]}
        success = Mock(status_code=200)
        success.json.return_value = {'data': {'shop': {'id': 'shop'}}}
        retry.side_effect = [throttle, success]
        result = g.execute_graphql_query('query { shop { id } }', shop_domain='test.myshopify.com', access_token='fake')
        self.assertEqual(result, success.json.return_value)
        self.assertEqual(retry.call_count, 2)
        sleep.assert_called_once()


if __name__ == '__main__':
    unittest.main()
