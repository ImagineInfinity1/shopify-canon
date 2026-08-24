#!/usr/bin/env python3
"""
Comprehensive test of all possible Shopify product category methods
Since the previous working approach now fails to set categories
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

def method_1_rest_api_create():
    """Method 1: Create product via REST API with product_type"""
    logger.info("=== METHOD 1: REST API Create with product_type ===")
    
    rest_url = f"https://{store_url}/admin/api/2025-07/products.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    unique_id = int(time.time() * 1000)
    data = {
        "product": {
            "title": f"REST CATEGORY TEST {unique_id}",
            "product_type": "Wall Art",
            "vendor": "Test Vendor",
            "status": "draft",
            "body_html": "<p>Test product for category debugging</p>"
        }
    }
    
    try:
        response = requests.post(rest_url, headers=headers, json=data, timeout=30)
        result = response.json()
        
        if 'product' in result:
            product_id = result['product']['id']
            product_type = result['product']['product_type']
            logger.info(f"✅ Method 1: Created product {product_id} with product_type: '{product_type}'")
            return product_id
        else:
            logger.error(f"❌ Method 1 failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 1 error: {e}")
        return None

def method_2_graphql_simple():
    """Method 2: Simple GraphQL productCreate"""
    logger.info("=== METHOD 2: GraphQL productCreate Simple ===")
    
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
    
    unique_id = int(time.time() * 1000)
    variables = {
        'input': {
            'title': f'GraphQL Simple Test {unique_id}',
            'productType': 'Artwork',
            'status': 'DRAFT'
        }
    }
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        if 'data' in result and result['data']['productCreate']['product']:
            product = result['data']['productCreate']['product']
            logger.info(f"✅ Method 2: Created {product['id']} with productType: '{product['productType']}'")
            return product['id']
        else:
            logger.error(f"❌ Method 2 failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 2 error: {e}")
        return None

def method_3_rest_then_update():
    """Method 3: Create via REST then update product_type"""
    logger.info("=== METHOD 3: REST Create + REST Update ===")
    
    # Step 1: Create basic product
    product_id = method_1_rest_api_create()
    if not product_id:
        return None
        
    # Step 2: Update product_type via REST
    rest_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    data = {
        "product": {
            "id": product_id,
            "product_type": "Modern Art"
        }
    }
    
    try:
        response = requests.put(rest_url, headers=headers, json=data, timeout=30)
        result = response.json()
        
        if 'product' in result:
            updated_type = result['product']['product_type']
            logger.info(f"✅ Method 3: Updated product {product_id} to product_type: '{updated_type}'")
            return product_id
        else:
            logger.error(f"❌ Method 3 update failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 3 error: {e}")
        return None

def method_4_graphql_with_vendor():
    """Method 4: GraphQL with additional fields to see if it helps"""
    logger.info("=== METHOD 4: GraphQL with Full Fields ===")
    
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
          vendor
          status
        }
        userErrors {
          field
          message
        }
      }
    }
    '''
    
    unique_id = int(time.time() * 1000)
    variables = {
        'input': {
            'title': f'Full GraphQL Test {unique_id}',
            'productType': 'Posters & Prints',
            'vendor': 'Test Store',
            'status': 'DRAFT',
            'descriptionHtml': '<p>Test product with full fields</p>'
        }
    }
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        if 'data' in result and result['data']['productCreate']['product']:
            product = result['data']['productCreate']['product']
            logger.info(f"✅ Method 4: Created {product['id']} with productType: '{product['productType']}'")
            return product['id']
        else:
            logger.error(f"❌ Method 4 failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 4 error: {e}")
        return None

def method_5_different_api_version():
    """Method 5: Try older API version"""
    logger.info("=== METHOD 5: Alternative API Version (2025-07) ===")
    
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
    
    unique_id = int(time.time() * 1000)
    variables = {
        'input': {
            'title': f'API Version Test {unique_id}',
            'productType': 'Canvas Art',
            'status': 'DRAFT'
        }
    }
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        if 'data' in result and result['data']['productCreate']['product']:
            product = result['data']['productCreate']['product']
            logger.info(f"✅ Method 5: Created {product['id']} with productType: '{product['productType']}'")
            return product['id']
        else:
            logger.error(f"❌ Method 5 failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 5 error: {e}")
        return None

def method_6_productset():
    """Method 6: Use productSet mutation"""
    logger.info("=== METHOD 6: productSet Mutation ===")
    
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    mutation = '''
    mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) {
      productSet(input: $input, synchronous: $synchronous) {
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
    
    unique_id = int(time.time() * 1000)
    variables = {
        'synchronous': True,
        'input': {
            'title': f'ProductSet Test {unique_id}',
            'productType': 'Digital Art',
            'status': 'DRAFT'
        }
    }
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        if 'data' in result and result['data']['productSet']['product']:
            product = result['data']['productSet']['product']
            logger.info(f"✅ Method 6: Created {product['id']} with productType: '{product['productType']}'")
            return product['id']
        else:
            logger.error(f"❌ Method 6 failed: {result}")
            return None
    except Exception as e:
        logger.error(f"❌ Method 6 error: {e}")
        return None

def verify_all_products():
    """Verify which products actually show categories in Shopify admin"""
    logger.info("=== VERIFICATION: Checking All Recent Products ===")
    
    # REST API check
    rest_url = f"https://{store_url}/admin/api/2025-07/products.json?fields=id,title,product_type,created_at&limit=15&order=created_at%20desc"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    try:
        response = requests.get(rest_url, headers=headers, timeout=30)
        result = response.json()
        
        logger.info("RECENT PRODUCTS WITH CATEGORIES:")
        if 'products' in result:
            for product in result['products']:
                category_status = "✅ HAS CATEGORY" if product.get('product_type') else "❌ NO CATEGORY"
                logger.info(f"{category_status}: {product['title']} = '{product.get('product_type', 'NONE')}'")
        else:
            logger.error(f"Verification failed: {result}")
            
    except Exception as e:
        logger.error(f"Verification error: {e}")

if __name__ == "__main__":
    logger.info("🧪 COMPREHENSIVE SHOPIFY CATEGORY TESTING")
    logger.info("Testing 6 different methods to set product categories")
    
    results = {}
    
    # Test all methods
    results['method_1_rest'] = method_1_rest_api_create()
    results['method_2_graphql_simple'] = method_2_graphql_simple()
    results['method_3_rest_update'] = method_3_rest_then_update()
    results['method_4_graphql_full'] = method_4_graphql_with_vendor()
    results['method_5_old_api'] = method_5_different_api_version()
    results['method_6_productset'] = method_6_productset()
    
    # Verification
    verify_all_products()
    
    # Summary
    logger.info("\n🏁 FINAL RESULTS:")
    for method, result in results.items():
        status = "✅ SUCCESS" if result else "❌ FAILED"
        logger.info(f"{method}: {status}")
    
    logger.info("\n📋 Check your Shopify admin to see which products show categories!")