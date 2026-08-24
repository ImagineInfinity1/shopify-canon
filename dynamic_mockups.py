#!/usr/bin/env python3
"""
Dynamic Mockups API Integration for PSD Smart Object Replacement
Replaces the Photopea-based system with proper smart object automation
"""

import os
import json
import requests
import logging
import time
import shutil
from urllib.parse import urljoin
from PIL import Image

logger = logging.getLogger(__name__)

class DynamicMockupsAPI:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('DYNAMIC_MOCKUPS_API_KEY')
        self.base_url = 'https://app.dynamicmockups.com/api/v1'
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-api-key': self.api_key
        }
        
        if not self.api_key:
            raise ValueError("Dynamic Mockups API key not found. Set DYNAMIC_MOCKUPS_API_KEY environment variable.")

    def upload_psd_template(self, psd_url, template_name, category_id=1):
        """
        Upload PSD template to Dynamic Mockups and create mockup template
        
        Args:
            psd_url (str): Public URL to the PSD file
            template_name (str): Name for the template
            category_id (int): Category ID (1 = general, 3 = apparel, etc.)
        
        Returns:
            dict: Upload result with mockup UUID and smart objects
        """
        
        endpoint = f"{self.base_url}/psd/upload"
        
        payload = {
            "psd_file_url": psd_url,
            "psd_name": template_name,
            "psd_category_id": category_id,
            "mockup_template": {
                "create_after_upload": True
            }
        }
        
        # Try multiple times with different timeouts and strategies
        max_retries = 3
        timeouts = [180, 300, 600]  # 3 minutes, 5 minutes, 10 minutes
        
        for attempt in range(max_retries):
            try:
                timeout = timeouts[attempt]
                logger.info(f"Uploading PSD template: {template_name} (attempt {attempt + 1}/{max_retries}, timeout: {timeout}s)")
                
                response = requests.post(endpoint, headers=self.headers, json=payload, timeout=timeout)
                response.raise_for_status()
                
                result = response.json()
                
                if result.get('success'):
                    logger.info(f"PSD upload successful. Mockup UUID: {result['data']['uuid']}")
                    return {
                        'success': True,
                        'mockup_uuid': result['data']['uuid'],
                        'template_name': result['data']['name'],
                        'smart_objects': result['data']['smart_objects'],
                        'thumbnail': result['data'].get('thumbnail'),
                        'data': result['data']
                    }
                else:
                    error_msg = result.get('message', 'Unknown error')
                    logger.error(f"PSD upload failed: {error_msg}")
                    
                    # Don't retry on certain errors
                    if 'file size' in error_msg.lower() or 'too large' in error_msg.lower():
                        return {
                            'success': False,
                            'error': f"File too large for API: {error_msg}"
                        }
                    
                    if attempt == max_retries - 1:
                        return {
                            'success': False,
                            'error': error_msg
                        }
                    
                    logger.info(f"Retrying upload in 10 seconds...")
                    time.sleep(10)
                    continue
                    
            except requests.exceptions.Timeout as e:
                logger.warning(f"Upload timeout on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    return {
                        'success': False,
                        'error': f"Upload timed out after {max_retries} attempts. File may be too large."
                    }
                logger.info(f"Retrying with longer timeout...")
                time.sleep(15)
                continue
                
            except requests.exceptions.RequestException as e:
                logger.error(f"API request failed on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    return {
                        'success': False,
                        'error': f"API request failed after {max_retries} attempts: {str(e)}"
                    }
                logger.info(f"Retrying request in 10 seconds...")
                time.sleep(10)
                continue
        
        return {
            'success': False,
            'error': 'All upload attempts failed'
        }

    def render_mockup(self, mockup_uuid, image_url, smart_object_uuid=None, options=None):
        """
        Render mockup by inserting image into smart object
        
        Args:
            mockup_uuid (str): UUID of the uploaded mockup template
            image_url (str): Public URL to the image to insert
            smart_object_uuid (str): Specific smart object UUID (uses first if None)
            options (dict): Additional rendering options
        
        Returns:
            dict: Render result with mockup URL
        """
        
        endpoint = f"{self.base_url}/renders"
        
        # Default rendering options
        if options is None:
            options = {
                "fit": "contain",  # Options: stretch, contain, cover
                "format": "png",   # Output format
                "quality": 95      # JPEG quality (0-100)
            }
        
        smart_objects = [{
            "uuid": smart_object_uuid,
            "asset": {
                "url": image_url
            }
        }]
        
        # Add rendering options to smart object
        if options:
            smart_objects[0].update(options)
        
        payload = {
            "mockup_uuid": mockup_uuid,
            "smart_objects": smart_objects
        }
        
        try:
            logger.info(f"Rendering mockup {mockup_uuid} with image {image_url}")
            # Increase timeout for mockup rendering (can take several minutes for complex PSDs)
            response = requests.post(endpoint, headers=self.headers, json=payload, timeout=300)  # 5 minutes
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Render API response: {result}")
            
            if result.get('success'):
                # Handle different response formats
                data = result.get('data', {})
                render_url = (data.get('url') or data.get('render_url') or data.get('export_path') or 
                             result.get('url') or result.get('render_url') or result.get('export_path'))
                
                if not render_url:
                    logger.error(f"No render URL found in response: {result}")
                    return {
                        'success': False,
                        'error': 'No render URL in API response'
                    }
                
                logger.info(f"Mockup rendered successfully: {render_url}")
                return {
                    'success': True,
                    'render_url': render_url,
                    'render_id': data.get('id') or result.get('id'),
                    'data': data
                }
            else:
                logger.error(f"Mockup rendering failed: {result.get('message', 'Unknown error')}")
                return {
                    'success': False,
                    'error': result.get('message', 'Rendering failed')
                }
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Render API request failed: {e}")
            return {
                'success': False,
                'error': f"Render request failed: {str(e)}"
            }

    def get_mockup_info(self, mockup_uuid):
        """
        Get information about an uploaded mockup template
        
        Args:
            mockup_uuid (str): UUID of the mockup template
        
        Returns:
            dict: Mockup information including smart objects
        """
        
        endpoint = f"{self.base_url}/mockups/{mockup_uuid}"
        
        try:
            response = requests.get(endpoint, headers=self.headers)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get('success'):
                return {
                    'success': True,
                    'data': result['data']
                }
            else:
                return {
                    'success': False,
                    'error': result.get('message', 'Failed to get mockup info')
                }
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Get mockup info failed: {e}")
            return {
                'success': False,
                'error': f"Request failed: {str(e)}"
            }

    def create_public_url_for_file(self, file_path, base_url="https://4e080412-7f9c-4a97-9900-626455f1eca2-00-1njhjl3ienimw.kirk.replit.dev"):
        """
        Helper to create public URLs for local files
        This assumes your Replit app serves files from a public endpoint
        
        Args:
            file_path (str): Local file path
            base_url (str): Base URL of your Replit app
        
        Returns:
            str: Public URL to the file
        """
        
        # Remove leading paths and create web-accessible URL
        filename = os.path.basename(file_path)
        return f"{base_url}/serve_file/{filename}"


