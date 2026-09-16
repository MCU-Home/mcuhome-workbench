# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build environment store: unpack, finalize, freeze, and never twice.

Every package in here is synthetic and tiny — a few files, a wheel set of
empty zip archives — because what is under test is the *sequence*, not
the content: which directory an entry lands in, what happens when two
builds want it at once, what is left behind when one is interrupted, and
that the result cannot be written to afterwards. One test creates a real
virtual environment from a real (if trivial) wheel, because "offline,
from the bundled wheels" is the one step whose failure mode is a
subprocess and not a branch in this module.

The real published packages are not exercised here. That is
``test_buildenvstore_real.py``, which needs the network and several
gigabytes of disk and skips unless the archives have been put where it
can find them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import zstandard
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import buildenvstore as store
from mcuhome.workbench.api import (
    OFFICIAL_BASE_DOMAIN,
    BuildOptions,
    Project,
    RegistrySettings,
    provision_environment,
)
from mcuhome.workbench.packageregistry import host_platform

VERSION = "1.2.3"
TOOLS = f"mcuhome-build-tools_{host_platform()}"
WORKSPACE = "mcuhome-build-workspace"

#: The wheel tag of the interpreter running this suite. A synthetic tools
#: package is built for it, so that the interpreter check passes for
#: every test that is not about the interpreter check.
THIS_PYTHON = f"cp{sys.version_info[0]}{sys.version_info[1]}"
OTHER_PYTHON = f"cp{sys.version_info[0]}{sys.version_info[1] + 1}"

#: The real function, kept before the fixture below replaces it: the one
#: test that creates a real virtual environment puts it back.
REAL_RUN = store._run


# --------------------------------------------------------------------------
# Synthetic packages
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """A symlink member, by its target."""

    target: str


def build_package(
    members: dict[str, bytes | Link], *, executable: frozenset[str] = frozenset()
) -> bytes:
    """A ``.tar.zst`` of *members*, the way the package builder writes one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, content in sorted(members.items()):
            if isinstance(content, Link):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = content.target
                tar.addfile(info)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name in executable else 0o644
            tar.addfile(info, io.BytesIO(content))
    return zstandard.ZstdCompressor(level=1).compress(buffer.getvalue())


def wheel_name(name: str, *, python: str = "py3", abi: str = "none", platform: str = "any") -> str:
    return f"{name}-1.0-{python}-{abi}-{platform}.whl"


def empty_wheel() -> bytes:
    """Enough of a zip to be a file. Nothing installs these."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("placeholder", b"")
    return buffer.getvalue()


def real_wheel(name: str = "mcuhome_provisioning_probe") -> tuple[str, bytes]:
    """A wheel pip actually installs — the smallest one there is."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}.py", "VALUE = 1\n")
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return f"{name}-1.0-py3-none-any.whl", buffer.getvalue()


def tools_members(*, python: str = THIS_PYTHON, wheels: dict[str, bytes] | None = None) -> dict:
    """A tools package: its own statement, the entry point, a wheel set."""
    members: dict[str, bytes | Link] = {
        store.TOOLS_MANIFEST: json.dumps(
            {"package": TOOLS, "version": VERSION, "entry-point": "bin/build-environment-entry"}
        ).encode(),
        "bin/build-environment-entry": b"#!/bin/sh\nexit 0\n",
        "cmake/bin/cmake": b"#!/bin/sh\nexit 0\n",
        # The compiler driver every toolchain reaches through a link.
        "zephyr-sdk-1.0.1/gnu/bin/cc": Link("gcc"),
        "zephyr-sdk-1.0.1/gnu/bin/gcc": b"#!/bin/sh\nexit 0\n",
    }
    for filename, content in (
        wheels
        or {
            wheel_name("pure"): empty_wheel(),
            wheel_name("compiled", python=python, abi=python, platform="linux_x86_64"): (
                empty_wheel()
            ),
        }
    ).items():
        members[f"{store.WHEELS_DIR}/{filename}"] = content
    return members


def workspace_members(*, zephyr_base: str | None = "zephyr") -> dict:
    """A workspace package: its statement, and west's configuration in it."""
    config = "[manifest]\npath = mcuhome-sdk\nfile = west.yml\n"
    if zephyr_base is not None:
        config += f"\n[zephyr]\nbase = {zephyr_base}\n"
    return {
        store.WORKSPACE_MANIFEST: json.dumps(
            {"package": WORKSPACE, "version": VERSION, "workspace": "workspace"}
        ).encode(),
        "workspace/.west/config": config.encode(),
        "workspace/zephyr/VERSION": b"VERSION_MAJOR = 4\n",
        # A source tree that links its own subdirectories, like CHIP does.
        "workspace/zephyr/link": Link("VERSION"),
    }


