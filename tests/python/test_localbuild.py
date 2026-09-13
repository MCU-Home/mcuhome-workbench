# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a container build from a device model (``containerbuild.py``).

**No container ever runs here.** The one impure operation is the runtime
seam: a scripted stand-in dispatches on the argv
:class:`~mcuhome.workbench.containerbuild.ContainerRuntime` composed and writes
the result document a real container would (build-environment
specification §6.2). What is asserted is the composition above the
profile — :func:`mcuhome.workbench.build.compose_container_build`:
that a device model becomes a locked context and one ``build`` step, that
the image is chosen by the packages that context pins and checked before
anything is fetched, and that the **private** key never appears in any
argv while the context carries only the public half.

The suite deliberately builds no context of its own: the whole subject
here is that the composition creates and locks one, and a context these
tests wrote would be the one thing capable of hiding a defect in it.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import zstandard
from conftest import (
    ENVIRONMENT_DIGEST,
    ENVIRONMENT_REPOSITORY,
    EXAMPLES_DIR,
    ScriptedRegistry,
    resolve_file,
    sdk_members,
    write_environment_packages,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench import build, containerbuild
from mcuhome.workbench.buildenvsession import RESULT_PREFIX, RESULT_SUFFIX, SPEC_GENERATION
from mcuhome.workbench.buildprocess import Completed
from mcuhome.workbench.contextdir import create_build_context, read_context_manifest
from mcuhome.workbench.packagefetch import SDK_PACKAGE_NAME
from mcuhome.workbench.resolve_pins import SDK_ANY, resolve_sdk_pin
from mcuhome.workbench.signing import (
    generate_key_pem,
    looks_like_p256_key,
    looks_like_p256_public_key,
    public_key_pem,
)

#: A P-256 key with a known scalar, so this module never draws one.
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0

#: The digest the scripted registry answers with — what a build resolves
#: to, and what it reports afterwards.
DIGEST = ENVIRONMENT_DIGEST
SDK_VERSION = "0.1.0"
IMAGE = ENVIRONMENT_REPOSITORY


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
    archive = build_sdk_archive(sdk_members(SDK_VERSION))
    filename = f"mcuhome-sdk-{SDK_VERSION}.tar.zst"
    (directory / filename).write_bytes(archive)
    real = sha256_file(directory / filename)
    index = {
        "packages": {
            "mcuhome-sdk": {SDK_VERSION: {"file": filename, "sha256": real, "size": len(archive)}}
        }
    }
    write_environment_packages(directory, index)
    (directory / "index.json").write_text(json.dumps(index), "utf-8")
    return real


# --------------------------------------------------------------------------
# The scripted container runtime
# --------------------------------------------------------------------------


def build_result(request: dict[str, Any], out: Path, *, status: str = "success") -> None:
    """Write the artifacts into ``out`` and a conforming result document.

    §6.2 exactly: the generation this side speaks, the invocation id
    echoed back, the status, and the names of the files this step wrote
    into ``out``. The orchestrator hashes them itself, which is the only
    order in which the measurement is worth anything — so nothing here
    declares a hash.
    """
    files = {"firmware.hex": b"HEX", "firmware.bin": b"BIN", "build-report.json": b'{"report": 1}'}
    for name, data in files.items():
        (out / name).write_bytes(data)
    invocation = request["invocation_id"]
    document = {
        "spec_generation": SPEC_GENERATION,
        "invocation_id": invocation,
        "status": status,
        "message": "" if status == "success" else "the build failed",
        "artifacts": sorted(files) if status == "success" else [],
    }
    (out / f"{RESULT_PREFIX}{invocation}{RESULT_SUFFIX}").write_text(json.dumps(document), "utf-8")


class Seam:
    """A scripted stand-in for the container runtime, recording every argv.

    Dispatches on the argv :class:`~mcuhome.workbench.containerbuild.ContainerRuntime`
    composed, so the tests exercise the true composition and can then
    assert it. The step itself is played by reading the request document
    through the mounts the ``run`` was given — a path no ``--volume``
    reaches does not exist inside a real container, and resolving one
    anyway would let the suite pass over the one defect this layout is
    about.
    """

    def __init__(
        self,
        *,
        build=None,
        present: bool = True,
        pull_status: int = 0,
        exit_status: int = 0,
    ) -> None:
        self.build = build if build is not None else build_result
        self.present = present
        self.pull_status = pull_status
        self.exit_status = exit_status
        self.calls: list[list[str]] = []
        self.request: dict[str, Any] | None = None
        #: ``container target -> host source``, from the ``--volume``
        #: arguments of the run that played a step.
        self.mounts: dict[PurePosixPath, Path] = {}

    def __call__(self, argv, on_line=None) -> Completed:
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "version":
            return Completed(0, "29.7.2\n")
        if argv[1:3] == ["image", "inspect"]:
            return Completed(0 if self.present else 1, "" if self.present else "No such image")
        if verb == "pull":
            return Completed(self.pull_status, "pulled" if not self.pull_status else "denied")
        if verb == "rm":
            return Completed(0, "")
        raise AssertionError(f"unexpected runtime call: {argv}")

    def spawn(self, argv, on_line=None):
        """The step, which is spawned rather than run.

        It plays the scripted build synchronously and answers with a
        handle that has already finished — the shape a supervisor walks
        over without a rung ever firing.
        """
        argv = list(argv)
        self.calls.append(argv)
        assert argv[1] == "run", f"only a step is spawned: {argv}"
        self.mounts = {}
        for volume in [argv[i + 1] for i, item in enumerate(argv) if item == "--volume"]:
            source, target = volume.removesuffix(":ro").rsplit(":", 1)
            self.mounts[PurePosixPath(target)] = Path(source)
        request = json.loads(self._host(containerbuild.REQUEST_TARGET).read_text("utf-8"))
        self.request = request
        self.build(request, self._host(containerbuild.OUT_TARGET))
        if on_line is not None:
            on_line("compiling...")
        return _Finished(self.exit_status)

    def _host(self, path: str) -> Path:
        inside = PurePosixPath(path)
        for target, source in self.mounts.items():
            if inside == target:
                return source
            if target in inside.parents:
                return source / inside.relative_to(target)
        raise AssertionError(
            f"{path} is reached by no --volume of {sorted(map(str, self.mounts))}: "
            "inside a real container that path does not exist"
        )

    @property
    def step(self) -> list[str]:
        """The argv of the run that played a step."""
        return next(argv for argv in self.calls if argv[1] == "run")

    @property
    def volumes(self) -> list[str]:
        """Every ``--volume`` argument of that run."""
        return [self.step[i + 1] for i, item in enumerate(self.step) if item == "--volume"]


class _Finished:
    """A spawned step that is already over."""

    output = "compiling..."
    started = True

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


def _runtime(seam) -> containerbuild.ContainerRuntime:
    """A runtime driven by *seam* in both of its roles.

    Short commands go through the runner and the step through the
    spawner, which is the split the real one has: a step is neither short
    nor bounded, and something has to watch the clock while it runs.
    """
    return containerbuild.ContainerRuntime(
        runner=seam, spawner=getattr(seam, "spawn", _never_spawned)
    )


def _never_spawned(argv, on_line=None):
    """For the seams of tests that refuse before a step exists."""
    raise AssertionError(f"nothing should have been started: {list(argv)}")


def _flatten(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(argv) for argv in calls)


def _build(tmp_path, model, public_pem, **overrides):
    """One composed container build, with this suite's seams in place.

    ``make_sdk_source`` publishes all three package kinds into one
    directory, so — unless a test states its own — the workspace and
    tools packages are looked for there too, exactly as the SDK is: each
    key states the same directory rather than one key doing it for all
    three.
    """
    seam = overrides.pop("seam", None) or Seam()
    sdk_sources = overrides.pop("sdk_sources", (tmp_path / "src",))
    options = overrides.pop("options", None) or build.BuildOptions()
    if not options.workspace_sources and not options.tools_sources:
        options = dataclasses.replace(
            options, workspace_sources=sdk_sources, tools_sources=sdk_sources
        )
    return (
        seam,
        build.compose_container_build(
            model,
            signing_pub=public_pem,
            sdk_sources=sdk_sources,
            work_root=overrides.pop("work_root", tmp_path / "wr"),
            env=overrides.pop("env", {}),
            images=overrides.pop("images", None) or ScriptedRegistry(),
            runtime=_runtime(seam),
            options=options,
            **overrides,
        ),
    )


# --------------------------------------------------------------------------
# The happy path: model -> context -> one build step
# --------------------------------------------------------------------------


def test_a_container_build_composes_a_context_and_drives_one_step(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok, result.outcome.problems
    # What a build reports is the image it resolved to, tag and digest —
    # the tag is where it was found, the digest is what ran.
    assert result.container_image.startswith(f"{IMAGE}:")
    assert result.container_image.endswith(f"@{DIGEST}")
    # A locked context was created from the model, with the pins the pin
    # resolution produced.
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.board == model.device.board
    # The delivered artifacts are where the result says they are.
    assert (result.out_dir / "firmware.bin").is_file()
    assert (result.out_dir / "build-report.json").is_file()
    assert {a.role for a in result.outcome.artifacts} == {"firmware", "report"}
    # One step, one container, and §6's entry point named by its path
    # with no arguments after it — never the image's own CMD.
    assert seam.step[-2:] == [f"{IMAGE}@{DIGEST}", containerbuild.ENTRY_POINT_PATH]
    assert containerbuild.ENTRY_POINT_PATH == "/mcuhome/bin/build-environment-entry"
    assert len([argv for argv in seam.calls if argv[1] == "run"]) == 1


def test_the_composition_states_its_steps_in_order(tmp_path, model, public_pem):
    """``context`` before one exists, ``environment`` after it does.

    The honest-progress seam: a caller renders steps it was told about.
    The order is the order the decisions happen in — a context pins
    packages and the image is chosen from them, so the context comes
    first here exactly as it does in the subprocess profile.
    """
    make_sdk_source(tmp_path / "src")
    steps: list[tuple[str, dict]] = []
    seam, result = _build(
        tmp_path,
        model,
        public_pem,
        on_step=lambda stage, **facts: steps.append((stage, facts)),
    )
    assert result.outcome.ok
    assert [stage for stage, _facts in steps] == [
        "context",
        "context",
        "environment",
        "environment",
        "compile",
    ]
    assert steps[0][1] == {}
    facts = steps[1][1]
    # What the context says its environment is: the two packages it
    # pins, never the image.
    assert facts["build_environment"] == "mcuhome-build-workspace 0.1.0, mcuhome-build-tools 0.1.0"
    assert facts["board"] == model.device.board
    assert facts["patches"] == []
    # No id yet: freezing the context is what computes one, and it
    # happens after the environment is resolved so that an image refusal
    # costs no write into a directory the user keeps.
    assert "id" not in facts
    # The environment step, once it knows: which image, and what it says.
    assert steps[2][1] == {}
    chosen = steps[3][1]
    assert chosen["build_environment"].endswith(f"@{DIGEST}")
    assert chosen["zephyr"] == "4.4.0"
    assert chosen["found_under"]
    assert steps[4][1]["container_image"] == chosen["build_environment"]


# --------------------------------------------------------------------------
# The private key is never passed, never mounted, never in an argv
# --------------------------------------------------------------------------


def test_the_private_key_never_appears_in_any_argv(tmp_path, model):
    """The container gets keys/signing.pub and nothing else of the key pair.

    A private key file exists on this host, and its bytes and its path
    are grepped for across every composed runtime command — the run that
    plays the step, every mount argument. It appears in none of them,
    because the composition has no way to receive it: its only key input
    is the public PEM.
    """
    private_pem = generate_key_pem(TEST_SCALAR)
    private_path = tmp_path / "signing.key"
    private_path.write_text(private_pem, encoding="utf-8")

    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_key_pem(private_pem))
    assert result.outcome.ok

    flat = _flatten(seam.calls)
    assert str(private_path) not in flat
    assert "PRIVATE KEY" not in flat
    # What the context does carry is the public half, and only that.
    signing_pub = (result.context_dir / "keys" / "signing.pub").read_text(encoding="utf-8")
    assert looks_like_p256_public_key(signing_pub)
    assert not looks_like_p256_key(signing_pub)
    # And the context is mounted read-only, so even the public key cannot
    # be written back by the container.
    assert f"{result.context_dir}:{containerbuild.CONTEXT_TARGET}:ro" in seam.volumes


# --------------------------------------------------------------------------
# The tree of specification §4, and nothing besides
# --------------------------------------------------------------------------


def test_the_step_is_handed_the_specifications_tree_and_nothing_else(tmp_path, model, public_pem):
    """§4: the request document, sdk and build-context read-only, out
    writable, the cache tiers — and not one mount more.

    ``work`` is deliberately absent: it is empty at the start of a step
    because the container is new, and mounting a host directory there
    would hand the step something that outlives it. The entry point is
    absent for a related reason — it is the image's own content at the
    path §4 fixes.
    """
    make_sdk_source(tmp_path / "src")
    seam, result = _build(
        tmp_path,
        model,
        public_pem,
        env={"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
    )
    assert result.outcome.ok, result.outcome.problems
    targets = {volume.removesuffix(":ro").rsplit(":", 1)[1] for volume in seam.volumes}
    assert targets == {
        containerbuild.REQUEST_TARGET,
        containerbuild.SDK_TARGET,
        containerbuild.CONTEXT_TARGET,
        containerbuild.OUT_TARGET,
        f"{containerbuild.CACHE_TARGET}/local",
    }
    read_only = {
        volume.removesuffix(":ro").rsplit(":", 1)[1]
        for volume in seam.volumes
        if volume.endswith(":ro")
    }
    assert read_only == {
        containerbuild.REQUEST_TARGET,
        containerbuild.SDK_TARGET,
        containerbuild.CONTEXT_TARGET,
    }


def test_the_step_is_isolated_and_runs_as_the_calling_user(tmp_path, model, public_pem):
    """``--network none`` because §11 says an environment never requires
    one, ``--user`` because everything in ``out`` lands on a bind mount
    this side reads back, ``--rm`` and ``--init`` because a step's
    container is over when the step is."""
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok
    argv = seam.step
    assert "--rm" in argv and "--init" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == containerbuild.current_user()
    # The one environment variable the specification defines, and it is
    # the tree's root.
    assert f"MCUHOME_BUILDER_BASE_DIR={containerbuild.BASE_DIR}" in argv


def test_the_request_document_is_the_fields_of_the_specification(tmp_path, model, public_pem):
    """§6.1's five mandatory fields, and the limits beside them."""
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok
    assert set(seam.request) == {
        "spec_generation",
        "session_id",
        "invocation_id",
        "action",
        "parameters",
        "limits",
    }
    assert seam.request["spec_generation"] == SPEC_GENERATION
    assert seam.request["action"] == "build"
    assert seam.request["parameters"] == {}


def test_the_recommended_limits_are_the_ones_the_container_is_held_to(tmp_path, model, public_pem):
    """The two halves of one budget: what the request document
    recommends to the environment is what the runtime enforces from
    outside, so a build that honours the recommendation is a build that
    does not get killed for it."""
    make_sdk_source(tmp_path / "src")
    seam, result = _build(
        tmp_path,
        model,
        public_pem,
        options=build.BuildOptions(cpus=2, memory="4g"),
    )
    assert result.outcome.ok
    assert seam.request["limits"] == {"cpus": 2.0, "memory_bytes": 4 * 1024**3}
    argv = seam.step
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--memory") + 1] == str(4 * 1024**3)
    assert argv[argv.index("--pids-limit") + 1] == str(containerbuild.DEFAULT_CONTAINER_PIDS)


def test_a_build_nobody_bounded_is_given_this_machine(tmp_path, model, public_pem):
    """Unset is the machine as it is — a local build is not a tenant —
    and the guard is still there, because it exists against an
    environment that runs amok rather than against the person who
    started the build."""
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok
    argv = seam.step
    assert "--cpus" in argv and "--memory" in argv and "--pids-limit" in argv
    assert seam.request["limits"]["cpus"] == float(os.cpu_count() or 1)
    assert seam.request["limits"]["memory_bytes"] > 0


# --------------------------------------------------------------------------
# The refusals a container build must surface cleanly
# --------------------------------------------------------------------------


def test_an_image_that_cannot_be_fetched_refuses_before_a_step_starts(tmp_path, model, public_pem):
    """A missing image is a fetch, and a fetch that fails is a refusal.

    The reference is pinned to a digest by then, so there is exactly one
    set of bytes that answers to it — and either they arrive or the pull
    fails, which is this.
    """
    make_sdk_source(tmp_path / "src")
    seam = Seam(present=False, pull_status=1)
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, seam=seam)
    assert "could not fetch" in caught.value.message
    assert any(argv[1] == "pull" for argv in seam.calls), "it tried"
    assert not any(argv[1] == "run" for argv in seam.calls)


def test_an_image_of_another_zephyr_release_is_refused(tmp_path, model, public_pem):
    """The device's requirement is checked against what the image says
    about itself — read out of a registry, before anything is fetched."""
    make_sdk_source(tmp_path / "src")
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, images=ScriptedRegistry(zephyr="4.5.0"))
    assert "4.5.0" in caught.value.message
    assert model.toolchain.zephyr_constraint in caught.value.message


