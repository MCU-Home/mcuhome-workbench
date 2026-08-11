# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The firmware signing key (ADR 0015 decision 8).

Every MCUHome image is signed, and **each MCUHome user is their own
firmware vendor**: the builder draws one ECDSA P-256 key pair on first
need and stores it outside every repository and every build directory.
There is no central MCUHome key, no key in CI, and **MCUboot's demo key
is never used** — its private half is published in the MCUboot tree, so
signing with it verifies against a key the whole world holds. That is
theatre, and shipping it would be worse than shipping nothing, because it
looks like a signature.

**Where the key lives.** ``$XDG_CONFIG_HOME/mcuhome/signing.key``, which
is ``~/.config/mcuhome/signing.key`` on a normal Linux account, created
with owner-only permissions. ``--signing-key`` and
:data:`KEY_VAR` point somewhere else — that is the future dashboard's
path, which keeps the key in its own state directory (in a Home Assistant
add-on, ``/config/mcuhome/signing.key``). The rule the ADR fixes is
*where the user's controlling instance runs, never on a build server*.

**Why signing is a separate concern from building.** MCUboot signing is a
detached post-build step: ``imgtool`` runs over the finished binary, so a
build and a signature do not have to happen on the same machine. This
module therefore owns the key and nothing else — no build directory, no
device model, no Zephyr. A remote builder that returns an *unsigned*
image (ADR 0015 decision 8) needs exactly this module plus imgtool, and
nothing else that is in this package.

**Rotation is a bootstrap, not an update.** MCUboot verifies against a
public key compiled into the bootloader, so replacing the key means
running the ADR 0016 bootstrap again, with the board in hand. That is an
argument for generating the key well once, which is what happens here,
rather than for rotating it often.

**No crypto dependency.** The key pair is a random scalar and one
multiplication of the P-256 generator (:mod:`mcuhome.model.p256`), serialized
as PKCS#8 PEM — byte-for-byte the shape ``imgtool keygen -t ecdsa-p256``
writes, so a user can swap one for the other in either direction. The
private scalar comes from :func:`secrets.token_bytes`, i.e. the operating
system CSPRNG, and is never logged, never printed and never copied into a
build directory.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model import p256
from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import config_dir, expand

__all__ = [
    "KEY_FILE",
    "KEY_VAR",
    "PUBLIC_KEY_FILE",
    "SigningKey",
    "default_key_path",
    "generate_key_pem",
    "looks_like_p256_key",
    "looks_like_p256_public_key",
    "public_key_pem",
    "resolve_key_path",
    "signing_key",
]

#: File name under the configuration directory.
KEY_FILE = "signing.key"

#: Conventional file name of the *public* half — the only part of the key
#: pair that ever leaves the machine it was generated on. A build server
#: needs it (MCUboot verifies against a public key compiled into the
#: bootloader) and must never see the other half (ADR 0015 decision 8),
#: which is what ``mcuhome build --no-sign --public-key`` is for.
PUBLIC_KEY_FILE = "signing.pub"

#: Overrides where the key lives. ``--signing-key`` beats it, it beats the
#: XDG default. This is the knob a dashboard or an add-on sets once.
KEY_VAR = "MCUHOME_SIGNING_KEY"

#: PEM label of a PKCS#8 private key, and of the older SEC1 spelling that
#: OpenSSL writes with ``-----BEGIN EC PRIVATE KEY-----``. Both are
#: accepted on read; only the first is written.
_PEM_LABELS = ("PRIVATE KEY", "EC PRIVATE KEY")

#: DER encoding of the OID ``1.2.840.10045.3.1.7`` (prime256v1 / P-256).
#: Present in both PEM spellings above, which is what makes a curve check
#: possible without an ASN.1 parser.
_P256_OID_DER = bytes.fromhex("06082a8648ce3d030107")

#: DER encoding of the OID ``1.2.840.10045.2.1`` (id-ecPublicKey).
_EC_PUBLIC_KEY_OID_DER = bytes.fromhex("06072a8648ce3d0201")


# --------------------------------------------------------------------------
# Where the key is
# --------------------------------------------------------------------------


