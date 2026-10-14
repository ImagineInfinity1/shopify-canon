// Exact UI functions exercised without a browser or Shopify connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/templates/index.html', 'utf8');
const context = {
    getDefaultInventoryLocationId: () => 'location',
    bulkPriceLists: [{id: 'usd-list', currency: 'USD'}],
    prettyBulkJson: JSON.stringify,
    normalizeBulkWeightUnit: value => ({g: 'GRAMS', kg: 'KILOGRAMS'}[value] || value),
};
vm.createContext(context);
for (const name of ['buildEditableVariants', 'buildEditablePriceLists', 'syncFirstVariantField']) {
    const start = source.indexOf('        function ' + name + '(');
    assert(start >= 0, name + ' must exist');
    const end = source.indexOf('\n        }', start) + '\n        }'.length;
    vm.runInContext(source.slice(start, end), context);
}
const product = {handle: 'test', variants: [{id: 'v', title: 'Default Title', price: '0.00',
    weight: 0, weight_unit: 'GRAMS', inventory_quantity: 0, taxable: false, sku: ''}]};
const result = context.buildEditableVariants(product);
assert.equal(result.length, 1);
assert.equal(result[0].id, 'v');
assert.equal(result[0].weight, 0);
assert.equal(result[0].weight_unit, 'GRAMS');
assert.equal(result[0].inventory_quantity, 0);
assert.equal(result[0].sku, '');
assert.equal(result[0].taxable, false);
assert.equal(context.buildEditablePriceLists(product).length, 0);
const genuinePrices = [{price_list_id: 'p', currency: 'USD', prices: [{variant_id: 'v', price: '15'}]}];
assert.equal(JSON.stringify(context.buildEditablePriceLists({...product, price_lists_json: JSON.stringify(genuinePrices)})), JSON.stringify(genuinePrices));
const enhanced = {variants_json: JSON.stringify(result)};
context.syncFirstVariantField(enhanced, 'variant_apply_inventory', 'true');
context.syncFirstVariantField(enhanced, 'variant_inventory_location_id', 'selected');
assert.equal(JSON.parse(enhanced.variants_json)[0].apply_inventory, 'true');
assert.equal(JSON.parse(enhanced.variants_json)[0].inventory_location_id, 'selected');
assert.equal(product.variants[0].inventory_location_id, undefined, 'source must remain unchanged');
console.log('Editor regression checks passed: variants, zero/false values, weight, market prices, inventory controls.');
