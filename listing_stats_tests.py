import unittest
from unittest.mock import patch

import shopify_listing_stats as stats


class ShopifyListingStatsTests(unittest.TestCase):
    def test_helpers_and_merge(self):
        self.assertEqual(stats.numeric_shopify_id("gid://shopify/Product/123"), "123")
        self.assertEqual(stats.product_handle_from_path("/products/blue-print?variant=4"), "blue-print")
        self.assertIsNone(stats.product_handle_from_path("/collections/all"))
        self.assertIn("-90d", stats.shopifyql_period("90d"))
        rows = stats.merge_listing_stats(
            [{"product_id": "123", "handle": "blue-print", "title": "Blue print"}],
            [
                {"product_id": "gid://shopify/Product/123", "orders": "2", "net_items_sold": "3", "net_sales": "35.50"},
                {"product_id": "123", "orders": 1, "net_items_sold": 1, "net_sales": 10},
            ],
            [
                {"landing_page_path": "/products/blue-print", "sessions": "8"},
                {"landing_page_path": "/products/blue-print?utm_source=test", "sessions": 2},
            ],
        )
        self.assertEqual(rows[0]["orders"], 3)
        self.assertEqual(rows[0]["units_sold"], 4)
        self.assertEqual(rows[0]["net_sales"], 45.5)
        self.assertEqual(rows[0]["visits"], 10)
        self.assertEqual(rows[0]["conversion_rate"], 0.3)

    @patch("shopify_listing_stats.execute_graphql_query")
    def test_catalogue_paginates_every_product(self, execute):
        def page(product_id, title, status, has_next, cursor):
            return {
                "data": {
                    "shop": {"currencyCode": "GBP"},
                    "products": {
                        "nodes": [{
                            "id": f"gid://shopify/Product/{product_id}",
                            "legacyResourceId": product_id,
                            "title": title,
                            "handle": title.lower(),
                            "status": status,
                            "featuredMedia": None,
                            "priceRangeV2": None,
                        }],
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    },
                }
            }

        execute.side_effect = [page("1", "One", "ACTIVE", True, "next"), page("2", "Two", "DRAFT", False, None)]
        currency, products = stats.load_catalogue("example.myshopify.com", "token")
        self.assertEqual(currency, "GBP")
        self.assertEqual([row["product_id"] for row in products], ["1", "2"])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args_list[1].args[1]["after"], "next")

    @patch("shopify_listing_stats.time.sleep")
    @patch("shopify_listing_stats.execute_graphql_query")
    def test_analytics_retries_rate_limits_and_splits_reports(self, execute, sleep):
        execute.side_effect = [
            RuntimeError("Shopify GraphQL error: Rate limited. Please retry later."),
            {"data": {"report": {"tableData": {"rows": [{"product_id": "1", "orders": 2}]}, "parseErrors": []}}},
            {"data": {"report": {"tableData": {"rows": [{"landing_page_path": "/products/one", "sessions": 4}]}, "parseErrors": []}}},
        ]

        sales, traffic = stats.load_analytics("example.myshopify.com", "token", "all")

        self.assertEqual(sales[0]["orders"], 2)
        self.assertEqual(traffic[0]["sessions"], 4)
        self.assertEqual(execute.call_count, 3)
        sleep.assert_any_call(15)
        queries = [call.args[1]["reportQuery"] for call in execute.call_args_list]
        self.assertIn("FROM sales", queries[0])
        self.assertIn("FROM sales", queries[1])
        self.assertIn("FROM sessions", queries[2])


if __name__ == "__main__":
    unittest.main()
