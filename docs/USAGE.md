# utxoproof user guide ("slowstart")

> **Bringing years of wallets, exchanges and statements?** Start with the
> illustrated [Getting Started guide](https://natashaklum.github.io/utxoproof/guide/).
> It covers inventory, the owner tree, a one-slice pilot, reconciliation and the
> scaling playbook. This page is the deeper command reference.

The README quickstart gets you running in five minutes. This guide explains
what each step does, in the order you would actually use the tool. Every
command below is also documented with flags in `utxoproof --help`.

## 1. Concepts

- **Manual CSV** is the neutral interchange format:
  `date,side,btc,eur_per_btc,fee_eur` with `side` = BUY or SELL. Exchange
  imports convert to it; tax math consumes it.
- **Cost basis** is moving average by default (configurable). Buy fees join the
  cost pool; sell fees reduce proceeds.
- **Classification** (goede huisvader / speculator / passive holder) is a
  heuristic from observable signals plus your self-reported config flags —
  never a substitute for professional advice.
- **The SQLite DB** (`~/.utxoproof/utxoproof.db` by default) holds the on-chain
  transaction graph, price cache, KYC state and sync position. CSV flows work
  without it; on-chain and price features need it.
- **The v8 input tree** is one owner with account folders
  (`input/accounts/<id>/account.toml`). Descriptors establish on-chain ownership
  per output; BIP329 labels are display metadata, not tags. `utxoproof run`
  orchestrates labels, sync, imports and reports from it.

## 2. Installation

Two paths, same result. Pick the trust-maximal one unless you have a reason
not to.

**A. Debian-native (recommended for trust).** Everything from Debian's signed
archive; hashes pinned for the rest:

```bash
sudo apt install python3 python3-venv python3-pip git
git clone --recurse-submodules https://github.com/natashaklum/utxoproof
cd utxoproof
python3 -m venv .venv
.venv/bin/pip install "rp2 @ git+https://github.com/natashaklum/rp2.git@b1995bf1a07665035a46cf331cb06ccccd347ce2"
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install -e . --no-deps
mkdir -p ~/utxoproof-data/input
cp utxoproof.example.toml ~/utxoproof-data/input/utxoproof.toml
```

The pinned git SHA *is* rp2's integrity (git URLs can't carry hashes, so it
installs in its own step); every PyPI package is hash-checked. Contributors
add `.[dev]` tooling via `requirements-dev.txt` the same way. Regenerate both
files with `uv pip compile --generate-hashes` (see their headers).

**B. uv (faster).** Same outcome, one tool doing resolution + install:

```bash
git clone --recurse-submodules https://github.com/natashaklum/utxoproof
cd utxoproof
uv venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
mkdir -p ~/utxoproof-data/input
cp utxoproof.example.toml ~/utxoproof-data/input/utxoproof.toml
```

uv is a single static binary outside the Debian archive — convenient and
widely used, but a separate trust decision (see discussion in the project
history). Either path gives you `.venv/bin/utxoproof`.

The `.venv` lives inside the checkout on purpose: it is a disposable build
artifact (gitignored, no financial data in it), not code you keep and not
data you back up. Delete and recreate it any time with the two `uv` lines
above. What matters is keeping the *data directory* (next section) elsewhere.

Remote checkouts (sshfs etc.): the code works from a network mount, just
slower — but keep **both** the venv and the data directory on a local disk.
A venv is thousands of tiny files (painful over latency), and SQLite over
sshfs risks database corruption (no real file locking). Example layout with
a remote checkout:

```bash
uv venv ~/venvs/utxoproof
VIRTUAL_ENV=~/venvs/utxoproof uv pip install -e ".[dev]"
export UTXOPROOF_DATA_DIR=~/utxoproof-data   # local disk, always
```

Docker (runs the mainnet stack; regtest dev stack is `docker-compose.regtest.yml`):

```bash
cp utxoproof.example.toml utxoproof.toml
mkdir -p sources output
docker compose run --rm utxoproof --help
```

## 2b. Keep code and data separate (recommended)

Clone the tool into one folder, keep your real financial data in a canonical
data folder somewhere else. Everything utxoproof writes — the SQLite DB, the
price cache, evidence files — lives under a single data directory:

```bash
git clone --recurse-submodules https://github.com/natashaklum/utxoproof utxoproof
mkdir -p ~/utxoproof-data/input/accounts
export UTXOPROOF_DATA_DIR=~/utxoproof-data   # put this in your shell profile
```

With that set, every command uses `~/utxoproof-data/utxoproof.db` and
`~/utxoproof-data/evidence/` automatically — no flags needed, nothing lands
in the repo checkout or gets sprayed across home. Precedence when you need an
exception: explicit flags (`--db`, `--evidence-dir`, global `--data-dir`)
beat the environment variable, which beats the `~/.utxoproof` default:

