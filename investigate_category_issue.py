#!/usr/bin/env python3
"""
Deep investigation into why categories aren't appearing in Shopify admin
"""

import requests
import json
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
store_url = "85dfe8-3.myshopify.com"

def check_just_created_product():
    """Check the product that was just created"""
    product_id = "15087064744259"  # From the logs
    
    # Check via REST API
    rest_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    try:
        response = requests.get(rest_url, headers=headers, timeout=30)
        result = response.json()
        
        if 'product' in result:
            product = result['product']
            logger.info(f"JUST CREATED PRODUCT CHECK:")
            logger.info(f"ID: {product['id']}")
            logger.info(f"Title: {product['title']}")
            logger.info(f"product_type: '{product.get('product_type', 'NOT SET')}'")
            logger.info(f"Created at: {product['created_at']}")
            
            if not product.get('product_type'):
                logger.error(f"❌ PROBLEM: Product {product_id} has NO product_type!")
                return False
            else:
                logger.info(f"✅ Product {product_id} HAS product_type: '{product['product_type']}'")
                return True
        else:
            logger.error(f"Failed to get product: {result}")
            return False
            
    except Exception as e:
        logger.error(f"Error checking product: {e}")
        return False

def check_our_test_products():
    """Check if ANY of our test products have categories"""
    
    rest_url = f"https://{store_url}/admin/api/2025-07/products.json?fields=id,title,product_type,created_at&limit=30&order=created_at%20desc"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    try:
        response = requests.get(rest_url, headers=headers, timeout=30)
        result = response.json()
        
        logger.info("CHECKING ALL RECENT TEST PRODUCTS:")
        
        products_with_categories = 0
        products_without_categories = 0
        
        if 'products' in result:
            for product in result['products']:
                has_category = bool(product.get('product_type'))
                
                if any(test_name in product['title'] for test_name in [
                    'TEST', 'TAXONOMY', 'SIMPLE CAT', 'EXACT', 'LAST PART', 'Botanical Wall Art'
                ]):
                    status = "✅ HAS CATEGORY" if has_category else "❌ NO CATEGORY"
                    logger.info(f"{status}: {product['title']} = '{product.get('product_type', 'NONE')}'")
                    
                    if has_category:
                        products_with_categories += 1
                    else:
                        products_without_categories += 1
        
        logger.info(f"\nSUMMARY:")
        logger.info(f"Products WITH categories: {products_with_categories}")
        logger.info(f"Products WITHOUT categories: {products_without_categories}")
        
        if products_without_categories > 0:
            logger.error(f"❌ CRITICAL: {products_without_categories} products have NO categories!")
            return False
        else:
            logger.info(f"✅ All {products_with_categories} test products have categories")
            return True
            
    except Exception as e:
        logger.error(f"Error checking products: {e}")
        return False

def test_create_simple_product():
    """Create one simple test product to verify our API calls are working"""
    
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
    
    import time
    unique_id = int(time.time() * 1000)
    variables = {
        'input': {
            'title': f'FINAL CATEGORY TEST {unique_id}',
            'productType': 'Arts & Entertainment > Visual Arts > Posters',
            'status': 'DRAFT',
            'descriptionHtml': '<p>Final test to verify category system</p>'
        }
    }
    
    try:
        logger.info("Creating new test product...")
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        if 'data' in result and result['data']['productCreate']['product']:
            product = result['data']['productCreate']['product']
            logger.info(f"✅ Created test product: {product['id']}")
            logger.info(f"   Title: {product['title']}")
            logger.info(f"   ProductType: '{product['productType']}'")
            
            # Immediately check it via REST API
            product_gid = product['id']
            product_id = product_gid.split('/')[-1]
            
            time.sleep(1)  # Wait a moment
            
            rest_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}.json?fields=id,title,product_type"
            rest_response = requests.get(rest_url, headers=headers, timeout=30)
            rest_result = rest_response.json()
            
            if 'product' in rest_result:
                rest_product_type = rest_result['product'].get('product_type', 'NOT SET')
                logger.info(f"✅ REST API verification: productType = '{rest_product_type}'")
                
                if rest_product_type:
                    logger.info(f"✅ SUCCESS: Category is set correctly!")
                    return True
                else:
                    logger.error(f"❌ PROBLEM: REST API shows NO category!")
                    return False
            else:
                logger.error(f"❌ Could not verify via REST API: {rest_result}")
                return False
        else:
            logger.error(f"❌ Failed to create test product: {result}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error creating test product: {e}")
        return False

if __name__ == "__main__":
    logger.info("🔍 DEEP INVESTIGATION INTO CATEGORY ISSUE")
    
    logger.info("\n=== CHECK 1: Just Created Product ===")
    just_created_ok = check_just_created_product()
    
    logger.info("\n=== CHECK 2: All Test Products ===")
    test_products_ok = check_our_test_products()
    
    logger.info("\n=== CHECK 3: Create New Test Product ===")
    new_test_ok = test_create_simple_product()
    
    logger.info("\n🏁 INVESTIGATION RESULTS:")
    logger.info(f"Just created product has category: {'✅' if just_created_ok else '❌'}")
    logger.info(f"Test products have categories: {'✅' if test_products_ok else '❌'}")
    logger.info(f"New test product works: {'✅' if new_test_ok else '❌'}")
    
    if not any([just_created_ok, test_products_ok, new_test_ok]):
        logger.error("\n❌ CRITICAL: ALL METHODS FAILING - API ISSUE CONFIRMED")
    elif all([just_created_ok, test_products_ok, new_test_ok]):
        logger.info("\n✅ ALL WORKING - LIKELY SHOPIFY ADMIN DISPLAY ISSUE")
    else:
        logger.warning("\n⚠️ MIXED RESULTS - INVESTIGATE SPECIFIC FAILURES")