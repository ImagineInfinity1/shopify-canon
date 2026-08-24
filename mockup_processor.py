"""
Server-side mockup processor using psd-tools and Pillow.

This is a fallback when Photopea iframe processing doesn't work.
It composites artwork images into PSD mockups by:
1. Reading the PSD structure with psd-tools
2. Finding smart object layers or using the full document size
3. Compositing the artwork under the mockup layers using Pillow
4. Exporting as PNG
"""

import os
import logging
from PIL import Image
from io import BytesIO

logger = logging.getLogger(__name__)

# Try to import psd-tools
try:
    from psd_tools import PSDImage
    PSD_TOOLS_AVAILABLE = True
except ImportError:
    PSD_TOOLS_AVAILABLE = False
    logger.warning("psd-tools not available. Install with: pip install psd-tools")


def get_smart_object_info(psd_path):
    """
    Extract smart object layer information from a PSD file.
    Returns dict with layer info or None if no smart object found.
    """
    if not PSD_TOOLS_AVAILABLE:
        return None
    
    try:
        psd = PSDImage.open(psd_path)
        
        def find_smart_object(layers, depth=0):
            """Recursively search for smart object layers."""
            for layer in layers:
                # Check if this is a smart object
                if hasattr(layer, 'kind') and layer.kind == 'smartobject':
                    return {
                        'name': layer.name,
                        'bbox': layer.bbox,  # (left, top, right, bottom)
                        'width': layer.width,
                        'height': layer.height,
                        'offset': (layer.offset[0], layer.offset[1])
                    }
                
                # Check for nested layers (groups)
                if hasattr(layer, 'layers') and layer.layers:
                    result = find_smart_object(layer.layers, depth + 1)
                    if result:
                        return result
            
            return None
        
        smart_obj = find_smart_object(psd)
        
        # Also get overall PSD dimensions
        psd_info = {
            'width': psd.width,
            'height': psd.height,
            'smart_object': smart_obj
        }
        
        return psd_info
        
    except Exception as e:
        logger.error(f"Error reading PSD: {e}")
        return None


def render_psd_to_image(psd_path):
    """
    Render a PSD file to a PIL Image.
    Returns the flattened/composite image.
    """
    if not PSD_TOOLS_AVAILABLE:
        raise RuntimeError("psd-tools not available")
    
    try:
        psd = PSDImage.open(psd_path)
        # Composite all layers into a single image
        return psd.composite()
    except Exception as e:
        logger.error(f"Error rendering PSD: {e}")
        raise