```bash
utxoproof --data-dir /tmp/throwaway status --input manual.csv --price 40000
```

For a first real-data trial, point the variable at an empty folder: the worst
case is one folder to delete.

## 2c. Complete runs: `utxoproof run`, `discover`, thin `run.sh`

The v8 input tree is the source of truth: **one owner, many accounts**. There
is no central wallet/account list. Each folder under `input/accounts/` owns its
manifest and its type-specific files (descriptors, labels, statements, exchange
exports, balances):

```text
input/
  utxoproof.toml
  accounts/
    ledger-main/account.toml + receive.txt + labels.jsonl
    multisig-vault/account.toml + receive.txt + change.txt
    kbc-checking/account.toml + statements/**/*.csv
    kbc-visa/account.toml + statements/**/*.csv
    kraken/account.toml + ledgers.csv + trades.csv
    degiro/account.toml
```

Account kinds are `wallet` (on-chain), `bank`, `credit_card` (a liability),
`exchange`, and `broker` (informational only). The folder name is the default
account id; add `id = "..."` to override it. Minimal manifests:

```toml
# accounts/ledger-main/account.toml
kind = "wallet"
label = "Ledger main"
network = "mainnet"
gap_limit = 200
descriptor_receive_file = "receive.txt"
descriptor_change_file = "change.txt"
labels_file = "labels.jsonl"        # optional; labels.jsonl is auto-detected
tags = ["cold-storage"]
```

A multisig account exports both descriptors from Sparrow. Single-sig can use
the shorthand instead:

```toml
kind = "wallet"
label = "Hot wallet"
xpub = "zpub..."
fingerprint = "73c5da0a"
purpose = 84
```

```toml
# accounts/kbc-checking/account.toml
kind = "bank"
label = "KBC checking"
profile = "kbc-bankaccount"   # explicit; CSV files under statements/ are auto-found
currency = "EUR"
[[balances]]
amount_eur = "12500.00"
as_of = "2023-12-31"
```

`kind = "credit_card"` uses the same shape with a card profile and a negative
liability balance. Exchange and broker accounts:

```toml
# accounts/kraken/account.toml
kind = "exchange"
label = "Kraken"
exchange = "kraken"
ledgers = ["ledgers.csv"]
trades = ["trades.csv"]

# accounts/degiro/account.toml
kind = "broker"
label = "DEGIRO"
broker = "degiro"
[[holdings]]
symbol = "IWDA"
quantity = "25"
value_eur = "2125.00"
as_of = "2023-12-31"
note = "Informational only"
```

Generated files never go back into `input/`; the DB, imports and HTML live
under `--data-dir`. Discover the tree offline before trusting it:

```bash
utxoproof discover --input ~/utxoproof-data/input
utxoproof discover --input ~/utxoproof-data/input --json
```

`discover` parses every manifest/statement/label file, derives descriptors,
rejects duplicate scripts/addresses across accounts, and never writes. Add
`--online` to check the configured Electrum/Core backend. Then inspect the run
plan and execute:

```bash
utxoproof run --input ~/utxoproof-data/input --data-dir ~/utxoproof-data --plan
utxoproof run --input ~/utxoproof-data/input --data-dir ~/utxoproof-data \
  --year 2023
utxoproof run --input ~/utxoproof-data/input --data-dir ~/utxoproof-data \
  --offline   # imports + reports; skip Electrum/Core
```

`run` writes `imports/` (merged exchange ledgers/trades, `manual.csv`, bank
rows), the SQLite DB, `reports/<year>/report.html` plus the evidence ZIP (the
account source files, intermediates, attachments and the report; the root
`utxoproof.toml` is excluded because it may contain RPC credentials), the full
report, the account overview and account pages, provenance pages, and the bank
review page.
Use `--only labels,sync,exchanges,banks,report,portfolio` for a partial rerun;
`--force-rescan` for wallets funded before the first sync; `--no-zip` skips
the evidence bundle. Verify on-chain
balances against the selected backend with:

```bash
utxoproof reconcile --input ~/utxoproof-data/input \
  --db ~/utxoproof-data/utxoproof.db
```

`examples/run.sh` is now a thin wrapper around `utxoproof run`; it maps the
common environment variables and adds no pipeline logic:

```bash
INPUT=~/utxoproof-data/input DATA_DIR=~/utxoproof-data bash examples/run.sh
TAX_YEAR=2023 bash examples/run.sh
PLAN=1 bash examples/run.sh
OFFLINE=1 bash examples/run.sh
NO_ZIP=1 bash examples/run.sh
```

