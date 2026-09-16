# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The host check: what it examines, what it never examines, and its words.

Two properties carry this module. The first is the **mode split**: a
container build and a subprocess build need disjoint things of a host,
so a machine that never runs a container is never told to install one.
The doubles here are recording ones for exactly that reason — the
assertion "nothing asked the container runtime anything" is only worth
something if the runtime would have recorded it.

The second is that **nothing here raises**. Every probe can fail, and a
failed probe is a finding with the words a person needs; a host check
that threw would be the refusal it exists to replace.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcuhome.model.errors import BuildError

from mcuhome.workbench import hostcheck
from mcuhome.workbench.api import (
    HOST_CHECKS,
    PROJECT_MARKER_FILE,
    PROJECT_VERSION,
    BuildOptions,
    HostCheckResult,
    HostFinding,
    Project,
    ProjectFile,
    check_build_host,
    read_cache_usage,
    read_project,
    resolve_settings,
)
from mcuhome.workbench.buildprocess import Completed
from mcuhome.workbench.imgtool import find_imgtool
from mcuhome.workbench.projectfile import new_project_id, write_project_file


class RecordingRuntime:
    """A container runtime double that answers, and remembers being asked.

    It stands in for :class:`~mcuhome.workbench.containerbuild.ContainerRuntime`
    at the point the host check builds one, so a mode that must not touch
    a runtime can be checked by what this recorded: nothing.
    """

    #: Every runtime this double was asked to build, in order.
    made: list[RecordingRuntime] = []

    def __init__(self, program: str = "docker", *, status: int | None = 0) -> None:
        self.program = program
        self.status = status
        self.argvs: list[list[str]] = []
        RecordingRuntime.made.append(self)

    def run(self, argv: Sequence[str], on_line: Any = None) -> Completed:
        del on_line
        self.argvs.append(list(argv))
        return Completed(status=self.status, output="")


class RecordingRegistry:
    """A container registry double, with what each repository answers."""

    #: Every registry client the host check built, in order.
    made: list[RecordingRegistry] = []

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        self.answers = dict(answers or {})
        self.asked: list[str] = []
        RecordingRegistry.made.append(self)

    def tags(self, reference: Any) -> tuple[str, ...]:
        self.asked.append(reference.repository)
        answer = self.answers.get(reference.repository, ())
        if isinstance(answer, Exception):
            raise answer
        return tuple(answer)


