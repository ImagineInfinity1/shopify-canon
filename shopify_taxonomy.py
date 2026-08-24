"""
Shopify Taxonomy Integration - Fixed Version
Handles proper category assignment using Shopify's taxonomy node GIDs
"""

import os
import requests
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


def _resolve_taxonomy_credentials(shop_domain=None, access_token=None):
    """Resolve Shopify credentials for taxonomy module."""
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

def get_product_taxonomy_categories() -> Dict[str, str]:
    """
    Return hardcoded taxonomy categories since API fetch is failing
    Returns dict mapping category descriptions to GID values
    """
    logger.info("🔍 Using hardcoded taxonomy categories (API unavailable)")
    
    # Hardcoded categories that work based on successful product creations
    categories = {
        # Most specific poster/art categories
        'Arts & Entertainment > Visual Arts > Posters': 'gid://shopify/ProductTaxonomyNode/sg-2-17-2-17',
        'Arts & Entertainment > Visual Arts > Prints': 'gid://shopify/ProductTaxonomyNode/sg-2-17-2-18',
        'Home & Garden > Decor > Artwork': 'gid://shopify/ProductTaxonomyNode/sg-4-17-1-17',
        'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork': 'gid://shopify/ProductTaxonomyNode/sg-4-17-1-17-1',
        
        # Broader fallback categories
        'Arts & Entertainment > Visual Arts': 'gid://shopify/ProductTaxonomyNode/sg-2-17-2',
        'Arts & Entertainment': 'gid://shopify/ProductTaxonomyNode/sg-2',
        'Home & Garden > Decor': 'gid://shopify/ProductTaxonomyNode/sg-4-17-1',
        'Home & Garden': 'gid://shopify/ProductTaxonomyNode/sg-4',
        
        # Other common categories
        'Apparel & Accessories': 'gid://shopify/ProductTaxonomyNode/sg-3',
        'Electronics': 'gid://shopify/ProductTaxonomyNode/sg-1',
        'Health & Beauty': 'gid://shopify/ProductTaxonomyNode/sg-7',
        'Books & Media': 'gid://shopify/ProductTaxonomyNode/sg-6'
    }
    
    logger.info(f"✅ Using {len(categories)} hardcoded taxonomy categories")
    
    # Log available poster/art categories for debugging
    poster_categories = [cat for cat in categories.keys() if any(term in cat.lower() for term in ['poster', 'print', 'visual', 'artwork'])]
    logger.info(f"Found {len(poster_categories)} poster/art categories: {poster_categories}")
    
    return categories

