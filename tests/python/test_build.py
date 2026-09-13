# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build targets behind one interface (``build.py``).

No container and no socket: each target is stubbed at its own backend
seam — ``compose_local_build``, ``run_remote_build`` — and what is
asserted is the layer above them. That is deliberately the whole point of
the module: the compositions are tested where they live
(``test_localbuild.py``, ``test_sessionclient.py``), and this file asserts
that a caller reaches the right one and reads one answer whichever ran.

The properties, in the order they matter:

* the outcome shape does not depend on the target — success, the
  delivery directory, and the *name of the build report*, which is what
  the one shared host-side signing step needs;
* a target name nobody implements is a refusal that lists the ones that
  exist, rather than a ``KeyError`` or a silent default;
* ``remote`` refuses in words for the one thing it cannot invent — the
  build server's address — and for the missing transport extra, before
  either costs a connection, while the pins of the context it sends are
  resolved the way every other target resolves them: the configured
  source directories first, the registry second.

What ``remote`` *does* once it has the address — resolve the pins, write
the base context, drive a real session — is asserted in ``test_sessionclient.py``,
against the real build server. Here the composition is stubbed at
``run_remote_build`` and only the layer above it is the subject.
"""

from __future__ import annotations

import asyncio
import builtins
import json
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    ENVIRONMENT_VERSION,
    EXAMPLES_DIR,
    SDK_VERSION,
    TOOLS_PACKAGE,
    WORKSPACE_PACKAGE,
    make_package_source,
    resolve_file,
)
from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import DeveloperEnvironment
from mcuhome.model.errors import BuildError, ConfigError

from mcuhome.workbench import build, containerbuild, sessionclient, subprocessbuild
from mcuhome.workbench.buildenvsession import (
    EnvironmentUnavailable,
    EnvironmentUnusable,
    StepResult,
)
from mcuhome.workbench.builders import SelectedBuilder
from mcuhome.workbench.buildlock import holder_of
from mcuhome.workbench.buildprocess import Completed
from mcuhome.workbench.contextdir import (
    create_build_context,
    read_context_manifest,
    read_context_request,
)
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE
from mcuhome.workbench.packageregistry import OFFICIAL_BASE_DOMAIN, RegistrySettings
from mcuhome.workbench.resolve_pins import (
    KIND_SDK,
    KIND_TOOLS,
    KIND_WORKSPACE,
)
from mcuhome.workbench.signing import generate_key_pem, public_key_pem

#: A fixed public key, so nothing here draws one and every context this
#: module creates is reproducible.
_PUBLIC_PEM = public_key_pem(generate_key_pem(scalar=0x2233AABB))


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _artifacts() -> tuple[Artifact, ...]:
    return (
        Artifact(root="out", path="firmware.bin", role="firmware", sha256="0" * 64),
        Artifact(root="out", path=BUILD_REPORT_FILE, role="report", sha256="1" * 64),
    )


def _run(request: build.BuildRequest, target: str) -> build.BuildResult:
    """What a command line does at its entry point: one ``asyncio.run``."""
    return asyncio.run(build.build_firmware(request, target=target))


def _served_by(directory: Path) -> tuple[RegistrySettings, ...]:
    """The official registry, served by *directory* and checked by nothing.

    The three sources a chain is resolved from all point at the one
    directory :func:`~conftest.make_package_source` writes, which holds
    exactly what a registry serves for them: the packages, their
    sidecars, and the index that lists both. Marked untrusted, so no
    signatures and no trust anchor are needed for a fixture whose subject
    is the *resolution* — what a registry's signatures are worth is
    ``test_packageregistry.py``'s subject, and it uses real ones.

    A local mirror is read off the filesystem, so nothing here can reach
    a network even if it wanted to.
    """
    return (
        RegistrySettings(
            base_domain=OFFICIAL_BASE_DOMAIN,
            untrusted=True,
            mirrors={
                source: (str(directory),) for source in (KIND_SDK, KIND_WORKSPACE, KIND_TOOLS)
            },
        ),
    )


def _remote_context_of(
    monkeypatch: pytest.MonkeyPatch, **fields
) -> tuple[build.BuildResult, object, list[str]]:
    """Run a remote build to the socket and answer with the context it sent.

    The session client is stubbed where every other test in this file
    stubs it, and what it would have uploaded is read back off disk —
    which is the only place the pins a remote build resolved can be
    observed. The build's own log comes back with it: an untrusted
    registry says so at every read, which makes "the registry was asked
    for this source" and "it was never asked at all" two things a test
    can tell apart.
    """
    sent: dict[str, object] = {}

    async def fake(context_dir, **kwargs):
        del kwargs
        sent["request"] = read_context_request(Path(context_dir) / "context.yaml")
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            status="success",
            artifacts=_artifacts(),
            out_dir=Path(fields["out_dir"]) / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    lines: list[str] = []
    outcome = _run(
        build.BuildRequest(
            builder=SelectedBuilder(
                target=build.TARGET_REMOTE, server="ws://build.example/session"
            ),
            signing_pub=_PUBLIC_PEM,
            on_line=lines.append,
            **fields,
        ),
        build.TARGET_REMOTE,
    )
    return outcome, sent["request"], lines


# --------------------------------------------------------------------------
# What a build may be given
# --------------------------------------------------------------------------


def test_the_request_carries_the_fields_the_reference_states(model, tmp_path) -> None:
    """The request is one list of fields, and the reference is that list.

    Every field is here in the order the API reference states, and
    nothing else is: a caller reading the reference and a caller reading
    the class have to arrive at the same object.
    """
    assert [entry.name for entry in fields(build.BuildRequest)] == [
        "model",
        "out_dir",
        "env",
        "options",
        "builder",
        "mode",
        "container_image",
        "project_root",
        "registries",
        "signing_pub",
        "patches_dir",
        "context_dir",
        "work_root",
        "wait_for_turn",
        "max_wait_seconds",
        "on_line",
        "on_step",
        "on_wait",
        "should_stop",
    ]


@pytest.mark.parametrize(
    "retired",
    [
        "sdk_sources",
        "server",
        "token",
        "image",
        "builder_image",
        "dev_workspace",
        "ccache_dir",
        "build_mode",
    ],
)
def test_a_field_that_moved_is_refused_rather_than_ignored(model, tmp_path, retired) -> None:
    """A keyword that was a field is a `TypeError`, not a silent no-op.

    Every one of these moved somewhere a caller can still reach: the
    three package source lists, the development workspace and the cache
    root are `BuildOptions`, the build server and its token are the
    selected builder, and the image and the mode are spelled the way
    their configuration keys are. A request that still passed one of them
    would build with the value dropped.
    """
    with pytest.raises(TypeError):
        build.BuildRequest(model=model, out_dir=tmp_path, **{retired: None})


def test_the_pristine_mode_is_not_a_build_mode(model, tmp_path) -> None:
    """`mode` kept its spelling and lost its other meaning.

    It used to carry the session protocol's ``clean`` as well, which is
    the one retirement a `TypeError` cannot state: the field is still
    there, so the old *value* has to be refused instead — and it is, by
    the same refusal any other non-mode gets.
    """
    with pytest.raises(build.UnknownBuildMode) as refusal:
        build.build_target_for(
            build.TARGET_LOCAL, build.BuildRequest(model=model, out_dir=tmp_path, mode="clean")
        )
    assert '"clean"' in str(refusal.value)
    for name in build.BUILD_MODES:
        assert name in (refusal.value.hint or "")


# --------------------------------------------------------------------------
# Choosing a target
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", build.BUILD_TARGETS)
def test_every_target_name_resolves_to_itself(name: str) -> None:
    assert build.resolve_build_target(name) == name


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_local_container(nothing) -> None:
    """The default is a build on this machine, in a container."""
    assert build.resolve_build_target(nothing) == build.TARGET_LOCAL
    assert build.DEFAULT_BUILD_TARGET == build.TARGET_LOCAL


def test_a_caller_that_names_no_target_takes_the_configured_one(model, tmp_path) -> None:
    """``None`` is "no preference", and ``build.target`` is what answers it.

    The name a caller passes is the more explicit statement and wins
    where there is one; an embedder that passes nothing builds the way
    the machine is configured, exactly as it does for ``build.mode``.
    """
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        options=build.BuildOptions(target=build.TARGET_REMOTE),
    )
    assert isinstance(build.build_target_for(None, request), build.RemoteBuild)
    assert isinstance(
        build.build_target_for(build.TARGET_LOCAL, request),
        build.LocalBuild,
    )


def test_the_selected_builder_answers_before_the_configured_target(model, tmp_path) -> None:
    """A builder is a selection, and a selection beats a configured default.

    The ladder with no target name: the builder this build was selected
    for, then ``build.target``. A name states more than either and still
    wins over both.
    """
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        builder=SelectedBuilder(target=build.TARGET_REMOTE, server="attic", token="a-token"),
        options=build.BuildOptions(target=build.TARGET_LOCAL),
    )
    target = build.build_target_for(None, request)
    assert target == build.RemoteBuild(server="attic", token="a-token")
    assert isinstance(build.build_target_for(build.TARGET_LOCAL, request), build.LocalBuild)


def test_an_unknown_target_is_a_refusal_that_lists_the_real_ones() -> None:
    """Typed, and it names them all — a user who guessed wrong needs them."""
    with pytest.raises(build.UnknownBuildTarget) as refusal:
        build.resolve_build_target("cloud")
    rendered = str(refusal.value)
    assert '"cloud"' in rendered
    for name in build.BUILD_TARGETS:
        assert name in rendered


def test_build_firmware_refuses_an_unknown_target_before_it_runs_anything(model, tmp_path) -> None:
    request = build.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(build.UnknownBuildTarget):
        _run(request, "cloud")


# --------------------------------------------------------------------------
# Choosing how this machine executes it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", build.BUILD_MODES)
def test_every_build_mode_resolves_to_itself(name: str) -> None:
    assert build.resolve_build_mode(name) == name


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_container(nothing) -> None:
    """The default is the container: it is the only mode that isolates.

    A request that states no mode states none — the configuration
    answers, and with nothing configured that answer is the container.
    So the default is asserted where it is decided, on the target, and
    not on a field that now means "nobody said".
    """
    assert build.resolve_build_mode(nothing) == build.MODE_CONTAINER
    assert build.DEFAULT_BUILD_MODE == build.MODE_CONTAINER
    assert build.BuildRequest(model=None, out_dir=Path()).mode is None
    assert build.BuildOptions().mode == build.MODE_CONTAINER


def test_an_unknown_build_mode_is_a_refusal_that_lists_the_real_ones() -> None:
    with pytest.raises(build.UnknownBuildMode) as refusal:
        build.resolve_build_mode("vm")
    rendered = str(refusal.value)
    assert '"vm"' in rendered
    for name in build.BUILD_MODES:
        assert name in rendered


def test_the_mode_selects_the_execution_the_local_target_runs(model, tmp_path) -> None:
    """One name, two decisions: the target states them apart."""
    container = build.build_target_for(
        build.TARGET_LOCAL, build.BuildRequest(model=model, out_dir=tmp_path)
    )
    assert isinstance(container.execution, build.ContainerExecution)

    subprocess_target = build.build_target_for(
        build.TARGET_LOCAL,
        build.BuildRequest(model=model, out_dir=tmp_path, mode=build.MODE_SUBPROCESS),
    )
    execution = subprocess_target.execution
    assert isinstance(execution, build.SubprocessExecution)


def test_the_subprocess_mode_reaches_its_own_composition(model, tmp_path, monkeypatch):
    """``build.mode = subprocess`` selects the backend, and nothing else does."""
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = StepResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            exit_code=0,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "delivery",
        )
        return subprocessbuild.SubprocessBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            environment=None,
        )

    monkeypatch.setattr(build, "compose_subprocess_build", fake)
    outcome = _run(
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            mode=build.MODE_SUBPROCESS,
        ),
        build.TARGET_LOCAL,
    )
    assert outcome.target == build.TARGET_LOCAL
    assert outcome.ok
    assert outcome.artifacts == _artifacts()
    assert outcome.report == BUILD_REPORT_FILE
    # No image ran, and the outcome says so rather than naming one.
    assert outcome.container_image == ""
    # No job count travels: what a build may use of this machine is
    # `build.cpus`/`build.memory`, and the environment sizes itself from
    # the limits those become.
    assert "jobs" not in seen


def test_a_subprocess_build_resolves_its_pins_like_every_other_build(model, tmp_path) -> None:
    """The mode is no longer blocked, and the first thing it needs is a pin.

    A subprocess build creates its own context now — that is what makes
    it a build target rather than half of one — so a request with no
    package source at all fails where every other build target fails: at
    the SDK pin, naming the setting that supplies one. The old refusal
    ("nothing states which packages to use") is gone, and this test is
    what would notice it coming back.
    """
    with pytest.raises(BuildError) as refusal:
        asyncio.run(
            build.build_firmware(
                build.BuildRequest(
                    model=model,
                    out_dir=tmp_path,
                    signing_pub=_PUBLIC_PEM,
                    mode=build.MODE_SUBPROCESS,
                ),
                target=build.LocalBuild(execution=build.SubprocessExecution()),
            )
        )
    assert "build.sdk_sources" in refusal.value.hint
    assert "build.mode" not in refusal.value.hint


def test_a_subprocess_build_of_a_context_it_was_given_needs_no_image(
    model, tmp_path, monkeypatch
) -> None:
    """With an environment and a context, the composition drives the backend.

    The context is a real one — created by the real creator against a
    real package source — because the composition reads its pins now: an
    environment is provisioned *from* what the context names, and a
    hand-written directory would be testing a context nothing can build.
    Here the environment is supplied, so nothing is provisioned; what the
    checks are handed is recorded and their own behaviour is tested where
    they live.
    """
    driven: dict[str, object] = {}

    def fake_run(context_dir, **kwargs):
        driven["context_dir"] = context_dir
        driven.update(kwargs)
        return subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake_run)
    monkeypatch.setattr(build, "lock_context", lambda directory: None)
    checked: dict[str, object] = {}
    monkeypatch.setattr(
        subprocessbuild,
        "check_environment",
        lambda environment, **facts: checked.update(facts),
    )

    class FakeEnvironment:
        developer = False

        def described(self) -> str:
            return "mcuhome-build-workspace 0.1.0"

    make_package_source(tmp_path / "sdk")
    create_build_context(
        model,
        out_dir=tmp_path / "context",
        work_root=tmp_path / "made",
        sdk_sources=(tmp_path / "sdk",),
        workspace_sources=(tmp_path / "sdk",),
        tools_sources=(tmp_path / "sdk",),
        signing_pub=_PUBLIC_PEM,
    )
    steps: list[tuple] = []
    result = build.compose_subprocess_build(
        model,
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        environment=FakeEnvironment(),
        context_dir=tmp_path / "context",
        options=build.BuildOptions(cpus=5, memory="8g"),
        on_step=lambda name, **facts: steps.append((name, facts)),
    )
    assert result.out_dir == tmp_path / "out"
    # What this machine gives the build, as the request document states
    # it: the configured figures, not a job count.
    assert driven["limits"].cpus == 5
    assert driven["limits"].memory_bytes == 8 * 1024**3
    # The context's own pins are what the environment is checked against,
    # and the device's Zephyr constraint travels with them.
    assert checked["pin"].workspace.name == "mcuhome-build-workspace"
    assert checked["zephyr_constraint"] == model.toolchain.zephyr_constraint
    assert checked["generator"].startswith("mcuhome-workbench:")
    # Nobody configured a cache, so it is the user's cache directory —
    # the same answer a container build gets, from the same resolution.
    assert driven["cache_root"] == tmp_path / "cache" / "mcuhome" / "ccache"
    assert driven["context_dir"] == tmp_path / "context"
    assert [name for name, _ in steps] == ["environment", "environment", "compile"]
    assert steps[1][1]["build_environment"] == "mcuhome-build-workspace 0.1.0"


def test_the_environment_is_checked_before_the_context_is_locked(
    model, tmp_path, monkeypatch
) -> None:
    """Order, not merely presence: a refused build must change nothing first.

    Locking writes ``manifest.yaml`` into a directory the user keeps. A
    build that is going to be refused because its environment does not fit
    must therefore be refused **before** the lock, and a test that only
    asserted the check happens would still pass if somebody moved it one
    line down.
    """
    order: list[str] = []
    monkeypatch.setattr(
        subprocessbuild,
        "check_environment",
        lambda environment, **facts: order.append("check"),
    )
    monkeypatch.setattr(build, "lock_context", lambda directory: order.append("lock"))
    monkeypatch.setattr(
        subprocessbuild,
        "run_locked_build",
        lambda context_dir, **kwargs: subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=kwargs["environment"],
        ),
    )

    class FakeEnvironment:
        developer = False

        def described(self) -> str:
            return "mcuhome-build-workspace 0.1.0"

    make_package_source(tmp_path / "sdk")
    create_build_context(
        model,
        out_dir=tmp_path / "context",
        work_root=tmp_path / "made",
        sdk_sources=(tmp_path / "sdk",),
        workspace_sources=(tmp_path / "sdk",),
        tools_sources=(tmp_path / "sdk",),
        signing_pub=_PUBLIC_PEM,
    )
    build.compose_subprocess_build(
        model,
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={},
        environment=FakeEnvironment(),
        context_dir=tmp_path / "context",
    )
    assert order == ["check", "lock"]


# --------------------------------------------------------------------------
# One outcome shape, both targets
# --------------------------------------------------------------------------


def test_the_local_target_answers_with_the_backends_own_verdict(model, tmp_path, monkeypatch):
    """``local``: the legacy container invocation's outcome (retired at
    the switchover), unchanged, in the shared shape."""
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = StepResult(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "delivery",
        )
        return containerbuild.ContainerBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            container_image="registry.example.test/other/environment:test",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    outcome = _run(
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            signing_pub="-----BEGIN PUBLIC KEY-----\n",
            options=build.BuildOptions(sdk_sources=(tmp_path / "sdk",)),
            container_image="registry.example.test/other/environment:test",
        ),
        build.TARGET_LOCAL,
    )
    assert outcome.target == build.TARGET_LOCAL
    assert outcome.ok and not outcome.stopped
    assert outcome.context_id == "sha256:" + "1" * 64
    assert outcome.artifacts == _artifacts()
    assert outcome.out_dir == tmp_path / "delivery"
    assert outcome.report == BUILD_REPORT_FILE
    assert outcome.container_image == "registry.example.test/other/environment:test"
    # The scratch area defaults under the build directory, and the public
    # key travelled — no private key is a field of the request at all.
    assert seen["work_root"] == tmp_path / ".mcuhome-local"
    assert "jobs" not in seen
    assert seen["signing_pub"] == "-----BEGIN PUBLIC KEY-----\n"


def test_a_build_answers_one_document_whichever_target_ran(model, tmp_path, monkeypatch):
    """The verdict is ``ok``, said once, and the document says everything.

    A client renders a build out of this document and never assembles one
    out of fields it read off the object: every key is present, the
    values are JSON, and the composition's own object is not among them.
    """

    def fake(device_model, **kwargs):
        del device_model, kwargs
        return containerbuild.ContainerBuildResult(
            outcome=StepResult(
                action="build",
                context_id="sha256:" + "1" * 64,
                exit_code=0,
                status="success",
                artifacts=_artifacts(),
                out_dir=tmp_path / "delivery",
            ),
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            container_image="registry.example.test/other/environment:test",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    outcome = _run(build.BuildRequest(model=model, out_dir=tmp_path), build.TARGET_LOCAL)
    document = outcome.to_dict()
    assert list(document) == [
        "ok",
        "stopped",
        "target",
        "device",
        "context_id",
        "out_dir",
        "report",
        "container_image",
        "artifacts",
    ]
    assert document["ok"] is True
    assert document["stopped"] is False
    assert document["device"] == model.device.name
    assert document["out_dir"] == str(tmp_path / "delivery")
    assert document["artifacts"] == [entry.to_dict() for entry in _artifacts()]
    # A document, not an object graph: it survives json.dumps, and the
    # composition's own result is deliberately not in it.
    assert json.loads(json.dumps(document)) == document
    assert "detail" not in document
    assert outcome.detail is not None
    # There is no third word for the verdict.
    assert not hasattr(outcome, "status")
    assert not hasattr(outcome, "successful")


@pytest.mark.parametrize(
    ("target", "mode", "says"),
    [
        (build.TARGET_LOCAL, build.MODE_CONTAINER, "--container-image"),
        (build.TARGET_LOCAL, build.MODE_SUBPROCESS, "build.mode container"),
        (build.TARGET_REMOTE, None, "--build-target local"),
    ],
)
def test_an_environment_that_cannot_build_is_unusable_rather_than_failed(
    model, tmp_path, monkeypatch, target, mode, says
) -> None:
    """``unsupported`` says nothing about the firmware, so it is a refusal.

    The specification's word means *no environment of my kind can do
    this*. A caller told "the build failed" would look at its device; a
    caller told the environment is unusable looks for another
    environment, which is the only thing that helps.

    All three ways of running a build answer it — the two local
    executions and the remote target — and each says what the person in
    front of *that* one can do about it: whose environment it was decides
    who can replace it.
    """
    context = tmp_path / "context"
    context.mkdir()

    def container(device_model, **kwargs):
        del device_model, kwargs
        return containerbuild.ContainerBuildResult(
            outcome=_unsupported_step(tmp_path),
            out_dir=tmp_path / "delivery",
            context_dir=context,
            container_image="registry.example.test/other/environment:test",
        )

    def subprocess_build(device_model, **kwargs):
        del device_model, kwargs
        return subprocessbuild.SubprocessBuildResult(
            outcome=_unsupported_step(tmp_path),
            out_dir=tmp_path / "delivery",
            context_dir=context,
            environment=None,
        )

    async def remote(context_dir, **kwargs):
        del context_dir, kwargs
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            status="unsupported",
            artifacts=(),
            out_dir=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(
        build,
        "compose_local_build",
        container if mode != build.MODE_SUBPROCESS else subprocess_build,
    )
    monkeypatch.setattr(sessionclient, "run_remote_build", remote)
    with pytest.raises(EnvironmentUnusable) as refusal:
        _run(
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                mode=mode,
                context_dir=context,
                builder=SelectedBuilder(target=build.TARGET_REMOTE, server="attic"),
            ),
            target,
        )
    assert "build environment" in str(refusal.value)
    # The way out is the one that exists for whoever ran it: a container
    # build picks its image, a subprocess build can move into a
    # container, and a remote build can only come home — the server's
    # environments are its operator's.
    assert says in (refusal.value.hint or "")


def _unsupported_step(tmp_path) -> StepResult:
    """A step that answered ``unsupported``: the environment, not the build."""
    return StepResult(
        action="build",
        context_id="sha256:" + "1" * 64,
        exit_code=1,
        status="unsupported",
        out_dir=tmp_path / "delivery",
    )


def test_a_build_holds_its_build_directory_while_it_runs(model, tmp_path, monkeypatch):
    """Whichever target runs, the directory is taken for its duration.

    The guard sits at the dispatch and not in a target because every
    target writes into the same directory — and the two runs that
    collide need not even be the same program: a command line, a
    dashboard and a future ``device flash`` all reach the files through
    a directory somebody else may be rewriting. What the lock keeps out
    is another *process*, which is what this asserts; a caller that
    already holds the directory (the CLI, across compile and signing)
    nests without refusing itself, pinned in ``test_buildlock.py``.
    """
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen["holder"] = holder_of(tmp_path)
        outcome = StepResult(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "delivery",
        )
        return containerbuild.ContainerBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            container_image="registry.example.test/other/environment:test",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    request = build.BuildRequest(model=model, out_dir=tmp_path)
    assert _run(request, build.TARGET_LOCAL).ok
    holder = seen["holder"]
    assert holder["device"] == model.device.name  # type: ignore[index]
    assert holder["operation"] == "build"  # type: ignore[index]
    # And released again: the next build of that directory just runs.
    assert _run(request, build.TARGET_LOCAL).ok


def test_the_remote_target_answers_in_the_same_shape(model, tmp_path, monkeypatch):
    """``remote``: a server's verdict, in the shape the container path uses."""
    seen: dict[str, object] = {}
    context = tmp_path / "context"
    context.mkdir()

    async def fake(context_dir, **kwargs):
        seen["context_dir"] = context_dir
        seen.update(kwargs)
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    outcome = _run(
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(
                target=build.TARGET_REMOTE,
                server="ws://build.example:8080/session",
                token="a-token",
            ),
            context_dir=context,
        ),
        build.TARGET_REMOTE,
    )
    assert outcome.target == build.TARGET_REMOTE
    assert outcome.ok and not outcome.stopped
    assert outcome.context_id == "sha256:" + "2" * 64
    assert outcome.artifacts == _artifacts()
    assert outcome.out_dir == tmp_path / "out"
    # The same report name as the local target: both are deliveries out of
    # a build container, so one host-side signer reads either.
    assert outcome.report == BUILD_REPORT_FILE
    assert seen["context_dir"] == context
    assert seen["url"] == "ws://build.example:8080/session"
    assert seen["token"] == "a-token"
    assert seen["work_root"] == tmp_path / ".mcuhome-remote"


