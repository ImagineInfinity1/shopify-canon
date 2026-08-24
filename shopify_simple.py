import os
import requests
import json
import base64
import logging

logger = logging.getLogger(__name__)

def _resolve_simple_credentials(shop_domain=None, access_token=None):
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


def create_simple_shopify_product(image_path, metadata, filename, shop_domain=None, access_token=None):
    """Simplified Shopify product creation - minimal viable version"""
    
    store_url, token = _resolve_simple_credentials(shop_domain, access_token)
    
    if not store_url or not token:
        logger.error("Missing Shopify credentials")
        return None
    
    try:
        store_url = store_url.replace('https://', '').replace('http://', '')
        headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': token
        }
        
        # Create basic product first
        product_data = {
            "product": {
                "title": metadata.get('title', f'Poster - {filename}'),
                "body_html": metadata.get('description', 'Beautiful poster artwork'),
                "vendor": "Listing Cannon",
                "product_type": "Poster",
                "tags": ", ".join(metadata.get('tags', [])),
                "status": "active"
            }
        }
        
        url = f"https://{store_url}/admin/api/2025-07/products.json"
        logger.info(f"Creating Shopify product: {metadata.get('title', filename)}")
        
        response = requests.post(url, headers=headers, json=product_data, timeout=30)
        
        if response.status_code == 201:
            result = response.json()
            product = result['product']
            logger.info(f"Successfully created Shopify product ID: {product['id']}")
            
            return {
                'id': product['id'],
                'title': product['title'],
                'handle': product['handle'],
                'admin_url': f"https://{store_url}/admin/products/{product['id']}",
                'public_url': f"https://{store_url}/products/{product['handle']}"
            }
        else:
            logger.error(f"Shopify product creation failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Exception in Shopify product creation: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# Test the simple version
if __name__ == "__main__":
    test_metadata = {
        'title': 'Simple Test Poster',
        'description': 'Test description',
        'tags': ['test', 'poster']
    }
    
    result = create_simple_shopify_product(None, test_metadata, 'test.jpg')  
    print(f"Simple test result: {result}")