"""Manual exchange events for accounts without a machine-readable export.

An old exchange often leaves only a purchase confirmation and a withdrawal
email. This parser turns a small hand-written CSV into the same
:class:`~utxoproof.exchange.ExchangeTx` records as a real export, so the
purchase enters the cost pool and the withdrawal travels with the coins.

Required columns: ``date,kind`` (``side`` is accepted as an alias). Optional:
``btc,eur_per_btc,fee_eur,refid,note,evidence,txid``. ``evidence`` is a
semicolon-separated list of files relative to the account folder; every listed
file must exist, because an evidence link that points nowhere is worse than no
link. The files are already included in the evidence ZIP as account sources.
"""

from __future__ import annotations

import csv
import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from utxoproof.exchange import ExchangeTx

KINDS = ("BUY", "SELL", "DEPOSIT", "WITHDRAWAL")


def _decimal(value: str, what: str) -> Decimal:
    text = (value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return Decimal("0")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable manual-event {what}: {value!r}") from exc


def _evidence_paths(raw: str, account_dir: Path) -> tuple[str, ...]:
    values = [part.strip() for part in raw.replace("|", ";").split(";") if part.strip()]
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = account_dir / path
        if not path.is_file():
            raise ValueError(f"manual-event evidence file not found: {path}")
    return tuple(values)


def parse_manual_events(
    path: str | Path, account_dir: str | Path | None = None
) -> list[ExchangeTx]:
    """Parse ``events.csv`` into exchange transactions.

    BUY/SELL need a positive BTC amount and a positive EUR/BTC price.
    DEPOSIT/WITHDRAWAL need only BTC; a deposit price is optional and is filled
    from the bundled daily close at compute time.
    """
    path = Path(path)
    base = Path(account_dir) if account_dir is not None else path.parent
    txs: list[ExchangeTx] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            raw_kind = (row.get("kind") or row.get("side") or "").strip().upper()
            if not raw_kind:
                raise ValueError(f"{path}: row {index}: missing kind")
            if raw_kind not in KINDS:
                raise ValueError(
                    f"{path}: row {index}: kind must be one of {', '.join(KINDS)}; got {raw_kind!r}"
                )
            raw_date = (row.get("date") or "").strip()
            try:
                date = datetime.date.fromisoformat(raw_date[:10])
            except ValueError as exc:
                raise ValueError(f"{path}: row {index}: bad date {raw_date!r}") from exc
            btc = abs(_decimal(row.get("btc", ""), "BTC amount"))
            if btc == 0:
                raise ValueError(f"{path}: row {index}: btc must be non-zero")
            price = _decimal(row.get("eur_per_btc", ""), "EUR/BTC price")
            if raw_kind in ("BUY", "SELL") and price <= 0:
                raise ValueError(f"{path}: row {index}: {raw_kind} needs eur_per_btc")
            fee = abs(_decimal(row.get("fee_eur", ""), "fee"))
            refid = (row.get("refid") or "").strip() or f"manual-{index}"
            evidence = _evidence_paths(row.get("evidence", ""), base)
            note = (row.get("note") or "").strip()
            tx = ExchangeTx(
                date,
                raw_kind,
                btc,
                price,
                fee,
                refid,
                exchange="manual",
                kyc_status=str(row.get("kyc_status") or "unknown"),
                source_type="manual_event",
                source_label=note or f"Manual {raw_kind.lower()} {btc} BTC",
                source_evidence=f"{path.name}:row:{index}",
                trade_refs=(refid,),
                details=note,
                txid=(row.get("txid") or "").strip(),
                address=(row.get("address") or "").strip(),
                evidence=evidence,
            )
            txs.append(tx)
    txs.sort(key=lambda tx: (tx.date.isoformat(), tx.refid))
    return txs
