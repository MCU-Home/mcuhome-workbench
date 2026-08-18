# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``local`` build method (``mcuhome/workbench/orchestrator.py``).

**Docker never runs here.** The same rule as ``test_container.py``: the
one impure operation is the seam, and every test either calls a pure
argv/document composer directly or injects a scripted runner that writes
the result document a real container would. What is asserted is the whole
of what the backend decides before, around and after ``docker exec`` — the
composed argv, the request document, the mount set, the §5.3 judgment on
a stubbed success and on each failure it can see, and that the container
is torn down whatever happened.

The end-to-end proof that the assembled invocation actually builds
firmware is a manual step against a real image, not a test.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import zstandard
from mcuhome.model import buildimage, containerpaths
from mcuhome.model.context import ContextRequest, EnvironmentPin, SdkPin
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.contextdir import (
    lock_context,
    read_context_manifest,
    write_context_request,
)

DIGEST = "sha256:" + "1" * 64
SDK_VERSION = "0.1.0"
IMAGE = "ghcr.io/mcu-home/build-container"
TAG = "zephyr-4.4.0-r9"
BOARD = "nrf7002dk/nrf5340/cpuapp"
ZEPHYR = "4.4"
CONTAINER_ID = "c" * 64

PROGRAM_BLOCK = {
    "id": "org.mcuhome.build-container",
    "version": "0.1.0",
    "contract": 1,
    "request": [1],
    "result": [1],
    "actions": ["describe", "verify", "build"],
    "trees": {"sdk": {"path": None}, "zephyr": {"path": "/mcuhome/workspace/zephyr"}},
}


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
    archive = build_sdk_archive(
        {
            "mcuhome-sdk.json": (
                b'{"sdk": 1, "generate": {"program": "bin/generate", "runtime": "python3"}}',
                False,
            ),
            "bin/generate": (b"#!/usr/bin/env python3\n", True),
            "mcuhome/model/__init__.py": (b'__version__ = "0.1.0"\n', False),
        }
    )
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
# A locked context, written the way §3.2 says one is written
# --------------------------------------------------------------------------
#
# Through ``conftest``'s workbench-free writer since ADR 0024: the party
# that creates a context is the workbench, which is not installed next to
# these tests, and what the backend under test needs is the *document*.
# The ID still comes from ``mcuhome.model.context``, so what is compared
# below is the value the contract fixes rather than one this file made up.


def make_context(
    root: Path,
    *,
    sdk_sha: str,
    digest: str | None = DIGEST,
    zephyr: str = ZEPHYR,
    patches: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "model").mkdir()
    (root / "model" / "device-model.json").write_text('{"model_version": 2}\n', "utf-8")
    (root / "keys").mkdir()
    (root / "keys" / "signing.pub").write_text("-----BEGIN PUBLIC KEY-----\n", "utf-8")
    for layer, name in (patches or {}).items():
        layer_dir = root / "patches" / layer
        layer_dir.mkdir(parents=True)
        (layer_dir / name).write_text("--- a\n+++ b\n", "utf-8")
    # Build the environment reference with digest
    env_ref = f"{IMAGE}:{TAG}@{digest}" if digest else f"{IMAGE}:{TAG}"
    request = ContextRequest(
        sdk=SdkPin(constraint=f"=={SDK_VERSION}", version=SDK_VERSION, url="", sha256=sdk_sha),
        build_environment=EnvironmentPin(reference=env_ref),
        board=BOARD,
        created="2026-01-01T00:00:00Z",
    )
    write_context_request(request, out_dir=root)
    # Format 3: the request carries the pinned environment, written by the
    # client. The lock reads it back and includes it in the manifest
    # unchanged (ADR 0018 amendment).
    lock_context(root)
    return root


def context_id_of(root: Path) -> str:
    return read_context_manifest(root / "manifest.yaml").compute_id()


# --------------------------------------------------------------------------
# The scripted docker seam
# --------------------------------------------------------------------------


def image_facts(*, digest: str | None = DIGEST, labels: dict[str, str] | None = None) -> str:
    facts = {
        "Id": "sha256:" + "f" * 64,
        "RepoDigests": [f"{IMAGE}@{digest}"] if digest else [],
        "Config": {
            "Labels": labels
            or {
                "org.mcuhome.build-environment.contract": "1",
                "org.mcuhome.build-environment.zephyr.version": "4.4.0",
                "org.mcuhome.build-environment.toolchain": "zephyr-0.16.8",
            }
        },
    }
    return json.dumps(facts)


def describe_result_document(program: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "result": 1,
            "status": "success",
            "action": "describe",
            "program": program or PROGRAM_BLOCK,
        }
    )


def build_result(
    request: dict[str, Any],
    *,
    context: str,
    status: str = "success",
    action: str = "build",
    session: str | None = None,
    write: dict[str, bytes] | None = None,
    layers: dict[str, Any] | None = None,
) -> None:
    """Write ``write`` under ``out`` and a conforming result document.

    Every written file is hashed from disk and declared with the one legal
    hash spelling; a test overrides any of it to produce a failure.
    """
    out = Path(request["out"])
    files = (
        write
        if write is not None
        else {"firmware.hex": b"HEX", "firmware.bin": b"BIN", "build-report.json": b'{"report": 1}'}
    )
    roles = {"firmware.hex": "firmware", "firmware.bin": "firmware", "build-report.json": "report"}
    declared: list[dict[str, Any]] = []
    for name, data in files.items():
        (out / name).write_bytes(data)
        declared.append(
            {
                "root": "out",
                "path": name,
                "role": roles.get(name, "log"),
                "hashes": {"sha256": sha256_file(out / name)},
            }
        )
    document: dict[str, Any] = {
        "result": 1,
        "status": status,
        "action": action,
        "session": session if session is not None else request.get("session"),
        "reason": None if status in ("success", "cancelled") else "error.build.failed",
        "error": None
        if status in ("success", "cancelled")
        else {"retryable": False, "message": "x"},
        "context": context,
    }
    if action == "build" and status == "success":
        document["artifacts"] = declared
        document["layers"] = layers if layers is not None else {}
    Path(request["result"]).write_text(json.dumps(document), "utf-8")


