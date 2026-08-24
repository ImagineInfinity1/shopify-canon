#!/usr/bin/env python3
"""
Direct test with specific Shopify taxonomy categories
"""

import requests
import json
import os
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
store_url = "85dfe8-3.myshopify.com"

# Direct test categories based on known Shopify taxonomy
TEST_CATEGORIES = [
    # Standard art categories that should exist in Shopify
    "Art",
    "Artwork", 
    "Posters",
    "Prints",
    "Wall Art",
    "Visual Arts",
    "Fine Art",
    "Digital Art",
    "Canvas Art",
    "Photography"
]

def create_test_with_simple_categories():
    """Test with simple, standard categories"""
    
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    mutation = '''
    mutation productCreate($input: ProductInput!) {
      productCreate(input: $input) {
        product {
          id
          title
          productType
        }
        userErrors {
          field
          message
        }
      }
    }
    '''
    
    results = []
    base_time = int(time.time() * 1000)
    
    for i, category in enumerate(TEST_CATEGORIES, 1):
        unique_id = base_time + i
        variables = {
            'input': {
                'title': f'SIMPLE CAT TEST {i}: {category}',
                'productType': category,
                'status': 'DRAFT',
                'descriptionHtml': f'<p>Testing simple category: {category}</p>'
            }
        }
        
        try:
            response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
            result = response.json()
            
            if 'data' in result and result['data']['productCreate']['product']:
                product = result['data']['productCreate']['product']
                logger.info(f"✅ Test {i}: Created product with productType: '{product['productType']}'")
                results.append({
                    'test': i,
                    'category': category,
                    'product_id': product['id'],
                    'success': True
                })
            else:
                logger.error(f"❌ Test {i} ({category}) failed: {result}")
                results.append({
                    'test': i,
                    'category': category,
                    'product_id': None,
                    'success': False
                })
                
            time.sleep(0.3)  # Small delay
            
        except Exception as e:
            logger.error(f"❌ Test {i} ({category}) error: {e}")
            results.append({
                'test': i,
                'category': category,
                'product_id': None,
                'success': False
            })
    
    return results

def verify_simple_categories():
    """Check which simple category products appear correctly"""
    
    rest_url = f"https://{store_url}/admin/api/2025-07/products.json?fields=id,title,product_type&limit=30&order=created_at%20desc"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    try:
        response = requests.get(rest_url, headers=headers, timeout=30)
        result = response.json()
        
        logger.info("SIMPLE CATEGORY TEST RESULTS:")
        if 'products' in result:
            for product in result['products']:
                if 'SIMPLE CAT TEST' in product['title']:
                    has_category = bool(product.get('product_type'))
                    status = "✅ CATEGORY SET" if has_category else "❌ NO CATEGORY"
                    logger.info(f"{status}: {product['title']} = '{product.get('product_type', 'NONE')}'")
        
    except Exception as e:
        logger.error(f"Verification error: {e}")

if __name__ == "__main__":
    logger.info("🧪 TESTING SIMPLE STANDARD CATEGORIES")
    
    results = create_test_with_simple_categories()
    
    # Wait for processing
    time.sleep(2)
    
    # Verify
    verify_simple_categories()
    
    # Summary
    success_count = sum(1 for r in results if r['success'])
    logger.info(f"\n🏁 CREATED {success_count}/{len(results)} PRODUCTS")
    
    for result in results:
        status = "✅" if result['success'] else "❌"
        logger.info(f"{status} Test {result['test']}: '{result['category']}'")