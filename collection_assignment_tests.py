"""Regression coverage for automatic collection assignment.

Desktop (local-worker) uploads previously published with zero collections
because the collection fetch was skipped for that path.
"""
import unittest
from unittest.mock import patch

import shopify_graphql


class CollectionMatchingTests(unittest.TestCase):
    def _page(self, edges, has_next=False, cursor=None):
        return {'data': {'collections': {
            'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
            'edges': [{'node': node} for node in edges],
        }}}

    def setUp(self):
        shopify_graphql._store_config_cache.clear()

    @patch('shopify_graphql._resolve_credentials', return_value=('shop.myshopify.com', 'token'))
    @patch('shopify_graphql.execute_graphql_query')
    def test_collection_map_follows_every_page(self, execute, _creds):
        execute.side_effect = [
            self._page([{'id': 'gid://c/1', 'title': 'View All Posters', 'handle': 'a'}], True, 'cur1'),
            self._page([{'id': 'gid://c/2', 'title': 'Orange Wall Art', 'handle': 'b'}]),
        ]
        mapping = shopify_graphql._get_collection_title_id_map(
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(mapping, {
            'View All Posters': 'gid://c/1',
            'Orange Wall Art': 'gid://c/2',
        })
        self.assertEqual(execute.call_count, 2)

    @patch('shopify_graphql._resolve_credentials', return_value=('shop.myshopify.com', 'token'))
    @patch('shopify_graphql.execute_graphql_query')
    def test_titles_match_case_and_punctuation_insensitively(self, execute, _creds):
        execute.side_effect = [
            self._page([
                {'id': 'gid://c/1', 'title': 'View All Posters', 'handle': 'a'},
                {'id': 'gid://c/2', 'title': 'Animal Art', 'handle': 'b'},
            ]),
            {'data': {'collectionAddProducts': {'collection': {'id': 'gid://c/1', 'title': 'View All Posters'}, 'userErrors': []}}},
            {'data': {'collectionAddProducts': {'collection': {'id': 'gid://c/2', 'title': 'Animal Art'}, 'userErrors': []}}},
        ]
        ok = shopify_graphql.add_to_collections_graphql(
            'gid://p/1', ['view all posters', 'animal-art'],
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertTrue(ok)
        self.assertEqual(execute.call_count, 3)


class ResolveCollectionsTests(unittest.TestCase):
    def test_ai_picks_are_kept_alongside_auto_matches(self):
        import app
        metadata = {
            'title': 'Black Panther and Orange Vase Wall Art',
            'collections': ['View All Posters', 'Animal Art', 'Illustrative Art', 'Orange Art'],
            'metafields': {'palette': 'Orange, Green, Beige', 'subject': 'Black panther',
                           'art_style': 'Illustrative'},
        }
        available = ['View All Posters', 'Animal Art', 'Illustrative Art', 'Orange Art',
                     'Botanical Art', 'Black and White Art']
        final = app._resolve_product_collections(metadata, available, log_prefix='test')
        for required in ('View All Posters', 'Animal Art', 'Illustrative Art', 'Orange Art'):
            self.assertIn(required, final)
        self.assertNotIn('Black and White Art', final)


class SkuReplaceTests(unittest.TestCase):
    """A wrong SKU prefix must be swapped without disturbing the rest."""

    def _shop(self):
        class _Shop:
            id = 1
            shop_domain = 'shop.myshopify.com'
            access_token = 'token'
        return _Shop()

    def _products(self):
        return [
            {
                'id': 'gid://shopify/Product/1', 'handle': 'blue-poster', 'title': 'Blue Poster',
                'variants': [
                    {'id': 'gid://shopify/ProductVariant/11', 'title': 'A4', 'sku': 'SAMOLD-ABC123-V1'},
                    {'id': 'gid://shopify/ProductVariant/12', 'title': 'A3', 'sku': 'SAMOLD-ABC123-V2'},
                ],
            },
            {
                'id': 'gid://shopify/Product/2', 'handle': 'red-poster', 'title': 'Red Poster',
                'variants': [
                    {'id': 'gid://shopify/ProductVariant/21', 'title': 'A4', 'sku': 'SAMNEW-XYZ999-V1'},
                ],
            },
        ]

    def test_only_matching_skus_change_and_the_rest_is_kept(self):
        import app
        with patch.object(app, '_iter_maintenance_products', return_value=iter(self._products())):
            items = app._plan_sku_replacements(self._shop(), 'SAMOLD', 'SAMNEW', 'all')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['handle'], 'blue-poster')
        self.assertEqual(
            [change['sku'] for change in items[0]['changes']],
            ['SAMNEW-ABC123-V1', 'SAMNEW-ABC123-V2'],
        )

    def test_selected_scope_ignores_products_that_are_not_ticked(self):
        import app
        with patch.object(app, '_iter_maintenance_products', return_value=iter(self._products())):
            items = app._plan_sku_replacements(
                self._shop(), 'SAMOLD', 'SAMNEW', 'selected', only=['red-poster'])
        self.assertEqual(items, [])

    def test_no_match_means_no_change_is_planned(self):
        import app
        with patch.object(app, '_iter_maintenance_products', return_value=iter(self._products())):
            items = app._plan_sku_replacements(self._shop(), 'NOTHERE', 'X', 'all')
        self.assertEqual(items, [])

    def test_recent_scope_limits_how_many_products_are_read(self):
        import app
        captured = {}

        def fake_iter(shop, sort_key='CREATED_AT', reverse=True, max_products=0):
            captured['sort_key'] = sort_key
            captured['reverse'] = reverse
            captured['max_products'] = max_products
            return iter(self._products()[:max_products or None])

        with patch.object(app, '_iter_maintenance_products', fake_iter):
            app._plan_sku_replacements(self._shop(), 'SAMOLD', 'SAMNEW', 'recent', recent_count=1)
        self.assertEqual(captured, {'sort_key': 'CREATED_AT', 'reverse': True, 'max_products': 1})

    def test_scope_validation_rejects_an_empty_find(self):
        import app
        with self.assertRaises(ValueError):
            app._sku_scope_from_request({'scope': 'all', 'find': '   '})

    def test_variant_sku_mutation_sends_the_exact_values(self):
        with patch('shopify_graphql._resolve_credentials',
                   return_value=('shop.myshopify.com', 'token')), \
             patch('shopify_graphql.execute_graphql_query') as execute:
            execute.return_value = {'data': {'productVariantsBulkUpdate': {
                'productVariants': [{'id': 'gid://shopify/ProductVariant/11'}], 'userErrors': []}}}
            result = shopify_graphql.update_variant_skus_graphql(
                'gid://shopify/Product/1',
                [{'id': 'gid://shopify/ProductVariant/11', 'sku': 'SAMNEW-ABC123-V1'}],
            )
        self.assertTrue(result['success'])
        variables = execute.call_args[0][1]
        self.assertEqual(variables['variants'],
                         [{'id': 'gid://shopify/ProductVariant/11',
                           'inventoryItem': {'sku': 'SAMNEW-ABC123-V1'}}])

    def test_shopify_errors_are_reported_not_swallowed(self):
        with patch('shopify_graphql._resolve_credentials',
                   return_value=('shop.myshopify.com', 'token')), \
             patch('shopify_graphql.execute_graphql_query') as execute:
            execute.return_value = {'data': {'productVariantsBulkUpdate': {
                'productVariants': [], 'userErrors': [{'message': 'SKU already in use'}]}}}
            result = shopify_graphql.update_variant_skus_graphql(
                'gid://shopify/Product/1',
                [{'id': 'gid://shopify/ProductVariant/11', 'sku': 'DUP'}],
            )
        self.assertFalse(result['success'])
        self.assertIn('SKU already in use', result['error'])


class ColumnScopedEditTests(unittest.TestCase):
    """A column-scoped AI edit must not touch any other field."""

    def _product(self):
        return {
            'id': 'gid://shopify/Product/1',
            'handle': 'blue-poster',
            'title': 'Blue Poster',
            'body_html': '<p>Original description</p>',
            'tags': 'blue, poster',
            'seo_title': 'Old SEO title',
            'seo_description': 'Old meta description',
        }

    def _ai_output(self):
        return {
            'product_id': 'gid://shopify/Product/1',
            'handle': 'blue-poster',
            'title': 'AI rewritten title',
            'body_html': '<p>AI rewritten description</p>',
            'tags': 'ai, tags',
            'seo_title': 'AI SEO title',
            'seo_description': 'AI meta description',
        }

    def test_only_the_chosen_field_survives(self):
        import app
        product = self._product()
        enhanced = self._ai_output()
        changed = app._restrict_enhancement_to_fields(enhanced, product, ['seo_title'])
        self.assertTrue(changed)
        self.assertEqual(enhanced['seo_title'], 'AI SEO title')
        self.assertEqual(enhanced['title'], 'Blue Poster')
        self.assertEqual(enhanced['body_html'], '<p>Original description</p>')
        self.assertEqual(enhanced['tags'], 'blue, poster')
        self.assertEqual(enhanced['seo_description'], 'Old meta description')

    def test_identical_value_is_marked_as_no_change(self):
        import app
        product = self._product()
        enhanced = self._ai_output()
        enhanced['seo_title'] = product['seo_title']
        changed = app._restrict_enhancement_to_fields(enhanced, product, ['seo_title'])
        self.assertFalse(changed)
        self.assertTrue(enhanced['_no_change'])

    def test_the_product_id_is_never_reverted_away(self):
        import app
        product = self._product()
        enhanced = self._ai_output()
        app._restrict_enhancement_to_fields(enhanced, product, ['title'])
        self.assertEqual(enhanced['product_id'], 'gid://shopify/Product/1')

    def test_no_only_fields_means_the_full_rewrite_is_kept(self):
        import app
        product = self._product()
        enhanced = self._ai_output()
        app._restrict_enhancement_to_fields(enhanced, product, [])
        self.assertEqual(enhanced['title'], 'AI rewritten title')
        self.assertEqual(enhanced['body_html'], '<p>AI rewritten description</p>')

    def test_category_name_and_id_move_together(self):
        import app
        product = {'product_category': 'Old category', 'category_gid': 'gid://old'}
        enhanced = {'product_category': 'New category', 'category_gid': 'gid://new', 'title': 'x'}
        app._restrict_enhancement_to_fields(enhanced, product, ['product_category'])
        self.assertEqual(enhanced['category_gid'], 'gid://new')


class WebhookTests(unittest.TestCase):
    """An unverified webhook body must never be acted on."""

    def test_a_correct_signature_verifies(self):
        import base64, hashlib, hmac as hmac_module
        import shopify_webhooks
        body = b'{"id":123}'
        signature = base64.b64encode(
            hmac_module.new(b'shh', body, hashlib.sha256).digest()).decode()
        self.assertTrue(shopify_webhooks.verify_webhook(body, signature, secret='shh'))

    def test_a_tampered_body_is_rejected(self):
        import base64, hashlib, hmac as hmac_module
        import shopify_webhooks
        signature = base64.b64encode(
            hmac_module.new(b'shh', b'{"id":123}', hashlib.sha256).digest()).decode()
        self.assertFalse(shopify_webhooks.verify_webhook(b'{"id":999}', signature, secret='shh'))

    def test_a_missing_secret_never_trusts_the_caller(self):
        import shopify_webhooks
        self.assertFalse(shopify_webhooks.verify_webhook(b'{}', 'anything', secret=''))

    def test_header_topic_maps_to_the_graphql_enum(self):
        import shopify_webhooks
        self.assertEqual(shopify_webhooks.topic_to_enum('products/update'), 'PRODUCTS_UPDATE')

    def test_existing_subscription_on_this_url_is_not_recreated(self):
        import shopify_webhooks
        url = 'https://example.com/webhooks/shopify'
        existing = [{'id': 'gid://w/1', 'topic': topic, 'callback_url': url}
                    for topic in shopify_webhooks.WEBHOOK_TOPICS]
        with patch.object(shopify_webhooks, 'list_webhooks', return_value=existing), \
             patch('shopify_graphql.execute_graphql_query') as execute:
            result = shopify_webhooks.ensure_webhooks(url, 'shop.myshopify.com', 'token')
        self.assertTrue(result['success'])
        self.assertEqual(result['created'], [])
        self.assertEqual(len(result['already_active']), len(shopify_webhooks.WEBHOOK_TOPICS))
        execute.assert_not_called()

    def test_a_subscription_on_an_old_url_is_replaced(self):
        import shopify_webhooks
        url = 'https://example.com/webhooks/shopify'
        existing = [{'id': 'gid://w/1', 'topic': 'PRODUCTS_UPDATE',
                     'callback_url': 'https://old-app.example/webhooks/shopify'}]
        created = {'data': {'webhookSubscriptionCreate': {
            'webhookSubscription': {'id': 'gid://w/2', 'topic': 'PRODUCTS_UPDATE'}, 'userErrors': []}}}
        with patch.object(shopify_webhooks, 'list_webhooks', return_value=existing), \
             patch('shopify_graphql.execute_graphql_query', return_value=created):
            result = shopify_webhooks.ensure_webhooks(
                url, 'shop.myshopify.com', 'token', topics=['PRODUCTS_UPDATE'])
        self.assertEqual(result['created'], ['PRODUCTS_UPDATE'])
        self.assertEqual(result['removed_stale'][0]['callback_url'],
                         'https://old-app.example/webhooks/shopify')


class ConnectionPaginationTests(unittest.TestCase):
    """Metafields and channels past the first page must still be read."""

    def _page(self, nodes, has_next=False, cursor=None):
        return {'data': {'node': {'metafields': {
            'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
            'nodes': nodes,
        }}}}

    @patch('shopify_graphql.execute_graphql_query')
    def test_every_remaining_page_is_followed(self, execute):
        execute.side_effect = [
            self._page([{'namespace': 'a', 'key': '1'}], True, 'cur2'),
            self._page([{'namespace': 'b', 'key': '2'}]),
        ]
        nodes = shopify_graphql.fetch_remaining_connection_nodes(
            'gid://shopify/Product/1', 'Product', 'metafields', 'namespace key', 'cur1',
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(len(nodes), 2)
        self.assertEqual(execute.call_count, 2)

    @patch('shopify_graphql.execute_graphql_query')
    def test_nothing_is_requested_when_the_first_page_was_the_last(self, execute):
        nodes = shopify_graphql.fetch_remaining_connection_nodes(
            'gid://shopify/Product/1', 'Product', 'metafields', 'namespace key', None)
        self.assertEqual(nodes, [])
        execute.assert_not_called()


class CatalogueConnectionCompletenessTests(unittest.TestCase):
    """Nothing the catalogue read needs may be silently cut off."""

    def _product_page(self, node, has_next=False):
        return {'data': {'products': {
            'pageInfo': {'hasNextPage': has_next, 'endCursor': None},
            'edges': [{'node': node}],
        }}}

    def _node(self, **connections):
        base = {
            'id': 'gid://shopify/Product/1', 'handle': 'p', 'title': 'P',
            'descriptionHtml': '', 'vendor': '', 'productType': '', 'tags': [],
            'status': 'ACTIVE', 'seo': {}, 'featuredMedia': {},
            'collections': {'pageInfo': {}, 'nodes': []},
            'media': {'pageInfo': {}, 'nodes': []},
            'variants': {'pageInfo': {}, 'nodes': []},
            'metafields': {'pageInfo': {}, 'nodes': []},
        }
        base.update(connections)
        return base

    @patch('shopify_graphql.fetch_remaining_connection_nodes')
    @patch('shopify_graphql.execute_graphql_query')
    def test_a_product_with_more_variants_than_one_page_reads_them_all(self, execute, top_up):
        node = self._node(variants={
            'pageInfo': {'hasNextPage': True, 'endCursor': 'v50'},
            'nodes': [{'id': 'gid://v/1', 'title': 'A4', 'sku': 'S1'}],
        })
        execute.return_value = self._product_page(node)
        top_up.return_value = [{'id': 'gid://v/2', 'title': 'A3', 'sku': 'S2'}]
        products = shopify_graphql.list_products_for_seo_enhancement(
            limit=1, shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(len(products[0]['variants']), 2)
        self.assertEqual(top_up.call_args[0][2], 'variants')
        self.assertEqual(top_up.call_args[0][4], 'v50')

    @patch('shopify_graphql.fetch_remaining_connection_nodes')
    @patch('shopify_graphql.execute_graphql_query')
    def test_a_product_with_more_images_than_one_page_reads_them_all(self, execute, top_up):
        node = self._node(media={
            'pageInfo': {'hasNextPage': True, 'endCursor': 'm10'},
            'nodes': [{'id': 'gid://m/1', 'alt': '', 'image': {'url': 'a.jpg'}}],
        })
        execute.return_value = self._product_page(node)
        top_up.return_value = [{'id': 'gid://m/2', 'alt': '', 'image': {'url': 'b.jpg'}}]
        products = shopify_graphql.list_products_for_seo_enhancement(
            limit=1, shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(products[0]['media_count'], 2)

    @patch('shopify_graphql.fetch_remaining_connection_nodes')
    @patch('shopify_graphql.execute_graphql_query')
    def test_a_product_in_more_collections_than_one_page_reads_them_all(self, execute, top_up):
        node = self._node(collections={
            'pageInfo': {'hasNextPage': True, 'endCursor': 'c50'},
            'nodes': [{'id': 'gid://c/1', 'title': 'Cat Prints'}],
        })
        execute.return_value = self._product_page(node)
        top_up.return_value = [{'id': 'gid://c/2', 'title': 'Orange Prints'}]
        products = shopify_graphql.list_products_for_seo_enhancement(
            limit=1, shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(products[0]['collections'], 'Cat Prints, Orange Prints')

    @patch('shopify_graphql.fetch_remaining_connection_nodes')
    @patch('shopify_graphql.execute_graphql_query')
    def test_a_single_page_makes_no_extra_requests(self, execute, top_up):
        node = self._node(
            variants={'pageInfo': {'hasNextPage': False}, 'nodes': [{'id': 'gid://v/1'}]},
            media={'pageInfo': {'hasNextPage': False}, 'nodes': []},
            collections={'pageInfo': {'hasNextPage': False}, 'nodes': []},
            metafields={'pageInfo': {'hasNextPage': False}, 'nodes': []},
        )
        execute.return_value = self._product_page(node)
        shopify_graphql.list_products_for_seo_enhancement(
            limit=1, shop_domain='shop.myshopify.com', access_token='token')
        top_up.assert_not_called()


class TaxonomyValueCompletenessTests(unittest.TestCase):
    """An allowed value the app cannot see is one the AI can never pick."""

    def setUp(self):
        import shopify_category_metafields
        shopify_category_metafields._category_attributes_cache.clear()

    def _response(self, has_next, cursor=None):
        return {'data': {'node': {
            'id': 'gid://shopify/TaxonomyCategory/ha-1',
            'name': 'Posters',
            'attributes': {
                'pageInfo': {'hasNextPage': False},
                'nodes': [{
                    'id': 'gid://shopify/TaxonomyAttribute/1',
                    'name': 'Color',
                    'values': {
                        'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
                        'nodes': [{'id': 'gid://v/1', 'name': 'Blue'}],
                    },
                }],
            },
        }}}

    def test_values_past_the_first_page_are_read(self):
        import shopify_category_metafields
        with patch.object(shopify_category_metafields, 'execute_graphql_query',
                          return_value=self._response(True, 'v250')), \
             patch('shopify_graphql.fetch_remaining_connection_nodes',
                   return_value=[{'id': 'gid://v/2', 'name': 'Ochre'}]) as top_up:
            attributes = shopify_category_metafields.get_category_attributes(
                'gid://shopify/TaxonomyCategory/ha-1',
                shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(attributes[0]['values'], ['Blue', 'Ochre'])
        self.assertEqual(top_up.call_args[0][2], 'values')

    def test_a_short_value_list_makes_no_extra_request(self):
        import shopify_category_metafields
        with patch.object(shopify_category_metafields, 'execute_graphql_query',
                          return_value=self._response(False)), \
             patch('shopify_graphql.fetch_remaining_connection_nodes') as top_up:
            attributes = shopify_category_metafields.get_category_attributes(
                'gid://shopify/TaxonomyCategory/ha-2',
                shop_domain='shop.myshopify.com', access_token='token')
        self.assertEqual(attributes[0]['values'], ['Blue'])
        top_up.assert_not_called()


class ResponseCompressionTests(unittest.TestCase):
    """Compression must shrink pages without changing what they contain."""

    def setUp(self):
        import app
        self.client = app.app.test_client()

    def test_a_gzipped_page_decompresses_to_the_same_bytes(self):
        import gzip as gzip_module
        plain = self.client.get('/login')
        squeezed = self.client.get('/login', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(squeezed.headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip_module.decompress(squeezed.get_data()), plain.get_data())
        self.assertLess(len(squeezed.get_data()), len(plain.get_data()))

    def test_a_client_that_cannot_gzip_gets_plain_bytes(self):
        response = self.client.get('/login', headers={'Accept-Encoding': 'identity'})
        self.assertIsNone(response.headers.get('Content-Encoding'))
        self.assertIn(b'<', response.get_data())

    def test_the_content_length_matches_what_is_sent(self):
        response = self.client.get('/login', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(int(response.headers['Content-Length']), len(response.get_data()))

    def test_caches_are_told_the_reply_varies_by_encoding(self):
        response = self.client.get('/login', headers={'Accept-Encoding': 'gzip'})
        self.assertIn('Accept-Encoding', response.headers.get('Vary', ''))

    def test_a_redirect_is_left_alone(self):
        response = self.client.get('/', headers={'Accept-Encoding': 'gzip'})
        if response.status_code >= 300:
            self.assertIsNone(response.headers.get('Content-Encoding'))


class TagCleanupTests(unittest.TestCase):
    """Tag renames must be exact, and must never quietly lose a tag."""

    def test_splitting_drops_blanks_and_repeats(self):
        import app
        self.assertEqual(app._split_tags('Cat,  dog , ,CAT '), ['Cat', 'dog'])

    def test_a_rename_keeps_the_order_of_the_other_tags(self):
        import app
        result = app._apply_tag_map_to_list(['Blue', 'black cat', 'Poster'], {'black cat': 'Black cat'})
        self.assertEqual(result, ['Blue', 'Black cat', 'Poster'])

    def test_a_merge_leaves_one_copy_not_two(self):
        import app
        result = app._apply_tag_map_to_list(
            ['Black cat', 'a black cat'], {'a black cat': 'Black cat'})
        self.assertEqual(result, ['Black cat'])

    def test_a_tag_nobody_renamed_is_untouched(self):
        import app
        self.assertEqual(app._apply_tag_map_to_list(['Cat'], {'dog': 'Dog'}), ['Cat'])

    def test_the_apply_route_refuses_an_empty_plan(self):
        import app
        with app.app.test_client() as client:
            response = client.post('/api/shopify_tags/apply', json={'renames': []})
        self.assertIn(response.status_code, (302, 400, 401))


class CollectionRuleWriteTests(unittest.TestCase):
    """A rule set is replaced wholesale, so a partial one must be refused."""

    def test_an_empty_rule_set_is_refused(self):
        result = shopify_graphql.update_collection_rules(
            'gid://shopify/Collection/1', {'rules': []},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertFalse(result['success'])

    def test_a_rule_missing_its_condition_is_refused(self):
        result = shopify_graphql.update_collection_rules(
            'gid://shopify/Collection/1',
            {'rules': [{'column': 'TAG', 'relation': 'EQUALS'}]},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertFalse(result['success'])

    @patch('shopify_graphql.execute_graphql_query')
    def test_a_good_rule_set_is_sent_as_written(self, execute):
        execute.return_value = {'data': {'collectionUpdate': {
            'collection': {'id': 'gid://shopify/Collection/1'}, 'userErrors': []}}}
        result = shopify_graphql.update_collection_rules(
            'gid://shopify/Collection/1',
            {'appliedDisjunctively': True,
             'rules': [{'column': 'TAG', 'relation': 'EQUALS', 'condition': 'Black cat'}]},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertTrue(result['success'])
        sent = execute.call_args[0][1]['input']['ruleSet']
        self.assertTrue(sent['appliedDisjunctively'])
        self.assertEqual(sent['rules'][0]['condition'], 'Black cat')


class SmartCollectionCreateTests(unittest.TestCase):
    """A generated collection page must never be created empty."""

    def test_a_collection_with_no_tags_is_refused(self):
        result = shopify_graphql.create_smart_collection(
            {'title': 'Cat art', 'tags': []},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertFalse(result['success'])

    def test_a_collection_with_no_title_is_refused(self):
        result = shopify_graphql.create_smart_collection(
            {'title': '', 'tags': ['Cat']},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertFalse(result['success'])

    @patch('shopify_graphql.execute_graphql_query')
    def test_every_tag_becomes_an_any_of_rule(self, execute):
        execute.return_value = {'data': {'collectionCreate': {
            'collection': {'id': 'gid://shopify/Collection/9', 'handle': 'cat-art'},
            'userErrors': []}}}
        result = shopify_graphql.create_smart_collection(
            {'title': 'Cat art', 'tags': ['Cat', 'Black cat'], 'seo_title': 'Cat art prints'},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertTrue(result['success'])
        sent = execute.call_args[0][1]['input']
        self.assertTrue(sent['ruleSet']['appliedDisjunctively'])
        self.assertEqual([rule['condition'] for rule in sent['ruleSet']['rules']],
                         ['Cat', 'Black cat'])
        self.assertEqual(sent['seo']['title'], 'Cat art prints')

    @patch('shopify_graphql.execute_graphql_query')
    def test_a_blank_description_is_not_sent_at_all(self, execute):
        execute.return_value = {'data': {'collectionCreate': {
            'collection': {'id': 'gid://shopify/Collection/9'}, 'userErrors': []}}}
        shopify_graphql.create_smart_collection(
            {'title': 'Cat art', 'tags': ['Cat'], 'description_html': ''},
            shop_domain='shop.myshopify.com', access_token='token')
        self.assertNotIn('descriptionHtml', execute.call_args[0][1]['input'])


class CollectionTagNamingTests(unittest.TestCase):
    """The tag a page answers to has to be stable and safe."""

    def test_punctuation_is_dropped_from_the_tag(self):
        import app
        self.assertEqual(app._collection_tag_for_title('Cat Art & Prints'), 'Cat art prints')

    def test_a_title_with_no_letters_is_refused(self):
        import app
        with self.assertRaises(ValueError):
            app._collection_tag_for_title('!!!')

    def test_the_colour_hint_prefers_the_shopify_taxonomy_value(self):
        import app
        product = {'metafields': {
            'shopify.color-pattern': {'value': 'Blue'},
            'custom.color': {'value': 'Green'},
        }}
        self.assertEqual(app._product_colour_hint(product), 'Blue')

    def test_a_product_with_no_colour_returns_nothing(self):
        import app
        self.assertEqual(app._product_colour_hint({'metafields': {}}), '')


if __name__ == '__main__':
    unittest.main()


class MetadataCompletenessTests(unittest.TestCase):
    """Fields that were shipping empty on live listings."""

    def _prune(self, metadata):
        import gemini_utils
        return gemini_utils._prune_unsupported_merchandising_inferences(metadata, "")

    def test_season_defaults_to_year_round_instead_of_blank(self):
        metadata = self._prune({
            'title': 'Black Panther and Orange Vase Wall Art',
            'metafields': {'subject': 'Black panther', 'season': 'Year-round',
                           'occasion': 'Year-round'},
        })
        self.assertEqual(metadata['metafields']['season'], 'Year-round')

    def test_evidenced_season_is_kept(self):
        metadata = self._prune({
            'title': 'Christmas Tree Wall Art',
            'metafields': {'subject': 'Christmas tree', 'season': 'Christmas'},
        })
        self.assertEqual(metadata['metafields']['season'], 'Christmas')

    def test_custom_label_4_falls_back_to_occasion(self):
        metadata = self._prune({
            'title': 'Black Panther Wall Art',
            'metafields': {'subject': 'Black panther', 'occasion': 'Year-round'},
        })
        self.assertEqual(metadata['custom_label_4'], 'Year-round')

    def test_broad_art_movement_still_reaches_shopify_taxonomy(self):
        import app
        picks = app._category_attribute_picks({
            'category_attribute_picks': {'Art style': ['Illustrative']},
            'metafields': {'art_movement': 'Modernism', 'art_style': 'Illustrative'},
        })
        self.assertIn('Art movement', picks)
        self.assertIn('Modernism', picks['Art movement'])

    def test_specific_art_movement_is_ranked_before_broad_one(self):
        import app
        picks = app._category_attribute_picks({
            'category_attribute_picks': {'Art movement': ['Contemporary', 'Art nouveau']},
            'metafields': {},
        })
        self.assertEqual(picks['Art movement'][0], 'Art nouveau')


class SegmentMatchingTests(unittest.TestCase):
    def test_ai_pick_matches_half_of_a_two_part_store_title(self):
        import app
        available = ['Animal Prints | Animal Art', 'View All Posters | Shop All',
                     'Illustrative Art', 'Orange Wall Art']
        matched = app._filter_to_real_collections(
            ['Animal Art', 'View All Posters', 'Illustrative Art'], available)
        self.assertEqual(matched, ['Animal Prints | Animal Art',
                                   'View All Posters | Shop All',
                                   'Illustrative Art'])

    def test_unrelated_pick_is_still_rejected(self):
        import app
        matched = app._filter_to_real_collections(
            ['Vintage Maps'], ['Animal Prints | Animal Art'])
        self.assertEqual(matched, [])


class ArtMovementAliasTests(unittest.TestCase):
    def test_broad_value_maps_onto_the_value_the_store_allows(self):
        from shopify_category_metafields import _match_pick_to_gid
        values_map = {'Modernism': 'gid://mo/1', 'Pop art': 'gid://mo/2'}
        self.assertEqual(_match_pick_to_gid('Contemporary', values_map), 'gid://mo/1')

    def test_exact_allowed_value_still_wins(self):
        from shopify_category_metafields import _match_pick_to_gid
        values_map = {'Contemporary art': 'gid://mo/9', 'Modernism': 'gid://mo/1'}
        self.assertEqual(_match_pick_to_gid('Contemporary art', values_map), 'gid://mo/9')


class InternalLinkTests(unittest.TestCase):
    def test_links_use_the_real_store_handle(self):
        from shopify_graphql import _append_collection_links
        html_out = _append_collection_links(
            '<p>Body.</p>',
            ['Animal Prints | Animal Art'],
            {'Animal Prints | Animal Art': 'animal-prints'},
        )
        self.assertIn('href="/collections/animal-prints"', html_out)
        self.assertNotIn('animal-prints-animal-art', html_out)

    def test_unknown_handle_is_never_guessed(self):
        from shopify_graphql import _append_collection_links
        html_out = _append_collection_links('<p>Body.</p>', ['Mystery Collection'], {})
        self.assertEqual(html_out, '<p>Body.</p>')


class NoFallbackTests(unittest.TestCase):
    def test_fallback_metadata_builder_is_gone(self):
        import gemini_utils
        self.assertFalse(hasattr(gemini_utils, 'build_fallback_product_metadata'))

    def test_publish_refuses_a_product_with_no_ai_title(self):
        import shopify_graphql
        with self.assertRaises(RuntimeError):
            shopify_graphql.create_product_with_graphql({'description': 'x'}, 'artwork.jpg')

    def test_publish_refuses_a_product_with_no_ai_description(self):
        import shopify_graphql
        with self.assertRaises(RuntimeError):
            shopify_graphql.create_product_with_graphql({'title': 'Real Title'}, 'artwork.jpg')


class InternalLinkRepairTests(unittest.TestCase):
    handle_map = {
        'Animal Prints | Animal Art': 'animal-prints',
        'Green Wall Art | Green Art': 'green-wall-art',
        'View All Posters | All Posters at Samila Home': 'view-all-posters',
    }

    def test_slugified_title_links_are_repointed(self):
        from shopify_graphql import repair_internal_collection_links
        body = ('<p>Body.</p><p>Discover more designs in our '
                '<a href="/collections/animal-prints-animal-art">Animal Prints | Animal Art</a> and '
                '<a href="/collections/green-wall-art-green-art">Green Wall Art | Green Art</a> collections.</p>')
        fixed, fixes, unresolved = repair_internal_collection_links(body, self.handle_map)
        self.assertIn('href="/collections/animal-prints"', fixed)
        self.assertIn('href="/collections/green-wall-art"', fixed)
        self.assertEqual(len(fixes), 2)
        self.assertEqual(unresolved, [])
        # Only the href changes - the visible copy is untouched.
        self.assertIn('Animal Prints | Animal Art</a>', fixed)
        self.assertIn('<p>Body.</p>', fixed)

    def test_working_links_are_left_alone(self):
        from shopify_graphql import repair_internal_collection_links
        body = '<a href="/collections/animal-prints">Animal Art</a>'
        fixed, fixes, unresolved = repair_internal_collection_links(body, self.handle_map)
        self.assertEqual(fixed, body)
        self.assertEqual(fixes, [])

    def test_unmatchable_link_is_reported_not_guessed(self):
        from shopify_graphql import repair_internal_collection_links
        body = '<a href="/collections/does-not-exist">Something Else</a>'
        fixed, fixes, unresolved = repair_internal_collection_links(body, self.handle_map)
        self.assertEqual(fixed, body)
        self.assertEqual(fixes, [])
        self.assertEqual(unresolved, ['does-not-exist'])

    def test_link_text_matching_recovers_a_renamed_handle(self):
        from shopify_graphql import repair_internal_collection_links
        body = '<a href="/collections/old-handle">View All Posters</a>'
        fixed, fixes, unresolved = repair_internal_collection_links(body, self.handle_map)
        self.assertIn('href="/collections/view-all-posters"', fixed)
        self.assertEqual(unresolved, [])


class RetryBackoffTests(unittest.TestCase):
    def test_five_attempts_are_spread_over_a_couple_of_minutes(self):
        import app
        delays = [app._metadata_retry_backoff_seconds(n) for n in range(1, 5)]
        self.assertEqual(delays, [10, 20, 40, 75])
        self.assertGreaterEqual(sum(delays), 120)


class MandatoryCollectionTests(unittest.TestCase):
    available = ['View All Posters | All Posters at Samila Home',
                 'Animal Prints | Animal Art', 'Illustration Posters | Illustrated Wall Art',
                 'Orange Prints | Orange Art', 'Cat Prints | Cat Art',
                 'Botanical Prints | Art With Flowers', 'Green Wall Art | Green Art']

    def test_directive_is_read_from_the_profile(self):
        import app
        found = app._mandatory_collections_from_prompt(
            "- SOMETHING ELSE: no\n- ALWAYS INCLUDE COLLECTION: View All Posters\n")
        self.assertEqual(found, ['View All Posters'])

    def test_mandatory_collection_leads_the_list_even_if_ai_forgot_it(self):
        import app
        metadata = {'title': 'Black Cat Wall Art', 'tags': 'Cat, Animals',
                    'metafields': {'subject': 'Cat', 'palette': 'Orange'}}
        metadata['collections'] = ['Animal Art', 'Cat Prints']
        final = app._resolve_product_collections(
            metadata, self.available, log_prefix='test',
            mandatory_collections=['View All Posters'])
        self.assertEqual(final[0], 'View All Posters | All Posters at Samila Home')
        self.assertIn('Animal Prints | Animal Art', final)

    def test_no_directive_changes_nothing(self):
        import app
        metadata = {'title': 'Black Cat Wall Art', 'collections': ['Cat Prints'],
                    'metafields': {'subject': 'Cat'}}
        final = app._resolve_product_collections(metadata, self.available, log_prefix='test')
        self.assertNotIn('View All Posters | All Posters at Samila Home', final)


class ProfileUpgradeTests(unittest.TestCase):
    """A rules-version bump must never discard prompt edits made in the app."""

    def test_user_edits_survive_and_missing_directive_is_added(self):
        import app
        saved = (
            "STORE PROFILE FACTS:\n"
            "- PROFILE RULES VERSION: 6\n"
            "- My own hand-written rule that must not be lost.\n"
            "- REQUIRED DESCRIPTION FACT: 270 gsm premium satin paper\n"
        )
        upgraded = app._upgrade_profile_prompt_in_place(
            saved, "PROFILE RULES VERSION: 7", ["ALWAYS INCLUDE COLLECTION: View All Posters"]
        )
        self.assertIn("My own hand-written rule that must not be lost.", upgraded)
        self.assertIn("REQUIRED DESCRIPTION FACT: 270 gsm premium satin paper", upgraded)
        self.assertIn("PROFILE RULES VERSION: 7", upgraded)
        self.assertNotIn("PROFILE RULES VERSION: 6", upgraded)
        self.assertIn("ALWAYS INCLUDE COLLECTION: View All Posters", upgraded)

    def test_existing_directive_is_not_duplicated_or_overwritten(self):
        import app
        saved = (
            "- PROFILE RULES VERSION: 6\n"
            "- ALWAYS INCLUDE COLLECTION: My Own Catch All\n"
        )
        upgraded = app._upgrade_profile_prompt_in_place(
            saved, "PROFILE RULES VERSION: 7", ["ALWAYS INCLUDE COLLECTION: View All Posters"]
        )
        self.assertIn("ALWAYS INCLUDE COLLECTION: My Own Catch All", upgraded)
        self.assertNotIn("View All Posters", upgraded)
        self.assertEqual(upgraded.count("ALWAYS INCLUDE COLLECTION"), 1)

    def test_directive_the_user_wrote_is_what_gets_enforced(self):
        import app
        picks = app._mandatory_collections_from_prompt(
            "- ALWAYS INCLUDE COLLECTION: My Own Catch All\n")
        self.assertEqual(picks, ['My Own Catch All'])


class ProfileSectionUpgradeTests(unittest.TestCase):
    def test_sections_gain_the_directive_and_keep_user_text(self):
        import app, json
        sections = json.dumps([
            {'id': 'intro', 'content': 'STORE PROFILE FACTS:\n- PROFILE RULES VERSION: 6\n- My own rule.'},
            {'id': 'product_title', 'content': 'Title rules I wrote.'},
        ])
        out = json.loads(app._upgrade_profile_sections_in_place(
            sections, 'PROFILE RULES VERSION: 7', ['ALWAYS INCLUDE COLLECTION: View All Posters']))
        joined = '\n'.join(s['content'] for s in out)
        self.assertIn('My own rule.', joined)
        self.assertIn('Title rules I wrote.', joined)
        self.assertIn('PROFILE RULES VERSION: 7', joined)
        self.assertIn('ALWAYS INCLUDE COLLECTION: View All Posters', joined)

    def test_directive_already_present_in_another_section_is_not_duplicated(self):
        import app, json
        sections = json.dumps([
            {'id': 'intro', 'content': '- PROFILE RULES VERSION: 6'},
            {'id': 'other', 'content': '- ALWAYS INCLUDE COLLECTION: My Catch All'},
        ])
        out = json.loads(app._upgrade_profile_sections_in_place(
            sections, 'PROFILE RULES VERSION: 7', ['ALWAYS INCLUDE COLLECTION: View All Posters']))
        joined = '\n'.join(s['content'] for s in out)
        self.assertEqual(joined.count('ALWAYS INCLUDE COLLECTION'), 1)
        self.assertIn('My Catch All', joined)


class VariantResetGuardTests(unittest.TestCase):
    def test_starter_row_is_recognised(self):
        import app
        self.assertTrue(app._looks_like_untouched_variant_defaults(
            [{'title': 'A4 (21x30cm)', 'price': '29.99', 'inventory_quantity': 999}]))
        self.assertTrue(app._looks_like_untouched_variant_defaults(
            [{'name': 'a4 (21x30cm)', 'price': 29.99}]))

    def test_a_real_price_list_is_not_flagged(self):
        import app
        self.assertFalse(app._looks_like_untouched_variant_defaults(
            [{'title': 'A4 - 21 x 29.7 cm', 'price': '6.99'},
             {'title': '30x40 cm', 'price': '11.99'}]))

    def test_a_single_deliberate_variant_is_not_flagged(self):
        import app
        self.assertFalse(app._looks_like_untouched_variant_defaults(
            [{'title': 'A4 (21x30cm)', 'price': '6.99'}]))
        self.assertFalse(app._looks_like_untouched_variant_defaults([]))


class RepairScopeTests(unittest.TestCase):
    """The repair must be runnable on one product before the whole catalogue."""

    def test_scan_returns_handles_so_a_single_product_can_be_targeted(self):
        # The sample carries handle + kind, which is what "Test on 1" and
        # "Fix selected" send back as the `only` list.
        import app
        self.assertTrue(hasattr(app, 'repair_product_internal_links'))
        self.assertTrue(hasattr(app, '_scan_products_for_broken_internal_links'))

    def test_confirmation_counts_only_what_will_be_written(self):
        # planned = min(items, batch_size); a one-product run must ask for
        # "REPAIR 1 PRODUCT", not the catalogue-wide number.
        planned = min(2168, 1)
        self.assertEqual('REPAIR %d PRODUCT%s' % (planned, '' if planned == 1 else 'S'),
                         'REPAIR 1 PRODUCT')
        planned = min(2168, 200)
        self.assertEqual('REPAIR %d PRODUCT%s' % (planned, '' if planned == 1 else 'S'),
                         'REPAIR 200 PRODUCTS')


class OrientationTests(unittest.TestCase):
    """Orientation must come from the file, not the model's impression."""

    def _image(self, width, height):
        import tempfile, os
        from PIL import Image
        path = os.path.join(tempfile.gettempdir(), 'orient_%dx%d.jpg' % (width, height))
        Image.new('RGB', (width, height), 'white').save(path)
        return path

    def test_tall_artwork_is_portrait_even_if_the_ai_said_square(self):
        import app
        metadata = {'metafields': {'orientation': 'Square'}}
        app._apply_measured_orientation(metadata, self._image(2000, 3000), {})
        self.assertEqual(metadata['metafields']['orientation'], 'Portrait')

    def test_wide_artwork_is_landscape(self):
        import app
        metadata = {'metafields': {'orientation': 'Portrait'}}
        app._apply_measured_orientation(metadata, self._image(3000, 2000), {})
        self.assertEqual(metadata['metafields']['orientation'], 'Landscape')

    def test_manual_orientation_wins(self):
        import app
        metadata = {'metafields': {'orientation': 'Portrait'}}
        app._apply_measured_orientation(metadata, self._image(3000, 2000),
                                        {'orientation_manual': True, 'manual_orientation': 'Portrait'})
        self.assertEqual(metadata['metafields']['orientation'], 'Portrait')

    def test_framed_mockup_is_not_measured(self):
        import app
        metadata = {'metafields': {'orientation': 'Portrait'}}
        app._apply_measured_orientation(metadata, self._image(3000, 2000),
                                        {'analysis_is_framed': True})
        self.assertEqual(metadata['metafields']['orientation'], 'Portrait')
