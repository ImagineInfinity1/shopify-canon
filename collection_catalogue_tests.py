import unittest
from unittest.mock import patch

from shopify_graphql import (
    list_collection_catalogue,
    rank_collection_catalogue_item,
    update_collection_metadata,
)


class CollectionCatalogueTests(unittest.TestCase):
    """Regression coverage for cached collection audits."""
    def test_collection_score_is_transparent_and_capped(self):
        item = rank_collection_catalogue_item({
            'title': 'Modern Abstract Wall Art',
            'handle': 'modern-abstract-wall-art',
            'description': ' '.join(['useful'] * 80),
            'seo_title': 'Modern Abstract Wall Art Prints',
            'seo_description': ' '.join(['collection'] * 14),
            'image_url': 'https://cdn.example/image.jpg',
            'image_alt': 'Modern abstract art collection',
            'product_count': 12,
        })
        self.assertEqual(item['quality_score'], 100)
        self.assertEqual(item['quality_band'], 'good')
        self.assertEqual(item['quality_issues'], [])

    def test_missing_collection_content_ranks_as_poor(self):
        item = rank_collection_catalogue_item({
            'title': 'Sale', 'handle': 'sale', 'product_count': 0,
        })
        self.assertLess(item['quality_score'], 55)
        self.assertIn('Missing collection description', item['quality_issues'])
        self.assertIn('Missing custom SEO title', item['quality_issues'])
        self.assertIn('Collection has no products', item['quality_issues'])

    @patch('shopify_graphql.execute_graphql_query')
    def test_collection_import_uses_cursor_pagination_and_normalizes_data(self, execute):
        execute.side_effect = [
            {'data': {'collections': {
                'nodes': [{
                    'id': 'gid://shopify/Collection/123', 'title': 'First Collection',
                    'handle': 'first', 'description': '', 'descriptionHtml': '',
                    'updatedAt': '2026-08-18T10:00:00Z', 'sortOrder': 'BEST_SELLING',
                    'templateSuffix': None, 'productsCount': {'count': 4, 'precision': 'EXACT'},
                    'seo': {'title': '', 'description': ''}, 'image': None,
                    'metafields': {'nodes': []},
                }],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'next'},
            }}},
            {'data': {'collections': {
                'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None},
            }}},
        ]
        rows = list_collection_catalogue('example.myshopify.com', 'token')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['numeric_id'], '123')
        self.assertEqual(rows[0]['product_count'], 4)
        self.assertEqual(rows[0]['shop_url'], 'https://example.myshopify.com/collections/first')
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args_list[1].kwargs['variables']['after'], 'next')

    @patch('shopify_graphql.execute_graphql_query')
    def test_collection_update_sends_reviewed_fields_and_metafields(self, execute):
        execute.side_effect = [
            {'data': {'collectionUpdate': {'collection': {'id': 'gid://shopify/Collection/123'}, 'userErrors': []}}},
            {'data': {'metafieldsSet': {'metafields': [], 'userErrors': []}}},
        ]
        result = update_collection_metadata('gid://shopify/Collection/123', {
            'title': 'Improved Collection',
            'handle': 'improved-collection',
            'seo_title': 'Improved Collection SEO Title',
            'metafields': [{'namespace': 'custom', 'key': 'subtitle', 'type': 'single_line_text_field', 'value': 'Curated art'}],
        }, 'example.myshopify.com', 'token')
        self.assertTrue(result['success'])
        collection_input = execute.call_args_list[0].args[1]['input']
        self.assertTrue(collection_input['redirectNewHandle'])
        self.assertEqual(collection_input['seo']['title'], 'Improved Collection SEO Title')
        metafield_input = execute.call_args_list[1].args[1]['metafields'][0]
        self.assertEqual(metafield_input['ownerId'], 'gid://shopify/Collection/123')


if __name__ == '__main__':
    unittest.main()
