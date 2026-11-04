"""Optional live smoke test for the public Electrum protocol client."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.network
def test_public_electrum_live_smoke() -> None:
    if not os.environ.get("UTXOPROOF_LIVE_ELECTRUM"):
        pytest.skip("set UTXOPROOF_LIVE_ELECTRUM=1 to use a public Electrum server")
    # Fixed argv: this repo's script; the option is an environment gate only.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "electrum_live_check.py")],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "LIVE ELECTRUM OK" in proc.stdout
