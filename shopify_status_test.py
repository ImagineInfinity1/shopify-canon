#!/usr/bin/env python3
"""
Comprehensive Shopify API Status Test
Tests all API endpoints and provides complete diagnostic information
"""

import os
import requests
import json
from datetime import datetime

SHOPIFY_STORE_URL = os.environ.get('SHOPIFY_STORE_URL', '')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

def test_shopify_integration():
    """Complete diagnostic test of Shopify integration"""
    print("=== SHOPIFY INTEGRATION DIAGNOSTIC REPORT ===")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print(f"Store URL: {SHOPIFY_STORE_URL}")
    print(f"Token: ...{SHOPIFY_ACCESS_TOKEN[-6:] if SHOPIFY_ACCESS_TOKEN else 'MISSING'}")
    print()
    
    if not SHOPIFY_STORE_URL or not SHOPIFY_ACCESS_TOKEN:
        print("❌ CRITICAL: Missing Shopify credentials")
        return False
    
    store_url = SHOPIFY_STORE_URL.replace('https://', '').replace('http://', '')
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN
    }
    
    # Test 1: REST API Access
    print("1. TESTING REST API ACCESS")
    rest_endpoints = [
        ('Shop Info', f'https://{store_url}/admin/api/2025-07/shop.json', 'GET'),
        ('Products Count', f'https://{store_url}/admin/api/2025-07/products/count.json', 'GET'),
        ('Products List', f'https://{store_url}/admin/api/2025-07/products.json?limit=1', 'GET'),
    ]
    
    rest_working = True
    for name, url, method in rest_endpoints:
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            print(f"   {name}: {response.status_code}")
            
            if response.status_code != 200:
                rest_working = False
                print(f"     Error: {response.text[:100]}")
        except Exception as e:
            rest_working = False
            print(f"   {name}: ERROR - {e}")
    
    print(f"   REST API Status: {'✅ WORKING' if rest_working else '❌ FAILED'}")
    print()
    
    # Test 2: GraphQL API Access
    print("2. TESTING GRAPHQL API ACCESS")
    graphql_endpoints = [
        ('2025-07', f'https://{store_url}/admin/api/2025-07/graphql.json'),
        ('2025-04', f'https://{store_url}/admin/api/2025-04/graphql.json'),
        ('2025-01', f'https://{store_url}/admin/api/2025-01/graphql.json'),
        ('No Version', f'https://{store_url}/admin/api/graphql.json'),
        ('Simple', f'https://{store_url}/admin/api/graphql'),
    ]
    
    test_query = {'query': '{ shop { name myshopifyDomain } }'}
    graphql_working = False
    working_endpoint = None
    
    for version, endpoint in graphql_endpoints:
        try:
            response = requests.post(endpoint, headers=headers, json=test_query, timeout=10)
            print(f"   GraphQL {version}: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                if 'data' in result and 'shop' in result['data']:
                    graphql_working = True
                    working_endpoint = endpoint
                    print(f"     ✅ SUCCESS: {result['data']['shop']['name']}")
                    break
                else:
                    print(f"     Response: {result}")
            elif response.status_code != 404:
                print(f"     Response: {response.text[:100]}")
        except Exception as e:
            print(f"   GraphQL {version}: ERROR - {e}")
    
    print(f"   GraphQL API Status: {'✅ WORKING' if graphql_working else '❌ ALL ENDPOINTS 404'}")
    print()
    
    # Test 3: Product Creation (if GraphQL works)
    if graphql_working and working_endpoint:
        print("3. TESTING PRODUCT CREATION")
        
        mutation = '''
        mutation productCreate($input: ProductInput!) {
          productCreate(input: $input) {
            product {
              id
              title
              handle
            }
            userErrors {
              field
              message
            }
          }
        }
        '''
        
        variables = {
            'input': {
                'title': f'Diagnostic Test Product - {datetime.now().strftime("%Y%m%d_%H%M%S")}',
                'vendor': 'Samila Home',
                'productType': 'Poster'
            }
        }
        
        create_request = {'query': mutation, 'variables': variables}
        
        try:
            create_response = requests.post(working_endpoint, headers=headers, json=create_request, timeout=30)
            print(f"   Product Creation: {create_response.status_code}")
            
            if create_response.status_code == 200:
                create_result = create_response.json()
                
                if 'data' in create_result and 'productCreate' in create_result['data']:
                    product_create = create_result['data']['productCreate']
                    
                    if product_create['product']:
                        product = product_create['product']
                        print(f"   ✅ PRODUCT CREATED SUCCESSFULLY!")
                        print(f"     Product ID: {product['id']}")
                        print(f"     Title: {product['title']}")
                        
                        numeric_id = product['id'].split('/')[-1]
                        print(f"     Admin URL: https://{store_url}/admin/products/{numeric_id}")
                        return True
                    elif product_create['userErrors']:
                        print(f"   ❌ Validation Errors:")
                        for error in product_create['userErrors']:
                            print(f"     - {error['field']}: {error['message']}")
                    else:
                        print(f"   ❌ Empty product response")
                else:
                    print(f"   ❌ Unexpected response: {create_result}")
            else:
                print(f"   ❌ Request failed: {create_response.text[:200]}")
        except Exception as e:
            print(f"   ❌ Product creation error: {e}")
    else:
        print("3. PRODUCT CREATION SKIPPED (GraphQL not available)")
    
    print()
    print("=== DIAGNOSTIC SUMMARY ===")
    print(f"REST API: {'✅ Working' if rest_working else '❌ Failed'}")
    print(f"GraphQL API: {'✅ Working' if graphql_working else '❌ Not Available'}")
    print(f"Product Creation: {'✅ Working' if graphql_working else '❌ Blocked (GraphQL required)'}")
    
    if not graphql_working:
        print()
        print("RECOMMENDED ACTIONS:")
        print("1. Contact Shopify Support to enable GraphQL Admin API access")
        print("2. Verify store plan supports GraphQL Admin API")
        print("3. Check if developer account requires additional permissions")
        print("4. Consider Shopify Plus plan if currently on Basic/Shopify plan")
    
    return graphql_working

if __name__ == "__main__":
    test_shopify_integration()