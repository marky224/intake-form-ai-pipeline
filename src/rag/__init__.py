"""Phase 8 (V1) RAG layer — ColQwen 2.5 retrieval + correction feedback loop.

Four cooperating modules, all module-level functions, ``extra="forbid"``
Pydantic at boundaries, heavy deps lazy-imported (mirrors the cascade
providers' structure so CI never needs a GPU):

- :mod:`rag.aliases` — the live correction-driven alias overlay. The
  committed ``alias_table_seed.json`` (v1.0.0, frozen — it drives the
  portfolio F1 chart) is **never** mutated. Reviewer corrections append to a
  gitignored ``data/corrections_aliases.json`` overlay that the router +
  Tier 1 alias loaders union in. The progressive-partition F1 sweep
  explicitly *suppresses* the overlay so the frozen chart stays honest.
- :mod:`rag.embed` — ColQwen 2.5 (Qwen2.5-VL-3B late-interaction
  multivector embeddings). FP16, EVAL_LIVE-gated, ``.npy`` replay cache.
- :mod:`rag.store` — multivector embeddings packed into the *reserved*
  ``embeddings.vector BLOB`` column (Phase 5 left it for exactly this).
  Reads/writes only — no schema migration.
- :mod:`rag.retrieve` — brute-force late-interaction MaxSim over the
  corpus. Exact at the V1 corpus scale; no ANN index needed.
- :mod:`rag.corrections` — the feedback loop: a reviewer correction on a
  parked (``review_queue``) document writes a ``corrections`` row, appends
  the newly-recognized phrasing to the alias overlay, and refreshes the
  retrieval corpus.

Storage decision (deviates from a literal reading of
``architecture-locked.md`` "Embeddings: ... sqlite-vec"): ColQwen is a
*late-interaction multivector* model — one matrix of per-patch token
vectors per document, scored by MaxSim. ``sqlite-vec``'s ``vec0`` is
single-vector KNN with no native late interaction, so honoring the literal
lock would force lossy mean-pooling that defeats the locked model choice
and add a dependency that buys nothing at a 6-500 document corpus where
brute-force MaxSim is exact and instant. We keep the reserved BLOB column
shape exactly (genuinely reads/writes-only, no migration) and do MaxSim in
NumPy. The deviation is documented in ``architecture-locked.md`` and
``docs/eval-methodology.md`` rather than silently followed as a checkbox.
"""
