"""GenAI image generation tool — text-to-image, image-to-image, and reference-guided editing.

Uses Gemini 2.5 Flash (image model) via Google GenAI SDK.
Returns a file_path so the transport layer can deliver the image to chat.
"""

import logging
import os
import uuid
from typing import Literal, Optional, List, Dict

import PIL.Image
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    ImageConfig,
    ThinkingConfig,
    ThinkingLevel,
)

logger = logging.getLogger(__name__)

_IMAGES_DIR = os.path.abspath("./tmp/images")


async def generate_image(
    prompt: str,
    base_image_path: Optional[str] = None,
    reference_images: Optional[List[Dict[str, str]]] = None,
    aspect_ratio: Literal[
        "1:1", "1:4", "1:8", "2:3", "3:2", "3:4",
        "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
    ] = "1:1",
    resolution: Literal["512", "1K", "2K", "4K"] = "1K",
    thinking: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"] = "MINIMAL",
) -> dict:
    """Generate or edit an image using AI (Gemini).

    Supports three modes:
    - **Text-to-image**: provide only a prompt.
    - **Image-to-image**: provide a prompt + base_image_path to edit an existing image.
    - **Reference-guided**: provide a prompt + base_image_path + reference_images
      to guide the edit with style/content references.

    Args:
        prompt: Detailed description of what to generate or how to edit the image.
        base_image_path: Path to the image to edit (from a user upload). Omit for text-to-image.
        reference_images: List of dicts, each with 'path' (file path) and optional
            'description' (what this reference is for, e.g. "target color palette").
        aspect_ratio: Output aspect ratio. Default '1:1'.
        resolution: Output image size. Default '1K'.
        thinking: Model thinking effort — higher = better quality but slower. Default 'MINIMAL'.

    Returns:
        dict with 'status' and 'file_path' on success, or 'status' and 'message' on error.
    """
    os.makedirs(_IMAGES_DIR, exist_ok=True)

    client = genai.Client()
    contents = [prompt]

    # Track opened images so we can close them after the API call
    opened_images = []

    # Base image for image-to-image editing
    if base_image_path and os.path.exists(base_image_path):
        img = PIL.Image.open(base_image_path)
        img.load()  # force pixel data into memory
        opened_images.append(img)
        contents.append(img)

    # Reference images with optional descriptions (like the Streamlit AI Photoshop)
    if reference_images:
        for ref in reference_images:
            ref_path = ref.get("path")
            if not ref_path or not os.path.exists(ref_path):
                continue
            description = ref.get("description", "")
            if description:
                contents.append(description)
            img = PIL.Image.open(ref_path)
            img.load()
            opened_images.append(img)
            contents.append(img)

    config = GenerateContentConfig(
        thinking_config=ThinkingConfig(
            thinking_level=ThinkingLevel(value=thinking),
        ),
        image_config=ImageConfig(
            aspect_ratio=aspect_ratio,
            image_size=resolution,
        ),
        response_modalities=["IMAGE", "TEXT"],
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=contents,
            config=config,
        )

        if not response.candidates or not response.candidates[0].content or not response.candidates[0].content.parts:
            return {"status": "error", "message": "No image data in response — the model may have refused the prompt."}

        # Extract image and optional text explanation
        result_text = ""
        for part in response.candidates[0].content.parts:
            if part.text:
                result_text = part.text
            if part.inline_data:
                output_path = os.path.join(_IMAGES_DIR, f"img_{uuid.uuid4().hex[:8]}.png")
                generated_image = part.as_image()
                generated_image.save(output_path)
                logger.info("Image saved: %s (%d bytes)", output_path, os.path.getsize(output_path))
                result = {"status": "success", "file_path": output_path}
                if result_text:
                    result["description"] = result_text
                return result

        # Model returned text only (e.g. refusal or clarification)
        if result_text:
            return {"status": "error", "message": result_text}

        return {"status": "error", "message": "Response contained no image data."}
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        for img in opened_images:
            img.close()