class Seam:
    """A scripted stand-in for docker, recording every argv it is handed.

    Dispatches on the composed argv the real
    :class:`~mcuhome.workbench.orchestrator.Docker` produced, so the tests
    exercise the true argv composition and can then assert it.
    """

    def __init__(
        self,
        *,
        facts: str,
        build,
        describe_static: str | None = None,
        container_id: str = CONTAINER_ID,
        start_status: int = 0,
        exec_status: int = 0,
    ) -> None:
        self.facts = facts
        self.build = build
        self.describe_static = describe_static
        self.container_id = container_id
        self.start_status = start_status
        self.exec_status = exec_status
        self.calls: list[list[str]] = []
        self.exec_request: dict[str, Any] | None = None
        self.describe_invoked = False
        #: ``container target -> host source``, learned from the mounts of
        #: the ``docker run`` that created the container. It is what makes
        #: this seam a container rather than a rename: paths in the request
        #: document are the container's, and the invocation reaches the
        #: host files through the mounts, exactly as the real one does.
        self.mounts: dict[PurePosixPath, Path] = {}

    #: The request-document fields that name a directory the program is
    #: given (§5.2). Everything else that starts with a slash is not a
    #: path: ``required`` holds JSON pointers, and a ``trees`` entry may
    #: name a tree that lives in the image and is mounted by nobody.
    PATH_FIELDS = ("result", "out", "work", "tmp", "context", "events", "cancel")

    def _host(self, path: str, *, required: bool = True) -> Path:
        """*path* as the host spells it, through this container's mounts.

        A path no ``--volume`` reaches does not exist inside a real
        container, so resolving it anyway would let the suite pass over
        the one defect this layout is about: a backend naming a directory
        the container cannot see.
        """
        inside = PurePosixPath(path)
        for target, source in self.mounts.items():
            if inside == target:
                return source
            if target in inside.parents:
                return source / inside.relative_to(target)
        if required:
            raise AssertionError(
                f"{path} is in the request document and no --volume of "
                f"{sorted(map(str, self.mounts))} mounts it: inside a real container "
                "that path does not exist"
            )
        return Path(path)

    def _host_view(self, document):
        """The request document as the host can act on it.

        Every field of :data:`PATH_FIELDS` has to be reachable through a
        mount; a ``trees`` entry and a shared cache need not be, and are
        translated only when they are.
        """
        view = dict(document)
        for key in self.PATH_FIELDS:
            if key in view:
                view[key] = str(self._host(view[key]))
        if isinstance(view.get("trees"), dict):
            view["trees"] = {
                name: {**entry, "path": str(self._host(entry["path"], required=False))}
                for name, entry in view["trees"].items()
            }
        if isinstance(view.get("ccache"), dict):
            cache = view["ccache"]
            view["ccache"] = {**cache, "path": str(self._host(cache["path"], required=False))}
        return view

    def __call__(self, argv, on_line=None) -> lb.Completed:
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ""
        if argv[1:3] == ["image", "inspect"]:
            return lb.Completed(0, self.facts)
        if verb == "run" and "cat" in argv:
            if self.describe_static is None:
                return lb.Completed(1, "no such file")
            return lb.Completed(0, self.describe_static)
        if verb == "run" and lb.PROGRAM in argv and lb.ACTION_DESCRIBE in argv:
            self.describe_invoked = True
            request = json.loads(Path(argv[-1]).read_text("utf-8"))
            Path(request["result"]).write_text(describe_result_document(), "utf-8")
            return lb.Completed(0, "")
        if verb == "run" and "--detach" in argv:
            for volume in mounts_of(argv):
                source, target = volume.removesuffix(":ro").split(":")
                self.mounts[PurePosixPath(target)] = Path(source)
            return lb.Completed(self.start_status, self.container_id + "\n")
        if verb == "rm":
            return lb.Completed(0, "")
        raise AssertionError(f"unexpected docker call: {argv}")

    def spawn(self, argv, on_line=None) -> Scripted:
        """The invocation, which is spawned rather than run.

        It plays the program synchronously and hands back a handle that
        has already finished, which is what a scripted program is: the
        supervisor's ladder then walks over a process that is done, and
        the tests that are about the ladder script one that is not
        (:class:`Hanging`).
        """
        argv = list(argv)
        self.calls.append(argv)
        assert argv[1] == "exec", f"only an invocation is spawned: {argv}"
        request = json.loads(self._host(argv[-1]).read_text("utf-8"))
        self.exec_request = request
        self.build(self._host_view(request))
        if on_line is not None:
            on_line("compiling...")
        return Scripted(self.exec_status)


