# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 5, the parts that decide things (``mcuhome/workspace.py``).

**No build runs here, ever.** Compiling a Matter node takes minutes and a
toolchain the test suite has no business requiring — the pytest half of
the strategy (builder-pipeline.md §9) is the fast half and stays that way.
What is tested is everything stage 5 decides before the compiler starts:
where the workspace is, what the command line says, which prerequisite is
missing, and what the build log meant. The subprocess itself is mocked in
the one test that needs it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mcuhome import workspace
from mcuhome.errors import BuildError

_GIB = 1024**3


def _fake_workspace(root: Path) -> Path:
    (root / ".west").mkdir(parents=True)
    (root / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    return root


# --------------------------------------------------------------------------
# Finding the workspace
# --------------------------------------------------------------------------


def test_the_topdir_is_found_from_anywhere_below_it(tmp_path) -> None:
    top = _fake_workspace(tmp_path / "ws")
    deep = top / "build" / "node" / "app" / "src"
    deep.mkdir(parents=True)
    assert workspace.find_topdir(deep) == top


def test_a_file_is_as_good_a_starting_point_as_its_directory(tmp_path) -> None:
    top = _fake_workspace(tmp_path / "ws")
    entry = top / "main.yaml"
    entry.write_text("", "utf-8")
    assert workspace.find_topdir(entry) == top


def test_the_first_candidate_wins(tmp_path) -> None:
    """The builder offers its own location first, the cwd second."""
    first = _fake_workspace(tmp_path / "one")
    second = _fake_workspace(tmp_path / "two")
    assert workspace.find_topdir(first, second) == first
    assert workspace.find_topdir(tmp_path / "nowhere", second) == second


def test_a_directory_that_is_not_a_workspace_is_not_one(tmp_path) -> None:
    assert workspace.find_topdir(tmp_path) is None


def test_the_builder_is_installed_in_a_workspace_of_its_own() -> None:
    """A precondition of every native build; wrong, and nothing compiles."""
    assert (workspace.MODULE_DIR / "west.yml").is_file()
    assert (workspace.PYSHIM_DIR / "python_path.py").is_file()


def test_no_workspace_is_refused_with_both_ways_out(tmp_path) -> None:
    with pytest.raises(BuildError) as caught:
        workspace.require_topdir(tmp_path)
    assert caught.value.message == "MCUHome cannot compile here: this is not a west workspace."
    assert "west init -l mcuhome" in (caught.value.hint or "")
    assert "--generate-only" in (caught.value.hint or "")


# --------------------------------------------------------------------------
# How many jobs to run in parallel
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpu_count", "available_gib", "expected"),
    [
        # This development machine: 4 cores, 15 GiB.
        (4, 15, 4),
        # A 24-thread/24-GiB WSL machine.
        (24, 24, 12),
        # Plenty of RAM, few cores: the CPU count is the ceiling.
        (2, 64, 2),
        # Plenty of cores, little RAM: the RAM budget is the ceiling.
        (16, 6, 3),
        # A single core: never ask for more than one job, however much RAM
        # the max(2, ...) floor would otherwise suggest.
        (1, 15, 1),
        # A RAM-starved multi-core machine still gets the floor of 2, not 0
        # or 1: max(2, ...) always wins over a floor-dividing-to-zero RAM
        # budget.
        (8, 1, 2),
    ],
)
def test_auto_jobs_boundary_cases(cpu_count: int, available_gib: int, expected: int) -> None:
    assert workspace.auto_jobs(cpu_count, available_gib * _GIB) == expected


def test_available_ram_reads_memavailable(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\nSwapTotal:             0 kB\n",
        "utf-8",
    )
    assert workspace.available_ram_bytes(meminfo) == 8192000 * 1024