@pytest.fixture(autouse=True)
def _recording_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both impure seams replaced by recorders, for every test here.

    Installed for *every* test, including the ones that assert nothing
    was asked — a test that only installed the double where it expects a
    call could not tell "not asked" from "not installed".
    """
    RecordingRuntime.made = []
    RecordingRegistry.made = []
    monkeypatch.setattr(hostcheck, "ContainerRuntime", RecordingRuntime)
    monkeypatch.setattr(
        hostcheck,
        "ImageRegistry",
        lambda: RecordingRegistry({"ghcr.io/mcu-home/build-environment": ("0.1.0-r1",)}),
    )
    monkeypatch.setattr(hostcheck, "_run_program", lambda argv: "3 13 1")
    monkeypatch.setattr(hostcheck, "find_imgtool", lambda *, env, stated=None: ["imgtool"])


def _options(**values: Any) -> BuildOptions:
    """Build options with the container repository the fixtures answer for."""
    return replace(
        BuildOptions(container_repositories=("ghcr.io/mcu-home/build-environment",)), **values
    )


def _env(tmp_path: Path) -> dict[str, str]:
    """An environment with a home of its own, so no probe reads this one."""
    return {"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}


def _checks(result: HostCheckResult) -> list[str]:
    return [finding.check for finding in result.findings]


def _finding(result: HostCheckResult, check: str) -> HostFinding:
    return next(finding for finding in result.findings if finding.check == check)


# --------------------------------------------------------------------------
# The mode split
# --------------------------------------------------------------------------


def test_the_container_mode_examines_the_runtime_and_the_image_search(tmp_path: Path) -> None:
    """What a container build needs, and only that."""
    result = check_build_host(options=_options(mode="container"), env=_env(tmp_path))

    assert _checks(result) == ["project", "runtime", "image", "imgtool", "cache"]
    assert result.ok
    assert RecordingRuntime.made[0].argvs, "the runtime was never asked anything"
    assert RecordingRegistry.made[0].asked == ["ghcr.io/mcu-home/build-environment"]


def test_the_subprocess_mode_asks_a_container_runtime_nothing(tmp_path: Path) -> None:
    """The defect this module exists for: no container talk without a container.

    The doubles are installed and record every call, so this asserts
    that nothing reached them — not that nothing could have.
    """
    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    assert _checks(result) == ["project", "store", "python", "imgtool", "cache"]
    assert RecordingRuntime.made == []
    assert RecordingRegistry.made == []


def _a_workspace(tmp_path: Path) -> Path:
    """A directory west would recognise, with its manifest repository."""
    workspace = tmp_path / "zephyrproject"
    (workspace / ".west").mkdir(parents=True)
    (workspace / ".west" / "config").write_text("[manifest]\npath = mcuhome-sdk\n")
    (workspace / "mcuhome-sdk").mkdir()
    return workspace


def test_a_development_workspace_replaces_the_store_and_the_interpreter(
    tmp_path: Path,
) -> None:
    """A development build compiles a workspace and provisions nothing."""
    workspace = _a_workspace(tmp_path)

    result = check_build_host(
        options=_options(mode="subprocess", dev_workspace=workspace), env=_env(tmp_path)
    )

    assert _checks(result) == ["project", "workspace", "python", "imgtool", "cache"]
    assert result.ok
    assert str(workspace) in _finding(result, "workspace").detail


def test_every_check_it_reports_is_one_the_surface_publishes(tmp_path: Path) -> None:
    """A value a client switches on, from the tuple it can look up."""
    both = [
        *check_build_host(options=_options(mode="container"), env=_env(tmp_path)).findings,
        *check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path)).findings,
    ]
    for finding in both:
        assert finding.check in HOST_CHECKS


# --------------------------------------------------------------------------
# It reports instead of raising
# --------------------------------------------------------------------------


def test_a_runtime_that_is_not_there_is_a_finding_in_the_build_s_own_words(
    tmp_path: Path,
) -> None:
    """The refusal a build would have raised, read out as data.

    Two people with the same problem — one who ran the check, one whose
    build stopped — have to be told the same thing, so the finding
    carries the refusal's message and its fix rather than a second
    wording of them.
    """
    RecordingRuntime.made = []
    monkeypatched = RecordingRuntime

    def absent(program: str = "docker", **_: Any) -> RecordingRuntime:
        return monkeypatched(program, status=None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hostcheck, "ContainerRuntime", absent)
        result = check_build_host(options=_options(mode="container"), env=_env(tmp_path))

    finding = _finding(result, "runtime")
    assert not finding.ok
    assert not result.ok
    assert "docker" in finding.detail
    assert "install Docker" in finding.hint
    # The message alone in `detail`: `str()` of a refusal is the rendered
    # three-line form, which would carry the fix twice.
    assert "Fix:" not in finding.detail
    assert finding.hint not in finding.detail


def test_a_host_without_a_home_directory_is_answered_rather_than_refused(
    tmp_path: Path,
) -> None:
    """Every per-user path fails at once here, and none of them raises."""
    result = check_build_host(options=_options(mode="subprocess"), env={"PATH": ""})

    assert not result.ok
    assert not _finding(result, "store").ok
    assert "HOME" in _finding(result, "store").detail
    # A machine with nowhere to put a cache builds without one, which is
    # slow and not broken.
    assert _finding(result, "cache").ok


# --------------------------------------------------------------------------
# The container repositories
# --------------------------------------------------------------------------


def test_a_repository_that_cannot_be_asked_is_named_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repository answered: that is a finding about this machine."""
    monkeypatch.setattr(
        hostcheck,
        "ImageRegistry",
        lambda: RecordingRegistry({"ghcr.io/mcu-home/build-environment": BuildError("no route")}),
    )

    result = check_build_host(options=_options(mode="container"), env=_env(tmp_path))

    finding = _finding(result, "image")
    assert not finding.ok
    assert finding.detail.startswith("ghcr.io/mcu-home/build-environment could not be asked (")
    assert "no route" in finding.detail
    assert "network" in finding.hint


