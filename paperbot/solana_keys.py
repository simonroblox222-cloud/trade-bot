"""Minimal Solana address helpers (stdlib only): base58 decoding and the ed25519
on-curve test that tells a wallet from a program-derived address (PDA).

PDAs are by construction NOT valid ed25519 points, so bonding curves, AMM pool
authorities and vaults owned by programs show up as off-curve. This mirrors
`Pubkey.is_on_curve()` (curve25519-dalek `CompressedEdwardsY::decompress`).
"""
from __future__ import annotations

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}

P = 2**255 - 19
D = (-121665 * pow(121666, P - 2, P)) % P


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _B58_IDX:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + _B58_IDX[ch]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_on_curve(key: bytes) -> bool:
    """True if the 32 bytes decompress to an ed25519 point (i.e. could be a wallet)."""
    if len(key) != 32:
        raise ValueError("public key must be 32 bytes")
    y = int.from_bytes(key, "little") & ((1 << 255) - 1)
    y %= P
    yy = y * y % P
    u = (yy - 1) % P
    v = (D * yy + 1) % P
    x2 = u * pow(v, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


def is_wallet(address: str | None) -> bool:
    """Owner that a person can hold the key for. Unknown/unparseable owners count as
    wallets, so a missing lookup never hides a large holder."""
    if not address:
        return True
    try:
        key = b58decode(address)
    except ValueError:
        return True
    if len(key) != 32:
        return True
    return is_on_curve(key)
