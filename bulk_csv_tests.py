import csv
import io
import unittest

from bulk_csv import transform_csv


class BulkCsvTests(unittest.TestCase):
    def data(self):
        return {'original_csv': {
            'headers': ['Handle', 'Title', 'Option1 Name', 'Option1 Value', 'Variant SKU', 'Variant Price', 'Image Src'],
            'rows': [
                {'Handle': 'one', 'Title': 'One', 'Option1 Name': 'Size', 'Option1 Value': 'A4', 'Variant SKU': 'ONE-A4', 'Variant Price': '10', 'Image Src': 'main.jpg'},
                {'Handle': 'one', 'Title': '', 'Option1 Name': 'Size', 'Option1 Value': 'A3', 'Variant SKU': 'ONE-A3', 'Variant Price': '20', 'Image Src': 'second.jpg'},
                {'Handle': 'two', 'Title': 'Two', 'Option1 Name': 'Size', 'Option1 Value': 'A4', 'Variant SKU': 'TWO-A4', 'Variant Price': '30', 'Image Src': 'two.jpg'},
            ]}, 'enhancements': [{'originalIndex': 0, 'enhanced': {'handle': 'one', 'title': 'New'}}]}

    def render(self, data):
        return list(csv.DictReader(io.StringIO(transform_csv(data))))

    def test_normal_edit_preserves_variant_rows_and_skus(self):
        data = self.data()
        rows = self.render(data)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row['Variant SKU'] for row in rows], ['ONE-A4', 'ONE-A3', 'TWO-A4'])
        self.assertEqual([row['Title'] for row in rows], ['New', '', 'Two'])

    def test_first_variant_edit_does_not_duplicate_sku(self):
        data = self.data()
        data['enhancements'][0]['enhanced']['variant_sku'] = 'NEW'
        rows = self.render(data)
        self.assertEqual([r['Variant SKU'] for r in rows], ['NEW', 'ONE-A3', 'TWO-A4'])

    def test_replacement_occurs_once_only_on_selected_product(self):
        data = self.data()
        data.update(variant_override_enabled=True, variant_definitions=[
            {'name': 'Small', 'price': '1', 'quantity': 0}, {'name': 'Large', 'price': '2', 'quantity': 5}])
        rows = self.render(data)
        selected = [r for r in rows if r['Handle'] == 'one']
        self.assertEqual([r['Option1 Value'] for r in selected], ['Small', 'Large', ''])
        self.assertEqual(selected[0]['Variant Inventory Qty'], '0')
        self.assertEqual(selected[2]['Image Src'], 'second.jpg')
        other = next(r for r in rows if r['Handle'] == 'two')
        self.assertEqual(other['Variant SKU'], 'TWO-A4')
        self.assertEqual(other['Variant Price'], '30')

    def test_edited_handle_uses_original_identity(self):
        data = self.data()
        data['enhancements'][0]['enhanced']['handle'] = 'new-handle'
        self.assertEqual([r['Handle'] for r in self.render(data)], ['new-handle', 'new-handle', 'two'])

    def test_blank_variant_and_product_values_are_exported(self):
        data = self.data()
        data['enhancements'][0]['enhanced'].update(title='', variant_sku='', variant_price=0, status='draft')
        row = self.render(data)[0]
        self.assertEqual((row['Title'], row['Variant SKU'], row['Variant Price'], row['Status']), ('', '', '0', 'draft'))

    def test_current_headers_are_reused(self):
        data = {'original_csv': {'headers': ['URL handle', 'Title', 'Price', 'SKU'],
            'rows': [{'URL handle': 'one', 'Title': 'One', 'Price': '10', 'SKU': 'OLD'}]},
            'enhancements': [{'originalIndex': 0, 'enhanced': {'handle': 'one', 'variant_price': '12', 'variant_sku': 'NEW'}}]}
        row = self.render(data)[0]
        self.assertEqual(row['Price'], '12')
        self.assertEqual(row['SKU'], 'NEW')
        self.assertNotIn('Variant Price', row)
        self.assertNotIn('Handle', row)

    def test_dynamic_metafield_uses_supported_header(self):
        data = self.data()
        data['enhancements'][0]['enhanced']['mf__custom__subtitle'] = 'Hello'
        self.assertEqual(self.render(data)[0]['product.metafields.custom.subtitle'], 'Hello')

    def test_variant_image_does_not_replace_product_images(self):
        data = self.data()
        data['use_main_image_per_variant'] = True
        rows = self.render(data)
        self.assertEqual(rows[1]['Image Src'], 'second.jpg')
        self.assertEqual(rows[1]['Variant Image'], 'main.jpg')

    def test_legacy_blank_handle_continuations(self):
        data = self.data()
        data['original_csv']['rows'][1]['Handle'] = ''
        self.assertEqual(self.render(data)[1]['Handle'], 'one')

    def test_invalid_replacement_is_rejected(self):
        data = self.data()
        data.update(variant_override_enabled=True, variant_definitions=[{'name': 'Small', 'price': 'NaN'}])
        with self.assertRaises(ValueError):
            self.render(data)

    def test_thousand_products_have_exactly_one_replacement_group_each(self):
        data = self.data()
        data['original_csv']['rows'] = [{'Handle': str(i), 'Title': str(i), 'Option1 Value': size}
                                        for i in range(1000) for size in ('A4', 'A3')]
        data['enhancements'] = [{'originalIndex': i, 'enhanced': {'handle': str(i)}} for i in range(1000)]
        data.update(variant_override_enabled=True, variant_definitions=[{'name': 'Small', 'price': '1'}, {'name': 'Large', 'price': '2'}])
        rows = self.render(data)
        self.assertEqual(len(rows), 2000)
        self.assertTrue(all(row['Handle'] for row in rows))


if __name__ == '__main__':
    unittest.main()
