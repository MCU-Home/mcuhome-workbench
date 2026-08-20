# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a ``local`` build from a device model (``containerbuild.py``).

**Docker never runs here.** The one impure operation is the seam: a
scripted stand-in dispatches on the argv the real
:class:`~mcuhome.workbench.orchestrator.Docker` composed and writes the
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
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import zstandard
from conftest import (
    ENVIRONMENT_DIGEST,
    ENVIRONMENT_PIN,
    EXAMPLES_DIR,
    ScriptedRegistry,
    resolve_file,
)
from mcuhome.model import buildimage, containerpaths
from mcuhome.model.context import EnvironmentPin
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import buildmethods, containerbuild
from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.contextdir import create_build_context, read_context_manifest
from mcuhome.workbench.orchestrator import Docker
from mcuhome.workbench.resolve_pins import SDK_ANY, resolve_sdk_pin
from mcuhome.workbench.signing import (
    generate_key_pem,
    looks_like_p256_key,
    looks_like_p256_public_key,
    public_key_pem,
)

#: A P-256 key with a known scalar, so this module never draws one.
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0

#: The digest the scripted registry pins and the fake image reports —
#: one value, because the backend cross-checks the two and a test where
#: they differed would be testing the cross-check by accident.
DIGEST = ENVIRONMENT_DIGEST
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
                buildimage.CONTRACT_LABEL: "1",
                buildimage.ZEPHYR_LABEL: "4.4.0",
                buildimage.TOOLCHAIN_LABEL: "zephyr-0.16.8",
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
        #: ``container target -> host source``, from the mounts of the
        #: ``docker run`` that created the container. Container paths are
        #: the same for every session now, so this map is the only way
        #: back to a host file — which is the container's own situation.
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
        if verb == "version":
            return lb.Completed(0, "20.10.0\n")
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
            for volume in [argv[i + 1] for i, item in enumerate(argv) if item == "--volume"]:
                source, target = volume.removesuffix(":ro").split(":")
                self.mounts[PurePosixPath(target)] = Path(source)
            return lb.Completed(self.start_status, self.container_id + "\n")
        if verb == "rm":
            return lb.Completed(0, "")
        raise AssertionError(f"unexpected docker call: {argv}")

    def spawn(self, argv, on_line=None):
        """The invocation, which is spawned rather than run.

        It plays the scripted program synchronously and answers with a
        handle that has already finished — the shape a supervisor walks
        over without a rung ever firing.
        """
        argv = list(argv)
        self.calls.append(argv)
        assert argv[1] == "exec", f"only an invocation is spawned: {argv}"
        request = json.loads(self._host(argv[-1]).read_text("utf-8"))
        self.exec_request = request
        self.build(self._host_view(request))
        if on_line is not None:
            on_line("compiling...")
        return _Finished(self.exec_status)


class _Finished:
    """A spawned invocation that is already over."""

    output = "compiling..."

    def __init__(self, status: int | None) -> None:
        self.status = status

    def poll(self) -> int | None:
        return self.status

    def wait(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


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


def _docker(seam) -> Docker:
    """A docker seam driven by *seam* for both of its two roles.

    Short commands go through the runner and the invocation through the
    spawner, which is the split the real one has: an invocation is
    neither short nor bounded, and something has to watch the clock
    while it runs.
    """
    return Docker(runner=seam, spawner=getattr(seam, "spawn", _never_spawned))


def _never_spawned(argv, on_line=None):
    """For the seams of tests that refuse before an invocation exists."""
    raise AssertionError(f"nothing should have been invoked: {list(argv)}")


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
        registry=ScriptedRegistry(),
        docker=_docker(seam),
    )
    assert result.outcome.successful, result.outcome.problems
    # The pin, not the repository: what a build reports is the image it
    # resolved to, digest included.
    assert result.image.startswith(f"{IMAGE}:")
    assert result.image.endswith(f"@{ENVIRONMENT_DIGEST}")
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
        registry=ScriptedRegistry(),
        docker=_docker(_seam()),
    )
    assert result.outcome.successful
    assert [stage for stage, _facts in steps] == [
        "environment",
        "environment",
        "context",
        "context",
        "compile",
    ]
    assert steps[0][1] == {}
    # The environment step, once it knows: which image, and what it says.
    chosen = steps[1][1]
    assert chosen["build_environment"].endswith(f"@{ENVIRONMENT_DIGEST}")
    assert chosen["zephyr"] == "4.4.0"
    assert steps[2][1] == {}
    facts = steps[3][1]
    assert facts["build_environment"] == chosen["build_environment"]
    assert facts["board"] == model.device.board
    assert facts["patches"] == []
    assert facts["files"] >= 2  # the model and the public key, at least
    assert facts["id"].startswith("sha256:")
    assert (
        facts["sdk_sha256"]
        == read_context_manifest(tmp_path / "wr" / "context" / "manifest.yaml").sdk.sha256
    )
    assert steps[4][1]["image"] == steps[1][1]["build_environment"]


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
        registry=ScriptedRegistry(),
        docker=_docker(seam),
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
    assert f"{result.context_dir}:{containerpaths.CONTEXT}:ro" in mounts


