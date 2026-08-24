import os
import logging
from PIL import Image, ImageOps
import tempfile

logger = logging.getLogger(__name__)

def compress_image(input_path, output_folder=None, max_width=2000, max_size_mb=1, quality=85, task_id=None, fast=False):
    """
    Compress and optimize image for web use

    Args:
        input_path: Path to input image
        output_folder: Optional folder to save compressed image (if None, uses temp)
        max_width: Maximum width in pixels
        max_size_mb: Maximum file size in MB
        quality: JPEG quality (1-100)
        task_id: Optional task ID for temp file tracking
        fast: When True, do a single low-CPU encode (optimize=False, no quality-search
              loop). Used for images that arrive already optimized (e.g. desktop-framed
              mockups) so the 0.1-CPU Render free tier isn't pegged re-encoding a
              5MP image ~6x per file. The size cap is treated as a soft hint.

    Returns:
        Path to compressed image (temporary file)
    """
    try:
        # Import temp_file_service here to avoid circular imports
        from temp_file_service import temp_file_service
        
        # Use temp directory if output_folder not specified or if it's the old processed folder
        if output_folder is None or output_folder == 'processed':
            output_path = temp_file_service.create_temp_file(
                prefix='compressed',
                suffix='.jpg',
                task_id=task_id
            )
        else:
            # Legacy support: create output folder if it doesn't exist
            os.makedirs(output_folder, exist_ok=True)
            # Generate output filename
            base_name = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(output_folder, f"{base_name}_compressed.jpg")
        
        # Open and process image
        with Image.open(input_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                # Create a white background for transparency
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                else:
                    background.paste(img)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Fix orientation based on EXIF data
            img = ImageOps.exif_transpose(img)
            
            # Resize if too large
            original_width, original_height = img.size
            if original_width > max_width:
                ratio = max_width / original_width
                new_height = int(original_height * ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                logger.info(f"Resized from {original_width}x{original_height} to {max_width}x{new_height}")
            
            # Fast path: single encode, no optimize pass, no quality-search loop.
            # ~5-10x cheaper CPU than the loop below — critical on shared-CPU hosts.
            if fast:
                img.save(output_path, 'JPEG', quality=quality, optimize=False, progressive=False)
                logger.info(f"Compressed image (fast): {os.path.getsize(output_path) / 1024:.1f}KB at {quality}% quality")
                return output_path

            # Start with specified quality
            current_quality = quality
            max_size_bytes = max_size_mb * 1024 * 1024

            # Try different quality levels to meet size requirement
            while current_quality >= 60:  # Don't go below 60% quality
                # Save to temporary file to check size
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp_file:
                    temp_path = temp_file.name
                    img.save(temp_path, 'JPEG', quality=current_quality, optimize=True)
                
                # Check file size
                file_size = os.path.getsize(temp_path)
                
                if file_size <= max_size_bytes:
                    # Size is acceptable, copy to final location (avoid cross-device issues)
                    import shutil
                    shutil.move(temp_path, output_path)
                    logger.info(f"Compressed image: {file_size / 1024:.1f}KB at {current_quality}% quality")
                    return output_path
                else:
                    # File too large, try lower quality
                    os.unlink(temp_path)
                    current_quality -= 5
                    logger.debug(f"File too large ({file_size / 1024:.1f}KB), trying {current_quality}% quality")
            
            # If we get here, save with minimum quality
            img.save(output_path, 'JPEG', quality=60, optimize=True)
            final_size = os.path.getsize(output_path)
            logger.warning(f"Could not achieve target size, saved at {final_size / 1024:.1f}KB with 60% quality")
            
            return output_path
            
    except Exception as e:
        logger.error(f"Error compressing image {input_path}: {str(e)}")
        return None

def optimize_for_web(image_path, output_folder):
    """Optimize image specifically for web display"""
    return compress_image(
        image_path, 
        output_folder, 
        max_width=1920,  # Good for web display
        max_size_mb=0.8,  # Under 1MB
        quality=85
    )

def optimize_for_thumbnail(image_path, output_folder, size=(300, 300)):
    """Create optimized thumbnail"""
    try:
        os.makedirs(output_folder, exist_ok=True)
        
        base_name = os.path.splitext(os.path.basename(image_path))[0]
        output_path = os.path.join(output_folder, f"{base_name}_thumb.jpg")
        
        with Image.open(image_path) as img:
            # Convert to RGB
            if img.mode != 'RGB':
                if img.mode in ('RGBA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'RGBA':
                        background.paste(img, mask=img.split()[-1])
                    else:
                        background.paste(img)
                    img = background
                else:
                    img = img.convert('RGB')
            
            # Create thumbnail
            img.thumbnail(size, Image.Resampling.LANCZOS)
            
            # Save with high compression for thumbnails
            img.save(output_path, 'JPEG', quality=80, optimize=True)
            
            logger.info(f"Created thumbnail: {img.size}")
            return output_path
            
    except Exception as e:
        logger.error(f"Error creating thumbnail for {image_path}: {str(e)}")
        return None

def validate_image(image_path):
    """Validate if file is a valid image"""
    try:
        with Image.open(image_path) as img:
            img.verify()
        return True
    except Exception:
        return False

def get_image_info(image_path):
    """Get basic information about an image"""
    try:
        with Image.open(image_path) as img:
            return {
                'format': img.format,
                'mode': img.mode,
                'size': img.size,
                'width': img.width,
                'height': img.height,
                'file_size': os.path.getsize(image_path)
            }
    except Exception as e:
        logger.error(f"Error getting image info for {image_path}: {str(e)}")
        return None
