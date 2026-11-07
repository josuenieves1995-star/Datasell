"""Identify a clothing item from a photo of the front and a photo of the tag.

Uses the Claude API (set ANTHROPIC_API_KEY). Returns brand, item type, size,
material, era and an eBay search query that the market check then uses.
"""

from __future__ import annotations

import base64
import io
import json
import os

from .attributes import CATEGORIES

MODEL = os.environ.get("DATASELL_MODEL", "claude-opus-5-5")
MAX_EDGE = 1568  # larger images are downscaled by the API anyway

CATEGORY_NAMES = [c for c, _ in CATEGORIES] + ["Other"]

SYSTEM = """You are an expert used and vintage clothing reseller who identifies items from photos \
so they can be priced against eBay sold listings.

You get a photo of the front of the item and a photo of its tag (brand label, size tag or care \
tag). Read the tag carefully: brand, sub-label or line, size, fiber content, country of origin, \
RN/CA number and style number. Use tag design, fonts, union labels and "Made in" country to judge \
the era. Note anything that affects value: special lines or collaborations, vintage graphics, \
single stitch, deadstock, visible stains, holes, pilling or fading, and signs of a counterfeit.

The search_query is what a reseller would type into eBay to find sold comps for this exact item: \
3 to 7 words, brand first, then the model/line and the item type plus at most one defining \
detail (e.g. "Carhartt Detroit Jacket J97", "Patagonia Retro X Fleece Vest", "Vintage 90s Harley \
Davidson Tee"). Leave out size and common colors unless the color defines the item. The \
broad_query is just brand plus item type (e.g. "Carhartt Jacket"). If you can't read a brand, \
say so with an empty brand and describe the item in the queries.

Use an empty string for anything you can't tell. Don't guess a brand that isn't visible."""

SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "sub_brand_or_line": {"type": "string"},
        "item_type": {"type": "string", "description": "e.g. trucker jacket, crewneck sweater"},
        "category": {"type": "string", "enum": CATEGORY_NAMES},
        "department": {"type": "string", "enum": ["Men", "Women", "Unisex", "Kids", ""]},
        "size": {"type": "string"},
        "color": {"type": "string"},
        "material": {"type": "string"},
        "made_in": {"type": "string"},
        "rn_or_style_number": {"type": "string"},
        "era": {"type": "string", "description": "e.g. 1990s, Y2K, modern"},
        "vintage": {"type": "boolean"},
        "notable_features": {"type": "array", "items": {"type": "string"}},
        "condition_notes": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "search_query": {"type": "string"},
        "broad_query": {"type": "string"},
        "listing_title": {"type": "string", "description": "eBay title, max 80 characters"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["brand", "sub_brand_or_line", "item_type", "category", "department", "size",
                 "color", "material", "made_in", "rn_or_style_number", "era", "vintage",
                 "notable_features", "condition_notes", "red_flags", "search_query",
                 "broad_query", "listing_title", "confidence"],
    "additionalProperties": False,
}


class IdentifyError(Exception):
    pass


def api_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def prepare_image(data: bytes, max_edge: int = MAX_EDGE, quality: int = 85) -> bytes:
    """Rotate per EXIF, downscale and re-encode a phone photo as JPEG."""
    from PIL import Image, ImageOps

    try:
        try:  # iPhone HEIC photos, if pillow-heif is installed
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise IdentifyError("Couldn't read that photo. Please use a JPEG or PNG.") from e
    img.thumbnail((max_edge, max_edge))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def _image_block(jpeg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode()}}


def identify(front_jpeg: bytes, tag_jpeg: bytes | None, hint: str = "", client=None) -> dict:
    """Ask Claude what the item is. Images must already be prepared JPEGs."""
    import anthropic

    client = client or anthropic.Anthropic()
    content = [{"type": "text", "text": "Front of the item:"}, _image_block(front_jpeg)]
    if tag_jpeg:
        content += [{"type": "text", "text": "Tag:"}, _image_block(tag_jpeg)]
    text = "Identify this item for an eBay sold-comps search."
    if hint:
        text += f"\nThe seller's note (may help, may be wrong): {hint}"
    content.append({"type": "text", "text": text})

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            # If the model declines, retry automatically on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise IdentifyError("The Claude API key is missing or invalid (ANTHROPIC_API_KEY).") from e
    except anthropic.RateLimitError as e:
        raise IdentifyError("Claude API rate limit reached. Try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise IdentifyError(f"Claude API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise IdentifyError(f"Claude API error ({e.status_code}). Try again shortly.") from e
    except anthropic.APIConnectionError as e:
        raise IdentifyError("Couldn't reach the Claude API. Check the internet connection.") from e

    if response.stop_reason == "refusal":
        raise IdentifyError("Claude couldn't identify this photo. Try a clearer photo.")
    if response.stop_reason == "max_tokens":
        raise IdentifyError("Claude's answer was cut off. Please try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise IdentifyError("Claude returned an unreadable answer. Please try again.") from e
    if not result.get("search_query"):
        result["search_query"] = result.get("broad_query") or result.get("item_type") or ""
    return result