# --------------------------------------------------------------------------
# The two typed refusals E54 asks be surfaced cleanly (image, SDK source)
# --------------------------------------------------------------------------


def test_an_image_that_cannot_be_fetched_refuses_before_a_container_starts(
    tmp_path, model, public_pem
):
    """A missing image stopped being a refusal and became a fetch.

    The reference is pinned to a digest by then, so there is exactly one
    set of bytes that answers to it — and either they arrive or the pull
    fails, which is this. Asking the user to type a pull command for a
    name MCUHome resolved itself was only ever right while the name came
    from a constant they could have chosen differently.
    """
    make_sdk_source(tmp_path / "src")
    seen: list[list[str]] = []

    def runner(argv, on_line=None):
        seen.append(argv)
        if argv[1] == "version":
            return lb.Completed(0, "28.0.0")
        if argv[1:3] == ["image", "inspect"]:
            return _missing_image()
        if argv[1] == "pull":
            return lb.Completed(1, "Error response from daemon: manifest unknown")
        raise AssertionError(f"nothing else is asked once the fetch failed: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            registry=ScriptedRegistry(),
            docker=_docker(runner),
        )
    assert "could not fetch" in caught.value.message
    assert any(argv[1] == "pull" for argv in seen), "it tried"
    assert not (tmp_path / "wr" / "context").exists(), "nothing was written"


def test_an_environment_of_another_zephyr_release_refuses_before_anything_is_written(
    tmp_path, model, public_pem
):
    """The requirement is checked where the environment is chosen.

    It is checked against what the *image says about itself*, which is
    read out of a registry before anything is fetched — so the mismatch
    costs no context directory, no SDK lookup and no download, and the
    refusal names both what the device needs and what the image carries.
    """
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1] == "version":
            return lb.Completed(0, "28.0.0")
        raise AssertionError(f"nothing is asked of docker once the release is wrong: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=f"{IMAGE}:zephyr-4.5.0-r1",
            registry=ScriptedRegistry(tag="zephyr-4.5.0-r1", zephyr="4.5.0"),
            docker=_docker(runner),
        )
    assert "4.5.0" in caught.value.message
    assert model.toolchain.zephyr_constraint in caught.value.message
    assert not (tmp_path / "wr" / "context").exists(), "nothing was written"


def test_an_environment_with_no_zephyr_label_refuses_before_anything_is_written(
    tmp_path, model, public_pem
):
    """ "Absence is never read as compatible" (§2.1.1) — here too.

    An image that carries no Zephyr label states nothing about what it
    builds against, and there used to be a fallback that read the
    workbench's own pin into that silence — which invented a claim the
    image had never made and put it in a refusal. There is nothing to
    invent from any more: what the label says is the whole of what is
    known about an image nobody has fetched.
    """
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1] == "version":
            return lb.Completed(0, "28.0.0")
        raise AssertionError(f"nothing is asked of docker once the image says nothing: {argv}")

    with pytest.raises(BuildError) as caught:
        buildmethods.compose_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=f"{IMAGE}:silent",
            registry=ScriptedRegistry(tag="silent", zephyr=""),
            docker=_docker(runner),
        )
    assert "does not say which Zephyr it carries" in caught.value.message
    assert model.toolchain.zephyr_constraint in str(caught.value)
    assert not (tmp_path / "wr" / "context").exists(), "nothing was written"


def test_no_sdk_source_configured_is_a_typed_refusal(tmp_path, model, public_pem):
    calls: list[list[str]] = []

    def runner(argv, on_line=None):
        calls.append(argv)
        if argv[1] == "version":
            return lb.Completed(0, "28.0.0")
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
            registry=ScriptedRegistry(),
            docker=_docker(runner),
        )
    assert "SDK source" in caught.value.message
    assert not any("--detach" in argv for argv in calls)


def test_a_source_without_the_package_is_a_typed_refusal(tmp_path, model, public_pem):
    empty = tmp_path / "empty"
    empty.mkdir()

    def runner(argv, on_line=None):
        if argv[1] == "version":
            return lb.Completed(0, "28.0.0")
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
            registry=ScriptedRegistry(),
            docker=_docker(runner),
        )
    assert containerbuild.lb.SDK_PACKAGE_NAME in caught.value.message


# --------------------------------------------------------------------------
# A context the caller already holds
# --------------------------------------------------------------------------


