#!/usr/bin/env python3
"""
Debug the category field API response
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

def debug_category_api_response():
    """Debug what happens when we try to set category field"""
    
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
          category {
            id
            fullName
          }
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
            'title': f'DEBUG CATEGORY {unique_id}',
            'productType': 'Poster',
            'category': "gid://shopify/TaxonomyCategory/sg-4-17-1-17-1",
            'status': 'DRAFT',
            'descriptionHtml': '<p>Debug category field</p>'
        }
    }
    
    try:
        logger.info("Sending category test request...")
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        
        logger.info(f"Response status: {response.status_code}")
        logger.info(f"Response headers: {dict(response.headers)}")
        
        try:
            result = response.json()
            logger.info(f"Full API response: {json.dumps(result, indent=2)}")
            
            if 'errors' in result:
                logger.error(f"GraphQL errors: {result['errors']}")
                
            if 'data' in result:
                product_data = result.get('data', {}).get('productCreate', {})
                if product_data.get('userErrors'):
                    logger.error(f"User errors: {product_data['userErrors']}")
                    
                if product_data.get('product'):
                    logger.info("Product created successfully!")
                    logger.info(f"Product: {product_data['product']}")
                else:
                    logger.error("No product in response")
            else:
                logger.error("No data in response")
                
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            logger.error(f"Raw response: {response.text}")
            
    except Exception as e:
        logger.error(f"Request error: {e}")

def test_without_category_field():
    """Test creating product without category field for comparison"""
    
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
    
    unique_id = int(time.time() * 1000) + 100
    variables = {
        'input': {
            'title': f'NO CATEGORY TEST {unique_id}',
            'productType': 'Poster',
            'status': 'DRAFT',
            'descriptionHtml': '<p>No category field test</p>'
        }
    }
    
    try:
        logger.info("Testing without category field...")
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        logger.info(f"No-category response: {json.dumps(result, indent=2)}")
        
        if result.get('data', {}).get('productCreate', {}).get('product'):
            logger.info("✅ Product without category created successfully")
            return True
        else:
            logger.error("❌ Failed to create product without category")
            return False
            
    except Exception as e:
        logger.error(f"No-category test error: {e}")
        return False

if __name__ == "__main__":
    logger.info("🔍 DEBUGGING CATEGORY FIELD API")
    
    logger.info("\n=== TEST 1: With Category Field ===")
    debug_category_api_response()
    
    logger.info("\n=== TEST 2: Without Category Field (Control) ===")
    test_without_category_field()