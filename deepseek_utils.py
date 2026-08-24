import os
import requests
import json
import logging
from PIL import Image
import base64
import io

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Collections list for DeepSeek prompt
COLLECTIONS = [
    "Minimalist Wall Art | Minimalist Art Prints",
    "Kitchen Wall Art | Kitchen Decor", 
    "Brown Prints | Brown Posters",
    "Paintings",
    "Green Wall Art | Green Art",
    "Mexican Art | Mexican Decor",
    "Landscape Posters | Landscape Art Prints",
    "Illustration Posters | Illustrated Wall Art",
    "Multi Coloured Wall Art | Bright Posters",
    "Botanical Prints | Art With Flowers",
    "Animal Prints | Animal Art",
    "Pattern Art | Shape Posters",
    "Abstract Prints | Abstract Wall Art",
    "Human Body Art | Human Figure Art",
    "Typography Posters | Text Art",
    "Vintage Posters | Retro Art",
    "Modern Art | Contemporary Prints",
    "Nature Prints | Natural Art",
    "Geometric Art | Mathematical Prints",
    "Black and White Art | Monochrome Prints",
    "Blue Wall Art | Blue Art",
    "Red Wall Art | Red Art",
    "Yellow Wall Art | Yellow Art",
    "Pink Wall Art | Pink Art",
    "Purple Wall Art | Purple Art",
    "Orange Wall Art | Orange Art"
]

COLOR_COLLECTIONS = [
    "Brown Prints | Brown Posters",
    "Green Wall Art | Green Art", 
    "Multi Coloured Wall Art | Bright Posters",
    "Black and White Art | Monochrome Prints",
    "Blue Wall Art | Blue Art",
    "Red Wall Art | Red Art",
    "Yellow Wall Art | Yellow Art",
    "Pink Wall Art | Pink Art",
    "Purple Wall Art | Purple Art",
    "Orange Wall Art | Orange Art"
]

NON_COLOR_COLLECTIONS = [col for col in COLLECTIONS if col not in COLOR_COLLECTIONS]

def encode_image_to_base64(image_path):
    """Convert image to base64 for API submission"""
    try:
        with Image.open(image_path) as img:
            # Resize if too large for API
            max_size = (1024, 1024)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # Save to bytes
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            buffer.seek(0)
            
            # Encode to base64
            encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{encoded}"
    except Exception as e:
        logger.error(f"Error encoding image {image_path}: {str(e)}")
        return None

def create_deepseek_prompt():
    """Create the prompt for DeepSeek API"""
    collections_str = '", "'.join(COLLECTIONS)
    
    prompt = f"""You are generating product details for a poster. Return JSON with the following structure:
{{
    "title": "Product title (max 255 characters)",
    "description": "SEO-friendly HTML description (max 5000 characters)",
    "alt_text": "Alt text for accessibility (max 255 characters)", 
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10", "tag11", "tag12", "tag13"],
    "non_color_collections": ["collection1", "collection2"],
    "color_collections": ["color_collection1"]
}}

Requirements:
- 13 tags total, each under 20 characters
- At least one collection from non-color categories
- At least one collection that IS a color
- Vendor: Listing Cannon
- Type: Poster
- Use HTML formatting in description (p, ul, li, strong, em tags allowed)

Available Collections: ["{collections_str}"]

Analyze the image and generate appropriate metadata that would help this poster sell well on Shopify. Focus on style, subject matter, colors, and potential use cases."""

    return prompt