def test_a_supplied_context_is_built_as_it_is(tmp_path, model, public_pem):
    """The other half of the seam: what to build can arrive already made.

    The ordinary caller hands a device model over and this composition
    creates the context; a caller that already holds one — an embedder
    that assembled it elsewhere, a build server that received it over a
    socket — hands the directory over instead, and then nothing is
    resolved and nothing is written into it but the lock. That is what
    makes "create a context" and "build a context" two calls rather than
    one, and it is the seam a build server enters at.

    The context here comes from ``create_build_context``, the real
    creator, and not from this test: what is asserted is that the
    composition builds the directory it was given, which a hand-written
    context would say nothing about.
    """
    make_sdk_source(tmp_path / "src")
    context = tmp_path / "held"
    create_build_context(
        model,
        out_dir=context,
        sdk_sources=(tmp_path / "src",),
        build_environment=EnvironmentPin(reference=ENVIRONMENT_PIN),
        signing_pub=public_pem,
    )
    before = sorted(path.name for path in context.iterdir())
    steps: list[str] = []
    seam = _seam()
    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        # Still needed, and for the other of the two things a source is
        # for: the pin is already in the supplied context and is not
        # resolved again, but the bytes it pins still have to be found
        # and mounted.
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        context_dir=context,
        on_step=lambda stage, **facts: steps.append(stage),
        docker=_docker(seam),
    )
    assert result.outcome.successful, result.outcome.problems
    assert result.context_dir == context
    # The directory it was given, locked in place — not a copy, and not a
    # second context somewhere under the work root.
    assert not (tmp_path / "wr" / "context").exists()
    assert sorted(path.name for path in context.iterdir()) == sorted([*before, "manifest.yaml"])
    # No context step: this composition did not create one, and a step
    # bar that claimed otherwise would be showing work nobody did. The
    # environment step is still there — the image the supplied context
    # pins still has to be here, and getting it here is work.
    assert steps == ["environment", "environment", "compile"]
    # And it is the *supplied* context's pin that decided the image, not
    # the model's: no registry was needed at all.
    assert read_context_manifest(context / "manifest.yaml").build_environment.reference == (
        ENVIRONMENT_PIN
    )


# --------------------------------------------------------------------------
# A locally built image carries no repo digest — the placeholder path
# --------------------------------------------------------------------------


def test_an_image_built_here_is_pinned_by_its_own_id_and_still_builds(tmp_path, model, public_pem):
    """``--container-image localhost/…`` names bytes no registry has.

    The registry has no such tag — nobody published it — so the image is
    found on this host and pinned by the only identity it has, docker's
    own image ID. The pin is honest rather than portable: a build server
    handed this context will say it does not have the image, which is
    true.
    """
    make_sdk_source(tmp_path / "src")
    identity = "sha256:" + "f" * 64
    seam = _seam(facts=image_facts(digest=None))

    class NoSuchTag(ScriptedRegistry):
        def digest_of(self, reference):
            return None

        def tags(self, reference):
            return ()

    result = buildmethods.compose_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image="localhost/builder:wip",
        registry=NoSuchTag(),
        docker=_docker(seam),
    )
    assert result.outcome.successful, result.outcome.problems
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.build_environment.reference == f"localhost/builder:wip@{identity}"
    assert manifest.build_environment.digest == identity


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


def test_the_local_method_mounts_the_users_compiler_cache(tmp_path, model) -> None:
    """Both cache roles, from this user's cache directory into the container.

    The wiring crosses a repository boundary — the workbench composes,
    the compiler's backend mounts — and it is the whole difference
    between a build that recompiles Zephyr every time and one that does
    not, so it is pinned where the composition happens.
    """
    make_sdk_source(tmp_path / "src")
    seam = _seam()
    buildmethods.compose_local_build(
        model,
        signing_pub=public_key_pem(generate_key_pem(TEST_SCALAR)),
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
        image=IMAGE,
        registry=ScriptedRegistry(),
        docker=_docker(seam),
    )
    start = next(argv for argv in seam.calls if "--detach" in argv)
    mounts = [start[i + 1] for i, item in enumerate(start) if item == "--volume"]
    cache = tmp_path / "xdg" / "mcuhome" / "ccache"
    assert f"{cache / 'cache-local'}:{containerpaths.CCACHE_LOCAL}" in mounts
    assert f"{cache / 'cache-shared'}:{containerpaths.CCACHE_SHARED}:ro" in mounts


def test_a_stated_cache_directory_wins_over_the_users_own(tmp_path, model) -> None:
    """`ccache_dir` resolves through the configuration layers, so the
    caller states it and this composition passes it on unchanged."""
    make_sdk_source(tmp_path / "src")
    seam = _seam()
    buildmethods.compose_local_build(
        model,
        signing_pub=public_key_pem(generate_key_pem(TEST_SCALAR)),
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={"HOME": str(tmp_path / "home")},
        image=IMAGE,
        ccache_dir=tmp_path / "fast-disk",
        registry=ScriptedRegistry(),
        docker=_docker(seam),
    )
    start = next(argv for argv in seam.calls if "--detach" in argv)
    mounts = [start[i + 1] for i, item in enumerate(start) if item == "--volume"]
    assert f"{tmp_path / 'fast-disk' / 'cache-local'}:{containerpaths.CCACHE_LOCAL}" in mounts
