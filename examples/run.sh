#!/usr/bin/env bash
# Single script to run the full utxoproof pipeline: import -> check -> compute ->
# sync -> portfolio -> report -> status (+ alltime).
#
# Two modes:
#   MODE=prepare   (default)   — stops if check reports shortfalls
#   MODE=refresh   — idempotent rerun; shortfalls print but do not stop
#
# All settings are environment variables with sane defaults; copy this file
# next to your data dir and edit the variables, or override any from the
# environment (e.g. DATA_DIR, KRAKEN_LEDGERS, TAX_YEAR, BTC_PRICE, XPUB,
# FINGERPRINT, RPC_URL, etc.).
#
#   cp examples/run.sh ~/utxoproof-data/run.sh
#   $EDITOR ~/utxoproof-data/run.sh     # set your paths/flags
#   bash ~/utxoproof-data/run.sh          # prepare mode
#   TAX_YEAR=2023 bash ~/utxoproof-data/run.sh   # filing year in prepare mode
#   bash ~/utxoproof-data/run.sh            # refresh mode
set -euo pipefail

# ---------------------------------------------------------------------------
# Variables (edit these or export overrides)
# ---------------------------------------------------------------------------
: "${DATA_DIR:=$HOME/utxoproof-data}"
: "${KRAKEN_LEDGERS:=$HOME/exports/kraken_ledgers.csv}"
: "${KRAKEN_TRADES:=}"            # empty skips trades join
: "${TAX_YEAR:=$(date +%Y)}"
: "${BTC_PRICE:=}"                # empty = oracle yesterday-close for status
: "${CONFIG_FILE:=}"              # e.g. "$DATA_DIR/utxoproof.toml"
: "${XPUB:=}"                     # account xpub/ypub/zpub; empty skips on-chain stages
: "${FINGERPRINT:=}"             # master fingerprint, 8 hex; paired with XPUB
: "${RPC_URL:=http://127.0.0.1:8332}"  # bitcoind RPC URL
: "${RPC_USER:=}"
: "${RPC_PASSWORD:=}"
: "${WALLET:=utxoproof_watchonly}"
: "${PURPOSE:=84}"               # BIP44 purpose
: "${COIN:=0}"                    # BIP44 coin
: "${ACCOUNT:=0}"                 # BIP44 account
: "${ENTITIES_FILE:=}"            # path to entities TOML; empty skips portfolio
: "${MODE:=prepare}"             # prepare | refresh
: "${DEBUG:=}"                    # non-empty prints each on-chain command (password redacted)
: "${RESCAN_FROM:=now}"           # setup rescan: 'now' (fast) or unix time / 0 for full

UTXOPROOF_BIN="${UTXOPROOF_BIN:-utxoproof}"

log() { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# Debug helper: prints the effective on-chain target. Password is never logged.
dbg_rpc() {
    if [ -n "$DEBUG" ]; then
        log "debug: rpc_url=$RPC_URL rpc_user=${RPC_USER:-<none>} wallet=$WALLET bin=$UTXOPROOF_BIN"
    fi
}

# ---------------------------------------------------------------------------
# Stage: import (same for both modes)
# ---------------------------------------------------------------------------
stage_import() {
    log "import: kraken ledgers -> manual CSV"
    [ -f "$KRAKEN_LEDGERS" ] || fail "ledgers file not found: $KRAKEN_LEDGERS"
    mkdir -p "$DATA_DIR"
    if [ -n "$KRAKEN_TRADES" ]; then
        [ -f "$KRAKEN_TRADES" ] || fail "trades file not found: $KRAKEN_TRADES"
        "$UTXOPROOF_BIN" import --file "$KRAKEN_LEDGERS" --type kraken \
            --trades "$KRAKEN_TRADES" --out "$DATA_DIR/manual.csv"
    else
        "$UTXOPROOF_BIN" import --file "$KRAKEN_LEDGERS" --type kraken \
            --out "$DATA_DIR/manual.csv"
    fi
    log "import done: $DATA_DIR/manual.csv"
}

# ---------------------------------------------------------------------------
# Stage: check pool coverage
# ---------------------------------------------------------------------------
stage_check() {
    log "check: pool coverage (never crashes, shortfalls named)"
    out=$("$UTXOPROOF_BIN" check --input "$DATA_DIR/manual.csv" 2>&1) || true
    printf '%s\n' "$out"
    printf '%s\n' "$out" | tail -n 1
}

# ---------------------------------------------------------------------------
# Stage: compute
# ---------------------------------------------------------------------------
stage_compute() {
    log "compute: year $TAX_YEAR"
    "$UTXOPROOF_BIN" compute --input "$DATA_DIR/manual.csv" --year "$TAX_YEAR"
}

# ---------------------------------------------------------------------------
# Stage: setup on-chain wallet
# ---------------------------------------------------------------------------
stage_setup() {
    log "setup: initialize on-chain wallet from xpub"
    if [ -n "$XPUB" ] && [ -n "$FINGERPRINT" ]; then
        log "setup: wallet $WALLET via $RPC_URL (user ${RPC_USER:-<none>})"
        dbg_rpc
        setup_out=$("$UTXOPROOF_BIN" setup \
            --rpc-url "$RPC_URL" --rpc-user "$RPC_USER" \
            --rpc-password "$RPC_PASSWORD" \
            --xpub "$XPUB" --fingerprint "$FINGERPRINT" \
            --wallet "$WALLET" \
            --purpose "$PURPOSE" --coin "$COIN" --account "$ACCOUNT" \
            --timestamp "$RESCAN_FROM" 2>&1) || {
            log "warning: setup skipped (node unreachable or RPC error)"
            printf '%s\n' "$setup_out"
        }
    else
        log "note: setup skipped (set XPUB+FINGERPRINT to enable on-chain stages)"
    fi
}

# ---------------------------------------------------------------------------
# Stage: sync on-chain
# ---------------------------------------------------------------------------
stage_sync() {
    log "sync: pull new on-chain transactions into SQLite"
    if [ -n "$XPUB" ] && [ -n "$FINGERPRINT" ]; then
        log "sync: wallet $WALLET via $RPC_URL (user ${RPC_USER:-<none>})"
        dbg_rpc
        sync_out=$("$UTXOPROOF_BIN" sync --db "$DATA_DIR/utxoproof.db" \
            --rpc-url "$RPC_URL" --rpc-user "$RPC_USER" \
            --rpc-password "$RPC_PASSWORD" --wallet "$WALLET" 2>&1) || {
            log "warning: on-chain sync skipped (node unreachable or RPC error)"
            printf '%s\n' "$sync_out"
        }
    else
        log "note: on-chain sync skipped (set XPUB+FINGERPRINT to enable)"
    fi
}

# ---------------------------------------------------------------------------
# Stage: portfolio (overview + entity pages)
# ---------------------------------------------------------------------------
stage_portfolio() {
    log "portfolio: build overview + entity pages"
    if [ -n "$ENTITIES_FILE" ] && [ -f "$DATA_DIR/utxoproof.db" ]; then
        if [ -f "$ENTITIES_FILE" ]; then
            port_out=$("$UTXOPROOF_BIN" portfolio --db "$DATA_DIR/utxoproof.db" \
                --entities "$ENTITIES_FILE" --wallet "$WALLET" \
                --price "${BTC_PRICE:-40000}" \
                ${KRAKEN_LEDGERS:+--ledgers "$KRAKEN_LEDGERS"} \
                ${KRAKEN_TRADES:+--trades "$KRAKEN_TRADES"} \
                --out "$DATA_DIR/portfolio" 2>&1) || {
                log "warning: portfolio build skipped (entity config or DB issue)"
                printf '%s\n' "$port_out"
            }
        else
            log "warning: entities file not found: $ENTITIES_FILE"
        fi
    elif [ -n "$ENTITIES_FILE" ]; then
        log "note: portfolio skipped — DB $DATA_DIR/utxoproof.db not found; run with MODE=prepare first"
    else
        log "note: portfolio skipped (set ENTITIES_FILE to enable entity overview)"
    fi
}

# ---------------------------------------------------------------------------
# Stage: report (HTML tax report + evidence ZIP)
# ---------------------------------------------------------------------------
stage_report() {
    log "report: year $TAX_YEAR + evidence ZIP"
    if [ -n "$CONFIG_FILE" ]; then
        "$UTXOPROOF_BIN" report --input "$DATA_DIR/manual.csv" --year "$TAX_YEAR" \
            --config "$CONFIG_FILE" --source "$KRAKEN_LEDGERS" \
            ${KRAKEN_TRADES:+--source "$KRAKEN_TRADES"} \
            --out "$DATA_DIR/$TAX_YEAR"
    else
        "$UTXOPROOF_BIN" report --input "$DATA_DIR/manual.csv" --year "$TAX_YEAR" \
            --source "$KRAKEN_LEDGERS" \
            ${KRAKEN_TRADES:+--source "$KRAKEN_TRADES"} \
            --out "$DATA_DIR/$TAX_YEAR"
    fi
}

# ---------------------------------------------------------------------------
# Stage: status (holdings snapshot at price)
# ---------------------------------------------------------------------------
stage_status() {
    log "status: holdings snapshot"
    if [ -n "$BTC_PRICE" ]; then
        "$UTXOPROOF_BIN" status --input "$DATA_DIR/manual.csv" --price "$BTC_PRICE"
    else
        "$UTXOPROOF_BIN" status --input "$DATA_DIR/manual.csv" \
            --out "$DATA_DIR/current-status"
    fi
}

# ---------------------------------------------------------------------------
# Stage: all-time summary
# ---------------------------------------------------------------------------
stage_alltime() {
    log "alltime: multi-year summary"
    "$UTXOPROOF_BIN" report --input "$DATA_DIR/manual.csv" --alltime \
        --out "$DATA_DIR/alltime"
}

# ---------------------------------------------------------------------------
# Mode logic
# ---------------------------------------------------------------------------
case "$MODE" in
    prepare)
        log "mode: prepare (halts if check reports shortfalls)"
        stage_import
        shortfalls=$(stage_check)
        case "$shortfalls" in
            "shortfalls: 0") log "no shortfalls — proceeding" ;;
            *)
                fail "coverage has shortfalls ($shortfalls) — aborting per prepare mode; investigate with 'utxoproof check' or run with MODE=refresh"
                ;;
        esac
        stage_compute
        stage_setup
        stage_sync
        stage_portfolio
        stage_report
        stage_status
        ;;
    refresh)
        log "mode: refresh (idempotent rerun; shortfalls print but do not stop)"
        stage_import
        stage_check || true   # always prints; never aborts
        stage_compute
        stage_setup
        stage_sync
        stage_portfolio
        stage_report
        stage_status
        stage_alltime
        ;;
    *)
        fail "unknown MODE='$MODE'; use 'prepare' or 'refresh'"
        ;;
esac

log "done: $DATA_DIR"