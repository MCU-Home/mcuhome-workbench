# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Reading a package registry, without a registry and without a network.

Every source here is built by the publishing tool itself and signed with
keys this file generates in the test — never a key that signs anything
real, and never a fixture copied out of somewhere that does. The
signatures are therefore genuine and the anchor is genuinely the only
thing that decides; what is faked is the *host*, which is exactly the
part that is not supposed to matter.

The suite is offline by construction: an autouse fixture replaces the
real HTTP opener with one that fails the test if anything reaches it, so
a code path that quietly opened a socket is a failure here rather than a
flake on somebody's aeroplane.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import pytest
import zstandard
from mcuhome.model.errors import BuildError
from mcuhome.packagetool.documents import dump, write_signed
from mcuhome.packagetool.keys import SigningKey, generate, key_id, public_b64
from mcuhome.packagetool.source import (
    INDEX_FILE,
    KEYS_FILE,
    MIRRORS_FILE,
    PublicKey,
    add_meta_package,
    add_package,
    init_source,
    read_document,
)

from mcuhome.workbench import packagefetch, packageregistry
from mcuhome.workbench.packageregistry import (
    PackageRegistry,
    PackageRegistryError,
    RegistrySettings,
    TrustAnchorMissing,
    anchor_file,
    host_platform,
    install_trust_anchors,
    matching_version,
    merge_registries,
    meta_file_of,
    parse_registries,
    registry_factory,
    registry_for,
    resolve_entry,
    trust_anchor_for,
)
from mcuhome.workbench.project import create_project
from mcuhome.workbench.resolve_pins import (
    DEFAULT_SDK_CONSTRAINT,
    SDK_ANY,
    resolve_sdk,
    sdk_constraint,
)

DOMAIN = "packages.example.org"
MIRROR = f"https://mirror-1.{DOMAIN}/sdk/"
SOURCE = "sdk"
SDK = "mcuhome-sdk"
VERSION = "0.1.0"

TOOLS = "mcuhome-build-tools"
AMD64 = f"{TOOLS}_linux-amd64"
ARM64 = f"{TOOLS}_linux-arm64"

#: When the fixtures are issued, and when the tests verify as of. Far
#: enough apart to be a real source, close enough that a 30-day document
#: is still live.
ISSUED = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Nothing here touches the network
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str, timeout: float) -> IO[bytes]:
        raise AssertionError(f"this suite reached the network: {url}")

    monkeypatch.setattr(packageregistry, "_http_open", refuse)


class Offline:
    """An opener that fails the test if it is called at all."""

    def __call__(self, url: str, timeout: float) -> IO[bytes]:
        raise AssertionError(f"a mirror was fetched when nothing should have been: {url}")


class Served:
    """A host: URL to bytes, and a record of everything that was asked for."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def publish(self, base: str, directory: Path) -> Served:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                self.files[base + str(path.relative_to(directory))] = path.read_bytes()
        return self

    def put(self, url: str, payload: bytes) -> None:
        self.files[url] = payload

    def __call__(self, url: str, timeout: float) -> IO[bytes]:
        del timeout
        self.calls.append(url)
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # noqa: B904
        return io.BytesIO(self.files[url])


# --------------------------------------------------------------------------
# A real source, signed with keys generated here
# --------------------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public(signer: SigningKey, *, issued: datetime = ISSUED) -> PublicKey:
    return PublicKey(
        keyid=key_id(signer.public),
        public=public_b64(signer.public),
        not_before=_stamp(issued),
        not_after=_stamp(issued + timedelta(days=365 * 3)),
    )


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


SDK_ARCHIVE = build_sdk_archive(
    {
        "mcuhome-sdk.json": (b'{"sdk": 1}', False),
        "bin/generate": (b"#!/usr/bin/env python3\n", True),
    }
)


@pytest.fixture
def keys(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SigningKey]:
    """Three roots and a publisher, drawn for this test run and no other."""
    directory = tmp_path_factory.mktemp("keys")
    return {
        name: generate(directory, name)
        for name in ("root-a", "root-b", "root-c", "publisher", "outsider")
    }


def anchor_document(keys: dict[str, SigningKey], names: tuple[str, ...]) -> dict:
    return {
        "version": 1,
        "threshold": 2,
        "keys": [
            {"keyid": key_id(keys[name].public), "public": public_b64(keys[name].public)}
            for name in names
        ],
    }


def build_meta_document(
    name: str, version: str, *, requires: dict[str, str] | None = None
) -> bytes:
    """A schema-1 package meta document, just valid enough for the publishing tool to accept it.

    Its *content* is not this suite's subject: the registry client records
    and verifies the sidecar's bytes without ever reading what is inside
    them, and what the members mean once they are parsed is
    ``test_resolve_pins.py``'s business. This exists so a test can put
    something the real publishing tool actually accepts beside an
    archive, rather than an arbitrary blob it would refuse to record.
    """
    document = {
        "schema": 1,
        "package": {"name": name, "version": version, "architecture": None},
        "requires": requires if requires is not None else {"mcuhome-build-workspace": "~=0.1.0"},
        "inputs_sha256": "b" * 64,
        "contents": {},
    }
    return json.dumps(document).encode()


def build_source(
    directory: Path,
    keys: dict[str, SigningKey],
    *,
    packages: tuple[tuple[str, str, bytes], ...] = ((SDK, VERSION, SDK_ARCHIVE),),
    meta: tuple[str, str, dict] | None = None,
    meta_files: dict[str, bytes] | None = None,
    mirrors: tuple[str, ...] = (MIRROR,),
    issued: datetime = ISSUED,
) -> Path:
    """A complete served source, built by the tool that builds the real ones.

    *meta_files* puts a ``<archive>.meta.json`` sidecar beside a package's
    archive before it is recorded, keyed by package name. Writing the file
    is all a caller has to do: :func:`add_package` looks for it beside the
    archive on its own and records it into the index, exactly as the real
    publishing tool does for a real release.
    """
    directory.mkdir(parents=True, exist_ok=True)
    roots = [keys["root-a"], keys["root-b"], keys["root-c"]]
    publishers = [keys["publisher"]]
    init_source(
        directory,
        roots=[_public(signer, issued=issued) for signer in roots],
        threshold=2,
        publishers=[_public(signer, issued=issued) for signer in publishers],
        mirrors=[{"url": url} for url in mirrors],
        issued=issued,
        root_signers=roots,
        publisher_signers=publishers,
    )
    moment = issued
    for name, version, payload in packages:
        moment = moment + timedelta(minutes=1)
        filename = f"{name}-{version}.tar.zst"
        (directory / filename).write_bytes(payload)
        sidecar = (meta_files or {}).get(name)
        if sidecar is not None:
            (directory / (filename + ".meta.json")).write_bytes(sidecar)
        add_package(
            directory,
            name=name,
            version=version,
            file=filename,
            sha256=packagefetch.sha256_file(directory / filename),
            size=len(payload),
            issued=moment,
            signers=publishers,
        )
    if meta is not None:
        name, version, members = meta
        add_meta_package(
            directory,
            name=name,
            version=version,
            meta=members,
            issued=moment + timedelta(minutes=1),
            signers=publishers,
        )
    return directory


@pytest.fixture
def source(tmp_path: Path, keys: dict[str, SigningKey]) -> Path:
    return build_source(tmp_path / "served" / SOURCE, keys)


def _write_anchor(path: Path, keys: dict[str, SigningKey]) -> Path:
    path.write_bytes(dump(anchor_document(keys, ("root-a", "root-b", "root-c"))))
    return path


@pytest.fixture
def anchor(tmp_path: Path, keys: dict[str, SigningKey]) -> Path:
    path = tmp_path / "anchor.json"
    path.write_bytes(dump(anchor_document(keys, ("root-a", "root-b", "root-c"))))
    return path


def bootstrap(served: Served, *, mirrors: tuple[str, ...] = (MIRROR,)) -> Served:
    """The base domain's answer to "where is this source" — unsigned, as served."""
    served.put(
        f"https://{DOMAIN}/{SOURCE}/{MIRRORS_FILE}",
        json.dumps({"mirrors": [{"url": url} for url in mirrors]}).encode(),
    )
    return served