def test_one_repository_answering_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search list exists to have alternatives in it."""
    monkeypatch.setattr(
        hostcheck,
        "ImageRegistry",
        lambda: RecordingRegistry(
            {
                "ghcr.io/mcu-home/build-environment": BuildError("no route"),
                "registry.example/mirror": ("0.1.0-r1", "0.1.0-r2"),
            }
        ),
    )

    result = check_build_host(
        options=_options(
            mode="container",
            container_repositories=(
                "ghcr.io/mcu-home/build-environment",
                "registry.example/mirror",
            ),
        ),
        env=_env(tmp_path),
    )

    finding = _finding(result, "image")
    assert finding.ok
    assert "registry.example/mirror publishes 2 image(s)" in finding.detail
    assert "could not be asked" in finding.detail, "the miss is still named"


def test_a_repository_that_is_not_a_repository_name_reads_as_one(tmp_path: Path) -> None:
    """A value that is not an address was never a question about the network.

    A build refuses on it before it reaches a registry, so the finding
    says what is wrong with the configuration rather than reporting a
    repository that "could not be asked" — and it says so even though
    another repository answered, because that entry breaks every
    container build on this machine.
    """
    result = check_build_host(
        options=_options(
            mode="container",
            container_repositories=("ghcr.io/mcu-home/build-environment", "NOT A REPOSITORY"),
        ),
        env=_env(tmp_path),
    )

    finding = _finding(result, "image")
    assert not finding.ok
    assert "NOT A REPOSITORY is not a repository name" in finding.detail
    assert "could not be asked" not in finding.detail
    assert "Fix:" not in finding.detail, "the message alone, like every other finding"
    assert "build.container_repositories" in finding.hint
    assert "publishes 1 image(s)" in finding.detail, "what did answer is still named"


def test_a_machine_allowed_no_repository_at_all_is_told_so(tmp_path: Path) -> None:
    """An empty allowlist is a configuration answer, not a network one."""
    result = check_build_host(
        options=_options(mode="container", container_repositories=()), env=_env(tmp_path)
    )

    finding = _finding(result, "image")
    assert not finding.ok
    assert "build.container_repositories" in finding.hint
    assert RecordingRegistry.made == [], "nothing was asked, because nothing may be"


# --------------------------------------------------------------------------
# The store, the interpreter, the tool and the cache
# --------------------------------------------------------------------------


def test_a_store_that_cannot_be_created_is_not_ok(tmp_path: Path) -> None:
    """The directory a subprocess build unpacks its environment into."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")

    result = check_build_host(
        options=_options(mode="subprocess", env_store=blocked / "store"), env=_env(tmp_path)
    )

    finding = _finding(result, "store")
    assert not finding.ok
    assert str(blocked) in finding.detail
    assert "build.env_store" in finding.hint


def test_the_store_says_how_many_environments_are_in_it(tmp_path: Path) -> None:
    """What is already provisioned, because that is what a person asks."""
    store = tmp_path / "store"
    entry = store / "mcuhome-build-tools-0.1.0"
    entry.mkdir(parents=True)
    (entry / ".mcuhome-provisioned").write_text(
        '{"kind": "tools", "package": "mcuhome-build-tools", '
        '"version": "0.1.0", "sha256": "a", "provisioned": "now"}'
    )

    result = check_build_host(
        options=_options(mode="subprocess", env_store=store), env=_env(tmp_path)
    )

    assert _finding(result, "store").ok
    assert "1 build environment(s) provisioned" in _finding(result, "store").detail


def test_an_interpreter_without_venv_says_which_package_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary broken host: a system Python without ``python3-venv``."""
    monkeypatch.setattr(hostcheck, "_run_program", lambda argv: "3 13 0")

    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    finding = _finding(result, "python")
    assert not finding.ok
    assert "no venv module" in finding.detail
    assert "python3-venv" in finding.hint


def test_an_interpreter_that_is_not_on_the_path_is_named(tmp_path: Path) -> None:
    """``build.python`` is a program name, looked up on the stated PATH."""
    result = check_build_host(
        options=_options(mode="subprocess", python="python4.0"), env=_env(tmp_path)
    )

    finding = _finding(result, "python")
    assert not finding.ok
    assert "python4.0" in finding.detail
    assert "build.python" in finding.hint


def test_an_interpreter_that_answers_nonsense_is_not_taken_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program that is not a Python, and a program that did not run."""
    monkeypatch.setattr(hostcheck, "_run_program", lambda argv: "")

    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    assert not _finding(result, "python").ok
    assert "did not answer as a Python interpreter" in _finding(result, "python").detail