def get_available_dynamic_mockups():
    """Get list of uploaded PSD templates for Dynamic Mockups processing"""
    import os
    import logging
    
    logger = logging.getLogger(__name__)
    templates = []
    uploads_dir = "uploads"
    
    try:
        if os.path.exists(uploads_dir):
            for filename in os.listdir(uploads_dir):
                if filename.lower().endswith(('.psd', '.psb')):
                    filepath = os.path.join(uploads_dir, filename)
                    try:
                        file_stats = os.stat(filepath)
                        size_mb = round(file_stats.st_size / (1024 * 1024), 2)
                        templates.append({
                            'name': filename.replace('.psd', '').replace('.psb', '').replace('_', ' '),
                            'filename': filename,
                            'path': filepath,
                            'type': 'Uploaded PSD',
                            'size': f"{size_mb}MB"
                        })
                    except OSError:
                        continue
        
        return templates
    except Exception as e:
        logger.error(f"Error getting available Dynamic Mockups templates: {e}")
        return []


def check_file_size_for_api(file_path, max_size_mb=3):
    """
    Check if file size is suitable for Dynamic Mockups API
    
    Based on official documentation:
    - Recommended PSD size: 1500x1500px at 72dpi  
    - "Around 1000px resolution for best performance"
    - "The smaller your PSD resolution is, the faster your mockup will load online"
    
    Args:
        file_path (str): Path to the file
        max_size_mb (int): Maximum size in MB (recommended: <3MB for reliability)
    
    Returns:
        dict: Size check result
    """
    try:
        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)
        
        if size_mb > max_size_mb:
            logger.warning(f"File {file_path} is {size_mb:.2f}MB, which is too large for API (recommended: <{max_size_mb}MB)")
            return {
                'suitable': False,
                'size_mb': size_mb,
                'message': f"File is {size_mb:.2f}MB. API docs recommend 1500x1500px at 72dpi (~1-3MB) for best performance. Large files cause 504 timeouts."
            }
        else:
            logger.info(f"File {file_path} is {size_mb:.2f}MB - suitable for API")
            return {
                'suitable': True,
                'size_mb': size_mb,
                'message': f"File size ({size_mb:.2f}MB) meets API recommendations."
            }
    except Exception as e:
        logger.error(f"Error checking file size: {e}")
        return {
            'suitable': False,
            'size_mb': 0,
            'message': f"Could not check file size: {str(e)}"
        }


