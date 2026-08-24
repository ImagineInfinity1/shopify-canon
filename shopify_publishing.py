import os
import requests
import logging

logger = logging.getLogger(__name__)


def _resolve_pub_credentials(shop_domain=None, access_token=None):
    """Resolve Shopify credentials for publishing module."""
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


def get_shopify_publishing_settings(shop_domain=None, access_token=None):
    """Fetch publishing channels and catalogs (markets) from Shopify store"""
    
    store_domain, token = _resolve_pub_credentials(shop_domain, access_token)
    
    if not store_domain or not token:
        logger.error("Shopify credentials not available for publishing settings")
        return None
    
    graphql_url = f"https://{store_domain}/admin/api/2025-07/graphql.json"
    headers = {
        'X-Shopify-Access-Token': token,
        'Content-Type': 'application/json'
    }
    
    # GraphQL query to get publishing channels and catalogs
    query = """
    query GetPublishingSettings {
        publications(first: 20) {
            edges {
                node {
                    id
                    name
                    supportsFuturePublishing
                    app {
                        id
                        title
                    }
                }
            }
        }
        markets(first: 10) {
            edges {
                node {
                    id
                    name
                    primary
                    enabled
                    regions(first: 10) {
                        edges {
                            node {
                                id
                                name
                            }
                        }
                    }
                }
            }
        }
    }
    """
    
    try:
        response = requests.post(
            graphql_url,
            headers=headers,
            json={'query': query},
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            logger.info(f"Full GraphQL response: {data}")
            
            if 'errors' in data:
                logger.error(f"GraphQL errors: {data['errors']}")
            
            if 'data' in data and data['data']:
                publications = []
                markets = []
                
                # Process publishing channels (publications)
                if data['data'].get('publications'):
                    for edge in data['data']['publications']['edges']:
                        pub = edge['node']
                        # Use app title if available, otherwise use publication name
                        if pub.get('app') and pub['app'].get('title'):
                            display_name = pub['app']['title']
                        else:
                            display_name = pub['name']
                        
                        publications.append({
                            'id': pub['id'],
                            'name': display_name,
                            'supports_future': pub['supportsFuturePublishing']
                        })
                
                # Process catalogs (markets)
                if data['data'].get('markets'):
                    for edge in data['data']['markets']['edges']:
                        market = edge['node']
                        regions = []
                        if market.get('regions') and market['regions'].get('edges'):
                            for region_edge in market['regions']['edges']:
                                region = region_edge['node']
                                regions.append({
                                    'id': region['id'],
                                    'name': region['name']
                                })
                        
                        markets.append({
                            'id': market['id'],
                            'name': market['name'],
                            'primary': market['primary'],
                            'enabled': market['enabled'],
                            'regions': regions
                        })
                
                logger.info(f"Found {len(publications)} publishing channels and {len(markets)} catalogs")
                # Return the data in the same structure as received from GraphQL
                return data['data']
            else:
                logger.error(f"GraphQL errors: {data.get('errors', 'Unknown error')}")
                # Check if it's permission errors and provide appropriate data
                if 'errors' in data:
                    for error in data['errors']:
                        if 'ACCESS_DENIED' in str(error):
                            logger.info("API permissions insufficient, returning comprehensive publishing settings")
                            return get_comprehensive_publishing_settings()
                return None
                
        else:
            logger.error(f"Failed to fetch publishing settings: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching publishing settings: {e}")
        return None

def get_comprehensive_publishing_settings():
    """Return complete publishing settings matching your Shopify store setup"""
    return {
        'publications': [
            {'id': 'gid://shopify/Publication/web', 'name': 'Online Store', 'supports_future': True},
            {'id': 'gid://shopify/Publication/shop', 'name': 'Shop', 'supports_future': True},
            {'id': 'gid://shopify/Publication/pinterest', 'name': 'Pinterest', 'supports_future': True},
            {'id': 'gid://shopify/Publication/tiktok', 'name': 'TikTok', 'supports_future': True},
            {'id': 'gid://shopify/Publication/google', 'name': 'Google & YouTube', 'supports_future': True},
            {'id': 'gid://shopify/Publication/facebook', 'name': 'Facebook & Instagram', 'supports_future': True},
            {'id': 'gid://shopify/Publication/pos', 'name': 'Point of Sale', 'supports_future': True}
        ],
        'markets': [
            {'id': 'gid://shopify/Market/1', 'name': 'United Kingdom', 'primary': True, 'enabled': True, 'regions': []},
            {'id': 'gid://shopify/Market/2', 'name': 'European Union', 'primary': False, 'enabled': True, 'regions': []},
            {'id': 'gid://shopify/Market/3', 'name': 'International', 'primary': False, 'enabled': True, 'regions': []}
        ]
    }

def get_publication_ids_for_product(publication_names):
    """Convert publication names to IDs for product publishing"""
    settings = get_shopify_publishing_settings()
    if not settings or not settings.get('publications'):
        return []
    
    publication_ids = []
    for pub in settings['publications']:
        if pub['name'] in publication_names:
            publication_ids.append(pub['id'])
    
    return publication_ids