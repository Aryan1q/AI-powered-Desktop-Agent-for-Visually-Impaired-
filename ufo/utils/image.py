"""Image helpers: sizing, diffing, blurring, region crops, encoding."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

DEFAULT_FONT_CANDIDATES = (
    "arial.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def load_font(size: int = 13):
    from PIL import ImageFont

    for name in DEFAULT_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def fit_to_edge(img: Image.Image, max_edge: int) -> Image.Image:
    """Downscale so the longest edge is at most `max_edge`. Never upscales."""
    if not max_edge or max(img.size) <= max_edge:
        return img
    scale = max_edge / max(img.size)
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    return img.resize(size, Image.LANCZOS)


def encode(img: Image.Image, fmt: str = "png", quality: int = 70) -> str:
    buf = io.BytesIO()
    fmt = (fmt or "png").lower()
    if fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    else:
        img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def save(img: Image.Image, path: str | os.PathLike) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p)
    return p


def diff_ratio(before: Image.Image, after: Image.Image, threshold: int = 12) -> float:
    """Fraction of pixels that changed — a cheap 'did the UI move?' signal."""
    if before is None or after is None:
        return 1.0
    a = before.convert("RGB")
    b = after.convert("RGB")
    if a.size != b.size:
        return 1.0
    delta = ImageChops.difference(a, b).convert("L")
    mask = delta.point(lambda p: 255 if p > threshold else 0)
    changed_px = sum(mask.histogram()[255:])
    total = a.width * a.height
    return (changed_px / total) if total else 1.0


def changed_regions(before: Image.Image, after: Image.Image,
                    threshold: int = 12, min_area: int = 400) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of areas that differ between two frames."""
    if before is None or after is None:
        return []
    a, b = before.convert("RGB"), after.convert("RGB")
    if a.size != b.size:
        return [(0, 0, b.width, b.height)]
    mask = ImageChops.difference(a, b).convert("L")
    mask = mask.point(lambda p: 255 if p > threshold else 0)
    boxes = mask.convert("1").getbbox()
    if not boxes:
        return []
    x0, y0, x1, y1 = boxes
    if (x1 - x0) * (y1 - y0) < min_area:
        return []
    return [(x0, y0, x1, y1)]


def crop(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = box
    return img.crop((max(0, left), max(0, top),
                     min(img.width, right), min(img.height, bottom)))


def pad_for_display(img: Image.Image, border: int = 12, color=(24, 24, 28)) -> Image.Image:
    return ImageOps.expand(img, border=border, fill=color)


def draw_label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
               font=None, fill=(255, 255, 255), bg=(200, 30, 30), pad: int = 2) -> tuple:
    font = font or load_font(12)
    x, y = xy
    try:
        w = draw.textlength(text, font=font)
    except Exception:
        w = len(text) * 7
    rect = (x, y, x + w + pad * 2, y + 14)
    draw.rectangle(rect, fill=bg)
    draw.text((x + pad, y), text, fill=fill, font=font)
    return rect


def draw_box(draw: ImageDraw.ImageDraw, box, color=(220, 40, 40), width: int = 2,
             label: str | None = None, font=None) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle((x0, y0, x1, y1), outline=color, width=width)
    if label:
        draw_label(draw, (x0, max(0, y0 - 15)), label, font=font, bg=color)


def blur_regions(img: Image.Image, regions: list[tuple[int, int, int, int]],
                 radius: int = 18) -> Image.Image:
    """Blur boxes in place — used to keep secrets out of uploaded screenshots."""
    if not regions:
        return img
    out = img.copy()
    for box in regions:
        region = crop(out, box)
        if region.width < 2 or region.height < 2:
            continue
        out.paste(region.filter(ImageFilter.GaussianBlur(radius)), box[:2])
    return out


def upscale(img: Image.Image, factor: float = 2.0) -> Image.Image:
    factor = max(1.0, min(4.0, factor))
    if factor == 1.0:
        return img
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def dominant_colors(img: Image.Image, count: int = 5) -> list[tuple[tuple, int]]:
    small = img.convert("RGB").resize((64, 64))
    quant = small.quantize(colors=count)
    palette = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    out = []
    for n, idx in counts:
        base = idx * 3
        out.append(((palette[base], palette[base + 1], palette[base + 2]), n))
    return out
