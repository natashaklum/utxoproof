# AGENTS.md — utxoproof

> **Self-maintenance rule (binding).** Any coding agent that changes this repo's
> layout, toolchain, conventions, or multi-repo workflow MUST update this file
> in the same change. A change that alters how agents should work here but leaves
> this file stale is incomplete. Keep it short: facts and commands, no essays.
> This rule applies to itself — if the rule stops working, fix the rule.

## What this is

utxoproof: Bitcoin wealth management + Belgian tax compliance for self-custodians.
v8 account-centric input is the primary workflow; `utxoproof-plan_v7.md` remains
the tax/saga spec (§14). Two repos:

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
.venv/bin/utxoproof discover --input examples/input_v8
.venv/bin/utxoproof run --input examples/input_v8 --data-dir /tmp/utxoproof-demo --offline
.venv/bin/utxoproof compute --input tests/fixtures/manual_2023.csv --year 2023
.venv/bin/utxoproof status --input tests/fixtures/manual_2023.csv --price 40000
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy utxoproof/ scripts/               # strict, must be clean
.venv/bin/python -m pytest                         # must pass
.venv/bin/python scripts/build_demo.py --out site  # legacy + seeded v8 demos
.venv/bin/python scripts/electrum_live_check.py    # optional public-server smoke
```

No system pip, no `cc` on dev machines: keep runtime deps pure-Python.
DaLI is intentionally absent until its sprints (needs a C toolchain).
`embit` is allowed (pure-python wheel, no compiler needed).

## Conventions

- Money: `Decimal` only, never float. Tax math lives in `utxoproof/belgian_tax.py`.
- One owner, many accounts. `input/utxoproof.toml` + `input/accounts/<id>/account.toml`
  is the v8 source of truth (`utxoproof/accounts.py`); account kinds are
  `wallet`, `bank`, `credit_card`, `exchange`, `broker`. All metadata and
  type-specific files stay in the account folder; no central account list.
  Generated DB/reports stay in `--data-dir`, never in `input/`.
- Chain crypto (descriptor parsing, address/script derivation) goes through
  `utxoproof/descriptors.py` (embit); new vectors must be spec vectors,
  hand-verified before pinning. Exported descriptors (multisig included) are
  canonical; xpub shorthand is accepted for single-sig. Ownership is per output:
  `accounts.persist_registry` fills `account_scripts`, and
  `tx_outputs.account_id` drives portfolio/advisory attribution. Address labels
  never establish ownership. Overlapping scripts across accounts are hard errors.
- Electrum is the primary chain backend: `utxoproof/electrum.py` (pure-Python
  protocol) + `utxoproof/electrum_sync.py` (batched histories, raw-tx/header
  fetch, gap extension, per-output attribution). Configure `[chain]` in
  `utxoproof.toml` (`electrum_url`, optional `electrum_ca_file`).
- Bitcoin Core remains optional: `utxoproof/bitcoin_rpc.py` + `utxoproof/onchain.py`
  (descriptor import, paginated `listtransactions`, per-output `account_scripts`
  attribution). `setup --timestamp` defaults to `now` (no rescan);
  `RESCAN_FROM=0` in `run_legacy.sh` rescans history. `reconcile` diffs DB-owned
  unspent outputs against Electrum (`reconcile --input`) or Core balances.
  Daemon-backed tests are marked `regtest` and skip without a node; CI `regtest`
  job starts one via docker.
- BIP329 labels are first-class `labels` rows (`utxoproof/labels.py`), scoped by
  account, with source file/line and `origin` (`bip329`/`user`). Import replaces
  imported labels only; user labels survive. `utxoproof label` sets/deletes
  user labels. Labels are display metadata, never tags or ownership proofs;
  account and provenance pages show them.
- Workflow: `utxoproof discover --input <tree> [--json] [--online]` validates
  manifests/descriptors/collisions/parsers/backend without writing input;
  `utxoproof run --input <tree> --data-dir <data> [--plan] [--only ...] [--year Y] [--offline]`
  runs registry+labels+sync/imports+reports+portfolio. Low-level
  commands remain for diagnosis (`portfolio --input`, `reconcile --input`,
  `setup`/`sync`). `examples/run.sh` is a thin v8 wrapper; the old self-contained
  v7 script is `examples/run_legacy.sh` and owns `MODE=prepare/refresh/clean`,
  `WALLETS`, `BANK_CSV` and fixture tests.
- Menus: v8 `run` uses `account_site_pages` (`overview/`, `bank/`, year reports);
  the legacy `run_legacy.sh` layout keeps `run_site_pages`
  (`portfolio/overview/`, `current-status/`). Don't cross-wire them.
- Demos: `scripts/build_demo.py --out site` renders legacy fixture pages plus an
  offline, seeded v8 run under `site/v8/` (GitHub Pages workflow on `main`,
  published at <https://utxoproof.com/>).
  The illustrated high-volume onboarding guide is static HTML/CSS/SVG in
  `docs/guide/` (no build dependency); the demo builder copies it verbatim to
  `site/guide/` and links it from the landing page. Keep its commands and
  diagrams in lockstep with the runner flags.
  `scripts/electrum_live_check.py` is the optional public Electrum smoke test
  (BIP84 public test vector; `UTXOPROOF_LIVE_ELECTRUM=1` for the pytest mark,
  or run the manual `Live Electrum smoke` workflow).
- KYC lives in `utxoproof/kyc.py` (proportional propagation, mixing detection);
  `utxoproof privacy` renders it. Shared sample graph: `create_sample_graph()`.
- Advisory lives in `utxoproof/advisory.py` (Sec. 13 flag rules, estate score);
  cost basis is acquisition-date close (rp2 lot integration pending).
  `utxoproof advise --db --price --as-of [--out]`; speculation taint defaults
  off until the Sprint 7 classifier feeds it.
- Provenance lives in `utxoproof/provenance.py`: `build_saga` is the shipped
  all-parent DAG with proportional shares, fee/truncation accounting, and
  source detection; `build_provenance_chain` remains the legacy largest-parent
  walk used by funding-label tests. `utxoproof provenance txid:vout --db
  [--price/--as-of/--depth/--max-nodes/--out]` renders the saga. Unrealized
  gain is omitted from the header (needs lot matching); first-traced value
  shown instead.
- Cross-domain saga lives in `utxoproof/saga.py`: `ProvenanceLink`,
  `build_saga_document`, deterministic bank/EUR-deposit/trade/withdrawal/chain
  matchers, and value-weighted `saga_coverage`. Links persist in
  `provenance_links`; derived rebuilds are idempotent and never overwrite
  `origin='user'` confirmations/rejections. Tiers are exact / approximate /
  allocated / candidate; candidates never raise coverage. `--ledgers` +
  `--bank-csv`/`--bank-list` on `portfolio`/`provenance`/`report --full`
  rebuild links; parse failure clears derived links (user rows stay).
  `parse_kraken_fiat_flows` supplies EUR deposits/withdrawals (the BTC parser
  intentionally drops them).
- Evidence lives in `utxoproof/evidence.py` (SHA-256 manifest, content dedupe,
  ZIP bundle). `utxoproof run` builds the year ZIP from account-folder sources
  (root `utxoproof.toml` is excluded: it may hold RPC credentials) unless
  `--no-zip`; `utxoproof report --year Y [--source F]...` builds one too.
  `report --alltime` writes the per-year summary (no ZIP).
  Human artifacts go through `utxoproof attach` into `evidence/<year>/`
  (hash + tx link in `evidence_links`); provenance appends them with
  thumbnails for small images, links otherwise.
- One data dir: `utxoproof/paths.py` (`--data-dir` > `$UTXOPROOF_DATA_DIR` >
  `~/.utxoproof`); `--db`/`--evidence-dir` override per call.
- v8 examples live in `examples/input_v8/`; `tests/test_runner.py` covers
  discovery/plan/offline run and `tests/test_run_wrapper.py` covers the thin
  `run.sh`. Legacy `run_legacy.sh` is covered by `tests/test_run_scripts.py`
  (`MODE=prepare` stops on shortfalls unless `ALLOW_SHORTFALLS=1`,
  `MODE=refresh` idempotent, `MODE=clean` lists generated artifacts and deletes
  only with `CONFIRM=1`; `WALLETS`, `BANK_CSV` and fixture end-to-end runs).
- Legacy entities remain only for low-level compatibility:
  `examples/entities.example.toml`, `load_entities` + `portfolio_from_db`,
  `utxoproof portfolio --entities` and `report --full --entities`. New installs
  use account folders; `Entity` is a transitional Python alias. Each unspent
  output lands in the account whose attribution matches; unmapped legacy wallets
  still raise. `[[fiat]]` carries optional `as_of`/`statements`, audited by
  `utxoproof fiat-check`.
- `utxoproof/linking.py` keeps the legacy withdrawal→chain matcher and
  `funding_trail()` (exact sats + date window first, fee-skew second with ~Δ
  labels; ambiguous/unmatched reported, never guessed). The saga supersedes it
  as the primary provenance evidence; the funding trail remains the candidate
  fiat prologue. Depth/node truncation is rendered as a note, never silent.
- Dashboard lives in `utxoproof/portfolio.py` (account rollups, inline SVG
  charts — no JavaScript). Overview -> account pages -> provenance pages;
  chart HTML is trusted generator output (`| safe` in templates, everything
  else stays autoescaped). KYC colors are fixed per status (canonical order);
  account bars use dominant-KYC color. Tag model in `utxoproof/tags.py`
  (behavior/channel axes + tags from account manifests/legacy entities); donuts
  take explicit palettes, one donut per exclusive axis; bank page has a
  matched/unmatched donut. Credit-card balances are negative liabilities;
  broker holdings render as informational only.
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
  `exchange.ExchangeTx` (Bisq defaults non_kyc); `parse_kraken_fiat_flows`
  separately yields `FiatFlow` EUR deposits/withdrawals for the saga. Banks:
  `banks.py` profile engine (headers UNVERIFIED, alias-driven; `BankRow` keeps
  the CSV line number for stable evidence refs); `matching.py` fiat-leg matcher
  (two passes: exact euro equality, then fee/FX tolerance labeled approximate;
  review-only). Bank ids are explicit
  (`kbc-bankaccount`, `kbc-creditcard`; `kbc` kept as alias): card exports get
  their own profile (split credit/debit, merchant iso IBAN, settlement date),
  card numbers/holders never ingested, non-EUR rows without a rate abort. Bank rows are evidence, never
  pool input; v8 `run` auto-discovers statements and writes bank-rows.csv;
  legacy `run_legacy.sh` converts via `BANK_CSV`/`BANK_TYPE`; both render the
  bank review page via `write_bank_page`. Canonical rows carry `account_id`.
  All non-Kraken shapes are best-effort fixtures for later correction.
- Deposits enter the moving-average pool at stated or receipt-date price
  (`--price-history` CSV, else a loud error — never silent zero).
  `utxoproof check` diagnoses pool coverage without crashing.
- Regtest stack: `docker-compose.regtest.yml` (bitcoind only; no docker on dev
  boxes, so daemon runs are CI-only).
- SQLite schema (`utxoproof/schema.sql`) is created whole (Sec. 8) plus v8
  `accounts`, `account_scripts`, `labels`, `tx_outputs.account_id/script_hex`;
  schema changes need a migration note + `tests/test_schema.py` update.
  Rebuild v7 DBs from input; evidence files stay.
- `tests/fixtures/*.csv` are hand-verified: recompute expected values
  independently before pinning them in tests.
- `S101` (asserts) is ignored for `tests/**` in `pyproject.toml`; everything else
  must satisfy `ruff check`, `ruff format`, and strict `mypy`.
- Sprint deliverables follow `utxoproof-plan_v7.md` Sec. 20; flag spec-vs-reality
  gaps (e.g. rp2's `AbstractCountry` has no `apply_tax` hook) instead of forcing
  the spec's shape.

## Branches

Single branch: `main` (default). No `master`. Commit as
`natashaklum <natashaklum@users.noreply.github.com>`.
