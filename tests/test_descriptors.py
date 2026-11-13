"""Descriptor + address derivation tests against BIP32/BIP84 spec vectors."""

import pytest
from embit import bip32

from utxoproof.descriptors import build_descriptors, derive_addresses, validate_account_xpub

# BIP84 test vector (mnemonic abandon x11 + about), account m/84'/0'/0'.
BIP84_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCTo"
    "UG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)

# BIP32 test vector 1.
BIP32_SEED_HEX = "000102030405060708090a0b0c0d0e0f"
BIP32_M0H_XPUB = (
    "xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1VTsfTFU"
    "HCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw"
)
# Same BIP32 vector at Bisq's two BTC keychains. Bisq segwit is native P2WPKH
# on m/44'/0'/1', so a zpub prefix alone is not enough to describe it.
BISQ_LEGACY_XPUB = (
    "xpub6CDEarkRoiwWPj3n3gYygGwgoGchxYg3g6Zs5L2nB4B6wdojzcWCKKHMu9XuY1GyYygRfrVembjAko1T5x"
    "Tsxj7ecKXxEPzDxx7nCK8Dxtx"
)
BISQ_SEGWIT_ZPUB = (
    "zpub6qsmCC6G762U9EBWshJ26LaGg2LS68YP9gfsbJ1jcwEP2MShvyXgw19BLkYn9D5Qs45UmJUfAKMSHkVp"
    "fiDVcMcB7q2oALJ3nJUnbjmfUE8"
)


def test_bip84_receiving_addresses() -> None:
    assert derive_addresses(BIP84_ZPUB, 0, 0, 2) == [
        "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
        "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g",
    ]


def test_bip84_change_address() -> None:
    assert derive_addresses(BIP84_ZPUB, 1, 0, 1) == ["bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"]


def test_bip32_seed_to_hardened_account_xpub() -> None:
    master = bip32.HDKey.from_seed(bytes.fromhex(BIP32_SEED_HEX))
    account = master.derive("m/0h").to_public()
    assert account.to_base58() == BIP32_M0H_XPUB


def test_bip84_mnemonic_to_account_zpub() -> None:
    from embit import bip39

    seed = bip39.mnemonic_to_seed(" ".join(["abandon"] * 11 + ["about"]))
    master = bip32.HDKey.from_seed(seed)
    assert master.my_fingerprint.hex() == "73c5da0a"
    account = master.derive("m/84h/0h/0h").to_public()
    assert account.to_base58(version=b"\x04\xb2\x47\x46") == BIP84_ZPUB


def test_descriptor_templates() -> None:
    xpub = BIP32_M0H_XPUB
    fp = "aabbccdd"
    assert build_descriptors(xpub, fp, 84, 0, 0) == {
        "external": f"wpkh([{fp}/84h/0h/0h]{xpub}/0/*)",
        "change": f"wpkh([{fp}/84h/0h/0h]{xpub}/1/*)",
    }
    legacy = build_descriptors(xpub, fp, 44, 0, 0)
    assert legacy["external"] == f"pkh([{fp}/44h/0h/0h]{xpub}/0/*)"
    nested = build_descriptors(xpub, fp, 49, 0, 0)
    assert nested["external"] == f"sh(wpkh([{fp}/49h/0h/0h]{xpub}/0/*))"
    assert nested["change"] == f"sh(wpkh([{fp}/49h/0h/0h]{xpub}/1/*))"
    taproot = build_descriptors(xpub, fp, 86, 0, 0)
    assert taproot["external"] == f"tr([{fp}/86h/0h/0h]{xpub}/0/*)"


def test_bisq_legacy_and_segwit_descriptors() -> None:
    master = bip32.HDKey.from_seed(bytes.fromhex(BIP32_SEED_HEX))
    assert master.derive("m/44h/0h/0h").to_public().to_base58() == BISQ_LEGACY_XPUB
    segwit = master.derive("m/44h/0h/1h").to_public()
    assert segwit.to_base58(version=b"\x04\xb2\x47\x46") == BISQ_SEGWIT_ZPUB

    legacy = build_descriptors(BISQ_LEGACY_XPUB, "", 44, 0, 0, script_type="pkh")
    assert legacy == {
        "external": f"pkh({BISQ_LEGACY_XPUB}/0/*)",
        "change": f"pkh({BISQ_LEGACY_XPUB}/1/*)",
    }

    # No fingerprint: Bisq Wallet Info does not always show one.
    segwit = build_descriptors(
        BISQ_SEGWIT_ZPUB,
        "",
        84,
        0,
        0,
        script_type="wpkh",
        derivation="m/44'/0'/1'",
    )
    assert segwit == {
        "external": f"wpkh({BISQ_SEGWIT_ZPUB}/0/*)",
        "change": f"wpkh({BISQ_SEGWIT_ZPUB}/1/*)",
    }

    with_fingerprint = build_descriptors(
        BISQ_SEGWIT_ZPUB,
        "aabbccdd",
        84,
        0,
        0,
        script_type="wpkh",
        derivation="44h/0h/1h",
    )
    assert with_fingerprint["external"] == (f"wpkh([aabbccdd/44h/0h/1h]{BISQ_SEGWIT_ZPUB}/0/*)")


