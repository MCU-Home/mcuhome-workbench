# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Commissioning credentials: the math, the atomicity, the init flow.

The vectors below are not MCUHome's own output written down — every one
of them is taken from the connectedhomeip checkout the builder is pinned
against, with the file it came from named next to it. That is the whole
value: the QR payload, the manual code and the SPAKE2+ verifier this
builder emits have to be the ones a real controller expects, and the only
way to know is to reproduce numbers somebody else published.

The strongest vector is the last one: the pairing codes of the device
that was commissioned into a production Home Assistant on 2026-08-04.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, FIXTURE_TREE, VALID_CONFIG, package_modules, resolve_file

from mcuhome import pairing, provision
from mcuhome.errors import ConfigError
from mcuhome.generate import APP_DIR, generate
from mcuhome.model import PairingModel

# --------------------------------------------------------------------------
# SPAKE2+ verifier
# --------------------------------------------------------------------------

#: ``(passcode, salt, iterations, verifier)``. The first is CHIP's own
#: Kconfig default (``config/zephyr/Kconfig``, CHIP_DEVICE_SPAKE2_*); the
#: other four are the ones its certification test TC_CADMIN_1_5
#: (``src/python_testing/TC_CADMIN_1_5.py``) generated with
#: ``scripts/tools/spake2p/spake2p.py gen-verifier``, each with the exact
#: command recorded in a comment above it. Between them they cover both
#: iteration-count bounds and both salt-length bounds.
VERIFIER_VECTORS = [
    (
        20202021,
        "U1BBS0UyUCBLZXkgU2FsdA==",
        1000,
        "uWFwqugDNGiEck/po7KHwwMwwqZgN10XuyBajPGuyzUEV/iree4lOrao5GuwnlQ65CJzbeUB49s3"
        "1EH+NEkg0JVI5MGCQGMMT/SRPFNRODm3wH/MBiehuFc6FJ/NH6Rmzw==",
    ),
    (
        20202021,
        "U1BBS0UyUCBLZXkgU2FsdA==",
        999,
        "/q9Xque1iokBVf/SGwjfzJWY0vgmFapUoIcgR+4rXdEEHBELKQ2VYwF9XjZiIrfYztJo2adB8O9M"
        "tQ/LWlfJMqMUt8jYcuQtYTc2NQIOZWFiKXbT5K7ipt4svYVEs1rmLA==",
    ),
    (
        20202021,
        "U1BBS0UyUCBLZXkgU2FsdA==",
        100001,
        "CUhS9rS2NKjXGYwK0CCG80d6XkC1QSCAfs8++IcOCRcEwM4DlA/wxlm/B7w4G/7tZJmLycmdRLJG"
        "lYF2+HDsYdGmoxj0ENNuXTmXsoOhkZUmmTXThAak3U9vGFWbKUHXCQ==",
    ),
    (
        20202021,
        "dG9vX3Nob3J0",
        1000,
        "c8StVjueM851ZnKA+/0m83PHeVIhfhhWvGVCGcAnDD8EbCiPuKb1Z18I7l3TvxTbVkvzS2KPjKPO"
        "CZt1GW80ZoVDP48NAewqEXfl6lY7nmDG9ZzMIhfa8f1EIiBY0/7eJA==",
    ),
    (
        20202021,
        "dGhpcyBwYWtlIHNhbHQgdmVyeSB2ZXJ5IHZlcnkgbG9uZw==",
        1000,
        "nwkb2VD3OTPflW2sAChSwpfkaajErERg/XrhvWPPJL4EM6cSCY/h9lz5SgKy7WB5s1nn1u75amcu"
        "mZrxnVCXbI0vRrM74BV20p0VyOhpOMBaoHpT2Tvev8pc0JDYCjn6wg==",
    ),
]


@pytest.mark.parametrize(("passcode", "salt", "iterations", "expected"), VERIFIER_VECTORS)
def test_verifier_reproduces_the_chip_vectors(
    passcode: int, salt: str, iterations: int, expected: str
) -> None:
    assert pairing.spake2p_verifier(passcode, base64.b64decode(salt), iterations) == expected