def test_the_signing_tool_is_the_one_the_caller_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``signing.imgtool`` reaches the check the way it reaches a signing run."""
    asked: list[str | None] = []

    def find(*, env: Mapping[str, str], stated: str | None = None) -> list[str] | None:
        del env
        asked.append(stated)
        return None

    monkeypatch.setattr(hostcheck, "find_imgtool", find)

    result = check_build_host(
        options=_options(mode="container"), env=_env(tmp_path), imgtool="/opt/imgtool"
    )

    assert asked == ["/opt/imgtool"]
    finding = _finding(result, "imgtool")
    assert not finding.ok
    assert "signing.imgtool" in finding.hint


def test_a_host_with_nowhere_to_cache_is_still_a_host_that_builds(
    tmp_path: Path,
) -> None:
    """No cache is slow, not broken — so the finding is ok and says why."""
    result = check_build_host(options=_options(mode="container"), env={"PATH": ""})

    finding = _finding(result, "cache")
    assert finding.ok
    assert "compiles from scratch" in finding.detail
    assert "build.cache_root" in finding.hint


def test_a_cache_root_that_cannot_be_written_is_not_ok(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")

    result = check_build_host(
        options=_options(mode="container", cache_root=blocked / "cache"), env=_env(tmp_path)
    )

    finding = _finding(result, "cache")
    assert not finding.ok
    assert "build.cache_root" in finding.hint


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


def test_the_verdict_cannot_disagree_with_the_findings() -> None:
    """``ok`` is derived, so no caller can build a result that lies."""
    assert HostCheckResult().ok
    assert HostCheckResult(findings=(HostFinding(check="python", ok=True, detail="here"),)).ok
    assert not HostCheckResult(
        findings=(
            HostFinding(check="python", ok=True, detail="here"),
            HostFinding(check="cache", ok=False, detail="no"),
        )
    ).ok


def test_a_path_inside_the_project_is_reported_relative_to_it(tmp_path: Path) -> None:
    """What *project* is for: a person reads a path they recognise."""
    store = tmp_path / "project" / ".mcuhome-store"
    store.mkdir(parents=True)
    project = Project(root=tmp_path / "project", discovered=True)

    result = check_build_host(
        options=_options(mode="subprocess", env_store=store),
        env=_env(tmp_path),
        project=project,
    )

    assert _finding(result, "store").detail.startswith(".mcuhome-store")


@pytest.mark.parametrize(
    ("check", "key", "options", "imgtool"),
    [
        pytest.param(
            "store",
            "build.env_store",
            {"mode": "subprocess", "env_store": Path("~nosuchaccount/store")},
            None,
            id="store",
        ),
        pytest.param(
            "imgtool",
            "signing.imgtool",
            {"mode": "container"},
            "~nosuchaccount/bin/imgtool",
            id="imgtool",
        ),
    ],
)
def test_a_path_naming_an_account_this_machine_has_not_got_is_a_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    key: str,
    options: dict[str, Any],
    imgtool: str | None,
) -> None:
    """``~somebody`` is a question the path library answers by raising.

    Not one of this package's refusals — an account that does not exist
    is a `RuntimeError` out of `expanduser` — and a check that promises
    to raise nothing has to answer it too. The finding names the key the
    person set, because that is what they have to change.
    """
    monkeypatch.setattr(hostcheck, "find_imgtool", find_imgtool)

    result = check_build_host(
        options=_options(**options), env={"HOME": str(tmp_path), "PATH": ""}, imgtool=imgtool
    )

    finding = _finding(result, check)
    assert not finding.ok
    assert key in finding.detail
    assert "~nosuchaccount" in finding.detail
    assert key in finding.hint


def test_a_signing_tool_that_cannot_even_be_resolved_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stated `signing.imgtool` is a path, and paths can refuse.

    ``~/bin/imgtool`` without a home directory is a refusal from the path
    expansion, and this call promises to raise nothing — so it is read
    out like every other refusal. The autouse double is removed for this
    one on purpose: it is the real lookup that raises, and a test against
    the double would prove nothing about it.
    """
    monkeypatch.setattr(hostcheck, "find_imgtool", find_imgtool)

    result = check_build_host(
        options=_options(mode="container"), env={"PATH": ""}, imgtool="~/bin/imgtool"
    )

    finding = _finding(result, "imgtool")
    assert not finding.ok
    assert "HOME" in finding.detail
    assert "set HOME" in finding.hint


