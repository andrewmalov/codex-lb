"""CLI-level tests for the contract validator.

Run the actual CLI as a subprocess and assert on exit code and stderr.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI = REPO_ROOT / "openspec" / "process" / "scripts" / "validate_contracts.py"


def _run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_passes_when_all_contracts_present() -> None:
    proc = _run()
    assert proc.returncode == 0, proc.stderr
    assert "Validated 5 contracts" in proc.stdout


def test_cli_fails_when_a_contract_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Back up and corrupt feature.yaml, then restore.
    target = REPO_ROOT / "openspec" / "process" / "contracts" / "feature.yaml"
    backup = target.read_text(encoding="utf-8")
    target.write_text("name: feature\ntrigger: not-a-valid-trigger\n", encoding="utf-8")
    try:
        proc = _run()
        assert proc.returncode != 0
        assert "feature.yaml" in proc.stderr
    finally:
        target.write_text(backup, encoding="utf-8")