# --------------------------------------------------------------------------
# What `remote` cannot invent
# --------------------------------------------------------------------------


def test_remote_without_a_server_refuses_naming_both_rungs(model, tmp_path) -> None:
    """The builder ladder, as a refusal: a configured builder, or fully manual.

    There is no default build server and no discovery — the context
    carries the device model, so where it is sent is a decision. The
    refusal therefore names both ways of making it: a configured remote
    builder (with its token's place in the secrets layout) and the
    fully manual ``--build-mode`` rung. Builder selection is read by
    the caller rather than by this package, and is named here anyway:
    this is the text a user reads when nothing is configured, and a
    rung it does not mention is a rung nobody finds.
    """
    with pytest.raises(build.RemoteNotConfigured) as refusal:
        _run(
            build.BuildRequest(model=model, out_dir=tmp_path),
            build.TARGET_REMOTE,
        )
    rendered = str(refusal.value)
    assert "target: remote" in rendered
    assert "--builder attic" in rendered
    assert "build.builder" in rendered
    assert "secrets/builder/attic.yaml" in rendered
    assert "--build-target remote --build-server" in rendered
    assert "--build-server-token" in rendered


def test_remote_refuses_over_a_pin_only_when_nothing_can_resolve_one(model, tmp_path) -> None:
    """The pin is the client's, and so is the refusal — from the resolution itself.

    ``remote`` creates its own context, and a context is
    content-addressed over the SDK package's hash, so the one thing it
    cannot fall back on is "whatever the server has": that would be an
    identity describing a build nobody asked for. Which package it is
    resolves exactly as it does for every other target — the configured
    directories first, the registry second — so the refusal belongs to
    the pin resolution and names what would supply one. Here there is
    neither a directory nor a project to read a registry out of, which is
    the only case left in which a remote build cannot pin anything.

    The refusal is therefore **not** a :class:`RemoteNotConfigured` any
    more: nothing about this target is unconfigured, and a target-level
    guard is exactly what used to refuse the registry-only build the two
    tests below now make.
    """
    with pytest.raises(BuildError) as refusal:
        _run(
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                builder=SelectedBuilder(
                    target=build.TARGET_REMOTE, server="ws://build.example/session"
                ),
            ),
            build.TARGET_REMOTE,
        )
    assert not isinstance(refusal.value, build.RemoteNotConfigured)
    rendered = str(refusal.value)
    assert "--sdk-sources" in rendered
    assert "build.sdk_sources" in rendered


