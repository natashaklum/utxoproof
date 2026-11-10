"""Lenient TOML loader: valid TOML stays untouched, bad lines are reported."""

from utxoproof.toml_utils import loads_lenient


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