class Scripted:
    """An invocation that has already finished."""

    def __init__(self, status: int | None) -> None:
        self.status = status
        self.terminated = False
        self.killed = False
        self.output = "compiling..."

    def poll(self) -> int | None:
        return self.status

    def wait(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class Ends(Scripted):
    """One that finishes on its own, after *after* polls."""

    def __init__(self, *, after: int, status: int = 0) -> None:
        super().__init__(None)
        self._after = after
        self._status = status
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.polls > self._after:
            self.status = self._status
        return self.status

    def wait(self) -> int | None:
        return self._status


class Hanging(Scripted):
    """One that does not, until a rung of the ladder reaches it.

    ``stops_at`` is which rung: ``"terminate"`` for a program that goes
    away when asked, ``"kill"`` for one that has to be made to, and
    ``None`` for one that never does — which is the case the caller has
    to survive rather than fix.
    """

    def __init__(self, *, stops_at: str | None = "terminate", status: int = 143) -> None:
        super().__init__(None)
        self._stops_at = stops_at
        self._status = status

    def poll(self) -> int | None:
        return self.status

    def wait(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.terminated = True
        if self._stops_at == "terminate":
            self.status = self._status

    def kill(self) -> None:
        self.killed = True
        if self._stops_at in ("terminate", "kill"):
            self.status = self._status


def scenario(
    tmp_path: Path,
    *,
    build,
    describe_static: str | None = None,
    facts: str | None = None,
    patches: dict[str, str] | None = None,
    index_sha: str | None = None,
    ccache_dir: Path | None = None,
    **seam_kwargs,
):
    """Build a source, a matching context, and a backend over a scripted seam.

    ``build(request, context)`` plays the invocation. ``describe_static``
    defaults to the invoked-``describe`` fallback (``cat`` fails); pass a
    document to have the image answer ``/mcuhome/describe.json`` instead.
    Returns ``(backend, context, seam)`` — the test drives ``backend.run``.
    """
    real = make_sdk_source(tmp_path / "src", index_sha=index_sha)
    context = make_context(tmp_path / "ctx", sdk_sha=real, patches=patches)
    seam = Seam(
        facts=facts or image_facts(),
        build=lambda request: build(request, context),
        describe_static=describe_static,
        **seam_kwargs,
    )
    backend = lb.LocalBackend(
        lb.BackendConfig(sdk_sources=(tmp_path / "src",), jobs=4, ccache_dir=ccache_dir),
        docker=lb.Docker(runner=seam, spawner=seam.spawn),
    )
    return backend, context, seam


def conforming(request, context) -> None:
    build_result(request, context=context_id_of(context))


def mounts_of(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == "--volume"]


def _no_nulls(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return all(_no_nulls(item) for item in value.values())
    if isinstance(value, list):
        return all(_no_nulls(item) for item in value)
    return True


# --------------------------------------------------------------------------
# Pure argv composers (§5.1, §2.2, §9.1)
# --------------------------------------------------------------------------


def test_the_invocation_argv_is_the_program_the_action_and_the_request() -> None:
    """§5.1: exactly two operands after the program, and never a flag."""
    argv = lb.exec_command(
        docker="docker",
        container=CONTAINER_ID,
        action="build",
        request=Path("/x/req.json"),
        user="1000:1000",
    )
    assert argv == [
        "docker",
        "exec",
        "--user",
        "1000:1000",
        CONTAINER_ID,
        "/mcuhome/run",
        "build",
        "/x/req.json",
    ]


def test_a_platform_without_uids_execs_without_a_user_flag() -> None:
    argv = lb.exec_command(
        docker="docker", container=CONTAINER_ID, action="verify", request=Path("/x/req"), user=None
    )
    assert "--user" not in argv
    assert argv[-3:] == ["/mcuhome/run", "verify", "/x/req"]


def test_the_container_starts_detached_isolated_and_limited() -> None:
    """§2.2/§9.1: detached, no network, PID reaping, per-session limits."""
    argv = lb.start_command(
        docker="docker",
        image="img:tag",
        mounts=[lb.Mount(Path("/ctx"), Path("/ctx"), read_only=True)],
        user="1000:1000",
        limits=lb.ResourceLimits(memory="8g", pids=512),
    )
    assert argv[:5] == ["docker", "run", "--detach", "--init", "--network=none"]
    assert argv[5:7] == ["--user", "1000:1000"]
    assert "--memory" in argv and "8g" in argv
    assert "--pids-limit" in argv and "512" in argv
    assert argv[-3:] == list(lb.IDLE_COMMAND)
    assert "/ctx:/ctx:ro" in mounts_of(argv)


def test_describe_is_a_throwaway_networkless_run() -> None:
    argv = lb.describe_run_command(
        docker="docker",
        image="img:tag",
        mounts=[lb.Mount(Path("/p"), Path("/p"))],
        request=Path("/p/req.json"),
    )
    assert argv[:5] == ["docker", "run", "--rm", "--init", "--network=none"]
    assert argv[-3:] == ["/mcuhome/run", "describe", "/p/req.json"]


def test_reading_the_static_describe_grants_nothing() -> None:
    argv = lb.read_file_command("docker", "img:tag", "/mcuhome/describe.json")
    assert argv == [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "img:tag",
        "cat",
        "/mcuhome/describe.json",
    ]


def test_inspect_asks_for_one_json_object() -> None:
    assert lb.inspect_command("docker", "img@sha256:1") == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{json .}}",
        "img@sha256:1",
    ]


# --------------------------------------------------------------------------
# The request document (§5.2)
# --------------------------------------------------------------------------


def test_the_request_document_is_the_mandatory_fields_and_no_nulls(tmp_path) -> None:
    document = lb.request_document(
        result=Path("/inv/result.json"),
        session="local-abc",
        out=Path("/inv/out"),
        work=Path("/work"),
        tmp=Path("/inv/tmp"),
        context=Path("/ctx"),
        trees={"sdk": lb.TreeEntry(Path("/sdk"), writable=False)},
        jobs=4,
        deadline_seconds=5400,
        cancel_grace_seconds=60,
        params={"mode": "clean"},
        required=("/params/mode",),
    )
    path = tmp_path / "request.json"
    lb.write_request(document, path)
    written = json.loads(path.read_text("utf-8"))
    assert written["request"] == 1
    assert written["session"] == "local-abc"
    assert written["limits"]["jobs"] == 4
    assert written["trees"]["sdk"] == {"path": "/sdk", "writable": False}
    assert written["params"]["mode"] == "clean"
    assert written["required"] == ["/params/mode"]
    assert "invocation" not in written and "invocation_id" not in written
    assert _no_nulls(written)
    for key in ("result", "out", "work", "tmp", "context"):
        assert written[key].startswith("/")


def test_memory_bytes_is_not_written_because_no_number_is_enforced() -> None:
    document = lb.request_document(
        result=Path("/r"),
        session="s",
        out=Path("/o"),
        work=Path("/w"),
        tmp=Path("/t"),
        context=Path("/c"),
        trees={"sdk": lb.TreeEntry(Path("/sdk"))},
        jobs=2,
        deadline_seconds=1,
        cancel_grace_seconds=1,
    )
    assert "memory_bytes" not in document["limits"]
    assert "params" not in document and "required" not in document


# --------------------------------------------------------------------------
# The SDK package: acquire, verify, unpack safely (§6.1, §9.1)
# --------------------------------------------------------------------------


def test_acquire_sdk_finds_verifies_and_unpacks(tmp_path) -> None:
    real = make_sdk_source(tmp_path / "src")
    into = tmp_path / "sdk"
    package = lb.acquire_sdk(
        version=SDK_VERSION, sha256=real, sources=(tmp_path / "src",), into=into
    )
    assert package.tree == into
    assert (into / "mcuhome-sdk.json").is_file()
    assert (into / "bin" / "generate").is_file()


def test_the_entry_point_keeps_its_executable_bit(tmp_path) -> None:
    """§6.1 spawns bin/generate as a child — an SDK without its exec bit
    answers exit 127 where code generation should be."""
    real = make_sdk_source(tmp_path / "src")
    into = tmp_path / "sdk"
    lb.acquire_sdk(version=SDK_VERSION, sha256=real, sources=(tmp_path / "src",), into=into)
    assert (into / "bin" / "generate").stat().st_mode & 0o100
    assert not (into / "mcuhome-sdk.json").stat().st_mode & 0o100


def test_a_wrong_hash_is_refused_as_loudly_as_a_missing_file(tmp_path) -> None:
    """The hash decides, not the name: right name, wrong bytes is refused."""
    make_sdk_source(tmp_path / "src", index_sha="b" * 64)
    with pytest.raises(BuildError) as caught:
        # The pin matches the index but not the archive's real bytes.
        lb.acquire_sdk(
            version=SDK_VERSION, sha256="b" * 64, sources=(tmp_path / "src",), into=tmp_path / "sdk"
        )
    assert "hashes to" in caught.value.message


def test_an_index_that_disagrees_with_the_pin_is_refused(tmp_path) -> None:
    make_sdk_source(tmp_path / "src", index_sha="d" * 64)
    with pytest.raises(BuildError) as caught:
        lb.acquire_sdk(
            version=SDK_VERSION, sha256="e" * 64, sources=(tmp_path / "src",), into=tmp_path / "sdk"
        )
    assert "pins" in caught.value.message


def test_no_source_holding_the_package_is_a_typed_refusal(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BuildError) as caught:
        lb.acquire_sdk(
            version=SDK_VERSION, sha256="a" * 64, sources=(empty,), into=tmp_path / "sdk"
        )
    assert lb.SDK_PACKAGE_NAME in (caught.value.message + (caught.value.hint or ""))


def test_the_safe_extractor_refuses_a_traversal(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(BuildError) as caught:
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)
    assert "unsafe path" in caught.value.message


def test_the_safe_extractor_refuses_an_absolute_path(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("/etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(BuildError):
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)


def test_the_safe_extractor_refuses_a_symlink(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(BuildError) as caught:
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)
    assert "not a regular file" in caught.value.message


# --------------------------------------------------------------------------
# describe: static file vs. invoked fallback (§2.2.1)
# --------------------------------------------------------------------------


def test_a_static_describe_is_read_instead_of_invoking_describe(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful
    assert not seam.describe_invoked  # the static file answered


def test_a_missing_static_describe_falls_back_to_invoking_it(tmp_path) -> None:
    backend, context, seam = scenario(tmp_path, build=conforming, describe_static=None)
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful
    assert seam.describe_invoked


# --------------------------------------------------------------------------
# The full lifecycle: mounts, request document, §5.3 judgment, teardown
# --------------------------------------------------------------------------


def test_a_conforming_build_is_judged_successful(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful
    assert outcome.status == "success"
    assert {a.role for a in outcome.artifacts} == {"firmware", "report"}
    assert outcome.context_id == context_id_of(context)


def test_a_second_run_does_not_inherit_the_dead_sessions_work(tmp_path) -> None:
    """One ``run()`` is one session, and its container dies with it.

    A later run must not inherit the dead session's working area: the
    session ID is drawn fresh, so the program would refuse the leftover
    as ``error.work.foreign`` — which turned every second build into
    the same build directory into a refusal (bench find, 2026-08-15).
    """
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    work_root = tmp_path / "work"
    assert backend.run(context_dir=context, action="build", work_root=work_root).successful
    residue = work_root / "work" / "session-marker-of-a-dead-session"
    residue.write_text("{}", "utf-8")
    assert backend.run(context_dir=context, action="build", work_root=work_root).successful
    assert not residue.exists()


def test_the_exec_argv_drives_the_program_through_the_run_path(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    exec_calls = [c for c in seam.calls if c[1] == "exec"]
    assert len(exec_calls) == 1
    argv = exec_calls[0]
    assert lb.PROGRAM in argv
    assert argv[-2] == "build"
    assert argv[-1].endswith("request.json")
    assert CONTAINER_ID in argv


def test_the_request_document_on_disk_is_conforming(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    document = seam.exec_request
    assert document is not None
    assert _no_nulls(document)
    assert document["params"]["mode"] == "clean"
    assert "/params/mode" in document["required"]
    assert document["session"].startswith("local-")
    assert "invocation" not in json.dumps(document)
    assert document["trees"]["sdk"]["writable"] is False
    for key in ("result", "out", "work", "tmp", "context"):
        assert Path(document[key]).is_absolute()


def test_the_mounts_are_piece_by_piece_and_never_wholesale(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    start = next(c for c in seam.calls if "--detach" in c)
    volumes = mounts_of(start)
    document = seam.exec_request
    work_root = tmp_path / "work"
    assert f"{context}:{containerpaths.CONTEXT}:ro" in volumes  # context read-only
    assert f"{work_root / 'work'}:{containerpaths.WORK}" in volumes  # work writable
    # The invocation directories' **parent**, not one of them: a mount
    # cannot be added to a running container, and an environment may run
    # more than one invocation in it.
    inv = PurePosixPath(document["out"]).parent
    assert f"{work_root / 'inv'}:{containerpaths.INVOCATIONS}" in volumes
    assert inv.parent == containerpaths.INVOCATIONS
    sdk = document["trees"]["sdk"]["path"]
    assert any(volume.endswith(f":{sdk}:ro") for volume in volumes)  # the SDK, read-only
    assert f"{work_root}:{work_root}" not in volumes  # no wholesale mount
    # Every target is the same string on every machine (containerpaths).
    assert all(volume.split(":")[1].startswith(("/mcuhome/", "/ccache/")) for volume in volumes)


def test_a_bad_session_echo_fails_the_judgment(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(
            request, context=context_id_of(ctx), session="someone-else"
        ),
        describe_static=describe_result_document(),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("echoes session" in p for p in outcome.problems)


def test_a_wrong_action_echo_fails_the_judgment(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(
            request, context=context_id_of(ctx), action="verify"
        ),
        describe_static=describe_result_document(),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("echoes action" in p for p in outcome.problems)


def test_a_context_id_mismatch_fails_the_judgment(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(request, context="sha256:" + "9" * 64),
        describe_static=describe_result_document(),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("context id" in p for p in outcome.problems)


def test_a_missing_artifact_fails_the_judgment(tmp_path) -> None:
    """A build that declares firmware.hex but does not write it: §5.3
    condition 6 fails, and success with an artifact silently absent is the
    one delivery a client cannot detect."""

    def build(request, ctx):
        out = Path(request["out"])
        (out / "build-report.json").write_bytes(b"{}")
        document = {
            "result": 1,
            "status": "success",
            "action": "build",
            "session": request["session"],
            "reason": None,
            "error": None,
            "context": context_id_of(ctx),
            "layers": {},
            "artifacts": [
                {
                    "root": "out",
                    "path": "firmware.hex",
                    "role": "firmware",
                    "hashes": {"sha256": "0" * 64},
                },
                {
                    "root": "out",
                    "path": "build-report.json",
                    "role": "report",
                    "hashes": {"sha256": sha256_file(out / "build-report.json")},
                },
            ],
        }
        Path(request["result"]).write_text(json.dumps(document), "utf-8")

    backend, context, seam = scenario(
        tmp_path, build=build, describe_static=describe_result_document()
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("firmware.hex" in p for p in outcome.problems)


def test_a_build_without_a_report_is_not_a_deliverable(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(
            request, context=context_id_of(ctx), write={"firmware.hex": b"HEX"}
        ),
        describe_static=describe_result_document(),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("report" in p for p in outcome.problems)


def test_no_result_document_is_a_failed_invocation(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: None,  # writes nothing
        describe_static=describe_result_document(),
        exec_status=1,
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert any("no result document" in p for p in outcome.problems)


def test_exit_zero_with_a_non_success_status_is_a_contract_violation(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(
            request, context=context_id_of(ctx), status="failure"
        ),
        describe_static=describe_result_document(),
        exec_status=0,
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert not outcome.successful
    assert outcome.violation is not None


def test_the_container_is_torn_down_even_when_the_build_fails(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: None,
        describe_static=describe_result_document(),
        exec_status=1,
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    rm = [c for c in seam.calls if c[1] == "rm"]
    assert rm and CONTAINER_ID in rm[0]


def test_the_recorded_digest_is_cross_checked_against_the_resolved_one(tmp_path) -> None:
    """§9.1, as format 2 leaves it: the image found is the one recorded.

    A weaker statement than the digest-pinned format's, and deliberately
    so — the digest is this side's own record rather than a client's
    demand — but it still catches the case that matters: a manifest
    naming one image while this backend is about to invoke another, which
    would attribute a build to an environment it did not run in.
    """
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        facts=image_facts(digest="sha256:" + "7" * 64),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "pinned to" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)  # no container started


# Two tests lived here and are gone with what they checked: an image of
# another Zephyr line, and a patch release of the required one. Matching
# a release against a requirement is not this side's question any more —
# a context names one image, pinned to a digest, and what this module
# checks is that the bytes on this host are those bytes
# (`test_the_recorded_digest_is_cross_checked_against_the_resolved_one`).
# Which image satisfies which constraint is decided before a context
# exists, against a registry, and is tested in `test_resolve_env.py`.


def test_a_missing_image_refuses_before_a_container_starts(tmp_path) -> None:
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return lb.Completed(1, "No such image")
        raise AssertionError("nothing else is asked once the image is missing")

    backend = lb.LocalBackend(
        lb.BackendConfig(sdk_sources=(tmp_path / "src",), jobs=4), docker=lb.Docker(runner=runner)
    )
    real = sha256_file(next((tmp_path / "src").glob("*.tar.zst")))
    context = make_context(tmp_path / "ctx", sdk_sha=real)
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "w")
    assert "answers to" in caught.value.message


# --------------------------------------------------------------------------
# verify: no params, no required, tree entries still supplied (§7.3)
# --------------------------------------------------------------------------


def test_verify_demands_no_mode_and_no_tree_pointer(tmp_path) -> None:
    def build(request, ctx):
        document = {
            "result": 1,
            "status": "success",
            "action": "verify",
            "session": request["session"],
            "reason": None,
            "error": None,
            "context": context_id_of(ctx),
        }
        Path(request["result"]).write_text(json.dumps(document), "utf-8")

    backend, context, seam = scenario(
        tmp_path, build=build, describe_static=describe_result_document()
    )
    outcome = backend.run(context_dir=context, action="verify", work_root=tmp_path / "work")
    assert outcome.successful
    document = seam.exec_request
    assert "params" not in document
    assert "required" not in document
    assert "sdk" in document["trees"]  # supplied even though verify never writes it (§7.3)


# --------------------------------------------------------------------------
# Patched layers (§4.1, §6.2, E47)
# --------------------------------------------------------------------------


def test_a_patched_in_image_layer_is_writable_at_describes_path_with_no_mount(tmp_path) -> None:
    """E47: the container's own layer is the view — writable:true asserted
    at describe's path, and no bind mount that could shadow the ro SDK."""

    def build(request, ctx):
        out = Path(request["out"])
        (out / "firmware.hex").write_bytes(b"HEX")
        (out / "build-report.json").write_bytes(b"{}")
        document = {
            "result": 1,
            "status": "success",
            "action": "build",
            "session": request["session"],
            "reason": None,
            "error": None,
            "context": context_id_of(ctx),
            "layers": {"zephyr": {"patchset": "sha256:" + "2" * 64}},
            "artifacts": [
                {
                    "root": "out",
                    "path": "firmware.hex",
                    "role": "firmware",
                    "hashes": {"sha256": sha256_file(out / "firmware.hex")},
                },
                {
                    "root": "out",
                    "path": "build-report.json",
                    "role": "report",
                    "hashes": {"sha256": sha256_file(out / "build-report.json")},
                },
            ],
        }
        Path(request["result"]).write_text(json.dumps(document), "utf-8")

    backend, context, seam = scenario(
        tmp_path,
        build=build,
        describe_static=describe_result_document(),
        patches={"zephyr": "0001-fix.patch"},
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful, outcome.problems
    document = seam.exec_request
    assert document["trees"]["zephyr"] == {"path": "/mcuhome/workspace/zephyr", "writable": True}
    assert "/trees/zephyr" in document["required"]
    start = next(c for c in seam.calls if "--detach" in c)
    assert not any("/mcuhome/workspace/zephyr" in volume for volume in mounts_of(start))


def test_derive_patch_layers_reads_the_paths_not_a_declared_list(tmp_path) -> None:
    real = make_sdk_source(tmp_path / "src")
    context = make_context(tmp_path / "ctx", sdk_sha=real, patches={"zephyr": "0001-a.patch"})
    assert lb.derive_patch_layers(context) == ("zephyr",)
    plain = make_context(tmp_path / "plain", sdk_sha=real)
    assert lb.derive_patch_layers(plain) == ()


# --------------------------------------------------------------------------
# The §7.1.1 pre-invocation gate (contract/request/result versions, labels)
# --------------------------------------------------------------------------
#
# Field presence alone is not the gate: a program block can be complete
# and still name a contract, a request format or a result format this
# backend cannot speak, and a build invoked on it would read a result
# document described by a specification this side does not have. Every
# refusal below must land before a container is ever started — the
# `--detach` run — and it must gate the static describe.json path and the
# invoked describe path alike.


def _labels(**overrides: str) -> dict[str, str]:
    base = {
        buildimage.CONTRACT_LABEL: "1",
        buildimage.ZEPHYR_LABEL: "4.4.0",
        buildimage.TOOLCHAIN_LABEL: "zephyr-0.16.8",
    }
    base.update(overrides)
    return base


def test_a_static_describe_with_a_foreign_contract_is_refused(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document({**PROGRAM_BLOCK, "contract": 2}),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "contract version" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)  # refused before a container starts


def test_a_describe_that_cannot_parse_our_request_version_is_refused(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document({**PROGRAM_BLOCK, "request": [2]}),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "request format" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_a_describe_that_cannot_write_our_result_version_is_refused(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document({**PROGRAM_BLOCK, "result": [2]}),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "result format" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_a_contract_label_that_contradicts_describe_is_refused(tmp_path) -> None:
    """§7.1.1: program.contract MUST equal the org.mcuhome.contract label;
    a disagreement is a contract violation against the image, and this
    backend refuses cleanly on it rather than building."""
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),  # program.contract == 1
        facts=image_facts(labels=_labels(**{buildimage.CONTRACT_LABEL: "9"})),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert buildimage.CONTRACT_LABEL in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_an_image_missing_a_coupling_label_is_refused(tmp_path) -> None:
    """§2.1.1: a container that does not carry a named coupling label does
    not qualify — absence is never read as compatible."""
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        facts=image_facts(labels={buildimage.CONTRACT_LABEL: "1"}),  # no zephyr, no toolchain
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert buildimage.ZEPHYR_LABEL in caught.value.message
    assert "label" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_the_invoked_describe_path_is_gated_too(tmp_path) -> None:
    """The gate is a property of the answer, not of how it was obtained:
    with no static describe.json the backend invokes describe, and the very
    same label cross-check must fire on that answer."""
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=None,  # forces the invoked describe path
        facts=image_facts(labels=_labels(**{buildimage.CONTRACT_LABEL: "9"})),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert seam.describe_invoked  # the fallback ran
    assert buildimage.CONTRACT_LABEL in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


# --------------------------------------------------------------------------
# verify_artifacts egress negatives (§9.3): links and special files rejected
# --------------------------------------------------------------------------
#
# The security fix of the module: containment is checked segment by
# segment with lstat and never with Path.resolve, so an in-out symlink is
# rejected rather than followed and served under the declared name.


def test_verify_artifacts_rejects_an_in_out_symlink(tmp_path) -> None:
    """A firmware.hex that is a symlink to another file inside out must be
    refused — resolve() would have followed it to a contained-looking path
    and re-hashed the target under the declared name."""
    out = tmp_path / "out"
    out.mkdir()
    real = out / "real.bin"
    real.write_bytes(b"HEX")
    (out / "firmware.hex").symlink_to(real)
    declared = (
        lb.Artifact(root="out", path="firmware.hex", role="firmware", sha256=sha256_file(real)),
    )
    verified, problems = lb.verify_artifacts(out, declared)
    assert verified == ()
    assert any("firmware.hex" in p for p in problems)


def test_verify_artifacts_rejects_a_hardlink(tmp_path) -> None:
    """A hardlink is a second name for bytes that may live outside out, and
    lstat cannot tell which name came first — nlink > 1 is refused."""
    out = tmp_path / "out"
    out.mkdir()
    real = out / "real.bin"
    real.write_bytes(b"HEX")
    os.link(real, out / "firmware.hex")
    declared = (
        lb.Artifact(root="out", path="firmware.hex", role="firmware", sha256=sha256_file(real)),
    )
    verified, problems = lb.verify_artifacts(out, declared)
    assert verified == ()
    assert any("hardlink" in p for p in problems)


def test_verify_artifacts_rejects_a_special_file(tmp_path) -> None:
    """A non-regular file in out — here a FIFO, the device-node case a test
    can create without root — is not a servable artifact."""
    out = tmp_path / "out"
    out.mkdir()
    os.mkfifo(out / "firmware.hex")
    declared = (lb.Artifact(root="out", path="firmware.hex", role="firmware", sha256="0" * 64),)
    verified, problems = lb.verify_artifacts(out, declared)
    assert verified == ()
    assert any("regular file" in p for p in problems)


# --------------------------------------------------------------------------
# A fresh out/tmp per invocation, and a fresh work per run (§9.1, §6.3)
# --------------------------------------------------------------------------


def test_out_tmp_and_work_are_all_fresh_each_run(tmp_path) -> None:
    """§9.1: a second run() on one work_root gets an empty out and tmp — a
    stale out/firmware.hex must not survive into a later build. And work
    is fresh too: one run() is one session whose container dies with it,
    so a later run inheriting its work would be handed a directory the
    program refuses as foreign (§6.3 — the marker can never match a
    freshly drawn session ID)."""
    work_root = tmp_path / "work"
    out_seen: list[list[str]] = []
    work_marker_seen: list[bool] = []

    def build(request, ctx):
        out = Path(request["out"])
        work = Path(request["work"])
        out_seen.append(sorted(p.name for p in out.iterdir()))
        work_marker_seen.append((work / "marker").exists())
        (work / "marker").write_text("persisted", "utf-8")
        build_result(request, context=context_id_of(ctx))

    backend, context, seam = scenario(
        tmp_path, build=build, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=work_root)
    backend.run(context_dir=context, action="build", work_root=work_root)

    assert out_seen == [[], []]  # out empty at the start of both invocations
    assert work_marker_seen == [False, False]  # a dead session's work never carries over


# --------------------------------------------------------------------------
# Teardown on an exception propagating out of the try (§ lifecycle)
# --------------------------------------------------------------------------


def test_the_container_is_torn_down_when_an_exception_propagates(tmp_path) -> None:
    """Not only a graceful non-success: an exception raised from inside the
    invocation must still leave the container reaped by the finally arm."""

    def build(request, ctx):
        raise RuntimeError("boom during exec")

    backend, context, seam = scenario(
        tmp_path, build=build, describe_static=describe_result_document()
    )
    with pytest.raises(RuntimeError, match="boom"):
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    rm = [c for c in seam.calls if c[1] == "rm"]
    assert rm and CONTAINER_ID in rm[0]


# --------------------------------------------------------------------------
# The safe extractor's remaining negatives (§9.1)
# --------------------------------------------------------------------------


def test_the_safe_extractor_refuses_a_hardlink(tmp_path) -> None:
    spool = tmp_path / "evil.tar"
    with tarfile.open(spool, "w") as tar:
        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "real"
        tar.addfile(link)
    with pytest.raises(BuildError) as caught:
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)
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
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)
    assert "not a regular file" in caught.value.message


def test_a_member_name_with_a_nul_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        lb._safe_member_name("bad\x00name")
    assert "unsafe path" in caught.value.message


def test_a_member_name_with_a_backslash_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        lb._safe_member_name("bad\\name")
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
        lb._safe_extract(spool, into=tmp_path / "out", quota_bytes=lb.SDK_MAX_BYTES)
    assert "cannot unpack" in caught.value.message


# --------------------------------------------------------------------------
# Static paths, and the compiler cache behind them
# --------------------------------------------------------------------------


def test_every_path_the_program_is_given_is_the_same_on_every_machine(tmp_path) -> None:
    """The request document names container paths, not this machine's.

    It is what makes the cache worth having — Zephyr puts three
    ``-fmacro-prefix-map=<absolute path>`` options on every compile, so a
    host path in here is a host path in every cache key — and it is why a
    build cannot tell a local backend from a build server's.
    """
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    document = seam.exec_request
    assert document["context"] == str(containerpaths.CONTEXT)
    assert document["work"] == str(containerpaths.WORK)
    assert document["out"] == str(containerpaths.invocation("inv-1") / "out")
    assert document["tmp"] == str(containerpaths.invocation("inv-1") / "tmp")
    assert document["result"] == str(containerpaths.invocation("inv-1") / "result.json")
    assert str(tmp_path) not in json.dumps(document)


def test_the_invocation_is_numbered_like_a_sessions_first_step(tmp_path) -> None:
    """One container, one invocation here — and the shape of a session's.

    A session runs several invocations over its life (the steps of one
    build), each with its own out, tmp and documents. This backend runs
    the first and only one, and spells it the same way.
    """
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert PurePosixPath(seam.exec_request["out"]).parent.name == "inv-1"
    assert PurePosixPath(seam.exec_request["out"]).parent.parent == containerpaths.INVOCATIONS


def test_both_cache_roles_are_mounted_and_only_one_is_writable(tmp_path) -> None:
    """The image decides what ccache does; the backend decides what is there.

    Writable local cache, read-only shared one, both at the paths
    /etc/ccache.conf names — so nothing has to be said in the request
    document and nothing has to be honoured by the program.
    """
    cache = tmp_path / "cache"
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        ccache_dir=cache,
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    volumes = mounts_of(next(c for c in seam.calls if "--detach" in c))
    assert f"{cache / 'cache-local'}:{containerpaths.CCACHE_LOCAL}" in volumes
    assert f"{cache / 'cache-shared'}:{containerpaths.CCACHE_SHARED}:ro" in volumes
    # Never mentioned to the program: §10's request field stays unused,
    # because the image configures both roles statically.
    assert "ccache" not in seam.exec_request


def test_the_cache_directories_exist_before_docker_could_create_them(tmp_path) -> None:
    """A missing bind-mount source is created by docker, owned by root."""
    cache = tmp_path / "cache"
    backend, context, _ = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        ccache_dir=cache,
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert (cache / "cache-local").is_dir()
    assert (cache / "cache-shared").is_dir()


def test_without_a_cache_directory_nothing_is_mounted_for_it(tmp_path) -> None:
    """A slow build, never a broken one: the cache then dies with the container."""
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    volumes = mounts_of(next(c for c in seam.calls if "--detach" in c))
    assert not any("ccache" in volume or "/ccache" in volume for volume in volumes)


def test_the_build_method_hands_the_backend_the_users_own_cache(tmp_path, monkeypatch) -> None:
    """One cache per user, resolved from the environment it was given.

    Not per project and not per build directory: its keys are content
    addresses, so two projects share an entry exactly when the
    compilation is the same one — and the work directory the cache used
    to live in is wiped before every build.
    """
    from types import SimpleNamespace

    from mcuhome.workbench import containerbuild

    captured: dict[str, lb.BackendConfig] = {}

    class Stub:
        def __init__(self, config, *, docker) -> None:
            captured["config"] = config

        def run(self, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(out=None)

    monkeypatch.setattr(lb, "LocalBackend", Stub)
    # `run_locked_build` reads the manifest for the image it reports, so
    # the directory has to be a real locked context rather than a name.
    make_context(tmp_path / "ctx", sdk_sha="ab" * 32)
    containerbuild.run_locked_build(
        tmp_path / "ctx",
        sdk_sources=(),
        work_root=tmp_path / "work",
        env={"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
        docker=lb.Docker(runner=lambda argv, on_line=None: lb.Completed(0, "")),
    )
    assert captured["config"].ccache_dir == tmp_path / "xdg" / "mcuhome" / "ccache"


def test_a_caller_whose_environment_names_no_home_still_builds(tmp_path, monkeypatch) -> None:
    """No home directory means no cache — never a refused build.

    A service, a container or a test may run with no ``HOME`` at all, and
    the refusal :func:`mcuhome.model.userpaths.home` raises was written
    for the signing key, where guessing a directory would be wrong. A
    compiler cache is an optimization: its absence costs time.
    """
    from types import SimpleNamespace

    from mcuhome.workbench import containerbuild

    captured: dict[str, lb.BackendConfig] = {}

    class Stub:
        def __init__(self, config, *, docker) -> None:
            captured["config"] = config

        def run(self, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(out=None)

    monkeypatch.setattr(lb, "LocalBackend", Stub)
    # `run_locked_build` reads the manifest for the image it reports, so
    # the directory has to be a real locked context rather than a name.
    make_context(tmp_path / "ctx", sdk_sha="ab" * 32)
    containerbuild.run_locked_build(
        tmp_path / "ctx",
        sdk_sources=(),
        work_root=tmp_path / "work",
        env={},
        docker=lb.Docker(runner=lambda argv, on_line=None: lb.Completed(0, "")),
    )
    assert captured["config"].ccache_dir is None


# --------------------------------------------------------------------------
# The environment: one container, more than one invocation
# --------------------------------------------------------------------------


def test_an_environment_runs_several_invocations_in_one_container(tmp_path) -> None:
    """What ``open`` exists for, and what ``run`` hides.

    A build server holds one environment per session and drives
    ``verify`` and then ``build`` through it. Both statements the
    contract makes about a *session* — patches applied once per session
    (§6.2) and the session marker in ``work`` (§6.3) — are about
    something that outlives one invocation, so an orchestrator that
    started a container per action could honour neither.
    """
    # The action is an argv argument and not a request-document field, so
    # the scripted program is told which answer to write the same way the
    # invocations are ordered.
    answers = iter(("verify", "build"))
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: build_result(
            request, context=context_id_of(ctx), action=next(answers)
        ),
        describe_static=describe_result_document(),
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        first = environment.invoke("verify")
        second = environment.invoke("build")

    assert first.successful and second.successful
    assert len([call for call in seam.calls if "--detach" in call]) == 1
    assert len([call for call in seam.calls if call[1:2] == ["exec"]]) == 2


def test_every_invocation_of_one_environment_states_the_same_session(tmp_path) -> None:
    """§6.3: ``work`` carries a session marker, and a session is the environment.

    A fresh id per invocation would make the second one find the first
    one's working area foreign — which is precisely the refusal §6.3
    exists to produce for a working area left by a *dead* session.
    """
    sessions = []
    backend, context, seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            sessions.append(request["session"]),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        environment.invoke("build")
        environment.invoke("build")

    assert len(sessions) == 2
    assert sessions[0] == sessions[1]


def test_two_environments_are_two_sessions(tmp_path) -> None:
    """And the marker is what makes them different, so it must not repeat."""
    seen = []
    backend, context, _seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            seen.append(request["session"]),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert seen[0] != seen[1]


def test_each_invocation_gets_its_own_empty_directory(tmp_path) -> None:
    """§9.1's fresh ``out`` and ``tmp``, and the reason is egress.

    A stale ``out/firmware.hex`` that still matched a re-declared hash
    would let a later non-conforming build slip through, and an old
    ``result.json`` would be judged as this invocation's answer.
    """
    directories = []
    backend, context, _seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            directories.append(Path(request["out"]).parent),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        environment.invoke("build")
        environment.invoke("build")

    assert directories[0] != directories[1]
    assert all(entry.parent == tmp_path / "work" / "inv" for entry in directories)


def test_closing_an_environment_reaps_its_container_once(tmp_path) -> None:
    """And closing twice is not an error: a caller may close what a context manager did."""
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    environment = backend.open(context_dir=context, work_root=tmp_path / "work")
    environment.close()
    environment.close()
    assert len([call for call in seam.calls if call[1:2] == ["rm"]]) == 1


def test_a_closed_environment_refuses_to_invoke(tmp_path) -> None:
    """The container is gone; an exec into it would fail with docker's words, not ours."""
    backend, context, _seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    environment = backend.open(context_dir=context, work_root=tmp_path / "work")
    environment.close()
    with pytest.raises(RuntimeError):
        environment.invoke("build")


def test_an_action_the_image_does_not_announce_is_refused_before_it_is_invoked(tmp_path) -> None:
    """§7.1.1: "a backend MUST NOT invoke an action absent from the list"."""
    program = json.loads(json.dumps(PROGRAM_BLOCK))
    program["actions"] = ["describe", "build"]
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(program),
    )
    with (
        backend.open(context_dir=context, work_root=tmp_path / "work") as environment,
        pytest.raises(BuildError),
    ):
        environment.invoke("verify")
    assert not [call for call in seam.calls if call[1:2] == ["exec"]]


# --------------------------------------------------------------------------
# §8: the event stream, and the ladder that stops an invocation
# --------------------------------------------------------------------------


def emit(request: dict[str, Any], *events: dict[str, Any]) -> None:
    """Append *events* to the file the request document named."""
    with Path(request["events"]).open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
            handle.flush()


def test_the_request_document_offers_an_event_file_and_a_cancel_sentinel(tmp_path) -> None:
    """Both are §5.2 optional and both are offered, for opposite reasons.

    Without ``events`` a program has nowhere to report its phases and
    ``describe``'s registry is decoration; without ``cancel`` there is no
    stop signal at all, because "killing a ``docker exec`` client does
    not kill the process inside the container".
    """
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    document = seam.exec_request
    assert document is not None
    inv = PurePosixPath(document["out"]).parent
    assert document["events"] == str(inv / "events.ndjson")
    assert document["cancel"] == str(inv / "cancel")


def test_the_event_file_exists_before_the_program_is_invoked(tmp_path) -> None:
    """§8: created empty by the backend.

    A reader that had to tell "not created yet" from "no events yet"
    would be guessing at exactly the moment somebody is watching.
    """
    existed = []
    backend, context, _seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            existed.append(Path(request["events"]).is_file()),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert existed == [True]


def test_every_event_the_program_writes_is_relayed_verbatim(tmp_path) -> None:
    """§8: "unknown names are relayed opaquely … never rewrites it".

    The third one carries a name no registry has, which is exactly the
    case that makes a third-party program's phases readable through a
    backend that never heard of them.
    """
    seen: list[dict[str, Any]] = []
    backend, context, _seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            emit(
                request,
                {"event": "invocation.started", "seq": 1, "action": "build"},
                {
                    "event": "build.image.started",
                    "seq": 2,
                    "image": "app",
                    "current": 1,
                    "total": 2,
                },
                {"event": "x-vendor.thing", "seq": 3, "whatever": [1, 2, 3]},
            ),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        environment.invoke("build", on_event=seen.append)

    assert [event["event"] for event in seen] == [
        "invocation.started",
        "build.image.started",
        "x-vendor.thing",
    ]
    assert seen[2]["whatever"] == [1, 2, 3]


def test_rubbish_in_the_event_stream_is_dropped_and_not_an_abort(tmp_path) -> None:
    """§8: discarded and counted, "never treated as an abort".

    A program that writes nonsense into its own event stream has not
    failed its build.
    """
    seen: list[dict[str, Any]] = []

    def program(request, ctx) -> None:
        Path(request["events"]).write_text(
            "not json\n"
            + json.dumps([1, 2])
            + "\n"
            + json.dumps({"no": "name"})
            + "\n"
            + json.dumps({"event": "invocation.finished", "seq": 9, "status": "success"})
            + "\n",
            encoding="utf-8",
        )
        build_result(request, context=context_id_of(ctx))

    backend, context, _seam = scenario(
        tmp_path, build=program, describe_static=describe_result_document()
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        outcome = environment.invoke("build", on_event=seen.append)

    assert outcome.successful
    assert [event["event"] for event in seen] == ["invocation.finished"]


def test_a_caller_that_wants_no_events_is_offered_none(tmp_path) -> None:
    """No sink, no reading: the file is still there, and nobody drains it."""
    backend, context, _seam = scenario(
        tmp_path,
        build=lambda request, ctx: (
            emit(request, {"event": "invocation.started", "seq": 1, "action": "build"}),
            build_result(request, context=context_id_of(ctx)),
        )[-1],
        describe_static=describe_result_document(),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful


def test_touching_the_sentinel_is_how_an_invocation_is_stopped(tmp_path) -> None:
    """§8: the *existence* of the file means stop, and that is the whole signal.

    A caller holds the prepared invocation, so it has the path before
    the call that blocks — which is why ``prepare`` is a step of its own.
    """
    backend, context, _seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    with backend.open(context_dir=context, work_root=tmp_path / "work") as environment:
        invocation = environment.prepare("build")
        assert not invocation.cancel.exists()
        invocation.stop()
        assert invocation.cancel.exists()
        invocation.stop()  # asking twice is not an error


@pytest.fixture
def brisk(monkeypatch):
    """The ladder, with its two waits taken out.

    Both are widths of a window rather than policy — how long a program
    has to notice and how long it has to obey — so a test that wanted to
    see the next rung would otherwise be a test that waits.
    """
    monkeypatch.setattr(lb, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(lb, "_KILL_AFTER_SECONDS", 0.0)


def test_the_ladder_asks_before_it_signals(tmp_path, brisk) -> None:
    """The sentinel is rung one, and it is the only one that lets a program answer.

    A program that stops itself writes ``status: "cancelled"``; one that
    was killed writes nothing at all. So nothing is signalled until the
    grace period has run out.
    """
    cancel = tmp_path / "cancel"
    cancel.touch()
    obedient = Ends(after=3)
    liveness = lb.Liveness(cancel=cancel, deadline_seconds=3600, cancel_grace_seconds=3600)
    assert liveness.supervise(obedient) == 0
    assert not obedient.terminated
    assert not obedient.killed


def test_a_program_that_ignores_the_sentinel_is_signalled(tmp_path, brisk) -> None:
    """Rung two, and in a container what it reaches is the exec client.

    Not the build inside it — killing an exec client never has been —
    so what this buys back is this side's own ability to answer. What
    stops the build is reaping the container.
    """
    child = Hanging(stops_at="terminate")
    cancel = tmp_path / "cancel"
    cancel.touch()
    liveness = lb.Liveness(cancel=cancel, deadline_seconds=3600, cancel_grace_seconds=0)
    assert liveness.supervise(child) == 143
    assert child.terminated


def test_a_program_that_ignores_the_signal_is_killed(tmp_path, brisk) -> None:
    child = Hanging(stops_at="kill")
    cancel = tmp_path / "cancel"
    cancel.touch()
    liveness = lb.Liveness(cancel=cancel, deadline_seconds=3600, cancel_grace_seconds=0)
    assert liveness.supervise(child) == 143
    assert child.terminated and child.killed


def test_the_deadline_enters_at_the_top_of_the_ladder(tmp_path, brisk) -> None:
    """``limits.deadline_seconds`` is advisory to the program and enforced here.

    Enforced by touching the same sentinel rather than by signalling, so
    a program that honours it stops itself and says
    ``error.deadline.exceeded`` — which a killed one could never say.
    """
    cancel = tmp_path / "cancel"
    child = Ends(after=2)
    liveness = lb.Liveness(cancel=cancel, deadline_seconds=0, cancel_grace_seconds=3600)
    assert liveness.supervise(child) == 0
    assert cancel.exists(), "the deadline asked before anything else did"
    assert not child.terminated, "and the grace period was still running"


def test_events_are_drained_while_the_invocation_runs(tmp_path, brisk) -> None:
    """The same clock as the ladder, because a second one is a second thing to get wrong.

    And one last drain after it ends: the program's own
    ``invocation.finished`` is written immediately before the result
    document and lands between the final poll and the exit.
    """
    drained = []
    child = Ends(after=2)
    liveness = lb.Liveness(
        cancel=tmp_path / "cancel", deadline_seconds=3600, cancel_grace_seconds=3600
    )
    liveness.supervise(child, on_poll=lambda: drained.append(child.polls))
    assert len(drained) >= 3, drained


def test_a_caller_can_label_the_containers_it_starts(tmp_path) -> None:
    """Backend policy, not contract: §2.1 governs image labels and this is a container one.

    A long-running caller uses it so that an operator can find the
    containers of a process that was killed outright; a command line
    passes none, because it reaps its own before it exits.
    """
    real = make_sdk_source(tmp_path / "src")
    context = make_context(tmp_path / "ctx", sdk_sha=real)
    seam = Seam(
        facts=image_facts(),
        build=lambda request: conforming(request, context),
        describe_static=describe_result_document(),
    )
    backend = lb.LocalBackend(
        lb.BackendConfig(
            sdk_sources=(tmp_path / "src",),
            jobs=1,
            labels={"org.mcuhome.build-server.session": "s-7"},
        ),
        docker=lb.Docker(runner=seam, spawner=seam.spawn),
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    start = next(call for call in seam.calls if "--detach" in call)
    assert "--label" in start
    assert start[start.index("--label") + 1] == "org.mcuhome.build-server.session=s-7"


def test_nothing_is_labelled_when_nobody_asked(tmp_path) -> None:
    backend, context, seam = scenario(
        tmp_path, build=conforming, describe_static=describe_result_document()
    )
    backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "--label" not in next(call for call in seam.calls if "--detach" in call)
