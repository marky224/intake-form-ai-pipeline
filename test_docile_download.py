"""Tests for the DocILE download wrapper.

Covers the verification + guard rails — the actual curl-from-S3 path
isn't exercised here (mocked out) since real downloads need a live
token and ~1.6 GB of dataset traffic.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from synthetic_data.docile.download import (
    BUILD_DATASET,
    TOKEN_ENV_VAR,
    VENDORED_SCRIPT_PATH,
    VENDORED_SCRIPT_SHA256,
    DocileScriptMismatchError,
    DocileTokenMissingError,
    annotations_dir_is_populated,
    download_labeled_trainval,
    main,
    resolve_token,
    verify_vendored_script,
)


def test_vendored_script_present() -> None:
    """The pinned script must ship alongside the wrapper."""
    assert VENDORED_SCRIPT_PATH.is_file()


def test_vendored_script_sha256_matches_pinned() -> None:
    """Live sha256 of the on-disk script equals the pinned constant.

    Locks down any accidental edit to ``download_dataset.sh``. Re-vendor
    from the pinned upstream commit + bump ``VENDORED_SCRIPT_SHA256``
    together when intentionally upgrading.
    """
    actual = hashlib.sha256(VENDORED_SCRIPT_PATH.read_bytes()).hexdigest()
    assert actual == VENDORED_SCRIPT_SHA256


def test_verify_vendored_script_passes_on_match() -> None:
    """``verify_vendored_script`` returns silently when the on-disk file matches."""
    verify_vendored_script()


def test_verify_vendored_script_raises_on_drift(tmp_path: Path) -> None:
    """A drifted (edited or corrupted) script aborts before any execution."""
    drifted = tmp_path / "download_dataset.sh"
    drifted.write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    with pytest.raises(DocileScriptMismatchError, match="does not match"):
        verify_vendored_script(drifted)


def test_verify_vendored_script_raises_on_missing_file(tmp_path: Path) -> None:
    """A missing vendored script surfaces as DocileScriptMismatchError, not FileNotFoundError.

    Lets the CLI map both drift and absence to the same exit code; a
    raw ``FileNotFoundError`` would skip the wrapper's error handler.
    """
    missing = tmp_path / "does-not-exist.sh"
    with pytest.raises(DocileScriptMismatchError, match="not found"):
        verify_vendored_script(missing)


def test_resolve_token_returns_env_value() -> None:
    """Token comes from the injected env dict, not the real os.environ."""
    assert resolve_token(env={TOKEN_ENV_VAR: "abc123"}) == "abc123"


def test_resolve_token_raises_when_missing() -> None:
    """Empty/unset env raises with a clear remediation message."""
    with pytest.raises(DocileTokenMissingError, match="docile.rossum.ai"):
        resolve_token(env={})


def test_resolve_token_raises_when_empty_string() -> None:
    """An empty-string env var is treated as unset."""
    with pytest.raises(DocileTokenMissingError):
        resolve_token(env={TOKEN_ENV_VAR: ""})


def test_annotations_dir_is_populated_false_when_missing(tmp_path: Path) -> None:
    """Empty dir → not populated."""
    assert annotations_dir_is_populated(tmp_path) is False


def test_annotations_dir_is_populated_false_when_dir_exists_empty(tmp_path: Path) -> None:
    """``annotations/`` exists but contains no JSON → not populated."""
    (tmp_path / "annotations").mkdir()
    assert annotations_dir_is_populated(tmp_path) is False


def _make_populated(tmp_path: Path) -> None:
    """Materialize the three-signal complete-extraction shape used by the idempotency check."""
    (tmp_path / "annotations").mkdir()
    (tmp_path / "annotations" / "abc.json").write_text("{}", encoding="utf-8")
    (tmp_path / "train.json").write_text("[]", encoding="utf-8")
    (tmp_path / "val.json").write_text("[]", encoding="utf-8")


def test_annotations_dir_is_populated_true_when_all_signals_present(tmp_path: Path) -> None:
    """annotations/ + train.json + val.json all present → populated."""
    _make_populated(tmp_path)
    assert annotations_dir_is_populated(tmp_path) is True


def test_annotations_dir_is_populated_false_when_train_index_missing(tmp_path: Path) -> None:
    """Missing train.json (e.g., unzip aborted before writing it) → not populated."""
    _make_populated(tmp_path)
    (tmp_path / "train.json").unlink()
    assert annotations_dir_is_populated(tmp_path) is False


def test_annotations_dir_is_populated_false_when_val_index_missing(tmp_path: Path) -> None:
    """Missing val.json → not populated."""
    _make_populated(tmp_path)
    (tmp_path / "val.json").unlink()
    assert annotations_dir_is_populated(tmp_path) is False


def test_download_rejects_test_split(tmp_path: Path) -> None:
    """``test`` is reserved for Phase 7; download wrapper refuses it."""
    with pytest.raises(ValueError, match="process-batch"):
        download_labeled_trainval(
            tmp_path,
            dataset="test",
            env={TOKEN_ENV_VAR: "tok"},
        )


def test_download_rejects_synthetic_split(tmp_path: Path) -> None:
    """``synthetic`` is reserved; download wrapper refuses it."""
    with pytest.raises(ValueError, match="process-batch"):
        download_labeled_trainval(
            tmp_path,
            dataset="synthetic",
            env={TOKEN_ENV_VAR: "tok"},
        )


def test_download_rejects_unlabeled_split(tmp_path: Path) -> None:
    """``unlabeled`` is reserved; download wrapper refuses it."""
    with pytest.raises(ValueError, match="process-batch"):
        download_labeled_trainval(
            tmp_path,
            dataset="unlabeled",
            env={TOKEN_ENV_VAR: "tok"},
        )


def test_download_invokes_script_with_expected_args(tmp_path: Path) -> None:
    """Happy path: vendored script is invoked with the right argv shape."""
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(argv)
        # Simulate a successful extraction by writing one annotation file
        # plus the train/val split indexes the idempotency check expects.
        ann_dir = tmp_path / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)
        (ann_dir / "doc-001.json").write_text("{}", encoding="utf-8")
        (tmp_path / "train.json").write_text("[]", encoding="utf-8")
        (tmp_path / "val.json").write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = download_labeled_trainval(
        tmp_path,
        env={TOKEN_ENV_VAR: "secret-tok"},
        runner=fake_runner,
    )

    assert result == tmp_path
    assert len(calls) == 1
    argv = calls[0]
    # bash <script> <token> <dataset> <dir> --unzip
    assert argv[0] == "bash"
    assert argv[1] == str(VENDORED_SCRIPT_PATH)
    assert argv[2] == "secret-tok"
    assert argv[3] == BUILD_DATASET
    assert argv[4] == str(tmp_path)
    assert argv[5] == "--unzip"


def test_download_redacts_token_from_subprocess_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The vendored script echoes the token-bearing URL; the wrapper redacts it."""
    token = "secret-tok-abc123"

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        _make_populated(tmp_path)
        # Mirror the real script's stdout shape: one Downloading line + one Unzipping line.
        script_stdout = (
            f"Downloading https://docile-dataset-rossum.s3.eu-west-1.amazonaws.com/"
            f"{token}/labeled-trainval.zip\n"
            f"Unzipping labeled-trainval.zip\n"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=script_stdout, stderr=None)

    download_labeled_trainval(
        tmp_path,
        env={TOKEN_ENV_VAR: token},
        runner=fake_runner,
    )

    out = capsys.readouterr().out
    assert token not in out, f"token leaked into stdout: {out!r}"
    assert "<TOKEN-REDACTED>" in out