def test_a_remote_build_pins_the_chain_it_read_out_of_the_registry(
    model, tmp_path, monkeypatch
) -> None:
    """No source directory anywhere, and the context still carries three pins.

    This is what a user with nothing configured does: a project, a
    device, and a build server. The chain is resolved from the served
    index alone — the SDK, the build workspace its release requires, and
    the build tools that workspace requires — and the context that goes
    to the server pins all three by name, version and hash. Asserted
    against the index the fixture serves rather than against literals, so
    the test says "what the registry lists is what travelled" and not
    "these hashes".
    """
    served = tmp_path / "served"
    sdk_sha256 = make_package_source(served)
    listed = json.loads((served / "index.json").read_text(encoding="utf-8"))["packages"]
    project_root = tmp_path / "project"
    project_root.mkdir()

    outcome, request, log = _remote_context_of(
        monkeypatch,
        model=model,
        out_dir=tmp_path / "build",
        project_root=project_root,
        registries=_served_by(served),
    )

    assert outcome.ok
    assert (request.sdk.version, request.sdk.sha256) == (SDK_VERSION, sdk_sha256)
    environment = request.build_environment
    for pin, name in (
        (environment.workspace, WORKSPACE_PACKAGE),
        (environment.tools, TOOLS_PACKAGE),
    ):
        entry = listed[name][ENVIRONMENT_VERSION]
        assert (pin.name, pin.version, pin.sha256) == (name, ENVIRONMENT_VERSION, entry["sha256"])
    # And every one of the three came off the registry: each source it
    # was read from is named in the build's own log, which is where this
    # fixture's untrusted registry announces itself.
    read = [
        source
        for source in (KIND_SDK, KIND_WORKSPACE, KIND_TOOLS)
        if any(f"{source} is being read from {served}" in line for line in log)
    ]
    assert read == [KIND_SDK, KIND_WORKSPACE, KIND_TOOLS]


