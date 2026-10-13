"""Versioned local encoders for experimental product-image ranking."""

from __future__ import annotations

import hashlib
import html
import math
import string
from dataclasses import dataclass
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

# Marqo-FashionSigLIP vision tower (Apache-2.0), ONNX export in the model repository.
# Sizes and SHA-256 values come from the Hugging Face file listing at this revision.
FASHION_SIGLIP_REVISION = "c56244cc94f92419e8369fa71efdaf403b124ce8"
# Image and text embeddings are comparable only within one embedding space.
FASHION_SIGLIP_SPACE = "marqo-fashion-siglip"


class ImageEncoder(Protocol):
    version: str
    length: int
    cosine: bool
    space: str

    def encode(self, image: Image.Image) -> list[float]: ...

    def distance(self, query: list[float], indexed: list[float]) -> float: ...


class ColourLayoutEncoder:
    version = FEATURE_VERSION
    length = FEATURE_LENGTH
    cosine = False
    space = FEATURE_VERSION

    def encode(self, image: Image.Image) -> list[float]:
        return image_features(image)

    def distance(self, query: list[float], indexed: list[float]) -> float:
        return feature_distance(query, indexed)


@dataclass(frozen=True)
class OnnxArtifact:
    """One pinned ONNX file and the output that holds its image embedding."""

    version: str
    sha256: str
    size: int
    output: str
    first_token: bool  # True: the output is a token sequence; use token 0 (CLS)
    space: str
    length: int = 768


VIT = OnnxArtifact(
    version=VIT_FEATURE_VERSION,
    sha256=VIT_MODEL_SHA256,
    size=VIT_MODEL_BYTES,
    output="last_hidden_state",
    first_token=True,
    space="vit-base-patch32-224-in21k",
)
FASHION_SIGLIP = OnnxArtifact(
    version=(
        "marqo-fashion-siglip-vision-fp32-rgb224-bicubic-half-l2-v1:"
        "a7e773846b27a699c45ba7e3978514b7fca420662d7e69e3b9226982f09f4a13"
    ),
    sha256="a7e773846b27a699c45ba7e3978514b7fca420662d7e69e3b9226982f09f4a13",
    size=371_715_502,
    output="image_embeds",
    first_token=False,
    space=FASHION_SIGLIP_SPACE,
)
FASHION_SIGLIP_INT8 = OnnxArtifact(
    version=(
        "marqo-fashion-siglip-vision-int8-rgb224-bicubic-half-l2-v1:"
        "f1009cfce0eedd409f601e8351eedab72e8529641105eee6517821a9a634a2f4"
    ),
    sha256="f1009cfce0eedd409f601e8351eedab72e8529641105eee6517821a9a634a2f4",
    size=93_973_443,
    output="image_embeds",
    first_token=False,
    space=FASHION_SIGLIP_SPACE,
)
ARTIFACTS = {artifact.size: artifact for artifact in (VIT, FASHION_SIGLIP, FASHION_SIGLIP_INT8)}


class OnnxImageEncoder:
    """A pinned local ONNX model; embeddings are experimental, not identity proof.

    Both supported models expect RGB squashed to 224x224 (bicubic) and scaled to
    [-1, 1]; Marqo-FashionSigLIP publishes exactly this preprocessing.
    """

    cosine = True

    def __init__(self, path: Path):
        artifact = ARTIFACTS.get(path.stat().st_size)
        if artifact is None:
            raise ValueError("image model does not match a pinned image model")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != artifact.sha256:
            raise ValueError("image model does not match a pinned image model")
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise ValueError("install the image-model extra to use an ONNX image model") from exc
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
            or not any(output.name == artifact.output for output in outputs)
        ):
            raise ValueError("image model has an unexpected input or output contract")
        self.artifact = artifact
        self.version = artifact.version
        self.length = artifact.length
        self.space = artifact.space
        self._np = np
        self._session = session

    def encode(self, image: Image.Image) -> list[float]:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        resized = rgb.resize((224, 224), Image.Resampling.BICUBIC)
        pixels = self._np.asarray(resized, dtype=self._np.float32) / 127.5 - 1.0
        tensor = self._np.transpose(pixels, (2, 0, 1))[None, ...]
        output = self._session.run([self.artifact.output], {"pixel_values": tensor})[0]
        expected = 3 if self.artifact.first_token else 2
        if output.ndim != expected or output.shape[0] != 1 or output.shape[-1] != self.length:
            raise ValueError("image model returned an incompatible embedding")
        vector = (output[0, 0] if self.artifact.first_token else output[0]).astype(self._np.float64)
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
def _cached_model(path: str, size: int, modified_ns: int) -> OnnxImageEncoder:
    return OnnxImageEncoder(Path(path))


