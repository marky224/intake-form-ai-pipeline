"""ColQwen multivector embeddings → the *reserved* ``embeddings`` table.

Phase 5 created ``embeddings(doc_id TEXT PK, vector BLOB, created_at TEXT)``
explicitly so Phase 8 "only adds writes, never a schema migration"
(``cascade/store.py`` docstring). We keep that promise: ColQwen's
per-document multivector matrix is packed into the existing ``vector BLOB``
column as-is. **No ``ALTER TABLE``, no ``vec0`` virtual table, no new
dependency** — see ``rag/__init__`` for why ``sqlite-vec`` is the wrong
tool for a late-interaction model at this corpus scale.

Blob layout (little-endian, self-describing so a reader needs no sidecar)::

    magic   4 bytes  b"CQV1"
    rows    uint32   number of patch tokens
    cols    uint32   embedding dim
    data    rows*cols * float32   row-major

The ``embeddings`` FK references ``runs(doc_id)`` and ``connect()`` turns FK
enforcement on, so a ``runs`` row must exist for ``doc_id`` before
:func:`upsert_embedding` (every embedded document has been through the
cascade, so it always does in practice; tests insert a stub run row).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy is a declared dep; the hint stays import-light
    import numpy as np

_MAGIC = b"CQV1"
_HEADER = struct.Struct("<4sII")  # magic, rows, cols


def pack_matrix(mat: np.ndarray) -> bytes:
    """Serialize a 2-D float32 multivector to the self-describing blob.

    Coerces to contiguous little-endian ``float32``; rejects anything not
    2-D so a malformed embedding fails loudly at the write boundary rather
    than silently round-tripping garbage.
    """
    import numpy as np

    arr = np.ascontiguousarray(mat, dtype="<f4")
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D (tokens, dim) matrix, got ndim={arr.ndim}")
    rows, cols = arr.shape
    return _HEADER.pack(_MAGIC, rows, cols) + arr.tobytes()


def unpack_matrix(blob: bytes) -> np.ndarray:
    """Inverse of :func:`pack_matrix`. Validates the magic + length."""
    import numpy as np

    magic, rows, cols = _HEADER.unpack_from(blob)
    if magic != _MAGIC:
        raise ValueError(f"not a ColQwen embedding blob (magic={magic!r})")
    expected = _HEADER.size + rows * cols * 4
    if len(blob) != expected:
        raise ValueError(f"blob length {len(blob)} != expected {expected}")
    flat = np.frombuffer(blob, dtype="<f4", offset=_HEADER.size, count=rows * cols)
    return flat.reshape(rows, cols).astype(np.float32)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_embedding(conn: object, doc_id: str, mat: np.ndarray) -> None:
    """Insert-or-replace one document's multivector embedding.

    ``conn`` is an open ``sqlite3.Connection`` from
    ``cascade.store.connect``. ``INSERT OR REPLACE`` so re-embedding a
    ``doc_id`` (e.g. after a correction) overwrites cleanly.
    """
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR REPLACE INTO embeddings (doc_id, vector, created_at) VALUES (?, ?, ?)",
        (doc_id, pack_matrix(mat), _now_iso()),
    )
    conn.commit()  # type: ignore[attr-defined]


def load_embedding(conn: object, doc_id: str) -> np.ndarray | None:
    """The stored multivector for ``doc_id``, or ``None`` if not embedded."""
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT vector FROM embeddings WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return unpack_matrix(row[0])


def load_corpus(
    conn: object,
    exclude: Iterable[str] = (),
) -> dict[str, np.ndarray]:
    """Every stored embedding as ``{doc_id: matrix}``, minus ``exclude``.

    ``exclude`` is typically the query document itself so retrieval never
    returns the doc as its own nearest neighbor.
    """
    skip = set(exclude)
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT doc_id, vector FROM embeddings"
    ).fetchall()
    return {
        doc_id: unpack_matrix(blob)
        for doc_id, blob in rows
        if blob is not None and doc_id not in skip
    }
