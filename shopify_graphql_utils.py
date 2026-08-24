"""
Shopify GraphQL API utilities for product creation
REST API was deprecated in April 2024 - GraphQL is now required
"""

import requests
import logging
import os
import json
import time
from shopify_graphql import format_description_html


logger = logging.getLogger(__name__)


def _resolve_gql_utils_credentials(shop_domain=None, access_token=None):
    """Resolve Shopify credentials for graphql_utils module."""
    if shop_domain and access_token:
        return shop_domain, access_token
    try:
        from shop_helpers import get_shop_credentials
        sd, at = get_shop_credentials()
        if sd and at:
            return sd, at
    except Exception:
        pass
    return None, None


def create_product_with_graphql(metadata, filename, frame_paths=None, product_type_override=None,
                                shop_domain=None, access_token=None):
    """Create a Shopify product using GraphQL (required as of 2024-04)"""
    store_url, token = _resolve_gql_utils_credentials(shop_domain, access_token)
    if not store_url or not token:
        logger.error("Missing Shopify credentials")
        return None
    
    store_url = store_url.replace('https://', '').replace('http://', '')
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': token
    }
    
    # Use productSet mutation for complete product creation with variants
    mutation = '''
    mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) {
      productSet(input: $input, synchronous: $synchronous) {
        product {
          id
          title
          handle
          status
          variants(first: 10) {
            nodes {
              id
              price
              selectedOptions {
                name
                optionValue {
                  name
                }
              }
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    '''
    
    # Create unique handle to avoid conflicts
    unique_id = int(time.time() * 1000)
    handle = f"poster-{unique_id}"
    
    # Define variants with pricing
    variants = [
        {"optionValues": [{"optionName": "Size", "name": "30x40 cm"}], "price": "11.99"},
        {"optionValues": [{"optionName": "Size", "name": "40x50 cm"}], "price": "12.99"},
        {"optionValues": [{"optionName": "Size", "name": "50x70 cm"}], "price": "13.99"},
        {"optionValues": [{"optionName": "Size", "name": "A1 - 59.4 x 84.1 cm"}], "price": "14.99"},
        {"optionValues": [{"optionName": "Size", "name": "A2 - 42 x 59.4 cm"}], "price": "13.49"},
        {"optionValues": [{"optionName": "Size", "name": "A3 - 29.7 x 42 cm"}], "price": "11.99"},
        {"optionValues": [{"optionName": "Size", "name": "A4 - 21 x 29.7 cm"}], "price": "6.99"}
    ]
    
    variables = {
        'synchronous': True,
        'input': {
            'title': metadata.get('title', f'Poster - {filename}'),
            'descriptionHtml': format_description_html(metadata.get('description', 'Beautiful poster artwork')),
            'vendor': 'Listing Cannon',
            'productType': product_type_override or 'Poster',
            'status': 'ACTIVE',
            'handle': handle,
            'tags': metadata.get('tags', []),
            'productOptions': [
                {
                    'name': 'Size',
                    'position': 1,
                    'values': [
                        {'name': '30x40 cm'},
                        {'name': '40x50 cm'},
                        {'name': '50x70 cm'},
                        {'name': 'A1 - 59.4 x 84.1 cm'},
                        {'name': 'A2 - 42 x 59.4 cm'},
                        {'name': 'A3 - 29.7 x 42 cm'},
                        {'name': 'A4 - 21 x 29.7 cm'}
                    ]
                }
            ],
            'variants': variants
        }
    }
    
    graphql_request = {
        'query': mutation,
        'variables': variables
    }
    
    try:
        logger.info(f"Creating product via GraphQL: {variables['input']['title']}")
        response = requests.post(graphql_url, headers=headers, json=graphql_request, timeout=30)
        logger.info(f"GraphQL response status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            
            if 'data' in result and 'productSet' in result['data']:
                product_set = result['data']['productSet']
                
                if product_set['userErrors']:
                    logger.error("GraphQL validation errors:")
                    for error in product_set['userErrors']:
                        logger.error(f"  - {error['field']}: {error['message']}")
                    return None
                
                elif product_set['product']:
                    product = product_set['product']
                    logger.info(f"✅ GraphQL product created: {product['id']}")
                    
                    # Extract numeric ID from GraphQL GID
                    numeric_id = product['id'].split('/')[-1]
                    
                    return {
                        'id': numeric_id,
                        'title': product['title'],
                        'handle': product['handle'],
                        'admin_url': f"https://{store_url}/admin/products/{numeric_id}",
                        'public_url': f"https://{store_url}/products/{product['handle']}"
                    }
            
            logger.error(f"Unexpected GraphQL response: {result}")
            return None
        elif response.status_code == 404:
            logger.error("GraphQL endpoint not found - may need GraphQL permissions in Shopify app")
            return None
        else:
            logger.error(f"GraphQL request failed: {response.status_code}")
            logger.error(f"Response: {response.text[:300]}")
            return None
            
    except Exception as e:
        logger.error(f"GraphQL product creation failed: {str(e)}")
        return None

def test_graphql_access(shop_domain=None, access_token=None):
    """Test if GraphQL API is accessible"""
    store_url, token = _resolve_gql_utils_credentials(shop_domain, access_token)
    if not store_url or not token:
        return False, "Missing credentials"
    
    store_url = store_url.replace('https://', '').replace('http://', '')
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': token
    }
    
    test_query = '''
    {
      shop {
        name
        id
      }
    }
    '''
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': test_query}, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if 'data' in result and 'shop' in result['data']:
                return True, f"GraphQL access confirmed for {result['data']['shop']['name']}"
            else:
                return False, f"GraphQL returned unexpected structure: {result}"
        elif response.status_code == 404:
            return False, "GraphQL endpoint not found - check app permissions"
        else:
            return False, f"GraphQL access failed: {response.status_code}"
            
    except Exception as e:
        return False, f"GraphQL test error: {str(e)}"