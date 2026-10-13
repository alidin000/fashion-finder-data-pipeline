"""Versioned local encoders for experimental product-image ranking."""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageOps

from .visual_features import FEATURE_LENGTH, FEATURE_VERSION, feature_distance, image_features

VIT_MODEL_SHA256 = "c9ebea40814509fa2ff8d907408edfaa72ee13213e6ebdcbda0fcbe2ef7a5c92"
VIT_MODEL_BYTES = 88_631_709
VIT_FEATURE_VERSION = (
    f"vit-base-patch32-224-in21k-int8-rgb224-bicubic-half-cls-l2-v1:{VIT_MODEL_SHA256}"
)
VIT_FEATURE_LENGTH = 768


class ImageEncoder(Protocol):
    version: str
    length: int

    def encode(self, image: Image.Image) -> list[float]: ...

    def distance(self, query: list[float], indexed: list[float]) -> float: ...


class ColourLayoutEncoder:
    version = FEATURE_VERSION
    length = FEATURE_LENGTH

    def encode(self, image: Image.Image) -> list[float]:
        return image_features(image)

    def distance(self, query: list[float], indexed: list[float]) -> float:
        return feature_distance(query, indexed)


class ViTEncoder:
    """Pinned ImageNet-21k ViT; CLS embeddings are experimental, not identity proof."""

    version = VIT_FEATURE_VERSION
    length = VIT_FEATURE_LENGTH

    def __init__(self, path: Path):
        if path.stat().st_size != VIT_MODEL_BYTES:
            raise ValueError("image model does not match the pinned ViT artifact")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != VIT_MODEL_SHA256:
            raise ValueError("image model does not match the pinned ViT artifact")
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise ValueError("install the image-model extra to use the ViT encoder") from exc
        try:
            ort.disable_telemetry_events()
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise ValueError("cannot load the pinned image model") from exc
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if (
            len(inputs) != 1
            or inputs[0].name != "pixel_values"
            or inputs[0].type != "tensor(float)"
            or not any(output.name == "last_hidden_state" for output in outputs)
        ):
            raise ValueError("image model has an unexpected input or output contract")
        self._np = np
        self._session = session

    def encode(self, image: Image.Image) -> list[float]:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        resized = rgb.resize((224, 224), Image.Resampling.BICUBIC)
        pixels = self._np.asarray(resized, dtype=self._np.float32) / 127.5 - 1.0
        tensor = self._np.transpose(pixels, (2, 0, 1))[None, ...]
        output = self._session.run(["last_hidden_state"], {"pixel_values": tensor})[0]
        if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] != self.length:
            raise ValueError("image model returned an incompatible embedding")
        vector = output[0, 0].astype(self._np.float64)
        norm = float(self._np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("image model returned a nonfinite embedding")
        return (vector / norm).tolist()

    def distance(self, query: list[float], indexed: list[float]) -> float:
        if len(query) != self.length or len(indexed) != self.length:
            raise ValueError("incompatible image embeddings")
        return 1.0 - sum(a * b for a, b in zip(query, indexed))


COLOUR_LAYOUT = ColourLayoutEncoder()


@lru_cache(maxsize=2)
def _cached_vit(path: str, size: int, modified_ns: int) -> ViTEncoder:
    return ViTEncoder(Path(path))


def get_encoder(model_path: Path | None = None) -> ImageEncoder:
    """Use the small built-in descriptor unless a pinned local ViT file is selected."""
    if model_path is None:
        return COLOUR_LAYOUT
    try:
        path = model_path.resolve(strict=True)
        stat = path.stat()
    except OSError as exc:
        raise ValueError("image model file is unavailable") from exc
    if not path.is_file():
        raise ValueError("image model path must be a file")
    return _cached_vit(str(path), stat.st_size, stat.st_mtime_ns)