def test_a_configured_source_still_beats_the_registry_for_a_remote_build(
    model, tmp_path, monkeypatch
) -> None:
    """Two tiers, in their order — and the order is not "newest wins".

    The registry serves a *newer* SDK than the directory does, so a build
    that pinned the registry's would be indistinguishable from one that
    simply took the highest version. The configured directory wins
    anyway, which is what "local first" means: a machine that has the
    package never opens a socket, and an operator's own copy is not
    overtaken by a release appearing on a mirror.
    """
    served = tmp_path / "served"
    make_package_source(served, version="0.1.1")
    local = tmp_path / "local"
    local_sha256 = make_package_source(local, version=SDK_VERSION)
    project_root = tmp_path / "project"
    project_root.mkdir()

    _, request, log = _remote_context_of(
        monkeypatch,
        model=model,
        out_dir=tmp_path / "build",
        options=build.BuildOptions(
            sdk_sources=(local,), workspace_sources=(local,), tools_sources=(local,)
        ),
        project_root=project_root,
        registries=_served_by(served),
    )

    assert (request.sdk.version, request.sdk.sha256) == (SDK_VERSION, local_sha256)
    # Not merely "the registry lost": it was never opened, for any of the
    # three stages — an untrusted one would have said so in this log.
    assert not [line for line in log if str(served) in line]


def test_remote_without_the_extra_refuses_with_the_install_line(model, tmp_path, monkeypatch):
    """The transport is optional, so its absence is a sentence, not a traceback.

    ``aiohttp`` is installed in this environment, so its absence is
    simulated where the session client actually looks for it — the one
    import call ``_require`` makes — which is the path a base install
    takes on the first frame it would send.
    """
    context = tmp_path / "context"
    context.mkdir()
    real_import = builtins.__import__

    def without_aiohttp(name, *args, **kwargs):
        if name == "aiohttp":
            raise ImportError("No module named 'aiohttp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_aiohttp)
    with pytest.raises(sessionclient.RemoteDependencyMissing) as refusal:
        _run(
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                builder=SelectedBuilder(
                    target=build.TARGET_REMOTE, server="ws://build.example/session"
                ),
                context_dir=context,
            ),
            build.TARGET_REMOTE,
        )
    assert "pip install 'mcuhome-workbench[remote]'" in str(refusal.value)