def test_the_verifier_is_a_w0_and_an_uncompressed_point() -> None:
    raw = base64.b64decode(pairing.TEST_PAIRING.verifier)
    assert len(raw) == 97
    assert raw[32] == 0x04, "the second half is an uncompressed P-256 point"


def test_the_verifier_depends_on_every_input() -> None:
    """Change any one of the three and the verifier moves.

    The property the atomic Kconfig group exists to protect: a passcode
    change that left the verifier alone would produce a device nothing can
    commission, and this is what makes that visible.
    """
    base = pairing.TEST_PAIRING
    variants = [
        pairing.Pairing(base.discriminator, base.passcode + 1, base.salt, base.iterations),
        pairing.Pairing(base.discriminator, base.passcode, _other_salt(), base.iterations),
        pairing.Pairing(base.discriminator, base.passcode, base.salt, base.iterations + 1),
    ]
    verifiers = {base.verifier, *(variant.verifier for variant in variants)}
    assert len(verifiers) == 4


def _other_salt() -> str:
    return base64.b64encode(b"a different salt".ljust(16, b"!")).decode("ascii")


# --------------------------------------------------------------------------
# Onboarding payloads
# --------------------------------------------------------------------------

#: ``(discriminator, passcode, discovery, flow, vid, pid, manual, qr)``,
#: from ``src/setup_payload/tests/run_python_setup_payload_test.py``
#: (``test_code_generation`` supplies the parameters,
#: ``test_onboardingcode_parsing`` the codes CHIP's own ``chip-tool``
#: produced for them). The non-standard commissioning flows are here
#: because they are the only vectors that exercise the 21-digit manual
#: code and the vendor/product fields of the QR payload; MCUHome itself
#: only ever emits the standard flow.
PAYLOAD_VECTORS = [
    (3840, 20202021, 4, 0, 0, 0, "34970112332", "MT:00000CQM00KA0648G00"),
    (3781, 12349876, 4, 1, 1, 1, "745492075300001000013", "MT:A3L90ARR15G6N57Y900"),
    (2310, 23005908, 4, 2, 0xFFF3, 0x8098, "619156140465523329207", "MT:MZWA6G6026O2XP0II00"),
    (3091, 43338551, 2, 2, 0x1123, 0x0012, "702871264504387000187", "MT:KSNK4M5113-JPR4UY00"),
    (80, 54757432, 6, 2, 0x2345, 0x1023, "402104334209029041311", "MT:0A.T0P--00Y0OJ0.510"),
    (174, 81235604, 7, 1, 0x45, 0x10, "403732495800069000166", "MT:EPX0482F26DAVY09R10"),
    (3431, 49910688, 2, 0, 4891, 2, None, "MT:U9VJ0OMV172PX813210"),
]


@pytest.mark.parametrize(
    ("discriminator", "passcode", "discovery", "flow", "vid", "pid", "manual", "qr"),
    PAYLOAD_VECTORS,
)
def test_onboarding_payloads_reproduce_the_chip_vectors(
    discriminator: int,
    passcode: int,
    discovery: int,
    flow: int,
    vid: int,
    pid: int,
    manual: str | None,
    qr: str,
) -> None:
    assert (
        pairing.qr_payload(
            discriminator=discriminator,
            passcode=passcode,
            discovery=discovery,
            flow=flow,
            vendor_id=vid,
            product_id=pid,
        )
        == qr
    )
    if manual is not None:
        assert (
            pairing.manual_code(
                discriminator=discriminator,
                passcode=passcode,
                flow=flow,
                vendor_id=vid,
                product_id=pid,
            )
            == manual
        )


def test_the_codes_of_the_device_on_the_bench() -> None:
    """The nRF7002-DK commissioned into a production Home Assistant.

    Its Kconfig values are CHIP's defaults (discriminator 0xF00, passcode
    20202021, on-network discovery, test VID/PID), and these are the two
    codes that actually worked against a real controller — the end-to-end
    check that this module's constants are the ones the device advertises.
    """
    assert pairing.TEST_PAIRING.manual_code == "34970112332"
    assert pairing.TEST_PAIRING.qr_payload == "MT:Y.K90AFN00KA0648G00"


