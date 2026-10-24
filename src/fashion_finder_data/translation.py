"""Optional offline Hungarian-to-English translation for words the dictionary misses.

The model is Helsinki-NLP ``opus-mt-hu-en`` (Apache-2.0), as the INT8 ONNX export
in ``Xenova/opus-mt-hu-en``. It runs locally with greedy decoding; nothing is sent
to a translation service. It is slower than the dictionary and sometimes invents
words, so look search uses it only as a fallback.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

# Files from https://huggingface.co/Xenova/opus-mt-hu-en at this revision. The ONNX
# files are LFS objects with listed digests; tokenizer.json was hashed after download.
OPUS_MT_REVISION = "c9e8a7617fb4cb4fca3785c5bb22cfe457cde00b"
OPUS_MT_FILES = {
    "encoder_model_quantized.onnx": (
        51_630_494,
        "f0dce998a12ce35d4880d5f6ac153807be88bb50691244e512e9605b2cabd6fe",
    ),
    "decoder_model_quantized.onnx": (
        58_562_938,
        "bec30ea95c7331e4fb33ee61a9812d2a4a234fbcd116a21a86b114aa8b7fb2c5",
    ),
    "tokenizer.json": (
        6_133_233,
        "05467c7d69c3755d66e02b34d58d9643d0b2e7c28d3e761ba6036d7c855326c5",
    ),
}
OPUS_MT_VERSION = f"opus-mt-hu-en-int8-greedy-v1:{OPUS_MT_REVISION}"
EOS_ID = 0  # "</s>"
PAD_ID = 62521  # also the decoder start token; never generated
MAX_SOURCE_TOKENS = 64
MAX_NEW_TOKENS = 48


def _check(path: Path, size: int, digest: str) -> None:
    try:
        matches = path.stat().st_size == size
        if matches:
            with path.open("rb") as stream:
                matches = hashlib.file_digest(stream, "sha256").hexdigest() == digest
    except OSError as exc:
        raise ValueError(f"translation model file is unavailable: {path.name}") from exc
    if not matches:
        raise ValueError(f"{path.name} does not match the pinned opus-mt-hu-en file")


class OnnxTranslator:
    """Hungarian to English with a pinned local Marian model and greedy decoding."""

    version = OPUS_MT_VERSION

    def __init__(self, directory: Path):
        for name, (size, digest) in OPUS_MT_FILES.items():
            _check(directory / name, size, digest)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ValueError("install the image-model extra to translate queries") from exc
        spec = json.loads((directory / "tokenizer.json").read_text(encoding="utf-8"))
        # The export's SentencePiece normalizer has no character map, which
        # `tokenizers` cannot load; Marian's own normalization is NFKC-based.
        spec["normalizer"] = {"type": "NFKC"}
        try:
            ort.disable_telemetry_events()
            options = ort.SessionOptions()
            self._encoder = ort.InferenceSession(
                str(directory / "encoder_model_quantized.onnx"),
                options,
                providers=["CPUExecutionProvider"],
            )
            self._decoder = ort.InferenceSession(
                str(directory / "decoder_model_quantized.onnx"),
                options,
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = Tokenizer.from_str(json.dumps(spec))
        except Exception as exc:
            raise ValueError("cannot load the pinned translation model") from exc
        if {item.name for item in self._decoder.get_inputs()} != {
            "input_ids",
            "encoder_hidden_states",
            "encoder_attention_mask",
        }:
            raise ValueError("translation model has an unexpected input contract")
        self._np = np
        self.translate = lru_cache(maxsize=2048)(self._translate)

    def _translate(self, text: str) -> str:
        np = self._np
        ids = self._tokenizer.encode(text).ids
        if len(ids) > MAX_SOURCE_TOKENS:
            ids = [*ids[: MAX_SOURCE_TOKENS - 1], EOS_ID]
        source = np.asarray([ids], dtype=np.int64)
        mask = np.ones_like(source)
        hidden = self._encoder.run(
            ["last_hidden_state"], {"input_ids": source, "attention_mask": mask}
        )[0]
        output = [PAD_ID]
        for _ in range(min(MAX_NEW_TOKENS, 2 * len(ids) + 4)):
            logits = self._decoder.run(
                ["logits"],
                {
                    "input_ids": np.asarray([output], dtype=np.int64),
                    "encoder_hidden_states": hidden,
                    "encoder_attention_mask": mask,
                },
            )[0][0, -1]
            logits[PAD_ID] = -np.inf
            token = int(logits.argmax())
            if token == EOS_ID:
                break
            output.append(token)
        return self._tokenizer.decode(output[1:]).strip()


@lru_cache(maxsize=1)
def _cached(directory: str, stamp: tuple[int, ...]) -> OnnxTranslator:
    return OnnxTranslator(Path(directory))


def get_translator(directory: Path | str | None) -> OnnxTranslator:
    """The pinned translator in ``directory``; a string path is accepted for JSON options."""
    if directory is None:
        raise ValueError("machine translation needs a translation model directory")
    try:
        resolved = Path(directory).resolve(strict=True)
        stamp = tuple(
            value
            for name in OPUS_MT_FILES
            for value in ((resolved / name).stat().st_size, (resolved / name).stat().st_mtime_ns)
        )
    except OSError as exc:
        raise ValueError("translation model directory is unavailable") from exc
    return _cached(str(resolved), stamp)