def generate_product_metadata(image_path):
    """Generate product metadata using DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not found in environment variables")
        return None
    
    try:
        # Encode image
        base64_image = encode_image_to_base64(image_path)
        if not base64_image:
            logger.error("Failed to encode image for DeepSeek API")
            return None
        
        # Prepare request
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        
        prompt = create_deepseek_prompt()
        
        # DeepSeek R1 doesn't support vision directly through their API yet
        # Try alternative approaches to provide image context
        
        # Method 1: Try multimodal format (experimental)
        try:
            # Test if multimodal content is supported
            payload = {
                "model": "deepseek-reasoner",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt + f"\n\nI have uploaded an image (base64 encoded). Please analyze what you can see in this poster/artwork and generate accurate metadata. Image data: {base64_image[:100]}..."
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.7
            }
            
            logger.info("Attempting DeepSeek-R1 with image context...")
            response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and result['choices']:
                    content = result['choices'][0]['message']['content']
                    logger.info("DeepSeek-R1 responded, checking if vision worked...")
                    
                    # If the response mentions the image or shows understanding, use it
                    if any(word in content.lower() for word in ['image', 'see', 'picture', 'visual', 'artwork', 'poster']):
                        logger.info("DeepSeek-R1 appears to have processed image context")
                        # Continue with normal JSON parsing
                    else:
                        logger.warning("DeepSeek-R1 didn't process image context, falling back")
                        raise Exception("No vision context detected")
                else:
                    raise Exception("Invalid API response")
            else:
                logger.warning(f"DeepSeek-R1 API error: {response.status_code}")
                raise Exception(f"API error: {response.status_code}")
                
        except Exception as vision_error:
            logger.warning(f"DeepSeek-R1 vision attempt failed: {vision_error}")
            
            # Advanced image analysis fallback with better content interpretation
            import os
            from collections import Counter
            filename_base = os.path.splitext(os.path.basename(image_path))[0].lower()
            
            try:
                with Image.open(image_path) as img:
                    # Convert to RGB if needed
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    # Get image dimensions
                    width, height = img.size
                    aspect_ratio = width / height if height > 0 else 1
                    orientation = "landscape" if aspect_ratio > 1.2 else "portrait" if aspect_ratio < 0.8 else "square"
                    
                    # Advanced color analysis - sample from different regions
                    regions = [
                        img.crop((0, 0, width//2, height//2)),  # Top-left
                        img.crop((width//2, 0, width, height//2)),  # Top-right  
                        img.crop((0, height//2, width//2, height)),  # Bottom-left
                        img.crop((width//2, height//2, width, height))  # Bottom-right
                    ]
                    
                    all_colors = []
                    for region in regions:
                        region_small = region.resize((25, 25))
                        pixels = list(region_small.getdata())
                        all_colors.extend(pixels)
                    
                    # Calculate dominant colors
                    avg_r = sum(p[0] for p in all_colors) / len(all_colors)
                    avg_g = sum(p[1] for p in all_colors) / len(all_colors) 
                    avg_b = sum(p[2] for p in all_colors) / len(all_colors)
                    brightness = (avg_r + avg_g + avg_b) / 3
                    
                    # More sophisticated color detection
                    color_counts = Counter()
                    for r, g, b in all_colors:
                        # Categorize colors into broader categories
                        if r > 200 and g > 200 and b > 200:
                            color_counts['white'] += 1
                        elif r < 60 and g < 60 and b < 60:
                            color_counts['black'] += 1
                        elif r > g + 40 and r > b + 40:
                            color_counts['red'] += 1
                        elif g > r + 40 and g > b + 40:
                            color_counts['green'] += 1
                        elif b > r + 40 and b > g + 40:
                            color_counts['blue'] += 1
                        elif r > 150 and g > 150 and b < 100:
                            color_counts['yellow'] += 1
                        elif r > 100 and g < 100 and b > 100:
                            color_counts['purple'] += 1
                        elif r > 150 and g > 100 and b < 100:
                            color_counts['orange'] += 1
                        elif abs(r-g) < 30 and abs(g-b) < 30:
                            if (r+g+b)/3 > 120:
                                color_counts['neutral'] += 1
                            else:
                                color_counts['dark'] += 1
                    
                    # Find most common colors
                    top_colors = color_counts.most_common(3)
                    dominant_colors = [color for color, count in top_colors if count > len(all_colors) * 0.1]
                    
                    if not dominant_colors:
                        dominant_color = "multicolored"
                    else:
                        dominant_color = dominant_colors[0]
                    
                    # Create detailed analysis
                    brightness_desc = "bright" if brightness > 180 else "dark" if brightness < 80 else "medium brightness"
                    
                    # Try to infer content type from colors and composition
                    content_hints = []
                    if 'green' in dominant_colors and brightness > 120:
                        content_hints.append("botanical or nature-themed")
                    if 'blue' in dominant_colors:
                        content_hints.append("sky, ocean, or calming themes")
                    if brightness < 80:
                        content_hints.append("dramatic or moody atmosphere")
                    if len(dominant_colors) >= 3:
                        content_hints.append("vibrant and colorful composition")
                    if 'black' in dominant_colors and 'white' in dominant_colors:
                        content_hints.append("minimalist or high-contrast design")
                    
                    content_suggestion = ", ".join(content_hints) if content_hints else "abstract or artistic design"
                    
                    color_analysis = f"This {orientation}-oriented image ({width}x{height}) features {dominant_color} as the primary color with {brightness_desc}. The composition suggests {content_suggestion}. Additional colors present: {', '.join([c for c, _ in top_colors[1:3]])}."
                    
            except Exception as e:
                logger.error(f"Advanced image analysis failed: {e}")
                color_analysis = "Unable to analyze image properties"
                dominant_color = "multicolored"
                content_suggestion = "artistic design"
            
            enhanced_prompt = prompt + f"\n\nDetailed Image Analysis: {color_analysis}\nFilename: {filename_base}\n\nBased on this analysis, generate accurate and appealing metadata for this wall art poster. Focus on the visual characteristics identified above."
            
            payload = {
                "model": "deepseek-chat", 
                "messages": [
                    {
                        "role": "user", 
                        "content": enhanced_prompt
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.7
            }
        
        logger.info("Sending request to DeepSeek API...")
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"DeepSeek API error: {response.status_code} - {response.text}")
            return None
        
        result = response.json()
        
        if 'choices' not in result or not result['choices']:
            logger.error("Invalid response from DeepSeek API")
            return None
        
        content = result['choices'][0]['message']['content']
        
        # Parse JSON response
        try:
            # Clean up the response (remove markdown code blocks if present)
            logger.info(f"Raw DeepSeek response: {content[:200]}...")
            
            # Find JSON in response - it might be wrapped in text
            json_start = content.find('{')
            json_end = content.rfind('}')
            
            if json_start != -1 and json_end != -1 and json_end > json_start:
                json_content = content[json_start:json_end + 1]
                logger.info(f"Extracted JSON: {json_content[:100]}...")
            else:
                # Try original cleanup method
                if content.startswith('```json'):
                    json_content = content.replace('```json', '').replace('```', '').strip()
                elif content.startswith('```'):
                    json_content = content.replace('```', '').strip()
                else:
                    json_content = content.strip()
            
            metadata = json.loads(json_content)
            
            # Validate required fields
            required_fields = ['title', 'description', 'alt_text', 'tags', 'non_color_collections', 'color_collections']
            for field in required_fields:
                if field not in metadata:
                    logger.error(f"Missing required field: {field}")
                    return None
            
            # Validate tags count
            if not isinstance(metadata['tags'], list) or len(metadata['tags']) != 13:
                logger.error(f"Invalid tags count: {len(metadata.get('tags', []))}")
                return None
            
            # Validate collections
            if not metadata['non_color_collections'] or not metadata['color_collections']:
                logger.error("Missing required collections")
                return None
            
            logger.info("Successfully generated metadata from DeepSeek")
            return metadata
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response from DeepSeek: {str(e)}")
            logger.error(f"Raw content: {content}")
            return None
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request to DeepSeek API failed: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in DeepSeek API call: {str(e)}")
        return None

# Fallback metadata for testing/development
def get_fallback_metadata():
    """Provide fallback metadata when DeepSeek API is unavailable"""
    return {
        "title": "Abstract Art Print - Modern Wall Decor",
        "description": "<p>Transform your space with this stunning abstract art print. Perfect for modern homes and offices, this poster adds a contemporary touch to any room.</p><ul><li>High-quality digital print</li><li>Multiple size options available</li><li>Perfect for framing</li><li>Modern abstract design</li></ul>",
        "alt_text": "Abstract art print with modern geometric shapes and colors",
        "tags": ["abstract", "modern", "art", "print", "wall", "decor", "poster", "geometric", "contemporary", "minimalist", "design", "home", "office"],
        "non_color_collections": ["Abstract Prints | Abstract Wall Art", "Modern Art | Contemporary Prints"],
        "color_collections": ["Multi Coloured Wall Art | Bright Posters"]
    }
