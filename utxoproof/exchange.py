"""Shared exchange-trade record (Sprint 8).

All exchange CSV parsers yield ``ExchangeTx`` (DaLI-plugin-shaped for later);
``to_manual_csv_rows`` feeds ``compute``. Amounts are exact Decimals; non-EUR
fiat legs are recorded but converted 1:1 only for EUR (ECB conversion pending).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class FiatFlow:
    """A fiat deposit or withdrawal on an exchange (saga bank layer).

    ``eur`` is always positive; ``kind`` carries the direction. These rows are
    deliberately separate from ``ExchangeTx`` because the ledger parser drops
    non-BTC groups: a bank debit can only be joined to a deposit if the euro
    leg survives parsing.
    """

    date: datetime.date
    kind: str  # DEPOSIT | WITHDRAWAL
    eur: Decimal
    refid: str
    exchange: str = "kraken"
    source_evidence: str = ""


@dataclass
class ExchangeTx:
    date: datetime.date
    kind: str  # BUY | SELL | MARGIN | ROLLOVER | ADJUSTMENT | WITHDRAWAL | DEPOSIT
    btc: Decimal
    eur_per_btc: Decimal
    fee_eur: Decimal
    refid: str
    exchange: str = ""
    kyc_status: str = "kyc"
    source_type: str = "exchange_purchase"
    source_label: str = ""
    source_evidence: str = ""
    margin: bool = False
    wallet: str = ""
    subclass: str = ""
    trade_refs: tuple[str, ...] = ()

    @property
    def fiat_amount(self) -> Decimal:
        """Absolute fiat leg implied by price (fees excluded)."""
        return abs(self.btc * self.eur_per_btc)


def to_manual_csv_rows(txs: list[ExchangeTx]) -> list[dict[str, str]]:
    """Convert trade records to manual-CSV rows consumable by ``compute``.

    BUY/SELL pass through; MARGIN settles like a SELL (taxable disposal);
    ROLLOVER becomes a zero-BTC BUY carrying just the financing fee into the
    cost pool; DEPOSIT rows pass through for receipt-date valuation at compute
    time; WITHDRAWAL rows pass through to carry basis out (non-taxable moves).
    The original ``kind`` is preserved in its own column (compute
    ignores extra columns) so reports can show margin lines separately.
    """
    rows = []
    for tx in txs:
        if tx.kind == "MARGIN":
            # Fee-only margin rows (zero amount) carry cost like ROLLOVER;
            # funded margin closes dispose like SELL.
            if tx.btc == 0:
                side, btc = "BUY", Decimal("0")
            else:
                side, btc = "SELL", tx.btc
        elif tx.kind == "ADJUSTMENT":
            side, btc = "SELL", tx.btc
        elif tx.kind == "ROLLOVER":
            side, btc = "BUY", Decimal("0")
        elif tx.kind in ("BUY", "SELL", "DEPOSIT", "WITHDRAWAL"):
            side, btc = tx.kind, tx.btc
        else:
            continue
        rows.append(
            {
                "date": tx.date.isoformat(),
                "side": side,
                "kind": tx.kind,
                "trade_refs": ";".join(tx.trade_refs),
                "btc": str(btc),
                "eur_per_btc": str(tx.eur_per_btc),
                "fee_eur": str(tx.fee_eur),
            }
        )
    return rows