def create_mockup(psd_path, artwork_path, output_path=None, placement_mode='cover'):
    """
    Create a mockup by compositing artwork into a PSD template.
    
    Args:
        psd_path: Path to the PSD mockup template
        artwork_path: Path to the artwork image (PNG, JPG, etc.)
        output_path: Optional path to save the result (if None, returns bytes)
        placement_mode: 'cover' (fill area, may crop) or 'contain' (fit inside)
    
    Returns:
        If output_path is None: PNG image bytes
        If output_path is provided: True on success
    """
    try:
        # Load artwork
        logger.info(f"Loading artwork: {artwork_path}")
        artwork = Image.open(artwork_path)
        if artwork.mode != 'RGBA':
            artwork = artwork.convert('RGBA')
        
        # Try to get PSD info (including smart object location)
        psd_info = get_smart_object_info(psd_path)
        
        if psd_info and PSD_TOOLS_AVAILABLE:
            # Render the PSD to get the mockup overlay
            logger.info(f"Rendering PSD mockup: {psd_path}")
            mockup_overlay = render_psd_to_image(psd_path)
            if mockup_overlay.mode != 'RGBA':
                mockup_overlay = mockup_overlay.convert('RGBA')
            
            canvas_width = psd_info['width']
            canvas_height = psd_info['height']
            
            # Determine placement area
            if psd_info.get('smart_object'):
                # Use smart object bounds
                so = psd_info['smart_object']
                place_x = so['bbox'][0]
                place_y = so['bbox'][1]
                place_width = so['width']
                place_height = so['height']
                logger.info(f"Smart object found: {so['name']} at ({place_x}, {place_y}) size {place_width}x{place_height}")
            else:
                # Use full canvas
                place_x = 0
                place_y = 0
                place_width = canvas_width
                place_height = canvas_height
                logger.info(f"No smart object found, using full canvas: {place_width}x{place_height}")
            
        else:
            # Fallback: Open PSD as regular image with Pillow
            logger.info("Using Pillow fallback for PSD (limited support)")
            try:
                mockup_overlay = Image.open(psd_path)
                if mockup_overlay.mode != 'RGBA':
                    mockup_overlay = mockup_overlay.convert('RGBA')
                canvas_width = mockup_overlay.width
                canvas_height = mockup_overlay.height
            except Exception as e:
                logger.error(f"Pillow cannot read PSD: {e}")
                # Last resort: just resize artwork to a standard size
                canvas_width = artwork.width
                canvas_height = artwork.height
                mockup_overlay = None
            
            place_x = 0
            place_y = 0
            place_width = canvas_width
            place_height = canvas_height
        
        # Calculate artwork scaling
        art_width, art_height = artwork.size
        
        if placement_mode == 'cover':
            # Scale to cover the area (may crop)
            scale = max(place_width / art_width, place_height / art_height)
        else:  # contain
            # Scale to fit inside (may have borders)
            scale = min(place_width / art_width, place_height / art_height)
        
        new_width = int(art_width * scale)
        new_height = int(art_height * scale)
        
        # Resize artwork
        logger.info(f"Resizing artwork from {art_width}x{art_height} to {new_width}x{new_height}")
        artwork_resized = artwork.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Create canvas
        canvas = Image.new('RGBA', (canvas_width, canvas_height), (255, 255, 255, 255))
        
        # Center artwork in placement area
        art_x = place_x + (place_width - new_width) // 2
        art_y = place_y + (place_height - new_height) // 2
        
        # Paste artwork onto canvas
        canvas.paste(artwork_resized, (art_x, art_y), artwork_resized)
        
        # Overlay the mockup on top (if we have one)
        if mockup_overlay is not None:
            canvas = Image.alpha_composite(canvas, mockup_overlay)
        
        # Convert to RGB for output (PNG can be RGBA, but let's keep it simple)
        canvas_rgb = Image.new('RGB', canvas.size, (255, 255, 255))
        canvas_rgb.paste(canvas, mask=canvas.split()[3] if canvas.mode == 'RGBA' else None)
        
        # Output
        if output_path:
            canvas_rgb.save(output_path, 'PNG', quality=95)
            logger.info(f"Saved mockup to: {output_path}")
            return True
        else:
            buffer = BytesIO()
            canvas_rgb.save(buffer, 'PNG', quality=95)
            buffer.seek(0)
            return buffer.getvalue()
        
    except Exception as e:
        logger.error(f"Error creating mockup: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise


def process_mockup_batch(psd_templates, artwork_files, output_dir):
    """
    Process multiple artworks with multiple templates.
    
    Args:
        psd_templates: List of PSD file paths
        artwork_files: List of artwork file paths  
        output_dir: Directory to save results
    
    Returns:
        List of output file paths
    """
    os.makedirs(output_dir, exist_ok=True)
    results = []
    
    for artwork_path in artwork_files:
        artwork_name = os.path.splitext(os.path.basename(artwork_path))[0]
        
        for psd_path in psd_templates:
            psd_name = os.path.splitext(os.path.basename(psd_path))[0]
            
            output_name = f"{artwork_name}_{psd_name}_mockup.png"
            output_path = os.path.join(output_dir, output_name)
            
            try:
                create_mockup(psd_path, artwork_path, output_path)
                results.append({
                    'success': True,
                    'artwork': artwork_path,
                    'template': psd_path,
                    'output': output_path
                })
            except Exception as e:
                results.append({
                    'success': False,
                    'artwork': artwork_path,
                    'template': psd_path,
                    'error': str(e)
                })
    
    return results


# Test function
if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python mockup_processor.py <psd_file> <artwork_file> [output_file]")
        print(f"\npsd-tools available: {PSD_TOOLS_AVAILABLE}")
        sys.exit(1)
    
    psd_file = sys.argv[1]
    artwork_file = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else 'mockup_output.png'
    
    logging.basicConfig(level=logging.INFO)
    
    try:
        create_mockup(psd_file, artwork_file, output_file)
        print(f"Success! Output saved to: {output_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
