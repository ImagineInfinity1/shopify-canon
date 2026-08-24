import os
import re
import requests
import json
import logging
import base64
from urllib.parse import urljoin
from shopify_graphql import format_description_html

logger = logging.getLogger(__name__)


def _resolve_utils_credentials(shop_domain=None, access_token=None):
    """Resolve Shopify credentials for utils module."""
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

# Fixed variants for all products
PRODUCT_VARIANTS = [
    {"option1": "20x30 cm", "price": "6.99", "inventory_quantity": 999},
    {"option1": "30x40 cm", "price": "11.99", "inventory_quantity": 999},
    {"option1": "40x50 cm", "price": "12.99", "inventory_quantity": 999},
    {"option1": "50x70 cm", "price": "13.99", "inventory_quantity": 999},
    {"option1": "A1 - 59.4 x 84.1 cm", "price": "14.99", "inventory_quantity": 999},
    {"option1": "A2 - 42 x 59.4 cm", "price": "13.49", "inventory_quantity": 999},
    {"option1": "A3 - 29.7 x 42 cm", "price": "11.99", "inventory_quantity": 999},
    {"option1": "A4 - 21 x 29.7 cm", "price": "6.99", "inventory_quantity": 999}
]

def get_category_taxonomy_id(category_name):
    """Map AI-generated category to official Shopify taxonomy ID"""
    
    # Strip "Manual: " prefix so manual category from form can be resolved
    if category_name and isinstance(category_name, str):
        category_name = category_name.strip()
        if category_name.lower().startswith('manual:'):
            category_name = category_name[7:].strip()
    
    # Official Shopify taxonomy mappings discovered through testing (hg-3-4 = Home & Garden > Decor > Artwork)
    category_mappings = {
        # Full paths and poster/print variants
        'home & garden > decor > artwork > posters, prints, & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters, prints, & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters in poster, prints & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'poster, prints & visual artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        # Primary art/poster categories
        'artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'poster': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'posters': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'print': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'prints': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'visual art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'visual arts': 'gid://shopify/TaxonomyCategory/hg-3-4',
        
        # Fallback categories
        'arts & crafts': 'gid://shopify/TaxonomyCategory/ae-2-1',  # Arts & Entertainment > Hobbies & Creative Arts > Arts & Crafts
        'creative arts': 'gid://shopify/TaxonomyCategory/ae-2-1',
        'collectibles': 'gid://shopify/TaxonomyCategory/ae-2-2',  # Arts & Entertainment > Hobbies & Creative Arts > Collectibles
        
        # Home decor alternatives
        'decorative': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'home decor': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'wall art': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'wall decor': 'gid://shopify/TaxonomyCategory/hg-3-4',
        
        # Handle full taxonomy paths from manual input
        'home & garden > decor > artwork': 'gid://shopify/TaxonomyCategory/hg-3-4',
        'arts & entertainment > hobbies & creative arts > arts & crafts': 'gid://shopify/TaxonomyCategory/ae-2-1',
        'arts & entertainment > hobbies & creative arts > collectibles': 'gid://shopify/TaxonomyCategory/ae-2-2',
    }
    
    if not category_name:
        # Default to artwork category
        return 'gid://shopify/TaxonomyCategory/hg-3-4'
    
    # Try exact match first
    category_lower = category_name.lower().strip()
    if category_lower in category_mappings:
        logger.info(f"Found exact category mapping: '{category_name}' -> {category_mappings[category_lower]}")
        return category_mappings[category_lower]
    
    # Try partial matches
    for key, taxonomy_id in category_mappings.items():
        if key in category_lower or any(word in category_lower for word in key.split()):
            logger.info(f"Found partial category mapping: '{category_name}' -> {taxonomy_id}")
            return taxonomy_id
    
    # Default fallback to artwork
    logger.info(f"No category mapping found for '{category_name}', defaulting to Artwork category")
    return 'gid://shopify/TaxonomyCategory/hg-3-4'

def get_shopify_headers(access_token=None):
    """Get headers for Shopify API requests"""
    if not access_token:
        _, access_token = _resolve_utils_credentials()
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token or ""
    }