def test_an_image_declaring_other_bytes_is_not_used_instead(tmp_path, model, public_pem):
    """The near miss: the same package versions under other hashes.

    That is a different environment, whatever it is called, so the search
    ends without a match and the refusal says what was wanted and what
    each candidate declared instead.
    """
    make_sdk_source(tmp_path / "src")
    other = ScriptedRegistry(workspace="0.1.0@sha256:" + "cd" * 32)
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, images=other)
    assert "No container image declares" in caught.value.message
    assert "cd" * 32 in str(caught.value)


def test_an_image_that_accepts_no_context_from_this_workbench_is_refused(
    tmp_path, model, public_pem
):
    """§9.1: the environment declares which build contexts it takes, and
    the check runs before the step rather than inside it."""
    make_sdk_source(tmp_path / "src")
    with pytest.raises(BuildError) as caught:
        _build(
            tmp_path,
            model,
            public_pem,
            images=ScriptedRegistry(constraint="other-tool:~=1.0"),
        )
    assert "does not accept build contexts" in caught.value.message


def test_an_image_of_another_specification_generation_is_refused(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, images=ScriptedRegistry(generation="2"))
    assert "generation 2" in caught.value.message


def test_no_sdk_source_configured_is_a_typed_refusal(tmp_path, model, public_pem):
    seam = Seam()
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, seam=seam, sdk_sources=())
    assert "SDK source" in caught.value.message
    assert not any(argv[1] == "run" for argv in seam.calls)


