import os
import logging
from PIL import Image
import tempfile

logger = logging.getLogger(__name__)

def process_psd_frames(image_path, output_folder=None, psd_frames_folder=None, task_id=None):
    """Process image through uploaded PSD templates and return list of output paths"""
    
    output_paths = []
    
    try:
        # Import temp_file_service here to avoid circular imports
        from temp_file_service import temp_file_service
        
        # Use temp directory if output_folder not specified or if it's the old processed folder
        use_temp = output_folder is None or output_folder == 'processed'
        
        # CRITICAL FIX: Use uploaded PSD templates from temp directory
        # PSD templates should be in temp directory, not permanent uploads folder
        uploaded_psds = []
        
        # Check temp directory for PSD files (they should be tracked by task_id)
        if task_id:
            temp_files = temp_file_service.get_task_files(task_id)
            for temp_file in temp_files:
                if temp_file.lower().endswith('.psd') or temp_file.lower().endswith('.psb'):
                    uploaded_psds.append(temp_file)
                    logger.info(f"Found PSD template in temp: {os.path.basename(temp_file)}")
        
        # Also check if there are any PSD files in the temp base directory (for current session)
        temp_base = temp_file_service.get_temp_dir()
        if os.path.exists(temp_base):
            for filename in os.listdir(temp_base):
                if filename.lower().endswith(('.psd', '.psb')):
                    psd_path = os.path.join(temp_base, filename)
                    if psd_path not in uploaded_psds:
                        uploaded_psds.append(psd_path)
                        logger.info(f"Found PSD template in temp base: {filename}")
        
        if uploaded_psds:
            logger.info(f"Using {len(uploaded_psds)} PSD templates for processing")
            return process_with_real_psds(image_path, uploaded_psds, output_folder, task_id)
        
        # Final fallback to simulated frames (output to temp)
        logger.warning("No PSD files found, using simulated frames")
        return process_simulated_frames(image_path, output_folder, task_id)
        
    except Exception as e:
        logger.error(f"Error processing PSD frames for {image_path}: {str(e)}")
        return []

def create_simulated_frame(original_img, frame_name, frame_number):
    """Create a simulated frame around the image"""
    
    # Define frame styles
    frame_styles = {
        1: {  # SH Frame 1 - MAIN (minimal white border)
            'border_width': 40,
            'border_color': (255, 255, 255),
            'shadow': True
        },
        2: {  # SH Frame 4 (black border with mat)
            'border_width': 60,
            'border_color': (0, 0, 0),
            'mat_width': 30,
            'mat_color': (245, 245, 245),
            'shadow': True
        },
        3: {  # SH Frame 5 (wooden effect)
            'border_width': 50,
            'border_color': (139, 90, 43),
            'shadow': True,
            'texture': True
        }
    }
    
    style = frame_styles.get(frame_number, frame_styles[1])
    
    # Calculate dimensions
    border_width = style['border_width']
    mat_width = style.get('mat_width', 0)
    total_border = border_width + mat_width
    
    # Resize original image to fit in frame (maintaining aspect ratio)
    frame_size = 1200  # Target frame size
    available_size = frame_size - (total_border * 2)
    
    original_img.thumbnail((available_size, available_size), Image.Resampling.LANCZOS)
    
    # Create new image with frame
    final_width = original_img.width + (total_border * 2)
    final_height = original_img.height + (total_border * 2)
    
    # Create background
    framed_img = Image.new('RGB', (final_width, final_height), style['border_color'])
    
    # Add mat if specified
    if mat_width > 0:
        mat_color = style.get('mat_color', (255, 255, 255))
        mat_left = border_width
        mat_top = border_width
        mat_right = final_width - border_width
        mat_bottom = final_height - border_width
        
        mat_area = Image.new('RGB', (mat_right - mat_left, mat_bottom - mat_top), mat_color)
        framed_img.paste(mat_area, (mat_left, mat_top))
    
    # Calculate position to center the original image
    paste_x = (final_width - original_img.width) // 2
    paste_y = (final_height - original_img.height) // 2
    
    # Paste the original image
    framed_img.paste(original_img, (paste_x, paste_y))
    
    # Add texture effect for wooden frame
    if style.get('texture'):
        framed_img = add_wood_texture(framed_img, border_width)
    
    # Add shadow effect
    if style.get('shadow'):
        framed_img = add_shadow_effect(framed_img)
    
    return framed_img