# Cache to store successfully uploaded templates to avoid re-uploading
_template_cache = {}

def process_image_with_dynamic_mockups(image_path, psd_template_path, template_name):
    """
    Main function to process image using Dynamic Mockups API
    
    Args:
        image_path (str): Path to the image file
        psd_template_path (str): Path to the PSD template file
        template_name (str): Name for the template
    
    Returns:
        dict: Processing result with mockup URL
    """
    
    try:
        # Optimize files according to Dynamic Mockups specifications  
        optimized_psd = optimize_psd_for_upload(psd_template_path, max_size_mb=10)
        optimized_image = optimize_image_for_upload(image_path, max_size_mb=5)
        
        # Check file sizes after optimization
        psd_check = check_file_size_for_api(optimized_psd, max_size_mb=10)
        image_check = check_file_size_for_api(optimized_image, max_size_mb=5)
        
        logger.info(f"PSD size check: {psd_check['message']}")
        logger.info(f"Image size check: {image_check['message']}")
        
        if not psd_check['suitable']:
            logger.warning(f"PSD file is large but proceeding: {psd_check['message']}")
        
        if not image_check['suitable']:
            logger.warning(f"Image file is large but proceeding: {image_check['message']}")
        
        api = DynamicMockupsAPI()
        
        base_url = "https://4e080412-7f9c-4a97-9900-626455f1eca2-00-1njhjl3ienimw.kirk.replit.dev"
        psd_url = f"{base_url}/serve_file/{os.path.basename(optimized_psd)}"
        image_url = f"{base_url}/serve_file/{os.path.basename(optimized_image)}"
        
        logger.info(f"Processing with Dynamic Mockups: {template_name}")
        logger.info(f"PSD URL: {psd_url}")
        logger.info(f"Image URL: {image_url}")
        
        # Check if template is already uploaded (use cache)
        cache_key = f"{template_name}_{os.path.basename(optimized_psd)}"
        if cache_key in _template_cache:
            logger.info(f"Using cached template: {cache_key}")
            upload_result = _template_cache[cache_key]
        else:
            # DIRECT API CALL - exactly like the working test
            logger.info("Making direct API call (skipping our upload method)")
            endpoint = f"{api.base_url}/psd/upload"
            payload = {
                "psd_file_url": psd_url,
                "psd_name": template_name,
                "psd_category_id": 3,  # Wall Art
                "mockup_template": {
                    "create_after_upload": True
                }
            }
            
            # Implement retry logic for 504 Gateway Timeouts
            max_retries = 3
            base_delay = 5  # Start with 5 seconds
            result = None  # Initialize result variable
            
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"API attempt {attempt}/{max_retries}")
                    
                    response = requests.post(endpoint, headers=api.headers, json=payload, timeout=240)
                    
                    # Check for 504 specifically and retry
                    if response.status_code == 504:
                        if attempt < max_retries:
                            wait_time = base_delay * (2 ** (attempt - 1))  # Exponential backoff: 5s, 10s, 20s
                            logger.warning(f"504 Gateway Timeout on attempt {attempt}. Retrying in {wait_time} seconds...")
                            time.sleep(wait_time)
                            continue
                        else:
                            raise requests.exceptions.HTTPError(f"504 Gateway Timeout after {max_retries} attempts - Dynamic Mockups servers are overloaded")
                    
                    response.raise_for_status()
                    result = response.json()
                    logger.info(f"API call successful on attempt {attempt}")
                    break  # Success, exit retry loop
                    
                except requests.exceptions.Timeout:
                    if attempt < max_retries:
                        wait_time = base_delay * (2 ** (attempt - 1))
                        logger.warning(f"Request timeout on attempt {attempt}. Retrying in {wait_time} seconds...")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise requests.exceptions.Timeout(f"Request timeout after {max_retries} attempts")
                        
                except requests.exceptions.RequestException as e:
                    if attempt < max_retries and "504" in str(e):
                        wait_time = base_delay * (2 ** (attempt - 1))
                        logger.warning(f"Request failed with 504 on attempt {attempt}. Retrying in {wait_time} seconds...")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise
            
            # Check if we got a valid result
            if result is None:
                return {
                    'success': False,
                    'error': f"No response received after {max_retries} attempts"
                }
            
            if result.get('success'):
                upload_result = {
                    'success': True,
                    'mockup_uuid': result['data']['uuid'],
                    'template_name': result['data']['name'],
                    'smart_objects': result['data']['smart_objects'],
                    'thumbnail': result['data'].get('thumbnail'),
                    'data': result['data']
                }
                # Cache successful upload
                _template_cache[cache_key] = upload_result
                logger.info(f"Template uploaded and cached: {result['data']['uuid']}")
            else:
                return {
                    'success': False,
                    'error': f"PSD upload failed: {result.get('message', 'Unknown error')}"
                }
        
        if not upload_result['success']:
            return upload_result
        
        # Step 2: Get the first smart object UUID
        smart_objects = upload_result['smart_objects']
        if not smart_objects:
            return {
                'success': False,
                'error': "No smart objects found in PSD template"
            }
        
        first_smart_object = smart_objects[0]['uuid']
        logger.info(f"Using smart object: {first_smart_object}")
        
        # Step 3: Render the mockup
        render_result = api.render_mockup(
            mockup_uuid=upload_result['mockup_uuid'],
            image_url=image_url,
            smart_object_uuid=first_smart_object,
            options={
                "fit": "contain",
                "format": "png",
                "quality": 95
            }
        )
        
        if not render_result['success']:
            return {
                'success': False,
                'error': f"Mockup rendering failed: {render_result['error']}"
            }
        
        return {
            'success': True,
            'mockup_url': render_result['render_url'],
            'mockup_uuid': upload_result['mockup_uuid'],
            'smart_objects': smart_objects,
            'template_info': upload_result
        }
        
    except Exception as e:
        logger.error(f"Dynamic Mockups processing failed: {e}")
        return {
            'success': False,
            'error': f"Processing failed: {str(e)}"
        }