def test_a_source_without_the_package_is_a_typed_refusal(tmp_path, model, public_pem):
    empty = tmp_path / "empty"
    empty.mkdir()
    seam = Seam()
    with pytest.raises(BuildError) as caught:
        _build(tmp_path, model, public_pem, seam=seam, sdk_sources=(empty,))
    assert SDK_PACKAGE_NAME in caught.value.message
    assert not any(argv[1] == "run" for argv in seam.calls)


def test_a_failed_step_comes_back_as_an_answer_and_not_as_an_exception(tmp_path, model, public_pem):
    """A build that ran and failed is a verdict a caller renders."""
    make_sdk_source(tmp_path / "src")

    def failing(request, out):
        build_result(request, out, status="failure")

    seam = Seam(build=failing, exit_status=1)
    _seam, result = _build(tmp_path, model, public_pem, seam=seam)
    assert not result.outcome.ok
    assert result.outcome.status == "failure"
    assert any("the build failed" in problem for problem in result.outcome.problems)


# --------------------------------------------------------------------------
# A context the caller already holds
# --------------------------------------------------------------------------


def test_creating_a_context_twice_in_one_work_root_is_the_same_context(tmp_path, model, public_pem):
    """A build directory is reused, so context creation has to survive itself.

    Resolving the environment pins reads the SDK release's lock, which
    means the SDK package is unpacked into the work root every time a
    context is created — and a work root is a **stable** path a user keeps
    (`<build dir>/.mcuhome-local`). A second creation therefore lands on
    the first one's unpacked tree, and it has to answer with the same
    context rather than with a refusal or with something else.
    """
    make_sdk_source(tmp_path / "src")
    created = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
    requests = [
        create_build_context(
            model,
            out_dir=tmp_path / "work" / "context",
            work_root=tmp_path / "work",
            sdk_sources=(tmp_path / "src",),
            workspace_sources=(tmp_path / "src",),
            tools_sources=(tmp_path / "src",),
            signing_pub=public_pem,
            created=created,
        )
        for _ in range(3)
    ]
    assert requests[0] == requests[1] == requests[2]
    assert requests[0].build_environment.workspace.sha256