def add_wood_texture(img, border_width):
    """Add a subtle wood texture effect to the border"""
    try:
        # Create a simple wood-like texture by adding noise to border areas
        from PIL import ImageFilter, ImageEnhance
        
        # Apply a slight blur to simulate wood grain
        textured = img.filter(ImageFilter.GaussianBlur(radius=0.5))
        
        # Adjust contrast slightly
        enhancer = ImageEnhance.Contrast(textured)
        textured = enhancer.enhance(1.1)
        
        return textured
    except:
        return img

def add_shadow_effect(img):
    """Add a subtle drop shadow effect"""
    try:
        from PIL import ImageFilter
        
        # Create shadow
        shadow_offset = 10
        shadow_blur = 5
        
        # Create a slightly larger canvas for shadow
        shadow_width = img.width + shadow_offset
        shadow_height = img.height + shadow_offset
        
        # Create shadow image
        shadow_img = Image.new('RGB', (shadow_width, shadow_height), (240, 240, 240))
        
        # Create shadow
        shadow = Image.new('RGBA', (img.width, img.height), (0, 0, 0, 80))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
        
        # Paste shadow
        shadow_img.paste(shadow, (shadow_offset, shadow_offset), shadow)
        
        # Paste original image
        shadow_img.paste(img, (0, 0))
        
        return shadow_img
    except:
        return img

def process_with_real_psds(image_path, psd_paths, output_folder=None, task_id=None):
    """Process image with actual PSD files using psd-tools"""
    output_paths = []
    
    try:
        from psd_tools import PSDImage
    except ImportError:
        logger.error("psd-tools not available, falling back to simulated frames")
        return process_simulated_frames(image_path, output_folder, task_id)
    
    # Import temp_file_service here to avoid circular imports
    from temp_file_service import temp_file_service
    
    # Use temp directory if output_folder not specified
    use_temp = output_folder is None or output_folder == 'processed'
    
    base_filename = os.path.splitext(os.path.basename(image_path))[0]
    
    for i, psd_path in enumerate(psd_paths, 1):
        try:
            # Create temp file for output if using temp directory
            if use_temp:
                output_path = temp_file_service.create_temp_file(
                    prefix=f'frame_{i}',
                    suffix='.jpg',
                    task_id=task_id
                )
            else:
                output_filename = f"{base_filename}_frame_{i}.jpg"
                os.makedirs(output_folder, exist_ok=True)
                output_path = os.path.join(output_folder, output_filename)
            
            result = process_single_psd(image_path, psd_path, output_path)
            if result:
                output_paths.append(result)
                logger.info(f"Processed PSD frame {i}: {os.path.basename(output_path)}")
            else:
                logger.warning(f"Failed to process PSD frame {i}, using fallback")
                # Create fallback frame
                try:
                    fallback_path = create_fallback_frame(image_path, output_path, i)
                    if fallback_path:
                        output_paths.append(fallback_path)
                except Exception as fe:
                    logger.error(f"Error creating fallback frame: {str(fe)}")
        except Exception as e:
            logger.error(f"Error processing PSD {psd_path}: {str(e)}")
            # Create fallback frame
            try:
                if use_temp:
                    fallback_output_path = temp_file_service.create_temp_file(
                        prefix=f'fallback_frame_{i}',
                        suffix='.jpg',
                        task_id=task_id
                    )
                else:
                    output_filename = f"{base_filename}_frame_{i}.jpg"
                    fallback_output_path = os.path.join(output_folder, output_filename)
                fallback_path = create_fallback_frame(image_path, fallback_output_path, i)
                if fallback_path:
                    output_paths.append(fallback_path)
            except Exception as fe:
                logger.error(f"Error creating fallback frame: {str(fe)}")
    
    return output_paths

