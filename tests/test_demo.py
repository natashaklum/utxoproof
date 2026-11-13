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

    v8_overview = (out / "v8" / "overview" / "overview.html").read_text(encoding="utf-8")
    assert "synthetic v8 account workflow" in v8_overview
    assert "Ledger main" in v8_overview and "2.53230000" in v8_overview
    ledger = (out / "v8" / "entities" / "ledger-main.html").read_text(encoding="utf-8")
    assert "Cold storage UTXO" in ledger
    bisq = (out / "v8" / "exchange" / "bisq.html").read_text(encoding="utf-8")
    assert "Linked on-chain wallets" in bisq
    assert "0.03000000 BTC" in bisq
    assert "0.00730000 BTC" in bisq
    assert "not seen in linked wallet" not in bisq
    assert (out / "v8" / "entities" / "bisq-wallet-segwit.html").is_file()
    assert (out / "v8" / "bank" / "bank.html").is_file()
    assert (out / "v8" / "2023" / "report.html").is_file()
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "v8/overview/overview.html" in index
    assert "v8/entities/ledger-main.html" in index


def test_demo_builds_getting_started_guide(tmp_path: Path) -> None:
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.links: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for name, value in attrs:
                if name in ("href", "src") and value:
                    self.links.append(value)

    out = tmp_path / "site"
    # Fixed argv (this repo's demo builder + tmp out dir); no user input.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "build_demo.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    guide = out / "guide" / "index.html"
    assert guide.is_file()
    html = guide.read_text(encoding="utf-8")
    assert "From a shoebox of exports" in html
    assert "One owner. Many accounts. One ledger." in html
    assert "images/account-model.svg" in html
    assert (out / "guide" / "style.css").is_file()
    for asset in (
        "hero.svg",
        "account-model.svg",
        "pipeline.svg",
        "rollout.svg",
        "reports-map.svg",
    ):
        assert (out / "guide" / "images" / asset).is_file(), asset

    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="guide/index.html"' in index

    parser = LinkParser()
    parser.feed(html)
    checked = 0
    for link in parser.links:
        if link.startswith(("#", "http://", "https://", "mailto:", "data:", "javascript:")):
            continue
        path = link.split("#", 1)[0].split("?", 1)[0]
        if not path:
            continue
        assert (guide.parent / path).is_file(), f"dangling guide link: {link}"
        checked += 1
    assert checked >= 10


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
        # The landing page is not a report; usage.html and the hand-built guide
        # have their own navigation and are checked separately.
        if page.name in ("index.html", "usage.html") or page.parent.name == "guide":
            continue
        text = page.read_text(encoding="utf-8")
        nav = re.search(r'<nav class="site-nav">(.*?)</nav>', text, re.S)
        assert nav is not None, f"{page}: missing menu"
        for href in re.findall(r'href="([^"#]+)"', nav.group(1)):
            assert (page.parent / href).is_file(), f"{page}: dangling nav {href}"
            checked += 1
    assert checked > 0
