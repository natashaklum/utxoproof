"""utxoproof.toml configuration (plan Sec. 16 subset, Sprint 7).

Covers [taxpayer], [classifier] and [accounting]; bitcoin/price/output sections
arrive with their sprints. Missing file -> defaults; unknown keys ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from utxoproof.toml_utils import TomlError, loads_lenient, summarize_toml_warnings


@dataclass
class TaxpayerConfig:
    reference: str = "BE-LOCAL"
    communal_surcharge_rate: Decimal = Decimal("0.07")


@dataclass
class ClassifierConfig:
    speculator_threshold: int = 8
    passive_threshold: int = 5
    used_leverage: bool = False
    used_derivatives: bool = False
    has_professional_crypto_income: bool = False
    btc_income_fraction: Decimal = Decimal("0")


@dataclass
class AccountingConfig:
    method: str = "moving_average"


@dataclass
class OwnerConfig:
    name: str = "self"


@dataclass
class ChainConfig:
    backend: str = "electrum"  # electrum | core
    network: str = "mainnet"
    electrum_url: str = "ssl://127.0.0.1:50002"
    electrum_ca_file: str = ""
    rpc_url: str = "http://127.0.0.1:8332"
    rpc_user: str = ""
    rpc_password: str = ""
    core_wallet: str = "utxoproof_watchonly"


@dataclass
class ReportingConfig:
    price: Decimal | None = None
    as_of: str = ""
    tax_year: str = ""


@dataclass
class Config:
    taxpayer: TaxpayerConfig = field(default_factory=TaxpayerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    accounting: AccountingConfig = field(default_factory=AccountingConfig)
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    warnings: list[str] = field(default_factory=list)


def _decimal(value: object, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def find_config(path: str | Path | None = None) -> Path | None:
    """Locate the effective config file: flag > ./ > $DATA_DIR > ~/.utxoproof."""
    if path:
        candidate = Path(path).expanduser()
        return candidate.absolute() if candidate.is_file() else None
    data_home = os.environ.get("UTXOPROOF_DATA_DIR")
    candidates = [Path("utxoproof.toml").absolute()]
    if data_home:
        candidates.append(Path(data_home).expanduser() / "utxoproof.toml")
    candidates.append(Path("~/.utxoproof/utxoproof.toml").expanduser())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> Config:
    """Load utxoproof.toml; fall back to defaults when absent.

    Stray invalid lines are skipped with a warning instead of aborting;
    ``Config.warnings`` carries them to the CLI/discovery output.
    """
    data: dict[str, Any] = {}
    warnings: list[str] = []
    found = find_config(path)
    if found is not None:
        try:
            raw = found.read_text(encoding="utf-8")
            loaded, parse_warnings = loads_lenient(raw, str(found))
            warnings = summarize_toml_warnings(str(found), parse_warnings)
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"{found}: could not read config: {exc}")
            loaded = {}
        except TomlError as exc:
            warnings.append(f"{exc}; using defaults")
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded

    taxpayer = data.get("taxpayer", {})
    classifier = data.get("classifier", {})
    accounting = data.get("accounting", {})
    owner = data.get("owner", {})
    chain = data.get("chain", {})
    reporting = data.get("reporting", {})
    price_raw = reporting.get("price")
    return Config(
        taxpayer=TaxpayerConfig(
            reference=str(taxpayer.get("reference", "BE-LOCAL")),
            communal_surcharge_rate=_decimal(
                taxpayer.get("communal_surcharge_rate", "0.07"), Decimal("0.07")
            ),
        ),
        classifier=ClassifierConfig(
            speculator_threshold=int(classifier.get("speculator_threshold", 8)),
            passive_threshold=int(classifier.get("passive_threshold", 5)),
            used_leverage=bool(classifier.get("used_leverage", False)),
            used_derivatives=bool(classifier.get("used_derivatives", False)),
            has_professional_crypto_income=bool(
                classifier.get("has_professional_crypto_income", False)
            ),
            btc_income_fraction=_decimal(classifier.get("btc_income_fraction", "0"), Decimal("0")),
        ),
        accounting=AccountingConfig(method=str(accounting.get("method", "moving_average"))),
        owner=OwnerConfig(name=str(owner.get("name", "self"))),
        chain=ChainConfig(
            backend=str(chain.get("backend", "electrum")).strip().lower(),
            network=str(chain.get("network", "mainnet")).strip().lower(),
            electrum_url=str(chain.get("electrum_url", "ssl://127.0.0.1:50002")),
            electrum_ca_file=str(chain.get("electrum_ca_file", "")),
            rpc_url=str(chain.get("rpc_url", "http://127.0.0.1:8332")),
            rpc_user=str(chain.get("rpc_user", "")),
            rpc_password=str(chain.get("rpc_password", "")),
            core_wallet=str(chain.get("core_wallet", "utxoproof_watchonly")),
        ),
        reporting=ReportingConfig(
            price=_decimal(price_raw, Decimal("0")) if price_raw is not None else None,
            as_of=str(reporting.get("as_of", "")),
            tax_year=str(reporting.get("tax_year", "")),
        ),
        warnings=warnings,
    )
