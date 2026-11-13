"""Classifier + config tests (Sec. 6 rules, Sec. 16 subset)."""

from decimal import Decimal
from pathlib import Path

from utxoproof.classifier import (
    BelgianClassificationSignals,
    BelgianClassifier,
    BelgianTaxClass,
    signals_from_csv,
)
from utxoproof.config import load_config

FIXTURE = Path(__file__).parent / "fixtures" / "manual_2023.csv"


def _signals(**overrides) -> BelgianClassificationSignals:
    base = {
        "disposal_count": 2,
        "acquisition_count": 3,
        "avg_holding_days": Decimal("200"),
        "min_holding_days": 60,
        "max_position_size_eur": Decimal("50000"),
        "btc_proceeds_eur": Decimal("10000"),
        "btc_cost_basis_eur": Decimal("8000"),
        "used_leverage": False,
        "used_derivatives": False,
        "btc_income_as_pct_total_income": Decimal("0.1"),
        "has_professional_crypto_income": False,
        "exchange_count": 1,
        "uses_dca": True,
        "uses_tax_loss_harvesting": False,
        "consecutive_active_years": 1,
    }
    base.update(overrides)
    return BelgianClassificationSignals(**base)  # type: ignore[arg-type]


def test_speculator_high_frequency_leverage() -> None:
    cls, rationale = BelgianClassifier().classify(
        _signals(
            disposal_count=25,
            avg_holding_days=Decimal("10"),
            min_holding_days=3,
            used_leverage=True,
        )
    )
    assert cls == BelgianTaxClass.SPECULATOR
    assert "leverage" in rationale and "25" in rationale


def test_passive_holder() -> None:
    cls, rationale = BelgianClassifier().classify(
        _signals(disposal_count=0, avg_holding_days=Decimal("500"))
    )
    assert cls == BelgianTaxClass.PASSIVE_HOLDER
    assert "No disposals" in rationale


def test_default_is_goede_huisvader() -> None:
    cls, _ = BelgianClassifier().classify(_signals())
    assert cls == BelgianTaxClass.GOEDE_HUISVADER


def test_custom_thresholds() -> None:
    classifier = BelgianClassifier(speculator_threshold=20)
    cls, _ = classifier.classify(_signals(disposal_count=25))
    assert cls == BelgianTaxClass.GOEDE_HUISVADER  # 3 points < 20


def test_fixture_signals_classify_goede_huisvader() -> None:
    signals = signals_from_csv(FIXTURE, 2023)
    assert signals.disposal_count == 5
    assert signals.acquisition_count == 5
    assert signals.btc_proceeds_eur > 0
    cls, rationale = BelgianClassifier().classify(signals)
    assert cls == BelgianTaxClass.GOEDE_HUISVADER
    assert "Speculator score" in rationale


def test_config_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert config.taxpayer.communal_surcharge_rate == Decimal("0.07")
    assert config.classifier.used_leverage is False

    toml_path = tmp_path / "utxoproof.toml"
    toml_path.write_text(
        '[taxpayer]\nreference = "BE-TEST"\ncommunal_surcharge_rate = 0.0587\n'
        "[classifier]\nused_leverage = true\nspeculator_threshold = 10\n"
        "btc_income_fraction = 0.6\n",
        encoding="utf-8",
    )
    config = load_config(toml_path)
    assert config.taxpayer.reference == "BE-TEST"
    assert config.taxpayer.communal_surcharge_rate == Decimal("0.0587")
    assert config.classifier.used_leverage is True
    assert config.classifier.speculator_threshold == 10
    assert config.classifier.btc_income_fraction == Decimal("0.6")

    # Self-reported leverage + income share tip the fixture into speculator territory.
    signals = signals_from_csv(FIXTURE, 2023, config.classifier)
    cls, _ = BelgianClassifier().classify(signals)
    assert cls == BelgianTaxClass.SPECULATOR


def test_config_ignores_stray_legacy_lines(tmp_path: Path) -> None:
    toml_path = tmp_path / "utxoproof.toml"
    toml_path.write_text(
        '[taxpayer]\nreference = "BE-TEST"\nold_note: this used to be here\n'
        "[classifier]\nused_leverage = true\n",
        encoding="utf-8",
    )
    config = load_config(toml_path)
    assert config.taxpayer.reference == "BE-TEST"
    assert config.classifier.used_leverage is True
    assert any("there were invalid lines in" in warning for warning in config.warnings)
    log = tmp_path / "WARNINGS_utxoproof.toml.log"
    assert log.is_file()
    assert "ignored invalid TOML line 3" in log.read_text(encoding="utf-8")


def test_find_config_search_order(tmp_path: Path, monkeypatch) -> None:
    from utxoproof.config import find_config

    workdir = tmp_path / "work"
    workdir.mkdir()
    datadir = tmp_path / "data"
    datadir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    local = workdir / "utxoproof.toml"
    local.write_text("[classifier]\nused_leverage = false\n")
    in_data = datadir / "utxoproof.toml"
    in_data.write_text("[classifier]\nused_leverage = true\n")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("UTXOPROOF_DATA_DIR", str(datadir))
    monkeypatch.setenv("HOME", str(home))  # no ~/.utxoproof/utxoproof.toml
    assert find_config() == local  # ./ wins over data dir
    local.unlink()
    assert find_config() == in_data  # data dir wins over home
    in_data.unlink()
    assert find_config() is None  # nothing anywhere
    explicit = tmp_path / "custom.toml"
    explicit.write_text("[classifier]\n")
    assert find_config(explicit) == explicit
    assert find_config(tmp_path / "absent.toml") is None


def test_import_margin_note_respects_config(tmp_path: Path, monkeypatch, capsys) -> None:
    from utxoproof.cli import main

    ledgers = Path(__file__).parent / "fixtures" / "kraken_ledgers_margin.csv"
    trades = Path(__file__).parent / "fixtures" / "kraken_trades_2023.csv"
    monkeypatch.chdir(tmp_path)  # no ./utxoproof.toml here
    monkeypatch.delenv("UTXOPROOF_DATA_DIR", raising=False)

    out = tmp_path / "m.csv"
    assert (
        main(
            [
                "import",
                "--file",
                str(ledgers),
                "--type",
                "kraken",
                "--trades",
                str(trades),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "used_leverage" in capsys.readouterr().err  # nagged: nothing set

    config = tmp_path / "utxoproof.toml"
    config.write_text("[classifier]\nused_leverage = true\n")
    out2 = tmp_path / "m2.csv"
    assert (
        main(
            [
                "import",
                "--file",
                str(ledgers),
                "--type",
                "kraken",
                "--trades",
                str(trades),
                "--out",
                str(out2),
                "--config",
                str(config),
            ]
        )
        == 0
    )
    assert "used_leverage" not in capsys.readouterr().err  # silent: already set
