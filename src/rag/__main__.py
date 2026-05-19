"""``python -m rag <correct|embed>`` — the Phase 8 loop CLI.

- ``correct`` — the seeded-reviewer replay over the parked demo documents.
  Cached/$0, no GPU; prints the corrections / aliases-learned /
  embeddings-refreshed tally. This is the ``just correct`` recipe.
- ``embed`` — (re)generate the committed ColQwen ``.npy`` fixtures for the
  demo corpus. Only meaningful under ``EVAL_LIVE=true`` on the GPU box
  (``just embed`` sets it); without it every image is a cache hit / miss
  and nothing is regenerated. This is the live-regen step documented in
  ``.claude-context/starter-prompts/phase8-live-regen.md``.
"""

from __future__ import annotations

import sys

from cascade.eval_cache import is_live_mode
from demo.data import list_demo_docs, replay_review_queue_corrections
from rag.embed import embed_image


def _correct() -> int:
    replay = replay_review_queue_corrections()
    print(
        f"parked documents replayed : {len(replay.docs)}\n"
        f"corrections logged        : {replay.corrections_applied}\n"
        f"new aliases learned       : {replay.aliases_learned}\n"
        f"embeddings refreshed      : {replay.embeddings_refreshed}"
    )
    for d in replay.docs:
        print(f"\n{d.label} ({d.doc_id[:12]}…)")
        for c in d.corrections:
            mark = " +alias" if c.alias_learned else ""
            print(f"  {c.field_name}: {c.original_value!r} → {c.corrected_value!r}{mark}")
        if d.neighbors:
            nn = ", ".join(f"{n.doc_id[:12]}…={n.score:.2f}" for n in d.neighbors)
            print(f"  nearest corrected: {nn}")
    return 0


def _embed() -> int:
    if not is_live_mode():
        print(
            "EVAL_LIVE is not set — `python -m rag embed` only does real work on "
            "the GPU box with EVAL_LIVE=true (use `just embed`). Nothing to do.",
            file=sys.stderr,
        )
        return 1
    docs = list_demo_docs()
    for d in docs:
        embed_image(d.png_path.read_bytes())
        print(f"embedded {d.label} ({d.doc_id[:12]}…)")
    print(f"\n{len(docs)} ColQwen .npy fixtures (re)generated.")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "correct":
        return _correct()
    if cmd == "embed":
        return _embed()
    print("usage: python -m rag <correct|embed>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
