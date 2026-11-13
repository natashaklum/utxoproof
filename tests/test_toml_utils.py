"""Lenient TOML loader: valid TOML stays untouched, bad lines are reported."""

from pathlib import Path

from utxoproof.toml_utils import loads_lenient, summarize_toml_warnings


def test_valid_comments_and_lists_parse_without_warnings() -> None:
    data, warnings = loads_lenient(
        '# old comment is still valid TOML\nkind = "wallet"\ntags = ["cold", "vault"]\n'
    )
    assert data == {"kind": "wallet", "tags": ["cold", "vault"]}
    assert warnings == []


def test_invalid_line_is_skipped_and_rest_is_kept() -> None:
    data, warnings = loads_lenient(
        'kind = "wallet"\nnotes: old yaml style\ntags = [unquoted, list]\nlabel = "W"\n',
        "account.toml",
    )
    assert data == {"kind": "wallet", "label": "W"}
    assert len(warnings) == 2
    assert "account.toml: ignored invalid TOML line 2" in warnings[0]
    assert "account.toml: ignored invalid TOML line 3" in warnings[1]


def test_invalid_line_warnings_collapse_into_a_log(tmp_path: Path) -> None:
    source = tmp_path / "account.toml"
    source.write_text("kind: wallet\n", encoding="utf-8")
    details = [f"{source}: ignored invalid TOML line 1: 'kind: wallet' (Expected '=' after a key)"]
    summary = summarize_toml_warnings(str(source), details)
    log = tmp_path / "WARNINGS_account.toml.log"
    assert summary == [
        f"there were invalid lines in {source} - see {log} for details",
    ]
    assert log.read_text(encoding="utf-8") == details[0] + "\n"


def test_clean_parse_removes_a_stale_log(tmp_path: Path) -> None:
    source = tmp_path / "account.toml"
    source.write_text('kind = "wallet"\n', encoding="utf-8")
    log = tmp_path / "WARNINGS_account.toml.log"
    log.write_text("stale\n", encoding="utf-8")
    assert summarize_toml_warnings(str(source), []) == []
    assert not log.exists()