def put_source(directory: Path, name: str, members: dict) -> str:
    """One package in an operator source directory. Returns its sha256."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{name}-{VERSION}.tar.zst"
    archive.write_bytes(
        build_package(members, executable=frozenset({"bin/build-environment-entry"}))
    )
    return sha256_file(archive)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home")}


@pytest.fixture
def store_dir(tmp_path) -> Path:
    return tmp_path / "store"


@pytest.fixture(autouse=True)
def runs(monkeypatch) -> list[list[str]]:
    """No test creates a real virtual environment unless it is about one.

    Creating one costs a second and a pip run, and every test here but
    that one is about what happens around it. The stub leaves behind what
    the real thing leaves behind — a ``venv/bin/python`` — so freezing and
    the entry's shape are still tested against a realistic tree.
    """
    calls: list[list[str]] = []

    def fake(argv, *, what, on_line):
        del what, on_line
        calls.append([str(part) for part in argv])
        if "venv" in argv:
            interpreter = Path(argv[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
            interpreter.chmod(0o755)

    monkeypatch.setattr(store, "_run", fake)
    return calls


@pytest.fixture
def tools(tmp_path, env, store_dir):
    """Everything a call to :func:`provision` for the tools package needs."""

    def make(*, members: dict | None = None, **overrides):
        sources = tmp_path / "sources"
        sha256 = put_source(sources, TOOLS, members if members is not None else tools_members())
        arguments = {
            "kind": store.KIND_TOOLS,
            "name": TOOLS,
            "version": VERSION,
            "sha256": sha256,
            "env": env,
            "sources": [sources],
            "store": store_dir,
        }
        return {**arguments, **overrides}

    return make


@pytest.fixture
def workspace(tmp_path, env, store_dir):
    def make(*, members: dict | None = None, **overrides):
        sources = tmp_path / "sources"
        sha256 = put_source(
            sources, WORKSPACE, members if members is not None else workspace_members()
        )
        arguments = {
            "kind": store.KIND_WORKSPACE,
            "name": WORKSPACE,
            "version": VERSION,
            "sha256": sha256,
            "env": env,
            "sources": [sources],
            "store": store_dir,
        }
        return {**arguments, **overrides}

    return make


# --------------------------------------------------------------------------
# Where the store is
# --------------------------------------------------------------------------


def test_the_store_is_the_users_cache_home(tmp_path) -> None:
    """The naming scheme's path, resolved from the environment it is given."""
    assert store.store_root({"HOME": str(tmp_path)}) == (
        tmp_path / ".cache" / "mcuhome" / "build-environments"
    )
    moved = store.store_root({"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "elsewhere")})
    assert moved == tmp_path / "elsewhere" / "mcuhome" / "build-environments"


def test_the_store_location_can_be_overridden(tmp_path) -> None:
    """A volume with room for it, named by the caller. The tilde form is
    resolved against the stated environment like every other user path."""
    assert store.store_root({"HOME": str(tmp_path)}, override="/srv/environments") == Path(
        "/srv/environments"
    )
    assert store.store_root({"HOME": str(tmp_path)}, override="~/envs") == tmp_path / "envs"


def test_an_entry_is_named_for_its_package_and_version(tmp_path) -> None:
    assert store.entry_directory(tmp_path, TOOLS, VERSION) == tmp_path / f"{TOOLS}-{VERSION}"


# --------------------------------------------------------------------------
# The extraction bounds
# --------------------------------------------------------------------------

#: What MCUHome's own packages unpack to, measured on the published
#: 0.1.10.dev1 set. The bounds are sized against these, so the numbers are
#: written down where a change to either has to face the other.
MEASURED = {
    store.KIND_WORKSPACE: 1_640_427_520,
    store.KIND_TOOLS: 997_017_600,
    store.KIND_SDK: 1_249_280,
}


def test_every_kind_is_bounded_far_above_what_it_really_unpacks_to() -> None:
    """Ten times the real size at least: the bound is protection against a
    decompression bomb, not a budget — a workspace with more modules or a
    tools package with several toolchains is a legitimately larger thing."""
    for kind, measured in MEASURED.items():
        assert store.extraction_bound(kind) >= 10 * measured, kind


def test_a_kind_nobody_bounded_gets_the_smallest_bound() -> None:
    assert store.extraction_bound("something-else") == store.DEFAULT_BOUND


def test_a_package_over_its_bound_is_refused_and_leaves_nothing(tools, store_dir) -> None:
    arguments = tools(max_bytes=1024)
    with pytest.raises(BuildError) as caught:
        store.provision(**arguments)
    assert "unpacks to more than 1024 bytes" in caught.value.message
    assert not store.entry_directory(store_dir, TOOLS, VERSION).exists()
    assert list(store_dir.glob(".staging-*")) == []


# --------------------------------------------------------------------------
# One provisioning
# --------------------------------------------------------------------------


def test_a_package_is_unpacked_finalized_and_frozen(tools, store_dir, runs) -> None:
    entry = store.provision(**tools())
    assert entry.path == store.entry_directory(store_dir, TOOLS, VERSION)
    assert (entry.path / store.TOOLS_MANIFEST).is_file()
    # The entry point keeps the executable bit — a build spawns it.
    assert os.access(entry.path / "bin" / "build-environment-entry", os.X_OK)
    # The toolchain's links are there, and still links.
    assert (entry.path / "zephyr-sdk-1.0.1" / "gnu" / "bin" / "cc").is_symlink()
    # The virtual environment was created in place, from the bundled
    # wheels and from nothing else.
    created, installed = runs
    assert created[1:3] == ["-m", "venv"]
    # At its final path, because a virtual environment carries that path
    # in the shebang of every console script it installs.
    assert created[-1] == str(entry.path / store.VENV_DIR)
    assert "--no-index" in installed
    assert str(entry.path / store.WHEELS_DIR) in installed


def test_the_entry_is_read_only_afterwards(tools, store_dir) -> None:
    """Several builds share one entry while they run, so nothing may write
    into it — a build that tries fails instead of corrupting a neighbour."""
    entry = store.provision(**tools())
    assert not os.access(entry.path, os.W_OK)
    for path in entry.path.rglob("*"):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert not mode & 0o222, path
    with pytest.raises(PermissionError):
        (entry.path / store.TOOLS_MANIFEST).write_text("no", encoding="utf-8")


def test_the_marker_is_written_last_and_says_what_the_tree_is(
    tools, store_dir, monkeypatch
) -> None:
    """Last, because it is what makes an entry an entry: no marker, no
    environment, whatever else is lying in the directory."""
    seen: list[bool] = []
    real_freeze = store._freeze

    def watch(tree: Path) -> None:
        seen.append((tree / store.MARKER_FILE).exists())
        real_freeze(tree)

    monkeypatch.setattr(store, "_freeze", watch)
    arguments = tools()
    entry = store.provision(**arguments)
    assert seen == [False]
    document = json.loads(entry.marker.read_text(encoding="utf-8"))
    assert document["package"] == TOOLS
    assert document["version"] == VERSION
    assert document["sha256"] == arguments["sha256"]
    assert document["kind"] == store.KIND_TOOLS
    assert store.provisioned(entry.path) == entry


def test_the_unpacking_happens_beside_the_entry(tools, store_dir, monkeypatch) -> None:
    """One rename publishes the unpacked tree, so no build ever sees a
    half-unpacked one; what is on the entry's path while the finalization
    runs is not an environment yet, because the marker is not there."""
    entry = store.entry_directory(store_dir, TOOLS, VERSION)
    during: list[tuple[bool, bool]] = []
    real = store.acquire_package

    def watch(**keywords):
        during.append((entry.exists(), Path(keywords["into"]).name.startswith(".staging-")))
        return real(**keywords)

    monkeypatch.setattr(store, "acquire_package", watch)
    finalizing = []
    real_finalize = store._finalize

    def watch_finalize(tree: Path, **keywords):
        finalizing.append(store.provisioned(tree))
        real_finalize(tree, **keywords)

    monkeypatch.setattr(store, "_finalize", watch_finalize)
    store.provision(**tools())
    assert during == [(False, True)]
    assert finalizing == [None]
    assert entry.is_dir()
    assert list(store_dir.glob(".staging-*")) == []


def test_a_second_provisioning_is_a_no_op(tools, store_dir, monkeypatch) -> None:
    first = store.provision(**tools())

    def refuse(**keywords):
        raise AssertionError("a provisioned package was acquired a second time")

    monkeypatch.setattr(store, "acquire_package", refuse)
    again = store.provision(**tools())
    assert again == first


def test_two_provisionings_at_once_produce_one_entry(tools, store_dir, monkeypatch) -> None:
    """The second one waits for the first and then finds its result — a
    build environment is minutes of unpacking, not something to do twice."""
    real = store.acquire_package
    started = threading.Event()
    acquisitions: list[str] = []

    def slow(**keywords):
        acquisitions.append(keywords["name"])
        started.set()
        # Long enough that the other thread is inside `provision` and
        # waiting on the lock rather than merely about to call it.
        threading.Event().wait(0.2)
        return real(**keywords)

    monkeypatch.setattr(store, "acquire_package", slow)
    arguments = tools()
    results: list[store.StoreEntry] = []

    def run() -> None:
        results.append(store.provision(**arguments))

    threads = [threading.Thread(target=run) for _ in range(2)]
    threads[0].start()
    started.wait(5)
    threads[1].start()
    for thread in threads:
        thread.join(30)

    assert acquisitions == [TOOLS]
    assert len(results) == 2
    assert results[0] == results[1]
    assert [path.name for path in store_dir.iterdir() if path.is_dir() and path.name[0] != "."] == [
        f"{TOOLS}-{VERSION}"
    ]


# --------------------------------------------------------------------------
# Interruptions
# --------------------------------------------------------------------------


def test_an_interrupted_provisioning_leaves_no_entry(tools, store_dir, monkeypatch) -> None:
    def explode(tree: Path, **keywords):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(store, "_create_venv", explode)
    with pytest.raises(RuntimeError):
        store.provision(**tools())
    assert not store.entry_directory(store_dir, TOOLS, VERSION).exists()
    assert list(store_dir.glob(".staging-*")) == []


def test_a_staging_directory_left_by_a_killed_run_is_discarded(tools, store_dir) -> None:
    """A process that was killed rather than raised leaves its staging
    directory behind — frozen, possibly, and beside gigabytes of spool."""
    store_dir.mkdir(parents=True)
    staging = store_dir / f".staging-{TOOLS}-{VERSION}"
    (staging / "half").mkdir(parents=True)
    (staging / "half" / "unpacked").write_text("x", encoding="utf-8")
    (staging / "half" / "unpacked").chmod(0o400)
    (staging / "half").chmod(0o500)
    spool = store_dir / f".staging-{TOOLS}-{VERSION}.tar"
    spool.write_bytes(b"leftover")

    entry = store.provision(**tools())

    assert entry.path.is_dir()
    assert not staging.exists()
    assert not spool.exists()


def test_an_unmarked_entry_is_thrown_away_and_unpacked_again(tools, store_dir) -> None:
    """What a killed process leaves on the entry's path: a tree with no
    marker. It is not an environment to anybody, so the next provisioning
    replaces it rather than adding to it."""
    entry = store.entry_directory(store_dir, TOOLS, VERSION)
    (entry / "leftover").mkdir(parents=True)
    (entry / "leftover" / "half").write_text("x", encoding="utf-8")
    (entry / "leftover" / "half").chmod(0o400)
    (entry / "leftover").chmod(0o500)
    assert store.provisioned(entry) is None

    result = store.provision(**tools())

    assert result.path == entry
    assert not (entry / "leftover").exists()
    assert (entry / store.TOOLS_MANIFEST).is_file()


def test_a_different_build_of_the_same_version_is_refused(tools, store_dir) -> None:
    """Same name, same version, other bytes — answering with what is there
    would build something other than what the device pinned."""
    store.provision(**tools())
    with pytest.raises(BuildError) as caught:
        store.provision(**tools(sha256="0" * 64))
    assert "holds a different build" in caught.value.message


# --------------------------------------------------------------------------
# The host interpreter (the wheel set decides)
# --------------------------------------------------------------------------


def test_the_wheel_set_states_the_python_it_needs() -> None:
    assert store.PythonRequirement(exact=(3, 13)).described() == "3.13"
    assert store.PythonRequirement(minimum=(3, 11)).described() == "3.11 or newer"
    assert store.PythonRequirement().satisfied_by((3, 8))


def test_the_requirement_is_read_off_the_wheels(tmp_path) -> None:
    """A compiled wheel names one minor version exactly, a stable-ABI wheel
    names the oldest it works on, and a pure wheel names nothing."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name in (
        wheel_name("pure"),
        wheel_name("stable", python="cp311", abi="abi3", platform="linux_x86_64"),
        wheel_name("compiled", python="cp313", abi="cp313", platform="linux_x86_64"),
    ):
        (wheels / name).write_bytes(empty_wheel())
    assert store.required_python(wheels) == store.PythonRequirement(
        exact=(3, 13),
        evidence=wheel_name("compiled", python="cp313", abi="cp313", platform="linux_x86_64"),
    )


def test_a_stable_abi_wheel_set_states_a_floor(tmp_path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / wheel_name("pure")).write_bytes(empty_wheel())
    (
        wheels / wheel_name("stable", python="cp311", abi="abi3", platform="linux_x86_64")
    ).write_bytes(empty_wheel())
    requirement = store.required_python(wheels)
    assert requirement.exact is None
    assert requirement.minimum == (3, 11)
    assert requirement.satisfied_by((3, 13))
    assert not requirement.satisfied_by((3, 10))


def test_a_wheel_set_built_for_two_pythons_is_refused(tmp_path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for python in ("cp312", "cp313"):
        (wheels / wheel_name(python, python=python, abi=python, platform="linux")).write_bytes(
            empty_wheel()
        )
    with pytest.raises(BuildError) as caught:
        store.required_python(wheels)
    assert "different Python versions" in caught.value.message


def test_a_host_python_the_wheels_cannot_install_into_is_refused(tools, store_dir, runs) -> None:
    """Before anything is created: pip on the wrong minor version would
    find no candidate at all, and building the wheels from source needs the
    compiler, headers and network a packaged environment exists to avoid."""
    members = tools_members(
        wheels={
            wheel_name("pure"): empty_wheel(),
            wheel_name("compiled", python=OTHER_PYTHON, abi=OTHER_PYTHON, platform="linux"): (
                empty_wheel()
            ),
        }
    )
    with pytest.raises(BuildError) as caught:
        store.provision(**tools(members=members))
    wanted = f"{sys.version_info[0]}.{sys.version_info[1] + 1}"
    assert f"needs Python {wanted}" in caught.value.message
    assert "Debian stable" in caught.value.hint
    assert runs == []
    assert not store.entry_directory(store_dir, TOOLS, VERSION).exists()


def test_a_tools_package_without_wheels_is_refused(tools, store_dir) -> None:
    members = tools_members()
    for name in [name for name in members if name.startswith(f"{store.WHEELS_DIR}/")]:
        del members[name]
    with pytest.raises(BuildError) as caught:
        store.provision(**tools(members=members))
    assert "carries no Python packages" in caught.value.message


def test_the_virtual_environment_is_created_from_the_bundled_wheels(
    tools, store_dir, monkeypatch
) -> None:
    """The one test that runs the real thing: a venv at its final location,
    populated with ``--no-index`` from a wheel that is really in the
    package. Everything about this step is a subprocess, so a stub would
    prove nothing about it."""
    monkeypatch.setattr(store, "_run", REAL_RUN)
    filename, content = real_wheel()
    entry = store.provision(**tools(members=tools_members(wheels={filename: content})))
    python = entry.path / store.VENV_DIR / "bin" / "python"
    assert python.exists()
    installed = list((entry.path / store.VENV_DIR).rglob("mcuhome_provisioning_probe.py"))
    assert installed, "the bundled wheel was not installed into the environment"
    assert not os.access(entry.path / store.VENV_DIR, os.W_OK)
    # And it still runs once it is frozen — a read-only environment is
    # the normal state of one, not a broken one.
    ran = subprocess.run(
        [str(python), "-c", "import mcuhome_provisioning_probe as p; print(p.VALUE)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ran.stdout.strip() == "1"


# --------------------------------------------------------------------------
# The workspace package: west's configuration, git's ownership check
# --------------------------------------------------------------------------


def test_a_workspace_package_is_checked_not_written(workspace, store_dir) -> None:
    """Byte for byte what the package carried: the check reads west's
    configuration and never completes it, because a store is shared and
    frozen and there is no moment at which writing into one is safe."""
    written = workspace_members()["workspace/.west/config"]
    entry = store.provision(**workspace())
    config = entry.path / "workspace" / ".west" / "config"
    assert config.read_bytes() == written
    assert not os.access(config, os.W_OK)


def test_a_workspace_without_zephyr_base_is_refused(workspace, store_dir) -> None:
    """West's Zephyr extension would write it on first use, and there is
    nowhere to write in a frozen store — so the package has to carry it."""
    with pytest.raises(BuildError) as caught:
        store.provision(**workspace(members=workspace_members(zephyr_base=None)))
    assert "no zephyr.base" in caught.value.message
    assert not store.entry_directory(store_dir, WORKSPACE, VERSION).exists()


def test_the_workspace_entry_carries_the_ownership_exemption(workspace, store_dir) -> None:
    """Scoped to the workspace and written into the entry: a per-user git
    configuration would outlive the store and grow a line per entry."""
    entry = store.provision(**workspace())
    configuration = store.git_config_file(entry).read_text(encoding="utf-8")
    assert "[safe]" in configuration
    assert f"directory = {entry.path / 'workspace'}/*" in configuration


def test_a_package_that_is_not_what_it_was_acquired_as_is_refused(workspace, store_dir) -> None:
    members = workspace_members()
    del members[store.WORKSPACE_MANIFEST]
    with pytest.raises(BuildError) as caught:
        store.provision(**workspace(members=members))
    assert store.WORKSPACE_MANIFEST in caught.value.message


def test_the_workspaces_own_links_are_kept(workspace, store_dir) -> None:
    entry = store.provision(**workspace())
    assert (entry.path / "workspace" / "zephyr" / "link").is_symlink()


def test_a_workspace_that_puts_itself_outside_the_package_is_refused(workspace, store_dir) -> None:
    """The package says where its workspace is, and the answer ends up in
    a git configuration and in what a build is handed."""
    members = workspace_members()
    members[store.WORKSPACE_MANIFEST] = json.dumps(
        {"package": WORKSPACE, "version": VERSION, "workspace": "/etc"}
    ).encode()
    with pytest.raises(BuildError) as caught:
        store.provision(**workspace(members=members))
    assert "not inside the package" in caught.value.message


# --------------------------------------------------------------------------
# The paths around the happy one
# --------------------------------------------------------------------------


def test_a_package_with_nothing_to_finalize_is_only_unpacked_and_frozen(
    tmp_path, env, store_dir, runs
) -> None:
    """The SDK is delivered to a build rather than assembled into an
    environment, so a store entry for one has no virtual environment and
    no west configuration — and is frozen like every other entry."""
    sources = tmp_path / "sources"
    sha256 = put_source(sources, "mcuhome-sdk", {"mcuhome-sdk.json": b'{"sdk": 1}'})
    entry = store.provision(
        kind=store.KIND_SDK,
        name="mcuhome-sdk",
        version=VERSION,
        sha256=sha256,
        env=env,
        sources=[sources],
        store=store_dir,
    )
    assert (entry.path / "mcuhome-sdk.json").is_file()
    assert not (entry.path / store.VENV_DIR).exists()
    assert runs == []
    assert not os.access(entry.path, os.W_OK)


def test_a_family_name_is_refused_rather_than_stored_under_it(
    tools, store_dir, monkeypatch
) -> None:
    """A package published per platform resolves to one platform's package,
    and the entry has to be named for that one — the store path is computed
    before anything is fetched, and on the next machine the family name
    would stand for other bytes."""
    real = store.acquire_package

    def resolved(**keywords):
        acquired = real(**keywords)
        return replace(acquired, name=f"{keywords['name']}_other-arch")

    monkeypatch.setattr(store, "acquire_package", resolved)
    with pytest.raises(BuildError) as caught:
        store.provision(**tools())
    assert "published as a set of packages" in caught.value.message
    assert not store.entry_directory(store_dir, TOOLS, VERSION).exists()


def test_an_entry_of_another_kind_is_refused(tools, store_dir) -> None:
    store.provision(**tools())
    with pytest.raises(BuildError) as caught:
        store.provision(**tools(kind=store.KIND_WORKSPACE))
    assert "was unpacked as a" in caught.value.message


def test_a_marker_that_says_nothing_usable_is_not_an_entry(tmp_path) -> None:
    entry = tmp_path / "entry"
    entry.mkdir()
    assert store.provisioned(entry) is None
    (entry / store.MARKER_FILE).write_text("[]", encoding="utf-8")
    assert store.provisioned(entry) is None
    (entry / store.MARKER_FILE).write_text('{"package": "x"}', encoding="utf-8")
    assert store.provisioned(entry) is None
    (entry / store.MARKER_FILE).write_text("not json at all", encoding="utf-8")
    assert store.provisioned(entry) is None


def test_a_provisioning_step_that_fails_says_what_it_was_doing() -> None:
    """The last lines of the failing command, not a traceback: what breaks
    here is pip, and what the user needs is what pip said."""
    with pytest.raises(BuildError) as caught:
        REAL_RUN(
            [
                sys.executable,
                "-c",
                "import sys; print('no wheel here', file=sys.stderr); sys.exit(2)",
            ],
            what="install the Python packages of a build environment",
            on_line=None,
        )
    assert "could not install the Python packages" in caught.value.message
    assert "no wheel here" in caught.value.message


def test_a_program_that_is_not_there_at_all_says_so() -> None:
    with pytest.raises(BuildError) as caught:
        REAL_RUN(["/nonexistent/python"], what="create the Python environment", on_line=None)
    assert "did not run" in caught.value.message


def test_something_that_is_not_an_interpreter_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        store._interpreter_version("/bin/true")
    assert "did not answer as a Python interpreter" in caught.value.message


# --------------------------------------------------------------------------
# Provisioning without a build
# --------------------------------------------------------------------------
#
# `provision_environment` is the store's own entry point: the same
# sequence a build runs, for a caller that has no build context — a CI
# job that has just produced a package, a person warming a machine's
# store. What the tests below are about is what it does *before*
# `provision`: which package the caller named, of which kind, and where
# its bytes are looked for.


def put_package(directory: Path, name: str, version: str, members: dict) -> str:
    """One package file in *directory*. Returns its sha256."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{name}-{version}.tar.zst"
    archive.write_bytes(
        build_package(members, executable=frozenset({"bin/build-environment-entry"}))
    )
    return sha256_file(archive)


def write_index(directory: Path, *published: tuple[str, str, str]) -> None:
    """The ``index.json`` that names *published* — ``(name, version, sha256)``."""
    packages: dict[str, dict[str, dict]] = {}
    for name, version, sha256 in published:
        archive = directory / f"{name}-{version}.tar.zst"
        packages.setdefault(name, {})[version] = {
            "file": archive.name,
            "sha256": sha256,
            "size": archive.stat().st_size,
        }
    (directory / "index.json").write_text(json.dumps({"packages": packages}), "utf-8")


def write_family_index(
    directory: Path, family: str, member: str, version: str, sha256: str
) -> None:
    """An index in which *family* stands for *member* on this host.

    What a real registry publishes for the build tools: one concrete
    package per architecture and one meta entry naming them, whose own
    hash is computed over the members it points at. Spelled out here
    rather than imported from the packaging side, so the index under test
    is one a second implementation wrote.
    """
    from mcuhome.packagetool.verify import canonical_json

    archive = directory / f"{member}-{version}.tar.zst"
    packages = {
        member: {version: {"file": archive.name, "sha256": sha256, "size": archive.stat().st_size}}
    }
    expanded = {"arch": {host_platform(): {"name": member, "sha256": sha256}}}
    packages[family] = {
        version: {
            "meta": {"arch": {host_platform(): member}},
            "sha256": hashlib.sha256(canonical_json(expanded)).hexdigest(),
        }
    }
    (directory / "index.json").write_text(json.dumps({"packages": packages}), "utf-8")


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under *root* with its mode, size and modification time."""
    found = {".": _entry_facts(root)}
    for path in sorted(root.rglob("*")):
        found[str(path.relative_to(root))] = _entry_facts(path)
    return found


def _entry_facts(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    return info.st_mode, info.st_size, info.st_mtime_ns


@pytest.fixture
def options(store_dir) -> BuildOptions:
    """A machine whose store is the test's own, and nothing else configured."""
    return BuildOptions(env_store=store_dir)


@pytest.fixture
def published(tmp_path):
    """A source directory holding the workspace package, with its index."""

    def make(*, name: str = WORKSPACE, version: str = VERSION, members: dict | None = None):
        directory = tmp_path / "published"
        sha256 = put_package(
            directory, name, version, members if members is not None else workspace_members()
        )
        write_index(directory, (name, version, sha256))
        return directory, sha256

    return make


def test_a_package_file_is_provisioned_by_the_hash_computed_from_it(
    tmp_path, options, env, store_dir
) -> None:
    """A caller who points at bytes gets those bytes: there is no pin to
    check them against, so the hash is computed from the file and is what
    the entry records."""
    directory = tmp_path / "built"
    sha256 = put_package(directory, WORKSPACE, VERSION, workspace_members())

    entry = provision_environment(
        directory / f"{WORKSPACE}-{VERSION}.tar.zst", options=options, env=env
    )

    assert entry == store.StoreEntry(
        kind=store.KIND_WORKSPACE,
        name=WORKSPACE,
        version=VERSION,
        sha256=sha256,
        path=store.entry_directory(store_dir, WORKSPACE, VERSION),
    )
    # The whole sequence ran, not just the unpacking: finalized (the
    # ownership exemption is written for the workspace kind), frozen, and
    # marked as the last thing.
    assert (entry.path / store.GIT_CONFIG_FILE).is_file()
    assert stat.S_IMODE((entry.path / store.WORKSPACE_MANIFEST).lstat().st_mode) == 0o400
    assert store.provisioned(entry.path) == entry


def test_a_tools_package_file_is_finalized_like_one(tmp_path, options, env, runs) -> None:
    """Which kind a package is comes from its name, and the kind decides
    what is made of the tree: the tools package gets its virtual
    environment, the workspace package does not."""
    directory = tmp_path / "built"
    put_package(directory, TOOLS, VERSION, tools_members())

    entry = provision_environment(
        directory / f"{TOOLS}-{VERSION}.tar.zst", options=options, env=env
    )

    assert entry.kind == store.KIND_TOOLS
    created, _installed = runs
    assert created[1:3] == ["-m", "venv"]
    assert created[-1] == str(entry.path / store.VENV_DIR)


def test_a_package_name_is_resolved_against_a_source_directory(
    published, options, env, store_dir
) -> None:
    """The other half: a name, and the directories it is looked for in."""
    directory, sha256 = published()

    entry = provision_environment(WORKSPACE, options=options, env=env, sources=[directory])

    assert (entry.name, entry.version, entry.sha256) == (WORKSPACE, VERSION, sha256)
    assert entry.path == store.entry_directory(store_dir, WORKSPACE, VERSION)


def test_a_name_takes_the_newest_version_a_constraint_admits(tmp_path, options, env) -> None:
    """A bare name is the newest published version; a constraint narrows
    which of them that is."""
    directory = tmp_path / "published"
    older = put_package(directory, WORKSPACE, "1.2.3", workspace_members())
    newer = put_package(directory, WORKSPACE, "1.3.0", workspace_members())
    write_index(directory, (WORKSPACE, "1.2.3", older), (WORKSPACE, "1.3.0", newer))

    assert (
        provision_environment(WORKSPACE, options=options, env=env, sources=[directory]).version
        == "1.3.0"
    )
    narrowed = provision_environment(
        f"{WORKSPACE}:~=1.2.0", options=options, env=env, sources=[directory]
    )
    assert narrowed.version == "1.2.3"


def test_a_kind_is_looked_for_under_its_own_key_and_no_other(published, env, store_dir) -> None:
    """The configured directories are the ones of that kind of package. A
    directory holding the build workspace is not a statement about where
    SDK packages live, and it is not searched for one either."""
    directory, _sha256 = published()
    elsewhere = BuildOptions(env_store=store_dir, sdk_sources=(directory,))

    with pytest.raises(BuildError) as caught:
        provision_environment(WORKSPACE, options=elsewhere, env=env)
    assert "No package source offers" in caught.value.message

    here = BuildOptions(env_store=store_dir, workspace_sources=(directory,))
    assert provision_environment(WORKSPACE, options=here, env=env).version == VERSION


def test_a_second_provisioning_touches_nothing(published, options, env, store_dir, monkeypatch):
    """The entry is shared by every build that pins it, including builds
    running right now: a second provisioning reads the marker and answers,
    and does not so much as take the store's lock."""
    directory, _sha256 = published()
    first = provision_environment(WORKSPACE, options=options, env=env, sources=[directory])
    before = tree_snapshot(first.path)
    # What the first provisioning left behind, removed so that the second
    # one taking the lock would be visible rather than invisible.
    shutil.rmtree(store_dir / ".locks")

    def refuse(**keywords):
        raise AssertionError("a provisioned package was acquired a second time")

    monkeypatch.setattr(store, "acquire_package", refuse)
    again = provision_environment(WORKSPACE, options=options, env=env, sources=[directory])

    assert again == first
    assert tree_snapshot(first.path) == before
    assert not (store_dir / ".locks").exists()
    assert list(store_dir.glob(".staging-*")) == []


def test_a_hash_the_bytes_do_not_have_is_refused(tmp_path, options, env, store_dir) -> None:
    """A reference stating a version and a hash decides everything and
    reads no index — so the file it names has to hash to what it pins."""
    directory = tmp_path / "built"
    put_package(directory, WORKSPACE, VERSION, workspace_members())

    with pytest.raises(BuildError) as caught:
        provision_environment(
            f"{WORKSPACE}:{VERSION}@sha256:{'0' * 64}",
            options=options,
            env=env,
            sources=[directory],
        )
    assert "hashes to" in caught.value.message
    assert not store.entry_directory(store_dir, WORKSPACE, VERSION).exists()


def test_a_hash_the_index_contradicts_is_refused(published, options, env, store_dir) -> None:
    """The same version naming different bytes here than where the hash was
    taken from is the one thing that is never shopped around for."""
    directory, sha256 = published()

    with pytest.raises(BuildError) as caught:
        provision_environment(
            f"{WORKSPACE}@sha256:{'0' * 64}", options=options, env=env, sources=[directory]
        )
    assert sha256 in caught.value.message
    assert "0" * 64 in caught.value.message
    assert not store.entry_directory(store_dir, WORKSPACE, VERSION).exists()


def test_a_file_for_another_architecture_reaches_the_package_searchs_own_check(
    tmp_path, options, env
) -> None:
    """The refusal is not this wrapper's: deriving the name off the file
    hands it to the package search, whose architecture check
    (:func:`~mcuhome.workbench.packageregistry.check_platform`) refuses a
    suffix that is not this host's before a byte is read. Asserted here
    because the file form is the one path on which nothing else would
    catch it — no index resolved this name for the platform."""
    other = "mcuhome-build-tools_haiku-m68k"
    directory = tmp_path / "built"
    put_package(directory, other, VERSION, tools_members())

    with pytest.raises(BuildError) as caught:
        provision_environment(directory / f"{other}-{VERSION}.tar.zst", options=options, env=env)
    assert "is built for haiku-m68k" in caught.value.message


def test_a_name_that_is_no_build_environment_package_is_refused(options, env) -> None:
    """Which kind a package is decides how much it may unpack to and what
    is made of it, and only the name can say before anything is read."""
    with pytest.raises(BuildError) as caught:
        provision_environment("something-else", options=options, env=env)
    assert "does not know what kind of build environment package" in caught.value.message
    assert WORKSPACE in caught.value.hint


def test_a_file_that_is_not_there_or_not_a_package_name_is_refused(tmp_path, options, env) -> None:
    """A path is the caller's own statement that it named a file, so a
    mistyped one is answered as the missing file it is."""
    with pytest.raises(BuildError) as missing:
        provision_environment(tmp_path / "absent.tar.zst", options=options, env=env)
    assert "There is no package file at" in missing.value.message

    odd = tmp_path / "package.tar.zst"
    odd.write_bytes(b"")
    with pytest.raises(BuildError) as unnamed:
        provision_environment(odd, options=options, env=env)
    assert "not named like a build environment package" in unnamed.value.message


def test_a_reference_naming_a_registry_of_its_own_is_refused(options, env) -> None:
    """Which registry is asked is the machine's, not the reference's."""
    with pytest.raises(BuildError) as caught:
        provision_environment(
            f"packages.example.org/build-workspace/{WORKSPACE}", options=options, env=env
        )
    assert "names a registry" in caught.value.message


def test_a_reference_naming_a_shelf_is_refused(published, options, env) -> None:
    """A device file spells the shelf in front of the package, and that is
    the value somebody copies. Reading it here and then asking the kind's
    own shelf anyway would resolve the package somewhere other than where
    it was told to, and silently — so the form is refused, and the refusal
    names the grammar this call does take."""
    directory, _sha256 = published()
    for named in (f"build-workspace/{WORKSPACE}", f"house-workspaces/{WORKSPACE}:~=1.2"):
        with pytest.raises(BuildError) as caught:
            provision_environment(named, options=options, env=env, sources=[directory])
        assert "names the shelf" in caught.value.message
        assert "<package>[:<constraint>][@sha256:" in caught.value.hint


def test_a_family_is_resolved_per_platform_and_never_stored_as_one(
    tmp_path, options, env, store_dir
) -> None:
    """The build tools are published per architecture, and the family name
    is what lets one build context build on hosts of two of them. A store
    entry is one package, so the family is answered by this host's member
    where an index can resolve it — and refused where nothing did: a file
    carries the name it is named, and a reference pinning its own bytes
    asks no index at all."""
    directory = tmp_path / "published"
    sha256 = put_package(directory, TOOLS, VERSION, tools_members())
    write_family_index(directory, "mcuhome-build-tools", TOOLS, VERSION, sha256)

    entry = provision_environment(
        "mcuhome-build-tools", options=options, env=env, sources=[directory]
    )
    assert entry.name == TOOLS
    assert entry.path == store.entry_directory(store_dir, TOOLS, VERSION)

    # A directory with no index — a package that was built a minute ago
    # and is published nowhere. Nothing there can say what a family name
    # stands for, which is exactly why a family name may not arrive as
    # one: unpacked under it, the entry would be the wrong package on the
    # next host to read the store.
    unpublished = tmp_path / "built"
    put_package(unpublished, "mcuhome-build-tools", VERSION, tools_members())
    hand_named = unpublished / f"mcuhome-build-tools-{VERSION}.tar.zst"
    for stated in (hand_named, f"mcuhome-build-tools:{VERSION}@sha256:{sha256_file(hand_named)}"):
        with pytest.raises(BuildError) as caught:
            provision_environment(stated, options=options, env=env, sources=[unpublished])
        assert "a store entry holds one of them" in caught.value.message
        assert "name the family without a file" in caught.value.hint
    assert not store.entry_directory(store_dir, "mcuhome-build-tools", VERSION).exists()


def test_a_reference_that_names_no_package_is_refused(options, env) -> None:
    with pytest.raises(BuildError) as caught:
        provision_environment(":1.2.3", options=options, env=env)
    assert "does not name a build environment package" in caught.value.message


def test_the_registry_is_asked_for_the_shelf_a_package_is_published_on(env, store_dir) -> None:
    """What a tree *is* and where it is published are two statements.

    The kind decides the bound and the finalization and is one of three
    fixed values; the shelf is the device's own word and reaches the
    registry. They are the same on every ordinary build, which is why the
    shelf defaults to the kind — and why a package published elsewhere
    would otherwise be resolved in one place and fetched from another.
    """
    asked: list[str] = []

    class Shelf:
        def index(self, source: str):
            asked.append(source)
            raise BuildError("nothing is served here", hint="this is a test double")

    def provision_from(**overrides):
        with pytest.raises(BuildError):
            store.provision(
                kind=store.KIND_WORKSPACE,
                name=WORKSPACE,
                version=VERSION,
                sha256="0" * 64,
                env=env,
                store=store_dir,
                registry=lambda: Shelf(),
                **overrides,
            )

    provision_from()
    provision_from(source_name="house-workspaces")
    assert asked == [store.KIND_WORKSPACE, "house-workspaces"]


# --------------------------------------------------------------------------
# The registry a project configures
# --------------------------------------------------------------------------


class OpenedRegistry:
    """A registry double that records what it was asked for.

    It stands in for the client `provision_environment` opens from a
    project, so "the project's registry was asked" is checked by what
    this recorded rather than by a network.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    def index(self, source: str):
        self.asked.append(source)
        raise BuildError("nothing is served here", hint="this is a test double")


@pytest.fixture
def opened(monkeypatch):
    """Records every registry `provision_environment` opens for itself."""
    calls: list[dict] = []
    client = OpenedRegistry()

    def open_package_registry(base_domain: str, **arguments):
        calls.append(
            {"base_domain": base_domain, "existed": Path(arguments["into"]).is_dir(), **arguments}
        )
        return lambda: client

    monkeypatch.setattr(store, "open_package_registry", open_package_registry)
    return calls, client


def test_a_project_is_enough_to_reach_the_registry(tmp_path, options, env, opened) -> None:
    """The composition a client would otherwise have to write itself.

    Without this, a caller that wants a package the operator directories
    do not hold has to build a registry client, choose a directory to
    read it into and find the trust anchor — which this package already
    knows how to do for a build.
    """
    calls, client = opened
    project = Project(root=tmp_path / "project", discovered=True)
    configured = RegistrySettings(
        base_domain="packages.mcuhome.org",
        mirrors={"build-workspace": ("https://mirror.example/build-workspace/",)},
    )

    with pytest.raises(BuildError) as caught:
        provision_environment(
            WORKSPACE, options=options, env=env, project=project, registries=(configured,)
        )

    assert "nothing is served here" in str(caught.value)
    assert client.asked == [store.KIND_WORKSPACE], "the opened registry is the one asked"
    assert len(calls) == 1
    assert calls[0]["base_domain"] == OFFICIAL_BASE_DOMAIN
    assert calls[0]["project_root"] == project.root
    assert calls[0]["settings"] == (configured,)
    # The verified documents are read into a directory of this call's
    # own, which exists while the call runs and is gone with it: they are
    # checked on every read and are worth nothing afterwards.
    assert calls[0]["existed"]
    assert not Path(calls[0]["into"]).exists()


def test_a_stated_registry_wins_over_the_project(tmp_path, options, env, opened) -> None:
    """The more explicit of the two: a caller that built one meant it."""
    calls, project_client = opened
    stated = OpenedRegistry()

    with pytest.raises(BuildError):
        provision_environment(
            WORKSPACE,
            options=options,
            env=env,
            registry=lambda: stated,
            project=Project(root=tmp_path / "project", discovered=True),
        )

    assert stated.asked == [store.KIND_WORKSPACE]
    assert calls == [], "nothing was opened beside the registry the caller stated"
    assert project_client.asked == []


def test_a_package_file_opens_no_registry_even_inside_a_project(
    tmp_path, options, env, opened
) -> None:
    """Nothing is looked up, so there is nothing to ask anybody."""
    calls, client = opened
    directory = tmp_path / "built"
    put_package(directory, WORKSPACE, VERSION, workspace_members())

    entry = provision_environment(
        directory / f"{WORKSPACE}-{VERSION}.tar.zst",
        options=options,
        env=env,
        project=Project(root=tmp_path / "project", discovered=True),
    )

    assert entry.name == WORKSPACE
    assert calls == []
    assert client.asked == []


def test_without_a_project_the_directories_are_the_only_source(
    published, options, env, opened
) -> None:
    """The offline case, unchanged: no project, no registry, no network."""
    calls, client = opened
    directory, sha256 = published()

    entry = provision_environment(WORKSPACE, options=options, env=env, sources=[directory])

    assert entry.sha256 == sha256
    assert calls == []
    assert client.asked == []