def process_single_psd(image_path, psd_path, output_path):
    """Process a single image with a PSD template by replacing smart layer content"""
    try:
        from psd_tools import PSDImage
        from psd_tools.constants import BlendMode
        
        # Open PSD file
        psd = PSDImage.open(psd_path)
        logger.info(f"Opened PSD: {psd_path}")
        
        # Load the new image
        with Image.open(image_path) as new_image:
            # Convert to RGB if needed
            if new_image.mode in ('RGBA', 'P'):
                background = Image.new('RGB', new_image.size, (255, 255, 255))
                if new_image.mode == 'RGBA':
                    background.paste(new_image, mask=new_image.split()[-1])
                else:
                    background.paste(new_image)
                new_image = background
            elif new_image.mode != 'RGB':
                new_image = new_image.convert('RGB')
            
            # Find the top smart layer or image layer to replace
            smart_layer = find_smart_layer(psd)
            
            if smart_layer:
                logger.info(f"Found smart layer: {smart_layer.name}")
                # Process with simulated smart object approach
                result_image = process_psd_with_simulated_smart_object(psd, smart_layer, new_image)
            else:
                logger.info("No smart layer found, using manual positioning")
                # Fallback to manual positioning
                result_image = manual_image_positioning(psd, new_image)
            
            if not result_image:
                logger.error(f"Failed to process PSD: {psd_path}")
                return None
            
            # Convert to RGB if needed (important for JPEG saving)
            if result_image.mode in ('RGBA', 'P'):
                background = Image.new('RGB', result_image.size, (255, 255, 255))
                if result_image.mode == 'RGBA':
                    background.paste(result_image, mask=result_image.split()[-1])
                else:
                    background.paste(result_image)
                result_image = background
            elif result_image.mode != 'RGB':
                result_image = result_image.convert('RGB')
            
            # Save the result
            result_image.save(output_path, 'JPEG', quality=85, optimize=True)
            return output_path
        
    except ImportError as e:
        logger.error(f"Missing psd-tools dependency: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error processing PSD {psd_path}: {str(e)}")
        return None

def find_smart_layer(psd):
    """Find the topmost smart object or image layer in the PSD"""
    try:
        logger.info(f"Analyzing PSD with {len(psd)} layers")
        
        # Log all layers for debugging
        for i, layer in enumerate(psd):
            layer_info = f"Layer {i}: name='{getattr(layer, 'name', 'unnamed')}'"
            if hasattr(layer, 'kind'):
                layer_info += f", kind={layer.kind}"
            if hasattr(layer, 'visible'):
                layer_info += f", visible={layer.visible}"
            if hasattr(layer, 'bbox'):
                layer_info += f", bbox={layer.bbox}"
            logger.info(layer_info)
        
        # Find the topmost smart object layer only (ignore others)
        topmost_smart_layer = None
        for layer in psd:
            if hasattr(layer, 'kind') and layer.kind:
                kind_str = str(layer.kind).lower()
                if 'smart' in kind_str:
                    # Skip background/base layers even if they're smart objects
                    layer_name = str(getattr(layer, 'name', '')).lower()
                    if not any(bg_name in layer_name for bg_name in ['background', 'base', 'layer 3']):
                        if topmost_smart_layer is None:
                            topmost_smart_layer = layer
                            logger.info(f"Found topmost smart object layer: {layer.name} ({layer.kind})")
                        else:
                            logger.info(f"Ignoring additional smart layer: {layer.name}")
        
        if topmost_smart_layer:
            return topmost_smart_layer
        
        # Second, look for layers with names indicating they're for image placement
        for layer in psd:
            if hasattr(layer, 'name'):
                layer_name = str(layer.name).lower()
                if any(keyword in layer_name for keyword in ['image', 'artwork', 'design', 'your', 'mex5']):
                    logger.info(f"Found target layer by name: {layer.name}")
                    return layer
        
        # If no smart layer found, return the first visible layer that's not a background
        for layer in psd:
            if hasattr(layer, 'visible') and layer.visible:
                layer_name = str(getattr(layer, 'name', '')).lower()
                if 'background' not in layer_name:
                    logger.info(f"Using first visible non-background layer: {getattr(layer, 'name', 'unnamed')}")
                    return layer
                
        # Fallback to first layer
        if len(psd) > 0:
            logger.info(f"Using first layer as fallback: {getattr(psd[0], 'name', 'unnamed')}")
            return psd[0]
            
    except Exception as e:
        logger.error(f"Error finding smart layer: {str(e)}")
    
    return None