def test_available_ram_falls_back_to_half_of_memtotal_without_memavailable(tmp_path) -> None:
    """An old kernel's /proc/meminfo has MemTotal but not MemAvailable."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\n", "utf-8")
    assert workspace.available_ram_bytes(meminfo) == (16384000 * 1024) // 2


def test_available_ram_is_zero_when_meminfo_has_neither_key(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("VmallocTotal:   34359738367 kB\n", "utf-8")
    assert workspace.available_ram_bytes(meminfo) == 0


def test_available_ram_is_zero_without_proc_meminfo_at_all(tmp_path) -> None:
    """Non-Linux, or any other reason the file just is not there."""
    assert workspace.available_ram_bytes(tmp_path / "does-not-exist") == 0


def test_detect_jobs_wires_cpu_count_and_available_ram_together(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(workspace, "available_ram_bytes", lambda: 12 * _GIB)
    assert workspace.detect_jobs() == 6


def test_detect_jobs_survives_an_unknown_cpu_count(monkeypatch) -> None:
    """`os.cpu_count()` returns None where the count is indeterminable."""
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(workspace, "available_ram_bytes", lambda: 64 * _GIB)
    assert workspace.detect_jobs() == 1


def test_a_command_line_flag_beats_everything(monkeypatch) -> None:
    monkeypatch.setenv(workspace.JOBS_VAR, "6")
    resolved = workspace.resolve_jobs(cli_jobs=3)
    assert (resolved.value, resolved.source) == (3, "flag")


def test_the_environment_variable_beats_auto_detection(monkeypatch) -> None:
    monkeypatch.setattr(workspace, "detect_jobs", lambda: pytest.fail("auto-detection ran"))
    resolved = workspace.resolve_jobs(cli_jobs=None, env={workspace.JOBS_VAR: "6"})
    assert (resolved.value, resolved.source) == (6, "env")


def test_neither_flag_nor_environment_falls_back_to_auto_detection(monkeypatch) -> None:
    monkeypatch.setattr(workspace, "detect_jobs", lambda: 5)
    resolved = workspace.resolve_jobs(cli_jobs=None, env={})
    assert (resolved.value, resolved.source) == (5, "auto")


def test_a_nonsense_environment_value_is_treated_as_unset(monkeypatch) -> None:
    """A typo in a shell rc file falls back to auto rather than breaking every build."""
    monkeypatch.setattr(workspace, "detect_jobs", lambda: 5)
    resolved = workspace.resolve_jobs(cli_jobs=None, env={workspace.JOBS_VAR: "not-a-number"})
    assert (resolved.value, resolved.source) == (5, "auto")


def test_a_zero_environment_value_is_also_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setattr(workspace, "detect_jobs", lambda: 5)
    resolved = workspace.resolve_jobs(cli_jobs=None, env={workspace.JOBS_VAR: "0"})
    assert (resolved.value, resolved.source) == (5, "auto")


# --------------------------------------------------------------------------
# The environment
# --------------------------------------------------------------------------


def test_the_codegen_shim_goes_on_pythonpath_in_front() -> None:
    env = workspace.build_environment({"PYTHONPATH": "/somewhere/else"}, jobs=2)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(workspace.PYSHIM_DIR), "/somewhere/else"]


def test_pythonpath_is_set_even_when_there_was_none() -> None:
    assert workspace.build_environment({}, jobs=2)["PYTHONPATH"] == str(workspace.PYSHIM_DIR)


def test_running_the_builder_inside_its_own_environment_does_not_grow_pythonpath() -> None:
    once = workspace.build_environment({}, jobs=2)
    assert workspace.build_environment(once, jobs=2)["PYTHONPATH"] == once["PYTHONPATH"]


def test_the_callers_environment_is_not_modified() -> None:
    original = {"PYTHONPATH": "/x"}
    workspace.build_environment(original, jobs=2)
    assert original == {"PYTHONPATH": "/x"}


def test_the_chip_gn_sub_build_gets_the_same_job_cap() -> None:
    """The vendored CHIP GN sub-build otherwise ignores `-o=-j{jobs}` entirely.

    (patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch,
    config/common/cmake/chip_gn.cmake.)
    """
    assert workspace.build_environment({}, jobs=2)[workspace.CHIP_JOBS_VAR] == "2"


def test_an_explicit_jobs_value_reaches_the_chip_gn_sub_build_too() -> None:
    assert workspace.build_environment({}, jobs=4)[workspace.CHIP_JOBS_VAR] == "4"


# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------


def _tool_dir(tmp_path: Path, *names: str) -> str:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in names:
        executable = directory / name
        executable.write_text("#!/bin/sh\n", "utf-8")
        executable.chmod(0o755)
    return str(directory)


def test_every_missing_tool_is_named_at_once(tmp_path) -> None:
    env = {"PATH": _tool_dir(tmp_path)}
    assert [tool.name for tool in workspace.missing_tools(env)] == ["west", "gn", "zap"]
    with pytest.raises(BuildError) as caught:
        workspace.require_tools(env)
    assert caught.value.message == (
        "MCUHome cannot compile without west, gn, zap, which are not on your PATH."
    )
    hint = caught.value.hint or ""
    for tool in workspace.TOOLS:
        assert tool.why in hint
        assert tool.source in hint
    assert "--generate-only" in hint


def test_a_single_missing_tool_reads_as_one_thing(tmp_path) -> None:
    env = {"PATH": _tool_dir(tmp_path, "west", "zap")}
    with pytest.raises(BuildError) as caught:
        workspace.require_tools(env)
    assert caught.value.message == ("MCUHome cannot compile without gn, which is not on your PATH.")


def test_zap_is_satisfied_by_any_of_its_spellings(tmp_path) -> None:
    assert not workspace.missing_tools({"PATH": _tool_dir(tmp_path, "west", "gn", "zap-cli")})


def test_zap_is_satisfied_by_its_install_variable_instead(tmp_path) -> None:
    env = {"PATH": _tool_dir(tmp_path, "west", "gn"), "ZAP_INSTALL_PATH": "/opt/zap"}
    assert workspace.missing_tools(env) == []


def test_an_empty_install_variable_does_not_count(tmp_path) -> None:
    env = {"PATH": _tool_dir(tmp_path, "west", "gn"), "ZAP_INSTALL_PATH": ""}
    assert [tool.name for tool in workspace.missing_tools(env)] == ["zap"]


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def test_the_command_says_board_directories_snippets_and_parallelism() -> None:
    command = workspace.west_build_command(
        app_dir=Path("/w/build/node/app"),
        build_dir=Path("/w/build/node/build"),
        board="nrf7002dk/nrf5340/cpuapp",
        snippets=("matter", "debug-rtt"),
        bootloader_snippets=("boot-mode",),
        signing_key=Path("/home/someone/.config/mcuhome/signing.key"),
        jobs=2,
    )
    assert command == [
        "west",
        "build",
        "--board",
        "nrf7002dk/nrf5340/cpuapp",
        "--build-dir",
        "/w/build/node/build",
        "--pristine",
        "auto",
        "--sysbuild",
        "-o=-j2",
        "/w/build/node/app",
        "--",
        "-Dapp_SNIPPET=matter;debug-rtt",
        "-Dmcuboot_SNIPPET=boot-mode",
        '-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="/home/someone/.config/mcuhome/signing.key"',
    ]


def test_snippets_are_named_per_image_and_never_globally() -> None:
    """A bare SNIPPET reaches every image, and -S matter kills MCUboot.

    Sysbuild falls back to the global name for an image that has none of
    its own, so `-S matter` would put CHIP's heap sizing — and symbols
    MCUboot has never heard of — into the bootloader's Kconfig, where an
    assignment to an undefined symbol stops the build.
    """
    command = workspace.west_build_command(
        app_dir=Path("/w/build/node/app"),
        build_dir=Path("/w/build/node/build"),
        board="x",
        snippets=("matter",),
        bootloader_snippets=("boot-mode",),
        jobs=2,
    )
    assert "--snippet" not in command
    assert "-DSNIPPET=matter" not in command
    assert "-Dapp_SNIPPET=matter" in command
    assert "-Dmcuboot_SNIPPET=boot-mode" in command


def test_the_image_the_snippets_are_named_for_is_the_application_directory() -> None:
    """Sysbuild names the main image after the directory it is built from."""
    command = workspace.west_build_command(
        app_dir=Path("/w/somewhere/firmware"),
        build_dir=Path("/w/b"),
        board="x",
        snippets=("matter",),
        jobs=2,
    )
    assert "-Dfirmware_SNIPPET=matter" in command


def test_no_signing_key_means_no_signing_option_at_all() -> None:
    """Absence is visible rather than an empty value MCUboot would resolve."""
    command = workspace.west_build_command(
        app_dir=Path("app"), build_dir=Path("b"), board="x", jobs=2
    )
    assert not any(item.startswith(f"-D{workspace.SIGNING_KEY_OPTION}") for item in command)
    assert "--" not in command


def test_a_fresh_build_directory_builds_incrementally(tmp_path) -> None:
    assert workspace.pristine_mode(tmp_path) == "auto"


def test_a_sysbuild_directory_builds_incrementally(tmp_path) -> None:
    (tmp_path / "CMakeCache.txt").write_text("", "utf-8")
    (tmp_path / "domains.yaml").write_text("default: app\n", "utf-8")
    assert workspace.pristine_mode(tmp_path) == "auto"


def test_a_build_directory_from_before_sysbuild_is_rebuilt_from_scratch(tmp_path) -> None:
    """The one case --pristine=auto cannot see: CMake would refuse it."""
    (tmp_path / "CMakeCache.txt").write_text("", "utf-8")
    assert workspace.pristine_mode(tmp_path) == "always"


def test_parallelism_is_attached_with_an_equals_sign() -> None:
    """`-o -j4` would be read as two options; `-o=-j4` is one."""
    command = workspace.west_build_command(
        app_dir=Path("app"), build_dir=Path("b"), board="x", jobs=4
    )
    assert "-o=-j4" in command
    assert "-j4" not in [item for item in command if not item.startswith("-o=")]


def test_the_command_uses_whatever_job_count_it_is_given() -> None:
    """The resolved value flows straight through, not a machine constant."""
    command = workspace.west_build_command(
        app_dir=Path("app"), build_dir=Path("b"), board="x", jobs=12
    )
    assert "-o=-j12" in command


def test_a_device_without_snippets_gets_none() -> None:
    command = workspace.west_build_command(
        app_dir=Path("app"), build_dir=Path("b"), board="x", jobs=2
    )
    assert not any("SNIPPET" in item for item in command)


def test_the_plan_puts_the_build_tree_next_to_the_application(tmp_path, monkeypatch) -> None:
    top = _fake_workspace(tmp_path / "ws")
    monkeypatch.setattr(workspace, "MODULE_DIR", top / "mcuhome")
    monkeypatch.setattr(workspace, "require_tools", lambda env: None)
    out_dir = top / "build" / "node"

    plan = workspace.plan_build(out_dir=out_dir, app_subdir="app", board="x", env={}, jobs=2)

    assert plan.topdir == top
    assert plan.app_dir == out_dir / "app"
    assert plan.build_dir == out_dir / "build"
    assert plan.env["PYTHONPATH"] == str(workspace.PYSHIM_DIR)
    assert plan.env[workspace.CHIP_JOBS_VAR] == "2"


def test_zephyr_base_is_filled_in_because_west_does_not_export_it(tmp_path, monkeypatch) -> None:
    top = _fake_workspace(tmp_path / "ws")
    (top / "zephyr").mkdir()
    monkeypatch.setattr(workspace, "MODULE_DIR", top / "mcuhome")
    monkeypatch.setattr(workspace, "require_tools", lambda env: None)

    plan = workspace.plan_build(out_dir=top / "out", app_subdir="app", board="x", env={}, jobs=2)
    assert plan.env["ZEPHYR_BASE"] == str(top / "zephyr")


def test_a_zephyr_base_someone_set_on_purpose_is_left_alone(tmp_path, monkeypatch) -> None:
    """West follows it too; disagreeing with the tool we drive helps nobody."""
    top = _fake_workspace(tmp_path / "ws")
    (top / "zephyr").mkdir()
    monkeypatch.setattr(workspace, "MODULE_DIR", top / "mcuhome")
    monkeypatch.setattr(workspace, "require_tools", lambda env: None)

    plan = workspace.plan_build(
        out_dir=top / "out",
        app_subdir="app",
        board="x",
        env={"ZEPHYR_BASE": "/elsewhere/zephyr"},
        jobs=2,
    )
    assert plan.env["ZEPHYR_BASE"] == "/elsewhere/zephyr"


def test_the_plan_refuses_before_it_decides_anything_else(tmp_path, monkeypatch) -> None:
    """A missing prerequisite is reported, not discovered by the compiler."""
    monkeypatch.setattr(workspace, "MODULE_DIR", _fake_workspace(tmp_path / "ws"))
    with pytest.raises(BuildError) as caught:
        workspace.plan_build(
            out_dir=tmp_path / "out", app_subdir="app", board="x", env={"PATH": ""}, jobs=2
        )
    assert "cannot compile without" in caught.value.message


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def test_the_build_log_is_echoed_and_captured(tmp_path, monkeypatch) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.text = ""

        def write(self, chunk: str) -> None:
            self.text += chunk

        def flush(self) -> None:
            pass

    plan = workspace.BuildPlan(
        topdir=tmp_path,
        app_dir=tmp_path / "app",
        build_dir=tmp_path / "build",
        command=["true"],
        env={},
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(["one\n", "two\n"], 0),
    )
    stream = FakeStream()
    code, log = workspace.run_build(plan, stream=stream)
    assert (code, log) == (0, "one\ntwo\n")
    assert stream.text == log


def test_a_command_that_cannot_start_is_not_a_traceback(tmp_path) -> None:
    plan = workspace.BuildPlan(
        topdir=tmp_path,
        app_dir=tmp_path / "app",
        build_dir=tmp_path / "build",
        command=[str(tmp_path / "not-a-program")],
        env={},
    )
    with pytest.raises(BuildError) as caught:
        workspace.run_build(plan)
    assert caught.value.message.startswith("MCUHome could not start the build:")


class _FakeProcess:
    def __init__(self, lines: list[str], code: int) -> None:
        self.stdout = iter(lines)
        self._code = code

    def wait(self) -> int:
        return self._code


# --------------------------------------------------------------------------
# What came out
# --------------------------------------------------------------------------

_LOG = """\
[880/880] Linking C executable zephyr/zephyr.elf
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB      81.99%
             RAM:      196296 B       448 KB      42.79%
        IDT_LIST:          0 GB        32 KB       0.00%
