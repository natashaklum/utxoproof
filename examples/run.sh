#!/usr/bin/env bash
# Thin v8 wrapper: all orchestration lives in `utxoproof run`.
# Copy this next to your input tree and data dir, then edit the variables or
# override them from the environment.
#
#   INPUT=~/utxoproof-data/input DATA_DIR=~/utxoproof-data bash examples/run.sh
#   TAX_YEAR=2023 bash examples/run.sh
#   PLAN=1 bash examples/run.sh
#   OFFLINE=1 bash examples/run.sh          # imports/reports, no Electrum/Core
#
# The previous self-contained v7 script is examples/run_legacy.sh.
set -euo pipefail

: "${INPUT:=${UTXOPROOF_INPUT:-$PWD/input}}"
: "${DATA_DIR:=${UTXOPROOF_DATA_DIR:-$HOME/utxoproof-data}}"
if [ -z "${UTXOPROOF_BIN:-}" ]; then
  repo_bin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/utxoproof"
  if [ -x "$repo_bin" ]; then
    UTXOPROOF_BIN="$repo_bin"
  else
    UTXOPROOF_BIN="utxoproof"
  fi
fi

args=(run --input "$INPUT" --data-dir "$DATA_DIR")
[ -n "${TAX_YEAR:-}" ] && args+=(--year "$TAX_YEAR")
[ -n "${OFFLINE:-}" ] && args+=(--offline)
[ -n "${PLAN:-}" ] && args+=(--plan)
[ -n "${ONLY:-}" ] && args+=(--only "$ONLY")
[ -n "${FORCE_RESCAN:-}" ] && args+=(--force-rescan)
[ -n "${BTC_PRICE:-}" ] && args+=(--price "$BTC_PRICE")
[ -n "${AS_OF:-}" ] && args+=(--as-of "$AS_OF")
[ -n "${NO_ZIP:-}" ] && args+=(--no-zip)

exec "$UTXOPROOF_BIN" "${args[@]}" "$@"
