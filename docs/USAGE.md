# utxoproof user guide ("slowstart")

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
cp utxoproof.example.toml utxoproof.toml
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
cp utxoproof.example.toml utxoproof.toml
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
mkdir -p ~/utxoproof-data
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

## 2c. Complete runs: `run.sh` (`MODE=prepare` / `MODE=refresh`)

`examples/run.sh` is one self-contained script with the settings on top — copy
it next to your data dir and edit the paths, or override any variable from the
environment (`DATA_DIR`, `KRAKEN_LEDGERS`, `KRAKEN_TRADES`, `TAX_YEAR`,
`BTC_PRICE`, `CONFIG_FILE`, `XPUB`, `FINGERPRINT`, `RPC_URL`, `ENTITIES_FILE`,
plus `UTXOPROOF_BIN` to point at your install):

```bash
cp examples/run.sh ~/utxoproof-data/run.sh
$EDITOR ~/utxoproof-data/run.sh   # set KRAKEN_LEDGERS etc.
bash ~/utxoproof-data/run.sh      # prepare: import -> check -> compute -> report
MODE=refresh bash ~/utxoproof-data/run.sh   # repeatable: refresh + alltime
```

`MODE=prepare` (the default) **stops** when `check` reports shortfalls — first
runs deserve interrogation, not momentum. `MODE=refresh` is idempotent (full
rewrite, no merge logic) and safe to re-run or cron once you trust the data.
Both print `==>` stage lines, both honor `UTXOPROOF_DATA_DIR`, and both are
covered by end-to-end tests running them against fixtures.

## 2d. On-chain wallets and the entity overview

Exchange history alone never shows on-chain UTXOs. With a Bitcoin Core node
reachable, set `XPUB` + `FINGERPRINT` (master fingerprint, 8 hex) and the RPC
settings in `run.sh`: `setup` creates the watch-only wallet and imports
`m/84'/0'/0'`-style descriptors (purpose/coin/account hardened, `/0/*` and
`/1/*` below the account xpub), then `sync` pulls transactions into
`utxoproof.db`. Verify with `utxoproof advise --db <data-dir>/utxoproof.db` —
one line per unspent output — or `report --full` (advisory + provenance
sections come from the DB; the tax/status pages stay exchange-CSV based).

For the demo-style overview page (`overview/overview.html` with entities),
copy `examples/entities.example.toml`, declare one `[[entities]]` per wallet /
bank / exchange (the entity whose `wallet` matches the synced wallet owns the
chain UTXOs; `[[fiat]]` rows attach euro balances), and run:

```bash
utxoproof portfolio --db <data-dir>/utxoproof.db --entities entities.toml \
  --price 40000 --out <data-dir>/portfolio
```

That writes `overview/overview.html`, `entities/<id>.html`, and
`provenance/provenance_<txid>_<vout>.html` per unspent UTXO, so entity-page
UTXO links resolve. Pass `--ledgers ledgers.csv` (also wired in `run.sh` via
`KRAKEN_LEDGERS`) to label each chain receipt with the Kraken withdrawal that
funded it — amount + date matched, ambiguous or unmatched withdrawals reported
and left unlabeled, never guessed. Ledgers with margin legs need the same
`--trades trades.csv` join as `import` for funding labels; without it the
labels are skipped with a warning while the overview still builds.
`report --full --entities entities.toml` folds the overview into the full
report the same way. Provenance pages state when the chain hit the depth cap
instead of silently truncating.

Zero on-chain balance with the wallet present almost always means descriptors
were imported with timestamp `now` (no historical rescan): check
`listdescriptors` for the two `wpkh` entries, then either `rescanblockchain`
manually or set `RESCAN_FROM=0` (unix time also accepted) in `run.sh` before
`setup` — the default `now` keeps first setups fast.

## 3. Configuration (`utxoproof.toml`)

Optional, not mandatory. `report` and `import` read it; every other command
runs on documented defaults today. When several copies exist, the first hit
wins: `--config` flag → `./utxoproof.toml` → `$UTXOPROOF_DATA_DIR/utxoproof.toml`
→ `~/.utxoproof/utxoproof.toml`. The margin-trading note, for example, only
appears when the winning file leaves `used_leverage` unset — and it names the
file, so there is never a mystery about which copy counts. Your data-folder
copy is therefore a first-class citizen, not a spare.

Copy `utxoproof.example.toml` and review three sections:

- `[taxpayer]`: your report reference label and **communal surcharge rate**
  (Brussels 0.0587, Ghent 0.076, Antwerp 0.08, average 0.07). This rate lands
  directly on your computed tax, so set your municipality's value.
- `[classifier]`: score thresholds plus honest self-reporting — leverage,
  derivatives, professional crypto income, BTC share of total income. The
  classifier cannot observe these; wrong flags here mean a wrong classification.
- `[accounting]`: cost basis method (`moving_average` default; `fifo`, `lifo`,
  `hifo`, `lofo` also supported via rp2).

Bitcoin/RPC/output sections arrive with their sprints and are ignored for now.

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

## 5. Flow B — on-chain wallets

You need Bitcoin Core (your own node for real use; regtest for practice —
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
  --price 40000 --out /tmp/utxo/provenance
```

`status` values holdings at `--price` (or yesterday's oracle close without it).
`advise` ranks UTXOs by tax-if-sold with sell/hold/borrow/estate/privacy flags.
`provenance` walks a UTXO back to its origin with per-step EUR values.

## 6. Price history

Live prices come from Kraken OHLC (CoinGecko fallback, ECB for fiat) and are
cached in SQLite. For offline years, seed the cache from the vendored history
(first real BTC print 2010-08-18 through today):

```bash
ls data/  # btc_eur_daily.csv (Kraken + blockchain.info x ECB), usd_eur_daily.csv (ECB)
```

`EURPriceOracle.load_csv` imports them (existing rows win, so live data is
never clobbered). Refresh the vendored files with
`scripts/fetch_price_history.py`. Regenerate the demo site any time with
`scripts/build_demo.py --out site`.

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