def test_the_manual_code_carries_a_verhoeff_check_digit() -> None:
    code = pairing.TEST_PAIRING.manual_code
    assert pairing.verhoeff_check_digit(code[:-1]) == code[-1]
    # A single wrong digit is what the check digit is for.
    assert pairing.verhoeff_check_digit("3497011234") != code[-1]


# --------------------------------------------------------------------------
# The atomic Kconfig group
# --------------------------------------------------------------------------

#: Every Kconfig symbol that carries part of the commissioning identity.
IDENTITY_SYMBOLS = (
    "CONFIG_CHIP_DEVICE_VENDOR_ID",
    "CONFIG_CHIP_DEVICE_PRODUCT_ID",
    "CONFIG_CHIP_DEVICE_DISCRIMINATOR",
    "CONFIG_CHIP_DEVICE_SPAKE2_PASSCODE",
    "CONFIG_CHIP_DEVICE_SPAKE2_IT",
    "CONFIG_CHIP_DEVICE_SPAKE2_SALT",
    "CONFIG_CHIP_DEVICE_SPAKE2_TEST_VERIFIER",
)


def test_the_whole_group_comes_from_one_call(monkeypatch) -> None:
    """Silence :func:`pairing.kconfig_lines` and the group disappears whole.

    The footgun this guards against is a builder that emits a passcode
    without the verifier derived from it: the image builds, boots and
    advertises itself, and then refuses every commissioner, with nothing
    in the build log to look at. One call is what makes the half-written
    state unreachable — so if any of the seven symbols can still turn up
    with the call disabled, some second code path is writing it.
    """
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    monkeypatch.setattr(pairing, "kconfig_lines", lambda _: [])
    fragment = generate(model, config_name="x.yaml")[f"{APP_DIR}/prj.conf"]
    for symbol in IDENTITY_SYMBOLS:
        assert symbol not in fragment


def test_no_other_module_spells_an_identity_symbol() -> None:
    """The names exist in exactly one file, which is why one call suffices.

    ``generate.py`` is the only module that emits Kconfig at all, so it
    is the only plausible place a second spelling could appear — and
    ADR 0020 moves it into another distribution. Asserting it was
    examined is what keeps this test from passing while looking at less
    than it did yesterday.
    """
    modules = package_modules()
    assert any(path.name == "generate.py" for path in modules), (
        "the search no longer reaches generate.py, where a second spelling "
        "would appear — extend conftest.PACKAGES"
    )
    for module in modules:
        if module.name == "pairing.py":
            continue
        text = module.read_text(encoding="utf-8")
        for symbol in IDENTITY_SYMBOLS:
            assert symbol not in text, f"{module.name} names {symbol}"


def test_the_group_is_emitted_complete() -> None:
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    fragment = generate(model, config_name="x.yaml")[f"{APP_DIR}/prj.conf"]
    for symbol in IDENTITY_SYMBOLS:
        assert f"{symbol}=" in fragment
    assert 'CONFIG_CHIP_DEVICE_SPAKE2_TEST_VERIFIER="uWFwqugDNGiEck' in fragment


def test_the_verifier_is_derived_and_never_carried_in_the_model() -> None:
    """The model states the inputs; every consumer recomputes the output.

    A verifier stored next to the passcode would be a second copy that can
    disagree with it — the same drift the Kconfig group exists to prevent,
    moved one layer up.
    """
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    assert "verifier" not in model.to_json()
    assert model.network.pairing is not None
    assert model.network.pairing.passcode == pairing.TEST_PASSCODE


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_generation_is_deterministic_for_a_configuration_with_credentials() -> None:
    """Random credentials, deterministic build: the point of the design.

    The fixture tree's device has a passcode and a salt of its own, drawn
    once and written down. Everything derived from them — including the
    verifier, which costs a PBKDF2 — has to come out identical every time,
    or the builder's byte-for-byte reproducibility is gone.
    """
    entry = FIXTURE_TREE / "devices" / "bench-node" / "main.yaml"
    first = generate(resolve_file(entry), config_name="main.yaml")
    second = generate(resolve_file(entry), config_name="main.yaml")
    assert first == second
    fragment = first[f"{APP_DIR}/prj.conf"]
    assert "CONFIG_CHIP_DEVICE_SPAKE2_PASSCODE=84920174" in fragment
    assert "CONFIG_CHIP_DEVICE_DISCRIMINATOR=0x90A" in fragment
    assert f"CONFIG_CHIP_DEVICE_SPAKE2_IT={pairing.DEFAULT_ITERATIONS}" in fragment


