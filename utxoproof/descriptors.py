"""BIP32/84 descriptor helpers (plan Sec. 9). Pure-python via embit.

Covers descriptor-template building for the four supported address types and
watch-only address derivation from an account xpub. Verified against BIP32 and
BIP84 spec vectors in ``tests/test_descriptors.py``.
"""

from __future__ import annotations

import re

from embit import bip32, descriptor, networks, script

# purpose -> script type used when no explicit override is supplied
PURPOSE_SCRIPT_TYPES: dict[int, str] = {
    44: "pkh",
    49: "sh-wpkh",
    84: "wpkh",
    86: "tr",
}
SCRIPT_TYPE_ALIASES: dict[str, str] = {
    "pkh": "pkh",
    "legacy": "pkh",
    "p2pkh": "pkh",
    "sh-wpkh": "sh-wpkh",
    "shwpkh": "sh-wpkh",
    "p2sh-wpkh": "sh-wpkh",
    "p2sh-p2wpkh": "sh-wpkh",
    "nested-segwit": "sh-wpkh",
    "wpkh": "wpkh",
    "segwit": "wpkh",
    "bech32": "wpkh",
    "p2wpkh": "wpkh",
    "tr": "tr",
    "taproot": "tr",
    "p2tr": "tr",
}

NETWORKS = ("main", "test", "regtest")
NETWORK_ALIASES = {"mainnet": "main", "bitcoin": "main", "testnet": "test", "signet": "test"}


def canonical_network(network: str) -> str:
    """Normalize UI/config network names to embit network keys."""
    canonical = NETWORK_ALIASES.get(network, network)
    if canonical not in NETWORKS:
        raise ValueError(f"Unknown network {network!r}; expected one of {NETWORKS}")
    return canonical


def validate_account_xpub(xpub: str) -> bip32.HDKey:
    """Parse an account xpub (xpub/ypub/zpub/vpub/...). Raises on garbage."""
    try:
        return bip32.HDKey.from_base58(xpub)
    except Exception as exc:
        raise ValueError(f"Invalid account xpub: {exc}") from exc


def normalize_script_type(value: str) -> str:
    """Normalize user-facing script type names to pkh/sh-wpkh/wpkh/tr."""
    key = value.strip().lower().replace("_", "-")
    script_type = SCRIPT_TYPE_ALIASES.get(key)
    if script_type is None:
        script_type = SCRIPT_TYPE_ALIASES.get(key.replace("-", ""))
    if script_type is None:
        raise ValueError(f"Unsupported script_type {value!r}; expected pkh, sh-wpkh, wpkh or tr")
    return script_type


def normalize_derivation(value: str) -> str:
    """Normalize ``m/44'/0'/1'`` to the descriptor origin form ``44h/0h/1h``."""
    raw = value.strip()
    if raw.startswith("m/"):
        raw = raw[2:]
    if not raw:
        raise ValueError("derivation must not be empty")
    parts: list[str] = []
    for part in raw.split("/"):
        if not part:
            raise ValueError(f"invalid derivation path {value!r}")
        hardened = part[-1] in "'hH"
        number = part[:-1] if hardened else part
        if not number.isdigit():
            raise ValueError(f"invalid derivation path {value!r}")
        parts.append(f"{int(number)}h" if hardened else str(int(number)))
    return "/".join(parts)