def test_a_development_build_needs_the_python_on_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interpreter a development build actually runs, and only that.

    It runs the builder with the ``python3`` on the PATH the build was
    started from — never `build.python` and never this process's own
    interpreter — so the check looks it up the way the launcher does and
    says the same thing when it is not there.
    """
    workspace = _a_workspace(tmp_path)
    looked_up: list[tuple[str, str | None]] = []

    def which(program: str, path: str | None = None) -> str | None:
        looked_up.append((program, path))
        return None

    monkeypatch.setattr(hostcheck.shutil, "which", which)

    result = check_build_host(
        options=_options(mode="subprocess", dev_workspace=workspace, python="python3.13"),
        env={"PATH": "/opt/bin"},
    )

    assert looked_up == [("python3", "/opt/bin")], "the build's PATH, not this process's"
    finding = _finding(result, "python")
    assert not finding.ok
    assert "no python3 on this PATH" in finding.detail
    assert "build.dev_workspace" in finding.hint


def test_a_development_build_does_not_need_a_venv_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It installs nothing, so an interpreter without ``venv`` is fine.

    The store branch refuses the same interpreter, which is what makes
    the two checks two checks.
    """
    monkeypatch.setattr(hostcheck, "_run_program", lambda argv: "3 13 0")
    workspace = _a_workspace(tmp_path)

    developing = check_build_host(
        options=_options(mode="subprocess", dev_workspace=workspace), env=_env(tmp_path)
    )
    provisioning = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    assert _finding(developing, "python").ok
    assert not _finding(provisioning, "python").ok


# --------------------------------------------------------------------------
# The setup a build runs in: the project, the configuration, the builders
# --------------------------------------------------------------------------


def _project(tmp_path: Path, *, version: int = PROJECT_VERSION, config: str = "") -> Project:
    """A project directory on disk, with its marker and its secrets folder."""
    root = tmp_path / "project"
    (root / "secrets").mkdir(parents=True)
    os.chmod(root / "secrets", 0o700)
    write_project_file(
        root / PROJECT_MARKER_FILE, ProjectFile(root=root, version=version, id=new_project_id())
    )
    if config:
        (root / "mcuhome.yaml").write_text(config, encoding="utf-8")
    return read_project(root, require_version=False)


def test_a_host_outside_a_project_says_where_one_would_come_from(tmp_path: Path) -> None:
    """Not a failure: an embedder drives a bare device file and has none."""
    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    finding = _finding(result, "project")
    assert finding.ok
    assert finding.detail == "this is not a project directory"
    assert "mcuhome project init" in finding.hint


def test_a_project_is_reported_with_its_layout_version_and_id(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=project
    )

    finding = _finding(result, "project")
    assert finding.ok
    assert str(project.root) in finding.detail
    assert f"project version {PROJECT_VERSION}" in finding.detail
    assert project.file is not None
    assert project.file.short_id is not None
    assert project.file.short_id in finding.detail


def test_a_project_of_an_older_layout_is_the_refusal_every_command_gives(
    tmp_path: Path,
) -> None:
    """The check reports what the next command would refuse with, verbatim."""
    project = _project(tmp_path, version=PROJECT_VERSION - 1)

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=project
    )

    finding = _finding(result, "project")
    assert not finding.ok
    assert not result.ok
    assert "needs an upgrade" in finding.detail
    assert "mcuhome project upgrade" in finding.hint


