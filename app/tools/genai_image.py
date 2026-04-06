import os
from typing import Optional, List, Dict
import PIL.Image
from google import genai
from google.genai import types

async def generate_image(
    prompt: str,
    base_image_path: Optional[str] = None,
    reference_images: Optional[List[Dict[str, str]]] = None,
    resolution: str = "1024x1024",
    aspect_ratio: str = "1:1",
    thinking_effort: str = "low",
    output_path: str = "output.png"
) -> dict:
    """
    Generates an image using Google GenAI models.
    
    Args:
        prompt: The text prompt for image generation.
        base_image_path: Optional path to a base image for Image-to-Image.
        reference_images: Optional list of dicts with 'path' for reference images.
        resolution: Target resolution (default '1024x1024').
        aspect_ratio: Target aspect ratio (default '1:1').
        thinking_effort: Thinking effort level (default 'low').
        output_path: Path to save the generated image.
    """
    client = genai.Client()
    
    contents = [prompt]
    
    if base_image_path and os.path.exists(base_image_path):
        base_img = PIL.Image.open(base_image_path)
        contents.append(base_img)
        
    if reference_images:
        for ref in reference_images:
            ref_path = ref.get("path")
            if ref_path and os.path.exists(ref_path):
                ref_img = PIL.Image.open(ref_path)
                contents.append(ref_img)
                
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect_ratio,
        )
    )
    
    try:
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=contents,
            config=config
        )
        
        for part in response.parts:
            if part.inline_data:
                generated_image = part.as_image()
                generated_image.save(output_path)
                return {"status": "success", "image_path": output_path, "resolution": resolution, "thinking_effort": thinking_effort}
                
        return {"status": "error", "message": "No image data found in response"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