def registry(tmp_path: Path, anchor: Path, served: Served, **kwargs) -> PackageRegistry:
    return PackageRegistry(
        DOMAIN,
        anchor=packageregistry.load_trust_anchor(anchor),
        into=tmp_path / "fetched",
        opener=served,
        now=NOW,
        **kwargs,
    )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_served_source_verifies_and_its_index_is_readable(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    index = registry(tmp_path, anchor, served).index(SOURCE)

    assert index.verified
    assert index.base == MIRROR
    assert index.versions(SDK) == (VERSION,)
    entry = index.resolve(SDK, VERSION)
    assert entry.name == SDK
    assert entry.sha256 == packagefetch.sha256_file(source / f"{SDK}-{VERSION}.tar.zst")
    # The bootstrap host is asked exactly once, and only for the mirror list.
    assert served.calls[0] == f"https://{DOMAIN}/{SOURCE}/{MIRRORS_FILE}"


def test_the_index_is_read_once_per_source(tmp_path: Path, source: Path, anchor: Path) -> None:
    """A verified index and a re-downloaded one are two documents, and only
    one of them was checked."""
    served = bootstrap(Served().publish(MIRROR, source))
    client = registry(tmp_path, anchor, served)
    client.index(SOURCE)
    before = len(served.calls)
    client.index(SOURCE)
    assert len(served.calls) == before


def test_the_archive_is_fetched_and_hashed_against_the_index(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    client = registry(tmp_path, anchor, served)
    index = client.index(SOURCE)
    entry = index.resolve(SDK, VERSION)
    fetched = client.fetch_package(index, entry, into=tmp_path / "packages")
    assert fetched.read_bytes() == SDK_ARCHIVE


def test_an_archive_whose_bytes_are_not_the_signed_ones_is_refused(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    client = registry(tmp_path, anchor, served)
    index = client.index(SOURCE)
    entry = index.resolve(SDK, VERSION)
    # Same length, other bytes: the size check cannot see this one.
    served.put(MIRROR + entry.file, bytes(len(SDK_ARCHIVE)))
    with pytest.raises(PackageRegistryError, match="hashes to"):
        client.fetch_package(index, entry, into=tmp_path / "packages")
    assert not (tmp_path / "packages" / entry.file).exists()


# --------------------------------------------------------------------------
# The meta file sidecar: recorded by the index, fetched and hashed like a package
# --------------------------------------------------------------------------


def test_a_resolved_entry_carries_its_meta_file_record(
    tmp_path: Path, keys: dict[str, SigningKey], anchor: Path
) -> None:
    """The index records the sidecar exactly as it records the archive.

    Not its content — a client resolving a chain never has to fetch a
    meta file to learn a version even exists — but the file name, hash
    and size, which is what makes a later :meth:`fetch_meta` checkable
    against the same verified index the archive is checked against.
    """
    sidecar = build_meta_document(SDK, VERSION)
    src = build_source(tmp_path / "served" / SOURCE, keys, meta_files={SDK: sidecar})
    served = bootstrap(Served().publish(MIRROR, src))
    entry = registry(tmp_path, anchor, served).index(SOURCE).resolve(SDK, VERSION)

    assert entry.meta_file is not None
    assert entry.meta_file.file == f"{SDK}-{VERSION}.tar.zst.meta.json"
    assert entry.meta_file.sha256 == hashlib.sha256(sidecar).hexdigest()
    assert entry.meta_file.size == len(sidecar)


def test_fetch_meta_answers_with_exactly_the_sidecar_bytes(
    tmp_path: Path, keys: dict[str, SigningKey], anchor: Path
) -> None:
    sidecar = build_meta_document(SDK, VERSION)
    src = build_source(tmp_path / "served" / SOURCE, keys, meta_files={SDK: sidecar})
    served = bootstrap(Served().publish(MIRROR, src))
    client = registry(tmp_path, anchor, served)
    index = client.index(SOURCE)
    entry = index.resolve(SDK, VERSION)

    assert entry.meta_file is not None
    assert client.fetch_meta(index, entry.meta_file) == sidecar


def test_a_meta_file_whose_bytes_are_not_the_signed_ones_is_refused(
    tmp_path: Path, keys: dict[str, SigningKey], anchor: Path
) -> None:
    """A meta file decides what a build resolves to next, so it is held to
    the same arithmetic as an archive — same length is not enough."""
    sidecar = build_meta_document(SDK, VERSION)
    src = build_source(tmp_path / "served" / SOURCE, keys, meta_files={SDK: sidecar})
    served = bootstrap(Served().publish(MIRROR, src))
    client = registry(tmp_path, anchor, served)
    index = client.index(SOURCE)
    entry = index.resolve(SDK, VERSION)
    assert entry.meta_file is not None

    # Same length, other bytes: the size check cannot see this one.
    served.put(MIRROR + entry.meta_file.file, bytes(len(sidecar)))
    with pytest.raises(PackageRegistryError, match="hashes to"):
        client.fetch_meta(index, entry.meta_file)


def test_a_meta_file_of_another_length_is_refused_before_it_is_parsed(
    tmp_path: Path, keys: dict[str, SigningKey], anchor: Path
) -> None:
    """The cheap half of the same check, and the one that catches a truncated
    copy: a document of the wrong size is refused by its length, so nothing
    ever parses bytes the index did not record."""
    sidecar = build_meta_document(SDK, VERSION)
    src = build_source(tmp_path / "served" / SOURCE, keys, meta_files={SDK: sidecar})
    served = bootstrap(Served().publish(MIRROR, src))
    client = registry(tmp_path, anchor, served)
    index = client.index(SOURCE)
    entry = index.resolve(SDK, VERSION)
    assert entry.meta_file is not None

    served.put(MIRROR + entry.meta_file.file, sidecar[:-1])
    with pytest.raises(PackageRegistryError, match="and the index says"):
        client.fetch_meta(index, entry.meta_file)


def test_a_malformed_meta_file_record_is_refused() -> None:
    """A damaged index member is a refusal, not a member silently treated
    as absent — the two are different failures for a reader to tell apart."""
    entry = {
        "file": f"{SDK}-{VERSION}.tar.zst",
        "sha256": "a" * 64,
        "size": 1,
        "meta_file": {"file": "x.meta.json"},
    }
    with pytest.raises(PackageRegistryError, match="damaged meta file"):
        meta_file_of(entry, name=SDK, version=VERSION)


def test_an_entry_without_a_meta_file_answers_none() -> None:
    """Absent is a legitimate answer: a package published before the member
    existed carries none, and that is not the same as a damaged one."""
    entry = {"file": f"{SDK}-{VERSION}.tar.zst", "sha256": "a" * 64, "size": 1}
    assert meta_file_of(entry, name=SDK, version=VERSION) is None


# --------------------------------------------------------------------------
# Every way a source can fail to be trustworthy
# --------------------------------------------------------------------------


def test_tampered_index_bytes_are_refused(tmp_path: Path, source: Path, anchor: Path) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    document = json.loads(served.files[MIRROR + INDEX_FILE])
    document["packages"][SDK]["9.9.9"] = {"file": "x", "sha256": "a" * 64, "size": 1}
    served.put(MIRROR + INDEX_FILE, json.dumps(document).encode())

    with pytest.raises(PackageRegistryError) as refusal:
        registry(tmp_path, anchor, served).index(SOURCE)
    assert "does not verify" in (refusal.value.hint or "")


def test_a_tampered_signature_is_refused(tmp_path: Path, source: Path, anchor: Path) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    envelope = json.loads(served.files[MIRROR + INDEX_FILE + ".sig"])
    envelope["signatures"][0]["sig"] = "A" + envelope["signatures"][0]["sig"][1:]
    served.put(MIRROR + INDEX_FILE + ".sig", json.dumps(envelope).encode())

    with pytest.raises(PackageRegistryError) as refusal:
        registry(tmp_path, anchor, served).index(SOURCE)
    assert "does not verify" in (refusal.value.hint or "")


def test_another_anchor_verifies_nothing(
    tmp_path: Path, source: Path, keys: dict[str, SigningKey]
) -> None:
    """The anchor decides, and a source cannot talk its way past it."""
    foreign = tmp_path / "foreign.json"
    foreign.write_bytes(
        dump(
            {
                "version": 1,
                "threshold": 1,
                "keys": [
                    {
                        "keyid": key_id(keys["outsider"].public),
                        "public": public_b64(keys["outsider"].public),
                    }
                ],
            }
        )
    )
    served = bootstrap(Served().publish(MIRROR, source))
    with pytest.raises(PackageRegistryError):
        registry(tmp_path, foreign, served).index(SOURCE)


def test_an_expired_document_is_refused(
    tmp_path: Path, keys: dict[str, SigningKey], anchor: Path
) -> None:
    """A mirror serving a frozen copy runs out; that is what expiry is for."""
    stale = build_source(tmp_path / "stale" / SOURCE, keys, issued=NOW - timedelta(days=400))
    served = bootstrap(Served().publish(MIRROR, stale))
    with pytest.raises(PackageRegistryError) as refusal:
        registry(tmp_path, anchor, served).index(SOURCE)
    assert "expired" in (refusal.value.hint or "")


def test_a_mirror_that_fails_is_followed_by_the_next(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    second = "https://mirror-2.example.org/sdk/"
    served = bootstrap(Served().publish(second, source), mirrors=(MIRROR, second))
    index = registry(tmp_path, anchor, served).index(SOURCE)
    assert index.base == second


def test_a_bootstrap_host_naming_no_mirror_is_a_refusal(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served(), mirrors=())
    with pytest.raises(PackageRegistryError, match="names no mirror"):
        registry(tmp_path, anchor, served).index(SOURCE)


def test_a_mirror_that_is_not_an_https_base_is_refused(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served(), mirrors=("http://mirror.example.org/sdk",))
    with pytest.raises(PackageRegistryError, match="not an https address"):
        registry(tmp_path, anchor, served).index(SOURCE)


# --------------------------------------------------------------------------
# Meta entries: one name, one package per architecture
# --------------------------------------------------------------------------


@pytest.fixture
def tools(tmp_path: Path, keys: dict[str, SigningKey]) -> Path:
    return build_source(
        tmp_path / "served" / "build-tools",
        keys,
        packages=(
            (AMD64, VERSION, b"amd64 bytes"),
            (ARM64, VERSION, b"arm64 bytes"),
        ),
        meta=(TOOLS, VERSION, {"arch": {"linux-amd64": AMD64, "linux-arm64": ARM64}}),
    )


def entries_of(source: Path) -> dict:
    from mcuhome.packagetool.verify import all_entries

    return all_entries(source, read_document(source / INDEX_FILE))


def test_a_meta_entry_resolves_to_this_hosts_package(tools: Path) -> None:
    entries = entries_of(tools)
    found = resolve_entry(entries, TOOLS, VERSION, platform="linux-arm64")
    assert found.name == ARM64
    assert found.through_meta
    assert found.file == f"{ARM64}-{VERSION}.tar.zst"


def test_a_platform_the_meta_map_does_not_name_is_refused(tools: Path) -> None:
    entries = entries_of(tools)
    with pytest.raises(PackageRegistryError) as refusal:
        resolve_entry(entries, TOOLS, VERSION, platform="linux-riscv64")
    assert "is not published for linux-riscv64" in refusal.value.message
    assert "linux-amd64" in refusal.value.message


def test_a_meta_hash_that_does_not_describe_its_members_is_refused(tools: Path) -> None:
    """Recomputed, never believed: pinning the family pins every member."""
    entries = {name: dict(versions) for name, versions in entries_of(tools).items()}
    entries[TOOLS] = dict(entries[TOOLS])
    entries[TOOLS][VERSION] = {**entries[TOOLS][VERSION], "sha256": "f" * 64}
    with pytest.raises(PackageRegistryError) as refusal:
        resolve_entry(entries, TOOLS, VERSION, platform="linux-amd64")
    assert "does not describe the packages it points at" in refusal.value.message


def test_a_concrete_pin_of_a_foreign_platform_is_refused(tools: Path) -> None:
    entries = entries_of(tools)
    with pytest.raises(PackageRegistryError) as refusal:
        resolve_entry(entries, ARM64, VERSION, platform="linux-amd64")
    assert "is built for linux-arm64, and this machine is linux-amd64" in refusal.value.message


def test_a_package_without_an_architecture_resolves_anywhere(source: Path) -> None:
    found = resolve_entry(entries_of(source), SDK, VERSION, platform="linux-arm64")
    assert found.name == SDK
    assert not found.through_meta


def test_the_host_platform_is_the_name_packages_are_published_under() -> None:
    assert host_platform(system="Linux", machine="x86_64") == "linux-amd64"
    assert host_platform(system="Linux", machine="aarch64") == "linux-arm64"
    with pytest.raises(PackageRegistryError, match="publishes no build environment"):
        host_platform(system="Darwin", machine="arm64")


# --------------------------------------------------------------------------
# Trust anchors
# --------------------------------------------------------------------------


def project(tmp_path: Path) -> Path:
    """A project created the way a user creates one."""
    return create_project(tmp_path / "project").project.root


def bare(tmp_path: Path) -> Path:
    """A directory that is not a project: no init has run over it."""
    root = tmp_path / "bare"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_creating_a_project_writes_the_anchors_that_ship(tmp_path: Path) -> None:
    """The one moment a trust root is installed: when the project is made."""
    result = create_project(tmp_path / "project")
    path = anchor_file(result.project.root, packageregistry.OFFICIAL_BASE_DOMAIN)
    assert path in result.created
    assert (
        path.read_bytes()
        == (
            packageregistry.BUNDLED_ANCHOR_DIR / f"{packageregistry.OFFICIAL_BASE_DOMAIN}.json"
        ).read_bytes()
    )
    assert trust_anchor_for(result.project.root, packageregistry.OFFICIAL_BASE_DOMAIN) == path


def test_an_anchor_that_is_there_is_never_touched(tmp_path: Path) -> None:
    """A person who edited theirs meant to; restoring our copy would overrule them."""
    root = bare(tmp_path)
    path = anchor_file(root, packageregistry.OFFICIAL_BASE_DOMAIN)
    path.parent.mkdir(parents=True)
    edited = b'{"keys": [], "threshold": 1, "note": "mine"}\n'
    path.write_bytes(edited)

    assert install_trust_anchors(root) == ()
    assert trust_anchor_for(root, packageregistry.OFFICIAL_BASE_DOMAIN) == path
    assert path.read_bytes() == edited


def test_a_missing_official_anchor_is_refused_and_never_written(tmp_path: Path) -> None:
    """The rule changed here on purpose: a build does not install trust.

    A project whose anchor is gone is told to run init again. Writing the
    shipped copy at this moment would be the tool deciding what to trust,
    while about to download something.
    """
    root = bare(tmp_path)
    with pytest.raises(TrustAnchorMissing) as refusal:
        trust_anchor_for(root, packageregistry.OFFICIAL_BASE_DOMAIN)
    assert "mcuhome project init" in (refusal.value.hint or "")
    assert not anchor_file(root, packageregistry.OFFICIAL_BASE_DOMAIN).exists()


def test_a_foreign_registry_without_an_anchor_is_refused(tmp_path: Path) -> None:
    root = project(tmp_path)
    with pytest.raises(TrustAnchorMissing) as refusal:
        trust_anchor_for(root, DOMAIN)
    assert str(anchor_file(root, DOMAIN)) in (refusal.value.hint or "")
    assert not anchor_file(root, DOMAIN).exists()


def test_a_registry_marked_untrusted_needs_no_anchor(tmp_path: Path) -> None:
    assert trust_anchor_for(project(tmp_path), DOMAIN, untrusted=True) is None


def test_a_domain_that_is_a_path_is_not_a_domain(tmp_path: Path) -> None:
    with pytest.raises(PackageRegistryError, match="is not a registry domain"):
        anchor_file(bare(tmp_path), "../../etc/shadow")


def test_an_untrusted_source_is_read_and_says_so_loudly(
    tmp_path: Path, source: Path, keys: dict[str, SigningKey]
) -> None:
    """Every guarantee traded away, and nobody gets to not notice."""
    warnings: list[str] = []
    # Even a source signed by keys nothing trusts is read.
    served = bootstrap(Served().publish(MIRROR, source))
    client = PackageRegistry(
        DOMAIN,
        anchor=None,
        into=tmp_path / "fetched",
        untrusted=True,
        opener=served,
        on_warning=warnings.append,
        now=NOW,
    )
    assert not client.index(SOURCE).verified
    assert any("NOTHING IS VERIFIED" in warning for warning in warnings)


def test_an_untrusted_registry_may_serve_a_source_with_no_signatures_at_all(
    tmp_path: Path,
) -> None:
    """ "Unsigned" is the case the untrusted marking exists for.

    A source that carries nothing but an index — no key set, no publisher
    signatures, nothing to check — is readable exactly when the project
    has said in writing that it accepts one, and never otherwise.
    """
    warnings: list[str] = []
    served = bootstrap(Served())
    served.put(
        MIRROR + INDEX_FILE,
        json.dumps(
            {"packages": {SDK: {VERSION: {"file": "x.tar.zst", "sha256": "a" * 64, "size": 1}}}}
        ).encode(),
    )
    client = PackageRegistry(
        DOMAIN,
        anchor=None,
        into=tmp_path / "fetched",
        untrusted=True,
        opener=served,
        on_warning=warnings.append,
        now=NOW,
    )
    index = client.index(SOURCE)
    assert not index.verified
    assert index.versions(SDK) == (VERSION,)
    assert any("NOTHING IS VERIFIED" in warning for warning in warnings)


def test_a_source_with_no_signatures_is_refused_when_the_registry_is_trusted(
    tmp_path: Path, anchor: Path
) -> None:
    served = bootstrap(Served())
    served.put(MIRROR + INDEX_FILE, json.dumps({"packages": {}}).encode())
    with pytest.raises(PackageRegistryError):
        registry(tmp_path, anchor, served).index(SOURCE)


def test_a_registry_without_an_anchor_and_without_untrusted_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrustAnchorMissing):
        PackageRegistry(DOMAIN, anchor=None, into=tmp_path / "fetched")


def test_registry_for_ties_the_project_and_its_settings_together(tmp_path: Path) -> None:
    """The official domain in a project that was created properly."""
    root = project(tmp_path)
    client = registry_for(
        packageregistry.OFFICIAL_BASE_DOMAIN,
        project_root=root,
        settings=(),
        into=tmp_path / "fetched",
        opener=Offline(),
    )
    assert client.base_domain == packageregistry.OFFICIAL_BASE_DOMAIN
    assert not client.untrusted


def test_a_promised_registry_costs_nothing_until_something_asks(tmp_path: Path) -> None:
    """A build that finds its packages locally must not need an anchor.

    Refusing at construction would turn "you have not decided who to
    trust" into "you cannot build offline", which is the opposite of what
    an anchor is for.
    """
    root = bare(tmp_path)
    promised = registry_factory(
        packageregistry.OFFICIAL_BASE_DOMAIN,
        project_root=root,
        into=tmp_path / "fetched",
        opener=Offline(),
    )
    with pytest.raises(TrustAnchorMissing):
        packageregistry.opened(promised)


def test_a_promised_registry_is_built_once(tmp_path: Path) -> None:
    promised = registry_factory(
        packageregistry.OFFICIAL_BASE_DOMAIN,
        project_root=project(tmp_path),
        into=tmp_path / "fetched",
        opener=Offline(),
    )
    assert packageregistry.opened(promised) is packageregistry.opened(promised)


def test_an_anchor_beside_untrusted_is_ignored_and_said_so(
    tmp_path: Path, source: Path, keys: dict[str, SigningKey]
) -> None:
    """`untrusted` is the project's word, and a file does not overrule it."""
    root = project(tmp_path)
    path = anchor_file(root, packageregistry.OFFICIAL_BASE_DOMAIN)
    path.write_bytes(dump(anchor_document(keys, ("root-a", "root-b", "root-c"))))

    warnings: list[str] = []
    served = Served().publish(MIRROR, source)
    client = registry_for(
        packageregistry.OFFICIAL_BASE_DOMAIN,
        project_root=root,
        settings=(
            RegistrySettings(
                packageregistry.OFFICIAL_BASE_DOMAIN,
                untrusted=True,
                mirrors={SOURCE: (str(source),)},
            ),
        ),
        into=tmp_path / "fetched",
        opener=served,
        on_warning=warnings.append,
        now=NOW,
    )
    assert client.untrusted
    assert not client.index(SOURCE).verified
    assert any("deliberately ignored" in warning for warning in warnings)


def test_an_untrusted_registry_says_so_at_every_read(tmp_path: Path, source: Path) -> None:
    """Once at the top of a build would be a footnote; this is not one."""
    warnings: list[str] = []
    client = PackageRegistry(
        DOMAIN,
        anchor=None,
        into=tmp_path / "fetched",
        untrusted=True,
        mirrors={SOURCE: (str(source),)},
        opener=Offline(),
        on_warning=warnings.append,
        now=NOW,
    )
    index = client.index(SOURCE)
    client.index(SOURCE)
    client.fetch_package(index, index.resolve(SDK, VERSION), into=tmp_path / "packages")
    assert len(warnings) == 3


# --------------------------------------------------------------------------
# Mirror overrides, and the offline case they exist for
# --------------------------------------------------------------------------


def test_a_local_mirror_replaces_the_served_list_and_needs_no_network(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    """The air-gapped case: a directory synchronised out of band, and no socket."""
    client = PackageRegistry(
        DOMAIN,
        anchor=packageregistry.load_trust_anchor(anchor),
        into=tmp_path / "fetched",
        mirrors={SOURCE: (str(source),)},
        opener=Offline(),
        now=NOW,
    )
    index = client.index(SOURCE)
    assert index.base == str(source)
    entry = index.resolve(SDK, VERSION)
    fetched = client.fetch_package(index, entry, into=tmp_path / "packages")
    assert fetched.read_bytes() == SDK_ARCHIVE


def test_a_local_mirror_is_verified_like_any_other(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    document = read_document(source / INDEX_FILE)
    document["packages"][SDK]["9.9.9"] = {"file": "x", "sha256": "a" * 64, "size": 1}
    (source / INDEX_FILE).write_bytes(dump(document))

    client = PackageRegistry(
        DOMAIN,
        anchor=packageregistry.load_trust_anchor(anchor),
        into=tmp_path / "fetched",
        mirrors={SOURCE: (str(source),)},
        opener=Offline(),
        now=NOW,
    )
    with pytest.raises(PackageRegistryError):
        client.index(SOURCE)


def test_a_directory_that_is_not_a_source_says_so(tmp_path: Path, anchor: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    client = PackageRegistry(
        DOMAIN,
        anchor=packageregistry.load_trust_anchor(anchor),
        into=tmp_path / "fetched",
        mirrors={SOURCE: (str(empty),)},
        opener=Offline(),
        now=NOW,
    )
    with pytest.raises(PackageRegistryError) as refusal:
        client.index(SOURCE)
    assert "is not a package source" in (refusal.value.hint or "")


# --------------------------------------------------------------------------
# The configuration block
# --------------------------------------------------------------------------


def test_the_registry_block_parses(tmp_path: Path) -> None:
    file = tmp_path / "mcuhome.yaml"
    file.write_text("", encoding="utf-8")
    parsed = parse_registries(
        {
            DOMAIN: {
                "untrusted": True,
                "mirrors": {"sdk": ["./mirror/sdk", "https://mirror-2.example.org/sdk"]},
            }
        },
        file=file,
        origin="project",
        env={},
    )
    assert len(parsed) == 1
    assert parsed[0].base_domain == DOMAIN
    assert parsed[0].untrusted
    # A relative path resolves against the file that named it; a URL is
    # spelled as the base it will be used as, slash and all.
    assert parsed[0].mirrors["sdk"] == (
        str(tmp_path / "mirror" / "sdk"),
        "https://mirror-2.example.org/sdk/",
    )


def test_a_registry_may_name_its_own_trust_anchor(tmp_path: Path) -> None:
    """The third thing a registry entry may say, for anchors kept outside a project."""
    file = tmp_path / "mcuhome.yaml"
    file.write_text("", encoding="utf-8")
    parsed = parse_registries(
        {DOMAIN: {"anchor": "./anchors/private.json"}},
        file=file,
        origin="project",
        env={},
    )
    assert parsed[0].anchor == tmp_path / "anchors" / "private.json"
    # Unset stays unset: the project's own file answers then.
    assert parse_registries({DOMAIN: {}}, file=file, origin="project", env={})[0].anchor is None


def test_a_configured_anchor_replaces_the_projects_own(tmp_path: Path) -> None:
    root = project(tmp_path)
    elsewhere = tmp_path / "anchors" / "private.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b'{"keys": [], "threshold": 1}\n')
    assert trust_anchor_for(root, DOMAIN, stated=elsewhere) == elsewhere
    # …and it is used for that domain even where the project has no file
    # at all, which is the case it exists for.
    assert trust_anchor_for(bare(tmp_path), DOMAIN, stated=elsewhere) == elsewhere


def test_a_configured_anchor_that_is_not_there_is_refused(tmp_path: Path) -> None:
    """Not a fallback: a build must not check a registry against something
    other than what the operator named."""
    root = project(tmp_path)
    with pytest.raises(TrustAnchorMissing) as refusal:
        trust_anchor_for(root, DOMAIN, stated=tmp_path / "gone.json")
    assert str(tmp_path / "gone.json") in str(refusal.value)
    # …unless the project said it does not want this registry checked.
    assert trust_anchor_for(root, DOMAIN, stated=tmp_path / "gone.json", untrusted=True) is None


def test_registry_for_reads_the_configured_anchor(tmp_path: Path, keys) -> None:
    root = bare(tmp_path)
    elsewhere = tmp_path / "anchors" / f"{DOMAIN}.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(dump(anchor_document(keys, ("root-a", "root-b", "root-c"))))
    client = registry_for(
        DOMAIN,
        project_root=root,
        settings=(RegistrySettings(DOMAIN, anchor=elsewhere),),
        into=tmp_path / "fetched",
        opener=Offline(),
    )
    assert not client.untrusted


@pytest.mark.parametrize(
    "block",
    [
        "not a mapping",
        {DOMAIN: "not a mapping either"},
        {DOMAIN: {"untrusted": "yes"}},
        {DOMAIN: {"mirrors": {"sdk": "one string, not a list"}}},
        {DOMAIN: {"nonsense": True}},
        {DOMAIN: {"anchor": 7}},
        {DOMAIN: {"anchor": ""}},
    ],
)
def test_a_malformed_registry_block_is_refused(tmp_path: Path, block: object) -> None:
    from mcuhome.model.errors import ConfigError

    file = tmp_path / "mcuhome.yaml"
    file.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        parse_registries(block, file=file, origin="project", env={})


def test_layers_merge_a_whole_registry_at_a_time() -> None:
    below = (
        RegistrySettings(DOMAIN, untrusted=True, mirrors={"sdk": ("/a",)}),
        RegistrySettings("other.example.org"),
    )
    above = (RegistrySettings(DOMAIN, mirrors={"build-tools": ("/b",)}),)
    merged = {settings.base_domain: settings for settings in merge_registries(below, above)}
    assert set(merged) == {DOMAIN, "other.example.org"}
    assert not merged[DOMAIN].untrusted
    assert set(merged[DOMAIN].mirrors) == {"build-tools"}


# --------------------------------------------------------------------------
# Tiered acquisition, and the default constraint
# --------------------------------------------------------------------------


def local_source(directory: Path) -> str:
    """An operator directory holding the SDK archive and its index."""
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{SDK}-{VERSION}.tar.zst"
    (directory / filename).write_bytes(SDK_ARCHIVE)
    digest = packagefetch.sha256_file(directory / filename)
    (directory / INDEX_FILE).write_text(
        json.dumps(
            {
                "packages": {
                    SDK: {VERSION: {"file": filename, "sha256": digest, "size": len(SDK_ARCHIVE)}}
                }
            }
        ),
        encoding="utf-8",
    )
    return digest


def test_a_package_found_locally_never_touches_the_registry(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    digest = local_source(tmp_path / "operator")
    client = registry(tmp_path, anchor, bootstrap(Served().publish(MIRROR, source)))
    package = packagefetch.acquire_package(
        name=SDK,
        version=VERSION,
        sha256=digest,
        sources=(tmp_path / "operator",),
        into=tmp_path / "tree",
        registry=client,
    )
    assert (package.tree / "mcuhome-sdk.json").is_file()
    assert client._indexes == {}  # noqa: SLF001 - nothing was ever asked


def test_a_package_missing_locally_comes_off_the_registry(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    empty = tmp_path / "operator"
    empty.mkdir()
    digest = packagefetch.sha256_file(source / f"{SDK}-{VERSION}.tar.zst")
    served = bootstrap(Served().publish(MIRROR, source))
    package = packagefetch.acquire_package(
        name=SDK,
        version=VERSION,
        sha256=digest,
        sources=(empty,),
        into=tmp_path / "tree",
        registry=registry(tmp_path, anchor, served),
    )
    assert (package.tree / "bin" / "generate").stat().st_mode & 0o100
    assert not (tmp_path / "tree.download").exists()


def test_a_family_is_acquired_through_its_meta_entry(tmp_path: Path, keys) -> None:
    """The whole meta path end to end: family name in, this host's bytes out.

    The registry is asked for `mcuhome-build-tools`, which is not bytes
    at all; what comes back and is unpacked is the concrete member for
    this platform, hash-checked against the pin the meta entry's members
    state.
    """
    amd64 = build_sdk_archive({"bin/tool": (b"#!/bin/sh\n", True)})
    arm64 = build_sdk_archive({"bin/tool": (b"#!/bin/sh\n# arm\n", True)})
    tools = build_source(
        tmp_path / "served" / "build-tools",
        keys,
        packages=((AMD64, VERSION, amd64), (ARM64, VERSION, arm64)),
        meta=(TOOLS, VERSION, {"arch": {"linux-amd64": AMD64, "linux-arm64": ARM64}}),
    )
    client = PackageRegistry(
        DOMAIN,
        anchor=packageregistry.load_trust_anchor(
            _write_anchor(tmp_path / "tools-anchor.json", keys)
        ),
        into=tmp_path / "fetched",
        mirrors={"build-tools": (str(tools),)},
        opener=Offline(),
        now=NOW,
    )
    pinned = packagefetch.sha256_file(tools / f"{AMD64}-{VERSION}.tar.zst")
    empty = tmp_path / "operator"
    empty.mkdir()
    package = packagefetch.acquire_package(
        kind="build-tools",
        name=TOOLS,
        version=VERSION,
        sha256=pinned,
        sources=(empty,),
        into=tmp_path / "tree",
        registry=client,
        platform="linux-amd64",
    )
    assert package.name == AMD64
    assert (package.tree / "bin" / "tool").read_bytes() == b"#!/bin/sh\n"


def test_a_family_in_a_local_index_resolves_without_the_registry(tmp_path: Path, keys) -> None:
    """An operator directory that carries the index can answer for a family."""
    amd64 = build_sdk_archive({"bin/tool": (b"local\n", False)})
    arm64 = build_sdk_archive({"bin/tool": (b"local arm\n", False)})
    directory = build_source(
        tmp_path / "operator",
        keys,
        packages=((AMD64, VERSION, amd64), (ARM64, VERSION, arm64)),
        meta=(TOOLS, VERSION, {"arch": {"linux-amd64": AMD64, "linux-arm64": ARM64}}),
    )
    package = packagefetch.acquire_package(
        kind="build-tools",
        name=TOOLS,
        version=VERSION,
        sha256=packagefetch.sha256_file(directory / f"{AMD64}-{VERSION}.tar.zst"),
        sources=(directory,),
        into=tmp_path / "tree",
        platform="linux-amd64",
    )
    assert package.name == AMD64
    assert (package.tree / "bin" / "tool").read_bytes() == b"local\n"


def test_a_version_is_matched_the_way_pep_440_reads_it(tmp_path: Path) -> None:
    """`0.1` and `0.1.0` are one release, and an index may spell either.

    A lookup by text would walk past a package it is looking straight at,
    and the failure would read as "not published" — the one message
    guaranteed to send somebody looking in the wrong place.
    """
    directory = tmp_path / "operator"
    directory.mkdir()
    filename = f"{SDK}-0.1.tar.zst"
    (directory / filename).write_bytes(SDK_ARCHIVE)
    digest = packagefetch.sha256_file(directory / filename)
    (directory / INDEX_FILE).write_text(
        json.dumps(
            {
                "packages": {
                    SDK: {"0.1": {"file": filename, "sha256": digest, "size": len(SDK_ARCHIVE)}}
                }
            }
        ),
        encoding="utf-8",
    )
    package = packagefetch.acquire_package(
        name=SDK,
        version="0.1.0",
        sha256=digest,
        sources=(directory,),
        into=tmp_path / "tree",
    )
    assert (package.tree / "mcuhome-sdk.json").is_file()
    assert matching_version({SDK: {"0.1": {}}}, SDK, "0.1.0") == "0.1"
    assert matching_version({SDK: {"0.1": {}}}, SDK, "0.2") is None


def test_a_local_index_with_a_broken_meta_hash_refuses_rather_than_skips(
    tmp_path: Path, keys
) -> None:
    """A source the operator named on purpose is never silently demoted.

    The directory says it publishes this package and cannot describe what
    it points at. Walking on to the next source would turn a damaged
    source into a build against something else entirely.
    """
    directory = build_source(
        tmp_path / "operator",
        keys,
        packages=((AMD64, VERSION, b"amd64"), (ARM64, VERSION, b"arm64")),
        meta=(TOOLS, VERSION, {"arch": {"linux-amd64": AMD64, "linux-arm64": ARM64}}),
    )
    index = json.loads((directory / INDEX_FILE).read_text())
    index["packages"][TOOLS][VERSION]["sha256"] = "f" * 64
    (directory / INDEX_FILE).write_text(json.dumps(index), encoding="utf-8")

    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(BuildError) as refusal:
        packagefetch.acquire_package(
            kind="build-tools",
            name=TOOLS,
            version=VERSION,
            sha256="a" * 64,
            sources=(directory, second),
            into=tmp_path / "tree",
            platform="linux-amd64",
        )
    assert "does not describe the packages it points at" in refusal.value.message


def test_a_local_index_that_names_no_package_for_this_host_refuses(tmp_path: Path, keys) -> None:
    directory = build_source(
        tmp_path / "operator",
        keys,
        packages=((ARM64, VERSION, b"arm64"),),
        meta=(TOOLS, VERSION, {"arch": {"linux-arm64": ARM64}}),
    )
    with pytest.raises(BuildError) as refusal:
        packagefetch.acquire_package(
            kind="build-tools",
            name=TOOLS,
            version=VERSION,
            sha256="a" * 64,
            sources=(directory,),
            into=tmp_path / "tree",
            platform="linux-amd64",
        )
    assert "not published for linux-amd64" in refusal.value.message


def test_a_directory_without_an_index_refuses_a_foreign_architecture(
    tmp_path: Path,
) -> None:
    """The conventional filename is held to the platform check too.

    Nothing else would catch it there, and the alternative is finding out
    after the archive has been fetched, hashed and unpacked.
    """
    directory = tmp_path / "operator"
    directory.mkdir()
    (directory / f"{ARM64}-{VERSION}.tar.zst").write_bytes(SDK_ARCHIVE)
    with pytest.raises(BuildError) as refusal:
        packagefetch.acquire_package(
            kind="build-tools",
            name=ARM64,
            version=VERSION,
            sha256=packagefetch.sha256_file(directory / f"{ARM64}-{VERSION}.tar.zst"),
            sources=(directory,),
            into=tmp_path / "tree",
            platform="linux-amd64",
        )
    assert "is built for linux-arm64, and this machine is linux-amd64" in refusal.value.message


def test_a_directory_without_an_index_does_not_guess_a_family_filename(
    tmp_path: Path,
) -> None:
    """A family is not bytes, so no filename convention can name one.

    The directory holds this host's concrete archive and no index. What
    must not happen is a lookup for `mcuhome-build-tools-<version>.tar.zst`,
    which no publisher ever wrote; what happens instead is an honest
    "not here", naming the family.
    """
    directory = tmp_path / "operator"
    directory.mkdir()
    (directory / f"{AMD64}-{VERSION}.tar.zst").write_bytes(SDK_ARCHIVE)
    with pytest.raises(BuildError) as refusal:
        packagefetch.acquire_package(
            kind="build-tools",
            name=TOOLS,
            version=VERSION,
            sha256=packagefetch.sha256_file(directory / f"{AMD64}-{VERSION}.tar.zst"),
            sources=(directory,),
            into=tmp_path / "tree",
            platform="linux-amd64",
        )
    assert TOOLS in refusal.value.message


def test_a_registry_entry_that_is_not_the_pinned_bytes_is_refused(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    empty = tmp_path / "operator"
    empty.mkdir()
    served = bootstrap(Served().publish(MIRROR, source))
    with pytest.raises(BuildError) as refusal:
        packagefetch.acquire_package(
            name=SDK,
            version=VERSION,
            sha256="c" * 64,
            sources=(empty,),
            into=tmp_path / "tree",
            registry=registry(tmp_path, anchor, served),
        )
    assert "publishes" in refusal.value.message


def test_the_sdk_resolves_locally_first_and_from_the_registry_second(
    tmp_path: Path, source: Path, anchor: Path
) -> None:
    served = bootstrap(Served().publish(MIRROR, source))
    client = registry(tmp_path, anchor, served)

    empty = tmp_path / "empty"
    empty.mkdir()
    from_registry = resolve_sdk((empty,), constraint=SDK_ANY, registry=client)
    assert from_registry.source is None
    assert from_registry.base == MIRROR
    assert from_registry.url == f"{MIRROR}{SDK}-{VERSION}.tar.zst"

    local_source(tmp_path / "operator")
    locally = resolve_sdk((tmp_path / "operator",), constraint=SDK_ANY, registry=client)
    assert locally.source == tmp_path / "operator"
    assert locally.url == ""


def _sdk_source_holding(directory: Path, versions: tuple[str, ...]) -> Path:
    """An operator directory publishing exactly *versions* of the SDK."""
    directory.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (directory / f"{SDK}-{version}.tar.zst").write_bytes(SDK_ARCHIVE + version.encode())
    entries = {
        version: {
            "file": f"{SDK}-{version}.tar.zst",
            "sha256": packagefetch.sha256_file(directory / f"{SDK}-{version}.tar.zst"),
            "size": (directory / f"{SDK}-{version}.tar.zst").stat().st_size,
        }
        for version in versions
    }
    (directory / INDEX_FILE).write_text(json.dumps({"packages": {SDK: entries}}), encoding="utf-8")
    return directory


def test_the_default_constraint_pins_the_minor(tmp_path: Path) -> None:
    """A device that names no version follows patch releases, not feature ones."""
    assert sdk_constraint() == (DEFAULT_SDK_CONSTRAINT, True)
    assert sdk_constraint("sdk/mcuhome-sdk") == (DEFAULT_SDK_CONSTRAINT, True)
    # A version the device states is a stated constraint, and the ordinary
    # pre-release rule applies to it.
    assert sdk_constraint("sdk/mcuhome-sdk:0.1.9") == ("==0.1.9", None)

    # Three published versions, two of them in the pinned minor and one
    # past it: the resolution has to pick the newest patch, not the newest
    # version.
    directory = _sdk_source_holding(tmp_path / "operator", (VERSION, "0.1.4", "0.2.0"))
    constraint, prereleases = sdk_constraint()
    found = resolve_sdk((directory,), constraint=constraint, prereleases=prereleases)
    assert found.package.version == "0.1.4"


def test_the_default_resolves_the_newest_dev_release_of_the_minor(tmp_path: Path) -> None:
    """Every MCUHome package in 0.1 is a `.devN`, so the default has to take one.

    The stable-constraint rule applied to a line that publishes nothing
    but pre-releases resolves to nothing at all — not a pin, an outage,
    and the one the build server's end-to-end build hit. What the default
    must still refuse is the *next* minor, dev or not.
    """
    directory = _sdk_source_holding(
        tmp_path / "operator", ("0.1.0.dev1", "0.1.10.dev1", "0.2.0.dev1")
    )
    constraint, prereleases = sdk_constraint()
    found = resolve_sdk((directory,), constraint=constraint, prereleases=prereleases)
    assert found.package.version == "0.1.10.dev1"


def test_a_version_the_device_states_keeps_the_ordinary_pre_release_rule(
    tmp_path: Path,
) -> None:
    """A stated constraint is the user's, and the rule for it does not move.

    Somebody who wants a dev version says so; a device that pins `0.1.10`
    does not silently get `0.1.10.dev1`, which is a different SDK.
    """
    directory = _sdk_source_holding(tmp_path / "operator", ("0.1.10.dev1",))
    constraint, prereleases = sdk_constraint("sdk/mcuhome-sdk:0.1.10")
    assert (constraint, prereleases) == ("==0.1.10", None)
    # A source whose only candidate does not satisfy the constraint is a
    # source that does not hold the package, which is what the search says.
    with pytest.raises(BuildError, match="No configured SDK source holds"):
        resolve_sdk((directory,), constraint=constraint, prereleases=prereleases)

    stated, allow = sdk_constraint("sdk/mcuhome-sdk:0.1.10.dev1")
    found = resolve_sdk((directory,), constraint=stated, prereleases=allow)
    assert found.package.version == "0.1.10.dev1"


def test_the_default_still_refuses_the_next_minor(tmp_path: Path) -> None:
    directory = _sdk_source_holding(tmp_path / "operator", ("0.2.0.dev1", "0.2.0"))
    constraint, prereleases = sdk_constraint()
    with pytest.raises(BuildError, match="No configured SDK source holds"):
        resolve_sdk((directory,), constraint=constraint, prereleases=prereleases)


def test_a_source_directory_carrying_a_meta_entry_resolves_through_it(
    tmp_path: Path, tools: Path
) -> None:
    """The obligation a plain index reader used to fail on: an entry with no file."""
    document = read_document(tools / INDEX_FILE)
    shutil.copytree(tools, tmp_path / "operator")
    from mcuhome.workbench.resolve_pins import resolve_from_index

    found = resolve_from_index(document, TOOLS, f"=={VERSION}", platform="linux-amd64")
    assert found.name == AMD64
    assert found.file == f"{AMD64}-{VERSION}.tar.zst"


def test_write_signed_is_what_the_fixtures_use(source: Path, keys) -> None:
    """Guard on the fixture itself: these signatures are real, from test keys."""
    envelope = json.loads((source / (INDEX_FILE + ".sig")).read_text())
    assert envelope["signatures"][0]["keyid"] == key_id(keys["publisher"].public)
    write_signed(source / INDEX_FILE, read_document(source / INDEX_FILE), [keys["publisher"]])


# --------------------------------------------------------------------------
# The production path: a `registry:` block has to actually do something
# --------------------------------------------------------------------------


def test_a_configured_local_mirror_reaches_the_composition(tmp_path: Path, keys) -> None:
    """From `mcuhome.yaml` to the bytes, without the parts in between.

    A `registry:` block that parsed and then changed nothing would be
    configuration theatre. This drives the composition's own helper with
    what the configuration layer produces, and asserts the package the
    build would use came off the configured local mirror — offline, with
    an opener that fails the test if it is touched.
    """
    from mcuhome.workbench import build

    # Issued now rather than at the suite's fixed moment: this path goes
    # through the composition, which verifies against the real clock
    # exactly as a build does.
    source = build_source(
        tmp_path / "served" / SOURCE, keys, issued=datetime.now(UTC) - timedelta(minutes=5)
    )
    root = project(tmp_path)
    path = anchor_file(root, packageregistry.OFFICIAL_BASE_DOMAIN)
    served_keys = json.loads((source / KEYS_FILE).read_text())
    path.write_bytes(
        dump(
            {
                "version": 1,
                "threshold": served_keys["roots"]["threshold"],
                "keys": [
                    {"keyid": entry["keyid"], "public": entry["public"]}
                    for entry in served_keys["roots"]["keys"]
                ],
            }
        )
    )

    file = root / "mcuhome.yaml"
    file.write_text("", encoding="utf-8")
    settings = parse_registries(
        {
            packageregistry.OFFICIAL_BASE_DOMAIN: {
                "mirrors": {SOURCE: [str(source)]},
            }
        },
        file=file,
        origin="project",
        env={},
    )

    lines: list[str] = []
    promised = build._package_registry(  # noqa: SLF001 - the seam under test
        _model_with_default_sdk(),
        project_root=root,
        registries=settings,
        work_root=tmp_path / "wr",
        on_line=lines.append,
    )
    assert promised is not None

    empty = tmp_path / "operator"
    empty.mkdir()
    package = packagefetch.acquire_package(
        name=SDK,
        version=VERSION,
        sha256=packagefetch.sha256_file(source / f"{SDK}-{VERSION}.tar.zst"),
        sources=(empty,),
        into=tmp_path / "tree",
        registry=promised,
    )
    assert (package.tree / "mcuhome-sdk.json").is_file()
    # Verified, so nothing was warned about.
    assert lines == []


def test_a_build_without_a_project_has_no_registry(tmp_path: Path) -> None:
    """An embedder driving a bare model builds from its own directories."""
    from mcuhome.workbench import build

    assert (
        build._package_registry(  # noqa: SLF001 - the seam under test
            _model_with_default_sdk(),
            project_root=None,
            registries=(),
            work_root=tmp_path / "wr",
            on_line=None,
        )
        is None
    )


def _model_with_default_sdk():
    """The smallest thing `_package_registry` reads: `model.sources.sdk`."""
    from mcuhome.model.model import SourcesModel

    class _Model:
        sources = SourcesModel()

    return _Model()


def test_a_meta_entry_whose_hash_is_wrong_is_refused_when_it_is_pinned() -> None:
    """A pin over an unverified meta hash would be an identity over nothing.

    ``pin_entry`` is the only place a meta hash is recomputed on the
    context-creation path: the family entry is *kept* rather than
    followed, so its own hash is what a build context ends up naming and
    what the context ID is computed over. An entry whose hash does not
    match the members it points at is a damaged or tampered index, and it
    is refused before it can reach a document.
    """
    from mcuhome.workbench.packageregistry import PackageRegistryError, pin_entry

    entries = {
        "tools_linux-amd64": {"0.1.0": {"file": "a.tar.zst", "sha256": "a" * 64, "size": 1}},
        "tools": {
            "0.1.0": {"meta": {"arch": {"linux-amd64": "tools_linux-amd64"}}, "sha256": "f" * 64}
        },
    }
    with pytest.raises(PackageRegistryError) as caught:
        pin_entry(entries, "tools", "0.1.0")
    assert "does not describe the packages" in caught.value.message


def test_a_meta_entry_whose_hash_is_right_is_pinned_as_the_family() -> None:
    """The other half of the same rule: a correct entry answers as itself."""
    import hashlib

    from mcuhome.packagetool.verify import canonical_json

    from mcuhome.workbench.packageregistry import pin_entry

    entries = {
        "tools_linux-amd64": {"0.1.0": {"file": "a.tar.zst", "sha256": "a" * 64, "size": 1}},
    }
    expanded = {"arch": {"linux-amd64": {"name": "tools_linux-amd64", "sha256": "a" * 64}}}
    digest = hashlib.sha256(canonical_json(expanded)).hexdigest()
    entries["tools"] = {
        "0.1.0": {"meta": {"arch": {"linux-amd64": "tools_linux-amd64"}}, "sha256": digest}
    }
    found = pin_entry(entries, "tools", "0.1.0")
    assert (found.name, found.sha256, found.meta, found.file) == ("tools", digest, True, "")