def optimize_psd_for_upload(psd_path, max_size_mb=10):
    """
    Optimize PSD file for Dynamic Mockups upload following their official specs:
    - 1500x1500px at 72dpi for optimal performance
    - No actual file size limit, but larger files cause timeouts
    
    Args:
        psd_path (str): Path to the original PSD file
        max_size_mb (float): Warning threshold for large files
    
    Returns:
        str: Path to original PSD file (Dynamic Mockups has no size limit)
    """
    
    # Check file size for performance warnings
    file_size_mb = os.path.getsize(psd_path) / (1024 * 1024)
    
    if file_size_mb <= max_size_mb:
        logger.info(f"PSD file size OK: {file_size_mb:.1f}MB - within performance range")
    else:
        logger.warning(f"PSD file is large ({file_size_mb:.1f}MB) - may cause API timeouts. Dynamic Mockups recommends 1500x1500px at 72dpi for optimal performance.")
    
    # Dynamic Mockups API has no file size limit - return original
    # Performance optimization should be done in Photoshop before upload
    return psd_path


def optimize_image_for_upload(image_path, max_size_mb=1.0):
    """
    Optimize image file for Dynamic Mockups upload by reducing file size
    
    Args:
        image_path (str): Path to the original image file
        max_size_mb (float): Maximum file size in MB (default: 1.0MB)
    
    Returns:
        str: Path to optimized image file or original if already small enough
    """
    
    # Check if file already meets size requirements
    file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
    
    if file_size_mb <= max_size_mb:
        logger.info(f"Image file already optimized: {file_size_mb:.1f}MB")
        return image_path
    
    logger.info(f"Optimizing image file from {file_size_mb:.1f}MB to under {max_size_mb}MB")
    
    # Create optimized version
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    dir_name = os.path.dirname(image_path)
    optimized_path = os.path.join(dir_name, f"{base_name}_optimized.jpg")
    
    try:
        with Image.open(image_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            
            # Start with lower quality and adjust if needed
            quality = 85
            while quality > 30:
                # Save with current quality
                img.save(optimized_path, 'JPEG', quality=quality, optimize=True)
                
                # Check file size
                current_size_mb = os.path.getsize(optimized_path) / (1024 * 1024)
                
                if current_size_mb <= max_size_mb:
                    logger.info(f"Image optimized to {current_size_mb:.1f}MB at quality {quality}")
                    return optimized_path
                
                quality -= 10
            
            # If still too large, resize the image
            logger.info("Quality reduction not sufficient, resizing image")
            width, height = img.size
            
            # Reduce dimensions by 20% at a time
            while quality <= 30:
                width = int(width * 0.8)
                height = int(height * 0.8)
                
                resized_img = img.resize((width, height), Image.Resampling.LANCZOS)
                resized_img.save(optimized_path, 'JPEG', quality=75, optimize=True)
                
                current_size_mb = os.path.getsize(optimized_path) / (1024 * 1024)
                
                if current_size_mb <= max_size_mb:
                    logger.info(f"Image optimized to {current_size_mb:.1f}MB by resizing to {width}x{height}")
                    return optimized_path
                
                if width < 800 or height < 600:  # Don't make too small
                    break
            
            logger.warning(f"Could not optimize image below {max_size_mb}MB, using original")
            return image_path
            
    except Exception as e:
        logger.error(f"Image optimization failed: {e}")
        return image_path


