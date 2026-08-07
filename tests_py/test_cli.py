# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The command surface: exit codes, summary output, refusals."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, FIXTURE_TREE, VALID_CONFIG

from mcuhome import __version__, container, imgtool, signing, workspace
from mcuhome.cli import main
from mcuhome.generate import APP_DIR
from mcuhome.manifest import MANIFEST_FILE

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: A sysbuild build log: one linker report per image, each preceded by the
#: only line that says whose output follows.
MEMORY_LOG = """\
[1/2] Performing build step for 'mcuboot'
Memory region         Used Size  Region Size  %age Used
           FLASH:       57344 B        64 KB      87.50%
             RAM:       29424 B       448 KB       6.41%
[2/2] Performing build step for 'app'
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
                bootloader_snippets=kwargs.get("bootloader_snippets", ()),
                signing_key=kwargs.get("signing_key"),
                detached_signing=kwargs.get("detached_signing", False),
                jobs=kwargs["jobs"],
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
    assert "snippets: matter, boot-mode" in out


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
        for image in ("mcuboot", APP_DIR):
            output = plan.build_dir / image / "zephyr"
            output.mkdir(parents=True)
            (output / "zephyr.elf").write_text("", "utf-8")
            (output / "zephyr.hex").write_text("", "utf-8")
            (output / "zephyr.bin").write_text("x" * 1024, "utf-8")
        (plan.build_dir / APP_DIR / "zephyr" / "zephyr.signed.bin").write_text("", "utf-8")
        (plan.build_dir / "merged_nrf7002dk_nrf5340_cpuapp.hex").write_text("", "utf-8")
        return 0, MEMORY_LOG

    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", fake_run)

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native"]) == 0
    out = capsys.readouterr().out
    assert "Generated 12 files for bmp180-node" in out
    assert "app/src/mcuhome_config.c" in out
    assert "app/sysbuild.conf" in out
    # The application it generated is the application it built.
    assert str(tmp_path / "app") in " ".join(seen["command"])  # type: ignore[arg-type]
    assert "--board nrf7002dk/nrf5340/cpuapp" in " ".join(seen["command"])  # type: ignore[arg-type]
    assert "Built bmp180-node." in out
    # Both images, each under its own sysbuild sub-directory, and the
    # signed application next to the raw one.
    assert "mcuboot (bootloader)  1.0 KiB" in out
    assert "app (application)  1.0 KiB" in out
    assert str(tmp_path / "build" / "mcuboot" / "zephyr" / "zephyr.hex") in out
    assert str(tmp_path / "build" / APP_DIR / "zephyr" / "zephyr.signed.bin") in out
    assert str(tmp_path / "build" / "merged_nrf7002dk_nrf5340_cpuapp.hex") in out
    assert "memory: FLASH 56.0 KiB of 64.0 KiB (87.5%)" in out
    assert "memory: FLASH 839.5 KiB of 1024.0 KiB (82.0%)" in out
    assert "memory: RAM 191.7 KiB of 448.0 KiB (42.8%)" in out
    # And the layout those images were built against.
    assert "Flash layout (class A, MCUboot swap-using-offset, staging: external-flash)" in out
    assert "image-0  internal 0x014000..0x0f8000   912 KiB" in out


def test_build_passes_the_configurations_snippets_and_then_the_extra_ones(
    tmp_path, monkeypatch
) -> None:
    """Per image, because sysbuild would otherwise give them to both."""
    seen: list[str] = []

    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    argv = ["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native", "-S", "debug-rtt"]
    assert main(argv) == 0
    assert "--snippet" not in seen
    assert f"-D{APP_DIR}_SNIPPET=matter;boot-mode;debug-rtt" in seen
    assert "-Dmcuboot_SNIPPET=boot-mode" in seen


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
    assert main(["build", str(EXAMPLE), "--build-dir", "out", "--native"]) == 0
    assert str((tmp_path / "out" / APP_DIR).resolve()) in seen


def test_build_uses_the_jobs_flag_and_reports_its_source(tmp_path, capsys, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    argv = ["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native", "--jobs", "3"]
    assert main(argv) == 0
    assert "-o=-j3" in seen
    assert "jobs 3 (flag)" in capsys.readouterr().out


def test_build_uses_the_environment_variable_when_no_flag_is_given(
    tmp_path, capsys, monkeypatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    monkeypatch.setenv(workspace.JOBS_VAR, "5")
    argv = ["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native"]
    assert main(argv) == 0
    assert "-o=-j5" in seen
    assert "jobs 5 (env)" in capsys.readouterr().out


def test_build_auto_detects_when_neither_flag_nor_environment_is_given(
    tmp_path, capsys, monkeypatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(
        workspace,
        "run_build",
        lambda plan, stream=None: (seen.extend(plan.command), (0, ""))[1],
    )
    monkeypatch.delenv(workspace.JOBS_VAR, raising=False)
    monkeypatch.setattr(workspace, "detect_jobs", lambda: 7)
    argv = ["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native"]
    assert main(argv) == 0
    assert "-o=-j7" in seen
    assert "jobs 7 (auto)" in capsys.readouterr().out


def test_jobs_zero_is_a_plain_language_refusal(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["build", str(EXAMPLE), "--jobs", "0"])
    assert caught.value.code == 2
    assert "--jobs must be at least 1" in capsys.readouterr().err


def test_jobs_garbage_is_a_plain_language_refusal(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["build", str(EXAMPLE), "--jobs", "nope"])
    assert caught.value.code == 2
    assert "whole number of parallel build jobs" in capsys.readouterr().err


def test_build_reports_a_failed_compile_and_points_at_the_build_directory(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", lambda plan, stream=None: (2, "boom\n"))
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native"]) == 1
    err = capsys.readouterr().err
    assert "The firmware did not compile (west build exited with 2)." in err
    assert str(tmp_path / "build") in err
    assert "Traceback" not in err


def test_build_without_a_flag_compiles_in_the_container(tmp_path, capsys, monkeypatch) -> None:
    """ADR 0007's default, as the command line sees it.

    Nothing but the flag decides which of the two planners runs, and the
    container is the one that runs when nobody says otherwise.
    """
    chosen: list[str] = []

    def fake_plan(**kwargs) -> workspace.BuildPlan:
        chosen.append("container")
        plan = _planner(tmp_path)(**{k: v for k, v in kwargs.items() if k != "image"})
        return replace(plan, image=kwargs["image"] or container.IMAGE)

    monkeypatch.setattr(container, "plan_build", fake_plan)
    monkeypatch.setattr(workspace, "run_build", lambda plan, stream=None: (0, ""))
    monkeypatch.setattr(
        workspace, "plan_build", lambda **kwargs: pytest.fail("the native path ran by default")
    )

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path)]) == 0
    assert chosen == ["container"]
    assert container.IMAGE in capsys.readouterr().out


def test_the_image_can_be_named_per_build(tmp_path, capsys, monkeypatch) -> None:
    def fake_plan(**kwargs) -> workspace.BuildPlan:
        plan = _planner(tmp_path)(**{k: v for k, v in kwargs.items() if k != "image"})
        return replace(plan, image=kwargs["image"] or container.IMAGE)

    monkeypatch.setattr(container, "plan_build", fake_plan)
    monkeypatch.setattr(workspace, "run_build", lambda plan, stream=None: (0, ""))
    argv = ["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--image", "localhost/b:wip"]
    assert main(argv) == 0
    assert "in localhost/b:wip" in capsys.readouterr().out


def test_a_missing_image_is_a_plain_refusal_after_a_finished_generation(
    tmp_path, capsys, monkeypatch
) -> None:
    """Stage 4 output is worth keeping even when stage 5 cannot start."""
    monkeypatch.setattr(
        container, "_run_quiet", lambda command, env: 0 if "version" in command else 1
    )
    monkeypatch.setenv(container.CCACHE_DIR_VAR, str(tmp_path / "cache"))

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--no-native"]) == 1
    captured = capsys.readouterr()
    assert "Generated 12 files for bmp180-node" in captured.out
    assert (tmp_path / "app" / "src" / "mcuhome_config.c").is_file()
    assert "is not on this machine" in captured.err
    assert "docker pull" in captured.err
    assert "--native" in captured.err
    assert "Traceback" not in captured.err


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
    assert "Generated 12 files for bench-node" in capsys.readouterr().out


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


# --------------------------------------------------------------------------
# Commissioning codes
# --------------------------------------------------------------------------


def test_validate_prints_the_pairing_codes(capsys) -> None:
    assert main(["validate", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "Commissioning" in out
    assert "manual code    34970112332" in out
    assert "QR code        MT:Y.K90AFN00KA0648G00" in out
    assert "discriminator  3840 (0xF00)" in out


def test_the_published_test_credentials_are_called_out(capsys) -> None:
    assert main(["validate", "bench-node", "--config-root", str(FIXTURE_TREE)]) == 0
    own = capsys.readouterr().out
    assert "published with the Matter SDK" not in own, "this fixture has credentials of its own"

    assert main(["validate", str(EXAMPLE)]) == 0
    assert "published with the Matter SDK" in capsys.readouterr().out


def test_build_prints_the_pairing_codes_last(tmp_path, capsys) -> None:
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--generate-only"]) == 0
    out = capsys.readouterr().out
    assert out.index("Commissioning") > out.index("Generated 12 files")
    assert "MT:Y.K90AFN00KA0648G00" in out


def test_init_pairing_writes_the_codes_and_prints_them(tmp_path, capsys) -> None:
    tree = tmp_path / "config"
    (tree / "devices" / "bench-node").mkdir(parents=True)
    entry = tree / "devices" / "bench-node" / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("    use_test_pairing: true\n", ""), "utf-8")

    assert main(["init-pairing", "bench-node", "--config-root", str(tree)]) == 0
    out = capsys.readouterr().out
    assert str(entry) in out
    assert "Commissioning" in out
    assert "manual code" in out
    assert "QR code        MT:" in out

    # The device now builds, and the codes it reports are the ones just
    # written — the loop that makes the credentials ordinary input.
    assert main(["validate", "bench-node", "--config-root", str(tree)]) == 0
    assert out.splitlines()[3] in capsys.readouterr().out


def test_init_pairing_refuses_a_second_time(tmp_path, capsys) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    assert main(["init-pairing", str(entry)]) == 1
    captured = capsys.readouterr()
    assert "already has commissioning credentials" in captured.err
    assert "--force" in captured.err
    assert entry.read_text("utf-8") == VALID_CONFIG


def test_init_pairing_with_secrets_writes_two_files(tmp_path, capsys) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("    use_test_pairing: true\n", ""), "utf-8")
    assert main(["init-pairing", str(entry), "--secrets"]) == 0

    out = capsys.readouterr().out
    assert str(tmp_path / "secrets.yaml") in out
    assert "!secret bench_node_passcode" in entry.read_text("utf-8")
    assert main(["validate", str(entry)]) == 0


# --------------------------------------------------------------------------
# --json (dashboard ADR 0011 decision 4)
# --------------------------------------------------------------------------


def test_validate_json_prints_the_resolved_model(capsys) -> None:
    assert main(["validate", str(EXAMPLE), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["errors"] == []
    assert document["model"]["device"]["name"] == "bmp180-node"
    assert document["file"] == EXAMPLE.name


def test_validate_json_suppresses_the_human_summary(capsys) -> None:
    main(["validate", str(EXAMPLE), "--json"])
    out = capsys.readouterr().out
    assert "Commissioning" not in out
    assert "is valid." not in out


def test_validate_json_reports_every_problem_with_a_relative_path(tmp_path, capsys) -> None:
    tree = tmp_path / "config"
    entry = tree / "devices" / "bench-node" / "main.yaml"
    entry.parent.mkdir(parents=True)
    entry.write_text(VALID_CONFIG.replace("nrf7002dk/nrf5340/cpuapp", "nrf99dk"), "utf-8")

    assert main(["validate", "bench-node", "--config-root", str(tree), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["model"] is None
    problem = document["errors"][0]
    assert problem["file"] == "devices/bench-node/main.yaml"
    assert problem["kind"] == "ConfigError"
    assert problem["line"] and problem["hint"]


def test_a_refusal_before_the_tree_is_resolved_is_still_json(capsys) -> None:
    """Exit codes do not change with --json, and neither does the stream."""
    assert main(["validate", "no-such-device", "--config-root", "/nope", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["errors"][0]["kind"] == "ConfigError"


def test_build_json_mirrors_the_manifest(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", _fake_build_run)

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["device"] == "bmp180-node"
    assert document["manifest"]["signing"]["signed_by_the_build"] is True
    assert json.loads((tmp_path / MANIFEST_FILE).read_text("utf-8")) == document["manifest"]


def test_generate_only_json_says_there_is_no_manifest(tmp_path, capsys) -> None:
    assert (
        main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--generate-only", "--json"])
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["manifest"] is None
    assert f"{APP_DIR}/prj.conf" in document["generated"]


# --------------------------------------------------------------------------
# The build manifest lands in the build directory
# --------------------------------------------------------------------------


def _fake_build_run(plan, stream=None) -> tuple[int, str]:
    """Stage 5 without a compiler: the files a real build leaves behind."""
    for image in ("mcuboot", APP_DIR):
        output = plan.build_dir / image / "zephyr"
        output.mkdir(parents=True, exist_ok=True)
        (output / "zephyr.elf").write_bytes(b"\x7fELF")
        (output / "zephyr.hex").write_text(":00000001FF\n", "utf-8")
        (output / "zephyr.bin").write_bytes(bytes(1024))
    app_output = plan.build_dir / APP_DIR / "zephyr"
    app_output.joinpath(".config").write_text(
        'CONFIG_ROM_START_OFFSET=0x200\nCONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="0.0.0+0"\n', "utf-8"
    )
    if "-DMCUHOME_DETACHED_SIGNING=y" not in plan.command:
        app_output.joinpath("zephyr.signed.bin").write_bytes(bytes(1600))
        app_output.joinpath("zephyr.signed.hex").write_text(":00000001FF\n", "utf-8")
    (plan.build_dir / "merged_nrf7002dk_nrf5340_cpuapp.hex").write_text(":00000001FF\n", "utf-8")
    return 0, MEMORY_LOG


def test_a_build_writes_its_manifest(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", _fake_build_run)

    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--native"]) == 0
    assert str(tmp_path / MANIFEST_FILE) in capsys.readouterr().out
    document = json.loads((tmp_path / MANIFEST_FILE).read_text("utf-8"))
    assert document["device"]["name"] == "bmp180-node"
    assert document["build"]["snippets"] == ["matter", "boot-mode"]
    assert document["signing"]["arguments"]["slot-size"] == 912 * 1024


# --------------------------------------------------------------------------
# Detached signing (ADR 0015 decision 8)
# --------------------------------------------------------------------------


def _public_key(tmp_path: Path) -> Path:
    private = tmp_path / "signing.key"
    private.write_text(signing.generate_key_pem(0x1234567890ABCDEF), "utf-8")
    public = tmp_path / "signing.pub"
    public.write_text(signing.public_key_pem(private.read_text("utf-8")), "utf-8")
    return public


def test_no_sign_without_a_public_key_is_a_refusal(tmp_path, capsys) -> None:
    assert main(["build", str(EXAMPLE), "--build-dir", str(tmp_path), "--no-sign"]) == 1
    err = capsys.readouterr().err
    assert "mcuhome public-key" in err


def test_no_sign_refuses_a_private_key_as_the_public_one(tmp_path, capsys) -> None:
    """The one mistake the feature exists to prevent."""
    private = tmp_path / "signing.key"
    private.write_text(signing.generate_key_pem(0x1234567890ABCDEF), "utf-8")
    assert (
        main(
            [
                "build",
                str(EXAMPLE),
                "--build-dir",
                str(tmp_path / "out"),
                "--no-sign",
                "--public-key",
                str(private),
            ]
        )
        == 1
    )
    assert "is a private key" in capsys.readouterr().err


def test_no_sign_builds_unsigned_and_says_what_is_next(tmp_path, capsys, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", _fake_build_run)

    assert (
        main(
            [
                "build",
                str(EXAMPLE),
                "--build-dir",
                str(out_dir),
                "--native",
                "--no-sign",
                "--public-key",
                str(_public_key(tmp_path)),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "public key" in out
    assert f"mcuhome sign {out_dir}" in out

    document = json.loads((out_dir / MANIFEST_FILE).read_text("utf-8"))
    assert document["signing"]["signed_by_the_build"] is False
    assert document["signing"]["signed"] is False
    assert document["signing"]["arguments"]["header-size"] == 512
    # Nothing in the directory may look bootable: no signed image, and no
    # combined hex, which sysbuild fills with the *unsigned* application.
    assert not (out_dir / "build" / APP_DIR / "zephyr" / "zephyr.signed.bin").exists()
    assert not list((out_dir / "build").glob(workspace.MERGED_IMAGE_GLOB))
    assert document["merged"] is None


def test_the_detached_build_command_carries_the_public_key(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))

    def capture(plan, stream=None):
        seen["command"] = plan.command
        return _fake_build_run(plan, stream)

    monkeypatch.setattr(workspace, "run_build", capture)
    public = _public_key(tmp_path)
    main(
        [
            "build",
            str(EXAMPLE),
            "--build-dir",
            str(tmp_path / "out"),
            "--native",
            "--no-sign",
            "--public-key",
            str(public),
        ]
    )
    command = seen["command"]
    assert f'-D{workspace.SIGNING_KEY_OPTION}="{public}"' in command
    assert "-DMCUHOME_DETACHED_SIGNING=y" in command


def test_sign_applies_the_manifests_parameters(tmp_path, capsys, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    monkeypatch.setattr(workspace, "plan_build", _planner(tmp_path))
    monkeypatch.setattr(workspace, "run_build", _fake_build_run)
    main(
        [
            "build",
            str(EXAMPLE),
            "--build-dir",
            str(out_dir),
            "--native",
            "--no-sign",
            "--public-key",
            str(_public_key(tmp_path)),
        ]
    )
    capsys.readouterr()

    commands: list[list[str]] = []

    def fake_imgtool(command: list[str]) -> tuple[int, str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"signed")
        return 0, ""

    monkeypatch.setattr(imgtool, "_run", fake_imgtool)
    assert main(["sign", str(out_dir), "--signing-key", str(tmp_path / "signing.key")]) == 0

    out = capsys.readouterr().out
    assert "Signed the application image" in out
    assert len(commands) == 2  # one per artifact format
    for command in commands:
        assert command[command.index("--slot-size") + 1] == str(912 * 1024)
        assert command[command.index("--header-size") + 1] == "512"
        assert command[command.index("--align") + 1] == "4"

    document = json.loads((out_dir / MANIFEST_FILE).read_text("utf-8"))
    assert document["signing"]["signed"] is True
    assert document["signing"]["signed_by_the_build"] is False
    application = next(image for image in document["images"] if image["name"] == APP_DIR)
    assert any(entry["path"].endswith("zephyr.signed.bin") for entry in application["files"])


# --------------------------------------------------------------------------
# new, public-key, schema
# --------------------------------------------------------------------------


def test_new_scaffolds_a_device_and_names_the_next_step(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["new", "bench-node", "--board", "nrf7002dk/nrf5340/cpuapp"]) == 0
    out = capsys.readouterr().out
    assert "mcuhome init-pairing bench-node" in out
    assert (tmp_path / "devices" / "bench-node" / "main.yaml").is_file()


def test_new_refuses_an_existing_device(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "new",
                "bench-node",
                "--board",
                "nrf7002dk/nrf5340/cpuapp",
                "--config-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "new",
                "bench-node",
                "--board",
                "nrf7002dk/nrf5340/cpuapp",
                "--config-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert "already a device" in capsys.readouterr().err


def test_new_refuses_a_board_nobody_brought_up(tmp_path, capsys) -> None:
    assert main(["new", "bench-node", "--board", "nrf99dk", "--config-root", str(tmp_path)]) == 1
    assert "nrf7002dk/nrf5340/cpuapp" in capsys.readouterr().err


def test_public_key_writes_the_public_half(tmp_path, capsys, monkeypatch) -> None:
    key = tmp_path / "signing.key"
    key.write_text(signing.generate_key_pem(0x1234567890ABCDEF), "utf-8")
    monkeypatch.setenv(signing.KEY_VAR, str(key))

    assert main(["public-key"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("-----BEGIN PUBLIC KEY-----")
    assert signing.looks_like_p256_public_key(printed)

    assert main(["public-key", "-o", str(tmp_path / "signing.pub")]) == 0
    assert (tmp_path / "signing.pub").read_text("utf-8") == printed


def test_public_key_never_creates_a_key(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert main(["public-key"]) == 1
    assert "no such file" in capsys.readouterr().err


def test_schema_prints_the_configuration_schema(capsys) -> None:
    assert main(["schema"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["$id"].endswith("main.schema.json")
    assert "device" in document["properties"]


def test_schema_registry_prints_the_registry(tmp_path, capsys) -> None:
    assert main(["schema", "registry", "-o", str(tmp_path / "registry.json")]) == 0
    assert "registry" in capsys.readouterr().out
    document = json.loads((tmp_path / "registry.json").read_text("utf-8"))
    assert document["boards"][0]["name"] == "nrf7002dk/nrf5340/cpuapp"
