# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The store, against the real published build environment packages.

Everything else in the suite runs on synthetic packages of a few
kilobytes, which is right for the sequence and wrong for the content: the
real workspace package carries 124 000 members and 468 symlinks, the real
tools package a wheel set built for one interpreter and a toolchain that
reaches its compiler through a link. Whether provisioning survives *that*
is not something a fake archive can answer.

**This test is skipped unless you put the packages where it can find
them**, because it needs about 3 GB of disk and several minutes, and the
archives are 800 MB to download:

    mkdir -p tests/python/data/real-packages
    cd tests/python/data/real-packages
    base=https://mirror-1.packages.mcuhome.org
    curl -O $base/build-workspace/mcuhome-build-workspace-<version>.tar.zst
    curl -O $base/build-tools/mcuhome-build-tools_<os>-<arch>-<version>.tar.zst

The directory is ignored by git. Any published version does; the test
takes what is there. The archives' own bytes are what it verifies
against — the same hash a pin would carry — so a truncated download fails
here rather than half-way through a build.

It unpacks **beside the archives** rather than into pytest's temporary
directory, and removes that again when it is done. Two reasons, both
learned the hard way: ``/tmp`` is a tmpfs on many machines and this is
gigabytes, and pytest keeps the last three runs' temporary trees, so the
second and third run of the suite would each add another 2 GB before
anything was ever cleaned up.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import zstandard
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import buildenvstore as store
from mcuhome.workbench.packageregistry import host_platform

PACKAGES = Path(__file__).resolve().parent / "data" / "real-packages"

SUFFIX = ".tar.zst"
TOOLS_PREFIX = f"mcuhome-build-tools_{host_platform()}-"
WORKSPACE_PREFIX = "mcuhome-build-workspace-"


def _archive(prefix: str) -> Path | None:
    """The newest archive of that package in the directory, if any."""
    found = sorted(PACKAGES.glob(f"{prefix}*{SUFFIX}")) if PACKAGES.is_dir() else []
    return found[-1] if found else None


TOOLS_ARCHIVE = _archive(TOOLS_PREFIX)
WORKSPACE_ARCHIVE = _archive(WORKSPACE_PREFIX)

pytestmark = pytest.mark.skipif(
    TOOLS_ARCHIVE is None or WORKSPACE_ARCHIVE is None,
    reason=f"no published build environment packages in {PACKAGES} — see this file's docstring",
)


def _version(archive: Path, prefix: str) -> str:
    return archive.name[len(prefix) : -len(SUFFIX)]


@pytest.fixture
def work() -> Iterator[Path]:
    """A scratch directory on the disk the archives are on, cleaned up.

    Frozen entries have no write bit, so the cleanup is the same two steps
    an operator clearing the store performs — which is the procedure the
    README documents, exercised here on a real store.
    """
    directory = Path(tempfile.mkdtemp(prefix="provisioning-", dir=PACKAGES))
    try:
        yield directory
    finally:
        for parent, _directories, _files in os.walk(directory, topdown=True):
            Path(parent).chmod(0o700)
        shutil.rmtree(directory, ignore_errors=True)


