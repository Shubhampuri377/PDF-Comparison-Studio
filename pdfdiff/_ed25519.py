"""Minimal Ed25519 (RFC 8032) in pure Python.

Present so the application has zero hard crypto dependencies and still ships
only a *public* key.  If the ``cryptography`` package is installed,
:mod:`pdfdiff.licensing` uses it instead -- it is faster and audited.  This
implementation exists as the fallback and for the offline key generator.

Verification costs a few milliseconds, which is irrelevant for a licence check
that happens once at startup and once a minute thereafter.
"""
from __future__ import annotations

import hashlib
import os

# Curve constants ----------------------------------------------------------
Q = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, Q - 2, Q)) % Q
_I = pow(2, (Q - 1) // 4, Q)

# Neutral element in extended coordinates (X : Y : Z : T).
_IDENT = (0, 1, 1, 0)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, Q - 2, Q)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * _I) % Q
    if x % 2 != 0:
        x = Q - x
    return x


_BY = (4 * pow(5, Q - 2, Q)) % Q
_BX = _x_recover(_BY)
_B = (_BX, _BY, 1, (_BX * _BY) % Q)


def _add(p, q_):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q_
    a = (y1 - x1) * (y2 - x2) % Q
    b = (y1 + x1) * (y2 + x2) % Q
    c = t1 * 2 * _D * t2 % Q
    dd = z1 * 2 * z2 % Q
    e = (b - a) % Q
    f = (dd - c) % Q
    g = (dd + c) % Q
    h = (b + a) % Q
    return (e * f % Q, g * h % Q, f * g % Q, e * h % Q)


def _double(p):
    x1, y1, z1, _t1 = p
    a = x1 * x1 % Q
    b = y1 * y1 % Q
    c = 2 * z1 * z1 % Q
    d_ = (-a) % Q
    s = (x1 + y1) % Q
    e = (s * s - a - b) % Q
    g = (d_ + b) % Q
    f = (g - c) % Q
    h = (d_ - b) % Q
    return (e * f % Q, g * h % Q, f * g % Q, e * h % Q)


def _scalarmult(p, e: int):
    """Double-and-add.  Iterative: 256 levels of recursion is asking for
    trouble inside a frozen executable with a reduced stack."""
    result = _IDENT
    addend = p
    while e > 0:
        if e & 1:
            result = _add(result, addend)
        addend = _double(addend)
        e >>= 1
    return result


def _compress(p) -> bytes:
    x, y, z, _t = p
    zinv = pow(z, Q - 2, Q)
    x = x * zinv % Q
    y = y * zinv % Q
    return ((y & ((1 << 255) - 1)) | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(data: bytes):
    if len(data) != 32:
        raise ValueError("point must be 32 bytes")
    n = int.from_bytes(data, "little")
    sign = n >> 255
    y = n & ((1 << 255) - 1)
    if y >= Q:
        raise ValueError("point out of range")
    x = _x_recover(y)
    if x & 1 != sign:
        x = Q - x
    p = (x, y, 1, x * y % Q)
    if not _on_curve(p):
        raise ValueError("point not on curve")
    return p


def _on_curve(p) -> bool:
    x, y, z, t = p
    if z == 0:
        return False
    zinv = pow(z, Q - 2, Q)
    xa = x * zinv % Q
    ya = y * zinv % Q
    return (-xa * xa + ya * ya - 1 - _D * xa * xa * ya * ya) % Q == 0


def _equal(p, q_) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q_
    return (x1 * z2 - x2 * z1) % Q == 0 and (y1 * z2 - y2 * z1) % Q == 0


def _clamp(h: bytes) -> int:
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


# Public API ----------------------------------------------------------------
def generate_seed() -> bytes:
    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _clamp(h)
    return _compress(_scalarmult(_B, a))


def sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _clamp(h)
    prefix = h[32:]
    pub = _compress(_scalarmult(_B, a))
    r = int.from_bytes(_sha512(prefix + message), "little") % L
    big_r = _compress(_scalarmult(_B, r))
    k = int.from_bytes(_sha512(big_r + pub + message), "little") % L
    s = (r + k * a) % L
    return big_r + s.to_bytes(32, "little")


def verify(pub: bytes, message: bytes, signature: bytes) -> bool:
    try:
        if len(signature) != 64 or len(pub) != 32:
            return False
        big_r = _decompress(signature[:32])
        big_a = _decompress(pub)
        s = int.from_bytes(signature[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_sha512(signature[:32] + pub + message), "little") % L
        lhs = _scalarmult(_B, s)
        rhs = _add(big_r, _scalarmult(big_a, k))
        return _equal(lhs, rhs)
    except Exception:
        return False
