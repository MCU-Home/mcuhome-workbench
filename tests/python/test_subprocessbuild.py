# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The subprocess profile of the build environment specification.

**No package is fetched and no environment is provisioned here.** The
store entries are directories these tests write and then freeze exactly
as :mod:`mcuhome.workbench.buildenvstore` freezes a real one, with a
completion marker and a fake entry point in the tools entry — which is
all this profile reads out of a store. The driver above it has its own
suite (``test_buildenvsession.py``); the subject here is the profile: the
environment a step is run in, which store entries are used, and that the
store is not touched by a build that ran out of it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
import zstandard
from mcuhome.model.context import EnvironmentPin, PackagePin
from mcuhome.model.hashes import sha256_file
from test_buildenvsession import DELIVERS, entry_point

from mcuhome.workbench import buildenvsession, subprocessbuild
from mcuhome.workbench.buildenvstore import (
    GIT_CONFIG_FILE,
    MARKER_FILE,
    SDK_KIND,
    TOOLS_KIND,
    WORKSPACE_KIND,
    BuildEnvironmentError,
    StoreEntry,
)
from mcuhome.workbench.subprocessbuild import (
    Environment,
    cache_tiers,
    environment_from_store,
    run_locked_build,
)

SDK_VERSION = "0.1.0"
BOARD = "nrf7002dk/nrf5340/cpuapp"
ENVIRONMENT_REFERENCE = "ghcr.io/mcu-home/build-environment:1@sha256:" + "b" * 64

#: What a caller states as "the environment this build was asked from".
#: A sentinel is in it so that the one thing this profile promises about
#: the child's environment can be asserted: it is composed, not inherited.
CALLER_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SECRET_TOKEN": "do-not-leak"}


# --------------------------------------------------------------------------
# A store, written and frozen the way the provisioner writes and freezes one
# --------------------------------------------------------------------------


def freeze(tree: Path) -> None:
    """What :func:`buildenvstore.provision` does before it writes the marker."""
    for parent, directories, files in os.walk(tree, topdown=False):
        for name in files + directories:
            path = Path(parent) / name
            if path.is_symlink():
                continue
            path.chmod(0o500 if path.lstat().st_mode & 0o100 else 0o400)
    tree.chmod(0o500)


def thaw(tree: Path) -> None:
    """Make a frozen entry removable again, so a temporary directory can go."""
    for parent, directories, files in os.walk(tree):
        Path(parent).chmod(0o700)
        for name in files + directories:
            path = Path(parent) / name
            if not path.is_symlink():
                path.chmod(0o700)


def put_entry(
    store: Path, *, kind: str, name: str, version: str, files: dict[str, tuple[str, bool]]
) -> StoreEntry:
    """One provisioned entry: the files, the marker last, then frozen."""
    entry = store / f"{name}-{version}"
    for relative, (content, executable) in files.items():
        path = entry / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)
    digest = hashlib.sha256(f"{name}{version}".encode()).hexdigest()
    (entry / MARKER_FILE).write_text(
        json.dumps({"kind": kind, "package": name, "version": version, "sha256": digest}),
        encoding="utf-8",
    )
    freeze(entry)
    return StoreEntry(kind=kind, name=name, version=version, sha256=digest, path=entry)