def test_a_supplied_context_is_built_as_it_is(tmp_path, model, public_pem):
    """The other half of the seam: what to build can arrive already made.

    The ordinary caller hands a device model over and this composition
    creates the context; a caller that already holds one — an embedder
    that assembled it elsewhere, a build server that received it over a
    socket — hands the directory over instead, and then nothing is
    resolved and nothing is written into it but the lock.
    """
    make_sdk_source(tmp_path / "src")
    context = tmp_path / "held"
    create_build_context(
        model,
        out_dir=context,
        work_root=tmp_path / "held-wr",
        sdk_sources=(tmp_path / "src",),
        workspace_sources=(tmp_path / "src",),
        tools_sources=(tmp_path / "src",),
        signing_pub=public_pem,
    )
    before = sorted(path.name for path in context.iterdir())
    steps: list[str] = []
    _seam, result = _build(
        tmp_path,
        model,
        public_pem,
        context_dir=context,
        on_step=lambda stage, **facts: steps.append(stage),
    )
    assert result.outcome.ok, result.outcome.problems
    assert result.context_dir == context
    # The directory it was given, locked in place — not a copy, and not a
    # second context somewhere under the work root.
    assert not (tmp_path / "wr" / "context").exists()
    assert sorted(path.name for path in context.iterdir()) == sorted([*before, "manifest.yaml"])
    # No context step: this composition did not create one, and a step
    # bar that claimed otherwise would be showing work nobody did.
    assert steps == ["environment", "environment", "compile"]
    pinned = read_context_manifest(context / "manifest.yaml").build_environment
    assert pinned.workspace.name == "mcuhome-build-workspace"
    assert pinned.tools.version == "0.1.0"