The old self-contained v7 script is `examples/run_legacy.sh`. It still owns
the legacy `MODE=prepare` / `MODE=refresh` / `MODE=clean` lifecycle,
`WALLETS`/`XPUB`, `KRAKEN_LEDGERS`, `BANK_CSV`, `ENTITIES_FILE` and the
legacy report layout described below; use it only for an existing v7 data dir.
Its `MODE=clean` is a dry run by default: it lists exactly what a run generated
(`manual.csv`, `bank-list.txt`, `bank-rows*.csv`, the DB, year bundles,
`current-status/`, `alltime/`, `portfolio/`, `bank/`) and deletes nothing.
Re-run with `CONFIRM=1` to remove those artifacts. User sources, attachments
and `utxoproof.toml` are never touched.

`TAX_YEAR` empty in the legacy wrapper covers every year present in
`manual.csv` — one `compute` row and one `<year>/report.html` + evidence ZIP
each, plus the single `status` snapshot and `alltime` summary. Set
`TAX_YEAR=2023` for one filing year only. Folders are created per data year;
year dirs left behind by older runs are never auto-deleted, so a stale `2022/`
just means an earlier run covered 2022. The same works granularly:
`compute --all-years` prints the per-year table and `report --all-years
--out DIR` writes `DIR/<year>/` bundles; `--year`/`--alltime` keep their
single-shot behavior. Every page menu lists every built year (`Tax 2022 ·
Tax 2023 · …`, current marked), so per-year reports link to each other instead
of stranding all but the latest.

Caveat: an *exported* `TAX_YEAR` shadows the default for every later run in
that shell — `unset TAX_YEAR` (or `export TAX_YEAR=`) to get all-years back.
Both modes log the effective setting at startup (`years: all in manual.csv`
vs `years: 2023`), so a single-year run never happens silently.

`MODE=prepare` (the legacy default) **stops** when `check` reports shortfalls —
first runs deserve interrogation, not momentum. If the gaps are understood and
you want numbers-with-caveats anyway, `ALLOW_SHORTFALLS=1` proceeds with a loud
warning (figures are upper bounds, never filing-ready). `MODE=refresh` is
idempotent (full rewrite, no merge logic) and safe to re-run or cron once you
trust the data. All three print `==>` stage lines and honor
`UTXOPROOF_DATA_DIR`; prepare and refresh are covered by end-to-end tests
running them against fixtures, and clean is tested for dry-run and `CONFIRM=1`
behavior.

## 2d. On-chain wallets and the account overview

Wallet accounts carry exported Sparrow/Core descriptors (multisig included) or
the single-sig `xpub` + `fingerprint` + `purpose` shorthand. A descriptor pair
may use BIP389 multipath `<0;1>`, or explicit receive/change files. Ownership
is attributed **per output** through the derived script registry; address
labels never establish ownership, and duplicate scripts across account folders
abort the run.

Electrum is the primary sync backend, so no Bitcoin Core wallet import is
needed. Set it in `input/utxoproof.toml`:

```toml
[chain]
backend = "electrum"
network = "mainnet"
electrum_url = "ssl://127.0.0.1:50002"
electrum_ca_file = ""   # your self-hosted server's CA, if private
```

Then run the v8 workflow as above. `utxoproof run` derives each wallet's scripts,
batches scripthash histories, fetches raw transactions and block-header
timestamps, extends the gap limit when activity reaches it, and attributes every
output to its account. Bitcoin Core remains available as
`backend = "core"` (with the `[chain]` RPC fields) for diagnosis or fallback;
Core sync imports descriptors and paginates `listtransactions`, but ownership
still comes from `account_scripts`, not a wallet-wide label.

Public-server smoke check (optional, network): the script below uses the BIP84
spec vector's account-1 xpub — a deliberately public, fundless watch-only key —
against Blockstream's public Electrum server. It runs the full v8 pipeline and
checks DB-unspent sats against the server, so a missed spend link/balance
mismatch fails the check. Public servers see the queried addresses; use your
own Electrum server for real wallets.

```bash
.venv/bin/python scripts/electrum_live_check.py
UTXOPROOF_LIVE_ELECTRUM=1 .venv/bin/python -m pytest -m network \
  tests/test_electrum_live.py
```

Per-account pages and the owner-wide overview are generated by `run`. For a
low-level rebuild from an account tree:

```bash
utxoproof portfolio --input ~/utxoproof-data/input \
  --db ~/utxoproof-data/utxoproof.db --price 40000 \
  --out ~/utxoproof-data/portfolio
```

### BIP329 labels

Sparrow/Electrum BIP329 JSONL exports are imported as first-class labels, not
tags. Put `labels.jsonl` in the wallet account folder (or name it in
`labels_file`); `run` imports it automatically. Supported label types are
`tx`, `addr`, `output`, and `input`; unknown types are preserved but not shown.
Each row keeps its account scope, source file and line, and origin `bip329`.

User labels (`origin = "user"`) win on display and survive re-import; imported
rows are replaced, never merged over user assertions. Add one after the account
DB exists:

