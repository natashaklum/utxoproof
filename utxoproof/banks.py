"""Belgian bank statement parsers (plan Sec. 11, Sprint 8).

One engine, per-bank profiles. ALL headers below are best-effort
(UNVERIFIED against live exports): columns are matched by alias lists so a
renamed header only needs an alias entry, not a code change. All banks use
`;` delimiters; files open as UTF-8-SIG so a BOM is harmless.
"""

from __future__ import annotations

import csv
import datetime
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass
class BankRow:
    date: datetime.date
    description: str
    amount_eur: Decimal
    counterparty_iban: str | None
    reference: str | None
    source_file: str = ""


@dataclass
class BankProfile:
    date_cols: tuple[str, ...]
    date_fmt: str
    amount_cols: tuple[str, ...]
    decimal_sep: str  # "," (Belgian) or "." (plain)
    description_cols: tuple[str, ...]
    iban_cols: tuple[str, ...]
    reference_cols: tuple[str, ...]
    # Split-amount statements (e.g. credit-card exports): pre-converted EUR
    # first, else signed credit/debit (abs + applied sign, robust to both
    # signed and unsigned layouts), else amount (x rate for non-EUR).
    credit_cols: tuple[str, ...] = ()
    debit_cols: tuple[str, ...] = ()
    eur_amount_cols: tuple[str, ...] = ()
    currency_cols: tuple[str, ...] = ()
    rate_cols: tuple[str, ...] = ()
    # Extra description parts ("merchant — city, country"); fee columns whose
    # non-zero values surface in the description (BankRow stays amount-only).
    description_extra_cols: tuple[str, ...] = ()
    fee_cols: tuple[str, ...] = ()


BANK_PROFILES: dict[str, BankProfile] = {
    # UNVERIFIED headers; alias lists absorb renames.
    "ing": BankProfile(
        date_cols=("Datum", "Date", "Boekdatum"),
        date_fmt="%d/%m/%Y",
        amount_cols=("Bedrag", "Amount"),
        decimal_sep=",",
        description_cols=("Omschrijving", "Description", "Mededeling"),
        iban_cols=("Rekening tegenpartij", "Tegenrekening", "Counterparty"),
        reference_cols=("Referentie", "Reference", "Gestructureerde mededeling"),
    ),
    "kbc-bankaccount": BankProfile(
        date_cols=("Datum", "Date"),
        date_fmt="%d/%m/%Y",
        amount_cols=("Bedrag", "Amount"),
        decimal_sep=",",
        description_cols=("Omschrijving", "Description"),
        iban_cols=("Tegenpartijrekening", "Tegenrekening", "Counterparty"),
        reference_cols=("Referentie", "Reference", "Mededeling"),
    ),
    # KBC credit-card exports (Dutch, observed 2025): split credit/debit
    # columns, merchant instead of IBAN, settlement date first. Card number
    # and holder columns are deliberately NOT mapped (PII, no tax value).
    "kbc-creditcard": BankProfile(
        date_cols=("Datum verrekening", "datum verrichting", "Date"),
        date_fmt="%d/%m/%Y",
        amount_cols=("bedrag",),
        decimal_sep=",",
        credit_cols=("credit",),
        debit_cols=("debet",),
        eur_amount_cols=("bedrag in EUR",),
        currency_cols=("munt",),
        rate_cols=("koers",),
        fee_cols=("Kosten op verrichting",),
        description_cols=("Handelaar",),
        description_extra_cols=("locatie", "land"),
        iban_cols=(),
        reference_cols=("toelichting", "uitgavenstaat", "Referentie"),
    ),
    "bnp": BankProfile(
        date_cols=("Datum", "Date", "Transaction date"),
        date_fmt="%d/%m/%Y",
        amount_cols=("Bedrag", "Amount", "Montant"),
        decimal_sep=",",
        description_cols=("Omschrijving", "Description", "Libellé"),
        iban_cols=("Tegenpartij", "Contrepartie", "Counterparty"),
        reference_cols=("Referentie", "Référence", "Communication"),
    ),
    "belfius": BankProfile(
        date_cols=("Datum", "Date"),
        date_fmt="%d/%m/%Y",
        amount_cols=("Bedrag", "Amount"),
        decimal_sep=",",
        description_cols=("Omschrijving", "Description"),
        iban_cols=("Tegenrekening", "Counterparty"),
        reference_cols=("Mededeling", "Reference"),
    ),
    "argenta": BankProfile(
        date_cols=("Datum", "Date"),
        date_fmt="%d/%m/%Y",
        amount_cols=("Bedrag", "Amount"),
        decimal_sep=",",
        description_cols=("Omschrijving", "Description"),
        iban_cols=("Tegenrekening", "Counterparty"),
        reference_cols=("Mededeling", "Reference"),
    ),
}


# Backward-compatible ids (kept working byte-for-byte).
BANK_ALIASES = {
    "kbc": "kbc-bankaccount",
}

# Valid --type/--bank-type values (canonical ids + aliases).
BANK_TYPES = tuple(BANK_PROFILES) + tuple(BANK_ALIASES)


def resolve_bank(bank: str) -> str:
    """Map a user-facing bank id to its canonical profile."""
    return BANK_ALIASES.get(bank, bank)