def test_the_test_credentials_are_chips_own_tuple_unchanged() -> None:
    """Including the iteration count, which is why the sample still matches.

    ``use_test_pairing`` means "the values CHIP publishes", all four of
    them. Substituting MCUHome's own iteration count here would change the
    verifier and therefore the reference image, for a tuple whose only
    purpose is to be the well-known one.
    """
    assert pairing.TEST_PAIRING.iterations == 1000
    assert pairing.TEST_PAIRING.iterations != pairing.DEFAULT_ITERATIONS


# --------------------------------------------------------------------------
# Drawing credentials
# --------------------------------------------------------------------------


def test_random_credentials_are_in_range_and_never_forbidden() -> None:
    seen = set()
    for _ in range(50):
        drawn = pairing.random_pairing()
        assert 0 <= drawn.discriminator <= pairing.DISCRIMINATOR_MAX
        assert pairing.PASSCODE_MIN <= drawn.passcode <= pairing.PASSCODE_MAX
        assert drawn.passcode not in pairing.FORBIDDEN_PASSCODES
        assert len(base64.b64decode(drawn.salt)) == pairing.SALT_BYTES
        assert drawn.iterations == pairing.DEFAULT_ITERATIONS
        assert not drawn.test_credentials
        seen.add((drawn.discriminator, drawn.passcode, drawn.salt))
    assert len(seen) == 50, "credentials must not repeat"


def test_the_iteration_count_stays_inside_what_matter_allows() -> None:
    assert pairing.ITERATIONS_MIN <= pairing.DEFAULT_ITERATIONS <= pairing.ITERATIONS_MAX


def test_the_forbidden_list_is_the_specifications() -> None:
    assert len(pairing.FORBIDDEN_PASSCODES) == 12
    assert 12345678 in pairing.FORBIDDEN_PASSCODES
    assert 87654321 in pairing.FORBIDDEN_PASSCODES
    assert all(digit * 11111111 in pairing.FORBIDDEN_PASSCODES for digit in range(10))


# --------------------------------------------------------------------------
# mcuhome init-pairing
# --------------------------------------------------------------------------

FIXED = pairing.Pairing(
    discriminator=2314,
    passcode=84920174,
    salt="GBG/P9QwOwhgSLc1fnDR7FWutk+sSsOutub53NjXsp8=",
    iterations=pairing.DEFAULT_ITERATIONS,
)

WITHOUT_CREDENTIALS = VALID_CONFIG.replace("    use_test_pairing: true\n", "")


def _init(path: Path, **kwargs) -> provision.InitResult:
    return provision.init_pairing(
        path, secrets_file=path.parent / "secrets.yaml", draw=lambda: FIXED, **kwargs
    )


