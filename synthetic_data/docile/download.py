"""Download the DocILE ``labeled-trainval`` archive into a local directory.

Thin Python wrapper around the vendored upstream ``download_dataset.sh``.
The script itself is the canonical download path published by
``rossumai/docile`` — re-implementing the curl+unzip dance in Python
would diverge from upstream every time their hosting changes. The
wrapper exists to:

- Verify the vendored script's sha256 hasn't drifted from the pinned
  upstream commit, so a tampered or accidentally-edited script can't
  run.
- Pull the access token from ``DOCILE_ACCESS_TOKEN`` rather than the
  command line, so the token doesn't leak into shell history when the
  recipe is invoked interactively.
- Enforce a single allowed dataset (``labeled-trainval``). The
  half-now-half-later corpus-partitioning lock in ``cost-model.md``
  reserves the ``test`` split for the Phase 7 ``just process-batch``
  recipe; rejecting ``test`` here is defense-in-depth against
  accidentally pulling it during a build.
- Be idempotent: re-running is a no-op when the corpus is already on
  disk, matching the ``upload.py`` content-addressable retry pattern.

AWS-bucket-style auth note: the token is a URL path segment in the
upstream script (``https://docile-dataset-rossum.s3.eu-west-1.amazonaws.com/$token/<zip>``),
not an HTTP header. It functions as a presigned-share-link credential,
not a bearer token. We accept the small ``ps``-visibility window when
the script is invoked (the token shows up as a positional argv) because
the token is registration-scoped to this dev environment.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

VENDORED_SCRIPT_PATH = Path(__file__).parent / "download_dataset.sh"
"""Absolute path to the vendored upstream script bundled in this package."""

VENDORED_SCRIPT_SHA256 = "44422d0bbd05a8a62055ff98958974c423a739dee9e224b460e2c55da7261b9c"
"""sha256 of ``download_dataset.sh`` at upstream commit
``12f9502d1ee80143c24eb98d89abc324db8003b6``. Bump alongside the file
when re-vendoring."""

TOKEN_ENV_VAR = "DOCILE_ACCESS_TOKEN"
"""Environment variable carrying the DocILE registration token."""

BUILD_DATASET = "labeled-trainval"
"""The only dataset name Phase 3.5 may fetch. ``test`` / ``synthetic`` /
``unlabeled`` are reserved for post-launch batches per the
half-now-half-later partitioning lock; see module docstring."""

# Subprocess runner type — matches subprocess.run's signature closely
# enough for our purposes. Injecting it makes tests trivial without
# patching subprocess globally.
SubprocessRunner = Callable[..., subprocess.CompletedProcess]


class DocileScriptMismatchError(RuntimeError):
    """The vendored ``download_dataset.sh`` no longer matches the pinned sha256."""


class DocileTokenMissingError(RuntimeError):
    """``DOCILE_ACCESS_TOKEN`` was not set in the environment."""


def verify_vendored_script(script_path: Path = VENDORED_SCRIPT_PATH) -> None:
    """Raise ``DocileScriptMismatchError`` if the vendored script drifted.

    Hashes the file at ``script_path`` and compares to the pinned
    ``VENDORED_SCRIPT_SHA256``. Run this before invoking the script so a
    drifted (or missing) copy aborts the build rather than executing
    a tampered script or surfacing an opaque ``FileNotFoundError``.
    Both drift and absence land on the same error class so the CLI's
    exit-code mapping handles them identically.
    """
    try:
        actual = hashlib.sha256(script_path.read_bytes()).hexdigest()
    except FileNotFoundError as exc:
        raise DocileScriptMismatchError(
            f"Vendored download script not found at {script_path}. "
            f"Re-vendor from the pinned upstream commit."
        ) from exc
    if actual != VENDORED_SCRIPT_SHA256:
        raise DocileScriptMismatchError(
            f"Vendored {script_path.name} sha256 {actual!r} does not match "
            f"pinned {VENDORED_SCRIPT_SHA256!r}. Re-vendor from the pinned "
            f"upstream commit or update the constant after a deliberate bump."
        )


def resolve_token(env: dict[str, str] | None = None) -> str:
    """Return the DocILE access token from the environment.

    ``env`` defaults to ``os.environ`` and is injectable for testing.
    Raises ``DocileTokenMissingError`` with a clear remediation message
    if the env var is unset, empty, or whitespace-only. Mirrors
    ``_load_sidecar``'s ``.strip()`` treatment of ``source_id`` —
    whitespace-only values are functionally invalid (interpolating
    them into the URL path would produce a 404 from the upstream S3
    bucket, but with a less actionable error than catching the typo
    at the wrapper layer).
    """
    source = env if env is not None else os.environ
    token = source.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise DocileTokenMissingError(
            f"{TOKEN_ENV_VAR} is not set. Register at https://docile.rossum.ai "
            f"to obtain a token, then add `{TOKEN_ENV_VAR}=<token>` to .env."
        )
    return token


def annotations_dir_is_populated(dest_dir: Path) -> bool:
    """True when ``<dest_dir>`` looks like a fully-extracted DocILE root.

    Used as the idempotency check: a re-run after a successful download
    sees the populated directory and skips the curl+unzip step. Four
    signals must all be present:

    * ``annotations/`` exists and contains at least one JSON file.
    * ``pdfs/`` exists and contains at least one PDF — without this,
      a partial extraction that wrote annotations but stopped before
      the PDFs would look "populated" and ingest would fail later.
    * ``train.json`` exists at the root (one of the split indexes
      ``download_dataset.sh --unzip`` writes; absence indicates the
      unzip didn't reach the end of the archive).
    * ``val.json`` exists at the root (the other split index).

    If any signal is missing — e.g., the user Ctrl-Ced during
    extraction or manually deleted a file — the wrapper retriggers
    the full download rather than treating a partial state as
    complete.
    """
    annotations = dest_dir / "annotations"
    pdfs = dest_dir / "pdfs"
    if not annotations.is_dir() or not any(annotations.glob("*.json")):
        return False
    if not pdfs.is_dir() or not any(pdfs.glob("*.pdf")):
        return False
    if not (dest_dir / "train.json").is_file():
        return False
    return (dest_dir / "val.json").is_file()


def download_labeled_trainval(
    dest_dir: Path,
    *,
    dataset: str = BUILD_DATASET,
    skip_if_present: bool = True,
    runner: SubprocessRunner | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Download the DocILE labeled-trainval archive into ``dest_dir``.

    Returns ``dest_dir`` (the unzipped layout root: ``annotations/``,
    ``pdfs/``, ``train.json``, ``val.json`` land directly under it).

    ``dataset`` defaults to the only allowed value (``"labeled-trainval"``).
    Passing any other value — ``test``, ``synthetic``, ``unlabeled`` —
    raises ``ValueError`` per the half-now-half-later lock. The
    parameter exists so a future Phase 7 ``process-batch`` module can
    re-use the same wrapper with the locked guard relaxed via a
    separate, deliberate code change.
    """
    if dataset != BUILD_DATASET:
        raise ValueError(
            f"Phase 3.5 may only download {BUILD_DATASET!r}; got {dataset!r}. "
            f"The test/synthetic/unlabeled splits are reserved for the Phase 7 "
            f"process-batch recipe per the half-now-half-later partitioning lock "
            f"in cost-model.md."
        )

    # Idempotency check runs FIRST — before verify_vendored_script and
    # resolve_token — so a re-run against an already-populated dataset
    # is a true no-op. Requiring a valid token + intact vendored script
    # for a call that won't make any network requests would be
    # over-strict (and would break re-runs after token rotation).
    dest_dir = Path(dest_dir)
    if skip_if_present and annotations_dir_is_populated(dest_dir):
        return dest_dir

    verify_vendored_script()
    token = resolve_token(env=env)

    # Resolve `runner` at call time rather than capturing ``subprocess.run``
    # at function-definition time — late binding lets tests monkeypatch
    # ``synthetic_data.docile.download.subprocess.run`` and have the patched
    # value picked up by ``main()`` (which doesn't expose ``runner=`` in
    # its argparse surface).
    if runner is None:
        runner = subprocess.run

    dest_dir.mkdir(parents=True, exist_ok=True)
    # bash <script> <token> <dataset> <dir> --unzip
    # `bash` rather than direct exec sidesteps any chmod weirdness on
    # systems where the file checked out without the +x bit.
    #
    # Capture BOTH stdout and stderr so we can redact the token before
    # re-emitting. The vendored script echoes "Downloading <url>" on
    # stdout (URL contains the token as a path segment), and curl can
    # emit "curl: (22) ... <url>" on stderr when the server returns
    # an error (e.g., 404 for a wrong token) — without stderr redaction
    # those error paths leak credentials. Trade-off: capturing stderr
    # loses curl's live progress bar during the download, but the
    # finished progress lands at end-of-run. Token safety > live UX
    # for a recipe that runs once or twice manually.
    try:
        result = runner(
            [
                "bash",
                str(VENDORED_SCRIPT_PATH),
                token,
                dataset,
                str(dest_dir),
                "--unzip",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        # ``check=True`` raises ``CalledProcessError`` with raw stdout/stderr
        # attached when the script exits non-zero. Those attributes carry the
        # token-bearing URL (the script echoes it before curl, and curl's own
        # error output includes the URL). Redact in place before re-raising
        # so any Python-side traceback or programmatic caller that inspects
        # the exception sees the masked form. Also emit the redacted streams
        # to the user's terminal so the diagnostic isn't lost on the error
        # path (without this, the captured curl error message would only
        # survive on the exception object, never reaching stdout/stderr).
        if err.stdout:
            err.stdout = err.stdout.replace(token, "<TOKEN-REDACTED>")
            sys.stdout.write(err.stdout)
        if err.stderr:
            err.stderr = err.stderr.replace(token, "<TOKEN-REDACTED>")
            sys.stderr.write(err.stderr)
        raise
    captured_out = getattr(result, "stdout", None)
    if captured_out:
        sys.stdout.write(captured_out.replace(token, "<TOKEN-REDACTED>"))
    captured_err = getattr(result, "stderr", None)
    if captured_err:
        sys.stderr.write(captured_err.replace(token, "<TOKEN-REDACTED>"))
    return dest_dir


def main(argv: Sequence[str] | None = None) -> int:
    """Download the DocILE labeled-trainval archive.

    Usage::

        uv run python -m synthetic_data.docile.download \\
            --dest synthetic_data/output/docile

    Reads ``DOCILE_ACCESS_TOKEN`` from the environment (the ``just``
    recipe auto-loads ``.env``; outside ``just`` callers should
    ``source`` it themselves). Exit codes: 0 success, 1 token missing,
    2 vendored-script drift (or absent), 3 dataset-name reject,
    4 download subprocess failure.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Download the DocILE labeled-trainval archive (annotations + PDFs + "
            "split indexes) into a local directory via the vendored upstream script."
        ),
    )
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Directory to extract the dataset into (annotations/, pdfs/, *.json land here).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if annotations/ is already populated.",
    )
    args = parser.parse_args(argv)

    try:
        dest = download_labeled_trainval(args.dest, skip_if_present=not args.force)
    except DocileTokenMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except DocileScriptMismatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except subprocess.CalledProcessError as exc:
        # The vendored script exits non-zero on download failure (bad
        # token, wrong dataset name, or size-mismatch sanity check).
        # Bubble up as a stable exit code; stderr from the subprocess
        # has already flowed through to the user's terminal.
        print(
            f"DocILE download script failed (exit {exc.returncode}).",
            file=sys.stderr,
        )
        return 4
    print(f"DocILE labeled-trainval ready at {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
