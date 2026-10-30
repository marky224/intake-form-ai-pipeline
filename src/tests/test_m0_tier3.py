"""Tests for ``scripts/m0_tier3.py`` — the unattended live Tier-3 run.

Every test runs the script against stubs: ``ollama`` commands come from a
fake runner and the Tier-3 client replays the committed test-split
fixtures. No model is ever called. The script lives outside the ``src``
packages, so it is loaded by path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from _paths import repo_root
from cascade import eval_cache
from cascade.providers import tier3_qwen_32b_local as tier3
from evals.manifest import CMS1500_VALIDATION_DIR

_SPEC = importlib.util.spec_from_file_location("m0_tier3", repo_root() / "scripts" / "m0_tier3.py")
m0 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m0)

_HEADER = "NAME             ID              SIZE     PROCESSOR    CONTEXT    UNTIL"
_LOADED = "qwen2.5vl:32b    3edc3a52fe98    26 GB    100% GPU     8192       59 minutes from now"


class _Ollama:
    """Fake ``ollama`` CLI: nothing loaded until the first chat, then the 32B."""

    def __init__(self, loaded: list[str] | None = None, installed: bool = True):
        self.loaded = loaded or []
        self.installed = installed
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv[0] == "nvidia-smi":
            used = "15157" if self.loaded else "0"
            return f"0, NVIDIA GeForce RTX 4080, {used} MiB, 16376 MiB\n"
        if argv[1] == "stop":
            self.loaded = []
            return ""
        if argv[1] == "--version":
            return "ollama version is 0.20.7\n"
        if argv[1] == "ps":
            return "\n".join([_HEADER, *self.loaded]) + "\n"
        rows = ["qwen2.5vl:32b    3edc3a52fe98    21 GB    5 months ago"] if self.installed else []
        return "\n".join(["NAME    ID    SIZE    MODIFIED", *rows]) + "\n"


class _Client:
    """Replays the committed Tier-3 fixture for the image it is sent."""

    def __init__(self, ollama: _Ollama):
        self.ollama = ollama
        self.options: list[dict] = []

    def chat(self, **kwargs):
        self.options.append(kwargs["options"])
        self.ollama.loaded = [_LOADED]
        sha = hashlib.sha256(kwargs["messages"][0]["images"][0]).hexdigest()
        return eval_cache.load_cached(tier3.PROVIDER_NAME, sha)


def _committed_listing() -> list[str]:
    return sorted(str(p) for p in eval_cache.CACHE_ROOT.rglob("*.json"))


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--image-dir",
        str(CMS1500_VALIDATION_DIR),
        "--work-dir",
        str(tmp_path / "work"),
        "--output",
        str(tmp_path / "summary.json"),
        "--split",
        "test",
        "--limit",
        "3",
        *extra,
    ]


def test_stubbed_run_writes_the_summary_and_touches_no_cache(tmp_path):
    before = _committed_listing()
    ollama = _Ollama()
    clients: list[_Client] = []

    def factory(timeout):
        clients.append(_Client(ollama))
        return clients[-1]

    assert m0.main(_args(tmp_path), run=ollama, client_factory=factory) == m0.EXIT_OK
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == "ok", summary["error"]
    assert summary["docs"] == 3 and summary["num_ctx"] == 8192
    assert len(clients) == 1  # one client: Tier 3 loads once
    assert clients[0].options and all(o["num_ctx"] == 8192 for o in clients[0].options)
    assert summary["ollama"]["processor_loaded"] == "100% GPU"
    assert summary["ollama"]["version_before"] == "ollama version is 0.20.7"
    assert "ps_after" in summary["ollama"]
    full, narrow = summary["m0a_full"], summary["m0b_narrowed"]
    assert full["calls"] == 3 and full["tokens_per_s"]["mean"] > 0
    assert narrow["calls"] == summary["docs_sent_to_tier3"]
    # Same readings as the cache, so the scores match the cached replay and
    # every narrowed reading matches its full reading.
    assert full["by_stage_f1"] == summary["cached_replay"]["by_stage_f1"]
    assert summary["narrowed_vs_full"]["rate"] == 1.0
    assert _committed_listing() == before


def test_refuses_when_another_model_is_loaded(tmp_path):
    ollama = _Ollama(loaded=["gemma3:27b    a1b2c3    20 GB    100% GPU    4096    4 minutes"])
    factory_calls = []
    code = m0.main(_args(tmp_path), run=ollama, client_factory=factory_calls.append)
    assert code == m0.EXIT_REFUSED and not factory_calls
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == "refused" and "gemma3:27b" in summary["error"]


def test_refuses_when_the_model_is_not_installed_and_never_pulls(tmp_path):
    ollama = _Ollama(installed=False)
    factory_calls = []
    code = m0.main(_args(tmp_path), run=ollama, client_factory=factory_calls.append)
    assert code == m0.EXIT_REFUSED and not factory_calls
    assert not any("pull" in call for call in ollama.calls)


def test_refuses_a_work_dir_inside_the_repository(tmp_path):
    args = _args(tmp_path)
    args[args.index("--work-dir") + 1] = str(repo_root() / "m0-work")
    code = m0.main(args, run=_Ollama(), client_factory=lambda t: None)
    assert code == m0.EXIT_REFUSED
    assert not (repo_root() / "m0-work").exists()


def test_dry_run_makes_no_model_call(tmp_path):
    factory_calls = []
    code = m0.main(_args(tmp_path, "--dry-run"), run=_Ollama(), client_factory=factory_calls.append)
    assert code == m0.EXIT_OK and not factory_calls
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == "dry-run" and summary["cached_replay"]["by_stage_f1"]


def test_wall_clock_cap_fails_the_run(tmp_path):
    ollama = _Ollama()
    code = m0.main(
        _args(tmp_path, "--max-minutes", "0"), run=ollama, client_factory=lambda t: _Client(ollama)
    )
    assert code == m0.EXIT_FAILED
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == "failed" and "wall-clock cap" in summary["error"]


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        m0.main(["--help"])
    assert exc.value.code == 0
    assert "--image-dir" in capsys.readouterr().out


class _FailNarrowed(_Client):
    """Full reads work; every narrowed read times out."""

    def chat(self, **kwargs):
        if len(kwargs["format"]["properties"]) < 27:
            raise TimeoutError("narrowed call timed out")
        return super().chat(**kwargs)


def test_m0a_numbers_survive_a_failing_narrowed_pass(tmp_path):
    """M0a is summarized before M0b starts: a failure in M0b still leaves
    M0a's timing and F1 in the summary."""
    ollama = _Ollama()
    code = m0.main(_args(tmp_path), run=ollama, client_factory=lambda t: _FailNarrowed(ollama))
    assert code == m0.EXIT_FAILED
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == "failed" and "narrowed call timed out" in summary["error"]
    assert summary["m0a_full"]["calls"] == 3
    assert summary["m0a_full"]["by_stage_f1"] == summary["cached_replay"]["by_stage_f1"]
    assert "m0b_narrowed" not in summary


def test_run_unloads_the_model_and_records_gpu_memory(tmp_path):
    """The 32B is stopped at the end, after ps_after shows it loaded, and
    nvidia-smi's per-card lines are kept before, loaded and after."""
    ollama = _Ollama()
    code = m0.main(_args(tmp_path), run=ollama, client_factory=lambda t: _Client(ollama))
    assert code == m0.EXIT_OK
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["unloaded"] is True
    stop = ollama.calls.index(["ollama", "stop", "qwen2.5vl:32b"])
    last_ps = max(i for i, c in enumerate(ollama.calls) if c == ["ollama", "ps"])
    assert stop > last_ps and "qwen2.5vl:32b" in summary["ollama"]["ps_after"]
    gpu = summary["gpu_memory"]
    assert "0 MiB" in gpu["before"] and "15157 MiB" in gpu["loaded"] and "15157 MiB" in gpu["after"]
    assert ollama.loaded == []


def test_dry_run_never_stops_a_model(tmp_path):
    ollama = _Ollama()
    assert m0.main(_args(tmp_path, "--dry-run"), run=ollama, client_factory=print) == m0.EXIT_OK
    assert not any(c[:2] == ["ollama", "stop"] for c in ollama.calls)