@pytest.fixture
def store(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    yield root
    for entry in root.iterdir():
        if entry.is_dir():
            thaw(entry)


#: What the workspace entry declares about the whole set — the §5
#: self-description, the file an orchestrator reads before it starts
#: anything.
#:
#: This is the **abstract** shape, which is the one a real package
#: carries: the carrier states no hash of its own (the declaration is
#: inside the archive it would be describing) and the architecture-
#: specific package is named by its FAMILY at version level only. A
#: fixture that declared the concrete package with a hash would be a
#: delivery's declaration — what an image writes — and no MCUHome package
#: ever writes one, so a check tested only against it would be tested
#: against a shape it never meets.
DECLARATION = {
    "spec-generation": "3",
    "zephyr.version": "4.4.0",
    "build-context.generator-constraint": "mcuhome-workbench:",
    "packages.mcuhome-build-workspace": "0.1.0",
    "packages.mcuhome-build-tools": "0.1.0",
}

#: The hash ``put_entry`` records for the workspace entry, restated here
#: because a build context has to pin exactly the bytes the store holds
#: and the check compares the two.
WORKSPACE_SHA = hashlib.sha256(b"mcuhome-build-workspace0.1.0").hexdigest()


@pytest.fixture
def environment(store: Path) -> Environment:
    """The two entries a subprocess build runs against."""
    put_entry(
        store,
        kind=WORKSPACE_KIND,
        name="mcuhome-build-workspace",
        version="0.1.0",
        files={
            "build-workspace.json": ('{"workspace": "workspace"}', False),
            "build-environment.json": (json.dumps(DECLARATION), False),
            "workspace/.west/config": ("[zephyr]\n\tbase = zephyr\n", False),
            GIT_CONFIG_FILE: ("[safe]\n\tdirectory = /nowhere/*\n", False),
        },
    )
    tools = store / "mcuhome-build-tools_linux-amd64-0.1.0"
    tools.mkdir()
    entry_point(tools, DELIVERS)
    (tools / "build-tools.json").write_text('{"tools": 1}', encoding="utf-8")
    (tools / MARKER_FILE).write_text(
        json.dumps(
            {
                "kind": TOOLS_KIND,
                "package": "mcuhome-build-tools_linux-amd64",
                "version": "0.1.0",
                "sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    freeze(tools)
    return environment_from_store(
        store,
        workspace=("mcuhome-build-workspace", "0.1.0"),
        tools=("mcuhome-build-tools_linux-amd64", "0.1.0"),
    )


def snapshot(tree: Path) -> dict[str, tuple]:
    """Every byte and every bit of metadata under *tree*, for a before/after."""
    found: dict[str, tuple] = {}
    for parent, directories, files in os.walk(tree):
        for name in sorted(directories + files):
            path = Path(parent) / name
            relative = str(path.relative_to(tree))
            info = path.lstat()
            if path.is_symlink():
                found[relative] = ("link", os.readlink(path), info.st_mode)
            elif path.is_dir():
                found[relative] = ("dir", info.st_mode, info.st_mtime_ns)
            else:
                found[relative] = (
                    "file",
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    sha256_file(path),
                )
    return found


# --------------------------------------------------------------------------
# A build context and an SDK package, small enough to write here
# --------------------------------------------------------------------------


def make_sdk_source(directory: Path) -> str:
    """A source directory with one SDK archive and the index that names it."""
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        content = b'{"sdk": 1}'
        info = tarfile.TarInfo("mcuhome-sdk.json")
        info.size = len(content)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(content))
    archive = zstandard.ZstdCompressor(level=3).compress(buffer.getvalue())
    filename = f"mcuhome-sdk-{SDK_VERSION}.tar.zst"
    (directory / filename).write_bytes(archive)
    real = sha256_file(directory / filename)
    (directory / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "mcuhome-sdk": {
                        SDK_VERSION: {
                            "file": filename,
                            "sha256": real,
                            "size": len(archive),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return real


def make_context(directory: Path, sdk_sha256: str) -> Path:
    """A locked context directory, written by hand.

    Hand-written on purpose: what this suite is about starts at a context
    that already exists, and creating one through the workbench would
    pull the whole pin resolution into a test about running a step.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "context": 4,
        "mcuhome": {
            "constraint": f"=={SDK_VERSION}",
            "version": SDK_VERSION,
            "package": {"url": f"mcuhome-sdk-{SDK_VERSION}.tar.zst", "sha256": sdk_sha256},
        },
        # The packages the ``environment`` fixture's store actually holds:
        # the workspace by name and hash, the tools by its family, which
        # is what a context ordinarily pins.
        "build_environment": {
            "workspace": {
                "name": "mcuhome-build-workspace",
                "version": "0.1.0",
                "sha256": WORKSPACE_SHA,
            },
            "tools": {"name": "mcuhome-build-tools", "version": "0.1.0", "sha256": "ba" * 32},
        },
        "target": {"board": BOARD},
        "files": [],
        "id": "sha256:" + "d" * 64,
    }
    (directory / "manifest.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    # The one file the build environment specification names itself: the
    # environment's generator constraint is checked against it before a
    # step, so a context without it is not one.
    (directory / "build-context.json").write_text(
        json.dumps({"generator": "mcuhome-workbench:0.1.0.dev0"}), encoding="utf-8"
    )
    return directory


# --------------------------------------------------------------------------
# Which entries a build runs against
# --------------------------------------------------------------------------


def test_the_entries_of_a_provisioned_environment_are_found(store, environment) -> None:
    assert environment.tools.name == "mcuhome-build-tools_linux-amd64"
    assert environment.workspace.kind == WORKSPACE_KIND
    assert environment.entry_point.is_file()
    assert "mcuhome-build-workspace 0.1.0" in environment.described()


def test_each_pinned_package_is_provisioned_with_its_own_directories_and_bound(
    tmp_path, monkeypatch
) -> None:
    """What the machine is configured with reaches the provisioner per package.

    The two environment packages may be published in directories of their
    own and may be bounded differently, and both decisions are per
    package — so this asserts the arguments each call was made with, not
    that some call was made.
    """
    provisioned: list[dict] = []
    looked_up: list[dict] = []

    def fake_concrete(pin, **kwargs):
        looked_up.append({"name": pin.name, **kwargs})
        return PackagePin(name=pin.name + "-concrete", version="9.9.9", sha256="a" * 64)

    def fake_provision(**kwargs):
        provisioned.append(kwargs)
        return StoreEntry(
            path=tmp_path / kwargs["name"],
            kind=kwargs["kind"],
            name=kwargs["name"],
            version=kwargs["version"],
            sha256=kwargs["sha256"],
        )

    monkeypatch.setattr(subprocessbuild, "concrete_package", fake_concrete)
    monkeypatch.setattr(subprocessbuild, "provision", fake_provision)
    monkeypatch.setattr(subprocessbuild, "_require_entry_point", lambda entry: entry)

    subprocessbuild.environment_from_pins(
        EnvironmentPin(
            workspace=PackagePin(name="mcuhome-build-workspace", version="1", sha256="b" * 64),
            tools=PackagePin(name="mcuhome-build-tools", version="1", sha256="c" * 64),
        ),
        env={},
        sources=(tmp_path / "sdk",),
        workspace_sources=(tmp_path / "workspaces",),
        tools_sources=(tmp_path / "tools",),
        store=tmp_path / "store",
        interpreter="python3.13",
        bounds={WORKSPACE_KIND: 22, TOOLS_KIND: 33},
    )

    assert [call["sources"] for call in looked_up] == [
        (tmp_path / "workspaces",),
        (tmp_path / "tools",),
    ]
    assert [call["kind"] for call in provisioned] == [WORKSPACE_KIND, TOOLS_KIND]
    assert [call["max_bytes"] for call in provisioned] == [22, 33]
    assert [call["sources"] for call in provisioned] == [
        (tmp_path / "workspaces",),
        (tmp_path / "tools",),
    ]
    assert {call["store"] for call in provisioned} == {tmp_path / "store"}
    assert {call["interpreter"] for call in provisioned} == {"python3.13"}


def test_without_its_own_directories_a_package_is_looked_for_where_the_sdk_is(
    tmp_path, monkeypatch
) -> None:
    """One directory holding everything is the ordinary machine, and it
    keeps working: unset means the SDK's own sources."""
    provisioned: list[dict] = []
    monkeypatch.setattr(
        subprocessbuild,
        "concrete_package",
        lambda pin, **kwargs: PackagePin(name=pin.name, version="1", sha256="a" * 64),
    )

    def fake_provision(**kwargs):
        provisioned.append(kwargs)
        return StoreEntry(
            path=tmp_path / kwargs["name"],
            kind=kwargs["kind"],
            name=kwargs["name"],
            version=kwargs["version"],
            sha256=kwargs["sha256"],
        )

    monkeypatch.setattr(subprocessbuild, "provision", fake_provision)
    monkeypatch.setattr(subprocessbuild, "_require_entry_point", lambda entry: entry)
    subprocessbuild.environment_from_pins(
        EnvironmentPin(
            workspace=PackagePin(name="mcuhome-build-workspace", version="1", sha256="b" * 64),
            tools=PackagePin(name="mcuhome-build-tools", version="1", sha256="c" * 64),
        ),
        env={},
        sources=(tmp_path / "sdk",),
    )
    assert {call["sources"] for call in provisioned} == {(tmp_path / "sdk",)}
    # And a kind nobody bounded is left to the store's own table.
    assert {call["max_bytes"] for call in provisioned} == {None}


def test_a_package_that_was_never_provisioned_is_refused(store) -> None:
    with pytest.raises(BuildEnvironmentError) as refusal:
        environment_from_store(
            store,
            workspace=("mcuhome-build-workspace", "9.9.9"),
            tools=("mcuhome-build-tools_linux-amd64", "9.9.9"),
        )
    assert "mcuhome-build-workspace 9.9.9" in refusal.value.message
    assert "9.9.9" in refusal.value.hint


def test_an_entry_that_was_unpacked_as_another_kind_is_refused(store) -> None:
    put_entry(
        store,
        kind=SDK_KIND,
        name="mcuhome-build-workspace",
        version="0.1.0",
        files={"mcuhome-sdk.json": ("{}", False)},
    )
    with pytest.raises(BuildEnvironmentError, match="where a build-workspace one"):
        environment_from_store(
            store,
            workspace=("mcuhome-build-workspace", "0.1.0"),
            tools=("mcuhome-build-tools_linux-amd64", "0.1.0"),
        )


def test_a_tools_entry_without_an_entry_point_is_refused(store) -> None:
    put_entry(
        store,
        kind=WORKSPACE_KIND,
        name="mcuhome-build-workspace",
        version="0.1.0",
        files={"build-workspace.json": ("{}", False)},
    )
    put_entry(
        store,
        kind=TOOLS_KIND,
        name="mcuhome-build-tools_linux-amd64",
        version="0.1.0",
        files={"build-tools.json": ("{}", False)},
    )
    with pytest.raises(BuildEnvironmentError, match="no entry point"):
        environment_from_store(
            store,
            workspace=("mcuhome-build-workspace", "0.1.0"),
            tools=("mcuhome-build-tools_linux-amd64", "0.1.0"),
        )


def test_an_entry_point_that_cannot_be_run_is_refused_rather_than_waited_for(
    store, tmp_path
) -> None:
    """A program that cannot start is not a process the ladder can end."""
    put_entry(
        store,
        kind=WORKSPACE_KIND,
        name="mcuhome-build-workspace",
        version="0.1.0",
        files={"build-workspace.json": ("{}", False)},
    )
    put_entry(
        store,
        kind=TOOLS_KIND,
        name="mcuhome-build-tools_linux-amd64",
        version="0.1.0",
        files={
            "build-tools.json": ("{}", False),
            f"bin/{buildenvsession.ENTRY_POINT}": ("#!/bin/sh\nexit 0\n", False),
        },
    )
    with pytest.raises(BuildEnvironmentError, match="can run"):
        environment_from_store(
            store,
            workspace=("mcuhome-build-workspace", "0.1.0"),
            tools=("mcuhome-build-tools_linux-amd64", "0.1.0"),
        )


# --------------------------------------------------------------------------
# The environment a step is run in
# --------------------------------------------------------------------------


def run_one_step(
    tmp_path: Path, environment: Environment, **kwargs
) -> subprocessbuild.SubprocessBuildResult:
    sdk_sha256 = make_sdk_source(tmp_path / "packages")
    context = make_context(tmp_path / "context", sdk_sha256)
    return run_locked_build(
        context,
        environment=environment,
        sdk_sources=(tmp_path / "packages",),
        work_root=tmp_path / "work",
        env=CALLER_ENV,
        **kwargs,
    )


def child_environment(result: subprocessbuild.SubprocessBuildResult) -> dict[str, str]:
    dumps = sorted(result.out_dir.glob("environment-*.txt"))
    values: dict[str, str] = {}
    for line in dumps[-1].read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name] = value
    return values


def test_an_entry_point_that_cannot_be_executed_refuses_at_once(
    tmp_path, store, environment
) -> None:
    """Executable and still not a program: the case the mode bit misses.

    Without this, a handle to a program that never started answers "still
    running" to every question the liveness ladder asks, and the deadline
    is what would eventually end the wait.
    """
    thaw(environment.tools.path)
    environment.entry_point.write_bytes(b"\x00\x01not a program\n")
    environment.entry_point.chmod(0o755)
    freeze(environment.tools.path)

    started = time.monotonic()
    with pytest.raises(BuildEnvironmentError, match="could not start"):
        run_one_step(tmp_path, environment)
    assert time.monotonic() - started < 30


def test_a_step_runs_against_the_store_and_delivers(tmp_path, environment) -> None:
    result = run_one_step(tmp_path, environment)

    assert result.outcome.successful
    assert result.outcome.status == "success"
    assert [artifact.path for artifact in result.outcome.artifacts] == ["firmware.bin"]
    assert (result.out_dir / "firmware.bin").read_text(encoding="utf-8") == "FIRMWARE"
    assert result.environment is environment


def test_the_environment_the_child_is_given_is_composed_not_inherited(
    tmp_path, environment
) -> None:
    result = run_one_step(tmp_path, environment, jobs=4)
    values = child_environment(result)

    assert values["MCUHOME_BUILD_ENV_TOOLS"] == str(environment.tools.path)
    assert values["MCUHOME_BUILD_ENV_WORKSPACE"] == str(environment.workspace.path)
    assert values["GIT_CONFIG_GLOBAL"] == str(environment.workspace.path / GIT_CONFIG_FILE)
    assert Path(values["GIT_CONFIG_GLOBAL"]).is_file()
    assert values["MCUHOME_BUILDER_BASE_DIR"].endswith("-1")
    assert values["CCACHE_BASEDIR"] == values["MCUHOME_BUILDER_BASE_DIR"]
    assert values["CCACHE_NOHASHDIR"] == "1"
    assert values["CCACHE_COMPILERCHECK"] == "content"
    assert values["CCACHE_IGNOREOPTIONS"] == "-specs=*"
    assert values["MCUHOME_JOBS"] == "4"
    assert values["PATH"].endswith(CALLER_ENV["PATH"])
    assert values["HOME"]
    # The one thing this profile promises about the child's environment:
    # what the caller exported is not what the build sees.
    assert "SECRET_TOKEN" not in values


def test_the_compiler_cache_is_the_most_local_writable_tier(tmp_path, environment) -> None:
    cache = tmp_path / "ccache"
    result = run_one_step(tmp_path, environment, ccache_dir=cache)
    values = child_environment(result)

    durable = cache / "cache-local" / "ccache"
    assert values["CCACHE_DIR"] == str(durable)
    assert durable.is_dir()


def test_without_a_cache_root_the_cache_is_the_step_s_own_local_tier(tmp_path, environment) -> None:
    result = run_one_step(tmp_path, environment)
    values = child_environment(result)

    assert values["CCACHE_DIR"].endswith("/mcuhome/cache/local/ccache")


def test_cache_tiers_are_derived_from_a_cache_root(tmp_path) -> None:
    root = tmp_path / "ccache"
    assert cache_tiers(ccache_dir=None) == {}

    tiers = cache_tiers(ccache_dir=root)
    assert tiers["local"].path == root / "cache-local"
    assert tiers["local"].writable
    # The shared half is offered only when somebody filled it: a backend
    # that created it would be offering a cache nobody warmed.
    assert "shared" not in tiers

    (root / "cache-shared").mkdir(parents=True)
    tiers = cache_tiers(ccache_dir=root)
    assert tiers["shared"].path == root / "cache-shared"
    assert not tiers["shared"].writable

    # The two tiers an orchestrator provides per session and per project.
    tiers = cache_tiers(ccache_dir=root, session_dir=tmp_path / "s", project_dir=tmp_path / "p")
    assert tiers["session"].path == tmp_path / "s"
    assert tiers["project"].writable


def test_the_tiers_a_caller_states_are_the_ones_the_step_gets(tmp_path, environment) -> None:
    """A caller that resolved the tiers itself is not overruled."""
    project = tmp_path / "project-cache"
    project.mkdir()
    result = run_one_step(
        tmp_path,
        environment,
        tiers={"project": subprocessbuild.CacheTier(path=project, writable=True)},
    )
    values = child_environment(result)
    step = sorted((tmp_path / "work" / "session" / "steps").iterdir())[-1]

    assert result.outcome.successful
    assert (step / "mcuhome" / "cache" / "project").resolve() == project.resolve()
    # `local` is the most local tier and is always writable, so it stays
    # the compiler cache even when a caller provided another one — which
    # is why an orchestrator that wants a durable cache provides a
    # directory for `local` itself.
    assert values["CCACHE_DIR"].endswith("/mcuhome/cache/local/ccache")


# --------------------------------------------------------------------------
# The store is read-only, and stays that way
# --------------------------------------------------------------------------


def test_the_store_is_byte_identical_after_a_build(tmp_path, store, environment) -> None:
    before = snapshot(store)
    result = run_one_step(tmp_path, environment)
    after = snapshot(store)

    assert result.outcome.successful
    assert before == after


def test_the_frozen_store_denies_a_build_that_tries_to_write_into_it(
    tmp_path, store, environment
) -> None:
    # The entry point is replaced by one that patches its own package in
    # place, which is what a builder must never do in this profile.
    thaw(environment.tools.path)
    entry_point(
        environment.tools.path,
        'printf x > "$MCUHOME_BUILD_ENV_WORKSPACE/workspace/patched" || true\n'
        'test -f "$MCUHOME_BUILD_ENV_WORKSPACE/workspace/patched" && exit 9\n'
        'cat > "$mc/out/result-$id.json" <<EOF\n'
        '{"spec_generation": 3, "invocation_id": "$id", "status": "success", '
        '"message": "", "artifacts": []}\n'
        "EOF\nexit 0\n",
    )
    freeze(environment.tools.path)
    before = snapshot(store)
    result = run_one_step(tmp_path, environment)

    assert result.outcome.successful
    assert snapshot(store) == before


# --------------------------------------------------------------------------
# Development mode: trees the developer maintains
# --------------------------------------------------------------------------


@pytest.fixture
def developer(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace and a tools tree as a developer keeps them: unfrozen.

    Same shape as a store entry minus everything the store adds — no
    completion marker, no package hash, and writable, because the whole
    point is that the person running the build edits them.
    """
    workspace = tmp_path / "dev" / "workspace-tree"
    (workspace / "workspace" / ".west").mkdir(parents=True)
    (workspace / "workspace" / ".west" / "config").write_text(
        "[zephyr]\n\tbase = zephyr\n", encoding="utf-8"
    )
    (workspace / "build-workspace.json").write_text(
        json.dumps(
            {
                "package": "mcuhome-build-workspace",
                "version": "0.1.0+dev",
                "workspace": "workspace",
            }
        ),
        encoding="utf-8",
    )
    tools = tmp_path / "dev" / "tools-tree"
    tools.mkdir(parents=True)
    entry_point(tools, DELIVERS)
    (tools / "build-tools.json").write_text(
        json.dumps({"package": "mcuhome-build-tools_linux-amd64", "version": "0.1.0+dev"}),
        encoding="utf-8",
    )
    return workspace, tools


def test_an_environment_can_be_two_directories_a_developer_maintains(developer) -> None:
    """What the store would have said about the trees, read out of the trees."""
    workspace, tools = developer
    environment = subprocessbuild.environment_from_paths(workspace, tools)

    assert environment.developer is True
    assert environment.workspace.path == workspace
    assert environment.workspace.name == "mcuhome-build-workspace"
    assert environment.workspace.version == "0.1.0+dev"
    assert environment.tools.name == "mcuhome-build-tools_linux-amd64"
    # No package published these bytes, so there is no hash to state.
    assert environment.workspace.sha256 == ""
    assert environment.entry_point == tools / "bin" / "build-environment-entry"


def test_a_developer_environment_names_its_trees_where_it_is_described(developer) -> None:
    """Two builds a week apart can state the same version over different
    bytes, so the log line names the paths as well."""
    workspace, tools = developer
    described = subprocessbuild.environment_from_paths(workspace, tools).described()

    assert str(workspace) in described
    assert str(tools) in described


def test_a_developer_directory_that_is_not_there_is_refused(tmp_path, developer) -> None:
    workspace, tools = developer
    with pytest.raises(BuildEnvironmentError, match="no directory at") as refused:
        subprocessbuild.environment_from_paths(tmp_path / "absent", tools)
    assert subprocessbuild.DEV_WORKSPACE_OPTION in refused.value.hint

    with pytest.raises(BuildEnvironmentError, match="no directory at") as refused:
        subprocessbuild.environment_from_paths(workspace, tmp_path / "absent")
    assert subprocessbuild.DEV_TOOLS_OPTION in refused.value.hint


def test_a_developer_tree_that_does_not_say_what_it_is_gets_refused(developer) -> None:
    """The manifest is per kind, so the two trees swapped is caught here
    rather than three minutes into a compile."""
    workspace, tools = developer
    with pytest.raises(BuildEnvironmentError, match="build-workspace.json"):
        subprocessbuild.environment_from_paths(tools, tools)
    with pytest.raises(BuildEnvironmentError, match="build-tools.json"):
        subprocessbuild.environment_from_paths(workspace, workspace)


def test_a_developer_tree_without_a_package_and_version_is_refused(developer) -> None:
    """A build log that cannot name what it built against is worth fixing
    before the build, not after."""
    workspace, tools = developer
    (workspace / "build-workspace.json").write_text('{"workspace": "workspace"}', encoding="utf-8")
    with pytest.raises(BuildEnvironmentError, match="which package and version"):
        subprocessbuild.environment_from_paths(workspace, tools)


def test_a_developer_tools_tree_without_an_entry_point_is_refused(developer) -> None:
    workspace, tools = developer
    (tools / "bin" / "build-environment-entry").unlink()
    with pytest.raises(BuildEnvironmentError, match="no entry point"):
        subprocessbuild.environment_from_paths(workspace, tools)


def test_a_developer_build_reaches_the_launcher_with_its_own_trees(tmp_path, developer) -> None:
    """The ordinary case: no patches, and the build runs against the
    developer's trees exactly as it runs against a store's."""
    workspace, tools = developer
    environment = subprocessbuild.environment_from_paths(workspace, tools)
    result = run_one_step(tmp_path, environment)
    values = child_environment(result)

    assert result.outcome.successful
    assert values["MCUHOME_BUILD_ENV_WORKSPACE"] == str(workspace)
    assert values["MCUHOME_BUILD_ENV_TOOLS"] == str(tools)


def test_a_context_with_patches_is_refused_in_development_mode(tmp_path, developer) -> None:
    """Neither applied nor ignored.

    Applying would edit source trees the developer maintains; ignoring
    would build firmware that is not what the build context says it is.
    The refusal is typed, and it happens before anything is fetched or
    written — no SDK package, no session directory.
    """
    workspace, tools = developer
    environment = subprocessbuild.environment_from_paths(workspace, tools)
    sdk_sha256 = make_sdk_source(tmp_path / "packages")
    context = make_context(tmp_path / "context", sdk_sha256)
    (context / "patches" / "zephyr").mkdir(parents=True)
    (context / "patches" / "zephyr" / "0001-fix.patch").write_text("--- a\n+++ b\n")

    with pytest.raises(BuildEnvironmentError, match="cannot apply them") as refused:
        run_locked_build(
            context,
            environment=environment,
            sdk_sources=(tmp_path / "packages",),
            work_root=tmp_path / "work",
            env=CALLER_ENV,
        )
    assert "zephyr" in str(refused.value)
    assert subprocessbuild.DEV_WORKSPACE_OPTION in refused.value.hint
    assert not (tmp_path / "work").exists()


def test_a_context_with_patches_builds_against_a_store(tmp_path, environment) -> None:
    """The other side of the refusal: with a provisioned environment there
    is nothing to refuse, because the trees a patch names are copied and
    patched in the copy."""
    sdk_sha256 = make_sdk_source(tmp_path / "packages")
    context = make_context(tmp_path / "context", sdk_sha256)
    (context / "patches" / "zephyr").mkdir(parents=True)
    (context / "patches" / "zephyr" / "0001-fix.patch").write_text("--- a\n+++ b\n")

    result = run_locked_build(
        context,
        environment=environment,
        sdk_sources=(tmp_path / "packages",),
        work_root=tmp_path / "work",
        env=CALLER_ENV,
    )
    assert result.outcome.successful


def test_an_empty_patch_directory_is_not_a_patched_context(tmp_path, developer) -> None:
    """A context that carries the directory and no patches is the ordinary
    case, not a refusal."""
    workspace, tools = developer
    environment = subprocessbuild.environment_from_paths(workspace, tools)
    sdk_sha256 = make_sdk_source(tmp_path / "packages")
    context = make_context(tmp_path / "context", sdk_sha256)
    (context / "patches").mkdir()

    result = run_locked_build(
        context,
        environment=environment,
        sdk_sources=(tmp_path / "packages",),
        work_root=tmp_path / "work",
        env=CALLER_ENV,
    )
    assert result.outcome.successful


# --------------------------------------------------------------------------
# What the environment says about itself, checked before a step
# --------------------------------------------------------------------------


def _declared(store: Path, **overrides) -> None:
    """Rewrite the workspace entry's declaration, frozen store and all."""
    entry = store / "mcuhome-build-workspace-0.1.0"
    thaw(entry)
    document = {**DECLARATION, **overrides}
    for key, value in list(document.items()):
        if value is None:
            del document[key]
    (entry / "build-environment.json").write_text(json.dumps(document), encoding="utf-8")
    freeze(entry)


def _pin(**overrides) -> EnvironmentPin:
    pin = EnvironmentPin(
        workspace=PackagePin(name="mcuhome-build-workspace", version="0.1.0", sha256=WORKSPACE_SHA),
        tools=PackagePin(name="mcuhome-build-tools", version="0.1.0", sha256="a" * 64),
    )
    return replace(pin, **overrides)


def test_the_environments_declaration_is_read_off_the_store(environment) -> None:
    """The cheapest verified route: the entry, not a sidecar nobody signed.

    The bytes in a provisioned entry came out of an archive whose hash was
    checked against the pin, so what the environment claims about itself
    cannot have been substituted between the package host and this file.
    """
    declaration = subprocessbuild.declaration_of(environment)
    assert declaration.spec_generation == "3"
    assert declaration.zephyr_version == "4.4.0"
    assert set(declaration.packages) == {
        "mcuhome-build-workspace",
        "mcuhome-build-tools",
    }


def test_an_environment_that_agrees_with_everything_passes(environment) -> None:
    """All four questions answered, and the declaration handed back."""
    declaration = subprocessbuild.check_environment(
        environment,
        pin=_pin(),
        generator="mcuhome-workbench:0.1.0.dev0",
        zephyr_constraint="~=4.4.0",
    )
    assert declaration.zephyr_version == "4.4.0"


def test_an_environment_of_another_specification_generation_is_refused(store, environment) -> None:
    """An orchestrator does not start an environment it cannot speak to.

    The generation is the one number that says whether the two sides mean
    the same thing by a request document — so it is checked before a
    process is started, not discovered from a confused answer afterwards.
    """
    _declared(store, **{"spec-generation": "4"})
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment)
    assert "generation 4" in caught.value.message
    assert "generation 3" in caught.value.message


def test_an_environment_assembled_from_other_packages_is_refused(store, environment) -> None:
    """The store has to be the environment the context pinned."""
    _declared(
        store,
        **{
            "packages.mcuhome-build-workspace": None,
            "packages.mcuhome-build-elsewhere": "0.1.0",
        },
    )
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment, pin=_pin())
    assert "mcuhome-build-workspace" in caught.value.message


def test_a_declared_version_that_is_not_the_unpacked_one_is_refused(store, environment) -> None:
    """A tree that says it is one version while the entry is another."""
    _declared(store, **{"packages.mcuhome-build-workspace": "0.2.0"})
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment, pin=_pin())
    assert "0.2.0" in caught.value.message


def test_a_declared_hash_that_is_not_the_unpacked_one_is_refused(store, environment) -> None:
    """Where the declaration states bytes, they have to be the bytes present.

    Only a *delivery* states them — an image, or anything else assembled
    from exact archives — so this is the shape that reaches the check
    through the container profile rather than through the store.
    """
    _declared(
        store,
        **{
            "packages.mcuhome-build-tools": None,
            "packages.mcuhome-build-tools_linux-amd64": "0.1.0@sha256:" + "d" * 64,
        },
    )
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment, pin=_pin())
    assert "d" * 64 in caught.value.message


def test_a_concrete_tools_pin_is_accepted_by_an_abstract_declaration(environment) -> None:
    """A device may pin one platform's package, and the environment still fits.

    The declaration a real workspace package carries names the tools
    **family**; a device that pinned
    ``mcuhome-build-tools_linux-amd64`` outright names the concrete
    package, and the specification is explicit that the abstract
    declaration matches every delivery of that set. A check that probed
    only for exact names would refuse the one case the format calls an
    explicitly architecture-targeted build.
    """
    subprocessbuild.check_environment(
        environment,
        pin=_pin(
            tools=PackagePin(
                name="mcuhome-build-tools_linux-amd64", version="0.1.0", sha256="c" * 64
            )
        ),
    )


def test_a_delivery_declaration_naming_the_concrete_package_also_fits(store, environment) -> None:
    """The other shape §5.1 defines: an image completes the family entry."""
    _declared(
        store,
        **{
            "packages.mcuhome-build-tools": None,
            "packages.mcuhome-build-tools_linux-amd64": "0.1.0@sha256:" + "c" * 64,
        },
    )
    subprocessbuild.check_environment(environment, pin=_pin())


def test_a_zephyr_release_outside_the_devices_constraint_is_refused(environment) -> None:
    """The device asks for a Zephyr line; the environment states a release.

    This is where the two meet — against the workspace package's own
    declaration, out of bytes that were verified, rather than against a
    label or a sidecar.
    """
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment, zephyr_constraint="~=4.5.0")
    assert "4.4.0" in caught.value.message
    assert "4.5.0" in caught.value.message


def test_a_generator_the_environment_does_not_accept_is_refused(store, environment) -> None:
    """The check the specification runs before every step."""
    _declared(store, **{"build-context.generator-constraint": "mcuhome-workbench:~=9.0"})
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment, generator="mcuhome-workbench:0.1.0")
    assert "~=9.0" in caught.value.hint


