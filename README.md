# utxoproof

Bitcoin wealth management and compliance tool for Belgian self-custodians.
Self-hosted. Privacy-first. No data leaves your machine.

utxoproof answers three questions:

1. **What do I owe?** — Belgian personal income tax on Bitcoin disposals
   (goede huisvader 33% flat vs. speculator progressive), with a reproducible
   HTML report and evidence ZIP for SPF Finances.
2. **What should I do?** — per-UTXO advisory (sell, hold, borrow against,
   estate plan), flagging speculation taint, tax cost, and KYC complications.
3. **Where did this coin come from?** — full chain-of-custody provenance for
   any UTXO, EUR-valued at every step.

Browse the [v8 account-workflow demo](https://utxoproof.com/v8/overview/overview.html):
wallets, bank and exchange balances with allocation charts, drillable down to
each UTXO and its provenance chain. The [exchange review](https://utxoproof.com/v8/exchange/index.html)
shows Bisq trades, SEPA receipts, a manual old-exchange entry and the bank rows
that corroborate them. The demo runs offline against
`examples/input_v8` with deterministic seeded chain activity; all of the
older fixture reports remain in the [demo index](https://utxoproof.com/).

**New here with years of wallets, exchanges and statements?** Read the
[illustrated Getting Started guide](https://utxoproof.com/guide/):
inventory the sources, build one owner tree, pilot one slice, reconcile, then
scale. The deeper command reference remains in `docs/USAGE.md`.

Price history ships vendored (`utxoproof/data/btc_eur_daily.csv` from the first real
2010 print, `utxoproof/data/usd_eur_daily.csv` from 1999) — refresh with
`scripts/fetch_price_history.py`, seed caches via `EURPriceOracle.load_csv`.

## Status

Implemented and covered by tests + live regtest runs: the v8 account workflow
(one owner, per-account folders, exported descriptors including multisig,
BIP329 labels, Electrum primary / Bitcoin Core optional sync, discovery and
`utxoproof run`), exchange imports (Kraken ledgers + trades join incl.
margin/rollover, Coinbase, Binance, Bisq, plus manual `events.csv` for dead
exchanges), Bisq SEPA receipt matching, Belgian bank parsing and fiat-leg
review with visible unmatched warnings, EUR price oracle, Belgian classifier
with `utxoproof.toml`, HTML reports (tax, status, privacy, advisory,
provenance/saga, bank review, exchange review, all-time) with evidence ZIPs,
KYC propagation, all-parent provenance with persisted
bank↔exchange↔chain links and value-weighted coverage, and demo reports
published to <https://utxoproof.com/> on every `main` push.
`utxoproof-plan_v7.md` remains the tax/saga spec; the v8 input and account
workflow is introduced by the
[Getting Started guide](https://utxoproof.com/guide/) and documented in depth
in the [Slowstart reference](docs/USAGE.md).

## Installation

Requires Python 3.11+.

Debian-native (trust-maximal — everything from signed archives + hashes):

```bash
sudo apt install python3 python3-venv python3-pip git
git clone --recurse-submodules https://github.com/natashaklum/utxoproof
cd utxoproof
python3 -m venv .venv
.venv/bin/pip install "rp2 @ git+https://github.com/natashaklum/rp2.git@b1995bf1a07665035a46cf331cb06ccccd347ce2"
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install -e . --no-deps
mkdir -p ~/utxoproof-data/input
cp utxoproof.example.toml ~/utxoproof-data/input/utxoproof.toml  # edit rate/chain
```

Faster alternative with [uv](https://docs.astral.sh/uv/):

```bash
uv venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
```

See the [Slowstart guide](docs/USAGE.md) for the full discussion of both
paths (including why the git dependency installs separately).

Docker (mainnet stack per `docker-compose.yml`; regtest dev stack in
`docker-compose.regtest.yml`):

```bash
cp utxoproof.example.toml utxoproof.toml   # Docker bind-mount layout
mkdir -p sources output
docker compose run --rm utxoproof sync --rpc-url http://bitcoind:8332 ...
```

## Quickstart

Five minutes. For the twenty-minute version with concepts, config walkthrough
and troubleshooting, read the [Slowstart reference](docs/USAGE.md). If you are
bringing years of accounts, start with the [illustrated Getting Started
guide](https://utxoproof.com/guide/) instead.

The primary workflow is one owner with many accounts:

```text
input/
  utxoproof.toml
  accounts/<account-id>/account.toml   # wallet, bank, credit_card, exchange, broker
```

```bash
.venv/bin/utxoproof discover --input examples/input_v8
.venv/bin/utxoproof run --input examples/input_v8 --data-dir /tmp/utxoproof-demo --offline
.venv/bin/utxoproof run --input examples/input_v8 --data-dir /tmp/utxoproof-demo --plan
```

`run` imports labels/exchange/bank files, syncs wallet scripts through the
configured Electrum server (or Core with `[chain] backend = "core"`), builds the
tax report + evidence ZIP and the account/provenance dashboard (`--no-zip`
skips the bundle). Exchange accounts (`kraken`, `bisq`, `manual`) also get
`reports/exchange/` pages that link trades, SEPA receipts, bank rows and chain
transactions; unresolved items stay red instead of being guessed.
`examples/run.sh` is a thin environment-variable wrapper around it.

Offline CSV checks still work without an input tree:

```bash
.venv/bin/utxoproof compute --input tests/fixtures/manual_2023.csv --year 2023
.venv/bin/utxoproof status --input tests/fixtures/manual_2023.csv --price 40000
.venv/bin/utxoproof report --input tests/fixtures/manual_2023.csv --year 2023 --out /tmp/utxo-report
.venv/bin/python scripts/build_demo.py --out site  # legacy fixtures + v8 demo/index
```

Optional Core-only diagnosis (needs a node; regtest via
`docker-compose.regtest.yml`):

```bash
.venv/bin/utxoproof setup --rpc-url http://127.0.0.1:18443 --rpc-user utxoproof \
  --rpc-password utxoproof-test --wallet utxoproof_watchonly \
  --xpub <account-xpub> --fingerprint <master-fp>
.venv/bin/utxoproof sync --rpc-url http://127.0.0.1:18443 --rpc-user utxoproof \
  --rpc-password utxoproof-test --wallet utxoproof_watchonly
```

Checks (see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy utxoproof/ scripts/
.venv/bin/python -m pytest
```

## Layout

```text
utxoproof/
  belgian_tax.py  # 33% flat + communal surcharge, proportional fee-split
  cli.py          # discover/run + compute/report/status/privacy/advise/provenance/setup/sync
  db.py + schema.sql  # Sec. 8 SQLite schema + accounts/scripts/labels (v8)
  accounts.py     # v8 per-folder manifests, descriptor registry, attribution
  labels.py       # BIP329 import, account scope, user-label precedence
  descriptors.py  # descriptors (multisig/multipath) + address/script derivation
  electrum.py + electrum_sync.py  # primary chain backend: protocol + graph sync
  bitcoin_rpc.py + onchain.py  # optional Core JSON-RPC, descriptor import, sync
  runner.py       # discover/run orchestration and report/site generation
  exchange.py + kraken_csv.py (ledgers + trades join, margin/rollover,
    spend/receive, adjustments, earn subtypes; subclass/wallet captured)
    / coinbase_csv.py / binance_csv.py / bisq_csv.py / manual_exchange.py
  bisq_receipts.py  # SEPA receipt filename parsing + trade/bank matching
  banks.py + matching.py  # Belgian bank profiles, fiat-leg matcher
  price_oracle.py  # Kraken OHLC + CoinGecko + ECB, SQLite cache
  classifier.py + config.py  # Sec. 6 scores, utxoproof.toml
  portfolio.py + reports.py  # account dashboard, tax/status/provenance/bank/
                             # exchange review pages
  kyc.py + advisory.py + provenance.py + saga.py + evidence.py
tests/
  fixtures/  # synthetic only, hand-verified, never real data
```

## rp2 fork

Tax computation reuses [rp2](https://github.com/eprbell/rp2) via our fork
[`natashaklum/rp2`](https://github.com/natashaklum/rp2), which adds the Belgian
`BE` country plugin (`rp2_be`). The plan's full spec is `utxoproof-plan_v7.md`.

## License

See [LICENSE](LICENSE). This tool is a computational aid, not professional tax
advice — verify all figures with a certified Belgian tax adviser before filing.
