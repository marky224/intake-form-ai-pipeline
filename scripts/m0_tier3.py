"""M0: one live Tier-3 measurement on the dev split (GPU box only).

Measures what the cached replay can't: the 32B Tier 3 (``qwen2.5vl:32b``,
Q4_K_M) with ``num_ctx`` 8192 pinned per request (``tier3_qwen_32b_local.
NUM_CTX``) — its VRAM split, tokens/s and seconds per document — and how its
readings compare with the cached ones:

- **M0a** — a full-field Tier-3 read of every dev document (the 27-field
  prompt the replay cache was recorded with).
- **M0b** — a narrowed Tier-3 read of only the fields the cascade routes to
  Tier 3 for that document (what the cascade sends live since 2026-10-24).

It writes one JSON summary: tokens/s, seconds per document, the VRAM split
``ollama ps`` reports with the model loaded, by-stage F1 from the new
readings (M0a and M0b each, beside the cached-replay F1), and how often the
narrowed reading of a routed field matches the full reading.

Safety rules (the run is unattended):

- Refuses to start (exit 2) if ``ollama ps`` shows any other model loaded,
  or if ``qwen2.5vl:32b`` is not already installed (``ollama list``). It
  never pulls a model.
- Only Tier 3 runs live, so the 32B is loaded once (``keep_alive`` 1h).
  Tier 1, Tier 2 and the router's Stage 2 are replay-only: their files are
  copied from ``--source-cache`` into a fresh cache under ``--work-dir``,
  and any cache miss is an error, never a live call.
- Reads the page images from ``--image-dir`` and never writes there. Never
  writes the source cache, the committed fixtures or the checkout: the work
  dir and the output must lie outside the repository, the image dir and
  the primary checkout, and the work dir must be new or empty.
- ``--max-minutes`` caps the whole run; ``--call-timeout`` caps one call.
  Any failure exits 1 and still writes the summary with ``status: failed``.

Run (from the repository root, models otherwise idle)::

    uv run --offline python -m scripts.m0_tier3 \\
        --image-dir <dev render folder> \\
        --work-dir <new empty dir> \\
        --output <summary.json>

``--dry-run`` does the preflight, seeding and routing but makes no model
call; ``--limit N`` reads only the first N documents (a pilot).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _paths import repo_root
from cascade import eval_cache, router, store
from cascade.narrow import narrow_form_cls
from cascade.orchestrator import build_cascade, process_document, same_reading
from cascade.providers import _qwen_vl
from cascade.providers import tier1_paddleocr_local as tier1
from cascade.providers import tier2_qwen_7b_local as tier2
from cascade.providers import tier3_qwen_32b_local as tier3
from cascade.providers._base import ProviderResult
from evals import by_stage
from evals.by_stage import compute_by_stage
from evals.manifest import load_manifest
from intake_schemas import HealthcareIntakeForm

logger = logging.getLogger("m0_tier3")

#: The only model this run may load (never pulled).
MODEL = tier3.QWEN_MODEL_TAG

#: Replay-only namespaces copied into the work cache. Tier 1 and Tier 2 are
#: required for every document; the router's Stage 2 only when cached.
_REQUIRED = (tier1.PROVIDER_NAME, tier2.PROVIDER_NAME)
_OPTIONAL = (router.STAGE2_PROVIDER_NAME,)

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

Runner = Callable[[list[str]], str]


class Refused(Exception):
    """Preflight said no: the run must not start."""


def run_command(argv: list[str]) -> str:
    """Run a short command (``ollama ps`` / ``list`` / ``--version``)."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True)
    return done.stdout


def _model_names(table: str) -> list[str]:
    """First column of an ``ollama ps`` / ``ollama list`` table, header skipped."""
    rows = [line.split() for line in table.splitlines()[1:] if line.strip()]
    return [row[0] for row in rows if row]


