# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The SDK package: the same bytes twice, the allowlist, and a real unpack.

``scripts/build_sdk_archive.py`` produces the third artifact of a release
(ADR 0017 §2: "Repo, Python packages and SDK package are names for one
release"), and two of its properties are the kind that nothing notices
until a build somewhere else fails:

* **Determinism.** ADR 0018 §6 hashes ``mcuhome.package.sha256`` into the
  context ID, so a byte-different but content-identical archive gives a
  different identity to a build in which nothing changed — and a package
  a user built from the same tag can never satisfy a pin somebody else
  resolved. The proof is a second build compared byte for byte.
* **The allowlist.** What is in the archive is what a build container can
  reach; what is not is invisible until a CMake configure or a
  ``chip_configure_data_model()`` fails a quarter of an hour into a
  Matter build. The test is both directions — everything named is there,
  and nothing else is.

The third test is the loop rather than a property: a real archive, put in
a directory, acquired and unpacked by the **build server's own**
``sdkstore.acquire_sdk``, and then read by the program's own
``abi.sdk_entry_point``. That is every step between "CI wrote a file" and
"the container reaches code generation", with nothing simulated in
between, and it is the only place where the two repositories' assumptions
about one archive meet.

**No docker and no firmware build here**, the rule the container suites
already follow: building the archive costs under a second, and everything
this file asserts is a property of those bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

from mcuhome.compiler.abi import sdk_entry_point
from mcuhome.model.errors import BuildError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "build_sdk_archive.py"

#: Directories the archive must never carry, each for a reason the
#: script's docstring records: no consumer reads them out of
#: ``trees.sdk``, and three of them would be circular (the image
#: definition, the packaging metadata nothing installs, the test suite
#: that tests the tree).
EXCLUDED = (
    ".claude",
    ".github",
    "containers",
    "docs",
    "packaging",
    "patches",
    "tests",
    "tests_py",
)


def _script():
    """``build_sdk_archive.py`` as a module — ``scripts/`` is not a package.

    Registered in ``sys.modules`` before it is executed, because
    ``@dataclass`` resolves its own module out of there while the class
    body runs; loading it any other way fails inside the decorator rather
    than in anything this file wrote.
    """
    spec = importlib.util.spec_from_file_location("build_sdk_archive", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _unpacked(archive: Path) -> list[tarfile.TarInfo]:
    """The archive's members, decompressed in memory."""
    raw = zstandard.ZstdDecompressor().decompress(
        archive.read_bytes(), max_output_size=64 * 1024 * 1024
    )
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        return tar.getmembers()


@pytest.fixture(scope="module")
def module_script():
    return _script()


@pytest.fixture(scope="module")
def package(module_script, tmp_path_factory):
    """One real archive built from ``HEAD``, shared by everything below."""
    directory = tmp_path_factory.mktemp("sdk-package")
    return module_script.build_archive(repository=REPO_ROOT, revision="HEAD", output_dir=directory)


# --------------------------------------------------------------------------
# The bytes
# --------------------------------------------------------------------------


def test_two_builds_of_one_revision_are_byte_identical(module_script, package, tmp_path) -> None:
    """The property the pin depends on, proved rather than argued.

    Nothing about the archive may come from the machine that built it —
    not the order the filesystem returned the tree in, not a umask, not
    the clock. A second build into a different directory is the only test
    that covers all three at once, because it compares the artifact and
    not the intentions.
    """
    again = module_script.build_archive(
        repository=REPO_ROOT, revision="HEAD", output_dir=tmp_path / "second"
    )
    assert again.path.name == package.path.name
    assert again.sha256 == package.sha256
    assert again.path.read_bytes() == package.path.read_bytes()


def test_the_recorded_hash_is_the_hash_of_the_file(package) -> None:
    """The index and the sidecar describe the bytes on disk, or they describe nothing."""
    payload = package.path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == package.sha256
    assert package.size == len(payload)


def test_the_archive_carries_one_mtime_and_no_ownership(package) -> None:
    """Three ways a checkout leaks into an artifact, closed together.

    A per-file mtime makes the digest a function of when the tree was
    checked out; a ``uid``/``uname`` makes it a function of who did it.
    """
    members = _unpacked(package.path)
    assert {member.mtime for member in members} == {
        int(
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%ct", package.commit],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    }
    assert {(member.uid, member.gid) for member in members} == {(0, 0)}
    assert {(member.uname, member.gname) for member in members} == {("", "")}


# --------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------


def test_the_archive_holds_nothing_outside_the_allowlist(module_script, package) -> None:
    """An allowlist is only an allowlist if something checks the result.

    Every member is either a named file, a file inside a named tree, or a
    directory on the way to one. Anything else means the filter grew a
    hole — and the failure mode is a package shipping repository content
    nobody meant to redistribute.
    """
    members = _unpacked(package.path)
    files = {member.name for member in members if member.isfile()}
    directories = {member.name.rstrip("/") for member in members if member.isdir()}

    outside = sorted(name for name in files if not module_script.included(name))
    assert outside == [], "these files are in the archive and in no allowlist entry"

    needed = {parent for name in files for parent in _ancestors(name)}
    assert directories == needed, "the archive carries a directory nothing in it needs"


def _ancestors(name: str) -> set[str]:
    parts = name.split("/")[:-1]
    return {"/".join(parts[:depth]) for depth in range(1, len(parts) + 1)}


def test_every_allowlist_entry_actually_arrived(module_script, package) -> None:
    """The other direction: an entry that names nothing is a silent omission.

    A renamed or deleted directory would otherwise leave the allowlist
    quietly describing a tree that no longer exists, and the package would
    lose a build input without anything failing here.
    """
    files = {member.name for member in _unpacked(package.path) if member.isfile()}
    for name in sorted(module_script.SDK_FILES):
        assert name in files, f"{name} is allowlisted and not in the archive"
    for tree in module_script.SDK_TREES:
        assert any(name.startswith(f"{tree}/") for name in files), f"{tree}/ arrived empty"


@pytest.mark.parametrize("directory", EXCLUDED)
def test_what_is_deliberately_out_stays_out(package, directory: str) -> None:
    """Each of these has been argued about; the argument is recorded as a test."""
    files = {member.name for member in _unpacked(package.path) if member.isfile()}
    assert not [name for name in files if name.startswith(f"{directory}/")]


def test_the_entry_point_keeps_its_executable_bit(package) -> None:
    """§6.1 spawns it, never imports it: ``[str(entry), GENERATE_ACTION, …]``.

    ``mcuhome/compiler/abi.py`` runs ``generate.program`` as a child
    process, and ``_run_child`` answers ``127`` for an ``OSError`` — so a
    package that lost the mode bit fails as ``error.build.failed`` with
    nothing pointing at the archive. The bit is archive content.
    """
    executable = {
        member.name for member in _unpacked(package.path) if member.isfile() and member.mode & 0o111
    }
    assert executable == {"bin/generate"}


def test_the_tree_is_rooted_at_the_sdk_with_no_wrapper(package) -> None:
    """The server hands the unpack directory over as ``trees.sdk`` unchanged.

    No ``mcuhome-sdk-<version>/`` wrapper and no ``./`` prefix: the first
    would put ``mcuhome-sdk.json`` one level below the declared root, the
    second is refused outright by the extractor's path-shape check.
    Regular files and directories only, for the same reason the server
    refuses everything else — a symlink in an archive is a way out of the
    directory it is unpacked into.
    """
    members = _unpacked(package.path)
    assert "mcuhome-sdk.json" in {member.name for member in members}
    for member in members:
        assert member.isfile() or member.isdir(), f"{member.name} is neither file nor directory"
        assert not member.name.startswith(("/", "./"))
        assert ".." not in member.name.split("/")


def test_the_archive_fits_the_ingress_caps_the_server_applies(package) -> None:
    """The caps are not binding today, and they bound what may be added.

    ``acquire_sdk`` runs the SDK package through the same extractor a
    client's context goes through, so a tree that outgrew 4096 entries or
    depth 16 would be refused at unpack time — on the operator's own file,
    with a client's error code.
    """
    members = _unpacked(package.path)
    assert len(members) <= 4096
    assert max(len(member.name.rstrip("/").split("/")) for member in members) <= 16


# --------------------------------------------------------------------------
# The name, the sidecar, the index
# --------------------------------------------------------------------------


def test_the_file_name_carries_the_version_the_archived_tree_declares(package) -> None:
    """The lookup key and the content must agree.

    ``sdkstore`` finds a candidate by ``mcuhome-sdk-<version>.tar.zst``,
    so a name built from the working tree while the content came from a
    commit would be a package that answers to a version it does not
    carry. Checked against the ``__version__`` *inside* the archive, which
    is the only copy that can be wrong here.
    """
    members = _unpacked(package.path)
    raw = zstandard.ZstdDecompressor().decompress(
        package.path.read_bytes(), max_output_size=64 * 1024 * 1024
    )
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        handle = tar.extractfile("mcuhome/model/__init__.py")
        assert handle is not None
        source = handle.read().decode("utf-8")
    assert f'__version__ = "{package.version}"' in source
    assert package.path.name == f"mcuhome-sdk-{package.version}.tar.zst"
    assert members  # the archive is not empty, which the line above assumes


def test_the_sidecar_is_what_sha256sum_writes(package) -> None:
    """Bare hex, two spaces, the file name — so ``sha256sum -c`` checks a mirror.

    No format of ours, because the reader is an operator with the tool
    already on the machine. The pin itself travels in the context and is
    recomputed from the bytes; this file is for the hop in between.
    """
    sidecar = package.path.parent / f"{package.path.name}.sha256"
    assert sidecar.read_text(encoding="utf-8") == f"{package.sha256}  {package.path.name}\n"


def test_the_index_answers_name_and_version_with_file_and_hash(package) -> None:
    """The whole question the index exists for, and no URL anywhere.

    ADR 0019 §8: the backend "resolves (name, version, sha256) against
    its configured source list" and ``package.url`` "is a hint only". A
    URL in the index would be a second answer to a question the operator's
    source list already answers.
    """
    document = json.loads((package.path.parent / "index.json").read_text(encoding="utf-8"))
    assert document["packages"]["mcuhome-sdk"][package.version] == {
        "file": package.path.name,
        "sha256": package.sha256,
        "size": package.size,
    }
    assert "http" not in json.dumps(document)
    assert "mcuhome.org" not in json.dumps(document)


def test_a_second_release_extends_the_index_instead_of_replacing_it(
    module_script, tmp_path
) -> None:
    """A directory holding two releases is the first implementation of the index.

    Rewriting the file per package would leave the index describing the
    last thing built rather than what the directory holds.
    """
    index = tmp_path / "index.json"
    module_script.write_index(index, version="0.1.0", file="a.tar.zst", sha256="ab", size=1)
    module_script.write_index(index, version="0.2.0", file="b.tar.zst", sha256="cd", size=2)
    document = json.loads(index.read_text(encoding="utf-8"))
    assert sorted(document["packages"]["mcuhome-sdk"]) == ["0.1.0", "0.2.0"]


def test_an_unreadable_index_is_a_refusal_and_never_an_overwrite(module_script, tmp_path) -> None:
    """It is the only record of the packages already in that directory."""
    index = tmp_path / "index.json"
    index.write_text("not json at all", encoding="utf-8")
    with pytest.raises(SystemExit):
        module_script.write_index(index, version="0.1.0", file="a.tar.zst", sha256="ab", size=1)
    assert index.read_text(encoding="utf-8") == "not json at all"


# --------------------------------------------------------------------------
# The loop: CI's archive, the build server's unpack, the program's reader
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def unpacked_by_the_build_server(package, tmp_path_factory):
    """``acquire_sdk`` against a directory holding exactly this archive.

    The build server is the only implemented consumer, and it implements
    the first tier of ADR 0019's search order only: "one or more local
    directories, searched in the order the operator listed them". So the
    whole arrangement is a directory — which is also, by that amendment,
    "the whole first implementation" of the index.

    Skipped rather than faked where the build server is not installed:
    this repository does not depend on it, and a hand-written stand-in
    for the extractor would assert this suite's idea of the unpack rule
    instead of the server's.
    """
    sdkstore = pytest.importorskip(
        "mcuhome_buildserver.sdkstore",
        reason="the build server is not installed in this environment",
    )
    from mcuhome_buildserver.ingress import IngressCaps

    caps = IngressCaps(
        compressed_bytes=64 * 1024 * 1024,
        decompressed_bytes=256 * 1024 * 1024,
        entries=4096,
        file_bytes=64 * 1024 * 1024,
        path_depth=16,
    )
    return sdkstore.acquire_sdk(
        version=package.version,
        sha256=package.sha256,
        sources=(package.path.parent,),
        into=tmp_path_factory.mktemp("session") / "sdk",
        caps=caps,
        max_bytes=256 * 1024 * 1024,
    )


def test_the_build_server_finds_verifies_and_unpacks_this_archive(
    package, unpacked_by_the_build_server
) -> None:
    """Name, hash and extraction rule, end to end and in process.

    Three things could break this without either repository changing its
    mind: a file name the lookup does not build, a compression the
    decompressor does not read, and a member shape the extractor refuses.
    All three are answered by the same call the backend makes.
    """
    assert unpacked_by_the_build_server.version == package.version
    assert unpacked_by_the_build_server.sha256 == package.sha256
    assert unpacked_by_the_build_server.source == package.path
    assert (unpacked_by_the_build_server.tree / "mcuhome-sdk.json").is_file()


def test_the_unpacked_tree_is_an_sdk_the_program_can_read(unpacked_by_the_build_server) -> None:
    """§6.1's own reader, on the tree the server produced.

    ``sdk_entry_point`` is what a build calls before it generates
    anything, and it exists "so a caller can also raise while checking an
    SDK package it is *shipping*" — which is exactly this call. It
    refuses a missing file, a wrong ``sdk`` version, a missing field and
    an absolute or escaping ``generate.program``, so passing it means the
    archive's §6.1 interface survived the round trip.
    """
    tree = unpacked_by_the_build_server.tree
    entry, runtime = sdk_entry_point(tree)
    assert entry == tree / "bin" / "generate"
    assert entry.is_file()
    assert runtime == "python3"
    # A tree that is not an SDK is the refusal this reader exists for.
    with pytest.raises(BuildError):
        sdk_entry_point(tree / "app")


def test_the_entry_point_is_still_executable_after_the_unpack(unpacked_by_the_build_server) -> None:
    """The one archive property an unpack can destroy, and the contract requires.

    §6.1 makes ``generate.program`` a child process, and
    ``tests_py/test_abi.py`` already pins the rule on the repository's own
    copy: "the entry point is invoked, not imported (§6.1)". The archive
    ships the bit; whether a build can use it is decided here — this was
    an xfail until the build server learned to preserve the owner's
    execute bit through ``unpack_tree`` (it once chmodded everything to
    0600, so ``bin/generate`` arrived unexecutable and ``_run_child``
    answered 127). Cross-repo: a regression on either side goes red here.
    """
    entry, _runtime = sdk_entry_point(unpacked_by_the_build_server.tree)
    assert entry.stat().st_mode & 0o111, "generate.program is spawned, not imported (§6.1)"
