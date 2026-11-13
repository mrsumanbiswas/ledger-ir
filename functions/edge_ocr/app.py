"""Placeholder for the Phase-4 arm64 ONNX OCR container Lambda.

The pipeline's Choice state routes precomputed ``*_tokens.json`` keys straight
to the compiler, so this function only runs for raw image keys — which the
demo does not send yet. It fails loudly rather than pretending to read images.
"""
from __future__ import annotations


def lambda_handler(event, _context):
    raise NotImplementedError(
        f"edge_ocr: real OCR (rapidocr-onnxruntime container) lands in Phase 4; "
        f"got image key {event.get('key')!r} — supply a precomputed "
        f"*_tokens.json key instead")
