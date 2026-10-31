"""Account-centric input tree (plan v8).

One owner, many accounts. Each ``input/accounts/<folder>/account.toml`` is the
source of truth for one wallet, bank, credit-card, exchange or broker account.
The folder name is only a default id; all semantics live in the manifest.
"""

from __future__ import annotations

import datetime
import glob
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from utxoproof.banks import BANK_PROFILES, resolve_bank
from utxoproof.config import Config, load_config
from utxoproof.descriptors import (
    build_descriptors,
    derive_descriptor_scripts,
    normalize_descriptor_pair,
)
from utxoproof.paths import data_dir


class AccountError(ValueError):
    """Invalid account manifest or input tree."""


ACCOUNT_KINDS = ("wallet", "bank", "credit_card", "exchange", "broker")
SUPPORTED_EXCHANGES = ("kraken",)


@dataclass
class Balance:
    amount_eur: Decimal
    as_of: str = ""
    note: str = ""


@dataclass
class Holding:
    symbol: str
    quantity: Decimal
    value_eur: Decimal
    as_of: str = ""
    note: str = ""


@dataclass
class Account:
    id: str
    kind: str
    label: str
    detail: str = ""
    path: Path | None = None
    tags: tuple[str, ...] = ()
    balances: list[Balance] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)

    # Wallet accounts
    network: str = "mainnet"
    gap_limit: int = 200
    descriptors: dict[str, str] = field(default_factory=dict)
    labels_file: str = ""
    utxo_tags: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Bank / credit-card accounts
    profile: str = ""
    statements: tuple[Path, ...] = ()
    currency: str = "EUR"

    # Exchange accounts
    exchange: str = ""
    ledgers: tuple[Path, ...] = ()
    trades: tuple[Path, ...] = ()

    # Broker accounts
    broker: str = ""

    @property
    def wallet(self) -> str:
        """Compatibility name used by the existing portfolio code."""
        return self.id if self.kind == "wallet" else ""

    @property
    def wallets(self) -> tuple[str, ...]:
        return (self.id,) if self.kind == "wallet" else ()

    def scripts(self) -> list[tuple[str, str, str]]:
        """Return ``(chain, address, script_hex)`` for a wallet account."""
        if self.kind != "wallet" or not self.descriptors:
            return []
        out: list[tuple[str, str, str]] = []
        for chain, key in ((0, "external"), (1, "change")):
            for address, script_hex in derive_descriptor_scripts(
                self.descriptors[key],
                branch=0,
                start=0,
                count=self.gap_limit,
                network=self.network,
            ):
                out.append((str(chain), address, script_hex))
        return out


@dataclass
class Registry:
    root: Path
    config: Config
    accounts: list[Account]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_kind(self, kind: str) -> list[Account]:
        return [account for account in self.accounts if account.kind == kind]

    def get(self, account_id: str) -> Account:
        for account in self.accounts:
            if account.id == account_id:
                return account
        raise AccountError(f"unknown account {account_id!r}")

    def fiat_holdings(self) -> list[tuple[str, Balance]]:
        return [(account.id, balance) for account in self.accounts for balance in account.balances]


def input_dir(explicit: str | Path | None = None) -> Path:
    raw = explicit or Path.cwd() / "input"
    return Path(raw).expanduser()


_PRIVATE_EXTENDED_KEY = re.compile(r"\b[xyztuvYZUV]prv[0-9A-Za-z]{20,}")