def test_a_developer_environment_is_exempt_from_the_package_check_only(store, environment) -> None:
    """Nothing published a developer's trees, so no hash can agree with one.

    Everything else is still checked: a tree a developer maintains has to
    implement the specification this side speaks, or the build fails
    somewhere less legible.
    """
    developer = replace(environment, developer=True)
    subprocessbuild.check_environment(
        developer,
        pin=_pin(workspace=PackagePin(name="something-else", version="9.9.9", sha256="e" * 64)),
    )
    _declared(store, **{"spec-generation": "2"})
    with pytest.raises(BuildEnvironmentError):
        subprocessbuild.check_environment(developer, pin=_pin())


def test_an_environment_without_a_declaration_is_refused(store, environment) -> None:
    """A build environment that does not say what it is cannot be checked."""
    entry = store / "mcuhome-build-workspace-0.1.0"
    thaw(entry)
    (entry / "build-environment.json").unlink()
    freeze(entry)
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(environment)
    assert "does not say what it is" in caught.value.message


def test_a_developer_tree_that_carries_no_declaration_is_not_refused(environment) -> None:
    """Nothing published those bytes, so no statement about them exists.

    The mode's whole point is that the trees are the developer's own; a
    workspace assembled by hand has no ``build-environment.json``, and
    demanding one would be demanding that a developer package their work
    before they can build it.
    """
    developer = replace(environment, developer=True)
    thaw(environment.workspace.path)
    (environment.workspace.path / "build-environment.json").unlink()
    freeze(environment.workspace.path)
    assert subprocessbuild.check_environment(developer, pin=_pin()) is None


