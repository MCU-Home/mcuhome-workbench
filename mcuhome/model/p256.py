# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""NIST P-256, only as much of it as the builder needs.

Two unrelated parts of the builder multiply the fixed generator by a
scalar: :mod:`mcuhome.model.pairing` derives the SPAKE2+ verifier's ``L = w1·G``,
and :mod:`mcuhome.workbench.signing` derives the public half of the firmware signing
key. They share this module rather than each carrying thirty lines of
curve arithmetic.

**Why not a crypto library.** The builder has exactly one runtime
dependency (ruamel.yaml), and everything here runs at build time on the
user's own machine against the user's own secret. The side-channel
resistance a hardened library buys has nothing to protect against in that
setting, and the arithmetic below is small enough to read in one sitting.
It is emphatically **not** a general-purpose curve implementation: it adds
affine points and multiplies the generator, and nothing else. Nothing here
is constant time, and nothing here should ever run on a device or against
an observer.
"""

from __future__ import annotations

__all__ = [
    "A",
    "COORD_BYTES",
    "G",
    "N",
    "P",
    "Point",
    "add",
    "generator_times",
]

#: NIST P-256 (secp256r1) domain parameters, FIPS 186-4 D.1.2.3.
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
#: The generator.
G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
#: Order of the generator; the scalar field private keys live in.
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

#: Length of one coordinate, and of a private scalar, in bytes.
COORD_BYTES = 32

#: An affine point, or None for the point at infinity.
Point = tuple[int, int] | None


def add(left: Point, right: Point) -> Point:
    """Affine point addition on P-256, doubling included."""
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return (x3, (slope * (x1 - x3) - y1) % P)


def generator_times(scalar: int) -> Point:
    """``scalar · G``, by double-and-add.

    Not constant time, deliberately: see the module docstring.
    """
    result: Point = None
    addend: Point = G
    while scalar:
        if scalar & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        scalar >>= 1
    return result