def test_download_skip_if_present_avoids_runner(tmp_path: Path) -> None:
    """Pre-populated dataset root → wrapper short-circuits without calling the script."""
    _make_populated(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    result = download_labeled_trainval(
        tmp_path,
        env={TOKEN_ENV_VAR: "tok"},
        runner=fake_runner,
    )

    assert result == tmp_path
    assert calls == []


def test_download_force_redownload_invokes_runner_even_when_populated(tmp_path: Path) -> None:
    """``skip_if_present=False`` re-runs the download even with annotations present."""
    _make_populated(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    download_labeled_trainval(
        tmp_path,
        env={TOKEN_ENV_VAR: "tok"},
        skip_if_present=False,
        runner=fake_runner,
    )

    assert len(calls) == 1


def test_download_creates_dest_dir(tmp_path: Path) -> None:
    """Non-existent dest is created before invoking the script."""
    dest = tmp_path / "new" / "nested" / "docile"

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        ann_dir = dest / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)
        (ann_dir / "x.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    download_labeled_trainval(
        dest,
        env={TOKEN_ENV_VAR: "tok"},
        runner=fake_runner,
    )

    assert dest.is_dir()


def test_main_returns_1_when_token_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI exit code 1 surfaces a missing token to the calling shell."""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    rc = main(["--dest", str(tmp_path)])
    assert rc == 1


def test_main_returns_2_when_script_drifted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI exit code 2 surfaces script drift before any network activity."""

    def drift(_path: Path = VENDORED_SCRIPT_PATH) -> None:
        raise DocileScriptMismatchError("drift")

    monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
    monkeypatch.setattr("synthetic_data.docile.download.verify_vendored_script", drift)
    rc = main(["--dest", str(tmp_path)])
    assert rc == 2


def test_main_returns_4_on_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero exit from the vendored script surfaces as CLI exit 4."""
    import subprocess as sp

    def failing_runner(argv: list[str], **kwargs: object) -> sp.CompletedProcess:
        raise sp.CalledProcessError(returncode=22, cmd=argv)

    monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
    monkeypatch.setattr("synthetic_data.docile.download.subprocess.run", failing_runner)
    rc = main(["--dest", str(tmp_path)])
    assert rc == 4
    assert "exit 22" in capsys.readouterr().err
