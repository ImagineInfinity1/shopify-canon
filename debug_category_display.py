#!/usr/bin/env python3
"""
Debug why productType values aren't appearing in Shopify admin Category column
"""

import requests
import json
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHOPIFY_STORE_URL = os.environ.get('SHOPIFY_STORE_URL', '')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
store_url = "85dfe8-3.myshopify.com"

def query_recent_products():
    """Query recent products to see actual productType values"""
    
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    query = '''
    query {
      products(first: 10, sortKey: CREATED_AT, reverse: true) {
        nodes {
          id
          title
          productType
          createdAt
        }
      }
    }
    '''
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': query}, timeout=30)
        result = response.json()
        
        logger.info("RECENT PRODUCTS AND THEIR productType VALUES:")
        if 'data' in result and 'products' in result['data']:
            for product in result['data']['products']['nodes']:
                logger.info(f"Title: {product['title']}")
                logger.info(f"ProductType: '{product['productType']}'")
                logger.info(f"Created: {product['createdAt']}")
                logger.info("---")
        else:
            logger.error(f"Query failed: {result}")
            
    except Exception as e:
        logger.error(f"Error querying products: {e}")

def test_rest_api_comparison():
    """Compare GraphQL vs REST API productType handling"""
    
    rest_url = f"https://{store_url}/admin/api/2025-07/products.json?limit=5&order=created_at%20desc"
    headers = {
        'Content-Type': 'application/json', 
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    try:
        response = requests.get(rest_url, headers=headers, timeout=30)
        result = response.json()
        
        logger.info("REST API PRODUCT TYPES:")
        if 'products' in result:
            for product in result['products']:
                logger.info(f"Title: {product['title']}")
                logger.info(f"product_type: '{product['product_type']}'")
                logger.info("---")
        else:
            logger.error(f"REST query failed: {result}")
            
    except Exception as e:
        logger.error(f"Error with REST API: {e}")

def create_test_with_various_types():
    """Create test products with different productType values to debug"""
    
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
    
    test_types = [
        "Posters",
        "Art", 
        "Prints",
        "Wall Decor",
        "Artwork"
    ]
    
    for product_type in test_types:
        variables = {
            'input': {
                'title': f'DEBUG Category Test - {product_type}',
                'productType': product_type,
                'status': 'DRAFT'
            }
        }
        
        try:
            response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
            result = response.json()
            
            if 'data' in result and result['data']['productCreate']['product']:
                product = result['data']['productCreate']['product']
                logger.info(f"✅ Created: {product['title']} with productType: '{product['productType']}'")
            else:
                logger.error(f"❌ Failed to create {product_type}: {result}")
                
        except Exception as e:
            logger.error(f"❌ Error creating {product_type}: {e}")

if __name__ == "__main__":
    logger.info("🔍 DEBUGGING SHOPIFY CATEGORY DISPLAY ISSUE")
    
    logger.info("\n=== STEP 1: Query Recent Products ===")
    query_recent_products()
    
    logger.info("\n=== STEP 2: REST API Comparison ===")
    test_rest_api_comparison()
    
    logger.info("\n=== STEP 3: Create Test Products ===")
    create_test_with_various_types()
    
    logger.info("\n🎯 NEXT: Check Shopify admin to see if new test products show Category values")