def create_shopify_product_with_images(frame_paths, metadata, filename, task_data=None,
                                       shop_domain=None, access_token=None):
    """Create a Shopify product with images using GraphQL"""
    logger.warning(f"🔍 SHOPIFY: create_shopify_product_with_images called with category: {metadata.get('category', 'NO CATEGORY')}")
    store_url, token = _resolve_utils_credentials(shop_domain, access_token)
    if not store_url or not token:
        logger.error("Missing Shopify credentials")
        return None
    
    store_url = store_url.replace('https://', '').replace('http://', '')
    graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
    
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': token
    }
    
    # GraphQL productCreate mutation - NOW WITH REAL CATEGORY FIELD
    mutation = '''
    mutation productCreate($input: ProductInput!) {
      productCreate(input: $input) {
        product {
          id
          title
          handle
          status
          productType
          category {
            id
            fullName
          }
          seo {
            description
            title
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    '''
    
    # Use manual or AI-generated SEO-friendly URL handle
    import time
    unique_id = int(time.time() * 1000)
    
    # Check for manual handle first, then AI-generated
    if task_data and task_data.get('handle_manual', False) and task_data.get('manual_handle'):
        handle = task_data['manual_handle']
        logger.info(f"Using manual URL handle: {handle}")
    elif metadata.get('seo_url_handle'):
        handle = metadata['seo_url_handle']
        logger.info(f"Using AI-generated SEO URL handle: {handle}")
    else:
        handle = f"poster-{unique_id}"
        logger.info(f"Using fallback URL handle: {handle}")
    
    # Extract user configuration from task_data - Use defaults temporarily until frontend passes data
    vendor = 'Samila Home'  # Temporary - should come from frontend Product Organization
    
    # Extract the most granular category term from AI category for productType
    # Shopify productType field expects simple category names, not full paths
    ai_category = metadata.get('category', '')
    if 'Poster' in ai_category:
        product_type = 'Poster'
    elif 'Print' in ai_category:
        product_type = 'Print'
    elif 'Art' in ai_category:
        product_type = 'Art'
    elif 'Botanical' in ai_category or 'Plant' in ai_category:
        product_type = 'Botanical Art'
    elif 'Classical' in ai_category or 'Ancient' in ai_category:
        product_type = 'Classical Art'
    else:
        product_type = 'Wall Art'  # Generic fallback
    
    logger.info(f"🏷️ Set product_type to: '{product_type}' (extracted from AI category: '{ai_category}')")
    
    if task_data:
        # Vendor: prioritize explicit vendor field, then business_name as fallback
        if task_data.get('vendor') and task_data['vendor'].strip():
            vendor = task_data['vendor'].strip()
            logger.info(f"Using vendor from form: {vendor}")
        elif task_data.get('business_name') and task_data['business_name'].strip():
            vendor = task_data['business_name'].strip()
            logger.info(f"Using business_name as vendor: {vendor}")
        # IMPORTANT: Only override product_type if explicitly provided and not empty
        if task_data.get('product_type') and task_data.get('product_type').strip():
            product_type = task_data['product_type']
            logger.info(f"🔧 Using manual product_type from form: {product_type}")
        else:
            logger.info(f"🔧 Keeping AI-derived product_type: {product_type}")
            
    logger.info(f"Using vendor: {vendor}, product_type: {product_type}")
    
    # Generate SKU from manual or AI title with timestamp
    if task_data and task_data.get('sku_manual', False) and task_data.get('manual_sku'):
        sku = task_data['manual_sku'] + f'-{unique_id}'
        logger.info(f"Using manual SKU pattern: {sku}")
    else:
        sku_base = metadata.get('title', f'POSTER-{unique_id}')
        sku = ''.join(c.upper() if c.isalnum() else '-' for c in sku_base)[:20] + f'-{unique_id}'
        logger.info(f"Using auto-generated SKU: {sku}")
    
    # Get product status from manual settings
    product_status = 'ACTIVE'  # Default
    if task_data and task_data.get('product_status'):
        product_status = task_data['product_status']
        logger.info(f"Using manual product status: {product_status}")
    
    # Get inventory settings
    inventory_quantity = 999  # Default
    inventory_policy = 'continue'  # Default
    if task_data:
        if task_data.get('inventory_quantity'):
            try:
                inventory_quantity = int(task_data['inventory_quantity'])
            except (ValueError, TypeError):
                inventory_quantity = 999
        if task_data.get('inventory_policy'):
            inventory_policy = task_data['inventory_policy']
    logger.info(f"Using inventory settings: quantity={inventory_quantity}, policy={inventory_policy}")
    
    # Map AI-determined category to Shopify product taxonomy node
    category_mapping = {
        'Art & Entertainment > Visual Arts > Posters': 'gid://shopify/ProductTaxonomyNode/sg-2-17-2-17',
        'Home & Garden > Home Decor': 'gid://shopify/ProductTaxonomyNode/sg-4-17-1-1',
        'Art & Entertainment > Crafts > Art & Craft Supplies': 'gid://shopify/ProductTaxonomyNode/sg-2-1-1-1',
        'Fashion > Accessories': 'gid://shopify/ProductTaxonomyNode/sg-3-7-1-1',
        'Books & Media > Books': 'gid://shopify/ProductTaxonomyNode/sg-6-4-1-1',
        'Electronics > Electronics Accessories': 'gid://shopify/ProductTaxonomyNode/sg-1-11-1-1',
        'Toys & Games': 'gid://shopify/ProductTaxonomyNode/sg-8-1-1-1',
        'Beauty & Personal Care': 'gid://shopify/ProductTaxonomyNode/sg-7-1-1-1'
    }
    
    # Category will be determined below based on toggle states
    
    # Publishing will be handled after product creation based on user selections
    from datetime import datetime
    
    # Use manual values if toggles are enabled, otherwise use AI-generated metadata
    if task_data:
        # Check toggle states and use manual values or AI values accordingly
        if task_data.get('title_manual', False) and task_data.get('manual_title'):
            title = task_data['manual_title']
            logger.info(f"Using manual title: {title}")
        else:
            title = metadata.get('title', f'Poster - {filename}')
            logger.info(f"Using AI-generated title: {title}")
            
        if task_data.get('description_manual', False) and task_data.get('manual_description'):
            description = task_data['manual_description']
            logger.info(f"Using manual description: {description}")
        else:
            description = metadata.get('description', 'Beautiful poster artwork')
            logger.info(f"Using AI-generated description: {description}")
            
        # PROVEN WORKING: Use verified Shopify taxonomy categories
        if task_data and task_data.get('category_manual', False) and task_data.get('manual_category'):
            manual_cat = task_data['manual_category'].strip()
            logger.info(f"🎯 Manual category: '{manual_cat}'")
            
            # Get the official taxonomy ID for the REAL category field
            category_taxonomy_id = get_category_taxonomy_id(manual_cat)
            
            # Keep productType for backward compatibility (appears as "Product type" in admin)
            if '>' in manual_cat:
                product_type = manual_cat
            else:
                product_type = 'Poster'  # Simple fallback
                    
            ai_category = f"Manual: {manual_cat}"
            logger.info(f"🎯 MANUAL category = '{category_taxonomy_id}' | productType = '{product_type}'")
            
        else:
            # AI category - get official taxonomy ID for the REAL category field
            ai_category = metadata.get('category', 'Posters')
            logger.info(f"AI category: {ai_category}")
            
            # Get the official taxonomy ID
            category_taxonomy_id = get_category_taxonomy_id(ai_category)
            
            # Keep productType simple for backward compatibility
            product_type = 'Poster'  # Simple product type
                
            logger.info(f"🎯 AI category = '{category_taxonomy_id}' | productType = '{product_type}'")
        
        # Ensure category_taxonomy_id is always defined
        if 'category_taxonomy_id' not in locals():
            category_taxonomy_id = get_category_taxonomy_id(metadata.get('category', 'artwork'))
            
        if task_data.get('tags_manual', False) and task_data.get('manual_tags'):
            # Parse tags from comma-separated string
            tags = [tag.strip() for tag in task_data['manual_tags'].split(',') if tag.strip()]
            logger.info(f"Using manual tags: {tags}")
        else:
            tags = metadata.get('tags', [])
            logger.info(f"Using AI-generated tags: {tags}")
    else:
        # Fallback to AI-generated values if no task data
        title = metadata.get('title', f'Poster - {filename}')
        description = metadata.get('description', 'Beautiful poster artwork')
        ai_category = metadata.get('category', 'Art & Entertainment > Visual Arts > Posters')
        tags = metadata.get('tags', [])
    

    
    # Handle manual metafields if provided
    metafields_data = {}
    if task_data:
        # Check for manual metafield values
        if task_data.get('color_manual', False) and task_data.get('manual_color'):
            metafields_data['color'] = task_data['manual_color']
            logger.info(f"Using manual color metafield: {metafields_data['color']}")
        elif metadata.get('metafields', {}).get('color'):
            metafields_data['color'] = metadata['metafields']['color']
            logger.info(f"Using AI-generated color metafield: {metafields_data['color']}")
            
        if task_data.get('frame_style_manual', False) and task_data.get('manual_frame_style'):
            metafields_data['frame_style'] = task_data['manual_frame_style']
            logger.info(f"Using manual frame style metafield: {metafields_data['frame_style']}")
        elif metadata.get('metafields', {}).get('frame_style'):
            metafields_data['frame_style'] = metadata['metafields']['frame_style']
            logger.info(f"Using AI-generated frame style metafield: {metafields_data['frame_style']}")
            
        if task_data.get('theme_manual', False) and task_data.get('manual_theme'):
            metafields_data['theme'] = task_data['manual_theme']
            logger.info(f"Using manual theme metafield: {metafields_data['theme']}")
        elif metadata.get('metafields', {}).get('theme'):
            metafields_data['theme'] = metadata['metafields']['theme']
            logger.info(f"Using AI-generated theme metafield: {metafields_data['theme']}")
    
    # Create product input with all required fields
    product_input = {
        'title': title,
        'descriptionHtml': format_description_html(description),
        'productType': product_type,  # This maps to "Product type" in Shopify admin
        'category': category_taxonomy_id,  # This maps to the REAL "Category" field in Shopify admin
        'vendor': vendor,
        'status': product_status,
        'handle': handle,
        'tags': tags,
        # Note: category field is NOT supported in productCreate - only productType works
    }
    
    logger.warning(f"🎯 SENDING TO SHOPIFY: productType='{product_type}' (this becomes Category in admin)")
    
    # Add SEO fields - check manual overrides first, then fall back to AI-generated
    seo_title = None
    seo_description = None
    
    if task_data:
        # Manual SEO title
        if task_data.get('seo_title_manual', False) and task_data.get('manual_seo_title'):
            seo_title = task_data['manual_seo_title'].strip()
            logger.info(f"Using manual SEO title: {seo_title}")
        
        # Manual meta description
        if task_data.get('meta_desc_manual', False) and task_data.get('manual_meta_desc'):
            seo_description = task_data['manual_meta_desc'].strip()
            logger.info(f"Using manual meta description: {seo_description}")
    
    # Fall back to AI-generated values
    if not seo_title:
        seo_title = metadata.get('seo_title', title)  # Fall back to product title
    if not seo_description:
        meta_description = metadata.get('meta_description')
        if meta_description and len(meta_description) <= 160:
            seo_description = meta_description
    
    if seo_title or seo_description:
        product_input['seo'] = {}
        if seo_title:
            product_input['seo']['title'] = seo_title
        if seo_description:
            product_input['seo']['description'] = seo_description
        logger.info(f"SEO settings - title: '{seo_title}', description: '{seo_description}'")
    else:
        logger.info("No SEO title or meta description provided")
    
    # Collections will be handled separately - do not add to tags
    
    variables = {
        'input': product_input
    }
    
    graphql_request = {
        'query': mutation,
        'variables': variables
    }
    
    try:
        logger.info(f"Creating product via GraphQL: {variables['input']['title']}")
        logger.warning(f"🔍 FULL GraphQL REQUEST: {json.dumps(graphql_request, indent=2)}")
        response = requests.post(graphql_url, headers=headers, json=graphql_request, timeout=30)
        logger.info(f"GraphQL response status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"GraphQL product creation response: {result}")
            
            if 'data' in result and 'productCreate' in result['data']:
                product_create = result['data']['productCreate']
                
                if product_create and product_create.get('userErrors'):
                    logger.error("GraphQL validation errors:")
                    for error in product_create['userErrors']:
                        logger.error(f"  - {error['field']}: {error['message']}")
                    return None
                
                elif product_create and product_create.get('product'):
                    product = product_create['product']
                    logger.info(f"✅ GraphQL product created: {product['id']} with category: {ai_category}")
                    
                    # Extract numeric ID from GraphQL GID
                    numeric_id = product['id'].split('/')[-1]
                    
                    # Verify the product was created with correct productType
                    logger.warning(f"✅ SUCCESS: Product created with productType '{product['productType']}'")
                    logger.warning(f"🎯 This will appear as Category '{product['productType']}' in Shopify admin")
                    
                    # Add metafields for SEO enhancement - use already-built metafields_data (manual + AI)
                    # Do NOT overwrite with metadata.get('metafields') - that discards task manual overrides
                    logger.info(f"🔍 FULL METADATA DEBUG: {metadata}")
                    # Merge: start with built metafields_data (color, frame_style, theme from task/metadata), add any extra from metadata
                    extra_mf = metadata.get('metafields') or {}
                    for k, v in extra_mf.items():
                        if k not in metafields_data and v and str(v).strip():
                            metafields_data[k] = str(v).strip()
                    metafields_result = []  # Initialize empty list
                    logger.info(f"🔍 METAFIELDS DATA: {metafields_data}")
                    logger.info(f"🔍 METAFIELDS DATA TYPE: {type(metafields_data)}")
                    logger.info(f"🔍 METAFIELDS DATA EMPTY?: {not metafields_data}")
                    
                    if metafields_data:
                        logger.info("🏷️  Adding SEO metafields to product...")
                        metafields_result = add_metafields_to_product(numeric_id, metafields_data, store_url, headers)
                        logger.info(f"Created {len(metafields_result)} metafields")
                    else:
                        logger.warning("⚠️  No metafields data found in metadata - FORCING DEFAULT CREATION")
                        # Force creation of default universal metafields (Theme, Frame Style, Color)
                        default_metafields = {
                            'material': 'Mixed Materials',
                            'style': 'Modern',
                            'color': 'Multi-color',
                            'theme': 'General',
                            'frame_style': 'Unframed'
                        }
                        logger.info("🏷️  Creating default metafields for testing...")
                        metafields_result = add_metafields_to_product(numeric_id, default_metafields, store_url, headers)
                        logger.info(f"Created {len(metafields_result)} default metafields")
                    
                    # Now add images using REST API with SEO enhancements
                    images_added = []
                    alt_text = metadata.get('alt_text', '')
                    seo_filename = metadata.get('seo_filename', '')
                    
                    for i, image_path in enumerate(frame_paths):
                        if os.path.exists(image_path):
                            # Create unique SEO filename for each image
                            current_seo_filename = f"{seo_filename}-{i+1}.jpg" if seo_filename else None
                            
                            image_result = add_image_to_product(
                                numeric_id, 
                                image_path, 
                                store_url, 
                                headers,
                                alt_text=alt_text,
                                seo_filename=current_seo_filename
                            )
                            if image_result:
                                images_added.append(image_result)
                                logger.info(f"✅ Added SEO-optimized image: {current_seo_filename or os.path.basename(image_path)}")
                            else:
                                logger.error(f"❌ Failed to add image: {image_path}")
                    
                    # Smart category assignment using taxonomy API with timeout protection
                    try:
                        from shopify_taxonomy import implement_smart_category_assignment
                        import signal
                        logger.info(f"🏷️  Implementing smart category assignment...")
                        
                        # Add timeout protection for category assignment
                        import signal
                        def timeout_handler(signum, frame):
                            raise TimeoutError("Category assignment timed out")
                        
                        signal.signal(signal.SIGALRM, timeout_handler)
                        signal.alarm(15)  # 15 second timeout
                        
                        category_success = implement_smart_category_assignment(product['id'], ai_category)
                        signal.alarm(0)  # Cancel timeout
                        
                        if category_success:
                            logger.info(f"✅ Category successfully assigned: {ai_category}")
                        else:
                            logger.warning(f"⚠️  Category assignment failed, product created without category")
                    except (TimeoutError, Exception) as e:
                        try:
                            signal.alarm(0)  # Cancel timeout
                        except:
                            pass
                        logger.warning(f"⚠️  Category assignment failed ({e}), continuing without category")
                    
                    # Add options and variants using REST API in one step
                    logger.info("📐📦 Adding Size options and variants with REST API")
                    # Get variants data from task if available
                    variants_data = task_data.get('variants_data', []) if task_data else []
                    options_and_variants_result = add_options_and_variants_rest(product['id'], store_url, headers, sku, variants_data, inventory_quantity, inventory_policy)
                    logger.info(f"Options and variants result: {options_and_variants_result}")
                    
                    # Publish product to selected sales channels
                    logger.info("📢 Publishing to sales channels")  
                    publish_result = publish_product_to_selected_channels(product['id'], store_url, headers, task_data)
                    logger.info(f"Publishing result: {publish_result}")
                    
                    # Add product to collections - use manual collections if toggle enabled, otherwise AI-selected
                    if task_data and task_data.get('collections_manual', False) and task_data.get('manual_collections'):
                        # Parse manual collections from comma-separated string
                        manual_collections_list = [coll.strip() for coll in task_data['manual_collections'].split(',') if coll.strip()]
                        logger.info(f"Using manual collections: {manual_collections_list}")
                        collections_result = add_product_to_collections(product['id'], manual_collections_list, store_url, headers)
                    else:
                        # Use AI-selected collections from metadata
                        ai_collections = metadata.get('collections', [])
                        logger.info(f"Using AI-selected collections: {ai_collections}")
                        if ai_collections:
                            collections_result = add_product_to_collections(product['id'], ai_collections, store_url, headers)
                        else:
                            logger.info("No collections provided - skipping collection assignment")
                    
                    return {
                        'id': numeric_id,
                        'title': product['title'],
                        'handle': product['handle'],
                        'admin_url': f"https://{store_url}/admin/products/{numeric_id}",
                        'public_url': f"https://{store_url}/products/{product['handle']}",
                        'variants_created': options_and_variants_result,
                        'images_added': len(images_added),
                        'metafields_created': len(metafields_result) if metafields_data else 0,
                        'seo_enhanced': True if (alt_text or seo_filename or metafields_data) else False
                    }
            
            elif 'errors' in result:
                logger.error("GraphQL errors:")
                for error in result['errors']:
                    logger.error(f"  - {error.get('message', error)}")
                return None
            else:
                logger.error(f"Unexpected response structure: {result}")
                return None
            
            logger.error(f"Unexpected GraphQL response: {result}")
            return None
        elif response.status_code == 404:
            logger.error("❌ GraphQL Admin API not accessible with current Custom App")
            logger.error("🔧 Solution: Create a NEW Custom App to get GraphQL access")
            logger.error("📝 Steps: Settings → Apps → Develop apps → Create app → Configure Admin API access")
            return None
        else:
            logger.error(f"GraphQL request failed: {response.status_code}")
            logger.error(f"Response: {response.text[:300]}")
            return None
            
    except Exception as e:
        logger.error(f"GraphQL product creation failed: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def add_image_to_product(product_id, image_path, store_url, headers, alt_text=None, seo_filename=None):
    """Add an image to an existing product using REST API"""
    try:
        # Convert to base64 for attachment method
        import base64
        with open(image_path, 'rb') as f:
            image_data = f.read()
        
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        filename = seo_filename if seo_filename else os.path.basename(image_path)
        
        # REST API endpoint for product images
        image_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}/images.json"
        
        # Create image using base64 attachment with SEO-friendly alt text
        image_data = {
            'image': {
                'attachment': image_base64,
                'filename': filename,
                'alt': alt_text if alt_text else f'Poster - {filename}'
            }
        }
        
        response = requests.post(image_url, headers=headers, json=image_data, timeout=60)
        
        if response.status_code in [200, 201]:
            result = response.json()
            image_info = result['image']
            logger.info(f"✅ Image added to product: {image_info['id']}")
            return {
                'id': image_info['id'],
                'src': image_info['src'],
                'alt': image_info['alt']
            }
        else:
            logger.error(f"REST image upload failed: {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            return None
            
    except Exception as e:
        logger.error(f"Image upload exception: {str(e)}")
        return None

def add_metafields_to_product(product_id, metafields_data, store_url, headers):
    """Add metafields to a product using GraphQL API for proper visibility"""
    try:
        if not metafields_data or not isinstance(metafields_data, dict):
            logger.warning("No valid metafields data provided")
            return []
        
        # Extract product ID from GID if needed
        if 'gid://' in str(product_id):
            product_gid = f"gid://shopify/Product/{product_id}"
            product_id = product_id.split('/')[-1]
        else:
            product_gid = f"gid://shopify/Product/{product_id}"
        
        # Use GraphQL for metafields creation to ensure proper visibility
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        
        # Create metafields for all keys provided by AI (flexible system)
        metafields_to_create = []
        for key, value in metafields_data.items():
            if value and isinstance(value, str) and value.strip():
                metafields_to_create.append({'key': key, 'value': value.strip()})
        
        created_metafields = []
        
        for metafield in metafields_to_create:
            if metafield['value']:  # Only create if value exists
                # GraphQL mutation for creating metafields
                mutation = f"""
                mutation {{
                    metafieldsSet(metafields: [{{
                        ownerId: "{product_gid}"
                        namespace: "custom"
                        key: "{metafield['key']}"
                        value: "{metafield['value']}"
                        type: "single_line_text_field"
                    }}]) {{
                        metafields {{
                            id
                            namespace
                            key
                            value
                        }}
                        userErrors {{
                            field
                            message
                        }}
                    }}
                }}
                """
                
                response = requests.post(graphql_url, headers=headers, json={'query': mutation}, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    if 'data' in result and 'metafieldsSet' in result['data']:
                        metafields_set = result['data']['metafieldsSet']
                        if metafields_set.get('userErrors'):
                            for error in metafields_set['userErrors']:
                                logger.error(f"GraphQL metafield error: {error['field']}: {error['message']}")
                        elif metafields_set.get('metafields'):
                            created_metafields.extend(metafields_set['metafields'])
                            logger.info(f"✅ GraphQL Metafield created: {metafield['key']} = {metafield['value']}")
                        else:
                            logger.warning(f"No metafields returned for {metafield['key']}")
                    else:
                        logger.error(f"GraphQL response missing data: {result}")
                else:
                    logger.error(f"GraphQL metafield failed: {response.status_code}")
                    logger.error(f"Response: {response.text[:200]}")
        
        logger.info(f"Created {len(created_metafields)}/{len(metafields_to_create)} metafields via GraphQL")
        return created_metafields
        
    except Exception as e:
        logger.error(f"Metafields creation exception: {str(e)}")
        return []

def add_options_and_variants_rest(product_gid, store_url, headers, sku_prefix, variants_data=None, inventory_quantity=999, inventory_policy='continue'):
    """Add Size options and variants using REST API - with user-defined variants support"""
    try:
        # Extract product ID from GID
        product_id = product_gid.split('/')[-1]
        
        # Initialize variables
        option_values = []
        api_variants = []
        
        # Use user-defined variants if provided, otherwise fall back to defaults
        if variants_data and len(variants_data) > 0:
            logger.info(f"Using user-defined variants: {len(variants_data)} variants")
            
            for variant in variants_data:
                # Accept multiple formats: size/title/name for option, inventory/inventory_quantity for qty
                raw_size = variant.get('size') or variant.get('title') or variant.get('name') or ''
                size = str(raw_size).strip() if raw_size is not None else ''
                price = variant.get('price', '0')
                inventory = variant.get('inventory', variant.get('inventory_quantity', 999))
                
                if not size:
                    continue
                    
                option_values.append(size)
                
                # Create safe SKU suffix from size
                sku_suffix = ''.join(c.upper() if c.isalnum() else '-' for c in size)[:10]
                
                api_variants.append({
                    "option1": size,
                    "price": str(price),
                    "sku": f"{sku_prefix}-{sku_suffix}",
                    "inventory_management": "shopify",
                    "inventory_policy": inventory_policy,
                    "inventory_quantity": int(inventory) if inventory else inventory_quantity
                })
            
            if not api_variants:
                logger.warning("No valid variants in user data, using defaults")
                variants_data = None  # Fall back to defaults
        
        # Use default variants if no user data provided or user data was invalid  
        if not variants_data:
            logger.warning("No variants provided from Product Variants section - using basic defaults temporarily")
            option_values = ["30x40 cm", "40x50 cm", "50x70 cm"]
            api_variants = [
                {"option1": "30x40 cm", "price": "11.99", "sku": f"{sku_prefix}-30x40", "inventory_management": "shopify", "inventory_policy": inventory_policy, "inventory_quantity": inventory_quantity},
                {"option1": "40x50 cm", "price": "12.99", "sku": f"{sku_prefix}-40x50", "inventory_management": "shopify", "inventory_policy": inventory_policy, "inventory_quantity": inventory_quantity},
                {"option1": "50x70 cm", "price": "13.99", "sku": f"{sku_prefix}-50x70", "inventory_management": "shopify", "inventory_policy": inventory_policy, "inventory_quantity": inventory_quantity}
            ]
        
        # Use REST API to update product with options and variants in one call
        rest_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}.json"
        
        product_update = {
            "product": {
                "id": int(product_id),
                "options": [
                    {
                        "name": "Size",
                        "position": 1,
                        "values": option_values
                    }
                ],
                "variants": api_variants
            }
        }
        
        response = requests.put(rest_url, headers=headers, json=product_update, timeout=30)
        
        if response.status_code == 200:
            response_data = response.json()
            if 'product' in response_data:
                logger.info("✅ Added Size options and variants successfully using REST API")
                product = response_data['product']
                variant_count = len(product.get('variants', []))
                logger.info(f"Created {variant_count} variants with proper SKUs")
                return True
            else:
                logger.error(f"No product in response: {response_data}")
                return False
        else:
            logger.error(f"Failed to add options and variants: {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            return False
            
    except Exception as e:
        logger.error(f"Exception adding options and variants: {e}")
        return False


def add_product_options(product_gid, store_url, headers):
    """Add Size option to product using REST API since GraphQL doesn't support options field in ProductInput"""
    try:
        # Extract product ID from GID
        product_id = product_gid.split('/')[-1]
        
        # First get the product to see current options
        rest_url = f"https://{store_url}/admin/api/2025-07/products/{product_id}.json"
        response = requests.get(rest_url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Failed to get product: {response.status_code}")
            return False
            
        product_data = response.json()
        product = product_data['product']
        
        # Check if Size option already exists
        for option in product.get('options', []):
            if option['name'] == 'Size':
                logger.info("Size option already exists")
                return True
        
        # Add Size option using REST API with proper structure
        existing_options = product.get('options', [])
        
        # Create new option with proper structure for REST API
        size_option = {
            "name": "Size"
        }
        
        new_options = existing_options + [size_option]
        
        # For updating product options, we need to include option values as variants
        product_update = {
            "product": {
                "id": int(product_id),
                "options": new_options,
                "variants": [
                    {
                        "option1": "30x40 cm",
                        "price": "11.99",
                        "inventory_management": "shopify",
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "40x50 cm", 
                        "price": "12.99",
                        "inventory_management": "shopify",
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "50x70 cm",
                        "price": "13.99", 
                        "inventory_management": "shopify",
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "A1 - 59.4 x 84.1 cm",
                        "price": "14.99",
                        "inventory_management": "shopify", 
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "A2 - 42 x 59.4 cm",
                        "price": "13.49",
                        "inventory_management": "shopify",
                        "inventory_policy": "continue", 
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "A3 - 29.7 x 42 cm",
                        "price": "11.99",
                        "inventory_management": "shopify",
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    },
                    {
                        "option1": "A4 - 21 x 29.7 cm",
                        "price": "6.99",
                        "inventory_management": "shopify",
                        "inventory_policy": "continue",
                        "inventory_quantity": 999
                    }
                ]
            }
        }
        
        response = requests.put(rest_url, headers=headers, json=product_update, timeout=30)
        
        if response.status_code == 200:
            response_data = response.json()
            if 'product' in response_data:
                logger.info("✅ Added Size option and variants to product successfully")
                
                # Now add SKUs to the variants that were just created
                product = response_data['product']
                if 'variants' in product:
                    for i, variant in enumerate(product['variants']):
                        # Map option values to SKUs
                        option_value = variant.get('option1', '')
                        sku_suffix = ''
                        if '30x40' in option_value:
                            sku_suffix = '30x40'
                        elif '40x50' in option_value:
                            sku_suffix = '40x50'
                        elif '50x70' in option_value:
                            sku_suffix = '50x70'
                        elif 'A1' in option_value:
                            sku_suffix = 'A1'
                        elif 'A2' in option_value:
                            sku_suffix = 'A2'
                        elif 'A3' in option_value:
                            sku_suffix = 'A3'
                        elif 'A4' in option_value:
                            sku_suffix = 'A4'
                        
                        if sku_suffix:
                            # Update variant with SKU
                            variant_id = variant['id']
                            variant_update_url = f"https://{store_url}/admin/api/2025-07/variants/{variant_id}.json"
                            variant_update = {
                                "variant": {
                                    "id": variant_id,
                                    "sku": f"POSTER-{sku_suffix}-{int(product_id)}"
                                }
                            }
                            
                            sku_response = requests.put(variant_update_url, headers=headers, json=variant_update, timeout=30)
                            if sku_response.status_code == 200:
                                logger.info(f"✅ Added SKU to variant {option_value}")
                return True
            else:
                logger.error(f"No product in response: {response_data}")
                return False
        else:
            logger.error(f"Failed to add options: {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            return False
            
    except Exception as e:
        logger.error(f"Exception adding product options: {e}")
        return False
    
    try:
        # Dead code below (unreachable after return in earlier try block)
        pass
    except Exception as e:
        logger.error(f"Product options update exception: {str(e)}")
        return False

def add_product_category(product_gid, taxonomy_node_id, store_url, headers):
    """Add product category using GraphQL productUpdate"""
    mutation = '''
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          category {
            id
            name
          }
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
            'id': product_gid,
            'category': taxonomy_node_id
        }
    }
    
    try:
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        response = requests.post(graphql_url, headers=headers, json={
            'query': mutation,
            'variables': variables
        }, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Product category update response: {result}")
            
            if 'data' in result and 'productUpdate' in result['data']:
                product_update = result['data']['productUpdate']
                if product_update.get('userErrors'):
                    logger.warning("Product category update errors (non-critical):")
                    for error in product_update['userErrors']:
                        logger.warning(f"  - {error['field']}: {error['message']}")
                    return False
                else:
                    logger.info("✅ Product category added successfully")
                    return True
        
        logger.warning(f"Product category update failed: HTTP {response.status_code}")
        return False
        
    except Exception as e:
        logger.warning(f"Product category update exception (non-critical): {str(e)}")
        return False

def create_product_variants(product_gid, store_url, headers, sku_prefix="POSTER"):
    """Add variants to a product using GraphQL productVariantsBulkCreate"""
    mutation = '''
    mutation productVariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkCreate(productId: $productId, variants: $variants) {
        productVariants {
          id
          price
          inventoryQuantity
          sku
        }
        userErrors {
          field
          message
        }
      }
    }
    '''
    
    import time
    timestamp = int(time.time())
    
    # Define size variants without SKU (SKU will be added separately)
    variants_data = [
        {"price": "11.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "30x40 cm"}]},
        {"price": "12.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "40x50 cm"}]},
        {"price": "13.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "50x70 cm"}]},
        {"price": "14.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "A1 - 59.4 x 84.1 cm"}]},
        {"price": "13.49", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "A2 - 42 x 59.4 cm"}]},
        {"price": "11.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "A3 - 29.7 x 42 cm"}]},
        {"price": "6.99", "inventoryPolicy": "CONTINUE", "optionValues": [{"optionName": "Size", "name": "A4 - 21 x 29.7 cm"}]}
    ]
    
    # SKU mapping for later assignment
    sku_mapping = {
        "30x40 cm": f"{sku_prefix}-30x40",
        "40x50 cm": f"{sku_prefix}-40x50", 
        "50x70 cm": f"{sku_prefix}-50x70",
        "A1 - 59.4 x 84.1 cm": f"{sku_prefix}-A1",
        "A2 - 42 x 59.4 cm": f"{sku_prefix}-A2",
        "A3 - 29.7 x 42 cm": f"{sku_prefix}-A3",
        "A4 - 21 x 29.7 cm": f"{sku_prefix}-A4"
    }
    
    variables = {
        'productId': product_gid,
        'variants': variants_data
    }
    
    graphql_request = {
        'query': mutation,
        'variables': variables
    }
    
    try:
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        response = requests.post(graphql_url, headers=headers, json=graphql_request, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Variant creation response: {result}")
            
            if 'data' in result and 'productVariantsBulkCreate' in result['data']:
                bulk_create = result['data']['productVariantsBulkCreate']
                
                if bulk_create.get('userErrors') and len(bulk_create['userErrors']) > 0:
                    logger.error("Variant creation errors:")
                    for error in bulk_create['userErrors']:
                        logger.error(f"  - {error['field']}: {error['message']}")
                    return False
                
                elif bulk_create.get('productVariants'):
                    variants_count = len(bulk_create['productVariants'])
                    logger.info(f"✅ Created {variants_count} variants")
                    
                    # Now add SKUs to the created variants using REST API
                    add_skus_to_variants(product_gid, bulk_create['productVariants'], sku_mapping, store_url, headers)
                    
                    return True
                else:
                    logger.warning("No variants returned in response")
                    return False
        
        logger.error(f"Variant creation failed: HTTP {response.status_code}")
        logger.error(f"Response: {response.text[:500]}")
        return False
        
    except Exception as e:
        logger.error(f"Variant creation exception: {str(e)}")
        return False


def add_skus_to_variants(product_gid, variants, sku_mapping, store_url, headers):
    """Add SKUs to created variants using REST API since GraphQL doesn't support SKU in bulk creation"""
    try:
        # Extract product ID from GID
        product_id = product_gid.split('/')[-1]
        
        for variant in variants:
            variant_id = variant['id'].split('/')[-1]
            
            # Find the corresponding SKU for this variant based on option values
            size_option = None
            for option in variant.get('selectedOptions', []):
                if option['name'] == 'Size':
                    size_option = option['value']
                    break
            
            if size_option and size_option in sku_mapping:
                sku = sku_mapping[size_option]
                
                # Update variant with SKU using REST API
                rest_url = f"https://{store_url}/admin/api/2025-07/variants/{variant_id}.json"
                variant_data = {
                    "variant": {
                        "id": int(variant_id),
                        "sku": sku
                    }
                }
                
                response = requests.put(rest_url, headers=headers, json=variant_data, timeout=30)
                
                if response.status_code == 200:
                    logger.info(f"✅ Added SKU {sku} to variant {size_option}")
                else:
                    logger.warning(f"⚠️  Failed to add SKU to variant {variant_id}: {response.status_code}")
                    
    except Exception as e:
        logger.error(f"Error adding SKUs to variants: {e}")


def update_product_with_options(product_gid, store_url, headers):
    """Add size option to product before creating variants"""
    mutation = '''
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          options {
            id
            name
            values
          }
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
            'id': product_gid,
            'options': ['Size']
        }
    }
    
    graphql_request = {
        'query': mutation,
        'variables': variables
    }
    
    try:
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        response = requests.post(graphql_url, headers=headers, json=graphql_request, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Product options update response: {result}")
            
            if 'data' in result and 'productUpdate' in result['data']:
                product_update = result['data']['productUpdate']
                
                if product_update.get('userErrors') and len(product_update['userErrors']) > 0:
                    logger.error("Product options update errors:")
                    for error in product_update['userErrors']:
                        logger.error(f"  - {error['field']}: {error['message']}")
                    return False
                else:
                    logger.info("✅ Product options updated successfully")
                    return True
        
        logger.error(f"Product options update failed: HTTP {response.status_code}")
        return False
        
    except Exception as e:
        logger.error(f"Product options update exception: {str(e)}")
        return False

def add_product_to_collections(product_gid, selected_collections, store_url, headers):
    """Add product to collections using GraphQL"""
    if not selected_collections:
        logger.info("No collections specified")
        return True
        
    logger.info(f"Adding product to collections: {selected_collections}")
    
    # First, get existing collections to find their IDs
    collections_query = '''
    query getCollections($first: Int!, $after: String) {
      collections(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            title
          }
        }
      }
    }
    '''
    
    try:
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        
        # Get existing collections (all pages - a 50-collection cap used to make
        # every assignment past the first page silently fail)
        existing_collections = {}
        cursor = None
        response = None
        for _page in range(20):
            response = requests.post(graphql_url, headers=headers, json={
                'query': collections_query,
                'variables': {'first': 250, 'after': cursor}
            }, timeout=30)
            if response.status_code != 200:
                break
            result = response.json()
            connection = (result.get('data') or {}).get('collections') or {}
            for edge in connection.get('edges') or []:
                collection = edge.get('node') or {}
                if collection.get('title'):
                    existing_collections[collection['title']] = collection['id']
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')

        if response is not None and response.status_code == 200:
            # Case- and punctuation-insensitive fallback match
            normalised = {}
            for _title, _gid in existing_collections.items():
                normalised.setdefault(re.sub(r'[^a-z0-9]+', '', _title.lower()), (_title, _gid))

            # Add product to matching collections
            for collection_name in selected_collections:
                match = None
                if collection_name in existing_collections:
                    match = (collection_name, existing_collections[collection_name])
                else:
                    match = normalised.get(re.sub(r'[^a-z0-9]+', '', str(collection_name or '').lower()))
                if match:
                    collection_id = match[1]
                    
                    # Add product to collection
                    collection_update_mutation = '''
                    mutation collectionAddProducts($id: ID!, $productIds: [ID!]!) {
                      collectionAddProducts(id: $id, productIds: $productIds) {
                        collection {
                          id
                          title
                        }
                        userErrors {
                          field
                          message
                        }
                      }
                    }
                    '''
                    
                    variables = {
                        'id': collection_id,
                        'productIds': [product_gid]
                    }
                    
                    collection_response = requests.post(graphql_url, headers=headers, json={
                        'query': collection_update_mutation,
                        'variables': variables
                    }, timeout=30)
                    
                    if collection_response.status_code == 200:
                        collection_result = collection_response.json()
                        logger.info(f"✅ Added product to collection: {collection_name}")
                    else:
                        logger.error(f"❌ Failed to add to collection {collection_name}: HTTP {collection_response.status_code}")
                else:
                    logger.warning(f"⚠️  Collection '{collection_name}' not found in store")
            
            return True
        else:
            logger.error(f"Failed to get collections: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Collections update exception: {str(e)}")
        return False

def publish_product_to_selected_channels(product_gid, store_url, headers, task_data=None):
    """Publish product to user-selected sales channels"""
    try:
        # Get user's publishing preferences from task data (passed from upload)
        if task_data and task_data.get('selected_sales_channels'):
            selected_channels = task_data['selected_sales_channels']
            logger.info(f"Using selected sales channels from task data: {len(selected_channels)} channels")
        else:
            # Fallback to Online Store only if no selections
            logger.warning("No sales channels found in task data, using default Online Store channel")
            selected_channels = ['gid://shopify/Publication/177371447619']  # Online Store
        
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        
        # Publish to each selected sales channel using productPublish mutation
        successful_publishes = 0
        for channel_id in selected_channels:
            # Use correct productPublish mutation format based on actual GraphQL schema
            publication_mutation = '''
            mutation productPublish($id: ID!, $productPublications: [ProductPublicationInput!]!) {
              productPublish(input: {id: $id, productPublications: $productPublications}) {
                product {
                  id
                  title
                }
                userErrors {
                  field
                  message
                }
              }
            }
            '''
            
            variables = {
                'id': product_gid,
                'productPublications': [
                    {
                        'publicationId': channel_id
                    }
                ]
            }
            
            response = requests.post(graphql_url, headers=headers, json={
                'query': publication_mutation,
                'variables': variables
            }, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Publishing response for {channel_id}: {result}")
                
                if 'data' in result and result['data']['productPublish']:
                    if not result['data']['productPublish']['userErrors']:
                        successful_publishes += 1
                        logger.info(f"✅ Published product to channel: {channel_id}")
                    else:
                        logger.warning(f"⚠️  Publishing to {channel_id} had errors: {result['data']['productPublish']['userErrors']}")
                else:
                    logger.warning(f"⚠️  Publishing to {channel_id} failed - data: {result}")
            else:
                logger.warning(f"⚠️  HTTP error publishing to {channel_id}: {response.status_code}")
                logger.warning(f"Response: {response.text[:500]}")
        
        logger.info(f"✅ Published product to {successful_publishes}/{len(selected_channels)} sales channels")
        return successful_publishes > 0
        
    except Exception as e:
        logger.error(f"Publishing exception: {str(e)}")
        return False

def set_product_category_graphql(product_gid, taxonomy_node_id, store_url, headers):
    """Set product category using GraphQL API"""
    try:
        # Use store_url from function parameter (multi-tenant)
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        
        # GraphQL mutation to set product category
        category_mutation = '''
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product {
              id
              title
              category {
                id
                name
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        '''
        
        # Try just the node ID without the gid prefix
        node_id = taxonomy_node_id.replace('gid://shopify/ProductTaxonomyNode/', '')
        variables = {
            'input': {
                'id': product_gid,
                'category': node_id
            }
        }
        
        logger.info(f"Setting category with GraphQL: product_gid={product_gid}, taxonomy_node_id={taxonomy_node_id}")
        response = requests.post(graphql_url, headers=headers, json={
            'query': category_mutation,
            'variables': variables
        }, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Category GraphQL response: {result}")
            
            if 'data' in result and result['data']['productUpdate']:
                if not result['data']['productUpdate']['userErrors']:
                    category_info = result['data']['productUpdate']['product'].get('category')
                    logger.info(f"✅ Product category set successfully via GraphQL: {category_info}")
                    return True
                else:
                    logger.error(f"❌ GraphQL category errors: {result['data']['productUpdate']['userErrors']}")
                    return False
            else:
                logger.error(f"❌ Unexpected GraphQL response: {result}")
                return False
        else:
            logger.error(f"❌ Failed to set product category via GraphQL: HTTP {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"Exception setting product category via GraphQL: {str(e)}")
        return False

def get_shopify_collections(shop_domain=None, access_token=None):
    """Get all collections from Shopify store for AI to choose from"""
    collections_query = '''
    query getCollections($first: Int!, $after: String) {
      collections(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            title
          }
        }
      }
    }
    '''
    
    try:
        store_url, token = _resolve_utils_credentials(shop_domain, access_token)
        if not store_url or not token:
            logger.error("Shopify credentials not available for collections")
            return []
        
        store_url = store_url.replace('https://', '').replace('http://', '')
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token
        }
        
        graphql_url = f"https://{store_url}/admin/api/2025-07/graphql.json"
        
        collections = []
        cursor = None
        for _page in range(20):
            response = requests.post(graphql_url, headers=headers, json={
                'query': collections_query,
                'variables': {'first': 250, 'after': cursor}
            }, timeout=30)
            if response.status_code != 200:
                logger.error(f"Failed to get collections: HTTP {response.status_code}")
                return collections
            result = response.json()
            connection = (result.get('data') or {}).get('collections') or {}
            for edge in connection.get('edges') or []:
                collection = edge.get('node') or {}
                if collection.get('title'):
                    collections.append(collection['title'])
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')

        logger.info(f"Found {len(collections)} collections in store: {collections}")
        return collections
            
    except Exception as e:
        logger.error(f"Exception getting collections: {str(e)}")
        return []

def create_shopify_product(frame_paths, metadata, filename, task_data=None,
                           shop_domain=None, access_token=None):
    """Create a Shopify product with multiple variants and images"""
    logger.warning(f"🔍 SHOPIFY: create_shopify_product called with category: {metadata.get('category', 'NO CATEGORY')}")
    return create_shopify_product_with_images(frame_paths, metadata, filename, task_data,
                                              shop_domain=shop_domain, access_token=access_token)

def validate_shopify_credentials(shop_domain=None, access_token=None):
    """Validate Shopify API credentials"""
    store_url, token = _resolve_utils_credentials(shop_domain, access_token)
    if not store_url or not token:
        return False, "Missing Shopify credentials"
    
    try:
        store_url = store_url.replace('https://', '').replace('http://', '')
        url = f"https://{store_url}/admin/api/2025-07/shop.json"
        response = requests.get(url, headers=get_shopify_headers(access_token=token), timeout=10)
        
        if response.status_code == 200:
            return True, "Credentials valid"
        else:
            return False, f"Invalid credentials: {response.status_code}"
            
    except Exception as e:
        return False, f"Connection error: {str(e)}"