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
