"""Versioned colour and layout features for small local image-search experiments."""

from __future__ import annotations

from PIL import Image, ImageOps

FEATURE_VERSION = "rgb-layout-16x16-v1"
FEATURE_LENGTH = 792


def image_features(image: Image.Image) -> list[float]:
    """Encode coarse colour and layout; this is not a semantic clothing model."""
    rgb = ImageOps.exif_transpose(image).convert("RGB")
    small = rgb.resize((16, 16), Image.Resampling.BILINEAR)
    pixels = [channel / 255 for channel in small.tobytes()]
    histogram = []
    for channel in small.split():
        bins = channel.histogram()
        histogram.extend(sum(bins[index : index + 32]) / 256 for index in range(0, 256, 32))
    return pixels + histogram


def feature_distance(query: list[float], indexed: list[float]) -> float:
    if len(query) != FEATURE_LENGTH or len(indexed) != FEATURE_LENGTH:
        raise ValueError("incompatible image features")
    pixel_distance = sum(abs(a - b) for a, b in zip(query[:768], indexed[:768])) / 768
    colour_distance = sum(abs(a - b) for a, b in zip(query[768:], indexed[768:])) / 24
    return 0.7 * pixel_distance + 0.3 * colour_distance