def test_a_developer_tree_that_does_carry_one_is_held_to_it(store, environment) -> None:
    """Anything unpacked from a real package states a generation, and it counts."""
    _declared(store, **{"spec-generation": "9"})
    developer = replace(environment, developer=True)
    with pytest.raises(BuildEnvironmentError):
        subprocessbuild.check_environment(developer)


def test_the_pin_and_the_unpacked_package_have_to_be_the_same_bytes(environment) -> None:
    """The check that makes an environment the one the context asked for.

    The declaration says what the environment claims to be; this says the
    environment is what the **context** named. Both are needed: an
    environment can agree with itself perfectly and still be a different
    one than the build was pinned to.
    """
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(
            environment,
            pin=_pin(
                workspace=PackagePin(
                    name="mcuhome-build-workspace", version="0.1.0", sha256="f" * 64
                )
            ),
        )
    assert "f" * 64 in caught.value.message


def test_a_family_pin_is_compared_by_version_and_not_by_hash(environment) -> None:
    """A family's hash covers every platform; the entry holds one platform's.

    Comparing the two would refuse every correct build. What is left to
    compare here is the version, and that is compared.
    """
    subprocessbuild.check_environment(environment, pin=_pin())
    with pytest.raises(BuildEnvironmentError) as caught:
        subprocessbuild.check_environment(
            environment,
            pin=_pin(
                tools=PackagePin(name="mcuhome-build-tools", version="0.2.0", sha256="a" * 64)
            ),
        )
    assert "0.2.0" in caught.value.message