"""


def test_the_memory_report_is_read_out_of_the_build_log() -> None:
    regions = workspace.parse_memory_report(_LOG)
    assert [region.name for region in regions] == ["FLASH", "RAM", "IDT_LIST"]
    flash, ram, _ = regions
    assert (flash.used, flash.total, flash.percent) == (859672, 1024 * 1024, 81.99)
    assert (ram.used, ram.total, ram.percent) == (196296, 448 * 1024, 42.79)
    assert flash.describe() == "FLASH 839.5 KiB of 1024.0 KiB (82.0%)"


def test_a_log_without_a_link_reports_nothing_rather_than_failing() -> None:
    assert workspace.parse_memory_report("ninja: no work to do.\n") == []


def test_only_the_images_that_exist_are_reported(tmp_path) -> None:
    output = tmp_path / "zephyr"
    output.mkdir()
    (output / "zephyr.hex").write_text("", "utf-8")
    (output / "zephyr.elf").write_text("", "utf-8")
    (output / "zephyr.signed.bin").write_text("", "utf-8")
    assert [path.name for path in workspace.artifacts(tmp_path)] == [
        "zephyr.elf",
        "zephyr.hex",
        "zephyr.signed.bin",
    ]


def test_an_empty_build_directory_has_no_images(tmp_path) -> None:
    assert workspace.artifacts(tmp_path) == []


# --------------------------------------------------------------------------
# Two images, not one (ADR 0015)
# --------------------------------------------------------------------------


def _image(build_dir: Path, name: str, *, flash: int) -> None:
    output = build_dir / name / "zephyr"
    output.mkdir(parents=True)
    (output / "zephyr.elf").write_text("", "utf-8")
    (output / "zephyr.bin").write_bytes(b"\0" * flash)


def test_both_images_are_reported_bootloader_first(tmp_path) -> None:
    _image(tmp_path, "app", flash=2048)
    _image(tmp_path, "mcuboot", flash=1024)

    images = workspace.build_images(tmp_path, app_image="app")
    assert [(image.name, image.role, image.flash_bytes) for image in images] == [
        ("mcuboot", "bootloader", 1024),
        ("app", "application", 2048),
    ]
    assert images[0].describe() == "mcuboot (bootloader)  1.0 KiB"


def test_an_image_that_produced_nothing_is_simply_absent(tmp_path) -> None:
    _image(tmp_path, "app", flash=16)
    assert [image.name for image in workspace.build_images(tmp_path, app_image="app")] == ["app"]


def test_the_merged_image_is_reported_only_when_sysbuild_wrote_one(tmp_path) -> None:
    """Named after the board target, so it is found by pattern."""
    assert workspace.merged_image(tmp_path) is None
    merged = tmp_path / "merged_nrf7002dk_nrf5340_cpuapp.hex"
    merged.write_text("", "utf-8")
    assert workspace.merged_image(tmp_path) == merged


def test_more_than_one_merged_image_is_reported_as_none(tmp_path) -> None:
    """One board target per device; two means an upstream change to look at."""
    (tmp_path / "merged_one.hex").write_text("", "utf-8")
    (tmp_path / "merged_two.hex").write_text("", "utf-8")
    assert workspace.merged_image(tmp_path) is None


_SYSBUILD_LOG = """\
[1/2] Performing build step for 'mcuboot'
Memory region         Used Size  Region Size  %age Used
           FLASH:       57344 B        64 KB      87.50%