def replace_smart_layer_content(psd, smart_layer, new_image):
    """Replace the content of a smart layer with the new image using proper smart object handling"""
    try:
        # Check if this is actually a smart object
        if hasattr(smart_layer, 'smart_object') and smart_layer.smart_object:
            logger.info(f"Processing smart object: {smart_layer.name}")
            return replace_smart_object_content_advanced(psd, smart_layer, new_image)
        else:
            logger.info(f"Regular layer replacement: {smart_layer.name}")
            return replace_regular_layer_content(psd, smart_layer, new_image)
            
    except Exception as e:
        logger.error(f"Error replacing smart layer content: {str(e)}")
        return None

def replace_smart_object_content_advanced(psd, smart_layer, new_image):
    """Replace smart object content by accessing and modifying the embedded PSD"""
    try:
        # Get smart layer bounds
        bbox = smart_layer.bbox
        if hasattr(bbox, 'width'):
            layer_width, layer_height = bbox.width, bbox.height
            layer_left, layer_top = bbox.left, bbox.top
        else:
            layer_left, layer_top, layer_right, layer_bottom = bbox
            layer_width = layer_right - layer_left
            layer_height = layer_bottom - layer_top
        
        logger.info(f"Smart object layer bounds: {layer_width}x{layer_height} at ({layer_left}, {layer_top})")
        
        # Access the smart object's embedded content
        if hasattr(smart_layer, 'smart_object') and smart_layer.smart_object:
            so = smart_layer.smart_object
            
            # Method 1: Try to access embedded PSD directly
            if hasattr(so, 'psd') and so.psd:
                embedded_psd = so.psd
                logger.info(f"Found embedded PSD: {embedded_psd.width}x{embedded_psd.height}")
                
                # Create replacement image that fills the ENTIRE embedded PSD area
                # This mimics "stretching the image over the total open area"
                replacement_img = new_image.resize((embedded_psd.width, embedded_psd.height), Image.Resampling.LANCZOS)
                logger.info(f"Resized image to fill embedded PSD: {replacement_img.size}")
                
                # Now we need to composite the entire PSD with this replacement
                # Since we can't directly modify the embedded PSD, we'll simulate the result
                return create_smart_object_replacement(psd, smart_layer, replacement_img)
            
            # Method 2: If no embedded PSD, use the smart object data
            elif hasattr(so, 'data') and so.data:
                logger.info(f"Found smart object data: {len(so.data)} bytes")
                # Use the smart layer dimensions as the embedded dimensions
                replacement_img = new_image.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
                return create_smart_object_replacement(psd, smart_layer, replacement_img)
            
            else:
                logger.warning("Smart object has no accessible embedded content")
                # Fallback to using layer dimensions
                replacement_img = new_image.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
                return create_smart_object_replacement(psd, smart_layer, replacement_img)
        
        else:
            logger.error("Layer is not a smart object")
            return None
            
    except Exception as e:
        logger.error(f"Error in smart object replacement: {str(e)}")
        return None

