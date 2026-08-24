#!/usr/bin/env python3
"""
Find the correct Shopify taxonomy codes for art/poster categories
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

def test_art_related_codes():
    """Test various art-related taxonomy codes based on patterns"""
    
    # Based on the working patterns:
    # hg-3-17 = Home & Garden > Decor > Clocks
    # So Home & Garden > Decor > Artwork might be hg-3-X where X is different
    # Arts & Entertainment might start with ae-
    
    potential_art_codes = [
        # Home & Garden > Decor variations (hg-3-X pattern)
        "gid://shopify/TaxonomyCategory/hg-3-1",   # Home & Garden > Decor > ?
        "gid://shopify/TaxonomyCategory/hg-3-2",   
        "gid://shopify/TaxonomyCategory/hg-3-5", 
        "gid://shopify/TaxonomyCategory/hg-3-10",
        "gid://shopify/TaxonomyCategory/hg-3-15",
        "gid://shopify/TaxonomyCategory/hg-3-20",
        
        # Arts & Entertainment patterns (ae-X-Y)
        "gid://shopify/TaxonomyCategory/ae-1",     # Arts & Entertainment base
        "gid://shopify/TaxonomyCategory/ae-2",     
        "gid://shopify/TaxonomyCategory/ae-1-1",   
        "gid://shopify/TaxonomyCategory/ae-1-2",
        "gid://shopify/TaxonomyCategory/ae-2-1",
        "gid://shopify/TaxonomyCategory/ae-2-2",
        
        # Other common prefixes that might work
        "gid://shopify/TaxonomyCategory/art",      # Simple art
        "gid://shopify/TaxonomyCategory/poster",   # Simple poster
        "gid://shopify/TaxonomyCategory/print",    # Simple print
        "gid://shopify/TaxonomyCategory/va",       # Visual Arts abbreviation
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
    
    working_codes = []
    base_time = int(time.time() * 1000)
    
    for i, taxonomy_id in enumerate(potential_art_codes, 1):
        unique_id = base_time + i
        variables = {
            'input': {
                'title': f'ART CODE TEST {i}: {taxonomy_id.split("/")[-1]}',
                'category': taxonomy_id,
                'status': 'DRAFT',
                'descriptionHtml': f'<p>Testing art code: {taxonomy_id}</p>'
            }
        }
        
        try:
            logger.info(f"Test {i}: Trying {taxonomy_id}")
            response = requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables}, timeout=30)
            result = response.json()
            
            if 'errors' in result:
                error_msg = result['errors'][0]['message']
                if 'Invalid product_taxonomy_node_id' in error_msg:
                    logger.info(f"❌ Test {i}: Invalid ID")
                else:
                    logger.error(f"❌ Test {i}: Other error: {error_msg}")
            elif result.get('data', {}).get('productCreate', {}).get('product'):
                product = result['data']['productCreate']['product']
                category_info = product.get('category', {})
                
                logger.info(f"✅ TEST {i} SUCCESS: {taxonomy_id}")
                if category_info:
                    full_name = category_info.get('fullName', 'N/A')
                    logger.info(f"   Category: {full_name}")
                    working_codes.append({
                        'code': taxonomy_id,
                        'full_name': full_name,
                        'product_id': product['id']
                    })
                    
                    # If this looks art-related, we found gold!
                    if any(art_term in full_name.lower() for art_term in ['art', 'poster', 'print', 'visual', 'artwork']):
                        logger.info(f"🎯 FOUND ART-RELATED CATEGORY: {full_name}")
                
            time.sleep(0.8)  # Slower to avoid rate limits
            
        except Exception as e:
            logger.error(f"❌ Test {i} exception: {e}")
    
    return working_codes

def test_sequential_hg_codes():
    """Test sequential Home & Garden > Decor codes to find artwork"""
    
    # We know hg-3-17 = Clocks, so try around that area
    hg_codes_to_test = [
        f"gid://shopify/TaxonomyCategory/hg-3-{i}" for i in range(12, 25)
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
    
    artwork_codes = []
    base_time = int(time.time() * 1000) + 100
    
    for i, taxonomy_id in enumerate(hg_codes_to_test, 1):
        unique_id = base_time + i
        variables = {
            'input': {
                'title': f'HG SEQ TEST {i}',
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
                    logger.info(f"✅ {taxonomy_id} = {full_name}")
                    
                    if any(art_term in full_name.lower() for art_term in ['art', 'poster', 'print', 'artwork']):
                        logger.info(f"🎯 ARTWORK FOUND: {taxonomy_id} = {full_name}")
                        artwork_codes.append({'code': taxonomy_id, 'name': full_name})
            else:
                logger.info(f"❌ {taxonomy_id} invalid")
                
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error testing {taxonomy_id}: {e}")
    
    return artwork_codes

if __name__ == "__main__":
    logger.info("🎯 FINDING CORRECT ART/POSTER TAXONOMY CODES")
    
    logger.info("\n=== TEST 1: General Art Code Patterns ===")
    working_codes = test_art_related_codes()
    
    logger.info("\n=== TEST 2: Sequential Home & Garden Decor Codes ===")
    artwork_codes = test_sequential_hg_codes()
    
    logger.info(f"\n🏁 RESULTS:")
    logger.info(f"Working codes found: {len(working_codes)}")
    logger.info(f"Art-specific codes: {len(artwork_codes)}")
    
    all_art_codes = working_codes + artwork_codes
    if all_art_codes:
        logger.info("\n🎯 ART-RELATED TAXONOMY CODES DISCOVERED:")
        for code_info in all_art_codes:
            logger.info(f"  {code_info['code']} = {code_info.get('full_name', code_info.get('name', 'Unknown'))}")
    else:
        logger.warning("❌ No art-related codes found in this test batch")