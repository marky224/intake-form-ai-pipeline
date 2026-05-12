# DocILE ingestion

Thin Python wrapper around the upstream DocILE dataset download script,
plus an annotation parser and a PDF→PNG rasterizer that together stage
the business-documents vertical of the Phase 3 corpus into the project
documents bucket.

## Pinned upstream

- **Repo:** `rossumai/docile` ([github.com/rossumai/docile](https://github.com/rossumai/docile))
- **Commit:** `12f9502d1ee80143c24eb98d89abc324db8003b6` (2024-05-15)
- **Vendored file:** `download_dataset.sh`
- **SHA-256:** `44422d0bbd05a8a62055ff98958974c423a739dee9e224b460e2c55da7261b9c`

`download.py` verifies the vendored script's sha256 on every invocation;
a mismatched checksum aborts the run rather than executing a drifted
script. Bump the pinned commit + sha256 in `download.py` together when
upgrading.

## License

The DocILE dataset and `download_dataset.sh` are distributed under the
**CC BY-NC-ND 4.0** license. Vendored verbatim with no modifications.
See the upstream repo for full license terms. The dataset itself
requires a registration token, obtained at
[docile.rossum.ai](https://docile.rossum.ai); the token lives in `.env`
as `DOCILE_ACCESS_TOKEN` (gitignored, not mirrored in `.env.example`).

## Running

From the repo root with `DOCILE_ACCESS_TOKEN` exported (the `just`
recipe auto-loads `.env`):

```bash
just synthetic-data-docile-build              # full labeled-trainval (~1.6 GB on S3)
just synthetic-data-docile-build --limit 5    # 5-doc smoke
```

## Scope (locked)

- **Splits downloaded:** `labeled-trainval` only (combined train+val zip).
  The `test`/`synthetic`/`unlabeled` archives are explicitly rejected by
  `download.py` per the half-now-half-later corpus-partitioning lock in
  `cost-model.md` — the `test` split is reserved for the post-launch
  Phase 7 `just process-batch` recipe.
- **Annotation task:** KILE (the 55-field key-information taxonomy) only.
  LIR (`line_item_extractions`, `line_item_headers`) is parsed but not
  staged into the per-page sidecar — Phase 4 cascade work consumes the
  KILE fields against the `BusinessDocumentForm` schema.
- **Rasterization:** 200 DPI, one PNG per page. Matches the upstream
  metadata's `page_sizes_at_200dpi` so bbox coordinates round-trip
  cleanly between normalized and pixel space.
