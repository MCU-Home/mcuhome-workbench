# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The per-user firmware signing key (ADR 0015 decision 8).

Three things matter here and are checked as three separate concerns:
where the key is looked for, what a generated one *is*, and how the
refusals read. The last is not a formality — the failure this module
exists to prevent is a user unknowingly shipping firmware signed with
MCUboot's published demo key, and the way that is prevented is by making
every other outcome say what happened.
"""

from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest

from mcuhome import p256, signing
from mcuhome.errors import BuildError

#: One key the suite can compare bytes against, so nothing here draws a
#: random one and nothing here goes near the developer's own.
KNOWN_SCALAR = 0x0102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F20

#: A foreign fixture, in the sense of tests_py/README.md: this is the
#: literal output of ``imgtool keygen -t ecdsa-p256`` from the pinned
#: MCUboot checkout, not of anything in this package. It is what makes
#: "a user can bring their own key" a checked claim rather than a hope.
#: A throwaway generated for this file — nothing was ever signed with it,
#: and no device carries its public half.
IMGTOOL_KEY = """\
-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgHZMs6IloJOG8kDzY
TIiTZTLCgHIYrNt2DhkbDAP1rOehRANCAAS8VpXhXVyQZjY9+pC/GqvUEL0tkUpG
7LnqcREH3Kay8s4Kivr9I3z5dRaVkHj3qzSsOXnXcBiKAZpCofIHZYNG
-----END PRIVATE KEY-----
"""


# --------------------------------------------------------------------------
# Where it is
# --------------------------------------------------------------------------


def test_the_default_is_under_the_xdg_config_directory(tmp_path) -> None:
    """A config directory, not a cache: it is not reproducible if lost."""
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    assert signing.default_key_path(env) == tmp_path / "cfg" / "mcuhome" / "signing.key"


def test_without_the_variable_it_is_under_the_home_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert signing.default_key_path({}) == tmp_path / ".config" / "mcuhome" / "signing.key"


def test_the_environment_variable_moves_it(tmp_path) -> None:
    """The knob a dashboard or a Home Assistant add-on sets once."""
    env = {signing.KEY_VAR: str(tmp_path / "state" / "signing.key")}
    assert signing.resolve_key_path(env=env) == tmp_path / "state" / "signing.key"


def test_the_flag_beats_the_variable(tmp_path) -> None:
    env = {signing.KEY_VAR: str(tmp_path / "from-env")}
    assert signing.resolve_key_path(tmp_path / "from-flag", env=env) == tmp_path / "from-flag"


def test_a_tilde_is_a_home_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert signing.resolve_key_path("~/keys/mine.key", env={}) == tmp_path / "keys" / "mine.key"


# --------------------------------------------------------------------------
# What it is
# --------------------------------------------------------------------------


def _der_of(pem: str) -> bytes:
    body = pem.split("-----BEGIN PRIVATE KEY-----", 1)[1].split("-----END", 1)[0]
    return base64.b64decode("".join(body.split()))


def test_a_generated_key_is_a_p256_private_key_in_pem_form() -> None:
    pem = signing.generate_key_pem()
    assert pem.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert pem.endswith("-----END PRIVATE KEY-----\n")
    assert signing.looks_like_p256_key(pem)


def test_a_generated_key_carries_the_public_half_of_its_own_scalar() -> None:
    """The one property no shape check can see: the DER is self-consistent.

    imgtool derives the public key MCUboot compiles in from this file, so
    a private scalar and a public point that disagreed would produce a
    bootloader that rejects every image the same key signed.
    """
    scalar = 0x0102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F20
    der = _der_of(signing.generate_key_pem(scalar))

    assert scalar.to_bytes(32, "big") in der
    point = p256.generator_times(scalar)
    assert point is not None
    x, y = point
    uncompressed = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    assert uncompressed in der


def test_the_generated_shape_is_the_one_imgtool_writes() -> None:
    """So a user can swap an imgtool key in, or ours out, either way."""
    ours = _der_of(signing.generate_key_pem())
    theirs = _der_of(IMGTOOL_KEY)
    assert len(ours) == len(theirs)
    # Same header: version, AlgorithmIdentifier (id-ecPublicKey,
    # prime256v1) and the start of the wrapped ECPrivateKey.
    assert ours[:7] == theirs[:7]
    assert signing.looks_like_p256_key(IMGTOOL_KEY)


def test_two_generated_keys_are_different() -> None:
    assert signing.generate_key_pem() != signing.generate_key_pem()


def test_a_scalar_outside_the_group_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="scalar in"):
        signing.generate_key_pem(0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a key at all\n",
        "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
        # A P-384 key: right envelope, wrong curve — the mistake most
        # likely to be made by someone who already has keys.
        "-----BEGIN PRIVATE KEY-----\nMDECAQAwEwYHKoZIzj0CAQYIKoZIzj0DAQI=\n"
        "-----END PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nnot base64 !!!\n-----END PRIVATE KEY-----\n",
    ],
)
def test_what_is_not_a_p256_key_is_not_taken_for_one(text: str) -> None:
    assert not signing.looks_like_p256_key(text)


# --------------------------------------------------------------------------
# Getting one
# --------------------------------------------------------------------------


def test_the_first_build_generates_one_and_says_so(tmp_path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    key = signing.signing_key(env=env)

    assert key.created is True
    assert key.path == tmp_path / "mcuhome" / "signing.key"
    assert signing.looks_like_p256_key(key.path.read_text(encoding="utf-8"))


def test_a_generated_key_is_readable_by_nobody_else(tmp_path) -> None:
    key = signing.signing_key(env={"XDG_CONFIG_HOME": str(tmp_path)})
    mode = stat.S_IMODE(key.path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR, oct(mode)


def test_the_second_build_reuses_the_first_ones_key(tmp_path) -> None:
    """Generating twice would silently orphan every device already flashed."""
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    first = signing.signing_key(env=env)
    before = first.path.read_bytes()

    second = signing.signing_key(env=env)
    assert second.created is False
    assert second.path == first.path
    assert second.path.read_bytes() == before


def test_a_key_from_elsewhere_is_used_as_it_is(tmp_path) -> None:
    path = tmp_path / "imported.key"
    path.write_text(IMGTOOL_KEY, encoding="utf-8")
    key = signing.signing_key(path)
    assert key.created is False
    assert path.read_text(encoding="utf-8") == IMGTOOL_KEY


def test_an_explicit_path_that_does_not_exist_yet_is_created(tmp_path) -> None:
    """The dashboard flow: a state directory that starts out empty.

    ADR 0015 decision 8 puts the key where the controlling instance runs
    and has it generated on first need there, so an explicit path is a
    place to put one, not a promise that one is already there.
    """
    key = signing.signing_key(tmp_path / "config" / "mcuhome" / "signing.key")
    assert key.created is True
    assert key.path.is_file()


def test_generation_can_be_refused_outright(tmp_path) -> None:
    """For a caller that wants to sign, not to enrol — a later detached step."""
    with pytest.raises(BuildError) as caught:
        signing.signing_key(tmp_path / "nowhere.key", create=False)
    assert "no such file" in caught.value.render()


def test_a_file_that_is_not_a_key_is_never_overwritten(tmp_path) -> None:
    path = tmp_path / "signing.key"
    path.write_text("this is my shopping list\n", encoding="utf-8")

    with pytest.raises(BuildError) as caught:
        signing.signing_key(path)
    rendered = caught.value.render()
    assert "not an ECDSA P-256 private key" in rendered
    assert "imgtool keygen -t ecdsa-p256" in rendered
    assert path.read_text(encoding="utf-8") == "this is my shopping list\n"


def test_binary_rubbish_is_refused_as_a_key_rather_than_as_an_encoding(tmp_path) -> None:
    path = tmp_path / "signing.key"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(BuildError, match="not an ECDSA P-256 private key"):
        signing.signing_key(path)


def test_a_directory_where_the_key_should_be_is_a_plain_refusal(tmp_path) -> None:
    (tmp_path / "signing.key").mkdir()
    with pytest.raises(BuildError) as caught:
        signing.signing_key(tmp_path / "signing.key")
    assert "it is a directory" in caught.value.render()


def test_a_place_the_key_cannot_be_written_says_where(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        signing.signing_key(blocker / "mcuhome" / "signing.key")
    rendered = caught.value.render()
    assert "cannot create the firmware signing key" in rendered
    assert signing.KEY_VAR in rendered


def test_no_refusal_ever_prints_the_key(tmp_path) -> None:
    """The private half is never in a message, a log or a build directory."""
    path = tmp_path / "signing.key"
    path.write_text(IMGTOOL_KEY.replace("PRIVATE KEY", "RSA PRIVATE KEY"), encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        signing.signing_key(path)
    rendered = caught.value.render()
    assert "MIGHAgEAMBMGByqGSM49" not in rendered


# --------------------------------------------------------------------------
# The public half (ADR 0015 decision 8, detached signing)
# --------------------------------------------------------------------------


def test_the_public_half_is_derived_from_the_scalar() -> None:
    """Recomputed, never read out of the file's optional copy.

    A key file that disagrees with itself would otherwise produce a
    bootloader that rejects every image the same file signs.
    """
    pem = signing.public_key_pem(signing.generate_key_pem(KNOWN_SCALAR))
    assert pem.startswith("-----BEGIN PUBLIC KEY-----\n")
    assert pem.endswith("-----END PUBLIC KEY-----\n")
    assert signing.looks_like_p256_public_key(pem)


def test_the_public_half_is_the_point_the_private_key_names() -> None:
    point = p256.generator_times(KNOWN_SCALAR)
    assert point is not None
    x, y = point
    expected = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    pem = signing.public_key_pem(signing.generate_key_pem(KNOWN_SCALAR))
    body = pem.split("-----BEGIN PUBLIC KEY-----")[1].split("-----END PUBLIC KEY-----")[0]
    assert expected in base64.b64decode("".join(body.split()))


def test_the_public_half_is_stable() -> None:
    first = signing.public_key_pem(signing.generate_key_pem(KNOWN_SCALAR))
    assert first == signing.public_key_pem(signing.generate_key_pem(KNOWN_SCALAR))


def test_a_private_key_is_not_a_public_key() -> None:
    """--public-key has to be able to catch the one dangerous mix-up."""
    private = signing.generate_key_pem(KNOWN_SCALAR)
    assert signing.looks_like_p256_key(private)
    assert not signing.looks_like_p256_public_key(private)
    public = signing.public_key_pem(private)
    assert signing.looks_like_p256_public_key(public)
    assert not signing.looks_like_p256_key(public)


@pytest.mark.parametrize(
    "text",
    ["", "not a key", "-----BEGIN PRIVATE KEY-----\n@@@\n-----END PRIVATE KEY-----\n"],
)
def test_deriving_from_something_that_is_not_a_key_refuses(text: str) -> None:
    with pytest.raises(ValueError):
        signing.public_key_pem(text)