# --------------------------------------------------------------------------
# The compiler cache
# --------------------------------------------------------------------------


def test_the_users_compiler_cache_is_mounted_as_the_local_tier(tmp_path, model) -> None:
    """One cache per user, offered as §8's most local writable tier.

    The same root the subprocess profile lays its tiers out under, so a
    machine that builds both ways keeps one cache and not two.
    """
    make_sdk_source(tmp_path / "src")
    seam, _result = _build(
        tmp_path,
        model,
        public_key_pem(generate_key_pem(TEST_SCALAR)),
        env={"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
    )
    cache = tmp_path / "xdg" / "mcuhome" / "ccache"
    assert f"{cache / 'cache-local'}:{containerbuild.CACHE_TARGET}/local" in seam.volumes


def test_a_shared_cache_is_offered_read_only(tmp_path, model) -> None:
    """The shared tier exists when somebody filled it, and a build may
    only read it (§8)."""
    make_sdk_source(tmp_path / "src")
    cache = tmp_path / "xdg" / "mcuhome" / "ccache"
    (cache / "cache-shared").mkdir(parents=True)
    seam, _result = _build(
        tmp_path,
        model,
        public_key_pem(generate_key_pem(TEST_SCALAR)),
        env={"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
    )
    assert f"{cache / 'cache-shared'}:{containerbuild.CACHE_TARGET}/shared:ro" in seam.volumes


def test_a_stated_cache_directory_wins_over_the_users_own(tmp_path, model) -> None:
    """A cache root the caller states is passed on unchanged.

    `build.cache_root` resolves through the configuration layers above
    this composition, so what arrives here is already the answer."""
    make_sdk_source(tmp_path / "src")
    seam, _result = _build(
        tmp_path,
        model,
        public_key_pem(generate_key_pem(TEST_SCALAR)),
        env={"HOME": str(tmp_path / "home")},
        cache_root=tmp_path / "fast-disk",
    )
    assert (
        f"{tmp_path / 'fast-disk' / 'cache-local'}:{containerbuild.CACHE_TARGET}/local"
        in seam.volumes
    )


def test_the_configured_cache_root_answers_when_nothing_else_does(tmp_path, model) -> None:
    """`build.cache_root` is where an operator moves the cache.

    It is the only channel there is, so a composition that ignored it
    would send every build to the user's cache directory whatever the
    machine was configured to do.
    """
    make_sdk_source(tmp_path / "src")
    seam, _result = _build(
        tmp_path,
        model,
        public_key_pem(generate_key_pem(TEST_SCALAR)),
        env={"HOME": str(tmp_path / "home")},
        options=build.BuildOptions(cache_root=tmp_path / "operators-disk"),
    )
    assert (
        f"{tmp_path / 'operators-disk' / 'cache-local'}:{containerbuild.CACHE_TARGET}/local"
        in seam.volumes
    )


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

    The regression this pins: the pre-release rule (a dev version
    satisfies only a pre-release constraint) is right for a real pin like
    ``~=2.3`` and wrong for "any" — SDK_ANY is literally any, including a
    ``0.1.0.dev0``. An earlier version resolved SDK_ANY as a stable
    specifier and refused a dev-only source, which is exactly what every
    build did before the first stable release: the source directory holds
    one archive and it carries the ``.dev0`` version.
    """
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


def test_the_entry_point_is_named_by_its_path_and_not_left_to_the_image(
    tmp_path, model, public_pem
):
    """§6 fixes where the executable is and says nothing about ``CMD``.

    An image that declares none is conforming and would not start; one
    that declares something else is not the thing to run. So the path is
    composed from the base directory this side set and handed over as the
    container's command, with nothing after it.
    """
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok
    argv = seam.step
    assert argv[-1] == f"{containerbuild.BASE_DIR}mcuhome/bin/build-environment-entry"
    assert argv[-2].startswith(IMAGE), "the entry point is the command, the image is the image"


def test_a_stopped_step_has_its_container_removed_by_name(tmp_path, model, public_pem):
    """Signalling the client is not ending the build: the build is inside
    the container, and what stops it is removing the container.

    Both rungs of the ladder reach it, so the removal is asserted on the
    handle the launcher answers with rather than through a timing race.
    """
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok
    name = seam.step[seam.step.index("--name") + 1]
    assert name.startswith("mcuhome-")

    removed: list[str] = []
    runtime = containerbuild.ContainerRuntime(
        runner=lambda argv, on_line=None: removed.append(argv[-1]) or Completed(0, ""),
        spawner=lambda argv, on_line=None: _Finished(None),
    )
    handle = containerbuild._StepContainer(_Finished(None), runtime=runtime, name=name)
    handle.terminate()
    handle.kill()
    assert removed == [name, name], "each rung reaps the container it is stopping"


def test_a_machine_whose_memory_cannot_be_measured_states_no_memory_limit(
    tmp_path, model, public_pem, monkeypatch
):
    """Zero is not a small budget — it is the runtime's spelling for *no
    limit*, and §6.1's "how much memory the step should use".

    Both profiles run on hosts without a Linux ``/proc`` (macOS,
    Windows), where the available memory cannot be read at all. Such a
    machine states the CPU figure it does know and leaves the memory
    unstated, which both sides read as "decide for yourself" — rather
    than emitting ``--memory 0``, which would remove the hard limit while
    looking like one, and writing ``memory_bytes: 0``, which would tell a
    foreign builder to fit in nothing.
    """
    from mcuhome.model import jobs

    monkeypatch.setattr(jobs, "_MEMINFO_PATH", tmp_path / "no-such-meminfo")
    make_sdk_source(tmp_path / "src")
    seam, result = _build(tmp_path, model, public_pem)
    assert result.outcome.ok, result.outcome.problems
    argv = seam.step
    assert "--memory" not in argv
    assert "--cpus" in argv, "what the machine does know is still stated"
    assert argv[argv.index("--pids-limit") + 1] == str(containerbuild.DEFAULT_CONTAINER_PIDS)
    assert "memory_bytes" not in seam.request["limits"]
    assert seam.request["limits"]["cpus"] == float(os.cpu_count() or 1)