def test_without_settings_the_configuration_and_builder_checks_are_left_out(
    tmp_path: Path,
) -> None:
    """Not guessed at: a second resolution would be a second configuration."""
    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=_project(tmp_path)
    )

    assert "configuration" not in _checks(result)
    assert "builder" not in _checks(result)


def test_the_configuration_check_names_what_is_set_beyond_the_defaults(
    tmp_path: Path,
) -> None:
    """The line a person reads to find the setting they forgot."""
    env = _env(tmp_path) | {"MCUHOME_BUILD_MODE": "subprocess"}
    settings = resolve_settings(project=None, env=env)

    result = check_build_host(options=_options(mode="subprocess"), env=env, settings=settings)

    finding = _finding(result, "configuration")
    assert finding.ok
    assert "build.mode (environment)" in finding.detail
    assert "build.target" not in finding.detail, "a default is not something somebody set"


def test_a_configuration_with_nothing_set_says_so(tmp_path: Path) -> None:
    settings = resolve_settings(project=None, env=_env(tmp_path))

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), settings=settings
    )

    assert "at their default" in _finding(result, "configuration").detail


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("container", "in a build container"), ("subprocess", "as a child process")],
)
def test_the_builder_check_says_what_a_plain_build_does(
    tmp_path: Path, mode: str, expected: str
) -> None:
    """Both axes in one sentence: where a build runs, and how."""
    settings = resolve_settings(project=None, env=_env(tmp_path))

    result = check_build_host(options=_options(mode=mode), env=_env(tmp_path), settings=settings)

    finding = _finding(result, "builder")
    assert finding.ok
    assert "none configured" in finding.detail
    assert f"a plain build runs on this machine, {expected}" in finding.detail


def test_a_configured_builder_is_listed_with_the_layer_that_defined_it(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        config=(
            "builder:\n"
            "  attic:\n"
            "    target: remote\n"
            "    server: 10.0.0.5:8291\n"
            "build:\n"
            "  builder: attic\n"
        ),
    )
    settings = resolve_settings(project=project, env=_env(tmp_path))

    result = check_build_host(
        options=_options(mode="container"),
        env=_env(tmp_path),
        project=project,
        settings=settings,
    )

    finding = _finding(result, "builder")
    assert finding.ok
    assert "attic (remote, from the project layer)" in finding.detail
    assert "attic takes it: a plain build runs on 10.0.0.5:8291" in finding.detail


def test_a_builder_nobody_defined_is_the_selection_s_own_refusal(tmp_path: Path) -> None:
    """`build.builder` naming nothing is found here, not at the next build."""
    project = _project(tmp_path, config="build:\n  builder: nowhere\n")
    settings = resolve_settings(project=project, env=_env(tmp_path))

    result = check_build_host(
        options=_options(mode="container"),
        env=_env(tmp_path),
        project=project,
        settings=settings,
    )

    finding = _finding(result, "builder")
    assert not finding.ok
    assert not result.ok
    assert "names no configured builder" in finding.detail
    assert "--build-target" in finding.hint


# --------------------------------------------------------------------------
# The permissions of the project's secrets
# --------------------------------------------------------------------------


def test_the_secrets_check_needs_a_project(tmp_path: Path) -> None:
    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    assert "secrets" not in _checks(result)


def test_a_project_that_keeps_no_secrets_yet_has_nothing_to_report(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=project
    )

    finding = _finding(result, "secrets")
    assert finding.ok
    assert "no secrets yet" in finding.detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_owner_only_secrets_are_reported_as_such(tmp_path: Path) -> None:
    project = _project(tmp_path)
    main = project.secrets_file
    main.write_text("wifi_password: hunter2\n", encoding="utf-8")
    main.chmod(0o600)

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=project
    )

    finding = _finding(result, "secrets")
    assert finding.ok
    assert "1 file(s)" in finding.detail
    assert "secrets" in finding.detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_secrets_file_other_users_can_read_is_a_failing_finding(tmp_path: Path) -> None:
    """The guard every reader runs, over every file, reported as data.

    It is not `ok`: a secrets file the whole machine can read is
    something the person has to go and fix, and the fix is the one the
    warning itself carries.
    """
    project = _project(tmp_path)
    (project.secrets_dir / "device").mkdir()
    exposed = project.device_secrets_file("thermostat")
    exposed.write_text("matter_passcode: 12345678\n", encoding="utf-8")
    exposed.chmod(0o644)

    result = check_build_host(
        options=_options(mode="subprocess"), env=_env(tmp_path), project=project
    )

    finding = _finding(result, "secrets")
    assert not finding.ok
    assert not result.ok
    assert str(exposed) in finding.detail
    assert "mode 644" in finding.detail
    assert f"chmod 600 {exposed}" in finding.hint