# --------------------------------------------------------------------------
# No build reaches for the compiler at all
# --------------------------------------------------------------------------


def test_the_container_target_no_longer_asks_for_the_compiler(model, tmp_path, monkeypatch):
    """The container profile is the workbench's own, so no build needs a compiler.

    This is the point of it living here. A build needs a container
    runtime and nothing else of a toolchain — that was always the claim,
    and while the profile lived in the compiler's own distribution it was
    untrue at the level of installed distributions: a local build refused
    without ``mcuhome-compiler`` even though nothing it ran came from
    that package.

    Asserted by making *every* dynamic import fail and showing that the
    build gets past it: what it stops at instead is one of the things it
    genuinely needs — a container image on this host, an SDK source —
    and which of those comes first depends on the machine, so what is
    asserted is only that the compiler is not among them.
    """
    import importlib

    def refuse(name: str):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(importlib, "import_module", refuse)
    # A docker that answers, badly: the daemon is not running. It stops
    # the build at the first thing it genuinely needs, which is the
    # point — what is asserted below is which refusal it is *not*.
    monkeypatch.setattr(containerbuild, "run_command", lambda argv, on_line=None: Completed(1, ""))
    with pytest.raises(BuildError) as refusal:
        _run(build.BuildRequest(model=model, out_dir=tmp_path), build.TARGET_LOCAL)
    assert "mcuhome-compiler" not in str(refusal.value)


# --------------------------------------------------------------------------
# The dependency arrow, at run time
# --------------------------------------------------------------------------


def test_importing_the_dispatch_does_not_drag_in_the_compiler() -> None:
    """The compiler-optional edge, as the property rather than as syntax.

    ``test_packaging_workbench.py`` asserts no ``import mcuhome.compiler``
    appears in this package's syntax tree; that is the rule, and this is
    what the rule is *for*: a dashboard install that carries no toolchain
    must be able to import the dispatch. Checked in a fresh interpreter,
    because this suite's own imports have long since loaded everything.
    """
    import subprocess
    import sys

    from conftest import REPO_ROOT

    probe = (
        "import sys; import mcuhome.workbench.build; "
        "print(any(name.startswith('mcuhome.compiler') for name in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False", (
        "importing the build-method dispatch loaded mcuhome.compiler — the "
        "edge is optional and must stay resolved at "
        "call time"
    )


# --------------------------------------------------------------------------
# A build server's address, as a URL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "url"),
    [
        # What a builder configuration and --build-server document:
        # "server address (IP/hostname[:port])".
        ("attic", "ws://attic:8100/ws"),
        ("attic:8137", "ws://attic:8137/ws"),
        ("127.0.0.1:8137", "ws://127.0.0.1:8137/ws"),
        ("[::1]:8137", "ws://[::1]:8137/ws"),
        ("[::1]", "ws://[::1]:8100/ws"),
        # Written out in full, which is what somebody who has read the
        # server's log line will paste.
        ("ws://attic:8137/ws", "ws://attic:8137/ws"),
        ("wss://build.example:443/ws", "wss://build.example:443/ws"),
        # From a browser's address bar, where the same server is http.
        ("http://attic:8137/ws", "ws://attic:8137/ws"),
        ("https://build.example/ws", "wss://build.example:8100/ws"),
        ("  attic:8137  ", "ws://attic:8137/ws"),
    ],
)
def test_a_server_address_becomes_a_websocket_url(address, url) -> None:
    """``host:port`` is what people write and a URL is what sockets need.

    Nothing bridged the two: the address went verbatim into the connect
    call, where ``attic:8137`` reads as a URL whose scheme is ``attic``
    and aiohttp raised — a traceback out of a documented input.
    """
    assert build.websocket_url(address) == url


@pytest.mark.parametrize("address", ["", "   ", "://attic", "ftp://attic", "ssh://attic:22"])
def test_an_address_that_is_not_one_is_refused_in_words(address) -> None:
    """A refusal naming what is wrong, never a traceback from the socket."""
    with pytest.raises(build.RemoteNotConfigured) as refusal:
        build.websocket_url(address)
    assert refusal.value.hint
    assert "<host" in refusal.value.hint


def west_workspace(root: Path) -> Path:
    """A west workspace as a developer keeps one, minus everything else.

    Enough for the one check a development build makes: a
    ``.west/config`` naming a manifest repository, and that repository
    checked out. What west would answer about the layers is not asked
    here — that happens when a build actually runs.
    """
    (root / ".west").mkdir(parents=True)
    (root / ".west" / "config").write_text(
        "[manifest]\npath = mcuhome-sdk\nfile = west.yml\n", encoding="utf-8"
    )
    (root / "mcuhome-sdk").mkdir()
    return root


def test_the_development_workspace_reaches_the_execution(model, tmp_path) -> None:
    """``build.dev_workspace`` is read where every other target-specific
    field is read, and lands on the target."""
    target = build.build_target_for(
        build.TARGET_LOCAL,
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            mode=build.MODE_SUBPROCESS,
            options=build.BuildOptions(dev_workspace=tmp_path / "west-workspace"),
        ),
    )
    assert target.execution.dev_workspace == tmp_path / "west-workspace"


def test_a_development_workspace_is_refused_for_a_container_build(model, tmp_path) -> None:
    """The two settings contradict each other and neither can be honoured
    halfway.

    A development build runs on this machine with the person's own tools;
    a container build runs a fixed image that has neither their tools nor
    their workspace. The refusal names both settings and says which one
    to change.
    """
    with pytest.raises(ConfigError, match="development workspace") as refusal:
        build.build_target_for(
            build.TARGET_LOCAL,
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                mode=build.MODE_CONTAINER,
                options=build.BuildOptions(dev_workspace=tmp_path / "west-workspace"),
            ),
        )
    assert "build.mode subprocess" in refusal.value.hint
    assert "build.dev_workspace" in refusal.value.hint


def test_a_development_workspace_is_refused_for_a_remote_build(model, tmp_path) -> None:
    """A build server has neither your workspace nor your tools.

    And the context a development build writes names no environment a
    server could resolve one from, so the setting cannot be honoured
    there — nor quietly dropped, because a build that ignored it would
    compile the pinned packages and look exactly like the build the
    person meant.
    """
    with pytest.raises(ConfigError, match="development workspace") as refusal:
        build.build_target_for(
            build.TARGET_REMOTE,
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                builder=SelectedBuilder(target=build.TARGET_REMOTE, server="build.example.org"),
                options=build.BuildOptions(dev_workspace=tmp_path / "west-workspace"),
            ),
        )
    assert "build.dev_workspace" in refusal.value.hint
    assert str(tmp_path / "west-workspace") in refusal.value.message


def test_a_development_workspace_reaches_the_composition(model, tmp_path, monkeypatch) -> None:
    """The workspace a developer stated becomes the environment the
    subprocess composition runs against, in place of the store's entries."""
    workspace = west_workspace(tmp_path / "west-workspace")

    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        return subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(build, "compose_subprocess_build", fake)
    asyncio.run(
        build.build_firmware(
            build.BuildRequest(model=model, out_dir=tmp_path),
            target=build.LocalBuild(execution=build.SubprocessExecution(dev_workspace=workspace)),
        )
    )
    environment = seen["environment"]
    assert environment.developer is True
    assert environment.workspace.path == workspace
    assert environment.sdk == workspace / "mcuhome-sdk"
    assert environment.tools is None


