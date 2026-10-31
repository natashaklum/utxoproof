# Example v8 input tree

One owner, many accounts. Each `accounts/<folder>/account.toml` is the source
of truth. Run `utxoproof discover --input examples/input_v8` (offline) and
`utxoproof run --input examples/input_v8 --data-dir /tmp/utxoproof-demo --offline`
to exercise the account workflow without a node. The wallet descriptors are
demo-only keys; replace them with Sparrow/Core exports for real funds.

Bank and credit-card files are copied from the test fixtures. Broker holdings
are informational until Belgian stock tax rules are specified.
