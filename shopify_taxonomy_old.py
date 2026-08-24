"""
Shopify Taxonomy Integration
Handles proper category assignment using Shopify's taxonomy node GIDs
"""

import os
import requests
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_STORE_URL = "https://85dfe8-3.myshopify.com"

def get_product_taxonomy_categories() -> Dict[str, str]:
    """
    Fetch available product taxonomy categories from Shopify
    Returns dict mapping category descriptions to GID values
    """
    try:
        query = """
        query {
            productTaxonomyNodes(first: 250) {
                edges {
                    node {
                        id
                        name
                        fullName
                        isLeaf
                    }
                }
            }
        }
        """
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN
        }
        
        response = requests.post(
            f"{SHOPIFY_STORE_URL}/admin/api/2025-07/graphql.json",
            json={"query": query},
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if 'errors' in data:
                logger.error(f"GraphQL errors in taxonomy query: {data['errors']}")
                return {}
                
            categories = {}
            nodes = data.get('data', {}).get('productTaxonomyNodes', {}).get('edges', [])
            
            for edge in nodes:
                node = edge['node']
                full_name = node.get('fullName', '')
                node_id = node.get('id', '')
                
                if full_name and node_id and node.get('isLeaf', False):
                    categories[full_name] = node_id
                    logger.debug(f"Added category: {full_name} -> {node_id}")
            
            logger.info(f"✅ Retrieved {len(categories)} taxonomy categories from Shopify")
            
            # Log relevant categories for debugging
            relevant_count = 0
            for full_name in categories.keys():
                if any(term in full_name.lower() for term in ['art', 'home', 'entertainment']):
                    logger.debug(f"Relevant category found: {full_name}")
                    relevant_count += 1
            
            logger.info(f"Found {relevant_count} categories relevant to art/home products")
            return categories
            
        else:
            logger.error(f"Failed to fetch taxonomy: HTTP {response.status_code}")
            logger.error(f"Response: {response.text[:200]}")
            return {}
            
    except Exception as e:
        logger.error(f"Error fetching taxonomy categories: {e}")
        return {}

def find_best_category_match(ai_category: str, available_categories: Dict[str, str]) -> Optional[str]:
    """
    Find the best matching Shopify taxonomy category for AI-suggested category
    
    Args:
        ai_category: AI-generated category like "Art & Entertainment > Visual Arts > Posters"
        available_categories: Dict mapping category names to GIDs
    
    Returns:
        Best matching GID or None if no match found
    """
    if not ai_category or not available_categories:
        return None
    
    # Exact match first
    if ai_category in available_categories:
        logger.info(f"✅ Exact category match found: {ai_category}")
        return available_categories[ai_category]
    
    # Smart matching for poster/art products
    ai_lower = ai_category.lower()
    
    # Priority 1: Arts & Entertainment is perfect for posters/wall art
    if any(term in ai_lower for term in ['art', 'poster', 'entertainment', 'visual']):
        if 'Arts & Entertainment' in available_categories:
            logger.info(f"✅ Smart match: 'Arts & Entertainment' for '{ai_category}'")
            return available_categories['Arts & Entertainment']
    
    # Priority 2: Home & Garden for home decor items
    if any(term in ai_lower for term in ['home', 'decor', 'wall', 'interior']):
        if 'Home & Garden' in available_categories:
            logger.info(f"✅ Smart match: 'Home & Garden' for '{ai_category}'")
            return available_categories['Home & Garden']
    
    # Fallback: Default to Arts & Entertainment for any art-related content
    if 'Arts & Entertainment' in available_categories:
        logger.info(f"✅ Fallback match: 'Arts & Entertainment' for '{ai_category}'")
        return available_categories['Arts & Entertainment']
    
    logger.warning(f"⚠️  No suitable category match found for: {ai_category}")
    return None

def assign_product_category(product_gid: str, category_gid: str) -> bool:
    """
    Assign a category to an existing product using GraphQL mutation
    
    Args:
        product_gid: Product GID like 'gid://shopify/Product/123456'
        category_gid: Category taxonomy node GID
    
    Returns:
        True if successful, False otherwise
    """
    try:
        mutation = """
        mutation productUpdate($input: ProductInput!) {
            productUpdate(input: $input) {
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
        """
        
        variables = {
            "input": {
                "id": product_gid,
                "category": category_gid
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN
        }
        
        response = requests.post(
            f"{SHOPIFY_STORE_URL}/admin/api/2025-07/graphql.json",
            json={"query": mutation, "variables": variables},
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if 'errors' in data:
                logger.error(f"GraphQL errors in category assignment: {data['errors']}")
                return False
                
            result = data.get('data', {}).get('productUpdate', {})
            user_errors = result.get('userErrors', [])
            
            if user_errors:
                logger.error(f"Category assignment errors: {user_errors}")
                return False
                
            product = result.get('product', {})
            category = product.get('category', {})
            
            if category:
                logger.info(f"✅ Category assigned successfully: {category.get('fullName')}")
                return True
            else:
                logger.warning("⚠️  Category assignment completed but no category returned")
                return False
                
        else:
            logger.error(f"Failed to assign category: HTTP {response.status_code}")
            logger.error(f"Response: {response.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"Error assigning category: {e}")
        return False

def implement_smart_category_assignment(product_gid: str, ai_category: str) -> bool:
    """
    Complete category assignment workflow
    
    Args:
        product_gid: Product GID from Shopify
        ai_category: AI-suggested category string
    
    Returns:
        True if category was successfully assigned, False otherwise
    """
    logger.info(f"🏷️  Starting smart category assignment for product {product_gid}")
    logger.info(f"AI suggested category: {ai_category}")
    
    # Step 1: Get available taxonomy categories
    available_categories = get_product_taxonomy_categories()
    if not available_categories:
        logger.error("❌ Could not retrieve taxonomy categories from Shopify")
        return False
    
    # Step 2: Find best matching category
    category_gid = find_best_category_match(ai_category, available_categories)
    if not category_gid:
        logger.error(f"❌ No suitable category found for: {ai_category}")
        return False
    
    # Step 3: Assign the category
    success = assign_product_category(product_gid, category_gid)
    if success:
        logger.info(f"🎉 Category assignment completed successfully!")
        return True
    else:
        logger.error(f"❌ Category assignment failed")
        return False

if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    categories = get_product_taxonomy_categories()
    print(f"Retrieved {len(categories)} categories")
    if categories:
        print("Sample categories:")
        for i, (name, gid) in enumerate(list(categories.items())[:10]):
            print(f"  {name} -> {gid}")