[2/2] Performing build step for 'app'
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB      81.99%
             RAM:      196296 B       448 KB      42.79%
"""


def test_each_images_footprint_is_attributed_to_it() -> None:
    """Zephyr's report names no image; the build-step banner before it does."""
    by_image = workspace.parse_image_memory_report(_SYSBUILD_LOG, images=["mcuboot", "app"])
    assert sorted(by_image) == ["app", "mcuboot"]
    assert [region.name for region in by_image["mcuboot"]] == ["FLASH"]
    assert by_image["mcuboot"][0].used == 57344
    assert [region.name for region in by_image["app"]] == ["FLASH", "RAM"]


def test_a_report_before_any_image_banner_belongs_to_no_image() -> None:
    assert workspace.parse_image_memory_report(_LOG, images=["app"]) == {}


#: The Matter build is an ExternalProject of its own inside the
#: application image, and it prints the same banner sysbuild does.
_NESTED_LOG = """\
[9/16] Performing build step for 'app'
[334/880] Performing build step for 'chip-gn'
[1/321] c++ obj/src/lib/core/error.CHIPError.cpp.o
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB      81.99%
"""


def test_a_nested_external_project_is_not_mistaken_for_an_image() -> None:
    """Otherwise chip-gn is credited with the application's footprint."""
    by_image = workspace.parse_image_memory_report(_NESTED_LOG, images=["mcuboot", "app"])
    assert sorted(by_image) == ["app"]
    assert by_image["app"][0].used == 859672


def test_the_signing_key_is_passed_as_a_quoted_kconfig_string() -> None:
    """An unquoted string assignment is a fatal Kconfig warning.

    And the consequence of getting it wrong is not a build that stops: it
    is "Assignment ignored", after which MCUboot falls back to its own
    default, which is the published demo key.
    """
    command = workspace.west_build_command(
        app_dir=Path("app"),
        build_dir=Path("b"),
        board="x",
        signing_key=Path("/home/someone/keys/signing.key"),
        jobs=2,
    )
    assert '-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="/home/someone/keys/signing.key"' in command
