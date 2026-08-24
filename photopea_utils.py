#!/usr/bin/env python3
"""
Photopea API Integration for PSD Mockup Generation
Uses Photopea's web API to frame images into PSD templates
"""

import os
import json
import base64
import requests
import logging
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

def create_photopea_config(psd_path, image_path, output_formats=None):
    """Create Photopea configuration JSON for PSD mockup generation"""
    
    if output_formats is None:
        output_formats = ["png", "jpg:0.8"]
    
    # Read the PSD file and encode as base64
    with open(psd_path, 'rb') as psd_file:
        psd_data = base64.b64encode(psd_file.read()).decode('utf-8')
        psd_data_uri = f"data:application/octet-stream;base64,{psd_data}"
    
    # Read the image file and encode as base64
    with open(image_path, 'rb') as img_file:
        img_data = base64.b64encode(img_file.read()).decode('utf-8')
        
        # Determine MIME type based on file extension
        ext = os.path.splitext(image_path)[1].lower()
        if ext in ['.jpg', '.jpeg']:
            mime_type = 'image/jpeg'
        elif ext == '.png':
            mime_type = 'image/png'
        else:
            mime_type = 'image/jpeg'  # Default
            
        img_data_uri = f"data:{mime_type};base64,{img_data}"
    
    # Create configuration object
    config = {
        "files": [psd_data_uri, img_data_uri],
        "server": {
            "version": 1,
            "url": f"http://localhost:5000/photopea_callback",  # Our Flask callback
            "formats": output_formats
        },
        "script": """
            // Automated PSD mockup processing script
            var psdDoc = app.documents[0];
            var imageDoc = app.documents[1];
            
            // Make PSD the active document
            app.activeDocument = psdDoc;
            
            // Copy the image from second document
            app.activeDocument = imageDoc;
            app.activeDocument.selection.selectAll();
            app.activeDocument.selection.copy();
            
            // Switch back to PSD and paste
            app.activeDocument = psdDoc;
            
            // Paste the image
            var pastedLayer = app.activeDocument.paste();
            
            // Auto-resize and position the image
            var docBounds = [0, 0, app.activeDocument.width.value, app.activeDocument.height.value];
            pastedLayer.resize(100, 100, AnchorPosition.MIDDLECENTER);
            
            // Automatically save and send result
            app.activeDocument.saveAs(new File('mockup_result.png'), new PNGSaveOptions(), true);
            
            // Resize the pasted image to fit the canvas
            var docWidth = app.activeDocument.width.value;
            var docHeight = app.activeDocument.height.value;
            
            // Transform to fit (this is a basic resize - can be enhanced)
            pastedLayer.resize(100, 100);  // Resize to 100% of current size as starting point
            
            // Position the layer (center it)
            var layerBounds = pastedLayer.bounds;
            var deltaX = (docWidth - (layerBounds[2].value - layerBounds[0].value)) / 2 - layerBounds[0].value;
            var deltaY = (docHeight - (layerBounds[3].value - layerBounds[1].value)) / 2 - layerBounds[1].value;
            pastedLayer.translate(deltaX, deltaY);
            
            // Close the image document (we don't need it anymore)
            imageDoc.close(SaveOptions.DONOTSAVECHANGES);
            
            // Flatten if needed (optional)
            // app.activeDocument.flatten();
            
            // Save the result
            app.activeDocument.saveToOE("png");
        """
    }
    
    return config

def generate_photopea_url(config):
    """Generate the Photopea URL with embedded configuration"""
    
    config_json = json.dumps(config, separators=(',', ':'))
    encoded_config = quote(config_json)
    
    photopea_url = f"https://www.photopea.com/#{encoded_config}"
    
    logger.info(f"Generated Photopea URL length: {len(photopea_url)}")
    return photopea_url