def create_smart_object_replacement(psd, smart_layer, replacement_img):
    """Create the final composite with the replacement image in the smart object position"""
    try:
        # Get smart layer position
        bbox = smart_layer.bbox
        if hasattr(bbox, 'width'):
            layer_left, layer_top = bbox.left, bbox.top
            layer_width, layer_height = bbox.width, bbox.height
        else:
            layer_left, layer_top, layer_right, layer_bottom = bbox
            layer_width = layer_right - layer_left
            layer_height = layer_bottom - layer_top
        
        # Create base composite from all layers
        layers_to_composite = []
        smart_layer_index = None
        
        # Collect all layers and find smart layer index
        for i, layer in enumerate(psd):
            if layer.name == smart_layer.name:
                smart_layer_index = i
                # Add a placeholder for the smart layer position
                layers_to_composite.append(None)
            else:
                layers_to_composite.append(layer)
        
        # Create result image
        result_image = Image.new('RGBA', (psd.width, psd.height), (0, 0, 0, 0))
        
        # Composite layers in order
        for i, layer in enumerate(layers_to_composite):
            if i == smart_layer_index:
                # This is where the smart object goes - add our replacement
                # Scale the replacement to fit the smart object layer size
                final_replacement = replacement_img.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
                
                if final_replacement.mode != 'RGBA':
                    final_replacement = final_replacement.convert('RGBA')
                
                result_image.paste(final_replacement, (layer_left, layer_top), final_replacement)
                logger.info(f"Placed replacement image at smart object position: ({layer_left}, {layer_top})")
                
            elif layer and layer.visible:
                # Regular layer - composite normally
                try:
                    layer_composite = layer.composite()
                    if layer_composite:
                        if hasattr(layer.bbox, 'left'):
                            left, top = layer.bbox.left, layer.bbox.top
                        else:
                            left, top = layer.bbox[0], layer.bbox[1]
                        
                        if layer_composite.mode == 'RGBA':
                            result_image.paste(layer_composite, (left, top), layer_composite)
                        else:
                            layer_composite = layer_composite.convert('RGBA')
                            result_image.paste(layer_composite, (left, top), layer_composite)
                        
                        logger.info(f"Composited layer: {layer.name}")
                except Exception as e:
                    logger.warning(f"Failed to composite layer {layer.name}: {e}")
        
        # Convert to RGB for final output
        if result_image.mode == 'RGBA':
            background = Image.new('RGB', result_image.size, (255, 255, 255))
            background.paste(result_image, mask=result_image.split()[-1])
            result_image = background
        
        return result_image
        
    except Exception as e:
        logger.error(f"Error creating smart object replacement: {str(e)}")
        return None

def replace_regular_layer_content(psd, target_layer, new_image):
    """Replace content of a regular layer"""
    try:
        # Get the layer bounds
        bbox = target_layer.bbox
        if hasattr(bbox, 'width'):
            layer_width, layer_height = bbox.width, bbox.height
            layer_left, layer_top = bbox.left, bbox.top
        else:
            layer_left, layer_top, layer_right, layer_bottom = bbox
            layer_width = layer_right - layer_left
            layer_height = layer_bottom - layer_top
        
        logger.info(f"Regular layer bounds: {layer_width}x{layer_height} at ({layer_left}, {layer_top})")
        
        # Start with full PSD composite
        result_image = psd.composite()
        if not result_image:
            logger.error("Failed to create PSD composite")
            return None
        
        # Resize new image to fit the layer bounds
        replacement_image = new_image.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
        
        # Paste at layer position
        if replacement_image.mode == 'RGBA':
            result_image.paste(replacement_image, (layer_left, layer_top), replacement_image)
        else:
            result_image.paste(replacement_image, (layer_left, layer_top))
        
        return result_image
        
    except Exception as e:
        logger.error(f"Error replacing regular layer: {str(e)}")
        return None

def process_psd_with_simulated_smart_object(psd, smart_layer, new_image):
    """Process PSD by simulating proper smart object behavior"""
    try:
        # Get the full PSD composite first
        base_composite = psd.composite()
        if not base_composite:
            logger.error("Failed to get PSD composite")
            return None
        
        # Get smart layer bounds
        bbox = smart_layer.bbox
        if hasattr(bbox, 'width'):
            layer_left, layer_top = bbox.left, bbox.top
            layer_width, layer_height = bbox.width, bbox.height
        else:
            layer_left, layer_top, layer_right, layer_bottom = bbox
            layer_width = layer_right - layer_left
            layer_height = layer_bottom - layer_top
        
        logger.info(f"Smart object position: ({layer_left}, {layer_top}) size: {layer_width}x{layer_height}")
        
        # Create a mask for the smart object area from the original composite
        # This helps us preserve any frame effects or borders
        smart_area = base_composite.crop((layer_left, layer_top, layer_left + layer_width, layer_top + layer_height))
        
        # Resize the new image to exactly fit the smart object area
        replacement = new_image.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
        
        # Convert to same mode as base
        if base_composite.mode != replacement.mode:
            if base_composite.mode == 'RGBA':
                replacement = replacement.convert('RGBA')
            elif base_composite.mode == 'RGB':
                replacement = replacement.convert('RGB')
        
        # Create result starting with the base composite
        result = base_composite.copy()
        
        # Paste the replacement image at the exact smart object location
        if result.mode == 'RGBA' and replacement.mode == 'RGBA':
            result.paste(replacement, (layer_left, layer_top), replacement)
        else:
            result.paste(replacement, (layer_left, layer_top))
        
        logger.info("Successfully replaced smart object content")
        return result
        
    except Exception as e:
        logger.error(f"Error in simulated smart object processing: {str(e)}")
        return None