def _processor(ps_table: str, model: str) -> str | None:
    """The PROCESSOR column for ``model`` in ``ollama ps`` ("100% GPU",
    "22%/78% CPU/GPU"), read between the PROCESSOR header and the next one."""
    lines = ps_table.splitlines()
    if not lines or "PROCESSOR" not in lines[0]:
        return None
    header = lines[0]
    start = header.index("PROCESSOR")
    rest = header[start + len("PROCESSOR") :]
    stripped = rest.lstrip()
    end = start + len("PROCESSOR") + (len(rest) - len(stripped)) if stripped else len(header)
    for line in lines[1:]:
        if line.split() and line.split()[0] == model:
            return line[start:end].strip() or None
    return None


def preflight(run: Runner) -> dict[str, str]:
    """Refuse unless only ``MODEL`` (or nothing) is loaded and it is installed."""
    version = run(["ollama", "--version"]).strip()
    ps = run(["ollama", "ps"])
    others = [name for name in _model_names(ps) if name != MODEL]
    if others:
        raise Refused(f"other models loaded: {', '.join(others)} — stop them first")
    listed = run(["ollama", "list"])
    if MODEL not in _model_names(listed):
        raise Refused(f"{MODEL} is not installed; this runner never pulls a model")
    return {"version": version, "ps": ps}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _primary_checkout() -> Path | None:
    """The main checkout of this repository (parent of the git common dir)."""
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root()),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(common).parent if common else None


def _forbidden_roots(image_dir: Path, source_cache: Path) -> list[Path]:
    roots = [repo_root(), image_dir, source_cache]
    primary = _primary_checkout()
    if primary is not None:
        roots.append(primary)
    return roots


def output_allowed(output: Path, image_dir: Path, source_cache: Path) -> bool:
    """True when the summary may be written to ``output``."""
    return not any(_inside(output, root) for root in _forbidden_roots(image_dir, source_cache))


def check_paths(work_dir: Path, output: Path, image_dir: Path, source_cache: Path) -> None:
    """Refuse output locations that could touch the repo, images or cache."""
    forbidden = _forbidden_roots(image_dir, source_cache)
    for path, label in ((work_dir, "--work-dir"), (output, "--output")):
        for root in forbidden:
            if _inside(path, root):
                raise Refused(f"{label} {path} is inside {root}; use a path outside it")
    if work_dir.exists() and any(work_dir.iterdir()):
        raise Refused(f"--work-dir {work_dir} is not empty")
    if not image_dir.is_dir():
        raise Refused(f"--image-dir {image_dir} is not a directory")


def select_docs(split: str, image_dir: Path, limit: int | None) -> list[tuple[str, str, Path]]:
    """``(doc_id, image_sha256, png_path)`` for the split, every PNG present."""
    _, entries = load_manifest()
    docs = sorted((e for e in entries if e.split == split), key=lambda e: e.doc_id)
    if limit:
        docs = docs[:limit]
    out = []
    for e in docs:
        png = image_dir / f"{e.doc_id}.png"
        if not png.is_file():
            raise Refused(f"missing image {png}")
        sha = hashlib.sha256(png.read_bytes()).hexdigest()
        out.append((e.doc_id, sha, png))
    if not out:
        raise Refused(f"no {split!r} documents in the manifest")
    return out


def seed_cache(docs: list[tuple[str, str, Path]], source: Path, dest: Path) -> int:
    """Copy each document's Tier-1/Tier-2 (and any Stage-2) replay files."""
    copied = 0
    for doc_id, sha, _png in docs:
        for slug in (*_REQUIRED, *_OPTIONAL):
            src = source / slug / f"{sha}.json"
            if not src.is_file():
                if slug in _REQUIRED:
                    raise Refused(f"no cached {slug} reading for {doc_id} in {source}")
                continue
            (dest / slug).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / slug / src.name)
            copied += 1
    return copied