```bash
utxoproof label --account ledger-main --type output --ref <txid>:0 --label "cold storage"
utxoproof label --account ledger-main --type output --ref <txid>:0 --delete
```

Labels are display metadata only: they do not establish output ownership and do
not change tags, tax treatment, or KYC. Account pages and provenance pages show
them next to UTXOs and addresses.

### Legacy entities file (v7)

The old overview model still works for existing low-level commands:
`examples/entities.example.toml`, one `[[entities]]` entry per wallet / bank /
exchange, with the entity whose `wallet` matches the synced wallet owning the
chain UTXOs, and `[[fiat]]` rows attaching euro balances:

```bash
utxoproof portfolio --db <data-dir>/utxoproof.db --entities entities.toml \
  --price 40000 --out <data-dir>/portfolio
```

#### What it is, why you need it, what goes in it

The chain knows your BTC; nothing in the pipeline knows your euros or what
each wallet *is*. The entities file is the one place where **you** assert
off-chain truth, and the overview is rendered from it — everything else is
derived. Concretely:

- **What belongs:** one `[[entities]]` per place holding value — `id`
  (stable, URL-safe: it becomes `<id>.html`), `kind` (`wallet` | `bank` |
  `exchange`), human `label`/`detail`, and for exactly the on-chain wallet(s)
  the `wallet` name matching `setup`/`sync --wallet` (several wallets allowed,
  see `WALLETS` in `run_legacy.sh`). Plus `[[fiat]]` rows: `entity_id`, `amount_eur`,
  an `as_of` balance date, a `note` saying where the figure came from, and
  optionally `statements = [...]` on the entity pointing at the evidencing
  files (relative to the TOML file). `fiat-check` audits exactly this:
  missing/future/stale (`--max-age-days`, default 90) dates and absent
  statement files warn without halting; it runs as a legacy `run_legacy.sh` stage whenever
  `ENTITIES_FILE` is set.
- **What does NOT belong:** secrets — no xpubs, no private keys, no RPC
  passwords; the file sits next to your data dir, not a vault, and nothing
  reads secrets from it. No auto-derived balances either: a checking balance
  cannot be computed from a partial statement (no opening balance — the same
  pre-export problem as the BTC pool), so any `[[fiat]]` amount without a
  human as-of note is a guess, not data. And nothing the tool computes itself
  (cost basis, gains, UTXO values) — those stay derived so they can't drift.
- **Validation:** unknown `entity_id` in `[[fiat]]` aborts with an error
  naming the ghost; a file with no `[[entities]]` aborts too. Each unspent
  output lands in the entity whose `wallet` matches its attribution (shared
  self-transfer outputs deterministically take the first sorted match); rows
  from before attribution existed use the legacy single-owner rule; a synced
  wallet with no matching entry aborts naming it, never silently misattributes.
  `utxoproof portfolio` prints the resulting allocation, so misassignment is
  visible immediately.
- **Tags (optional):** in v8 put `tags = ["cold-storage"]` in the account
  manifest (inherited by every holding of that account) and an optional
  `[utxo_tags]` table in the account manifest; the legacy entities path uses
  `[entity_tags]` per entity id and `[utxo_tags]` per `"txid:vout"` ref (e.g.
  `"abc123:0" = ["cash", "gift"]`). Use them for what only you know — how fiat
  entered (`bank-transfer`, `cash`, `card`), custody roles (`cold-storage`),
  provenance notes. Derived tags always apply first (behavior bucket, channel
  from acquisition source, `mixed`); yours extend them, and an asserted channel
  wins its donut bucket. Unknown entities and malformed refs abort like
  `[[fiat]]` ghosts.

### Tags and tag charts: what the colors mean

Every holding carries three exclusive buckets (one donut apiece on the
overview, each summing to 100%) plus an overlapping free-tag set shown in the
UTXO tables:

| Axis | Buckets (priority order) | Source |
|---|---|---|
| Tax behavior | `speculation` > `buy-and-hold` (held ≥ 365 days) > `accumulating` | speculation taint flag, else holding duration |
| Funding channel | asserted > `exchange` / `p2p` / `mining` / `transfer` / `unknown` | your tags first, else acquisition `source_type` |
| Privacy | `kyc` / `non_kyc` / `mixed` / `unknown` + explicit `mixed` tag | KYC propagation (existing) |

Honesty rails: `unknown` is a first-class slice, never folded away. The
chain only proves *where coins arrived from* — bank vs card lives on trades
and bank rows where the evidence is (bank-matched legs, instant-buy flow),
or in your tags; the tool will not derive `cash` from chain data. Thresholds
(`365` days) and palettes are fixed and documented here, not tuned per run.
The bank page adds a matched-vs-unmatched review donut over statement euros.