def create_psd_mockup(image_path, psd_template_path, output_dir="processed"):
    """
    Create a PSD mockup by inserting an image into a PSD template's smart layer using Photopea API
    
    Args:
        image_path (str): Path to the plain image file to be inserted
        psd_template_path (str): Path to the uploaded PSD template with smart layers
        output_dir (str): Output directory for processed files
    
    Returns:
        dict: Configuration for Photopea API including URL and callback settings
    """
    
    if not os.path.exists(psd_template_path):
        logger.error(f"PSD template not found: {psd_template_path}")
        return None
        
    if not os.path.exists(image_path):
        logger.error(f"Image file not found: {image_path}")
        return None
    
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Create Photopea configuration with the uploaded PSD template
        config = create_photopea_config(psd_template_path, image_path)
        
        # Generate Photopea URL
        photopea_url = generate_photopea_url(config)
        
        # Generate expected output filename
        image_name = os.path.splitext(os.path.basename(image_path))[0]
        psd_name = os.path.splitext(os.path.basename(psd_template_path))[0]
        output_filename = f"{image_name}_{psd_name}_mockup.png"
        output_path = os.path.join(output_dir, output_filename)
        
        result = {
            'success': True,
            'photopea_url': photopea_url,
            'output_path': output_path,
            'psd_file': psd_template_path,
            'image_file': image_path,
            'config': config
        }
        
        logger.info(f"Mockup configuration created for {image_name} + {psd_name}")
        return result
        
    except Exception as e:
        logger.error(f"Error creating PSD mockup configuration: {e}")
        return None

def get_available_psd_templates():
    """Get list of available uploaded PSD templates from uploads directory"""
    
    uploads_dir = "uploads"
    if not os.path.exists(uploads_dir):
        logger.warning(f"Uploads directory not found: {uploads_dir}")
        return []
    
    psd_files = []
    for filename in os.listdir(uploads_dir):
        if filename.lower().endswith('.psd'):
            full_path = os.path.join(uploads_dir, filename)
            psd_files.append({
                'filename': filename,
                'path': full_path,
                'name': os.path.splitext(filename)[0]
            })
    
    logger.info(f"Found {len(psd_files)} uploaded PSD templates")
    return psd_files

def get_available_psd_frames():
    """Get list of available PSD frame templates from psd_frames directory"""
    
    psd_frames_dir = "psd_frames"
    if not os.path.exists(psd_frames_dir):
        logger.warning(f"PSD frames directory not found: {psd_frames_dir}")
        return []
    
    psd_files = []
    for filename in os.listdir(psd_frames_dir):
        if filename.lower().endswith('.psd'):
            full_path = os.path.join(psd_frames_dir, filename)
            psd_files.append({
                'filename': filename,
                'path': full_path,
                'name': os.path.splitext(filename)[0]
            })
    
    logger.info(f"Found {len(psd_files)} PSD frame templates")
    return psd_files

def create_multiple_mockups(image_path, frame_names=None, output_dir="processed"):
    """
    Create mockups for an image using multiple PSD frames
    
    Args:
        image_path: Path to the image to frame
        frame_names: List of frame names to use (None = use all available)
        output_dir: Directory to save processed mockups
    
    Returns:
        list: List of result dictionaries for each mockup
    """
    
    available_frames = get_available_psd_frames()
    
    if not available_frames:
        logger.error("No PSD frames available")
        return []
    
    # Filter frames if specific names requested
    if frame_names:
        available_frames = [f for f in available_frames if f['name'] in frame_names]
        logger.info(f"Using {len(available_frames)} specified frames")
    
    results = []
    for frame in available_frames:
        logger.info(f"Creating mockup with frame: {frame['name']}")
        
        mockup_result = create_psd_mockup(
            image_path=image_path,
            psd_template_path=frame['path'],
            output_dir=output_dir
        )
        
        if mockup_result:
            mockup_result['frame_name'] = frame['name']
            results.append(mockup_result)
        else:
            logger.error(f"Failed to create mockup with frame: {frame['name']}")
    
    logger.info(f"Created {len(results)} mockup configurations")
    return results

def validate_photopea_response(response_data, expected_filename):
    """
    Validate response data from Photopea callback
    
    Args:
        response_data: Raw response data from Photopea
        expected_filename: Expected filename for validation
    
    Returns:
        dict: Parsed response information
    """
    
    try:
        # First 2000 bytes should be JSON
        json_data = response_data[:2000].decode('utf-8').rstrip('\x00')
        metadata = json.loads(json_data)
        
        # Rest is the image file(s)
        image_data = response_data[2000:]
        
        result = {
            'success': True,
            'metadata': metadata,
            'image_data': image_data,
            'filename': expected_filename
        }
        
        logger.info(f"Validated Photopea response: {len(image_data)} bytes")
        return result
        
    except Exception as e:
        logger.error(f"Error validating Photopea response: {e}")
        return {'success': False, 'error': str(e)}