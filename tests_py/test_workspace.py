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
# The environment
# --------------------------------------------------------------------------


def test_the_codegen_shim_goes_on_pythonpath_in_front() -> None:
    env = workspace.build_environment({"PYTHONPATH": "/somewhere/else"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(workspace.PYSHIM_DIR), "/somewhere/else"]


def test_pythonpath_is_set_even_when_there_was_none() -> None:
    assert workspace.build_environment({})["PYTHONPATH"] == str(workspace.PYSHIM_DIR)


def test_running_the_builder_inside_its_own_environment_does_not_grow_pythonpath() -> None:
    once = workspace.build_environment({})
    assert workspace.build_environment(once)["PYTHONPATH"] == once["PYTHONPATH"]


def test_the_callers_environment_is_not_modified() -> None:
    original = {"PYTHONPATH": "/x"}
    workspace.build_environment(original)
    assert original == {"PYTHONPATH": "/x"}


def test_the_chip_gn_sub_build_gets_the_same_job_cap_by_default() -> None:
    """The vendored CHIP GN sub-build otherwise ignores `-o=-j{JOBS}` entirely.

    (patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch,
    config/common/cmake/chip_gn.cmake.)
    """
    assert workspace.build_environment({})[workspace.CHIP_JOBS_VAR] == str(workspace.JOBS)


def test_an_explicit_jobs_override_reaches_the_chip_gn_sub_build_too() -> None:
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
        "--snippet",
        "matter",
        "--snippet",
        "debug-rtt",
        "-o=-j2",
        "/w/build/node/app",
    ]


def test_parallelism_is_capped_and_attached_with_an_equals_sign() -> None:
    """`-o -j2` would be read as two options; `-o=-j2` is one.

    The cap itself is a machine limit, not a preference: Matter's compile
    units peak above a gigabyte each, so -j$(nproc) is how a four-core
    laptop OOMs instead of building.
    """
    command = workspace.west_build_command(
        app_dir=Path("app"), build_dir=Path("b"), board="x", jobs=workspace.JOBS
    )
    assert workspace.JOBS == 2
    assert "-o=-j2" in command
    assert "-j2" not in [item for item in command if not item.startswith("-o=")]


def test_a_device_without_snippets_gets_none() -> None:
    command = workspace.west_build_command(app_dir=Path("app"), build_dir=Path("b"), board="x")
    assert "--snippet" not in command


def test_the_plan_puts_the_build_tree_next_to_the_application(tmp_path, monkeypatch) -> None:
    top = _fake_workspace(tmp_path / "ws")
    monkeypatch.setattr(workspace, "MODULE_DIR", top / "mcuhome")
    monkeypatch.setattr(workspace, "require_tools", lambda env: None)
    out_dir = top / "build" / "node"

    plan = workspace.plan_build(out_dir=out_dir, app_subdir="app", board="x", env={})

    assert plan.topdir == top
    assert plan.app_dir == out_dir / "app"
    assert plan.build_dir == out_dir / "build"
    assert plan.env["PYTHONPATH"] == str(workspace.PYSHIM_DIR)
    assert plan.env[workspace.CHIP_JOBS_VAR] == str(workspace.JOBS)


def test_zephyr_base_is_filled_in_because_west_does_not_export_it(tmp_path, monkeypatch) -> None:
    top = _fake_workspace(tmp_path / "ws")
    (top / "zephyr").mkdir()
    monkeypatch.setattr(workspace, "MODULE_DIR", top / "mcuhome")
    monkeypatch.setattr(workspace, "require_tools", lambda env: None)

    plan = workspace.plan_build(out_dir=top / "out", app_subdir="app", board="x", env={})
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
    )
    assert plan.env["ZEPHYR_BASE"] == "/elsewhere/zephyr"


def test_the_plan_refuses_before_it_decides_anything_else(tmp_path, monkeypatch) -> None:
    """A missing prerequisite is reported, not discovered by the compiler."""
    monkeypatch.setattr(workspace, "MODULE_DIR", _fake_workspace(tmp_path / "ws"))
    with pytest.raises(BuildError) as caught:
        workspace.plan_build(
            out_dir=tmp_path / "out", app_subdir="app", board="x", env={"PATH": ""}
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
    assert [path.name for path in workspace.artifacts(tmp_path)] == ["zephyr.elf", "zephyr.hex"]


def test_an_empty_build_directory_has_no_images(tmp_path) -> None:
    assert workspace.artifacts(tmp_path) == []
