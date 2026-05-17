"""Phase 6 eval harness (V1, all-local, $0).

Measures the V1 cascade end-to-end against partitioned ground truth and
produces the three portfolio metrics — F1 (headline), latency p50/p99, and
cost-per-document (always $0 in V1) — plus the F1-over-time chart driven by
the progressive alias-table partition.

Design decisions (surfaced to + approved by Mark at Phase 6 entry):

- **Harness-owned ``eval_results`` table.** The harness writes its own table
  into the same ``data/v1.db`` file the orchestrator uses, joined to
  ``runs``/``field_attempts`` by ``doc_id``. ``cascade.store`` deliberately
  keeps ``ground_truth``/``batch_id`` out of the orchestrator's write path;
  this package owns truth + batch (see ``evals.store``).
- **Field-type-aware ground truth.** The CMS-1500 renderer sidecars store
  *box-packed rendered text* (e.g. box 3 packs DOB **and** sex), not clean
  per-schema-field values. ``evals.ground_truth`` projects sidecar boxes
  onto schema fields and canonicalizes per inner type (dates→ISO,
  free-text→casefold+whitespace-collapse, IDs→exact) so F1 reflects
  extraction quality, not the renderer's box layout.
- **Reuse the existing replay cache.** The eval corpus is the already-wired
  ``tests/fixtures/eval-cache/`` (6 CMS-1500 across all tiers + router),
  not a parallel ``evals/fixtures/`` store. ``evals/`` holds only the
  manifest, the thin fixtures manifest, results, the cost table, and the
  chart.

DocILE is local-only (CC-BY-NC-ND): no DocILE-derived fixtures are committed
and the DocILE eval path is gated on local file presence exactly like the
providers. CI scores the 6 committed CMS-1500 for $0.
"""
