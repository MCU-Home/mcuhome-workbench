# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Matter commissioning credentials: the pairing tuple and its codes.

A Matter node is commissioned with a *passcode* and found with a
*discriminator*. Neither reaches the device as written: the device stores
a **SPAKE2+ verifier** derived from the passcode, a per-device salt and an
iteration count, and it is that verifier — not the passcode — that the
commissioner authenticates against.

**Why this module exists at all.** Nothing on the Zephyr side derives the
verifier: CHIP's Kconfig takes ``CHIP_DEVICE_SPAKE2_PASSCODE`` and
``CHIP_DEVICE_SPAKE2_TEST_VERIFIER`` as independent strings, and its
``#error`` drift guards are inactive on this platform. Writing a new
passcode without recomputing the verifier therefore *builds*, flashes, and
fails at commissioning time with nothing to point at. :func:`kconfig_lines`
is the answer: it is the only place in the builder that emits any of the
seven symbols, and it emits all of them from one pairing tuple, so the two
cannot drift apart by construction.

**Everything here is pure.** Given the same tuple it produces the same
verifier, the same QR payload and the same manual code, forever — which is
what lets the whole builder stay byte-deterministic even though the
credentials themselves are random (they are random *once*, in
:mod:`mcuhome.workbench.provision`, and then live in the user's configuration).

**Algorithms**, all of them plain arithmetic over public constants:

* *verifier* — PBKDF2-HMAC-SHA256 over the passcode as a 4-byte
  little-endian integer, 80 bytes out, split into two 40-byte halves read
  big-endian and reduced modulo the P-256 group order into ``w0`` and
  ``w1``; ``L = w1 · G``; the verifier is ``w0`` (32 B) followed by ``L``
  in uncompressed point form (65 B), base64-encoded. Matter 1.4 §3.10;
  cross-checked against ``scripts/tools/spake2p/spake2p.py`` in
  connectedhomeip (Apache-2.0).
* *QR payload* — the 88-bit onboarding payload packed least-significant
  field first, base38-encoded, prefixed ``MT:``.
* *manual code* — three decimal chunks cut out of a 72-bit packing, plus
  a Verhoeff check digit.

The P-256 scalar multiplication this needs is ~30 lines of double-and-add
rather than a dependency, and lives in :mod:`mcuhome.model.p256`, which
:mod:`mcuhome.workbench.signing` shares.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import struct
from dataclasses import dataclass

from mcuhome.model import p256

__all__ = [
    "DEFAULT_ITERATIONS",
    "DISCOVERY_ON_NETWORK",
    "DISCRIMINATOR_MAX",
    "FORBIDDEN_PASSCODES",
    "ITERATIONS_MAX",
    "ITERATIONS_MIN",
    "PASSCODE_MAX",
    "PASSCODE_MIN",
    "PRODUCT_ID",
    "SALT_BYTES",
    "SALT_MAX_BYTES",
    "SALT_MIN_BYTES",
    "TEST_PAIRING",
    "VENDOR_ID",
    "Pairing",
    "base38_encode",
    "decode_salt",
    "kconfig_lines",
    "manual_code",
    "qr_payload",
    "random_pairing",
    "spake2p_verifier",
    "verhoeff_check_digit",
]

# --------------------------------------------------------------------------
# The constraints Matter puts on the tuple
# --------------------------------------------------------------------------

#: Largest 12-bit discriminator (Matter 1.4 §5.1.3).
DISCRIMINATOR_MAX = 0xFFF

#: Passcode range (Matter 1.4 §5.1.7.1). It is a 27-bit field, but the
#: usable range stops at 99999998 so that every passcode has an 8-digit
#: decimal spelling.
PASSCODE_MIN = 1
PASSCODE_MAX = 99999998

#: The twelve passcodes the specification forbids because they are the
#: first thing an attacker tries (Matter 1.4 §5.1.7.1, "Invalid
#: Passcodes"). CHIP's own Kconfig repeats the list.
FORBIDDEN_PASSCODES = frozenset(
    {
        0,
        11111111,
        22222222,
        33333333,
        44444444,
        55555555,
        66666666,
        77777777,
        88888888,
        99999999,
        12345678,
        87654321,
    }
)

#: Salt length bounds (Matter 1.4 §3.10) and the length MCUHome generates.
#: The maximum, because a longer salt costs a handful of bytes of flash
#: and nothing else.
SALT_MIN_BYTES = 16
SALT_MAX_BYTES = 32
SALT_BYTES = 32

#: PBKDF2 iteration bounds the specification allows.
ITERATIONS_MIN = 1_000
ITERATIONS_MAX = 100_000

#: What MCUHome uses. Ten times CHIP's default of 1000, and the cost is
#: paid entirely by the commissioner: the device stores the finished
#: verifier and never runs PBKDF2 at all, so a higher count is free on our
#: side of the exchange. It is *defense in depth, not a boundary* — the
#: attack this counter slows down (precomputing a table of verifiers once
#: and reusing it against every device, IACR 2025/1268) is already killed
#: outright by the per-device random salt below, and an attacker holding
#: the firmware image can recover a 27-bit passcode at any iteration count
#: this side of absurd. The specification maximum was not taken because
#: the extra factor of ten buys no new property and no controller in the
#: field has been measured at it.
DEFAULT_ITERATIONS = 10_000

#: Discovery capability bitmask advertised in the QR payload: bit 2, "on
#: IP network". MCUHome nodes commission on-network (ADR 0011: no BLE on
#: the netcore-sharing boards), so this is a constant, not a choice.
DISCOVERY_ON_NETWORK = 1 << 2

#: Commissioning flow 0, "standard": the device is commissionable as it
#: comes out of the box. The only flow MCUHome produces, and what makes
#: the manual code 11 digits rather than 21.
COMMISSIONING_FLOW_STANDARD = 0

#: Matter test vendor/product ID. Real IDs are a certification topic far
#: later (yaml-schema.md §3); until then these travel in the QR payload
#: and are stated explicitly rather than left to CHIP's Kconfig defaults,
#: because a payload that disagrees with the device does not commission.
VENDOR_ID = 0xFFF1
PRODUCT_ID = 32768

#: The credentials CHIP publishes in its own Kconfig defaults, offered
#: behind ``use_test_pairing: true``. Everybody has them, which is exactly
#: why they are opt-in and never a default (yaml-schema.md §4).
TEST_DISCRIMINATOR = 0xF00
TEST_PASSCODE = 20202021
TEST_SALT = "U1BBS0UyUCBLZXkgU2FsdA=="
TEST_ITERATIONS = 1_000


# --------------------------------------------------------------------------
# The scalar field the verifier lives in (curve arithmetic: mcuhome.model.p256)
# --------------------------------------------------------------------------

#: Length of one coordinate, and of w0/w1, in bytes.
_COORD_BYTES = p256.COORD_BYTES
#: PBKDF2 output per scalar: 32 bytes plus 8, so that the reduction
#: modulo the group order is statistically uniform (Matter 1.4 §3.10).
_WS_BYTES = _COORD_BYTES + 8


def spake2p_verifier(passcode: int, salt: bytes, iterations: int) -> str:
    """The base64 SPAKE2+ verifier for one (passcode, salt, iterations).

    97 bytes: ``w0`` followed by the uncompressed encoding of ``L``.
    """
    material = hashlib.pbkdf2_hmac(
        "sha256", struct.pack("<I", passcode), salt, iterations, _WS_BYTES * 2
    )
    w0 = int.from_bytes(material[:_WS_BYTES], "big") % p256.N
    w1 = int.from_bytes(material[_WS_BYTES:], "big") % p256.N
    point = p256.generator_times(w1)
    if point is None:  # pragma: no cover - w1 == 0 has probability 2^-256
        raise ValueError("the derived scalar is zero; regenerate the salt")
    x, y = point
    verifier = (
        w0.to_bytes(_COORD_BYTES, "big")
        + b"\x04"
        + x.to_bytes(_COORD_BYTES, "big")
        + y.to_bytes(_COORD_BYTES, "big")
    )
    return base64.b64encode(verifier).decode("ascii")


# --------------------------------------------------------------------------
# Onboarding payloads
# --------------------------------------------------------------------------

#: Base38 alphabet of the QR payload (Matter 1.4 §5.1.3.1).
_BASE38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."
#: Characters one chunk of 1, 2 or 3 bytes expands to.
_BASE38_CHARS = (2, 4, 5)


def base38_encode(data: bytes) -> str:
    """Matter's base38: three bytes at a time, little-endian, into 5 chars."""
    out: list[str] = []
    for start in range(0, len(data), 3):
        chunk = data[start : start + 3]
        value = int.from_bytes(chunk, "little")
        for _ in range(_BASE38_CHARS[len(chunk) - 1]):
            out.append(_BASE38[value % 38])
            value //= 38
    return "".join(out)


def qr_payload(
    *,
    discriminator: int,
    passcode: int,
    discovery: int = DISCOVERY_ON_NETWORK,
    flow: int = COMMISSIONING_FLOW_STANDARD,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
    version: int = 0,
) -> str:
    """The ``MT:`` onboarding payload.

    88 bits, packed least-significant field first: version (3), vendor ID
    (16), product ID (16), commissioning flow (2), discovery capabilities
    (8), discriminator (12), passcode (27), padding (4).
    """
    value = (
        version
        | (vendor_id << 3)
        | (product_id << 19)
        | (flow << 35)
        | (discovery << 37)
        | (discriminator << 45)
        | (passcode << 57)
    )
    return "MT:" + base38_encode(value.to_bytes(11, "little"))


#: Verhoeff's dihedral-group multiplication and permutation tables
#: (Verhoeff 1969), the check-digit scheme Matter's manual code uses.
_VERHOEFF_MUL = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_PERM = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_check_digit(digits: str) -> str:
    """The digit that makes *digits* a valid Verhoeff-checked number."""
    check = 0
    for position, digit in enumerate(reversed(digits + "0")):
        check = _VERHOEFF_MUL[check][_VERHOEFF_PERM[position % 8][int(digit)]]
    return str(_VERHOEFF_MUL[check].index(0))


def manual_code(
    *,
    discriminator: int,
    passcode: int,
    flow: int = COMMISSIONING_FLOW_STANDARD,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
) -> str:
    """The typed pairing code: 11 digits, or 21 for a non-standard flow.

    Cut out of a 72-bit packing (Matter 1.4 §5.1.4.1): one digit holding
    the version bit, the "vendor and product follow" bit and the top two
    bits of the *short* discriminator; five digits holding its remaining
    two bits and the low 14 bits of the passcode; four digits holding the
    passcode's top 13 bits. The short discriminator is the top 4 bits of
    the 12-bit one — which is why two devices can share a manual code
    prefix and still be told apart on the network.
    """
    short = discriminator >> 8
    chunk1 = (0 << 3) | ((1 if flow != COMMISSIONING_FLOW_STANDARD else 0) << 2) | (short >> 2)
    chunk2 = ((short & 0x3) << 14) | (passcode & 0x3FFF)
    chunk3 = passcode >> 14
    body = f"{chunk1:01d}{chunk2:05d}{chunk3:04d}"
    if flow != COMMISSIONING_FLOW_STANDARD:
        body += f"{vendor_id:05d}{product_id:05d}"
    return body + verhoeff_check_digit(body)


# --------------------------------------------------------------------------
# The tuple, and the one place it becomes Kconfig
# --------------------------------------------------------------------------


def decode_salt(salt: str) -> bytes:
    """Decode a base64 salt, raising :class:`binascii.Error` if it is not one."""
    return base64.b64decode(salt, validate=True)


@dataclass(frozen=True)
class Pairing:
    """One device's commissioning credentials.

    Everything below is derived from these four values and from the
    vendor/product identity, which is why they travel together everywhere
    — in the canonical model, into the Kconfig fragment, onto the screen.
    """

    discriminator: int
    passcode: int
    #: Base64, as it is written in the configuration and in Kconfig.
    salt: str
    iterations: int
    #: True for the published test credentials, so the CLI can say so.
    test_credentials: bool = False

    @property
    def verifier(self) -> str:
        return spake2p_verifier(self.passcode, decode_salt(self.salt), self.iterations)

    @property
    def manual_code(self) -> str:
        return manual_code(discriminator=self.discriminator, passcode=self.passcode)

    @property
    def qr_payload(self) -> str:
        return qr_payload(discriminator=self.discriminator, passcode=self.passcode)


#: The published test credentials as a tuple, for ``use_test_pairing``.
TEST_PAIRING = Pairing(
    discriminator=TEST_DISCRIMINATOR,
    passcode=TEST_PASSCODE,
    salt=TEST_SALT,
    iterations=TEST_ITERATIONS,
    test_credentials=True,
)


def kconfig_lines(pairing: Pairing) -> list[str]:
    """Every Kconfig symbol the commissioning identity consists of.

    **The whole point of this function is that it is the only one.** The
    passcode and the verifier derived from it are separate CHIP symbols
    with no build-time consistency check on Zephyr, so a builder that
    wrote one without the other would produce firmware that flashes,
    boots, advertises itself and then refuses every commissioner. Emitting
    the group from a single tuple in a single call makes that failure mode
    unreachable rather than unlikely — no caller can update half of it.

    The vendor and product IDs are in the group because the QR payload
    encodes them: a payload that disagrees with the device is a payload
    that does not commission it.
    """
    return [
        f"CONFIG_CHIP_DEVICE_VENDOR_ID={VENDOR_ID}",
        f"CONFIG_CHIP_DEVICE_PRODUCT_ID={PRODUCT_ID}",
        f"CONFIG_CHIP_DEVICE_DISCRIMINATOR=0x{pairing.discriminator:03X}",
        f"CONFIG_CHIP_DEVICE_SPAKE2_PASSCODE={pairing.passcode}",
        f"CONFIG_CHIP_DEVICE_SPAKE2_IT={pairing.iterations}",
        f'CONFIG_CHIP_DEVICE_SPAKE2_SALT="{pairing.salt}"',
        f'CONFIG_CHIP_DEVICE_SPAKE2_TEST_VERIFIER="{pairing.verifier}"',
    ]


# --------------------------------------------------------------------------
# Generating a fresh tuple
# --------------------------------------------------------------------------


def random_passcode() -> int:
    """A uniformly random passcode from the allowed set.

    Rejection sampling, not "pick again if forbidden, else clamp": every
    allowed value has exactly the same probability, and the loop runs a
    second time with probability 12 / 99999998.
    """
    while True:
        candidate = PASSCODE_MIN + secrets.randbelow(PASSCODE_MAX - PASSCODE_MIN + 1)
        if candidate not in FORBIDDEN_PASSCODES:
            return candidate


def random_pairing() -> Pairing:
    """Fresh credentials for one device, from the system CSPRNG."""
    return Pairing(
        discriminator=secrets.randbelow(DISCRIMINATOR_MAX + 1),
        passcode=random_passcode(),
        salt=base64.b64encode(secrets.token_bytes(SALT_BYTES)).decode("ascii"),
        iterations=DEFAULT_ITERATIONS,
    )


def describe_salt_problem(salt: str) -> str | None:
    """Why *salt* is not a usable salt, or None when it is one."""
    try:
        raw = decode_salt(salt)
    except (binascii.Error, ValueError):
        return "not base64"
    if not SALT_MIN_BYTES <= len(raw) <= SALT_MAX_BYTES:
        return f"{len(raw)} bytes"
    return None
