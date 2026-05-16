"""Cascade extraction package.

V1 is a 3-tier all-local cascade — no cloud providers and no
``EXTRACTION_MODE`` failover; every tier runs on the local GPUs.

Shipped:
  - ``cascade.providers._base`` — ``CascadeProvider`` Protocol + ``ProviderResult``
  - ``cascade.eval_cache`` — replay-cache machinery (default-on, ``EVAL_LIVE`` bypass)
  - ``cascade.providers.tier1_paddleocr_local`` — Tier 1, local PaddleOCR-VL

Pending Phase 4 PRs:
  - ``cascade.providers.tier2_qwen_7b_local`` — Tier 2, local Qwen 2.5 VL 7B
  - ``cascade.providers.tier3_qwen_32b_local`` — Tier 3, local Qwen 2.5 VL 32B
    (+ Q8_0-vs-Q6_K dual-quant sanity test)

Phase 5 (not Phase 4):
  - ``cascade.orchestrator`` — chains the three providers per confidence
    thresholds + retry-then-escalate failure handling
  - Two-stage router (Stage 1 vocab match + Stage 2 local Qwen 7B fallback)
  - ``HIPAA_MODE`` no-op (no V1 provider-routing surface)
"""
