# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a ``local`` build from a device model (``localbuild.py``).

**Docker never runs here.** The one impure operation is the seam: a
scripted stand-in dispatches on the argv the real
:class:`~mcuhome.compiler.localbackend.Docker` composed and writes the
result document a real container would. What is asserted is the
composition above the backend — since the ADR 0024 inversion it lives in
the workbench (:func:`mcuhome.workbench.buildmethods.compose_local_build`)
over the compiler's two halves: that a device model becomes a locked
context and one ``build`` invocation, that the two typed refusals E54 asks
for (a missing image, a missing SDK source) land before a container
starts, and — the E55 security invariant — that the **private** key never
appears in any docker argv and the context carries only the public half.

The seam and the SDK-source fixture used to be imported from
``test_localbackend.py``, which went to ``mcuhome-sdk`` with the backend
it tests. They are restated below rather than reached across a repository
boundary, and they are deliberately the *smaller* half: the backend's own
suite over there builds a context too, this one must not — the whole
subject here is that ``compose_local_build`` creates and locks the
context itself, so a context these tests wrote would be the one thing
capable of hiding a defect in it.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
import zstandard
from conftest import EXAMPLES_DIR, resolve_file
from mcuhome.compiler import localbackend as lb
from mcuhome.compiler import localbuild
from mcuhome.compiler.localbackend import Docker
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import buildmethods
from mcuhome.workbench.contextdir import read_context_manifest
from mcuhome.workbench.resolve_pins import SDK_ANY, resolve_sdk_pin
from mcuhome.workbench.signing import (
    generate_key_pem,
    looks_like_p256_key,
    looks_like_p256_public_key,
    public_key_pem,
)

#: A P-256 key with a known scalar, so this module never draws one.
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0

DIGEST = "sha256:" + "1" * 64
SDK_VERSION = "0.1.0"
IMAGE = "ghcr.io/mcu-home/build-container"
CONTAINER_ID = "c" * 64

#: The ``program`` block a conforming image answers ``describe`` with
#: (build-container-contract.md §7.1). Only the fields the preflight
#: judges are load-bearing here.
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


