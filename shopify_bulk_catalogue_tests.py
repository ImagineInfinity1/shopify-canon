import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from shopify_graphql import (
    _variant_update_input_from_data,
    add_product_images_staged_graphql,
    publish_to_channels_graphql,
    stream_product_catalogue_bulk_result,
)


class ShopifyBulkCatalogueTests(unittest.TestCase):
    @patch("shopify_graphql.execute_graphql_query")
    def test_publications_are_sent_in_one_bulk_mutation(self, execute):
        execute.return_value = {
            "data": {"publishablePublish": {"publishable": {"availablePublicationsCount": {"count": 2}}, "userErrors": []}}
        }
        result = publish_to_channels_graphql(
            "gid://shopify/Product/1",
            ["gid://shopify/Publication/1", "gid://shopify/Publication/2"],
            shop_domain="test.myshopify.com",
            access_token="secret",
        )
        self.assertTrue(result)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(execute.call_args.args[1]["input"]), 2)

    @patch("shopify_graphql.requests.post")
    @patch("shopify_graphql.execute_graphql_query")
    def test_staged_images_use_two_graphql_mutations(self, execute, post):
        execute.side_effect = [
            {"data": {"stagedUploadsCreate": {"stagedTargets": [{
                "url": "https://storage.invalid/upload",
                "resourceUrl": "https://storage.invalid/resource.jpg",
                "parameters": [{"name": "key", "value": "value"}],
            }], "userErrors": []}}},
            {"data": {"productCreateMedia": {"media": [{
                "id": "gid://shopify/MediaImage/7", "alt": "Art print", "status": "UPLOADED"
            }], "mediaUserErrors": []}}},
        ]
        post.return_value.raise_for_status.return_value = None
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        handle.write(b"jpeg")
        handle.close()
        try:
            result = add_product_images_staged_graphql(
                "gid://shopify/Product/1",
                [{"image_path": handle.name, "seo_filename": "art-print.jpg", "alt_text": "Art print"}],
                shop_domain="test.myshopify.com",
                access_token="secret",
            )
            self.assertEqual(result, ["gid://shopify/MediaImage/7"])
            self.assertEqual(execute.call_count, 2)
            post.assert_called_once()
        finally:
            os.unlink(handle.name)

    def test_variant_weight_is_sent_as_inventory_measurement(self):
        payload = _variant_update_input_from_data({
            "id": "gid://shopify/ProductVariant/4",
            "sku": "ONE-A4",
            "weight": "275",
            "weight_unit": "g",
        })
        self.assertEqual(payload["inventoryItem"]["sku"], "ONE-A4")
        self.assertEqual(
            payload["inventoryItem"]["measurement"]["weight"],
            {"value": 275.0, "unit": "GRAMS"},
        )

    def test_variant_weight_rejects_invalid_units(self):
        with self.assertRaisesRegex(ValueError, "grams, kilograms, ounces, or pounds"):
            _variant_update_input_from_data({
                "id": "gid://shopify/ProductVariant/4",
                "weight": "275",
                "weight_unit": "stones",
            })

    @patch("shopify_graphql.requests.get")
    def test_jsonl_export_normalizes_product_children_and_offsets(self, get):
        product_id = "gid://shopify/Product/1"
        rows = [
            {"__typename": "Product", "id": product_id, "title": "One", "handle": "one", "tags": ["Art"]},
            {"__typename": "Collection", "__parentId": product_id, "id": "gid://shopify/Collection/2", "title": "Prints"},
            {"__typename": "MediaImage", "__parentId": product_id, "id": "gid://shopify/MediaImage/3", "image": {"url": "https://cdn.example/one.jpg"}},
            {"__typename": "ProductVariant", "__parentId": product_id, "id": "gid://shopify/ProductVariant/4", "sku": "ONE-A4", "price": "12.00", "taxable": True, "selectedOptions": [{"name": "Size", "value": "A4"}], "inventoryItem": {"tracked": True, "unitCost": {"amount": "3.25", "currencyCode": "GBP"}}},
            {"__typename": "Metafield", "__parentId": product_id, "namespace": "custom", "key": "color", "type": "single_line_text_field", "value": "Blue"},
        ]
        response = Mock()
        response.iter_lines.return_value = [json.dumps(row).encode("utf-8") for row in rows]
        get.return_value = response
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        output_path = handle.name
        handle.close()
        try:
            offsets = stream_product_catalogue_bulk_result("https://example.invalid/export.jsonl", output_path)
            self.assertEqual(offsets, [0])
            with open(output_path, "rb") as output:
                output.seek(offsets[0])
                product = json.loads(output.readline().decode("utf-8"))
            self.assertEqual(product["title"], "One")
            self.assertEqual(product["collections"], "Prints")
            self.assertEqual(product["variant_sku"], "ONE-A4")
            self.assertEqual(product["metafield_color"], "Blue")
            self.assertEqual(product["media_count"], 1)
            self.assertEqual(product["variant_options"], "A4")
            self.assertTrue(product["variant_inventory_tracked"])
            self.assertTrue(product["variant_taxable"])
            self.assertEqual(product["variant_unit_cost"], "3.25")
            self.assertEqual(product["variant_unit_cost_currency"], "GBP")
            response.close.assert_called_once()
        finally:
            os.unlink(output_path)


if __name__ == "__main__":
    unittest.main()