class _ReplayOnly:
    """While active: the cache root is ``root``, ``EVAL_LIVE`` is off, and
    every live seam except the runner's own Tier-3 calls raises."""

    def __init__(self, root: Path):
        self.root = root
        self.saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> _ReplayOnly:
        def refuse(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("replay-only: a cache miss would have made a live call")

        patches = [
            (eval_cache, "CACHE_ROOT", self.root),
            (tier1, "_load_paddleocr_vl_pipeline", refuse),
            (tier1, "_invoke_pipeline", refuse),
            (tier2, "_load_ollama_client", refuse),
            (tier2, "_invoke_model", refuse),
            (tier3, "_load_ollama_client", refuse),
            (router, "_load_ollama_client", refuse),
            (router, "_stage2_invoke", refuse),
        ]
        for obj, name, value in patches:
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        self.live = os.environ.pop("EVAL_LIVE", None)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        if self.live is not None:
            os.environ["EVAL_LIVE"] = self.live


class _RecordTier3:
    """A Tier 3 that records which fields the cascade asks it for."""

    name = tier3.PROVIDER_NAME
    tier = tier3.TIER

    def __init__(self) -> None:
        self.asked: list[str] = []

    def extract(self, png: bytes, form_cls: type) -> ProviderResult:
        self.asked = list(_qwen_vl._extractable_fields(form_cls))
        form = form_cls(metadata=tier1._stub_metadata(form_cls))
        return ProviderResult(form=form, latency_ms=0.0, cost_usd=0.0, raw_response={})


def routed_fields(docs: list[tuple[str, str, Path]], cache: Path) -> dict[str, list[str]]:
    """Per document, the fields the cascade sends to Tier 3 (from the cached
    Tier-1/Tier-2 readings; no model call)."""
    out: dict[str, list[str]] = {}
    with _ReplayOnly(cache):
        t1, t2, _ = build_cascade()
        conn = store.connect(":memory:")
        store.init_db(conn)
        try:
            for doc_id, sha, png in docs:
                rec3 = _RecordTier3()
                rec = process_document(
                    png.read_bytes(), doc_id=doc_id, conn=conn, providers=(t1, t2, rec3)
                )
                out[sha] = list(rec.escalations.get("3a", []))
        finally:
            conn.close()
    return out


def default_client(call_timeout: float) -> Any:
    """An Ollama client with a per-call timeout (lazy import)."""
    from ollama import Client  # type: ignore[import-not-found]

    return Client(host=_qwen_vl.OLLAMA_HOST, timeout=call_timeout)


def live_pass(
    client: Any,
    docs: list[tuple[str, str, Path]],
    fields: dict[str, list[str]] | None,
    out_dir: Path,
    deadline: float,
    on_first: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """One Tier-3 call per document (all fields, or only ``fields[sha]``);
    saves each raw response as ``out_dir/<sha>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for i, (doc_id, sha, png) in enumerate(docs, 1):
        names = None if fields is None else fields[sha]
        if names == []:
            continue  # the cascade sends this document nothing
        if time.monotonic() > deadline:
            raise TimeoutError(f"wall-clock cap reached before document {i}/{len(docs)}")
        form_cls = (
            HealthcareIntakeForm if names is None else narrow_form_cls(HealthcareIntakeForm, names)
        )
        t0 = time.perf_counter()
        raw = tier3._invoke_model(client, png.read_bytes(), form_cls)
        wall = time.perf_counter() - t0
        (out_dir / f"{sha}.json").write_text(json.dumps(raw), encoding="utf-8")
        if not rows and on_first is not None:
            on_first()
        rows.append({"doc_id": doc_id, "sha": sha, "fields": names, "wall_s": wall, "raw": raw})
        logger.info("%s %d/%d %s %.1f s", out_dir.name, i, len(docs), doc_id, wall)
    return rows


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "max": None}
    return {
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def timing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tokens/s and seconds per call from Ollama's own durations (ns)."""
    tps, total, load, prompt = [], [], [], []
    for row in rows:
        raw = row["raw"]
        if raw.get("eval_count") and raw.get("eval_duration"):
            tps.append(raw["eval_count"] / (raw["eval_duration"] / 1e9))
        if raw.get("total_duration"):
            total.append(raw["total_duration"] / 1e9)
        if raw.get("load_duration") is not None:
            load.append(raw["load_duration"] / 1e9)
        if raw.get("prompt_eval_duration"):
            prompt.append(raw["prompt_eval_duration"] / 1e9)
    fields = [len(r["fields"]) for r in rows if r["fields"] is not None]
    return {
        "calls": len(rows),
        "tokens_per_s": _stats(tps),
        "seconds_per_call_ollama": _stats(total),
        "seconds_per_call_wall": _stats([r["wall_s"] for r in rows]),
        "load_seconds": _stats(load),
        "prompt_eval_seconds": _stats(prompt),
        "fields_per_call": _stats([float(f) for f in fields]) if fields else None,
    }


def score(
    seed: Path,
    responses: Path,
    dest: Path,
    split: str,
    image_dir: Path,
    doc_ids: set[str],
) -> dict[str, Any]:
    """By-stage F1 over ``doc_ids`` with ``responses`` as the Tier-3
    readings (replay only)."""
    shutil.copytree(seed, dest)
    tier3_dir = dest / tier3.PROVIDER_NAME
    if tier3_dir.exists():
        shutil.rmtree(tier3_dir)
    shutil.copytree(responses, tier3_dir)
    seed_version, entries = load_manifest()
    chosen = [e for e in entries if e.split != split or e.doc_id in doc_ids]
    saved = by_stage.load_manifest
    by_stage.load_manifest = lambda: (seed_version, chosen)
    try:
        with _ReplayOnly(dest):
            result = compute_by_stage(split=split, image_dir=image_dir)
    finally:
        by_stage.load_manifest = saved
    return {
        "by_stage_f1": [round(f1, 4) for _label, f1 in result.stages],
        "funnel": dict(result.funnel.__dict__),
    }


def agreement(full: list[dict[str, Any]], narrow: list[dict[str, Any]]) -> dict[str, Any]:
    """How often a routed field's narrowed reading matches its full reading."""
    full_by_sha = {row["sha"]: row["raw"] for row in full}
    agree = total = 0
    examples = []
    for row in narrow:
        sub = narrow_form_cls(HealthcareIntakeForm, row["fields"])
        n = _qwen_vl.parse_response(row["raw"], sub, tier=tier3.TIER, pipeline_version="m0")
        raw_full = full_by_sha.get(row["sha"])
        if raw_full is None:
            continue
        f = _qwen_vl.parse_response(
            raw_full, HealthcareIntakeForm, tier=tier3.TIER, pipeline_version="m0"
        )
        for name in row["fields"]:
            a, b = getattr(f, name).value, getattr(n, name).value
            total += 1
            if (a is None and b is None) or same_reading(a, b):
                agree += 1
            elif len(examples) < 25:
                examples.append(
                    {"doc_id": row["doc_id"], "field": name, "full": str(a), "narrowed": str(b)}
                )
    return {
        "fields": total,
        "agree": agree,
        "rate": round(agree / total, 4) if total else None,
        "disagreements": examples,
    }


def main(
    argv: list[str] | None = None,
    *,
    run: Runner = run_command,
    client_factory: Callable[[float], Any] = default_client,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.m0_tier3",
        description="M0: live Tier-3 (qwen2.5vl:32b, num_ctx 8192) measurement on the dev split.",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        required=True,
        help="folder of <doc_id>.png / .json pages (read-only)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="new or empty folder outside the repo for caches and raw responses",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="path of the JSON summary (outside the repo)"
    )
    parser.add_argument(
        "--source-cache",
        type=Path,
        default=eval_cache.CACHE_ROOT,
        help="replay cache to copy Tier-1/Tier-2 readings from (read-only)",
    )
    parser.add_argument("--split", default="dev", help="manifest split (default: dev)")
    parser.add_argument("--limit", type=int, default=None, help="first N documents only")
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=240.0,
        help="wall-clock cap for the whole run (default: 240)",
    )
    parser.add_argument(
        "--call-timeout",
        type=float,
        default=600.0,
        help="seconds one Tier-3 call may take (default: 600)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preflight, seed and route only; no model call"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    started = time.monotonic()
    deadline = started + args.max_minutes * 60
    summary: dict[str, Any] = {
        "status": "failed",
        "error": None,
        "model": MODEL,
        "num_ctx": tier3.NUM_CTX,
        "split": args.split,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    code = EXIT_FAILED
    try:
        check_paths(args.work_dir, args.output, args.image_dir, args.source_cache)
        before = preflight(run)
        summary["ollama"] = {"version_before": before["version"], "ps_before": before["ps"]}
        docs = select_docs(args.split, args.image_dir, args.limit)
        summary["docs"] = len(docs)
        seed = args.work_dir / "seed-cache"
        summary["seeded_files"] = seed_cache(docs, args.source_cache, seed)
        routed = routed_fields(docs, seed)
        summary["routed_fields_per_doc"] = round(
            sum(len(v) for v in routed.values()) / len(docs), 3
        )
        summary["docs_sent_to_tier3"] = sum(1 for v in routed.values() if v)
        ids = {doc_id for doc_id, _sha, _png in docs}
        cached = score(
            seed,
            args.source_cache / tier3.PROVIDER_NAME,
            args.work_dir / "score-cached",
            args.split,
            args.image_dir,
            ids,
        )
        summary["cached_replay"] = cached
        if args.dry_run:
            summary["status"] = "dry-run"
            code = EXIT_OK
            return code

        client = client_factory(args.call_timeout)

        def note_loaded() -> None:
            ps = run(["ollama", "ps"])
            summary["ollama"]["ps_loaded"] = ps
            summary["ollama"]["processor_loaded"] = _processor(ps, MODEL)

        full = live_pass(client, docs, None, args.work_dir / "m0a-full", deadline, note_loaded)
        narrow = live_pass(client, docs, routed, args.work_dir / "m0b-narrowed", deadline)
        summary["m0a_full"] = {
            **timing(full),
            **score(
                seed,
                args.work_dir / "m0a-full",
                args.work_dir / "score-m0a",
                args.split,
                args.image_dir,
                ids,
            ),
        }
        summary["m0b_narrowed"] = (
            {
                **timing(narrow),
                **score(
                    seed,
                    args.work_dir / "m0b-narrowed",
                    args.work_dir / "score-m0b",
                    args.split,
                    args.image_dir,
                    ids,
                ),
            }
            if narrow
            else timing(narrow)
        )
        summary["narrowed_vs_full"] = agreement(full, narrow)
        summary["status"] = "ok"
        code = EXIT_OK
    except Refused as exc:
        summary["status"] = "refused"
        summary["error"] = str(exc)
        code = EXIT_REFUSED
    except Exception as exc:  # unattended run: record and exit non-zero
        logger.exception("M0 failed")
        summary["error"] = f"{type(exc).__name__}: {exc}"
        code = EXIT_FAILED
    finally:
        if code != EXIT_REFUSED or "ollama" in summary:
            try:
                summary.setdefault("ollama", {})["version_after"] = run(
                    ["ollama", "--version"]
                ).strip()
                summary["ollama"]["ps_after"] = run(["ollama", "ps"])
            except Exception as exc:  # pragma: no cover - logged, not fatal
                summary.setdefault("ollama", {})["after_error"] = str(exc)
        summary["elapsed_s"] = round(time.monotonic() - started, 1)
        summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        text = json.dumps(summary, indent=2) + "\n"
        if output_allowed(args.output, args.image_dir, args.source_cache):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
