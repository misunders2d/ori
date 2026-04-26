"""GenAI image generation tool — text-to-image, image-to-image, and reference-guided editing.

Uses Gemini Flash (image model) via Google GenAI SDK.
Returns a file_path so the transport layer can deliver the image to chat.
"""

import json
import logging
import os
import uuid
from typing import Literal

import PIL.Image
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    ImageConfig,
    ThinkingConfig,
    ThinkingLevel,
)

logger = logging.getLogger(__name__)

_ENHANCE_PROMPT_INSTRUCTIONS = """\
Generate a JSON structured prompt that includes ALL improved image details.
Everything must be described, including but not limited to: colors, shapes, materials, \
facial expressions, mood, lighting, interior, style, textures, spatial composition, \
camera angle, depth of field, and environmental props.

Refer to this structure example:
{
  "composition_and_spatial_geometry": {
    "camera_angle": "...",
    "focal_point": "...",
    "spatial_depth_planes": {"foreground": "...", "midground": "...", "background": "..."},
    "shapes": "..."
  },
  "product_details_and_materials": {
    "primary_item": {"label": "...", "material": "...", "color": "...", "texture": "...", "physics": "..."},
    "layering_materials": [{"item": "...", "material": "...", "color": "...", "shape": "..."}]
  },
  "lighting_and_chromatic_atmosphere": {
    "primary_light": {"type": "...", "color_temp": "...", "direction": "...", "effect": "..."},
    "secondary_light": {"type": "...", "color": "...", "placement": "..."},
    "global_illumination": "..."
  },
  "environmental_props_and_surfaces": {
    "...": {"material": "...", "color": "...", "shape": "..."}
  },
  "technical_rendering_intent": {
    "engine_style": "...",
    "post_processing": "..."
  }
}

Adapt the structure to fit the actual image content. Be extremely specific with \
HEX colors, material descriptions, and spatial coordinates. \
Do not mention resolution or aspect ratio."""

_IMAGES_DIR = os.path.abspath("./tmp/images")


async def generate_image(
    prompt: str,
    base_image_path: str | None = None,
    reference_images: list[dict[str, str]] | None = None,
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
            model="gemini-3.1-flash-image-preview",
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


async def enhance_image_prompt(
    prompt: str,
    base_image_path: str | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict:
    """Analyze an image and generate a highly detailed structured prompt for precise editing.

    Call this BEFORE generate_image when the user wants to change specific parts of an
    image (not the whole thing). It returns a detailed JSON description of the entire image,
    which you should then modify according to the user's request and pass to generate_image.

    Workflow: enhance_image_prompt → modify the returned prompt → generate_image

    Args:
        prompt: What the user wants to change (e.g. "change the bedsheet color to green").
        base_image_path: Path to the image to analyze (from a user upload).
        reference_images: Optional list of dicts with 'path' and 'description' for references.

    Returns:
        dict with 'status' and 'enhanced_prompt' (the detailed JSON prompt string) on success.
    """
    client = genai.Client()
    contents = [prompt + "\n\n" + _ENHANCE_PROMPT_INSTRUCTIONS]

    opened_images = []

    if base_image_path and os.path.exists(base_image_path):
        img = PIL.Image.open(base_image_path)
        img.load()
        opened_images.append(img)
        contents.append(img)

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

    config = GenerateContentConfig(response_mime_type="application/json")

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-image-preview",
            contents=contents,
            config=config,
        )

        if not response.candidates or not response.candidates[0].content or not response.candidates[0].content.parts:
            return {"status": "error", "message": "No response from model."}

        texts = [
            part.text for part in response.candidates[0].content.parts if part.text
        ]
        if texts:
            enhanced = "".join(texts)
            # Validate it's actual JSON
            try:
                json.loads(enhanced)
            except json.JSONDecodeError:
                pass  # still usable as a detailed text prompt
            return {"status": "success", "enhanced_prompt": enhanced}

        return {"status": "error", "message": "Model returned no text."}
    except Exception as e:
        logger.error("Prompt enhancement failed: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        for img in opened_images:
            img.close()