def find_best_category_match(ai_category: str, available_categories: Dict[str, str]) -> Optional[str]:
    """
    Find the best matching Shopify taxonomy category for AI-suggested category
    Prioritizes most specific categories over broad ones
    """
    print(f"🔍 MAPPING: Starting category mapping for: '{ai_category}'")
    print(f"🔍 MAPPING: Available categories count: {len(available_categories) if available_categories else 0}")
    logger.info(f"🔍 Starting category mapping for: '{ai_category}'")
    logger.info(f"Available categories count: {len(available_categories) if available_categories else 0}")
    
    if not ai_category or not available_categories:
        logger.warning("⚠️ Missing AI category or available categories")
        return None
    
    # Debug: Log some relevant categories
    for key in list(available_categories.keys())[:5]:
        logger.debug(f"Sample available category: {key}")
    
    # Exact match first - this handles manual category input
    if ai_category in available_categories:
        print(f"✅ EXACT MATCH found: {ai_category} -> {available_categories[ai_category]}")
        logger.info(f"✅ EXACT MATCH found: {ai_category} -> {available_categories[ai_category]}")
        return available_categories[ai_category]
    
    # Smart matching with strict priority order - MOST SPECIFIC FIRST
    ai_lower = ai_category.lower()
    
    # PRIORITY 1: Most specific poster/print categories (4+ levels deep)
    level4_categories = [
        'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork',
        'Arts & Entertainment > Hobbies & Creative Arts > Crafts > Scrapbooking & Stamping > Scrapbooking Embellishments > Scrapbooking Stickers'
    ]
    
    for specific_cat in level4_categories:
        if specific_cat in available_categories:
            if any(term in ai_lower for term in ['poster', 'print', 'art', 'wall', 'visual']):
                logger.info(f"✅ LEVEL 4 MATCH: '{specific_cat}' -> {available_categories[specific_cat]}")
                return available_categories[specific_cat]
        else:
            logger.debug(f"❌ Level 4 category '{specific_cat}' not found in available categories")
    
    # PRIORITY 2: Specific 3-level categories  
    level3_categories = [
        'Arts & Entertainment > Visual Arts > Posters',
        'Arts & Entertainment > Visual Arts > Prints',
        'Home & Garden > Decor > Artwork'
    ]
    
    for specific_cat in level3_categories:
        if specific_cat in available_categories:
            if any(term in ai_lower for term in ['poster', 'print', 'art', 'visual']):
                logger.info(f"✅ LEVEL 3 MATCH: '{specific_cat}' -> {available_categories[specific_cat]}")
                return available_categories[specific_cat]
        else:
            logger.debug(f"❌ Level 3 category '{specific_cat}' not found in available categories")
    
    # PRIORITY 3: 2-level categories (only if more specific ones don't exist)
    level2_categories = [
        'Arts & Entertainment > Visual Arts',
        'Home & Garden > Decor'
    ]
    
    for specific_cat in level2_categories:
        if specific_cat in available_categories:
            if any(term in ai_lower for term in ['art', 'visual', 'poster', 'print']):
                logger.info(f"✅ LEVEL 2 MATCH: '{specific_cat}' -> {available_categories[specific_cat]}")
                return available_categories[specific_cat]
        else:
            logger.debug(f"❌ Level 2 category '{specific_cat}' not found in available categories")
    
    # PRIORITY 4: Single-level categories (least preferred) - Log as WARNING since this is too broad
    if any(term in ai_lower for term in ['art', 'entertainment', 'visual']):
        if 'Arts & Entertainment' in available_categories:
            logger.warning(f"⚠️ BROAD FALLBACK: Using 'Arts & Entertainment' for '{ai_category}' - this is too general!")
            return available_categories['Arts & Entertainment']
    
    if any(term in ai_lower for term in ['home', 'garden', 'decor']):
        if 'Home & Garden' in available_categories:
            logger.warning(f"⚠️ BROAD FALLBACK: Using 'Home & Garden' for '{ai_category}' - this is too general!")
            return available_categories['Home & Garden']
    
    # Log all categories that contain relevant terms for debugging
    logger.warning(f"🔍 DEBUGGING: Searching for categories containing poster/print/art terms...")
    relevant_found = []
    for cat_path, node_id in available_categories.items():
        if any(term in cat_path.lower() for term in ['poster', 'print', 'art', 'visual']):
            relevant_found.append(cat_path)
    
    logger.warning(f"📋 Found {len(relevant_found)} relevant categories: {relevant_found[:10]}")
    
    # FINAL FALLBACK: Use the most specific poster/art category if available
    poster_fallbacks = [
        'Arts & Entertainment > Visual Arts > Posters',
        'Arts & Entertainment > Visual Arts',
        'Arts & Entertainment'
    ]
    
    for fallback in poster_fallbacks:
        if fallback in available_categories:
            logger.warning(f"🔧 ULTIMATE FALLBACK: Using '{fallback}' for unmatched category '{ai_category}'")
            return available_categories[fallback]
    
    print(f"❌ NO CATEGORY MATCH FOUND for: '{ai_category}'")
    logger.error(f"❌ COMPLETE FAILURE: No categories found in taxonomy for '{ai_category}'")
    return None

def assign_product_category(product_gid: str, category_gid: str,
                            shop_domain=None, access_token=None) -> bool:
    """
    Assign a category to an existing product using GraphQL mutation
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
        
        _sd, _at = _resolve_taxonomy_credentials(shop_domain, access_token)
        if not _sd or not _at:
            logger.error("No Shopify credentials available for category assignment")
            return False

        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": _at
        }
        
        response = requests.post(
            f"https://{_sd}/admin/api/2025-07/graphql.json",
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
    # Test the functionality
    logging.basicConfig(level=logging.INFO)
    categories = get_product_taxonomy_categories()
    print(f"Retrieved {len(categories)} categories")
    if categories:
        print("Sample categories:")
        for i, (name, gid) in enumerate(list(categories.items())[:5]):
            print(f"  {name} -> {gid}")
    
    # Test category matching
    test_category = "Art & Entertainment > Visual Arts > Posters"
    match = find_best_category_match(test_category, categories)
    print(f"\nBest match for '{test_category}': {match}")