def default_key_path(env: dict[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/mcuhome/signing.key``, or the ``~/.config`` form.

    A configuration directory rather than a cache or a state directory:
    the key is not derivable, not reproducible and not disposable. Losing
    it means every device signed with it needs the bootstrap operation of
    ADR 0016 again.

    *env* is stated, never read from the process: this resolves the path
    of a private key, and a server process must resolve it from what it
    was given rather than from the environment it happens to run in.
    """
    return config_dir(env) / KEY_FILE


def resolve_key_path(override: Path | str | None = None, *, env: dict[str, str]) -> Path:
    """Which key file this build signs with: flag, then variable, then default."""
    if override:
        return expand(override, env)
    from_env = env.get(KEY_VAR)
    if from_env:
        return expand(from_env, env)
    return default_key_path(env)


# --------------------------------------------------------------------------
# Making one
# --------------------------------------------------------------------------


def _der(tag: int, payload: bytes) -> bytes:
    """One ASN.1 DER element: tag, definite length, contents."""
    if len(payload) < 0x80:
        return bytes([tag, len(payload)]) + payload
    length = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + payload


def _der_integer(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big")
    return _der(0x02, raw)


def generate_key_pem(scalar: int | None = None) -> str:
    """A fresh ECDSA P-256 private key as PKCS#8 PEM text.

    *scalar* exists for the test suite, which needs one known key to
    compare bytes against; leave it out and the private scalar is drawn
    from the operating system CSPRNG by rejection sampling, so it is
    uniform over ``[1, n-1]`` rather than biased by a modulo.
    """
    if scalar is None:
        while True:
            candidate = int.from_bytes(secrets.token_bytes(p256.COORD_BYTES), "big")
            if 1 <= candidate < p256.N:
                scalar = candidate
                break
    if not 1 <= scalar < p256.N:
        raise ValueError("a P-256 private key is a scalar in [1, n-1]")

    point = p256.generator_times(scalar)
    assert point is not None  # noqa: S101 - scalar < n and non-zero, so is scalar*G
    x, y = point
    public = b"\x04" + x.to_bytes(p256.COORD_BYTES, "big") + y.to_bytes(p256.COORD_BYTES, "big")

    # RFC 5915 ECPrivateKey. The optional [0] parameters field is left
    # out: the curve is already named in the PKCS#8 AlgorithmIdentifier
    # below, and repeating it there is what `cryptography` omits too.
    ec_private_key = _der(
        0x30,
        _der_integer(1)
        + _der(0x04, scalar.to_bytes(p256.COORD_BYTES, "big"))
        + _der(0xA1, _der(0x03, b"\x00" + public)),
    )
    # RFC 5208 PrivateKeyInfo.
    private_key_info = _der(
        0x30,
        _der_integer(0)
        + _der(0x30, _EC_PUBLIC_KEY_OID_DER + _P256_OID_DER)
        + _der(0x04, ec_private_key),
    )
    return _pem("PRIVATE KEY", private_key_info)


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[index : index + 64] for index in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----", ""])


def _der_elements(data: bytes) -> list[tuple[int, bytes]]:
    """One level of DER, as ``(tag, contents)`` pairs.

    Enough of a parser to walk a key file and not one byte more: the two
    private-key spellings this module accepts are three elements deep,
    and anything malformed falls out as an empty list rather than as an
    exception the caller would have to distinguish from a real one.
    """
    elements: list[tuple[int, bytes]] = []
    index = 0
    while index + 1 < len(data):
        tag = data[index]
        length = data[index + 1]
        index += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or index + count > len(data):
                break
            length = int.from_bytes(data[index : index + count], "big")
            index += count
        if index + length > len(data):
            break
        elements.append((tag, data[index : index + length]))
        index += length
    return elements


def _private_scalar(der: bytes) -> int | None:
    """The private scalar of a PKCS#8 or SEC1 EC key, or None.

    Recomputing the public point from the scalar rather than reading the
    optional public half stored next to it is deliberate: the stored copy
    is optional in both spellings, and a key file that disagrees with
    itself would otherwise produce a bootloader that rejects every image
    the same file signs.
    """
    for tag, payload in _der_elements(der):
        if tag != 0x30:  # SEQUENCE
            continue
        items = _der_elements(payload)
        if len(items) >= 3 and items[0][0] == 0x02 and items[1][0] == 0x30 and items[2][0] == 0x04:
            # RFC 5208 PrivateKeyInfo: the ECPrivateKey is inside the
            # OCTET STRING.
            return _private_scalar(items[2][1])
        if len(items) >= 2 and items[0][0] == 0x02 and items[1][0] == 0x04:
            # RFC 5915 ECPrivateKey: version, then the scalar.
            return int.from_bytes(items[1][1], "big")
    return None


def _pem_der(text: str, labels: tuple[str, ...]) -> bytes | None:
    """The DER inside the first PEM block of *text* carrying one of *labels*."""
    for label in labels:
        begin = f"-----BEGIN {label}-----"
        end = f"-----END {label}-----"
        if begin not in text or end not in text:
            continue
        body = text.split(begin, 1)[1].split(end, 1)[0]
        try:
            return base64.b64decode("".join(body.split()), validate=True)
        except (binascii.Error, ValueError):
            return None
    return None


def public_key_pem(private_pem: str) -> str:
    """The public half of a P-256 private key, as SubjectPublicKeyInfo PEM.

    The file a build server is given (ADR 0015 decision 8): MCUboot
    compiles the public key into the bootloader, and a builder that never
    signs never needs the private half. Byte-for-byte the format
    ``imgtool getpub -k <key> --output <file>`` writes in PEM mode, and
    what ``openssl ec -pubout`` writes, so the file is interchangeable
    with both.
    """
    der = _pem_der(private_pem, _PEM_LABELS)
    scalar = None if der is None else _private_scalar(der)
    if scalar is None or not 1 <= scalar < p256.N:
        raise ValueError("not an ECDSA P-256 private key in PEM form")
    point = p256.generator_times(scalar)
    assert point is not None  # noqa: S101 - scalar is in [1, n-1], so is scalar*G
    x, y = point
    public = b"\x04" + x.to_bytes(p256.COORD_BYTES, "big") + y.to_bytes(p256.COORD_BYTES, "big")
    # RFC 5280 SubjectPublicKeyInfo.
    spki = _der(
        0x30,
        _der(0x30, _EC_PUBLIC_KEY_OID_DER + _P256_OID_DER) + _der(0x03, b"\x00" + public),
    )
    return _pem("PUBLIC KEY", spki)


def looks_like_p256_public_key(text: str) -> bool:
    """Whether *text* is a PEM **public** key on the P-256 curve.

    The counterpart of :func:`looks_like_p256_key`, and the check a
    ``--public-key`` argument gets: handing a *private* key to a build
    server is the one mistake this feature exists to prevent, so it is
    worth catching by shape before the file is mounted anywhere.
    """
    der = _pem_der(text, ("PUBLIC KEY",))
    return der is not None and _P256_OID_DER in der


def looks_like_p256_key(text: str) -> bool:
    """Whether *text* is a PEM private key on the P-256 curve.

    A shape check, not a validation: it confirms the PEM envelope and
    that the DER inside names ``prime256v1``. That is enough to catch the
    mistakes that actually happen — an RSA key, an Ed25519 key, a public
    key, a text file — and it needs no ASN.1 parser. Anything subtler is
    caught by imgtool, loudly, before an image is signed with it.
    """
    der = _pem_der(text, _PEM_LABELS)
    return der is not None and _P256_OID_DER in der


# --------------------------------------------------------------------------
# Getting one, whatever it takes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """The key this build signs with, and whether it had to be made."""

    path: Path
    #: True when this call created it. The caller says so out loud:
    #: firmware signed with a new key is not accepted by a device that
    #: was bootstrapped with an older one.
    created: bool


def _refuse_unreadable(path: Path, reason: str) -> BuildError:
    return BuildError(
        f"MCUHome cannot read the firmware signing key {path}: {reason}.",
        hint=(
            "every MCUHome image is signed with your own key (ADR 0015). Point "
            "--signing-key at the right file, or move the unreadable one aside "
            f"and let MCUHome generate a new one — but note that a device already "
            f"running firmware signed with the old key will refuse the new one "
            f"until it is bootstrapped again.\n"
            f"The {KEY_VAR} environment variable selects the file too."
        ),
    )


def _refuse_not_a_key(path: Path) -> BuildError:
    return BuildError(
        f"{path} is not an ECDSA P-256 private key in PEM form.",
        hint=(
            "MCUHome signs with ECDSA P-256 (ADR 0015 decision 8) and will not "
            "overwrite a file it does not recognize. Either point --signing-key "
            "at the right file, or move this one aside so MCUHome can generate a "
            "key of its own.\n"
            "An existing key from elsewhere is fine as long as it is P-256: "
            "`imgtool keygen -t ecdsa-p256 -k <file>` writes exactly this format."
        ),
    )


def _refuse_unwritable(path: Path, reason: str) -> BuildError:
    return BuildError(
        f"MCUHome cannot create the firmware signing key {path}: {reason}.",
        hint=(
            "the key has to live outside every repository and every build "
            "directory, so it survives a clean checkout and never reaches a "
            f"build server (ADR 0015 decision 8). Pick a writable location with "
            f"--signing-key, or set {KEY_VAR}."
        ),
    )


def signing_key(
    override: Path | str | None = None,
    *,
    env: dict[str, str],
    create: bool = True,
) -> SigningKey:
    """The key file to sign with, generating one on first need.

    Generating rather than refusing is deliberate: a user who has never
    signed anything should get a working, private, per-user key by
    running ``mcuhome build``, not a lecture about key management. What
    is *not* deliberate is generating a second one by accident, so every
    caller is expected to say when this created a key — a device only
    accepts images signed with the key its bootloader carries.
    """
    path = resolve_key_path(override, env=env)
    if path.exists():
        if path.is_dir():
            raise _refuse_unreadable(path, "it is a directory")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise _refuse_unreadable(path, error.strerror or "unreadable") from error
        except UnicodeDecodeError as error:
            raise _refuse_not_a_key(path) from error
        if not looks_like_p256_key(text):
            raise _refuse_not_a_key(path)
        return SigningKey(path=path, created=False)

    if not create:
        raise _refuse_unreadable(path, "no such file")

    pem = generate_key_pem()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only from the moment it exists, rather than written and
        # then chmod-ed: between those two calls the private half of a
        # key would be world-readable on a shared machine.
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(pem)
    except OSError as error:
        raise _refuse_unwritable(path, error.strerror or "cannot write") from error
    return SigningKey(path=path, created=True)
