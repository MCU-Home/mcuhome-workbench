# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome device matter-pairing --new``: :mod:`mcuhome.workbench.provision`.

The workbench half of the commissioning credentials. The math, the CHIP
vectors and the atomic Kconfig group are :mod:`mcuhome.model.pairing` and
live in ``mcuhome-sdk``'s ``tests/python/test_pairing.py``; what this file
covers is the one command that *draws* credentials — once, into the
user's YAML, never per build (yaml-schema.md §4.1) — and then the builder
accepting what it wrote.

Plus, at the bottom, this repository's half of that file's whole-package
invariant, created from the recipe it left behind: the seven Kconfig
symbols that carry a device's commissioning identity are emitted by one
function, and the search that proves no second spelling exists runs over
``conftest.PACKAGES`` — which names the workbench alone since ADR 0024,
so the claim has to be made once on each side of the split.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import VALID_CONFIG, package_modules, resolve_file
from mcuhome.model import pairing
from mcuhome.model.errors import ConfigError
from mcuhome.model.model import PairingModel

from mcuhome.workbench import provision

FIXED = pairing.Pairing(
    discriminator=2314,
    passcode=84920174,
    salt="GBG/P9QwOwhgSLc1fnDR7FWutk+sSsOutub53NjXsp8=",
    iterations=pairing.DEFAULT_ITERATIONS,
)

WITHOUT_CREDENTIALS = VALID_CONFIG.replace("    use_test_pairing: true\n", "")


def _init(path: Path, **kwargs) -> provision.PairingResult:
    return provision.init_pairing(
        path, secrets_file=path.parent / "secrets" / "main.yaml", draw=lambda: FIXED, **kwargs
    )


def test_init_pairing_writes_credentials_the_builder_then_accepts(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    result = _init(path)

    assert result.pairing == FIXED
    assert not result.replaced
    assert result.secrets_file == path.parent / "secrets" / "devices" / "bench-node.yaml"

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
    # References only — the values themselves are in the secrets file
    # (PO 2026-08-15, security-relevant values never in the committable file).
    assert added == [
        *[f"    {line}" for line in provision.CREDENTIAL_COMMENT],
        "    discriminator: !secret matter_discriminator",
        "    passcode: !secret matter_passcode",
        "    salt: !secret matter_salt",
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
        path, secrets_file=path.parent / "secrets" / "main.yaml", force=True, draw=lambda: other
    )
    twice = path.read_text(encoding="utf-8")

    assert result.replaced
    assert twice.count(provision.CREDENTIAL_COMMENT[0]) == 1
    assert len(twice.splitlines()) == len(once.splitlines())
    secrets_text = result.secrets_file.read_text(encoding="utf-8")
    assert "matter_passcode: 11223344" in secrets_text
    assert str(FIXED.passcode) not in secrets_text
    assert str(FIXED.passcode) not in twice


def test_force_also_clears_a_test_pairing_opt_in(write_config) -> None:
    path = write_config(VALID_CONFIG)
    provision.init_pairing(
        path, secrets_file=path.parent / "secrets" / "main.yaml", force=True, draw=lambda: FIXED
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


def test_the_values_stay_out_of_the_committable_file(write_config) -> None:
    path = write_config(WITHOUT_CREDENTIALS)
    result = _init(path)

    device_text = path.read_text(encoding="utf-8")
    assert "passcode: !secret matter_passcode" in device_text
    assert str(FIXED.passcode) not in device_text
    assert FIXED.salt not in device_text

    secrets_text = result.secrets_file.read_text(encoding="utf-8")
    assert f"matter_passcode: {FIXED.passcode}" in secrets_text
    assert f'matter_salt: "{FIXED.salt}"' in secrets_text
    assert (result.secrets_file.stat().st_mode & 0o777) == 0o600
    # The directories the command created are owner-only too (ADR 0022 §5).
    assert (result.secrets_file.parent.stat().st_mode & 0o777) == 0o700
    assert (result.secrets_file.parent.parent.stat().st_mode & 0o777) == 0o700

    # And the two halves still add up to a device the builder accepts.
    model = resolve_file(path)
    assert model.network.pairing is not None
    assert model.network.pairing.passcode == FIXED.passcode


def test_the_project_secrets_file_is_not_touched(write_config) -> None:
    """The values go to the device's own file; main.yaml stays the user's."""
    path = write_config(WITHOUT_CREDENTIALS, secrets="wifi_password: hunter2\n")
    result = _init(path)
    main_text = (path.parent / "secrets" / "main.yaml").read_text(encoding="utf-8")
    assert main_text == "wifi_password: hunter2\n"
    assert result.secrets_file != path.parent / "secrets" / "main.yaml"
    assert f"matter_discriminator: {FIXED.discriminator}" in result.secrets_file.read_text(
        encoding="utf-8"
    )


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


def test_a_configuration_without_a_matter_section_is_refused(write_config) -> None:
    """PO 2026-08-15: absence means off, and a credentials command must
    not switch a protocol on behind the author's back."""
    path = write_config(WITHOUT_CREDENTIALS.replace("  matter:\n    enabled: true\n", ""))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        _init(path)
    assert "there is no matter: section" in caught.value.message
    assert "enabled: true" in (caught.value.hint or "")
    assert path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# The atomic Kconfig group, from this side of the split
# --------------------------------------------------------------------------

#: Every Kconfig symbol that carries part of the commissioning identity.
#: The same seven ``mcuhome.model.pairing.kconfig_lines()`` writes from
#: one tuple, restated because this repository cannot import a list out
#: of the other one's test suite.
IDENTITY_SYMBOLS = (
    "CONFIG_CHIP_DEVICE_VENDOR_ID",
    "CONFIG_CHIP_DEVICE_PRODUCT_ID",
    "CONFIG_CHIP_DEVICE_DISCRIMINATOR",
    "CONFIG_CHIP_DEVICE_SPAKE2_PASSCODE",
    "CONFIG_CHIP_DEVICE_SPAKE2_IT",
    "CONFIG_CHIP_DEVICE_SPAKE2_SALT",
    "CONFIG_CHIP_DEVICE_SPAKE2_TEST_VERIFIER",
)


def test_no_workbench_module_spells_an_identity_symbol() -> None:
    """The names exist in exactly one file, which is why one call suffices.

    The footgun is a builder that emits a passcode without the verifier
    derived from it: the image builds, boots and advertises itself, and
    then refuses every commissioner, with nothing in the build log to
    look at. One call is what makes that half-written state unreachable,
    and one call is only true while one file spells the names.

    ``provision.py`` is asserted to be among the modules examined because
    it is the plausible place a second spelling would appear here — it is
    the module that *draws* the credentials, one editing step away from
    writing them out as Kconfig too. Asserting it was reached is what
    keeps this test from passing while looking at less than it did
    yesterday. Text and not syntax, deliberately: a symbol named in a
    comment is a second spelling waiting to be uncommented.
    """
    modules = package_modules()
    assert any(path.name == "provision.py" for path in modules), (
        "the search no longer reaches provision.py, where a second spelling "
        "would appear — extend conftest.PACKAGES"
    )
    for module in modules:
        text = module.read_text(encoding="utf-8")
        for symbol in IDENTITY_SYMBOLS:
            assert symbol not in text, f"{module.name} names {symbol}"
