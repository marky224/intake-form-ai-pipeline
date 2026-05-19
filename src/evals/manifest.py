"""Corpus partition manifest + leakage validation.

``evals/manifest.json`` partitions every corpus document into ``train`` /
``dev`` / ``test``. The eval harness reports F1 on ``test`` only; ``train``
informs prompt/threshold work, ``dev`` is the router spot-check set.

**Partition key is the patient, not the document** (``eval-methodology.md``
"Leakage mitigations"). A Synthea patient can render multiple documents; if
one patient straddled train and test, a memorizing extractor would look
like it generalized. The CMS-1500 filename is ``<patient-uuid>-<sha8>`` and
the locked Phase 3 corpus is 1:1 patient:document, so the patient UUID is
``doc_id.rsplit("-", 1)[0]`` and ``assign_split`` hashes *that*. DocILE
partitions on its own document id and is constrained to DocILE val (never
DocILE train) by the caller.

**Committed manifest = the 6 CMS-1500 validation docs, all ``test``.** They
were never used for prompt/threshold tuning (they are held-out validation
fixtures), so they are an honest test slice; the F1-over-time chart is
computed over them. ``train``/``dev`` populate locally when the deferred
500-doc local-store render runs (``phases.md`` Phase 3 follow-up) — that is
why ``assign_split`` exists even though the committed manifest doesn't use
it. No DocILE entry is ever committed (CC-BY-NC-ND); the DocILE manifest is
built locally and gitignored.

``validate_partition`` is the CI guard: it fails the build if any partition
key appears in more than one split, mirroring the Phase 5 router
schema-drift test. It runs in the standard suite on every PR.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from _paths import src_root

Split = Literal["train", "dev", "test"]

#: Committed manifest (sibling file) + the CMS-1500 validation corpus
#: (under ``src/tests/fixtures/`` since the 2026-05-19 src-layout refactor).
MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
CMS1500_VALIDATION_DIR = src_root() / "tests" / "fixtures" / "eval-validation" / "cms1500"

#: The gitignored full local healthcare corpus (``just synthetic-data-
#: render-500`` output). ``build_corpus_manifest`` walks its sidecars to
#: regenerate the committed 584-entry stratified ``manifest.json``. This is
#: a LOCAL regeneration input (not in CI) — CI validates the committed
#: manifest via ``load_manifest`` + the stratification drift guard, never
#: by rebuilding from this dir. Only the ``test``-split subset is staged
#: into ``CMS1500_VALIDATION_DIR`` and committed (``run_eval`` reads only
#: the ``test`` split; train/dev PNG bytes are never read by CI).
CORPUS_RENDER_DIR = src_root() / "synthetic_data" / "output" / "render"


@dataclass(frozen=True)
class ManifestEntry:
    """One corpus document's partition assignment.

    ``partition_key`` is what leakage validation groups on (patient UUID for
    CMS-1500, DocILE doc id for business). ``image_sha256`` ties the entry
    to its ``tests/fixtures/eval-cache/`` replay fixtures.
    """

    doc_id: str
    partition_key: str
    vertical: str
    split: Split
    image_sha256: str


def patient_key_from_doc_id(doc_id: str) -> str:
    """CMS-1500 ``<patient-uuid>-<sha8>`` → the patient UUID."""
    return doc_id.rsplit("-", 1)[0]


def derive_partition_key(doc_id: str, vertical: str) -> str:
    """Partition key from ``doc_id``: patient UUID (healthcare) / id (DocILE).

    The key is *not* serialized into ``manifest.json`` — it is fully
    derivable, and persisting the bare Synthea patient UUID trips gitleaks'
    ``generic-api-key`` entropy heuristic (a documented false positive: it's
    public synthetic data, already on disk as fixture filenames). Deriving
    it on load removes the finding at the source instead of allowlisting it,
    and keeps the file free of redundant derived data.
    """
    return patient_key_from_doc_id(doc_id) if vertical == "healthcare" else doc_id


def assign_split(
    partition_key: str,
    *,
    ratios: tuple[float, float, float] = (0.7, 0.15, 0.15),
    salt: str = "intake-v1",
) -> Split:
    """Deterministic stratification: hash the *partition key*, not the doc.

    Same patient → same bucket regardless of how many documents they render.
    Stable across runs (sha256 of ``salt:key``), so the local 500-doc
    manifest regenerates identically. Used for the deferred local corpus;
    the committed 6-doc manifest is all ``test``.
    """
    train, dev, _ = ratios
    h = hashlib.sha256(f"{salt}:{partition_key}".encode()).hexdigest()
    frac = int(h[:16], 16) / float(1 << 64)
    if frac < train:
        return "train"
    if frac < train + dev:
        return "dev"
    return "test"


def build_cms1500_manifest(
    validation_dir: Path | str = CMS1500_VALIDATION_DIR,
) -> list[ManifestEntry]:
    """The committed CMS-1500 corpus: every validation doc as ``test``.

    These are held-out validation fixtures (never tuning inputs), so the
    honest split for the committed CI/portfolio set is ``test``.
    """
    validation_dir = Path(validation_dir)
    entries: list[ManifestEntry] = []
    for sidecar in sorted(validation_dir.glob("*.json")):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        doc_id = sidecar.stem
        entries.append(
            ManifestEntry(
                doc_id=doc_id,
                partition_key=derive_partition_key(doc_id, "healthcare"),
                vertical="healthcare",
                split="test",
                image_sha256=meta["image_sha256"],
            )
        )
    return entries


def build_corpus_manifest(
    render_dir: Path | str = CORPUS_RENDER_DIR,
) -> list[ManifestEntry]:
    """The full local healthcare corpus → patient-stratified entries.

    Walks every ``<doc_id>.json`` sidecar in the gitignored render dir
    (``just synthetic-data-render-500`` output, 1:1 patient:document) and
    assigns each to ``train``/``dev``/``test`` via :func:`assign_split` on
    the patient key. This is the *local regeneration tool* behind the
    committed broad ``manifest.json``: ``render_dir`` is not present in CI,
    so CI validates the committed file (``load_manifest`` + the
    stratification drift guard), never rebuilds it here.

    Stable across regenerations: ``assign_split`` hashes the patient key,
    and the ``<patient-uuid>-<sha8>`` doc id derives from patient content
    (not the PNG bytes, which are Chromium-render-dependent), so the
    partition is reproducible even though the pixels are not.
    """
    render_dir = Path(render_dir)
    entries: list[ManifestEntry] = []
    for sidecar in sorted(render_dir.glob("*.json")):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        doc_id = sidecar.stem
        partition_key = derive_partition_key(doc_id, "healthcare")
        entries.append(
            ManifestEntry(
                doc_id=doc_id,
                partition_key=partition_key,
                vertical="healthcare",
                split=assign_split(partition_key),
                image_sha256=meta["image_sha256"],
            )
        )
    if not entries:
        raise ValueError(
            f"no sidecars under {render_dir} — run `just synthetic-data-render-500` "
            f"first (the broad corpus is gitignored and built locally)"
        )
    return entries


def validate_partition(entries: list[ManifestEntry]) -> None:
    """Raise if any partition key spans more than one split.

    The leakage guard. CI runs this over the committed manifest on every
    PR; a partition edit that lands a patient in two splits fails the build.
    """
    splits_by_key: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        splits_by_key[e.partition_key].add(e.split)
    leaked = {k: sorted(v) for k, v in splits_by_key.items() if len(v) > 1}
    if leaked:
        raise ValueError(f"partition leakage — keys in >1 split: {leaked}")


def write_manifest(
    entries: list[ManifestEntry],
    *,
    seed_version: str,
    path: Path | str = MANIFEST_PATH,
) -> None:
    """Serialize the manifest, validating partitions before writing."""
    validate_partition(entries)
    payload = {
        "seed_version": seed_version,
        "partition_key": "patient-level — derived on load (CMS-1500 patient "
        "UUID / DocILE doc id); not serialized (gitleaks false-positive)",
        "entries": [
            # partition_key is deliberately omitted — derived on load.
            {
                "doc_id": e.doc_id,
                "vertical": e.vertical,
                "split": e.split,
                "image_sha256": e.image_sha256,
            }
            for e in sorted(entries, key=lambda e: e.doc_id)
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(path: Path | str = MANIFEST_PATH) -> tuple[str, list[ManifestEntry]]:
    """Return ``(seed_version, entries)`` from a manifest file."""
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [
        ManifestEntry(
            doc_id=e["doc_id"],
            partition_key=derive_partition_key(e["doc_id"], e["vertical"]),
            vertical=e["vertical"],
            split=e["split"],
            image_sha256=e["image_sha256"],
        )
        for e in payload["entries"]
    ]
    return payload["seed_version"], entries
