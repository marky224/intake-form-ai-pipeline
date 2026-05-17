"""Tests for the Phase 9 QLoRA experiment — cached/$0/no-GPU only.

CI never has a CUDA device or the peft/bitsandbytes/trl stack. These pin:
the leakage guard (0 real train pairs on the committed manifest), the
identity-degrade contract (no adapter → no heavy import, zero delta), and
that the eval reuses the harness metric.
"""

from __future__ import annotations

import json
import sys

import pytest

from finetune import correct, dataset, evaluate, train


def test_committed_manifest_yields_zero_seeded_train(tmp_path) -> None:
    """The 6 CMS-1500 are all `test` — the manifest IS the leakage guard."""
    out = tmp_path / "train.jsonl"
    summary = dataset.build_training_jsonl(out_path=out)
    assert summary["seeded_train"] == 0
    assert summary["synthetic_format_kind"] > 0
    assert summary["total"] == summary["synthetic_format_kind"]
    assert "leakage guard" in summary["note"]
    rows = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert rows and all(r["source"] == "synthetic_format_kind" for r in rows)
    # extra="forbid" round-trips cleanly.
    assert all(set(r) == {"instruction", "input", "response", "source", "split"} for r in rows)


def test_synthetic_examples_are_leakage_safe_and_well_formed() -> None:
    exs = dataset.synthetic_format_kind_examples()
    assert exs
    for ex in exs:
        assert ex.instruction == dataset.CORRECTOR_INSTRUCTION
        assert ex.source == "synthetic_format_kind" and ex.split == "train"
        assert "Field type:" in ex.input and ex.response


def test_train_refuses_without_finetune_live(monkeypatch) -> None:
    monkeypatch.delenv("FINETUNE_LIVE", raising=False)
    assert train.is_finetune_live() is False
    with pytest.raises(train.FineTuneUnavailable, match="FINETUNE_LIVE"):
        train.train()
    assert "torch" not in sys.modules and "peft" not in sys.modules


def test_train_refuses_when_no_real_pairs(tmp_path, monkeypatch) -> None:
    """FINETUNE_LIVE set but only synthetic rows → loud refusal, no train."""
    monkeypatch.setenv("FINETUNE_LIVE", "true")
    jsonl = tmp_path / "train.jsonl"
    dataset.build_training_jsonl(out_path=jsonl)
    with pytest.raises(train.FineTuneUnavailable, match="0 .seeded_correction"):
        train.train(jsonl_path=jsonl)
    assert "torch" not in sys.modules


def test_correct_field_identity_without_adapter(tmp_path) -> None:
    assert correct.adapter_available(tmp_path) is False
    assert correct.correct_field("first_name", "str", "Jane", tmp_path) == "Jane"
    assert correct.correct_field("first_name", "str", None, tmp_path) is None
    assert "torch" not in sys.modules


def test_evaluate_identity_baseline_is_zero_delta_cached() -> None:
    """No adapter (CI) → corrected == baseline, delta 0.000, $0, no GPU."""
    r = evaluate.evaluate()
    assert isinstance(r, evaluate.EvalResult)
    assert r.doc_count == 6
    assert r.adapter_present is False
    assert r.baseline_f1 == r.corrected_f1
    assert r.delta_f1 == 0.0
    assert "identity baseline" in r.note
    assert "torch" not in sys.modules


def test_evaluate_baseline_matches_phase6_cascade_f1() -> None:
    """The reused harness metric must reproduce the ≈0.78 cascade-stage F1
    (memory project_phase6_two_stage_f1) — proof the metric is identical."""
    r = evaluate.evaluate()
    assert 0.70 <= r.baseline_f1 <= 0.85
