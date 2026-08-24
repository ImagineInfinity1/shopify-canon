"""
PhotoshopAPI-based PSD Smart Object mockup renderer.

This replaces the older psd-tools/Photopea fallback for the raw artwork workflow.
It performs a real Smart Object content replacement, writes a temporary PSD, and
exports a JPEG preview while preserving common Smart Object blend modes.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import numpy as np
import photoshopapi as psapi
from PIL import Image, ImageOps
from psd_tools import PSDImage


logger = logging.getLogger(__name__)

MAX_RENDER_INPUT_PIXELS = int(os.environ.get("SMART_MOCKUP_MAX_INPUT_PIXELS", "24000000"))
# Pillow's default decompression-bomb threshold rejects legitimate very large
# poster artwork before draft()/thumbnail() can reduce it. These files are
# selected locally by the user, and the renderer immediately downsizes them to
# MAX_RENDER_INPUT_PIXELS, so permit a larger source decode header here while
# retaining a configurable upper bound.
MAX_SOURCE_IMAGE_PIXELS = int(os.environ.get("SMART_MOCKUP_MAX_SOURCE_PIXELS", "500000000"))
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_IMAGE_PIXELS


def slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or "output"


def _iter_psapi_layers(layers, prefix: str = ""):
    for layer in layers:
        path = f"{prefix} / {layer.name}" if prefix else layer.name
        yield path, layer
        if "GroupLayer" in type(layer).__name__:
            yield from _iter_psapi_layers(layer.layers, path)


def list_smart_object_layers(psd_path: str | os.PathLike) -> list[str]:
    layered_file = psapi.LayeredFile.read(str(psd_path))
    return [
        path
        for path, layer in _iter_psapi_layers(layered_file.layers)
        if "SmartObjectLayer" in type(layer).__name__
    ]


def resolve_psapi_layer(layered_file, layer_path: str):
    parts = [part.strip() for part in layer_path.split("/") if part.strip()]
    layers = layered_file.layers
    layer = None
    for part in parts:
        matches = [candidate for candidate in layers if candidate.name == part]
        if not matches:
            raise ValueError(f"Layer path not found: {layer_path}")
        layer = matches[0]
        if part != parts[-1]:
            if "GroupLayer" not in type(layer).__name__:
                raise ValueError(f"Layer path enters non-group layer: {part}")
            layers = layer.layers
    return layer


def find_smart_object_layer_path(psd_path: str | os.PathLike, smart_layer_name: str = "1") -> str:
    smart_paths = list_smart_object_layers(psd_path)
    exact_name_matches = [
        path for path in smart_paths if path.split("/")[-1].strip() == smart_layer_name
    ]
    if exact_name_matches:
        return exact_name_matches[0]
    available = ", ".join(repr(path) for path in smart_paths) or "none"
    raise ValueError(
        f"PSD must contain a Smart Object layer named {smart_layer_name!r}. "
        f"Available Smart Object layers: {available}"
    )


def validate_psd_template(psd_path: str | os.PathLike, smart_layer_name: str = "1") -> dict:
    layer_path = find_smart_object_layer_path(psd_path, smart_layer_name)
    layered_file = psapi.LayeredFile.read(str(psd_path))
    layer = resolve_psapi_layer(layered_file, layer_path)
    return {
        "layer_path": layer_path,
        "original_width": layer.original_width(),
        "original_height": layer.original_height(),
        "rendered_width": layer.width,
        "rendered_height": layer.height,
    }


def prepare_artwork_for_smart_object(
    artwork_path: str | os.PathLike,
    width: int,
    height: int,
    output_path: str | os.PathLike,
    fit_mode: str = "stretch",
) -> str:
    with Image.open(artwork_path) as image:
        # Avoid decoding very large source artwork at full resolution. A 100MP
        # JPEG expands to hundreds of MB in memory before we even resize it.
        # draft() lets JPEG/TIFF loaders decode closer to the target size.
        image.draft("RGB", (max(width, 1), max(height, 1)))
        image = ImageOps.exif_transpose(image)
        source_pixels = image.width * image.height
        target_pixels = max(width, 1) * max(height, 1)
        max_needed_pixels = max(MAX_RENDER_INPUT_PIXELS, target_pixels * 4)
        if source_pixels > max_needed_pixels:
            scale = (max_needed_pixels / float(source_pixels)) ** 0.5
            draft_size = (
                max(width, int(image.width * scale)),
                max(height, int(image.height * scale)),
            )
            logger.info(
                "Downscaling oversized artwork before smart-object fit: %sx%s -> %sx%s",
                image.width,
                image.height,
                draft_size[0],
                draft_size[1],
            )
            image.thumbnail(draft_size, Image.Resampling.LANCZOS)
        has_alpha = image.mode in ("RGBA", "LA") or ("transparency" in image.info)
        image = image.convert("RGBA" if has_alpha else "RGB")
        if fit_mode == "cover":
            canvas = ImageOps.fit(
                image,
                (width, height),
                Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        elif fit_mode == "contain":
            fitted = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (width, height), "#ffffff")
            fitted = fitted.convert("RGBA")
            canvas.alpha_composite(
                fitted,
                ((width - fitted.width) // 2, (height - fitted.height) // 2),
            )
        else:
            # "stretch" (default): resize to the smart layer's exact pixel
            # dimensions so the ENTIRE artwork is always visible — never
            # cropped (cover) and never letterboxed (contain).
            canvas = image.resize((max(width, 1), max(height, 1)), Image.Resampling.LANCZOS)
        canvas.convert("RGB").save(output_path, quality=100)
    return str(output_path)


def hide_psdtools_layer_and_get_render_info(
    psd: PSDImage,
    layer_path: str,
) -> tuple[tuple[int, int, int, int], str, float]:
    parts = [part.strip() for part in layer_path.split("/") if part.strip()]

    def walk(layers, index: int):
        for layer in layers:
            if layer.name != parts[index]:
                continue
            if index == len(parts) - 1:
                blend_mode = str(layer.blend_mode).lower()
                opacity = float(layer.opacity) / 255 if layer.opacity > 1 else float(layer.opacity)
                layer.visible = False
                return tuple(map(int, layer.bbox)), blend_mode, opacity
            if not layer.is_group():
                raise ValueError(f"Layer path enters non-group layer: {layer.name}")
            return walk(layer, index + 1)
        return None

    result = walk(psd, 0)
    if result is None:
        raise ValueError(f"Could not find layer path in PSD preview renderer: {layer_path}")
    return result


def apply_blend_mode(base_crop: Image.Image, smart: Image.Image, blend_mode: str, opacity: float) -> Image.Image:
    base = np.asarray(base_crop.convert("RGBA")).astype("float32") / 255.0
    top = np.asarray(smart.convert("RGBA")).astype("float32") / 255.0

    base_rgb = base[:, :, :3]
    top_rgb = top[:, :, :3]
    alpha = top[:, :, 3:4] * max(0.0, min(opacity, 1.0))

    if "multiply" in blend_mode:
        blended = base_rgb * top_rgb
    elif "linearburn" in blend_mode or "linear_burn" in blend_mode or "linear burn" in blend_mode:
        blended = np.clip(base_rgb + top_rgb - 1.0, 0.0, 1.0)
    elif "screen" in blend_mode:
        blended = 1.0 - ((1.0 - base_rgb) * (1.0 - top_rgb))
    else:
        blended = top_rgb

    out_rgb = (blended * alpha) + (base_rgb * (1.0 - alpha))
    out_alpha = base[:, :, 3:4]
    out = np.dstack([out_rgb, out_alpha])
    return Image.fromarray((np.clip(out, 0.0, 1.0) * 255).astype("uint8"), "RGBA")


def render_preview_from_replaced_psd(
    source_psd_path: str | os.PathLike,
    replaced_psd_path: str | os.PathLike,
    layer_path: str,
    output_jpg_path: str | os.PathLike,
    quality: int = 95,
) -> str:
    psd = PSDImage.open(source_psd_path)
    bbox, blend_mode, opacity = hide_psdtools_layer_and_get_render_info(psd, layer_path)
    base = psd.composite().convert("RGBA")

    layered_file = psapi.LayeredFile.read(str(replaced_psd_path))
    layer = resolve_psapi_layer(layered_file, layer_path)
    data = layer.get_image_data()

    red = data[0]
    green = data[1]
    blue = data[2]
    alpha = data.get(-1, np.full_like(red, 255))
    smart = Image.fromarray(np.dstack([red, green, blue, alpha]).astype("uint8"), "RGBA")

    bbox_width = bbox[2] - bbox[0]
    bbox_height = bbox[3] - bbox[1]
    if smart.size != (bbox_width, bbox_height):
        smart = smart.resize((bbox_width, bbox_height), Image.Resampling.LANCZOS)

    base_crop = base.crop(bbox)
    blended = apply_blend_mode(base_crop, smart, blend_mode, opacity)
    base.alpha_composite(blended, (bbox[0], bbox[1]))
    base.convert("RGB").save(output_jpg_path, "JPEG", quality=quality, optimize=True)
    return str(output_jpg_path)


def render_artwork_with_frame(
    artwork_path: str | os.PathLike,
    psd_path: str | os.PathLike,
    output_jpg_path: str | os.PathLike,
    temp_dir: str | os.PathLike,
    smart_layer_name: str = "1",
    fit_mode: str = "stretch",
) -> str:
    os.makedirs(temp_dir, exist_ok=True)
    psd_path = Path(psd_path)
    artwork_path = Path(artwork_path)
    output_jpg_path = Path(output_jpg_path)

    layer_path = find_smart_object_layer_path(psd_path, smart_layer_name)
    layered_file = psapi.LayeredFile.read(str(psd_path))
    layer = resolve_psapi_layer(layered_file, layer_path)
    if "SmartObjectLayer" not in type(layer).__name__:
        raise TypeError(f"{layer_path!r} in {psd_path.name} is not a Smart Object layer")

    prepared_path = Path(temp_dir) / f"{slug(artwork_path.stem)}__{layer.original_width()}x{layer.original_height()}.jpg"
    temp_psd_path = Path(temp_dir) / f"{slug(psd_path.stem)}__{slug(artwork_path.stem)}.psd"

    prepare_artwork_for_smart_object(
        artwork_path,
        layer.original_width(),
        layer.original_height(),
        prepared_path,
        fit_mode=fit_mode,
    )
    layer.replace(str(prepared_path), False)
    layered_file.write(str(temp_psd_path))

    try:
        output_jpg_path.parent.mkdir(parents=True, exist_ok=True)
        return render_preview_from_replaced_psd(
            psd_path,
            temp_psd_path,
            layer_path,
            output_jpg_path,
        )
    finally:
        for path in (prepared_path, temp_psd_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                logger.warning("Could not remove temp mockup file: %s", path)
