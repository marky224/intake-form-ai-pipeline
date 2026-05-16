"""Cascade extraction package.

Phase 4 (a+b) ships:
  - ``cascade.providers._base`` — Protocol + ProviderResult shared types
  - ``cascade.eval_cache`` — replay-cache machinery (default-on, ``EVAL_LIVE`` bypass)
  - ``cascade.providers.tier1_paddleocr_local`` — Tier 1 concrete provider

Out of scope this PR (lands in subsequent Phase 4 PRs):
  - Tier 2 Textract (PR (c))
  - Tier 3a Qwen local + dual-quant sanity test (PR (d))
  - Tier 3b Bedrock Sonnet + ``EXTRACTION_MODE`` dispatch (PR (e))

Out of scope this phase (Phase 5):
  - ``cascade.orchestrator`` — chains the four providers per confidence
    thresholds + retry-then-escalate failure handling
  - Two-stage router (Stage 1 vocab match + Stage 2 Bedrock Nova Lite)
  - ``HIPAA_MODE`` startup BAA assertion
"""
