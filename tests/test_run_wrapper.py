"""The v8 examples/run.sh is a thin wrapper around ``utxoproof run``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "input_v8"


def test_run_sh_is_thin_v8_wrapper(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "INPUT": str(EXAMPLE),
            "DATA_DIR": str(tmp_path / "data"),
            "OFFLINE": "1",
            "TAX_YEAR": "2023",
            "AS_OF": "2023-12-31",
            "UTXOPROOF_BIN": os.path.join(os.path.dirname(sys.executable), "utxoproof"),
        }
    )
    proc = subprocess.run(  # noqa: S603 - fixed repo script, controlled env
        ["bash", str(REPO / "examples" / "run.sh")],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "accounts: 9" in proc.stdout
    assert (tmp_path / "data" / "reports" / "2023" / "report.html").is_file()
    assert (tmp_path / "data" / "reports" / "overview" / "overview.html").is_file()