def test_a_development_build_writes_a_context_that_names_no_environment(model, tmp_path) -> None:
    """The context a development build creates, and the two fields that
    make it one.

    There is no package set to pin — the sources are a checkout and the
    tools are whatever is on a ``PATH`` — so the format's second form is
    written instead: the word, and the empty SDK hash that travels with
    it. No index is read and no archive is fetched to write it, which is
    why no source directory is given here.
    """
    create_build_context(
        model,
        out_dir=tmp_path / "context",
        work_root=tmp_path / "made",
        sdk_sources=(),
        signing_pub=_PUBLIC_PEM,
        developer=True,
    )
    written = (tmp_path / "context" / "context.yaml").read_text(encoding="utf-8")
    assert "build_environment: developer" in written

    request = read_context_request(tmp_path / "context" / "context.yaml")
    assert isinstance(request.build_environment, DeveloperEnvironment)
    assert request.sdk.sha256 == ""


def test_a_development_context_has_the_same_id_from_two_workspaces(
    model, tmp_path, monkeypatch
) -> None:
    """What such a context identifies, driven through the composition twice.

    Its ID covers the files, the board and the word — never the bytes it
    was compiled against, because there are none it could name. Two
    developers building the same device out of two different workspaces
    therefore end up with one identity over two firmware images, which is
    exactly why the format calls this form neither reproducible nor
    remote-buildable. Proven by building the contexts the way a build
    builds them, from two workspaces, rather than by creating one twice.
    """

    def fake(context_dir, *, environment, **kwargs):
        return subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=environment,
        )

    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake)
    identities = []
    for name in ("first", "second"):
        workspace = west_workspace(tmp_path / f"{name}-workspace")
        build.compose_subprocess_build(
            model,
            sdk_sources=(),
            work_root=tmp_path / name,
            env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
            signing_pub=_PUBLIC_PEM,
            created=datetime(2026, 9, 7, tzinfo=UTC),
            environment=subprocessbuild.environment_from_workspace(workspace),
        )
        identities.append(read_context_manifest(tmp_path / name / "context" / "manifest.yaml").id)
    assert identities[0] == identities[1]


def test_a_pinned_context_is_not_built_against_a_workspace(model, tmp_path, monkeypatch) -> None:
    """A context somebody pinned, handed to a build that compiles a checkout.

    Building it would produce firmware whose own context says it was
    compiled from packages it never saw, which is the one thing a pin is
    for. Neither half can be honoured, so the build stops and names both.
    """
    context = tmp_path / "context"
    make_package_source(tmp_path / "sdk")
    create_build_context(
        model,
        out_dir=context,
        work_root=tmp_path / "made",
        sdk_sources=(tmp_path / "sdk",),
        workspace_sources=(tmp_path / "sdk",),
        tools_sources=(tmp_path / "sdk",),
        signing_pub=_PUBLIC_PEM,
    )
    workspace = west_workspace(tmp_path / "west-workspace")
    monkeypatch.setattr(
        subprocessbuild,
        "run_locked_build",
        lambda *a, **k: pytest.fail("the backend must not be reached"),
    )
    with pytest.raises(EnvironmentUnavailable, match="pinned to") as refused:
        build.compose_subprocess_build(
            model,
            sdk_sources=(tmp_path / "sdk",),
            work_root=tmp_path / "work",
            env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
            signing_pub=_PUBLIC_PEM,
            context_dir=context,
            environment=subprocessbuild.environment_from_workspace(workspace),
        )
    assert str(workspace) in refused.value.message
    assert "build.dev_workspace" in refused.value.hint
    assert not (context / "manifest.yaml").exists()


@pytest.mark.parametrize(
    ("key", "reference"),
    [
        ("sdk", "sdk/mcuhome-sdk:0.1.0"),
        ("build_workspace", "build-workspace/mcuhome-build-workspace:0.1.0"),
        ("build_tools", "build-tools/mcuhome-build-tools:0.1.0"),
    ],
)
def test_a_device_that_pins_a_package_is_refused_in_a_development_build(
    model, tmp_path, key, reference
) -> None:
    """Every ``sources.*`` entry names a package to fetch, and this build
    fetches nothing.

    Honouring one would fetch a package nothing then builds; ignoring it
    would silently build something other than what the device says. So
    the build stops, and says which of the two to drop.
    """
    pinned = replace(model, sources=replace(model.sources, **{key: reference}))
    with pytest.raises(BuildError, match=f"sources.{key}") as refused:
        create_build_context(
            pinned,
            out_dir=tmp_path / "context",
            work_root=tmp_path / "made",
            sdk_sources=(),
            signing_pub=_PUBLIC_PEM,
            developer=True,
        )
    assert "build.dev_workspace" in refused.value.hint
    assert not (tmp_path / "context").exists()


def test_a_development_context_is_not_sent_to_a_build_server(model, tmp_path) -> None:
    """Refused before the upload, not after it.

    A server finds an environment by the packages a context pins, and
    this one pins none — so it could only refuse it, and it would refuse
    it after a gigabyte had crossed the network.
    """
    context = tmp_path / "context"
    create_build_context(
        model,
        out_dir=context,
        work_root=tmp_path / "made",
        sdk_sources=(),
        signing_pub=_PUBLIC_PEM,
        developer=True,
    )
    with pytest.raises(build.RemoteNotConfigured, match="development build") as refused:
        asyncio.run(
            build.build_firmware(
                build.BuildRequest(
                    model=model,
                    out_dir=tmp_path,
                    context_dir=context,
                    builder=SelectedBuilder(target=build.TARGET_REMOTE, server="build.example.org"),
                ),
                target=build.RemoteBuild(server="build.example.org"),
            )
        )
    assert "mcuhome build" in refused.value.hint


def test_a_package_pinned_context_is_sent_as_before(model, tmp_path, monkeypatch) -> None:
    """The other side of that refusal: an ordinary context still travels."""
    context = tmp_path / "context"
    make_package_source(tmp_path / "sdk")
    create_build_context(
        model,
        out_dir=context,
        work_root=tmp_path / "made",
        sdk_sources=(tmp_path / "sdk",),
        workspace_sources=(tmp_path / "sdk",),
        tools_sources=(tmp_path / "sdk",),
        signing_pub=_PUBLIC_PEM,
    )
    # Reached: the refusal is about the form of the context and about
    # nothing else, so a pinned one gets as far as the socket.
    build._refuse_developer_context(context)


def test_a_patched_context_is_refused_before_the_context_is_locked(
    model, tmp_path, monkeypatch
) -> None:
    """The refusal comes before anything the build would have changed.

    Locking writes into a directory the user keeps, so a dev-mode build
    that is going to be refused for its patches must not have written the
    lock into that directory first — nor fetched an SDK, nor talked to a
    registry.
    """
    workspace = west_workspace(tmp_path / "west-workspace")
    context = tmp_path / "context"
    (context / "patches" / "zephyr").mkdir(parents=True)
    (context / "patches" / "zephyr" / "0001-fix.patch").write_text("--- a\n+++ b\n")

    locked: list[Path] = []
    monkeypatch.setattr(build, "lock_context", lambda directory: locked.append(directory))
    monkeypatch.setattr(
        subprocessbuild,
        "run_locked_build",
        lambda *arguments, **keywords: pytest.fail("the backend must not be reached"),
    )

    with pytest.raises(subprocessbuild.BuildEnvironmentError, match="cannot apply them"):
        asyncio.run(
            build.build_firmware(
                build.BuildRequest(model=model, out_dir=tmp_path, context_dir=context),
                target=build.LocalBuild(
                    execution=build.SubprocessExecution(dev_workspace=workspace)
                ),
            )
        )
    assert locked == []
    assert not (context / "manifest.yaml").exists()
    assert not (tmp_path / ".mcuhome-local").exists()


def test_a_remote_build_records_the_environment_that_ran_it(model, tmp_path, monkeypatch):
    """What built it, in the same form a local container build records.

    A context pins packages, and an image is one delivery of that set:
    which delivery ran is the server's choice and the server's answer, so
    without carrying it back a remote build's record would name the
    packages and never the bytes. Here the server named no tag, so the
    pair recorded is ``<repository>@sha256:…``.
    """
    digest = "sha256:" + "f" * 64
    context = tmp_path / "context"
    context.mkdir()

    async def fake(context_dir, **kwargs):
        del context_dir, kwargs
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "out",
            invocation_id="inv-1",
            container_image=f"ghcr.io/mcu-home/build-environment@{digest}",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    outcome = _run(
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(
                target=build.TARGET_REMOTE,
                server="ws://build.example:8080/session",
                token="a-token",
            ),
            context_dir=context,
        ),
        build.TARGET_REMOTE,
    )
    assert outcome.container_image == f"ghcr.io/mcu-home/build-environment@{digest}"


