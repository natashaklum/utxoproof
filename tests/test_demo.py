"""Demo site build (offline fixtures, deterministic output)."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_demo_builds_bank_page_and_kbc_entity(tmp_path: Path) -> None:
    out = tmp_path / "site"
    # Fixed argv (this repo's demo builder + tmp out dir); no user input.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "build_demo.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    bank = out / "bank" / "bank.html"
    assert bank.is_file()
    html = bank.read_text(encoding="utf-8")
    assert "BINANCE storting" in html and "11,000.00" in html
    kbc = out / "entities" / "kbc-checking.html"
    assert kbc.is_file()
    assert "not a balance" in kbc.read_text(encoding="utf-8")
    overview = (out / "overview" / "overview.html").read_text(encoding="utf-8")
    assert "kbc-checking" in overview


def test_demo_nav_links_all_resolve(tmp_path: Path) -> None:
    """Guard against menu drift: every nav href in the demo must hit a file."""
    import re

    out = tmp_path / "site"
    # Fixed argv (this repo's demo builder + tmp out dir); no user input.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "build_demo.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    checked = 0
    for page in sorted(out.rglob("*.html")):
        if page.name in ("index.html", "usage.html"):
            continue
        text = page.read_text(encoding="utf-8")
        nav = re.search(r'<nav class="site-nav">(.*?)</nav>', text, re.S)
        assert nav is not None, f"{page}: missing menu"
        for href in re.findall(r'href="([^"#]+)"', nav.group(1)):
            assert (page.parent / href).is_file(), f"{page}: dangling nav {href}"
            checked += 1
    assert checked > 0
