"""Cascade provider implementations.

One module per tier (V1 — 3-tier all-local)::

    tier1_paddleocr_local   — Tier 1, local PaddleOCR-VL
    tier2_qwen_7b_local     — Tier 2, local Qwen 2.5 VL 7B via Ollama
    tier3_qwen_32b_local    — Tier 3, local Qwen 2.5 VL 32B via Ollama

All providers conform to the ``CascadeProvider`` Protocol in ``_base``.
"""