def test_a_remote_build_carries_the_image_pin_to_the_server(model, tmp_path, monkeypatch):
    """``--container-image`` on a remote build reaches the far side.

    The pin is a statement about *this build* and the server is the side
    that resolves it, so a remote build that dropped it would compile in
    an environment other than the one it was told to — silently, which is
    the one outcome a pin exists to prevent. Which repositories may be
    used at all is not carried: that stays the server operator's.
    """
    seen: dict[str, object] = {}
    context = tmp_path / "context"
    context.mkdir()

    async def fake(context_dir, **kwargs):
        del context_dir
        seen.update(kwargs)
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="sha256:" + "2" * 64,
            status="success",
            artifacts=_artifacts(),
            out_dir=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    _run(
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(
                target=build.TARGET_REMOTE,
                server="ws://build.example:8080/session",
                token="a-token",
            ),
            context_dir=context,
            container_image=":0.1.10.dev2-r1",
        ),
        build.TARGET_REMOTE,
    )
    assert seen["image"] == ":0.1.10.dev2-r1"


# --------------------------------------------------------------------------
# The device's own image pin (``sources.container_image``)
# --------------------------------------------------------------------------


def test_the_device_pin_answers_where_no_invocation_named_one(model) -> None:
    """Two statements can name an image, and the more specific one wins."""
    plain = replace(model, sources=replace(model.sources, container_image=None))
    pinned = replace(model, sources=replace(model.sources, container_image=":0.1.10.dev2-r1"))
    assert build.image_pin(plain, None) is None
    assert build.image_pin(plain, "@sha256:" + "a" * 64) == "@sha256:" + "a" * 64
    assert build.image_pin(pinned, None) == ":0.1.10.dev2-r1"
    assert build.image_pin(pinned, "localhost/other:wip") == "localhost/other:wip"


def test_a_container_build_resolves_the_pin_the_device_carries(
    model, tmp_path, monkeypatch
) -> None:
    """The persistent pin reaches the image search, and the flag overrides it.

    The search is where a pin means anything — it narrows which images
    are looked at — so this asserts the value that arrives there, once
    for a device that carries one and once for a build that named its
    own.
    """
    seen: list[str | None] = []

    def resolve(pin, **kwargs):
        seen.append(kwargs["image_pin"])
        raise BuildError("stopped after the pin was read", hint="nothing to fix")

    monkeypatch.setattr(containerbuild, "prepare_environment", resolve)
    pinned = replace(
        model,
        sources=replace(model.sources, container_image="ghcr.io/mcu-home/build-environment"),
    )
    make_package_source(tmp_path / "sdk")
    for index, image in enumerate((None, ":wip")):
        with pytest.raises(BuildError, match="stopped after the pin was read"):
            build.compose_container_build(
                pinned,
                sdk_sources=(tmp_path / "sdk",),
                work_root=tmp_path / f"work-{index}",
                env={},
                signing_pub=_PUBLIC_PEM,
                options=build.BuildOptions(
                    workspace_sources=(tmp_path / "sdk",), tools_sources=(tmp_path / "sdk",)
                ),
                container_image=image,
            )
    assert seen == ["ghcr.io/mcu-home/build-environment", ":wip"]


def test_a_remote_build_carries_the_device_pin_as_well(model, tmp_path) -> None:
    """The far side is told what the device pins, not only what a flag said."""
    pinned = replace(model, sources=replace(model.sources, container_image=":0.1.10.dev2-r1"))
    target = build.build_target_for(
        build.TARGET_REMOTE,
        build.BuildRequest(
            model=pinned,
            out_dir=tmp_path,
            builder=SelectedBuilder(target=build.TARGET_REMOTE, server="attic"),
        ),
    )
    assert target.container_image == ":0.1.10.dev2-r1"
    stated = build.build_target_for(
        build.TARGET_REMOTE,
        build.BuildRequest(
            model=pinned,
            out_dir=tmp_path,
            builder=SelectedBuilder(target=build.TARGET_REMOTE, server="attic"),
            container_image="@sha256:" + "b" * 64,
        ),
    )
    assert stated.container_image == "@sha256:" + "b" * 64


def test_a_subprocess_build_says_the_pin_has_no_effect_rather_than_refusing(
    model, tmp_path, monkeypatch
) -> None:
    """A build without a container has no image to pin, and says so once.

    Refusing would be wrong: the same device builds in a container on the
    next machine, and the pin is a statement about that delivery. Silence
    would be worse than either — the person pinned an image and would
    never learn that this build used none.
    """
    said: list[str] = []

    def stop(*args, **kwargs):
        raise BuildError("stopped after the note", hint="nothing to fix")

    monkeypatch.setattr(subprocessbuild, "environment_from_pins", stop)
    make_package_source(tmp_path / "sdk")
    pinned = replace(model, sources=replace(model.sources, container_image=":0.1.10.dev2-r1"))
    with pytest.raises(BuildError, match="stopped after the note"):
        build.compose_subprocess_build(
            pinned,
            sdk_sources=(tmp_path / "sdk",),
            work_root=tmp_path / "work",
            env={},
            signing_pub=_PUBLIC_PEM,
            options=build.BuildOptions(
                workspace_sources=(tmp_path / "sdk",), tools_sources=(tmp_path / "sdk",)
            ),
            on_line=said.append,
        )
    assert any(":0.1.10.dev2-r1" in line and "no effect" in line for line in said)


def test_an_image_named_for_this_build_is_refused_without_a_container(model, tmp_path) -> None:
    """``--container-image`` is a statement about *this* build, so it stops it.

    The flag names an image for the build that is running now. Dropping
    it would compile against something other than what was asked for, so
    the two statements are put to the person instead of one of them being
    honoured halfway.
    """
    with pytest.raises(ConfigError) as refused:
        build.build_target_for(
            build.TARGET_LOCAL,
            build.BuildRequest(
                model=model,
                out_dir=tmp_path,
                mode=build.MODE_SUBPROCESS,
                container_image=":0.1.10.dev2-r1",
            ),
        )
    assert ":0.1.10.dev2-r1" in str(refused.value)
    assert "build.mode" in refused.value.hint


def test_a_configured_builders_image_is_a_note_and_not_a_refusal(
    model, tmp_path, monkeypatch
) -> None:
    """A builder's ``image:`` describes the machine, not this build.

    Refusing over it would refuse *every* build of a machine that is
    configured to build without a container, which is a configuration
    question and not a statement about the job in hand. So the target is
    built, the image travels as something to say once, and the
    composition says it.
    """
    target = build.build_target_for(
        build.TARGET_LOCAL,
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            mode=build.MODE_SUBPROCESS,
            builder=SelectedBuilder(
                target=build.TARGET_LOCAL,
                container_image="ghcr.io/mcu-home/build-environment:0.1.10.dev2-r1",
            ),
        ),
    )
    assert isinstance(target.execution, build.SubprocessExecution)
    assert (
        target.execution.stated_container_image
        == "ghcr.io/mcu-home/build-environment:0.1.10.dev2-r1"
    )

    said: list[str] = []

    def stop(*args, **kwargs):
        raise BuildError("stopped after the note", hint="nothing to fix")

    monkeypatch.setattr(subprocessbuild, "environment_from_pins", stop)
    make_package_source(tmp_path / "sdk")
    with pytest.raises(BuildError, match="stopped after the note"):
        build.compose_subprocess_build(
            replace(model, sources=replace(model.sources, container_image=None)),
            sdk_sources=(tmp_path / "sdk",),
            work_root=tmp_path / "work",
            env={},
            signing_pub=_PUBLIC_PEM,
            options=build.BuildOptions(
                workspace_sources=(tmp_path / "sdk",), tools_sources=(tmp_path / "sdk",)
            ),
            on_line=said.append,
            stated_container_image="ghcr.io/mcu-home/build-environment:0.1.10.dev2-r1",
        )
    assert any("0.1.10.dev2-r1" in line and "no effect" in line for line in said)


