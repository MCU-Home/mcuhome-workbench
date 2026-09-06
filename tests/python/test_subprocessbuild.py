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
from pathlib import Path

import pytest
import zstandard
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
        "context": 3,
        "mcuhome": {
            "constraint": f"=={SDK_VERSION}",
            "version": SDK_VERSION,
            "package": {"url": f"mcuhome-sdk-{SDK_VERSION}.tar.zst", "sha256": sdk_sha256},
        },
        "build_environment": ENVIRONMENT_REFERENCE,
        "target": {"board": BOARD},
        "files": [],
        "id": "sha256:" + "d" * 64,
    }
    (directory / "manifest.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


# --------------------------------------------------------------------------
# Which entries a build runs against
# --------------------------------------------------------------------------


def test_the_entries_of_a_provisioned_environment_are_found(store, environment) -> None:
    assert environment.tools.name == "mcuhome-build-tools_linux-amd64"
    assert environment.workspace.kind == WORKSPACE_KIND
    assert environment.entry_point.is_file()
    assert "mcuhome-build-workspace 0.1.0" in environment.described()


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