def build_descriptors(
    xpub: str,
    fingerprint_hex: str = "",
    purpose: int = 84,
    coin: int = 0,
    account: int = 0,
    *,
    script_type: str = "",
    derivation: str = "",
) -> dict[str, str]:
    """Build Core external/change descriptors for an account xpub.

    Returns ``{"external": ..., "change": ...}`` e.g.
    ``wpkh([aabbccdd/84h/0h/0h]xpub.../0/*)``. ``fingerprint_hex`` is the
    master fingerprint (8 hex chars); it may be empty when the source (for
    example Bisq Wallet Info) does not expose one, in which case the descriptor
    origin is omitted.

    ``script_type`` overrides the purpose-derived address type and
    ``derivation`` overrides the origin path. This is what makes Bisq's
    nonstandard segwit keychain expressible: a ``zpub`` at ``m/44'/0'/1'`` is
    native segwit even though its purpose is 44.
    """
    validate_account_xpub(xpub)
    if script_type:
        script = normalize_script_type(script_type)
    else:
        script = PURPOSE_SCRIPT_TYPES.get(purpose, "")
        if not script:
            raise ValueError(
                f"Unsupported purpose {purpose}; expected one of "
                f"{sorted(PURPOSE_SCRIPT_TYPES)} or an explicit script_type"
            )
    if purpose < 0 or coin < 0 or account < 0:
        raise ValueError("purpose, coin and account must be >= 0")
    if derivation:
        origin_path = normalize_derivation(derivation)
    else:
        origin_path = f"{purpose}h/{coin}h/{account}h"
    origin = ""
    if fingerprint_hex:
        if len(fingerprint_hex) != 8:
            raise ValueError("fingerprint_hex must be 8 hex chars (master fingerprint)")
        try:
            bytes.fromhex(fingerprint_hex)
        except ValueError as exc:
            raise ValueError("fingerprint_hex must be 8 hex chars (master fingerprint)") from exc
        origin = f"[{fingerprint_hex}/{origin_path}]"

    if script == "sh-wpkh":
        inner = f"wpkh({origin}{xpub}/{{chain}}/*)"
        return {
            "external": f"sh({inner.format(chain=0)})",
            "change": f"sh({inner.format(chain=1)})",
        }
    func = {"pkh": "pkh", "wpkh": "wpkh", "tr": "tr"}[script]
    return {
        "external": f"{func}({origin}{xpub}/0/*)",
        "change": f"{func}({origin}{xpub}/1/*)",
    }


def derive_addresses(
    xpub: str,
    chain: int = 0,
    start: int = 0,
    count: int = 1,
    network: str = "main",
) -> list[str]:
    """Derive ``count`` P2WPKH addresses from an account xpub.

    ``chain`` 0 = external, 1 = change. ``network`` one of main/test/regtest.
    """
    if network not in NETWORKS:
        raise ValueError(f"Unknown network {network!r}; expected one of {NETWORKS}")
    if chain not in (0, 1):
        raise ValueError("chain must be 0 (external) or 1 (change)")
    if start < 0 or count < 1:
        raise ValueError("start must be >= 0 and count >= 1")
    account = validate_account_xpub(xpub)
    net = networks.NETWORKS[network]
    return [
        script.p2wpkh(account.derive([chain, index]).get_public_key()).address(net)
        for index in range(start, start + count)
    ]


_INPUT_CHARSET = (
    "0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~"
    'ijklmnopqrstuvwxyzABCDEFGH`#"\\ '
)
_CHECKSUM_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_POLYMOD_GEN = (0xF5DEE51989, 0xA9FDCA3312, 0x1BAB10E32D, 0x3706B1677A, 0x644D626FFD)


def _polymod(c: int, val: int) -> int:
    c0 = c >> 35
    c = ((c & 0x7FFFFFFFF) << 5) ^ val
    for i, generator in enumerate(_POLYMOD_GEN):
        if (c0 >> i) & 1:
            c ^= generator
    return c


def descriptor_checksum(desc: str) -> str:
    """Core-compatible 8-char descriptor checksum (cf. `DescriptorChecksum`).

    Bitcoin Core rejects `importdescriptors` entries without a `#checksum`
    suffix; this computes it in pure python.
    """
    core = desc.split("#")[0]
    c = 1
    cls = 0
    clscount = 0
    for char in core:
        pos = _INPUT_CHARSET.find(char)
        if pos == -1:
            raise ValueError(f"Character {char!r} not in descriptor charset")
        c = _polymod(c, pos & 31)
        cls = cls * 3 + (pos >> 5)
        clscount += 1
        if clscount == 3:
            c = _polymod(c, cls)
            cls = 0
            clscount = 0
    if clscount > 0:
        c = _polymod(c, cls)
    for _ in range(8):
        c = _polymod(c, 0)
    c ^= 1
    return "".join(_CHECKSUM_CHARSET[(c >> (5 * (7 - j))) & 31] for j in range(8))


def with_checksum(desc: str) -> str:
    """Append `#checksum` unless already present."""
    return desc if "#" in desc else f"{desc}#{descriptor_checksum(desc)}"