def _wheel_requirement(archive: Path, directory: Path) -> store.PythonRequirement:
    """Which interpreter the package's wheel set needs, read off the archive.

    Off the archive rather than off an unpacked entry, because the answer
    decides whether this host can finalize it at all. The names are the
    whole input, so the wheel set is reproduced as empty files in
    *directory* and the production rule answers about those.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as raw:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        with tarfile.open(fileobj=io.BufferedReader(reader), mode="r|") as tar:
            for member in tar:
                name = Path(member.name)
                if name.parent.name == store.WHEELS_DIR and name.suffix == ".whl":
                    (directory / name.name).write_bytes(b"")
    return store.required_python(directory)


def _interpreter_for(requirement: store.PythonRequirement) -> str | None:
    """An interpreter on this host the wheel set installs into, or ``None``."""
    if requirement.satisfied_by((sys.version_info[0], sys.version_info[1])):
        return sys.executable
    if requirement.exact is not None:
        return shutil.which(f"python{requirement.exact[0]}.{requirement.exact[1]}")
    return None


def test_the_published_packages_provision_into_a_working_environment(work) -> None:
    """One run, both packages, everything the profile needs afterwards.

    Deliberately one test and not five: provisioning the real set is
    minutes of work, and every question below is about the same two
    entries.
    """
    assert TOOLS_ARCHIVE is not None and WORKSPACE_ARCHIVE is not None
    environment = {"HOME": str(work / "home")}
    store_dir = work / "store"
    tools_version = _version(TOOLS_ARCHIVE, TOOLS_PREFIX)

    def provision_tools(**overrides) -> store.StoreEntry:
        return store.provision(
            kind=store.KIND_TOOLS,
            name=TOOLS_PREFIX[:-1],
            version=tools_version,
            sha256=sha256_file(TOOLS_ARCHIVE),
            env=environment,
            sources=[PACKAGES],
            store=store_dir,
            **overrides,
        )

    # ------------------------------------------------------------------
    # The source world: 1.5 GB, 124 000 members, hundreds of links
    # ------------------------------------------------------------------
    workspace = store.provision(
        kind=store.KIND_WORKSPACE,
        name=WORKSPACE_PREFIX[:-1],
        version=_version(WORKSPACE_ARCHIVE, WORKSPACE_PREFIX),
        sha256=sha256_file(WORKSPACE_ARCHIVE),
        env=environment,
        sources=[PACKAGES],
        store=store_dir,
    )
    assert (workspace.path / "workspace" / "zephyr" / "VERSION").is_file()
    assert (workspace.path / "workspace" / ".west" / "config").is_file()
    links = [path for path in (workspace.path / "workspace").rglob("*") if path.is_symlink()]
    assert len(links) > 100, "the workspace's own links did not survive unpacking"
    assert all(not str(link.readlink()).startswith("/") for link in links)

    # git reads the ownership exemption back, and it names the workspace.
    exempted = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(store.git_config_file(workspace)),
            "--get",
            "safe.directory",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert exempted.stdout.strip() == f"{workspace.path / 'workspace'}/*"

    # ------------------------------------------------------------------
    # The host tools, if this host's Python can have them
    # ------------------------------------------------------------------
    requirement = _wheel_requirement(TOOLS_ARCHIVE, work / "wheel-names")
    assert requirement.exact is not None, "the published wheel set names no interpreter"
    interpreter = _interpreter_for(requirement)

    if interpreter is None:
        # This host cannot install the wheel set at all, and that is the
        # case worth proving on the real bytes: the refusal names the
        # version it needs and leaves nothing behind.
        with pytest.raises(BuildError) as caught:
            provision_tools()
        assert f"needs Python {requirement.described()}" in caught.value.message
        assert not store.entry_directory(store_dir, TOOLS_PREFIX[:-1], tools_version).exists()
        return

    tools = provision_tools(interpreter=interpreter)
    assert os.access(tools.path / "bin" / "build-environment-entry", os.X_OK)
    assert os.access(tools.path / "cmake" / "bin" / "cmake", os.X_OK)
    assert list(tools.path.glob("zephyr-sdk-*")), "no Zephyr SDK in the tools package"

    # The virtual environment, created at the entry's final path: `west`
    # is a console script whose shebang names the interpreter it was
    # installed for, so this is what an environment finalized anywhere
    # else fails at.
    west = tools.path / store.VENV_DIR / "bin" / "west"
    version = subprocess.run([str(west), "--version"], capture_output=True, text=True, check=True)
    assert "West version" in version.stdout

    for entry in (tools, workspace):
        assert not os.access(entry.path, os.W_OK)
        with pytest.raises(PermissionError):
            (entry.path / "written-by-a-build").write_text("no", encoding="utf-8")

    # A second provisioning of the same package does nothing at all.
    marker = tools.marker.stat().st_mtime_ns
    assert provision_tools(interpreter=interpreter) == tools
    assert tools.marker.stat().st_mtime_ns == marker
