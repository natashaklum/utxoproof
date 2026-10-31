-- utxoproof SQLite schema (plan Sec. 8). Single database, all mutable state.
-- Raw source imports stay as flat files; everything derived/queryable lives here.

-- ── Transaction graph ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS transactions (
    txid            TEXT PRIMARY KEY,
    block_height    INTEGER,
    block_time      TEXT NOT NULL,       -- ISO 8601
    eur_price_btc   TEXT,                -- Decimal string; NULL until populated
    is_coinbase     INTEGER NOT NULL DEFAULT 0
);

-- Every output ever created in a watched transaction.
-- Core UTXO table; spent outputs remain here with spent_by set.
CREATE TABLE IF NOT EXISTS tx_outputs (
    txid            TEXT NOT NULL,
    vout            INTEGER NOT NULL,
    address         TEXT,
    value_sat       INTEGER NOT NULL,
    eur_value       TEXT,                -- value_sat/1e8 * eur_price_btc
    -- KYC
    kyc_status      TEXT NOT NULL DEFAULT 'unknown',
                                         -- kyc|non_kyc|mixed|unknown
    kyc_fraction    TEXT NOT NULL DEFAULT '0',
                                         -- Decimal 0.0-1.0
    -- Source annotation (populated at import time)
    source_type     TEXT,                -- exchange_purchase|p2p_purchase|
                                         -- mining|self_transfer|received|unknown
    source_label    TEXT,
    source_evidence TEXT,
    -- Wallet / account attribution
    wallet_label    TEXT,
    descriptor      TEXT,
    account_id      TEXT,                -- owning account (plan v8; NULL = legacy)
    script_hex      TEXT,                -- scriptPubKey hex for exact attribution
    -- Spend tracking
    spent_by_txid   TEXT,                -- NULL = currently unspent
    spent_by_input  INTEGER,
    -- Ownership (plan Sec. 9): 1 = ours (default; sample/demo rows stay counted),
    -- 0 = proven external (e.g. payment to a third party). Totals, advisory and
    -- portfolio count only is_mine=1 unspent outputs; external rows stay in-graph
    -- for chain context. Added after v1 DBs existed; see init_db migration.
    is_mine         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (txid, vout)
);

-- Every input in a watched transaction.
CREATE TABLE IF NOT EXISTS tx_inputs (
    txid            TEXT NOT NULL,
    input_index     INTEGER NOT NULL,
    prev_txid       TEXT NOT NULL,
    prev_vout       INTEGER NOT NULL,
    PRIMARY KEY (txid, input_index)
);

-- ── Accounts and script registry (plan v8) ─────────────────────────
-- Mirror of input/accounts/<folder>/account.toml for querying. The input tree
-- is the source of truth; `utxoproof run` rebuilds this cache from it.

CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,           -- wallet|bank|credit_card|exchange|broker
    label       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    network     TEXT NOT NULL DEFAULT 'mainnet',
    tags        TEXT NOT NULL DEFAULT '', -- comma-separated
    source_path TEXT NOT NULL DEFAULT ''
);

-- Every derived receive/change script; one script belongs to exactly one
-- account. Electrum sync uses these as its scripthash watch set.
CREATE TABLE IF NOT EXISTS account_scripts (
    account_id    TEXT NOT NULL,
    chain         INTEGER NOT NULL,       -- 0 external, 1 change
    derivation_index INTEGER NOT NULL,
    address       TEXT NOT NULL,
    script_hex    TEXT NOT NULL,
    PRIMARY KEY (account_id, chain, derivation_index),
    UNIQUE (script_hex),
    UNIQUE (address)
);

-- BIP329 + user labels. Labels are assertions, not ownership or tags.
CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY,
    account_id  TEXT NOT NULL,
    type        TEXT NOT NULL,           -- tx|addr|output|input|<preserved unknown>
    ref         TEXT NOT NULL,
    label       TEXT NOT NULL,
    spendable   INTEGER,
    origin      TEXT NOT NULL DEFAULT 'bip329', -- bip329|user
    source_file TEXT NOT NULL DEFAULT '',
    source_line INTEGER,
    updated_at  TEXT NOT NULL,
    UNIQUE (account_id, type, ref)
);

CREATE INDEX IF NOT EXISTS idx_labels_ref ON labels(type, ref);

-- ── Price cache ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_cache (
    date        TEXT NOT NULL,           -- 'YYYY-MM-DD'
    pair        TEXT NOT NULL,           -- 'BTC/EUR' | 'USD/EUR' etc.
    close_eur   TEXT NOT NULL,           -- Decimal string
    source      TEXT NOT NULL,           -- 'kraken'|'coingecko'|'ecb'
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (date, pair)
);

-- ── Sync state ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Keys used:
--   'last_block_height'           -> last Bitcoin Core block processed
--   'last_sync_at'                -> ISO 8601 datetime of last sync
--   'last_import_{source}'        -> datetime of last import per source name

-- ── Source file manifest ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS source_manifest (
    id          INTEGER PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,    -- UNIQUE prevents duplicate imports
    size_bytes  INTEGER NOT NULL,
    role        TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

-- ── Evidence links ─────────────────────────────────────────────────
-- Human artifacts (screenshots, PDFs, scans) registered via `attach`,
-- linked to the transactions they support. Files live under evidence/<year>/;
-- filename is stored relative to the evidence root. Added in Sprint 11;
-- pre-existing DBs gain it automatically (init_db is idempotent).

CREATE TABLE IF NOT EXISTS evidence_links (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL,           -- -> source_manifest.sha256
    txid        TEXT,                    -- NULL = general (non-tx) evidence
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ── Provenance links ───────────────────────────────────────────────
-- One row per cross-domain link in a UTXO saga (plan v7 Sec. 14). Derived
-- links are rebuilt idempotently; origin='user' rows survive rebuilds.
-- confidence: exact | approximate | allocated | candidate | rejected.

CREATE TABLE IF NOT EXISTS provenance_links (
    id            INTEGER PRIMARY KEY,
    utxo_txid     TEXT NOT NULL,
    utxo_vout     INTEGER NOT NULL,
    layer         TEXT NOT NULL,         -- bank | exchange | chain
    from_ref      TEXT NOT NULL,         -- statement row | ledger refid | txid:vout
    to_ref        TEXT NOT NULL,
    confidence    TEXT NOT NULL,
    evidence_ref  TEXT NOT NULL,         -- file:row / txid / block height
    amount_eur    TEXT,
    amount_sat    INTEGER,
    origin        TEXT NOT NULL DEFAULT 'derived',  -- derived | user
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (utxo_txid, utxo_vout, layer, from_ref, to_ref)
);

-- ── Advisory cache ─────────────────────────────────────────────────
-- Derived; can be recomputed at any time.

CREATE TABLE IF NOT EXISTS advisory_cache (
    txid                    TEXT NOT NULL,
    vout                    INTEGER NOT NULL,
    computed_at             TEXT NOT NULL,
    acquisition_cost_eur    TEXT,
    current_value_eur       TEXT,
    unrealized_gain_eur     TEXT,
    tax_if_sold_eur         TEXT,
    holding_days            INTEGER,
    kyc_status              TEXT,
    kyc_fraction            TEXT,
    speculation_tainted       INTEGER NOT NULL DEFAULT 0,
    estate_score            INTEGER NOT NULL DEFAULT 0,
    advisory_flag           TEXT,
    PRIMARY KEY (txid, vout)
);