def manual_image_positioning(psd, new_image):
    """Fallback method for manual image positioning when smart layer is not found"""
    try:
        # Composite the PSD to get the base template
        psd_image = psd.composite()
        
        if not psd_image:
            logger.error("Failed to composite PSD")
            return None
        
        # Resize new image to fit nicely in the frame (80% of PSD dimensions, centered)
        psd_width, psd_height = psd_image.size
        max_img_width = int(psd_width * 0.8)
        max_img_height = int(psd_height * 0.8)
        
        # Maintain aspect ratio but fill more of the frame
        img_ratio = new_image.width / new_image.height
        frame_ratio = max_img_width / max_img_height
        
        if img_ratio > frame_ratio:
            # Image is wider, fit to width
            final_width = max_img_width
            final_height = int(max_img_width / img_ratio)
        else:
            # Image is taller, fit to height
            final_height = max_img_height
            final_width = int(max_img_height * img_ratio)
        
        resized_image = new_image.resize((final_width, final_height), Image.Resampling.LANCZOS)
        
        # Calculate position to center the image
        paste_x = (psd_width - final_width) // 2
        paste_y = (psd_height - final_height) // 2
        
        # Paste the image onto the PSD template
        psd_image.paste(resized_image, (paste_x, paste_y))
        
        return psd_image
        
    except Exception as e:
        logger.error(f"Error in manual positioning: {str(e)}")
        return None

def create_fallback_frame(image_path, output_path, frame_number):
    """Create a fallback frame when PSD processing fails"""
    try:
        with Image.open(image_path) as original_img:
            framed_img = create_simulated_frame(original_img, f"Frame {frame_number}", frame_number)
            framed_img.save(output_path, 'JPEG', quality=85, optimize=True)
            return output_path
    except Exception as e:
        logger.error(f"Error creating fallback frame: {str(e)}")
        return None

def process_simulated_frames(image_path, output_folder=None, task_id=None):
    """Process using simulated frames (fallback when PSD processing fails)"""
    output_paths = []
    
    try:
        # Import temp_file_service here to avoid circular imports
        from temp_file_service import temp_file_service
        
        # Use temp directory if output_folder not specified
        use_temp = output_folder is None or output_folder == 'processed'
        
        with Image.open(image_path) as original_img:
            # Convert to RGB if needed
            if original_img.mode in ('RGBA', 'P'):
                original_img = original_img.convert('RGB')
            
            base_filename = os.path.splitext(os.path.basename(image_path))[0]
            
            for i in range(1, 4):  # Create 3 frames
                try:
                    # Simulate frame processing
                    framed_img = create_simulated_frame(original_img, f"Simulated Frame {i}", i)
                    
                    # Save the framed image to temp or specified folder
                    if use_temp:
                        output_path = temp_file_service.create_temp_file(
                            prefix=f'sim_frame_{i}',
                            suffix='.jpg',
                            task_id=task_id
                        )
                    else:
                        output_filename = f"{base_filename}_frame_{i}.jpg"
                        os.makedirs(output_folder, exist_ok=True)
                        output_path = os.path.join(output_folder, output_filename)
                    
                    framed_img.save(output_path, 'JPEG', quality=85, optimize=True)
                    output_paths.append(output_path)
                    
                    logger.info(f"Created simulated frame: {os.path.basename(output_path)}")
                    
                except Exception as e:
                    logger.error(f"Error processing simulated frame {i}: {str(e)}")
                    continue
        
        return output_paths
        
    except Exception as e:
        logger.error(f"Error processing simulated frames for {image_path}: {str(e)}")
        return []


