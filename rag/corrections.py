"""The reviewer-correction feedback loop — primitives.

A correction on a parked (``review_queue``) document does three things,
all here as small composable functions (the demo / ``just correct`` recipe
orchestrate them over the committed corpus — same testable-core vs. view
split as ``demo/data.py`` vs. ``demo/app.py``):

1. **Log it.** A row in the reserved ``corrections`` table (Phase 5
   created it; this is the write Phase 5 deferred — no migration).
2. **Learn the phrasing.** *If* the reviewer supplies the on-form label
   the cascade failed to recognize, it is appended to the live alias
   overlay (``rag.aliases``) and both alias caches are invalidated, so the
   *next* extraction of a similarly-labeled form resolves that label at
   Tier 1 with no escalation. This is the live half of the exact mechanism
   the progressive-partition F1 sweep simulates offline — same alias path,
   different (live vs. seeded) source. It is **not** a second, hidden
   self-improvement signal: when no unrecognized phrasing is supplied
   (the seeded-replay default) nothing is learned and the correction is
   purely logged + re-embedded. The portfolio chart stays the seed-only
   offline analogue; this is honest about which is which.
3. **Refresh retrieval.** Re-embed the corrected document so it joins the
   ColQwen corpus and can surface as a nearest neighbor for later
   documents. Degrades cleanly (returns ``False``) when no GPU / no cached
   embedding is available — CI never has one.

``bootstrap_alias_table`` formalizes the "alias-table population from seed"
phases.md bullet: the committed ``alias_table_seed.json`` *is* the loop's
bootstrap corpus of already-known corrections (canonical-priority order);
the overlay is its live extension. There is no separate table to populate —
making that explicit is the deliverable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from rag import store as rag_store
from rag.aliases import append_correction_alias, invalidate_alias_caches
from rag.embed import EmbeddingUnavailable, embed_image

if TYPE_CHECKING:
    from cascade.router import ALIAS_TABLE_PATH  # noqa: F401  (doc reference)

#: Routed cascade vertical → the seed ``vertical`` the alias loaders read
#: for that form. ``HealthcareIntakeForm`` draws from ``base`` + ``healthcare``
#: and a healthcare-distinctive label should also feed the router vocab, so a
#: healthcare correction lands in ``healthcare``. ``BusinessDocumentForm``
#: draws **only** ``base`` (DocILE's KILE taxonomy isn't in the seed), and
#: ``base`` is excluded from the router's healthcare-distinctive vocab, so a
#: business correction lands in ``base`` — visible to the business alias map,
#: inert for the router. (Healthcare/business are the only V1 verticals.)
_SEED_VERTICAL = {"healthcare": "healthcare", "business": "base"}


class CorrectionOutcome(BaseModel):
    """The result of recording one reviewer correction."""

    model_config = ConfigDict(extra="forbid")

    correction_id: int
    doc_id: str
    field_name: str
    original_value: str | None
    corrected_value: str
    tier_that_produced_original: str | None
    #: True iff a previously-unrecognized phrasing was added to the overlay.
    alias_learned: bool
    learned_alias: str | None


def seed_vertical_for(routed_vertical: str) -> str:
    """Map a routed cascade vertical to its seed ``vertical`` bucket."""
    return _SEED_VERTICAL.get(routed_vertical, "base")


def humanize_field_label(field_name: str) -> str:
    """``patient_name`` → ``Patient Name`` (a plausible default form label).

    Used by the demo so a reviewer who doesn't hand-type the on-form label
    still produces *a* candidate phrasing. It is deduped against the seed +
    overlay before being learned, so when the humanized label is already a
    recognized alias (the common case for canonical-named fields) nothing
    is added — the loop never fabricates a phantom new alias.
    """
    return field_name.replace("_", " ").title()


def record_correction(
    conn: object,
    *,
    doc_id: str,
    field_name: str,
    original_value: str | None,
    corrected_value: str,
    vertical: str,
    tier_that_produced_original: str | None = None,
    session_id: str | None = None,
    label_phrasing: str | None = None,
) -> CorrectionOutcome:
    """Log a correction; optionally learn its on-form phrasing.

    ``conn`` is an open ``cascade.store`` connection. The ``corrections``
    FK references ``runs(doc_id)`` (FK enforcement is on), so the document
    must already have a run row — it always does, having been through the
    cascade to reach the review queue. ``label_phrasing`` is the label as
    *printed on the form* that the cascade failed to map; supply it (the
    demo button does) to close the alias half of the loop, omit it (the
    seeded replay default) to log + re-embed only.
    """
    created_at = datetime.now(UTC).isoformat()
    cur = conn.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO corrections
          (doc_id, field_name, original_value, corrected_value,
           tier_that_produced_original, session_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            field_name,
            original_value,
            corrected_value,
            tier_that_produced_original,
            session_id,
            created_at,
        ),
    )
    conn.commit()  # type: ignore[attr-defined]
    correction_id = int(cur.lastrowid)

    alias_learned = False
    learned_alias: str | None = None
    if label_phrasing and label_phrasing.strip():
        learned = append_correction_alias(
            field_name, seed_vertical_for(vertical), label_phrasing.strip()
        )
        if learned:
            invalidate_alias_caches()
            alias_learned = True
            learned_alias = label_phrasing.strip()

    return CorrectionOutcome(
        correction_id=correction_id,
        doc_id=doc_id,
        field_name=field_name,
        original_value=original_value,
        corrected_value=corrected_value,
        tier_that_produced_original=tier_that_produced_original,
        alias_learned=alias_learned,
        learned_alias=learned_alias,
    )


def refresh_embedding(conn: object, doc_id: str, png: bytes) -> bool:
    """Re-embed a corrected document into the ColQwen retrieval corpus.

    Returns ``True`` when the embedding was (re)written, ``False`` when no
    cached embedding exists and ``EVAL_LIVE`` is unset (CI / no GPU) — the
    loop degrades rather than dragging in torch. Live embedding regeneration
    is the ``EVAL_LIVE=true just embed`` step on the GPU box.
    """
    try:
        mat = embed_image(png)
    except EmbeddingUnavailable:
        return False
    rag_store.upsert_embedding(conn, doc_id, mat)
    return True


def count_corrections(conn: object) -> int:
    """Total corrections logged so far (the demo surfaces this count)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]  # type: ignore[attr-defined]
    )


def bootstrap_alias_table(seed_path: Any) -> dict[str, int]:
    """The loop's bootstrap: the committed seed *is* the prior corrections.

    Returns ``{"records": N, "aliases": M}`` for the frozen seed — a small
    explicit accounting that the alias table is *populated from the seed*
    (canonical-priority schema-design corrections), with the live overlay as
    its runtime extension. There is no separate store to migrate into; this
    function makes the "alias-table population from seed" deliverable
    concrete and testable rather than implicit in the partition harness.
    """
    fields = json.loads(seed_path.read_text(encoding="utf-8"))["fields"]
    return {
        "records": len(fields),
        "aliases": sum(len(r["aliases"]) for r in fields),
    }
