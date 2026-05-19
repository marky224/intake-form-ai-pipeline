# Vendored fonts for the CMS-1500 renderer

These fonts are vendored to keep the synthetic-data pipeline reproducible
byte-for-byte: the CDN can't drop a release, DNS can't flake mid-render,
and offline dev keeps working. Used only by `synthetic_data/render/`.

| File | Source | License | Used for |
|---|---|---|---|
| `Caveat-Regular.ttf` | [google/fonts ofl/caveat](https://github.com/google/fonts/tree/main/ofl/caveat) (variable-weight file) | SIL OFL 1.1 (`Caveat-OFL.txt`) | Handwritten signature variant |
| `Sacramento-Regular.ttf` | [google/fonts ofl/sacramento](https://github.com/google/fonts/tree/main/ofl/sacramento) | SIL OFL 1.1 (`Sacramento-OFL.txt`) | Handwritten signature variant |
| `HomemadeApple-Regular.ttf` | [google/fonts apache/homemadeapple](https://github.com/google/fonts/tree/main/apache/homemadeapple) | Apache 2.0 (`HomemadeApple-LICENSE.txt`) | Handwritten signature variant |

Arial is intentionally not vendored — typed signatures render against the
host Chromium's default sans-serif stack, which substitutes Liberation
Sans (Linux) / Arial (macOS, Windows) for the `Arial` family. RATIONALE
§1 specifies "Arial" semantically; consistent rendering across CI and
local dev is what matters, not the exact glyph metrics.

To refresh the vendored copies, re-download from the URLs above. The
upstream `google/fonts` repo is the canonical distribution channel that
Google Fonts CDN itself pulls from.
