# AGENTS.md — utxoproof

> **Self-maintenance rule (binding).** Any coding agent that changes this repo's
> layout, toolchain, conventions, or multi-repo workflow MUST update this file
> in the same change. A change that alters how agents should work here but leaves
> this file stale is incomplete. Keep it short: facts and commands, no essays.
> This rule applies to itself — if the rule stops working, fix the rule.

## What this is

utxoproof: Bitcoin wealth management + Belgian tax compliance for self-custodians.
Full spec: `utxoproof-plan_v5.md`. Two repos:

- This repo (`utxoproof`): the tool. Python 3.11+, `utxoproof/` package.
- [`natashaklum/rp2`](https://github.com/natashaklum/rp2) fork: rp2 + Belgian `BE`
  country plugin. This repo depends on it (pinned SHA in `pyproject.toml`, kept in
  lockstep with the `vendor/rp2` submodule pointer).
  rp2-side changes (country plugin, templates) go there, not here.
- Local checkouts: this repo embeds the fork as the `vendor/rp2` git submodule
  (never clone rp2 separately; `/tmp` is wiped on restart anyway).
  Iterating on both: `VIRTUAL_ENV=.venv uv pip install -e vendor/rp2`, run rp2
  checks from the submodule (`cd vendor/rp2 && ../.venv/bin/python -m pytest
  tests/test_plugin_country_be.py`), then commit+push in `vendor/rp2` first,
  `git add vendor/rp2` here, and bump the pinned SHA in `pyproject.toml`,
  `requirements*.txt` (+ headers), `README.md` and `docs/USAGE.md`
  (grep for the old SHA to catch them all), then regenerate the requirements
  files if dependencies changed.

## Commands

```bash
uv venv                                            # one-time
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/utxoproof compute --input tests/fixtures/manual_2023.csv --year 2023
.venv/bin/utxoproof status --input tests/fixtures/manual_2023.csv --price 40000
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy utxoproof/ scripts/               # strict, must be clean
.venv/bin/python -m pytest                         # must pass
.venv/bin/python scripts/build_demo.py --out site  # demo HTML reports
```

No system pip, no `cc` on dev machines: keep runtime deps pure-Python.
DaLI is intentionally absent until its sprints (needs a C toolchain).
`embit` is allowed (pure-python wheel, no compiler needed).

## Conventions

- Money: `Decimal` only, never float. Tax math lives in `utxoproof/belgian_tax.py`.
- Chain crypto (xpub parsing, address derivation) goes through `utxoproof/descriptors.py`
  (embit); new vectors must be spec vectors, hand-verified before pinning.
- Bitcoin Core access goes through `utxoproof/bitcoin_rpc.py` (httpx JSON-RPC) and
  `utxoproof/onchain.py` (tx-graph sync). `setup --timestamp` defaults to `now`
  (no rescan); `RESCAN_FROM=0` in `run.sh` rescans history for funded wallets.
  Outputs carry `is_mine` (default ours; `getaddressinfo` proves external at
  sync, incl. a backfill pass); totals/advisory/portfolio count only ours, and
  `utxoproof reconcile` diffs the DB against node balances.
  Daemon-backed tests are marked
  `regtest` and skip without a node; CI `regtest` job starts one via docker.
- KYC lives in `utxoproof/kyc.py` (proportional propagation, mixing detection);
  `utxoproof privacy` renders it. Shared sample graph: `create_sample_graph()`.
- Advisory lives in `utxoproof/advisory.py` (Sec. 13 flag rules, estate score);
  cost basis is acquisition-date close (rp2 lot integration pending).
  `utxoproof advise --db --price --as-of [--out]`; speculation taint defaults
  off until the Sprint 7 classifier feeds it.
- Provenance lives in `utxoproof/provenance.py` (backward graph walk, largest
  parent, cycle guard, depth cap); `utxoproof provenance txid:vout --db
  [--price/--as-of/--depth/--out]`. Unrealized gain is omitted from the header
  (needs lot matching); first-traced value shown instead.
- Evidence lives in `utxoproof/evidence.py` (SHA-256 manifest, content dedupe,
  ZIP bundle). `utxoproof report --year Y [--source F]...` builds it unless
  `--no-zip`; `report --alltime` writes the per-year summary (no ZIP).
  Human artifacts go through `utxoproof attach` into `evidence/<year>/`
  (hash + tx link in `evidence_links`); provenance appends them with
  thumbnails for small images, links otherwise.
- One data dir: `utxoproof/paths.py` (`--data-dir` > `$UTXOPROOF_DATA_DIR` >
  `~/.utxoproof`); `--db`/`--evidence-dir` override per call.
- Runnable workflows live in `examples/` (single self-contained `run.sh`,
  `MODE=prepare` stops on shortfalls unless `ALLOW_SHORTFALLS=1`,
  `MODE=refresh` idempotent, `TAX_YEAR` empty covers every data year, incl.
  `setup`/`sync`/`portfolio` on-chain stages); covered by
  `tests/test_run_scripts.py` driving it against fixtures.
- Entities for the overview come from a TOML file (`examples/entities.example.toml`;
  `load_entities` + `portfolio_from_db` in `utxoproof/portfolio.py`, surfaced via
  `utxoproof portfolio` and `report --full --entities`); each unspent output
  lands in the entity whose `wallet` matches one of its `wallet_label`
  attributions (sync stamps every wallet that lists the tx; legacy unlabeled
  rows use the old single-owner rule; unmapped wallets raise). `[[fiat]]`
  carries optional `as_of`/`statements`, audited by `utxoproof fiat-check`
  (freshness + evidence, warn-only `run.sh` stage); `WALLETS="name:fp:xpub|…"`
  loops setup/sync/reconcile, legacy XPUB vars synthesize one entry.
- Exchange funding links live in `utxoproof/linking.py` (exact sats + date
  window first, fee-skim tolerance second with ~Δ labels; ambiguous/unmatched
  at either tier reported, never guessed) and
  surface as provenance-step evidence (`--ledgers` on `portfolio`/`provenance`/
  `report`); `funding_trail()` extends funded receipts with candidate buys +
  bank matches (`--bank-csv`/`--bank-type`) rendered as a prologue, never fact.
  Depth-cap truncation is rendered as a note, not silent.
- Dashboard lives in `utxoproof/portfolio.py` (entities, rollups, inline SVG
  charts — no JavaScript). Overview -> entity pages -> provenance pages;
  chart HTML is trusted generator output (`| safe` in templates, everything
  else stays autoescaped). KYC colors are fixed per status (canonical order);
  entity bars use dominant-KYC color. Tag model in `utxoproof/tags.py`
  (behavior/channel axes + user tags from entities.toml); donuts take explicit
  palettes, one donut per exclusive axis; bank page has a matched/unmatched donut.
- Price history: `utxoproof/data/btc_eur_daily.csv` (2010-08-18 first real print ->
  now; Kraken native, blockchain.info x ECB before) and
  `utxoproof/data/usd_eur_daily.csv` (ECB 1999 -> now), plus raw upstream
  `utxoproof/data/btc_usd_daily.csv` for offline re-stitching. Refresh via
  `scripts/fetch_price_history.py` (extends the 2024 seam pattern: BCI covers
  what Kraken retention lacks; regens add dates only, shipped rows win; the
  script refuses gappy output). Seed caches with
  `EURPriceOracle.load_csv` (existing rows win). All default price paths are
  offline (bundled CSV; stale-noted past coverage; strict miss outside it);
  the live Kraken/CoinGecko oracle runs only with `--live-prices`.
- Packaging: `Dockerfile` (+`.dockerignore`), `docker-compose.yml` (mainnet)
  and `docker-compose.regtest.yml` (dev). CI `docker` job builds + smoke-tests
  the image (no docker on dev boxes, so image builds are CI-only).
- Classification lives in `utxoproof/classifier.py` (Sec. 6 scores) with signals
  from `signals_from_csv`; config in `utxoproof/config.py` (`utxoproof.toml`,
  example at `utxoproof.example.toml`). `utxoproof report --config` applies
  communal rate + classifier flags. Progressive speculator rates are NOT
  computed (needs annual bracket table); the report says so.
- Exchanges: `kraken_csv` / `coinbase_csv` / `binance_csv` / `bisq_csv` yield
  `exchange.ExchangeTx` (Bisq defaults non_kyc). Banks: `banks.py` profile
  engine (headers UNVERIFIED, alias-driven); `matching.py` fiat-leg matcher
  (two passes: exact euro equality, then fee/FX tolerance labeled approximate;
  review-only). Bank ids are explicit
  (`kbc-bankaccount`, `kbc-creditcard`; `kbc` kept as alias): card exports get
  their own profile (split credit/debit, merchant iso IBAN, settlement date),
  card numbers/holders never ingested, non-EUR rows without a rate abort. Bank rows are evidence, never
  pool input; `run.sh` converts via `BANK_CSV`/`BANK_TYPE` into bank-rows.csv +
  report `--source`; demo bank page + `kbc-checking` entity via `write_bank_page`.
  All non-Kraken shapes are best-effort fixtures for later correction.
- Deposits enter the moving-average pool at stated or receipt-date price
  (`--price-history` CSV, else a loud error — never silent zero).
  `utxoproof check` diagnoses pool coverage without crashing.
- Regtest stack: `docker-compose.regtest.yml` (bitcoind only; no docker on dev
  boxes, so daemon runs are CI-only).
- SQLite schema (`utxoproof/schema.sql`) is created whole (Sec. 8); schema changes
  need a migration note + `tests/test_schema.py` update.
- `tests/fixtures/*.csv` are hand-verified: recompute expected values
  independently before pinning them in tests.
- `S101` (asserts) is ignored for `tests/**` in `pyproject.toml`; everything else
  must satisfy `ruff check`, `ruff format`, and strict `mypy`.
- Sprint deliverables follow `utxoproof-plan_v5.md` Sec. 20; flag spec-vs-reality
  gaps (e.g. rp2's `AbstractCountry` has no `apply_tax` hook) instead of forcing
  the spec's shape.

## Branches

Single branch: `main` (default). No `master`. Commit as
`natashaklum <natashaklum@users.noreply.github.com>`.
