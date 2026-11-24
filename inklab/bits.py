"""Exact binary32 encoding/decoding of weighing readings ("称量原件").

The workshop registers every chamber reading as a *real* 32-bit pattern, not as
a host float.  The existing ``obsdelivery.semantics`` module already decodes such
patterns exactly; here we add the forward direction so a rational reading can be
turned into its original bits locally, without going through host float.

Only dyadic rationals are exactly representable as binary32.  ``encode_bits``
returns ``(hex, exact)``; ``exact`` is ``False`` when the value had to be
rounded half-to-even.  All workshop readings in the task (0,1,2,3,4,…) are exact.
"""

from __future__ import annotations

from fractions import Fraction

from obsdelivery.semantics import decode, format_bits

SIGN_BIT = 0x80000000


def _pow2(exp: int) -> Fraction:
    return Fraction(1 << exp) if exp >= 0 else Fraction(1, 1 << -exp)


def _floor_log2(x: Fraction) -> int:
    """floor(log2(x)) for a positive Fraction."""
    n, d = x.numerator, x.denominator
    e = n.bit_length() - d.bit_length()
    while _pow2(e) > x:
        e -= 1
    while _pow2(e + 1) <= x:
        e += 1
    return e


def _round_half_even_int(x: Fraction) -> int:
    q, r = divmod(x.numerator, x.denominator)
    twice = 2 * r
    if twice < x.denominator:
        return q
    if twice > x.denominator:
        return q + 1
    return q if q % 2 == 0 else q + 1


def encode_bits(value: Fraction | int, *, negative_zero: bool = False) -> tuple[str, bool]:
    """Encode a rational as an 8-hex-digit binary32 original.

    Returns ``(hex, exact)``.  Zero may be requested as negative zero to keep the
    sign identity that ``obsdelivery`` preserves.
    """
    x = Fraction(value)
    if x == 0:
        return ("80000000" if negative_zero else "00000000"), True
    sign = 1 if x < 0 else 0
    x = abs(x)
    exact = True
    exponent = _floor_log2(x)
    if exponent < -126:  # subnormal
        mantissa = x * _pow2(149)
        m = _round_half_even_int(mantissa)
        exact = Fraction(m) == mantissa
        exp_field = 0
        frac_field = m
    else:
        m_scaled = x / _pow2(exponent) * (1 << 23)  # in [2^23, 2^24)
        m = _round_half_even_int(m_scaled)
        exact = Fraction(m) == m_scaled
        if m == (1 << 24):  # rounded up across the binade
            m >>= 1
            exponent += 1
            if exponent > 127:
                raise OverflowError(f"{value!r} overflows binary32")
        exp_field = exponent + 127
        frac_field = m - (1 << 23)
    raw = (sign << 31) | (exp_field << 23) | frac_field
    return format_bits(raw), exact


def decode_bits(bits: str) -> Fraction:
    """Exact rational value of an original bit pattern (must be finite)."""
    decoded = decode(int(bits, 16))
    if decoded.value is None:
        raise ValueError(f"{bits!r} is not a finite value")
    return decoded.value