def split_bank_spec(spec: str, default_type: str) -> tuple[str, str]:
    """Split a ``type:path`` spec (or bare path using ``default_type``).

    Returns ``(path, type)`` with the type validated. Whitespace around both
    parts is stripped; paths with spaces are not supported (word splitting).
    """
    if ":" in spec:
        raw_type, _, raw_path = spec.partition(":")
        path, bank_type = raw_path.strip(), raw_type.strip() or default_type
    else:
        path, bank_type = spec.strip(), default_type
    canonical = resolve_bank(bank_type)
    if canonical not in BANK_PROFILES:
        raise ValueError(
            f"Unknown bank {bank_type!r} in {spec!r}; expected one of {sorted(BANK_TYPES)}"
        )
    if not path:
        raise ValueError(f"Empty path in bank spec {spec!r}")
    return path, canonical


def parse_bank_specs(specs: list[str], default_type: str) -> list[BankRow]:
    """Parse several statements (``type:path`` or bare paths) into merged rows.

    Every row keeps its own ``source_file`` so merged review stays attributable.
    """
    rows: list[BankRow] = []
    for spec in specs:
        if not spec.strip():
            continue
        path, bank_type = split_bank_spec(spec, default_type)
        rows.extend(parse_bank_csv(path, bank_type))
    rows.sort(key=lambda r: (r.date.isoformat(), r.source_file))
    return rows


def _pick(row: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def parse_amount(raw: str, decimal_sep: str) -> Decimal:
    """Parse Belgian `1.234,56` or plain `1234.56` amounts."""
    text = raw.strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return Decimal("0")
    if decimal_sep == ",":
        text = text.replace(".", "").replace(",", ".")
    return Decimal(text)


def _resolve_amount(row: dict[str, str], profile: BankProfile, hint: str) -> Decimal:
    """Statement amount in EUR (signed).

    Pre-converted EUR column first (direction from credit/debit presence);
    else the original-currency figure (credit, debit, then amount) converted
    at the row rate for non-EUR currencies. Credit/debit values are
    abs()-resolved with applied sign — robust to signed and unsigned layouts.
    A non-EUR row without a rate raises instead of guessing FX.
    """
    sep = profile.decimal_sep
    eur_raw = _pick(row, profile.eur_amount_cols)
    credit_raw = _pick(row, profile.credit_cols)
    debit_raw = _pick(row, profile.debit_cols)
    currency = _pick(row, profile.currency_cols)
    foreign = bool(currency) and currency.strip().upper() not in ("EUR", "€")
    if eur_raw:
        # Pre-converted figure is authoritative in any currency.
        amount = parse_amount(eur_raw, sep)
        if credit_raw and not debit_raw:
            return abs(amount)
        if debit_raw and not credit_raw:
            return -abs(amount)
        return amount
    base_raw = credit_raw or debit_raw or _pick(row, profile.amount_cols)
    if not base_raw:
        return Decimal("0")
    if credit_raw and not debit_raw:
        sign: Decimal = Decimal("1")
    elif debit_raw and not credit_raw:
        sign = Decimal("-1")
    else:
        value = parse_amount(base_raw, sep)
        sign = Decimal("-1") if value < 0 else Decimal("1")
    value = abs(parse_amount(base_raw, sep))
    if foreign:
        rate_raw = _pick(row, profile.rate_cols)
        if not rate_raw:
            raise ValueError(f"non-EUR row without rate ({hint}): {currency}")
        value *= parse_amount(rate_raw, sep)
    return sign * value


def _describe(row: dict[str, str], profile: BankProfile) -> str:
    """Main description plus location-style extras and non-zero fees."""
    desc = _pick(row, profile.description_cols)
    extras = [_pick(row, (col,)) for col in profile.description_extra_cols]
    extras = [e for e in extras if e]
    if extras:
        desc = f"{desc} — {', '.join(extras)}" if desc else ", ".join(extras)
    fee_raw = _pick(row, profile.fee_cols)
    if fee_raw:
        fee = parse_amount(fee_raw, profile.decimal_sep)
        if fee != 0:
            desc = f"{desc} (+ €{fee:,.2f} fee)" if desc else f"+ €{fee:,.2f} fee"
    return desc


def parse_bank_csv(path: str | Path, bank: str) -> list[BankRow]:
    """Parse one bank statement file with the bank's profile."""
    canonical = resolve_bank(bank)
    if canonical not in BANK_PROFILES:
        raise ValueError(f"Unknown bank {bank!r}; expected one of {sorted(BANK_TYPES)}")
    profile = BANK_PROFILES[canonical]
    path = Path(path)
    rows: list[BankRow] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        # Strip header whitespace (CRLF exports, padded headers) so aliases match.
        reader.fieldnames = [(name or "").strip() for name in reader.fieldnames or []]
        for row in reader:
            date_raw = _pick(row, profile.date_cols)
            if not date_raw:
                continue
            hint = f"{path.name}:{reader.line_num}"
            rows.append(
                BankRow(
                    date=datetime.datetime.strptime(date_raw[:10], profile.date_fmt).date(),  # noqa: DTZ007
                    description=_describe(row, profile),
                    amount_eur=_resolve_amount(row, profile, hint),
                    counterparty_iban=_pick(row, profile.iban_cols) or None,
                    reference=_pick(row, profile.reference_cols) or None,
                    source_file=str(path),
                )
            )
    rows.sort(key=lambda r: r.date.isoformat())
    return rows
