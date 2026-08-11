# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``local`` build method (``mcuhome/compiler/localbackend.py``).

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
from pathlib import Path
from typing import Any

import pytest
import zstandard

from mcuhome.compiler import localbackend as lb
from mcuhome.model.context import ContainerResolution, ContextRequest, SdkPin
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.workbench.contextdir import (
    lock_context,
    read_context_manifest,
    write_context_request,
)

DIGEST = "sha256:" + "1" * 64
SDK_VERSION = "0.1.0"
IMAGE = "ghcr.io/mcu-home/builder"
TAG = "zephyr-4.4.0-r7"
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
# A locked context, via the real slice-1 code path
# --------------------------------------------------------------------------


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
    (root / "model" / "device-model.json").write_text('{"model_version": 1}\n', "utf-8")
    (root / "keys").mkdir()
    (root / "keys" / "signing.pub").write_text("-----BEGIN PUBLIC KEY-----\n", "utf-8")
    for layer, name in (patches or {}).items():
        layer_dir = root / "patches" / layer
        layer_dir.mkdir(parents=True)
        (layer_dir / name).write_text("--- a\n+++ b\n", "utf-8")
    request = ContextRequest(
        sdk=SdkPin(constraint=f"=={SDK_VERSION}", version=SDK_VERSION, url="", sha256=sdk_sha),
        zephyr=zephyr,
        board=BOARD,
        created="2026-01-01T00:00:00Z",
    )
    write_context_request(request, out_dir=root)
    # The backend half's own answer, as the local build method supplies
    # it: a context states a line and the party that locks records what
    # it resolved that to (E61).
    lock_context(root, container=ContainerResolution(image=IMAGE, tag=TAG, digest=digest))
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
                "org.mcuhome.contract": "1",
                "org.mcuhome.zephyr": "4.4.0",
                "org.mcuhome.toolchain": "zephyr-0.16.8",
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
    :class:`~mcuhome.compiler.localbackend.Docker` produced, so the tests
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
            return lb.Completed(self.start_status, self.container_id + "\n")
        if verb == "exec":
            request = json.loads(Path(argv[-1]).read_text("utf-8"))
            self.exec_request = request
            self.build(request)
            return lb.Completed(self.exec_status, "compiling...\n")
        if verb == "rm":
            return lb.Completed(0, "")
        raise AssertionError(f"unexpected docker call: {argv}")


def scenario(
    tmp_path: Path,
    *,
    build,
    describe_static: str | None = None,
    facts: str | None = None,
    patches: dict[str, str] | None = None,
    index_sha: str | None = None,
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
        lb.BackendConfig(sdk_sources=(tmp_path / "src",), jobs=4), docker=lb.Docker(runner=seam)
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
    assert f"{context}:{context}:ro" in volumes  # context read-only
    assert f"{document['work']}:{document['work']}" in volumes  # work writable
    inv = str(Path(document["out"]).parent)
    assert f"{inv}:{inv}" in volumes  # the invocation dir, mounted as itself
    sdk = document["trees"]["sdk"]["path"]
    assert f"{sdk}:{sdk}:ro" in volumes  # the SDK, read-only (unpatched)
    work_root = str(Path(document["work"]).parent)
    assert f"{work_root}:{work_root}" not in volumes  # no wholesale mount


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
    assert "records" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)  # no container started


def test_an_image_of_another_zephyr_line_is_refused(tmp_path) -> None:
    """E61's requirement, checked on the backend's side of the boundary.

    The context asks for a line; the image on this host answers with its
    own ``org.mcuhome.zephyr`` label. A build in a container of another
    line is the failure mode the whole requirement exists to prevent, and
    it is refused before a container starts rather than as a compiler
    error ten minutes in.
    """
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        facts=image_facts(
            labels={
                "org.mcuhome.contract": "1",
                "org.mcuhome.zephyr": "4.5.0",
                "org.mcuhome.toolchain": "zephyr-0.16.8",
            }
        ),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "4.5.0" in caught.value.message
    assert f"{ZEPHYR} line" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_a_patch_release_of_the_required_line_serves_it(tmp_path) -> None:
    """ADR 0013: "a line, never a frozen point release"."""
    backend, context, _ = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        facts=image_facts(
            labels={
                "org.mcuhome.contract": "1",
                "org.mcuhome.zephyr": "4.4.12",
                "org.mcuhome.toolchain": "zephyr-0.16.8",
            }
        ),
    )
    outcome = backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert outcome.successful


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
        "org.mcuhome.contract": "1",
        "org.mcuhome.zephyr": "4.4.0",
        "org.mcuhome.toolchain": "zephyr-0.16.8",
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
        facts=image_facts(labels=_labels(**{"org.mcuhome.contract": "9"})),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "org.mcuhome.contract" in caught.value.message
    assert not any("--detach" in c for c in seam.calls)


def test_an_image_missing_a_coupling_label_is_refused(tmp_path) -> None:
    """§2.1.1: a container that does not carry a named coupling label does
    not qualify — absence is never read as compatible."""
    backend, context, seam = scenario(
        tmp_path,
        build=conforming,
        describe_static=describe_result_document(),
        facts=image_facts(labels={"org.mcuhome.contract": "1"}),  # no zephyr, no toolchain
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert "org.mcuhome.zephyr" in caught.value.message
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
        facts=image_facts(labels=_labels(**{"org.mcuhome.contract": "9"})),
    )
    with pytest.raises(BuildError) as caught:
        backend.run(context_dir=context, action="build", work_root=tmp_path / "work")
    assert seam.describe_invoked  # the fallback ran
    assert "org.mcuhome.contract" in caught.value.message
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
# A fresh out/tmp per invocation, while work persists (§9.1)
# --------------------------------------------------------------------------


def test_out_and_tmp_are_fresh_each_invocation_but_work_persists(tmp_path) -> None:
    """§9.1: a second run() on one work_root gets an empty out and tmp — a
    stale out/firmware.hex must not survive into a later build — while
    work, the session's persistent area, carries over."""
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
    assert work_marker_seen == [False, True]  # the marker written in run 1 survived into run 2


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