Tags describe (*what it is*: behavior, channel, mixed/privacy-risk, yours);
flags recommend (*what to do*: sell/hold/borrow/estate/tax-loss). The tables
label the columns exactly that way, and the two layers never duplicate: the
old `speculation_tainted` / `privacy_risk` flags now live only as tags.

The v8 account dashboard writes `reports/overview/overview.html`,
`reports/entities/<id>.html` (one account page per account) and
`reports/provenance/provenance_<txid>_<vout>.html` per unspent UTXO, so
account-page UTXO links resolve. The legacy low-level `portfolio` command uses
`overview/`, `entities/` and `provenance/` under `--out`. Every page walks the **full all-parent saga**: consolidation
parents each carry their proportional share (an 80/20 funding split is visible,
not hidden behind the largest parent), fan-out siblings are listed, and hitting
the `--depth`/`--max-nodes` caps shows the truncated share instead of a
complete-looking trail. The portfolio cards and account table show each UTXO's
**bank-proven coverage**; candidates never raise it.

Pass `--ledgers ledgers.csv` (v8 `run` finds the exchange account folder; the
legacy wrapper uses `KRAKEN_LEDGERS`) plus
bank statements (`--bank-csv`/`--bank-type`, or `--bank-list`) to the same
commands and the saga joins the layers in evidence order: bank debit →
exchange EUR deposit → trade (FIFO EUR balance replay) → BTC withdrawal (FIFO
BTC replay, or nearest-buy candidates when the ledger starts mid-history) →
chain receipt (exact sats/date, then fee-skew tolerance). Each edge cites both
sides (`bank.csv:row` + ledger refid, `txid:vout`); ambiguous or unmatched legs
remain unresolved, never guessed. Tiers are `exact`, `approximate`,
`allocated`, and `candidate` (candidate-only panel). A rejected user edge is
remembered in the DB and survives rebuilds; deleting a supplied source clears
its derived links on the next run. Ledgers with margin legs need the same
`--trades trades.csv` join as `import`.

`report --full --entities entities.toml` (or v8 `run`) folds the saga and
coverage into the full report the same way. The old fiat funding prologue still appears above
the saga as a candidate cross-check (nearest exact-BTC buys + bank match);
without `--bank-csv` that prologue is omitted, while the saga still shows
exchange-only links when `--ledgers` alone is supplied. Without either source,
persisted links from the last rebuild are shown, and the page says how to
rebuild them.

Zero on-chain balance with the wallet present almost always means descriptors
were imported with timestamp `now` (no historical rescan): check
`listdescriptors` for the two `wpkh` entries, then either `rescanblockchain`
manually or set `RESCAN_FROM=0` (unix time also accepted) in
`run_legacy.sh` before
`setup` — the default `now` keeps first setups fast.

Only outputs proven yours count: `sync` marks each output via `getaddressinfo`
(`is_mine`), so coins you sent away (Kraken deposits, payments) stay in the
chain graph for context but leave every total — advisory, portfolio and the
default provenance targets. Verify the books against the node any time with:

```bash
utxoproof reconcile --db <data-dir>/utxoproof.db --rpc-url ... --wallet <name>
```

which prints `RECONCILED (gap 0)` or `GAP … BTC` instead of eyeballing
`overview.html` against `getbalances`. For v8, `utxoproof reconcile --input
~/utxoproof-data/input` compares every account against Electrum scripthash
balances in one pass. With several wallets on the Core path, run it once per
wallet: the DB side counts that wallet's attributed outputs plus legacy
unattributed rows (flagged, never silently dropped).

## 3. Configuration (`utxoproof.toml`)

For v8 the config belongs at `input/utxoproof.toml` next to `accounts/`.
Legacy commands keep the old search order: `--config` flag → `./utxoproof.toml`
→ `$UTXOPROOF_DATA_DIR/utxoproof.toml` → `~/.utxoproof/utxoproof.toml`.
Copy `utxoproof.example.toml` and review its sections:

- `[owner]`: the one taxpayer name shown in the report header.
- `[chain]`: `backend = "electrum"` (primary) or `"core"` (optional fallback);
  Electrum URL/CA or Core RPC URL/user/password/wallet.
- `[reporting]`: optional `as_of` and `tax_year`; `run --as-of`/`--year` win.
- `[taxpayer]`: your report reference label and **communal surcharge rate**
  (Brussels 0.0587, Ghent 0.076, Antwerp 0.08, average 0.07). This rate lands
  directly on your computed tax, so set your municipality's value.
- `[classifier]`: score thresholds plus honest self-reporting — leverage,
  derivatives, professional crypto income, BTC share of total income. The
  classifier cannot observe these; wrong flags here mean a wrong classification.
- `[accounting]`: cost basis method (`moving_average` default; `fifo`, `lifo`,
  `hifo`, `lofo` also supported via rp2).