# --------------------------------------------------------------------------
# What the compiler cache holds
# --------------------------------------------------------------------------


def _fill(directory: Path, name: str, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(payload)
    return path


def test_the_cache_usage_is_one_entry_per_configured_tier(tmp_path: Path) -> None:
    """Configured, not possible: an absent tier is absent from the answer.

    "no session cache" and "an empty session cache" are different
    answers, and a client that renders a table of tiers shows the second
    as a row of zeroes and the first not at all.
    """
    root = tmp_path / "cache"
    _fill(root / "cache-local", "one", b"0123456789")
    _fill(root / "cache-local" / "deeper", "two", b"01234")
    session = _fill(tmp_path / "session", "three", b"012").parent

    usage = read_cache_usage(
        options=_options(cache_root=root, cache_session=session), env=_env(tmp_path)
    )

    assert [entry.tier for entry in usage] == ["local", "session"]
    assert [entry.to_dict() for entry in usage] == [
        {"tier": "local", "path": str(root / "cache-local"), "size": 15, "files": 2},
        {"tier": "session", "path": str(session), "size": 3, "files": 1},
    ]


def test_a_tier_that_was_never_written_to_is_zero_rather_than_a_refusal(
    tmp_path: Path,
) -> None:
    """A build creates the directory; a reading before that is not an error."""
    root = tmp_path / "cache"

    usage = read_cache_usage(options=_options(cache_root=root), env=_env(tmp_path))

    assert [(entry.tier, entry.size, entry.files) for entry in usage] == [("local", 0, 0)]
    assert not root.exists(), "reading what a cache holds creates nothing"


def test_a_machine_with_nowhere_to_put_a_cache_holds_nothing(tmp_path: Path) -> None:
    """No cache root, no tiers — the same answer `resolve_cache_root` gives."""
    assert read_cache_usage(options=_options(), env={"PATH": ""}) == ()


def test_a_shared_tier_stated_but_absent_is_reported_rather_than_refused(
    tmp_path: Path,
) -> None:
    """The reading answers what is there; the build is what refuses.

    `resolve_cache_tiers` refuses a shared cache somebody named and that
    is not there — a machine meant to start warm must not build cold in
    silence. That refusal belongs to the build: a host check that raised
    it would stop reporting halfway through the answer it exists to give.
    """
    absent = tmp_path / "not-mounted"

    usage = read_cache_usage(
        options=_options(cache_root=tmp_path / "cache", cache_shared=absent),
        env=_env(tmp_path),
    )

    assert [entry.tier for entry in usage] == ["local", "shared"]
    shared = next(entry for entry in usage if entry.tier == "shared")
    assert (shared.path, shared.size, shared.files) == (absent, 0, 0)


def test_a_stated_shared_tier_replaces_the_one_under_the_cache_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    _fill(root / "cache-shared", "derived", b"0" * 100)
    elsewhere = _fill(tmp_path / "mounted", "stated", b"0" * 7).parent

    usage = read_cache_usage(
        options=_options(cache_root=root, cache_shared=elsewhere), env=_env(tmp_path)
    )

    shared = next(entry for entry in usage if entry.tier == "shared")
    assert (shared.path, shared.size) == (elsewhere, 7)


def test_a_link_is_not_counted_a_second_time(tmp_path: Path) -> None:
    """A cache that links one entry to another is not measured twice."""
    root = tmp_path / "cache"
    target = _fill(root / "cache-local", "object", b"0" * 64)
    (root / "cache-local" / "link").symlink_to(target)

    usage = read_cache_usage(options=_options(cache_root=root), env=_env(tmp_path))

    assert [(entry.size, entry.files) for entry in usage] == [(64, 1)]
