#!/usr/bin/env python3
"""
Find the specific Artwork taxonomy code
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

def find_artwork_category():
    """Look for the specific Artwork category in Home & Garden > Decor"""
    
    # We found hg-3-24 = Decorative Plates, so Artwork is likely further up
    # Let's test more codes in the hg-3-X range
    codes_to_test = [
        f"gid://shopify/TaxonomyCategory/hg-3-{i}" for i in range(25, 40)
    ] + [
        f"gid://shopify/TaxonomyCategory/hg-3-{i}" for i in range(1, 12)  # Fill in gaps
    ] + [
        f"gid://shopify/TaxonomyCategory/hg-4-{i}" for i in range(1, 20)  # Try hg-4 pattern
    ]
    
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
    
    artwork_categories = []
    base_time = int(time.time() * 1000)
    
    for i, taxonomy_id in enumerate(codes_to_test, 1):
        unique_id = base_time + i
        variables = {
            'input': {
                'title': f'ARTWORK SEARCH {i}',
                'category': taxonomy_id,
                'status': 'DRAFT'
            }
        }
        
        try:
            response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
            result = response.json()
            
            if not 'errors' in result and result.get('data', {}).get('productCreate', {}).get('product'):
                product = result['data']['productCreate']['product']
                category_info = product.get('category', {})
                
                if category_info:
                    full_name = category_info.get('fullName', '')
                    
                    # Log all valid categories for reference
                    logger.info(f"✅ {taxonomy_id} = {full_name}")
                    
                    # Look for artwork specifically
                    if 'artwork' in full_name.lower():
                        logger.info(f"🎯 FOUND ARTWORK: {taxonomy_id} = {full_name}")
                        artwork_categories.append({
                            'code': taxonomy_id,
                            'full_name': full_name,
                            'product_id': product['id']
                        })
                    elif any(term in full_name.lower() for term in ['poster', 'print', 'art', 'visual']):
                        logger.info(f"🎨 ART-RELATED: {taxonomy_id} = {full_name}")
                        artwork_categories.append({
                            'code': taxonomy_id,
                            'full_name': full_name,
                            'product_id': product['id']
                        })
            else:
                # Don't log invalid IDs to reduce noise
                pass
                
            time.sleep(0.3)  # Fast but not too fast
            
        except Exception as e:
            logger.error(f"Error testing {taxonomy_id}: {e}")
    
    return artwork_categories

def test_known_working_category():
    """Use the closest working category we found for posters"""
    
    # From our findings, ae-2-1 = "Arts & Entertainment > Hobbies & Creative Arts > Arts & Crafts"
    # This is the closest to what we need for art/posters
    
    test_code = "gid://shopify/TaxonomyCategory/ae-2-1"
    
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
    
    unique_id = int(time.time() * 1000) + 500
    variables = {
        'input': {
            'title': f'POSTER WITH WORKING CATEGORY {unique_id}',
            'productType': 'Poster',  # Keep productType separate
            'category': test_code,    # Set the REAL category field
            'status': 'DRAFT',
            'descriptionHtml': '<p>Testing poster with working category field</p>'
        }
    }
    
    try:
        logger.info(f"Testing poster with working category: {test_code}")
        response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
        result = response.json()
        
        logger.info(f"Full response: {json.dumps(result, indent=2)}")
        
        if result.get('data', {}).get('productCreate', {}).get('product'):
            product = result['data']['productCreate']['product']
            logger.info(f"✅ SUCCESS! Created poster with category!")
            logger.info(f"   Product ID: {product['id']}")
            logger.info(f"   Title: {product['title']}")
            
            category_info = product.get('category', {})
            if category_info:
                logger.info(f"   Category: {category_info.get('fullName', 'N/A')}")
                return {
                    'success': True,
                    'product_id': product['id'],
                    'category_code': test_code,
                    'category_name': category_info.get('fullName')
                }
        else:
            logger.error(f"❌ Failed: {result}")
            return {'success': False}
            
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {'success': False}

if __name__ == "__main__":
    logger.info("🎯 FINDING SPECIFIC ARTWORK TAXONOMY CODE")
    
    logger.info("\n=== SEARCH FOR ARTWORK CATEGORY ===")
    artwork_categories = find_artwork_category()
    
    logger.info("\n=== TEST WORKING CATEGORY FOR POSTERS ===")
    poster_result = test_known_working_category()
    
    logger.info(f"\n🏁 RESULTS:")
    logger.info(f"Artwork categories found: {len(artwork_categories)}")
    logger.info(f"Poster test: {'✅ SUCCESS' if poster_result.get('success') else '❌ FAILED'}")
    
    if artwork_categories:
        logger.info("\n🎨 ARTWORK-RELATED CATEGORIES:")
        for cat in artwork_categories:
            logger.info(f"  {cat['code']} = {cat['full_name']}")
    
    if poster_result.get('success'):
        logger.info(f"\n🎯 RECOMMENDED CATEGORY FOR POSTERS:")
        logger.info(f"  Code: {poster_result['category_code']}")
        logger.info(f"  Name: {poster_result['category_name']}")
        logger.info("  This category can be implemented in our system!")