def get_encoder(model_path: Path | None = None) -> ImageEncoder:
    """Use the small built-in descriptor unless a pinned local ONNX file is selected."""
    if model_path is None:
        return COLOUR_LAYOUT
    try:
        path = model_path.resolve(strict=True)
        stat = path.stat()
    except OSError as exc:
        raise ValueError("image model file is unavailable") from exc
    if not path.is_file():
        raise ValueError("image model path must be a file")
    return _cached_model(str(path), stat.st_size, stat.st_mtime_ns)


# Marqo-FashionSigLIP text tower and tokenizer at FASHION_SIGLIP_REVISION. The text
# model files are LFS objects (listed digests); tokenizer.json is a plain git file,
# hashed after download from that revision.
TEXT_ARTIFACTS = {
    441_361_402: "c1d501b23bddf27ba828c037b0780e44fdf47ca4c0b925ef190ab5bcf7aaf6e6",  # fp32
    111_125_229: "f5a7995e029c6ee9346fa46857661c4a171d28b164fa7703b85a527d73adf170",  # int8
}
TOKENIZER_BYTES = 2_421_300
TOKENIZER_SHA256 = "5bc9b16db0e7adf5c9a2bd57f8f9c8e6ebdaabe75679cc956d007f78036d5b4a"
TEXT_CONTEXT_LENGTH = 64
_PUNCTUATION = str.maketrans("", "", string.punctuation)


def canonical_text(text: str) -> str:
    """open_clip's ``canonicalize`` clean, which this model was trained with."""
    text = html.unescape(html.unescape(text)).strip().replace("_", " ")
    return " ".join(text.translate(_PUNCTUATION).lower().split())


def _check_pinned(path: Path, size_to_digest: dict[int, str], label: str) -> None:
    digest = size_to_digest.get(path.stat().st_size)
    if digest is not None:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                return
    raise ValueError(f"{label} does not match a pinned Marqo-FashionSigLIP file")


class OnnxTextEncoder:
    """Embed a shopper's words into the same space as FashionSigLIP photo embeddings."""

    space = FASHION_SIGLIP_SPACE
    length = 768

    def __init__(self, model_path: Path, tokenizer_path: Path):
        _check_pinned(model_path, TEXT_ARTIFACTS, "text model")
        _check_pinned(tokenizer_path, {TOKENIZER_BYTES: TOKENIZER_SHA256}, "tokenizer")
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ValueError("install the image-model extra to use text-to-photo search") from exc
        try:
            ort.disable_telemetry_events()
            session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            # tokenizer.json pads to 64 tokens with </s> and appends </s> itself.
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except Exception as exc:
            raise ValueError("cannot load the pinned text model or tokenizer") from exc
        inputs = {item.name for item in session.get_inputs()}
        if (
            "input_ids" not in inputs
            or inputs - {"input_ids", "attention_mask"}
            or not any(output.name == "text_embeds" for output in session.get_outputs())
        ):
            raise ValueError("text model has an unexpected input or output contract")
        self._np = np
        self._session = session
        self._tokenizer = tokenizer
        self._inputs = inputs

    def encode(self, text: str) -> list[float]:
        ids = self._tokenizer.encode(canonical_text(text)).ids
        if len(ids) != TEXT_CONTEXT_LENGTH:
            raise ValueError("tokenizer did not produce 64 tokens")
        feed = {"input_ids": self._np.asarray([ids], dtype=self._np.int64)}
        if "attention_mask" in self._inputs:
            # open_clip, which produced these weights, never masks padding.
            feed["attention_mask"] = self._np.ones_like(feed["input_ids"])
        output = self._session.run(["text_embeds"], feed)[0]
        if output.shape != (1, self.length):
            raise ValueError("text model returned an incompatible embedding")
        vector = output[0].astype(self._np.float64)
        norm = float(self._np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("text model returned a nonfinite embedding")
        return (vector / norm).tolist()


@lru_cache(maxsize=2)
def _cached_text(model: str, tokenizer: str, stamp: tuple[int, ...]) -> OnnxTextEncoder:
    return OnnxTextEncoder(Path(model), Path(tokenizer))


def get_text_encoder(model_path: Path | None, tokenizer_path: Path | None) -> OnnxTextEncoder:
    if model_path is None or tokenizer_path is None:
        raise ValueError("text-to-photo search needs a text model and tokenizer path")
    try:
        model = model_path.resolve(strict=True)
        tokenizer = tokenizer_path.resolve(strict=True)
        stamp = tuple(
            value
            for item in (model.stat(), tokenizer.stat())
            for value in (item.st_size, item.st_mtime_ns)
        )
    except OSError as exc:
        raise ValueError("text model or tokenizer file is unavailable") from exc
    return _cached_text(str(model), str(tokenizer), stamp)