## 4. Flow A — exchange history to tax report

Export your history (Kraken: ledgers.csv **and** trades.csv — see below;
Coinbase: transaction history; Binance: trade history; Bisq: trade history)
and convert it:

```bash
.venv/bin/utxoproof import --file ~/kraken_ledgers.csv --type kraken \
  --trades ~/kraken_trades.csv --out /tmp/utxo/manual.csv
```

Supported `--type` values: `kraken`, `coinbase`, `binance`, `bisq` (tagged
non-KYC), plus Belgian banks `ing`, `kbc`, `bnp`, `belfius`, `argenta` (these
produce bank-row CSVs for fiat-leg review, not trade rows). `--kyc` overrides
the exchange default. All non-Kraken shapes are best-effort — check the first
converted rows by eye before trusting a full year.

Deposits (transfers-in, staking rewards, exchange credits) enter the cost
pool at receipt-date value: stated price first, otherwise `--price-history`
with a `(date,close_eur)` history file (defaults to the copy vendored with
the package; override with `--price-history other.csv`).
Without a price source for a priceless deposit you get an error naming the
date, never a silent zero. Withdrawals to self-custody are not disposals,
but they carry proportional basis out of the pool — otherwise exchange
holdings would be overstated by everything you moved to your own wallets.
A withdrawal bigger than the tracked pool is clamped with a loud warning
(pre-export holdings are not modeled) instead of aborting the whole run;
only a *sell* with genuinely nothing behind it still raises.

### Kraken: ledgers, trades, margin

Kraken exports two files that belong together:

- **ledgers.csv**
  (`txid,refid,time,type,subtype,aclass[,subclass],asset[,wallet],amount,fee,balance`)
  records money movement, grouped by `refid`. Newer exports add `subclass`
  (fiat/crypto/…) and `wallet` (`spot / main`, Earn, …) — both captured.
- **trades.csv**
  (`txid,ordertxid,pair,time,type,ordertype,price,cost,fee,vol,margin,misc,ledgers`)
  records execution economics. Its `ledgers` column lists one *or more* ledger
  txids, which is how trades join back to ledger groups.

Ledgers alone suffice for plain spot history, but pass `--trades` anyway: it
supplies exact execution prices and flags **margined trades** (`margin`
non-zero). Margin settlements are taxable disposals (shown as `MARGIN` rows in
reports); **`rollover` rows are financing costs** added to your cost basis;
`settled` rows are ignorable; anything else unknown fails loudly instead of
being silently dropped. If margin activity is detected, `import` tells you to
set `used_leverage=true` in `utxoproof.toml [classifier]` — the tool cannot
infer leverage from spot legs alone.

Margin rows split on economics: **zero amount means pure financing cost**
(fee only, no disposal — like your `margin`/EUR/0.0000/fee rows); **non-zero
means P&L settlement**, priced via linked trade, else fiat leg, else a loud
error naming the refid. Instant-buy `spend`+`receive` pairs become ordinary
trades (lone legs stay visible as cashflow, gains never invented);
`adjustment` (delisting conversions) prices off its fiat leg; `earn` rewards
and `invite bonus` book as income while allocation-style subtypes
(`spottostaking`, `migration`, …) are internal moves, not income.

Preview the number, then build the report bundle:

```bash
.venv/bin/utxoproof compute --input /tmp/utxo/manual.csv --year 2023
.venv/bin/utxoproof report --input /tmp/utxo/manual.csv --year 2023 \
  --source ~/kraken_2023.csv --out /tmp/utxo/2023
```

`--source` (repeatable) pulls raw files into the evidence ZIP, which lands next
to `report.html` together with `evidence.db` and `manifest.json`. `--no-zip`
skips it; `--alltime` writes a multi-year summary instead. Open `report.html`
in a browser and print to PDF for filing.

## 5. Flow B — on-chain wallets (v8 and low-level)

The recommended path is a wallet account folder with an exported descriptor
(multisig included) and `utxoproof run`; see section 2c/2d. It uses Electrum by
default and attributes each output to its account. The low-level commands below
remain for diagnosis and for a single-wallet Core-only setup.

Bitcoin Core is optional (your own node for real use; regtest for practice —
see `docker-compose.regtest.yml`). One-time setup per xpub:

```bash
.venv/bin/utxoproof setup --rpc-url http://127.0.0.1:8332 \
  --rpc-user bitcoinrpc --rpc-password CHANGE_THIS \
  --wallet utxoproof_watchonly \
  --xpub <account-xpub> --fingerprint <master-fp: 8 hex chars>
```

`--purpose/--coin/--account` select the BIP44/49/84/86 template (default 84).
Then sync incrementally:

```bash
.venv/bin/utxoproof sync --rpc-url http://127.0.0.1:8332 \
  --rpc-user bitcoinrpc --rpc-password CHANGE_THIS \
  --wallet utxoproof_watchonly
```

Now the analytical commands read the local DB:

```bash
.venv/bin/utxoproof status --input /tmp/utxo/manual.csv --price 40000
.venv/bin/utxoproof privacy --db ~/.utxoproof/utxoproof.db --out /tmp/utxo/privacy
.venv/bin/utxoproof advise --db ~/.utxoproof/utxoproof.db --price 40000 \
  --as-of 2024-06-01 --out /tmp/utxo/advisory
.venv/bin/utxoproof provenance <txid>:<vout> --db ~/.utxoproof/utxoproof.db \
  --price 40000 --ledgers ~/exports/kraken_ledgers.csv \
  --bank-csv ~/exports/kbc.csv --bank-type kbc-bankaccount \
  --out /tmp/utxo/provenance
```

`status` values holdings at `--price` (or the bundled history close without it).
`advise` ranks UTXOs by tax-if-sold with sell/hold/borrow/estate/privacy flags.
`provenance` walks a UTXO back through all on-chain parents and, when ledgers +
bank statements are supplied, through the exchange and bank layers with a
value-weighted coverage badge.

## 5b. Flow C — bank statements (fiat-leg corroboration)

Bank CSVs prove the euro side of your trades. They corroborate exchange legs
(plan Sec. 11), join the evidence ZIP via `--source`, and feed account balances
— but they **never enter the cost pool**: a statement is a list of movements,
not a balance, and only you know the opening position.

For v8, put each export under its account folder; discovery finds every CSV
recursively and uses the manifest's explicit profile:

```text
input/accounts/kbc-checking/account.toml
input/accounts/kbc-checking/statements/2023/kbc.csv
```

`utxoproof run` parses them, writes `imports/bank-rows.csv` with `account_id`
and source file/line, and renders the bank review page. For a standalone
conversion:

```bash
.venv/bin/utxoproof import --file ~/kbc-checking.csv --type kbc --out /tmp/utxo/bank-rows.csv
```

Supported types: `ing`, `kbc-bankaccount`, `kbc-creditcard`, `bnp`, `belfius`,
`argenta` (all `;`-delimited, BOM-tolerant; plain `kbc` still works as an alias
for `kbc-bankaccount`). Files decode as UTF-8-SIG first, Windows-1252 second
(Excel-era Belgian exports: the ë in "België" is byte `0xeb` outside UTF-8) —
UTF-8 always wins when valid, so previously parseable bytes behave identically;
anything else aborts naming the file and both codecs. Checking and savings accounts share one export shape
per bank, so one profile covers both — **credit cards do not**: KBC card
exports (Dutch, observed 2025) split credit/debit columns, name the merchant
instead of an IBAN, and settle on `Datum verrekening` (used as the row date,
consistent with booking-date semantics). Amount resolution is documented in
`banks.py` and pinned by tests: pre-converted `bedrag in EUR` wins, else the
populated credit/debit leg (abs + applied sign, robust to signed and unsigned
layouts), else `bedrag` converted at the row `koers` — a non-EUR row without
a rate aborts loudly instead of guessing FX. Card numbers, holder names and
other PII columns are deliberately never ingested; non-zero card fees surface
in the description (`+ €X fee`) rather than silently. Every profile header is
best-effort and **UNVERIFIED against live exports** — columns match by alias lists, so if your export renames a
header, check whether an alias already covers it and report the exact header
line otherwise; do not silently hand-edit the CSV. v8 `run` auto-discovers
account statements and merges them. The legacy `run_legacy.sh` still automates
its own layout with `BANK_CSV` + `BANK_TYPE`: `|`-separated `type:path` specs
(bare paths use `BANK_TYPE`), e.g.
`BANK_CSV="kbc:~/exports/kbc.csv|ing:~/my exports/ing.csv"`. Its `stage_bank`
converts each file with its own profile, merges them into one
`$DATA_DIR/bank-rows.csv` (rows keep their source file), renders one merged
`bank/` review page, and attaches every statement as a report `--source`.
Spaces survive (split on `|` only; `bank-list.txt` carries one spec per line
and every consumer reads it line by line); colons stay reserved as the type
separator. Direct CLI use mirrors it: repeatable `--file`/`--type` pairs, or
`--bank-list` with one spec per line.

Review the converted rows before trusting a year (`description`, `amount_eur`,
counterparty IBAN, reference). The fiat-leg matcher (`match_fiat_legs`) runs
two passes, exact before approximate, so a near-miss never steals an exact
match: direction (BUY↔outflow, SELL↔inflow) plus calendar date ±2 days in both
passes; the exact pass needs euro-cent equality (reference hits raise it to
`strong`), the approximate pass tolerates bank fees and FX rounding (within
min(€1, 1%), labeled `approximate` with its Δ). Ties at either tier — and
anything outside tolerance — stay unmatched for manual review rather than
auto-booked. The demo
site's bank page (`bank/bank.html`, KBC fixture) shows exactly this shape.

