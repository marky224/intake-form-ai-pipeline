"""Cascade provider implementations.

One module per tier::

    tier1_paddleocr_local   — Tier 1, local PaddleOCR-VL (this PR)
    tier2_textract          — Tier 2, AWS Textract Queries (PR (c))
    tier3a_qwen_local       — Tier 3a, local Qwen 2.5 VL 32B (PR (d))
    tier3b_claude_bedrock   — Tier 3b, Bedrock Claude Sonnet 4.6 (PR (e))

All providers conform to the ``CascadeProvider`` Protocol in ``_base``.
"""