_EXTENDED_KEY = re.compile(r"(?:[xyztuvYZUV]pub)[1-9A-HJ-NP-Za-km-z]+")


def descriptor_for_core(text: str) -> str:
    """Return a checksummed descriptor Bitcoin Core can import.

    SLIP-132 y/z/v/u account keys are valid BIP32 but Core only accepts
    xpub/tpub. Electrum and embit accept both; this conversion is applied only
    on the optional Core path so the canonical descriptor stays untouched.
    """
    body = text.strip().split("#", 1)[0]

    def convert(match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            key = bip32.HDKey.from_base58(token)
        except Exception:
            return token
        prefix = token[:4].lower()
        if prefix in ("xpub", "tpub"):
            return token
        main = networks.NETWORKS["main"]
        test = networks.NETWORKS["test"]
        if key.version in {
            main["ypub"],
            main["zpub"],
            main["Ypub"],
            main["Zpub"],
        }:
            key.version = main["xpub"]
        elif key.version in {
            test["ypub"],
            test["zpub"],
            test["Ypub"],
            test["Zpub"],
        }:
            key.version = test["xpub"]
        else:
            return token
        return str(key.to_base58())

    return with_checksum(_EXTENDED_KEY.sub(convert, body))


def parse_descriptor(text: str, network: str = "main") -> descriptor.Descriptor:
    """Parse and validate one output descriptor (checksum included if present)."""
    network = canonical_network(network)
    raw = text.strip()
    if not raw:
        raise ValueError("Empty descriptor")
    if raw.startswith("#"):
        raise ValueError("Descriptor is a comment, not a descriptor")
    if " " in raw and "\n" not in raw:
        # Descriptor files may wrap one descriptor over several lines; internal
        # spaces are otherwise invalid and usually mean prose was pasted.
        raw = "".join(raw.split())
    else:
        raw = "".join(raw.split())
    body, _, checksum = raw.rpartition("#")
    if not body:
        body, checksum = raw, ""
    if checksum:
        if len(checksum) != 8 or checksum != descriptor_checksum(body):
            raise ValueError(f"Bad descriptor checksum {checksum!r}")
    try:
        parsed = descriptor.Descriptor.from_string(body)
    except Exception as exc:
        raise ValueError(f"Invalid descriptor: {exc}") from exc
    if not parsed.is_wildcard:
        raise ValueError("Descriptor must be ranged (contain /*) for address discovery")
    return parsed


def normalize_descriptor_pair(
    receive: str, change: str | None = None, network: str = "main"
) -> tuple[str, str]:
    """Return checksummed external/change descriptors.

    A BIP389 multipath descriptor (``/<0;1>/*``) is split at its branches; an
    explicit change descriptor wins for a single-branch receive descriptor.
    """
    parsed = parse_descriptor(receive, network)
    if change is not None:
        change_parsed = parse_descriptor(change, network)
        external = parsed.branch(0) if parsed.num_branches > 1 else parsed
        change_desc = change_parsed.branch(1) if change_parsed.num_branches > 1 else change_parsed
        return with_checksum(external.to_string()), with_checksum(change_desc.to_string())
    if parsed.num_branches < 2:
        raise ValueError("A single-branch descriptor needs an explicit change descriptor")
    return (
        with_checksum(parsed.branch(0).to_string()),
        with_checksum(parsed.branch(1).to_string()),
    )


def derive_descriptor_scripts(
    desc: str | descriptor.Descriptor,
    branch: int = 0,
    start: int = 0,
    count: int = 1,
    network: str = "main",
) -> list[tuple[str, str]]:
    """Derive ``(address, script_pubkey_hex)`` pairs from a descriptor."""
    network = canonical_network(network)
    if branch < 0 or start < 0 or count < 1:
        raise ValueError("branch/start must be >= 0 and count must be >= 1")
    parsed = parse_descriptor(desc, network) if isinstance(desc, str) else desc
    if branch >= int(parsed.num_branches):
        raise ValueError(f"Descriptor has no branch {branch}")
    net = networks.NETWORKS[network]
    out: list[tuple[str, str]] = []
    for index in range(start, start + count):
        item = parsed.derive(index, branch)
        out.append((item.address(net), item.script_pubkey().data.hex()))
    return out
