# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The command surface: exit codes, summary output, refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, FIXTURE_TREE, VALID_CONFIG

from mcuhome import __version__, workspace
from mcuhome.cli import main
from mcuhome.generate import APP_DIR

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: The tail of a Zephyr build log, as the linker prints it.
MEMORY_LOG = """\
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB      81.99%
             RAM:      196296 B       448 KB      42.79%
        IDT_LIST:          0 GB        32 KB       0.00%
"""


def _planner(out_dir: Path):
    """A stand-in for plan_build that needs neither west nor a toolchain.

    Only the two things the tests cannot provide are replaced — the
    workspace and the prerequisite check. The command itself is assembled
    by the real code, because that is what the tests are looking at.
    """

    def plan(**kwargs) -> workspace.BuildPlan:
        app_dir = kwargs["out_dir"] / kwargs["app_subdir"]
        build_dir = kwargs["out_dir"] / workspace.BUILD_SUBDIR
        return workspace.BuildPlan(
            topdir=out_dir,
            app_dir=app_dir,
            build_dir=build_dir,
            command=workspace.west_build_command(
                app_dir=app_dir,
                build_dir=build_dir,
                board=kwargs["board"],
                snippets=kwargs["snippets"],
            ),
            env={},
        )

    return plan


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


def test_build_compiles_what_it_generated(tmp_path, capsys, monkeypatch) -> None:
    """The whole command, with only the compiler itself stubbed out."""
    seen: dict[str, object] = {}

    def fake_run(plan, stream=None) -> tuple[int, str]:
        seen["command"] = plan.command
        seen["cwd"] = plan.topdir
        (plan.build_dir / "zephyr").mkdir(parents=True)
        (plan.build_dir / "zephyr" / "zephyr.elf").write_text("", "utf-8")
        (plan.build_dir / "zephyr" / "zephyr.hex").write_text("", "utf-8")
        return 0, MEMORY_LOG

    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", fake_run)

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Generated 7 files for bmp180-node" in out
    assert "app/src/mcuhome_config.c" in out
    # The application it generated is the application it built.
    assert str(tmp_path / "app") in " ".join(seen["command"])  # type: ignore[arg-type]
    assert "--board nrf7002dk/nrf5340/cpuapp" in " ".join(seen["command"])  # type: ignore[arg-type]
    assert "Built bmp180-node." in out
    assert str(tmp_path / "build" / "zephyr" / "zephyr.hex") in out
    assert "FLASH 839.5 KiB of 1024.0 KiB (82.0%)" in out
    assert "RAM 191.7 KiB of 448.0 KiB (42.8%)" in out


def test_build_passes_the_configurations_snippets_and_then_the_extra_ones(
    tmp_path, monkeypatch
) -> None:
    seen: list[str] = []

    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "-S", "debug-rtt"]) == 0
    snippets = [seen[index + 1] for index, item in enumerate(seen) if item == "--snippet"]
    assert snippets == ["matter", "debug-rtt"]


def test_a_relative_build_dir_still_names_an_absolute_application(tmp_path, monkeypatch) -> None:
    """The build runs from the workspace top, not from the user's cwd."""
    seen: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    assert main(["build", str(EXAMPLE), "--build-dir", "out"]) == 0
    assert str((tmp_path / "out" / APP_DIR).resolve()) in seen


def test_build_reports_a_failed_compile_and_points_at_the_build_directory(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", lambda plan, stream=None: (2, "boom\n"))
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "The firmware did not compile (west build exited with 2)." in err
    assert str(tmp_path / "build") in err
    assert "Traceback" not in err


def test_build_in_the_container_refuses_and_names_the_block(tmp_path, capsys) -> None:
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--no-native"]) == 1
    captured = capsys.readouterr()
    # Generation still happened and is still worth having.
    assert "Generated 7 files for bmp180-node" in captured.out
    assert (tmp_path / "app" / "src" / "mcuhome_config.c").is_file()
    assert "builder container is not implemented yet (builder phase 2, block D)" in captured.err
    assert "--no-native" in captured.err


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
    assert (tree / "build" / "bench-node" / APP_DIR / "prj.conf").is_file()
    # Build output stays out of the configuration tree proper.
    assert list((tree / "devices" / "bench-node").iterdir()) == [
        tree / "devices" / "bench-node" / "main.yaml"
    ]
    assert "Generated 7 files for bench-node" in capsys.readouterr().out


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
