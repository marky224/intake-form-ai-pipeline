"""Late-interaction (ColBERT-style MaxSim) retrieval over the corpus.

ColQwen 2.5 emits a *multivector*: one L2-comparable embedding per image
patch token. The relevance of a candidate document to a query document is
the ColBERT MaxSim::

    score(q, d) = sum over q's tokens of  max over d's tokens of  <q_i, d_j>

with both sides L2-normalized so each token-pair term is a cosine in
``[-1, 1]``. This is exact — there is no approximation. ``sqlite-vec`` /
ANN indices exist to avoid an O(N) scan; at the V1 corpus scale (6 committed
CMS-1500, ≤500 in the deferred local corpus) the scan is microseconds in
NumPy and ANN would only *lose* recall, so brute force is the correct V1
choice (see ``rag/__init__``).

The retrieval surface this powers in V1: given a parked/just-corrected
document, surface the nearest *other* corpus documents. The README's
"corrected document becomes a few-shot example for Tier 2/3" framing is the
production mechanism; V1 ships and *measures/surfaces* the retrieval but
deliberately does **not** inject examples into the cascade providers'
prompts — that would change the providers' prompt and invalidate the
committed replay-cache fixtures + the frozen two-stage F1 artifact, which
Phase 8 must leave untouched. The honest V1 boundary is documented in
``docs/eval-methodology.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class Neighbor:
    """One retrieved document and its MaxSim score to the query."""

    doc_id: str
    score: float


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalization; zero rows stay zero (no divide-by-zero)."""
    import numpy as np

    arr = np.asarray(mat, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return arr / norms


def maxsim(query: np.ndarray, doc: np.ndarray) -> float:
    """ColBERT MaxSim between two multivector matrices (order-independent).

    Both matrices are L2-normalized per token, so the full token-pair
    similarity matrix is ``q_norm @ d_norm.T``; MaxSim sums the per-query
    maxima. Returns ``0.0`` if either side has no tokens.
    """

    q = _l2_normalize(query)
    d = _l2_normalize(doc)
    if q.shape[0] == 0 or d.shape[0] == 0:
        return 0.0
    sim = q @ d.T
    return float(sim.max(axis=1).sum())


def top_k(
    query: np.ndarray,
    corpus: dict[str, np.ndarray],
    k: int = 3,
) -> list[Neighbor]:
    """The ``k`` highest-MaxSim documents in ``corpus`` for ``query``.

    Ties break by ``doc_id`` (deterministic — the demo and tests must get a
    stable ordering on the tiny committed corpus). ``k <= 0`` returns ``[]``.
    """
    if k <= 0:
        return []
    scored = [Neighbor(doc_id, maxsim(query, mat)) for doc_id, mat in corpus.items()]
    scored.sort(key=lambda n: (-n.score, n.doc_id))
    return scored[:k]