def make_sdk_source(directory: Path) -> str:
    """A source directory with one SDK archive and the index that names it.

    Returns the archive's **real** sha256 — the value the pin resolution
    reads out of the index and writes into the context it creates.
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
            "mcuhome-sdk": {SDK_VERSION: {"file": filename, "sha256": real, "size": len(archive)}}
        }
    }
    (directory / "index.json").write_text(json.dumps(index), "utf-8")
    return real


# --------------------------------------------------------------------------
# The scripted docker seam
# --------------------------------------------------------------------------


def image_facts(*, digest: str | None = DIGEST, labels: dict[str, str] | None = None) -> str:
    """What ``docker image inspect`` answers for a conforming image."""
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
) -> None:
    """Write the artifacts under ``out`` and a conforming result document.

    Every written file is hashed from disk and declared with the one legal
    hash spelling, so the §5.3 judgment the backend performs runs over a
    document that really matches what is on disk.
    """
    out = Path(request["out"])
    files = {"firmware.hex": b"HEX", "firmware.bin": b"BIN", "build-report.json": b'{"report": 1}'}
    roles = {"firmware.hex": "firmware", "firmware.bin": "firmware", "build-report.json": "report"}
    declared: list[dict[str, Any]] = []
    for name, data in files.items():
        (out / name).write_bytes(data)
        declared.append(
            {
                "root": "out",
                "path": name,
                "role": roles[name],
                "hashes": {"sha256": sha256_file(out / name)},
            }
        )
    document: dict[str, Any] = {
        "result": 1,
        "status": status,
        "action": action,
        "session": request.get("session"),
        "reason": None if status in ("success", "cancelled") else "error.build.failed",
        "error": None
        if status in ("success", "cancelled")
        else {"retryable": False, "message": "x"},
        "context": context,
    }
    if action == "build" and status == "success":
        document["artifacts"] = declared
        document["layers"] = {}
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


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


@pytest.fixture
def public_pem() -> str:
    return public_key_pem(generate_key_pem(TEST_SCALAR))


def _conforming(request) -> None:
    """Play a conforming build, computing the context id the backend expects.

    The context id is not known until the composition has created
    and locked the context, so the seam reads it back from the manifest at
    the ``context`` path the request names — exactly what a real program
    does when it computes ``result.context`` from the context as mounted.
    """
    manifest = read_context_manifest(Path(request["context"]) / "manifest.yaml")
    build_result(request, context=manifest.compute_id())


def _seam(**overrides) -> Seam:
    return Seam(
        facts=overrides.pop("facts", image_facts()),
        build=overrides.pop("build", _conforming),
        describe_static=overrides.pop("describe_static", describe_result_document()),
        **overrides,
    )


def _flatten(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(argv) for argv in calls)


# --------------------------------------------------------------------------
# The happy path: model -> context -> one build invocation
# --------------------------------------------------------------------------


def test_run_local_build_composes_a_context_and_drives_one_build(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")
    seam = _seam()
    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        jobs=2,
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful, result.outcome.problems
    assert result.image == IMAGE
    # A locked context was created from the model, with the pins the pin
    # resolution produced.
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.board == model.device.board
    # The delivered artifacts are where the result says they are.
    assert (result.out_dir / "firmware.bin").is_file()
    assert (result.out_dir / "build-report.json").is_file()
    assert {a.role for a in result.outcome.artifacts} == {"firmware", "report"}


def test_the_composition_states_its_steps_in_order(tmp_path, model, public_pem):
    """``context`` before one exists, ``compile`` before the drive.

    The honest-progress seam of cli ADR 0004: a caller renders steps it
    was told about, and these two are the ones this composition owns.
    The context reports itself twice — once on entry, once with what it
    turned out to be (PO 2026-08-16) — and the facts are read back off
    the locked directory rather than remembered here, so what a caller
    renders is what the build environment receives.
    """
    make_sdk_source(tmp_path / "src")
    steps: list[tuple[str, dict]] = []
    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        on_step=lambda stage, **facts: steps.append((stage, facts)),
        docker=Docker(runner=_seam()),
    )
    assert result.outcome.successful
    assert [stage for stage, _facts in steps] == ["context", "context", "compile"]
    assert steps[0][1] == {}
    facts = steps[1][1]
    assert facts["zephyr"] == model.toolchain.zephyr_line
    assert facts["board"] == model.device.board
    assert facts["patches"] == []
    assert facts["files"] >= 2  # the model and the public key, at least
    assert facts["id"].startswith("sha256:")
    assert (
        facts["sdk_sha256"]
        == read_context_manifest(tmp_path / "wr" / "context" / "manifest.yaml").sdk.sha256
    )
    assert steps[2][1]["image"] == IMAGE


# --------------------------------------------------------------------------
# E55: the private key is never passed, never mounted, never in an argv
# --------------------------------------------------------------------------


def test_the_private_key_never_appears_in_any_docker_argv(tmp_path, model):
    """The container gets keys/signing.pub and nothing else of the key pair.

    A private key file exists on this host, and its bytes and its path are
    grepped for across every composed docker command the backend produced —
    the run that starts the container, the exec that invokes the program,
    every mount argument. It appears in none of them, because
    :func:`~mcuhome.workbench.buildmethods.compose_local_build` has no way
    to receive it: its only key input is the public PEM.
    """
    private_pem = generate_key_pem(TEST_SCALAR)
    private_path = tmp_path / "signing.key"
    private_path.write_text(private_pem, encoding="utf-8")
    public_pem = public_key_pem(private_pem)

    make_sdk_source(tmp_path / "src")
    seam = _seam()
    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful

    # The container really was started and the program really was invoked —
    # so the grep below is over a real invocation, not an empty one.
    assert any("--detach" in argv for argv in seam.calls)
    assert any(argv[1] == "exec" for argv in seam.calls)

    flat = _flatten(seam.calls)
    assert str(private_path) not in flat
    assert "PRIVATE KEY" not in flat  # no private key material rode along in an argv
    # What the context does carry is the public half, and only that.
    signing_pub = (result.context_dir / "keys" / "signing.pub").read_text(encoding="utf-8")
    assert looks_like_p256_public_key(signing_pub)
    assert not looks_like_p256_key(signing_pub)
    # And the context is mounted read-only, so even the public key cannot
    # be written back by the container.
    start = next(argv for argv in seam.calls if "--detach" in argv)
    mounts = [start[i + 1] for i, item in enumerate(start) if item == "--volume"]
    assert f"{result.context_dir}:{result.context_dir}:ro" in mounts


# --------------------------------------------------------------------------
# The two typed refusals E54 asks be surfaced cleanly (image, SDK source)
# --------------------------------------------------------------------------


def test_a_missing_image_refuses_before_a_container_starts(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return _missing_image()
        raise AssertionError(f"nothing else is asked once the image is missing: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "is missing on this host" in caught.value.message


def test_an_image_of_another_zephyr_line_refuses_before_anything_is_written(
    tmp_path, model, public_pem
):
    """E61's requirement, checked by the half that states it.

    The composition plays both roles: it writes the requirement into
    ``context.yaml`` and it answers that requirement out of this host's
    images. So the mismatch is caught here, before the context directory
    or the SDK lookup exist — a refusal that costs nothing and names both
    the line the device needs and the line the image carries.
    """
    make_sdk_source(tmp_path / "src")
    seen: list[list[str]] = []

    def runner(argv, on_line=None):
        seen.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return lb.Completed(
                0,
                image_facts(
                    labels={
                        "org.mcuhome.contract": "1",
                        "org.mcuhome.zephyr": "4.5.0",
                        "org.mcuhome.toolchain": "zephyr-0.16.8",
                    }
                ),
            )
        raise AssertionError(f"nothing else is asked once the line does not match: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "4.5.0" in caught.value.message
    assert model.toolchain.zephyr_line in caught.value.message
    assert not (tmp_path / "wr" / "context").exists(), "nothing was written"


def test_an_image_with_no_zephyr_label_refuses_before_anything_is_written(
    tmp_path, model, public_pem
):
    """ "Absence is never read as compatible" (§2.1.1) — here too.

    An image carrying no ``org.mcuhome.zephyr`` label states nothing
    about what it builds against, and there used to be a fallback here
    that read the workbench's own pin into that silence. It was wrong
    twice over. ``LocalBackend._resolve_image`` performs this identical
    match a few steps later with no fallback, so the image was admitted
    here and refused there — after the context directory, the SDK lookup
    and the manifest lock had been paid for, which is precisely what the
    comment above this check promises will not happen. And when the
    required line was one the invented value did not satisfy, the refusal
    said "carries Zephyr 4.4.0" about an image that had said nothing.

    So: refused here, and named for what is actually wrong with it.
    """
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return lb.Completed(0, image_facts(labels={"org.mcuhome.contract": "1"}))
        raise AssertionError(f"nothing else is asked once the line does not match: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "carries no org.mcuhome.zephyr label" in caught.value.message
    assert model.toolchain.zephyr_line in caught.value.message
    assert not (tmp_path / "wr" / "context").exists(), "nothing was written"


def test_no_sdk_source_configured_is_a_typed_refusal(tmp_path, model, public_pem):
    calls: list[list[str]] = []

    def runner(argv, on_line=None):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return _image_ok()
        raise AssertionError(f"no container should start with no SDK: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "SDK source" in caught.value.message
    assert not any("--detach" in argv for argv in calls)


def test_a_source_without_the_package_is_a_typed_refusal(tmp_path, model, public_pem):
    empty = tmp_path / "empty"
    empty.mkdir()

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return _image_ok()
        raise AssertionError(f"no container should start: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(empty,),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert localbuild.lb.SDK_PACKAGE_NAME in caught.value.message


# --------------------------------------------------------------------------
# A locally built image carries no repo digest — the placeholder path
# --------------------------------------------------------------------------


def test_a_local_image_without_a_digest_still_builds(tmp_path, model, public_pem):
    """A ``--container-image localhost/…`` names no pinnable bytes; the build proceeds.

    ``docker image inspect`` reports no ``RepoDigests``, and under context
    format 2 the manifest records exactly that — ``digest: null`` — rather
    than the placeholder the digest-pinned format needed because a hashed
    field could not be absent. Nothing depends on the value: it is this
    backend's record of what it chose, and the record is honest about an
    image whose bytes nobody can fetch.
    """
    make_sdk_source(tmp_path / "src")
    seam = _seam(facts=image_facts(digest=None))
    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image="localhost/builder:wip",
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful, result.outcome.problems
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.container.digest is None
    assert manifest.container.image == "localhost/builder"
    assert manifest.container.tag == "wip"


# --------------------------------------------------------------------------
# resolve_sdk_pin in isolation
# --------------------------------------------------------------------------


def test_resolve_sdk_pin_reads_the_source_index(tmp_path):
    real = make_sdk_source(tmp_path / "src")
    constraint, version, sha256 = resolve_sdk_pin((tmp_path / "src",))
    assert sha256 == real
    assert version
    assert constraint == SDK_ANY


def test_resolve_sdk_pin_resolves_a_dev_only_source_under_any(tmp_path):
    """SDK_ANY means the newest, and during development that is a dev release.

    The regression this pins: the E52 pre-release rule (a dev version
    satisfies only a pre-release constraint) is right for a real pin like
    ``~=2.3`` and wrong for "any" — SDK_ANY is literally any, including a
    ``0.1.0.dev0``. An earlier version resolved SDK_ANY as a stable
    specifier and refused a dev-only source, which is exactly what every
    build did before the first stable release: the source directory holds
    one archive and it carries the ``.dev0`` version.
    """
    import json

    source = tmp_path / "src"
    source.mkdir()
    (source / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "mcuhome-sdk": {
                        "0.1.0.dev0": {
                            "file": "mcuhome-sdk-0.1.0.dev0.tar.zst",
                            "sha256": "ab" * 32,
                            "size": 100,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    constraint, version, sha256 = resolve_sdk_pin((source,))
    assert version == "0.1.0.dev0"
    assert sha256 == "ab" * 32
    assert constraint == SDK_ANY


def test_resolve_sdk_pin_without_a_source_refuses(tmp_path):
    with pytest.raises(BuildError) as caught:
        resolve_sdk_pin(())
    assert "SDK source" in caught.value.message


def _image_ok():
    return lb.Completed(0, image_facts())


def _missing_image():
    return lb.Completed(1, "No such image")
