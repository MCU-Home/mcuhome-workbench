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
    BuildOptions,
    HostCheckResult,
    HostFinding,
    Project,
    check_build_host,
)
from mcuhome.workbench.buildprocess import Completed
from mcuhome.workbench.imgtool import find_imgtool


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
    monkeypatch.setattr(hostcheck, "ImageRegistry", RecordingRegistry)
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

    assert _checks(result) == [
        "container_runtime",
        "container_image",
        "signing_imgtool",
        "cache_root",
    ]
    assert result.ok
    assert RecordingRuntime.made[0].argvs, "the runtime was never asked anything"
    assert RecordingRegistry.made[0].asked == ["ghcr.io/mcu-home/build-environment"]


def test_the_subprocess_mode_asks_a_container_runtime_nothing(tmp_path: Path) -> None:
    """The defect this module exists for: no container talk without a container.

    The doubles are installed and record every call, so this asserts
    that nothing reached them — not that nothing could have.
    """
    result = check_build_host(options=_options(mode="subprocess"), env=_env(tmp_path))

    assert _checks(result) == ["env_store", "python", "signing_imgtool", "cache_root"]
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

    assert _checks(result) == ["dev_workspace", "signing_imgtool", "cache_root"]
    assert result.ok
    assert str(workspace) in _finding(result, "dev_workspace").detail


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

    finding = _finding(result, "container_runtime")
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
    assert not _finding(result, "env_store").ok
    assert "HOME" in _finding(result, "env_store").detail
    # A machine with nowhere to put a cache builds without one, which is
    # slow and not broken.
    assert _finding(result, "cache_root").ok


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

    finding = _finding(result, "container_image")
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

    finding = _finding(result, "container_image")
    assert finding.ok
    assert "registry.example/mirror publishes 2 image(s)" in finding.detail
    assert "could not be asked" in finding.detail, "the miss is still named"


def test_a_machine_allowed_no_repository_at_all_is_told_so(tmp_path: Path) -> None:
    """An empty allowlist is a configuration answer, not a network one."""
    result = check_build_host(
        options=_options(mode="container", container_repositories=()), env=_env(tmp_path)
    )

    finding = _finding(result, "container_image")
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

    finding = _finding(result, "env_store")
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

    assert _finding(result, "env_store").ok
    assert "1 build environment(s) provisioned" in _finding(result, "env_store").detail


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
    finding = _finding(result, "signing_imgtool")
    assert not finding.ok
    assert "signing.imgtool" in finding.hint


def test_a_host_with_nowhere_to_cache_is_still_a_host_that_builds(
    tmp_path: Path,
) -> None:
    """No cache is slow, not broken — so the finding is ok and says why."""
    result = check_build_host(options=_options(mode="container"), env={"PATH": ""})

    finding = _finding(result, "cache_root")
    assert finding.ok
    assert "compiles from scratch" in finding.detail
    assert "build.cache_root" in finding.hint


def test_a_cache_root_that_cannot_be_written_is_not_ok(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")

    result = check_build_host(
        options=_options(mode="container", cache_root=blocked / "cache"), env=_env(tmp_path)
    )

    finding = _finding(result, "cache_root")
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
            HostFinding(check="cache_root", ok=False, detail="no"),
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

    assert _finding(result, "env_store").detail.startswith(".mcuhome-store")


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

    finding = _finding(result, "signing_imgtool")
    assert not finding.ok
    assert "HOME" in finding.detail
    assert "set HOME" in finding.hint