def _reject_private_keys(text: str, where: Path) -> None:
    """Account files are watch-only; never let an xprv reach disk or evidence."""
    if _PRIVATE_EXTENDED_KEY.search(text):
        raise AccountError(
            f"{where}: private extended keys are not accepted; export a watch-only descriptor"
        )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AccountError(f"{path}: {exc}") from exc
    _reject_private_keys(raw, path)
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise AccountError(f"{path}: invalid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise AccountError(f"{path}: account.toml must contain a table")
    return data


def _resolve_file(account_dir: Path, value: object, field_name: str) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = account_dir / path
    if not path.is_file():
        raise AccountError(f"{account_dir}: {field_name} not found: {path}")
    return path


def _read_descriptor_file(account_dir: Path, value: object, field_name: str) -> str:
    path = _resolve_file(account_dir, value, field_name)
    if path is None:
        return ""
    text = path.read_text(encoding="utf-8")
    _reject_private_keys(text, path)
    lines = [
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    if len(lines) != 1:
        raise AccountError(f"{path}: expected exactly one descriptor line, found {len(lines)}")
    return lines[0]


def _descriptor_pair(data: dict[str, Any], account_dir: Path, network: str) -> dict[str, str]:
    receive = str(data.get("descriptor_receive", "") or "")
    change = str(data.get("descriptor_change", "") or "")
    if not receive and data.get("descriptor_receive_file"):
        receive = _read_descriptor_file(
            account_dir, data["descriptor_receive_file"], "descriptor_receive_file"
        )
    if not change and data.get("descriptor_change_file"):
        change = _read_descriptor_file(
            account_dir, data["descriptor_change_file"], "descriptor_change_file"
        )
    if not receive and data.get("descriptor"):
        receive = str(data["descriptor"])
    if not receive and not change:
        # Conventional filenames inside an explicit account folder.
        for candidate in ("receive.txt", "descriptor.txt"):
            if (account_dir / candidate).is_file():
                receive = _read_descriptor_file(account_dir, candidate, candidate)
                break
        for candidate in ("change.txt",):
            if (account_dir / candidate).is_file():
                change = _read_descriptor_file(account_dir, candidate, candidate)
                break
    if not receive:
        raise AccountError(f"{account_dir}: wallet needs descriptor_receive or xpub")
    try:
        external, change_desc = normalize_descriptor_pair(receive, change or None, network=network)
    except ValueError as exc:
        raise AccountError(f"{account_dir}: {exc}") from exc
    return {"external": external, "change": change_desc}


def _parse_balances(data: dict[str, Any], account_dir: Path) -> list[Balance]:
    balances: list[Balance] = []
    for item in data.get("balances", []):
        if not isinstance(item, dict):
            raise AccountError(f"{account_dir}: [[balances]] entries must be tables")
        raw = item.get("amount_eur")
        if raw is None:
            raise AccountError(f"{account_dir}: balance needs amount_eur")
        as_of = str(item.get("as_of", "") or "")
        if as_of:
            try:
                datetime.date.fromisoformat(as_of)
            except ValueError as exc:
                raise AccountError(f"{account_dir}: bad balance as_of {as_of!r}") from exc
        balances.append(
            Balance(
                amount_eur=Decimal(str(raw)),
                as_of=as_of,
                note=str(item.get("note", "") or ""),
            )
        )
    return balances


def _parse_holdings(data: dict[str, Any], account_dir: Path) -> list[Holding]:
    holdings: list[Holding] = []
    for item in data.get("holdings", []):
        if not isinstance(item, dict):
            raise AccountError(f"{account_dir}: [[holdings]] entries must be tables")
        for key in ("symbol", "quantity", "value_eur"):
            if key not in item:
                raise AccountError(f"{account_dir}: holding needs {key}")
        holdings.append(
            Holding(
                symbol=str(item["symbol"]),
                quantity=Decimal(str(item["quantity"])),
                value_eur=Decimal(str(item["value_eur"])),
                as_of=str(item.get("as_of", "") or ""),
                note=str(item.get("note", "") or ""),
            )
        )
    return holdings


def _statement_paths(data: dict[str, Any], account_dir: Path) -> tuple[Path, ...]:
    raw = data.get("statements", ["statements/**/*.csv"])
    patterns = [str(raw)] if isinstance(raw, str) else [str(item) for item in raw]
    found: list[Path] = []
    for pattern in patterns:
        full = pattern if Path(pattern).is_absolute() else str(account_dir / pattern)
        found.extend(Path(p) for p in sorted(glob.glob(full, recursive=True)) if Path(p).is_file())
    # Keep deterministic order and remove exact duplicates.
    unique: dict[str, Path] = {}
    for path in found:
        unique[str(path.resolve())] = path
    return tuple(unique.values())


def _multi_paths(data: dict[str, Any], account_dir: Path, key: str) -> tuple[Path, ...]:
    raw = data.get(key)
    if raw is None:
        return ()
    values = [str(raw)] if isinstance(raw, str) else [str(item) for item in raw]
    out: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = account_dir / path
        if not path.is_file():
            raise AccountError(f"{account_dir}: {key} file not found: {path}")
        out.append(path)
    return tuple(out)


def load_account(account_dir: str | Path) -> Account:
    """Load one ``account.toml`` folder."""
    directory = Path(account_dir).expanduser()
    manifest = directory / "account.toml"
    if not manifest.is_file():
        raise AccountError(f"{directory}: missing account.toml")
    data = _read_manifest(manifest)
    kind = str(data.get("kind", "")).strip()
    if kind not in ACCOUNT_KINDS:
        raise AccountError(
            f"{manifest}: kind must be one of {', '.join(ACCOUNT_KINDS)}; got {kind!r}"
        )
    account_id = str(data.get("id", directory.name)).strip()
    if not account_id or "/" in account_id or "\\" in account_id:
        raise AccountError(f"{manifest}: invalid account id {account_id!r}")
    account = Account(
        id=account_id,
        kind=kind,
        label=str(data.get("label", account_id)),
        detail=str(data.get("detail", "") or ""),
        path=directory.resolve(),
        tags=tuple(str(tag) for tag in data.get("tags", [])),
        balances=_parse_balances(data, directory),
        holdings=_parse_holdings(data, directory),
        utxo_tags={
            str(ref): tuple(str(tag) for tag in tags)
            for ref, tags in data.get("utxo_tags", {}).items()
        },
    )
    if kind == "wallet":
        account.network = str(data.get("network", "mainnet"))
        account.gap_limit = int(data.get("gap_limit", 200))
        if account.gap_limit < 1:
            raise AccountError(f"{manifest}: gap_limit must be >= 1")
        if (
            "descriptor_receive" in data
            or "descriptor_receive_file" in data
            or ((directory / "receive.txt").is_file())
        ):
            account.descriptors = _descriptor_pair(data, directory, account.network)
        elif data.get("xpub"):
            xpub = str(data["xpub"])
            fingerprint = str(data.get("fingerprint", ""))
            if not fingerprint:
                raise AccountError(f"{manifest}: xpub shorthand needs fingerprint")
            built = build_descriptors(
                xpub,
                fingerprint,
                int(data.get("purpose", 84)),
                int(data.get("coin", 0)),
                int(data.get("account", 0)),
            )
            try:
                external, change = normalize_descriptor_pair(
                    built["external"], built["change"], network=account.network
                )
            except ValueError as exc:
                raise AccountError(f"{manifest}: {exc}") from exc
            account.descriptors = {"external": external, "change": change}
        else:
            raise AccountError(f"{manifest}: wallet needs descriptors or xpub")
        labels_name = str(data.get("labels_file", "") or "")
        labels_path = (
            _resolve_file(directory, labels_name, "labels_file")
            if labels_name
            else (directory / "labels.jsonl" if (directory / "labels.jsonl").is_file() else None)
        )
        account.labels_file = str(labels_path) if labels_path is not None else ""
    elif kind in ("bank", "credit_card"):
        profile = resolve_bank(str(data.get("profile", "")))
        if profile not in BANK_PROFILES:
            raise AccountError(
                f"{manifest}: profile must be one of {', '.join(sorted(BANK_PROFILES))}"
            )
        account.profile = profile
        account.currency = str(data.get("currency", "EUR")).upper()
        if account.currency != "EUR":
            raise AccountError(f"{manifest}: only EUR statements are supported")
        account.statements = _statement_paths(data, directory)
        if not account.statements:
            raise AccountError(f"{manifest}: no statement CSV files matched")
    elif kind == "exchange":
        exchange = str(data.get("exchange", "kraken")).lower()
        if exchange not in SUPPORTED_EXCHANGES:
            raise AccountError(
                f"{manifest}: exchange must be one of {', '.join(SUPPORTED_EXCHANGES)}"
            )
        account.exchange = exchange
        account.ledgers = _multi_paths(data, directory, "ledgers")
        account.trades = _multi_paths(data, directory, "trades")
        if not account.ledgers:
            raise AccountError(f"{manifest}: exchange needs ledgers file(s)")
    elif kind == "broker":
        account.broker = str(data.get("broker", "") or "")
        if not account.holdings:
            raise AccountError(f"{manifest}: broker account needs [[holdings]]")
    return account


def load_registry(root: str | Path | None = None) -> Registry:
    """Load ``input/accounts/*/account.toml`` and validate cross-account rules."""
    base = input_dir(root)
    config_path = base / "utxoproof.toml"
    config = load_config(config_path if config_path.is_file() else None)
    accounts_dir = base / "accounts"
    if not accounts_dir.is_dir():
        raise AccountError(f"{base}: missing accounts/ directory")
    accounts: list[Account] = []
    warnings: list[str] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for directory in sorted(accounts_dir.iterdir()):
        if not directory.is_dir() or not (directory / "account.toml").is_file():
            if directory.is_dir():
                warnings.append(f"ignored {directory.name}: no account.toml")
            continue
        try:
            account = load_account(directory)
        except AccountError as exc:
            errors.append(str(exc))
            continue
        if account.id in seen_ids:
            errors.append(f"duplicate account id {account.id!r}")
            continue
        seen_ids.add(account.id)
        accounts.append(account)
    if not accounts:
        errors.append(f"{base}: no valid accounts found under accounts/")
    # Global duplicate-script detection across every wallet account.
    script_owner: dict[str, str] = {}
    for account in accounts:
        if account.kind != "wallet":
            continue
        try:
            scripts = account.scripts()
        except ValueError as exc:
            errors.append(f"{account.id}: descriptor/derivation error: {exc}")
            continue
        for _chain, _address, script_hex in scripts:
            other = script_owner.get(script_hex)
            if other is not None and other != account.id:
                errors.append(
                    f"wallet accounts {other!r} and {account.id!r} derive the same script"
                )
                break
            script_owner[script_hex] = account.id
    if len(accounts) == 0 and not errors:
        errors.append(f"{base}: no accounts")
    return Registry(
        root=base,
        config=config,
        accounts=accounts,
        warnings=warnings,
        errors=list(dict.fromkeys(errors)),
    )


def registry_summary(registry: Registry) -> list[str]:
    lines = [
        f"input: {registry.root}",
        f"owner: {registry.config.owner.name}",
        f"chain: {registry.config.chain.backend} ({registry.config.chain.network})",
        f"accounts: {len(registry.accounts)}",
    ]
    for account in registry.accounts:
        if account.kind == "wallet":
            detail = f"{len(account.descriptors)} descriptor(s), gap {account.gap_limit}"
        elif account.kind in ("bank", "credit_card"):
            detail = f"{account.profile}, {len(account.statements)} statement(s)"
        elif account.kind == "exchange":
            detail = f"{account.exchange}, {len(account.ledgers)} ledger(s)"
        elif account.kind == "broker":
            detail = f"{account.broker}, {len(account.holdings)} holding(s)"
        else:
            detail = ""
        lines.append(
            f"  {account.id}: {account.kind} — {account.label}" + (f" [{detail}]" if detail else "")
        )
    lines.extend(f"WARNING: {warning}" for warning in registry.warnings)
    lines.extend(f"ERROR: {error}" for error in registry.errors)
    return lines


def output_data_dir(explicit: str | None = None) -> Path:
    """Default data dir for the v8 runner, shared with the existing CLI."""
    return data_dir(explicit)


# ── DB mirror + attribution ─────────────────────────────────────────


def persist_registry(db: Any, registry: Registry) -> None:
    """Mirror manifests and all derived scripts into SQLite.

    Full replace: the input tree is the source of truth. Any script claimed by
    two accounts is rejected before a single row is written.
    """
    script_owner: dict[str, str] = {}
    address_owner: dict[str, str] = {}
    rows: list[tuple[str, int, int, str, str]] = []
    for account in registry.accounts:
        if account.kind != "wallet":
            continue
        for chain, address, script_hex in account.scripts():
            if script_hex in script_owner and script_owner[script_hex] != account.id:
                raise AccountError(
                    f"script collision between {script_owner[script_hex]!r} and {account.id!r}"
                )
            if address in address_owner and address_owner[address] != account.id:
                raise AccountError(
                    f"address collision between {address_owner[address]!r} and {account.id!r}"
                )
            script_owner[script_hex] = account.id
            address_owner[address] = account.id
            # Derivation index is recoverable from the order per chain; use the
            # stored position rather than parsing the descriptor.
            chain_int = int(chain)
            index = sum(1 for row in rows if row[0] == account.id and row[1] == chain_int)
            rows.append((account.id, chain_int, index, address, script_hex))
    db.execute("DELETE FROM account_scripts")
    # Labels are account-scoped: drop rows for accounts that no longer exist,
    # while leaving labels of surviving accounts (incl. user rows) untouched.
    placeholders = ",".join("?" for _ in registry.accounts)
    if registry.accounts:
        db.execute(
            f"DELETE FROM labels WHERE account_id NOT IN ({placeholders})",  # noqa: S608 - placeholders only
            [a.id for a in registry.accounts],
        )
    else:
        db.execute("DELETE FROM labels")
    db.execute("DELETE FROM accounts")
    for account in registry.accounts:
        db.execute(
            "INSERT INTO accounts (id,kind,label,detail,network,tags,source_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                account.id,
                account.kind,
                account.label,
                account.detail,
                account.network,
                ",".join(account.tags),
                str(account.path or ""),
            ),
        )
    db.executemany(
        "INSERT INTO account_scripts (account_id,chain,derivation_index,address,script_hex) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    db.commit()


def attribute_outputs(db: Any, registry: Registry, strict: bool = True) -> int:
    """Set ``tx_outputs.account_id`` from the script registry.

    Returns the number of outputs attributed to an account. With ``strict``,
    unmatched outputs are marked external (``is_mine=0``); use strict only
    after a sync that fetched exactly the watched graph.
    """
    by_script: dict[str, str] = {}
    by_address: dict[str, str] = {}
    for account in registry.accounts:
        if account.kind != "wallet":
            continue
        for _chain, address, script_hex in account.scripts():
            by_script[script_hex] = account.id
            by_address[address] = account.id
    attributed = 0
    rows = db.execute(
        "SELECT txid, vout, address, script_hex, account_id, is_mine FROM tx_outputs"
    ).fetchall()
    for txid, vout, address, script_hex, old_account, old_mine in rows:
        owner = by_script.get(str(script_hex or "")) or by_address.get(str(address or ""))
        if owner:
            attributed += 1
            if owner != old_account or not old_mine:
                db.execute(
                    "UPDATE tx_outputs SET account_id=?, is_mine=1 WHERE txid=? AND vout=?",
                    (owner, txid, vout),
                )
        elif strict and (old_account is not None or old_mine):
            db.execute(
                "UPDATE tx_outputs SET account_id=NULL, is_mine=0 WHERE txid=? AND vout=?",
                (txid, vout),
            )
    db.commit()
    return attributed


def account_for_script(db: Any, script_hex: str) -> str | None:
    row = db.execute(
        "SELECT account_id FROM account_scripts WHERE script_hex=?", (script_hex,)
    ).fetchone()
    return str(row[0]) if row else None


def account_for_address(db: Any, address: str) -> str | None:
    row = db.execute(
        "SELECT account_id FROM account_scripts WHERE address=?", (address,)
    ).fetchone()
    return str(row[0]) if row else None


def portfolio_config(
    registry: Registry,
) -> tuple[list[Any], list[Any], dict[str, tuple[str, ...]]]:
    """Convert the registry into portfolio accounts, fiat rows and UTXO tags."""
    from utxoproof.portfolio import Account as PortfolioAccount
    from utxoproof.portfolio import FiatHolding

    accounts: list[PortfolioAccount] = []
    fiat: list[FiatHolding] = []
    utxo_tags: dict[str, tuple[str, ...]] = {}
    for account in registry.accounts:
        accounts.append(
            PortfolioAccount(
                id=account.id,
                kind=account.kind,
                label=account.label,
                detail=account.detail,
                wallet=account.id if account.kind == "wallet" else "",
                tags=account.tags,
                statements=tuple(str(path) for path in account.statements),
                holdings=tuple(
                    (holding.symbol, holding.quantity, holding.value_eur)
                    for holding in account.holdings
                ),
            )
        )
        for balance in account.balances:
            fiat.append(
                FiatHolding(
                    entity_id=account.id,
                    amount_eur=balance.amount_eur,
                    note=balance.note or account.label,
                    as_of=balance.as_of,
                )
            )
        utxo_tags.update(account.utxo_tags)
    return accounts, fiat, utxo_tags
