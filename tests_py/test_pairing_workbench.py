# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome init-pairing``: :mod:`mcuhome.workbench.provision`.

The workbench half of the commissioning credentials. The math, the
CHIP vectors and the atomic Kconfig group are :mod:`mcuhome.model.pairing`
and live in ``test_pairing.py``; what this file covers is the one command
that *draws* credentials — once, into the user's YAML, never per build
(yaml-schema.md §4.1) — and then the builder accepting what it wrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import VALID_CONFIG, resolve_file

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