def test_script_type_and_derivation_overrides() -> None:
    explicit = build_descriptors(
        BIP32_M0H_XPUB,
        "aabbccdd",
        44,
        0,
        1,
        script_type="wpkh",
        derivation="44'/0'/1'",
    )
    assert explicit["external"] == (f"wpkh([aabbccdd/44h/0h/1h]{BIP32_M0H_XPUB}/0/*)")
    nested = build_descriptors(BIP32_M0H_XPUB, "", 84, script_type="sh-wpkh")
    assert nested["external"] == f"sh(wpkh({BIP32_M0H_XPUB}/0/*))"
    with pytest.raises(ValueError, match="Unsupported script_type"):
        build_descriptors(BIP32_M0H_XPUB, "", 84, script_type="wsh")
    with pytest.raises(ValueError, match="invalid derivation"):
        build_descriptors(BIP32_M0H_XPUB, "", 84, derivation="m/44x/0")


def test_regtest_addresses_use_bcrt_hrp() -> None:
    addrs = derive_addresses(BIP84_ZPUB, 0, 0, 1, network="regtest")
    assert addrs[0].startswith("bcrt1")


def test_descriptor_checksum_accepted_by_core() -> None:
    # Checksum value verified against a real regtest node
    # (importdescriptors success); key derived from the BIP84 vector zpub.
    from embit import bip32

    from utxoproof.descriptors import descriptor_checksum, with_checksum

    tpub = bip32.HDKey.from_base58(BIP84_ZPUB).to_base58(version=b"\x04\x35\x87\xcf")
    desc = f"wpkh([00000000/84h/1h/0h]{tpub}/0/*)"
    assert descriptor_checksum(desc) == "hcelrxxg"
    assert with_checksum(desc).endswith("#hcelrxxg")
    assert with_checksum(desc + "#hcelrxxg") == desc + "#hcelrxxg"
    with pytest.raises(ValueError):
        descriptor_checksum("wpkh(\x01invalid)")


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        validate_account_xpub("not-an-xpub")
    with pytest.raises(ValueError):
        build_descriptors(BIP32_M0H_XPUB, "xyz", 84, 0, 0)
    with pytest.raises(ValueError):
        build_descriptors(BIP32_M0H_XPUB, "aabbccdd", 45, 0, 0)
    with pytest.raises(ValueError):
        derive_addresses(BIP84_ZPUB, 0, 0, 1, network="mainnet")
    with pytest.raises(ValueError):
        derive_addresses(BIP84_ZPUB, 2, 0, 1)


def test_slip132_descriptor_is_converted_for_core() -> None:
    from utxoproof.descriptors import descriptor_for_core, with_checksum

    zpub = (
        "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCTo"
        "UG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
    )
    core = descriptor_for_core(f"wpkh({zpub}/<0;1>/*)")
    assert "zpub" not in core
    assert core.startswith("wpkh(xpub")
    assert core.endswith("#" + core.rsplit("#", 1)[1])
    # Re-checking an xpub descriptor is stable and keeps a valid checksum.
    assert descriptor_for_core(core) == core
    assert core == with_checksum(core.split("#")[0])


def test_slip132_testnet_descriptor_uses_tpub() -> None:
    from embit import bip32

    from utxoproof.descriptors import descriptor_for_core

    key = bip32.HDKey.from_seed(b"\x01" * 16).derive("m/84h/1h/0h")
    vpub = key.to_public().to_base58()
    # Embit writes xpub by default; force the SLIP-132 testnet version.
    from embit import networks

    parsed = bip32.HDKey.from_base58(vpub)
    parsed.version = networks.NETWORKS["test"]["zpub"]
    vpub = parsed.to_base58()
    core = descriptor_for_core(f"wpkh([00000000/84h/1h/0h]{vpub}/<0;1>/*)")
    assert "tpub" in core
    assert "vpub" not in core
