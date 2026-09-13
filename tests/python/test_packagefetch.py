# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Getting a pinned package onto this machine, and unpacking it safely
(``mcuhome/workbench/packagefetch.py``).

Salvaged from ``test_orchestrator.py`` at the build-environment
switchover, when the module that carried these tests split into
``packagefetch.py`` (this file) and ``buildprocess.py``
(``test_buildprocess.py``). The subject is unchanged: a source directory
is searched for the pinned bytes, the hash decides rather than the name,
and the archive is unpacked under rules it cannot talk its way out of —
no traversal, no absolute path, and no link out of the tree unless the
caller says links may exist at all.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import zstandard
from conftest import sdk_members
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import packagefetch

SDK_VERSION = "0.1.0"


# --------------------------------------------------------------------------
# A real SDK package, built the way scripts/build_sdk_archive.py builds one
# --------------------------------------------------------------------------


def build_sdk_archive(members: dict[str, tuple[bytes, bool]]) -> bytes:
    """A deterministic ``.tar.zst`` of *members* (path -> (bytes, executable))."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, (content, executable) in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if executable else 0o644
            tar.addfile(info, io.BytesIO(content))
    return zstandard.ZstdCompressor(level=3).compress(buffer.getvalue())


def make_sdk_source(directory: Path, *, index_sha: str | None = None) -> str:
    """A source directory with one SDK archive and the index that names it.

    Returns the archive's **real** sha256 — the value a matching context
    must pin. ``index_sha`` forces the index to declare a different hash,
    so a test can make the index disagree with the pin.
    """
    directory.mkdir(parents=True, exist_ok=True)
    archive = build_sdk_archive(sdk_members(SDK_VERSION))
    filename = f"mcuhome-sdk-{SDK_VERSION}.tar.zst"
    (directory / filename).write_bytes(archive)
    real = sha256_file(directory / filename)
    index = {
        "packages": {
            "mcuhome-sdk": {
                SDK_VERSION: {"file": filename, "sha256": index_sha or real, "size": len(archive)}
            }
        }
    }
    (directory / "index.json").write_text(json.dumps(index), "utf-8")
    return real


# --------------------------------------------------------------------------
# The SDK package: acquire, verify, unpack safely
# --------------------------------------------------------------------------


def test_fetch_sdk_package_finds_verifies_and_unpacks(tmp_path) -> None:
    real = make_sdk_source(tmp_path / "src")
    into = tmp_path / "sdk"
    package = packagefetch.fetch_sdk_package(
        version=SDK_VERSION, sha256=real, sources=(tmp_path / "src",), into=into
    )
    assert package.tree == into
    assert (into / "mcuhome-sdk.json").is_file()
    assert (into / "bin" / "generate").is_file()


def test_the_entry_point_keeps_its_executable_bit(tmp_path) -> None:
    """The legacy container invocation (retired at the switchover) spawns
    bin/generate as a child — an SDK without its exec bit answers exit 127
    where code generation should be."""
    real = make_sdk_source(tmp_path / "src")
    into = tmp_path / "sdk"
    packagefetch.fetch_sdk_package(
        version=SDK_VERSION, sha256=real, sources=(tmp_path / "src",), into=into
    )
    assert (into / "bin" / "generate").stat().st_mode & 0o100
    assert not (into / "mcuhome-sdk.json").stat().st_mode & 0o100


def test_a_wrong_hash_is_refused_as_loudly_as_a_missing_file(tmp_path) -> None:
    """The hash decides, not the name: right name, wrong bytes is refused."""
    make_sdk_source(tmp_path / "src", index_sha="b" * 64)
    with pytest.raises(BuildError) as caught:
        # The pin matches the index but not the archive's real bytes.
        packagefetch.fetch_sdk_package(
            version=SDK_VERSION, sha256="b" * 64, sources=(tmp_path / "src",), into=tmp_path / "sdk"
        )
    assert "hashes to" in caught.value.message


def test_an_index_that_disagrees_with_the_pin_is_refused(tmp_path) -> None:
    make_sdk_source(tmp_path / "src", index_sha="d" * 64)
    with pytest.raises(BuildError) as caught:
        packagefetch.fetch_sdk_package(
            version=SDK_VERSION, sha256="e" * 64, sources=(tmp_path / "src",), into=tmp_path / "sdk"
        )
    # Both hashes, so the refusal says which two values disagree.
    assert "d" * 64 in caught.value.message
    assert "e" * 64 in caught.value.message


def test_no_source_holding_the_package_is_a_typed_refusal(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BuildError) as caught:
        packagefetch.fetch_sdk_package(
            version=SDK_VERSION, sha256="a" * 64, sources=(empty,), into=tmp_path / "sdk"
        )
    assert packagefetch.SDK_PACKAGE_NAME in (caught.value.message + (caught.value.hint or ""))


def test_the_safe_extractor_refuses_a_traversal(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )
    assert "unsafe path" in caught.value.message


def test_the_safe_extractor_refuses_an_absolute_path(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("/etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(BuildError):
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )


def test_the_safe_extractor_refuses_a_symlink(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )
    assert "not a regular file" in caught.value.message


def test_the_safe_extractor_refuses_a_hardlink(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "real"
        tar.addfile(link)
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )
    assert "not a regular file" in caught.value.message


# --------------------------------------------------------------------------
# Symlinks, for the packages a build environment is assembled from
# --------------------------------------------------------------------------
#
# A toolchain reaches its compiler through `arm-zephyr-eabi-cc -> ...-gcc`
# and a vendored source tree links its own subdirectories, so the two
# environment packages carry hundreds of links and cannot be delivered
# without them. The rule that replaces "no links at all" is lexical
# containment, and these are its edges.


def _link_archive(spool: Path, target: str, *, name: str = "deep/link") -> None:
    with tarfile.open(spool, "w") as tar:
        directory = tarfile.TarInfo("deep")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        payload = tarfile.TarInfo("deep/real")
        payload.size = 2
        tar.addfile(payload, io.BytesIO(b"hi"))
        link = tarfile.TarInfo(name)
        link.type = tarfile.SYMTYPE
        link.linkname = target
        tar.addfile(link)


def test_a_link_inside_the_tree_is_created_when_the_package_may_carry_links(tmp_path) -> None:
    """And verbatim: a relative target stays relative, so the tree relocates."""
    spool = tmp_path / "package.tar"
    _link_archive(spool, "real")
    into = tmp_path / "out"
    packagefetch._safe_extract(
        spool, into=into, quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
    )
    assert (into / "deep" / "link").is_symlink()
    assert os.readlink(into / "deep" / "link") == "real"
    assert (into / "deep" / "link").read_bytes() == b"hi"


def test_a_link_climbing_back_into_the_tree_is_created(tmp_path) -> None:
    """``../..`` is only an escape when it ends up outside — CHIP's own
    ``third_party/connectedhomeip -> ../../..`` does not."""
    spool = tmp_path / "package.tar"
    _link_archive(spool, "../deep", name="deep/inner/link")
    into = tmp_path / "out"
    packagefetch._safe_extract(
        spool, into=into, quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
    )
    assert (into / "deep" / "inner" / "link").is_symlink()


def test_a_link_out_of_the_tree_is_refused(tmp_path) -> None:
    spool = tmp_path / "package.tar"
    _link_archive(spool, "../../../../etc/passwd")
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
        )
    assert "outside the package" in caught.value.message


def test_a_target_that_only_begins_with_dots_is_a_name_and_not_a_climb(tmp_path) -> None:
    """``..data`` is a file name. The check is segment-wise for that reason:
    a string prefix test would refuse it and the package with it."""
    spool = tmp_path / "package.tar"
    _link_archive(spool, "..data")
    into = tmp_path / "out"
    packagefetch._safe_extract(
        spool, into=into, quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
    )
    assert os.readlink(into / "deep" / "link") == "..data"


def test_an_absolute_link_is_refused(tmp_path) -> None:
    spool = tmp_path / "package.tar"
    _link_archive(spool, "/etc/passwd")
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
        )
    assert "outside the package" in caught.value.message


def test_an_entry_under_a_link_the_archive_placed_itself_is_refused(tmp_path) -> None:
    """The escape a lexical check alone does not catch: link ``l1`` to
    ``.``, then link ``l1/l2`` to ``..`` — both resolve inside *by name*,
    while on disk the second one lands a level above the tree and the file
    written through it lands outside it. Each further link climbs another
    level, so the write target is arbitrary."""
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        for name, target in (("l1", "."), ("l1/l2", "..")):
            link = tarfile.TarInfo(name)
            link.type = tarfile.SYMTYPE
            link.linkname = target
            tar.addfile(link)
        payload = tarfile.TarInfo("l1/l2/escaped")
        payload.size = 5
        tar.addfile(payload, io.BytesIO(b"pwned"))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=outside / "tree", quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
        )
    assert "which it made a link" in caught.value.message
    assert not (outside / "escaped").exists()


def test_a_hardlink_stays_refused_where_symlinks_are_allowed(tmp_path) -> None:
    """A hardlink names an inode, not a path — nothing about it can be
    checked lexically, so widening the rule for symlinks does not widen it."""
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "real"
        tar.addfile(link)
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES, symlinks=True
        )
    assert "not a regular file" in caught.value.message


def test_the_safe_extractor_refuses_a_device_node(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        dev = tarfile.TarInfo("dev")
        dev.type = tarfile.CHRTYPE
        dev.devmajor = 1
        dev.devminor = 3
        tar.addfile(dev)
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )
    assert "not a regular file" in caught.value.message


def test_a_member_name_with_a_nul_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_member_name("bad\x00name")
    assert "unsafe path" in caught.value.message


def test_a_member_name_with_a_backslash_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_member_name("bad\\name")
    assert "unsafe path" in caught.value.message


def test_the_safe_extractor_types_a_name_the_filesystem_rejects(tmp_path) -> None:
    """A name the tar reads back fine but the filesystem cannot create — a
    component over NAME_MAX — is a typed BuildError, not a bare OSError."""
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("a" * 300)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(BuildError) as caught:
        packagefetch._safe_extract(
            spool, into=tmp_path / "out", quota_bytes=packagefetch.SDK_MAX_BYTES
        )
    assert "cannot unpack" in caught.value.message
