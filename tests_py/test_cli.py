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


def test_build_generates_and_then_refuses_to_compile(tmp_path, capsys) -> None:
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "Generated 6 files for bmp180-node" in captured.out
    assert "app/src/mcuhome_config.c" in captured.out
    assert captured.err.startswith(
        "Stopped after code generation: compiling the firmware is not implemented yet "
        "(builder phase 2, block C)."
    )
    assert "--generate-only" in captured.err
    assert (tmp_path / "app" / "src" / "mcuhome_config.c").is_file()


def test_build_generate_only_succeeds(tmp_path, capsys) -> None:
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--generate-only"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert str(tmp_path) in captured.out
    assert (tmp_path / "device-model.json").is_file()
    assert (tmp_path / "app" / "boards" / "nrf7002dk_nrf5340_cpuapp.overlay").is_file()


def test_build_defaults_to_a_build_dir_at_the_tree_root(tmp_path, capsys) -> None:
    tree = tmp_path / "config"
    (tree / "devices" / "bench-node").mkdir(parents=True)
    (tree / "devices" / "bench-node" / "main.yaml").write_text(VALID_CONFIG, "utf-8")

    assert main(["build", "bench-node", "--config-root", str(tree), "--generate-only"]) == 0
    assert (tree / "build" / "bench-node" / "app" / "prj.conf").is_file()
    # Build output stays out of the configuration tree proper.
    assert list((tree / "devices" / "bench-node").iterdir()) == [
        tree / "devices" / "bench-node" / "main.yaml"
    ]
    assert "Generated 6 files for bench-node" in capsys.readouterr().out


def test_build_reports_configuration_problems_and_writes_nothing(tmp_path, capsys) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("device_role: ftd", "device_role: sed"), "utf-8")
    out_dir = tmp_path / "out"
    assert main(["build", str(entry), "--build-dir", str(out_dir), "--generate-only"]) == 1
    assert "Sleepy end devices" in capsys.readouterr().err
    assert not out_dir.exists()


def test_clean_refuses_cleanly(capsys) -> None:
    assert main(["clean", "--all"]) == 1
    assert "mcuhome clean is not implemented yet" in capsys.readouterr().err