def test_init_pairing_writes_credentials_the_builder_then_accepts(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    result = _init(path)

    assert result.pairing == FIXED
    assert not result.replaced
    assert result.secrets_file is None

    model = resolve_file(path)
    assert model.network.pairing is not None
    assert model.network.pairing.passcode == FIXED.passcode
    assert model.network.pairing.salt == FIXED.salt
    assert model.network.pairing.discriminator == FIXED.discriminator
    assert not model.network.pairing.test_credentials


def test_init_pairing_only_adds_lines(write_config) -> None:
    """Everything the user typed is still there, byte for byte.

    The file is edited by line, not re-serialized, precisely so that a
    command about credentials cannot reformat somebody's configuration on
    the way past.
    """
    path = write_config(WITHOUT_CREDENTIALS)
    before = path.read_text(encoding="utf-8").splitlines()
    _init(path)
    after = path.read_text(encoding="utf-8").splitlines()

    added = [line for line in after if line not in before or after.count(line) > before.count(line)]
    assert [line for line in after if line not in added] == before
    assert added == [
        *[f"    {line}" for line in provision.CREDENTIAL_COMMENT],
        f"    discriminator: {FIXED.discriminator}",
        f"    passcode: {FIXED.passcode}",
        f'    salt: "{FIXED.salt}"',
    ]


def test_init_pairing_refuses_to_overwrite_without_being_told(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    _init(path)
    untouched = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        _init(path)
    assert caught.value.message == "This device already has commissioning credentials."
    assert "--force" in (caught.value.hint or "")
    assert path.read_text(encoding="utf-8") == untouched


def test_force_replaces_the_credentials_without_stacking_up_comments(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    _init(path)
    once = path.read_text(encoding="utf-8")

    other = pairing.Pairing(
        discriminator=17, passcode=11223344, salt=FIXED.salt, iterations=FIXED.iterations
    )
    result = provision.init_pairing(
        path, secrets_file=path.parent / "secrets.yaml", force=True, draw=lambda: other
    )
    twice = path.read_text(encoding="utf-8")

    assert result.replaced
    assert twice.count(provision.CREDENTIAL_COMMENT[0]) == 1
    assert len(twice.splitlines()) == len(once.splitlines())
    assert "passcode: 11223344" in twice
    assert str(FIXED.passcode) not in twice


def test_force_also_clears_a_test_pairing_opt_in(write_config) -> None:
    path = write_config(VALID_CONFIG)
    provision.init_pairing(
        path, secrets_file=path.parent / "secrets.yaml", force=True, draw=lambda: FIXED
    )
    text = path.read_text(encoding="utf-8")
    assert "use_test_pairing" not in text
    assert resolve_file(path).network.pairing == PairingModel(
        discriminator=FIXED.discriminator,
        passcode=FIXED.passcode,
        salt=FIXED.salt,
        iterations=FIXED.iterations,
        test_credentials=False,
    )


def test_secrets_mode_keeps_the_values_out_of_the_device_file(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    result = _init(path, use_secrets=True)

    device_text = path.read_text(encoding="utf-8")
    assert "passcode: !secret bench_node_passcode" in device_text
    assert str(FIXED.passcode) not in device_text
    assert FIXED.salt not in device_text

    assert result.secrets_file is not None
    secrets_text = result.secrets_file.read_text(encoding="utf-8")
    assert f"bench_node_passcode: {FIXED.passcode}" in secrets_text
    assert f'bench_node_salt: "{FIXED.salt}"' in secrets_text

    # And the two halves still add up to a device the builder accepts.
    model = resolve_file(path)
    assert model.network.pairing is not None
    assert model.network.pairing.passcode == FIXED.passcode


def test_secrets_mode_keeps_what_the_secrets_file_already_had(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS, secrets="wifi_password: hunter2\n")
    _init(path, use_secrets=True)
    text = (path.parent / "secrets.yaml").read_text(encoding="utf-8")
    assert "wifi_password: hunter2" in text
    assert "bench_node_discriminator: 2314" in text


def test_a_device_without_a_transport_has_nothing_to_commission(write_config) -> None:
    path = write_config("device:\n  name: bench-node\n  board: nrf7002dk/nrf5340/cpuapp\n")
    with pytest.raises(ConfigError) as caught:
        _init(path)
    assert "no network: section" in caught.value.message


def test_a_device_with_matter_switched_off_is_never_commissioned(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS.replace("    enabled: true", "    enabled: false"))
    with pytest.raises(ConfigError) as caught:
        _init(path)
    assert "Matter is switched off" in caught.value.message


def test_a_matter_section_that_is_only_a_key_still_works(write_config) -> None:
    text = WITHOUT_CREDENTIALS.replace("  matter:\n    enabled: true\n", "  matter:\n")
    path = write_config(text)
    _init(path)
    assert resolve_file(path).network.pairing is not None


def test_a_configuration_without_a_matter_section_gets_one(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS.replace("  matter:\n    enabled: true\n", ""))
    _init(path)
    text = path.read_text(encoding="utf-8")
    assert "  matter:\n" in text
    assert resolve_file(path).network.pairing is not None