def test_the_note_names_the_more_specific_of_the_two_statements(
    model, tmp_path, monkeypatch
) -> None:
    """A builder's image and a device pin can both be there; one is named.

    The one named is the one that would have won had a container run —
    otherwise the note would tell the person about an image the build
    would not have used anyway.
    """
    said: list[str] = []

    def stop(*args, **kwargs):
        raise BuildError("stopped after the note", hint="nothing to fix")

    monkeypatch.setattr(subprocessbuild, "environment_from_pins", stop)
    make_package_source(tmp_path / "sdk")
    pinned = replace(model, sources=replace(model.sources, container_image=":device-pin"))
    with pytest.raises(BuildError, match="stopped after the note"):
        build.compose_subprocess_build(
            pinned,
            sdk_sources=(tmp_path / "sdk",),
            work_root=tmp_path / "work",
            env={},
            signing_pub=_PUBLIC_PEM,
            options=build.BuildOptions(
                workspace_sources=(tmp_path / "sdk",), tools_sources=(tmp_path / "sdk",)
            ),
            on_line=said.append,
            stated_container_image=":builder-image",
        )
    notes = [line for line in said if "no effect" in line]
    assert notes and ":builder-image" in notes[0]
    assert not any(":device-pin" in line for line in notes)


def test_a_container_build_takes_the_builders_image_and_the_flag_beats_it(model, tmp_path) -> None:
    """Where a container does run, both statements are pins and the flag wins."""
    from_builder = build.build_target_for(
        build.TARGET_LOCAL,
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(target=build.TARGET_LOCAL, container_image=":from-the-builder"),
        ),
    )
    assert from_builder.execution.container_image == ":from-the-builder"
    from_flag = build.build_target_for(
        build.TARGET_LOCAL,
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(target=build.TARGET_LOCAL, container_image=":from-the-builder"),
            container_image=":from-this-build",
        ),
    )
    assert from_flag.execution.container_image == ":from-this-build"
    remote = build.build_target_for(
        build.TARGET_REMOTE,
        build.BuildRequest(
            model=model,
            out_dir=tmp_path,
            builder=SelectedBuilder(
                target=build.TARGET_REMOTE, server="attic", container_image=":from-the-builder"
            ),
        ),
    )
    assert remote.container_image == ":from-the-builder"


def test_a_development_build_refuses_the_pin_and_notes_nothing(model, tmp_path) -> None:
    """A development build starts no container, so no image can name it.

    Driven the way a person drives it — a workspace on the request, the
    whole composition underneath — because the two halves of this answer
    live in two places: the note that says a pin has no effect is the
    subprocess composition's, and the refusal is the context writer's.
    A build that printed the note and then refused over the same pin
    would have told the person the opposite of what happened.
    """
    workspace = west_workspace(tmp_path / "west-workspace")
    pinned = replace(model, sources=replace(model.sources, container_image=":0.1.10.dev2-r1"))
    said: list[str] = []
    with pytest.raises(BuildError, match="sources.container_image") as refused:
        asyncio.run(
            build.build_firmware(
                build.BuildRequest(
                    model=pinned,
                    out_dir=tmp_path / "out",
                    signing_pub=_PUBLIC_PEM,
                    on_line=said.append,
                ),
                target=build.LocalBuild(
                    execution=build.SubprocessExecution(dev_workspace=workspace)
                ),
            )
        )
    assert "build.dev_workspace" in refused.value.hint
    assert not any("no effect" in line for line in said)


def test_a_subprocess_build_prints_the_override_note_the_container_one_prints(
    model, tmp_path, monkeypatch
) -> None:
    """The note travels in both profiles, or it is not a guarantee.

    A device may pin an environment package outside what the resolved SDK
    declares; that is built and said out loud rather than refused. The
    line reaches the build log only if the composition hands the context
    creation somewhere to say it — which the subprocess profile once did
    not, and which is the profile the sdk's own CI builds in.
    """

    def fake_run(context_dir, **kwargs):
        return subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake_run)
    monkeypatch.setattr(build, "lock_context", lambda directory: None)
    monkeypatch.setattr(subprocessbuild, "check_environment", lambda environment, **facts: None)

    class FakeEnvironment:
        developer = False

        def described(self) -> str:
            return "mcuhome-build-workspace 0.9.0"

    source = tmp_path / "sdk"
    make_package_source(source)
    _publish_workspace(source, "0.9.0")
    pinned = replace(
        model,
        sources=replace(
            model.sources, build_workspace="build-workspace/mcuhome-build-workspace:0.9.0"
        ),
    )
    lines: list[str] = []
    build.compose_subprocess_build(
        pinned,
        sdk_sources=(source,),
        work_root=tmp_path / "work",
        env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        signing_pub=_PUBLIC_PEM,
        options=build.BuildOptions(workspace_sources=(source,), tools_sources=(source,)),
        environment=FakeEnvironment(),
        on_line=lines.append,
    )
    note = [line for line in lines if line.startswith("Note: ")]
    assert len(note) == 1
    assert "sources.build_workspace" in note[0]
    assert "0.9.0" in note[0]
    manifest = read_context_request(tmp_path / "work" / "context" / "context.yaml")
    assert manifest.build_environment.workspace.version == "0.9.0"


def _publish_workspace(directory: Path, version: str) -> None:
    """A second build workspace release in a source directory, sidecar and all."""
    import hashlib
    import json

    from conftest import ENVIRONMENT_CONSTRAINT, TOOLS_PACKAGE, WORKSPACE_PACKAGE, package_meta

    payload = f"{WORKSPACE_PACKAGE} {version}\n".encode()
    filename = f"{WORKSPACE_PACKAGE}-{version}.tar.zst"
    (directory / filename).write_bytes(payload)
    meta = package_meta(
        WORKSPACE_PACKAGE, version, requires={TOOLS_PACKAGE: ENVIRONMENT_CONSTRAINT}
    )
    (directory / f"{filename}.meta.json").write_bytes(meta)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    index["packages"][WORKSPACE_PACKAGE][version] = {
        "file": filename,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "meta_file": {
            "file": f"{filename}.meta.json",
            "sha256": hashlib.sha256(meta).hexdigest(),
            "size": len(meta),
        },
    }
    (directory / "index.json").write_text(json.dumps(index), encoding="utf-8")


def test_the_configured_cache_root_reaches_the_subprocess_profile(
    model, tmp_path, monkeypatch
) -> None:
    """`build.cache_root` is the operator's key, and both profiles obey it.

    The container profile reads it where it resolves the cache; this one
    resolves the cache the same way and must therefore read the same
    key. It once read a per-build override alone, so a machine that had
    moved its compiler cache kept it for container builds and silently
    lost it for `build.mode = subprocess`.
    """
    seen: dict[str, object] = {}

    def fake_run(context_dir, **kwargs):
        seen.update(kwargs)
        return subprocessbuild.SubprocessBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake_run)
    monkeypatch.setattr(build, "lock_context", lambda directory: None)
    monkeypatch.setattr(subprocessbuild, "check_environment", lambda environment, **facts: None)

    class FakeEnvironment:
        developer = False

        def described(self) -> str:
            return "mcuhome-build-workspace 0.9.0"

    source = tmp_path / "sdk"
    make_package_source(source)
    build.compose_subprocess_build(
        model,
        sdk_sources=(source,),
        work_root=tmp_path / "work",
        env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        signing_pub=_PUBLIC_PEM,
        options=build.BuildOptions(
            workspace_sources=(source,),
            tools_sources=(source,),
            cache_root=tmp_path / "operators-disk",
        ),
        environment=FakeEnvironment(),
    )
    assert seen["cache_root"] == tmp_path / "operators-disk"
    assert seen["tiers"]["local"].path == tmp_path / "operators-disk" / "cache-local"
