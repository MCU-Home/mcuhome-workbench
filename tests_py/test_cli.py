# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The command surface: exit codes, summary output, refusals."""

from __future__ import annotations

import json

import pytest
from conftest import EXAMPLES_DIR, FIXTURE_TREE, VALID_CONFIG

from mcuhome import __version__
from mcuhome.cli import main

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"


def test_version_still_works(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"mcuhome {__version__}"


def test_no_arguments_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "usage: mcuhome" in capsys.readouterr().out


def test_validate_by_device_name_with_config_root(capsys) -> None:
    assert main(["validate", "bench-node", "--config-root", str(FIXTURE_TREE)]) == 0
    out = capsys.readouterr().out
    assert "Device     bench-node (Bench Node)" in out
    assert "Board      nrf7002dk/nrf5340/cpuapp" in out
    assert "Transport  Thread, end device" in out
    assert "Zephyr     4.4" in out
    assert "is valid." in out


def test_validate_by_file_path(capsys) -> None:
    assert main(["validate", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "endpoint 1 [temperature]: temperature_sensor (0x0302 rev 3)" in out
    assert "endpoint 2 [pressure]: pressure_sensor (0x0305 rev 2)" in out
    assert "temperature_measurement (0x0402 rev 4, 3 attributes)" in out
    assert "baro.temperature -> endpoint 1 0x0402/0x0000, every 10 s" in out
    assert "report on 0.1 °C change" in out
    assert "snippets: matter" in out


def test_validate_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["validate", str(EXAMPLE)]) == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [["-v", "validate", str(EXAMPLE)], ["validate", "-v", str(EXAMPLE)]],
)
def test_verbose_prints_the_model(capsys, argv: list[str]) -> None:
    assert main(argv) == 0
    out = capsys.readouterr().out
    payload = out[out.index("{") : out.rindex("}") + 1]
    assert json.loads(payload)["model_version"] == 1


def test_validate_reports_problems_and_exits_one(tmp_path, capsys) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("device_role: ftd", "device_role: sed"), "utf-8")
    assert main(["validate", str(entry)]) == 1
    captured = capsys.readouterr()
    assert "Sleepy end devices" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_unknown_device_exits_one(capsys) -> None:
    assert main(["validate", "nope", "--config-root", str(FIXTURE_TREE)]) == 1
    assert "no device called" in capsys.readouterr().err


def test_build_refuses_cleanly(capsys) -> None:
    assert main(["build", str(EXAMPLE)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("mcuhome build is not implemented yet (builder phase 2, block C).")
    assert "mcuhome validate" in err


def test_clean_refuses_cleanly(capsys) -> None:
    assert main(["clean", "--all"]) == 1
    assert "mcuhome clean is not implemented yet" in capsys.readouterr().err