## 6. Price history

Prices come from the history vendored with the package — no network on any
default path. `status`/`advise`/`provenance`/`portfolio`/`report` value
display prices at the bundled close (stale-noted past file coverage), and
acquisition curves raise naming the date outside coverage instead of
inventing basis. Pass `--price` for an explicit print, `--price-history
other.csv` for a custom curve, or `--live-prices` to opt back into the live
Kraken OHLC oracle (CoinGecko fallback, ECB for fiat), which caches into
SQLite. Seeded files live under `data/` (first real BTC print 2010-08-18
through today):

```bash
ls data/  # btc_eur_daily.csv (Kraken + blockchain.info x ECB), usd_eur_daily.csv (ECB)
```

Three files, three roles: `btc_eur_daily.csv` is the consumed series
(`date,close_eur,source`); `btc_usd_daily.csv` keeps the raw upstream BTC/USD
prints and `usd_eur_daily.csv` the raw ECB business-day rates, so later
tweaks re-stitch offline via `scripts/fetch_price_history.py --offline`
without touching API rate limits. Regens only *add* dates — shipped rows are
never revised (tax basis filed off old rows must stay reproducible), and the
script refuses to write a gappy series.

Why EUR-first instead of BTC/USD: EUR is the pipeline currency (Belgian tax),
and Kraken `XXBTZEUR` is the actual EU execution venue — one direct print
instead of routing every valuation through two. The BTC/USD × ECB synthesis
covers only the pre-2024 era with no EUR venue history; the ECB file converts
USD-denominated legs at the official reference rate.

`EURPriceOracle.load_csv` imports them (existing rows win, so live data is
never clobbered). Refresh the vendored files with
`scripts/fetch_price_history.py`. Regenerate the demo site any time with
`scripts/build_demo.py --out site`; it publishes the legacy fixture reports,
the offline, seeded v8 account workflow under `site/v8/`, and the illustrated
Getting Started guide under `site/guide/` (index: `site/index.html`).

## 7. Supporting evidence (scans, screenshots, PDFs)

Anything that is not machine-readable CSV goes through `attach`, which copies
the file into `evidence/<year>/` next to your database, hashes it, and links
it to a transaction (or leaves it general with `--tx` omitted):

```bash
.venv/bin/utxoproof attach --file ~/withdrawal-mail.png --tx <txid> \
  --note "Kraken withdrawal confirmation" --year 2023
.venv/bin/utxoproof attach --file ~/annual-statement.pdf --note "Bank 2023 totals"
```

Rules: originals stay untouched (the registry copy counts); re-attaching the
same content is a no-op via content hash; name collisions gain a suffix.
Conventions: one receipt often covers several transactions, so folders are
organized by year, not by txid. Images render as thumbnails in provenance
reports (large files become links); PDFs are always links.

What flows where: `provenance` shows an Attachments appendix for the chain's
transactions; `report --year` bundles `evidence/<year>/` into the evidence ZIP
under `attachments/` with hashes in `manifest.json`. Verify a bundle any time
by re-hashing against the manifest.

## 8. Troubleshooting

- `utxoproof check --input manual.csv`: start here when numbers look wrong.
  It walks the file like the pool does but never crashes — each disposal is
  annotated with pool coverage and cumulative buys vs deposits, so shortfalls
  show exactly where coverage ran out and what kind of inflow is missing.
  Shortfalls get a verdict (`ORDER` = the day nets fine, intraday sequence
  lost; `STRUCTURAL` = funding genuinely missing), plus two suspect lists:
  `DOUBLE-COUNT` (a trades ref booked on two disposal rows — certain) and
  `SUSPECT` (same-date margin/sell of matching size — confirm manually).
  Everything runs locally; paste back only what you're comfortable sharing.
- `SELL with empty inventory`: your CSV sells more than it bought (check date
  order and missed deposits). A `MARGIN` disposal on empty inventory is
  usually a **short sale** (sell first, cover later) or an opening leg outside
  the import — short positions need negative-inventory support, which is not
  implemented yet; import fuller history or record the opening position.
- `importdescriptors ... Missing checksum`: upgrade utxoproof — checksums are
  appended automatically since Sprint 3.
- Regtest `No such mempool transaction`: fixed — confirmed txs resolve via
  blockhash without txindex.
- No docker on this machine: daemon-backed runs are CI-only; everything else
  runs locally. The `regtest` pytest marker skips without a node.
- Pages demo not updating: Actions tab → re-run `Demo reports` (needs Pages
  source = GitHub Actions).
